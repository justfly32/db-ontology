"""
Incremental Update & Monitoring Module
스키마 변경 감지, 관계 이력 관리, 알림
"""

import os
import sqlite3
import hashlib
import json
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict


class SchemaDriftDetector:
    """스키마 변경 감지기"""

    def __init__(self, store_path: str = "~/.hermes/data/ontology_metadata.db"):
        self.store_path = os.path.expanduser(store_path)
        self.snapshot_path = self.store_path.replace(".db", "_snapshot.json")

    def take_snapshot(self) -> dict:
        """현재 스키마 스냅샷 저장"""
        conn = sqlite3.connect(self.store_path)
        cur = conn.cursor()

        # 테이블 + 컬럼 해시
        cur.execute("""
            SELECT d.name, t.schema_name, t.table_name, c.column_name, c.data_type
            FROM columns c
            JOIN tables t ON c.table_id = t.id
            JOIN databases d ON t.database_id = d.id
            ORDER BY d.name, t.schema_name, t.table_name, c.ordinal_position
        """)

        schema_hash = hashlib.md5()
        changes = {"tables": {}, "columns": {}}

        for row in cur.fetchall():
            db, schema, table, col, dtype = row
            key = f"{db}.{schema}.{table}"
            schema_hash.update(f"{key}.{col}:{dtype}".encode())
            if key not in changes["tables"]:
                changes["tables"][key] = []
            changes["tables"][key].append(f"{col}:{dtype}")

        snapshot = {
            "hash": schema_hash.hexdigest(),
            "timestamp": datetime.now().isoformat(),
            "tables": changes["tables"],
        }

        with open(self.snapshot_path, "w") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)

        conn.close()
        return snapshot

    def detect_changes(self) -> dict:
        """이전 스냅샷과 비교하여 변경 감지"""
        if not os.path.exists(self.snapshot_path):
            return {"status": "no_snapshot", "changes": []}

        with open(self.snapshot_path, "r") as f:
            old = json.load(f)

        new_snapshot = self.take_snapshot()

        old_tables = set(old.get("tables", {}).keys())
        new_tables = set(new_snapshot.get("tables", {}).keys())

        changes = []

        # 새 테이블
        for t in new_tables - old_tables:
            changes.append({"type": "TABLE_ADDED", "target": t, "severity": "INFO"})

        # 삭제된 테이블
        for t in old_tables - new_tables:
            changes.append({"type": "TABLE_REMOVED", "target": t, "severity": "WARNING"})

        # 컬럼 변경
        for t in old_tables & new_tables:
            old_cols = set(old["tables"][t])
            new_cols = set(new_snapshot["tables"][t])

            for c in new_cols - old_cols:
                changes.append({"type": "COLUMN_ADDED", "target": f"{t}.{c}", "severity": "INFO"})
            for c in old_cols - new_cols:
                changes.append({"type": "COLUMN_REMOVED", "target": f"{t}.{c}", "severity": "WARNING"})

        status = "changed" if changes else "unchanged"
        return {
            "status": status,
            "old_hash": old.get("hash"),
            "new_hash": new_snapshot.get("hash"),
            "changes": changes,
            "checked_at": datetime.now().isoformat(),
        }


class ChangeHistoryManager:
    """관계 변경 이력 관리"""

    def __init__(self, store_path: str = "~/.hermes/data/ontology_metadata.db"):
        self.store_path = os.path.expanduser(store_path)
        self.conn = sqlite3.connect(self.store_path)
        self._create_history_table()

    def _create_history_table(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS change_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id INTEGER,
                target_name TEXT,
                old_value TEXT,
                new_value TEXT,
                confidence REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.conn.commit()

    def log_event(self, event_type: str, target_type: str, target_id: int,
                  target_name: str, old_value: str = None,
                  new_value: str = None, confidence: float = None):
        self.conn.execute("""
            INSERT INTO change_history
                (event_type, target_type, target_id, target_name, old_value, new_value, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (event_type, target_type, target_id, target_name, old_value, new_value, confidence))
        self.conn.commit()

    def get_history(self, days: int = 7, limit: int = 100) -> list:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT * FROM change_history
            WHERE created_at >= datetime('now', ?)
            ORDER BY created_at DESC
            LIMIT ?
        """, (f"-{days} days", limit))
        return [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]

    def get_summary(self, days: int = 7) -> dict:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT event_type, COUNT(*) as cnt
            FROM change_history
            WHERE created_at >= datetime('now', ?)
            GROUP BY event_type
            ORDER BY cnt DESC
        """, (f"-{days} days",))
        return {row[0]: row[1] for row in cur.fetchall()}


# ── 테스트 ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    meta_path = "/tmp/test_drift.db"
    if os.path.exists(meta_path): os.remove(meta_path)

    store = MetadataStore(db_path=meta_path)
    store._create_tables()

    # 스냅샷 테스트
    detector = SchemaDriftDetector(meta_path)
    snap = detector.take_snapshot()
    print(f"  스냅샷: {snap['hash'][:12]}... ({len(snap['tables'])} tables)")

    # 변경 감지 (동일 상태)
    result = detector.detect_changes()
    print(f"  변경 감지: {result['status']} ({len(result['changes'])} changes)")

    # 이력 테스트
    history = ChangeHistoryManager(meta_path)
    history.log_event("RELATIONSHIP_ADDED", "column", 1, "users.user_id → orders.user_id",
                       confidence=0.95)
    history.log_event("SCHEMA_CHANGED", "table", 1, "orders",
                       old_value="5 columns", new_value="6 columns")

    h = history.get_history(days=1)
    print(f"  이력: {len(h)} events")
    for event in h:
        print(f"    [{event['event_type']}] {event['target_name']}")

    store.close()
    for p in [meta_path, meta_path.replace(".db", "_snapshot.json")]:
        if os.path.exists(p): os.remove(p)

    print("\n✅ 증분 업데이트 테스트 완료")
