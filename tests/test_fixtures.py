"""Tests for the synthetic fixture corpus itself (milestone M0).

Every later test and eval answer is pinned to this corpus, so its ground truth
must match what is actually printed in the PDFs.
"""

from __future__ import annotations

import hashlib

import pymupdf
import pytest
from fixtures.make_fixtures import generate_corpus


def file_hashes(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


def by_name(manifest):
    return {d["filename"]: d for d in manifest["documents"]}


def test_fixtures_generate_deterministically(corpus, tmp_path):
    directory, _ = corpus
    generate_corpus(tmp_path)
    assert file_hashes(tmp_path) == file_hashes(directory)


def test_fixtures_manifest_lists_every_pdf(corpus):
    directory, manifest = corpus
    on_disk = {p.name for p in directory.glob("*.pdf")}
    assert on_disk == set(by_name(manifest))
    kinds = [d["kind"] for d in manifest["documents"]]
    assert kinds.count("scanned") == 1 and kinds.count("corrupt") == 1
    types = [d["document_type"] for d in manifest["documents"]]
    assert types.count("invoice") == 12 and types.count("purchase_order") == 4


def test_fixtures_ground_truth_matches_printed_text(corpus):
    directory, manifest = corpus
    for doc in manifest["documents"]:
        if doc["kind"] != "digital":
            continue
        with pymupdf.open(directory / doc["filename"]) as pdf:
            assert pdf.page_count == doc["page_count"], doc["filename"]
            pages = [page.get_text() for page in pdf]
        for name, truth in doc["fields"].items():
            assert truth["raw"] in pages[truth["page"] - 1], (doc["filename"], name, truth)


def test_fixtures_scanned_pdf_has_no_text_layer(corpus):
    directory, manifest = corpus
    (scan,) = [d for d in manifest["documents"] if d["kind"] == "scanned"]
    with pymupdf.open(directory / scan["filename"]) as pdf:
        assert pdf.page_count == 1
        assert pdf[0].get_text().strip() == ""
        assert pdf[0].get_images()


def test_fixtures_corrupt_pdf_does_not_open(corpus):
    directory, manifest = corpus
    (bad,) = [d for d in manifest["documents"] if d["kind"] == "corrupt"]
    with pytest.raises(Exception):
        with pymupdf.open(directory / bad["filename"]) as pdf:
            pdf.load_page(0)


def test_fixtures_contain_the_planned_edge_cases(corpus):
    _, manifest = corpus
    docs = by_name(manifest)
    invoices = [d for d in docs.values() if d["document_type"] == "invoice"]
    numbers = [d["fields"]["record_number"]["value"] for d in invoices]

    # Same invoice number from both suppliers -> ambiguity case.
    dupes = [d for d in invoices if d["fields"]["record_number"]["value"] == "INV-0044"]
    assert {d["fields"]["counterparty_name"]["value"] for d in dupes} == {
        "Northwind Components Ltd",
        "Bluefin Office Supply GmbH",
    }
    assert len(numbers) == len(set(numbers)) + 1

    # One invoice with no due date -> missing-field case.
    assert [d["filename"] for d in invoices if "due_date" not in d["fields"]] == ["invoice_bluefin_inv-1003.pdf"]

    # Totals on page 2 -> page provenance.
    two_page = docs["invoice_northwind_inv-0045.pdf"]
    assert two_page["page_count"] == 2
    assert two_page["fields"]["total_minor"]["page"] == 2
    assert two_page["fields"]["record_number"]["page"] == 1

    # Invoices cite POs that exist in the corpus.
    orders = [d for d in docs.values() if d["document_type"] == "purchase_order"]
    po_numbers = {d["fields"]["record_number"]["value"] for d in orders}
    cited = {d["fields"]["po_reference"]["value"] for d in invoices if "po_reference" in d["fields"]}
    assert cited and cited <= po_numbers

    # Two currencies -> mixed-currency aggregation must be refused later.
    assert {d["fields"]["currency"]["value"] for d in invoices} == {"USD", "EUR"}


def test_fixtures_invoice_totals_reconcile(corpus):
    _, manifest = corpus
    for doc in manifest["documents"]:
        f = doc["fields"]
        if doc["document_type"] == "invoice":
            assert f["subtotal_minor"]["value"] + f["tax_minor"]["value"] == f["total_minor"]["value"], doc["filename"]
