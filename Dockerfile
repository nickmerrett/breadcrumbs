FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY *.py ./
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
# /data is the volume mount — entries/*.md live here, index.sqlite is derived
ENV BREADCRUMBS_DIR=/data
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8080/healthz')"
# --proxy-headers so redirects use the public https address behind Caddy or a tunnel
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
