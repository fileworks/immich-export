# Contributing

Open an issue before changing the export layout, the manifest schema, or the
exit-code contract. Downstream scripts depend on all three.

Use a focused branch and a Conventional Commit subject. `feat:` and `fix:` cut a
release through semantic-release; `chore:` and `docs:` deliberately do not.

```console
uv sync --all-extras --dev
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -q
uv build
```

Tests run against a mocked API — never against a live server, and never with a
real token in the repository. A bug fix arrives with the regression test that
fails without it.
