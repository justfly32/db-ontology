"""
LLM-based Semantic Relationship Analyzer
OpenRouter API를 활용한 의미적 연관관계 추론
"""

import os
import json
import time
import hashlib
from typing import Optional
from dataclasses import dataclass
from collections import defaultdict

import requests
import sqlite3


@dataclass
class LLMConfig:
    """LLM 설정"""
    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "deepseek/deepseek-v4-flash:free"  # 무료 모델 기본
    max_tokens: int = 500
    temperature: float = 0.1
    rpm_limit: int = 20  # OpenRouter 무료 티어: 분당 20회
    cache_ttl: int = 86400  # 캐시 TTL (24시간)


class LLMCache:
    """LLM 응답 캐시 (SQLite 기반)"""

    def __init__(self, cache_path: str = "~/.hermes/data/llm_cache.db"):
        self.cache_path = os.path.expanduser(cache_path)
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        self.conn = sqlite3.connect(self.cache_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                response TEXT,
                created_at INTEGER
            )
        """)
        self.conn.commit()

    def _make_key(self, prompt: str) -> str:
        return hashlib.md5(prompt.encode()).hexdigest()

    def get(self, prompt: str) -> Optional[str]:
        key = self._make_key(prompt)
        cur = self.conn.cursor()
        cur.execute("SELECT response FROM cache WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else None

    def set(self, prompt: str, response: str):
        key = self._make_key(prompt)
        self.conn.execute(
            "INSERT OR REPLACE INTO cache (key, response, created_at) VALUES (?, ?, ?)",
            (key, response, int(time.time()))
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


class SemanticAnalyzer:
    """LLM 기반 의미적 연관관계 분석기"""

    # 연관관계 유형 분류
    RELATION_TYPES = {
        "IDENTICAL": "동일 필드 (같은 의미, 같은 데이터)",
        "SIMILAR": "유사 필드 (비슷한 의미, 다른 세부사항)",
        "PARENT_CHILD": "부모-자식 (포함/소속 관계)",
        "REFERENCE": "참조 관계 (A가 B를 참조)",
        "DERIVED": "파생 관계 (A로부터 B가 계산됨)",
        "TEMPORAL": "시간 관련 (생성일/수정일 등)",
        "HIERARCHICAL": "계층 관련 (카테고리/분류 등)",
        "UNRELATED": "관련 없음",
    }

    def __init__(self, config: LLMConfig = None):
        self.config = config or LLMConfig()
        self.cache = LLMCache()
        self._last_request_time = 0

    def _rate_limit(self):
        """RPM 제한 준수"""
        min_interval = 60.0 / self.config.rpm_limit
        elapsed = time.time() - self._last_request_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request_time = time.time()

    def _call_llm(self, prompt: str) -> Optional[str]:
        """LLM API 호출 (캐시 포함)"""
        # 캐시 확인
        cached = self.cache.get(prompt)
        if cached:
            return cached

        if not self.config.api_key:
            return None

        self._rate_limit()

        try:
            resp = requests.post(
                f"{self.config.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.config.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": self.config.max_tokens,
                    "temperature": self.config.temperature,
                },
                timeout=30,
            )
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"]
            self.cache.set(prompt, result)
            return result
        except Exception as e:
            print(f"  ⚠️ LLM 오류: {e}")
            return None

    def _build_prompt(self, col1: dict, col2: str) -> str:
        """LLM 프롬프트 생성"""
        sample1 = col1.get("sample_values", [])[:5]
        sample2 = col2.get("sample_values", [])[:5]

        prompt = f"""당신은 데이터베이스 스키마 분석 전문가입니다.
두 필드가 의미적으로 관련이 있는지 판단하세요.

필드 1:
- 이름: {col1['column_name']}
- 테이블: {col1.get('schema', 'main')}.{col1['table']}
- 데이터 타입: {col1.get('data_type', 'unknown')}
- 설명: {col1.get('description', '없음')}
- 샘플 값: {sample1}

필드 2:
- 이름: {col2['column_name']}
- 테이블: {col2.get('schema', 'main')}.{col2['table']}
- 데이터 타입: {col2.get('data_type', 'unknown')}
- 설명: {col2.get('description', '없음')}
- 샘플 값: {sample2}

다음 JSON으로 응답하세요 (다른 텍스트 없이):
{{
  "relation_type": "<IDENTICAL|SIMILAR|PARENT_CHILD|REFERENCE|DERIVED|TEMPORAL|HIERARCHICAL|UNRELATED>",
  "confidence": <0.0~1.0>,
  "reason": "<1~2문장 설명>"
}}"""
        return prompt

    def _parse_response(self, response: str) -> dict:
        """LLM 응답 파싱"""
        if not response:
            return {"relation_type": "UNRELATED", "confidence": 0}

        # JSON 추출
        json_match = None
        for pattern in [r'\{[^}]+\}', r'```json\s*(\{.*?\})\s*```', r'(\{.*\})']:
            import re
            m = re.search(pattern, response, re.DOTALL)
            if m:
                json_match = m.group(1) if m.lastindex else m.group(0)
                break

        if not json_match:
            return {"relation_type": "UNRELATED", "confidence": 0}

        try:
            result = json.loads(json_match)
            rtype = result.get("relation_type", "UNRELATED")
            if rtype not in self.RELATION_TYPES:
                rtype = "UNRELATED"
            return {
                "relation_type": rtype,
                "confidence": min(1.0, max(0.0, float(result.get("confidence", 0)))),
                "reason": result.get("reason", ""),
            }
        except (json.JSONDecodeError, ValueError):
            if "IDENTICAL" in response: return {"relation_type": "IDENTICAL", "confidence": 0.8, "reason": response[:100]}
            if "SIMILAR" in response: return {"relation_type": "SIMILAR", "confidence": 0.6, "reason": response[:100]}
            return {"relation_type": "UNRELATED", "confidence": 0}

    def analyze_pair(self, col1: dict, col2: dict) -> dict:
        """두 필드 간 의미적 관계 분석"""
        # 같은 테이블 내 필드는 스킵
        if col1.get("table") == col2.get("table") and col1.get("schema") == col2.get("schema"):
            if col1.get("database") == col2.get("database"):
                return {"relation_type": "UNRELATED", "confidence": 0, "reason": "같은 테이블"}

        # FK로 이미 연결된 것은 스킵
        if col1.get("is_foreign_key") and col1.get("fk_references"):
            ref = col1["fk_references"]
            if col2["column_name"] in ref and col2["table"] in ref:
                return {"relation_type": "FK_KNOWN", "confidence": 0.95, "reason": "FK로 이미 연결됨"}

        prompt = self._build_prompt(col1, col2)
        response = self._call_llm(prompt)
        return self._parse_response(response)

    def analyze_batch(self, columns: list[dict], batch_size: int = 10) -> list[dict]:
        """배치 분석 (RPM 제한 고려)"""
        results = []
        total = len(columns) * (len(columns) - 1) // 2
        count = 0

        print(f"  🧠 LLM 의미 분석 시작: {len(columns)}개 컬럼 → {total}쌍")

        for i in range(len(columns)):
            for j in range(i + 1, len(columns)):
                count += 1
                if count % 5 == 0:
                    print(f"     진행: {count}/{total} ({count*100//total}%)")

                result = self.analyze_pair(columns[i], columns[j])
                if result["relation_type"] not in ("UNRELATED",) and result["confidence"] >= 0.5:
                    results.append({
                        "source": columns[i],
                        "target": columns[j],
                        **result
                    })

                if count >= batch_size:
                    print(f"     ⚠️ 배치 제한 ({batch_size}쌍) 도달")
                    return results

        return results

    def close(self):
        self.cache.close()


# ── 테스트 ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from collector.db_adapter import SQLiteAdapter, MetadataStore, SchemaCollector

    # 테스트 DB
    test_db_path = "/tmp/test_llm.db"
    meta_path = "/tmp/test_llm_meta.db"
    for p in [test_db_path, meta_path]:
        if os.path.exists(p): os.remove(p)

    conn = sqlite3.connect(test_db_path)
    conn.executescript("""
        CREATE TABLE employee (
            emp_id INTEGER PRIMARY KEY,
            emp_name TEXT,
            dept_id INTEGER,
            email TEXT,
            hire_date DATE,
            salary REAL,
            manager_id INTEGER,
            FOREIGN KEY (dept_id) REFERENCES department(dept_id),
            FOREIGN KEY (manager_id) REFERENCES employee(emp_id)
        );
        CREATE TABLE department (
            dept_id INTEGER PRIMARY KEY,
            dept_name TEXT,
            parent_dept_id INTEGER,
            location TEXT,
            FOREIGN KEY (parent_dept_id) REFERENCES department(dept_id)
        );
        CREATE TABLE project (
            project_id INTEGER PRIMARY KEY,
            project_name TEXT,
            lead_emp_id INTEGER,
            start_date DATE,
            end_date DATE,
            budget REAL,
            FOREIGN KEY (lead_emp_id) REFERENCES employee(emp_id)
        );
        CREATE TABLE emp_project (
            emp_id INTEGER,
            project_id INTEGER,
            role TEXT,
            assigned_date DATE,
            PRIMARY KEY (emp_id, project_id),
            FOREIGN KEY (emp_id) REFERENCES employee(emp_id),
            FOREIGN KEY (project_id) REFERENCES project(project_id)
        );
        CREATE TABLE salary_history (
            history_id INTEGER PRIMARY KEY,
            emp_id INTEGER,
            old_salary REAL,
            new_salary REAL,
            change_date DATE,
            FOREIGN KEY (emp_id) REFERENCES employee(emp_id)
        );
    """)
    conn.close()

    # 수집
    store = MetadataStore(db_path=meta_path)
    collector = SchemaCollector(store)
    collector.add_database("sqlite", file_path=test_db_path, db_name="hr")
    collector.collect_all()

    # LLM 분석 (API 키 없으면 rule-based fallback)
    print("\n=== LLM 의미적 관계 분석 ===")
    analyzer = SemanticAnalyzer()

    all_columns = store.get_all_columns()
    print(f"  전체 컬럼: {len(all_columns)}개")

    # API 키 확인
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        print("  ⚠️ OPENROUTER_API_KEY 없음 → rule-based fallback 사용")
        # 키가 없으면 동의어/패턴 기반으로 간이 분석
        from analyzer.relationship_analyzer import NamingPatternAnalyzer
        fallback = NamingPatternAnalyzer()
        naming_rels = fallback.find_all(all_columns)
        print(f"  명명 패턴 fallback: {len(naming_rels)}개 관계 탐지")
    else:
        analyzer.config.api_key = api_key
        results = analyzer.analyze_batch(all_columns, batch_size=5)
        for r in results[:10]:
            print(f"  🧠 [{r['relation_type']}] {r['confidence']:.2f}: "
                  f"{r['source']['table']}.{r['source']['column_name']} → "
                  f"{r['target']['table']}.{r['target']['column_name']}")
            print(f"     근거: {r['reason']}")

    analyzer.close()
    store.close()

    for p in [test_db_path, meta_path]:
        if os.path.exists(p): os.remove(p)

    print("\n✅ LLM 분석 테스트 완료")
