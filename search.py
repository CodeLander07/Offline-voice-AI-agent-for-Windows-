"""
Project Javis - Web search data layer.

Uses DuckDuckGo's Instant Answer JSON API. No API key, no browser launch,
no anti-bot blocking. Returns a list of SearchResult(title, snippet, url).

Notes:
  - The DDG /html/ HTML endpoint now returns a 202 + anomaly-challenge for
    all non-browser clients; we avoid it entirely.
  - The Instant Answer API is JSON, free, and bot-friendly, but the answer
    coverage is best for encyclopedia / factual queries (Wikipedia-sourced
    Abstract + RelatedTopics). Creative / live-web queries may return
    fewer results; we surface that honestly with "I didn't find anything."
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SEARCH_URL = "https://api.duckduckgo.com/"
USER_AGENT = "Javis/1.0 (offline-voice-agent; +https://github.com)"
DEFAULT_TIMEOUT = 10.0
MIN_QUERY_LEN = 2
MAX_QUERY_LEN = 200

STOPWORDS = {"a", "an", "the", "it", "is", "and", "or", "of", "to", "in", "on"}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class SearcherError(Exception):
    """Base class for all search-layer errors."""


class NetworkError(SearcherError):
    """Could not reach the search engine (DNS, offline, timeout)."""


class ParseError(SearcherError):
    """Response was not valid JSON in the expected shape."""


# (Removed CAPTCHA / rate-limit errors: the Instant Answer API doesn't
# serve anti-bot challenges. Kept the simpler hierarchy because the
# router in javis.py references these names for spoken-fallback messages.)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class SearchResult:
    title: str
    snippet: str
    url: str


# ---------------------------------------------------------------------------
# Searcher
# ---------------------------------------------------------------------------
class Searcher:
    """Performs DuckDuckGo Instant Answer searches and returns structured results."""

    def __init__(self, *, user_agent: str = USER_AGENT, timeout: float = DEFAULT_TIMEOUT):
        self.user_agent = user_agent
        self.timeout = timeout

    def search(self, query: str, *, max_results: int = 5, timeout: float | None = None) -> list[SearchResult]:
        """Run a search and return up to `max_results` results.

        Raises:
            ValueError: query is empty / too short / only stopwords.
            NetworkError: connection / timeout failure.
            ParseError: response was not valid JSON in the expected shape.
        """
        query = self._normalize_query(query)
        if not query:
            raise ValueError("Empty search query")
        if not self._is_meaningful(query):
            raise ValueError("Query is too short or only stopwords")

        timeout = timeout if timeout is not None else self.timeout
        try:
            response = requests.get(
                SEARCH_URL,
                params={
                    "q": query,
                    "format": "json",
                    "no_html": "1",
                    "skip_disambig": "1",
                    "t": "JavisVoice",
                },
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise NetworkError(f"Connection failed: {exc}") from exc

        # The Instant Answer API always returns 200 with a JSON body, even
        # for queries with no answer. Don't treat the response as a non-200
        # unless it's truly a hard failure.
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ParseError(f"Non-JSON response from search engine: {exc}") from exc

        return _extract_results(payload, max_results=max_results)

    # ---------- internals ----------

    @staticmethod
    def _normalize_query(query: str) -> str:
        """Trim, collapse whitespace, cap to MAX_QUERY_LEN."""
        if not query:
            return ""
        q = " ".join(query.split())
        if len(q) > MAX_QUERY_LEN:
            q = q[:MAX_QUERY_LEN].rsplit(" ", 1)[0]
        return q

    @staticmethod
    def _is_meaningful(query: str) -> bool:
        if len(query) < MIN_QUERY_LEN:
            return False
        tokens = [t for t in query.split() if t.lower() not in STOPWORDS]
        return bool(tokens)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
def _extract_results(payload: dict, *, max_results: int) -> list[SearchResult]:
    """Walk a DuckDuckGo Instant Answer JSON payload into SearchResult rows.

    Sources, in priority order:
      1. Abstract + AbstractURL (Wikipedia summary if DDG has one)
      2. Answer + AnswerType  (e.g., "Sunrise time in Paris: 06:42")
      3. Definition + DefinitionURL
      4. RelatedTopics[*].Text + FirstURL  (nested topics are flattened)
    """
    out: list[SearchResult] = []

    abstract = (payload.get("Abstract") or "").strip()
    abstract_url = payload.get("AbstractURL") or payload.get("AbstractSource") or ""
    if abstract:
        out.append(
            SearchResult(
                title=payload.get("Heading") or "Summary",
                snippet=abstract,
                url=abstract_url,
            )
        )

    answer = (payload.get("Answer") or "").strip()
    if answer:
        ans_type = payload.get("AnswerType", "")
        out.append(
            SearchResult(
                title=ans_type.capitalize() if ans_type else "Direct answer",
                snippet=answer,
                url="",
            )
        )

    definition = (payload.get("Definition") or "").strip()
    if definition:
        out.append(
            SearchResult(
                title=payload.get("DefinitionSource") or "Definition",
                snippet=definition,
                url=payload.get("DefinitionURL") or "",
            )
        )

    for t in _flatten_related(payload.get("RelatedTopics") or []):
        if len(out) >= max_results:
            break
        text = (t.get("Text") or "").strip()
        url = t.get("FirstURL") or ""
        if not text:
            continue
        # Related topic text format: "<Topic> — <description>"
        title, _, snippet = text.partition(" — ")
        out.append(
            SearchResult(
                title=title.strip() or "Related topic",
                snippet=snippet.strip() or text,
                url=url,
            )
        )

    return out


def _flatten_related(related: list, depth: int = 0) -> list[dict]:
    """Yield leaf topics, recursing into nested Topics lists up to depth 2."""
    if depth > 2:
        return []
    leaves: list[dict] = []
    for item in related:
        if not isinstance(item, dict):
            continue
        if "Topics" in item and isinstance(item["Topics"], list):
            leaves.extend(_flatten_related(item["Topics"], depth + 1))
        elif item.get("Text"):
            leaves.append(item)
    return leaves


# Public convenience
def search(query: str, *, max_results: int = 5) -> list[SearchResult]:
    """One-shot wrapper around Searcher().search()."""
    return Searcher().search(query, max_results=max_results)
