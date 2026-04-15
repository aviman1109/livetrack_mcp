FROM python:3.12-slim AS base

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests

RUN pip install --no-cache-dir --upgrade pip setuptools wheel hatchling && \
    pip install --no-cache-dir ".[test]"

# ── test stage: runs pytest; fails build if any test fails ────────────────────

FROM base AS test

RUN pytest tests/ -v --tb=short

# ── runtime stage: lean image without test deps ───────────────────────────────

FROM python:3.12-slim AS runtime

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=38100 \
    HOST=0.0.0.0 \
    MCP_PATH=/mcp \
    DB_PATH=/data/livetrack.db \
    CLAUDE_RUNNER_URL=http://localhost:38095 \
    RUNNER_WORKSPACE=training

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip setuptools wheel hatchling && \
    pip install --no-cache-dir .

RUN mkdir -p /data

EXPOSE 38100

CMD ["livetrack-mcp"]
