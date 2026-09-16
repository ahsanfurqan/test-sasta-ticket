"""Shared fixtures. Owned by test-engineer."""

import os

import pytest

BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8000")


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def api_key() -> str:
    from meter.config import get_settings

    return get_settings().dev_api_key
