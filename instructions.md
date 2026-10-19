# sqlmesh-mcp Release Instructions

This document describes how to release the package to PyPI. Do not publish a
release until the working tree, tests, changelog, and package version agree.

## Prerequisites

- GitHub access to `Zain-ul-Abdin45/sqlmesh-mcp`.
- PyPI access to the `sqlmesh-mcp` project.
- PyPI Trusted Publishing configured for:
  - owner: `Zain-ul-Abdin45`
  - repository: `sqlmesh-mcp`
  - workflow: `.github/workflows/publish.yml`
  - environment: `pypi`
- A clean checkout on `main` with the intended commit pushed to GitHub.

## Prepare a Release

1. Inspect the current state:

  ```bash
  git status --short --branch
  git log --oneline -5
  ```

2. Choose the next package version and update the same version in
  `pyproject.toml`.

3. Add a dated entry to `CHANGELOG.md` using Keep a Changelog sections such
  as `Added`, `Changed`, `Fixed`, and `Security`.

4. Update `README.md`, `architecture.mmd`, and `sql-mesh-phases.md` when the
  release changes public capabilities or project status.

5. Run the complete local validation:

  ```bash
  rm -f db.db
  .venv/bin/ruff check src tests
  .venv/bin/python -m pytest -q
  git diff --check
  ```

6. Build and validate the distribution:

  ```bash
  rm -rf dist build
  .venv/bin/python -m pip install --upgrade build twine
  .venv/bin/python -m build
  .venv/bin/python -m twine check dist/*
  ```

7. Inspect the package contents before publishing:

  ```bash
  unzip -l dist/*.whl
  tar -tzf dist/*.tar.gz
  ```

  Confirm that source code, `README.md`, `LICENSE`, and `py.typed` are
  present, and that local databases, audit logs, caches, and virtualenvs are
  absent.

## Commit and Publish

1. Commit the release changes with a precise message, for example:

  ```text
  Release 0.3.0: add scoped MCP security and async operations
  ```

2. Push the commit to `main`:

  ```bash
  git push origin main
  ```

3. Create an annotated version tag matching `pyproject.toml`:

  ```bash
  git tag -a v0.3.0 -m "Release v0.3.0"
  git push origin v0.3.0
  ```

4. Create a GitHub Release for that tag. The publish workflow is triggered by
  the GitHub Release `published` event, not by a tag push alone.

5. Monitor the `Publish to PyPI` GitHub Actions workflow. It builds the wheel
  and source distribution, then publishes through Trusted Publishing using
  OIDC. No PyPI API token should be added to the repository.

## Verify the Release

After the workflow succeeds:

```bash
PACKAGE_VERSION="$(.venv/bin/python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
python3 -m venv /tmp/sqlmesh-mcp-release-check
/tmp/sqlmesh-mcp-release-check/bin/python -m pip install --upgrade pip
/tmp/sqlmesh-mcp-release-check/bin/python -m pip install "sqlmesh-mcp==${PACKAGE_VERSION}"
/tmp/sqlmesh-mcp-release-check/bin/python -c 'import importlib.metadata; print(importlib.metadata.version("sqlmesh-mcp"))'
/tmp/sqlmesh-mcp-release-check/bin/python -c 'import sqlmesh_mcp; print(sqlmesh_mcp.__file__)'
```

Do not run `sqlmesh-mcp --help`: the default stdio MCP server waits for an
MCP client connection and does not implement a command-line help mode.

Do not run this verification step until the matching version has been
published. For example, the current published versions are `0.1.0` and
`0.2.0`; `0.3.0` becomes valid only after completing the release workflow.

Also verify the package page, version, files, and release metadata on PyPI:

`https://pypi.org/project/sqlmesh-mcp/`

Finally confirm that the repository is clean and the tag points at the
intended commit:

```bash
git status --short --branch
git show --stat --oneline v0.3.0
```

## Release Safety

- Never commit tokens, bearer secrets, connection strings, audit logs, or
  approval files containing real project data.
- Never publish from a dirty worktree without reviewing every change.
- Keep `pyproject.toml`, `CHANGELOG.md`, GitHub tag, and PyPI version aligned.
- Do not reuse a published version number. Fix the version and publish a new
  release instead.
- If Trusted Publishing fails, inspect GitHub Actions and PyPI publisher
  configuration; do not add a long-lived PyPI token as a workaround.
