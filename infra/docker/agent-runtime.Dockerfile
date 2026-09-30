# ad-vit agent runtime. Build context is the REPO ROOT, because the runtime
# walks up from app/meta/fixture.py to find packages/marketing-db/fixtures and
# from app/models/router.py to find config/routing.yaml - the image keeps the
# same tree shape so neither lookup needs a special case.
#
# One Dockerfile, two Coolify apps, selected by build target:
#   api   - the default (last stage), uvicorn on :8000
#   jobs  - `python -m app.jobs.runner`, no port at all (see app/jobs/runner.py);
#           Coolify sets dockerfile_target_build=jobs on that app
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app/apps/agent-runtime

COPY apps/agent-runtime/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

COPY config/routing.yaml                     /app/config/routing.yaml
COPY packages/marketing-db/fixtures          /app/packages/marketing-db/fixtures
COPY apps/agent-runtime/app                  ./app

# Drop root before the process starts. Nothing here writes to disk.
RUN useradd --system --uid 10001 --no-create-home advit
USER advit

# --- jobs: no port, no healthcheck, no HTTP surface ---------------------------
FROM base AS jobs
# Coolify sees the api stage's HEALTHCHECK in this file and then inspects
# this container's health, so this stage needs one too: PID 1 must still be
# the runner. Liveness, not readiness - the process has nothing to answer.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3   CMD ["python", "-c", "import sys; sys.exit(0 if b'app.jobs.runner' in open('/proc/1/cmdline','rb').read() else 1)"]
CMD ["python", "-m", "app.jobs.runner"]

# --- api: last stage, so it is what an untargeted build produces -------------
FROM base AS api
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
