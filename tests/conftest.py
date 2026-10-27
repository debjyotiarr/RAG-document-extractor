"""Shared pytest fixtures."""

from __future__ import annotations

import shutil

import pytest
from fixtures.make_fixtures import generate_corpus

from docintel.config import DEFAULT_CONFIG_PATH, load_config
from docintel.pdf_extract import PageText, ocr_unavailable_reason

# Tests that run real Tesseract OCR are skipped where it is not installed.
requires_tesseract = pytest.mark.skipif(
    ocr_unavailable_reason() is not None, reason=ocr_unavailable_reason() or ""
)


@pytest.fixture(scope="session")
def corpus(tmp_path_factory):
    """The synthetic fixture corpus, generated once per test session.

    Returns (directory, manifest). Tests must not modify files in it; use the
    `vault` fixture for a private, writable copy.
    """
    out = tmp_path_factory.mktemp("corpus")
    manifest = generate_corpus(out)
    return out, manifest


@pytest.fixture
def vault(corpus, tmp_path):
    """A private copy of the corpus PDFs (without manifest.json) to ingest and modify."""
    directory, _ = corpus
    target = tmp_path / "vault"
    target.mkdir()
    for pdf in directory.glob("*.pdf"):
        shutil.copy2(pdf, target / pdf.name)
    return target


@pytest.fixture
def config(vault, tmp_path):
    """The repository config, pointed at the private vault and a fresh database."""
    return load_config(DEFAULT_CONFIG_PATH, vault_dir=vault, db_path=tmp_path / "app.db")


def fake_ocr(path, pages, language, min_chars):
    """Stand-in for pdf_extract.ocr_pages, so ingestion logic is testable without Tesseract."""
    return [
        PageText(
            page_number=p.page_number,
            text=f"FAKE OCR TEXT page {p.page_number}: Invoice No: INV-0046 Total Due: USD 483.31",
            words=[[0.1, 0.1, 0.3, 0.12, "FAKE", 0, 0]],
            method="ocr",
            quality="ok",
        )
        for p in pages
    ]


def no_ocr(language):
    return "OCR unavailable: Tesseract is not installed (test)"


def ocr_ok(language):
    return None
