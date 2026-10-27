"""Tests for loading and validating config/config.yaml (milestone M0)."""

from __future__ import annotations

from pathlib import Path

import pytest

from docintel.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, ConfigError, load_config


def write_config(tmp_path: Path, text: str) -> Path:
    (tmp_path / "config").mkdir()
    path = tmp_path / "config" / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_repo_config_loads_with_pinned_decisions():
    config = load_config(DEFAULT_CONFIG_PATH)
    assert config.vault_dir == PROJECT_ROOT / "data" / "vault"
    assert config.db_path == PROJECT_ROOT / "data" / "app.db"
    assert config.extraction.templates_dir == PROJECT_ROOT / "config" / "templates"
    assert config.llm.provider == "ollama"
    assert config.llm.ollama.model == "qwen2.5:7b-instruct"
    assert config.agent.max_tool_calls == 6


def test_relative_paths_resolve_against_project_root_not_cwd(tmp_path, monkeypatch):
    path = write_config(tmp_path, "vault_dir: my_pdfs\n")
    monkeypatch.chdir("/")
    assert load_config(path).vault_dir == tmp_path / "my_pdfs"


def test_absolute_vault_path_is_kept(tmp_path):
    path = write_config(tmp_path, f"vault_dir: {tmp_path / 'elsewhere'}\n")
    assert load_config(path).vault_dir == tmp_path / "elsewhere"


def test_override_wins_and_resolves_against_cwd(tmp_path, monkeypatch):
    path = write_config(tmp_path, "vault_dir: from_file\n")
    monkeypatch.chdir(tmp_path)
    config = load_config(path, vault_dir="from_cli", db_path="cli.db")
    assert config.vault_dir == tmp_path / "from_cli"
    assert config.db_path == tmp_path / "cli.db"


def test_empty_file_gives_defaults(tmp_path):
    config = load_config(write_config(tmp_path, ""))
    assert config.vault_dir == tmp_path / "data" / "vault"
    assert config.ocr.min_chars_per_page == 32


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    "text, message",
    [
        ("vault_dir: [unclosed\n", "not valid YAML"),
        ("- just\n- a list\n", "mapping"),
        ("vault_dri: typo\n", "vault_dri"),
        ("llm:\n  provider: openai\n", "provider"),
        ("agent:\n  max_tool_calls: 0\n", "max_tool_calls"),
        ("ocr:\n  enabled: true\n  min_chars: 10\n", "min_chars"),
    ],
)
def test_invalid_config_raises_with_useful_message(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, text))
