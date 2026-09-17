"""Lazily-created, cached SQLMesh Context for the configured project."""

from __future__ import annotations

import os
from functools import lru_cache

from sqlmesh.core.context import Context


class ProjectNotConfiguredError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_context() -> Context:
    path = os.environ.get("SQLMESH_PROJECT_PATH")
    if not path:
        raise ProjectNotConfiguredError(
            "SQLMESH_PROJECT_PATH is not set. Point it at a directory containing "
            "a SQLMesh config.py/config.yaml."
        )
    return Context(paths=path)
