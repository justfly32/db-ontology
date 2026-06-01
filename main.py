"""
DB Ontology Analyzer - PostgreSQL 엔트리포인트
프리셋/CLI 테이블 선택 → 수집 → 분석 → 값 검증 → 그래프 → 대시보드
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


META_PATH = os.path.expanduser("~/.hermes/data/ontology_pg.db")
OUTPUT_DIR = os.path.expanduser("~/coding_projects/db-ontology/output")


def fetch_all_tables(host, port, dbname, user, password) -> list[dict]:
    """PostgreSQL에서 전체 테이블 목록 조회 (schema.table, 한글 코멘트 포함)"""
    import psycopg2
    conn = psycopg2.connect(
        host=host, port=port, database=dbname, user=user, password=password
    )
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
    return rows


def show_preset_menu(store: MetadataStore) -> list[str] | None:
    """프리셋 불러오기 메뉴. None이면 새로 선택"""
    presets = store.list_presets()
    if not presets:
        return None

    print("\n📁 저장된 프리셋 목록")
    print("─" * 40)
    for i, p in enumerate(presets, 1):
        tables = store.load_preset(p["id"])
        print(f"  {i}: {p['name']} ({len(tables)}개 테이블)")
    print("  0: 새로 선택")
    print("─" * 40)

    raw = input("  선택: ").strip()
    try:
        n = int(raw)
        if n == 0:
            return None
        if 1 <= n <= len(presets):
            selected = store.load_preset(presets[n - 1]["id"])
            print(f"  ✅ 프리셋 불러옴: {presets[n - 1]['name']} ({len(selected)}개)")
            return selected
    except ValueError:
        pass
    return None


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
            comment = f" — {t['comment']}" if t.get("comment") else ""
            print(f"    {idx:>4}: {t['table']}{comment}")
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


def ask_save_preset(store: MetadataStore, selected: list[str]):
    """프리셋 저장 여부 확인"""
    raw = input("\n💾 현재 선택을 프리셋으로 저장할까요? (y/n): ").strip().lower()
    if raw != "y":
        return
    name = input("  프리셋 이름: ").strip()
    if name:
        store.save_preset(name, selected)
        print(f"  ✅ 프리셋 저장됨: {name}")


def main():
    """전체 파이프라인 실행"""
    print("=" * 60)
    print("🔮 DB Ontology Analyzer - PostgreSQL")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # DB 접속 정보
    db_config = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "mydb"),
        "user": os.getenv("DB_USER", "myuser"),
        "password": os.getenv("DB_PASSWORD", ""),
    }

    if not db_config.get("password"):
        print("⚠️  DB_PASSWORD가 설정되지 않았습니다. 로컬 인증(peer/trust) 가정")
        print("   (로컬 macOS PostgreSQL: peer 인증이므로 비밀번호 불필요)")

    print(f"\n🔗 연결 대상: {db_config['host']}:{db_config['port']}/{db_config['dbname']}")

    # 전체 테이블 조회
    print("\n📋 사용 가능한 테이블 조회 중...")
    all_tables = fetch_all_tables(**db_config)
    print(f"   ✅ {len(all_tables)}개 테이블 발견")

    # 저장소 초기화 (기존 데이터 유지)
    os.makedirs(os.path.dirname(META_PATH), exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    store = MetadataStore(db_path=META_PATH)

    # 프리셋 메뉴 또는 새 선택
    selected = show_preset_menu(store)
    if selected is None:
        selected = show_table_selection(all_tables)
        ask_save_preset(store, selected)

    print(f"   ✅ 선택됨: {len(selected)}개 테이블")
    for s in selected:
        print(f"      - {s}")

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

    # Phase 2: 연관관계 분석 (FK + 명명패턴 + 코멘트 + 동의어)
    print("\n🔍 [Phase 2] 연관관계 분석")
    orchestrator = RelationshipOrchestrator(store)
    rels = orchestrator.analyze_all()

    # Phase 2b: 값 기반 관계 검증 (FK가 없는 경우 보완)
    if rels:
        print("\n🔎 [Phase 2b] 값 기반 관계 검증")
        from analyzer.relationship_analyzer import DataSimilarityAnalyzer
        data_analyzer = DataSimilarityAnalyzer()
        value_rels = data_analyzer.fk_validation_by_values(
            candidates=rels,
            db_config=db_config,
            min_overlap_pct=0.5,
        )
        if value_rels:
            existing_types = {r.relation_type for r in rels}
            saved_count = 0
            for vr in value_rels:
                if "DATA_SIMILAR" not in existing_types:
                    src_id = store.resolve_column_id(
                        vr.source_db, vr.source_schema,
                        vr.source_table, vr.source_column)
                    tgt_id = store.resolve_column_id(
                        vr.target_db, vr.target_schema,
                        vr.target_table, vr.target_column)
                    if src_id and tgt_id:
                        store.save_relationship(
                            source_id=src_id, target_id=tgt_id,
                            relation_type=vr.relation_type,
                            confidence=vr.confidence,
                            detected_by=vr.detected_by,
                        )
                        rels.append(vr)
                        saved_count += 1
            print(f"  ✅ 값 검증 완료: {saved_count}개 관계 저장")
        else:
            print(f"  값 검증: 유효한 관계 없음")

    # Phase 3: 온톨로지 그래프 구축
    print("\n🕸️ [Phase 3] 온톨로지 그래프 구축")
    ont_graph = OntologyGraph()
    ont_graph.build_from_store(store)
    ont_graph.add_domain_nodes()
    stats = ont_graph.get_statistics()
    print(f"  그래프: {stats['total_nodes']}노드, {stats['total_edges']}엣지")

    # Phase 4: 시각화 대시보드
    print("\n📊 [Phase 4] 대시보드 생성")
    provider = DashboardDataProvider(META_PATH)
    d3_data = ont_graph.to_d3_json()
    builder = DashboardHTMLBuilder(provider, d3_data)

    from analyzer.insight_engine import InsightEngine
    ie = InsightEngine(store, ont_graph.graph)
    builder.build_full_dashboard(
        f"{OUTPUT_DIR}/dashboard.html",
        insight_engine=ie,
    )
    ont_graph.save_html_visualization(f"{OUTPUT_DIR}/ontology_graph.html")
    ont_graph.export_graphml(f"{OUTPUT_DIR}/ontology.graphml")

    # Phase 5: 스키마 드리프트 감지
    print("\n🔄 [Phase 5] 스키마 드리프트 감지")
    detector = SchemaDriftDetector(META_PATH)
    detector.take_snapshot()
    drift = detector.detect_changes()
    print(f"  스키마 상태: {drift['status']}")
    if drift.get("changes"):
        for c in drift["changes"][:5]:
            print(f"    [{c['type']}] {c['target']}")

    # 요약
    print("\n" + "=" * 60)
    print("✅ 전체 파이프라인 완료")
    print(f"  메타데이터: {META_PATH}")
    print(f"  대시보드:   {OUTPUT_DIR}/dashboard.html")
    print(f"  그래프:     {OUTPUT_DIR}/ontology_graph.html")
    print(f"  GraphML:    {OUTPUT_DIR}/ontology.graphml")
    print("=" * 60)

    store.close()


if __name__ == "__main__":
    main()
