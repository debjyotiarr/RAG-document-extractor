"""Tests for vault discovery, hashing, idempotency and the schema (milestones M0-M1)."""

from __future__ import annotations

import sqlite3

import pytest

from docintel import db

EXPECTED_TABLES = {"documents", "document_pages", "records", "field_evidence", "pages_fts", "query_log"}


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "app.db")
    yield connection
    connection.close()


def add_document(conn, document_id="doc_000000000001", status="discovered"):
    conn.execute(
        "INSERT INTO documents (document_id, file_hash, filename, relative_path, file_size, file_mtime, processing_status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
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
