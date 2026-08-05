# DevGuard AI — backend production image.
# Multi-stage: build wheels in one layer, ship a slim runtime.
#
# BUILD CONTEXT IS THE REPOSITORY ROOT, not ./backend. That is deliberate, and it
# is the fix for AUDIT B1 and B2 — which together meant this image had never
# built and could never have booted:
#
#   B1  `COPY requirements.txt .` failed outright, because requirements.txt lives
#       at the repo ROOT while compose passed `context: ./backend`. The build
#       died on the first COPY.
#   B2  `CMD uvicorn main:app` could not have worked even past B1: main.py does
#       `from backend.core import telemetry` and `from backend.api.router import
#       router`, so the app must be imported as `backend.main:app` with the repo
#       root on sys.path — not as a top-level `main`.
#
# Both are fixed by building from the root and running the package-qualified
# module:
#
#   docker build -f backend/Dockerfile -t devguard-backend .
#   (or `docker compose build backend`, which now passes context: . )

# ---- Stage 1: build dependencies ----
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps some wheels need (kept minimal).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---- Stage 2: runtime ----
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    PYTHONPATH=/app

WORKDIR /app

# Non-root user for a smaller attack surface.
RUN useradd --create-home --uid 1000 appuser

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# Copy only what the service needs at runtime. `COPY . .` from the repo root
# would drag in the frontend, venvs, .next/ and the SigNoz deploy tree;
# .dockerignore prunes those from the context and these explicit COPYs keep the
# image honest about its own surface.
COPY backend/ ./backend/
COPY groq_client.py ./
COPY data/ ./data/

USER appuser

EXPOSE 8000

# Healthcheck lets Railway/Render/Compose know when we're actually ready.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://localhost:{os.getenv(\"PORT\",\"8000\")}/slo-status')" || exit 1

# Shell form so ${PORT} expands (Railway/Render inject their own PORT).
# `backend.main:app`, not `main:app` — see B2 above.
CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --workers 2
