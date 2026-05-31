"""
Insight Engine - 관계 네트워크 기반 비즈니스 인사이트 도출
조인 경로, 비즈니스 엔티티, 영향도 분석, 데이터 품질
"""

import sqlite3
from collections import defaultdict

import networkx as nx


class InsightEngine:
    def __init__(self, store, graph):
        self.store = store
        self.graph = graph
        self._table_graph = None

    # ── 1. 조인 경로 탐색 ─────────────────────────────────

    def _build_table_graph(self):
        """테이블 간 관계 그래프 (컬럼 관계를 테이블 레벨로 집계)"""
        if self._table_graph is not None:
            return self._table_graph

        conn = self.store.conn
        cur = conn.cursor()

        g = nx.Graph()

        cur.execute("SELECT id, table_name, schema_name FROM tables")
        for rid, rname, rschema in cur.fetchall():
            g.add_node(f"table_{rschema}.{rname}", name=rname, schema=rschema,
                       label=f"{rschema}.{rname}")

        cur.execute("""
            SELECT DISTINCT st.table_name, st.schema_name,
                            tt.table_name, tt.schema_name
            FROM relationships r
            JOIN columns sc ON r.source_column_id = sc.id
            JOIN tables st ON sc.table_id = st.id
            JOIN columns tc ON r.target_column_id = tc.id
            JOIN tables tt ON tc.table_id = tt.id
            WHERE r.confidence >= 0.6
        """)
        for src_t, src_s, tgt_t, tgt_s in cur.fetchall():
            if f"table_{src_s}.{src_t}" in g and f"table_{tgt_s}.{tgt_t}" in g:
                g.add_edge(f"table_{src_s}.{src_t}", f"table_{tgt_s}.{tgt_t}")

        self._table_graph = g
        return g

    def find_join_paths(
        self,
        source_table: str,
        target_table: str,
        schema: str = None,
        max_paths: int = 5,
    ) -> list[dict]:
        conn = self.store.conn
        cur = conn.cursor()

        q_schema = schema or "%"
        cur.execute("""
            SELECT table_name, schema_name FROM tables
            WHERE table_name = ? AND schema_name LIKE ?
        """, (source_table, q_schema))
        src = cur.fetchone()
        cur.execute("""
            SELECT table_name, schema_name FROM tables
            WHERE table_name = ? AND schema_name LIKE ?
        """, (target_table, q_schema))
        tgt = cur.fetchone()

        if not src or not tgt:
            return []

        src_node = f"table_{src[1]}.{src[0]}"
        tgt_node = f"table_{tgt[1]}.{tgt[0]}"

        tg = self._build_table_graph()
        if src_node not in tg or tgt_node not in tg:
            return []

        paths = []
        try:
            for i, path in enumerate(nx.shortest_simple_paths(tg, src_node, tgt_node)):
                if i >= max_paths or len(path) > 6:
                    break
                steps = []
                for j in range(len(path) - 1):
                    a_label = path[j].replace("table_", "", 1)
                    b_label = path[j + 1].replace("table_", "", 1)
                    steps.append({"from": a_label, "to": b_label})
                if steps:
                    paths.append({
                        "path_index": i,
                        "length": len(steps),
                        "steps": steps,
                    })
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            pass

        return paths

    def find_all_join_paths(
        self, max_paths_per_pair: int = 3, max_pairs: int = 20
    ) -> list[dict]:
        """모든 테이블 쌍 간 조인 경로 자동 탐색"""
        conn = self.store.conn
        cur = conn.cursor()
        cur.execute("SELECT id, table_name, schema_name FROM tables ORDER BY schema_name, table_name")
        tables = cur.fetchall()

        results = []
        count = 0
        for i in range(len(tables)):
            for j in range(i + 1, len(tables)):
                if count >= max_pairs:
                    break
                src_name = f"{tables[i][2]}.{tables[i][1]}"
                tgt_name = f"{tables[j][2]}.{tables[j][1]}"
                paths = self.find_join_paths(
                    tables[i][1], tables[j][1],
                    schema=tables[i][2], max_paths=1
                )
                if paths:
                    results.append({
                        "source": src_name,
                        "target": tgt_name,
                        "paths": paths,
                    })
                    count += 1
            if count >= max_pairs:
                break

        return results

    # ── 2. 비즈니스 엔티티 ─────────────────────────────────

    def detect_entities(self) -> list[dict]:
        conn = self.store.conn
        cur = conn.cursor()

        # 테이블+코멘트 조회
        cur.execute("""
            SELECT t.id, t.table_name, t.schema_name, t.description
            FROM tables t
            ORDER BY t.schema_name, t.table_name
        """)
        tables = cur.fetchall()
        table_map = {f"table_{t[0]}": {"id": t[0], "name": t[1], "schema": t[2],
                                        "comment": t[3] or ""} for t in tables}

        # FK 기반 강한 연결 서브그래프
        fk_graph = nx.Graph()
        for tid, _, _, _ in tables:
            fk_graph.add_node(f"table_{tid}")

        cur.execute("""
            SELECT c1.table_id, c2.table_id
            FROM columns c1
            JOIN columns c2 ON c1.fk_references = (
                SELECT t2.schema_name || '.' || t2.table_name || '.' || c2.column_name
                FROM tables t2 WHERE t2.id = c2.table_id
            )
            WHERE c1.is_foreign_key = 1 AND c1.fk_references IS NOT NULL
        """)
        for src_tid, tgt_tid in cur.fetchall():
            fk_graph.add_edge(f"table_{src_tid}", f"table_{tgt_tid}")

        # DATA_SIMILAR도 엔티티 연결에 포함
        cur.execute("""
            SELECT DISTINCT sc.table_id, tc.table_id
            FROM relationships r
            JOIN columns sc ON r.source_column_id = sc.id
            JOIN columns tc ON r.target_column_id = tc.id
            WHERE r.relation_type IN ('DATA_SIMILAR', 'FK', 'EXACT_MATCH')
              AND r.confidence >= 0.7
        """)
        for src_tid, tgt_tid in cur.fetchall():
            fk_graph.add_edge(f"table_{src_tid}", f"table_{tgt_tid}")

        # 커뮤니티 탐지
        communities = list(nx.community.greedy_modularity_communities(fk_graph))
        if not communities:
            communities = [set(fk_graph.nodes())]

        entities = []
        for i, community in enumerate(communities):
            entity_tables = []
            all_comments = []
            for node in community:
                info = table_map.get(node)
                if info:
                    entity_tables.append(f"{info['schema']}.{info['name']}")
                    if info["comment"]:
                        all_comments.append(info["comment"])

            if not entity_tables:
                continue

            # 엔티티 이름 추론: 가장 짧은 공통 접두사 또는 첫 테이블 기준
            entity_name = self._infer_entity_name(entity_tables, all_comments)

            entities.append({
                "id": i + 1,
                "name": entity_name,
                "tables": entity_tables,
                "table_count": len(entity_tables),
            })

        entities.sort(key=lambda e: e["table_count"], reverse=True)
        return entities

    def _infer_entity_name(self, tables: list[str], comments: list[str]) -> str:
        name_hints = {
            "user": "사용자/계정",
            "member": "사용자/계정",
            "account": "사용자/계정",
            "emp": "직원/인사",
            "employee": "직원/인사",
            "dept": "부서/조직",
            "department": "부서/조직",
            "order": "주문/거래",
            "payment": "결제/회계",
            "pay": "결제/회계",
            "product": "상품/재고",
            "item": "상품/재고",
            "inventory": "상품/재고",
            "project": "프로젝트/업무",
            "salary": "급여/보상",
            "history": "이력/로그",
            "log": "이력/로그",
        }

        # 코멘트 기반
        for c in comments:
            for key, name in name_hints.items():
                if key in c.lower():
                    return name

        # 테이블명 기반
        all_text = " ".join(tables).lower()
        for key, name in name_hints.items():
            if key in all_text:
                return name

        # 첫 테이블의 스키마 기준
        first = tables[0]
        schema = first.split(".")[0]
        schema_names = {"hr": "인사/조직", "public": "서비스/운영", "sales": "영업/판매",
                        "finance": "재무/회계", "logistics": "물류/배송"}
        return schema_names.get(schema, f"엔티티_{tables[0].split('.')[-1].split('_')[0]}")

    # ── 3. 영향도 분석 ────────────────────────────────────

    def impact_analysis(self, target: str, target_type: str = "table",
                        max_depth: int = 3) -> dict:
        """target: table_name or column_name
        target_type: 'table' or 'column'
        """
        conn = self.store.conn
        cur = conn.cursor()

        if target_type == "table":
            cur.execute("""
                SELECT id, table_name, schema_name FROM tables
                WHERE table_name = ? OR ? || '.' || ? = schema_name || '.' || table_name
            """, (target, target.split(".")[0] if "." in target else "",
                  target.split(".")[1] if "." in target else target))
            row = cur.fetchone()
            if not row:
                return {}
            start_node = f"table_{row[0]}"
            label = f"{row[2]}.{row[1]}"
        else:
            cur.execute("""
                SELECT c.id, c.column_name, t.table_name, t.schema_name
                FROM columns c
                JOIN tables t ON c.table_id = t.id
                WHERE c.column_name = ?
            """, (target.split(".")[-1],))
            row = cur.fetchone()
            if not row:
                return {}
            start_node = f"col_{row[0]}"
            label = f"{row[3]}.{row[2]}.{row[1]}"

        if start_node not in self.graph:
            return {}

        # BFS로 영향도 추적
        visited = set()
        queue = [(start_node, 0)]
        impacts = defaultdict(list)

        while queue:
            node, depth = queue.pop(0)
            if node in visited or depth > max_depth:
                continue
            visited.add(node)

            for neighbor in self.graph.neighbors(node):
                if neighbor not in visited:
                    edge = self.graph.get_edge_data(node, neighbor)
                    rel_type = "연결"
                    confidence = 1.0
                    if edge:
                        for _, ed in edge.items():
                            rel_type = ed.get("type", "연결")
                            confidence = ed.get("confidence", 1.0)
                            break

                    n_attrs = self.graph.nodes[neighbor]
                    n_label = n_attrs.get("label", neighbor)
                    n_type = n_attrs.get("type", "")
                    n_group = n_attrs.get("group", n_type)

                    impacts[depth + 1].append({
                        "node": neighbor,
                        "label": n_label,
                        "type": n_group,
                        "relation": rel_type,
                        "confidence": confidence,
                    })
                    queue.append((neighbor, depth + 1))

        severity = "낮음"
        if any(item["relation"] == "FK" for items in impacts.values() for item in items):
            severity = "높음"
        elif impacts:
            severity = "중간"

        return {
            "target": target,
            "target_label": label,
            "max_depth": max_depth,
            "severity": severity,
            "total_affected": len(visited) - 1,
            "impacts": [
                {"depth": d, "items": items}
                for d, items in sorted(impacts.items())
            ],
        }

    def find_impactful_tables(self, min_connections: int = 3) -> list[dict]:
        conn = self.store.conn
        cur = conn.cursor()
        cur.execute("""
            SELECT t.id, t.table_name, t.schema_name, t.description,
                   COUNT(DISTINCT r.id) as rel_count
            FROM tables t
            JOIN columns c ON c.table_id = t.id
            LEFT JOIN relationships r ON r.source_column_id = c.id OR r.target_column_id = c.id
            GROUP BY t.id
            HAVING rel_count >= ?
            ORDER BY rel_count DESC
            LIMIT 20
        """, (min_connections,))
        results = []
        for r in cur.fetchall():
            results.append({
                "table": f"{r[2]}.{r[1]}",
                "description": r[3] or "",
                "relationship_count": r[4],
            })
        return results

    # ── 4. 데이터 품질 리포트 ──────────────────────────────

    def quality_report(self) -> dict:
        conn = self.store.conn
        cur = conn.cursor()

        # 누락 FK 추천
        cur.execute("""
            SELECT r.confidence,
                   sc.column_name, st.table_name, st.schema_name,
                   tc.column_name, tt.table_name, tt.schema_name
            FROM relationships r
            JOIN columns sc ON r.source_column_id = sc.id
            JOIN tables st ON sc.table_id = st.id
            JOIN columns tc ON r.target_column_id = tc.id
            JOIN tables tt ON tc.table_id = tt.id
            WHERE r.relation_type = 'DATA_SIMILAR' AND r.confidence >= 0.8
            ORDER BY r.confidence DESC
        """)
        missing_fk = []
        for r in cur.fetchall():
            missing_fk.append({
                "source": f"{r[3]}.{r[1]}",
                "target": f"{r[6]}.{r[4]}",
                "confidence": r[0],
                "suggestion": f"CREATE FOREIGN KEY ({r[1]}) REFERENCES {r[5]}({r[4]})",
            })

        # 고립 테이블
        cur.execute("""
            SELECT t.table_name, t.schema_name, t.description
            FROM tables t
            WHERE t.id NOT IN (
                SELECT DISTINCT sc.table_id FROM relationships r
                JOIN columns sc ON r.source_column_id = sc.id
                UNION
                SELECT DISTINCT tc.table_id FROM relationships r
                JOIN columns tc ON r.target_column_id = tc.id
            )
        """)
        isolated = [f"{r[1]}.{r[0]}" for r in cur.fetchall()]

        # 문서화 부족
        cur.execute("""
            SELECT t.table_name, t.schema_name
            FROM tables t
            WHERE t.description IS NULL OR t.description = ''
        """)
        no_desc_tables = [f"{r[1]}.{r[0]}" for r in cur.fetchall()]

        cur.execute("""
            SELECT c.column_name, t.table_name, t.schema_name
            FROM columns c
            JOIN tables t ON c.table_id = t.id
            WHERE (c.description IS NULL OR c.description = '')
              AND c.is_primary_key = 0 AND c.is_foreign_key = 0
            LIMIT 30
        """)
        no_desc_cols = [f"{r[2]}.{r[1]}.{r[0]}" for r in cur.fetchall()]

        # 가장 많이 연결된 테이블 (허브)
        cur.execute("""
            SELECT t.table_name, t.schema_name, COUNT(DISTINCT r.id) as rel_count
            FROM tables t
            JOIN columns c ON c.table_id = t.id
            JOIN relationships r ON r.source_column_id = c.id OR r.target_column_id = c.id
            GROUP BY t.id
            ORDER BY rel_count DESC
            LIMIT 5
        """)
        hubs = [f"{r[1]}.{r[0]}({r[2]}개 관계)" for r in cur.fetchall()]

        return {
            "missing_fk": missing_fk,
            "isolated_tables": isolated,
            "no_description_tables": no_desc_tables,
            "no_description_columns": no_desc_cols,
            "hub_tables": hubs,
            "summary": {
                "total_missing_fk": len(missing_fk),
                "total_isolated": len(isolated),
                "total_no_desc_tables": len(no_desc_tables),
                "total_no_desc_cols": len(no_desc_cols),
            },
        }

    # ── 5. 분석 추천 ─────────────────────────────────────

    _description_templates = [
        ({"user", "order"}, "고객 정보와 주문 내역을 연결하여 고객별 구매 패턴 분석"),
        ({"user", "order", "item", "product"}, "고객별 주문 상품 구성 및 선호도 분석"),
        ({"order", "item", "product"}, "주문별 상품 구성과 매출 기여도 분석"),
        ({"user", "order", "payment"}, "고객의 주문-결제 전체 구매 흐름 분석"),
        ({"order", "payment"}, "주문별 결제 내역 및 결제 상태 추적"),
        ({"order", "payment", "item", "product"}, "결제된 주문의 상품별 매출 구성 분석"),
        ({"employee", "salary", "history"}, "직원별 급여 변동 이력 및 추세 분석"),
        ({"employee", "department"}, "부서별 직원 구성 및 인력 분포 분석"),
        ({"employee", "department", "salary"}, "부서별 직원 급여 현황 및 비교 분석"),
        ({"project", "employee"}, "프로젝트별 참여 직원 구성 및 리더십 분석"),
        ({"product", "category"}, "상품 카테고리별 분류 및 구성 분석"),
        ({"user", "payment", "method"}, "고객별 결제 수단 선호도 및 금액 패턴 분석"),
        ({"order", "status", "payment", "status"}, "주문-결제 상태 연계 및 지연 구간 분석"),
        ({"user", "order", "item"}, "고객의 반복 주문 상품 및 재구매 패턴 분석"),
        ({"department", "employee", "project"}, "부서별 담당 프로젝트 현황 분석"),
        ({"salary", "history", "employee", "department"}, "부서별 급여 인상률 및 추세 비교"),
        ({"user", "order", "product", "price"}, "고객 세그먼트별 선호 상품 가격대 분석"),
        ({"order", "payment", "amount"}, "주문 금액 대비 결제 금액 정합성 검증"),
        ({"employee", "manager"}, "조직 계층 구조 및 관리 체계 분석"),
        ({"product", "stock", "inventory"}, "상품 재고 현황 및 재고 회전율 분석"),
    ]

    def generate_recommendations(self, max_results: int = 20) -> list[dict]:
        conn = self.store.conn
        cur = conn.cursor()

        cur.execute("""
            SELECT t.table_name, t.schema_name, t.description
            FROM tables t ORDER BY t.schema_name, t.table_name
        """)
        all_tables = {f"{r[1]}.{r[0]}": {"name": r[0], "schema": r[1],
                      "comment": r[2] or ""} for r in cur.fetchall()}

        tg = self._build_table_graph()

        path_chains = set()
        tables_list = list(all_tables.keys())

        for i in range(len(tables_list)):
            for j in range(i + 1, len(tables_list)):
                src = f"table_{tables_list[i]}"
                tgt = f"table_{tables_list[j]}"
                if src not in tg or tgt not in tg:
                    continue
                try:
                    for path in nx.shortest_simple_paths(tg, src, tgt):
                        if len(path) < 3 or len(path) > 6:
                            break
                        chain = []
                        skip = False
                        for node in path:
                            label = node.replace("table_", "", 1)
                            parts = label.split(".")
                            chain.append((parts[0], parts[1]))
                        chain_tuple = tuple(chain)
                        if chain_tuple not in path_chains:
                            path_chains.add(chain_tuple)
                        break
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue

        recommendations = []
        for chain in path_chains:
            if len(chain) < 2 or len(chain) > 5:
                continue

            path_str = " → ".join(f"{s}.{t}" for s, t in chain)
            schema_set = set(s for s, _ in chain)
            name_set = set(t for _, t in chain)

            description = self._generate_description(chain, all_tables, name_set)

            joins = self._get_joins_for_chain(chain)

            result_cols = self._get_result_columns(chain)

            rel_confidences = [j.get("confidence", 1.0) for j in joins]
            avg_conf = sum(rel_confidences) / len(rel_confidences) if rel_confidences else 1.0
            col_score = min(len(result_cols) * 5, 30)
            join_score = min(len(joins) * 10, 30)
            desc_score = 20 if description else 0
            conf_score = int(avg_conf * 20)
            total = col_score + join_score + desc_score + conf_score

            recommendations.append({
                "title": description or f"{chain[0][1]} → {chain[-1][1]} 연결 분석",
                "path": path_str,
                "joins": joins,
                "result_columns": result_cols,
                "description": description,
                "value_score": total,
            })

        recommendations.sort(key=lambda r: r["value_score"], reverse=True)
        return recommendations[:max_results]

    def _get_joins_for_chain(self, chain: list) -> list[dict]:
        conn = self.store.conn
        cur = conn.cursor()
        joins = []
        for i in range(len(chain) - 1):
            src_s, src_t = chain[i]
            tgt_s, tgt_t = chain[i + 1]
            cur.execute("""
                SELECT sc.column_name, tc.column_name, r.relation_type, r.confidence
                FROM relationships r
                JOIN columns sc ON r.source_column_id = sc.id
                JOIN tables st ON sc.table_id = st.id
                JOIN columns tc ON r.target_column_id = tc.id
                JOIN tables tt ON tc.table_id = tt.id
                WHERE st.schema_name = ? AND st.table_name = ?
                  AND tt.schema_name = ? AND tt.table_name = ?
                ORDER BY r.confidence DESC LIMIT 1
            """, (src_s, src_t, tgt_s, tgt_t))
            row = cur.fetchone()
            if row:
                joins.append({
                    "from": f"{src_s}.{src_t}.{row[0]}",
                    "to": f"{tgt_s}.{tgt_t}.{row[1]}",
                    "type": row[2],
                    "confidence": row[3],
                })
        return joins

    def _get_result_columns(self, chain: list) -> list[dict]:
        conn = self.store.conn
        cur = conn.cursor()
        cols = []
        seen = set()
        for s, t in chain:
            cur.execute("""
                SELECT c.column_name, c.description, c.is_primary_key
                FROM columns c
                JOIN tables tb ON c.table_id = tb.id
                WHERE tb.schema_name = ? AND tb.table_name = ?
                ORDER BY c.ordinal_position
            """, (s, t))
            for row in cur.fetchall():
                if row[2] and row[0] in seen:
                    continue
                if row[0] not in seen:
                    seen.add(row[0])
                    cols.append({
                        "table": f"{s}.{t}",
                        "column": row[0],
                        "comment": row[1] or "",
                        "is_pk": bool(row[2]),
                    })
        return cols

    def _generate_description(self, chain: list, all_tables: dict,
                               name_set: set) -> str:
        comments = []
        for s, t in chain:
            key = f"{s}.{t}"
            info = all_tables.get(key)
            if info and info["comment"]:
                comments.append(info["comment"])

        # 템플릿 매칭 (테이블명 기반)
        for keywords, desc in self._description_templates:
            if keywords.issubset(name_set):
                return desc

        # 코멘트 기반
        comment_text = " ".join(comments).lower()
        comment_keywords = {
            "고객": "사용자/고객",
            "사용자": "사용자/고객",
            "주문": "주문/거래",
            "결제": "결제/회계",
            "급여": "급여/보상",
            "직원": "직원/인사",
            "부서": "부서/조직",
            "상품": "상품/재고",
            "프로젝트": "프로젝트",
        }
        found_cats = []
        for kw, cat in comment_keywords.items():
            if kw in comment_text:
                found_cats.append(cat)
        if found_cats:
            unique_cats = list(dict.fromkeys(found_cats))
            return f"{' · '.join(unique_cats)} 연계 분석"

        # 첫/마지막 테이블 기반
        first_comment = all_tables.get(f"{chain[0][0]}.{chain[0][1]}", {}).get("comment", "")
        last_comment = all_tables.get(f"{chain[-1][0]}.{chain[-1][1]}", {}).get("comment", "")
        if first_comment and last_comment:
            return f"{first_comment} → {last_comment} 연계 분석"

        return ""
