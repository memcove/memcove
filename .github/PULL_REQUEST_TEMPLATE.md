## Summary

<!-- What does this change and why? -->

## Changes

<!-- Bullet the notable changes. -->

-

## Testing

<!-- How did you verify this? -->

- [ ] `uv run ruff check .` passes
- [ ] `uv run pytest -m "not integration"` passes
- [ ] `uv run pytest -m integration` passes (if the change touches the data/registry path)

## Checklist

- [ ] Conventional Commit title (`feat:` / `fix:` / `chore:` / `docs:` …)
- [ ] `CHANGELOG.md` updated (and `pyproject.toml` version bumped) if user-facing
- [ ] Docs updated if behavior or configuration changed
