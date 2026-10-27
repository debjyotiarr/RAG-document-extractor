"""Tests for full-text retrieval and query sanitising (milestone M3)."""

from __future__ import annotations

import pytest

from docintel import db
from docintel.retrieval import FTSRetriever, search_terms


@pytest.fixture(scope="module")
def conn(ingested):
    connection = db.connect_readonly(ingested.db_path)
    yield connection
    connection.close()


def search(conn, query, **kwargs):
    return FTSRetriever().search(conn, query, **kwargs)


def test_search_terms_drop_operators_and_punctuation():
    assert search_terms('Net 30 OR "total" NEAR(x, y) *') == ["net", "30", "or", "total", "near", "x", "y"]
    assert search_terms("INV-0044") == ["inv", "0044"]
    assert search_terms("  :*()\"  ") == []


def test_exact_phrase_first(conn):
    result = search(conn, "Net 30")
    assert result.match_mode == "phrase"
    files = {h.filename for h in result.hits}
    assert files and all(name.startswith("invoice_northwind") for name in files)
    assert all("Payment Terms: [Net 30]" in h.snippet for h in result.hits)  # a phrase is marked as one span


def test_all_terms_when_words_are_not_adjacent(conn):
    result = search(conn, "warranty months")  # printed as "Warranty: 12 months"
    assert result.match_mode == "all_terms"
    assert [(h.filename, h.page) for h in result.hits] == [("invoice_northwind_inv-0047.pdf", 1)]


def test_any_term_as_last_resort(conn):
    result = search(conn, "warranty zebra")
    assert result.match_mode == "any_term"
    assert [h.filename for h in result.hits] == ["invoice_northwind_inv-0047.pdf"]


def test_no_hits(conn):
    result = search(conn, "zebra giraffe")
    assert (result.match_mode, result.hits) == ("none", [])


def test_identifier_search(conn):
    result = search(conn, "INV-0044")
    assert result.match_mode == "phrase"
    assert {h.filename for h in result.hits} == {"invoice_northwind_inv-0044.pdf", "invoice_bluefin_inv-0044.pdf"}


@pytest.mark.parametrize(
    "query",
    ['"', "NEAR(", "*", "AND OR NOT", "page_text:total", "total)", "^net", "{a b}", "'; DROP TABLE documents; --"],
)
def test_hostile_queries_never_error(conn, query):
    result = search(conn, query)
    assert result.match_mode in ("phrase", "all_terms", "any_term", "none")
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] > 0


def test_filters(conn):
    order_hits = search(conn, "Northwind Components", record_type="purchase_order").hits
    assert order_hits and all(h.filename.startswith("po_") for h in order_hits)
    doc_id = conn.execute(
        "SELECT document_id FROM documents WHERE filename = 'invoice_northwind_inv-0045.pdf'"
    ).fetchone()[0]
    hits = search(conn, "Total Due", document_id=doc_id).hits
    assert [(h.filename, h.page) for h in hits] == [("invoice_northwind_inv-0045.pdf", 2)]


def test_limit_and_ranking(conn):
    hits = search(conn, "invoice", limit=3).hits
    assert len(hits) == 3
    assert [h.score for h in hits] == sorted(h.score for h in hits)
