# DB Ontology Analyzer

다중 데이터베이스 스키마를 수집하고, 테이블/컬럼 간 연관관계를 분석하여 온톨로지 그래프로 시각화하는 도구입니다.

## Features

- **멀티 DB 지원**: PostgreSQL, MySQL, SQLite, OpenAPI, GraphQL
- **연관관계 분석**: FK 기반 + 명명패턴(정확/정규화/동의어) + LLM 의미 분석
- **온톨로지 그래프**: NetworkX 기반 계층형 그래프 (DB → Schema → Table → Column)
- **시각화**: D3.js 인터랙티브 대시보드
- **증분 업데이트**: 스키마 드리프트 감지 및 변경 이력 관리
- **알림**: Telegram / Slack 연동
- **REST API**: FastAPI 기반, RBAC 인증 포함

## Quick Start

```bash
# 설치
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 환경 설정
cp .env.example .env
# .env 파일에서 DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD 수정

# 실행
python main.py
```

실행하면 전체 테이블 목록이 출력되고, 분석할 테이블을 번호로 선택합니다.

## Usage

### CLI 인터랙티브 모드

```text
📋 전체 1042개 테이블 (12개 스키마)
  ── public ──
     1: orders
     2: users
     3: products
     ...
  ── sales ──
   101: transactions
   ...
─────────────────────────────────
  선택할 테이블 번호 (쉼표/범위 구분, a=전체, q=종료):
```

### API 서버

```bash
uvicorn src.api.server:app --reload --port 8000
# API 문서: http://localhost:8000/docs
```

### Docker

```bash
docker compose up
```

## Project Structure

```
db-ontology/
├── main.py                 # CLI 엔트리포인트
├── src/
│   ├── collector/          # DB/API 스키마 수집
│   │   ├── db_adapter.py   # SQLite/PostgreSQL/MySQL 어댑터
│   │   ├── api_collector.py # OpenAPI/GraphQL/REST 수집
│   │   ├── drift_detector.py # 스키마 변경 감지
│   │   └── notifier.py     # Telegram/Slack 알림
│   ├── analyzer/           # 연관관계 분석
│   │   ├── relationship_analyzer.py  # FK + 명명패턴 분석
│   │   └── semantic_analyzer.py      # LLM 의미 분석
│   ├── ontology/           # 그래프 구축
│   │   └── graph_builder.py
│   ├── visualizer/         # 대시보드
│   │   └── dashboard.py
│   └── api/                # FastAPI 서버
│       ├── server.py
│       └── auth.py         # JWT + RBAC
├── output/                 # 분석 결과물
├── k8s/                    # Kubernetes 배포
└── tests/
```

## Outputs

| 파일 | 설명 |
|------|------|
| `output/dashboard.html` | 통합 대시보드 (통계 + 그래프 + 검색 + 관계) |
| `output/ontology_graph.html` | D3.js 인터랙티브 그래프 |
| `output/ontology.graphml` | GraphML 포맷 (Gephi 등에서 열기) |

## Requirements

- Python 3.12+
- PostgreSQL (or MySQL, SQLite)
- See `requirements.txt` for full dependency list

## License

MIT
