# Build from the repository root:
#   docker build -t agent-memory-service:local .
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/amc

COPY deploy/requirements.txt deploy/sqlite_gate.py /srv/amc/deploy/
RUN pip install -r /srv/amc/deploy/requirements.txt

RUN useradd --system --create-home --uid 10001 amc \
    && mkdir -p /var/lib/amc/data \
    && chown -R amc:amc /srv/amc /var/lib/amc

COPY app/ /srv/amc/app/

USER amc
ENV AMC_DB_DIR=/var/lib/amc/data

RUN ["python", "/srv/amc/deploy/sqlite_gate.py"]

EXPOSE 8080
VOLUME ["/var/lib/amc/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["python", "-c", "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4); sys.exit(0 if 200 <= r.status < 300 else 1)"]

# Single worker only: startup recovery assumes one process per data directory.
ENTRYPOINT ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
