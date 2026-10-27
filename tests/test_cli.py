"""Tests for the docintel command line (milestone M1: ingest and status)."""

from __future__ import annotations

from conftest import FakeLLM, fake_ocr, ocr_ok

from docintel import ingest as ingest_mod
from docintel import pdf_extract
from docintel.cli import main


def cli(vault, db_path, *args, config_file=None):
    extra = ["--config", str(config_file)] if config_file else []
    return main([*args, "--vault", str(vault), "--db", str(db_path), *extra])


def test_cli_ingest_then_status(vault, corpus, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(pdf_extract, "ocr_unavailable_reason", ocr_ok)
    monkeypatch.setattr(pdf_extract, "ocr_pages", fake_ocr)
    monkeypatch.setattr(ingest_mod, "make_client", lambda config: FakeLLM(corpus[1]))
    db_path = tmp_path / "app.db"
    # A config of the test's own, so learned templates go to tmp_path, not the repository.
    (tmp_path / "config").mkdir()
    config_file = tmp_path / "config" / "config.yaml"
    config_file.write_text("extraction:\n  templates_dir: templates\n")

    assert cli(vault, db_path, "ingest", config_file=config_file) == 0
    out = capsys.readouterr().out
    assert "18 PDF(s) on disk: 18 new, 0 re-read, 0 unchanged." in out
    assert "ocr_completed   invoice_northwind_inv-0046_scan.pdf" in out
    assert "FAILED        corrupt_upload.pdf: cannot open as PDF" in out
    assert "Coverage: INCOMPLETE" in out
    assert "Extracting fields:" in out
    assert "validated       invoice_bluefin_inv-1003.pdf  [invoice, match, tpl_" in out

    assert cli(vault, db_path, "show", "invoice_bluefin_inv-1003.pdf", config_file=config_file) == 0
    out = capsys.readouterr().out
    assert "record:   invoice, valid" in out
    assert "due_date             absent" in out
    assert "total_minor          found        3321.94 (EUR 3.321,94)" in out

    assert cli(vault, db_path, "templates", config_file=config_file) == 0
    assert "learned from invoice_bluefin_inv-0044.pdf" in capsys.readouterr().out
    assert (tmp_path / "config" / ".." / "templates").resolve().is_dir()

    assert cli(vault, db_path, "ingest", config_file=config_file) == 0
    assert "0 new, 0 re-read, 18 unchanged." in capsys.readouterr().out

    (vault / "corrupt_upload.pdf").unlink()
    assert cli(vault, db_path, "status", config_file=config_file) == 0
    out = capsys.readouterr().out
    assert "GONE          corrupt_upload.pdf" in out and "Coverage: INCOMPLETE" in out

    assert cli(vault, db_path, "ingest", config_file=config_file) == 0
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
