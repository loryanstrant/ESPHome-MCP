FROM python:3.13-slim

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
