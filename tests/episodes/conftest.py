"""Fixtures for the episode tests."""

from __future__ import annotations

import pytest


@pytest.fixture(params=["direct", "mcp"])
def transport(request: pytest.FixtureRequest) -> str:
    """How the controller reaches the roles' shared state: both ways must decide the same."""
    return request.param
