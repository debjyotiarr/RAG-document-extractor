"""Shared pytest fixtures."""

from __future__ import annotations

import shutil

import pytest
from fixtures.make_fixtures import generate_corpus, generate_extra_layouts

from docintel.config import DEFAULT_CONFIG_PATH, load_config
from docintel.ingest import ingest
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


@pytest.fixture(scope="session")
def extra(tmp_path_factory):
    """Extra layouts for template tests (a third layout, a near-miss variant). Returns (directory, manifest)."""
    out = tmp_path_factory.mktemp("extra")
    return out, generate_extra_layouts(out)


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
    """The repository config, pointed at the private vault, a fresh database and a private templates folder."""
    cfg = load_config(DEFAULT_CONFIG_PATH, vault_dir=vault, db_path=tmp_path / "app.db")
    cfg.extraction.templates_dir = tmp_path / "templates"
    return cfg


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


class FakeLLM:
    """Plays the LLM from the corpus ground truth, copying values exactly as printed.

    `overrides` maps a filename to {llm_field_name: {"value": ..., "page": ...}} to
    script mistakes, such as a hallucinated value. `calls` records every request.
    """

    def __init__(self, manifest, overrides=None):
        self.docs = [d for d in manifest["documents"] if d["fields"]]
        self.overrides = overrides or {}
        self.calls: list[tuple[str, str | None]] = []

    def complete_json(self, system, user, schema):
        props = schema["properties"]
        doc = self._which(user)
        name = doc["filename"] if doc else None
        if "document_type" in props:
            self.calls.append(("classify", name))
            if doc is None:
                return {"document_type": "unknown", "confidence": "low"}
            return {"document_type": doc["document_type"], "confidence": "high"}
        self.calls.append(("extract", name))
        answer = {field: {"value": "", "page": 0} for field in props}
        if doc is not None:
            for field, truth in doc["fields"].items():
                answer[field.removesuffix("_minor")] = {"value": truth["raw"], "page": truth["page"]}
            answer.update(self.overrides.get(name, {}))
        return answer

    @property
    def extract_calls(self):
        return [name for kind, name in self.calls if kind == "extract"]

    def _which(self, text):
        def hits(doc):
            return sum(truth["raw"] in text for truth in doc["fields"].values())

        best = max(self.docs, key=hits)
        return best if hits(best) >= 3 else None


@pytest.fixture(scope="session")
def ingested(corpus, tmp_path_factory):
    """A fully ingested copy of the corpus (fake OCR, FakeLLM), shared read-only by tool tests.

    Returns the Config. Tests must not modify its vault or database.
    """
    directory, manifest = corpus
    root = tmp_path_factory.mktemp("ingested")
    vault = root / "vault"
    vault.mkdir()
    for pdf in directory.glob("*.pdf"):
        shutil.copy2(pdf, vault / pdf.name)
    cfg = load_config(DEFAULT_CONFIG_PATH, vault_dir=vault, db_path=root / "app.db")
    cfg.extraction.templates_dir = root / "templates"
    ingest(cfg, llm=FakeLLM(manifest), ocr_check=ocr_ok, ocr_run=fake_ocr)
    return cfg
