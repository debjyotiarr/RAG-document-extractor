"""Tests for field validation, arithmetic checks and provenance gating (milestone M2)."""

from __future__ import annotations

import pytest

from docintel.validate import (
    Parsed,
    appears_in,
    document_date_order,
    find_currency,
    parse_amount,
    parse_date,
    parse_value,
    snippet,
    validate_record,
)


@pytest.mark.parametrize(
    "raw, minor, currency",
    [
        ("USD 1,333.26", 133326, "USD"),
        ("EUR 1.225,46", 122546, "EUR"),
        ("1234.50", 123450, None),
        ("12,5", 1250, None),
        ("1 234,50", 123450, None),
        ("1,234,567.89", 123456789, None),
        ("₹ 94,400.00", 9440000, "INR"),
        ("-USD 10.00", -1000, "USD"),
        ("(1,000.00)", -100000, None),
        ("AUD 8,470.00", 847000, "AUD"),
    ],
)
def test_parse_amount(raw, minor, currency):
    parsed = parse_amount(raw)
    assert (parsed.value, parsed.currency, parsed.status) == (minor, currency, "valid")


@pytest.mark.parametrize(
    "raw, note",
    [
        ("5 12.00", "more than one number"),  # never glued into 512.00
        ("Qty 3 at 4.00", "more than one number"),
        ("n/a", "no number"),
        ("12.3456", "cannot read number format"),
    ],
)
def test_parse_amount_refuses_to_guess(raw, note):
    parsed = parse_amount(raw)
    assert parsed.value is None and parsed.status == "warning" and note in parsed.note


def test_parse_amount_flags_ambiguous_grouping():
    parsed = parse_amount("1,234")  # 1234 or 1.234?
    assert parsed.value == 123400 and parsed.status == "warning" and "ambiguous separator" in parsed.note


@pytest.mark.parametrize("raw", ["$ 5.00", "¥ 500", "Rs. 100.00", "kr 99,00"])
def test_shared_currency_symbols_are_not_mapped(raw):
    assert parse_amount(raw).currency is None


def test_find_currency():
    assert find_currency("All amounts in EUR") == "EUR"
    assert find_currency("£12.00") == "GBP"
    assert find_currency("USD 5 / EUR 4") is None  # two currencies: no single answer
    assert find_currency("THE INVOICE") is None  # capital words that are not ISO codes


@pytest.mark.parametrize(
    "raw, iso",
    [
        ("12 Jan 2026", "2026-01-12"),
        ("February 3, 2026", "2026-02-03"),
        ("2026-02-21", "2026-02-21"),
        ("14.02.2026", "2026-02-14"),
        ("13/04/26", "2026-04-13"),
        ("04/13/2026", "2026-04-13"),
        ("Jan 12th, 2026", "2026-01-12"),
        ("12-Jan-26", "2026-01-12"),
        ("03.03.2026", "2026-03-03"),  # same either way round
    ],
)
def test_parse_date(raw, iso):
    assert parse_date(raw).value == iso


def test_parse_date_never_guesses_day_month_order():
    parsed = parse_date("05.04.2026")
    assert parsed.value is None and parsed.status == "warning" and "ambiguous" in parsed.note
    assert parse_date("05.04.2026", order_hint="dmy").value == "2026-04-05"
    assert parse_date("05.04.2026", order_hint="mdy").value == "2026-05-04"


def test_parse_date_rejects_impossible_dates():
    assert parse_date("31.02.2026").value is None
    assert parse_date("no date here").status == "warning"


def test_document_date_order():
    assert document_date_order(["19.03.2026", "02.04.2026"]) == "dmy"
    assert document_date_order(["04/13/2026"]) == "mdy"
    assert document_date_order(["02.04.2026", "12 Jan 2026"]) is None  # nothing unambiguous and numeric
    assert document_date_order(["13.04.2026", "04/13/2026"]) is None  # contradictory


def test_parse_value_keeps_ids_and_text_as_printed():
    assert parse_value("record_number", " INV-0041 ").value == "INV-0041"
    assert parse_value("record_number", "00815").value == "00815"  # leading zeros kept
    assert parse_value("payment_terms", "Net   30").value == "Net 30"
    assert parse_value("currency", "EUR").value == "EUR"
    assert parse_value("currency", "$").value is None


def test_verbatim_gate_and_snippet():
    page = "Invoice No: INV-0041\nTotal Due:   USD 1,333.26\n"
    assert appears_in("USD 1,333.26", page)
    assert appears_in("Total Due: USD 1,333.26", page)  # whitespace differences tolerated
    assert not appears_in("USD 1,333.27", page)
    assert not appears_in("", page)
    assert "Total Due: USD 1,333.26" in snippet("USD 1,333.26", page)
    assert snippet("INV-9999", page) is None


def amounts(sub, tax, total, currency="USD"):
    def p(minor):
        return Parsed(f"{currency} {minor / 100:.2f}", f"{minor / 100:.2f}", minor, currency=currency)

    return {"subtotal_minor": p(sub), "tax_minor": p(tax), "total_minor": p(total)}


def base_record(**overrides):
    values = {
        "record_number": Parsed("INV-1", "INV-1", "INV-1"),
        "record_date": Parsed("2026-01-01", "2026-01-01", "2026-01-01"),
        "currency": Parsed("USD", "USD", "USD"),
        **amounts(10000, 800, 10800),
    }
    values.update(overrides)
    return values


def test_validate_record_valid():
    assert validate_record(base_record(), "invoice") == ("valid", [])


def test_validate_record_flags_totals_that_do_not_reconcile():
    status, notes = validate_record(base_record(**amounts(10000, 800, 11000)), "invoice")
    assert status == "warning" and any("subtotal + tax" in n for n in notes)


def test_validate_record_allows_one_minor_unit_of_rounding():
    assert validate_record(base_record(**amounts(10000, 801, 10800)), "invoice")[0] == "valid"


def test_validate_record_missing_number_needs_review():
    status, notes = validate_record(base_record(record_number=None), "invoice")
    assert status == "needs_review" and "record_number: missing" in notes


def test_validate_record_currency_conflict():
    status, notes = validate_record(base_record(currency=Parsed("EUR", "EUR", "EUR")), "invoice")
    assert status == "warning" and any("currency" in n for n in notes)
