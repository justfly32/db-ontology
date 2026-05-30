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

from fastapi import FastAPI, HTTPException, Query, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from collector.db_adapter import MetadataStore, SchemaCollector
from analyzer.relationship_analyzer import RelationshipOrchestrator
from ontology.graph_builder import OntologyGraph
from collector.drift_detector import SchemaDriftDetector, ChangeHistoryManager
from api.auth import (
    rbac, get_user_store, get_jwt_manager, create_default_admin,
    Role, User,
)


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


# ── Pydantic 모델 ──────────────────────────────────────

class DatabaseConfig(BaseModel):
    db_type: str
    host: Optional[str] = "localhost"
    port: Optional[int] = None
    database: str = ""
    user: Optional[str] = ""
    password: Optional[str] = ""
    db_name: Optional[str] = ""

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
def get_overview(user: User = Depends(require("graph:read"))):
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
    """테이블 목록"""
    cur = store.conn.cursor()
    if database_id:
        cur.execute("""
            SELECT t.id, t.table_name, t.schema_name, t.row_count, t.description, d.name
            FROM tables t JOIN databases d ON t.database_id = d.id
            WHERE t.database_id = ? ORDER BY t.table_name
        """, (database_id,))
    else:
        cur.execute("""
            SELECT t.id, t.table_name, t.schema_name, t.row_count, t.description, d.name
            FROM tables t JOIN databases d ON t.database_id = d.id
            ORDER BY d.name, t.table_name
        """)
    return [dict(zip(["id","name","schema","rows","description","database"], r)) for r in cur.fetchall()]


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
def search(
    q: str = Query(..., min_length=1, description="검색어"),
    user: User = Depends(require("search:read")),
):
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


@app.get("/api/databases")
def list_databases(user: User = Depends(require("table:read"))):
    """데이터베이스 목록"""
    cur = store.conn.cursor()
    cur.execute("SELECT id, name, db_type, host, database_name, description FROM databases")
    return [dict(zip(["id","name","db_type","host","database","description"], r)) for r in cur.fetchall()]


@app.get("/api/tables")
def list_tables(
    database_id: Optional[int] = None,
    user: User = Depends(require("table:read")),
):
    """테이블 목록"""
    cur = store.conn.cursor()
    if database_id:
        cur.execute("""
            SELECT t.id, t.table_name, t.schema_name, t.row_count, t.description, d.name
            FROM tables t JOIN databases d ON t.database_id = d.id
            WHERE t.database_id = ? ORDER BY t.table_name
        """, (database_id,))
    else:
        cur.execute("""
            SELECT t.id, t.table_name, t.schema_name, t.row_count, t.description, d.name
            FROM tables t JOIN databases d ON t.database_id = d.id
            ORDER BY d.name, t.table_name
        """)
    return [dict(zip(["id","name","schema","rows","description","database"], r)) for r in cur.fetchall()]


@app.get("/api/tables/{table_id}")
def get_table_detail(table_id: int, user: User = Depends(require("table:read"))):
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


@app.get("/api/graph")
def get_graph(format: str = "d3", user: User = Depends(require("graph:read"))):
    """그래프 데이터 (D3.js / Cytoscape)"""
    graph.build_from_store(store)
    graph.add_domain_nodes()
    if format == "cytoscape":
        return graph.to_cytoscape_json()
    return graph.to_d3_json()


@app.get("/api/graph/path")
def find_path(source: str, target: str, user: User = Depends(require("graph:read"))):
    """두 테이블 간 경로"""
    path = graph.find_path(source, target)
    if not path:
        return {"path": [], "found": False}
    return {"path": path, "found": True, "length": len(path) - 1}


@app.get("/api/relationships")
def list_relationships(
    type: Optional[str] = None,
    min_confidence: float = 0.0,
    limit: int = 100,
    user: User = Depends(require("relationship:read")),
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
    query += " ORDER BY r.confidence DESC LIMIT ?"
    params.append(limit)

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
def check_drift(user: User = Depends(require("drift:read"))):
    """스키마 드리프트 확인"""
    detector = SchemaDriftDetector(store.db_path)
    return detector.detect_changes()


@app.get("/api/history")
def get_history(days: int = 7, user: User = Depends(require("drift:read"))):
    """변경 이력"""
    history = ChangeHistoryManager(store.db_path)
    return history.get_history(days=days)


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


# 실행: uvicorn src.api.server:app --reload --port 8000
if __name__ == "__main__":
    import uvicorn
    print("🚀 DB Ontology API 서버 시작: http://localhost:8000")
    print("📖 API 문서: http://localhost:8000/docs")
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
