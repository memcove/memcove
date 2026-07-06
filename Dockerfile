# syntax=docker/dockerfile:1

# Multi-stage build for the Memcove image. One image ships all three console
# entrypoints — memcove-server (default), memcove-flight, memcove-reconcile —
# so k8s can run them as a Deployment, a second Deployment, and a CronJob by
# overriding the command. Deps are installed frozen from the committed uv.lock.

FROM python:3.12-slim AS builder

# uv provides fast, reproducible installs from uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Layer 1: dependencies only (cached until pyproject/lock change).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Layer 2: the project itself.
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime

# Version passed at build time (from pyproject) → OCI labels.
ARG MEMCOVE_VERSION=0.0.0
LABEL org.opencontainers.image.title="memcove" \
      org.opencontainers.image.description="Lakehouse-backed memory service for LLM agents, exposed over MCP" \
      org.opencontainers.image.source="https://github.com/memcove/memcove" \
      org.opencontainers.image.documentation="https://memcove.github.io/memcove/" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${MEMCOVE_VERSION}"

# Run as an unprivileged user.
RUN groupadd --system memcove && useradd --system --gid memcove --home /app memcove

WORKDIR /app
COPY --from=builder --chown=memcove:memcove /app /app

# The venv's bin (with the three console scripts) on PATH.
ENV PATH="/app/.venv/bin:$PATH"

USER memcove

# 8090 = MCP Streamable HTTP; 8815 = Arrow Flight gRPC (memcove-flight).
EXPOSE 8090 8815

# Default to the control-plane server; override `command` for flight/reconcile.
CMD ["memcove-server"]
