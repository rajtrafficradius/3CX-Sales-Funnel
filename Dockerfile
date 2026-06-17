FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Railway injects $PORT; the entrypoint binds the dashboard to it.
EXPOSE 8080

# Runtime config comes from env (Railway variables); never bake secrets in.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["dashboard"]
