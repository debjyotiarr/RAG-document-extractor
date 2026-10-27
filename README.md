# docintel — RAG document extractor

Build a system to extract information from PDFs (invoices and purchase orders), index them, and answer questions about them, with citations and without inventing answers. The full design is in [BLUEPRINT.md](BLUEPRINT.md); `document_extractor_handoff.md` is background reading only.

## Status

Milestone M0 is complete: config loader (`src/docintel/config.py`), SQLite schema (`src/docintel/db.py`) and the synthetic fixture corpus (`tests/fixtures/make_fixtures.py`), with tests. The pipeline modules are still empty; M1 (PDF ingestion and OCR) is next.

Run the tests with `pytest`. To look at the fixture PDFs, run `python tests/fixtures/make_fixtures.py` and open `tests/fixtures/corpus/`.

## Where the PDFs go

Put PDFs in `data/vault/`, which is the default vault. To use a different folder, either:

- set `vault_dir` in `config/config.yaml`, or
- pass `--vault /path/to/pdfs` on the command line (once the CLI exists). This overrides the config.

`data/` is gitignored apart from its empty folder markers, so documents and the SQLite database never get committed.

## Layout

```
config/config.yaml      settings (vault path, OCR, Ollama model, agent limits)
data/templates/         layout templates learned from your PDFs (gitignored)
data/vault/             input PDFs (gitignored)
src/docintel/           the package, with one module per pipeline stage
tests/                  pytest suite and synthetic fixture generator
eval/                   evaluation questions and results
```

## Setup (development)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

You also need [Ollama](https://ollama.com) with `ollama pull qwen2.5:7b-instruct`. For scanned PDFs, install Tesseract (`brew install tesseract` / `apt install tesseract-ocr`).
