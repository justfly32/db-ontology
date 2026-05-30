"""
DB Ontology Analyzer - 메인 엔트리포인트
빌드 → 수집 → 분석 → 그래프 → 대시보드 → API 서버 전체 파이프라인
"""

import os
import sys
import sqlite3
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from collector.db_adapter import SQLiteAdapter, MetadataStore, SchemaCollector
from analyzer.relationship_analyzer import RelationshipOrchestrator
from ontology.graph_builder import OntologyGraph
from collector.drift_detector import SchemaDriftDetector, ChangeHistoryManager
from visualizer.dashboard import DashboardHTMLBuilder, DashboardDataProvider


def create_sample_databases():
    """테스트용 샘플 DB 생성"""
    dbs = {}

    # 1. 이커머스 DB
    path = "/tmp/ontology_ecommerce.db"
    if os.path.exists(path): os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL,
            phone TEXT,
            full_name TEXT,
            birth_date DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 1,
            status TEXT DEFAULT 'active'
        );
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_amount REAL NOT NULL,
            discount_amount REAL DEFAULT 0,
            payment_status TEXT DEFAULT 'pending',
            shipping_address_id INTEGER,
            order_status TEXT DEFAULT 'processing',
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        CREATE TABLE order_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            unit_price REAL NOT NULL,
            item_discount REAL DEFAULT 0,
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        );
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            category_id INTEGER,
            price REAL NOT NULL,
            stock_quantity INTEGER DEFAULT 0,
            product_description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE categories (
            category_id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_name TEXT NOT NULL UNIQUE,
            parent_category_id INTEGER,
            category_description TEXT,
            FOREIGN KEY (parent_category_id) REFERENCES categories(category_id)
        );
        CREATE TABLE payments (
            payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            payment_method TEXT NOT NULL,
            amount REAL NOT NULL,
            transaction_id TEXT UNIQUE,
            payment_date TIMESTAMP,
            payment_status TEXT DEFAULT 'pending',
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        );
        CREATE TABLE addresses (
            address_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            address_line1 TEXT NOT NULL,
            address_line2 TEXT,
            city TEXT,
            state TEXT,
            zip_code TEXT NOT NULL,
            country TEXT DEFAULT 'KR',
            is_default BOOLEAN DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        CREATE INDEX idx_orders_user_id ON orders(user_id);
        CREATE INDEX idx_orders_order_status ON orders(order_status);
        CREATE INDEX idx_order_items_order_id ON order_items(order_id);
        CREATE INDEX idx_order_items_product_id ON order_items(product_id);
        CREATE INDEX idx_payments_order_id ON payments(order_id);
        CREATE INDEX idx_products_category_id ON products(category_id);
    """)
    conn.close()
    dbs["ecommerce"] = path
    print(f"  ✅ 이커머스 DB: {path}")

    # 2. HR DB
    path = "/tmp/ontology_hr.db"
    if os.path.exists(path): os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE department (
            dept_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dept_name TEXT NOT NULL UNIQUE,
            parent_dept_id INTEGER,
            dept_location TEXT,
            budget REAL,
            FOREIGN KEY (parent_dept_id) REFERENCES department(dept_id)
        );
        CREATE TABLE employee (
            emp_id INTEGER PRIMARY KEY AUTOINCREMENT,
            emp_name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            hire_date DATE,
            salary REAL,
            dept_id INTEGER,
            manager_id INTEGER,
            position TEXT,
            emp_status TEXT DEFAULT 'active',
            FOREIGN KEY (dept_id) REFERENCES department(dept_id),
            FOREIGN KEY (manager_id) REFERENCES employee(emp_id)
        );
        CREATE TABLE project (
            project_id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            description TEXT,
            lead_emp_id INTEGER,
            start_date DATE,
            end_date DATE,
            budget REAL,
            project_status TEXT DEFAULT 'active',
            FOREIGN KEY (lead_emp_id) REFERENCES employee(emp_id)
        );
        CREATE TABLE emp_project (
            emp_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            role TEXT,
            assigned_date DATE,
            PRIMARY KEY (emp_id, project_id),
            FOREIGN KEY (emp_id) REFERENCES employee(emp_id),
            FOREIGN KEY (project_id) REFERENCES project(project_id)
        );
        CREATE TABLE salary_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            emp_id INTEGER NOT NULL,
            old_salary REAL,
            new_salary REAL,
            change_date DATE,
            change_reason TEXT,
            FOREIGN KEY (emp_id) REFERENCES employee(emp_id)
        );
        CREATE INDEX idx_employee_dept_id ON employee(dept_id);
        CREATE INDEX idx_employee_manager_id ON employee(manager_id);
        CREATE INDEX idx_salary_history_emp_id ON salary_history(emp_id);
    """)
    conn.close()
    dbs["hr"] = path
    print(f"  ✅ HR DB: {path}")

    return dbs


def main():
    """전체 파이프라인 실행"""
    print("=" * 60)
    print("🔮 DB Ontology Analyzer - Full Pipeline")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 초기화
    meta_path = os.path.expanduser("~/.hermes/data/ontology_demo.db")
    output_dir = os.path.expanduser("~/coding_projects/db-ontology/output")
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    if os.path.exists(meta_path): os.remove(meta_path)

    store = MetadataStore(db_path=meta_path)

    # ── Phase 1: 샘플 DB 생성 ──────────────────────────
    print("\n📦 [Phase 1] 샘플 DB 생성")
    dbs = create_sample_databases()

    # ── Phase 2: 스키마 수집 ───────────────────────────
    print("\n📡 [Phase 2] 스키마 수집")
    collector = SchemaCollector(store)
    for name, path in dbs.items():
        collector.add_database("sqlite", file_path=path, db_name=name)
    results = collector.collect_all()

    total_tables = sum(len(db.tables) for db in results)
    total_cols = sum(sum(len(t.columns) for t in db.tables) for db in results)
    print(f"  수집 완료: {len(results)}개 DB, {total_tables}개 테이블, {total_cols}개 컬럼")

    # ── Phase 3: 연관관계 분석 ─────────────────────────
    print("\n🔍 [Phase 3] 연관관계 분석")
    orchestrator = RelationshipOrchestrator(store)
    rels = orchestrator.analyze_all()

    # ── Phase 4: 온톨로지 그래프 구축 ──────────────────
    print("\n🕸️ [Phase 4] 온톨로지 그래프 구축")
    ont_graph = OntologyGraph()
    ont_graph.build_from_store(store)
    ont_graph.add_domain_nodes()
    stats = ont_graph.get_statistics()
    print(f"  그래프: {stats['total_nodes']}노드, {stats['total_edges']}엣지")

    # ── Phase 5: 시각화 대시보드 ───────────────────────
    print("\n📊 [Phase 5] 대시보드 생성")
    provider = DashboardDataProvider(meta_path)
    d3_data = ont_graph.to_d3_json()
    builder = DashboardHTMLBuilder(provider, d3_data)
    builder.build_full_dashboard(f"{output_dir}/dashboard.html")
    ont_graph.save_html_visualization(f"{output_dir}/ontology_graph.html")
    ont_graph.export_graphml(f"{output_dir}/ontology.graphml")

    # ── Phase 6: 증분 업데이트 테스트 ──────────────────
    print("\n🔄 [Phase 6] 스키마 드리프트 감지")
    detector = SchemaDriftDetector(meta_path)
    detector.take_snapshot()
    drift = detector.detect_changes()
    print(f"  스키프 상태: {drift['status']}")

    # ── Phase 7: API 서버 정보 ─────────────────────────
    print("\n🌐 [Phase 7] API 서버")
    print("  실행 방법: cd src && python -m uvicorn api.server:app --reload --port 8000")
    print("  API 문서: http://localhost:8000/docs")

    # ── 요약 ─────────────────────────────────────────
    print("\n" + "=" * 60)
    print("✅ 전체 파이프라인 완료")
    print(f"  메타데이터: {meta_path}")
    print(f"  대시보드:   {output_dir}/dashboard.html")
    print(f"  그래프:     {output_dir}/ontology_graph.html")
    print(f"  GraphML:    {output_dir}/ontology.graphml")
    print("=" * 60)

    store.close()

    # 임시 파일 정리
    for path in dbs.values():
        if os.path.exists(path): os.remove(path)


if __name__ == "__main__":
    main()
