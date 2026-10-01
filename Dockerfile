# carrier-desk — the service the HappyRobot workflow calls as tools.
#
#   docker build -t carrier-desk .
#   docker run --rm -p 8000:8000 --env-file .env carrier-desk
#
# Or with compose: `docker compose up`.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Metadata and source first, dependencies last, so a code change does not
# invalidate the dependency layer.
COPY pyproject.toml README.md ./
COPY app ./app

# The fake TMS (`TMS_MODE=fake`) reads the recorded fixtures from the repository
# layout, which an installed package no longer has. Keep the copy and point the
# fake at it by name.
COPY tests/fixtures ./tests/fixtures

RUN pip install . \
 && useradd --create-home --uid 10001 appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /app /data

# The event log survives a restart when a volume is mounted here; without one it
# is still written, into the container's own filesystem.
ENV EVENTS_JSONL_PATH=/data/events.jsonl \
    TMS_FAKE_FIXTURES=/app/tests/fixtures/tms_samples.txt

USER appuser

EXPOSE 8000

# Uses python rather than curl: the slim image has no curl, and adding one just
# to run a probe would be the largest thing in it. Reads PORT the same way the
# server does, so the probe follows the platform's port rather than assuming.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, sys, urllib.request; port = os.environ.get('PORT', '8000'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + port + '/health', timeout=3).status == 200 else 1)"

# The port is whatever the platform injects as PORT, falling back to 8000 for
# local runs. Hardcoding it would mean a deploy that starts and is then never
# reachable, which looks like a hung service rather than a misconfiguration.
# `exec` so uvicorn is PID 1 and receives the shutdown signal directly.
#
# The factory, not a module-level app: see app/main.py.
CMD ["sh", "-c", "exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port ${PORT:-8000}"]
