# Pinned by digest for reproducible builds (python:3.13-slim).
FROM python:3.13-slim@sha256:aec3f1588bdda76cde971575692e33d11bf83a2bcaa2e1c315c47de6f72ee21a

WORKDIR /app

ARG SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0

COPY pyproject.toml .
COPY src/ src/

RUN pip install uv && \
    SETUPTOOLS_SCM_PRETEND_VERSION=${SETUPTOOLS_SCM_PRETEND_VERSION} uv pip install --system --no-cache .

EXPOSE 8080

COPY healthcheck.py .
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "healthcheck.py"]

ENTRYPOINT ["esphome-mcp-web"]
