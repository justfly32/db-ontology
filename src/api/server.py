"""
FastAPI REST API Server
온톨로지 그래프 조회/검색/관계 관리 API (RBAC 인증 포함)
"""

import os
import sys
import json
from typing import Optional
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

os.environ["PYTHONIOENCODING"] = "utf-8"
sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
sys.stderr.reconfigure(encoding="utf-8") if hasattr(sys.stderr, "reconfigure") else None

import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from collector.db_adapter import MetadataStore, SchemaCollector
from analyzer.relationship_analyzer import RelationshipOrchestrator
from ontology.graph_builder import OntologyGraph
from collector.drift_detector import SchemaDriftDetector, ChangeHistoryManager
from visualizer.dashboard import DashboardHTMLBuilder, DashboardDataProvider
from analyzer.insight_engine import InsightEngine
from api.auth import (
    rbac, get_user_store, get_jwt_manager, create_default_admin,
    Role, User,
)

load_dotenv()

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "output")


# ── 앱 초기화 ───────────────────────────────────────────

app = FastAPI(
    title="DB Ontology API",
    description="데이터베이스 온톨로지 관계 분석 REST API (RBAC)",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 전역 저장소
store = MetadataStore()
graph = OntologyGraph()

# 기본 관리자 생성 (앱 시작 시)
create_default_admin()

os.makedirs(OUTPUT_DIR, exist_ok=True)


# ── Pydantic 모델 ──────────────────────────────────────

class DatabaseConfig(BaseModel):
    db_type: str
    host: Optional[str] = "localhost"
    port: Optional[int] = None
    database: str = ""
    user: Optional[str] = ""
    password: Optional[str] = ""
    db_name: Optional[str] = ""

class PipelineRequest(BaseModel):
    selected_tables: list[str]
    mode: str = "first"

class FieldLookupRequest(BaseModel):
    field_name: str

class RelationshipVerify(BaseModel):
    verified: bool = True
    notes: Optional[str] = ""

class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    email: str
    password: str
    role: str = "viewer"


# ── RBAC 의존성 ─────────────────────────────────────────

async def get_current_user(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> User:
    """인증 미들웨어 — JWT 또는 API Key"""
    try:
        return rbac.get_current_user(authorization=authorization, x_api_key=x_api_key)
    except PermissionError:
        raise HTTPException(status_code=401, detail="Invalid credentials")

def require(permission: str):
    """권한 요청 의존성 팩토리"""
    async def checker(user: User = Depends(get_current_user)) -> User:
        try:
            rbac.require_permission(user, permission)
            return user
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
    return checker


# ── 인증 API ─────────────────────────────────────────────

@app.post("/api/auth/login")
def login(body: LoginRequest):
    """로그인 → JWT 토큰 발급"""
    store_us = get_user_store()
    user = store_us.authenticate(body.username, body.password)
    if not user:
        raise HTTPException(401, "잘못된 사용자명 또는 비밀번호")
    jwt_mgr = get_jwt_manager()
    token = jwt_mgr.create_token(user)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "user": {
            "id": user.user_id,
            "username": user.username,
            "role": user.role.value,
        },
    }

@app.get("/api/auth/me")
def get_me(user: User = Depends(get_current_user)):
    """현재 사용자 정보"""
    return {
        "id": user.user_id,
        "username": user.username,
        "email": user.email,
        "role": user.role.value,
        "api_key": user.api_key[:8] + "..." if user.api_key else "",
    }

@app.post("/api/auth/users")
def create_user(
    body: CreateUserRequest,
    user: User = Depends(require("user:manage")),
):
    """새 사용자 생성 (admin only)"""
    store_us = get_user_store()
    try:
        role = Role(body.role)
    except ValueError:
        raise HTTPException(400, f"Invalid role: {body.role}. Allowed: {[r.value for r in Role]}")
    new_user = store_us.create_user(
        username=body.username,
        email=body.email,
        password=body.password,
        role=role,
    )
    return {
        "id": new_user.user_id,
        "username": new_user.username,
        "role": new_user.role.value,
        "api_key": new_user.api_key,
    }

@app.get("/api/auth/users")
def list_users(user: User = Depends(require("user:manage"))):
    """사용자 목록 (admin only)"""
    store_us = get_user_store()
    return [
        {
            "id": u.user_id,
            "username": u.username,
            "email": u.email,
            "role": u.role.value,
            "is_active": u.is_active,
            "created_at": u.created_at,
        }
        for u in store_us.list_users()
    ]


# ── API 엔드포인트 ───────────────────────────────────────

@app.get("/api/overview")
def get_overview():
    """전체 요약 통계"""
    cur = store.conn.cursor()
    return {
        "databases": cur.execute("SELECT COUNT(*) FROM databases").fetchone()[0],
        "tables": cur.execute("SELECT COUNT(*) FROM tables").fetchone()[0],
        "columns": cur.execute("SELECT COUNT(*) FROM columns").fetchone()[0],
        "relationships": cur.execute("SELECT COUNT(*) FROM relationships").fetchone()[0],
        "fk_count": cur.execute("SELECT COUNT(*) FROM relationships WHERE relation_type='FK'").fetchone()[0],
        "verified": cur.execute("SELECT COUNT(*) FROM relationships WHERE verified=1").fetchone()[0],
    }


@app.get("/api/databases")
def list_databases():
    """데이터베이스 목록"""
    cur = store.conn.cursor()
    cur.execute("SELECT id, name, db_type, host, database_name, description FROM databases")
    return [dict(zip(["id","name","db_type","host","database","description"], r)) for r in cur.fetchall()]


@app.get("/api/tables")
def list_tables(database_id: Optional[int] = None):
    """테이블 목록 (중복 제거)"""
    cur = store.conn.cursor()
    if database_id:
        cur.execute("""
            SELECT MIN(t.id), t.table_name, t.schema_name, t.row_count, t.description, d.name
            FROM tables t JOIN databases d ON t.database_id = d.id
            WHERE t.database_id = ?
            GROUP BY t.table_name, t.schema_name, t.database_id
            ORDER BY t.table_name
        """, (database_id,))
    else:
        cur.execute("""
            SELECT MIN(t.id), t.table_name, t.schema_name, t.row_count, t.description, d.name
            FROM tables t JOIN databases d ON t.database_id = d.id
            GROUP BY t.table_name, t.schema_name, t.database_id
            ORDER BY t.table_name
        """)
    return [dict(zip(["id","name","schema","rows","description","database"], r)) for r in cur.fetchall()]

class BatchDeleteRequest(BaseModel):
    ids: list[int]

@app.delete("/api/tables/batch")
def batch_delete_tables(body: BatchDeleteRequest):
    """테이블 일괄 삭제"""
    if not body.ids:
        raise HTTPException(400, "삭제할 테이블 ID가 없습니다")
    cur = store.conn.cursor()
    placeholders = ",".join("?" * len(body.ids))
    cur.execute(f"DELETE FROM relationships WHERE source_column_id IN (SELECT id FROM columns WHERE table_id IN ({placeholders})) OR target_column_id IN (SELECT id FROM columns WHERE table_id IN ({placeholders}))", body.ids + body.ids)
    cur.execute(f"DELETE FROM columns WHERE table_id IN ({placeholders})", body.ids)
    cur.execute(f"DELETE FROM tables WHERE id IN ({placeholders})", body.ids)
    store.conn.commit()
    return {"status": "ok", "deleted_count": len(body.ids)}

@app.get("/api/tables/{table_id}")
def get_table_detail(table_id: int):
    """테이블 상세 (컬럼 + 관계 포함)"""
    cur = store.conn.cursor()
    cur.execute("""
        SELECT t.table_name, t.schema_name, t.description, t.row_count, d.name
        FROM tables t JOIN databases d ON t.database_id = d.id WHERE t.id = ?
    """, (table_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "테이블 없음")

    table = {"name": row[0], "schema": row[1], "description": row[2], "rows": row[3], "database": row[4]}

    cur.execute("""
        SELECT id, column_name, data_type, is_nullable, is_primary_key,
               is_foreign_key, fk_references, description
        FROM columns WHERE table_id = ? ORDER BY ordinal_position
    """, (table_id,))
    table["columns"] = [dict(zip(
        ["id","name","type","nullable","is_pk","is_fk","fk_ref","description"], r
    )) for r in cur.fetchall()]

    cur.execute("""
        SELECT r.id, r.relation_type, r.confidence, r.detected_by, r.notes,
               c1.column_name, t1.table_name, c2.column_name, t2.table_name
        FROM relationships r
        JOIN columns c1 ON r.source_column_id = c1.id
        JOIN tables t1 ON c1.table_id = t1.id
        JOIN columns c2 ON r.target_column_id = c2.id
        JOIN tables t2 ON c2.table_id = t2.id
        WHERE c1.table_id = ? OR c2.table_id = ?
        ORDER BY r.confidence DESC
    """, (table_id, table_id))
    table["relationships"] = [dict(zip(
        ["id","type","confidence","detected_by","notes","source_col","source_table","target_col","target_table"], r
    )) for r in cur.fetchall()]

    return table


@app.get("/api/search")
def search(q: str = Query(..., min_length=1, description="검색어")):
    """테이블/필드 통합 검색"""
    pattern = f"%{q}%"
    cur = store.conn.cursor()

    cur.execute("""
        SELECT t.id, t.table_name, t.schema_name, t.description, d.name,
               COUNT(c.id) as col_count
        FROM tables t
        JOIN databases d ON t.database_id = d.id
        LEFT JOIN columns c ON c.table_id = t.id
        WHERE t.table_name LIKE ? OR t.description LIKE ?
        GROUP BY t.id ORDER BY t.table_name LIMIT 30
    """, (pattern, pattern))
    tables = [dict(zip(["id","name","schema","description","database","columns"], r)) for r in cur.fetchall()]

    cur.execute("""
        SELECT c.id, c.column_name, c.data_type, c.description,
               c.is_primary_key, c.is_foreign_key,
               t.table_name, t.schema_name, d.name
        FROM columns c
        JOIN tables t ON c.table_id = t.id
        JOIN databases d ON t.database_id = d.id
        WHERE c.column_name LIKE ? OR c.description LIKE ?
        ORDER BY c.column_name LIMIT 30
    """, (pattern, pattern))
    columns = [dict(zip(
        ["id","name","type","description","is_pk","is_fk","table","schema","database"], r
    )) for r in cur.fetchall()]

    return {"query": q, "tables": tables, "columns": columns}


@app.get("/api/field-analysis")
def field_analysis(min_table_count: int = 1):
    """필드 분석: 중복 필드명 기준 그룹화"""
    cur = store.conn.cursor()
    cur.execute("""
        SELECT c.column_name, c.data_type, t.schema_name, t.table_name, d.name,
               c.description, t.id
        FROM columns c
        JOIN tables t ON c.table_id = t.id
        JOIN databases d ON t.database_id = d.id
        ORDER BY c.column_name
    """)
    from collections import defaultdict
    groups = defaultdict(list)
    for row in cur.fetchall():
        col_name, data_type, schema, table, db, desc, tid = row
        groups[col_name].append({
            "schema": schema, "table": table, "database": db,
            "data_type": data_type, "description": desc, "table_id": tid,
        })
    result = []
    for col_name, tables in groups.items():
        if len(tables) >= min_table_count:
            result.append({"column_name": col_name, "table_count": len(tables), "tables": tables})
    result.sort(key=lambda x: -x["table_count"])
    return result


@app.get("/api/graph")
def get_graph(format: str = "d3"):
    """그래프 데이터 (D3.js / Cytoscape)"""
    graph.build_from_store(store)
    graph.add_domain_nodes()
    if format == "cytoscape":
        return graph.to_cytoscape_json()
    return graph.to_d3_json()


@app.get("/api/graph/path")
def find_path(source: str, target: str):
    """두 테이블 간 경로"""
    path = graph.find_path(source, target)
    if not path:
        return {"path": [], "found": False}
    return {"path": path, "found": True, "length": len(path) - 1}


@app.delete("/api/tables/{table_id}")
def delete_table(table_id: int):
    """테이블 및 관련 관계 삭제"""
    cur = store.conn.cursor()
    cur.execute("SELECT id FROM tables WHERE id = ?", (table_id,))
    if not cur.fetchone():
        raise HTTPException(404, "테이블 없음")
    cur.execute("DELETE FROM relationships WHERE source_column_id IN (SELECT id FROM columns WHERE table_id = ?) OR target_column_id IN (SELECT id FROM columns WHERE table_id = ?)", (table_id, table_id))
    cur.execute("DELETE FROM columns WHERE table_id = ?", (table_id,))
    cur.execute("DELETE FROM tables WHERE id = ?", (table_id,))
    store.conn.commit()
    return {"status": "ok", "deleted_id": table_id}


@app.get("/api/relationships")
def list_relationships(
    type: Optional[str] = None,
    min_confidence: float = 0.0,
    limit: int = 100,
    offset: int = 0,
):
    """관계 목록"""
    cur = store.conn.cursor()
    query = """
        SELECT r.id, r.relation_type, r.confidence, r.detected_by, r.notes,
               c1.column_name, t1.table_name, d1.name,
               c2.column_name, t2.table_name, d2.name
        FROM relationships r
        JOIN columns c1 ON r.source_column_id = c1.id
        JOIN tables t1 ON c1.table_id = t1.id
        JOIN databases d1 ON t1.database_id = d1.id
        JOIN columns c2 ON r.target_column_id = c2.id
        JOIN tables t2 ON c2.table_id = t2.id
        JOIN databases d2 ON t2.database_id = d2.id
        WHERE r.confidence >= ?
    """
    params = [min_confidence]
    if type:
        query += " AND r.relation_type = ?"
        params.append(type)
    query += " ORDER BY r.confidence DESC, r.id DESC LIMIT ? OFFSET ?"
    params.append(limit)
    params.append(offset)

    cur.execute(query, params)
    return [dict(zip(
        ["id","type","confidence","detected_by","notes",
         "source_col","source_table","source_db","target_col","target_table","target_db"], r
    )) for r in cur.fetchall()]


@app.post("/api/relationships/{rel_id}/verify")
def verify_relationship(
    rel_id: int,
    body: RelationshipVerify,
    user: User = Depends(require("relationship:write")),
):
    """관계 검증/승인"""
    cur = store.conn.cursor()
    cur.execute("SELECT id FROM relationships WHERE id = ?", (rel_id,))
    if not cur.fetchone():
        raise HTTPException(404, "관계 없음")
    cur.execute("""
        UPDATE relationships SET verified = ?, notes = COALESCE(?, notes) WHERE id = ?
    """, (body.verified, body.notes, rel_id))
    store.conn.commit()
    return {"status": "ok", "id": rel_id, "verified": body.verified}


@app.post("/api/collect")
def trigger_collection(
    config: DatabaseConfig,
    user: User = Depends(require("collect:schema")),
):
    """스키마 수집 트리거"""
    try:
        collector = SchemaCollector(store)
        collector.add_database(
            config.db_type,
            host=config.host, port=config.port or 5432,
            database=config.database, user=config.user or "",
            password=config.password or "", db_name=config.db_name or config.database
        )
        results = collector.collect_all()

        orchestrator = RelationshipOrchestrator(store)
        rels = orchestrator.analyze_all()

        graph.build_from_store(store)
        graph.add_domain_nodes()

        return {
            "status": "ok",
            "databases": len(results),
            "tables": sum(len(db.tables) for db in results),
            "columns": sum(sum(len(t.columns) for t in db.tables) for db in results),
            "relationships": len(rels),
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/analyze")
def trigger_analysis(user: User = Depends(require("analysis:run"))):
    """연관관계 분석 트리거"""
    try:
        orchestrator = RelationshipOrchestrator(store)
        rels = orchestrator.analyze_all()
        graph.build_from_store(store)
        graph.add_domain_nodes()
        return {"status": "ok", "relationships": len(rels)}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/drift")
def check_drift():
    """스키마 드리프트 확인"""
    detector = SchemaDriftDetector(store.db_path)
    return detector.detect_changes()


@app.get("/api/history")
def get_history(days: int = 7):
    """변경 이력"""
    history = ChangeHistoryManager(store.db_path)
    return history.get_history(days=days)


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DB Ontology Dashboard</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f0f2f5;color:#333;padding:20px}
.header{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;padding:24px 32px;border-radius:12px;margin-bottom:24px}
.header h1{font-size:24px;margin-bottom:4px}
.header p{opacity:.9;font-size:14px}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;margin-bottom:24px}
.stat-card{background:#fff;border-radius:10px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,.08);text-align:center}
.stat-card .value{font-size:32px;font-weight:700;color:#667eea}
.stat-card .label{font-size:13px;color:#888;margin-top:4px}
.section{background:#fff;border-radius:10px;padding:20px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.section h2{font-size:16px;margin-bottom:16px;color:#555;border-bottom:2px solid #f0f2f5;padding-bottom:8px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px 10px;background:#f8f9fa;color:#666;font-weight:600;border-bottom:2px solid #e9ecef}
td{padding:8px 10px;border-bottom:1px solid #f0f2f5}
tr:hover{background:#f8f9fa}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.badge-fk{background:#e3f2fd;color:#1976d2}
.badge-naming{background:#f3e5f5;color:#7b1fa2}
.badge-value{background:#e8f5e9;color:#388e3c}
.search-box{width:100%;padding:10px 14px;border:1px solid #ddd;border-radius:8px;font-size:14px;margin-bottom:12px}
.search-box:focus{outline:none;border-color:#667eea;box-shadow:0 0 0 3px rgba(102,126,234,.15)}
.tab-bar{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.tab-btn{padding:8px 20px;border:none;border-radius:6px;background:#e9ecef;cursor:pointer;font-size:13px}
.tab-btn.active{background:#667eea;color:#fff}
.tab-btn:hover{opacity:.85}
.btn{display:inline-block;padding:10px 24px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .2s}
.btn:hover{opacity:.85}
.btn-primary{background:#667eea;color:#fff}
.btn-success{background:#4caf50;color:#fff}
.btn-danger{background:#f44336;color:#fff}
.btn-sm{padding:6px 14px;font-size:12px}
.btn:disabled{opacity:.5;cursor:not-allowed}
#graph-container{width:100%;height:500px;border:1px solid #eee;border-radius:8px;overflow:hidden}
.link{stroke:#ccc;stroke-width:1.5;fill:none}
.node circle{fill:#667eea;stroke:#fff;stroke-width:2;cursor:pointer}
.node text{font-size:11px;fill:#333}
.node:hover circle{fill:#764ba2}
.status-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.status-ok{background:#4caf50}
.status-warn{background:#ff9800}
@media(max-width:600px){.stats-grid{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="header"><h1>DB Ontology Analyzer</h1><p id="subtitle">대시보드 로딩 중...</p></div>
<div class="stats-grid" id="statsGrid"></div>

<div class="tab-bar">
<button class="tab-btn" onclick="switchTab('collect',this)">📡 수집</button>
<button class="tab-btn active" onclick="switchTab('tables',this)">테이블</button>
<button class="tab-btn" onclick="switchTab('relations',this)">관계</button>
<button class="tab-btn" onclick="switchTab('graph',this)">그래프</button>
<button class="tab-btn" onclick="switchTab('search',this)">검색</button>
<button class="tab-btn" onclick="switchTab('field',this)">📊 필드분석</button>
<button class="tab-btn" onclick="switchTab('consolidate',this)">🔗 통합조회</button>
</div>

<div id="collect-tab" class="section" style="display:none"><h2>PostgreSQL 테이블 선택</h2><div id="collectContent"><p style="color:#999">🔄 <a href="#" onclick="loadPgTables();return false" style="color:#667eea;text-decoration:underline">재조회</a> 버튼을 눌러 PostgreSQL 테이블 목록을 불러오세요.</p></div><div id="semanticSuggestArea" style="margin-top:12px;display:none"><h2 style="font-size:14px;margin-bottom:8px">🧠 의미 연관 테이블 제안</h2><div id="semanticSuggestions"></div></div></div>
<div id="tables-tab" class="section"><h2>테이블 목록</h2><div id="tablesContent"><p style="color:#999">로딩 중...</p></div></div>
<div id="relations-tab" class="section" style="display:none"><h2>관계 목록</h2><div id="relationsContent"><p style="color:#999">로딩 중...</p></div></div>
<div id="graph-tab" class="section" style="display:none"><h2>온톨로지 그래프</h2><div id="graph-container"><p style="color:#999">데이터를 불러오는 중...</p></div></div>
<div id="search-tab" class="section" style="display:none"><h2>검색</h2><input class="search-box" id="searchInput" placeholder="테이블/컬럼명 검색..." oninput="doSearch(this.value)"><div id="searchResults"><p style="color:#999">검색어를 입력하세요</p></div></div>
<div id="field-tab" class="section" style="display:none"><h2>📊 필드 분석</h2><p style="color:#666;font-size:13px;margin-bottom:12px">동일한 컬럼명을 가진 테이블들을 그룹화하여, 중복 필드명이 많은 순으로 정렬</p><div id="fieldContent"><p style="color:#999">로딩 중...</p></div></div>
<div id="consolidate-tab" class="section" style="display:none"><h2>🔗 통합조회</h2><p style="color:#666;font-size:13px;margin-bottom:12px">특정 필드명을 가진 모든 테이블의 첫 row 데이터를 한눈에 비교</p><div style="display:flex;gap:6px;margin-bottom:12px"><input id="lookupFieldInput" placeholder="필드명 입력 후 Enter" style="flex:1;padding:8px 12px;border:1px solid #ddd;border-radius:6px;font-size:13px" onkeydown="if(event.key==='Enter')lookupField()"><button class="btn btn-sm btn-primary" onclick="lookupField()">조회</button><button class="btn btn-sm btn-success" onclick="exportCSV()" id="csvBtn" style="display:none">CSV 저장</button></div><div id="lastLookupFields" style="margin-bottom:8px"></div><div id="consolidateContent"><p style="color:#999">조회할 필드명을 입력하세요.<br>예: bld_cd, addr, tel_no, dept_cd</p></div></div>

<script>
async function getJSON(url){try{const ctrl=new AbortController();const to=setTimeout(()=>ctrl.abort(),5000);const r=await fetch(url,{signal:ctrl.signal});clearTimeout(to);if(!r.ok)return null;return r.json()}catch(e){return null}}
function badge(type){if(type==='FK')return'<span class="badge badge-fk">FK</span>';if(type==='NAMING_PATTERN')return'<span class="badge badge-naming">명명</span>';if(type==='DATA_SIMILAR')return'<span class="badge badge-value">값유사</span>';return`<span class="badge" style="background:#eee">${type}</span>`}
function switchTab(name,btn){document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');document.querySelectorAll('[id$="-tab"]').forEach(t=>t.style.display='none');document.getElementById(name+'-tab').style.display='block';if(name==='graph')loadGraph();if(name==='collect'&&allPgTables.length>0)renderPgTables();if(name==='field')loadFieldAnalysis();if(name==='consolidate'){const el=document.getElementById('lookupFieldInput');if(el)el.focus();if(recentLookups.length){document.getElementById('lastLookupFields').innerHTML='<span style="color:#888;font-size:12px">최근 조회: </span>'+recentLookups.slice().reverse().map(n=>'<a href="#" onclick="document.getElementById(\'lookupFieldInput\').value=\''+n+'\';lookupField();return false" style="color:#667eea;text-decoration:underline;margin-right:6px;font-size:12px">'+n+'</a>').join('')}if(!window._lastLookupData)loadConsolidate()}}
async function loadOverview(){const d=await getJSON('/api/overview');if(!d){document.getElementById('subtitle').textContent='로드 실패 (서버 응답 없음)';return}const stats=[{v:d.databases||0,l:'데이터베이스'},{v:d.tables||0,l:'테이블'},{v:d.columns||0,l:'컬럼'},{v:d.relationships||0,l:'관계'},{v:d.fk_count||0,l:'FK 관계'},{v:d.verified||0,l:'검증됨'}];document.getElementById('statsGrid').innerHTML=stats.map(s=>`<div class="stat-card"><div class="value">${s.v}</div><div class="label">${s.l}</div></div>`).join('');document.getElementById('subtitle').textContent=d.tables+'개 테이블 · '+d.relationships+'개 관계'}
async function deleteTable(id){if(!confirm('이 테이블을 삭제하시겠습니까?'))return;const r=await fetch('/api/tables/'+id,{method:'DELETE'});if(r.ok){loadTables();loadRelations();loadOverview()}else{alert('삭제 실패')}}
let selectedTableIds=new Set();
function toggleSelTable(id){if(selectedTableIds.has(id))selectedTableIds.delete(id);else selectedTableIds.add(id);document.getElementById('batchDelBtn').disabled=selectedTableIds.size===0}
async function batchDeleteTables(){if(selectedTableIds.size===0)return;if(!confirm(selectedTableIds.size+'개 테이블을 삭제하시겠습니까?'))return;const ids=[...selectedTableIds];const r=await fetch('/api/tables/batch',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})});if(r.ok){selectedTableIds.clear();loadTables();loadRelations();loadOverview()}else{alert('일괄 삭제 실패')}}
async function loadTables(){const tbls=await getJSON('/api/tables');if(!tbls){document.getElementById('tablesContent').innerHTML='<p style="color:red">API 오류</p>';return}if(!tbls.length){document.getElementById('tablesContent').innerHTML='<p style="color:#999">데이터가 없습니다. 먼저 스키마를 수집하세요.</p>';return}allCachedTables=tbls;let html='<div style="margin-bottom:8px;display:flex;gap:6px;align-items:center"><button class="btn btn-sm btn-primary" onclick="tblSelectAll()">전체 선택</button><button class="btn btn-sm btn-danger" onclick="tblSelectNone()">전체 해제</button><button class="btn btn-sm btn-danger" id="batchDelBtn" onclick="batchDeleteTables()" disabled>🗑 선택 삭제 (<span id="batchDelCount">0</span>)</button></div><table><tr><th style="width:30px"></th><th>DB</th><th>스키마</th><th>테이블</th><th>Rows</th><th>COMMENT</th><th style="width:50px">삭제</th></tr>';tbls.forEach(t=>{const checked=selectedTableIds.has(t.id)?'checked':'';const comment=t.description||'';html+=`<tr><td><input type="checkbox" ${checked} onchange="toggleSelTable(${t.id})"></td><td>${t.database||''}</td><td>${t.schema||''}</td><td><strong>${t.name||t.table_name}</strong></td><td>${t.rows??'-'}</td><td style="color:#666;font-size:12px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${comment}</td><td><button class="btn btn-sm btn-danger" onclick="deleteTable(${t.id})" style="padding:2px 8px;font-size:11px">🗑</button></td></tr>`});html+='</table>';document.getElementById('tablesContent').innerHTML=html;document.getElementById('batchDelCount').textContent=selectedTableIds.size;document.getElementById('batchDelBtn').disabled=selectedTableIds.size===0}
function tblSelectAll(){document.querySelectorAll('#tablesContent table input[type=checkbox]').forEach(cb=>{cb.checked=true});selectedTableIds=new Set(allCachedTables.map(t=>t.id));document.getElementById('batchDelCount').textContent=selectedTableIds.size;document.getElementById('batchDelBtn').disabled=false}
function tblSelectNone(){document.querySelectorAll('#tablesContent table input[type=checkbox]').forEach(cb=>{cb.checked=false});selectedTableIds.clear();document.getElementById('batchDelCount').textContent='0';document.getElementById('batchDelBtn').disabled=true}
let allCachedTables=[];
let selectedTables=new Set();let allPgTables=[];let pgConfig={};
async function loadPgTables(){document.getElementById('collectContent').innerHTML='<p style="color:#999">PostgreSQL 테이블 목록 조회 중...</p>';const res=await getJSON('/api/pg-tables');if(!res||!res.tables){document.getElementById('collectContent').innerHTML='<p style="color:red">PostgreSQL 연결 실패</p>';return}allPgTables=res.tables;pgConfig=res.config||{};selectedTables.clear();renderPgTables()}
function renderPgTables(){const tbls=allPgTables;const cfg=pgConfig;let html=`<p style="color:#666;margin-bottom:12px">총 ${tbls.length}개 테이블 | 연결: ${cfg.host||''}:${cfg.port||''}</p><div style="margin-bottom:12px;display:flex;flex-wrap:wrap;gap:6px"><button class="btn btn-sm btn-primary" onclick="loadPgTables()">🔄 재조회</button><button class="btn btn-sm btn-primary" onclick="selectAllPg()">전체 선택</button><button class="btn btn-sm btn-danger" onclick="deselectAllPg()">전체 해제</button><button class="btn btn-sm btn-primary" onclick="selectCommentPg()" style="background:#17a2b8">COMMENT만 선택</button><button class="btn btn-sm" id="sortSelBtn" onclick="toggleSortSelected()" style="background:#6c757d;color:#fff">선택정렬</button><span id="selectedCount" style="margin-left:auto;color:#666;font-size:13px;line-height:30px">0개 선택됨</span></div>`;if(tbls.length===0){html+='<p style="color:#999">PostgreSQL 테이블 목록을 불러오려면 재조회 버튼을 클릭하세요.</p>';document.getElementById('collectContent').innerHTML=html;renderPresetSelect();return}html+=`<div style="margin-bottom:12px;padding:10px;background:#f8f9fa;border-radius:8px;font-size:13px"><strong style="color:#555">📋 프리셋</strong><div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap"><input id="presetName" placeholder="프리셋 이름" style="padding:4px 8px;border:1px solid #ddd;border-radius:4px;font-size:12px;flex:1;min-width:100px"><button class="btn btn-sm btn-primary" onclick="savePreset()">저장</button><select id="presetSelect" onchange="renderPresetPreview()" style="padding:4px 8px;border:1px solid #ddd;border-radius:4px;font-size:12px;flex:1"><option value="">=== 불러올 프리셋 ===</option></select><button class="btn btn-sm btn-success" onclick="loadPreset()">불러오기</button><button class="btn btn-sm btn-danger" onclick="deletePreset()">삭제</button></div><div id="presetPreview" style="margin-top:4px;color:#888;font-size:11px"></div><div style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap"><button class="btn btn-sm" style="background:#9c27b0;color:#fff" onclick="showSemanticSuggest()">🧠 의미 연관 제안</button></div><div style="display:flex;gap:6px;margin-bottom:8px"><input id="tableSearch" placeholder="테이블명 검색 (Enter)" onkeydown="if(event.key==='Enter')filterTables(this.value)" style="flex:1;padding:8px 10px;border:1px solid #ddd;border-radius:6px;font-size:13px;box-sizing:border-box"><button class="btn btn-sm btn-primary" onclick="filterTables(document.getElementById('tableSearch').value)">검색</button><button class="btn btn-sm btn-secondary" onclick="document.getElementById('tableSearch').value='';tableFilter='';renderPgTables()" style="background:#6c757d;color:#fff">초기화</button></div><div style="max-height:300px;overflow-y:auto;border:1px solid #eee;border-radius:8px">`;const filtered=tbls.filter(t=>!tableFilter||t.table.toLowerCase().includes(tableFilter)||t.schema.toLowerCase().includes(tableFilter));let schemaGroups={};filtered.forEach(t=>{if(!schemaGroups[t.schema])schemaGroups[t.schema]=[];schemaGroups[t.schema].push(t)});const schemaOrder=Object.keys(schemaGroups).sort();if(sortSelected){schemaOrder.forEach(s=>{const sSel=schemaGroups[s].filter(t=>selectedTables.has(t.schema+'.'+t.table));if(!sSel.length)return;html+=`<div style="padding:6px 10px;background:#f8f9fa;font-weight:600;font-size:13px;color:#555;border-bottom:1px solid #eee">${s}</div>`;sSel.forEach(t=>{const id=t.schema+'.'+t.table;const checked='checked';html+=`<label style="display:flex;align-items:center;padding:5px 10px;cursor:pointer;border-bottom:1px solid #f5f5f5;font-size:13px"><input type="checkbox" ${checked} onchange="toggleTable('${id}')" style="margin-right:8px;flex-shrink:0"><span style="flex-shrink:0;font-weight:500">${t.table}</span>${t.comment?' <span style="color:#888;font-size:11px;margin-left:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">— '+t.comment+'</span>':''}</label>`})});schemaOrder.forEach(s=>{const sUnsel=schemaGroups[s].filter(t=>!selectedTables.has(t.schema+'.'+t.table));if(!sUnsel.length)return;html+=`<div style="padding:6px 10px;background:#f8f9fa;font-weight:600;font-size:13px;color:#555;border-bottom:1px solid #eee">${s}</div>`;sUnsel.forEach(t=>{const id=t.schema+'.'+t.table;const checked='';html+=`<label style="display:flex;align-items:center;padding:5px 10px;cursor:pointer;border-bottom:1px solid #f5f5f5;font-size:13px"><input type="checkbox" ${checked} onchange="toggleTable('${id}')" style="margin-right:8px;flex-shrink:0"><span style="flex-shrink:0;font-weight:500">${t.table}</span>${t.comment?' <span style="color:#888;font-size:11px;margin-left:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">— '+t.comment+'</span>':''}</label>`})})}else{schemaOrder.forEach(s=>{html+=`<div style="padding:6px 10px;background:#f8f9fa;font-weight:600;font-size:13px;color:#555;border-bottom:1px solid #eee">${s}</div>`;schemaGroups[s].forEach(t=>{const id=t.schema+'.'+t.table;const checked=selectedTables.has(id)?'checked':'';html+=`<label style="display:flex;align-items:center;padding:5px 10px;cursor:pointer;border-bottom:1px solid #f5f5f5;font-size:13px"><input type="checkbox" ${checked} onchange="toggleTable('${id}')" style="margin-right:8px;flex-shrink:0"><span style="flex-shrink:0;font-weight:500">${t.table}</span>${t.comment?' <span style="color:#888;font-size:11px;margin-left:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">— '+t.comment+'</span>':''}</label>`})})}html+=`</div><div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap"><button class="btn btn-primary" onclick="runPipeline()" id="runBtn">분석 시작</button><button class="btn btn-sm btn-secondary" onclick="resetPipeline()" id="resetBtn" style="background:#ef5350;color:#fff">초기화</button><button class="btn btn-sm btn-primary" onclick="firstRowsMode='first';loadFirstRows()" id="firstRowsBtn">첫줄 불러오기</button><button class="btn btn-sm btn-primary" onclick="firstRowsMode='last';loadFirstRows()">마지막줄 불러오기</button></div><div id="firstRowsDisplay" style="margin-top:12px"></div><div id="pipelineStatus" style="margin-top:12px"></div>`;document.getElementById('collectContent').innerHTML=html;const si=document.getElementById('tableSearch');if(si)si.value=tableFilter;const sb=document.getElementById('sortSelBtn');if(sb)sb.style.background=sortSelected?'#007bff':'#6c757d';updateSelectedCount();renderPresetSelect()}
function selectAllPg(){selectedTables=new Set(allPgTables.map(t=>t.schema+'.'+t.table));renderPgTables()}
function selectCommentPg(){selectedTables=new Set(allPgTables.filter(t=>t.comment).map(t=>t.schema+'.'+t.table));renderPgTables()}
function deselectAllPg(){selectedTables=new Set();renderPgTables()}
function toggleTable(id){if(selectedTables.has(id))selectedTables.delete(id);else selectedTables.add(id);updateSelectedCount()}
function updateSelectedCount(){const el=document.getElementById('selectedCount');if(el)el.textContent=selectedTables.size+'개 선택됨'}
function getPresets(){try{return JSON.parse(localStorage.getItem('dbOntologyPresets')||'{}')}catch(e){return {}}}
function savePresets(p){localStorage.setItem('dbOntologyPresets',JSON.stringify(p))}
function renderPresetSelect(){const presets=getPresets();const sel=document.getElementById('presetSelect');if(!sel)return;sel.innerHTML='<option value="">=== 불러올 프리셋 ===</option>';Object.keys(presets).sort().forEach(k=>{sel.innerHTML+='<option value="'+k+'">'+k+' ('+presets[k].length+'개)</option>'})}
function renderPresetPreview(){const presets=getPresets();const name=document.getElementById('presetSelect').value;const el=document.getElementById('presetPreview');if(!name||!presets[name]){el.textContent='';return}el.textContent=presets[name].join(', ')}
function savePreset(){const name=document.getElementById('presetName').value.trim();if(!name){alert('프리셋 이름을 입력하세요');return}if(selectedTables.size===0){alert('선택된 테이블이 없습니다');return}const presets=getPresets();presets[name]=[...selectedTables];savePresets(presets);renderPresetSelect();document.getElementById('presetName').value=''}
function loadPreset(){const name=document.getElementById('presetSelect').value;if(!name){alert('프리셋을 선택하세요');return}const presets=getPresets();if(!presets[name]){alert('프리셋 없음');return}const valid=presets[name].filter(id=>allPgTables.some(t=>t.schema+'.'+t.table===id));if(valid.length!==presets[name].length){alert(presets[name].length-valid.length+'개 테이블이 현재 목록에 없어 제외됨')}selectedTables=new Set(valid);renderPgTables()}
function deletePreset(){const name=document.getElementById('presetSelect').value;if(!name){alert('프리셋을 선택하세요');return}if(!confirm('프리셋 "'+name+'"을 삭제하시겠습니까?'))return;const presets=getPresets();delete presets[name];savePresets(presets);renderPresetSelect();document.getElementById('presetPreview').textContent=''}
let firstRowsMode='first';let tableFilter='';let sortSelected=false;
let filterDebounce;function filterTables(v){clearTimeout(filterDebounce);filterDebounce=setTimeout(()=>{tableFilter=v.toLowerCase();renderPgTables()},200)}
function toggleSortSelected(){sortSelected=!sortSelected;renderPgTables()}
async function showSemanticSuggest(){if(selectedTables.size===0){alert('테이블을 먼저 선택하세요');return}const el=document.getElementById('semanticSuggestions');const area=document.getElementById('semanticSuggestArea');area.style.display='block';el.innerHTML='<p style="color:#667eea">🧠 분석 중...</p>';try{const res=await fetch('/api/table-semantic-suggest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selected_tables:[...selectedTables]})});const data=await res.json();if(!data.suggestions||!data.suggestions.length){el.innerHTML='<p style="color:#999">제안할 유사 테이블이 없습니다.</p>';return}let html=`<p style="color:#555;font-size:12px;margin-bottom:8px">💡 선택한 테이블과 의미적으로 유사한 <strong>${data.suggestions.length}</strong>개 테이블</p><div style="display:flex;flex-wrap:wrap;gap:6px">`;data.suggestions.forEach(s=>{const key=s.schema+'.'+s.table;const sel=selectedTables.has(key);html+=`<div style="padding:8px 12px;border:1px solid #e0e0e0;border-radius:8px;background:${sel?'#e8f5e9':'#fff'};font-size:12px;cursor:pointer" onclick="toggleSuggestTable('${key}')"><div style="font-weight:600;font-size:13px">${s.schema}.<strong>${s.table}</strong></div><div style="color:#888;margin-top:2px">${s.comment||'<em style="color:#ccc">코멘트 없음</em>'}</div><div style="margin-top:4px;display:flex;gap:8px;flex-wrap:wrap"><span style="background:#f3e5f5;padding:1px 6px;border-radius:4px;font-size:10px">🗝️ ${Math.round(s.token_score*100)}%</span><span style="background:#e3f2fd;padding:1px 6px;border-radius:4px;font-size:10px">📊 ${Math.round(s.column_score*100)}%</span><span style="background:#e8f5e9;padding:1px 6px;border-radius:4px;font-size:10px">⭐ ${Math.round(s.score*100)}%</span></div><div style="color:#999;font-size:10px;margin-top:2px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${(s.overlap_tokens||[]).join(', ')}</div></div>`});html+='</div>';el.innerHTML=html}catch(e){el.innerHTML='<p style="color:red">❌ 오류: '+(e.message||'요청 실패')+'</p>'}}
function toggleSuggestTable(key){if(selectedTables.has(key))selectedTables.delete(key);else selectedTables.add(key);renderPgTables();showSemanticSuggest()}
async function loadFirstRows(){const mode=firstRowsMode;const label=mode==='last'?'마지막줄':'첫줄';if(selectedTables.size===0){alert('테이블을 선택하세요');return}const btn=document.getElementById('firstRowsBtn');const el=document.getElementById('firstRowsDisplay');const els=document.querySelectorAll('[onclick*=\"loadFirstRows\"]');els.forEach(b=>b.disabled=true);el.innerHTML='<p style="color:#667eea">'+selectedTables.size+'개 테이블 '+label+' 조회 중...</p>';const res=await fetch('/api/pg-first-rows',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selected_tables:[...selectedTables],mode})});const data=await res.json();els.forEach(b=>b.disabled=false);if(!data||!data.rows){el.innerHTML='<p style="color:red">조회 실패</p>';return}let html='<h3 style="font-size:14px;margin:8px 0">'+label+' 데이터 ('+Object.keys(data.rows).length+'개)</h3><table><tr><th>테이블</th><th>데이터</th></tr>';Object.keys(data.rows).sort().forEach(tbl=>{const row=data.rows[tbl];if(row._error){html+='<tr><td>'+tbl+'</td><td style="color:red">'+row._error+'</td></tr>';return}const vals=Object.entries(row).map(([k,v])=>{const val=v===null?'<em style="color:#999">NULL</em>':String(v);return'<span style="background:#f0f2f5;padding:1px 6px;border-radius:3px;margin:1px;display:inline-block;font-size:11px"><strong>'+k+':</strong> '+val+'</span>'});html+='<tr><td style="white-space:nowrap;font-weight:600;vertical-align:top">'+tbl+'</td><td style="font-size:12px">'+(vals.length?vals.join(''):'<em style="color:#999">빈 테이블</em>')+'</td></tr>'});html+='</table>';el.innerHTML=html}
async function runPipeline(){if(selectedTables.size===0){alert('테이블을 선택하세요');return}const btn=document.getElementById('runBtn');const st=document.getElementById('pipelineStatus');btn.disabled=true;btn.textContent='분석 중...';st.innerHTML='<p style="color:#667eea">스키마 수집 및 분석 중...</p>';try{const res=await fetch('/api/run-pipeline',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selected_tables:[...selectedTables]})});if(!res.ok){const err=await res.json().catch(()=>({detail:'HTTP '+res.status}));throw new Error(err.detail||err.message||'HTTP '+res.status)}const data=await res.json();if(data.status==='ok'){st.innerHTML=`<div style="background:#e8f5e9;border-radius:8px;padding:16px;margin-top:8px"><p style="font-weight:600;color:#2e7d32">✅ 분석 완료</p><p style="font-size:13px;color:#555;margin-top:4px">${data.tables}개 테이블 · ${data.columns}개 컬럼 · ${data.relationships}개 관계</p><p style="margin-top:8px"><a href="/dashboard" class="btn btn-sm btn-success" onclick="alert('탭을 전환합니다')">대시보드 보기</a> <a href="${data.dashboard}" target="_blank" class="btn btn-sm btn-primary">HTML 대시보드</a> <a href="${data.graph}" target="_blank" class="btn btn-sm btn-primary">그래프</a></p></div>`;loadOverview();loadTables();loadRelations();setTimeout(loadGraph,500)}else{st.innerHTML='<div style="background:#ffebee;border-radius:8px;padding:16px;margin-top:8px"><p style="font-weight:600;color:#c62828">❌ 오류</p><p style="font-size:13px">'+(data.detail||data.message||'알 수 없는 오류')+'</p></div>'}}catch(e){st.innerHTML='<div style="background:#ffebee;border-radius:8px;padding:16px;margin-top:8px"><p style="font-weight:600;color:#c62828">❌ 오류</p><p style="font-size:13px">'+(e.message||'요청 실패')+'</p></div>'}finally{btn.disabled=false;btn.textContent='분석 시작'}}
async function resetPipeline(){const btn=document.getElementById('runBtn');const st=document.getElementById('pipelineStatus');btn.disabled=true;btn.textContent='초기화 중...';st.innerHTML='<p style="color:#667eea">데이터 초기화 중...</p>';try{const res=await fetch('/api/reset-pipeline',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selected_tables:[...selectedTables]})});if(!res.ok){const err=await res.json().catch(()=>({detail:'HTTP '+res.status}));throw new Error(err.detail||err.message||'HTTP '+res.status)}const data=await res.json();st.innerHTML='<div style="background:#e8f5e9;border-radius:8px;padding:16px;margin-top:8px"><p style="font-weight:600;color:#2e7d32">✅ 초기화 완료</p><p style="font-size:13px;color:#555;margin-top:4px">삭제된 테이블: '+(data.deleted_tables||0)+'개</p></div>';loadOverview();loadTables();loadRelations()}catch(e){st.innerHTML='<div style="background:#ffebee;border-radius:8px;padding:16px;margin-top:8px"><p style="font-weight:600;color:#c62828">❌ 초기화 오류</p><p style="font-size:13px">'+(e.message||'요청 실패')+'</p></div>'}finally{btn.disabled=false;btn.textContent='분석 시작'}}
function matchLabel(n){if(!n)return'';if(n.startsWith('동일 필드명'))return'<span style="color:#4caf50">✅ 필드명 일치:</span> '+n.replace('동일 필드명: ','');if(n.startsWith('정규화 매칭'))return'<span style="color:#4caf50">✅ 필드명 일치:</span> '+n.replace('정규화 매칭: ','');if(n.startsWith('동의어'))return'<span style="color:#4caf50">✅ 필드명 일치:</span> '+n.replace('동의어: ','');if(n.startsWith('코멘트 유사')){const m=n.match(/[\d.]+%/);const rest=n.replace(/코멘트 유사:\s*[\d.]+%\s*\|\s*/,'');return'<span style="color:#2196f3">📝 코멘트(노트) 일치'+(m?' '+m[0]:'')+':</span> '+rest}if(n.startsWith('값 중복'))return'<span style="color:#ff9800">📊 일치비율:</span> '+n.replace('값 중복: ','');if(n.startsWith('외래키'))return'<span style="color:#9c27b0">🔗 FK 참조:</span> '+n.replace('외래키: ','');return'<span style="color:#888">'+n+'</span>'}
let allRels=[];let relOffset=0;let relHasMore=true;
async function loadRelations(){allRels=[];relOffset=0;relHasMore=true;const rels=await getJSON('/api/relationships?limit=200');if(!rels){document.getElementById('relationsContent').innerHTML='<p style="color:red">API 오류</p>';return}if(!rels.length){document.getElementById('relationsContent').innerHTML='<p style="color:#999">데이터가 없습니다.</p>';return}allRels=rels;relHasMore=rels.length>=200;renderRelations()}
async function moreRelations(){if(!relHasMore)return;relOffset+=200;const rels=await getJSON('/api/relationships?limit=200&offset='+relOffset);if(!rels||!rels.length){relHasMore=false;renderRelations();return}allRels=allRels.concat(rels);relHasMore=rels.length>=200;renderRelations()}
function renderRelations(){const rels=allRels;let html='<p style="color:#666;font-size:12px;margin-bottom:8px">총 '+rels.length+'개 표시</p><table><tr><th>유형</th><th>비교내역</th><th>출처</th><th>대상</th><th>신뢰도</th><th>감지</th></tr>';rels.forEach(r=>{html+=`<tr><td>${badge(r.type)}</td><td style="font-size:11px;max-width:200px">${matchLabel(r.notes)}</td><td>${r.source_db||''}.${r.source_table}.${r.source_col}</td><td>${r.target_db||''}.${r.target_table}.${r.target_col}</td><td>${(r.confidence*100).toFixed(0)}%</td><td>${r.detected_by||''}</td></tr>`});html+='</table>';if(relHasMore)html+='<div style="text-align:center;padding:12px"><button class="btn btn-sm btn-primary" onclick="moreRelations()">더보기 (+200)</button></div>';document.getElementById('relationsContent').innerHTML=html}
async function loadGraph(){const container=document.getElementById('graph-container');container.innerHTML='<p style="color:#999;padding:20px">그래프 로딩 중...</p>';const data=await getJSON('/api/graph?format=d3');if(!data||!data.nodes||!data.nodes.length){container.innerHTML='<p style="color:#999;padding:20px">그래프 데이터가 없습니다. 먼저 분석을 실행하세요.</p>';return}container.innerHTML='';const w=container.clientWidth||800,h=500;const svg=d3.select(container).append('svg').attr('width',w).attr('height',h);const g=svg.append('g');const zoom=d3.zoom().scaleExtent([.1,4]).on('zoom',(e)=>g.attr('transform',e.transform));svg.call(zoom);const sim=d3.forceSimulation(data.nodes).force('link',d3.forceLink(data.links).id(d=>d.id).distance(80)).force('charge',d3.forceManyBody().strength(-200)).force('center',d3.forceCenter(w/2,h/2)).force('collision',d3.forceCollide(25));const link=g.selectAll('.link').data(data.links).enter().append('line').attr('class','link').attr('stroke-width',d=>Math.sqrt(d.value||1));const node=g.selectAll('.node').data(data.nodes).enter().append('g').attr('class','node').call(d3.drag().on('start',(e,d)=>{if(!e.active)sim.alphaTarget(.3).restart();d.fx=d.x;d.fy=d.y}).on('drag',(e,d)=>{d.fx=e.x;d.fy=e.y}).on('end',(e,d)=>{if(!e.active)sim.alphaTarget(0);d.fx=null;d.fy=null}));node.append('circle').attr('r',d=>{if(d.group===1||d.group==='DATABASE')return 18;if(d.group===2||d.group==='SCHEMA')return 12;if(d.group==='TABLE')return 8;return 6}).attr('fill',d=>{const m={DATABASE:'#667eea',SCHEMA:'#764ba2',TABLE:'#4caf50',COLUMN:'#ff9800',UNKNOWN:'#888'};return m[d.group]||'#667eea'});node.append('text').text(d=>d.name||d.label||d.id).attr('dx',15).attr('dy',4).style('font-size','10px');sim.on('tick',()=>{link.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y).attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);node.attr('transform',d=>`translate(${d.x},${d.y})`)})}
async function doSearch(q){if(q.length<2){document.getElementById('searchResults').innerHTML='<p style="color:#999">2글자 이상 입력하세요</p>';return}const d=await getJSON('/api/search?q='+encodeURIComponent(q));if(!d){document.getElementById('searchResults').innerHTML='<p style="color:#999">오류</p>';return}let html='';if(d.tables&&d.tables.length){html+='<h3 style="font-size:14px;margin:8px 0">테이블</h3><table>';d.tables.forEach(t=>{html+=`<tr><td><strong>${t.name}</strong></td><td>${t.description||''}</td></tr>`});html+='</table>'}if(d.columns&&d.columns.length){html+='<h3 style="font-size:14px;margin:8px 0">컬럼</h3><table>';d.columns.forEach(c=>{html+=`<tr><td>${c.name}</td><td>${c.type||''}</td><td>${c.table}</td><td>${c.description||''}</td></tr>`});html+='</table>'}if(!html)html='<p style="color:#999">검색 결과 없음</p>';document.getElementById('searchResults').innerHTML=html}
let fieldExpanded={};
async function loadFieldAnalysis(){const el=document.getElementById('fieldContent');const data=await getJSON('/api/field-analysis');if(!data||!data.length){el.innerHTML='<p style="color:#999">필드 분석 데이터가 없습니다. 먼저 분석을 실행하세요.</p>';return}let html='<p style="color:#666;font-size:12px;margin-bottom:8px">총 '+data.length+'개 필드</p><div style="max-height:500px;overflow-y:auto;border:1px solid #eee;border-radius:8px">';data.forEach(d=>{const exp=fieldExpanded[d.column_name];const enc=d.column_name.replace(/'/g,"\\'");html+=`<div style="border-bottom:1px solid #f0f2f5"><div style="display:flex;align-items:center;padding:8px 12px;cursor:pointer;font-size:13px" onclick='toggleField("${enc}")'><span style="font-weight:600;flex:1">${d.column_name}</span><span class="badge" style="background:#667eea;color:#fff;margin-right:12px">${d.table_count}개 테이블</span><span style="color:#999;font-size:11px">${exp?'▲':'▼'}</span></div>${exp?`<div style="padding:4px 12px 12px;background:#fafbfc;font-size:12px">${d.tables.map(t=>`<div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid #f0f2f5"><span style="flex:1"><strong>${t.database}</strong>.${t.schema}.${t.table}</span><span style="color:#888;font-size:11px">${t.data_type}</span><button class="btn btn-sm" style="padding:2px 8px;font-size:10px;background:#e9ecef" onclick="event.stopPropagation();previewField('${t.schema}.${t.table}','${enc}')">데이터보기</button></div>`).join('')}<div id="preview-${d.column_name}" style="margin-top:4px"></div></div>`:''}</div>`});html+='</div>';el.innerHTML=html}
function toggleField(name){fieldExpanded[name]=!fieldExpanded[name];loadFieldAnalysis()}
async function previewField(tbl,col){const el=document.getElementById('preview-'+col);el.innerHTML='<span style="color:#667eea">조회 중...</span>';const res=await fetch('/api/pg-first-rows',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selected_tables:[tbl],mode:'first'})});const data=await res.json();if(!data||!data.rows||!data.rows[tbl]){el.innerHTML='<span style="color:red">조회 실패</span>';return}const row=data.rows[tbl];if(row._error){el.innerHTML='<span style="color:red">'+row._error+'</span>';return}const val=col in row?row[col]:'(컬럼 없음)';const display=val===null?'<em style="color:#999">NULL</em>':String(val);el.innerHTML='<span style="background:#e8f5e9;padding:2px 8px;border-radius:4px;color:#2e7d32">📄 '+display+'</span>'}
let recentLookups=JSON.parse(localStorage.getItem('dbOntologyRecentLookups')||'[]');
function saveRecentLookups(name){recentLookups=recentLookups.filter(n=>n!==name).concat(name).slice(-10);localStorage.setItem('dbOntologyRecentLookups',JSON.stringify(recentLookups))}
function loadConsolidate(){const el=document.getElementById('consolidateContent');const input=document.getElementById('lookupFieldInput');if(input)input.focus();document.getElementById('csvBtn').style.display='none';if(recentLookups.length){document.getElementById('lastLookupFields').innerHTML='<span style="color:#888;font-size:12px">최근 조회: </span>'+recentLookups.slice().reverse().map(n=>'<a href="#" onclick="document.getElementById(\'lookupFieldInput\').value=\''+n+'\';lookupField();return false" style="color:#667eea;text-decoration:underline;margin-right:6px;font-size:12px">'+n+'</a>').join('')}el.innerHTML='<p style="color:#999">조회할 필드명을 입력하세요.</p>'}
function exportCSV(){const d=window._lastLookupData;if(!d||!d.tables||!d.rows)return;const sep=',';const esc=v=>{const s=String(v??'');return s.includes(sep)||s.includes('"')?'"'+s.replace(/"/g,'""')+'"':s};let csv='필드명'+sep+'코멘트'+sep+d.tables.map(t=>esc(t.schema+'.'+t.table)).join(sep)+'\n';d.rows.forEach(r=>{const label=r.sample_name||r.key||'';const comment=r.comment||'';csv+=esc(label)+sep+esc(comment);d.tables.forEach(t=>{const cell=r.tables[t.key];const val=cell&&cell.meta?cell.value:null;csv+=sep+esc(val===null||val===undefined?'':val)});csv+='\n'});const blob=new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8;charset=utf-8'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=d.field_name+'_lookup.csv';a.click();URL.revokeObjectURL(a.href)}
async function lookupField(){const input=document.getElementById('lookupFieldInput');const name=input.value.trim();if(!name){alert('\ud544\ub4dc\uba85\uc744 \uc785\ub825\ud558\uc138\uc694');return}const el=document.getElementById('consolidateContent');el.innerHTML='<p style="color:#667eea">📡 조회 중...</p>';saveRecentLookups(name);if(recentLookups.length){document.getElementById('lastLookupFields').innerHTML='<span style="color:#888;font-size:12px">최근 조회: </span>'+recentLookups.slice().reverse().map(n=>'<a href="#" onclick="document.getElementById(\'lookupFieldInput\').value=\''+n+'\';lookupField();return false" style="color:#667eea;text-decoration:underline;margin-right:6px;font-size:12px">'+n+'</a>').join('')}try{const res=await fetch('/api/field-lookup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({field_name:name})});if(!res.ok){const err=await res.json().catch(()=>({detail:'HTTP '+res.status}));throw new Error(err.detail||err.message||'HTTP '+res.status)}const data=await res.json();if(!data.tables||!data.tables.length){el.innerHTML='<p style="color:#999">"'+name+'" 필드를 가진 테이블이 없습니다.</p>';return}let html='<p style="color:#555;font-size:13px;margin-bottom:8px">🔑 <strong>'+data.field_name+'</strong> · <strong>'+data.tables.length+'</strong>개 테이블 · <strong>'+data.rows.length+'</strong>개 컬럼</p><div style="margin-bottom:8px"><button class="btn btn-sm btn-success" onclick="exportCSV()">CSV 저장</button></div>';window._lastLookupData=data;html+='<div style="overflow-x:auto;border:1px solid #e0e0e0;border-radius:8px"><table style="font-size:12px;min-width:100%"><thead><tr><th style="position:sticky;left:0;background:#f8f9fa;z-index:2;min-width:100px">필드명</th><th style="position:sticky;left:100px;background:#f8f9fa;z-index:2;min-width:140px">코멘트</th>';data.tables.forEach(t=>{html+='<th style="min-width:120px;white-space:nowrap;text-align:center">'+t.schema+'.<br><strong>'+t.table+'</strong></th>'});html+='</tr></thead><tbody>';data.rows.forEach(r=>{const isLookup=r.is_lookup;const label=r.sample_name||r.key||'';const commentText=r.comment?r.comment:'';const bg=isLookup?'#fffbe6':'#fff';html+='<tr'+(isLookup?' style="background:#fffbe6;font-weight:600"':'')+'>';html+='<td style="position:sticky;left:0;background:'+bg+';z-index:1;border-right:1px solid #e0e0e0">'+label+'</td>';html+='<td style="position:sticky;left:100px;background:'+bg+';z-index:1;border-right:1px solid #e0e0e0;color:#888;font-size:11px">'+(commentText||'')+'</td>';data.tables.forEach(t=>{const cell=r.tables[t.key];let val='<span style="color:#ccc">—</span>';if(cell&&cell.meta){const v=cell.value;if(v===null||v===undefined)val='<em style="color:#999">NULL</em>';else val=String(v);if(isLookup)val='<span style="background:#e8f5e9;padding:2px 8px;border-radius:4px;font-weight:600">'+val+'</span>'}html+='<td style="text-align:center;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+val+'</td>'});html+='</tr>'});html+='</tbody></table></div>';el.innerHTML=html}catch(e){el.innerHTML='<p style="color:red">❌ '+(e.message||'요청 실패')+'</p>'}}loadOverview();loadTables();loadRelations();
</script>
</body>
</html>"""

@app.get("/")
def root():
    return HTMLResponse(DASHBOARD_HTML)

@app.get("/dashboard")
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


# ── PostgreSQL 테이블 목록 조회 ──────────────────────────

def get_pg_config() -> dict:
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "mydb"),
        "user": os.getenv("DB_USER", "myuser"),
        "password": os.getenv("DB_PASSWORD", ""),
    }

@app.post("/api/pg-first-rows")
def pg_first_rows(body: PipelineRequest):
    """선택한 테이블의 첫/마지막 행 병렬 조회"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from psycopg2.sql import SQL, Identifier
    cfg = get_pg_config()

    def fetch_one(ft: str):
        parts = ft.split(".", 1)
        schema = parts[0] if len(parts) == 2 else "public"
        table = parts[1] if len(parts) == 2 else parts[0]
        c = psycopg2.connect(**cfg)
        cu = c.cursor()
        try:
            if body.mode == "last":
                cu.execute(SQL("SELECT * FROM {} ORDER BY ctid DESC LIMIT 1").format(Identifier(schema, table)))
            else:
                cu.execute(SQL("SELECT * FROM {} LIMIT 1").format(Identifier(schema, table)))
            cols = [desc[0] for desc in cu.description]
            row = cu.fetchone()
            return (ft, dict(zip(cols, row)) if row else {})
        except Exception as e:
            return (ft, {"_error": str(e)})
        finally:
            cu.close(); c.close()

    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        fut_map = {ex.submit(fetch_one, ft): ft for ft in body.selected_tables}
        for fut in as_completed(fut_map):
            ft, data = fut.result()
            results[ft] = data
    return {"rows": results, "mode": body.mode}

@app.get("/api/pg-tables")
def list_pg_tables():
    """PostgreSQL에서 접근 가능한 테이블 목록 조회"""
    cfg = get_pg_config()
    try:
        conn = psycopg2.connect(**cfg)
        cur = conn.cursor()
        cur.execute("""
            SELECT t.table_schema, t.table_name,
                   pg_catalog.obj_description(
                     (quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass::oid,
                     'pg_class'
                   ) AS table_comment
            FROM information_schema.tables t
            WHERE t.table_schema NOT IN ('pg_catalog', 'information_schema')
              AND has_schema_privilege(t.table_schema, 'USAGE')
            ORDER BY t.table_schema, t.table_name
        """)
        rows = [{"schema": r[0], "table": r[1], "comment": r[2]} for r in cur.fetchall()]
        cur.close()
        conn.close()
        return {"tables": rows, "total": len(rows), "config": {k: v for k, v in cfg.items() if k != "password"}}
    except Exception as e:
        raise HTTPException(500, f"PostgreSQL 연결 실패: {e}")

class SemanticSuggestRequest(BaseModel):
    selected_tables: list[str]
    top_k: int = 20

@app.post("/api/table-semantic-suggest")
def table_semantic_suggest(body: SemanticSuggestRequest):
    """선택한 테이블과 COMMENT/테이블명 토큰이 유사한 테이블 제안"""
    import re
    from collections import Counter

    cfg = get_pg_config()
    try:
        conn = psycopg2.connect(**cfg)
        cur = conn.cursor()
        cur.execute("""
            SELECT t.table_schema, t.table_name,
                   pg_catalog.obj_description(
                     (quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass::oid,
                     'pg_class'
                   ) AS table_comment
            FROM information_schema.tables t
            WHERE t.table_schema NOT IN ('pg_catalog', 'information_schema')
              AND has_schema_privilege(t.table_schema, 'USAGE')
        """)
        all_tables = [{"schema": r[0], "table": r[1], "comment": r[2] or ""} for r in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception as e:
        raise HTTPException(500, f"PostgreSQL 연결 실패: {e}")

    # 토큰화
    def tokenize(text: str) -> set[str]:
        text = text.lower().strip()
        # 언더스코어/공백 분할 + 한글/영문 경계 분할
        tokens = set()
        for part in re.split(r'[\s_]+', text):
            if not part:
                continue
            # 한글/영문/숫자 경계 분할
            for sub in re.findall(r'[가-힣]+|[a-zA-Z][a-zA-Z0-9]*|[0-9]+', part):
                if len(sub) >= 1:
                    tokens.add(sub)
        return tokens

    # 테이블 인덱스
    table_index = {}
    for t in all_tables:
        key = f"{t['schema']}.{t['table']}"
        comment = t["comment"] or ""
        tokens = tokenize(t["table"] + " " + comment)
        table_index[key] = {
            "schema": t["schema"],
            "table": t["table"],
            "comment": t["comment"],
            "tokens": tokens,
        }

    # 선택된 테이블의 토큰 합집합
    selected_keys = set()
    selected_tokens = set()
    for ft in body.selected_tables:
        if ft in table_index:
            selected_keys.add(ft)
            selected_tokens |= table_index[ft]["tokens"]

    if not selected_tokens:
        return {"suggestions": []}

    # SQLite에서 각 테이블의 컬럼명 목록 가져오기
    col_map = {}
    cur2 = store.conn.cursor()
    for t in all_tables:
        key = f"{t['schema']}.{t['table']}"
        sch, tbl = t["schema"], t["table"]
        cur2.execute("""
            SELECT DISTINCT column_name FROM columns c
            JOIN tables t2 ON c.table_id = t2.id
            WHERE t2.schema_name = ? AND t2.table_name = ?
        """, (sch, tbl))
        cols = {r[0] for r in cur2.fetchall()}
        col_map[key] = cols

    # 선택된 테이블들의 컬럼명 합집합
    selected_cols = set()
    for ft in selected_keys:
        selected_cols |= col_map.get(ft, set())

    # 각 비선택 테이블에 대해 점수 계산
    suggestions = []
    for key, info in table_index.items():
        if key in selected_keys:
            continue
        # 토큰 오버랩 점수
        overlap = info["tokens"] & selected_tokens
        if not overlap:
            continue
        overlap_score = len(overlap) / max(len(selected_tokens), 1)
        # 컬럼 공유 점수
        other_cols = col_map.get(key, set())
        col_shared = other_cols & selected_cols
        col_score = len(col_shared) / max(len(selected_cols), 1) if selected_cols else 0
        # 종합 점수
        combined = 0.7 * overlap_score + 0.3 * col_score
        suggestions.append({
            "schema": info["schema"],
            "table": info["table"],
            "comment": info["comment"],
            "score": round(combined, 4),
            "overlap_tokens": sorted(overlap)[:10],
            "shared_columns": sorted(col_shared)[:10] if col_shared else [],
            "token_score": round(overlap_score, 4),
            "column_score": round(col_score, 4),
        })

    suggestions.sort(key=lambda x: -x["score"])
    if body.top_k:
        suggestions = suggestions[:body.top_k]

    return {"suggestions": suggestions}


# ── 전체 파이프라인 실행 ──────────────────────────────────

@app.post("/api/run-pipeline")
def run_pipeline(body: PipelineRequest):
    """선택한 테이블로 수집 → 분석 → 그래프 → 대시보드 생성"""
    if not body.selected_tables:
        raise HTTPException(400, "선택된 테이블이 없습니다")

    cfg = get_pg_config()
    selected = body.selected_tables
    try:
        # Phase 0: 기존 중복 데이터 정리
        cur_clean = store.conn.cursor()
        for ft in selected:
            parts = ft.split(".", 1)
            schema = parts[0] if len(parts) == 2 else "public"
            table = parts[1] if len(parts) == 2 else parts[0]
            cur_clean.execute("""
                DELETE FROM relationships WHERE source_column_id IN (
                    SELECT c.id FROM columns c JOIN tables t ON c.table_id=t.id
                    WHERE t.schema_name=? AND t.table_name=?
                ) OR target_column_id IN (
                    SELECT c.id FROM columns c JOIN tables t ON c.table_id=t.id
                    WHERE t.schema_name=? AND t.table_name=?
                )
            """, (schema, table, schema, table))
            cur_clean.execute("""
                DELETE FROM columns WHERE table_id IN (
                    SELECT id FROM tables WHERE schema_name=? AND table_name=?
                )
            """, (schema, table))
            cur_clean.execute("DELETE FROM tables WHERE schema_name=? AND table_name=?", (schema, table))
        store.conn.commit()

        # Phase 1: 스키마 수집
        conn = psycopg2.connect(**cfg)
        cur = conn.cursor()
        from collector.db_adapter import TableInfo, ColumnInfo, DatabaseInfo

        tables_info = []
        for ft in selected:
            parts = ft.split(".", 1)
            schema = parts[0] if len(parts) == 2 else "public"
            table = parts[1] if len(parts) == 2 else parts[0]
            cur.execute("""
                SELECT t.table_schema, t.table_name, t.table_type,
                       pg_catalog.obj_description(
                         (quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass::oid,
                         'pg_class'
                       ) AS table_comment
                FROM information_schema.tables t
                WHERE t.table_schema = %s AND t.table_name = %s
            """, (schema, table))
            row = cur.fetchone()
            if not row:
                continue

            ti = TableInfo(schema_name=row[0], table_name=row[1], table_type=row[2], description=row[3])

            cur.execute("""
                SELECT c.column_name, c.data_type, c.is_nullable, c.column_default,
                       c.character_maximum_length, c.numeric_precision, c.ordinal_position,
                       pgd.description,
                       CASE WHEN pk.column_name IS NOT NULL THEN true ELSE false END as is_pk
                FROM information_schema.columns c
                LEFT JOIN pg_catalog.pg_statio_all_tables st
                    ON c.table_schema = st.schemaname AND c.table_name = st.relname
                LEFT JOIN pg_catalog.pg_description pgd
                    ON pgd.objoid = st.relid AND pgd.objsubid = c.ordinal_position
                LEFT JOIN (
                    SELECT ku.column_name, tc.table_schema, tc.table_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage ku
                        ON tc.constraint_name = ku.constraint_name
                        AND tc.table_schema = ku.table_schema
                    WHERE tc.constraint_type = 'PRIMARY KEY'
                ) pk ON pk.table_schema = c.table_schema
                    AND pk.table_name = c.table_name
                    AND pk.column_name = c.column_name
                WHERE c.table_schema = %s AND c.table_name = %s
                ORDER BY c.ordinal_position
            """, (schema, table))
            cols = []
            for cr in cur.fetchall():
                col = ColumnInfo(
                    name=cr[0], data_type=cr[1], is_nullable=cr[2] == "YES",
                    default_value=cr[3], max_length=cr[4], numeric_precision=cr[5],
                    ordinal_position=cr[6], description=cr[7], is_primary_key=cr[8],
                )
                cols.append(col)

            cur.execute("""
                SELECT kcu.column_name, ccu.table_schema, ccu.table_name, ccu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                    ON ccu.constraint_name = tc.constraint_name
                    AND ccu.table_schema = tc.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                    AND tc.table_schema = %s AND tc.table_name = %s
            """, (schema, table))
            fk_map = {}
            for fk in cur.fetchall():
                fk_map[fk[0]] = f"{fk[1]}.{fk[2]}.{fk[3]}"
            for col in cols:
                if col.name in fk_map:
                    col.is_foreign_key = True
                    col.fk_references = fk_map[col.name]
            ti.columns = cols
            tables_info.append(ti)

        cur.close()
        conn.close()

        if not tables_info:
            return {"status": "error", "message": "선택한 테이블을 찾을 수 없습니다 (이름 확인 필요)"}

        # DatabaseInfo 생성 및 저장
        from collector.db_adapter import DatabaseInfo
        db_info = DatabaseInfo(
            name=cfg["dbname"], db_type="postgresql",
            host=cfg["host"], port=cfg["port"],
            database_name=cfg["dbname"],
        )
        db_info.tables = tables_info
        store.save_database(db_info)
        results = [db_info]
        total_tables = len(tables_info)
        total_cols = sum(len(t.columns) for t in tables_info)

        # Phase 2: 연관관계 분석
        orchestrator = RelationshipOrchestrator(store)
        rels = orchestrator.analyze_all()

        # Phase 2b: 값 기반 검증
        if rels:
            from analyzer.relationship_analyzer import DataSimilarityAnalyzer
            data_analyzer = DataSimilarityAnalyzer()
            value_rels = data_analyzer.fk_validation_by_values(
                candidates=rels, db_config=cfg, min_overlap_pct=0.5,
            )
            if value_rels:
                existing_types = {r.relation_type for r in rels}
                for vr in value_rels:
                    if "DATA_SIMILAR" not in existing_types:
                        src_id = store.resolve_column_id(
                            vr.source_db, vr.source_schema, vr.source_table, vr.source_column)
                        tgt_id = store.resolve_column_id(
                            vr.target_db, vr.target_schema, vr.target_table, vr.target_column)
                        if src_id and tgt_id:
                            store.save_relationship(
                                source_id=src_id, target_id=tgt_id,
                                relation_type=vr.relation_type,
                                confidence=vr.confidence, detected_by=vr.detected_by,
                            )
                            rels.append(vr)

        # Phase 3: 온톨로지 그래프 구축
        ont_graph = OntologyGraph()
        ont_graph.build_from_store(store)
        ont_graph.add_domain_nodes()
        stats = ont_graph.get_statistics()

        # Phase 4: 대시보드 생성
        provider = DashboardDataProvider(store.db_path)
        d3_data = ont_graph.to_d3_json()
        builder = DashboardHTMLBuilder(provider, d3_data)
        ie = InsightEngine(store, ont_graph.graph)
        builder.build_full_dashboard(f"{OUTPUT_DIR}/dashboard.html", insight_engine=ie)
        ont_graph.save_html_visualization(f"{OUTPUT_DIR}/ontology_graph.html")
        ont_graph.export_graphml(f"{OUTPUT_DIR}/ontology.graphml")

        return {
            "status": "ok",
            "databases": len(results),
            "tables": total_tables,
            "columns": total_cols,
            "relationships": len(rels),
            "dashboard": "/output/dashboard.html",
            "graph": "/output/ontology_graph.html",
        }
    except Exception as e:
        raise HTTPException(500, f"파이프라인 실행 실패: {e}")


# 정적 파일 마운트
app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")


@app.post("/api/reset-pipeline")
def reset_pipeline(body: PipelineRequest):
    """분석 데이터 초기화 (선택 테이블 또는 전체 삭제)"""
    cur = store.conn.cursor()
    selected = body.selected_tables
    if not selected:
        cur.execute("DELETE FROM relationships")
        cur.execute("DELETE FROM columns")
        cur.execute("DELETE FROM tables")
        cur.execute("DELETE FROM databases")
        deleted_tables = cur.rowcount  # not accurate for multi-delete but ok
    else:
        for ft in selected:
            parts = ft.split(".", 1)
            schema = parts[0] if len(parts) == 2 else "public"
            table = parts[1] if len(parts) == 2 else parts[0]
            cur.execute("""
                DELETE FROM relationships WHERE source_column_id IN (
                    SELECT c.id FROM columns c JOIN tables t ON c.table_id=t.id
                    WHERE t.schema_name=? AND t.table_name=?
                ) OR target_column_id IN (
                    SELECT c.id FROM columns c JOIN tables t ON c.table_id=t.id
                    WHERE t.schema_name=? AND t.table_name=?
                )
            """, (schema, table, schema, table))
            cur.execute("""
                DELETE FROM columns WHERE table_id IN (
                    SELECT id FROM tables WHERE schema_name=? AND table_name=?
                )
            """, (schema, table))
            cur.execute("DELETE FROM tables WHERE schema_name=? AND table_name=?", (schema, table))
        # orphan databases 정리
        cur.execute("""
            DELETE FROM databases WHERE id NOT IN (
                SELECT DISTINCT database_id FROM tables
            )
        """)
    store.conn.commit()
    return {"status": "ok", "deleted_tables": len(selected) if selected else 0}


@app.post("/api/field-lookup")
def field_lookup(body: FieldLookupRequest):
    """특정 필드명을 가진 모든 테이블의 첫 row → 매트릭스 반환"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from psycopg2.sql import SQL, Identifier

    cur = store.conn.cursor()
    # lookup_field 컬럼을 가진 테이블 ID 조회
    cur.execute("""
        SELECT DISTINCT c.table_id, t.schema_name, t.table_name, d.name as db_name,
               t.description as table_comment
        FROM columns c
        JOIN tables t ON c.table_id = t.id
        JOIN databases d ON t.database_id = d.id
        WHERE c.column_name = ?
        ORDER BY t.schema_name, t.table_name
    """, (body.field_name,))
    table_rows = cur.fetchall()
    if not table_rows:
        return {"field_name": body.field_name, "tables": [], "rows": []}

    table_ids = [r[0] for r in table_rows]
    tables_meta = []
    table_keys = []
    for r in table_rows:
        tid, schema, table, db, tbl_desc = r
        key = f"{db}.{schema}.{table}"
        table_keys.append(key)
        tables_meta.append({"key": key, "schema": schema, "table": table, "database": db, "table_comment": tbl_desc})

    # 해당 테이블들의 모든 컬럼 조회
    placeholders = ",".join("?" * len(table_ids))
    cur.execute(f"""
        SELECT c.column_name, c.data_type, c.description, c.is_primary_key,
               c.is_foreign_key, c.ordinal_position,
               t.schema_name, t.table_name, d.name as db_name
        FROM columns c
        JOIN tables t ON c.table_id = t.id
        JOIN databases d ON t.database_id = d.id
        WHERE c.table_id IN ({placeholders})
        ORDER BY t.schema_name, t.table_name, c.ordinal_position
    """, table_ids)
    col_rows = cur.fetchall()

    # 테이블명 → 컬럼 목록 맵
    from collections import OrderedDict, defaultdict
    tbl_cols = OrderedDict()
    for tm in tables_meta:
        tbl_cols[tm["key"]] = {"table_comment": tm.get("table_comment", "") or "", "columns": []}
    for r in col_rows:
        col_name, data_type, desc, is_pk, is_fk, ord_pos, schema, table, db = r
        key = f"{db}.{schema}.{table}"
        if key in tbl_cols:
            tbl_cols[key]["columns"].append({
                "name": col_name, "data_type": data_type, "description": desc,
                "is_primary_key": bool(is_pk), "is_foreign_key": bool(is_fk),
                "ordinal_position": ord_pos,
            })

    # 시맨틱 키 그룹핑: COMMENT → 컬럼명
    col_concepts = OrderedDict()
    for key, tc in tbl_cols.items():
        for col in tc["columns"]:
            sk = col["description"] or col["name"]
            if sk not in col_concepts:
                col_concepts[sk] = {"key": sk, "label": col["description"] or col["name"],
                                    "comment": col["description"] or None,
                                    "sample_name": col["name"], "frequency": 0, "tables": {}}
            col_concepts[sk]["frequency"] += 1
            col_concepts[sk]["tables"][key] = col

    # 제외할 개념 (skip patterns)
    SKIP_NAMES = {"id", "created_at", "updated_at", "modified_at", "create_at", "update_at",
                  "version", "delflag", "is_deleted", "deleted_at", "rowversion", "seq",
                  "created", "modified", "deleted", "timestamp", "row_id", "serial#"}

    def should_skip(cc: dict) -> bool:
        n = cc["label"].lower()
        if n in SKIP_NAMES:
            return True
        # 컬럼명 자체가 skip 패턴인 경우 (COMMENT가 있으면 유지)
        if cc["comment"]:
            return False
        base = cc["sample_name"].lower()
        if base in SKIP_NAMES:
            return True
        return False

    # 조회 필드가 아닌 개념 중 skip 제외
    rows_out = []
    for sk, cc in col_concepts.items():
        if cc["sample_name"] == body.field_name:
            cc["is_lookup"] = True
        else:
            cc["is_lookup"] = False
            if should_skip(cc):
                continue
        rows_out.append(cc)

    # PostgreSQL 병렬 첫줄 조회
    cfg = get_pg_config()

    row_data = {}
    def fetch_first(tm: dict):
        key = tm["key"]
        try:
            c = psycopg2.connect(**cfg)
            cu = c.cursor()
            cu.execute(SQL("SELECT * FROM {} LIMIT 1").format(Identifier(tm["schema"], tm["table"])))
            cols = [desc[0] for desc in cu.description]
            row = cu.fetchone()
            cu.close(); c.close()
            row_data[key] = dict(zip(cols, row)) if row else {}
        except Exception as e:
            row_data[key] = {"_error": str(e)}

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(fetch_first, tm) for tm in tables_meta]
        for fut in as_completed(futs):
            pass  # results collected in row_data

    # 값 채우기
    for cc in rows_out:
        for key in table_keys:
            rd = row_data.get(key, {})
            col = cc["tables"].get(key)
            if col and rd and not rd.get("_error"):
                cc["tables"][key] = rd.get(col["name"])
            elif col:
                cc["tables"][key] = None
            else:
                cc["tables"][key] = None
            # col 정보에 값 포함
            if col and key in cc["tables"]:
                cc["tables"][key + "_meta"] = {
                    "name": col["name"], "data_type": col["data_type"],
                    "description": col["description"],
                    "is_primary_key": col["is_primary_key"],
                }

    # 정렬: lookup 우선 → COMMENT 있는 것 → frequency desc → label
    def sort_key(cc):
        if cc["is_lookup"]:
            return (0, 0, "")
        return (1, 0 if cc["comment"] else 1, -cc["frequency"], cc["label"])

    rows_out.sort(key=sort_key)

    # 메타 정보 정리 (불필요한 원본 col dict 제거)
    for cc in rows_out:
        for key in table_keys:
            if key + "_meta" in cc["tables"]:
                cc["tables"][key] = {
                    "value": cc["tables"][key],
                    "meta": cc["tables"][key + "_meta"],
                }
                del cc["tables"][key + "_meta"]
            else:
                cc["tables"][key] = {"value": None, "meta": None}

    return {
        "field_name": body.field_name,
        "tables": tables_meta,
        "rows": rows_out,
    }


# 실행: uvicorn src.api.server:app --reload --port 8000
if __name__ == "__main__":
    import uvicorn
    print("🚀 DB Ontology API 서버 시작: http://localhost:8000")
    print("📖 API 문서: http://localhost:8000/docs")
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
