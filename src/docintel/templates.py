"""Layout fingerprints, template matching, induction and lifecycle (BLUEPRINT §8.4).

A *template* describes one document layout: the labels printed on it (with
their positions) and, for each field, a rule saying where its value sits
relative to a label. The LLM reads the first document of a new layout; this
module turns those values into rules, checks the rules reproduce them, and
from then on parses that layout in plain Python.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median

import yaml

from docintel.config import TemplateMatchConfig
from docintel.validate import CORE_FIELDS, collapse, document_date_order

# Labels that carry no layout information.
_IGNORED_LABELS = {"page", "page of"}
# Common labels printed without a colon, matched at the start of a line (longest first).
_LEXICON = sorted(
    [
        "invoice number", "invoice no", "invoice date", "due date", "date of issue", "issue date",
        "order number", "order date", "po number", "purchase order number", "grand total", "total due",
        "amount due", "balance due", "total amount", "subtotal", "sub total", "net amount", "net total",
        "total", "payment terms", "terms", "vat number", "vat no", "tax id", "currency",
    ],
    key=len,
    reverse=True,
)
_LABEL_END = re.compile(r"(:|#)$|^(no|nr|num)\.$", re.IGNORECASE)
_TITLE_HEIGHT_RATIO = 1.25  # OCR boxes hug glyphs, so titles measure only ~1.3x body text
_SAME_LINE_GAP = 0.15  # a horizontal gap wider than this (fraction of page) ends a value
_BELOW_MAX_GAP = 0.06  # how far below a label its value may start (fraction of page)


def normalize_label(text: str) -> str:
    return " ".join(re.sub(r"[^a-z]+", " ", text.casefold()).split())


def similarity(a: str, b: str) -> float:
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()


# ------------------------------------------------------------------ page layout


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


@dataclass
class Label:
    text: str  # normalised
    kind: str  # 'colon' | 'lexicon' | 'title'
    page: int
    line: Line
    start: int  # token index in the line where the label starts
    end: int  # token index after the label's last token
    value_end: int  # token index after the value that follows the label on the same line

    @property
    def x(self) -> float:
        return (self.line.words[self.start].x0 + self.line.words[self.end - 1].x1) / 2

    @property
    def y(self) -> float:
        return (self.line.words[self.start].y0 + self.line.words[self.end - 1].y1) / 2

    @property
    def value_words(self) -> list[Word]:
        return self.line.words[self.end:self.value_end]


@dataclass(eq=False)
class Line:
    page: int
    words: list[Word]
    labels: list[Label] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def y0(self) -> float:
        return min(w.y0 for w in self.words)

    @property
    def y1(self) -> float:
        return max(w.y1 for w in self.words)


@dataclass
class Layout:
    """The lines and labels of one document, built from document_pages.words_json."""

    lines: list[Line]
    page_texts: dict[int, str]

    @property
    def labels(self) -> list[Label]:
        return [label for line in self.lines for label in line.labels]

    def lines_on(self, page: int) -> list[Line]:
        return [line for line in self.lines if line.page == page]


def build_layout(pages: list[tuple[int, str, str | None]]) -> Layout:
    """`pages` is (page_number, page_text, words_json) per page."""
    lines: list[Line] = []
    for page_number, _, words_json in pages:
        grouped: dict[tuple[int, int], list[Word]] = {}
        for x0, y0, x1, y1, text, block, line in json.loads(words_json or "[]"):
            grouped.setdefault((block, line), []).append(Word(x0, y0, x1, y1, text))
        page_lines = [Line(page_number, sorted(ws, key=lambda w: w.x0)) for ws in grouped.values()]
        page_lines.sort(key=lambda ln: (round(ln.y0, 3), ln.x0))
        heights = [w.y1 - w.y0 for ln in page_lines for w in ln.words]
        typical = median(heights) if heights else 0
        for ln in page_lines:
            _find_labels(ln, typical)
        lines.extend(page_lines)
    return Layout(lines, {n: text for n, text, _ in pages})


def _find_labels(line: Line, typical_height: float) -> None:
    words = line.words
    tokens = [w.text for w in words]

    def gap(i: int) -> float:  # horizontal gap before token i
        return words[i].x0 - words[i - 1].x1 if i > 0 else 0.0

    # Labels ending in ':' or '#' (or "No."), extended back over digit-free words.
    ends = [i for i, t in enumerate(tokens) if _LABEL_END.search(t)]
    previous_end = 0
    spans = []
    for i in ends:
        start = i
        while (
            start > previous_end
            and i - start < 4
            and not any(c.isdigit() for c in tokens[start - 1])
            and not _LABEL_END.search(tokens[start - 1])
            and not tokens[start - 1].endswith((",", ";"))  # end of the previous value
            and gap(start) < _SAME_LINE_GAP
        ):
            start -= 1
        if start < previous_end:
            continue
        spans.append((start, i + 1))
        previous_end = i + 1
    for n, (start, end) in enumerate(spans):
        text = normalize_label(" ".join(tokens[start:end]))
        if not text or text in _IGNORED_LABELS:
            continue
        limit = spans[n + 1][0] if n + 1 < len(spans) else len(words)
        value_end = end
        while value_end < limit and (value_end == end or gap(value_end) < _SAME_LINE_GAP):
            value_end += 1
        line.labels.append(Label(text, "colon", line.page, line, start, end, value_end))
    if line.labels:
        return

    # A common label without a colon at the start of the line ("Grand Total EUR 1.225,46").
    joined = normalize_label(" ".join(tokens))
    for phrase in _LEXICON:
        if joined == phrase or joined.startswith(phrase + " "):
            end, consumed = 0, ""
            while end < len(tokens) and consumed != phrase:
                consumed = normalize_label(" ".join(tokens[: end + 1]))
                end += 1
            if consumed == phrase:
                line.labels.append(Label(phrase, "lexicon", line.page, line, 0, end, len(words)))
            return

    # A document title: short, no digits, noticeably larger than the page's body text.
    height = median(w.y1 - w.y0 for w in words)
    if (
        typical_height
        and height >= _TITLE_HEIGHT_RATIO * typical_height
        and len(words) <= 5
        and not any(c.isdigit() for c in "".join(tokens))
    ):
        text = normalize_label(" ".join(tokens))
        if text:
            line.labels.append(Label(text, "title", line.page, line, 0, len(words), len(words)))


# -------------------------------------------------------------------- templates


@dataclass
class TemplateLabel:
    text: str
    kind: str
    page: int
    x: float
    y: float
    required: bool = False


@dataclass
class FieldRule:
    relation: str  # 'right_of' | 'below' | 'at_position'
    page: int
    x: float  # expected position: of the anchor label, or of the value for at_position
    y: float
    anchor: str | None = None
    date_order: str | None = None  # 'dmy' | 'mdy' for numeric dates, learned from an unambiguous one


@dataclass
class Template:
    template_id: str
    version: int
    document_type: str
    labels: list[TemplateLabel]
    rules: dict[str, FieldRule]
    learned_from_file: str
    status: str = "probation"

    @property
    def key(self) -> str:
        return f"{self.template_id}.v{self.version}"

    @property
    def required(self) -> list[TemplateLabel]:
        return [label for label in self.labels if label.required]

    def to_yaml(self) -> str:
        body = {
            "template_id": self.template_id,
            "version": self.version,
            "document_type": self.document_type,
            "learned_from": self.learned_from_file,
            "labels": [asdict(label) for label in self.labels],
            "rules": {
                name: {k: v for k, v in asdict(rule).items() if v is not None} for name, rule in self.rules.items()
            },
        }
        header = (
            "# Generated by docintel from one document of this layout (BLUEPRINT §8.4).\n"
            "# You may edit it; an edited template goes back on probation.\n"
        )
        return header + yaml.safe_dump(body, sort_keys=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, text: str, status: str) -> Template:
        body = yaml.safe_load(text)
        return cls(
            template_id=body["template_id"],
            version=int(body["version"]),
            document_type=body["document_type"],
            labels=[TemplateLabel(**label) for label in body["labels"]],
            rules={name: FieldRule(**rule) for name, rule in body["rules"].items()},
            learned_from_file=body["learned_from"],
            status=status,
        )


# --------------------------------------------------------------------- matching


@dataclass
class MatchResult:
    outcome: str  # 'match' | 'near_miss' | 'no_match'
    template: Template | None  # the matched template, or the parent of a near miss
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class _Score:
    template: Template
    score: float
    missing: list[str]
    moved: list[str]
    unknown: list[str]
    overlap: float

    @property
    def is_match(self) -> bool:
        return self.score == 1.0 and not self.moved and not self.unknown


def _score(template: Template, layout: Layout, cfg: TemplateMatchConfig) -> _Score:
    doc_labels = layout.labels
    required = template.required
    found, missing, moved = 0, [], []
    for want in required:
        candidates = [d for d in doc_labels if similarity(d.text, want.text) >= cfg.label_similarity]
        if not candidates:
            missing.append(want.text)
            continue
        found += 1
        best = min(candidates, key=lambda d: (d.page != want.page, max(abs(d.x - want.x), abs(d.y - want.y))))
        if best.page != want.page or max(abs(best.x - want.x), abs(best.y - want.y)) > cfg.moved_tolerance:
            moved.append(want.text)
    known = {label.text for label in template.labels}
    unknown = sorted({
        d.text for d in doc_labels
        if d.kind != "title" and not any(similarity(d.text, k) >= cfg.label_similarity for k in known)
    })
    doc_texts = {d.text for d in doc_labels}
    overlap = len(doc_texts & known) / len(doc_texts | known) if doc_texts | known else 0.0
    score = found / len(required) if required else 0.0
    return _Score(template, score, missing, moved, unknown, overlap)


def match(layout: Layout, templates: list[Template], cfg: TemplateMatchConfig) -> MatchResult:
    """Compare a document with the active templates of its document type."""
    scores = [_score(t, layout, cfg) for t in templates]
    matches = [s for s in scores if s.is_match]
    if matches:
        rank = lambda s: (len(s.template.required), round(s.overlap, 6))  # noqa: E731
        best_rank = max(rank(s) for s in matches)
        tied = [s for s in matches if rank(s) == best_rank]
        families = {s.template.template_id for s in tied}
        best = max(tied, key=lambda s: (s.template.status == "trusted", s.template.version))
        if len(families) == 1:
            return MatchResult("match", best.template, 1.0)
        return MatchResult("near_miss", best.template, 1.0, [f"matches several layouts: {sorted(families)}"])

    near = [s for s in scores if s.score >= cfg.required_threshold]
    if near:
        # The parent is the template this document differs from least structurally.
        best = max(near, key=lambda s: (s.score, -len(s.moved), -len(s.unknown), s.overlap))
        reasons = (
            [f"required label missing: {t}" for t in best.missing]
            + [f"required label moved: {t}" for t in best.moved]
            + [f"label not in template: {t}" for t in best.unknown]
        )
        return MatchResult("near_miss", best.template, best.score, reasons)
    top = max((s.score for s in scores), default=0.0)
    return MatchResult("no_match", None, top)


# -------------------------------------------------------------- applying rules


@dataclass
class Found:
    raw: str
    page: int


def _find_anchor(layout: Layout, rule: FieldRule, cfg: TemplateMatchConfig) -> Label | None:
    candidates = [label for label in layout.labels if similarity(label.text, rule.anchor) >= cfg.label_similarity]
    if not candidates:
        return None
    return min(candidates, key=lambda d: (d.page != rule.page, max(abs(d.x - rule.x), abs(d.y - rule.y))))


def _value_below(layout: Layout, label: Label) -> list[Word]:
    below = [
        ln for ln in layout.lines_on(label.page)
        if ln is not label.line and ln.y0 >= label.line.y1 - 0.002 and ln.y0 - label.line.y1 <= _BELOW_MAX_GAP
    ]
    left = label.line.words[label.start].x0
    for ln in sorted(below, key=lambda ln: ln.y0):
        words = [w for w in ln.words if w.x0 >= left - 0.05]
        if not words or words[0].x0 > label.line.words[label.end - 1].x1 + 0.05:
            continue
        run = [words[0]]
        for w in words[1:]:
            if w.x0 - run[-1].x1 >= _SAME_LINE_GAP:
                break
            run.append(w)
        return run
    return []


def apply_rule(layout: Layout, rule: FieldRule, cfg: TemplateMatchConfig) -> Found | None:
    """Read one field's value according to its rule; None means the field is absent."""
    if rule.relation == "at_position":
        lines = [ln for ln in layout.lines_on(rule.page) if not ln.labels]
        if not lines:
            return None
        best = min(lines, key=lambda ln: max(abs((ln.x0 + ln.x1) / 2 - rule.x), abs((ln.y0 + ln.y1) / 2 - rule.y)))
        if max(abs((best.x0 + best.x1) / 2 - rule.x), abs((best.y0 + best.y1) / 2 - rule.y)) > cfg.moved_tolerance:
            return None
        return Found(best.text, best.page)
    label = _find_anchor(layout, rule, cfg)
    if label is None:
        return None
    words = label.value_words if rule.relation == "right_of" else _value_below(layout, label)
    if not words:
        return None
    return Found(" ".join(w.text for w in words), label.page)


def apply_template(template: Template, layout: Layout, cfg: TemplateMatchConfig) -> dict[str, Found | None]:
    return {name: apply_rule(layout, rule, cfg) for name, rule in template.rules.items()}


# -------------------------------------------------------------------- induction


def _locate(layout: Layout, raw: str, page: int) -> list[tuple[Line, int, int]]:
    """Token runs on `page` whose text is `raw` (or contains it, failing an exact run)."""
    target = collapse(raw)
    exact, containing = [], []
    for line in layout.lines_on(page):
        tokens = [w.text for w in line.words]
        for i in range(len(tokens)):
            for j in range(i + 1, min(len(tokens), i + 16) + 1):
                text = " ".join(tokens[i:j])
                if text == target:
                    exact.append((line, i, j))
                elif target in text and not containing:
                    containing.append((line, i, j))
                if len(text) > len(target) + 20:
                    break
    return exact or containing


def _rule_for(layout: Layout, field_name: str, raw: str, page: int) -> FieldRule | None:
    occurrences = _locate(layout, raw, page)
    # Prefer an occurrence that directly follows a label on its own line.
    for line, i, _ in occurrences:
        for label in line.labels:
            if label.kind != "title" and label.end <= i < label.value_end:
                return FieldRule("right_of", label.page, round(label.x, 4), round(label.y, 4), anchor=label.text)
    # Otherwise a label line directly above, with nothing else on it.
    for line, i, _ in occurrences:
        value_x = line.words[i].x0
        above = [
            ln for ln in layout.lines_on(line.page)
            if ln.labels and ln.y1 <= line.y0 + 0.002 and line.y0 - ln.y1 <= _BELOW_MAX_GAP
        ]
        for ln in sorted(above, key=lambda ln: -ln.y1):
            for label in ln.labels:
                left = ln.words[label.start].x0
                if label.kind != "title" and label.value_end == label.end and abs(left - value_x) <= 0.05:
                    return FieldRule("below", label.page, round(label.x, 4), round(label.y, 4), anchor=label.text)
    # The issuer's name is often printed with no label: remember where it sits.
    if field_name == "counterparty_name" and occurrences:
        line, _, _ = occurrences[0]
        if not line.labels and collapse(line.text) == collapse(raw):
            centre = (round((line.x0 + line.x1) / 2, 4), round((line.y0 + line.y1) / 2, 4))
            return FieldRule("at_position", line.page, *centre)
    return None


def induce(
    layout: Layout, values: dict[str, Found], document_type: str, learned_from_file: str
) -> tuple[Template | None, str | None]:
    """Build a template whose rules reproduce `values` (already through the verbatim gate).

    Returns (template, None) or (None, reason). The template id and version are
    provisional; TemplateStore assigns the final ones.
    """
    rules: dict[str, FieldRule] = {}
    # A layout prints all its dates one way, so one unambiguous date settles the order for all.
    date_order = document_date_order([f.raw for n, f in values.items() if n.endswith("date")])
    for name, found in values.items():
        rule = _rule_for(layout, name, found.raw, found.page)
        if rule is None:
            continue
        if name.endswith("date"):
            rule.date_order = date_order
        rules[name] = rule

    labels: list[TemplateLabel] = []
    required_anchors = {(r.anchor, r.page) for n, r in rules.items() if n in CORE_FIELDS and r.anchor}
    for label in layout.labels:
        required = (label.kind == "title" and label.page == 1) or (label.text, label.page) in required_anchors
        labels.append(TemplateLabel(label.text, label.kind, label.page, round(label.x, 4), round(label.y, 4), required))
    # A label text printed twice (e.g. repeated on a continuation page) is required once at most.
    seen: set[tuple[str, int]] = set()
    for label in labels:
        if label.required:
            if (label.text, label.page) in seen:
                label.required = False
            seen.add((label.text, label.page))

    required_texts = sorted({label.text for label in labels if label.required})
    if not any(n in rules for n in CORE_FIELDS) or len(required_texts) < 2:
        return None, "too few labelled core fields to fingerprint this layout"
    digest = hashlib.sha1((document_type + "|" + "|".join(required_texts)).encode()).hexdigest()[:8]
    return Template(f"tpl_{digest}", 1, document_type, labels, rules, learned_from_file), None


# -------------------------------------------------------------------- lifecycle


class TemplateStore:
    """Learned templates: rules in YAML files, lifecycle state in the `templates` table."""

    def __init__(self, conn: sqlite3.Connection, templates_dir: Path, cfg: TemplateMatchConfig):
        self.conn = conn
        self.dir = Path(templates_dir)
        self.cfg = cfg

    def _path(self, template_id: str, version: int) -> Path:
        return self.dir / f"{template_id}.v{version}.yaml"

    def active(self, document_type: str) -> list[Template]:
        """Probation and trusted templates for a document type, loaded from their YAML files."""
        result = []
        rows = self.conn.execute(
            "SELECT * FROM templates WHERE document_type = ? AND status != 'retired' ORDER BY template_id, version",
            (document_type,),
        ).fetchall()
        for row in rows:
            path = self._path(row["template_id"], row["version"])
            if not path.is_file():
                self._set_status(row["template_id"], row["version"], "retired")
                continue
            text = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode()).hexdigest()
            status = row["status"]
            if digest != row["yaml_sha256"]:  # edited by hand: earn trust again
                status = "probation"
                with self.conn:
                    self.conn.execute(
                        "UPDATE templates SET status = 'probation', docs_agreed = 0, docs_disagreed = 0,"
                        " yaml_sha256 = ? WHERE template_id = ? AND version = ?",
                        (digest, row["template_id"], row["version"]),
                    )
            try:
                result.append(Template.from_yaml(text, status))
            except (yaml.YAMLError, KeyError, TypeError, ValueError):
                self._set_status(row["template_id"], row["version"], "retired")
        return result

    def save(self, template: Template, *, parent: Template | None, document_id: str) -> Template:
        """Store a newly learned template. A near miss becomes a new version of its parent."""
        if parent is not None:
            template.template_id = parent.template_id
        latest = self.conn.execute(
            "SELECT MAX(version) FROM templates WHERE template_id = ?", (template.template_id,)
        ).fetchone()[0]
        template.version = (latest or 0) + 1
        template.status = "trusted" if self.cfg.probation_docs == 0 else "probation"
        self.dir.mkdir(parents=True, exist_ok=True)
        text = template.to_yaml()
        self._path(template.template_id, template.version).write_text(text, encoding="utf-8")
        with self.conn:
            self.conn.execute(
                "INSERT INTO templates (template_id, version, document_type, status, learned_from, learned_from_file,"
                " yaml_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (template.template_id, template.version, template.document_type, template.status, document_id,
                 template.learned_from_file, hashlib.sha256(text.encode()).hexdigest(),
                 datetime.now(tz=timezone.utc).isoformat(timespec="seconds")),
            )
        return template

    def record_probation(self, template: Template, agreed: bool) -> str:
        """Count one probation check; promote or retire the template. Returns its new status."""
        column = "docs_agreed" if agreed else "docs_disagreed"
        with self.conn:
            self.conn.execute(
                f"UPDATE templates SET {column} = {column} + 1 WHERE template_id = ? AND version = ?",
                (template.template_id, template.version),
            )
            row = self.conn.execute(
                "SELECT docs_agreed, docs_disagreed FROM templates WHERE template_id = ? AND version = ?",
                (template.template_id, template.version),
            ).fetchone()
        status = template.status
        if row["docs_disagreed"] >= 2:
            status = "retired"
        elif row["docs_agreed"] >= self.cfg.probation_docs:
            status = "trusted"
        if status != template.status:
            self._set_status(template.template_id, template.version, status)
            template.status = status
        return status

    def _set_status(self, template_id: str, version: int, status: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE templates SET status = ? WHERE template_id = ? AND version = ?", (status, template_id, version)
            )
