"""Shared JSON fixtures for unit tests."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    """Load a JSON document shaped like an OpenAPI response."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
