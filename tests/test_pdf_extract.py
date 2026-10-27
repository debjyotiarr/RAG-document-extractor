"""Tests for page-level text extraction, OCR fallback and failure paths (milestone M1)."""

from __future__ import annotations

import json

import pymupdf
import pytest
from conftest import requires_tesseract

from docintel import pdf_extract
from docintel.pdf_extract import PdfOpenError, extract_pages, ocr_pages, text_quality


def manifest_doc(manifest, filename):
    return next(d for d in manifest["documents"] if d["filename"] == filename)


@pytest.mark.parametrize(
    "text, expected",
    [("", "empty"), ("  \n- / ", "empty"), ("abc", "empty"), ("Page 2", "low"), ("x" * 32, "ok")],
)
def test_text_quality_thresholds(text, expected):
    assert text_quality(text, min_chars=32) == expected


def test_digital_pdf_pages_text_and_words(corpus):
    directory, manifest = corpus
    doc = manifest_doc(manifest, "invoice_northwind_inv-0045.pdf")
    pages = extract_pages(directory / doc["filename"], min_chars=32)

    assert [p.page_number for p in pages] == [1, 2]
    assert all(p.method == "pymupdf" and p.quality == "ok" for p in pages)
    total = doc["fields"]["total_minor"]
    assert total["raw"] in pages[total["page"] - 1].text
    assert "Invoice No: INV-0045" in pages[0].text

    words = pages[0].words
    assert any(w[4] == "INV-0045" for w in words)
    for x0, y0, x1, y1, word, block, line in words:
        assert 0 <= x0 <= x1 <= 1 and 0 <= y0 <= y1 <= 1
        assert isinstance(word, str) and isinstance(block, int) and isinstance(line, int)
    assert json.loads(pages[0].words_json) == words
    assert pages[0].char_count == len(pages[0].text)


def test_scanned_pdf_has_empty_text_layer(corpus):
    directory, manifest = corpus
    (scan,) = [d for d in manifest["documents"] if d["kind"] == "scanned"]
    (page,) = extract_pages(directory / scan["filename"], min_chars=32)
    assert page.quality == "empty" and page.words == []


def test_corrupt_pdf_raises(corpus):
    directory, _ = corpus
    with pytest.raises(PdfOpenError):
        extract_pages(directory / "corrupt_upload.pdf", min_chars=32)


def test_non_pdf_with_pdf_extension_raises(tmp_path):
    fake = tmp_path / "notes.pdf"
    fake.write_text("this is a text file, not a PDF")
    with pytest.raises(PdfOpenError):
        extract_pages(fake, min_chars=32)


def test_encrypted_pdf_raises(corpus, tmp_path):
    directory, _ = corpus
    locked = tmp_path / "locked.pdf"
    with pymupdf.open(directory / "po_po-2026-0003.pdf") as doc:
        doc.save(locked, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
    with pytest.raises(PdfOpenError, match="password"):
        extract_pages(locked, min_chars=32)


def test_ocr_unavailable_reason_without_tesseract(monkeypatch):
    monkeypatch.setattr(pdf_extract.shutil, "which", lambda name: None)
    assert "Tesseract is not installed" in pdf_extract.ocr_unavailable_reason()


@requires_tesseract
def test_ocr_unavailable_reason_for_missing_language():
    assert "language data missing for xxx" in pdf_extract.ocr_unavailable_reason("eng+xxx")


@requires_tesseract
def test_ocr_reads_the_scanned_invoice(corpus):
    directory, manifest = corpus
    (scan,) = [d for d in manifest["documents"] if d["kind"] == "scanned"]
    path = directory / scan["filename"]
    (page,) = ocr_pages(path, extract_pages(path, min_chars=32), "eng", min_chars=32)

    assert page.method == "ocr" and page.quality == "ok"
    for name in ("record_number", "total_minor", "counterparty_tax_id"):
        assert scan["fields"][name]["raw"] in page.text, name
    assert any(w[4] == "INV-0046" for w in page.words)


@requires_tesseract
def test_ocr_keeps_digital_text_on_low_text_pages(tmp_path):
    # A page with a little real text and no images: image-only OCR must not drop the digital text.
    path = tmp_path / "short.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "Page 2 of 2")
        doc.save(path)
    (low,) = extract_pages(path, min_chars=32)
    assert low.quality == "low"
    (page,) = ocr_pages(path, [low], "eng", min_chars=32)
    assert "Page 2 of 2" in page.text
