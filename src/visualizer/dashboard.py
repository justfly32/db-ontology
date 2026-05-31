"""
Ontology Dashboard - 통합 시각화 대시보드
검색/필터링, 경로 탐색, 도메인 클러스터링, 관계 상세 패널
"""

import os
import json
import sqlite3
from typing import Optional
from datetime import datetime

import networkx as nx
from analyzer.insight_engine import InsightEngine


class DashboardDataProvider:
    """대시보드용 데이터 제공자"""

    def __init__(self, store_path: str = "~/.hermes/data/ontology_metadata.db"):
        self.store_path = os.path.expanduser(store_path)
        self.conn = sqlite3.connect(self.store_path)
        self.conn.row_factory = sqlite3.Row

    def get_overview(self) -> dict:
        """전체 요약 통계"""
        cur = self.conn.cursor()
        databases = cur.execute("SELECT COUNT(*) FROM databases").fetchone()[0]
        tables = cur.execute("SELECT COUNT(*) FROM tables").fetchone()[0]
        columns = cur.execute("SELECT COUNT(*) FROM columns").fetchone()[0]
        relationships = cur.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
        fk_count = cur.execute("SELECT COUNT(*) FROM relationships WHERE relation_type='FK'").fetchone()[0]
        verified = cur.execute("SELECT COUNT(*) FROM relationships WHERE verified=1").fetchone()[0]

        # 도메인 분포
        cur.execute("""
            SELECT t.table_name, COUNT(c.id) as col_count
            FROM tables t
            JOIN columns c ON c.table_id = t.id
            GROUP BY t.id
            ORDER BY col_count DESC
            LIMIT 10
        """)
        top_tables = [{"table": row[0], "columns": row[1]} for row in cur.fetchall()]

        # 관계 유형 분포
        cur.execute("""
            SELECT relation_type, COUNT(*) as cnt, AVG(confidence) as avg_conf
            FROM relationships
            GROUP BY relation_type
            ORDER BY cnt DESC
        """)
        rel_dist = [{"type": row[0], "count": row[1], "avg_confidence": round(row[2], 2)} for row in cur.fetchall()]

        return {
            "databases": databases,
            "tables": tables,
            "columns": columns,
            "relationships": relationships,
            "fk_count": fk_count,
            "verified": verified,
            "top_tables": top_tables,
            "relationship_distribution": rel_dist,
        }

    def search(self, query: str, limit: int = 50) -> dict:
        """테이블/필드 통합 검색"""
        cur = self.conn.cursor()
        pattern = f"%{query}%"

        # 테이블 검색
        cur.execute("""
            SELECT t.id, t.table_name, t.schema_name, t.description,
                   d.name as db_name, COUNT(c.id) as col_count
            FROM tables t
            JOIN databases d ON t.database_id = d.id
            LEFT JOIN columns c ON c.table_id = t.id
            WHERE t.table_name LIKE ? OR t.description LIKE ?
            GROUP BY t.id
            ORDER BY t.table_name
            LIMIT ?
        """, (pattern, pattern, limit))
        tables = [{"id": r[0], "name": r[1], "schema": r[2], "description": r[3],
                    "database": r[4], "columns": r[5]} for r in cur.fetchall()]

        # 컬럼 검색
        cur.execute("""
            SELECT c.id, c.column_name, c.data_type, c.description,
                   c.is_primary_key, c.is_foreign_key,
                   t.table_name, t.schema_name, d.name as db_name
            FROM columns c
            JOIN tables t ON c.table_id = t.id
            JOIN databases d ON t.database_id = d.id
            WHERE c.column_name LIKE ? OR c.description LIKE ?
            ORDER BY c.column_name
            LIMIT ?
        """, (pattern, pattern, limit))
        columns = [{"id": r[0], "name": r[1], "type": r[2], "description": r[3],
                     "is_pk": bool(r[4]), "is_fk": bool(r[5]),
                     "table": r[6], "schema": r[7], "database": r[8]} for r in cur.fetchall()]

        return {"tables": tables, "columns": columns, "query": query}

    def get_table_detail(self, table_id: int) -> dict:
        """테이블 상세 정보 + 관계"""
        cur = self.conn.cursor()

        # 테이블 기본 정보
        cur.execute("""
            SELECT t.table_name, t.schema_name, t.description, t.row_count,
                   d.name as db_name
            FROM tables t
            JOIN databases d ON t.database_id = d.id
            WHERE t.id = ?
        """, (table_id,))
        row = cur.fetchone()
        if not row:
            return {}

        table_info = {
            "name": row[0], "schema": row[1], "description": row[2],
            "row_count": row[3], "database": row[4],
        }

        # 컬럼 목록
        cur.execute("""
            SELECT id, column_name, data_type, is_nullable, is_primary_key,
                   is_foreign_key, fk_references, description, ordinal_position
            FROM columns WHERE table_id = ?
            ORDER BY ordinal_position
        """, (table_id,))
        columns = []
        for r in cur.fetchall():
            columns.append({
                "id": r[0], "name": r[1], "type": r[2],
                "nullable": bool(r[3]), "is_pk": bool(r[4]),
                "is_fk": bool(r[5]), "fk_ref": r[6], "description": r[7],
            })
        table_info["columns"] = columns

        # 관련 관계
        cur.execute("""
            SELECT r.relation_type, r.confidence, r.detected_by, r.notes,
                   c1.column_name as source_col, t1.table_name as source_table,
                   c2.column_name as target_col, t2.table_name as target_table
            FROM relationships r
            JOIN columns c1 ON r.source_column_id = c1.id
            JOIN tables t1 ON c1.table_id = t1.id
            JOIN columns c2 ON r.target_column_id = c2.id
            JOIN tables t2 ON c2.table_id = t2.id
            WHERE c1.table_id = ? OR c2.table_id = ?
            ORDER BY r.confidence DESC
        """, (table_id, table_id))
        relationships = []
        for r in cur.fetchall():
            relationships.append({
                "type": r[0], "confidence": r[1], "detected_by": r[2], "notes": r[3],
                "source_col": r[4], "source_table": r[5],
                "target_col": r[6], "target_table": r[7],
            })
        table_info["relationships"] = relationships

        return table_info

    def get_relationship_detail(self, rel_id: int) -> dict:
        """관계 상세 정보"""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT r.relation_type, r.confidence, r.detected_by, r.notes,
                   r.detected_at, r.verified,
                   c1.column_name, t1.table_name, d1.name,
                   c2.column_name, t2.table_name, d2.name
            FROM relationships r
            JOIN columns c1 ON r.source_column_id = c1.id
            JOIN tables t1 ON c1.table_id = t1.id
            JOIN databases d1 ON t1.database_id = d1.id
            JOIN columns c2 ON r.target_column_id = c2.id
            JOIN tables t2 ON c2.table_id = t2.id
            JOIN databases d2 ON t2.database_id = d2.id
            WHERE r.id = ?
        """, (rel_id,))
        row = cur.fetchone()
        if not row:
            return {}
        return {
            "type": row[0], "confidence": row[1], "detected_by": row[2],
            "notes": row[3], "detected_at": row[4], "verified": bool(row[5]),
            "source": {"column": row[6], "table": row[7], "database": row[8]},
            "target": {"column": row[9], "table": row[10], "database": row[11]},
        }

    def get_domain_clusters(self) -> list:
        """도메인별 클러스터"""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT DISTINCT c.column_name
            FROM columns c
            WHERE c.column_name LIKE '%_id' OR c.column_name LIKE '%_name'
               OR c.column_name LIKE '%_date' OR c.column_name LIKE '%_status'
               OR c.column_name LIKE '%_type' OR c.column_name LIKE '%_code'
            LIMIT 100
        """)
        return [row[0] for row in cur.fetchall()]

    def get_interpretation(self) -> dict:
        """그래프 해석을 위한 구조화된 데이터"""
        cur = self.conn.cursor()

        # DB 기본 정보
        cur.execute("SELECT name, db_type FROM databases LIMIT 1")
        db_row = cur.fetchone()
        db_name = db_row[0] if db_row else "Unknown"
        db_type = db_row[1] if db_row else "Unknown"

        # 스키마별 구성
        cur.execute("""
            SELECT t.schema_name, COUNT(t.id) as table_cnt,
                   GROUP_CONCAT(t.table_name) as table_list
            FROM tables t
            GROUP BY t.schema_name
            ORDER BY t.schema_name
        """)
        schemas = []
        for r in cur.fetchall():
            schemas.append({
                "name": r[0], "table_count": r[1], "tables": r[2].split(","),
            })

        # 요약
        cur.execute("SELECT COUNT(*) FROM tables")
        total_tables = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM columns")
        total_columns = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM relationships")
        total_relations = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM relationships WHERE relation_type='FK'")
        fk_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM relationships WHERE relation_type='DATA_SIMILAR'")
        value_based_count = cur.fetchone()[0]

        schema_desc = " · ".join(
            f"{s['name']}({s['table_count']}개)"
            for s in schemas
        )

        summary_desc = (
            f"{db_name}는 {len(schemas)}개 스키마({schema_desc}), "
            f"총 {total_tables}개 테이블, {total_columns}개 컬럼, "
            f"{total_relations}개 관계로 구성된 데이터베이스입니다. "
        )
        if fk_count > 0:
            summary_desc += (
                f"명시적 외래키(FK)는 {fk_count}개 탐지되었고, "
            )
        if value_based_count > 0:
            summary_desc += (
                f"FK 제약 없이 실제 데이터 값 기반으로 탐지된 관계는 "
                f"{value_based_count}개입니다."
            )

        # 주요 관계 (FK + DATA_SIMILAR, 신뢰도 상위)
        cur.execute("""
            SELECT r.relation_type, r.confidence,
                   sc.column_name, st.table_name, st.schema_name,
                   tc.column_name, tt.table_name, tt.schema_name
            FROM relationships r
            JOIN columns sc ON r.source_column_id = sc.id
            JOIN tables st ON sc.table_id = st.id
            JOIN columns tc ON r.target_column_id = tc.id
            JOIN tables tt ON tc.table_id = tt.id
            WHERE r.relation_type IN ('FK', 'DATA_SIMILAR')
              AND r.confidence >= 0.7
            ORDER BY r.confidence DESC
            LIMIT 20
        """)
        key_relations = []
        for r in cur.fetchall():
            note = ""
            if r[0] == "FK":
                note = "명시적 외래키(FK) 제약조건"
            elif r[0] == "DATA_SIMILAR":
                note = f"값 기반 탐지 (FK 제약 없음, 신뢰도 {r[1]:.0%})"
            key_relations.append({
                "type": r[0], "confidence": r[1],
                "source": f"{r[3]}.{r[2]}", "target": f"{r[6]}.{r[5]}",
                "note": note,
            })

        # 값 기반 관계 (상세 정보)
        cur.execute("""
            SELECT r.confidence,
                   sc.column_name, st.table_name, st.schema_name,
                   tc.column_name, tt.table_name, tt.schema_name
            FROM relationships r
            JOIN columns sc ON r.source_column_id = sc.id
            JOIN tables st ON sc.table_id = st.id
            JOIN columns tc ON r.target_column_id = tc.id
            JOIN tables tt ON tc.table_id = tt.id
            WHERE r.relation_type = 'DATA_SIMILAR'
            ORDER BY r.confidence DESC
        """)
        value_based = []
        for r in cur.fetchall():
            pct = f"{r[0]:.0%}"
            value_based.append({
                "source": f"{r[3]}.{r[1]}", "target": f"{r[6]}.{r[5]}",
                "overlap": pct,
                "note": f"{r[2]}.{r[1]} → {r[5]}.{r[4]} (중복률: {pct})",
            })

        # 주의사항: 다른 의미지만 값이 겹친 관계 탐지
        warnings = []
        suspicious_pairs = [
            ("dept_id", "emp_id", "부서 ID와 직원 ID는 숫자 값이 겹칠 수 있지만 의미가 다름"),
            ("item_id", "product_id", "주문 항목 ID와 상품 ID는 같은 숫자 체계일 수 있음"),
            ("total_amount", "amount", "총 금액과 개별 금액은 의미 범위가 다름"),
        ]
        for r in key_relations:
            src_col = r["source"].split(".")[-1]
            tgt_col = r["target"].split(".")[-1]
            for sc, tc, msg in suspicious_pairs:
                if (sc in src_col and tc in tgt_col) or (tc in src_col and sc in tgt_col):
                    if r["type"] != "FK":
                        warnings.append(
                            f"⚠️  {r['source']} ↔ {r['target']}: {msg}"
                        )

        cur.close()
        return {
            "summary": {
                "db_name": db_name,
                "db_type": db_type,
                "total_schemas": len(schemas),
                "total_tables": total_tables,
                "total_columns": total_columns,
                "total_relations": total_relations,
                "fk_count": fk_count,
                "value_based_count": value_based_count,
                "description": summary_desc,
            },
            "schemas": schemas,
            "key_relations": key_relations,
            "value_based": value_based,
            "warnings": warnings,
        }

    def close(self):
        self.conn.close()


class DashboardHTMLBuilder:
    """대시보드 HTML 빌더"""

    def __init__(self, data_provider: DashboardDataProvider, graph_data: dict):
        self.data = data_provider
        self.graph_data = graph_data

    def build_full_dashboard(self, output_path: str, insight_engine: InsightEngine = None):
        """통합 대시보드 HTML 생성"""
        overview = self.data.get_overview()
        today = datetime.now().strftime("%Y-%m-%d %H:%M")

        insight_data = None
        if insight_engine:
            insight_data = {
                "entities": insight_engine.detect_entities(),
                "impactful_tables": insight_engine.find_impactful_tables(min_connections=2),
                "quality": insight_engine.quality_report(),
                "join_paths": insight_engine.find_all_join_paths(max_pairs=10),
                "recommendations": insight_engine.generate_recommendations(max_results=20),
            }

        html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DB 온톨로지 대시보드</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
:root {{--bg:#0d1117;--surface:#161b22;--surface2:#21262d;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--blue:#58a6ff;--green:#3fb950;--red:#f85149;--yellow:#d29922;--purple:#a371f7}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans KR',sans-serif;background:var(--bg);color:var(--text);line-height:1.6}}
.container{{max-width:1400px;margin:0 auto;padding:20px}}
h1{{font-size:24px;margin-bottom:4px}}
h2{{font-size:18px;margin-bottom:12px;color:var(--blue)}}
h3{{font-size:15px;margin-bottom:8px;color:var(--green)}}
.subtitle{{color:var(--muted);font-size:13px;margin-bottom:20px}}

/* 통계 카드 */
.stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}}
.stat-card{{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;text-align:center}}
.stat-card .number{{font-size:28px;font-weight:700;color:var(--blue)}}
.stat-card .label{{font-size:12px;color:var(--muted);margin-top:4px}}

/* 탭 네비게이션 */
.tabs{{display:flex;gap:4px;margin-bottom:20px;border-bottom:1px solid var(--border)}}
.tab{{padding:10px 20px;background:var(--surface);border:1px solid var(--border);border-bottom:none;border-radius:8px 8px 0 0;cursor:pointer;font-size:14px;color:var(--muted)}}
.tab.active{{background:var(--surface2);color:var(--text);border-color:var(--blue)}}
.tab:hover{{color:var(--text)}}

/* 패널 */
.panel{{display:none;background:var(--surface);border:1px solid var(--border);border-radius:0 0 12px 12px;padding:20px}}
.panel.active{{display:block}}

/* 그래프 영역 */
#graph-container{{width:100%;height:500px;background:#0a0e14;border-radius:8px;border:1px solid var(--border);position:relative;overflow:hidden}}
#graph-svg{{width:100%;height:100%}}

/* 검색 */
.search-box{{display:flex;gap:8px;margin-bottom:16px}}
.search-box input{{flex:1;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:8px;font-size:14px}}
.search-box input:focus{{outline:none;border-color:var(--blue)}}
.search-box button{{background:var(--blue);color:#fff;border:none;padding:10px 20px;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600}}
.search-box button:hover{{opacity:.9}}

/* 결과 리스트 */
.result-list{{max-height:400px;overflow-y:auto}}
.result-item{{padding:12px;border-bottom:1px solid var(--border);cursor:pointer}}
.result-item:hover{{background:var(--surface2)}}
.result-item .name{{font-weight:600;font-size:14px}}
.result-item .meta{{font-size:12px;color:var(--muted);margin-top:2px}}
.result-item .badge{{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600;margin-left:6px}}
.badge-pk{{background:rgba(248,81,73,.2);color:var(--red)}}
.badge-fk{{background:rgba(210,153,34,.2);color:var(--yellow)}}
.badge-match{{background:rgba(63,185,80,.2);color:var(--green)}}

/* 관계 테이블 */
.rel-table{{width:100%;border-collapse:collapse;font-size:13px}}
.rel-table th{{text-align:left;padding:10px 12px;color:var(--muted);font-weight:500;border-bottom:2px solid var(--border);font-size:11px;text-transform:uppercase;letter-spacing:.5px}}
.rel-table td{{padding:10px 12px;border-bottom:1px solid var(--border)}}
.rel-table tr:hover td{{background:var(--surface2)}}
.confidence-bar{{display:inline-block;height:6px;border-radius:3px;background:var(--surface2);width:60px;vertical-align:middle;margin-right:6px}}
.confidence-fill{{height:100%;border-radius:3px}}

/* 경로 */
.path-container{{background:var(--surface2);border-radius:8px;padding:16px;margin-top:12px}}
.path-step{{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;background:var(--surface);border-radius:6px;font-size:13px;margin:2px}}
.path-arrow{{color:var(--muted);font-size:16px}}

/* 인사이트 패널 */
.section{{margin-bottom:20px}}
.section-title{{font-weight:600;font-size:15px;color:var(--green);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)}}
.entity-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}}
.entity-card{{background:var(--surface2);border-radius:8px;padding:14px;border-left:3px solid var(--purple)}}
.entity-name{{font-weight:600;font-size:14px;color:var(--purple);margin-bottom:4px}}
.entity-tables{{font-size:12px;color:var(--muted);line-height:1.6}}
.entity-count{{font-size:11px;color:var(--muted);margin-top:6px}}
.hub-item{{background:var(--surface2);border-radius:6px;padding:10px;margin-bottom:6px}}
.hub-bar{{height:4px;background:var(--surface);border-radius:2px;margin:4px 0}}
.hub-fill{{height:100%;background:var(--blue);border-radius:2px}}
.path-card{{background:var(--surface2);border-radius:6px;padding:10px;margin-bottom:6px}}
.quality-item{{padding:3px 0;font-size:12px}}
.rec-card{{background:var(--surface2);border-radius:8px;padding:12px;margin-bottom:8px}}
.rec-title{{font-weight:600;font-size:13px;color:var(--text)}}
.rec-score{{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:700;color:#fff}}

/* 도메인 클러스터 */
.domain-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}}
.domain-card{{background:var(--surface2);border-radius:10px;padding:16px;border-left:3px solid var(--purple)}}
.domain-card .domain-name{{font-weight:600;font-size:15px;margin-bottom:8px;color:var(--purple)}}
.domain-card .field-list{{font-size:12px;color:var(--muted)}}
.domain-card .field-item{{padding:3px 0;border-bottom:1px solid var(--border)}}

/* 범례 */
.legend{{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px;font-size:12px}}
.legend-item{{display:flex;align-items:center;gap:5px}}
.legend-color{{width:10px;height:10px;border-radius:50%}}

/* 툴팁 */
.tooltip{{position:fixed;background:var(--surface);border:1px solid var(--border);padding:10px;border-radius:6px;font-size:12px;pointer-events:none;display:none;z-index:100;max-width:300px;box-shadow:0 4px 12px rgba(0,0,0,.3)}}

/* 해석 패널 */
.interpretation-toggle{{margin-top:12px;padding:10px 16px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;cursor:pointer;font-size:14px;color:var(--blue);user-select:none;display:flex;align-items:center;gap:6px}}
.interpretation-toggle:hover{{background:var(--border)}}
.interpretation-toggle .arrow{{transition:transform .2s;font-size:12px}}
.interpretation-toggle.open .arrow{{transform:rotate(180deg)}}
.interpretation-panel{{margin-top:0;background:var(--surface);border:1px solid var(--border);border-top:none;border-radius:0 0 8px 8px;padding:16px;font-size:13px;line-height:1.7;color:var(--text)}}
.interpretation-panel p{{margin-bottom:8px}}
.interpretation-panel strong{{color:var(--blue)}}
.interpretation-panel .section{{margin-bottom:16px}}
.interpretation-panel .section-title{{font-weight:600;font-size:14px;color:var(--green);margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)}}
.interpretation-panel .rel-item{{padding:4px 0;display:flex;align-items:center;gap:8px}}
.interpretation-panel .rel-type{{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600}}
.interpretation-panel .rel-type.FK{{background:rgba(248,81,73,.2);color:var(--red)}}
.interpretation-panel .rel-type.DATA_SIMILAR{{background:rgba(163,113,247,.2);color:var(--purple)}}
.interpretation-panel .warning{{padding:8px 12px;background:rgba(210,153,34,.1);border-left:3px solid var(--yellow);border-radius:4px;margin:6px 0;font-size:12px;color:var(--yellow)}}
</style>
</head>
<body>
<div class="container">
<h1>🗄️ DB 온톨로지 대시보드</h1>
<div class="subtitle">{today} 기준 | 자동 분석 결과</div>

<!-- 통계 카드 -->
<div class="stats-grid">
    <div class="stat-card"><div class="number">{overview['databases']}</div><div class="label">데이터베이스</div></div>
    <div class="stat-card"><div class="number">{overview['tables']}</div><div class="label">테이블</div></div>
    <div class="stat-card"><div class="number">{overview['columns']}</div><div class="label">컬럼</div></div>
    <div class="stat-card"><div class="number">{overview['relationships']}</div><div class="label">관계</div></div>
    <div class="stat-card"><div class="number">{overview['fk_count']}</div><div class="label">외래키</div></div>
    <div class="stat-card"><div class="number">{overview['verified']}</div><div class="label">검증됨</div></div>
</div>

<!-- 탭 -->
<div class="tabs">
    <div class="tab active" onclick="switchTab('graph')">📊 그래프</div>
    <div class="tab" onclick="switchTab('search')">🔍 검색</div>
    <div class="tab" onclick="switchTab('relationships')">🔗 관계</div>
    <div class="tab" onclick="switchTab('insights')">💡 인사이트</div>
    <div class="tab" onclick="switchTab('domains')">🏷️ 도메인</div>
</div>

<!-- 그래프 탭 -->
<div id="tab-graph" class="panel active">
    <div class="legend">
        <div class="legend-item"><div class="legend-color" style="background:#58a6ff"></div>DB</div>
        <div class="legend-item"><div class="legend-color" style="background:#3fb950"></div>테이블</div>
        <div class="legend-item"><div class="legend-color" style="background:#d29922"></div>컬럼</div>
        <div class="legend-item"><div class="legend-color" style="background:#f85149"></div>PK</div>
        <div class="legend-item"><div class="legend-color" style="background:#a371f7"></div>관계</div>
    </div>
    <div id="graph-container"><svg id="graph-svg"></svg></div>
    <div class="interpretation-toggle" onclick="toggleInterpretation()">
        <span class="arrow">▾</span> 📋 해석 보기
    </div>
    <div id="interpretation-panel" class="interpretation-panel" style="display:none"></div>
</div>

<!-- 검색 탭 -->
<div id="tab-search" class="panel">
    <div class="search-box">
        <input type="text" id="search-input" placeholder="테이블명, 필드명, 설명 검색..." onkeydown="if(event.key==='Enter')doSearch()">
        <button onclick="doSearch()">검색</button>
    </div>
    <div id="search-results">
        <p style="color:var(--muted);padding:20px;text-align:center">검색어를 입력하세요</p>
    </div>
</div>

<!-- 관계 탭 -->
<div id="tab-relationships" class="panel">
    <h3>탐지된 연관관계</h3>
    <table class="rel-table">
    <thead><tr><th>유형</th><th>소스</th><th>대상</th><th>신뢰도</th><th>탐지방법</th></tr></thead>
    <tbody id="rel-tbody"></tbody>
    </table>
</div>

<!-- 인사이트 탭 -->
<div id="tab-insights" class="panel">
    <div id="insight-content"></div>
</div>

<!-- 도메인 탭 -->
<div id="tab-domains" class="panel">
    <h3>도메인별 클러스터</h3>
    <div class="domain-grid" id="domain-grid"></div>
</div>
</div>

<div class="tooltip" id="tooltip"></div>

<script>
// 그래프 데이터
const graphData = {json.dumps(self.graph_data, ensure_ascii=False)};

// 관계 데이터
const relData = {json.dumps(overview.get('relationship_distribution', []), ensure_ascii=False)};

// 해석 데이터
const interpData = {json.dumps(self.data.get_interpretation(), ensure_ascii=False)};

// 해석 토글
function toggleInterpretation() {{
    const panel = document.getElementById('interpretation-panel');
    const toggle = document.querySelector('.interpretation-toggle');
    if (panel.style.display === 'none') {{
        panel.style.display = 'block';
        toggle.classList.add('open');
        renderInterpretation();
    }} else {{
        panel.style.display = 'none';
        toggle.classList.remove('open');
    }}
}}

function renderInterpretation() {{
    const d = interpData;
    let html = '';

    // 1. 개요
    html += `<div class="section">
        <div class="section-title">📊 개요</div>
        <p>${{d.summary.description}}</p>
    </div>`;

    // 2. 스키마별 구성
    html += `<div class="section">
        <div class="section-title">🗂️ 스키마별 구성</div>`;
    for (const s of d.schemas) {{
        html += `<p><strong>${{s.name}}</strong>: ${{s.tables.join(', ')}}</p>`;
    }}
    html += `</div>`;

    // 3. 주요 관계
    if (d.key_relations.length) {{
        html += `<div class="section">
            <div class="section-title">🔗 주요 관계</div>`;
        for (const r of d.key_relations) {{
            const cls = r.type === 'FK' ? 'FK' : 'DATA_SIMILAR';
            html += `<div class="rel-item">
                <span class="rel-type ${{cls}}">${{r.type}}</span>
                <span>${{r.source}} → ${{r.target}}</span>
                <span style="color:var(--muted);font-size:11px">(${{ r.note }})</span>
            </div>`;
        }}
        html += `</div>`;
    }}

    // 4. 값 기반 탐지
    if (d.value_based.length) {{
        html += `<div class="section">
            <div class="section-title">🔎 값 기반 탐지 결과</div>
            <p style="color:var(--muted);margin-bottom:6px">
                FK 제약 없이 실제 데이터 값 중복으로 탐지된 관계입니다. 컬럼명이 달라도 값이 겹치면 연결됩니다.
            </p>`;
        const MAX_VB = 10;
        const shown = d.value_based.slice(0, MAX_VB);
        for (const v of shown) {{
            html += `<div class="rel-item">
                <span class="rel-type DATA_SIMILAR">DATA</span>
                <span>${{v.source}} → ${{v.target}}</span>
                <span style="color:var(--muted);font-size:11px">(중복률: ${{v.overlap}})</span>
            </div>`;
        }}
        if (d.value_based.length > MAX_VB) {{
            html += `<p style="color:var(--muted);font-size:11px;margin-top:4px">
                ... 외 ${{d.value_based.length - MAX_VB}}개
            </p>`;
        }}
        html += `</div>`;
    }}

    // 5. 주의사항
    if (d.warnings.length) {{
        html += `<div class="section">
            <div class="section-title">⚠️ 주의사항</div>`;
        for (const w of d.warnings) {{
            html += `<div class="warning">${{w}}</div>`;
        }}
        html += `<p style="color:var(--muted);font-size:11px;margin-top:6px">
            위 관계들은 데이터 값만 일치할 뿐 실제 의미상 관련이 없을 수 있습니다.
            도메인 지식을 바탕으로 검토가 필요합니다.
        </p></div>`;
    }}

    document.getElementById('interpretation-panel').innerHTML = html;
}}

// 탭 전환
function switchTab(name) {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('tab-' + name).classList.add('active');
    if (name === 'graph') initGraph();
    if (name === 'relationships') renderRelationships();
    if (name === 'insights') renderInsights();
    if (name === 'domains') renderDomains();
}}

// 그래프 초기화
function initGraph() {{
    const container = document.getElementById('graph-container');
    const svg = d3.select('#graph-svg');
    svg.selectAll('*').remove();

    const w = container.clientWidth;
    const h = container.clientHeight;

    svg.attr('viewBox', `0 0 ${{w}} ${{h}}`);

    const g = svg.append('g');
    svg.call(d3.zoom().on('zoom', (e) => g.attr('transform', e.transform)));

    const nodes = graphData.nodes || [];
    const links = graphData.links || [];

    const simulation = d3.forceSimulation(nodes)
        .force('link', d3.forceLink(links).id(d => d.id).distance(60))
        .force('charge', d3.forceManyBody().strength(-200))
        .force('center', d3.forceCenter(w/2, h/2))
        .force('collision', d3.forceCollide().radius(d => (d.size||10) + 5));

    const link = g.append('g').selectAll('line').data(links).join('line')
        .attr('stroke', d => d.type === 'FK' ? '#f85149' : d.type === 'EXACT_MATCH' ? '#3fb950' : '#30363d')
        .attr('stroke-width', d => d.value || 1)
        .attr('stroke-dasharray', d => d.type === 'FK' ? '' : '3,3')
        .attr('opacity', 0.5);

    const node = g.append('g').selectAll('g').data(nodes).join('g')
        .call(d3.drag().on('start', dragstart).on('drag', drag).on('end', dragend));

    node.append('circle')
        .attr('r', d => d.size || 10)
        .attr('fill', d => d.color || '#8b949e')
        .attr('stroke', '#0d1117').attr('stroke-width', 1.5);

    node.append('text').text(d => d.name)
        .attr('x', d => (d.size||10) + 3).attr('y', 3)
        .attr('fill', '#e6edf3').attr('font-size', d => (d.size > 15 ? 11 : 9));

    const tooltip = d3.select('#tooltip');
    node.on('mouseover', (e, d) => {{
        tooltip.style('display','block').style('left',(e.pageX+10)+'px').style('top',(e.pageY-10)+'px')
            .html(`<strong>${{d.name}}</strong><br>타입: ${{d.group}}`);
    }}).on('mouseout', () => tooltip.style('display','none'));

    simulation.on('tick', () => {{
        link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
            .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
        node.attr('transform', d => `translate(${{d.x}},${{d.y}})`);
    }});

    function dragstart(e, d) {{ if(!e.active) simulation.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; }}
    function drag(e, d) {{ d.fx=e.x; d.fy=e.y; }}
    function dragend(e, d) {{ if(!e.active) simulation.alphaTarget(0); d.fx=null; d.fy=null; }}
}}

// 검색
function doSearch() {{
    const q = document.getElementById('search-input').value.trim();
    if (!q) return;
    // 클라이언트 사이드 검색
    const nodes = graphData.nodes || [];
    const results = nodes.filter(n =>
        n.name.toLowerCase().includes(q.toLowerCase()) ||
        (n.table && n.table.toLowerCase().includes(q.toLowerCase()))
    ).slice(0, 30);

    const html = results.length ? results.map(n => {{
        const badge = n.group === 'COLUMN' ? (n.is_pk ? '<span class="badge badge-pk">PK</span>' : n.is_fk ? '<span class="badge badge-fk">FK</span>' : '') : '';
        return `<div class="result-item" onclick="focusNode('${{n.id}}')">
            <div class="name">${{n.icon || ''}} ${{n.name}} ${{badge}}</div>
            <div class="meta">${{n.group}} ${{n.table ? '· ' + n.table : ''}} ${{n.database ? '· ' + n.database : ''}}</div>
        </div>`;
    }}).join('') : '<p style="color:var(--muted);padding:20px;text-align:center">검색 결과 없음</p>';

    document.getElementById('search-results').innerHTML = `<div class="result-list">${{html}}</div>`;
}}

function focusNode(nodeId) {{
    switchTab('graph');
    // 해당 노드로 줌
    const node = (graphData.nodes || []).find(n => n.id === nodeId);
    if (node) {{
        d3.select('#graph-svg').transition().duration(500).call(
            d3.zoom().transform,
            d3.zoomIdentity.translate(400 - node.x * 0.8, 250 - node.y * 0.8).scale(0.8)
        );
    }}
}}

// 관계 테이블
function renderRelationships() {{
    const rels = {json.dumps([{
        'type': r['type'], 'count': r['count'], 'avg_conf': r['avg_confidence']
    } for r in overview.get('relationship_distribution', [])])};
    const tbody = document.getElementById('rel-tbody');
    tbody.innerHTML = rels.map(r => {{
        const pct = Math.round(r.avg_conf * 100);
        const color = pct >= 90 ? '#3fb950' : pct >= 70 ? '#d29922' : '#f85149';
        return `<tr>
            <td><strong>${{r.type}}</strong></td>
            <td>${{r.count}}개</td>
            <td>${{r.avg_conf.toFixed(2)}}</td>
            <td><span class="confidence-bar"><span class="confidence-fill" style="width:${{pct}}%;background:${{color}}"></span></span> ${{pct}}%</td>
        </tr>`;
    }}).join('');
}}

// 도메인 클러스터
function renderDomains() {{
    const domains = {{
        '인물/사용자': ['user_id', 'username', 'email', 'phone', 'name'],
        '주문': ['order_id', 'order_date', 'order_status', 'total_amount'],
        '상품': ['product_id', 'product_name', 'price', 'category'],
        '결제': ['payment_id', 'payment_method', 'payment_date', 'amount'],
        '시간': ['created_at', 'updated_at', 'deleted_at', 'timestamp'],
        '상태': ['status', 'state', 'is_active', 'is_deleted'],
    }};
    const grid = document.getElementById('domain-grid');
    grid.innerHTML = Object.entries(domains).map(([name, fields]) => `
        <div class="domain-card">
            <div class="domain-name">🏷️ ${{name}}</div>
            <div class="field-list">${{fields.map(f => `<div class="field-item">📌 ${{f}}</div>`).join('')}}</div>
        </div>
    `).join('');
}}

// 인사이트 데이터
const insightData = {json.dumps(insight_data, ensure_ascii=False) if insight_data else 'null'};

// 인사이트 렌더링
function renderInsights() {{
    if (!insightData) {{
        document.getElementById('insight-content').innerHTML =
            '<p style="color:var(--muted);padding:20px;text-align:center">인사이트 엔진이 연결되지 않았습니다.</p>';
        return;
    }}
    let html = '';

    // 2. 비즈니스 엔티티
    const entities = insightData.entities || [];
    if (entities.length) {{
        html += `<div class="section"><div class="section-title">🏢 비즈니스 엔티티</div>
        <p style="color:var(--muted);margin-bottom:8px">관계 네트워크 기반으로 탐지된 업무 단위입니다.</p>
        <div class="entity-grid">`;
        for (const e of entities) {{
            html += `<div class="entity-card">
                <div class="entity-name">${{e.name}}</div>
                <div class="entity-tables">${{e.tables.join(' → ')}}</div>
                <div class="entity-count">${{e.table_count}}개 테이블</div>
            </div>`;
        }}
        html += `</div></div>`;
    }}

    // 3. 영향도 (허브 테이블)
    const hubs = insightData.impactful_tables || [];
    if (hubs.length) {{
        html += `<div class="section"><div class="section-title">🎯 영향도 높은 테이블</div>
        <p style="color:var(--muted);margin-bottom:8px">관계 수가 많아 변경 시 파급력이 큰 테이블입니다.</p>`;
        for (const h of hubs) {{
            const bar = Math.min(h.relationship_count * 10, 100);
            html += `<div class="hub-item">
                <div style="display:flex;justify-content:space-between;margin-bottom:2px">
                    <span><strong>${{h.table}}</strong></span>
                    <span style="color:var(--muted);font-size:11px">${{h.relationship_count}}개 관계</span>
                </div>
                <div class="hub-bar"><div class="hub-fill" style="width:${{bar}}%"></div></div>
                <div style="color:var(--muted);font-size:11px">${{h.description}}</div>
            </div>`;
        }}
        html += `</div>`;
    }}

    // 1. 조인 경로
    const paths = insightData.join_paths || [];
    if (paths.length) {{
        html += `<div class="section"><div class="section-title">🔗 테이블 간 조인 경로</div>
        <p style="color:var(--muted);margin-bottom:8px">두 테이블을 연결하는 최단 관계 경로입니다.</p>`;
        for (const p of paths.slice(0, 8)) {{
            const firstPath = p.paths[0];
            if (!firstPath) continue;
            const steps = firstPath.steps.map(s =>
                `<span class="path-step">${{s.from}} <span style="color:var(--muted)">→</span> ${{s.to}}</span>`
            ).join(' <span style="color:var(--muted);font-size:16px">›</span> ');
            html += `<div class="path-card">
                <div style="font-size:12px;color:var(--muted);margin-bottom:4px">${{p.source}} ↔ ${{p.target}}</div>
                <div style="display:flex;flex-wrap:wrap;gap:4px">${{steps}}</div>
            </div>`;
        }}
        if (paths.length > 8) {{
            html += `<p style="color:var(--muted);font-size:11px;margin-top:4px">... 외 ${{paths.length - 8}}개 경로</p>`;
        }}
        html += `</div>`;
    }}

    // 4. 데이터 품질
    const quality = insightData.quality || {{}};
    const q = quality.summary || {{}};
    html += `<div class="section"><div class="section-title">✅ 데이터 품질</div>`;

    if (quality.missing_fk && quality.missing_fk.length) {{
        html += `<div style="margin-bottom:8px">
            <span style="color:var(--yellow);font-weight:600">⚠️ 누락 FK 추천 (${{quality.missing_fk.length}}개)</span>`;
        for (const fk of quality.missing_fk.slice(0, 5)) {{
            html += `<div class="quality-item">🔗 <code>${{fk.source}}</code> → <code>${{fk.target}}</code>
                <span style="color:var(--muted);font-size:11px">(${{Math.round(fk.confidence * 100)}}% 일치)</span></div>`;
        }}
        if (quality.missing_fk.length > 5) {{
            html += `<div style="color:var(--muted);font-size:11px">... 외 ${{quality.missing_fk.length - 5}}개</div>`;
        }}
        html += `</div>`;
    }}

    if (quality.isolated_tables && quality.isolated_tables.length) {{
        html += `<div style="margin-bottom:8px">
            <span style="color:var(--red);font-weight:600">🔴 고립 테이블 (${{quality.isolated_tables.length}}개)</span>
            <div style="color:var(--muted);font-size:12px">${{quality.isolated_tables.join(', ')}}</div>
        </div>`;
    }}

    if (quality.hub_tables && quality.hub_tables.length) {{
        html += `<div style="margin-bottom:8px">
            <span style="color:var(--blue);font-weight:600">🔵 중심 테이블</span>
            <div style="font-size:12px;color:var(--text)">${{quality.hub_tables.join(' · ')}}</div>
        </div>`;
    }}

    html += `</div>`;

    // 5. 분석 추천
    const recs = insightData.recommendations || [];
    if (recs.length) {{
        html += `<div class="section"><div class="section-title">💡 추천 분석 경로</div>
        <p style="color:var(--muted);margin-bottom:8px">테이블 간 관계를 활용한 분석 추천입니다.</p>`;
        for (const r of recs.slice(0, 10)) {{
            const pct = r.value_score;
            const barColor = pct >= 80 ? 'var(--green)' : pct >= 60 ? 'var(--yellow)' : 'var(--muted)';
            let joinHtml = '';
            for (const j of r.joins) {{
                joinHtml += `<div style="font-size:11px;color:var(--muted);padding:1px 0">
                    🔗 <code>${{j.from}}</code> = <code>${{j.to}}</code></div>`;
            }}
            let colHtml = '';
            for (const c of r.result_columns.slice(0, 6)) {{
                colHtml += `<span style="display:inline-block;background:var(--surface2);padding:1px 6px;border-radius:3px;font-size:10px;margin:1px">${{c.table.split('.')[1]}}.${{c.column}}</span> `;
            }}
            if (r.result_columns.length > 6) {{
                colHtml += `<span style="font-size:10px;color:var(--muted)">+${{r.result_columns.length - 6}}</span>`;
            }}
            html += `<div class="rec-card">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                    <span class="rec-title">${{r.title}}</span>
                    <span class="rec-score" style="background:${{barColor}}">${{pct}}</span>
                </div>
                <div style="font-size:11px;color:var(--muted);margin-bottom:4px">📌 ${{r.path}}</div>
                ${{joinHtml}}
                <div style="margin-top:4px">${{colHtml}}</div>
            </div>`;
        }}
        if (recs.length > 10) {{
            html += `<p style="color:var(--muted);font-size:11px;margin-top:4px">... 외 ${{recs.length - 10}}개 추천</p>`;
        }}
        html += `</div>`;
    }}

    document.getElementById('insight-content').innerHTML = html;
}}

// 초기 로드
document.addEventListener('DOMContentLoaded', () => {{
    initGraph();
    renderRelationships();
    renderInsights();
    renderDomains();
}});
</script>
</body>
</html>"""

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  대시보드 HTML 저장: {output_path}")


# ── 메인 실행 ──────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from collector.db_adapter import SQLiteAdapter, MetadataStore, SchemaCollector
    from analyzer.relationship_analyzer import RelationshipOrchestrator
    from ontology.graph_builder import OntologyGraph

    # 테스트 DB
    test_db_path = "/tmp/test_dashboard.db"
    meta_path = "/tmp/test_dashboard_meta.db"
    for p in [test_db_path, meta_path]:
        if os.path.exists(p): os.remove(p)

    conn = sqlite3.connect(test_db_path)
    conn.executescript("""
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY, username TEXT, email TEXT,
            phone TEXT, created_at TIMESTAMP, status TEXT
        );
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
            order_date TIMESTAMP, amount REAL, status TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY, product_name TEXT,
            price REAL, category TEXT
        );
        CREATE TABLE order_items (
            item_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL, quantity INTEGER, price REAL,
            FOREIGN KEY (order_id) REFERENCES orders(order_id),
            FOREIGN KEY (product_id) REFERENCES products(product_id)
        );
        CREATE TABLE payments (
            payment_id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL,
            amount REAL, payment_date TIMESTAMP, status TEXT,
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        );
    """)
    conn.close()

    # 수집 → 분석 → 그래프 → 대시보드
    store = MetadataStore(db_path=meta_path)
    collector = SchemaCollector(store)
    collector.add_database("sqlite", file_path=test_db_path, db_name="shop")
    collector.collect_all()

    orchestrator = RelationshipOrchestrator(store)
    orchestrator.analyze_all()

    graph = OntologyGraph()
    graph.build_from_store(store)
    graph.add_domain_nodes()

    # 대시보드 생성
    print("\n=== 대시보드 생성 ===")
    provider = DashboardDataProvider(meta_path)
    builder = DashboardHTMLBuilder(provider, graph.to_d3_json())

    output_dir = "/Users/bearj/coding_projects/db-ontology/output"
    os.makedirs(output_dir, exist_ok=True)
    builder.build_full_dashboard(f"{output_dir}/dashboard.html")

    provider.close()
    store.close()

    for p in [test_db_path, meta_path]:
        if os.path.exists(p): os.remove(p)

    print("\n✅ 대시보드 테스트 완료")
