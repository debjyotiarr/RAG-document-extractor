# docintel — RAG document extractor

Build a system to extract information from PDFs (invoices and purchase orders), index them, and answer questions about them, with citations and without inventing answers. The full design is in [BLUEPRINT.md](BLUEPRINT.md); `document_extractor_handoff.md` is background reading only.

## Status

Milestones M0 to M2 are complete:

- **Text:** the vault is read into the database page by page, with OCR for scanned pages.
- **Fields:** each invoice and purchase order is reduced to fields (number, dates, party, amounts, terms), with the page and printed text each value came from.

Question answering comes next (M3 onwards).

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
docintel status                         # what has been read, what failed, what needs OCR or review
docintel ingest --force                 # re-read everything
docintel show invoice_0042.pdf          # one document's fields, where each came from, and how
docintel templates                      # the layouts learned so far
```

How `ingest` treats the vault:

- **Running it again** only reads new or changed files.
- **Renamed or moved files** are recognised by content and not re-read.
- **Identical copies** are stored once.
- **Deleted files** are removed from the database, along with everything derived from them.
- **Files that can't be read** (corrupt, password-protected) are listed as `FAILED` with the reason.
- **Scanned pages without Tesseract installed** are listed as `NEEDS OCR`, and picked up automatically by the next `ingest` once Tesseract is installed.

While anything is failed or unread, `status` reports coverage as incomplete. Later milestones use that to answer "couldn't search everything" instead of "not found".

### How fields are extracted

PDFs come in many layouts. Each layout is learned once and then parsed with plain Python:

1. **New layout.** The first document of a layout is read by the local LLM (qwen via Ollama), which copies each value exactly as printed. Any value that isn't actually on the page is thrown away. From those values the code works out the rules ("the total is to the right of *Total Due:*") and saves them as a template in `data/templates/`.
2. **Probation.** For the next 3 documents of that layout, the template and the LLM both read each document and must agree. After that, the layout is parsed without the LLM, in milliseconds instead of tens of seconds.
3. **Changed layout.** A document whose layout differs a little (an extra line, a moved total) gets a new version of the template. The old version is kept.

Each field is stored as `found`, `absent` (the layout has a place for it and it isn't printed), or `not_covered` (the layout has no rule for it). Only `absent` can later back up a "not found" answer.

If Ollama isn't running, documents of known layouts are still extracted. The rest keep their text, so they stay searchable, and `status` lists them under "no fields yet" until the next `ingest`.

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

For field extraction you need [Ollama](https://ollama.com) running, with the model pulled: `ollama pull qwen2.5:7b-instruct`. The test suite doesn't need it, because a scripted stand-in plays the LLM. To check against the real model, run:

```bash
DOCINTEL_LIVE_OLLAMA=1 pytest tests/test_llm.py -s
```
