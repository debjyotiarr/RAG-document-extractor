"""SQLite schema, migrations and read-write / read-only connections (BLUEPRINT §7).

Ingestion uses `connect()` (read-write). The query agent uses only
`connect_readonly()`, so it cannot change the database even if a tool has a bug
(grounding invariant G5).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Bump this and add a step to `_MIGRATIONS` whenever the schema changes.
SCHEMA_VERSION = 3

DOCUMENT_TYPES = ("invoice", "purchase_order", "credit_note", "other", "unknown")
RECORD_TYPES = ("invoice", "purchase_order", "credit_note")
PROCESSING_STATUSES = (
    "discovered",
    "text_extracted",
    "ocr_required",
    "ocr_completed",
    "fields_extracted",
    "validated",
    "needs_review",
    "failed",
)
VALIDATION_STATUSES = ("valid", "warning", "needs_review")


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


_SCHEMA_V1 = f"""
CREATE TABLE documents (
  document_id       TEXT PRIMARY KEY,          -- 'doc_' + first 12 hex chars of sha256
  file_hash         TEXT NOT NULL UNIQUE,      -- full sha256
  filename          TEXT NOT NULL,
  relative_path     TEXT NOT NULL,             -- relative to the vault
  file_size         INTEGER NOT NULL,
  file_mtime        TEXT NOT NULL,             -- ISO-8601
  page_count        INTEGER,
  document_type     TEXT CHECK (document_type IN {_in(DOCUMENT_TYPES)}),
  processing_status TEXT NOT NULL CHECK (processing_status IN {_in(PROCESSING_STATUSES)}),
  error_message     TEXT,
  last_processed_at TEXT
);

-- page_id is an explicit INTEGER PRIMARY KEY (an alias for rowid) so that the
-- FTS index below keeps pointing at the right rows; a hidden rowid can be
-- renumbered by VACUUM.
CREATE TABLE document_pages (
  page_id           INTEGER PRIMARY KEY,
  document_id       TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
  page_number       INTEGER NOT NULL CHECK (page_number >= 1),   -- 1-based, user-facing
  page_text         TEXT,
  extraction_method TEXT NOT NULL CHECK (extraction_method IN ('pymupdf', 'ocr')),
  char_count        INTEGER NOT NULL,
  text_quality      TEXT NOT NULL CHECK (text_quality IN ('ok', 'low', 'empty')),
  UNIQUE (document_id, page_number)
);

-- One row per extracted business record (invoice or purchase order).
CREATE TABLE records (
  record_id              INTEGER PRIMARY KEY,
  document_id            TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
  record_type            TEXT NOT NULL CHECK (record_type IN {_in(RECORD_TYPES)}),
  counterparty_name_raw  TEXT,
  counterparty_name_norm TEXT,                 -- casefolded, whitespace collapsed
  counterparty_tax_id    TEXT,                 -- as printed; never inferred or repaired
  record_number          TEXT,                 -- TEXT: leading zeros matter
  record_date            TEXT,                 -- ISO-8601 or NULL; raw value is in field_evidence
  currency               TEXT,                 -- ISO-4217 or NULL; never assumed
  subtotal_minor         INTEGER,              -- minor units (cents, paise, ...)
  tax_minor              INTEGER,
  total_minor            INTEGER,
  payment_terms          TEXT,
  due_date               TEXT,
  po_reference           TEXT,                 -- on invoices: the PO they cite
  validation_status      TEXT NOT NULL CHECK (validation_status IN {_in(VALIDATION_STATUSES)})
);
CREATE INDEX idx_records_number   ON records(record_number);
CREATE INDEX idx_records_party    ON records(counterparty_name_norm);
CREATE INDEX idx_records_date     ON records(record_date);
CREATE INDEX idx_records_document ON records(document_id);
-- record_number is deliberately NOT unique: duplicates are an ambiguity case.

-- Per-field provenance (mandatory, BLUEPRINT D4).
CREATE TABLE field_evidence (
  evidence_id       INTEGER PRIMARY KEY,
  record_id         INTEGER NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
  field_name        TEXT NOT NULL,
  raw_value         TEXT,                      -- exactly as printed in the PDF
  normalized_value  TEXT,
  source_page       INTEGER,                   -- 1-based
  supporting_text   TEXT,                      -- up to 300 chars around the value
  extraction_method TEXT NOT NULL,             -- 'template:<id>' | 'rule' | 'llm' | 'llm_bootstrap'
  validation_status TEXT NOT NULL CHECK (validation_status IN {_in(VALIDATION_STATUSES)})
);
CREATE INDEX idx_evidence_record ON field_evidence(record_id, field_name);

-- Full-text index over page text, kept in sync by the triggers below.
CREATE VIRTUAL TABLE pages_fts USING fts5(
  page_text,
  content='document_pages',
  content_rowid='page_id'
);

CREATE TRIGGER document_pages_ai AFTER INSERT ON document_pages BEGIN
  INSERT INTO pages_fts(rowid, page_text) VALUES (new.page_id, new.page_text);
END;
CREATE TRIGGER document_pages_ad AFTER DELETE ON document_pages BEGIN
  INSERT INTO pages_fts(pages_fts, rowid, page_text) VALUES ('delete', old.page_id, old.page_text);
END;
CREATE TRIGGER document_pages_au AFTER UPDATE ON document_pages BEGIN
  INSERT INTO pages_fts(pages_fts, rowid, page_text) VALUES ('delete', old.page_id, old.page_text);
  INSERT INTO pages_fts(rowid, page_text) VALUES (new.page_id, new.page_text);
END;

CREATE TABLE query_log (
  query_id        INTEGER PRIMARY KEY,
  asked_at        TEXT NOT NULL,
  question        TEXT NOT NULL,
  tool_calls_json TEXT NOT NULL,               -- [{{tool, args, result_summary}}, ...]
  status          TEXT NOT NULL,
  answer          TEXT
);
"""

# v2 (M1): word positions per page, the input for layout fingerprints (BLUEPRINT §8.4).
# JSON list of [x0, y0, x1, y1, word, block, line], coordinates normalised to 0..1 of the page.
_SCHEMA_V2 = """
ALTER TABLE document_pages ADD COLUMN words_json TEXT;
"""

# v3 (M2): learned layout templates and field presence (BLUEPRINT §8.4).
_SCHEMA_V3 = """
ALTER TABLE documents ADD COLUMN template_id TEXT;
ALTER TABLE documents ADD COLUMN template_version INTEGER;
ALTER TABLE documents ADD COLUMN template_match TEXT
  CHECK (template_match IN ('match', 'near_miss', 'no_match'));

-- found = value extracted; absent = the template has a rule for the field and its
-- label is not on the page; not_covered = no rule for it. Only 'absent' can support
-- a not_found answer.
ALTER TABLE field_evidence ADD COLUMN presence TEXT NOT NULL DEFAULT 'found'
  CHECK (presence IN ('found', 'absent', 'not_covered'));

-- Lifecycle state of learned templates. The rules live in
-- <templates_dir>/<template_id>.v<version>.yaml; yaml_sha256 detects hand edits.
CREATE TABLE templates (
  template_id      TEXT NOT NULL,
  version          INTEGER NOT NULL,
  document_type    TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN ('probation', 'trusted', 'retired')),
  learned_from     TEXT REFERENCES documents(document_id) ON DELETE SET NULL,
  learned_from_file TEXT NOT NULL,
  docs_agreed      INTEGER NOT NULL DEFAULT 0,
  docs_disagreed   INTEGER NOT NULL DEFAULT 0,
  yaml_sha256      TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (template_id, version)
);
"""

# _MIGRATIONS[n] upgrades a database from version n to version n + 1.
_MIGRATIONS = {0: _SCHEMA_V1, 1: _SCHEMA_V2, 2: _SCHEMA_V3}


class SchemaVersionError(Exception):
    """The database was created by a newer version of docintel."""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a read-write connection, creating or upgrading the schema if needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    init_schema(conn)
    return conn


def connect_readonly(db_path: Path | str) -> sqlite3.Connection:
    """Open a read-only connection. Any write raises sqlite3.OperationalError."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path} (run ingestion first)")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.close()
        raise SchemaVersionError(
            f"Database schema is version {version}, expected {SCHEMA_VERSION}; run ingestion to upgrade"
        )
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create or upgrade the schema in place. Safe to call on every connection."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Database schema is version {version}, newer than this code ({SCHEMA_VERSION})"
        )
    while version < SCHEMA_VERSION:
        # executescript commits any open transaction first; wrap each step in its
        # own transaction so a failed migration leaves the previous version intact.
        try:
            conn.executescript(
                f"BEGIN;\n{_MIGRATIONS[version]}\nPRAGMA user_version = {version + 1};\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            raise
        version += 1
