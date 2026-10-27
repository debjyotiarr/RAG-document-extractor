"""Per-page text extraction with PyMuPDF and OCR fallback (BLUEPRINT §8.2).

Digital pages are read with PyMuPDF. Pages with too little text are OCR'd with
PyMuPDF's built-in Tesseract integration, which needs only the `tesseract`
binary and its language data (no OCRmyPDF or Ghostscript).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pymupdf

# Fewer alphanumeric characters than this and a page counts as empty.
_EMPTY_BELOW = 4
_OCR_DPI = 300


class PdfOpenError(Exception):
    """The file cannot be read as a PDF (corrupt, encrypted, not a PDF, no pages)."""


@dataclass
class PageText:
    page_number: int  # 1-based
    text: str
    # [x0, y0, x1, y1, word, block, line], coordinates normalised to 0..1 of the page.
    words: list[list]
    method: str  # 'pymupdf' | 'ocr'
    quality: str  # 'ok' | 'low' | 'empty'

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def words_json(self) -> str:
        return json.dumps(self.words, separators=(",", ":"))


def text_quality(text: str, min_chars: int) -> str:
    alnum = sum(c.isalnum() for c in text)
    if alnum < _EMPTY_BELOW:
        return "empty"
    return "low" if alnum < min_chars else "ok"


def extract_pages(path: Path | str, min_chars: int) -> list[PageText]:
    """Read every page's text layer. Raises PdfOpenError if the PDF is unreadable."""
    with _open(path) as doc:
        pages = []
        for index in range(doc.page_count):
            try:
                pages.append(_page_text(doc[index], None, "pymupdf", min_chars))
            except Exception as exc:  # a damaged page object; the file is not usable as a whole
                raise PdfOpenError(f"cannot read page {index + 1}: {exc}") from exc
        return pages


def ocr_unavailable_reason(language: str = "eng") -> str | None:
    """None if OCR can run; otherwise a message saying what to install."""
    if shutil.which("tesseract") is None:
        return "OCR unavailable: Tesseract is not installed (macOS: brew install tesseract)"
    try:
        tessdata = Path(pymupdf.get_tessdata())
    except Exception as exc:  # PyMuPDF raises if it cannot locate the language data
        return f"OCR unavailable: Tesseract language data not found ({exc}); set TESSDATA_PREFIX"
    missing = [lang for lang in language.split("+") if not (tessdata / f"{lang}.traineddata").is_file()]
    if missing:
        return f"OCR unavailable: Tesseract language data missing for {', '.join(missing)} in {tessdata}"
    return None


def ocr_pages(path: Path | str, pages: list[PageText], language: str, min_chars: int) -> list[PageText]:
    """OCR the given pages and return their replacements, in the same order.

    A page with no text layer at all is OCR'd in full. A page with a little
    digital text only has its images OCR'd, so the digital text (which is exact)
    is kept rather than replaced by OCR's guesses. Errors propagate to the caller.
    """
    results = []
    with _open(path) as doc:
        for old in pages:
            page = doc[old.page_number - 1]
            full = old.quality == "empty"
            textpage = page.get_textpage_ocr(language=language, dpi=_OCR_DPI, full=full)
            results.append(_page_text(page, textpage, "ocr", min_chars))
    return results


def _open(path: Path | str) -> pymupdf.Document:
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception as exc:
        raise PdfOpenError(f"cannot open as PDF: {exc}") from exc
    if doc.needs_pass:
        doc.close()
        raise PdfOpenError("PDF is password-protected")
    if doc.page_count == 0:
        doc.close()
        raise PdfOpenError("PDF has no pages")
    return doc


def _page_text(page: pymupdf.Page, textpage, method: str, min_chars: int) -> PageText:
    text = page.get_text("text", textpage=textpage)
    width, height = page.rect.width or 1, page.rect.height or 1
    words = [
        [round(x0 / width, 4), round(y0 / height, 4), round(x1 / width, 4), round(y1 / height, 4), word, block, line]
        for x0, y0, x1, y1, word, block, line, _ in page.get_text("words", textpage=textpage)
    ]
    return PageText(
        page_number=page.number + 1,
        text=text,
        words=words,
        method=method,
        quality=text_quality(text, min_chars),
    )
