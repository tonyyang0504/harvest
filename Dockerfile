# harvest (harvest-ai): web app + API (default command) or scheduler worker (`harvest daemon`).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HARVEST_HOME=/data
RUN useradd --create-home --uid 10001 harvest && mkdir -p /data && chown harvest:harvest /data
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir ".[web,parquet,postgres]"

USER harvest
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health').status == 200 else 1)"
CMD ["harvest", "web", "--host", "0.0.0.0", "--port", "8080"]
