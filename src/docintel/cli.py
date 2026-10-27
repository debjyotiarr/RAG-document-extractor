"""Command-line entry point: ingest, status, ask, show, eval (BLUEPRINT §8.7).

Implemented so far: `ingest`, `status`, `show` and `templates` (milestones M1-M2).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from docintel import db
from docintel import ingest as ingest_mod
from docintel.config import ConfigError, load_config


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, vault_dir=args.vault, db_path=args.db)
        return args.handler(config, args)
    except (ConfigError, ingest_mod.VaultError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="path to config.yaml (default: config/config.yaml)")
    common.add_argument("--vault", help="folder of PDFs to use instead of the config's vault_dir")
    common.add_argument("--db", help="SQLite database to use instead of the config's db_path")

    parser = argparse.ArgumentParser(prog="docintel", description="Grounded Q&A over a folder of PDFs.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", parents=[common], help="read new and changed PDFs into the database")
    p.add_argument("--force", action="store_true", help="re-read every PDF, even unchanged ones")
    p.set_defaults(handler=_cmd_ingest)

    p = sub.add_parser("status", parents=[common], help="show what has and has not been read")
    p.set_defaults(handler=_cmd_status)

    p = sub.add_parser("show", parents=[common], help="show one document's extracted fields and their sources")
    p.add_argument("document", help="document id (doc_...) or file name / path inside the vault")
    p.set_defaults(handler=_cmd_show)

    p = sub.add_parser("templates", parents=[common], help="list the layout templates learned so far")
    p.set_defaults(handler=_cmd_templates)
    return parser


def _cmd_ingest(config, args) -> int:
    print(f"Vault: {config.vault_dir}\nReading text:")

    def progress(rel: str, status: str) -> None:
        print(f"  {status:<15} {rel}", flush=True)

    printed_header = False

    def fields(rel: str, outcome) -> None:
        nonlocal printed_header
        if not printed_header:
            print("Extracting fields:")
            printed_header = True
        how = outcome.template_key or "LLM only"
        print(f"  {outcome.status:<15} {rel}  [{outcome.document_type}, {outcome.match or '-'}, {how}]", flush=True)

    report = ingest_mod.ingest(config, force=args.force, on_progress=progress, on_fields=fields)
    print(
        f"\n{report.files_on_disk} PDF(s) on disk: {len(report.new)} new, {len(report.reprocessed)} re-read,"
        f" {len(report.unchanged)} unchanged. Fields extracted for {len(report.extracted)}."
    )
    for old, new in report.renamed:
        print(f"  renamed: {old} -> {new}")
    for path, same_as in report.duplicates:
        print(f"  duplicate: {path} is identical to {same_as} (stored once)")
    for path in report.removed:
        print(f"  removed from database (no longer in vault): {path}")
    print()
    _print_coverage(ingest_mod.coverage(config))
    return 0


def _cmd_status(config, args) -> int:
    print(f"Vault: {config.vault_dir}\nDatabase: {config.db_path}\n")
    _print_coverage(ingest_mod.coverage(config))
    return 0


def _print_coverage(cov: ingest_mod.Coverage) -> None:
    counts = ", ".join(f"{n} {status}" for status, n in sorted(cov.by_status.items()))
    print(f"Documents: {cov.documents} of {cov.files_on_disk} PDF file(s) on disk" + (f" ({counts})" if counts else ""))
    for path, error in cov.failed:
        print(f"  FAILED        {path}: {error}")
    for path, error in cov.ocr_required:
        print(f"  NEEDS OCR     {path}: {error}")
    for path in cov.not_ingested:
        print(f"  NOT INGESTED  {path} (new or changed; run docintel ingest)")
    for path in cov.missing_from_disk:
        print(f"  GONE          {path} (deleted from vault; run docintel ingest)")
    for path, why in cov.fields_pending:
        print(f"  NO FIELDS YET {path}: {why}")
    for path, why in cov.needs_review:
        print(f"  REVIEW        {path}: {why}")
    if cov.is_complete:
        print("Coverage: complete. Every PDF in the vault is searchable.")
    else:
        print("Coverage: INCOMPLETE. Answers will say so rather than report information as not found.")
    if cov.fields_pending:
        print(f"{len(cov.fields_pending)} document(s) are searchable but not yet in the structured fields.")


def _cmd_show(config, args) -> int:
    conn = db.connect(config.db_path)
    try:
        doc = conn.execute(
            "SELECT * FROM documents WHERE document_id = ? OR relative_path = ? OR filename = ?"
            " ORDER BY relative_path LIMIT 1",
            (args.document, args.document, args.document),
        ).fetchone()
        if doc is None:
            print(f"error: no document {args.document!r} in {config.db_path}", file=sys.stderr)
            return 1
        print(f"{doc['relative_path']}  ({doc['document_id']})")
        print(f"  status:   {doc['processing_status']}")
        print(f"  type:     {doc['document_type'] or '-'}")
        template = f"{doc['template_id']}.v{doc['template_version']}" if doc["template_id"] else "-"
        print(f"  template: {template} ({doc['template_match'] or '-'})")
        if doc["error_message"]:
            print(f"  notes:    {doc['error_message']}")
        record = conn.execute("SELECT * FROM records WHERE document_id = ?", (doc["document_id"],)).fetchone()
        if record is None:
            print("  (no extracted record)")
            return 0
        print(f"  record:   {record['record_type']}, {record['validation_status']}\n")
        print(f"  {'field':<20} {'presence':<12} {'value':<28} {'page':<5} {'status':<13} method")
        for e in conn.execute(
            "SELECT * FROM field_evidence WHERE record_id = ? ORDER BY evidence_id", (record["record_id"],)
        ):
            value = e["normalized_value"] or e["raw_value"] or ""
            if e["raw_value"] and e["normalized_value"] and e["raw_value"] != e["normalized_value"]:
                value = f"{e['normalized_value']} ({e['raw_value']})"
            print(
                f"  {e['field_name']:<20} {e['presence']:<12} {value[:28]:<28} {e['source_page'] or '':<5}"
                f" {e['validation_status']:<13} {e['extraction_method']}"
            )
    finally:
        conn.close()
    return 0


def _cmd_templates(config, args) -> int:
    conn = db.connect(config.db_path)
    try:
        rows = conn.execute("SELECT * FROM templates ORDER BY document_type, template_id, version").fetchall()
        used = dict(conn.execute(
            "SELECT template_id || '.v' || template_version, COUNT(*) FROM documents"
            " WHERE template_id IS NOT NULL GROUP BY 1"
        ).fetchall())
    finally:
        conn.close()
    if not rows:
        print("No templates learned yet.")
        return 0
    print(f"Templates in {config.extraction.templates_dir}:")
    for r in rows:
        key = f"{r['template_id']}.v{r['version']}"
        print(
            f"  {key:<18} {r['document_type']:<15} {r['status']:<10} agreed {r['docs_agreed']},"
            f" disagreed {r['docs_disagreed']}, documents {used.get(key, 0)}, learned from {r['learned_from_file']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
