"""Tests for classification and template/LLM field extraction, end to end (milestone M2).

These run the real pipeline (ingest -> classify -> match -> template or LLM ->
validate -> store) with FakeLLM standing in for Ollama, so they need no model.
"""

from __future__ import annotations

import shutil

import pytest
from conftest import FakeLLM, fake_ocr, ocr_ok, requires_tesseract

from docintel import db
from docintel.classify import classify, classify_by_rules
from docintel.ingest import coverage, ingest
from docintel.llm import LLMUnavailable
from docintel.validate import FIELDS, appears_in

DIGITAL_RECORDS = 15  # 11 digital invoices + 4 purchase orders in the corpus


def run(config, llm, **kwargs):
    kwargs.setdefault("ocr_check", ocr_ok)
    kwargs.setdefault("ocr_run", fake_ocr)
    return ingest(config, llm=llm, **kwargs)


def query(config, sql, params=()):
    conn = db.connect(config.db_path)
    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return rows


def records_by_file(config):
    return {
        r["filename"]: r
        for r in query(
            config,
            "SELECT d.filename, d.processing_status, d.template_id, d.template_version, d.template_match,"
            " d.error_message, r.* FROM records r JOIN documents d USING (document_id)",
        )
    }


def evidence(config, filename):
    return {
        e["field_name"]: e
        for e in query(
            config,
            "SELECT e.* FROM field_evidence e JOIN records r USING (record_id) JOIN documents d USING (document_id)"
            " WHERE d.filename = ?",
            (filename,),
        )
    }


def expected_value(field, truth):
    return truth["raw"] if field == "counterparty_name" else truth["value"]


@pytest.fixture
def llm(corpus):
    return FakeLLM(corpus[1])


# ------------------------------------------------------------------ accuracy


def test_all_fields_extracted_correctly_with_pages(config, corpus, llm):
    _, manifest = corpus
    run(config, llm)
    records = records_by_file(config)
    checked = 0
    for doc in manifest["documents"]:
        if doc["kind"] != "digital" or doc["document_type"] not in ("invoice", "purchase_order"):
            continue
        rec, ev = records[doc["filename"]], evidence(config, doc["filename"])
        assert rec["record_type"] == doc["document_type"]
        assert rec["processing_status"] == "validated", (doc["filename"], rec["error_message"])
        for field, truth in doc["fields"].items():
            column = "counterparty_name_raw" if field == "counterparty_name" else field
            assert rec[column] == expected_value(field, truth), (doc["filename"], field)
            assert ev[field]["presence"] == "found" and ev[field]["source_page"] == truth["page"]
            checked += 1
    assert checked == 132
    assert len([r for r in records.values() if r["processing_status"] == "validated"]) == DIGITAL_RECORDS


def test_every_found_value_is_printed_on_its_cited_page(config, llm):
    run(config, llm)
    pages = {
        (r["filename"], r["page_number"]): r["page_text"]
        for r in query(config, "SELECT d.filename, p.page_number, p.page_text FROM document_pages p JOIN documents d"
                               " USING (document_id)")
    }
    rows = query(config, "SELECT d.filename, e.* FROM field_evidence e JOIN records USING (record_id)"
                         " JOIN documents d USING (document_id) WHERE e.presence = 'found'")
    assert rows
    for e in rows:
        assert appears_in(e["raw_value"], pages[(e["filename"], e["source_page"])]), e
        assert e["supporting_text"] and appears_in(e["raw_value"], e["supporting_text"])


def test_every_record_has_one_evidence_row_per_field(config, llm):
    run(config, llm)
    for rec in records_by_file(config).values():
        rows = query(config, "SELECT field_name FROM field_evidence WHERE record_id = ?", (rec["record_id"],))
        assert sorted(r["field_name"] for r in rows) == sorted(FIELDS)


def test_presence_distinguishes_absent_from_not_covered(config, llm):
    run(config, llm)
    ev = evidence(config, "invoice_bluefin_inv-1003.pdf")
    assert ev["due_date"]["presence"] == "absent"  # its layout prints a due date; this invoice does not
    assert ev["due_date"]["raw_value"] is None
    assert ev["po_reference"]["presence"] == "not_covered"  # this layout version has no PO line
    rec = records_by_file(config)["invoice_bluefin_inv-1003.pdf"]
    assert rec["due_date"] is None and rec["po_reference"] is None


def test_currency_is_never_assumed(config, llm):
    run(config, llm)
    records = records_by_file(config)
    assert {r["currency"] for name, r in records.items() if "bluefin" in name} == {"EUR"}
    assert {r["currency"] for name, r in records.items() if "northwind" in name and "scan" not in name} == {"USD"}


def test_non_records_get_a_type_but_no_record(config, llm):
    run(config, llm)
    doc = query(config, "SELECT * FROM documents WHERE filename = 'delivery_note_dn-5521.pdf'")[0]
    assert (doc["document_type"], doc["processing_status"]) == ("other", "validated")
    assert "delivery_note_dn-5521.pdf" not in records_by_file(config)


# ------------------------------------------------------------- templates & LLM


def test_each_layout_is_learned_once_then_parsed_without_llm(config, corpus, llm):
    config.extraction.template_match.probation_docs = 0  # trust at once: isolates the learning behaviour
    run(config, llm)
    # The LLM read the first document of each layout, the near misses (an extra "Your Order",
    # "Notes" or "Warranty" line, or totals moved to page 2), and the scan, whose fake OCR text
    # resembles no layout. Each of those calls was exactly one read.
    assert sorted(llm.extract_calls) == [
        "invoice_bluefin_inv-0044.pdf", "invoice_bluefin_inv-1002.pdf", "invoice_bluefin_inv-1004.pdf",
        "invoice_northwind_inv-0041.pdf", "invoice_northwind_inv-0045.pdf", "invoice_northwind_inv-0046_scan.pdf",
        "invoice_northwind_inv-0047.pdf", "po_po-2026-0003.pdf",
    ]
    # Everything else was parsed by a template alone.
    records = records_by_file(config).values()
    parsed_by_template = sorted(r["filename"] for r in records if r["template_match"] == "match")
    assert parsed_by_template == [
        "invoice_bluefin_inv-1001.pdf", "invoice_bluefin_inv-1003.pdf", "invoice_northwind_inv-0042.pdf",
        "invoice_northwind_inv-0043.pdf", "invoice_northwind_inv-0044.pdf", "po_po-2026-0005.pdf",
        "po_po-2026-0007.pdf", "po_po-2026-0009.pdf",
    ]
    learned_from = {t["learned_from_file"] for t in query(config, "SELECT learned_from_file FROM templates")}
    assert learned_from <= set(llm.extract_calls)

    llm.calls.clear()
    run(config, llm, force=True)  # re-read everything: every digital layout is known now
    assert llm.extract_calls == ["invoice_northwind_inv-0046_scan.pdf"]


def test_rerun_is_a_no_op_with_no_llm_calls(config, llm):
    run(config, llm)
    before = query(config, "SELECT * FROM field_evidence ORDER BY evidence_id")
    llm.calls.clear()
    report = run(config, llm)
    assert llm.calls == [] and report.extracted == {}
    assert query(config, "SELECT * FROM field_evidence ORDER BY evidence_id") == before


def test_probation_double_checks_then_trusts(config, llm):
    run(config, llm)  # probation_docs = 3 from the repository config
    po = query(config, "SELECT * FROM templates WHERE document_type = 'purchase_order'")
    assert len(po) == 1 and po[0]["status"] == "trusted" and po[0]["docs_agreed"] == 3
    # All four orders were read by the LLM: one to learn, three to check the template.
    assert sum(1 for name in llm.extract_calls if name.startswith("po_")) == 4


def test_probation_disagreement_flags_the_document(config, corpus, llm):
    # The LLM reads INV-1001's due date as its issue date: printed, so it passes the gate, but wrong.
    truth = next(d for d in corpus[1]["documents"] if d["filename"] == "invoice_bluefin_inv-1001.pdf")
    wrong = {"value": truth["fields"]["record_date"]["raw"], "page": 1}
    llm.overrides["invoice_bluefin_inv-1001.pdf"] = {"due_date": wrong}
    run(config, llm)
    rec = records_by_file(config)["invoice_bluefin_inv-1001.pdf"]
    assert rec["processing_status"] == "needs_review"
    assert "disagree on due_date" in rec["error_message"]
    assert evidence(config, "invoice_bluefin_inv-1001.pdf")["due_date"]["validation_status"] == "needs_review"
    template = query(config, "SELECT * FROM templates WHERE learned_from_file = 'invoice_bluefin_inv-0044.pdf'")[0]
    assert template["docs_disagreed"] == 1


def test_hallucinated_value_is_discarded(config, llm):
    llm.overrides["invoice_northwind_inv-0041.pdf"] = {"record_number": {"value": "INV-9999", "page": 1}}
    run(config, llm)
    rec = records_by_file(config)["invoice_northwind_inv-0041.pdf"]
    assert rec["processing_status"] == "needs_review"
    assert "not printed on page 1; discarded" in rec["error_message"]
    assert rec["record_number"] is None
    assert not query(config, "SELECT * FROM field_evidence WHERE raw_value = 'INV-9999'")


def test_value_on_wrong_page_is_relocated_when_unique(config, llm):
    llm.overrides["po_po-2026-0003.pdf"] = {"record_number": {"value": "PO-2026-0003", "page": 7}}
    run(config, llm)
    assert evidence(config, "po_po-2026-0003.pdf")["record_number"]["source_page"] == 1


def test_near_miss_learns_a_new_version_and_unseen_layout_a_new_template(config, extra, llm):
    extra_dir, extra_manifest = extra
    for d in extra_manifest["documents"]:
        shutil.copy2(extra_dir / d["filename"], config.vault_dir / d["filename"])
    llm.docs += extra_manifest["documents"]
    run(config, llm)
    records = records_by_file(config)
    northwind = records["invoice_northwind_inv-0041.pdf"]["template_id"]
    discount = records["invoice_northwind_inv-0048_discount.pdf"]
    assert (discount["template_match"], discount["template_id"]) == ("near_miss", northwind)
    assert discount["template_version"] > 1 and discount["processing_status"] == "validated"
    k1, k2 = records["invoice_kestrel_kf-88812.pdf"], records["invoice_kestrel_kf-88840.pdf"]
    assert k1["template_match"] == "no_match" and k1["template_id"] not in (northwind, None)
    assert (k2["template_match"], k2["template_id"]) == ("match", k1["template_id"])
    truth = next(d for d in extra_manifest["documents"] if d["filename"] == "invoice_kestrel_kf-88840.pdf")
    assert k2["total_minor"] == truth["fields"]["total_minor"]["value"] == 856350
    assert k2["record_date"] == "2026-05-18" and k2["processing_status"] == "validated"


# ----------------------------------------------------------- LLM unavailable


class DownLLM:
    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user, schema):
        self.calls += 1
        raise LLMUnavailable("cannot reach Ollama (test)")


def test_llm_down_postpones_extraction_and_retries_later(config, llm):
    report = run(config, DownLLM())
    assert len(report.fields_pending) == 16  # every readable document; the delivery note too
    cov = coverage(config)
    assert len(cov.fields_pending) == 16 and all("cannot reach Ollama" in why for _, why in cov.fields_pending)
    assert records_by_file(config) == {}

    report = run(config, llm)
    assert report.fields_pending == {} and len(report.extracted) == 16
    assert coverage(config).fields_pending == []


def test_llm_down_does_not_stop_trusted_templates(config, llm):
    config.extraction.template_match.probation_docs = 0
    run(config, llm)
    down = DownLLM()
    report = run(config, down, force=True)
    # Known layouts parse with no LLM; only the document that needs it is postponed.
    assert list(report.fields_pending) == ["invoice_northwind_inv-0046_scan.pdf"]
    assert down.calls == 1
    assert len(report.extracted) == 16  # incl. INV-0041, which taught a template and now matches it


# ------------------------------------------------------------- classification


def test_classification_rules(corpus):
    assert classify_by_rules("TAX INVOICE\nInvoice No: 1") == "invoice"
    assert classify_by_rules("PURCHASE ORDER\nPO Number: 7\nPlease quote it on all invoices") == "purchase_order"
    assert classify_by_rules("CREDIT NOTE\nCredit Note No: 5") == "credit_note"
    assert classify_by_rules("CREDIT NOTE\nInvoice No: 5") is None  # mixed signals: left to the LLM
    assert classify_by_rules("DELIVERY NOTE\nYour Order: PO-1") == "other"
    assert classify_by_rules("Dear customer, thank you") is None


def test_classification_falls_back_to_llm_and_respects_low_confidence():
    class Says:
        def __init__(self, answer):
            self.answer = answer

        def complete_json(self, system, user, schema):
            return self.answer

    assert classify("hello", Says({"document_type": "invoice", "confidence": "high"})).document_type == "invoice"
    assert classify("hello", Says({"document_type": "invoice", "confidence": "low"})).document_type == "unknown"
    assert classify("hello", Says({"document_type": "spaceship", "confidence": "high"})).document_type == "unknown"
    with pytest.raises(LLMUnavailable):
        classify("hello", DownLLM())


@requires_tesseract
def test_scanned_invoice_read_by_template_after_real_ocr(config, llm):
    config.extraction.template_match.probation_docs = 0
    ingest(config, llm=llm)  # real Tesseract
    rec = records_by_file(config)["invoice_northwind_inv-0046_scan.pdf"]
    assert rec["template_match"] == "match"
    assert (rec["record_number"], rec["total_minor"], rec["record_date"]) == ("INV-0046", 48331, "2026-04-09")
    assert "invoice_northwind_inv-0046_scan.pdf" not in llm.extract_calls
