"""Retriever interface with a full-text (FTS5) implementation (BLUEPRINT §8.5, §9 Phase 2).

Tools search page text through a `Retriever`, so semantic search can be added
in Phase 2 (a hybrid retriever) without changing any tool. Exact lookups by
identifier are structured queries and live in tools.py.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Protocol

# Snippet markers around matched words. Plain characters, so they survive JSON and terminals.
MARK_START, MARK_END = "[", "]"


@dataclass
class Hit:
    document_id: str
    filename: str  # path inside the vault
    page: int
    snippet: str
    score: float  # lower is better (bm25)


@dataclass
class SearchResult:
    hits: list[Hit]
    match_mode: str  # 'phrase' | 'all_terms' | 'any_term' | 'none'
    terms: list[str]


class Retriever(Protocol):
    def search(
        self,
        conn: sqlite3.Connection,
        query: str,
        *,
        record_type: str | None = None,
        document_id: str | None = None,
        limit: int = 10,
    ) -> SearchResult: ...


def search_terms(query: str) -> list[str]:
    """The words of a query, as FTS5 tokenises them. Operators and punctuation are dropped."""
    return re.findall(r"\w+", query.casefold())


class FTSRetriever:
    """Lexical search over page text: exact phrase first, then all words, then any word."""

    def search(
        self,
        conn: sqlite3.Connection,
        query: str,
        *,
        record_type: str | None = None,
        document_id: str | None = None,
        limit: int = 10,
    ) -> SearchResult:
        terms = search_terms(query)
        if not terms:
            return SearchResult([], "none", [])
        # Every term is quoted, so nothing in the query can act as an FTS5 operator.
        quoted = [f'"{t}"' for t in terms]
        attempts = [("phrase", '"' + " ".join(terms) + '"')]
        if len(terms) > 1:
            attempts += [("all_terms", " AND ".join(quoted)), ("any_term", " OR ".join(quoted))]
        for mode, fts_query in attempts:
            hits = self._run(conn, fts_query, record_type, document_id, limit)
            if hits:
                return SearchResult(hits, mode, terms)
        return SearchResult([], "none", terms)

    @staticmethod
    def _run(conn, fts_query, record_type, document_id, limit) -> list[Hit]:
        sql = (
            "SELECT p.document_id, d.relative_path, p.page_number,"
            f" snippet(pages_fts, 0, '{MARK_START}', '{MARK_END}', '…', 24) AS snip, bm25(pages_fts) AS score"
            " FROM pages_fts JOIN document_pages p ON p.page_id = pages_fts.rowid"
            " JOIN documents d ON d.document_id = p.document_id"
            " WHERE pages_fts MATCH ?"
        )
        params: list = [fts_query]
        if record_type:
            sql += " AND d.document_type = ?"
            params.append(record_type)
        if document_id:
            sql += " AND p.document_id = ?"
            params.append(document_id)
        sql += " ORDER BY score, d.relative_path, p.page_number LIMIT ?"
        params.append(limit)
        return [
            Hit(r[0], r[1], r[2], " ".join(r[3].split()), round(r[4], 4))
            for r in conn.execute(sql, params)
        ]
