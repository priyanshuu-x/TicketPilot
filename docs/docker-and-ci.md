# Docker and CI/CD

## One image, four containers

```
docker compose up --build
                                        ┌──────────── ticketpilot:local ────────────┐
  postgres (pgvector) ◄── api :8000 ◄── ui :8501        mcp-server :8001
         ▲                  │                               ▲
         └──────────────────┴───── MCP over HTTP ───────────┘
```

| Service | Command | State |
|---|---|---|
| `postgres` | `pgvector/pgvector:pg17` | checkpoints + memories, volume `pgdata` |
| `mcp-server` | `python -m ticketpilot.mcp_server.server` | SQLite business data, volume `supportdata` (seeded on first start) |
| `api` | `python -m ticketpilot.api` | stateless (everything lives in Postgres) |
| `ui` | `streamlit run src/ticketpilot/ui.py` | stateless (everything comes from the API) |

The [`Dockerfile`](../Dockerfile) builds **one image for all three Python services**. They share
the same code and dependencies, so one build and one tag keep them in step.

- **Multi-stage.** The `uv` stage installs the locked dependencies (`uv sync --locked --no-dev`);
  the runtime stage copies only the venv, the code and the model. No compilers, no uv, no dev tools.
- **Layer caching.** `pyproject.toml` + `uv.lock` are copied before the code, so changing code
  doesn't reinstall dependencies.
- **The embedding model is downloaded at build time**, so containers start fast and work offline.
- **Runs as a non-root user** (`app`).
- **Configuration through environment variables only.** `.env` provides the API keys
  (`env_file`); compose overrides the addresses (`MCP_URL`, `DATABASE_URL`, `API_URL`) because
  inside Docker services reach each other by name, not `localhost`.
- **Health checks + `depends_on: service_healthy`** start things in order:
  postgres → mcp-server → api → ui.

The image is about 1.25 GB, mostly onnxruntime (local embeddings), numpy, pyarrow and Streamlit.

## CI/CD (GitHub Actions)

**[`ci.yml`](../.github/workflows/ci.yml)**, on every push and pull request:

1. `check`: `uv sync --locked`, ruff (lint + format), mypy, pytest. The tests use a scripted
   fake LLM, so CI needs no API keys and never flakes on a model.
2. `docker` (only if `check` passed): builds the image. On `main` and version tags (`v1.2.3`)
   it also **pushes to GHCR** as `ghcr.io/<owner>/<repo>` with tags `latest`, `<version>` and
   `sha-<commit>`. Build layers are cached between runs (`type=gha`).

**[`evals.yml`](../.github/workflows/evals.yml)**: manual (`workflow_dispatch`) and weekly. It
runs the golden tickets against real LLMs, writes the report into the run summary, and uploads it
as an artifact. It needs the repository secret `GROQ_API_KEY` (optionally `GOOGLE_API_KEY`,
`LANGSMITH_API_KEY`). It's separate from CI because real model calls cost quota and vary between
runs; a PR shouldn't fail because a free tier ran out.

Pull the published image:

```bash
docker pull ghcr.io/<owner>/<repo>:latest
```
