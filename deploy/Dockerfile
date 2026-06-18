# Dockerfile for the Amperstand vault server.
#
# Build context is the amperstand-core repo root. With docker-compose.yml in
# this directory, that means `build: { context: .., dockerfile: deploy/Dockerfile }`.
#
# Image is intentionally minimal — slim base + only what `amperstand-core` and
# its server runtime need. CLI and email-watch features live in the
# bare-metal deploy (deploy/bootstrap.sh); the container is API-only.

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/amperstand/amperstand-core

# Install deps first (dependency-only step caches well across source edits).
COPY pyproject.toml ./
RUN pip install . --no-deps && pip install fastapi uvicorn

# Now drop in the source. Mounted as a bind in docker-compose.yml for dev;
# baked in here for `docker run` standalone use.
COPY src ./src

# Re-install editable so the COPY'd source is what runs.
RUN pip install -e .

# Vault data and env live in volumes — see docker-compose.yml.
ENV AMPERSTAND_DATA_DIR=/var/lib/amperstand/vault
VOLUME ["/var/lib/amperstand"]

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/health || exit 1

CMD ["uvicorn", "amperstand_core.server.app:app", \
     "--host", "0.0.0.0", "--port", "8765", "--workers", "1"]
