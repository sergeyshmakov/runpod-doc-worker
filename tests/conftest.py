"""Shared pytest setup."""

from __future__ import annotations

import os

import pytest

from runpod_doc_worker import config


# Tests should not see any operator credentials.
for key in ("RUNPOD_API_KEY", "RUNPOD_ENDPOINT_ID"):
    os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def _default_config():
    """Every test starts from the default config and leaves it that way.

    The active config is process-wide, so a test that installs one would
    otherwise leak it into whatever runs next.
    """
    config.reset()
    yield
    config.reset()
