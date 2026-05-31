-- ============================================================
-- DB Ontology Analyzer - PostgreSQL Sample Data
-- Docker: docker run -d --name pg-test -e POSTGRES_PASSWORD=test1234 \
--         -e POSTGRES_DB=testdb -p 5432:5432 postgres:16-alpine
-- 실행: psql -h localhost -U postgres -d testdb -f sample_data.sql
-- ============================================================

-- ── public.users ──────────────────────────────────────────
COMMENT ON SCHEMA public IS '기본 서비스 스키마';

CREATE TABLE public.users (
    user_id INTEGER PRIMARY KEY,
    username VARCHAR(50) NOT NULL,
    email VARCHAR(100),
    phone VARCHAR(20),
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT NOW()
);
COMMENT ON TABLE public.users IS '서비스 사용자 정보';
COMMENT ON COLUMN public.users.user_id IS '사용자 고유 식별자';
COMMENT ON COLUMN public.users.username IS '사용자 로그인 아이디';
COMMENT ON COLUMN public.users.email IS '이메일 주소';
COMMENT ON COLUMN public.users.phone IS '휴대폰 번호';

-- ── public.orders ─────────────────────────────────────────
CREATE TABLE public.orders (
    order_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES public.users(user_id),
    total_amount NUMERIC(12,2),
    status VARCHAR(20) DEFAULT 'pending',
    order_date TIMESTAMP DEFAULT NOW()
);
COMMENT ON TABLE public.orders IS '주문 정보 (FK가 명시적으로 정의됨)';
COMMENT ON COLUMN public.orders.user_id IS '주문자 (users 참조)';
COMMENT ON COLUMN public.orders.total_amount IS '주문 총 금액';

-- ── public.products ───────────────────────────────────────
CREATE TABLE public.products (
    product_id INTEGER PRIMARY KEY,
    product_name VARCHAR(200),
    price NUMERIC(12,2),
    category VARCHAR(100),
    stock_qty INTEGER DEFAULT 0
);
COMMENT ON TABLE public.products IS '상품 마스터';
COMMENT ON COLUMN public.products.product_name IS '상품명';
COMMENT ON COLUMN public.products.price IS '판매 가격';

-- ── public.order_items ────────────────────────────────────
CREATE TABLE public.order_items (
    item_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES public.orders(order_id),
    product_id INTEGER NOT NULL REFERENCES public.products(product_id),
    quantity INTEGER NOT NULL,
    unit_price NUMERIC(12,2)
);
COMMENT ON TABLE public.order_items IS '주문별 상품 내역';
COMMENT ON COLUMN public.order_items.order_id IS '주문 (orders 참조)';
COMMENT ON COLUMN public.order_items.product_id IS '상품 (products 참조)';

-- ── public.payments ───────────────────────────────────────
CREATE TABLE public.payments (
    payment_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES public.orders(order_id),
    amount NUMERIC(12,2),
    method VARCHAR(30),
    status VARCHAR(20) DEFAULT 'pending',
    paid_at TIMESTAMP
);
COMMENT ON TABLE public.payments IS '결제 내역';
COMMENT ON COLUMN public.payments.order_id IS '결제 대상 주문';
COMMENT ON COLUMN public.payments.amount IS '결제 금액';

-- ── hr.department ─────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS hr;
COMMENT ON SCHEMA hr IS '인사 관리 스키마';

CREATE TABLE hr.department (
    dept_id INTEGER PRIMARY KEY,
    dept_name VARCHAR(100) NOT NULL,
    parent_dept_id INTEGER REFERENCES hr.department(dept_id),
    location VARCHAR(200)
);
COMMENT ON TABLE hr.department IS '부서 정보 (계층 구조)';
COMMENT ON COLUMN hr.department.dept_id IS '부서 고유 번호';
COMMENT ON COLUMN hr.department.dept_name IS '부서명';

-- ── hr.employee ───────────────────────────────────────────
CREATE TABLE hr.employee (
    emp_id INTEGER PRIMARY KEY,
    emp_name VARCHAR(50) NOT NULL,
    email VARCHAR(100),
    dept_id INTEGER REFERENCES hr.department(dept_id),
    manager_id INTEGER REFERENCES hr.employee(emp_id),
    salary NUMERIC(12,2),
    hire_date DATE
);
COMMENT ON TABLE hr.employee IS '직원 정보';
COMMENT ON COLUMN hr.employee.emp_id IS '직원 고유 번호';
COMMENT ON COLUMN hr.employee.email IS '회사 이메일';
COMMENT ON COLUMN hr.employee.dept_id IS '소속 부서';

-- ── hr.salary_history (FK 없음 — 값 기반 검증으로 관계 탐지) ─────
CREATE TABLE hr.salary_history (
    history_id INTEGER PRIMARY KEY,
    emp_id INTEGER NOT NULL,
    old_salary NUMERIC(12,2),
    new_salary NUMERIC(12,2),
    change_date DATE
);
COMMENT ON TABLE hr.salary_history IS '급여 변경 이력 (FK 제약 없음)';
COMMENT ON COLUMN hr.salary_history.emp_id IS '직원 번호 (employee.emp_id와 같은 값)';

-- ── hr.project ────────────────────────────────────────────
CREATE TABLE hr.project (
    project_id INTEGER PRIMARY KEY,
    project_name VARCHAR(200),
    lead_emp_id INTEGER,
    budget NUMERIC(12,2)
);
COMMENT ON TABLE hr.project IS '프로젝트';
COMMENT ON COLUMN hr.project.lead_emp_id IS '프로젝트 리드 (employee.emp_id 참조, FK 없음)';

-- ============================================================
-- 샘플 데이터 (값 기반 검증용)
-- ============================================================

INSERT INTO public.users VALUES
    (1, 'alice', 'alice@test.com', '010-1111-1111', 'active', NOW()),
    (2, 'bob', 'bob@test.com', '010-2222-2222', 'active', NOW()),
    (3, 'carol', 'carol@test.com', NULL, 'inactive', NOW());

INSERT INTO public.orders VALUES
    (1, 1, 15000, 'completed', NOW()),
    (2, 1, 25000, 'completed', NOW()),
    (3, 2, 8000, 'pending', NOW()),
    (4, 3, 12000, 'cancelled', NOW());

INSERT INTO public.products VALUES
    (1, '노트북', 1500000, '전자제품', 10),
    (2, '마우스', 25000, '전자제품', 50),
    (3, '키보드', 45000, '전자제품', 30);

INSERT INTO public.order_items VALUES
    (1, 1, 1, 1, 1500000),
    (2, 2, 2, 2, 25000),
    (3, 3, 2, 1, 25000),
    (4, 4, 3, 1, 45000);

INSERT INTO public.payments VALUES
    (1, 1, 15000, 'card', 'completed', NOW()),
    (2, 2, 25000, 'transfer', 'completed', NOW()),
    (3, 3, 8000, 'card', 'pending', NULL);

INSERT INTO hr.department VALUES
    (1, '개발팀', NULL, '서울'),
    (2, '디자인팀', NULL, '서울'),
    (3, '프론트개발', 1, '서울');

INSERT INTO hr.employee VALUES
    (1, '김철수', 'chulsoo@company.com', 1, NULL, 5000, '2020-01-01'),
    (2, '이영희', 'younghee@company.com', 1, 1, 4500, '2021-03-01'),
    (3, '박민수', 'minsu@company.com', 2, NULL, 4800, '2020-06-01');

INSERT INTO hr.salary_history VALUES
    (1, 1, 4500, 5000, '2020-07-01'),
    (2, 1, 5000, 5500, '2021-07-01'),
    (3, 2, 4000, 4500, '2021-07-01'),
    (4, 3, 4200, 4800, '2021-06-01');

INSERT INTO hr.project VALUES
    (1, '차세대 시스템', 1, 100000),
    (2, 'UI 리뉴얼', 3, 50000);
