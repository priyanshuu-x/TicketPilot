FROM ghcr.io/astral-sh/uv:0.11-python3.12-trixie-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

ENV FASTEMBED_CACHE_PATH=/app/models
RUN .venv/bin/python -c "from ticketpilot.embeddings import embed; embed(['warm up'])"

FROM python:3.12-slim-trixie
RUN useradd --create-home app && mkdir /data && chown app /data
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" FASTEMBED_CACHE_PATH=/app/models PYTHONUNBUFFERED=1
USER app
CMD ["python", "-m", "ticketpilot.api"]
