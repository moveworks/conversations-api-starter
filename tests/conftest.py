"""Shared fixtures for the test suite.

The package is installed in editable mode (``pip install -e ".[app,test]"``),
so ``moveworks_capi`` and ``app`` import without path manipulation.
"""

import pytest

from moveworks_capi import rendering

SHORT_LINKS: dict = {}


@pytest.fixture(autouse=True)
def offline_short_links(monkeypatch):
    """Short links resolve from ``SHORT_LINKS`` instead of the network."""
    SHORT_LINKS.clear()
    monkeypatch.setattr(rendering, "short_link_target", SHORT_LINKS.get)
    yield SHORT_LINKS
