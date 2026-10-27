"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from fixtures.make_fixtures import generate_corpus


@pytest.fixture(scope="session")
def corpus(tmp_path_factory):
    """The synthetic fixture corpus, generated once per test session.

    Returns (directory, manifest). Tests must not modify files in it; copy them
    into their own tmp_path first.
    """
    out = tmp_path_factory.mktemp("corpus")
    manifest = generate_corpus(out)
    return out, manifest
