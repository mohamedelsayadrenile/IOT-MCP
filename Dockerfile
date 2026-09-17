FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Dependencies first so a source change does not re-resolve them. This is a
# virtual project (package = false), so there is nothing else to install.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src ./src

RUN useradd --create-home --uid 10001 nojo
USER nojo

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ['PORT'] + '/healthz').read()"]

# uvicorn from the venv directly rather than `uv run`, which would try to
# re-sync a root-owned .venv as an unprivileged user.
# --proxy-headers so the app sees the real client scheme and host behind TLS
# termination; set FORWARDED_ALLOW_IPS to the proxy's network.
CMD ["sh", "-c", "exec uvicorn src.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips ${FORWARDED_ALLOW_IPS:-127.0.0.1}"]
