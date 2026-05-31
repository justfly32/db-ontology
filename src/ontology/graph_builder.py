"""
Ontology Graph Builder
네트워크 그래프로 온톨로지 구축 및 시각화 데이터 생성
"""

import os
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Optional

import networkx as nx


# ── 노드/엣지 타입 정의 ─────────────────────────────────

NODE_TYPES = {
    "DATABASE": {"color": "#58a6ff", "size": 30, "icon": "🗄️"},
    "SCHEMA": {"color": "#8b949e", "size": 20, "icon": "📁"},
    "TABLE": {"color": "#3fb950", "size": 25, "icon": "📋"},
    "COLUMN": {"color": "#d29922", "size": 15, "icon": "📌"},
    "DOMAIN": {"color": "#f85149", "size": 22, "icon": "🏷️"},
}

EDGE_TYPES = {
    "HAS_SCHEMA": {"color": "#8b949e", "width": 1, "style": "solid"},
    "HAS_TABLE": {"color": "#8b949e", "width": 1, "style": "solid"},
    "HAS_COLUMN": {"color": "#8b949e", "width": 1, "style": "solid"},
    "HAS_DOMAIN": {"color": "#d29922", "width": 1, "style": "dashed"},
    "FK": {"color": "#f85149", "width": 2, "style": "solid", "label": "FK"},
    "EXACT_MATCH": {"color": "#3fb950", "width": 1.5, "style": "dotted", "label": "정확매칭"},
    "NORMALIZED": {"color": "#58a6ff", "width": 1, "style": "dotted", "label": "정규화매칭"},
    "SYNONYM": {"color": "#d29922", "width": 1, "style": "dashed", "label": "동의어"},
    "SEMANTIC": {"color": "#a371f7", "width": 1.5, "style": "dashed", "label": "의미적"},
    "SIMILAR": {"color": "#a371f7", "width": 1, "style": "dotted", "label": "유사"},
    "DATA_SIMILAR": {"color": "#79c0ff", "width": 1, "style": "dotted", "label": "데이터유사"},
    "BELONGS_TO": {"color": "#8b949e", "width": 1, "style": "solid"},
}


class OntologyGraph:
    """온톨로지 그래프 빌더"""

    def __init__(self):
        self.graph = nx.DiGraph()
        self.metadata_store = None

    def build_from_store(self, store):
        """메타데이터 저장소에서 그래프 구축"""
        self.metadata_store = store
        conn = store.conn

        # 1. 데이터베이스 노드
        cur = conn.cursor()
        cur.execute("SELECT id, name, db_type FROM databases")
        dbs = cur.fetchall()

        for db_id, db_name, db_type in dbs:
            node_id = f"db_{db_id}"
            self.graph.add_node(node_id,
                type="DATABASE", label=db_name, db_type=db_type,
                color=NODE_TYPES["DATABASE"]["color"],
                size=NODE_TYPES["DATABASE"]["size"],
                icon=NODE_TYPES["DATABASE"]["icon"],
            )

            # 2. 스키마 노드
            cur.execute("""
                SELECT DISTINCT schema_name FROM tables WHERE database_id = ?
            """, (db_id,))
            schemas = [row[0] for row in cur.fetchall()]

            for schema in schemas:
                schema_node_id = f"schema_{db_id}_{schema}"
                self.graph.add_node(schema_node_id,
                    type="SCHEMA", label=schema,
                    color=NODE_TYPES["SCHEMA"]["color"],
                    size=NODE_TYPES["SCHEMA"]["size"],
                    icon=NODE_TYPES["SCHEMA"]["icon"],
                )
                self.graph.add_edge(node_id, schema_node_id,
                    type="HAS_SCHEMA", color="#8b949e", width=1,
                )

                # 3. 테이블 노드
                cur.execute("""
                    SELECT id, table_name, row_count, description
                    FROM tables WHERE database_id = ? AND schema_name = ?
                """, (db_id, schema))
                tables = cur.fetchall()

                for table_id, table_name, row_count, desc in tables:
                    table_node_id = f"table_{table_id}"
                    self.graph.add_node(table_node_id,
                        type="TABLE", label=table_name,
                        row_count=row_count or 0,
                        description=desc or "",
                        color=NODE_TYPES["TABLE"]["color"],
                        size=NODE_TYPES["TABLE"]["size"],
                        icon=NODE_TYPES["TABLE"]["icon"],
                        database=db_name, schema=schema,
                    )
                    self.graph.add_edge(schema_node_id, table_node_id,
                        type="HAS_TABLE", color="#8b949e", width=1,
                    )

                    # 4. 컬럼 노드
                    cur.execute("""
                        SELECT id, column_name, data_type, is_nullable,
                               is_primary_key, is_foreign_key, fk_references,
                               description, ordinal_position
                        FROM columns WHERE table_id = ?
                        ORDER BY ordinal_position
                    """, (table_id,))
                    columns = cur.fetchall()

                    for col in columns:
                        col_id, col_name, dtype, nullable, is_pk, is_fk, fk_ref, col_desc, pos = col
                        col_node_id = f"col_{col_id}"

                        # PK/FK에 따라 색상 변화
                        if is_pk:
                            color = "#f85149"  # 빨강
                            size = 18
                        elif is_fk:
                            color = "#d29922"  # 노랑
                            size = 16
                        else:
                            color = NODE_TYPES["COLUMN"]["color"]
                            size = NODE_TYPES["COLUMN"]["size"]

                        self.graph.add_node(col_node_id,
                            type="COLUMN", label=col_name,
                            data_type=dtype,
                            is_nullable=bool(nullable),
                            is_primary_key=bool(is_pk),
                            is_foreign_key=bool(is_fk),
                            fk_references=fk_ref or "",
                            description=col_desc or "",
                            ordinal_position=pos,
                            color=color, size=size,
                            icon="🔑" if is_pk else "🔗" if is_fk else "📌",
                            table=table_name, schema=schema, database=db_name,
                        )
                        self.graph.add_edge(table_node_id, col_node_id,
                            type="HAS_COLUMN", color="#8b949e", width=1,
                        )

            # 5. FK 관계 엣지
            cur.execute("""
                SELECT c1.id, c1.fk_references
                FROM columns c1
                JOIN tables t1 ON c1.table_id = t1.id
                WHERE t1.database_id = ? AND c1.is_foreign_key = 1
            """, (db_id,))
            fks = cur.fetchall()

            # FK 대상 컬럼 찾기
            for src_col_id, fk_ref in fks:
                parts = (fk_ref or "").split(".")
                if len(parts) >= 2:
                    ref_schema = parts[0] if len(parts) >= 3 else schema
                    ref_table = parts[1] if len(parts) >= 3 else parts[0]
                    ref_column = parts[-1]

                    cur.execute("""
                        SELECT c.id FROM columns c
                        JOIN tables t ON c.table_id = t.id
                        WHERE t.database_id = ? AND t.schema_name = ?
                          AND t.table_name = ? AND c.column_name = ?
                    """, (db_id, ref_schema, ref_table, ref_column))
                    target = cur.fetchone()
                    if target:
                        src_node = f"col_{src_col_id}"
                        tgt_node = f"col_{target[0]}"
                        self.graph.add_edge(src_node, tgt_node,
                            type="FK", color=EDGE_TYPES["FK"]["color"],
                            width=EDGE_TYPES["FK"]["width"],
                            label="FK", fk_references=fk_ref,
                        )

        # 6. 관계 탐지 결과 반영
        self._load_detected_relationships(conn)
        cur.close()

        print(f"  그래프 구축 완료: {self.graph.number_of_nodes()}노드, {self.graph.number_of_edges()}엣지")

    def _load_detected_relationships(self, conn):
        """탐지된 관계를 그래프에 추가"""
        cur = conn.cursor()
        cur.execute("""
            SELECT rc.source_column_id, rc.target_column_id,
                   rc.relation_type, rc.confidence, rc.notes
            FROM relationships rc
            WHERE rc.confidence >= 0.5
        """)
        for src_id, tgt_id, rel_type, confidence, notes in cur.fetchall():
            src_node = f"col_{src_id}"
            tgt_node = f"col_{tgt_id}"
            if src_node in self.graph and tgt_node in self.graph:
                edge_config = EDGE_TYPES.get(rel_type, EDGE_TYPES["SEMANTIC"])
                self.graph.add_edge(src_node, tgt_node,
                    type=rel_type,
                    confidence=confidence,
                    color=edge_config["color"],
                    width=edge_config["width"],
                    style=edge_config.get("style", "dashed"),
                    label=edge_config.get("label", rel_type[:4]),
                    notes=notes or "",
                )
        cur.close()

    def add_domain_nodes(self, domain_rules: dict = None):
        """도메인 노드 추가 및 컬럼 연결"""
        if not domain_rules:
            domain_rules = {
                "identity": ["user_id", "customer_id", "emp_id", "member_id", "account_id"],
                "person_name": ["user_name", "customer_name", "emp_name", "full_name", "first_name", "last_name"],
                "contact": ["email", "phone", "mobile", "fax", "telephone"],
                "address": ["address", "city", "state", "zip", "country", "postal_code", "street"],
                "datetime": ["created_at", "updated_at", "deleted_at", "timestamp", "date", "time"],
                "status": ["status", "state", "stage", "is_active", "is_deleted", "flag"],
                "monetary": ["amount", "price", "cost", "fee", "total", "salary", "budget", "balance"],
                "quantity": ["count", "quantity", "qty", "num", "number", "stock"],
                "description": ["description", "desc", "note", "comment", "memo", "detail"],
            }

        for domain_name, field_patterns in domain_rules.items():
            domain_node_id = f"domain_{domain_name}"
            self.graph.add_node(domain_node_id,
                type="DOMAIN", label=domain_name,
                color=NODE_TYPES["DOMAIN"]["color"],
                size=NODE_TYPES["DOMAIN"]["size"],
                icon=NODE_TYPES["DOMAIN"]["icon"],
            )

            for node, attrs in self.graph.nodes(data=True):
                if attrs.get("type") != "COLUMN":
                    continue
                col_name = attrs.get("label", "").lower()
                for pattern in field_patterns:
                    if pattern in col_name:
                        self.graph.add_edge(domain_node_id, node,
                            type="BELONGS_TO", color="#8b949e", width=1,
                        )
                        break

    def get_statistics(self) -> dict:
        """그래프 통계"""
        type_counts = defaultdict(int)
        for _, attrs in self.graph.nodes(data=True):
            type_counts[attrs.get("type", "UNKNOWN")] += 1

        rel_counts = defaultdict(int)
        for _, _, attrs in self.graph.edges(data=True):
            rel_counts[attrs.get("type", "UNKNOWN")] += 1

        connected = list(nx.weakly_connected_components(self.graph))

        return {
            "total_nodes": self.graph.number_of_nodes(),
            "total_edges": self.graph.number_of_edges(),
            "node_types": dict(type_counts),
            "edge_types": dict(rel_counts),
            "connected_components": len(connected),
            "largest_component": max(len(c) for c in connected) if connected else 0,
        }

    def find_path(self, source_table: str, target_table: str) -> list:
        """두 테이블 간 경로 탐색"""
        source_nodes = [n for n, a in self.graph.nodes(data=True)
                        if a.get("type") == "TABLE" and a.get("label") == source_table]
        target_nodes = [n for n, a in self.graph.nodes(data=True)
                        if a.get("type") == "TABLE" and a.get("label") == target_table]

        if not source_nodes or not target_nodes:
            return []

        try:
            path = nx.shortest_path(self.graph.to_undirected(),
                                     source_nodes[0], target_nodes[0])
            return [
                {
                    "node": n,
                    "type": self.graph.nodes[n].get("type", ""),
                    "label": self.graph.nodes[n].get("label", ""),
                }
                for n in path
            ]
        except nx.NetworkXNoPath:
            return []

    def to_cytoscape_json(self) -> dict:
        """Cytoscape.js 형식으로 변환"""
        elements = []

        # 노드
        for node, attrs in self.graph.nodes(data=True):
            elements.append({
                "data": {
                    "id": node,
                    "label": attrs.get("label", node),
                    "type": attrs.get("type", ""),
                    **{k: v for k, v in attrs.items()
                       if k not in ("label", "type", "color", "size", "icon")},
                },
                "style": {
                    "background-color": attrs.get("color", "#8b949e"),
                    "width": attrs.get("size", 15) * 2,
                    "height": attrs.get("size", 15) * 2,
                },
            })

        # 엣지
        for src, tgt, attrs in self.graph.edges(data=True):
            elements.append({
                "data": {
                    "id": f"edge_{src}_{tgt}",
                    "source": src,
                    "target": tgt,
                    "label": attrs.get("label", ""),
                    "type": attrs.get("type", ""),
                    "confidence": attrs.get("confidence", 1.0),
                },
                "style": {
                    "line-color": attrs.get("color", "#8b949e"),
                    "width": attrs.get("width", 1),
                    "line-style": "dashed" if attrs.get("style") == "dashed" else "solid",
                },
            })

        return {"elements": elements}

    def to_d3_json(self) -> dict:
        """D3.js Force Graph 형식으로 변환"""
        nodes = []
        node_index = {}

        for i, (node, attrs) in enumerate(self.graph.nodes(data=True)):
            node_index[node] = i
            nodes.append({
                "id": node,
                "name": attrs.get("label", node),
                "group": attrs.get("type", "UNKNOWN"),
                "size": attrs.get("size", 15),
                "color": attrs.get("color", "#8b949e"),
            })

        links = []
        for src, tgt, attrs in self.graph.edges(data=True):
            if src in node_index and tgt in node_index:
                links.append({
                    "source": src,
                    "target": tgt,
                    "type": attrs.get("type", ""),
                    "confidence": attrs.get("confidence", 1.0),
                    "value": attrs.get("width", 1),
                })

        return {"nodes": nodes, "links": links}

    def export_graphml(self, path: str):
        """GraphML로 내보내기"""
        nx.write_graphml(self.graph, path)

    def export_gexf(self, path: str):
        """GEXF로 내보내기 (Gephi 호환)"""
        nx.write_gexf(self.graph, path)

    def save_html_visualization(self, output_path: str):
        """D3.js 기반 HTML 시각화 저장"""
        d3_data = self.to_d3_json()
        stats = self.get_statistics()

        html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>DB 온톨로지 그래프</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
  body {{ margin: 0; background: #0d1117; color: #e6edf3; font-family: 'Noto Sans KR', sans-serif; }}
  #graph {{ width: 100vw; height: 100vh; }}
  .stats {{ position: fixed; top: 10px; left: 10px; background: #161b22; padding: 14px; border-radius: 8px; font-size: 13px; border: 1px solid #30363d; z-index: 10; }}
  .stats h3 {{ margin: 0 0 8px; color: #58a6ff; }}
  .legend {{ position: fixed; bottom: 10px; left: 10px; background: #161b22; padding: 12px; border-radius: 8px; font-size: 12px; border: 1px solid #30363d; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; margin: 4px 0; }}
  .legend-color {{ width: 12px; height: 12px; border-radius: 50%; }}
  .tooltip {{ position: fixed; background: #161b22; border: 1px solid #30363d; padding: 10px; border-radius: 6px; font-size: 12px; pointer-events: none; display: none; z-index: 100; max-width: 300px; }}
  .search-box {{ position: fixed; top: 10px; right: 10px; z-index: 10; }}
  .search-box input {{ background: #21262d; border: 1px solid #30363d; color: #e6edf3; padding: 8px 12px; border-radius: 6px; width: 240px; font-size: 13px; }}
  .search-box input:focus {{ outline: none; border-color: #58a6ff; }}
</style>
</head>
<body>
<div class="stats">
  <h3>📊 DB 온톨로지 통계</h3>
  <div>전체 노드: {stats['total_nodes']}</div>
  <div>전체 엣지: {stats['total_edges']}</div>
  <div>연결 컴포넌트: {stats['connected_components']}</div>
  <div>최대 컴포넌트: {stats['largest_component']}노드</div>
  <hr style="border-color: #30363d; margin: 8px 0;">
  <div>{'<br>'.join(f'{k}: {v}' for k, v in stats['node_types'].items())}</div>
</div>
<div class="search-box">
  <input type="text" id="search" placeholder="테이블/필드 검색..." oninput="searchNode(this.value)">
</div>
<div class="legend">
  <div class="legend-item"><div class="legend-color" style="background:#58a6ff"></div> 데이터베이스</div>
  <div class="legend-item"><div class="legend-color" style="background:#3fb950"></div> 테이블</div>
  <div class="legend-item"><div class="legend-color" style="background:#d29922"></div> 컬럼</div>
  <div class="legend-item"><div class="legend-color" style="background:#f85149"></div> PK 컬럼</div>
  <div class="legend-item"><div class="legend-color" style="background:#a371f7"></div> 관계 엣지</div>
</div>
<div id="graph"></div>
<div class="tooltip" id="tooltip"></div>
<script>
const data = {json.dumps(d3_data, ensure_ascii=False)};
const width = window.innerWidth;
const height = window.innerHeight;

const svg = d3.select("#graph").append("svg")
    .attr("width", width).attr("height", height);

const g = svg.append("g");

// 줌
svg.call(d3.zoom().on("zoom", (event) => g.attr("transform", event.transform)));

const simulation = d3.forceSimulation(data.nodes)
    .force("link", d3.forceLink(data.links).id(d => d.id).distance(80))
    .force("charge", d3.forceManyBody().strength(-300))
    .force("center", d3.forceCenter(width / 2, height / 2))
    .force("collision", d3.forceCollide().radius(d => d.size + 5));

// 엣지
const link = g.append("g")
    .selectAll("line").data(data.links).join("line")
    .attr("stroke", d => d.type === 'FK' ? '#f85149' : d.type === 'EXACT_MATCH' ? '#3fb950' : '#8b949e')
    .attr("stroke-width", d => d.value)
    .attr("stroke-dasharray", d => d.type === 'FK' ? '' : '4,4')
    .attr("opacity", 0.6);

// 노드
const node = g.append("g")
    .selectAll("g").data(data.nodes).join("g")
    .call(d3.drag()
        .on("start", dragstarted)
        .on("drag", dragged)
        .on("end", dragended));

node.append("circle")
    .attr("r", d => d.size)
    .attr("fill", d => d.color)
    .attr("stroke", "#0d1117")
    .attr("stroke-width", 2);

node.append("text")
    .text(d => d.name)
    .attr("x", d => d.size + 4)
    .attr("y", 4)
    .attr("fill", "#e6edf3")
    .attr("font-size", d => d.group === 'DATABASE' ? 14 : d.group === 'TABLE' ? 12 : 10)
    .attr("font-weight", d => d.group === 'DATABASE' || d.group === 'TABLE' ? '600' : '400');

// 호버
const tooltip = d3.select("#tooltip");
node.on("mouseover", (event, d) => {{
    tooltip.style("display", "block")
        .style("left", (event.pageX + 10) + "px")
        .style("top", (event.pageY - 10) + "px")
        .html(`<strong>${{d.name}}</strong><br>타입: ${{d.group}}<br>크기: ${{d.size}}`);
}}).on("mouseout", () => tooltip.style("display", "none"));

// 검색
function searchNode(value) {{
    if (!value) {{
        node.selectAll("circle").attr("opacity", 1);
        link.attr("opacity", 0.6);
        return;
    }}
    const v = value.toLowerCase();
    node.selectAll("circle").attr("opacity", d => d.name.toLowerCase().includes(v) ? 1 : 0.2);
    link.attr("opacity", 0.1);
}}

simulation.on("tick", () => {{
    link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
    node.attr("transform", d => `translate(${{d.x}},${{d.y}})`);
}});

function dragstarted(event, d) {{
    if (!event.active) simulation.alphaTarget(0.3).restart();
    d.fx = d.x; d.fy = d.y;
}}
function dragged(event, d) {{
    d.fx = event.x; d.fy = event.y;
}}
function dragended(event, d) {{
    if (!event.active) simulation.alphaTarget(0);
    d.fx = null; d.fy = null;
}}
</script>
</body>
</html>"""

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  HTML 시각화 저장: {output_path}")


# ── 테스트 ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from collector.db_adapter import SQLiteAdapter, MetadataStore, SchemaCollector
    from analyzer.relationship_analyzer import RelationshipOrchestrator

    test_db_path = "/tmp/test_graph.db"
    meta_path = "/tmp/test_graph_meta.db"
    for p in [test_db_path, meta_path]:
        if os.path.exists(p): os.remove(p)

    conn = sqlite3.connect(test_db_path)
    conn.executescript("""
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            created_at TIMESTAMP,
            status TEXT
        );
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            order_date TIMESTAMP,
            amount REAL,
            status TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY,
            product_name TEXT,
            price REAL,
            category TEXT
        );
        CREATE TABLE order_items (
            item_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER,
            price REAL,
            FOREIGN KEY (order_id) REFERENCES orders(order_id),
            FOREIGN KEY (product_id) REFERENCES products(product_id)
        );
        CREATE TABLE payments (
            payment_id INTEGER PRIMARY KEY,
            order_id INTEGER NOT NULL,
            amount REAL,
            payment_date TIMESTAMP,
            status TEXT,
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        );
    """)
    conn.close()

    # 수집 → 분석 → 그래프
    store = MetadataStore(db_path=meta_path)
    collector = SchemaCollector(store)
    collector.add_database("sqlite", file_path=test_db_path, db_name="shop")
    results = collector.collect_all()

    orchestrator = RelationshipOrchestrator(store)
    rels = orchestrator.analyze_all()

    # 온톨로지 그래프
    print("\n=== 온톨로지 그래프 구축 ===")
    graph = OntologyGraph()
    graph.build_from_store(store)
    graph.add_domain_nodes()

    stats = graph.get_statistics()
    print(f"\n=== 그래프 통계 ===")
    print(json.dumps(stats, indent=2, ensure_ascii=False))

    # 경로 탐색
    print("\n=== 경로 탐색 ===")
    path = graph.find_path("users", "order_items")
    if path:
        print("  users → order_items 경계:")
        for p in path:
            print(f"    [{p['type']}] {p['label']}")

    # 시각화
    output_dir = "/Users/bearj/coding_projects/db-ontology/output"
    os.makedirs(output_dir, exist_ok=True)

    graph.save_html_visualization(f"{output_dir}/ontology_graph.html")
    graph.export_graphml(f"{output_dir}/ontology_graph.graphml")

    # D3 데이터
    d3_data = graph.to_d3_json()
    with open(f"{output_dir}/graph_data.json", "w") as f:
        json.dump(d3_data, f, ensure_ascii=False, indent=2)

    store.close()
    for p in [test_db_path, meta_path]:
        if os.path.exists(p): os.remove(p)

    print("\n✅ 온톨로지 그래프 테스트 완료")
    print(f"  출력 파일: {output_dir}/")
