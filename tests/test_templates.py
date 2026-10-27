"""Tests for learned layout templates: labels, induction, matching, lifecycle (milestone M2)."""

from __future__ import annotations

import pytest

from docintel import db
from docintel.config import TemplateMatchConfig
from docintel.pdf_extract import extract_pages, ocr_pages, ocr_unavailable_reason
from docintel.templates import (
    Found,
    Template,
    TemplateStore,
    apply_template,
    build_layout,
    induce,
    match,
)
from docintel.validate import parse_value

CFG = TemplateMatchConfig()


def layout_of(path):
    pages = extract_pages(path, min_chars=32)
    return build_layout([(p.page_number, p.text, p.words_json) for p in pages])


def doc(manifest, filename):
    return next(d for d in manifest["documents"] if d["filename"] == filename)


def truth_values(entry):
    return {f: Found(t["raw"], t["page"]) for f, t in entry["fields"].items()}


def learn(directory, manifest, filename):
    entry = doc(manifest, filename)
    template, reason = induce(layout_of(directory / filename), truth_values(entry), entry["document_type"], filename)
    assert reason is None, reason
    return template


def read_back(template, layout):
    """Apply a template and parse its values the way field_extract does."""
    found = apply_template(template, layout, CFG)
    return {
        f: parse_value(f, v.raw, date_order=template.rules[f].date_order).value
        for f, v in found.items()
        if v is not None
    }


def expected(entry, fields):
    values = {f: t["value"] for f, t in entry["fields"].items() if f in fields}
    if "counterparty_name" in values:
        values["counterparty_name"] = entry["fields"]["counterparty_name"]["raw"]  # as printed
    return values


# ----------------------------------------------------------------------- labels


def test_labels_found_on_a_digital_invoice(corpus):
    directory, _ = corpus
    labels = {(lb.text, lb.kind): " ".join(w.text for w in lb.value_words)
              for lb in layout_of(directory / "invoice_northwind_inv-0041.pdf").labels}
    assert labels[("tax invoice", "title")] == ""
    assert labels[("invoice no", "colon")] == "INV-0041"
    assert labels[("total due", "colon")].startswith("USD ")
    # One line with three labels splits correctly; a value never becomes part of the next label.
    assert labels[("bank", "colon")] == "First Harbour Bank,"
    assert labels[("account no", "colon")] == "00123456,"
    assert labels[("sort code", "colon")] == "20-45-17"


def test_labels_without_colons_and_hash_labels(corpus):
    directory, _ = corpus
    texts = {lb.text for lb in layout_of(directory / "invoice_bluefin_inv-1001.pdf").labels}
    assert {"invoice", "date", "payment due", "net amount", "vat", "grand total"} <= texts
    assert "total" in texts  # the table's "Total" column header, a lexicon label


# -------------------------------------------------------------------- induction


@pytest.mark.parametrize(
    "teacher, students",
    [
        ("invoice_northwind_inv-0042.pdf", ["invoice_northwind_inv-0041.pdf", "invoice_northwind_inv-0044.pdf"]),
        ("invoice_bluefin_inv-0044.pdf", ["invoice_bluefin_inv-1001.pdf", "invoice_bluefin_inv-1003.pdf"]),
        ("po_po-2026-0003.pdf", ["po_po-2026-0005.pdf", "po_po-2026-0009.pdf"]),
    ],
)
def test_template_learned_from_one_document_reads_the_others(corpus, teacher, students):
    directory, manifest = corpus
    template = learn(directory, manifest, teacher)
    assert read_back(template, layout_of(directory / teacher)) == expected(doc(manifest, teacher), template.rules)
    for student in students:
        entry = doc(manifest, student)
        got = read_back(template, layout_of(directory / student))
        assert got == expected(entry, template.rules), student


def test_template_rules_and_required_labels(corpus):
    directory, manifest = corpus
    template = learn(directory, manifest, "invoice_northwind_inv-0042.pdf")
    assert template.template_id.startswith("tpl_") and template.version == 1
    assert {lb.text for lb in template.required} == {"tax invoice", "invoice no", "invoice date", "total due"}
    assert template.rules["record_number"].relation == "right_of"
    assert template.rules["record_number"].anchor == "invoice no"
    assert template.rules["counterparty_name"].relation == "at_position"  # printed with no label
    assert "po_reference" not in template.rules  # INV-0042 has no PO reference


def test_dates_follow_the_layouts_convention(corpus):
    directory, manifest = corpus
    template = learn(directory, manifest, "invoice_bluefin_inv-0044.pdf")
    # 19.03.2026 is unambiguous, so the layout is day-first, and 02.04.2026 can be read too.
    assert template.rules["record_date"].date_order == "dmy"
    assert template.rules["due_date"].date_order == "dmy"
    assert read_back(template, layout_of(directory / "invoice_bluefin_inv-0044.pdf"))["due_date"] == "2026-04-02"


def test_values_below_their_labels(extra):
    directory, manifest = extra
    template = learn(directory, manifest, "invoice_kestrel_kf-88812.pdf")
    assert {r.relation for n, r in template.rules.items() if n != "counterparty_name"} == {"below"}
    student = doc(manifest, "invoice_kestrel_kf-88840.pdf")
    got = read_back(template, layout_of(directory / student["filename"]))
    assert got == expected(student, template.rules)


def test_induction_needs_labelled_core_fields(corpus):
    directory, manifest = corpus
    entry = doc(manifest, "invoice_northwind_inv-0042.pdf")
    only_name = {"counterparty_name": truth_values(entry)["counterparty_name"]}
    template, reason = induce(layout_of(directory / entry["filename"]), only_name, "invoice", entry["filename"])
    assert template is None and "core fields" in reason


# --------------------------------------------------------------------- matching


@pytest.fixture(scope="module")
def northwind(corpus):
    directory, manifest = corpus
    return learn(directory, manifest, "invoice_northwind_inv-0042.pdf")


def test_match_same_layout(corpus, northwind):
    directory, _ = corpus
    result = match(layout_of(directory / "invoice_northwind_inv-0044.pdf"), [northwind], CFG)
    assert result.outcome == "match" and result.template is northwind


def test_extra_label_is_a_near_miss_but_missing_optional_one_is_not(corpus, northwind):
    directory, manifest = corpus
    # Learned from INV-0042 (no PO line): INV-0041's "PO Ref:" is a label it has never seen.
    result = match(layout_of(directory / "invoice_northwind_inv-0041.pdf"), [northwind], CFG)
    assert result.outcome == "near_miss" and result.reasons == ["label not in template: po ref"]
    # Learned from INV-0041 (with a PO line): INV-0042 simply lacks an optional field.
    with_po = learn(directory, manifest, "invoice_northwind_inv-0041.pdf")
    assert match(layout_of(directory / "invoice_northwind_inv-0042.pdf"), [with_po], CFG).outcome == "match"


def test_optional_field_missing_still_matches(corpus):
    directory, manifest = corpus
    template = learn(directory, manifest, "invoice_bluefin_inv-0044.pdf")
    result = match(layout_of(directory / "invoice_bluefin_inv-1003.pdf"), [template], CFG)  # no due date printed
    assert result.outcome == "match"


def test_no_match_for_another_layout(corpus, northwind):
    directory, _ = corpus
    result = match(layout_of(directory / "invoice_bluefin_inv-1001.pdf"), [northwind], CFG)
    assert result.outcome == "no_match" and result.template is None


def test_near_miss_on_unknown_label(corpus, extra, northwind):
    directory, _ = extra
    result = match(layout_of(directory / "invoice_northwind_inv-0048_discount.pdf"), [northwind], CFG)
    assert result.outcome == "near_miss" and result.template is northwind
    assert result.reasons == ["label not in template: discount applied"]


def test_near_miss_on_moved_label(corpus, northwind):
    directory, _ = corpus
    result = match(layout_of(directory / "invoice_northwind_inv-0045.pdf"), [northwind], CFG)  # totals on page 2
    assert result.outcome == "near_miss" and "required label moved: total due" in result.reasons


def test_scanned_copy_of_a_known_layout_matches(corpus, northwind):
    # OCR text of the scan: same labels, slightly different positions and one misread word.
    if ocr_unavailable_reason():
        pytest.skip("Tesseract not installed")
    directory, _ = corpus
    path = directory / "invoice_northwind_inv-0046_scan.pdf"
    page = ocr_pages(path, extract_pages(path, 32), "eng", 32)[0]
    pages = [(1, page.text, page.words_json)]
    assert match(build_layout(pages), [northwind], CFG).outcome == "match"


def _with_required(template, n_required):
    """A copy of `template` whose fingerprint has n required labels (using its existing labels)."""
    copy = Template.from_yaml(template.to_yaml(), template.status)
    for i, label in enumerate(sorted(copy.labels, key=lambda lb: (lb.kind != "title", lb.y))):
        label.required = i < n_required
    return copy


def _drop_labels(layout, texts):
    for line in layout.lines:
        line.labels = [lb for lb in line.labels if lb.text not in texts]
    return layout


def test_threshold_is_eighty_percent(corpus, northwind):
    directory, _ = corpus
    path = directory / "invoice_northwind_inv-0041.pdf"
    five = _with_required(northwind, 5)
    required = [lb.text for lb in five.required]
    assert len(required) == 5

    # 4 of 5 required labels (80%) -> near miss; 3 of 5 (60%) -> new layout.
    result = match(_drop_labels(layout_of(path), {required[-1]}), [five], CFG)
    assert result.outcome == "near_miss" and result.score == 0.8
    assert match(_drop_labels(layout_of(path), set(required[-2:])), [five], CFG).outcome == "no_match"

    # With four required labels, one missing is already below 80%.
    four = _with_required(northwind, 4)
    missing_one = {[lb.text for lb in four.required][-1]}
    assert match(_drop_labels(layout_of(path), missing_one), [four], CFG).outcome == "no_match"


def test_two_versions_prefer_the_closest(corpus, extra, northwind):
    directory, manifest = extra
    entry = doc(manifest, "invoice_northwind_inv-0048_discount.pdf")
    v2, _ = induce(layout_of(directory / entry["filename"]), truth_values(entry), "invoice", entry["filename"])
    v2.template_id, v2.version = northwind.template_id, 2
    corpus_dir, _ = corpus
    # A plain invoice matches both versions; v1 (no discount label) fits it more closely.
    result = match(layout_of(corpus_dir / "invoice_northwind_inv-0044.pdf"), [northwind, v2], CFG)
    assert result.outcome == "match" and result.template.version == 1
    result = match(layout_of(directory / entry["filename"]), [northwind, v2], CFG)
    assert result.outcome == "match" and result.template.version == 2


# -------------------------------------------------------------------- lifecycle


@pytest.fixture
def store(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    conn.execute(
        "INSERT INTO documents (document_id, file_hash, filename, relative_path, file_size, file_mtime,"
        " processing_status) VALUES ('doc_1', 'h', 'a.pdf', 'a.pdf', 1, 't', 'text_extracted')"
    )
    yield TemplateStore(conn, tmp_path / "templates", TemplateMatchConfig(probation_docs=3))
    conn.close()


def test_store_saves_yaml_and_versions(store, northwind):
    first = store.save(Template.from_yaml(northwind.to_yaml(), "probation"), parent=None, document_id="doc_1")
    assert (first.version, first.status) == (1, "probation")
    assert (store.dir / f"{first.key}.yaml").is_file()
    second = store.save(Template.from_yaml(northwind.to_yaml(), "probation"), parent=first, document_id="doc_1")
    assert (second.template_id, second.version) == (first.template_id, 2)
    loaded = store.active("invoice")
    assert [t.key for t in loaded] == [first.key, second.key]
    assert loaded[0].rules == northwind.rules and store.active("purchase_order") == []


def test_store_probation_promotes_after_three_agreements(store, northwind):
    t = store.save(Template.from_yaml(northwind.to_yaml(), "probation"), parent=None, document_id="doc_1")
    assert [store.record_probation(t, agreed=True) for _ in range(3)] == ["probation", "probation", "trusted"]


def test_store_retires_after_two_disagreements(store, northwind):
    t = store.save(Template.from_yaml(northwind.to_yaml(), "probation"), parent=None, document_id="doc_1")
    assert store.record_probation(t, agreed=False) == "probation"
    assert store.record_probation(t, agreed=False) == "retired"
    assert store.active("invoice") == []


def test_store_hand_edit_resets_probation(store, northwind):
    t = store.save(Template.from_yaml(northwind.to_yaml(), "probation"), parent=None, document_id="doc_1")
    for _ in range(3):
        store.record_probation(t, agreed=True)
    assert store.active("invoice")[0].status == "trusted"
    path = store.dir / f"{t.key}.yaml"
    path.write_text(path.read_text() + "\n# reviewed by hand\n")
    assert store.active("invoice")[0].status == "probation"


def test_store_missing_or_broken_file_retires_template(store, northwind):
    t = store.save(Template.from_yaml(northwind.to_yaml(), "probation"), parent=None, document_id="doc_1")
    (store.dir / f"{t.key}.yaml").unlink()
    assert store.active("invoice") == []
    status = store.conn.execute("SELECT status FROM templates").fetchone()[0]
    assert status == "retired"


def test_store_probation_zero_trusts_immediately(tmp_path, northwind):
    conn = db.connect(tmp_path / "app.db")
    store = TemplateStore(conn, tmp_path / "t", TemplateMatchConfig(probation_docs=0))
    assert store.save(Template.from_yaml(northwind.to_yaml(), "x"), parent=None, document_id=None).status == "trusted"
    conn.close()
