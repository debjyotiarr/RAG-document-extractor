"""Vault discovery, SHA-256 hashing and the document status machine (BLUEPRINT §8.1).

The database mirrors the vault: every PDF on disk has a `documents` row, and
rows whose file is no longer on disk are deleted (with their pages, records and
search entries). A document is identified by its content hash, so a renamed file
is the same document, and an edited file is a new one.

No silent drops: every file ends up in a status, and every failure keeps its
error message so the coverage report can say exactly what was not searched.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from docintel import db, pdf_extract
from docintel.config import Config
from docintel.pdf_extract import PageText, PdfOpenError

# Statuses that mean the vault cannot be fully searched (BLUEPRINT G4).
INCOMPLETE_STATUSES = ("discovered", "ocr_required", "failed")


class VaultError(Exception):
    """The vault folder is missing or not a folder."""


@dataclass
class IngestReport:
    files_on_disk: int = 0
    new: list[str] = field(default_factory=list)
    reprocessed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)  # (old path, new path)
    duplicates: list[tuple[str, str]] = field(default_factory=list)  # (path, identical to path)
    removed: list[str] = field(default_factory=list)  # paths whose content left the vault
    statuses: dict[str, str] = field(default_factory=dict)  # path -> status, for files processed this run


@dataclass
class Coverage:
    files_on_disk: int
    documents: int
    by_status: dict[str, int]
    failed: list[tuple[str, str]]  # (path, error)
    ocr_required: list[tuple[str, str]]
    not_ingested: list[str]  # on disk but not in the database (or changed since)
    missing_from_disk: list[str]  # in the database but no longer on disk

    @property
    def is_complete(self) -> bool:
        """True when every PDF in the vault has been read and indexed (BLUEPRINT G4)."""
        return (
            not self.not_ingested
            and not self.missing_from_disk
            and not any(self.by_status.get(s) for s in INCOMPLETE_STATUSES)
        )


def discover(vault_dir: Path) -> list[Path]:
    """All PDFs under the vault, recursively, in a stable order. Hidden files and folders are skipped."""
    if not vault_dir.is_dir():
        raise VaultError(f"Vault folder not found: {vault_dir}")
    found = []
    for path in vault_dir.rglob("*"):
        rel = path.relative_to(vault_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if path.suffix.lower() == ".pdf" and path.is_file():
            found.append(path)
    return sorted(found, key=lambda p: p.relative_to(vault_dir).as_posix())


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def document_id_for(file_hash: str) -> str:
    return "doc_" + file_hash[:12]


def ingest(
    config: Config,
    *,
    force: bool = False,
    on_progress: Callable[[str, str], None] | None = None,
    ocr_check: Callable[[str], str | None] | None = None,
    ocr_run: Callable[..., list[PageText]] | None = None,
) -> IngestReport:
    """Bring the database in line with the vault.

    New and changed files are processed; unchanged files are skipped unless
    `force` is set. Documents waiting for OCR are retried when OCR has become
    available. `ocr_check` and `ocr_run` default to pdf_extract's real OCR and
    are injectable for tests.
    """
    ocr_check = ocr_check or pdf_extract.ocr_unavailable_reason
    ocr_run = ocr_run or pdf_extract.ocr_pages
    vault = config.vault_dir
    files = discover(vault)
    report = IngestReport(files_on_disk=len(files))
    ocr_problem = ocr_check(config.ocr.language) if config.ocr.enabled else "OCR is disabled in config"

    conn = db.connect(config.db_path)
    try:
        seen: dict[str, str] = {}  # file_hash -> relative path
        for path in files:
            rel = path.relative_to(vault).as_posix()
            file_hash = file_sha256(path)
            if file_hash in seen:
                report.duplicates.append((rel, seen[file_hash]))
                continue
            seen[file_hash] = rel

            row = conn.execute(
                "SELECT document_id, relative_path, processing_status FROM documents WHERE file_hash = ?",
                (file_hash,),
            ).fetchone()
            if row is None:
                doc_id = document_id_for(file_hash)
                with conn:
                    conn.execute(
                        "INSERT INTO documents (document_id, file_hash, filename, relative_path, file_size,"
                        " file_mtime, processing_status) VALUES (?, ?, ?, ?, ?, ?, 'discovered')",
                        (doc_id, file_hash, path.name, rel, *_stat(path)),
                    )
                report.new.append(rel)
            else:
                doc_id = row["document_id"]
                if row["relative_path"] != rel:
                    with conn:
                        conn.execute(
                            "UPDATE documents SET filename = ?, relative_path = ? WHERE document_id = ?",
                            (path.name, rel, doc_id),
                        )
                    report.renamed.append((row["relative_path"], rel))
                status = row["processing_status"]
                retry = status == "discovered" or (status == "ocr_required" and ocr_problem is None)
                if not (force or retry):
                    report.unchanged.append(rel)
                    continue
                report.reprocessed.append(rel)

            status = _process(conn, doc_id, path, config, ocr_problem, ocr_run)
            report.statuses[rel] = status
            if on_progress:
                on_progress(rel, status)

        report.removed = _remove_missing(conn, set(seen))
    finally:
        conn.close()
    return report


def coverage(config: Config) -> Coverage:
    """What has and has not been read, comparing the database against the vault on disk."""
    files = discover(config.vault_dir)
    on_disk = {file_sha256(p): p.relative_to(config.vault_dir).as_posix() for p in files}
    conn = db.connect(config.db_path)
    try:
        rows = conn.execute(
            "SELECT file_hash, relative_path, processing_status, error_message FROM documents ORDER BY relative_path"
        ).fetchall()
    finally:
        conn.close()
    in_db = {r["file_hash"] for r in rows}
    present = [r for r in rows if r["file_hash"] in on_disk]
    by_status: dict[str, int] = {}
    for r in present:
        by_status[r["processing_status"]] = by_status.get(r["processing_status"], 0) + 1
    return Coverage(
        files_on_disk=len(files),
        documents=len(present),
        by_status=by_status,
        failed=[(r["relative_path"], r["error_message"]) for r in present if r["processing_status"] == "failed"],
        ocr_required=[
            (r["relative_path"], r["error_message"]) for r in present if r["processing_status"] == "ocr_required"
        ],
        not_ingested=sorted(rel for h, rel in on_disk.items() if h not in in_db),
        missing_from_disk=[r["relative_path"] for r in rows if r["file_hash"] not in on_disk],
    )


def _process(
    conn: sqlite3.Connection,
    doc_id: str,
    path: Path,
    config: Config,
    ocr_problem: str | None,
    ocr_run: Callable[..., list[PageText]],
) -> str:
    """Extract one document's pages and record the outcome. Never raises for a bad file."""
    min_chars = config.ocr.min_chars_per_page
    error = None
    try:
        pages = pdf_extract.extract_pages(path, min_chars)
    except PdfOpenError as exc:
        pages, status, error = [], "failed", str(exc)
    except Exception as exc:  # unexpected: still record it rather than drop the file
        pages, status, error = [], "failed", f"{type(exc).__name__}: {exc}"
    else:
        status = "text_extracted"
        weak = [p for p in pages if p.quality != "ok"]
        if weak:
            if ocr_problem:
                status, error = "ocr_required", f"{len(weak)} page(s) need OCR. {ocr_problem}"
            else:
                try:
                    redone = {p.page_number: p for p in ocr_run(path, weak, config.ocr.language, min_chars)}
                except Exception as exc:
                    status, error = "ocr_required", f"OCR failed: {type(exc).__name__}: {exc}"
                else:
                    pages = [redone.get(p.page_number, p) for p in pages]
                    status = "ocr_completed"
                    if all(p.quality == "empty" for p in pages):
                        # Nothing searchable: this must block not_found answers, so it is a failure.
                        status, error = "failed", "no text found on any page, even after OCR"

    with conn:
        conn.execute("DELETE FROM document_pages WHERE document_id = ?", (doc_id,))
        conn.executemany(
            "INSERT INTO document_pages (document_id, page_number, page_text, extraction_method, char_count,"
            " text_quality, words_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(doc_id, p.page_number, p.text, p.method, p.char_count, p.quality, p.words_json) for p in pages],
        )
        size, mtime = _stat(path)
        conn.execute(
            "UPDATE documents SET page_count = ?, processing_status = ?, error_message = ?, file_size = ?,"
            " file_mtime = ?, last_processed_at = ? WHERE document_id = ?",
            (len(pages) or None, status, error, size, mtime, _now(), doc_id),
        )
    return status


def _remove_missing(conn: sqlite3.Connection, hashes_on_disk: set[str]) -> list[str]:
    rows = conn.execute("SELECT document_id, file_hash, relative_path FROM documents").fetchall()
    gone = [r for r in rows if r["file_hash"] not in hashes_on_disk]
    with conn:
        # ON DELETE CASCADE removes pages, records and evidence; the FTS triggers clean the index.
        conn.executemany("DELETE FROM documents WHERE document_id = ?", [(r["document_id"],) for r in gone])
    return sorted(r["relative_path"] for r in gone)


def _stat(path: Path) -> tuple[int, str]:
    st = path.stat()
    return st.st_size, datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
