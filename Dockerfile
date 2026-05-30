# ============================================================
# Dockerfile — DB Ontology API Server
# ============================================================
FROM python:3.12-slim AS base

# ── 시스템 의존성 ────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# ── 작업 디렉토리 ────────────────────────────────────────
WORKDIR /app

# ── Python 의존성 ────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── 소스 코드 복사 ───────────────────────────────────────
COPY src/ src/
COPY main.py .
COPY README.md .

# ── 데이터 볼륨 ──────────────────────────────────────────
VOLUME ["/app/data", "/app/output"]

# ── 포트 ─────────────────────────────────────────────────
EXPOSE 8000

# ── 헬스체크 ─────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health').raise_for_status()"

# ── 시작 명령 ────────────────────────────────────────────
CMD ["uvicorn", "src.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
