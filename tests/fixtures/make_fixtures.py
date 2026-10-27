"""Generate the synthetic invoice/order fixture corpus (BLUEPRINT §8.8). Never commit real PDFs.

The corpus is fully deterministic: the same code always produces byte-identical
PDFs, so tests and eval answers can be pinned to it. Alongside the PDFs it
writes `manifest.json`, the ground truth for every document: the expected
document type and, for each field, the raw text as printed, the normalized
value, and the page it appears on. A field missing from a document's `fields`
is genuinely absent from that PDF.

What the corpus exercises:
  - two invoice layouts (two suppliers) with different labels, currencies,
    number formats (1,234.50 vs 1.234,50) and date formats
  - invoice numbers with leading zeros (INV-0041)
  - the same invoice number (INV-0044) issued by both suppliers -> ambiguity
  - an invoice with no due date -> missing-field case
  - bank details on one supplier's invoices only -> missing-field case
  - a two-page invoice whose totals are on page 2 -> page provenance
  - purchase orders, three of them cited by invoices via "PO Ref"
  - a delivery note -> a document that is neither invoice nor order
  - an image-only "scanned" invoice -> OCR path
  - a truncated, unreadable PDF -> failure path

Usage:  python tests/fixtures/make_fixtures.py [OUTPUT_DIR]
        (default OUTPUT_DIR: tests/fixtures/corpus, which is gitignored)
"""

from __future__ import annotations

import io
import json
import random
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "corpus"
# A fixed creation date makes fpdf2's output (including the file ID) byte-identical.
_CREATION_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)
_BUYER = "Acme Retail Ltd"
_CENT = Decimal("0.01")


# --------------------------------------------------------------------------- spec


@dataclass(frozen=True)
class Supplier:
    key: str
    name: str
    address: str
    tax_label: str
    tax_id: str
    currency: str
    tax_rate: Decimal
    number_style: str  # "us" (1,234.50) or "eu" (1.234,50)
    payment_terms: str
    payment_days: int
    bank_details: str | None


NORTHWIND = Supplier(
    key="northwind",
    name="Northwind Components Ltd",
    address="14 Harbour Road, Portsmouth PO1 3AX",
    tax_label="Tax ID",
    tax_id="NW-4471-2290",
    currency="USD",
    tax_rate=Decimal("0.08"),
    number_style="us",
    payment_terms="Net 30",
    payment_days=30,
    bank_details="Bank: First Harbour Bank, Account No: 00123456, Sort Code: 20-45-17",
)

BLUEFIN = Supplier(
    key="bluefin",
    name="Bluefin Office Supply GmbH",
    address="Lindenstrasse 8, 20095 Hamburg",
    tax_label="VAT ID",
    tax_id="DE811234567",
    currency="EUR",
    tax_rate=Decimal("0.19"),
    number_style="eu",
    payment_terms="Payable within 14 days",
    payment_days=14,
    bank_details=None,
)

_CATALOG = {
    "northwind": [
        ("Hex bolt M8 x 40, zinc", Decimal("0.42")),
        ("Bearing 6204-2RS", Decimal("6.80")),
        ("Aluminium bracket, 90 deg", Decimal("3.15")),
        ("Hydraulic hose 1/2in, per metre", Decimal("11.60")),
        ("Spring washer M10", Decimal("0.09")),
        ("Pillow block bearing UCP205", Decimal("18.25")),
        ("Cable gland M20", Decimal("1.35")),
        ("Steel shaft 20mm x 500mm", Decimal("24.90")),
    ],
    "bluefin": [
        ("Copy paper A4, 80gsm, box of 5 reams", Decimal("21.40")),
        ("Ballpoint pens, blue, pack of 50", Decimal("9.95")),
        ("Lever arch file, A4", Decimal("2.65")),
        ("Desk organiser, mesh", Decimal("14.20")),
        ("Toner cartridge TN-2420", Decimal("48.00")),
        ("Whiteboard markers, assorted, pack of 12", Decimal("11.30")),
    ],
}


@dataclass
class InvoiceSpec:
    supplier: Supplier
    number: str
    issued: date
    date_style: str
    n_items: int = 4
    po_reference: str | None = None
    has_due_date: bool = True
    totals_on_page_2: bool = False
    notes: str | None = None
    scanned: bool = False
    extra_line: str | None = None  # an additional labelled line, for template near-miss tests


@dataclass
class OrderSpec:
    vendor: Supplier
    number: str
    issued: date
    n_items: int = 3
    delivery_by: date | None = None


INVOICES = [
    InvoiceSpec(NORTHWIND, "INV-0041", date(2026, 1, 12), "dmy_short", po_reference="PO-2026-0003"),
    InvoiceSpec(NORTHWIND, "INV-0042", date(2026, 2, 3), "mdy_long"),
    InvoiceSpec(NORTHWIND, "INV-0043", date(2026, 2, 21), "iso", po_reference="PO-2026-0007"),
    InvoiceSpec(NORTHWIND, "INV-0044", date(2026, 3, 15), "dmy_short"),
    InvoiceSpec(NORTHWIND, "INV-0045", date(2026, 3, 28), "mdy_long", n_items=18, totals_on_page_2=True),
    InvoiceSpec(NORTHWIND, "INV-0046", date(2026, 4, 9), "dmy_short", scanned=True),
    InvoiceSpec(
        NORTHWIND, "INV-0047", date(2026, 4, 30), "iso",
        notes="Warranty: 12 months from date of delivery. Goods remain our property until paid in full.",
    ),
    InvoiceSpec(BLUEFIN, "INV-1001", date(2026, 1, 20), "eu"),
    InvoiceSpec(BLUEFIN, "INV-1002", date(2026, 2, 14), "eu", po_reference="PO-2026-0005"),
    InvoiceSpec(BLUEFIN, "INV-1003", date(2026, 3, 3), "eu", has_due_date=False),
    InvoiceSpec(BLUEFIN, "INV-0044", date(2026, 3, 19), "eu"),
    InvoiceSpec(
        BLUEFIN, "INV-1004", date(2026, 4, 11), "eu",
        notes="Delivery by courier to the loading bay. Returns accepted within 30 days.",
    ),
]

ORDERS = [
    OrderSpec(NORTHWIND, "PO-2026-0003", date(2026, 1, 5), delivery_by=date(2026, 1, 15)),
    OrderSpec(BLUEFIN, "PO-2026-0005", date(2026, 2, 9)),
    OrderSpec(NORTHWIND, "PO-2026-0007", date(2026, 2, 15), delivery_by=date(2026, 2, 25)),
    OrderSpec(BLUEFIN, "PO-2026-0009", date(2026, 4, 2), n_items=2),
]


# ----------------------------------------------------------------- formatting


def fmt_amount(amount: Decimal, style: str) -> str:
    us = f"{amount:,.2f}"
    if style == "us":
        return us
    return us.replace(",", "_").replace(".", ",").replace("_", ".")


def fmt_date(d: date, style: str) -> str:
    if style == "dmy_short":
        return f"{d.day:02d} {d:%b} {d.year}"
    if style == "mdy_long":
        return f"{d:%B} {d.day}, {d.year}"
    if style == "iso":
        return d.isoformat()
    if style == "eu":
        return f"{d.day:02d}.{d.month:02d}.{d.year}"
    raise ValueError(f"unknown date style {style!r}")


def to_minor(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value())


def make_items(supplier: Supplier, seed: str, n: int) -> list[tuple[str, int, Decimal, Decimal]]:
    """Deterministic line items: (description, qty, unit_price, line_total)."""
    rng = random.Random(seed)  # str seeds are hashed deterministically across runs
    catalog = _CATALOG[supplier.key]
    items = []
    for _ in range(n):
        desc, price = catalog[rng.randrange(len(catalog))]
        qty = rng.randint(1, 40)
        items.append((desc, qty, price, (price * qty).quantize(_CENT)))
    return items


# --------------------------------------------------------------------- layout


@dataclass
class Layout:
    """A renderer-independent page layout plus the ground truth it encodes.

    Blocks are ("title" | "text" | "table" | "pagebreak", payload). Both the
    digital and the scanned renderer consume the same blocks, so the ground
    truth is identical regardless of how the PDF is produced.
    """

    blocks: list[tuple[str, object]] = field(default_factory=list)
    fields: dict[str, dict] = field(default_factory=dict)
    page: int = 1

    def title(self, text: str) -> None:
        self.blocks.append(("title", text))

    def text(self, text: str) -> None:
        self.blocks.append(("text", text))

    def field(self, name: str, line: str, raw: str, value: object) -> None:
        """Emit a line that contains a field's raw value and record its ground truth."""
        assert raw in line, (raw, line)
        self.blocks.append(("text", line))
        self.fields[name] = {"raw": raw, "value": value, "page": self.page}

    def table(self, header: list[str], rows: list[list[str]]) -> None:
        self.blocks.append(("table", (header, rows)))

    def field_below(self, name: str, label: str, raw: str, value: object) -> None:
        """Emit a label on its own line with the value on the line below it."""
        self.blocks.append(("text", label))
        self.blocks.append(("text", raw))
        self.fields[name] = {"raw": raw, "value": value, "page": self.page}

    def pagebreak(self) -> None:
        self.blocks.append(("pagebreak", None))
        self.page += 1


def invoice_layout(spec: InvoiceSpec) -> Layout:
    s = spec.supplier
    items = make_items(s, spec.number + s.key, spec.n_items)
    subtotal = sum((total for *_, total in items), Decimal("0"))
    tax = (subtotal * s.tax_rate).quantize(_CENT, rounding=ROUND_HALF_UP)
    total = subtotal + tax
    money = lambda amount: f"{s.currency} {fmt_amount(amount, s.number_style)}"  # noqa: E731
    issued_raw = fmt_date(spec.issued, spec.date_style)
    due = spec.issued + timedelta(days=s.payment_days)
    due_raw = fmt_date(due, spec.date_style)
    rate_pct = f"{(s.tax_rate * 100).normalize()}%"

    lay = Layout()
    if s is NORTHWIND:
        lay.field("counterparty_name", s.name.upper(), s.name.upper(), s.name)
        lay.text(s.address)
        lay.field("counterparty_tax_id", f"{s.tax_label}: {s.tax_id}", s.tax_id, s.tax_id)
        lay.title("TAX INVOICE")
        lay.field("record_number", f"Invoice No: {spec.number}", spec.number, spec.number)
        lay.field("record_date", f"Invoice Date: {issued_raw}", issued_raw, spec.issued.isoformat())
        if spec.has_due_date:
            lay.field("due_date", f"Due Date: {due_raw}", due_raw, due.isoformat())
        if spec.po_reference:
            lay.field("po_reference", f"PO Ref: {spec.po_reference}", spec.po_reference, spec.po_reference)
        lay.text(f"Bill To: {_BUYER}")
        header = ["Description", "Qty", "Unit Price", "Amount"]
        labels = ("Subtotal", f"Tax ({rate_pct})", "Total Due")
    else:
        lay.field("counterparty_name", s.name, s.name, s.name)
        lay.text(s.address)
        lay.field("counterparty_tax_id", f"{s.tax_label}: {s.tax_id}", s.tax_id, s.tax_id)
        lay.title("Invoice")
        lay.field("record_number", f"Invoice # {spec.number}", spec.number, spec.number)
        lay.field("record_date", f"Date: {issued_raw}", issued_raw, spec.issued.isoformat())
        if spec.has_due_date:
            lay.field("due_date", f"Payment Due: {due_raw}", due_raw, due.isoformat())
        if spec.po_reference:
            lay.field("po_reference", f"Your Order: {spec.po_reference}", spec.po_reference, spec.po_reference)
        lay.text(f"Customer: {_BUYER}")
        header = ["Item", "Qty", "Price", "Total"]
        labels = ("Net Amount", f"VAT {rate_pct}", "Grand Total")

    lay.table(
        header,
        [[d, str(q), fmt_amount(p, s.number_style), fmt_amount(t, s.number_style)] for d, q, p, t in items],
    )
    if spec.totals_on_page_2:
        lay.pagebreak()
        lay.text(f"Invoice No: {spec.number} (continued)")
    lay.field("currency", f"All amounts in {s.currency}", s.currency, s.currency)
    lay.field("subtotal_minor", f"{labels[0]}: {money(subtotal)}", money(subtotal), to_minor(subtotal))
    lay.field("tax_minor", f"{labels[1]}: {money(tax)}", money(tax), to_minor(tax))
    lay.field("total_minor", f"{labels[2]}: {money(total)}", money(total), to_minor(total))
    terms_label = "Payment Terms" if s is NORTHWIND else "Terms"
    lay.field("payment_terms", f"{terms_label}: {s.payment_terms}", s.payment_terms, s.payment_terms)
    if s.bank_details:
        lay.text(s.bank_details)
    if spec.notes:
        lay.text(f"Notes: {spec.notes}")
    if spec.extra_line:
        lay.text(spec.extra_line)
    return lay


def order_layout(spec: OrderSpec) -> Layout:
    v = spec.vendor
    items = make_items(v, spec.number, spec.n_items)
    net = sum((total for *_, total in items), Decimal("0"))
    issued_raw = spec.issued.isoformat()
    net_raw = f"{v.currency} {fmt_amount(net, v.number_style)}"

    lay = Layout()
    lay.text(_BUYER.upper())
    lay.text("Purchasing Department, 200 Market Street")
    lay.title("PURCHASE ORDER")
    lay.field("record_number", f"PO Number: {spec.number}", spec.number, spec.number)
    lay.field("record_date", f"Order Date: {issued_raw}", issued_raw, spec.issued.isoformat())
    lay.field("counterparty_name", f"Vendor: {v.name}", v.name, v.name)
    if spec.delivery_by:
        lay.text(f"Deliver by: {spec.delivery_by.isoformat()}")
    lay.table(
        ["Description", "Qty", "Unit Price", "Line Total"],
        [[d, str(q), fmt_amount(p, v.number_style), fmt_amount(t, v.number_style)] for d, q, p, t in items],
    )
    lay.field("currency", f"Currency: {v.currency}", v.currency, v.currency)
    # Orders are priced net; tax is charged on the supplier's invoice.
    lay.field("total_minor", f"Order Total (excl. tax): {net_raw}", net_raw, to_minor(net))
    lay.text("Please quote the PO number on all invoices and delivery notes.")
    return lay


def delivery_note_layout() -> Layout:
    lay = Layout()
    lay.text(NORTHWIND.name.upper())
    lay.text(NORTHWIND.address)
    lay.title("DELIVERY NOTE")
    lay.text("Delivery Note No: DN-5521")
    lay.text("Dispatched: 2026-02-24")
    lay.text("Your Order: PO-2026-0007")
    lay.text(f"Deliver To: {_BUYER}, Goods-In, 200 Market Street")
    lay.table(
        ["Description", "Qty Shipped"],
        [["Bearing 6204-2RS", "12"], ["Cable gland M20", "30"]],
    )
    lay.text("Received in good condition: ____________________")
    return lay


# ------------------------------------------------------------------ renderers


def _new_pdf() -> FPDF:
    pdf = FPDF(format="A4")
    pdf.set_creation_date(_CREATION_DATE)
    pdf.set_auto_page_break(True, margin=20)
    pdf.set_margins(18, 18, 18)
    pdf.add_page()
    return pdf


def render_digital(lay: Layout) -> bytes:
    pdf = _new_pdf()
    for kind, payload in lay.blocks:
        if kind == "title":
            pdf.ln(3)
            pdf.set_font("Helvetica", "B", 16)
            pdf.cell(0, 9, payload, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        elif kind == "text":
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 6, payload, new_x="LMARGIN", new_y="NEXT")
        elif kind == "table":
            header, rows = payload
            widths = [94, 18, 30, 32] if len(header) == 4 else [140, 34]
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 9)
            for w, h in zip(widths, header):
                pdf.cell(w, 7, h, border=1)
            pdf.ln()
            pdf.set_font("Helvetica", "", 9)
            for row in rows:
                for i, (w, cell) in enumerate(zip(widths, row)):
                    pdf.cell(w, 6, cell, border=1, align="L" if i == 0 else "R")
                pdf.ln()
            pdf.ln(3)
        elif kind == "pagebreak":
            pdf.add_page()
    return bytes(pdf.output())


def render_scanned(lay: Layout) -> bytes:
    """Rasterize the layout (about 150 dpi) and embed it as an image-only PDF page."""
    font = ImageFont.load_default(size=24)
    bold = ImageFont.load_default(size=36)
    pages: list[Image.Image] = []

    def new_page() -> tuple[Image.Image, ImageDraw.ImageDraw]:
        img = Image.new("L", (1240, 1754), 255)
        pages.append(img)
        return img, ImageDraw.Draw(img)

    _, draw = new_page()
    y = 100
    for kind, payload in lay.blocks:
        if kind == "title":
            y += 20
            draw.text((100, y), payload, fill=0, font=bold)
            y += 64
        elif kind == "text":
            draw.text((100, y), payload, fill=0, font=font)
            y += 38
        elif kind == "table":
            header, rows = payload
            cols = [100, 640, 760, 960]
            for row in [header, *rows]:
                for x, cell in zip(cols, row):
                    draw.text((x, y), cell, fill=0, font=font)
                y += 36
            y += 20
        elif kind == "pagebreak":
            _, draw = new_page()
            y = 100

    pdf = FPDF(format="A4")
    pdf.set_creation_date(_CREATION_DATE)
    for img in pages:
        pdf.add_page()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        pdf.image(buf, x=0, y=0, w=210, h=297)
    return bytes(pdf.output())


# A PDF header followed by a truncated object: opens in no reader.
CORRUPT_PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R"


# --------------------------------------------------------------------- driver


KESTREL = Supplier(
    key="kestrel",
    name="Kestrel Freight Pty Ltd",
    address="9 Wharf Street, Fremantle WA 6160",
    tax_label="ABN",
    tax_id="51 824 753 556",
    currency="AUD",
    tax_rate=Decimal("0.10"),
    number_style="us",
    payment_terms="7 days from invoice",
    payment_days=7,
    bank_details=None,
)
_CATALOG["kestrel"] = [
    ("Pallet freight, Perth to Adelaide", Decimal("185.00")),
    ("Fuel surcharge", Decimal("22.50")),
    ("Tail-lift delivery", Decimal("65.00")),
]


def kestrel_layout(number: str, issued: date) -> Layout:
    """A third layout: every label on its own line, with its value on the line below."""
    s = KESTREL
    items = make_items(s, number, 3)
    subtotal = sum((total for *_, total in items), Decimal("0"))
    tax = (subtotal * s.tax_rate).quantize(_CENT, rounding=ROUND_HALF_UP)
    total = subtotal + tax
    money = lambda amount: f"{s.currency} {fmt_amount(amount, 'us')}"  # noqa: E731
    issued_raw = fmt_date(issued, "dmy_short")

    lay = Layout()
    lay.field("counterparty_name", s.name, s.name, s.name)
    lay.text(s.address)
    lay.title("INVOICE")
    lay.field_below("record_number", "Invoice Number:", number, number)
    lay.field_below("record_date", "Issue Date:", issued_raw, issued.isoformat())
    lay.field_below("counterparty_tax_id", "ABN:", s.tax_id, s.tax_id)
    lay.table(
        ["Service", "Qty", "Rate", "Charge"],
        [[d, str(q), fmt_amount(p, "us"), fmt_amount(t, "us")] for d, q, p, t in items],
    )
    lay.field_below("subtotal_minor", "Subtotal:", money(subtotal), to_minor(subtotal))
    lay.field_below("tax_minor", "GST:", money(tax), to_minor(tax))
    lay.field_below("total_minor", "Amount Payable:", money(total), to_minor(total))
    lay.field_below("payment_terms", "Terms:", s.payment_terms, s.payment_terms)
    lay.fields["currency"] = {"raw": money(total), "value": s.currency, "page": 1}  # printed with the amounts
    return lay


def generate_extra_layouts(output_dir: Path | str) -> dict:
    """Documents for template tests, kept out of the main corpus so its counts stay fixed.

    - two invoices in a third, unseen layout (values below labels): no match, then match
    - a Northwind invoice with an extra "Discount applied:" line: near miss of the Northwind layout
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    documents = []
    for number, issued in (("KF-88812", date(2026, 5, 4)), ("KF-88840", date(2026, 5, 18))):
        lay = kestrel_layout(number, issued)
        filename = f"invoice_kestrel_{number.lower()}.pdf"
        (out / filename).write_bytes(render_digital(lay))
        documents.append({"filename": filename, "document_type": "invoice", "kind": "digital",
                          "page_count": lay.page, "fields": lay.fields})
    spec = InvoiceSpec(NORTHWIND, "INV-0048", date(2026, 5, 6), "dmy_short",
                       extra_line="Discount applied: 5% loyalty discount, included in unit prices")
    lay = invoice_layout(spec)
    filename = "invoice_northwind_inv-0048_discount.pdf"
    (out / filename).write_bytes(render_digital(lay))
    documents.append({"filename": filename, "document_type": "invoice", "kind": "digital",
                      "page_count": lay.page, "fields": lay.fields})
    manifest = {"buyer": _BUYER, "documents": documents}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def generate_corpus(output_dir: Path | str = DEFAULT_OUTPUT_DIR) -> dict:
    """Write every fixture PDF plus manifest.json into `output_dir`; return the manifest."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    documents = []

    def emit(filename: str, data: bytes, document_type: str, kind: str, lay: Layout | None) -> None:
        (out / filename).write_bytes(data)
        documents.append({
            "filename": filename,
            "document_type": document_type,
            "kind": kind,  # digital | scanned | corrupt
            "page_count": (lay.page if lay else None),
            "fields": (lay.fields if lay else {}),
        })

    for spec in INVOICES:
        lay = invoice_layout(spec)
        filename = f"invoice_{spec.supplier.key}_{spec.number.lower()}.pdf"
        if spec.scanned:
            emit(filename.replace(".pdf", "_scan.pdf"), render_scanned(lay), "invoice", "scanned", lay)
        else:
            emit(filename, render_digital(lay), "invoice", "digital", lay)

    for spec in ORDERS:
        lay = order_layout(spec)
        emit(f"po_{spec.number.lower()}.pdf", render_digital(lay), "purchase_order", "digital", lay)

    lay = delivery_note_layout()
    emit("delivery_note_dn-5521.pdf", render_digital(lay), "other", "digital", lay)
    emit("corrupt_upload.pdf", CORRUPT_PDF, "unknown", "corrupt", None)

    manifest = {"buyer": _BUYER, "documents": sorted(documents, key=lambda d: d["filename"])}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT_DIR
    result = generate_corpus(target)
    print(f"Wrote {len(result['documents'])} PDFs and manifest.json to {target}")
