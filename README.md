# docintel — RAG document extractor

Build a system to extract information from PDFs (invoices and purchase orders), index them, and answer questions about them, with citations and without inventing answers. The full design is in [BLUEPRINT.md](BLUEPRINT.md); `document_extractor_handoff.md` is background reading only.

## Status

Milestone M0, in progress. The folder structure is in place, and the modules are still empty.

## Where the PDFs go

Put PDFs in `data/vault/`, which is the default vault. To use a different folder, either:

- set `vault_dir` in `config/config.yaml`, or
- pass `--vault /path/to/pdfs` on the command line (once the CLI exists). This overrides the config.

`data/` is gitignored apart from its empty folder markers, so documents and the SQLite database never get committed.

## Layout

```
config/config.yaml      settings (vault path, OCR, Ollama model, agent limits)
config/templates/       per-issuer parsing templates
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
