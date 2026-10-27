"""Tests for the LLM client (milestone M2).

The Ollama driver's error handling is tested with a stubbed client. The live
test talks to a real Ollama and only runs when DOCINTEL_LIVE_OLLAMA=1, e.g.

    DOCINTEL_LIVE_OLLAMA=1 pytest tests/test_llm.py -s
"""

from __future__ import annotations

import os
import shutil
from types import SimpleNamespace

import ollama
import pytest
from conftest import fake_ocr, ocr_ok

from docintel import db
from docintel.config import DEFAULT_CONFIG_PATH, load_config
from docintel.ingest import ingest
from docintel.llm import LLMError, LLMUnavailable, OllamaClient, make_client


class StubClient:
    """Replaces ollama.Client; `behaviour` decides what chat() does."""

    instances = 0

    def __init__(self, behaviour):
        self.behaviour = behaviour

    def __call__(self, host, timeout):
        StubClient.instances += 1
        return self

    def chat(self, **kwargs):
        return self.behaviour(**kwargs)


def use(monkeypatch, behaviour):
    StubClient.instances = 0
    monkeypatch.setattr(ollama, "Client", StubClient(behaviour))


def reply(content):
    return lambda **kwargs: SimpleNamespace(message=SimpleNamespace(content=content))


def test_returns_parsed_json_and_asks_for_deterministic_output(monkeypatch):
    seen = {}

    def behaviour(**kwargs):
        seen.update(kwargs)
        return reply('{"document_type": "invoice", "confidence": "high"}')()

    use(monkeypatch, behaviour)
    schema = {"type": "object"}
    answer = OllamaClient("qwen2.5:7b-instruct", "http://localhost:11434").complete_json("sys", "user", schema)
    assert answer == {"document_type": "invoice", "confidence": "high"}
    assert seen["format"] is schema and seen["options"]["temperature"] == 0
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]


def test_unreachable_server_is_unavailable_and_fails_fast(monkeypatch):
    def behaviour(**kwargs):
        raise ConnectionError("Failed to connect to Ollama")

    use(monkeypatch, behaviour)
    client = OllamaClient("m", "http://localhost:11434")
    with pytest.raises(LLMUnavailable, match="cannot reach Ollama"):
        client.complete_json("s", "u", {})
    with pytest.raises(LLMUnavailable):
        client.complete_json("s", "u", {})
    assert StubClient.instances == 1  # the second call did not try again


def test_missing_model_says_how_to_install_it(monkeypatch):
    def behaviour(**kwargs):
        raise ollama.ResponseError("model not found", 404)

    use(monkeypatch, behaviour)
    with pytest.raises(LLMUnavailable, match="ollama pull qwen2.5:7b-instruct"):
        OllamaClient("qwen2.5:7b-instruct", "http://localhost:11434").complete_json("s", "u", {})


@pytest.mark.parametrize("content", ["not json", "[1, 2]", ""])
def test_bad_output_is_an_error_not_unavailability(monkeypatch, content):
    use(monkeypatch, reply(content))
    with pytest.raises(LLMError):
        OllamaClient("m", "http://localhost:11434").complete_json("s", "u", {})


def test_other_server_errors_are_errors(monkeypatch):
    def behaviour(**kwargs):
        raise ollama.ResponseError("out of memory", 500)

    use(monkeypatch, behaviour)
    with pytest.raises(LLMError, match="out of memory"):
        OllamaClient("m", "http://localhost:11434").complete_json("s", "u", {})


def test_anthropic_provider_is_not_available_yet():
    config = load_config(DEFAULT_CONFIG_PATH)
    config.llm.provider = "anthropic"
    with pytest.raises(LLMUnavailable, match="M4"):
        make_client(config).complete_json("s", "u", {})


@pytest.mark.skipif(os.environ.get("DOCINTEL_LIVE_OLLAMA") != "1", reason="set DOCINTEL_LIVE_OLLAMA=1 to run")
def test_live_ollama_extracts_one_layout(corpus, tmp_path):
    """Real qwen2.5 on two purchase orders: learn the layout from one, parse the other by template."""
    directory, manifest = corpus
    vault = tmp_path / "vault"
    vault.mkdir()
    for name in ("po_po-2026-0003.pdf", "po_po-2026-0005.pdf"):
        shutil.copy2(directory / name, vault / name)
    config = load_config(DEFAULT_CONFIG_PATH, vault_dir=vault, db_path=tmp_path / "app.db")
    config.extraction.templates_dir = tmp_path / "templates"
    config.extraction.template_match.probation_docs = 0
    report = ingest(config, ocr_check=ocr_ok, ocr_run=fake_ocr)
    print(report.extracted, report.fields_pending)
    assert report.fields_pending == {}, report.fields_pending
    conn = db.connect(config.db_path)
    rows = {r["filename"]: dict(r) for r in conn.execute(
        "SELECT d.filename, d.processing_status, d.template_match, d.error_message, r.record_number, r.total_minor"
        " FROM documents d LEFT JOIN records r USING (document_id)"
    )}
    conn.close()
    print(rows)
    truth = {d["filename"]: d["fields"] for d in manifest["documents"]}
    for name, row in rows.items():
        assert row["record_number"] == truth[name]["record_number"]["value"], row
        assert row["total_minor"] == truth[name]["total_minor"]["value"], row
    assert rows["po_po-2026-0005.pdf"]["template_match"] == "match"
