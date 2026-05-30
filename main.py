"""
DB Ontology Analyzer - PostgreSQL 엔트리포인트
CLI 테이블 선택 → 수집 → 분석 → 그래프 → 대시보드
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from dotenv import load_dotenv

load_dotenv()

from collector.db_adapter import MetadataStore, SchemaCollector
from analyzer.relationship_analyzer import RelationshipOrchestrator
from ontology.graph_builder import OntologyGraph
from collector.drift_detector import SchemaDriftDetector
from visualizer.dashboard import DashboardHTMLBuilder, DashboardDataProvider


def fetch_all_tables(host, port, dbname, user, password) -> list[dict]:
    """PostgreSQL에서 전체 테이블 목록 조회 (schema.table)"""
    import psycopg2
    conn = psycopg2.connect(
        host=host, port=port, database=dbname, user=user, password=password
    )
    cur = conn.cursor()
    cur.execute("""
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY table_schema, table_name
    """)
    rows = [{"schema": r[0], "table": r[1]} for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def show_table_selection(tables: list[dict]) -> list[str]:
    """CLI 인터랙티브 테이블 선택"""
    schema_groups: dict[str, list[dict]] = {}
    for t in tables:
        schema_groups.setdefault(t["schema"], []).append(t)

    print(f"\n📋 전체 {len(tables)}개 테이블 ({len(schema_groups)}개 스키마)")
    print("─" * 50)

    idx = 1
    index_map: dict[int, str] = {}
    for schema, tbls in sorted(schema_groups.items()):
        print(f"  ── {schema} ──")
        for t in tbls:
            label = f"{schema}.{t['table']}"
            print(f"    {idx:>4}: {t['table']}")
            index_map[idx] = label
            idx += 1

    print("─" * 50)
    while True:
        raw = input("  선택할 테이블 번호 (쉼표/범위 구분, a=전체, q=종료): ").strip()
        if raw.lower() == "q":
            print("  종료합니다.")
            sys.exit(0)
        if raw.lower() == "a":
            return [v for v in index_map.values()]

        selected = []
        parts = [p.strip() for p in raw.replace(" ", "").split(",")]
        for p in parts:
            if "-" in p:
                try:
                    lo, hi = p.split("-", 1)
                    for n in range(int(lo), int(hi) + 1):
                        if n in index_map:
                            selected.append(index_map[n])
                except ValueError:
                    continue
            else:
                try:
                    n = int(p)
                    if n in index_map:
                        selected.append(index_map[n])
                except ValueError:
                    continue

        if selected:
            return selected
        print("  ⚠️ 올바른 번호를 입력하세요.")


def main():
    """전체 파이프라인 실행"""
    print("=" * 60)
    print("🔮 DB Ontology Analyzer - PostgreSQL")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # DB 접속 정보 (비밀키는 환경변수로 별도 주입)
    db_config = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "mydb"),
        "user": os.getenv("DB_USER", "myuser"),
        "password": os.getenv("DB_PASSWORD", ""),
    }

    if not db_config["password"]:
        print("❌ DB_PASSWORD 환경변수가 설정되지 않았습니다.")
        print("   export DB_PASSWORD=... 또는 Docker secret/k8s secret으로 주입하세요.")
        sys.exit(1)

    print(f"\n🔗 연결 대상: {db_config['host']}:{db_config['port']}/{db_config['dbname']}")

    # 전체 테이블 조회
    print("\n📋 사용 가능한 테이블 조회 중...")
    all_tables = fetch_all_tables(**db_config)
    print(f"   ✅ {len(all_tables)}개 테이블 발견")

    # CLI 테이블 선택
    selected = show_table_selection(all_tables)
    print(f"   ✅ 선택됨: {len(selected)}개 테이블")
    for s in selected:
        print(f"      - {s}")

    # 초기화
    meta_path = os.path.expanduser("~/.hermes/data/ontology_pg.db")
    output_dir = os.path.expanduser("~/coding_projects/db-ontology/output")
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    if os.path.exists(meta_path):
        os.remove(meta_path)

    store = MetadataStore(db_path=meta_path)

    # Phase 1: 스키마 수집 (선택된 테이블만)
    print("\n📡 [Phase 1] 스키마 수집")
    collector = SchemaCollector(store)
    collector.add_database(
        "postgresql",
        host=db_config["host"],
        port=db_config["port"],
        database=db_config["dbname"],
        user=db_config["user"],
        password=db_config["password"],
        db_name=db_config["dbname"],
    )
    results = collector.collect_all(table_filter=selected)

    total_tables = sum(len(db.tables) for db in results)
    total_cols = sum(sum(len(t.columns) for t in db.tables) for db in results)
    print(f"  수집 완료: {len(results)}개 DB, {total_tables}개 테이블, {total_cols}개 컬럼")

    if total_tables == 0:
        print("  ⚠️ 수집된 테이블이 없습니다. 종료합니다.")
        store.close()
        return

    # Phase 2: 연관관계 분석
    print("\n🔍 [Phase 2] 연관관계 분석")
    orchestrator = RelationshipOrchestrator(store)
    rels = orchestrator.analyze_all()

    # Phase 3: 온톨로지 그래프 구축
    print("\n🕸️ [Phase 3] 온톨로지 그래프 구축")
    ont_graph = OntologyGraph()
    ont_graph.build_from_store(store)
    ont_graph.add_domain_nodes()
    stats = ont_graph.get_statistics()
    print(f"  그래프: {stats['total_nodes']}노드, {stats['total_edges']}엣지")

    # Phase 4: 시각화 대시보드
    print("\n📊 [Phase 4] 대시보드 생성")
    provider = DashboardDataProvider(meta_path)
    d3_data = ont_graph.to_d3_json()
    builder = DashboardHTMLBuilder(provider, d3_data)
    builder.build_full_dashboard(f"{output_dir}/dashboard.html")
    ont_graph.save_html_visualization(f"{output_dir}/ontology_graph.html")
    ont_graph.export_graphml(f"{output_dir}/ontology.graphml")

    # Phase 5: 스키마 드리프트 감지
    print("\n🔄 [Phase 5] 스키마 드리프트 감지")
    detector = SchemaDriftDetector(meta_path)
    detector.take_snapshot()
    drift = detector.detect_changes()
    print(f"  스키마 상태: {drift['status']}")

    # 요약
    print("\n" + "=" * 60)
    print("✅ 전체 파이프라인 완료")
    print(f"  메타데이터: {meta_path}")
    print(f"  대시보드:   {output_dir}/dashboard.html")
    print(f"  그래프:     {output_dir}/ontology_graph.html")
    print(f"  GraphML:    {output_dir}/ontology.graphml")
    print("=" * 60)

    store.close()


if __name__ == "__main__":
    main()
