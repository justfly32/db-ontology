"""
테스트용 샘플 DB 생성 및 스키마 수집 테스트
"""
import sqlite3
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.collector.db_adapter import (
    SQLiteAdapter, MetadataStore, SchemaCollector
)

# 1. 테스트용 샘플 DB 생성 (이커머스 스키마)
def create_test_db(path: str):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL,
            phone TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 1
        );
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            shipping_address_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        CREATE TABLE order_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            unit_price REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(order_id),
            FOREIGN KEY (product_id) REFERENCES products(product_id)
        );
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            category_id INTEGER,
            price REAL NOT NULL,
            stock_quantity INTEGER DEFAULT 0,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE categories (
            category_id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_name TEXT NOT NULL,
            parent_category_id INTEGER,
            FOREIGN KEY (parent_category_id) REFERENCES categories(category_id)
        );
        CREATE TABLE addresses (
            address_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            address_line1 TEXT NOT NULL,
            address_line2 TEXT,
            city TEXT,
            state TEXT,
            zip_code TEXT,
            country TEXT DEFAULT 'KR',
            is_default BOOLEAN DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );
        CREATE TABLE payments (
            payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            payment_method TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            transaction_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        );
        CREATE INDEX idx_orders_user_id ON orders(user_id);
        CREATE INDEX idx_orders_status ON orders(status);
        CREATE INDEX idx_order_items_order_id ON order_items(order_id);
        CREATE INDEX idx_order_items_product_id ON order_items(product_id);
        CREATE INDEX idx_products_category_id ON products(category_id);
    """)
    conn.commit()

    # 샘플 데이터 삽입
    conn.executemany("INSERT INTO users (username, email, phone) VALUES (?, ?, ?)", [
        ("alice", "alice@example.com", "010-1111-2222"),
        ("bob", "bob@example.com", "010-3333-4444"),
        ("charlie", "charlie@example.com", "010-5555-6666"),
    ])
    conn.executemany("INSERT INTO categories (category_name, parent_category_id) VALUES (?, ?)", [
        ("전자제품", None),
        ("의류", None),
        ("스마트폰", 1),
        ("노트북", 1),
        ("남성의류", 2),
        ("여성의류", 2),
    ])
    conn.executemany("INSERT INTO products (product_name, category_id, price, stock_quantity, description) VALUES (?, ?, ?, ?, ?)", [
        ("갤럭시 S25", 3, 1200000, 50, "삼성 최신 스마트폰"),
        ("아이폰 16", 3, 1300000, 30, "애플 최신 아이폰"),
        ("맥북 프로 16", 4, 3500000, 20, "애플 노트북"),
        ("리전 슬림 5", 4, 1800000, 15, "레노버 노트북"),
        ("남성 긴팔 티", 5, 29000, 100, "기본 티셔츠"),
        ("여성 원피스", 6, 59000, 80, "봄/여름 원피스"),
    ])
    conn.close()
    print(f"  ✅ 테스트 DB 생성: {path}")


# 2. 테스트 실행
if __name__ == "__main__":
    test_db_path = "/tmp/test_ecommerce.db"
    metadata_path = "/tmp/test_ontology.db"

    # 테스트 DB 생성
    create_test_db(test_db_path)

    # 메타데이터 저장소
    store = MetadataStore(db_path=metadata_path)

    # SQLite 어댑터로 스키마 수집
    adapter = SQLiteAdapter(test_db_path, db_name="ecommerce")
    collector = SchemaCollector(store)
    collector.add_adapter(adapter)

    print("\n=== 스키마 수집 ===")
    results = collector.collect_all()

    # 수집 결과 출력
    for db in results:
        print(f"\n📦 {db.name} ({db.db_type})")
        for table in db.tables:
            cols = table.columns
            pk = [c.name for c in cols if c.is_primary_key]
            fk = [c.name for c in cols if c.is_foreign_key]
            print(f"  📋 {table.table_name}: {len(cols)} cols")
            if pk:
                print(f"     PK: {', '.join(pk)}")
            if fk:
                print(f"     FK: {', '.join(fk)}")
            for col in cols:
                flags = []
                if col.is_primary_key: flags.append("PK")
                if col.is_foreign_key: flags.append(f"FK→{col.fk_references}")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                desc = f" - {col.description}" if col.description else ""
                print(f"       {col.name}: {col.data_type}{flag_str}{desc}")

    # 저장된 컬럼 확인
    print("\n=== 저장된 컬럼 (관계 분석 대상) ===")
    all_columns = store.get_all_columns()
    print(f"  전체 컬럼: {len(all_columns)}개")
    for col in all_columns[:20]:
        print(f"    {col['database']}.{col['schema']}.{col['table']}.{col['column_name']} ({col['data_type']})")

    store.close()

    # 정리
    os.remove(test_db_path)
    os.remove(metadata_path)
    print("\n✅ 테스트 완료 (임시 파일 삭제됨)")
