"""Tests for vault discovery, hashing, idempotency and the schema (milestones M0-M1)."""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest
from conftest import fake_ocr, no_ocr, ocr_ok, requires_tesseract

from docintel import db
from docintel import pdf_extract as pdf_extract_module
from docintel.ingest import VaultError, coverage, discover, ingest

EXPECTED_TABLES = {"documents", "document_pages", "records", "field_evidence", "pages_fts", "query_log"}


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "app.db")
    yield connection
    connection.close()


def add_document(conn, document_id="doc_000000000001", status="discovered"):
    conn.execute(
        "INSERT INTO documents (document_id, file_hash, filename, relative_path, file_size, file_mtime,"
        " processing_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (document_id, document_id + "hash", "a.pdf", "a.pdf", 10, "2026-01-01T00:00:00", status),
    )


def add_page(conn, text, document_id="doc_000000000001", page_number=1):
    cur = conn.execute(
        "INSERT INTO document_pages (document_id, page_number, page_text, extraction_method, char_count, text_quality)"
        " VALUES (?, ?, ?, 'pymupdf', ?, 'ok')",
        (document_id, page_number, text, len(text)),
    )
    return cur.lastrowid


def fts_hits(conn, query):
    return [r[0] for r in conn.execute("SELECT rowid FROM pages_fts WHERE pages_fts MATCH ?", (query,))]


def test_schema_creates_all_tables_and_sets_version(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table')")}
    assert EXPECTED_TABLES <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_schema_init_is_idempotent(tmp_path):
    path = tmp_path / "app.db"
    db.connect(path).close()
    conn = db.connect(path)  # second open must not try to re-create tables
    db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_schema_creates_parent_directory(tmp_path):
    db.connect(tmp_path / "nested" / "dir" / "app.db").close()
    assert (tmp_path / "nested" / "dir" / "app.db").is_file()


def test_schema_newer_than_code_is_refused(tmp_path):
    path = tmp_path / "app.db"
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    raw.close()
    with pytest.raises(db.SchemaVersionError):
        db.connect(path)


@pytest.mark.parametrize(
    "sql, params",
    [
        (  # unknown processing status
            "INSERT INTO documents (document_id, file_hash, filename, relative_path, file_size, file_mtime,"
            " processing_status) VALUES ('d', 'h', 'f', 'f', 1, 't', 'done')",
            (),
        ),
        (  # unknown document type
            "INSERT INTO documents (document_id, file_hash, filename, relative_path, file_size, file_mtime,"
            " processing_status, document_type) VALUES ('d', 'h', 'f', 'f', 1, 't', 'discovered', 'receipt')",
            (),
        ),
        (  # page numbers are 1-based
            "INSERT INTO document_pages (document_id, page_number, page_text, extraction_method, char_count,"
            " text_quality) VALUES ('doc_000000000001', 0, 'x', 'pymupdf', 1, 'ok')",
            (),
        ),
        (  # unknown validation status
            "INSERT INTO records (document_id, record_type, validation_status)"
            " VALUES ('doc_000000000001', 'invoice', 'ok')",
            (),
        ),
        (  # 'other' is a document type, not a record type
            "INSERT INTO records (document_id, record_type, validation_status)"
            " VALUES ('doc_000000000001', 'other', 'valid')",
            (),
        ),
    ],
)
def test_schema_check_constraints_reject_bad_values(conn, sql, params):
    add_document(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)


def test_schema_enforces_unique_hash_and_page(conn):
    add_document(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO documents (document_id, file_hash, filename, relative_path, file_size, file_mtime,"
            " processing_status) VALUES ('other_id', 'doc_000000000001hash', 'b.pdf', 'b.pdf', 1, 't', 'discovered')"
        )
    add_page(conn, "first")
    with pytest.raises(sqlite3.IntegrityError):
        add_page(conn, "duplicate page number")


def test_schema_record_number_is_not_unique(conn):
    # The same invoice number from two suppliers is an ambiguity case, not an error.
    add_document(conn)
    for _ in range(2):
        conn.execute(
            "INSERT INTO records (document_id, record_type, record_number, validation_status)"
            " VALUES ('doc_000000000001', 'invoice', 'INV-0044', 'valid')"
        )


def test_schema_foreign_keys_and_cascade(conn):
    with pytest.raises(sqlite3.IntegrityError):
        add_page(conn, "orphan page", document_id="doc_missing")
    add_document(conn)
    page_id = add_page(conn, "net thirty days")
    record_id = conn.execute(
        "INSERT INTO records (document_id, record_type, validation_status)"
        " VALUES ('doc_000000000001', 'invoice', 'valid')"
    ).lastrowid
    conn.execute(
        "INSERT INTO field_evidence (record_id, field_name, extraction_method, validation_status)"
        " VALUES (?, 'total_minor', 'rule', 'valid')",
        (record_id,),
    )
    # Deleting a document removes everything derived from it, including its FTS entries.
    conn.execute("DELETE FROM documents WHERE document_id = 'doc_000000000001'")
    for table in ("document_pages", "records", "field_evidence"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert page_id not in fts_hits(conn, "thirty")


def test_schema_fts_stays_in_sync_with_pages(conn):
    add_document(conn)
    page_id = add_page(conn, "Payment Terms: Net 30")
    assert fts_hits(conn, "net") == [page_id]

    conn.execute("UPDATE document_pages SET page_text = 'Payable within 14 days' WHERE page_id = ?", (page_id,))
    assert fts_hits(conn, "net") == []
    assert fts_hits(conn, "payable") == [page_id]

    conn.execute("DELETE FROM document_pages WHERE page_id = ?", (page_id,))
    assert fts_hits(conn, "payable") == []


def test_schema_fts_survives_vacuum(conn):
    add_document(conn)
    add_page(conn, "alpha", page_number=1)
    second = add_page(conn, "bravo", page_number=2)
    conn.execute("DELETE FROM document_pages WHERE page_number = 1")
    conn.commit()
    conn.execute("VACUUM")
    row = conn.execute(
        "SELECT p.page_number FROM pages_fts JOIN document_pages p ON p.page_id = pages_fts.rowid"
        " WHERE pages_fts MATCH 'bravo'"
    ).fetchone()
    assert row["page_number"] == 2
    assert fts_hits(conn, "bravo") == [second]


def test_schema_readonly_connection_rejects_writes(tmp_path):
    path = tmp_path / "app.db"
    writer = db.connect(path)
    add_document(writer)
    writer.commit()
    writer.close()

    reader = db.connect_readonly(path)
    assert reader.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("DELETE FROM documents")
    with pytest.raises(sqlite3.OperationalError):
        reader.execute("CREATE TABLE sneaky (x)")
    reader.close()


def test_schema_readonly_connection_requires_existing_current_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        db.connect_readonly(tmp_path / "missing.db")
    old = tmp_path / "old.db"
    sqlite3.connect(old).close()  # version 0: never initialised
    with pytest.raises(db.SchemaVersionError):
        db.connect_readonly(old)
    assert not (tmp_path / "missing.db").exists()


def test_schema_migrates_v1_database_to_add_word_positions(tmp_path):
    path = tmp_path / "v1.db"
    raw = sqlite3.connect(path)
    raw.executescript(db._SCHEMA_V1 + "\nPRAGMA user_version = 1;")
    raw.close()
    conn = db.connect(path)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(document_pages)")}
    assert "words_json" in columns
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


# --------------------------------------------------------------------------- ingestion (M1)


def run(config, **kwargs):
    kwargs.setdefault("ocr_check", ocr_ok)
    kwargs.setdefault("ocr_run", fake_ocr)
    return ingest(config, **kwargs)


def doc_rows(config):
    conn = db.connect(config.db_path)
    rows = {r["relative_path"]: dict(r) for r in conn.execute("SELECT * FROM documents")}
    conn.close()
    return rows


def table_dump(config):
    conn = db.connect(config.db_path)
    dump = {
        t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1")]
        for t in ("documents", "document_pages")
    }
    conn.close()
    return dump


def pages_of(config, filename):
    conn = db.connect(config.db_path)
    rows = conn.execute(
        "SELECT p.* FROM document_pages p JOIN documents d USING (document_id) WHERE d.filename = ?"
        " ORDER BY page_number",
        (filename,),
    ).fetchall()
    conn.close()
    return rows


def fts_files(config, query):
    conn = db.connect(config.db_path)
    rows = conn.execute(
        "SELECT DISTINCT d.filename FROM pages_fts f JOIN document_pages p ON p.page_id = f.rowid"
        " JOIN documents d USING (document_id) WHERE pages_fts MATCH ? ORDER BY 1",
        (query,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def test_ingest_accounts_for_every_file(config, corpus):
    _, manifest = corpus
    report = run(config)
    rows = doc_rows(config)

    assert report.files_on_disk == len(manifest["documents"]) == len(rows) == len(report.new)
    for doc in manifest["documents"]:
        row = rows[doc["filename"]]
        assert row["document_id"] == "doc_" + row["file_hash"][:12]
        expected = {"corrupt": "failed", "scanned": "ocr_completed", "digital": "text_extracted"}[doc["kind"]]
        assert row["processing_status"] == expected, doc["filename"]
        assert row["page_count"] == doc["page_count"]
        assert row["last_processed_at"]

    corrupt = rows["corrupt_upload.pdf"]
    assert corrupt["error_message"].startswith("cannot open as PDF")
    assert pages_of(config, "corrupt_upload.pdf") == []


def test_ingest_stores_pages_words_and_search_index(config):
    run(config)
    pages = pages_of(config, "invoice_northwind_inv-0045.pdf")
    assert [p["page_number"] for p in pages] == [1, 2]
    assert "Total Due" in pages[1]["page_text"] and "Total Due" not in pages[0]["page_text"]
    assert all(p["extraction_method"] == "pymupdf" and p["text_quality"] == "ok" for p in pages)
    assert any(w[4] == "INV-0045" for w in json.loads(pages[0]["words_json"]))

    northwind = {f"invoice_northwind_inv-00{n}.pdf" for n in (41, 42, 43, 44, 45, 47)}
    assert set(fts_files(config, '"Net 30"')) == northwind  # the scan's fake OCR text has no terms


def test_ingest_scanned_page_gets_ocr_text(config):
    run(config)
    (page,) = pages_of(config, "invoice_northwind_inv-0046_scan.pdf")
    assert page["extraction_method"] == "ocr" and page["text_quality"] == "ok"
    assert "INV-0046" in page["page_text"]
    assert fts_files(config, "FAKE") == ["invoice_northwind_inv-0046_scan.pdf"]


def test_ingest_without_ocr_reports_ocr_required_and_incomplete(config):
    run(config, ocr_check=no_ocr)
    row = doc_rows(config)["invoice_northwind_inv-0046_scan.pdf"]
    assert row["processing_status"] == "ocr_required"
    assert "1 page(s) need OCR" in row["error_message"] and "not installed" in row["error_message"]
    # The empty page is still stored, so the document is known but marked unreadable.
    assert pages_of(config, "invoice_northwind_inv-0046_scan.pdf")[0]["text_quality"] == "empty"

    cov = coverage(config)
    assert cov.ocr_required == [("invoice_northwind_inv-0046_scan.pdf", row["error_message"])]
    assert not cov.is_complete


def test_ingest_ocr_disabled_in_config_is_reported(config):
    config.ocr.enabled = False
    report = run(config)
    assert report.statuses["invoice_northwind_inv-0046_scan.pdf"] == "ocr_required"
    assert "disabled" in doc_rows(config)["invoice_northwind_inv-0046_scan.pdf"]["error_message"]


def test_ingest_ocr_crash_is_recorded_not_raised(config):
    def broken_ocr(*args):
        raise RuntimeError("tesseract exploded")

    run(config, ocr_run=broken_ocr)
    row = doc_rows(config)["invoice_northwind_inv-0046_scan.pdf"]
    assert row["processing_status"] == "ocr_required"
    assert row["error_message"] == "OCR failed: RuntimeError: tesseract exploded"


def test_ingest_ocr_finding_nothing_fails_the_document(config):
    def blank_ocr(path, pages, language, min_chars):
        return [p for p in pages]  # still empty after "OCR"

    run(config, ocr_run=blank_ocr)
    row = doc_rows(config)["invoice_northwind_inv-0046_scan.pdf"]
    assert row["processing_status"] == "failed"
    assert "even after OCR" in row["error_message"]


def test_ingest_rerun_on_unchanged_vault_is_a_no_op(config):
    run(config)
    before = table_dump(config)
    report = run(config)
    assert report.new == report.reprocessed == report.removed == []
    assert len(report.unchanged) == report.files_on_disk
    assert table_dump(config) == before


def test_ingest_retries_ocr_required_once_ocr_is_available(config):
    run(config, ocr_check=no_ocr)
    report = run(config)
    assert report.reprocessed == ["invoice_northwind_inv-0046_scan.pdf"]
    assert doc_rows(config)["invoice_northwind_inv-0046_scan.pdf"]["processing_status"] == "ocr_completed"
    # Still no OCR -> no retry, nothing touched.
    run(config, ocr_check=no_ocr)
    assert run(config, ocr_check=no_ocr).reprocessed == []


def test_ingest_does_not_retry_failed_files_unless_forced(config):
    run(config)
    assert "corrupt_upload.pdf" in run(config).unchanged
    report = run(config, force=True)
    assert sorted(report.reprocessed) == sorted(report.statuses) and len(report.reprocessed) == report.files_on_disk
    assert report.statuses["corrupt_upload.pdf"] == "failed"


def test_ingest_removes_documents_deleted_from_vault(config, vault):
    run(config)
    (vault / "invoice_northwind_inv-0047.pdf").unlink()
    assert coverage(config).missing_from_disk == ["invoice_northwind_inv-0047.pdf"]

    report = run(config)
    assert report.removed == ["invoice_northwind_inv-0047.pdf"]
    assert "invoice_northwind_inv-0047.pdf" not in doc_rows(config)
    assert pages_of(config, "invoice_northwind_inv-0047.pdf") == []
    assert fts_files(config, "warranty") == []  # only INV-0047 mentions a warranty


def test_ingest_changed_file_replaces_old_version(config, vault):
    run(config)
    target = vault / "po_po-2026-0009.pdf"
    old_id = doc_rows(config)["po_po-2026-0009.pdf"]["document_id"]
    target.write_bytes(target.read_bytes() + b"\n%edited")  # same name, new content
    assert coverage(config).not_ingested == ["po_po-2026-0009.pdf"]  # detected before ingesting

    report = run(config)
    rows = doc_rows(config)
    assert report.new == ["po_po-2026-0009.pdf"]
    assert report.removed == ["po_po-2026-0009.pdf"]  # the old version's row
    assert rows["po_po-2026-0009.pdf"]["document_id"] != old_id
    assert old_id not in {r["document_id"] for r in rows.values()}
    assert len(rows) == report.files_on_disk


def test_ingest_rename_keeps_document_without_reprocessing(config, vault):
    run(config)
    old = doc_rows(config)["po_po-2026-0005.pdf"]
    (vault / "orders").mkdir()
    (vault / "po_po-2026-0005.pdf").rename(vault / "orders" / "renamed.pdf")

    report = run(config)
    assert report.renamed == [("po_po-2026-0005.pdf", "orders/renamed.pdf")]
    assert report.reprocessed == [] and report.removed == []
    new = doc_rows(config)["orders/renamed.pdf"]
    assert new["document_id"] == old["document_id"] and new["filename"] == "renamed.pdf"
    assert new["last_processed_at"] == old["last_processed_at"]


def test_ingest_identical_copies_are_stored_once(config, vault):
    shutil.copy2(vault / "po_po-2026-0007.pdf", vault / "copy_of_po.pdf")
    report = run(config)
    assert report.duplicates == [("po_po-2026-0007.pdf", "copy_of_po.pdf")]
    assert len(doc_rows(config)) == report.files_on_disk - 1
    assert coverage(config).is_complete is False  # the corrupt file


def test_ingest_unexpected_error_fails_one_document_only(config, monkeypatch):
    real = pdf_extract_module.extract_pages

    def flaky(path, min_chars):
        if path.name == "po_po-2026-0003.pdf":
            raise ValueError("boom")
        return real(path, min_chars)

    monkeypatch.setattr(pdf_extract_module, "extract_pages", flaky)
    report = run(config)
    rows = doc_rows(config)
    assert rows["po_po-2026-0003.pdf"]["processing_status"] == "failed"
    assert rows["po_po-2026-0003.pdf"]["error_message"] == "ValueError: boom"
    assert report.statuses["po_po-2026-0005.pdf"] == "text_extracted"


def test_discover_finds_nested_and_uppercase_pdfs_and_skips_hidden(tmp_path):
    for rel in ("a.pdf", "sub/b.PDF", "sub/deeper/c.pdf", ".hidden.pdf", ".cache/d.pdf", "notes.txt", "sub/e.pdf.bak"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    found = [p.relative_to(tmp_path).as_posix() for p in discover(tmp_path)]
    assert found == ["a.pdf", "sub/b.PDF", "sub/deeper/c.pdf"]


def test_ingest_missing_vault_raises(config, tmp_path):
    config.vault_dir = tmp_path / "does-not-exist"
    with pytest.raises(VaultError, match="not found"):
        run(config)


def test_coverage_is_complete_only_when_everything_is_readable(config, vault):
    (vault / "corrupt_upload.pdf").unlink()
    run(config)
    cov = coverage(config)
    assert cov.is_complete and cov.documents == cov.files_on_disk == 17
    assert cov.failed == cov.ocr_required == cov.not_ingested == []

    shutil.copy2(vault / "po_po-2026-0003.pdf", vault / "late_arrival.pdf")
    (vault / "late_arrival.pdf").write_bytes((vault / "late_arrival.pdf").read_bytes() + b"\n%extra")
    cov = coverage(config)
    assert cov.not_ingested == ["late_arrival.pdf"] and not cov.is_complete


@requires_tesseract
def test_ingest_with_real_ocr(config):
    ingest(config)  # real Tesseract
    (page,) = pages_of(config, "invoice_northwind_inv-0046_scan.pdf")
    assert page["extraction_method"] == "ocr" and "INV-0046" in page["page_text"]
    assert "invoice_northwind_inv-0046_scan.pdf" in fts_files(config, '"Net 30"')
