"""Async client for the LawBey Open WebUI (Railway) instance.

Talks to the OpenAI-compatible chat completions endpoint, scoped to the CARTA
knowledge base via the `files` field. The Open WebUI API key is held only here
(server-side) and never returned to callers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from .config import Settings

logger = logging.getLogger("lawbey_mcp.openwebui")


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

    async def chat_completion(
        self,
        query: str,
        context: Optional[str] = None,
        use_rag: bool = True,
        system: Optional[str] = None,
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
        if use_rag:
            payload["files"] = [
                {"type": "collection", "id": self._settings.openwebui_kb_id}
            ]

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
