# Phase 1-3: 대상 DB 목록 및 접근 방식 정의

## 대상 DB 유형

### 1. RDBMS (관계형 데이터베이스)
| DBMS | 비율 | 접근 방식 | 스키마 수집 방법 |
|------|------|----------|----------------|
| PostgreSQL | 35% | psycopg2/asyncpg | `information_schema.columns`, `pg_catalog.pg_constraint` |
| MySQL/MariaDB | 25% | mysql-connector/PyMySQL | `information_schema.columns`, `information_schema.key_column_usage` |
| Oracle | 20% | cx_Oracle | `all_tab_columns`, `all_constraints` |
| SQL Server | 10% | pyodbc | `INFORMATION_SCHEMA.COLUMNS`, `sys.foreign_keys` |
| SQLite | 5% | sqlite3 (built-in) | `PRAGMA table_info()`, `sqlite_master` |

### 2. NoSQL
| DBMS | 비율 | 접근 방식 | 스키마 수집 방법 |
|------|------|----------|----------------|
| MongoDB | 3% | pymongo | `listCollections()`, `db.coll.stats()` |
| Redis | 1% | redis-py | `INFO KEYSPACE`, `SCAN` |
| Elasticsearch | 1% | elasticsearch-py | `_mapping API`, `_cat/indices` |

### 3. 기타 데이터 소스
| 유형 | 접근 방식 | 스키마 수집 |
|------|----------|------------|
| REST API | requests/httpx | OpenAPI/Swagger 스펙, 응답 JSON 분석 |
| GraphQL | gql | Introspection Query |
| CSV/Parquet 파일 | pandas/pyarrow | 파일 헤더/스키마 추출 |
| Kafka Topic | confluent-kafka | Schema Registry 조회 |

## 접근 방식 아키텍처

### 방법 A: 직접 접속 (Direct Connection)
```
프로그램 → DB 드라이버 → INFORMATION_SCHEMA → 스키마 추출
```
- **장점**: 정확한 FK 관계, 제약조건, 인덱스 정보 수집 가능
- **단점**: DB 접속 정보 필요, 보안 이슈
- **용도**: 내부 DB, 개발/스테이징 환경

### 방법 B: 덤프 파일 분석 (Dump Analysis)
```
프로그램 → SQL dump 파일 → DDL 파싱 → 스키마 추출
```
- **장품**: 접속 정보 불필요, 오프라인 분석 가능
- **단점**: 실시간 반영 안됨, FK 관계가 dump에 없을 수 있음
- **용도**: 운영 DB 접근 불가 시, 버전 관리된 스키마

### 방법 C: 쿼리 로그 분석 (Query Log Analysis)
```
프로그램 → 쿼리 로그/Slow Query Log → JOIN 패턴 파싱 → 관계 추출
```
- **장점**: 실제 사용 관계 파악, 접속 권한 적게 필요
- **단점**: 로그 포맷 의존, 미사용 관계는 누락
- **용도**: 운영 환경에서 실제 관계 분석

### 방법 D: API 메타데이터 수집 (API Metadata)
```
프로그램 → REST/GraphQL API → 응답 스키마 분석 → 관계 추출
```
- **장점**: 현대적 마이크로서비스 아키텍처에 적합
- **단점**: 내부 DB 구조와 다를 수 있음
- **용도**: API Gateway 뒤의 서비스들

## 메타데이터 저장 스키마 (내부 SQLite)

```sql
-- 데이터베이스 목록
CREATE TABLE databases (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,          -- DB 이름
    db_type     TEXT NOT NULL,          -- postgresql, mysql, oracle, ...
    host        TEXT,
    port        INTEGER,
    database_name TEXT,                 -- 실제 DB명
    description TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP
);

-- 테이블 목록
CREATE TABLE tables (
    id              INTEGER PRIMARY KEY,
    database_id     INTEGER REFERENCES databases(id),
    schema_name     TEXT,               -- schema (public, dbo, ...)
    table_name      TEXT NOT NULL,
    table_type      TABLE, VIEW, etc.
    row_count       INTEGER,            -- 예상 행 수
    description     TEXT,               -- 테이블 설명 (COMMENT)
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP,
    UNIQUE(database_id, schema_name, table_name)
);

-- 필드(컬럼) 목록
CREATE TABLE columns (
    id              INTEGER PRIMARY KEY,
    table_id        INTEGER REFERENCES tables(id),
    column_name     TEXT NOT NULL,
    data_type       TEXT NOT NULL,      -- varchar, int, timestamp
    is_nullable     BOOLEAN,
    default_value   TEXT,
    max_length      INTEGER,
    numeric_precision INTEGER,
    ordinal_position  INTEGER,
    description     TEXT,               -- 컬럼 설명 (COMMENT)
    is_primary_key  BOOLEAN DEFAULT 0,
    is_foreign_key  BOOLEAN DEFAULT 0,
    fk_references   TEXT,               -- "schema.table.column"
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP,
    UNIQUE(table_id, column_name)
);

-- 관계 (탐지된 연관관계)
CREATE TABLE relationships (
    id              INTEGER PRIMARY KEY,
    source_column_id INTEGER REFERENCES columns(id),
    target_column_id INTEGER REFERENCES columns(id),
    relation_type   TEXT NOT NULL,      -- FK, SEMANTIC, NAMING, DATA_SIMILAR
    confidence      REAL NOT NULL,      -- 0.0 ~ 1.0
    detected_by     TEXT NOT NULL,      -- 탐지 엔진 이름
    detected_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    verified        BOOLEAN DEFAULT 0,  -- 사용자 확인 여부
    notes           TEXT,
    UNIQUE(source_column_id, target_column_id, relation_type)
);

-- 스키마 수집 이력
CREATE TABLE collection_log (
    id              INTEGER PRIMARY KEY,
    database_id     INTEGER REFERENCES databases(id),
    method          TEXT,              -- DIRECT, DUMP, LOG, API
    status          TEXT,              -- SUCCESS, FAILED, PARTIAL
    tables_found    INTEGER,
    columns_found   INTEGER,
    relationships_found INTEGER,
    error_message   TEXT,
    collected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    duration_seconds REAL
);
```

## 접근 우선순위

1. **1순위**: 내부 개발/스테이징 DB 직접 접속
2. **2순위**: SQL 덤프 파일 (형상관리된 마이그레이션 파일)
3. **3순위**: 쿼리 로그 분석 (운영 DB)
4. **4순위**: API 메타데이터 (OpenAPI, GraphQL Introspection)
