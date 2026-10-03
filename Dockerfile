FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY *.py ./
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
ENV BREADCRUMBS_DB=/data/breadcrumbs.db
VOLUME /data
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8080/healthz')"
# --proxy-headers so redirects use the public https address behind Caddy or a tunnel
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
