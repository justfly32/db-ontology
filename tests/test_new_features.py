"""
DB Ontology - 신규 기능 통합 테스트
SQLite 기반 (Docker/PostgreSQL 없이 실행 가능)
"""

import os
import sys
import sqlite3
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from collector.db_adapter import MetadataStore, SchemaCollector
from analyzer.relationship_analyzer import (
    NamingPatternAnalyzer, RelationshipOrchestrator,
)
from ontology.graph_builder import OntologyGraph
from visualizer.dashboard import DashboardHTMLBuilder, DashboardDataProvider

PASS = 0
FAIL = 0

def test(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")

def run():
    print("=" * 60)
    print("🧪 DB Ontology - 신규 기능 통합 테스트")
    print("=" * 60)

    # ── 1. 프리셋 CRUD ─────────────────────────────────
    print("\n📁 [1] 테이블 프리셋 CRUD")
    db_path = "/tmp/test_features.db"
    if os.path.exists(db_path): os.remove(db_path)

    store = MetadataStore(db_path=db_path)
    pid1 = store.save_preset("주문 분석", ["public.users", "public.orders", "public.order_items", "public.products"])
    pid2 = store.save_preset("HR 분석", ["hr.employee", "hr.department"])
    pid3 = store.save_preset("전체", ["public.users", "public.orders"])

    presets = store.list_presets()
    test("프리셋 목록 조회", len(presets) == 3)

    loaded = store.load_preset(pid1)
    test("프리셋 불러오기", loaded == ["public.users", "public.orders", "public.order_items", "public.products"])

    store.delete_preset(pid3)
    test("프리셋 삭제", len(store.list_presets()) == 2)

    # ── 2. SQLite 어댑터 table_filter ──────────────────
    print("\n📡 [2] table_filter 테스트")

    test_db = "/tmp/test_filter.db"
    if os.path.exists(test_db): os.remove(test_db)

    conn = sqlite3.connect(test_db)
    conn.executescript("""
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE orders (order_id INTEGER PRIMARY KEY, user_id INTEGER);
        CREATE TABLE products (product_id INTEGER PRIMARY KEY, product_name TEXT);
        CREATE TABLE logs (log_id INTEGER PRIMARY KEY, message TEXT);
    """)
    conn.close()

    store2 = MetadataStore(db_path="/tmp/test_filter_meta.db")
    collector = SchemaCollector(store2)
    collector.add_database("sqlite", file_path=test_db, db_name="test")
    results = collector.collect_all(table_filter=["main.users", "main.products"])

    names = [t.table_name for db in results for t in db.tables]
    test("table_filter: users 포함", "users" in names)
    test("table_filter: products 포함", "products" in names)
    test("table_filter: orders 제외", "orders" not in names)
    test("table_filter: logs 제외", "logs" not in names)

    # ── 3. NamingPatternAnalyzer.find_comment_matches() ──
    print("\n💬 [3] 코멘트 기반 유사도 분석")

    npa = NamingPatternAnalyzer()
    comment_cols = [
        {"id": 1, "column_name": "user_id",  "description": "사용자 고유 식별자", "schema": "public", "table": "users", "database": "test", "data_type": "INTEGER"},
        {"id": 2, "column_name": "member_no", "description": "사용자 고유 번호",   "schema": "public", "table": "members", "database": "test", "data_type": "INTEGER"},
        {"id": 3, "column_name": "emp_id",    "description": "직원 고유 번호",   "schema": "hr", "table": "employee", "database": "test", "data_type": "INTEGER"},
        {"id": 4, "column_name": "price",     "description": "상품 판매 가격",   "schema": "public", "table": "products", "database": "test", "data_type": "NUMERIC"},
        {"id": 5, "column_name": "amount",    "description": "결제 금액",       "schema": "public", "table": "payments", "database": "test", "data_type": "NUMERIC"},
        {"id": 6, "column_name": "total_amt", "description": "주문 총 금액",     "schema": "public", "table": "orders", "database": "test", "data_type": "NUMERIC"},
        {"id": 7, "column_name": "name",      "description": "",                "schema": "public", "table": "users", "database": "test", "data_type": "TEXT"},
        {"id": 8, "column_name": "created_at","description": "생성 일시",       "schema": "public", "table": "orders", "database": "test", "data_type": "TIMESTAMP"},
    ]

    matches = npa.find_comment_matches(comment_cols)

    # user_id ↔ member_no ("사용자 고유 식별자" ↔ "사용자 고유 번호")
    user_member = [(m.source_column, m.target_column) for m in matches
                   if "user_id" in (m.source_column, m.target_column)]
    test("코멘트 매칭: user_id ↔ member_no (사용자)",
         len(user_member) > 0, detail=f"→ {user_member}")

    # amount ↔ total_amt ("결제 금액" ↔ "주문 총 금액")
    amt_matches = [(m.source_column, m.target_column, m.confidence) for m in matches
                   if m.source_column in ("amount", "total_amt") and m.target_column in ("amount", "total_amt")]
    test("코멘트 매칭: amount ↔ total_amt (금액)",
         len(amt_matches) > 0, detail=f"→ {amt_matches}")

    for m in matches:
        print(f"     {m.source_table}.{m.source_column} ↔ {m.target_table}.{m.target_column}  [{m.confidence:.2f}]  {m.evidence[:40]}")

    # ── 4. 전체 파이프라인 ─────────────────────────────
    print("\n🔄 [4] 전체 파이프라인 (수집 → 분석 → 그래프 → 대시보드)")

    meta_path = "/tmp/test_pipeline_meta.db"
    output_dir = "/tmp/test_pipeline_output"
    for p in [meta_path]:
        if os.path.exists(p): os.remove(p)
    os.makedirs(output_dir, exist_ok=True)

    store3 = MetadataStore(db_path=meta_path)
    collector3 = SchemaCollector(store3)

    # 7개 테이블 DB 생성
    pipeline_db = "/tmp/test_pipeline.db"
    if os.path.exists(pipeline_db): os.remove(pipeline_db)

    conn = sqlite3.connect(pipeline_db)
    conn.executescript("""
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            email TEXT
        );
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            total_amount REAL,
            order_date TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY,
            product_name TEXT,
            price REAL
        );
        CREATE TABLE order_items (
            item_id INTEGER PRIMARY KEY,
            order_id INTEGER,
            product_id INTEGER,
            quantity INTEGER,
            FOREIGN KEY (order_id) REFERENCES orders(order_id),
            FOREIGN KEY (product_id) REFERENCES products(product_id)
        );
        INSERT INTO users VALUES (1, 'alice', 'a@test.com'), (2, 'bob', 'b@test.com');
        INSERT INTO orders VALUES (1, 1, 15000, '2024-01-01'), (2, 1, 25000, '2024-01-02');
        INSERT INTO products VALUES (1, 'A', 1000), (2, 'B', 2000);
        INSERT INTO order_items VALUES (1, 1, 1, 2), (2, 1, 2, 1), (3, 2, 1, 1);
    """)
    conn.close()

    collector3.add_database("sqlite", file_path=pipeline_db, db_name="shop")
    results = collector3.collect_all()

    total_tables = sum(len(db.tables) for db in results)
    total_cols = sum(sum(len(t.columns) for t in db.tables) for db in results)
    test("수집 완료", total_tables == 4 and total_cols > 0, detail=f"({total_tables}개 테이블, {total_cols}개 컬럼)")

    orchestrator = RelationshipOrchestrator(store3)
    rels = orchestrator.analyze_all()
    test("관계 분석 완료", len(rels) > 0, detail=f"({len(rels)}개 관계)")
    if rels:
        for r in rels[:5]:
            print(f"     [{r.relation_type}] {r.confidence:.2f}  {r.source_table}.{r.source_column} → {r.target_table}.{r.target_column}")

    graph = OntologyGraph()
    graph.build_from_store(store3)
    graph.add_domain_nodes()
    stats = graph.get_statistics()
    test("그래프 구축 완료", stats['total_nodes'] > 0 and stats['total_edges'] > 0,
         detail=f"({stats['total_nodes']}노드, {stats['total_edges']}엣지)")

    # 대시보드 생성
    provider = DashboardDataProvider(meta_path)
    d3_data = graph.to_d3_json()
    builder = DashboardHTMLBuilder(provider, d3_data)
    builder.build_full_dashboard(f"{output_dir}/dashboard.html")
    graph.save_html_visualization(f"{output_dir}/ontology_graph.html")
    graph.export_graphml(f"{output_dir}/ontology.graphml")

    test("대시보드 HTML 생성", os.path.exists(f"{output_dir}/dashboard.html"))
    test("그래프 HTML 생성", os.path.exists(f"{output_dir}/ontology_graph.html"))
    test("GraphML 생성", os.path.exists(f"{output_dir}/ontology.graphml"))

    # ── 5. SchemaDriftDetector ─────────────────────────
    print("\n🔄 [5] 스키마 드리프트 감지")
    from collector.drift_detector import SchemaDriftDetector

    detector = SchemaDriftDetector(meta_path)
    snap = detector.take_snapshot()
    test("스냅샷 생성", "hash" in snap and "tables" in snap)

    drift = detector.detect_changes()
    test("변경 감지 (변경 없음)", drift['status'] == "unchanged")

    print(f"\n  스냅샷 해시: {snap['hash'][:16]}...")

    # ── 정리 ──────────────────────────────────────────
    store.close()
    store2.close()
    store3.close()
    provider.close()

    for p in [db_path, "/tmp/test_filter_meta.db", test_db, meta_path, pipeline_db]:
        if os.path.exists(p): os.remove(p)

    # ── 결과 ──────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"📊 결과: {PASS} passed, {FAIL} failed out of {PASS + FAIL}")
    print("=" * 60)
    return FAIL == 0


if __name__ == "__main__":
    success = run()
    sys.exit(0 if success else 1)
