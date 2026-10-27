"""Tests for the docintel command line (milestone M1: ingest and status)."""

from __future__ import annotations

from conftest import fake_ocr, ocr_ok

from docintel import pdf_extract
from docintel.cli import main


def cli(vault, db_path, *args):
    return main([*args, "--vault", str(vault), "--db", str(db_path)])


def test_cli_ingest_then_status(vault, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(pdf_extract, "ocr_unavailable_reason", ocr_ok)
    monkeypatch.setattr(pdf_extract, "ocr_pages", fake_ocr)
    db_path = tmp_path / "app.db"

    assert cli(vault, db_path, "ingest") == 0
    out = capsys.readouterr().out
    assert "18 PDF(s) on disk: 18 new, 0 re-read, 0 unchanged." in out
    assert "ocr_completed   invoice_northwind_inv-0046_scan.pdf" in out
    assert "FAILED        corrupt_upload.pdf: cannot open as PDF" in out
    assert "Coverage: INCOMPLETE" in out

    assert cli(vault, db_path, "ingest") == 0
    assert "0 new, 0 re-read, 18 unchanged." in capsys.readouterr().out

    (vault / "corrupt_upload.pdf").unlink()
    assert cli(vault, db_path, "status") == 0
    out = capsys.readouterr().out
    assert "GONE          corrupt_upload.pdf" in out and "Coverage: INCOMPLETE" in out

    assert cli(vault, db_path, "ingest") == 0
    out = capsys.readouterr().out
    assert "removed from database (no longer in vault): corrupt_upload.pdf" in out
    assert "Coverage: complete" in out


def test_cli_reports_missing_vault_as_error(tmp_path, capsys):
    assert cli(tmp_path / "nope", tmp_path / "app.db", "ingest") == 1
    assert "Vault folder not found" in capsys.readouterr().err


def test_cli_reports_bad_config_as_error(tmp_path, capsys):
    bad = tmp_path / "config.yaml"
    bad.write_text("vault_dri: typo\n")
    assert main(["status", "--config", str(bad)]) == 1
    assert "vault_dri" in capsys.readouterr().err
