# Phase 1-4 & 1-5: 연관관계 유형 상세 정의 + 기술 스택 선정

## 연관관계 유형 상세

### 유형 A: 구조적 관계 (Structural Relationship)

#### A1. 외래키 (Foreign Key)
```sql
-- 탐지 쿼리 (PostgreSQL)
SELECT
    tc.table_schema, tc.table_name, kcu.column_name,
    ccu.table_schema AS foreign_schema,
    ccu.table_name AS foreign_table,
    ccu.column_name AS foreign_column
FROM information_schema.table_constraints AS tc
JOIN information_schema.key_column_usage AS kcu
    ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage AS ccu
    ON ccu.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY';
```
- **신뢰도**: 0.95 (명시적 선언)
- **자동 채택**: YES

#### A2. 인덱스 기반 관계
- 동일 컬럼명 + 인덱스 패턴으로 추론
- 예: `idx_order_user_id` → `user_id` FK 추정
- **신뢰도**: 0.70

#### A3. JOIN 패턴 (쿼리 로그)
```python
# SQL 파싱으로 JOIN 관계 추출
import sqlparse
parsed = sqlparse.parse(query)
for token in parsed[0].flatten():
    if token.ttype is None and isinstance(token, sqlparse.sql.Comparison):
        # "table1.col = table2.col" 패턴 추출
        pass
```
- **신뢰도**: 0.85 (실제 사용 관계)

### 유형 B: 명명 패턴 관계 (Naming Pattern)

#### B1. 정확 매칭
```python
# 동일 필드명 매칭
exact_matches = {}
for col in all_columns:
    key = col.name.lower()
    if key in exact_matches:
        exact_matches[key].append(col)
    else:
        exact_matches[key] = [col]
# user_id가 여러 테이블에 존재 → 관계 후보
```
- **신뢰도**: 0.90

#### B2. 정규화 매칭 (Normalization)
```python
import re
def normalize(name):
    # userId → user_id, UserID → user_id
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    s = re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s)
    s = re.sub(r'[\s\-]', '_', s)
    return s.lower()

# user_id ≈ userId ≈ usr_id ≈ USER_ID
```
- **신뢰도**: 0.75

#### B3. 접두사/접미사 그룹핑
```python
# order_id, order_date, order_status → "order" 도메인
# user_name, user_email, user_phone → "user" 도메인
from collections import defaultdict
domains = defaultdict(list)
for col in all_columns:
    prefix = col.name.split('_')[0]  # 간단한 분리
    domains[prefix].append(col)
```
- **신뢰도**: 0.60

#### B4. 동의어 매칭
```python
# 사전 기반 동의어 매칭
synonyms = {
    'customer': ['client', 'buyer', 'purchaser', 'consumer'],
    'product': ['item', 'goods', 'merchandise', 'sku'],
    'order': ['purchase', 'transaction', 'deal'],
    'user': ['account', 'member', 'person', 'employee'],
    'address': ['location', 'place', 'residence'],
    'phone': ['telephone', 'mobile', 'cell', 'contact'],
    'email': ['mail', 'e_mail', 'electronic_mail'],
    'name': ['title', 'label', 'display_name'],
    'date': ['time', 'timestamp', 'created_at', 'updated_at'],
    'amount': ['price', 'cost', 'fee', 'total', 'sum'],
    'status': ['state', 'condition', 'stage', 'phase'],
    'description': ['desc', 'detail', 'note', 'comment', 'memo'],
}
```
- **신뢰도**: 0.50

### 유형 C: 데이터 유사도 (Data Similarity)

#### C1. 값 중복도 (Jaccard Similarity)
```python
def jaccard_similarity(set1, set2):
    intersection = len(set1 & set2)
    union = len(set1 | set2)
    return intersection / union if union > 0 else 0

# 예: tableA.email 값 집합 ∩ tableB.email 값 집합
# Jaccard > 0.3 → 관계 후보
```
- **신뢰도**: 0.80 (높을수록)

#### C2. 패턴 매칭
```python
import re
patterns = {
    'email':    r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$',
    'phone':    r'^\d{2,3}-\d{3,4}-\d{4}$',
    'ssn':      r'^\d{6}-\d{7}$',
    'date':     r'^\d{4}-\d{2}-\d{2}$',
    'datetime': r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}',
    'url':      r'^https?://',
    'ip':       r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$',
    'uuid':     r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    'zipcode':  r'^\d{5}$',
    'currency': r'^\$?\d{1,3}(,\d{3})*(\.\d{2})?$',
}
# 같은 패턴 매칭 → 같은 의미 추정
```
- **신뢰도**: 0.85

#### C3. 분포 유사도
```python
# 데이터 프로파일링 기반
profile = {
    'type': 'varchar',
    'avg_length': 45.2,
    'null_ratio': 0.05,
    'unique_ratio': 0.85,
    'min': 'A',
    'max': 'zzz',
}
# 유사한 프로파일 → 관계 후보
```
- **신뢰도**: 0.65

### 유형 D: 의미적 관계 (Semantic / LLM)

#### D1. LLM 기반 관계 추론
```python
prompt = f"""
다음 두 데이터베이스 필드가 의미적으로 관련이 있는지 판단하세요.

필드 1: {table1}.{column1} (타입: {type1}, 설명: {desc1})
필드 2: {table2}.{column2} (타입: {type2}, 설명: {desc2})

샘플 데이터:
- 필드 1: {sample1[:5]}
- 필드 2: {sample2[:5]}

관계 유형: [동일 / 유사 / 관련없음 / 부모-자식]
신뢰도: 0~100
이유: 간단히 설명
"""
```
- **신뢰도**: 0.70

#### D2. 도메인 자동 분류
```python
# 필드 패턴으로 도메인 분류
domain_rules = {
    'person':     ['name', 'first_name', 'last_name', 'full_name', 'birth', 'gender', 'age'],
    'contact':    ['email', 'phone', 'mobile', 'fax', 'contact'],
    'address':    ['address', 'city', 'state', 'zip', 'country', 'postal'],
    'order':      ['order_id', 'order_date', 'order_status', 'order_total'],
    'product':    ['product_id', 'product_name', 'sku', 'price', 'category'],
    'payment':    ['payment', 'card', 'billing', 'invoice', 'amount', 'total'],
    'timestamp':  ['created_at', 'updated_at', 'deleted_at', 'timestamp', 'date'],
    'status':     ['status', 'state', 'stage', 'is_active', 'is_deleted'],
    'user':       ['user_id', 'username', 'password', 'role', 'permission'],
}
```
- **신뢰도**: 0.60

## 통합 신뢰도 점수 산출

```python
def calculate_confidence(evidence_list):
    """
    evidence_list: [(score, weight, source), ...]
    예: [(0.95, 3.0, 'FK'), (0.75, 1.5, 'naming'), (0.80, 2.0, 'data_similarity')]
    """
    total_weight = sum(w for _, w, _ in evidence_list)
    weighted_sum = sum(s * w for s, w, _ in evidence_list)
    confidence = weighted_sum / total_weight if total_weight > 0 else 0

    # FK 발견 시 자동 상향
    if any(s >= 0.95 for s, _, src in evidence_list if src == 'FK'):
        confidence = max(confidence, 0.95)

    return round(confidence, 2)

# 판정 기준
# >= 0.90: 자동 채택 (AUTO)
# >= 0.70: 자동 제안 (SUGGEST)
# >= 0.50: 수동 검토 (REVIEW)
# <  0.50: 무시 (IGNORE)
```

## 기술 스택 선정 (1-5)

### 최종 선정

| 영역 | 기술 | 버전 | 선정 이유 |
|------|------|------|----------|
| **언어** | Python | 3.11+ | 데이터 분석 생태계, async 지원 |
| **그래프 DB** | Neo4j Community | 5.x | 온톨로지 그래프, Cypher 쿼리, 시각화 |
| **내부 저장** | SQLite | 3.x | 메타데이터 캐시, 설정 저장 |
| **캐시** | Redis | 7.x | 작업 큐, 세션, 증분 업데이트 |
| **API** | FastAPI | 0.110+ | 비동기, 자동 문서화, Pydantic |
| **작업 큐** | Celery | 5.x | 비동기 스키마 수집/분석 |
| **SQL 파싱** | sqlparse | 0.5.x | 쿼리 로그 분석 |
| **HTTP** | httpx | 0.27+ | 비동기 HTTP (API 메타데이터) |
| **LLM** | OpenRouter API | - | 다양한 모델 활용, 비용 효율 |
| **프론트** | React 18 | 18.x | 컴포넌트 기반 UI |
| **그래프 UI** | Cytoscape.js | 3.x | 대규모 그래프 시각화, 인터랙티브 |
| **테이블 UI** | AG Grid | 31.x | 대규모 데이터 테이블, 필터링 |
| **스타일** | Tailwind CSS | 3.x | 유틸리티 기반 스타일링 |
| **컨테이너** | Docker | 24.x | 개발/배포 환경 일관성 |
| **오케스트레이션** | Docker Compose | - | 로컬 개발 (k8s는 옵션) |
| **모니터링** | Prometheus + Grafana | - | 메트릭 수집/시각화 |

### 대안 (상황에 따라)
- **Neo4j 대신**: NetworkX (순수 Python, 소규모) + SQLite
- **Redis 대신**: 파일 기반 캐시 (소규모)
- **Celery 대신**: asyncio (단순 작업)
- **React 대신**: Jinja2 서버사이드 렌더링 (빠른 프로토타이핑)
