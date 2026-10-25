FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends tzdata=2026e-0+deb13u1 ca-certificates=20250419+deb13u1 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 10001 botuser
WORKDIR /app
ENV PYTHONPATH=/app/src
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --only-binary :all: -r /app/requirements.txt
COPY src /app/src
COPY config /app/config
RUN chown -R botuser:botuser /app
USER botuser
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 CMD python -m gg_bot healthcheck --config /app/config/config.example.yaml || exit 1
ENTRYPOINT ["python", "-m", "gg_bot"]
CMD ["run", "--config", "/app/config/config.yaml"]
