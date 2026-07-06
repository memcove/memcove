# Contributing to Memcove

Thanks for your interest in Memcove! This guide covers the local setup, the test
and lint gates, and the pull-request flow.

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

Memcove targets **Python 3.12+** and uses [`uv`](https://docs.astral.sh/uv/) for
dependency management (a committed `uv.lock` pins exact versions).

```bash
# 1. install deps (dev extras include pytest + ruff)
uv sync --extra dev

# 2. bring up the local lakehouse for integration work (MinIO + Iceberg REST +
#    Trino + Postgres). --wait blocks until everything is healthy.
docker compose up -d --wait
```

The defaults in `.env.example` already point at the local stack; copy it to `.env`
if you want to override anything.

## Tests & lint

CI gates every PR on these — run them locally first:

```bash
uv run ruff check .                    # lint
uv run pytest -m "not integration"     # unit tests (no infra needed)
uv run pytest -m integration           # end-to-end (needs the compose stack up)
```

Integration tests skip themselves automatically if Trino/Postgres aren't
reachable, so `pytest` with no marker is safe to run anytime.

New behavior should come with tests. The unit suite mirrors `src/` under `tests/`.

## Pull requests

- Branch from `main` using a conventional prefix: `feat/…`, `fix/…`, `chore/…`,
  `docs/…`.
- Keep PRs focused — one concern per PR.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for the title
  (e.g. `feat: …`, `fix: …`, `chore: …`).
- Update `CHANGELOG.md` under the appropriate version heading, and bump the
  `version` in `pyproject.toml` when your change is user-facing.
- Make sure `ruff check` and the unit tests pass. Fill in the PR template.

## Reporting bugs & requesting features

Open an issue using the templates. For **security vulnerabilities**, do **not**
open a public issue — see [SECURITY.md](SECURITY.md).
