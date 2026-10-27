# docintel — RAG document extractor

Build a system to extract information from PDFs (invoices and purchase orders), index them, and answer questions about them, with citations and without inventing answers. The full design is in [BLUEPRINT.md](BLUEPRINT.md); `document_extractor_handoff.md` is background reading only.

## Status

Milestones M0 and M1 are complete. The vault is read into the database page by page, with OCR for scanned pages, and `docintel status` shows what has and hasn't been read. Field extraction and question answering come next (M2 onwards).

Run the tests with `pytest`. To look at the fixture PDFs, run `python tests/fixtures/make_fixtures.py` and open `tests/fixtures/corpus/`.

## Where the PDFs go

Put PDFs in `data/vault/`, which is the default vault. To use a different folder, either:

- set `vault_dir` in `config/config.yaml`, or
- pass `--vault /path/to/pdfs` on the command line. This overrides the config.

`data/` is gitignored apart from its empty folder markers, so documents and the SQLite database never get committed.

## Usage

```bash
docintel ingest                         # read new and changed PDFs from the vault
docintel ingest --vault ~/invoices      # use another folder for this run
docintel status                         # what has been read, what failed, what needs OCR
docintel ingest --force                 # re-read everything
```

How `ingest` treats the vault:

- **Running it again** only reads new or changed files.
- **Renamed or moved files** are recognised by content and not re-read.
- **Identical copies** are stored once.
- **Deleted files** are removed from the database, along with everything derived from them.
- **Files that can't be read** (corrupt, password-protected) are listed as `FAILED` with the reason.
- **Scanned pages without Tesseract installed** are listed as `NEEDS OCR`, and picked up automatically by the next `ingest` once Tesseract is installed.

While anything is failed or unread, `status` reports coverage as incomplete. Later milestones use that to answer "couldn't search everything" instead of "not found".

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

For scanned PDFs, install Tesseract: `brew install tesseract` (macOS) or `apt install tesseract-ocr` (Linux). It's optional. Without it, digital PDFs work normally and scanned ones are reported as needing OCR.

From M2 on you also need [Ollama](https://ollama.com) with `ollama pull qwen2.5:7b-instruct`.
