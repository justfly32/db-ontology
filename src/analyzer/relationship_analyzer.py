"""
Relationship Analyzer - 연관관계 분석 엔진
Phase 3: 필드 연관관계 탐지 및 신뢰도 산출
"""

from dataclasses import dataclass, field
from typing import Optional
import re
import os
import sqlite3
from collections import defaultdict


@dataclass
class Relationship:
    """탐지된 연관관계"""
    source_db: str
    source_schema: str
    source_table: str
    source_column: str
    target_db: str
    target_schema: str
    target_table: str
    target_column: str
    relation_type: str       # FK, EXACT_MATCH, NORMALIZED, SYNONYM, DATA_SIMILAR, LLM
    confidence: float        # 0.0 ~ 1.0
    detected_by: str         # 탐지 엔진 이름
    evidence: str = ""       # 탐지 근거 설명


# ── 1. 명명 패턴 분석기 ─────────────────────────────────

class NamingPatternAnalyzer:
    """필드명 패턴 기반 연관관계 탐지"""

    def __init__(self):
        # 동의어 사전
        self.synonyms = {
            'customer': ['client', 'buyer', 'purchaser', 'consumer', 'cust'],
            'product': ['item', 'goods', 'merchandise', 'sku', 'prod'],
            'order': ['purchase', 'transaction', 'deal', 'ord'],
            'user': ['account', 'member', 'person', 'employee', 'usr'],
            'address': ['location', 'place', 'residence', 'addr'],
            'phone': ['telephone', 'mobile', 'cell', 'contact', 'tel', 'ph'],
            'email': ['mail', 'e_mail', 'electronic_mail', 'eml'],
            'name': ['title', 'label', 'display_name', 'nm'],
            'date': ['time', 'timestamp', 'created_at', 'updated_at', 'dt'],
            'amount': ['price', 'cost', 'fee', 'total', 'sum', 'amt', 'qty'],
            'status': ['state', 'condition', 'stage', 'phase', 'sts', 'st'],
            'description': ['desc', 'detail', 'note', 'comment', 'memo', 'dsc'],
            'identifier': ['id', 'code', 'key', 'no', 'num'],
            'category': ['type', 'class', 'group', 'cat', 'ctg'],
        }
        # 역방향 동의어 맵 구성
        self._synonym_map = {}
        for canonical, syns in self.synonyms.items():
            self._synonym_map[canonical] = canonical
            for syn in syns:
                self._synonym_map[syn] = canonical

    def normalize(self, name: str) -> str:
        """필드명 정규화: camelCase → snake_case, 소문자"""
        # camelCase → snake_case
        s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
        s = re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s)
        s = re.sub(r'[\s\-.]', '_', s)
        s = s.lower().strip('_')
        return s

    def extract_base_name(self, name: str) -> str:
        """접두사/접미사 제거한 기본 이름 추출"""
        normalized = self.normalize(name)
        # 일반적인 접두사/접미사 제거
        prefixes = ['is_', 'has_', 'can_', 'should_', 'will_', 'old_', 'new_']
        suffixes = ['_id', '_code', '_key', '_no', '_num', '_date', '_time',
                     '_flag', '_type', '_status', '_count', '_amt', '_price',
                     '_name', '_desc', '_note', '_memo', '_addr', '_tel',
                     '_email', '_phone', '_url', '_path', '_file']

        base = normalized
        for p in prefixes:
            if base.startswith(p):
                base = base[len(p):]
        for s in suffixes:
            if base.endswith(s):
                base = base[:-len(s)]
        return base or normalized

    def get_canonical(self, name: str) -> Optional[str]:
        """동의어 사전에서 정규 이름 반환"""
        base = self.extract_base_name(name)
        return self._synonym_map.get(base)

    def find_exact_matches(self, all_columns: list[dict]) -> list[Relationship]:
        """동일 필드명 매칭"""
        groups = defaultdict(list)
        for col in all_columns:
            key = col["column_name"].lower()
            groups[key].append(col)

        relationships = []
        for key, cols in groups.items():
            if len(cols) < 2:
                continue
            # 같은 DB 내에서는 건너뛰기
            for i in range(len(cols)):
                for j in range(i + 1, len(cols)):
                    if cols[i]["database"] == cols[j]["database"] and cols[i]["table"] == cols[j]["table"]:
                        continue
                    relationships.append(Relationship(
                        source_db=cols[i]["database"], source_schema=cols[i]["schema"],
                        source_table=cols[i]["table"], source_column=cols[i]["column_name"],
                        target_db=cols[j]["database"], target_schema=cols[j]["schema"],
                        target_table=cols[j]["table"], target_column=cols[j]["column_name"],
                        relation_type="EXACT_MATCH", confidence=0.90,
                        detected_by="NamingPattern",
                        evidence=f"동일 필드명: {key}"
                    ))
        return relationships

    def find_normalized_matches(self, all_columns: list[dict]) -> list[Relationship]:
        """정규화된 필드명 매칭 (camelCase ↔ snake_case)"""
        groups = defaultdict(list)
        seen_pairs = set()
        for col in all_columns:
            normalized = self.normalize(col["column_name"])
            groups[normalized].append(col)

        relationships = []
        for key, cols in groups.items():
            if len(cols) < 2:
                continue
            for i in range(len(cols)):
                for j in range(i + 1, len(cols)):
                    pair_key = (cols[i]["id"], cols[j]["id"])
                    if pair_key in seen_pairs:
                        continue
                    # 정확 매칭이 아닌 것만 (이미 잡힌 것 제외)
                    if cols[i]["column_name"].lower() == cols[j]["column_name"].lower():
                        continue
                    seen_pairs.add(pair_key)
                    seen_pairs.add((cols[j]["id"], cols[i]["id"]))
                    relationships.append(Relationship(
                        source_db=cols[i]["database"], source_schema=cols[i]["schema"],
                        source_table=cols[i]["table"], source_column=cols[i]["column_name"],
                        target_db=cols[j]["database"], target_schema=cols[j]["schema"],
                        target_table=cols[j]["table"], target_column=cols[j]["column_name"],
                        relation_type="NORMALIZED", confidence=0.75,
                        detected_by="NamingPattern",
                        evidence=f"정규화 매칭: {cols[i]['column_name']} ≈ {cols[j]['column_name']} → {key}"
                    ))
        return relationships

    def find_synonym_matches(self, all_columns: list[dict]) -> list[Relationship]:
        """동의어 기반 매칭"""
        groups = defaultdict(list)
        for col in all_columns:
            canonical = self.get_canonical(col["column_name"])
            if canonical:
                groups[canonical].append(col)

        relationships = []
        for key, cols in groups.items():
            if len(cols) < 2:
                continue
            for i in range(len(cols)):
                for j in range(i + 1, len(cols)):
                    relationships.append(Relationship(
                        source_db=cols[i]["database"], source_schema=cols[i]["schema"],
                        source_table=cols[i]["table"], source_column=cols[i]["column_name"],
                        target_db=cols[j]["database"], target_schema=cols[j]["schema"],
                        target_table=cols[j]["table"], target_column=cols[j]["column_name"],
                        relation_type="SYNONYM", confidence=0.55,
                        detected_by="NamingPattern",
                        evidence=f"동의어: {self.extract_base_name(cols[i]['column_name'])} ≈ {self.extract_base_name(cols[j]['column_name'])}"
                    ))
        return relationships

    def find_comment_matches(self, all_columns: list[dict]) -> list[Relationship]:
        """컬럼 코멘트(description) 기반 유사도 매칭"""
        has_comment = [c for c in all_columns if c.get("description")]
        if len(has_comment) < 2:
            return []

        def tokenize(text: str) -> set:
            import re
            tokens = re.sub(r"[^\w가-힣\s]", " ", str(text).lower()).split()
            return {t for t in tokens if len(t) > 1}

        relationships = []
        seen = set()
        for i in range(len(has_comment)):
            for j in range(i + 1, len(has_comment)):
                c1, c2 = has_comment[i], has_comment[j]
                pair = (c1["id"], c2["id"])
                if pair in seen:
                    continue
                seen.add(pair)
                seen.add((c2["id"], c1["id"]))

                tok1 = tokenize(c1["description"])
                tok2 = tokenize(c2["description"])
                if not tok1 or not tok2:
                    continue

                inter = tok1 & tok2
                jaccard = len(inter) / len(tok1 | tok2) if tok1 | tok2 else 0
                if jaccard < 0.3:
                    continue

                relationships.append(Relationship(
                    source_db=c1["database"], source_schema=c1["schema"],
                    source_table=c1["table"], source_column=c1["column_name"],
                    target_db=c2["database"], target_schema=c2["schema"],
                    target_table=c2["table"], target_column=c2["column_name"],
                    relation_type="COMMENT_MATCH", confidence=min(0.85, jaccard + 0.3),
                    detected_by="NamingPattern",
                    evidence=f"코멘트 유사: {jaccard:.0%} | {' '.join(inter)[:60]}"
                ))
        return relationships

    def find_all(self, all_columns: list[dict]) -> list[Relationship]:
        """모든 명명 패턴 관계 탐지"""
        results = []
        results.extend(self.find_exact_matches(all_columns))
        results.extend(self.find_normalized_matches(all_columns))
        results.extend(self.find_synonym_matches(all_columns))
        results.extend(self.find_comment_matches(all_columns))
        return results


# ── 2. 데이터 유사도 분석기 ─────────────────────────────

class DataSimilarityAnalyzer:
    """샘플 데이터 기반 연관관계 탐지"""

    def __init__(self, connection_map: dict = None):
        """
        connection_map: {db_name: connection_object} — 샘플 데이터 조회용
        """
        self.connection_map = connection_map or {}

    def jaccard_similarity(self, set1: set, set2: set) -> float:
        if not set1 or not set2:
            return 0.0
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        return intersection / union if union > 0 else 0.0

    def pattern_match_score(self, sample1: list, sample2: list) -> float:
        """데이터 패턴 매칭 점수"""
        patterns = {
            'email': r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$',
            'phone': r'^\d{2,3}[-\s]?\d{3,4}[-\s]?\d{4}$',
            'date': r'^\d{4}[-/]\d{2}[-/]\d{2}',
            'uuid': r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            'url': r'^https?://',
            'ip': r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$',
            'numeric': r'^\d+(\.\d+)?$',
            'alphanumeric': r'^[a-zA-Z0-9]+$',
            'korean': r'^[가-힣]+$',
        }
        def detect_pattern(values):
            if not values:
                return set()
            detected = set()
            pattern_scores = defaultdict(int)
            for v in values[:50]:  # 최대 50개 샘플
                v_str = str(v)
                for pname, regex in patterns.items():
                    if re.match(regex, v_str):
                        pattern_scores[pname] += 1
            threshold = len(values[:50]) * 0.5
            return {p for p, c in pattern_scores.items() if c >= threshold}

        pat1 = detect_pattern(sample1)
        pat2 = detect_pattern(sample2)
        if not pat1 or not pat2:
            return 0.0
        return 1.0 if pat1 & pat2 else 0.3 if pat1 | pat2 else 0.0

    def profile_similarity(self, profile1: dict, profile2: dict) -> dict:
        """데이터 프로파일 유사도"""
        score = 0.0
        factors = []

        # 타입 일치
        if profile1.get("type") == profile2.get("type"):
            score += 0.3
            factors.append("type_match")

        # 평균 길이 유사
        len1 = profile1.get("avg_length", 0)
        len2 = profile2.get("avg_length", 0)
        if len1 > 0 and len2 > 0:
            ratio = min(len1, len2) / max(len1, len2)
            if ratio > 0.8:
                score += 0.2
                factors.append("length_similar")

        # NULL 비율 유사
        null1 = profile1.get("null_ratio", 0)
        null2 = profile2.get("null_ratio", 0)
        if abs(null1 - null2) < 0.1:
            score += 0.1
            factors.append("null_ratio_similar")

        # 유니크 비율 유성
        uniq1 = profile1.get("unique_ratio", 0)
        uniq2 = profile2.get("unique_ratio", 0)
        if abs(uniq1 - uniq2) < 0.15:
            score += 0.15
            factors.append("unique_ratio_similar")

        return {"score": score, "factors": factors}

    def fk_validation_by_values(
        self,
        candidates: list[Relationship],
        db_config: dict,
        min_overlap_pct: float = 0.5,
    ) -> list[Relationship]:
        """실제 데이터 값 교집합으로 관계 검증 (PostgreSQL)
        candidates: 검증할 관계 후보 목록
        db_config: {"host", "port", "dbname", "user", "password"}
        min_overlap_pct: 최소 overlap 비율 (기본 50%)
        """
        import psycopg2
        validated = []
        conn = psycopg2.connect(**db_config)
        cur = conn.cursor()

        for rel in candidates:
            try:
                src_full = f"{rel.source_schema}.{rel.source_table}"
                tgt_full = f"{rel.target_schema}.{rel.target_table}"

                qi = lambda s: '"' + s.replace('"', '""') + '"'
                src_qi = f"{qi(rel.source_schema)}.{qi(rel.source_table)}"
                tgt_qi = f"{qi(rel.target_schema)}.{qi(rel.target_table)}"

                src_col = qi(rel.source_column)
                tgt_col = qi(rel.target_column)

                # overlap count
                cur.execute(f"""
                    SELECT COUNT(*) FROM {src_qi} s
                    WHERE s.{src_col} IS NOT NULL
                      AND EXISTS (
                        SELECT 1 FROM {tgt_qi} t
                        WHERE t.{tgt_col} = s.{src_col}
                      )
                """)
                overlap = cur.fetchone()[0]

                cur.execute(f"SELECT COUNT(*) FROM {src_qi} WHERE {src_col} IS NOT NULL")
                src_total = cur.fetchone()[0]

                cur.execute(f"SELECT COUNT(*) FROM {tgt_qi} WHERE {tgt_col} IS NOT NULL")
                tgt_total = cur.fetchone()[0]

                src_pct = overlap / src_total if src_total > 0 else 0
                tgt_pct = overlap / tgt_total if tgt_total > 0 else 0
                avg_pct = (src_pct + tgt_pct) / 2

                if avg_pct >= min_overlap_pct:
                    validated.append(Relationship(
                        source_db=rel.source_db, source_schema=rel.source_schema,
                        source_table=rel.source_table, source_column=rel.source_column,
                        target_db=rel.target_db, target_schema=rel.target_schema,
                        target_table=rel.target_table, target_column=rel.target_column,
                        relation_type="DATA_SIMILAR", confidence=min(0.95, avg_pct),
                        detected_by="DataSimilarity",
                        evidence=f"값 중복: src={overlap}/{src_total}({src_pct:.0%}) tgt={overlap}/{tgt_total}({tgt_pct:.0%})"
                    ))
            except Exception as e:
                continue

        cur.close()
        conn.close()
        return validated


# ── 3. FK/인덱스 기반 관계 탐지기 ───────────────────────

class StructuralAnalyzer:
    """구조적 관계 (FK, 인덱스) 탐지"""

    def __init__(self, store: 'MetadataStore'):
        self.store = store

    def find_fk_relationships(self) -> list[Relationship]:
        """FK 제약조건 기반 관계"""
        cur = self.store.conn.cursor()
        cur.execute("""
            SELECT
                d1.name as source_db, t1.schema_name as source_schema,
                t1.table_name as source_table, c1.column_name as source_column,
                c1.fk_references
            FROM columns c1
            JOIN tables t1 ON c1.table_id = t1.id
            JOIN databases d1 ON t1.database_id = d1.id
            WHERE c1.is_foreign_key = 1 AND c1.fk_references IS NOT NULL
        """)
        relationships = []
        for row in cur.fetchall():
            # 참조 대상 컬럼 찾기
            fk_ref = row[4]  # "schema.table.column"
            if fk_ref:
                parts = fk_ref.split(".")
                if len(parts) >= 3:
                    ref_schema, ref_table, ref_column = parts[0], parts[1], parts[2]
                elif len(parts) == 2:
                    ref_schema, ref_table, ref_column = "main", parts[0], parts[1]
                else:
                    continue

                relationships.append(Relationship(
                    source_db=row[0], source_schema=row[1],
                    source_table=row[2], source_column=row[3],
                    target_db=row[0],  # 같은 DB 내
                    target_schema=ref_schema, target_table=ref_table,
                    target_column=ref_column,
                    relation_type="FK", confidence=0.95,
                    detected_by="Structural",
                    evidence=f"외래키: {row[3]} → {fk_ref}"
                ))
        cur.close()
        return relationships


# ── 5. 통합 분석 오케스트레이터 ─────────────────────────

class RelationshipOrchestrator:
    """모든 분석 엔진을 통합하여 연관관계 탐지"""

    def __init__(self, store):
        self.store = store
        self.naming_analyzer = NamingPatternAnalyzer()
        self.structural_analyzer = StructuralAnalyzer(store)
        self.data_analyzer = DataSimilarityAnalyzer()

    def analyze_all(self) -> list[Relationship]:
        """전체 연관관계 분석"""
        all_columns = self.store.get_all_columns()
        print(f"  📊 분석 대상: {len(all_columns)}개 컬럼")

        all_relationships = []

        # 1. FK 기반 관계
        print("  🔍 FK 분석...")
        fk_rels = self.structural_analyzer.find_fk_relationships()
        all_relationships.extend(fk_rels)
        print(f"     → {len(fk_rels)}개 FK 관계")

        # 2. 명명 패턴 분석
        print("  🔍 명명 패턴 분석...")
        naming_rels = self.naming_analyzer.find_all(all_columns)
        all_relationships.extend(naming_rels)
        print(f"     → {len(naming_rels)}개 명명 패턴 관계")

        # 3. 중복 제거 및 신뢰도 병합
        merged = self._merge_relationships(all_relationships)
        print(f"  ✅ 최종 관계: {len(merged)}개")

        # 4. 결과 저장
        for rel in merged:
            self._save_relationship(rel)

        return merged

    def _merge_relationships(self, relationships: list[Relationship]) -> list[Relationship]:
        """중복 관계 병합 및 신뢰도 통합"""
        groups = defaultdict(list)
        for rel in relationships:
            key = (rel.source_db, rel.source_table, rel.source_column,
                   rel.target_db, rel.target_table, rel.target_column)
            groups[key].append(rel)

        merged = []
        for key, rels in groups.items():
            if len(rels) == 1:
                merged.append(rels[0])
            else:
                # 여러 방법으로 탐지된 경우 신뢰도 통합
                best = max(rels, key=lambda r: r.confidence)
                total_confidence = max(r.confidence for r in rels)
                # 다중 근거 시 신뢰도 상향 (최대 0.98)
                boosted = min(total_confidence + 0.05 * (len(rels) - 1), 0.98)

                evidence_parts = []
                for r in rels:
                    evidence_parts.append(f"[{r.relation_type}] {r.evidence}")

                merged.append(Relationship(
                    source_db=rels[0].source_db, source_schema=rels[0].source_schema,
                    source_table=rels[0].source_table, source_column=rels[0].source_column,
                    target_db=rels[0].target_db, target_schema=rels[0].target_schema,
                    target_table=rels[0].target_table, target_column=rels[0].target_column,
                    relation_type=best.relation_type,
                    confidence=boosted,
                    detected_by="Orchestrator(merged)",
                    evidence=" | ".join(evidence_parts)
                ))

        # 신뢰도 순 정렬
        merged.sort(key=lambda r: r.confidence, reverse=True)
        return merged

    def _save_relationship(self, rel: Relationship):
        # 컬럼 ID 조회
        cur = self.store.conn.cursor()
        cur.execute("""
            SELECT c.id FROM columns c
            JOIN tables t ON c.table_id = t.id
            JOIN databases d ON t.database_id = d.id
            WHERE d.name = ? AND t.schema_name = ? AND t.table_name = ? AND c.column_name = ?
        """, (rel.source_db, rel.source_schema, rel.source_table, rel.source_column))
        source_row = cur.fetchone()

        cur.execute("""
            SELECT c.id FROM columns c
            JOIN tables t ON c.table_id = t.id
            JOIN databases d ON t.database_id = d.id
            WHERE d.name = ? AND t.schema_name = ? AND t.table_name = ? AND c.column_name = ?
        """, (rel.target_db, rel.target_schema, rel.target_table, rel.target_column))
        target_row = cur.fetchone()

        if source_row and target_row:
            cur.execute("""
                INSERT OR REPLACE INTO relationships
                    (source_column_id, target_column_id, relation_type, confidence,
                     detected_by, notes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (source_row[0], target_row[0], rel.relation_type,
                  rel.confidence, rel.detected_by, rel.evidence))
            self.store.conn.commit()


# ── 테스트 ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from collector.db_adapter import SQLiteAdapter, MetadataStore, SchemaCollector

    # 테스트 DB 생성
    test_db_path = "/tmp/test_analyzer.db"
    metadata_path = "/tmp/test_analyzer_meta.db"

    if os.path.exists(test_db_path): os.remove(test_db_path)
    if os.path.exists(metadata_path): os.remove(metadata_path)

    conn = sqlite3.connect(test_db_path)
    conn.executescript("""
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            user_name TEXT NOT NULL,
            email_addr TEXT,
            phone_num TEXT,
            created_at TIMESTAMP,
            status TEXT
        );
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            order_date TIMESTAMP,
            total_amount REAL,
            payment_status TEXT,
            FOREIGN KEY (customer_id) REFERENCES users(user_id)
        );
        CREATE TABLE order_detail (
            detail_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            prod_id INTEGER,
            unit_price REAL,
            quantity INTEGER,
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        );
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY,
            product_name TEXT,
            category TEXT,
            price REAL,
            stock_qty INTEGER
        );
        CREATE TABLE payments (
            payment_id INTEGER PRIMARY KEY,
            order_id INTEGER,
            amount REAL,
            payment_method TEXT,
            payment_date TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        );
    """)
    conn.close()

    # 스키마 수집
    store = MetadataStore(db_path=metadata_path)
    collector = SchemaCollector(store)
    collector.add_database("sqlite", file_path=test_db_path, db_name="shop")
    collector.collect_all()

    # 연관관계 분석
    print("\n=== 연관관계 분석 ===")
    orchestrator = RelationshipOrchestrator(store)
    results = orchestrator.analyze_all()

    # 결과 출력
    print("\n=== 탐지된 연관관계 ===")
    for rel in results:
        status = "✅" if rel.confidence >= 0.9 else "💡" if rel.confidence >= 0.7 else "⚠️"
        print(f"  {status} [{rel.relation_type}] {rel.confidence:.2f}")
        print(f"     {rel.source_table}.{rel.source_column} → {rel.target_table}.{rel.target_column}")
        print(f"     ({rel.detected_by}) {rel.evidence}")

    store.close()
    os.remove(test_db_path)
    os.remove(metadata_path)
    print("\n✅ 분석 테스트 완료")
