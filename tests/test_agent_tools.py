"""Tests for the five agent tools, called directly without an LLM (milestone M3).

Expected values are computed from the corpus ground truth (manifest.json), not
typed in, so the tests check the tools against what is actually printed.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from decimal import ROUND_HALF_UP, Decimal

import pytest
from conftest import FakeLLM, fake_ocr, ocr_ok

from docintel import db
from docintel.cli import main
from docintel.config import DEFAULT_CONFIG_PATH, load_config
from docintel.ingest import ingest
from docintel.tools import ToolBox, tool_specs

SCAN = "invoice_northwind_inv-0046_scan.pdf"  # fake OCR in these tests: a record with no values


@pytest.fixture(scope="module")
def tools(ingested):
    with ToolBox(ingested) as box:
        yield box


@pytest.fixture(scope="module")
def truth(corpus):
    """Ground-truth fields of every digital invoice/order, by filename."""
    _, manifest = corpus
    return {
        d["filename"]: {f: t["value"] for f, t in d["fields"].items()} | {"type": d["document_type"]}
        for d in manifest["documents"]
        if d["kind"] == "digital" and d["document_type"] in ("invoice", "purchase_order")
    }


def call(tools, name, **args):
    result = tools.call(name, args)
    json.dumps(result)  # every result must be JSON-serialisable for the LLM
    return result


def money(minor):
    return f"{minor / 100:.2f}"


# ------------------------------------------------------------------ dispatcher


def test_tool_specs_are_flat_function_definitions():
    specs = tool_specs()
    assert [s["function"]["name"] for s in specs] == [
        "lookup_documents", "query_structured", "search_text", "get_evidence", "get_coverage",
    ]
    for spec in specs:
        params = spec["function"]["parameters"]
        assert spec["type"] == "function" and params["type"] == "object"
        assert "$defs" not in json.dumps(params) and "$ref" not in json.dumps(params)  # easy for a small model
    query = specs[1]["function"]["parameters"]
    assert query["required"] == ["operation"]
    assert set(query["properties"]["operation"]["enum"]) == {"count", "sum", "avg", "min", "max", "list"}


@pytest.mark.parametrize(
    "name, args, expected",
    [
        ("drop_tables", {}, "unknown_tool"),
        ("query_structured", {"operation": "explode"}, "invalid_arguments"),
        ("query_structured", {"operation": "sum", "field": "total; DROP TABLE records"}, "invalid_arguments"),
        ("query_structured", {"operation": "sum"}, "invalid_arguments"),  # sum needs an amount field
        ("query_structured", {"operation": "sum", "field": "counterparty"}, "invalid_arguments"),
        ("query_structured", {"operation": "min", "field": "payment_terms"}, "invalid_arguments"),
        ("lookup_documents", {"sql": "SELECT 1"}, "invalid_arguments"),  # unknown arguments are rejected
        ("lookup_documents", {"date_from": "last tuesday"}, "invalid_arguments"),
        ("search_text", {}, "invalid_arguments"),
        ("search_text", {"query": "x" * 500}, "invalid_arguments"),
        ("get_evidence", {"document_id": "doc_nope"}, "unknown_document"),
    ],
)
def test_bad_calls_return_errors_not_exceptions(tools, name, args, expected):
    assert call(tools, name, **args)["error"] == expected


def test_toolbox_connection_is_read_only(tools):
    with pytest.raises(sqlite3.OperationalError):
        tools.conn.execute("DELETE FROM records")


def test_every_result_reports_what_could_not_be_searched(tools):
    for name, args in [
        ("lookup_documents", {}), ("query_structured", {"operation": "count"}),
        ("search_text", {"query": "invoice"}), ("get_evidence", {"document_id": "po_po-2026-0003.pdf"}),
    ]:
        scope = call(tools, name, **args)["scope"]
        assert scope["not_searchable"] == ["corrupt_upload.pdf"]
        assert scope["documents"] == 18 and scope["fields_pending"] == []


# ------------------------------------------------------------ lookup_documents


def test_lookup_unique_number(tools, truth):
    result = call(tools, "lookup_documents", record_number="inv-0041 ")  # case and spaces don't matter
    assert (result["match"], result["count"]) == ("exact", 1)
    (m,) = result["matches"]
    t = truth[m["filename"]]
    assert m["filename"] == "invoice_northwind_inv-0041.pdf"
    assert (m["record_number"], m["record_date"], m["total"], m["currency"]) == (
        t["record_number"], t["record_date"], money(t["total_minor"]), t["currency"]
    )
    assert m["po_reference"] == "PO-2026-0003" and "ambiguous" not in result


def test_lookup_reports_ambiguity_instead_of_choosing(tools):
    result = call(tools, "lookup_documents", record_number="INV-0044")
    assert result["ambiguous"] is True and result["count"] == 2
    assert {m["counterparty"] for m in result["matches"]} == {"NORTHWIND COMPONENTS LTD", "Bluefin Office Supply GmbH"}
    narrowed = call(tools, "lookup_documents", record_number="INV-0044", counterparty="bluefin")
    assert narrowed["count"] == 1 and "ambiguous" not in narrowed


def test_lookup_partial_number_is_labelled_partial(tools):
    result = call(tools, "lookup_documents", record_number="0047")
    assert result["match"] == "partial"
    assert [m["filename"] for m in result["matches"]] == ["invoice_northwind_inv-0047.pdf"]


def test_lookup_nonexistent_number(tools):
    result = call(tools, "lookup_documents", record_number="INV-9999")
    assert (result["match"], result["count"], result["matches"]) == ("none", 0, [])


def test_lookup_filters(tools, truth):
    orders = call(tools, "lookup_documents", record_type="purchase_order")["matches"]
    assert sorted(m["filename"] for m in orders) == sorted(f for f, t in truth.items() if t["type"] == "purchase_order")
    march = call(tools, "lookup_documents", date_from="2026-03-01", date_to="2026-03-31")["matches"]
    assert sorted(m["filename"] for m in march) == sorted(
        f for f, t in truth.items() if "2026-03-01" <= t["record_date"] <= "2026-03-31"
    )
    citing = call(tools, "lookup_documents", po_reference="po-2026-0007")["matches"]
    assert [m["filename"] for m in citing] == ["invoice_northwind_inv-0043.pdf"]


def test_lookup_by_filename_includes_documents_without_records(tools):
    (m,) = call(tools, "lookup_documents", filename="delivery_note")["matches"]
    assert m["document_type"] == "other" and "no extracted fields" in m["note"]


def test_lookup_wildcards_are_literal(tools):
    # As SQL LIKE wildcards, "%" would match every document; taken literally it matches none.
    assert call(tools, "lookup_documents", counterparty="%")["count"] == 0
    assert call(tools, "lookup_documents", filename="%")["count"] == 0
    assert call(tools, "lookup_documents", filename="inv_0041")["count"] == 0  # "_" is not "any character"


def test_lookup_missing_field_skips_documents_without_records(tools):
    result = call(tools, "lookup_documents", record_type="invoice", missing=["due_date"])
    names = [m["filename"] for m in result["matches"]]
    assert "invoice_bluefin_inv-1003.pdf" in names
    assert "delivery_note_dn-5521.pdf" not in names and "corrupt_upload.pdf" not in names


# ------------------------------------------------------------ query_structured


def test_count(tools, truth):
    result = call(tools, "query_structured", operation="count", record_type="purchase_order")
    assert result["value"] == sum(1 for t in truth.values() if t["type"] == "purchase_order") == 4
    assert len(result["records"]) == 4 and all(r["document_id"].startswith("doc_") for r in result["records"])


def test_sum_refuses_to_mix_currencies(tools, truth):
    result = call(tools, "query_structured", operation="sum", field="total", record_type="invoice")
    assert result["error"] == "mixed_currencies" and "value" not in result
    assert result["currencies"] == ["EUR", "USD"]
    # The scan's fake OCR text prints its number and "Total Due: USD 483.31", so that total is real data too.
    scan_total = 48331
    for cur, extra in (("EUR", 0), ("USD", scan_total)):
        expected = [t["total_minor"] for t in truth.values() if t["type"] == "invoice" and t["currency"] == cur]
        assert result["per_currency"][cur]["value_minor"] == sum(expected) + extra
        assert result["per_currency"][cur]["records"] == len(expected) + (1 if extra else 0)


def test_records_without_the_value_are_excluded_and_listed(tools):
    result = call(tools, "query_structured", operation="sum", field="subtotal", record_type="invoice", currency="USD")
    assert [m["filename"] for m in result["missing_values"]] == [SCAN]  # no subtotal in its OCR text


def test_amount_without_currency_is_not_added_to_others(config, corpus):
    ingest(config, llm=FakeLLM(corpus[1]), ocr_check=ocr_ok, ocr_run=fake_ocr)
    conn = db.connect(config.db_path)
    with conn:  # simulate an invoice whose currency could not be read
        conn.execute("UPDATE records SET currency = NULL WHERE record_number = 'INV-1001'")
    conn.close()
    with ToolBox(config) as box:
        result = box.call("query_structured", {"operation": "sum", "field": "total", "counterparty": "bluefin",
                                               "record_type": "invoice"})
    assert result["error"] == "mixed_currencies" and result["currencies"] == ["(unknown)", "EUR"]


def test_sum_in_one_currency(tools, truth):
    result = call(tools, "query_structured", operation="sum", field="total", record_type="invoice", currency="eur")
    expected = [t["total_minor"] for t in truth.values() if t["type"] == "invoice" and t["currency"] == "EUR"]
    assert (result["value_minor"], result["value"], result["currency"]) == (sum(expected), money(sum(expected)), "EUR")
    assert result["records_included"] == len(expected) == 5
    assert "missing_values" not in result


def test_sum_by_counterparty_and_period(tools, truth):
    result = call(tools, "query_structured", operation="sum", field="total", record_type="invoice",
                  counterparty="northwind", date_from="2026-02-01", date_to="2026-03-31")
    expected = [
        t["total_minor"] for f, t in truth.items()
        if t["type"] == "invoice" and "northwind" in f and "2026-02-01" <= t["record_date"] <= "2026-03-31"
    ]
    assert result["value_minor"] == sum(expected) and result["records_included"] == len(expected) == 4


def test_avg_rounds_half_up_in_minor_units(tools, truth):
    result = call(tools, "query_structured", operation="avg", field="tax", record_type="invoice", currency="USD")
    taxes = [t["tax_minor"] for t in truth.values() if t["type"] == "invoice" and t["currency"] == "USD"]
    expected = int((Decimal(sum(taxes)) / len(taxes)).to_integral_value(rounding=ROUND_HALF_UP))
    assert result["value_minor"] == expected


def test_min_max_return_the_records_that_hold_them(tools, truth):
    result = call(tools, "query_structured", operation="max", field="total", record_type="purchase_order",
                  currency="USD")
    usd_orders = {f: t for f, t in truth.items() if t["type"] == "purchase_order" and t["currency"] == "USD"}
    best = max(usd_orders, key=lambda f: usd_orders[f]["total_minor"])
    assert result["value_minor"] == usd_orders[best]["total_minor"]
    assert [r["filename"] for r in result["records"]] == [best]

    earliest = call(tools, "query_structured", operation="min", field="record_date", record_type="invoice")
    first = min((t["record_date"], f) for f, t in truth.items() if t["type"] == "invoice")
    assert earliest["value"] == first[0] and [r["filename"] for r in earliest["records"]] == [first[1]]


def test_max_across_currencies_is_refused(tools):
    result = call(tools, "query_structured", operation="max", field="total", record_type="purchase_order")
    assert result["error"] == "mixed_currencies"


def test_list_is_ordered_and_limited(tools):
    result = call(tools, "query_structured", operation="list", record_type="invoice", limit=3)
    dates = [r["record_date"] for r in result["records"]]
    assert len(dates) == 3 and dates == sorted(dates) and result["truncated"] is True


def test_records_flagged_for_review_are_called_out(tools):
    result = call(tools, "query_structured", operation="count", record_type="invoice")
    assert result["unverified_records"] == [SCAN]


def test_no_matching_records(tools):
    result = call(tools, "query_structured", operation="sum", field="total", counterparty="acme widgets")
    assert result["records_matched"] == 0 and result["value"] is None


def test_possible_duplicates_are_reported(corpus, tmp_path):
    directory, manifest = corpus
    vault = tmp_path / "vault"
    vault.mkdir()
    for name in ("po_po-2026-0003.pdf", "po_po-2026-0005.pdf"):
        shutil.copy2(directory / name, vault / name)
    # The same order saved twice as different files (different bytes, same content).
    (vault / "po_copy.pdf").write_bytes((directory / "po_po-2026-0003.pdf").read_bytes() + b"\n%resaved")
    config = load_config(DEFAULT_CONFIG_PATH, vault_dir=vault, db_path=tmp_path / "app.db")
    config.extraction.templates_dir = tmp_path / "templates"
    ingest(config, llm=FakeLLM(manifest), ocr_check=ocr_ok, ocr_run=fake_ocr)
    with ToolBox(config) as box:
        result = box.call("query_structured", {"operation": "count", "record_type": "purchase_order"})
    files = ["po_copy.pdf", "po_po-2026-0003.pdf"]
    assert result["possible_duplicates"] == [{"record_number": "PO-2026-0003", "files": files}]


# ------------------------------------------------------------------ search_text


def test_search_text_tool(tools):
    result = call(tools, "search_text", query="returns accepted within 30 days")
    assert result["match_mode"] == "phrase"
    (hit,) = result["hits"]
    assert (hit["filename"], hit["page"]) == ("invoice_bluefin_inv-1004.pdf", 1)
    assert "[Returns accepted within 30 days]" in hit["snippet"]


def test_search_text_partial_match_is_flagged(tools):
    result = call(tools, "search_text", query="courier zebra")
    assert result["match_mode"] == "any_term" and "only some" in result["note"]


def test_search_text_empty_query(tools):
    result = call(tools, "search_text", query="?!")
    assert result["hits"] == [] and "no searchable words" in result["note"]


# ----------------------------------------------------------------- get_evidence


def test_evidence_for_a_found_field(tools, corpus):
    _, manifest = corpus
    truth = next(d for d in manifest["documents"] if d["filename"] == "invoice_northwind_inv-0045.pdf")
    result = call(tools, "get_evidence", document_id="invoice_northwind_inv-0045.pdf", field="total")
    (f,) = result["fields"]
    assert f["presence"] == "found" and f["page"] == 2 == truth["fields"]["total_minor"]["page"]
    assert f["printed_as"] == truth["fields"]["total_minor"]["raw"]
    assert f["value"] == money(truth["fields"]["total_minor"]["value"])
    assert f["printed_as"] in f["supporting_text"]
    assert f["method"].startswith(("template:", "llm"))


def test_evidence_distinguishes_absent_and_not_covered(tools):
    result = call(tools, "get_evidence", document_id="invoice_bluefin_inv-1003.pdf")
    by_field = {f["field"]: f for f in result["fields"]}
    assert len(by_field) == 11
    assert by_field["due_date"]["presence"] == "absent" and "not printed" in by_field["due_date"]["meaning"]
    assert by_field["po_reference"]["presence"] == "not_covered"
    assert "search its text" in by_field["po_reference"]["meaning"]
    assert "value" not in by_field["due_date"]


def test_evidence_by_id_with_question(tools):
    doc_id = call(tools, "lookup_documents", record_number="INV-0047")["matches"][0]["document_id"]
    result = call(tools, "get_evidence", document_id=doc_id, question="warranty")
    assert result["filename"] == "invoice_northwind_inv-0047.pdf"
    assert result["excerpts"] and result["excerpts"][0]["page"] == 1
    assert "[Warranty]" in result["excerpts"][0]["snippet"]


def test_evidence_for_document_without_record(tools):
    result = call(tools, "get_evidence", document_id="corrupt_upload.pdf")
    assert result["fields"] == [] and "cannot open as PDF" in result["note"]
    assert "/" not in result["note"].split("cannot open as PDF")[1]  # no local folder paths


# ----------------------------------------------------------------- get_coverage


def test_coverage(tools):
    result = call(tools, "get_coverage")
    assert result["complete"] is False
    assert result["files_on_disk"] == result["documents"] == 18
    assert [f["filename"] for f in result["failed"]] == ["corrupt_upload.pdf"]
    assert result["ocr_required"] == [] and result["fields_pending"] == [] and result["not_ingested"] == []
    assert result["needs_review"] == [SCAN]
    assert "scope" not in result


def test_coverage_sees_new_files_on_disk(config, corpus):
    _, manifest = corpus
    (config.vault_dir / "corrupt_upload.pdf").unlink()
    ingest(config, llm=FakeLLM(manifest), ocr_check=ocr_ok, ocr_run=fake_ocr)
    with ToolBox(config) as box:
        assert box.call("get_coverage", {})["complete"] is True
        late = config.vault_dir / "late.pdf"
        late.write_bytes((config.vault_dir / "po_po-2026-0003.pdf").read_bytes() + b"\n%new")
        result = box.call("get_coverage", {})
    assert result["complete"] is False and result["not_ingested"] == ["late.pdf"]


# ----------------------------------------------------------------------- CLI


def test_cli_tool_command(ingested, capsys):
    common = ["--vault", str(ingested.vault_dir), "--db", str(ingested.db_path)]
    assert main(["tool", *common]) == 0
    assert "query_structured(" in capsys.readouterr().out

    assert main(["tool", "lookup_documents", '{"record_number": "INV-0044"}', *common]) == 0
    assert json.loads(capsys.readouterr().out)["ambiguous"] is True

    assert main(["tool", "lookup_documents", "{not json", *common]) == 1
    assert "must be a JSON object" in capsys.readouterr().err

    assert main(["tool", "query_structured", '{"operation": "sum"}', *common]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_arguments"


def test_cli_tool_without_database(tmp_path, capsys):
    (tmp_path / "vault").mkdir()
    assert main(["tool", "get_coverage", "--vault", str(tmp_path / "vault"), "--db", str(tmp_path / "none.db")]) == 1
    assert "run ingestion first" in capsys.readouterr().err
    assert not (tmp_path / "none.db").exists()  # the read-only tools never create a database


def test_toolbox_requires_current_schema(tmp_path):
    path = tmp_path / "old.db"
    sqlite3.connect(path).close()
    with pytest.raises(db.SchemaVersionError):
        ToolBox(load_config(DEFAULT_CONFIG_PATH, db_path=path))
