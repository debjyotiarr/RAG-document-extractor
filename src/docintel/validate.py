"""Value parsers, record validation and the verbatim gate (BLUEPRINT §8.4).

Everything here is deterministic. Parsers never guess: an ambiguous date or
amount is left unparsed with a warning rather than given a plausible value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

# The fields of a record, and the parser each one uses.
FIELD_TYPES: dict[str, str] = {
    "counterparty_name": "text",
    "counterparty_tax_id": "id",
    "record_number": "id",
    "record_date": "date",
    "due_date": "date",
    "po_reference": "id",
    "currency": "currency",
    "subtotal_minor": "amount",
    "tax_minor": "amount",
    "total_minor": "amount",
    "payment_terms": "text",
}
FIELDS = tuple(FIELD_TYPES)
# Fields that identify a document's format and that every invoice/order should have.
CORE_FIELDS = ("record_number", "record_date", "total_minor")
AMOUNT_FIELDS = ("subtotal_minor", "tax_minor", "total_minor")

# ISO 4217 codes recognised in documents. A deliberately broad list: no currency is
# assumed, so a code has to be printed for a currency to be recorded.
CURRENCY_CODES = frozenset(
    "AED ARS AUD BDT BGN BHD BRL CAD CHF CLP CNY COP CZK DKK EGP EUR GBP GHS HKD HUF IDR ILS INR "
    "ISK JOD JPY KES KRW KWD LKR MAD MXN MYR NGN NOK NPR NZD OMR PEN PHP PKR PLN QAR RON RSD RUB "
    "SAR SEK SGD THB TND TRY TWD TZS UAH UGX USD UYU VND XAF XOF ZAR".split()
)
# Only symbols that name exactly one currency. "$", "¥", "kr" and "Rs" are shared by
# several currencies, so they are not mapped.
CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP", "₹": "INR", "₩": "KRW", "₦": "NGN", "₱": "PHP", "₺": "TRY",
                    "₫": "VND", "₪": "ILS", "฿": "THB", "₴": "UAH"}

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
)}
_MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"


@dataclass
class Parsed:
    """A raw value as printed, and what it means."""

    raw: str
    normalized: str | None  # text form stored in field_evidence.normalized_value
    value: object  # typed value for the records table (str, int minor units, ISO date)
    status: str = "valid"  # 'valid' | 'warning'
    note: str | None = None
    currency: str | None = None  # for amounts: a currency printed alongside the number
    date_order: str | None = None  # for numeric dates: 'dmy' or 'mdy' if the raw value shows it


def collapse(text: str) -> str:
    return " ".join(text.split())


def parse_value(field: str, raw: str, *, date_order: str | None = None) -> Parsed:
    kind = FIELD_TYPES[field]
    raw = collapse(raw)
    if kind == "amount":
        return parse_amount(raw)
    if kind == "date":
        return parse_date(raw, order_hint=date_order)
    if kind == "currency":
        code = find_currency(raw)
        if code is None:
            return Parsed(raw, None, None, "warning", f"no unambiguous currency in {raw!r}")
        return Parsed(raw, code, code)
    return Parsed(raw, raw, raw)  # id and text: kept exactly as printed (whitespace collapsed)


def find_currency(text: str) -> str | None:
    """The single currency named in `text`, or None if there is none or more than one."""
    found = {code for code in re.findall(r"\b[A-Z]{3}\b", text) if code in CURRENCY_CODES}
    found |= {code for symbol, code in CURRENCY_SYMBOLS.items() if symbol in text}
    return found.pop() if len(found) == 1 else None


def parse_amount(raw: str) -> Parsed:
    # Grouped numbers (1,234.50 / 1.234,50 / 1 234,50) need exact 3-digit groups, so
    # "5 12.00" stays two numbers instead of becoming 512.00.
    numbers = re.findall(r"\d{1,3}(?:[  '.,]\d{3})+(?:[.,]\d{1,2})?(?!\d)|\d+(?:[.,]\d+)?", raw)
    if len(numbers) != 1:
        what = "no number" if not numbers else "more than one number"
        return Parsed(raw, None, None, "warning", f"{what} in amount {raw!r}")
    number = numbers[0].replace(" ", "").replace(" ", "").replace("'", "")
    negative = raw.strip().startswith(("-", "(")) or raw.strip().endswith("-")

    status, note = "valid", None
    seps = [c for c in number if c in ".,"]
    if not seps:
        digits = number
    else:
        last = number.rfind(seps[-1])
        tail = number[last + 1:]
        if len(tail) in (1, 2) and seps.count(seps[-1]) == 1:
            # The last separator is the decimal point; everything before it is grouping.
            digits = re.sub(r"[.,]", "", number[:last]) + "." + tail
        elif len(tail) == 3:
            digits = re.sub(r"[.,]", "", number)  # grouping only: 1,234 or 1.234.567
            if len(seps) == 1:
                status, note = "warning", f"ambiguous separator in {raw!r}; read as {digits}"
        else:
            return Parsed(raw, None, None, "warning", f"cannot read number format in {raw!r}")
    try:
        amount = Decimal(digits)
    except InvalidOperation:
        return Parsed(raw, None, None, "warning", f"cannot read number in {raw!r}")
    if amount != amount.quantize(Decimal("0.01")):
        status, note = "warning", f"more than two decimals in {raw!r}"
    if negative:
        amount = -amount
    minor = int((amount * 100).to_integral_value())
    return Parsed(raw, f"{amount:.2f}", minor, status, note, currency=find_currency(raw))


def parse_date(raw: str, *, order_hint: str | None = None) -> Parsed:
    text = raw.lower().replace(",", " ")
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text)

    m = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
    if m:
        return _date_result(raw, int(m[1]), int(m[2]), int(m[3]))

    m = re.search(rf"\b(\d{{1,2}})[\s\-/.]*{_MONTH_RE}[\s\-/.]*(\d{{4}}|\d{{2}})\b", text)
    if m:
        return _date_result(raw, _year(m[3]), _MONTHS[m[2][:3]], int(m[1]))
    m = re.search(rf"\b{_MONTH_RE}[\s\-/.]*(\d{{1,2}})[\s\-/.]+(\d{{4}}|\d{{2}})\b", text)
    if m:
        return _date_result(raw, _year(m[3]), _MONTHS[m[1][:3]], int(m[2]))

    m = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})\b", text)
    if m:
        a, b, year = int(m[1]), int(m[2]), _year(m[3])
        if a > 12 and b <= 12:
            order = "dmy"
        elif b > 12 and a <= 12:
            order = "mdy"
        elif a == b:
            order = order_hint or "dmy"  # same either way
        elif order_hint:
            order = order_hint
        else:
            return Parsed(raw, None, None, "warning", f"ambiguous day/month order in {raw!r}")
        day, month = (a, b) if order == "dmy" else (b, a)
        result = _date_result(raw, year, month, day)
        if a != b and (a > 12 or b > 12):
            result.date_order = order
        return result

    return Parsed(raw, None, None, "warning", f"cannot read date {raw!r}")


def document_date_order(raws: list[str]) -> str | None:
    """'dmy' or 'mdy' if the unambiguous numeric dates among `raws` agree on one order.

    A document (and a layout) prints all its dates the same way, so one date like
    19.03.2026 settles how to read 02.04.2026 printed next to it.
    """
    orders = {parse_date(raw).date_order for raw in raws} - {None}
    return orders.pop() if len(orders) == 1 else None


def _year(text: str) -> int:
    return int(text) + 2000 if len(text) == 2 else int(text)


def _date_result(raw: str, year: int, month: int, day: int) -> Parsed:
    try:
        iso = date(year, month, day).isoformat()
    except ValueError:
        return Parsed(raw, None, None, "warning", f"not a valid date: {raw!r}")
    return Parsed(raw, iso, iso)


# --------------------------------------------------------------------- the gate


def appears_in(raw: str, page_text: str) -> bool:
    """The verbatim gate: does `raw` appear in the page text (ignoring whitespace differences)?"""
    needle = collapse(raw)
    return bool(needle) and needle in collapse(page_text)


def snippet(raw: str, page_text: str, width: int = 300) -> str | None:
    """Up to `width` characters of page text around `raw`, for citations."""
    text = collapse(page_text)
    at = text.find(collapse(raw))
    if at < 0:
        return None
    start = max(0, at - (width - len(raw)) // 2)
    return text[start:start + width]


# ---------------------------------------------------------- record validation


def validate_record(values: dict[str, Parsed | None], record_type: str) -> tuple[str, list[str]]:
    """Cross-field checks. Returns ('valid' | 'warning' | 'needs_review', notes).

    Printed values are never overwritten: a total that does not reconcile is
    flagged, not recomputed.
    """
    notes = [f"{f}: {p.note}" for f, p in values.items() if p is not None and p.status != "valid" and p.note]
    status = "warning" if notes else "valid"

    number = values.get("record_number")
    if number is None or not number.value:
        notes.append("record_number: missing")
        status = "needs_review"
    for field in ("record_date", "total_minor"):
        if values.get(field) is None or values[field].value is None:
            notes.append(f"{field}: missing")
            status = "needs_review" if status == "needs_review" else "warning"

    sub, tax, total = (values.get(f) for f in AMOUNT_FIELDS)
    if all(p is not None and p.value is not None for p in (sub, tax, total)):
        if abs(sub.value + tax.value - total.value) > 1:
            notes.append(f"amounts: subtotal + tax = {sub.value + tax.value} but total is {total.value} (minor units)")
            status = "needs_review" if status == "needs_review" else "warning"

    currency = values.get("currency")
    printed = {p.currency for f in AMOUNT_FIELDS if (p := values.get(f)) is not None and p.currency}
    if currency is not None and currency.value and printed - {currency.value}:
        notes.append(f"currency: field says {currency.value}, amounts say {', '.join(sorted(printed))}")
        status = "needs_review" if status == "needs_review" else "warning"
    if len(printed) > 1:
        notes.append(f"currency: amounts use several currencies ({', '.join(sorted(printed))})")
        status = "needs_review" if status == "needs_review" else "warning"
    return status, notes
