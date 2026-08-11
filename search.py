"""
Project Javis - Web search data layer.

Uses a no-key web search feed first, then falls back to DuckDuckGo's
Instant Answer JSON API. No browser launch. Returns SearchResult rows with
title, snippet, and URL fields.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from xml.etree import ElementTree

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BING_RSS_URL = "https://www.bing.com/search"
DUCKDUCKGO_ANSWER_URL = "https://api.duckduckgo.com/"
JINA_DUCKDUCKGO_URL = "https://r.jina.ai/http://duckduckgo.com/html/"
USER_AGENT = "Javis/1.0 (offline-voice-agent; +https://github.com)"
DEFAULT_TIMEOUT = 8.0
MIN_QUERY_LEN = 2
MAX_QUERY_LEN = 200

STOPWORDS = {"a", "an", "the", "it", "is", "and", "or", "of", "to", "in", "on"}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class SearcherError(Exception):
    """Base class for all search-layer errors."""


class NetworkError(SearcherError):
    """Could not reach the search engine."""


class ParseError(SearcherError):
    """Response was not in the expected shape."""


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
    """Performs web searches and returns structured results."""

    def __init__(self, *, user_agent: str = USER_AGENT, timeout: float = DEFAULT_TIMEOUT):
        self.user_agent = user_agent
        self.timeout = timeout

    def search(self, query: str, *, max_results: int = 5, timeout: float | None = None) -> list[SearchResult]:
        """Run a search and return up to `max_results` results."""
        query = self._normalize_query(query)
        if not query:
            raise ValueError("Empty search query")
        if not self._is_meaningful(query):
            raise ValueError("Query is too short or only stopwords")

        timeout = timeout if timeout is not None else self.timeout

        results = self._search_duckduckgo_text(query, max_results=max_results, timeout=timeout)
        if results:
            return results

        results = self._search_bing_rss(query, max_results=max_results, timeout=timeout)
        if results:
            return results

        return self._search_duckduckgo_answer(query, max_results=max_results, timeout=timeout)

    def _search_duckduckgo_text(self, query: str, *, max_results: int, timeout: float) -> list[SearchResult]:
        try:
            response = requests.get(
                f"{JINA_DUCKDUCKGO_URL}?q={quote_plus(query)}",
                headers={"User-Agent": self.user_agent, "Accept": "text/plain"},
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException:
            return []

        return _extract_markdown_results(response.text, max_results=max_results)

    def _search_bing_rss(self, query: str, *, max_results: int, timeout: float) -> list[SearchResult]:
        try:
            response = requests.get(
                BING_RSS_URL,
                params={"q": query, "format": "rss"},
                headers={"User-Agent": self.user_agent, "Accept": "application/rss+xml, text/xml"},
                timeout=timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise NetworkError(f"Connection failed: {exc}") from exc

        try:
            return _extract_rss_results(response.text, max_results=max_results)
        except ElementTree.ParseError as exc:
            raise ParseError(f"Search feed was not valid XML: {exc}") from exc

    def _search_duckduckgo_answer(self, query: str, *, max_results: int, timeout: float) -> list[SearchResult]:
        try:
            response = requests.get(
                DUCKDUCKGO_ANSWER_URL,
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
            response.raise_for_status()
        except requests.RequestException as exc:
            raise NetworkError(f"Connection failed: {exc}") from exc

        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ParseError(f"Non-JSON response from search engine: {exc}") from exc

        return _extract_answer_results(payload, max_results=max_results)

    # ---------- internals ----------

    @staticmethod
    def _normalize_query(query: str) -> str:
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
RESULT_HEADING_RE = re.compile(r"^## \[(?P<title>[^\]]+)\]\((?P<url>[^)]+)\)\s*$", re.MULTILINE)


def _extract_markdown_results(markdown_text: str, *, max_results: int) -> list[SearchResult]:
    matches = list(RESULT_HEADING_RE.finditer(markdown_text))
    out: list[SearchResult] = []

    for index, match in enumerate(matches):
        if len(out) >= max_results:
            break

        block_start = match.end()
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown_text)
        block = markdown_text[block_start:block_end]

        title = _clean_text(match.group("title"))
        url = _decode_duckduckgo_url(match.group("url"))
        snippet = _snippet_from_markdown_block(block)
        if not title and not snippet:
            continue

        out.append(
            SearchResult(
                title=title or "Search result",
                snippet=snippet or title,
                url=url,
            )
        )

    return out


def _extract_rss_results(feed_text: str, *, max_results: int) -> list[SearchResult]:
    root = ElementTree.fromstring(feed_text)
    out: list[SearchResult] = []

    for item in root.findall("./channel/item"):
        if len(out) >= max_results:
            break

        title = _clean_text(item.findtext("title") or "")
        snippet = _clean_text(item.findtext("description") or "")
        url = _clean_text(item.findtext("link") or "")
        if not title and not snippet:
            continue

        out.append(
            SearchResult(
                title=title or "Search result",
                snippet=snippet or title,
                url=url,
            )
        )

    return out


def _extract_answer_results(payload: dict, *, max_results: int) -> list[SearchResult]:
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

    for topic in _flatten_related(payload.get("RelatedTopics") or []):
        if len(out) >= max_results:
            break
        text = (topic.get("Text") or "").strip()
        url = topic.get("FirstURL") or ""
        if not text:
            continue
        title, _, snippet = text.partition(" - ")
        out.append(
            SearchResult(
                title=title.strip() or "Related topic",
                snippet=snippet.strip() or text,
                url=url,
            )
        )

    return out


def _flatten_related(related: list, depth: int = 0) -> list[dict]:
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


def _clean_text(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return " ".join(text.split()).strip()


def _snippet_from_markdown_block(block: str) -> str:
    lines = []
    for raw_line in block.splitlines():
        line = _clean_text(raw_line)
        if not line:
            continue
        if line.startswith("http://") or line.startswith("https://"):
            continue
        line = re.sub(r"^[\w.-]+\.[a-z]{2,}(?:/\S*)?\s*", "", line, flags=re.IGNORECASE)
        line = re.sub(r"^\d{4}-\d{2}-\d{2}T\S+\s*", "", line)
        if line.lower().startswith("cached"):
            continue
        if not line:
            continue
        lines.append(line)

    snippet = " ".join(lines)
    if len(snippet) > 320:
        snippet = snippet[:320].rsplit(" ", 1)[0] + "..."
    return snippet


def _decode_duckduckgo_url(url: str) -> str:
    parsed = urlparse(url)
    if "duckduckgo.com" not in parsed.netloc:
        return url

    query = parse_qs(parsed.query)
    target = query.get("uddg", [""])[0]
    return unquote(target) if target else url


# Public convenience
def search(query: str, *, max_results: int = 5) -> list[SearchResult]:
    """One-shot wrapper around Searcher().search()."""
    return Searcher().search(query, max_results=max_results)
