"""Async client for the LawBey Open WebUI (Railway) instance.

Talks to the OpenAI-compatible chat completions endpoint, scoped to the CARTA
knowledge base via the `files` field. The Open WebUI API key is held only here
(server-side) and never returned to callers.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .config import Settings

logger = logging.getLogger("lawbey_mcp.openwebui")

# Cap on how many file IDs we pass into chat completions after a collection
# pre-query. Enough for grounded multi-doc answers; low enough to avoid the
# per-file hybrid-search hang that appears once dozens of files are attached.
_RAG_FILE_CAP = 12

# Legal / regulatory acronyms common in the CARTA KB. Expanded forms are
# appended to retrieval queries so MiniLM (and stronger embedders) can match
# statute filenames and body text that spell the term out.
_ACRONYM_EXPANSIONS: Dict[str, str] = {
    "IBC": "International Business Company",
    "IBCs": "International Business Companies",
    "DARE": "Digital Assets and Registered Exchanges",
    "AML": "anti-money laundering",
    "CFT": "countering the financing of terrorism",
    "BTCRA": "Banks and Trust Companies Regulation Act",
    "BTCR": "Banks and Trust Companies Regulation",
    "FCSP": "Financial and Corporate Service Providers",
    "FTRA": "Financial Transactions Reporting Act",
    "SIA": "Securities Industry Act",
    "CESRA": "Commercial Entities Substance Requirements",
    "IFA": "Investment Funds Act",
    "AIFM": "alternative investment fund manager",
    "SMART": "SMART fund",
}

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "in",
        "on",
        "for",
        "to",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "when",
        "where",
        "why",
        "does",
        "do",
        "did",
        "can",
        "could",
        "should",
        "would",
        "may",
        "might",
        "must",
        "with",
        "from",
        "into",
        "about",
        "under",
        "over",
        "between",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "as",
        "at",
        "by",
        "please",
        "tell",
        "me",
        "explain",
        "describe",
        "bahamas",
        "bahamian",
        "law",
        "laws",
        "act",
        "acts",
        "section",
        "sections",
    }
)


def _retrieval_queries(query: str) -> List[str]:
    """Build complementary retrieval strings for one user question.

    MiniLM ranks short keyword queries far better than long natural-language
    questions over a large multi-act KB. We therefore search with:
      1. the original question (preserves phrasing),
      2. the question with known acronyms expanded,
      3. a keyword-only distill (tokens + expansions, stopwords stripped).
    """
    q = (query or "").strip()
    if not q:
        return []

    out: List[str] = [q]

    # Acronym expansion: case-sensitive whole-word match so English words like
    # "smart" do not expand via the SMART fund entry.
    expanded = q
    for acr, full in _ACRONYM_EXPANSIONS.items():
        expanded = re.sub(rf"\b{re.escape(acr)}\b", f"{acr} {full}", expanded)
    if expanded != q:
        out.append(expanded)

    # Keyword distill: keep content tokens + any matched expansions.
    tokens = re.findall(r"[A-Za-z0-9§]+", q)
    keywords: List[str] = []
    seen_kw: set[str] = set()
    for tok in tokens:
        low = tok.lower()
        if low in _STOPWORDS or len(tok) < 2:
            continue
        if low not in seen_kw:
            seen_kw.add(low)
            keywords.append(tok)
        # Case-exact acronym match (same rule as expansion) so "smart" ≠ SMART.
        if tok in _ACRONYM_EXPANSIONS:
            full = _ACRONYM_EXPANSIONS[tok]
            if full.lower() not in seen_kw:
                seen_kw.add(full.lower())
                keywords.append(full)
    if keywords:
        kw_query = " ".join(keywords)
        if kw_query.lower() not in {s.lower() for s in out}:
            out.append(kw_query)

    return out


class OpenWebUIError(Exception):
    """Raised when the upstream LawBey instance returns an unusable response."""

    def __init__(self, kind: str, message: str, status: Optional[int] = None):
        self.kind = kind
        self.status = status
        self.message = message
        super().__init__(message)


class OpenWebUIClient:
    """Thin async wrapper over the Open WebUI REST API used by the MCP server."""

    def __init__(self, settings: Settings, timeout: float = 120.0):
        self._settings = settings
        self._base = settings.openwebui_base_url
        self._timeout = timeout
        # Cached display name of the scoped knowledge base. Open WebUI's chat
        # completions `sources[].source` only carries {type, id} (no name), so
        # we resolve the KB name once and inject it into each source's
        # `collection` field to satisfy the response contract.
        self._kb_name: Optional[str] = None

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.openwebui_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def get_models(self) -> List[Dict[str, Any]]:
        """Return the list of models available on the LawBey instance."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self._base}/api/models", headers=self._headers())
        if resp.status_code == 401:
            # Do not leak upstream auth detail to partners.
            logger.error("Open WebUI returned 401 while listing models")
            raise OpenWebUIError("auth_error", "Upstream authentication failed")
        if resp.status_code >= 400:
            raise OpenWebUIError(
                "upstream_error",
                f"LawBey returned non-200 status: {resp.status_code}",
                status=resp.status_code,
            )
        data = resp.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data if isinstance(data, list) else []

    async def _resolve_kb_name(self) -> str:
        """Fetch and cache the display name of the scoped knowledge base.

        Falls back to the KB id if the lookup fails, so the response contract
        always carries a non-empty `collection` label.
        """
        if self._kb_name is not None:
            return self._kb_name
        kb_id = self._settings.openwebui_kb_id
        name = kb_id
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._base}/api/v1/knowledge/{kb_id}",
                    headers=self._headers(),
                )
            if resp.status_code < 400:
                data = resp.json()
                if isinstance(data, dict) and data.get("name"):
                    name = data["name"]
        except Exception as e:
            logger.warning("KB name lookup failed (using id as fallback): %s", e)
        self._kb_name = name
        return name

    async def _query_collection_once(
        self, client: httpx.AsyncClient, kb_id: str, query: str, k: int
    ) -> List[Tuple[str, str, float]]:
        """Run one collection vector query. Returns [(file_id, name, rank_score)]."""
        try:
            resp = await client.post(
                f"{self._base}/api/v1/retrieval/query/collection",
                headers=self._headers(),
                json={
                    "collection_names": [kb_id],
                    "query": query,
                    "k": k,
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            logger.warning("Collection pre-query transport error: %s", e)
            return []

        if resp.status_code >= 400:
            logger.warning(
                "Collection pre-query failed with status %s", resp.status_code
            )
            return []

        data = resp.json() if resp.content else {}
        metadatas = data.get("metadatas") or []
        distances = data.get("distances") or []
        flat_meta: List[Dict[str, Any]] = []
        flat_dist: List[float] = []
        for group in metadatas:
            if isinstance(group, list):
                flat_meta.extend(m for m in group if isinstance(m, dict))
            elif isinstance(group, dict):
                flat_meta.append(group)
        for group in distances:
            if isinstance(group, list):
                flat_dist.extend(float(x) for x in group if isinstance(x, (int, float)))
            elif isinstance(group, (int, float)):
                flat_dist.append(float(group))

        # Ignore raw distances: Open WebUI may return similarity or distance
        # depending on hybrid vs vector mode. Rank position is stable.
        _ = flat_dist
        hits: List[Tuple[str, str, float]] = []
        for i, meta in enumerate(flat_meta):
            fid = meta.get("file_id") or meta.get("id")
            name = str(meta.get("name") or "")
            if not isinstance(fid, str) or not fid:
                continue
            hits.append((fid, name, 1.0 / (1.0 + i)))
        return hits

    async def _query_relevant_file_ids(self, query: str) -> List[str]:
        """Pre-retrieve relevant file IDs via multi-query collection search.

        Open WebUI v0.6.34's chat-completions path with ``type: collection``
        returns irrelevant chunks even when the retrieval API can find the
        right statutes. We therefore:
          1. rewrite the user question into complementary retrieval strings
             (original + acronym-expanded + keyword distill),
          2. query the KB collection with each,
          3. merge/rank unique file IDs and pass the top set into chat as
             ``type: file`` references (generation works; all-files hangs).
        """
        kb_id = self._settings.openwebui_kb_id
        queries = _retrieval_queries(query)
        if not queries:
            return []

        # Per-query k a bit above the final cap so merges have headroom.
        per_k = max(_RAG_FILE_CAP, 8)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            results = await asyncio.gather(
                *[
                    self._query_collection_once(client, kb_id, rq, per_k)
                    for rq in queries
                ]
            )

        # Aggregate: sum rank scores; bonus once per file when filename tokens
        # intersect query tokens (stopwords stripped; no loose substring match).
        q_tokens = {
            t.lower()
            for t in re.findall(r"[A-Za-z0-9]+", query)
            if len(t) > 2 and t.lower() not in _STOPWORDS
        }
        for acr, full in _ACRONYM_EXPANSIONS.items():
            if acr.lower() in q_tokens or any(
                w.lower() in q_tokens for w in full.split() if len(w) > 3
            ):
                q_tokens.add(acr.lower())
                q_tokens.update(
                    w.lower()
                    for w in full.split()
                    if len(w) > 2 and w.lower() not in _STOPWORDS
                )

        scores: Dict[str, float] = {}
        names: Dict[str, str] = {}
        boosted: set[str] = set()
        for hits in results:
            for fid, name, rank_score in hits:
                scores[fid] = scores.get(fid, 0.0) + rank_score
                names[fid] = name or names.get(fid, "")
                if fid in boosted:
                    continue
                # Filename tokens: ibc_act_01.md -> {ibc, act, 01, md}
                name_tokens = {
                    t.lower()
                    for t in re.findall(r"[A-Za-z0-9]+", name)
                    if len(t) >= 3 and t.lower() not in _STOPWORDS
                }
                if q_tokens & name_tokens:
                    scores[fid] += 0.75
                    boosted.add(fid)

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        file_ids = [fid for fid, _ in ranked[:_RAG_FILE_CAP]]
        if file_ids:
            logger.info(
                "RAG multi-query (%d variants) selected %d file(s): %s",
                len(queries),
                len(file_ids),
                [names.get(f, f[:8]) for f in file_ids[:6]],
            )
        return file_ids

    async def chat_completion(
        self,
        query: str,
        context: Optional[str] = None,
        use_rag: bool = True,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Call POST /api/chat/completions scoped to the CARTA knowledge base.

        Returns a dict with: answer (str), sources (list), raw (full json).
        Retries once on 503 / network error after a 2s delay.
        """
        content = query if not context else f"{query}\n\nContext: {context}"
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})
        payload: Dict[str, Any] = {
            "model": self._settings.openwebui_model_id,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if use_rag:
            # Two-step RAG: collection vector search → pass only the top file
            # IDs into chat completions. type:collection chat is broken on this
            # Open WebUI version (irrelevant chunks), so an empty pre-query
            # fails closed instead of falling back.
            file_ids = await self._query_relevant_file_ids(query)
            if not file_ids:
                raise OpenWebUIError(
                    "rag_unavailable",
                    "Knowledge base retrieval returned no matching documents",
                )
            payload["files"] = [
                {"type": "file", "id": fid} for fid in file_ids
            ]
            logger.info(
                "RAG pre-query selected %d file(s) for chat completions",
                len(file_ids),
            )

        last_exc: Optional[OpenWebUIError] = None
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        f"{self._base}/api/chat/completions",
                        headers=self._headers(),
                        json=payload,
                    )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                logger.warning("Open WebUI transport error (attempt %d): %s", attempt, e)
                if attempt == 1:
                    await asyncio.sleep(2)
                    last_exc = OpenWebUIError("upstream_unavailable", "Upstream timeout")
                    continue
                raise OpenWebUIError("upstream_unavailable", "Upstream timed out")

            if resp.status_code == 401:
                logger.error("Open WebUI returned 401 during chat completion")
                raise OpenWebUIError("auth_error", "Upstream authentication failed")
            if resp.status_code in (502, 503, 504):
                logger.warning("Open WebUI %d on attempt %d", resp.status_code, attempt)
                if attempt == 1:
                    await asyncio.sleep(2)
                    last_exc = OpenWebUIError(
                        "upstream_unavailable",
                        f"LawBey returned status {resp.status_code}",
                        status=resp.status_code,
                    )
                    continue
                raise OpenWebUIError(
                    "upstream_unavailable",
                    f"LawBey returned status {resp.status_code}",
                    status=resp.status_code,
                )
            if resp.status_code >= 400:
                raise OpenWebUIError(
                    "upstream_error",
                    f"LawBey returned non-200 status: {resp.status_code}",
                    status=resp.status_code,
                )

            data = resp.json()
            answer = _extract_answer(data)
            sources = _extract_sources(data)
            # Open WebUI omits the collection name from `sources[].source`; fill
            # it in from the (cached) KB display name for our scoped collection.
            kb_name = await self._resolve_kb_name()
            for s in sources:
                if not s.get("collection"):
                    s["collection"] = kb_name
            return {"answer": answer, "sources": sources, "raw": data}

        # Should be unreachable, but keep a safe fallback.
        raise last_exc or OpenWebUIError("upstream_unavailable", "Upstream unavailable")


def _extract_answer(data: Dict[str, Any]) -> str:
    """Pull the assistant message content out of an OpenAI-shaped response."""
    try:
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                return content
    except Exception:
        pass
    return ""


def _extract_sources(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize Open WebUI's `sources` extension into a flat list of dicts.

    Open WebUI returns sources as:
        [{"source": {"name": ...}, "document": ["chunk", ...],
          "metadata": [{"file_id":..., "name":...}], "distances": [...]}]
    """
    out: List[Dict[str, Any]] = []
    for src in data.get("sources") or []:
        if not isinstance(src, dict):
            continue
        source_meta = src.get("source") or {}
        metadata = src.get("metadata") or []
        document = src.get("document") or []
        distances = src.get("distances") or []

        # metadata may be a list of per-chunk dicts; pick the first file name.
        document_name = ""
        if isinstance(metadata, list) and metadata:
            first = metadata[0]
            if isinstance(first, dict):
                document_name = first.get("name") or first.get("file_id") or ""

        out.append(
            {
                "document_name": document_name,
                "collection": source_meta.get("name") if isinstance(source_meta, dict) else "",
                "chunks": list(document) if isinstance(document, list) else [],
                "relevance_scores": list(distances) if isinstance(distances, list) else [],
            }
        )
    return out
