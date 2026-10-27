"""Command-line entry point: ingest, status, ask, show, eval (BLUEPRINT §8.7).

Implemented so far: `ingest` and `status` (milestone M1).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

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
    return parser


def _cmd_ingest(config, args) -> int:
    print(f"Vault: {config.vault_dir}")

    def progress(rel: str, status: str) -> None:
        print(f"  {status:<15} {rel}", flush=True)

    report = ingest_mod.ingest(config, force=args.force, on_progress=progress)
    print(
        f"\n{report.files_on_disk} PDF(s) on disk: {len(report.new)} new, {len(report.reprocessed)} re-read,"
        f" {len(report.unchanged)} unchanged."
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
    if cov.is_complete:
        print("Coverage: complete. Every PDF in the vault is searchable.")
    else:
        print("Coverage: INCOMPLETE. Answers will say so rather than report information as not found.")


if __name__ == "__main__":
    sys.exit(main())
