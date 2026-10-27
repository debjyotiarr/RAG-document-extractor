"""Load and validate config/config.yaml (BLUEPRINT §6).

This is the only module that reads the YAML file. Every other module gets a
validated `Config` object, with all paths already resolved to absolute paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# The project root is the folder that holds config/ and src/.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


class ConfigError(Exception):
    """The config file is missing, malformed, or has invalid values."""


class _Section(BaseModel):
    # Reject unknown keys so a typo in config.yaml fails loudly instead of being ignored.
    model_config = ConfigDict(extra="forbid")


class OcrConfig(_Section):
    enabled: bool = True
    min_chars_per_page: int = Field(32, ge=1)
    language: str = "eng"


class OllamaConfig(_Section):
    model: str = "qwen2.5:7b-instruct"
    base_url: str = "http://localhost:11434"


class AnthropicConfig(_Section):
    model: str = "claude-haiku-5-5"


class LlmConfig(_Section):
    provider: Literal["ollama", "anthropic"] = "ollama"
    ollama: OllamaConfig = OllamaConfig()
    anthropic: AnthropicConfig = AnthropicConfig()


class AgentConfig(_Section):
    max_tool_calls: int = Field(6, ge=1, le=20)
    max_retries_per_tool: int = Field(1, ge=0, le=3)


class ExtractionConfig(_Section):
    templates_dir: Path = Path("config/templates")
    use_llm_fallback: bool = True


class Config(_Section):
    vault_dir: Path = Path("data/vault")
    db_path: Path = Path("data/app.db")
    ocr: OcrConfig = OcrConfig()
    llm: LlmConfig = LlmConfig()
    agent: AgentConfig = AgentConfig()
    extraction: ExtractionConfig = ExtractionConfig()


def load_config(
    path: Path | str | None = None,
    *,
    vault_dir: Path | str | None = None,
    db_path: Path | str | None = None,
) -> Config:
    """Read, validate and resolve the config.

    `vault_dir` and `db_path` override the file's values (for example from a
    CLI `--vault` flag). Relative paths in the file resolve against the project
    root; relative override paths resolve against the current working directory,
    the way a user typing a path on the command line expects.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config file is not valid YAML: {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file must contain a mapping at the top level: {config_path}")

    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid config in {config_path}:\n{exc}") from exc

    # Paths in the file are relative to the project root (the parent of config/).
    root = config_path.resolve().parent.parent
    config.vault_dir = _resolve(config.vault_dir, root)
    config.db_path = _resolve(config.db_path, root)
    config.extraction.templates_dir = _resolve(config.extraction.templates_dir, root)

    if vault_dir is not None:
        config.vault_dir = Path(vault_dir).expanduser().resolve()
    if db_path is not None:
        config.db_path = Path(db_path).expanduser().resolve()
    return config


def _resolve(path: Path, root: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else (root / path).resolve()
