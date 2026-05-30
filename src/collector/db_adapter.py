"""
DB Ontology - Multi-DB Connection Adapter
다중 데이터베이스 접속 및 스키마 자동 수집 모듈
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import sqlite3
import json
import os
import re
from datetime import datetime


# ── 데이터 모델 ─────────────────────────────────────────

@dataclass
class ColumnInfo:
    name: str
    data_type: str
    is_nullable: bool = True
    default_value: Optional[str] = None
    max_length: Optional[int] = None
    numeric_precision: Optional[int] = None
    ordinal_position: int = 0
    description: Optional[str] = None
    is_primary_key: bool = False
    is_foreign_key: bool = False
    fk_references: Optional[str] = None  # "schema.table.column"
    sample_values: list = field(default_factory=list)

@dataclass
class TableInfo:
    schema_name: str
    table_name: str
    table_type: str = "TABLE"
    row_count: Optional[int] = None
    description: Optional[str] = None
    columns: list = field(default_factory=list)  # list[ColumnInfo]

@dataclass
class DatabaseInfo:
    name: str
    db_type: str  # postgresql, mysql, oracle, sqlite, mongodb
    host: Optional[str] = None
    port: Optional[int] = None
    database_name: Optional[str] = None
    description: Optional[str] = None
    tables: list = field(default_factory=list)  # list[TableInfo]


# ── DB 어댑터 인터페이스 ────────────────────────────────

class DBAdapter(ABC):
    """데이터베이스 접속 어댑터 기본 클래스"""

    def __init__(self, connection_string: str, db_name: str = ""):
        self.connection_string = connection_string
        self.db_name = db_name
        self.conn = None

    @abstractmethod
    def connect(self):
        ...

    @abstractmethod
    def disconnect(self):
        ...

    @abstractmethod
    def get_tables(self, table_filter: list[str] = None) -> list[TableInfo]:
        ...

    @abstractmethod
    def get_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        ...

    @abstractmethod
    def get_foreign_keys(self, schema: str, table: str) -> list[dict]:
        ...

    @abstractmethod
    def get_sample_data(self, schema: str, table: str, column: str, limit: int = 100) -> list:
        ...

    def collect_all(self, table_filter: list[str] = None) -> DatabaseInfo:
        """전체 스키마 수집 (table_filter: ['schema.table', ...] 형태)"""
        db_info = DatabaseInfo(
            name=self.db_name,
            db_type=self.__class__.__name__.replace("Adapter", "").lower(),
        )
        tables = self.get_tables(table_filter)
        for table in tables:
            table.columns = self.get_columns(table.schema_name, table.table_name)
            fks = self.get_foreign_keys(table.schema_name, table.table_name)
            # FK 정보를 컬럼에 반영
            for fk in fks:
                for col in table.columns:
                    if col.name == fk.get("column"):
                        col.is_foreign_key = True
                        col.fk_references = f"{fk.get('ref_schema', '')}.{fk.get('ref_table')}.{fk.get('ref_column')}"
            db_info.tables.append(table)
        return db_info


# ── PostgreSQL 어댑터 ───────────────────────────────────

class PostgreSQLAdapter(DBAdapter):
    """PostgreSQL 접속 어댑터"""

    def __init__(self, host: str, port: int, database: str, user: str, password: str, db_name: str = ""):
        super().__init__(f"postgresql://{user}:{password}@{host}:{port}/{database}", db_name or database)
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password

    def connect(self):
        import psycopg2
        self.conn = psycopg2.connect(
            host=self.host, port=self.port,
            database=self.database, user=self.user, password=self.password
        )

    def disconnect(self):
        if self.conn:
            self.conn.close()

    def get_tables(self, table_filter: list[str] = None) -> list[TableInfo]:
        cur = self.conn.cursor()
        if table_filter:
            tables = []
            for ft in table_filter:
                parts = ft.split(".", 1)
                if len(parts) == 2:
                    schema, table = parts
                else:
                    schema, table = "public", parts[0]
                cur.execute("""
                    SELECT t.table_schema, t.table_name, t.table_type,
                           pg_catalog.obj_description(
                             (quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass::oid,
                             'pg_class'
                           ) AS table_comment
                    FROM information_schema.tables t
                    WHERE t.table_schema = %s AND t.table_name = %s
                """, (schema, table))
                row = cur.fetchone()
                if row:
                    tables.append(TableInfo(
                        schema_name=row[0], table_name=row[1],
                        table_type=row[2], description=row[3]
                    ))
            cur.close()
            return tables
        cur.execute("""
            SELECT t.table_schema, t.table_name, t.table_type,
                   pg_catalog.obj_description(
                     (quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass::oid,
                     'pg_class'
                   ) AS table_comment
            FROM information_schema.tables t
            WHERE t.table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY t.table_schema, t.table_name
        """)
        tables = []
        for row in cur.fetchall():
            tables.append(TableInfo(
                schema_name=row[0], table_name=row[1],
                table_type=row[2], description=row[3]
            ))
        cur.close()
        return tables

    def get_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT c.column_name, c.data_type, c.is_nullable, c.column_default,
                   c.character_maximum_length, c.numeric_precision, c.ordinal_position,
                   pgd.description,
                   CASE WHEN pk.column_name IS NOT NULL THEN true ELSE false END as is_pk
            FROM information_schema.columns c
            LEFT JOIN pg_catalog.pg_statio_all_tables st
                ON c.table_schema = st.schemaname AND c.table_name = st.relname
            LEFT JOIN pg_catalog.pg_description pgd
                ON pgd.objoid = st.relid AND pgd.objsubid = c.ordinal_position
            LEFT JOIN (
                SELECT kcu.column_name, kcu.table_schema, kcu.table_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                WHERE tc.constraint_type = 'PRIMARY KEY'
            ) pk ON pk.column_name = c.column_name
                AND pk.table_schema = c.table_schema AND pk.table_name = c.table_name
            WHERE c.table_schema = %s AND c.table_name = %s
            ORDER BY c.ordinal_position
        """, (schema, table))
        columns = []
        for row in cur.fetchall():
            columns.append(ColumnInfo(
                name=row[0], data_type=row[1],
                is_nullable=row[2] == "YES",
                default_value=row[3],
                max_length=row[4],
                numeric_precision=row[5],
                ordinal_position=row[6],
                description=row[7],
                is_primary_key=row[8],
            ))
        cur.close()
        return columns

    def get_foreign_keys(self, schema: str, table: str) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT
                kcu.column_name,
                ccu.table_schema AS ref_schema,
                ccu.table_name AS ref_table,
                ccu.column_name AS ref_column
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
                ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage AS ccu
                ON ccu.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = %s AND tc.table_name = %s
        """, (schema, table))
        fks = []
        for row in cur.fetchall():
            fks.append({
                "column": row[0], "ref_schema": row[1],
                "ref_table": row[2], "ref_column": row[3]
            })
        cur.close()
        return fks

    def get_sample_data(self, schema: str, table: str, column: str, limit: int = 100) -> list:
        cur = self.conn.cursor()
        cur.execute(f'SELECT DISTINCT "{column}" FROM "{schema}"."{table}" WHERE "{column}" IS NOT NULL LIMIT %s', (limit,))
        return [row[0] for row in cur.fetchall()]

    def _get_table_comment(self, schema: str, table: str) -> Optional[str]:
        try:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT pg_catalog.obj_description(
                    (quote_ident(%s) || '.' || quote_ident(%s))::regclass, 'pg_class'
                )
            """, (schema, table))
            row = cur.fetchone()
            cur.close()
            return row[0] if row else None
        except:
            return None


# ── MySQL 어댑터 ────────────────────────────────────────

class MySQLAdapter(DBAdapter):
    """MySQL/MariaDB 접속 어댑터"""

    def __init__(self, host: str, port: int, database: str, user: str, password: str, db_name: str = ""):
        super().__init__(f"mysql://{user}:{password}@{host}:{port}/{database}", db_name or database)
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password

    def connect(self):
        import mysql.connector
        self.conn = mysql.connector.connect(
            host=self.host, port=self.port,
            database=self.database, user=self.user, password=self.password
        )

    def disconnect(self):
        if self.conn:
            self.conn.close()

    def get_tables(self, table_filter: list[str] = None) -> list[TableInfo]:
        cur = self.conn.cursor()
        if table_filter:
            tables = []
            for ft in table_filter:
                parts = ft.split(".", 1)
                table = parts[1] if len(parts) == 2 else parts[0]
                cur.execute("""
                    SELECT table_schema, table_name, table_type
                    FROM information_schema.tables
                    WHERE table_schema = %s AND table_name = %s
                """, (self.database, table))
                row = cur.fetchone()
                if row:
                    tables.append(TableInfo(schema_name=row[0], table_name=row[1], table_type=row[2]))
            cur.close()
            return tables
        cur.execute("""
            SELECT table_schema, table_name, table_type
            FROM information_schema.tables
            WHERE table_schema = %s
            ORDER BY table_name
        """, (self.database,))
        tables = []
        for row in cur.fetchall():
            tables.append(TableInfo(schema_name=row[0], table_name=row[1], table_type=row[2]))
        cur.close()
        return tables

    def get_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT column_name, data_type, is_nullable, column_default,
                   character_maximum_length, numeric_precision, ordinal_position,
                   column_comment,
                   CASE WHEN column_key = 'PRI' THEN true ELSE false END
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
        """, (schema, table))
        columns = []
        for row in cur.fetchall():
            columns.append(ColumnInfo(
                name=row[0], data_type=row[1],
                is_nullable=row[2] == "YES",
                default_value=row[3], max_length=row[4],
                numeric_precision=row[5], ordinal_position=row[6],
                description=row[7], is_primary_key=row[8],
            ))
        cur.close()
        return columns

    def get_foreign_keys(self, schema: str, table: str) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT kcu.column_name,
                   kcu.referenced_table_schema,
                   kcu.referenced_table_name,
                   kcu.referenced_column_name
            FROM information_schema.key_column_usage kcu
            JOIN information_schema.table_constraints tc
                ON kcu.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND kcu.table_schema = %s AND kcu.table_name = %s
        """, (schema, table))
        fks = []
        for row in cur.fetchall():
            fks.append({"column": row[0], "ref_schema": row[1], "ref_table": row[2], "ref_column": row[3]})
        cur.close()
        return fks

    def get_sample_data(self, schema: str, table: str, column: str, limit: int = 100) -> list:
        cur = self.conn.cursor()
        cur.execute(f"SELECT DISTINCT `{column}` FROM `{schema}`.`{table}` WHERE `{column}` IS NOT NULL LIMIT %s", (limit,))
        return [row[0] for row in cur.fetchall()]


# ── SQLite 어댑터 ───────────────────────────────────────

class SQLiteAdapter(DBAdapter):
    """SQLite 접속 어댑터 (파일 기반)"""

    def __init__(self, file_path: str, db_name: str = ""):
        super().__init__(f"sqlite://{file_path}", db_name or file_path.split("/")[-1])
        self.file_path = file_path

    def connect(self):
        self.conn = sqlite3.connect(self.file_path)
        self.conn.row_factory = sqlite3.Row

    def disconnect(self):
        if self.conn:
            self.conn.close()

    def get_tables(self, table_filter: list[str] = None) -> list[TableInfo]:
        cur = self.conn.cursor()
        if table_filter:
            tables = []
            for ft in table_filter:
                parts = ft.split(".", 1)
                table = parts[1] if len(parts) == 2 else parts[0]
                cur.execute("""
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name = ?
                """, (table,))
                row = cur.fetchone()
                if row:
                    tables.append(TableInfo(schema_name="main", table_name=row[0]))
            cur.close()
            return tables
        cur.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
        """)
        tables = []
        for row in cur.fetchall():
            tables.append(TableInfo(schema_name="main", table_name=row[0]))
        cur.close()
        return tables

    def get_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        cur = self.conn.cursor()
        cur.execute(f'PRAGMA table_info("{table}")')
        columns = []
        for row in cur.fetchall():
            # cid, name, type, notnull, dflt_value, pk
            columns.append(ColumnInfo(
                name=row[1], data_type=row[2] or "TEXT",
                is_nullable=not row[3],
                default_value=row[4],
                ordinal_position=row[0],
                is_primary_key=bool(row[5]),
            ))
        cur.close()
        return columns

    def get_foreign_keys(self, schema: str, table: str) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(f'PRAGMA foreign_key_list("{table}")')
        fks = []
        for row in cur.fetchall():
            # id, seq, table, from, to, on_update, on_delete, match
            fks.append({
                "column": row[3], "ref_schema": "main",
                "ref_table": row[2], "ref_column": row[4]
            })
        cur.close()
        return fks

    def get_sample_data(self, schema: str, table: str, column: str, limit: int = 100) -> list:
        cur = self.conn.cursor()
        cur.execute(f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT ?', (limit,))
        return [row[0] for row in cur.fetchall()]


# ── 어댑터 팩토리 ───────────────────────────────────────

class AdapterFactory:
    """DB 타입에 따른 어댑터 생성"""

    _adapters = {
        "postgresql": PostgreSQLAdapter,
        "mysql": MySQLAdapter,
        "sqlite": SQLiteAdapter,
    }

    @classmethod
    def create(cls, db_type: str, **kwargs) -> DBAdapter:
        adapter_class = cls._adapters.get(db_type.lower())
        if not adapter_class:
            raise ValueError(f"지원하지 않는 DB 타입: {db_type}")
        return adapter_class(**kwargs)

    @classmethod
    def register(cls, db_type: str, adapter_class: type):
        cls._adapters[db_type.lower()] = adapter_class


# ── 메타데이터 저장소 (SQLite) ──────────────────────────

class MetadataStore:
    """수집된 메타데이터를 SQLite에 저장"""

    def __init__(self, db_path: str = "~/.hermes/data/ontology_metadata.db"):
        self.db_path = os.path.expanduser(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS databases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                db_type TEXT NOT NULL,
                host TEXT, port INTEGER, database_name TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tables (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                database_id INTEGER REFERENCES databases(id),
                schema_name TEXT,
                table_name TEXT NOT NULL,
                table_type TEXT DEFAULT 'TABLE',
                row_count INTEGER,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP,
                UNIQUE(database_id, schema_name, table_name)
            );
            CREATE TABLE IF NOT EXISTS columns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_id INTEGER REFERENCES tables(id),
                column_name TEXT NOT NULL,
                data_type TEXT NOT NULL,
                is_nullable BOOLEAN DEFAULT 1,
                default_value TEXT,
                max_length INTEGER,
                numeric_precision INTEGER,
                ordinal_position INTEGER DEFAULT 0,
                description TEXT,
                is_primary_key BOOLEAN DEFAULT 0,
                is_foreign_key BOOLEAN DEFAULT 0,
                fk_references TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP,
                UNIQUE(table_id, column_name)
            );
            CREATE TABLE IF NOT EXISTS relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_column_id INTEGER REFERENCES columns(id),
                target_column_id INTEGER REFERENCES columns(id),
                relation_type TEXT NOT NULL,
                confidence REAL NOT NULL,
                detected_by TEXT NOT NULL,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verified BOOLEAN DEFAULT 0,
                notes TEXT,
                UNIQUE(source_column_id, target_column_id, relation_type)
            );
            CREATE TABLE IF NOT EXISTS collection_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                database_id INTEGER REFERENCES databases(id),
                method TEXT, status TEXT,
                tables_found INTEGER, columns_found INTEGER,
                relationships_found INTEGER,
                error_message TEXT,
                collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                duration_seconds REAL
            );
            CREATE TABLE IF NOT EXISTS table_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                tables TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.conn.commit()

    def save_database(self, db_info: DatabaseInfo) -> int:
        """DB 정보 저장 후 database_id 반환"""
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO databases (name, db_type, host, port, database_name, description)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (db_info.name, db_info.db_type, db_info.host, db_info.port,
              db_info.database_name, db_info.description))
        db_id = cur.lastrowid

        for table in db_info.tables:
            cur.execute("""
                INSERT INTO tables (database_id, schema_name, table_name, table_type, row_count, description)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (db_id, table.schema_name, table.table_name,
                  table.table_type, table.row_count, table.description))
            table_id = cur.lastrowid

            for col in table.columns:
                cur.execute("""
                    INSERT INTO columns (table_id, column_name, data_type, is_nullable,
                        default_value, max_length, numeric_precision, ordinal_position,
                        description, is_primary_key, is_foreign_key, fk_references)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (table_id, col.name, col.data_type, col.is_nullable,
                      col.default_value, col.max_length, col.numeric_precision,
                      col.ordinal_position, col.description, col.is_primary_key,
                      col.is_foreign_key, col.fk_references))

        self.conn.commit()
        return db_id

    def get_all_columns(self) -> list[dict]:
        """전체 컬럼 목록 (관계 분석용)"""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT c.id, c.column_name, c.data_type, c.description,
                   t.schema_name, t.table_name, d.name as db_name
            FROM columns c
            JOIN tables t ON c.table_id = t.id
            JOIN databases d ON t.database_id = d.id
        """)
        columns = []
        for row in cur.fetchall():
            columns.append({
                "id": row[0], "column_name": row[1], "data_type": row[2],
                "description": row[3], "schema": row[4], "table": row[5], "database": row[6]
            })
        return columns

    def save_relationship(self, source_id: int, target_id: int,
                          relation_type: str, confidence: float, detected_by: str):
        cur = self.conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO relationships
                (source_column_id, target_column_id, relation_type, confidence, detected_by)
            VALUES (?, ?, ?, ?, ?)
        """, (source_id, target_id, relation_type, confidence, detected_by))
        self.conn.commit()

    # ── 테이블 프리셋 관리 ─────────────────────────────

    def save_preset(self, name: str, tables: list[str]) -> int:
        cur = self.conn.cursor()
        cur.execute("INSERT INTO table_presets (name, tables) VALUES (?, ?)",
                     (name, json.dumps(tables)))
        self.conn.commit()
        return cur.lastrowid

    def list_presets(self) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT id, name, created_at FROM table_presets ORDER BY created_at DESC")
        return [{"id": r[0], "name": r[1], "created_at": r[2]} for r in cur.fetchall()]

    def load_preset(self, preset_id: int) -> list[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT tables FROM table_presets WHERE id = ?", (preset_id,))
        row = cur.fetchone()
        if not row:
            return []
        return json.loads(row[0])

    def delete_preset(self, preset_id: int):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM table_presets WHERE id = ?", (preset_id,))
        self.conn.commit()

    def close(self):
        self.conn.close()


# ── 스키마 수집 오케스트레이터 ──────────────────────────

class SchemaCollector:
    """여러 DB에서 스키마를 수집하는 오케스트레이터"""

    def __init__(self, store: MetadataStore):
        self.store = store
        self.adapters: list[DBAdapter] = []

    def add_adapter(self, adapter: DBAdapter):
        self.adapters.append(adapter)

    def add_database(self, db_type: str, **kwargs):
        adapter = AdapterFactory.create(db_type, **kwargs)
        self.adapters.append(adapter)

    def collect_all(self, table_filter: list[str] = None) -> list[DatabaseInfo]:
        """table_filter: ['schema.table', ...] — 지정된 테이블만 수집"""
        results = []
        for adapter in self.adapters:
            try:
                print(f"  📡 수집 중: {adapter.db_name} ({adapter.__class__.__name__})")
                adapter.connect()
                db_info = adapter.collect_all(table_filter)
                db_id = self.store.save_database(db_info)
                print(f"  ✅ 완료: {len(db_info.tables)}개 테이블, "
                      f"{sum(len(t.columns) for t in db_info.tables)}개 컬럼")
                results.append(db_info)
            except Exception as e:
                print(f"  ❌ 실패: {adapter.db_name} - {e}")
            finally:
                adapter.disconnect()
        return results


# ── 사용 예시 ──────────────────────────────────────────

if __name__ == "__main__":
    import os

    # 메타데이터 저장소
    store = MetadataStore()

    # 수집기
    collector = SchemaCollector(store)

    # SQLite 예시 (로컬 파일)
    collector.add_database(
        "sqlite",
        file_path=os.path.expanduser("~/.hermes/data/ontology_metadata.db"),
        db_name="ontology_metadata"
    )

    # 수집 실행
    results = collector.collect_all()

    # 수집 결과 요약
    print("\n=== 수집 결과 ===")
    for db in results:
        print(f"\n📦 {db.name} ({db.db_type})")
        for table in table_info in db.tables:
            pk_cols = [c.name for c in table.columns if c.is_primary_key]
            fk_cols = [c.name for c in table.columns if c.is_foreign_key]
            print(f"  📋 {table.schema_name}.{table.table_name} "
                  f"({len(table.columns)} cols)"
                  f"{' PK:' + ','.join(pk_cols) if pk_cols else ''}"
                  f"{' FK:' + ','.join(fk_cols) if fk_cols else ''}")

    store.close()
