# Agentic Document Intelligence Pipeline

## Project Handoff: PDF Invoice and Order Extraction with Grounded Q&A

**Document purpose:** Technical handoff and implementation blueprint\
**Initial use case:** Query a local folder of approximately 50 similar
PDF invoices\
**Implementation philosophy:** Local-first, free/open-source where
practical, demonstrable before production-grade\
**Primary requirement:** Answer questions using evidence from the
document collection and explicitly report when information is absent,
ambiguous, or not successfully searched.

------------------------------------------------------------------------

## 1. Executive Summary

Build a local document-intelligence pipeline that ingests PDF invoices,
extracts useful fields, stores the results in a queryable database, and
lets a user ask natural-language questions. The system should combine:

1.  **Deterministic document processing** to extract PDF text and
    preserve page-level provenance.
2.  **Structured extraction** to turn common invoice fields into
    records.
3.  **A small relational database** for exact lookups, filters,
    aggregations, and comparisons.
4.  **Document search** for finding relevant passages and fields.
5.  **An LLM-powered orchestration layer** that chooses the appropriate
    tools and composes answers from retrieved evidence.
6.  **Verification and abstention logic** so the system does not invent
    answers when evidence is missing or the search was incomplete.

The first version is a proof of concept (PoC), not a production-ready
product. It should be easy to run locally, easy to demonstrate, and
simple enough to debug. A polished frontend, cloud deployment, multiple
autonomous agents, fine-tuning, and complex infrastructure are
deliberately out of scope for the initial build.

The central design principle is:

> **Use conventional code and a database for extraction, calculations,
> filtering, and verification. Use the LLM for language understanding,
> tool selection, and evidence-based synthesis---not as the source of
> truth.**

A RAG-only chatbot is not the preferred design. For invoices, structured
fields and exact identifiers are essential; semantic retrieval is useful
for free-text questions but should complement, not replace, SQL and
keyword search.

------------------------------------------------------------------------

## 2. Goals and Non-Goals

### 2.1 Initial goals

The initial implementation should:

-   Process a folder of approximately 50 PDF invoices with broadly
    similar layouts.
-   Extract text from digitally generated PDFs and preserve the filename
    and page number for each source.
-   Detect PDFs that have no usable text and report them as requiring
    OCR or manual review.
-   Extract a limited, clearly defined set of invoice fields.
-   Store extracted data and document-processing status in SQLite.
-   Support exact lookup, filtering, simple aggregations, and full-text
    search.
-   Accept natural-language questions and route them to the appropriate
    search/database tools.
-   Return answers with source filenames, page numbers, and supporting
    evidence.
-   Distinguish between `supported`, `partial`, `not_found`,
    `ambiguous`, and `incomplete` outcomes.
-   Use local models where practical to avoid per-query API charges.
-   Include a small evaluation set and a repeatable demonstration
    procedure.

### 2.2 Explicit non-goals for the first version

Do not initially build:

-   A general-purpose system for every document type.
-   A polished multi-user web application.
-   Cloud hosting, enterprise identity management, or complex access
    control.
-   A multi-agent architecture.
-   Fine-tuning or training a model.
-   A knowledge graph.
-   A vector database unless evaluation shows that simpler retrieval is
    inadequate.
-   Automatic posting of invoices into accounting software.
-   Autonomous changes to source documents.
-   A guarantee that the system can prove universal absence of
    information.

These can be considered later if a demonstrated need justifies the
complexity.

------------------------------------------------------------------------

## 3. Demonstration Use Cases

The initial demo should showcase a small set of realistic questions.

### 3.1 Exact field lookup

-   "What is the invoice number in `invoice_1042.pdf`?"
-   "What is the invoice date and total for invoice INV-1042?"
-   "What GSTIN is listed on this invoice?"

Expected behaviour: retrieve the structured field and return its value,
source filename, and page.

### 3.2 Search and filtering

-   "Find invoices issued by Supplier X."
-   "Show invoices dated in March."
-   "Find invoices that mention a 30-day payment term."

Expected behaviour: use structured filters for dates and supplier
fields, and full-text search for text such as payment terms.

### 3.3 Aggregation

-   "What is the total invoiced by Supplier X?"
-   "How many invoices were issued in March?"
-   "What is the sum of invoice totals for the selected period?"

Expected behaviour: use SQL and deterministic arithmetic. The LLM should
not calculate totals by mentally adding retrieved text snippets.

Aggregation rules must be explicit. For example, the sum of invoice
totals is not necessarily the same as the sum of pre-tax subtotals, and
duplicate or cancelled invoices may need special treatment.

### 3.4 Questions about information that is absent

-   "What is the delivery date?" when no delivery date appears in the
    invoice.
-   "What is the bank account number?" when it is not present.
-   "Find invoice INV-9999" when that invoice is not in the collection.

Expected behaviour: report that the requested information was not found
in the successfully processed and searched documents. Do not infer a
value from general knowledge or from an unrelated document.

### 3.5 Ambiguous or incomplete cases

-   Two documents have the same invoice number.
-   A scanned PDF could not be read.
-   A field appears differently in two places.
-   Some files have not been indexed.

Expected behaviour: explain the ambiguity or incomplete processing. Do
not present a failed search as proof that information is absent.

------------------------------------------------------------------------

## 4. Recommended Architecture

``` text
                    LOCAL PDF FOLDER
                           |
                           v
              Document discovery and hashing
                           |
                           v
                 PDF text extraction
                 (OCR fallback if needed)
                           |
                           v
              Page-level text and provenance
                           |
                           v
              Invoice field extraction
              (rules + optional local LLM)
                           |
                           v
                  Validation and review
                           |
                           v
                 SQLite structured store
                 + full-text search index
                           |
User question --> Query interpretation / orchestrator
                           |
              +------------+-------------+
              |            |             |
              v            v             v
          SQL tools    Exact/full-text   Evidence
                       document search   retrieval
              |            |             |
              +------------+-------------+
                           |
                           v
                 Evidence validation
                           |
                           v
            Answer with source and status
```

### 4.1 Design principles

-   **One source of truth:** SQLite stores the accepted extracted
    records and processing status.
-   **Preserve provenance:** retain document ID, filename, page number,
    field name, extracted value, and---where feasible---supporting text.
-   **Separate extraction from answering:** the pipeline should process
    documents before query time; avoid re-reading all PDFs for every
    question.
-   **Prefer deterministic operations:** use Python and SQL for
    arithmetic, date filtering, exact matching, and validation.
-   **Keep the agent constrained:** it can call approved tools but
    cannot execute arbitrary code or unrestricted SQL.
-   **Make failure visible:** failed extraction, OCR, indexing, and
    ambiguous matching must be recorded.
-   **Keep the build modular:** extraction, storage, retrieval,
    orchestration, and evaluation should be independently testable.

------------------------------------------------------------------------

## 5. Free-First Technology Stack

  -----------------------------------------------------------------------------
  Component         Recommended       Purpose           When to change or
                    starting option                     extend
  ----------------- ----------------- ----------------- -----------------------
  Language          Python            Pipeline and      Keep unless deployment
                                      orchestration     constraints require
                                                        otherwise

  PDF text          PyMuPDF           Read digital PDFs Add pdfplumber if table
  extraction                          page by page      layout needs it

  OCR               Tesseract and     Process scanned   Evaluate a layout-aware
                    OCRmyPDF          PDFs              OCR/document tool if
                                                        quality is inadequate

  Data validation   Pydantic or       Validate          Add richer
                    standard Python   extracted records schema/version
                    validation        and outputs       management as needed

  Database          SQLite            Structured        PostgreSQL for
                                      invoice records   multi-user/concurrent
                                      and metadata      deployments

  Full-text search  SQLite FTS5       Keyword search    Add a dedicated search
                                      over extracted    service for larger
                                      text              collections

  Local LLM runtime Ollama            Run a local       Hosted model only if
                                      instruction model quality or speed
                                                        justifies the cost and
                                                        data policy permits it

  Embeddings        Sentence          Optional semantic Add only if
                    Transformers      retrieval         keyword/structured
                                                        search misses relevant
                                                        evidence

  Vector index      FAISS or Chroma   Optional vector   Use after retrieval
                                      retrieval         evaluation demonstrates
                                                        a need

  Agent             Plain Python      Tool routing and  LangGraph when
  orchestration     functions and a   control flow      branching, state
                    bounded state                       persistence, or human
                    machine                             approval becomes useful

  Interface         Terminal /        Demonstration     Streamlit for a
                    notebook                            lightweight UI
                    initially                           

  Tests             pytest plus a     Regression and    Add an evaluation
                    manually verified evaluation        framework if the
                    question set                        benchmark grows
  -----------------------------------------------------------------------------

### 5.1 Local hardware assumptions

A modern Apple Silicon laptop with 16 GB RAM is a reasonable starting
point for a small corpus and a quantized local model. Model size and
inference speed will constrain performance. No dedicated GPU server is
required for the initial PoC.

The stack can avoid subscription and per-query API fees, but "free" does
not mean zero cost: hardware, storage, setup time, and maintenance
remain costs. Check the licences and redistribution conditions of
selected models and dependencies before commercial distribution.

### 5.2 Why not start with a vector database?

The initial collection is small and has similar invoice layouts. Exact
invoice-number search, structured fields, SQL, and full-text search may
cover most useful questions. Vector search adds complexity and can be
less reliable for exact identifiers and numerical fields.

Add embeddings only when a test demonstrates that semantic
questions---for example, paraphrased payment or delivery terms---cannot
be answered adequately using extracted fields and full-text search.

------------------------------------------------------------------------

## 6. Repository Layout

A compact, maintainable project structure could be:

``` text
document-intelligence/
├── README.md
├── pyproject.toml              # or requirements.txt
├── .gitignore
├── config/
│   └── schema.yaml             # target fields and extraction settings
├── data/
│   ├── input_pdfs/             # local documents; usually excluded from Git
│   ├── extracted_text/          # optional cached text
│   └── app.db                   # SQLite database; excluded from Git
├── src/
│   └── docintel/
│       ├── __init__.py
│       ├── ingest.py            # discover files, hash, process status
│       ├── pdf_extract.py       # text extraction and page provenance
│       ├── ocr.py               # optional OCR fallback
│       ├── field_extract.py     # rules/LLM extraction into schema
│       ├── validation.py        # types, arithmetic, required fields
│       ├── database.py          # schema and persistence
│       ├── search.py            # exact and full-text search
│       ├── query_tools.py       # safe tools exposed to the agent
│       ├── agent.py             # query routing and bounded tool loop
│       ├── answer.py            # evidence-grounded answer formatting
│       └── evaluation.py        # benchmark runner and metrics
├── tests/
│   ├── test_pdf_extract.py
│   ├── test_field_extract.py
│   ├── test_validation.py
│   ├── test_search.py
│   ├── test_missing_information.py
│   └── fixtures/                # synthetic/redacted sample documents
└── eval/
    ├── questions.jsonl
    └── results/
```

This is a suggested structure, not a requirement to create every file
immediately. Begin with a few modules and split them when
responsibilities become clear. Avoid committing confidential invoices,
database files, or sensitive extracted data to a public repository.

------------------------------------------------------------------------

## 7. Ingestion Pipeline

The ingestion pipeline converts a folder of PDFs into a reliable
internal representation.

### 7.1 Discover and identify documents

For each PDF:

1.  Find the file recursively in the configured input folder.
2.  Calculate a stable document identifier, preferably using a
    cryptographic file hash such as SHA-256.
3.  Record filename, relative path, file size, modification time, hash,
    and processing status.
4.  Check whether the exact file hash has already been processed.
5.  Skip unchanged files; reprocess changed files according to a
    documented policy.

A hash is more reliable than a filename alone because filenames can be
reused or renamed.

### 7.2 Extract text page by page

Use PyMuPDF first. Preserve the page boundary rather than flattening the
entire PDF into one text string.

For each page, store:

-   Document ID
-   Page number (choose one-based numbering for user-facing citations)
-   Extracted text
-   Extraction method
-   Basic extraction diagnostics

For a first version, plain text may be enough. If fields are badly
reordered or tables are broken, preserve word coordinates or use a
table-aware extraction approach.

### 7.3 Detect PDFs that require OCR

If a page has little or no usable text, mark it for OCR rather than
silently treating it as empty. Some PDFs contain a mix of selectable
text and scanned pages, so detection should happen at page level where
practical.

Use OCRmyPDF/Tesseract as a free local fallback. OCR can misread digits,
decimal points, GSTINs, and invoice IDs; those fields need additional
validation.

### 7.4 Store processing status

Useful document statuses include:

-   `discovered`
-   `text_extracted`
-   `ocr_required`
-   `ocr_completed`
-   `fields_extracted`
-   `validated`
-   `needs_review`
-   `failed`

The exact set can be simpler initially, but failure states should not be
discarded.

### 7.5 Idempotency and incremental processing

Running ingestion twice on unchanged files should not create duplicate
invoice records. Use the document hash and database constraints to
enforce this. When a file changes, retain enough metadata to identify
which version produced the current extracted record.

------------------------------------------------------------------------

## 8. Invoice Schema and Structured Extraction

Start with a small schema tied to actual demo questions. The fields
below are candidates; only implement those that appear consistently in
the sample invoices.

### 8.1 Candidate fields

  ----------------------------------------------------------------------------
  Field                        Suggested type          Notes
  ---------------------------- ----------------------- -----------------------
  `document_id`                Text                    Internal stable ID for
                                                       the PDF

  `document_type`              Text                    For example, invoice,
                                                       credit note, order

  `supplier_name`              Text                    Preserve original
                                                       spelling and optionally
                                                       normalized form

  `supplier_gstin`             Text / nullable         Validate format where
                                                       applicable; do not
                                                       infer missing
                                                       characters

  `invoice_number`             Text                    Treat as text, not an
                                                       integer; leading zeros
                                                       may matter

  `invoice_date`               ISO date / nullable     Preserve raw value if
                                                       parsing fails

  `currency`                   Text / nullable         Do not assume every
                                                       document uses INR

  `subtotal`                   Decimal / nullable      State whether this is
                                                       pre-tax

  `tax_amount`                 Decimal / nullable      May need a breakdown by
                                                       tax type

  `total_amount`               Decimal / nullable      Include currency and
                                                       tax semantics

  `payment_terms`              Text / nullable         May be free text

  `due_date`                   ISO date / nullable     Distinguish stated date
                                                       from calculated date

  `purchase_order_reference`   Text / nullable         Useful for matching
                                                       invoice to order

  `source_page`                Integer                 Page supporting the
                                                       field

  `supporting_text`            Text / nullable         Short source snippet or
                                                       extracted region

  `extraction_method`          Text                    Rule, parser, or LLM

  `validation_status`          Text                    Valid, warning, or
                                                       review required
  ----------------------------------------------------------------------------

For line-item support, use a separate `invoice_line_items` table
containing document ID, line number, description, quantity, unit price,
discount, tax rate, line total, source page, and validation status.

### 8.2 Keep raw and normalized values

For important fields, preserve both:

-   The **raw value**, as it appears in the PDF.
-   The **normalized value**, used for filtering or calculation.

For example, raw date `15/03/26` may normalize to `2026-03-15`, but the
raw string should remain available for debugging. Supplier names may
also need normalization, but do not erase the original value.

### 8.3 Extraction strategy: rules first, LLM where needed

Use regular expressions and deterministic parsing for predictable
fields. For instance, if invoice numbers always follow a known label and
format, a rule is cheaper and more repeatable than an LLM call.

Use a local LLM for fields where layout or wording varies. Ask it to
return a strict JSON object matching the schema. Validate the output
before storing it.

Do not assume that valid JSON means correct extraction. A syntactically
valid answer can still contain an invented invoice number or an
incorrect total.

### 8.4 Field-level provenance

Each critical extracted field should have provenance where feasible:

-   Source document and page
-   Raw text or supporting snippet
-   Extraction method
-   Validation outcome

This makes it possible to answer "Where did that number come from?" and
to diagnose errors.

### 8.5 Financial validation

Use Python `Decimal` or integer minor units for monetary calculations,
not binary floating-point arithmetic.

Possible validation checks:

-   Subtotal + tax + shipping − discounts reconciles to total within the
    document's rounding rules.
-   Quantities and unit prices reconcile to line totals.
-   Dates parse into valid dates.
-   Invoice identifiers are not blank.
-   Currency is known or explicitly marked unknown.
-   GSTIN-like values pass format checks where applicable.

Validation failures should create warnings or review states. Do not
silently replace a source value with a calculated value. A calculated
reconciliation can be shown as a diagnostic, separate from the amount
printed on the invoice.

------------------------------------------------------------------------

## 9. Database Design

SQLite is sufficient for a small local PoC.

### 9.1 Suggested logical tables

#### `documents`

One row per PDF/version.

Suggested columns:

-   `document_id` --- primary key
-   `file_hash`
-   `filename`
-   `relative_path`
-   `page_count`
-   `processing_status`
-   `extraction_method`
-   `last_processed_at`
-   `error_message`

#### `document_pages`

One row per page.

Suggested columns:

-   `document_id`
-   `page_number`
-   `page_text`
-   `text_quality_status`

Composite key: `(document_id, page_number)`.

#### `invoices`

One row per extracted invoice record.

Suggested columns:

-   `invoice_record_id`
-   `document_id`
-   `supplier_name_raw`
-   `supplier_name_normalized`
-   `supplier_gstin`
-   `invoice_number`
-   `invoice_date`
-   `currency`
-   `subtotal`
-   `tax_amount`
-   `total_amount`
-   `payment_terms`
-   `due_date`
-   `purchase_order_reference`
-   `validation_status`

Do not assume invoice number alone is globally unique. It may only be
unique within a supplier or time period.

#### `field_evidence` (recommended if manageable)

One row per extracted field, storing field name, raw value, normalized
value, source page, supporting text, extraction method, and validation
status. This is more flexible than adding provenance columns for every
field, although it adds implementation work.

#### `invoice_line_items` (optional for first version)

Store line-level details only if the demo requires product/quantity
matching or line-item analysis.

### 9.2 Indexes and constraints

Add indexes on frequently queried fields such as invoice number,
supplier name, and invoice date. Use uniqueness constraints only where
the business rules justify them.

Use parameterized SQL queries. Never concatenate user-provided strings
into SQL.

### 9.3 Full-text search

SQLite FTS5 can index page text or selected text fields for keyword
search. It is useful for queries such as "find invoices mentioning net
30" or "search for warranty terms."

FTS5 is lexical search, not semantic search. It will not always
recognize paraphrases. This limitation is acceptable initially and can
be measured.

------------------------------------------------------------------------

## 10. Retrieval Strategy: SQL + Exact Search + Full Text

Use a hybrid retrieval layer, but keep it simple.

### 10.1 Exact search

Use for:

-   Invoice numbers
-   Purchase order references
-   GSTINs
-   Exact supplier names
-   Known document identifiers

Exact identifiers should not be entrusted solely to embeddings.
Normalize casing and whitespace where appropriate, but preserve the
original value.

### 10.2 Structured SQL

Use for:

-   Filtering by date
-   Supplier-level totals
-   Counting invoices
-   Comparing invoice fields
-   Finding missing values in a selected field
-   Sorting by amount or date

SQL should calculate aggregations. The LLM can interpret a question and
choose an approved query template or tool, but the result should come
from the database.

### 10.3 Full-text retrieval

Use for:

-   Payment terms
-   Delivery terms
-   Bank details
-   Descriptions and notes
-   Other free-text content not captured in the structured schema

Return the matching document, page, and a short excerpt. A keyword match
alone does not prove that the excerpt answers the question; the evidence
still needs to be checked.

### 10.4 Optional semantic retrieval

Add embeddings if evaluation shows that full-text search misses relevant
passages because the user's wording differs from the document wording.

If added:

1.  Chunk page text into meaningful passages.
2.  Retain document ID, page number, and chunk ID as metadata.
3.  Embed chunks using a local embedding model.
4.  Retrieve top-k candidates.
5.  Optionally combine vector results with FTS results.
6.  Rerank only if necessary.
7.  Return source passages to the answer-generation step.

For invoices, chunk size should respect page and table boundaries as
much as practical. A chunk should not detach an amount from the label or
line item it belongs to.

------------------------------------------------------------------------

## 11. Agentic Query Orchestration

### 11.1 What "agentic" means for this project

The system should do more than pass a question to a model and paste the
response. It should choose tools, inspect their results, decide whether
further retrieval is required, and return a supported answer or a
controlled failure state.

The initial design should use **one orchestrator with a small set of
tools**, not a group of autonomous agents. This is easier to debug, more
predictable, and adequate for the target corpus.

### 11.2 Initial tool set

Implement these Python functions first:

#### `search_documents(query, filters=None)`

Search by exact identifier, metadata, and/or full-text terms. Return
document IDs, filenames, page numbers, and relevant excerpts.

#### `query_invoice_fields(filters, fields)`

Run a restricted structured query for approved fields and filters.
Return typed values and relevant source references.

#### `retrieve_evidence(document_id, field_or_question)`

Return the structured field and its provenance, or the best supporting
page excerpts for a free-text question.

#### `validate_result(result)`

Check data types, source references, required evidence, and any relevant
numerical consistency rules.

#### Optional: `find_related_documents(reference)`

Search for related invoices/orders using purchase order references,
invoice references, supplier, dates, or other identifiers. Add this only
when cross-document linking is in scope.

### 11.3 Query flow

A basic query cycle should be:

1.  Receive the natural-language question.
2.  Interpret the requested entity, fields, filters, and intent.
3.  Select a retrieval strategy: exact lookup, SQL, full-text search, or
    a combination.
4.  Call the relevant approved tool(s).
5.  Inspect whether the result is sufficient and unambiguous.
6.  If necessary, run a bounded alternative search.
7.  Validate evidence and calculations.
8.  Produce a structured answer with status and citations.
9.  Log the query, tools used, outcome, and errors.

Set a small maximum number of tool calls or retries. If the agent cannot
resolve the query within that limit, it should explain the limitation
rather than loop indefinitely.

### 11.4 Use ordinary Python first

A framework such as LangGraph can be introduced when explicit state,
branching, retries, checkpoints, or human approval become useful. It is
not a prerequisite for an agentic demonstration. A small Python state
machine with well-defined functions is a perfectly reasonable first
implementation.

### 11.5 Restrict tool permissions

-   Keep the database read-only from the query agent's perspective.
-   Expose approved query functions rather than unrestricted SQL
    execution.
-   If text-to-SQL is later introduced, validate and restrict the SQL to
    read-only operations and permitted tables.
-   Never let document contents instruct the agent to ignore system
    rules, access unrelated files, or execute arbitrary actions.
-   Treat PDFs as untrusted data. They may contain text that resembles
    instructions; it must remain source content, not control
    instructions.

------------------------------------------------------------------------

## 12. Grounded Answers and "Not Found" Behaviour

Reliable absence handling is a core product requirement.

### 12.1 Required answer statuses

Use a small set of explicit statuses:

  -----------------------------------------------------------------------
  Status                  Meaning                 Example response
  ----------------------- ----------------------- -----------------------
  `supported`             Retrieved evidence      "The total is ₹94,400.
                          directly supports the   Source:
                          answer                  invoice_1042.pdf, page
                                                  2."

  `partial`               Some requested fields   "The invoice date is
                          were found, others were present, but no due
                          not                     date was found."

  `not_found`             The relevant searchable "No delivery date was
                          documents were          found in the processed
                          processed and searched, invoices searched."
                          but no supporting       
                          evidence was found      

  `ambiguous`             Multiple candidates or  "Two invoices match
                          conflicting values      this identifier; please
                          prevent a reliable      specify the supplier or
                          answer                  date."

  `incomplete`            Search coverage is      "I could not determine
                          insufficient due to     this because three
                          extraction, OCR,        relevant PDFs failed
                          indexing, or other      text extraction."
                          failures                
  -----------------------------------------------------------------------

Avoid reducing all failure cases to a single `not found` message.

### 12.2 Search coverage matters

The system should maintain an ingestion registry so it knows:

-   How many PDFs exist in the configured folder.
-   How many have been successfully processed.
-   Which files/pages failed extraction or OCR.
-   Which documents are indexed for full-text or semantic search.
-   Whether any files have changed since the last ingestion run.

When a query returns no result, use this information to decide whether a
`not_found` or `incomplete` status is appropriate.

### 12.3 No universal proof of absence

A system cannot prove that information does not exist merely because a
search returned zero results. It can report that no supporting
information was found within a defined collection and search process,
provided the collection was processed adequately.

For a small corpus, a query may be able to search all successfully
extracted page text and structured records. That is stronger evidence
than searching only a few top-ranked vector chunks, but it still depends
on extraction quality and search coverage.

### 12.4 Structured output contract

Internally, use a predictable response structure. For example:

``` json
{
  "status": "supported",
  "answer": "The invoice total is INR 94400.00.",
  "evidence": [
    {
      "document_id": "doc_1042",
      "filename": "invoice_1042.pdf",
      "page": 2,
      "field": "total_amount",
      "value": "94400.00",
      "supporting_text": "Grand Total: INR 94,400.00"
    }
  ],
  "search_coverage": {
    "documents_in_scope": 50,
    "documents_processed": 50,
    "documents_with_extraction_errors": 0
  },
  "reason": null
}
```

This is an example contract, not a claim that every implementation must
use exactly these field names. Keep internal results machine-readable
and render them into a human-readable response at the final step.

### 12.5 Evidence-based answer generation

The final LLM prompt should clearly separate:

-   User question
-   Retrieved evidence
-   Calculated results
-   Search coverage and diagnostics
-   Permitted status values

Instruct the model to use only supplied evidence for claims about the
document collection, identify missing evidence, and preserve the
distinction between a value stated in the document and a value
calculated by the program.

A source citation is only useful if the cited page actually supports the
claim. Citation correctness must be evaluated.

------------------------------------------------------------------------

## 13. Cross-Document Matching: A Later Capability

A natural extension is matching purchase orders, invoices, delivery
notes, and payment records.

Start with deterministic matching keys:

1.  Exact purchase order reference.
2.  Exact invoice reference where appropriate.
3.  Supplier identity.
4.  Date and amount as secondary evidence.
5.  Line-item descriptions and quantities only when needed.

Do not automatically assume that similar totals or supplier names prove
two documents are related. The system should distinguish a strong exact
match from a heuristic candidate match.

A later matching table might store:

-   Source document ID
-   Related document ID
-   Relationship type
-   Match method
-   Match score or confidence category
-   Supporting evidence
-   Review status

For example, an invoice may reference a purchase order, but an invoice
total can legitimately differ from the original order because of taxes,
shipping, discounts, partial deliveries, or changes. Business rules must
be explicit before discrepancies are flagged.

------------------------------------------------------------------------

## 14. Evaluation and Testing

A working demo should be supported by a small benchmark whose answers
have been checked against the PDFs.

### 14.1 Build a representative test set

Include questions from these categories:

-   Exact field lookup
-   Supplier/date filtering
-   Aggregation
-   Free-text terms
-   Missing-field questions
-   Nonexistent invoice IDs
-   Ambiguous matches
-   Documents with extraction errors
-   Cross-document lookup if implemented

Each test should record the question, expected answer or expected
status, relevant source document/page, and evaluation notes.

A practical first benchmark could contain 20--30 manually verified
questions. Expand it as the system and corpus grow.

### 14.2 Metrics

  -----------------------------------------------------------------------
  Metric                              What it measures
  ----------------------------------- -----------------------------------
  Field extraction accuracy           Whether structured values match the
                                      source

  Retrieval recall@k                  Whether relevant evidence appears
                                      among the top-k results

  Answer correctness                  Whether the final answer is correct

  Citation accuracy                   Whether the cited page supports the
                                      claim

  Numerical accuracy                  Whether calculations and monetary
                                      values are correct

  False-answer rate                   How often the system asserts
                                      unsupported facts

  Abstention quality                  Whether it distinguishes missing,
                                      ambiguous, and incomplete cases

  Processing coverage                 Whether all expected files were
                                      successfully processed

  Latency and tool calls              Whether the system is practical to
                                      use
  -----------------------------------------------------------------------

Prioritize false-answer rate, numerical accuracy, citation correctness,
and missing-information behaviour over conversational style.

### 14.3 Regression tests

Keep examples of failures and run them after changes. Important
regression cases include:

-   Leading zeros in invoice numbers.
-   Different date formats.
-   Amounts with commas and decimal points.
-   OCR confusion between `0` and `O`, or `1` and `I`.
-   Multiple invoices from the same supplier.
-   Missing due dates.
-   Duplicate filenames or repeated documents.
-   Queries where the correct answer is absent.
-   A document that failed ingestion.

### 14.4 Do not overstate the evaluation

A small benchmark demonstrates behaviour on the tested sample; it does
not prove production-grade reliability across all future documents.
Report the test set size, observed results, and known limitations
honestly.

------------------------------------------------------------------------

## 15. Implementation Milestones

The work should proceed by deliverables and quality gates, rather than
by a fixed calendar.

### Milestone A --- PDF ingestion

**Build initially** - Folder scanner - File hashing and duplicate
detection - Page-level text extraction - Extraction status and error
logging

**Acceptance condition** - Every input PDF is accounted for, and each
successfully extracted page has a retrievable source reference.

### Milestone B --- Structured extraction

**Build initially** - Small invoice schema - Deterministic parsers for
predictable fields - Optional local LLM extraction for irregular
fields - Field-level validation and provenance

**Acceptance condition** - The selected fields can be extracted and
checked on a representative subset, with failures recorded rather than
hidden.

### Milestone C --- Queryable storage and retrieval

**Build initially** - SQLite schema - Exact lookup and indexed fields -
FTS5 or equivalent full-text search - Deterministic aggregations

**Acceptance condition** - Core queries work without requiring the LLM
to invent or calculate the result.

### Milestone D --- Tool-using assistant

**Build initially** - Query interpretation - A small approved tool set -
Bounded tool loop - Structured answer contract - Source citations and
explicit statuses

**Acceptance condition** - Natural-language questions route to the
correct tools and return evidence-backed results.

### Milestone E --- Evaluation and demonstration

**Build initially** - Verified question set - Regression tests - Error
and coverage report - Repeatable demo commands and README

**Acceptance condition** - A user can run the pipeline and reproduce a
small set of successful and unsuccessful query examples.

### Milestone F --- Hardening and expansion

Proceed only when the PoC demonstrates a clear use case. Candidate
additions are described below.

------------------------------------------------------------------------

## 16. Expansion Roadmap

### Phase 1: Stronger invoice extraction

Add: - Table-aware line-item extraction. - Tax breakdowns and
discounts. - Supplier-name normalization. - OCR quality checks and
low-confidence review. - Document-type classification for invoices,
credit notes, purchase orders, and receipts. - Configurable schemas for
different invoice templates.

**Why:** improves accuracy and broadens the types of questions that can
be answered.

### Phase 2: Semantic retrieval

Add: - Local embeddings. - Chunking that preserves page/table context. -
Hybrid lexical + vector retrieval. - Optional reranking. - Retrieval
metrics and tuning.

**Why:** supports paraphrased questions and free-text details that are
not represented as structured fields.

### Phase 3: Related-document workflows

Add: - Invoice-to-purchase-order matching. - Quantity and amount
comparisons. - Duplicate invoice detection. - Discrepancy reports with
source evidence. - Human review of uncertain matches.

**Why:** turns document lookup into an operational workflow.

### Phase 4: User interface

Add a lightweight Streamlit UI with: - Folder or file selection where
appropriate. - Ingestion status. - Natural-language question box. -
Answer, evidence excerpt, filename, and page number. - Extracted field
table. - Review flags for ambiguous or low-quality records.

**Why:** makes the system easier to demonstrate to non-technical
stakeholders.

### Phase 5: Production deployment

Potential additions: - PostgreSQL and multi-user support. -
Authentication and role-based access. - Secure document storage and
access controls. - Background ingestion workers. - Audit logs and
versioned extraction results. - Backups, monitoring, and recovery. -
Human approval for consequential actions. - Retention and deletion
policies. - Deployment and model-serving strategy.

**Why:** a local PoC does not provide the operational, security, or
reliability guarantees expected of a production application.

### Phase 6: More document types and integrations

Potential additions: - Purchase orders, delivery notes, contracts,
receipts, and statements. - Email or shared-drive ingestion. -
Accounting/ERP integration. - Export to CSV/Excel or structured APIs. -
Customer-specific extraction schemas and evaluation suites.

**Why:** expands the product beyond the initial invoice use case.

------------------------------------------------------------------------

## 17. Security, Privacy, and Operational Considerations

Invoices can contain names, addresses, tax identifiers, banking
information, pricing, and other commercially sensitive data.

For the local PoC:

-   Keep PDFs, extracted text, and SQLite files out of public Git
    repositories.
-   Add local data directories and database files to `.gitignore`.
-   Prefer local inference if data must not leave the machine.
-   Do not send documents to a hosted model without explicit
    authorization and a suitable data-handling review.
-   Treat document contents as untrusted input; never follow
    instructions embedded in a PDF.
-   Keep the query agent read-only.
-   Log operational details without unnecessarily duplicating sensitive
    invoice contents.
-   Keep a way to delete derived records when source documents are
    removed.
-   Do not describe the system as secure, compliant, or production-ready
    without a separate review.

If the system later serves multiple users or processes business-critical
records, revisit authentication, authorization, encryption, backups,
auditability, and data-retention requirements before deployment.

------------------------------------------------------------------------

## 18. Common Failure Modes and Mitigations

  -----------------------------------------------------------------------
  Failure mode            Why it happens          Initial mitigation
  ----------------------- ----------------------- -----------------------
  Wrong total extracted   Layout ambiguity, OCR   Preserve supporting
                          errors, label confusion text, validate
                                                  arithmetic, flag
                                                  suspicious records

  Wrong invoice retrieved Similar wording or      Exact identifiers,
                          duplicate IDs           metadata filters, show
                                                  candidate matches

  Missing answer treated  Search did not cover    Check ingestion
  as absent               all files or extraction registry and return
                          failed                  `incomplete` when
                                                  needed

  Hallucinated answer     Model fills gaps using  Restrict answer to
                          prior knowledge         evidence and enforce
                                                  output statuses

  Incorrect aggregation   Duplicate records or    SQL calculations,
                          mixed                   explicit scope,
                          currencies/periods      duplicate checks,
                                                  currency-aware rules

  Incorrect source        Page references lost    Preserve document/page
  citation                during chunking         metadata throughout
                                                  every stage

  Repeated processing     Pipeline has no         File hashes, database
                          idempotency             constraints,
                                                  incremental processing

  Agent loops or          Unbounded tool calling  Maximum tool-call
  over-searches                                   count, clear stop
                                                  conditions, logging

  SQL injection or unsafe Untrusted question is   Approved query
  query                   turned into arbitrary   functions,
                          SQL                     parameterized SQL,
                                                  read-only access

  Semantic search misses  Embeddings are not      Search identifiers
  exact IDs               reliable exact-match    lexically and
                          tools                   structurally first

  OCR takes too long or   Low-quality scans or    Test sample scans
  reads poorly            complex layouts         early; route bad pages
                                                  to review

  Model output is valid   Schema validity is not  Verify fields against
  JSON but factually      factual validity        source evidence and run
  wrong                                           tests
  -----------------------------------------------------------------------

------------------------------------------------------------------------

## 19. Suggested Demonstration Script

A credible client demonstration should show both successful retrieval
and honest failure handling.

1.  **Show ingestion status.** Display how many PDFs were discovered,
    processed, and flagged.
2.  **Ask an exact lookup.** Retrieve an invoice by number and show the
    extracted date and total.
3.  **Ask an aggregation.** Query total invoiced by a supplier or during
    a date range.
4.  **Ask a free-text question.** Find payment terms or another phrase
    from the invoice.
5.  **Ask a missing-information question.** Request a field known to be
    absent and show the `not_found` result.
6.  **Show provenance.** Open or inspect the cited filename/page and
    supporting text.
7.  **Show a known limitation.** Explain how the system reports an
    unreadable file or ambiguous match rather than claiming certainty.

Do not use only handpicked successful examples. Demonstrating a sensible
response to missing information is a differentiator for document
extraction systems.

------------------------------------------------------------------------

## 20. Definition of Done for the Initial PoC

The initial PoC is ready to demonstrate when:

-   [ ] The configured folder is scanned and all PDFs are accounted for.
-   [ ] Text extraction is page-aware and failures are visible.
-   [ ] The selected invoice fields are stored in SQLite with
    provenance.
-   [ ] Exact lookups and common filters work.
-   [ ] At least one useful aggregation is computed deterministically.
-   [ ] Free-text search can retrieve relevant passages.
-   [ ] A natural-language interface routes questions to approved tools.
-   [ ] Factual answers include filename, page, and supporting evidence.
-   [ ] Missing, ambiguous, and incomplete cases are distinct.
-   [ ] A small verified benchmark and regression tests exist.
-   [ ] The repository has clear setup and run instructions.
-   [ ] Confidential source documents are not accidentally committed to
    version control.
-   [ ] Limitations are documented and the demo does not overclaim
    reliability.

A frontend is not required to satisfy these criteria. A terminal-based
interface is sufficient for a technical demonstration.

------------------------------------------------------------------------

## 21. Recommended First Implementation Decisions

To keep the initial build focused, adopt these defaults unless
inspection of the PDFs gives a reason to change them:

1.  **Corpus:** one folder containing roughly 50 similar invoice PDFs.
2.  **Processing:** PyMuPDF first, OCR fallback only where necessary.
3.  **Schema:** a small number of useful fields; add line items only if
    needed for the demo.
4.  **Storage:** SQLite for structured records and processing status.
5.  **Search:** exact identifier lookup, SQL, and SQLite FTS5 before
    embeddings.
6.  **LLM:** a suitable quantized local instruction model via Ollama for
    query interpretation and irregular extraction.
7.  **Agent:** one orchestrator with a small, bounded tool set.
8.  **Answer policy:** evidence-backed claims only; report not found,
    ambiguous, or incomplete states explicitly.
9.  **Interface:** terminal first; Streamlit later if it improves client
    demonstrations.
10. **Evaluation:** a manually verified question set and regression
    tests before expanding features.

------------------------------------------------------------------------

## 22. Final Recommendation

The best initial product is not a generic "chat with PDFs" application.
It is a **small, local invoice intelligence pipeline with structured
extraction, exact search, SQL-based business queries, evidence-grounded
answers, and explicit handling of missing information**.

This approach is inexpensive to prototype, practical for a small corpus,
and provides a credible foundation for a larger agentic system. Its
value comes from reliable retrieval and traceability---not from adding
as many agents or frameworks as possible.

The implementation should progress from deterministic ingestion to
structured storage, then retrieval, then a constrained tool-using agent,
and finally evaluation. Add semantic search, document matching, a
frontend, and production infrastructure only when test results or client
needs justify them.
