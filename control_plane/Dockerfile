# Multi-stage build for the control-plane API.
#
# Stage 1 compiles wheels (some dependencies need a toolchain); stage 2 ships
# only the runtime, so no compiler ends up in the published image.

# --- stage 1: build ------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only what the dependency resolution needs first, so an application-code
# change does not invalidate the (slow) dependency layer.
COPY pyproject.toml ./
COPY app/__init__.py ./app/

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip && pip install .

# --- stage 2: runtime ----------------------------------------------------
FROM python:3.12-slim AS runtime

# curl is required by the HEALTHCHECK below; without it the container reports
# unhealthy forever and depends_on: service_healthy never releases.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser migrations ./migrations
COPY --chown=appuser:appuser app ./app

# Non-root. The Docker socket is mounted read-only for the components adapter;
# running as root as well would make an already root-equivalent mount worse.
USER appuser

EXPOSE 8000

# Hits /healthz, which by design touches no dependency: this reports on the API
# process only, so a Milvus or Postgres outage never restarts the API.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
