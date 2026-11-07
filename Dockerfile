# Multi-stage build. Stages:
#   base    - runtime dependencies
#   test    - runs the pytest suite; the pipeline builds this with `--target test`
#   runtime - the deployed image (default target)
#
# The test stage lives here rather than in the pipeline so that tests run against
# the same base image and dependency set as the runtime image.

FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Prefetch the embedding model into the image so the app does not pull it from
# the Hugging Face Hub on every cold start. This makes startup deterministic and
# offline, and removes the network round-trip (and its memory spike) from the
# pod's boot path. FASTEMBED_CACHE_PATH is read by fastembed's TextEmbedding.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

# ---- test -------------------------------------------------------------------
# CI gate. `buildah bud --target test` runs this at build time, so a failing
# suite fails the build. Needs the dev requirements (pytest, httpx) and the test
# files, which .dockerignore previously excluded.
FROM base AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY *.py pytest.ini ./
COPY tests ./tests
RUN pytest

# ---- runtime ----------------------------------------------------------------
FROM base AS runtime
COPY *.py ./
RUN useradd --create-home app && mkdir /data && chown app /data \
 && chown -R app:app /opt/fastembed
USER app
# The model is baked into the image; never reach for the Hub at runtime.
ENV HF_HUB_OFFLINE=1
# /data is the volume mount — entries/*.md live here, index.sqlite is derived
ENV BREADCRUMBS_DIR=/data
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8080/healthz')"
# --proxy-headers so redirects use the public https address behind Caddy or a tunnel
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
