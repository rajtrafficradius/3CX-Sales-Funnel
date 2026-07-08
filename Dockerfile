FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System libraries WeasyPrint needs to render the audit PDF (Pango/Cairo/GDK-Pixbuf + fonts).
# Kept in one layer; slim base otherwise has no graphics stack.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangocairo-1.0-0 libpangoft2-1.0-0 libgdk-pixbuf-2.0-0 \
      libcairo2 libffi8 shared-mime-info fonts-dejavu-core fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

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
