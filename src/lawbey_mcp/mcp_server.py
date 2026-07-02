"""MCP tool definitions for the LawBey server.

Exposes ``search_bahamian_law`` — a grounded Q&A tool over LawBey's Bahamian
legal knowledge base — plus a phase-2 ``list_available_statutes`` stub.

The actual upstream call is shared (``run_legal_query``) so the MCP tool and the
dev ``/debug/query`` REST endpoint behave identically.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .openwebui import OpenWebUIClient, OpenWebUIError

logger = logging.getLogger("lawbey_mcp.tool")

DISCLAIMER = (
    "For specific legal matters, please consult with a qualified Bahamian "
    "attorney or contact the Bahamas Bar Association."
)

MAX_QUERY_LEN = 2000

# NOTE: We intentionally do NOT send a system prompt — see run_legal_query.
# Open WebUI's RAG injects retrieved chunks itself; an extra system message
# causes the model to reject its own retrieved context.

# Matches inline citations like [Smuggling of Migrants Act, 2025 §5(3)(a)] or [§5(1)]
_CITATION_RE = re.compile(r"\[([^\[\]]+?(?:§[^\[\]]+)?)\]")
# Decline signals — Open WebUI's standard RAG non-answer phrasings plus explicit
# scope statements. When matched, the answer is treated as NOT grounded even if
# chunks were retrieved (the model declined rather than using the context).
_DECLINE_RE = re.compile(
    r"(provided context does not contain|does not contain (enough )?information|"
    r"not (mentioned|included|found) in (the )?(provided )?context|"
    r"context does not (include|contain)|"
    r"\b(only covers bahamian law|outside (my )?scope|not about bahamian|"
    r"cannot (help|answer)|beyond (my )?scope)\b)",
    re.IGNORECASE,
)


def create_mcp_server(settings: Settings) -> FastMCP:
    """Build the FastMCP instance with the LawBey tools registered."""
    mcp = FastMCP("lawbey-mcp")
    client = OpenWebUIClient(settings)

    @mcp.tool()
    async def search_bahamian_law(query: str, context: Optional[str] = None) -> Dict[str, Any]:
        """Search LawBey's Bahamian legal knowledge base and return a grounded
        legal answer with citations to specific statutes and sections.

        Args:
            query: The legal question to ask (plain English).
                   Example: "What are the penalties for smuggling migrants in The Bahamas?"
            context: Optional. Additional context about the user's situation
                     to help frame the question. Do NOT include personally
                     identifiable information.

        Returns:
            {
                "answer": str,           # Legal answer with inline citations
                "sources": list[dict],   # Retrieved statute chunks
                "citations": list[str],  # Extracted citation references
                "grounded": bool,        # True if answer is based on retrieved docs
                "disclaimer": str        # Standard legal disclaimer
            }
        """
        return await run_legal_query(settings, client, query, context, use_rag=True)

    @mcp.tool()
    async def list_available_statutes() -> Dict[str, Any]:
        """Returns a list of Bahamian statutes currently available in LawBey's
        knowledge base. Use this to understand what topics LawBey can answer
        authoritatively before constructing a query.

        Phase 2: returns a placeholder for now; will be backed by a KB listing.
        """
        return {
            "available": False,
            "message": (
                "Statute listing is not yet wired up. The CARTA knowledge base "
                "covers Bahamian statutes, regulations, and legal guidance — "
                "use search_bahamian_law to query it directly."
            ),
            "statutes": [],
        }

    return mcp


async def run_legal_query(
    settings: Settings,
    client: OpenWebUIClient,
    query: str,
    context: Optional[str],
    use_rag: bool = True,
) -> Dict[str, Any]:
    """Shared query executor used by the MCP tool and the /debug/query endpoint.

    Enforces the query length cap, calls Open WebUI, and shapes the response
    per the brief's response contract.
    """
    if not query or not query.strip():
        return _error("empty_query", "Query must not be empty.")
    if len(query) > MAX_QUERY_LEN:
        return _error("query_too_long", f"Query exceeds maximum length of {MAX_QUERY_LEN} characters.")

    # We deliberately do NOT send a system prompt: Open WebUI's RAG injects the
    # retrieved CARTA chunks into the conversation itself, and an extra system
    # message makes the model reject its own retrieved context ("the provided
    # context does not contain..."). Open WebUI's default behavior already
    # produces well-cited answers and naturally declines off-topic questions
    # (e.g. US corporate tax) when the retrieved context is irrelevant.
    try:
        result = await client.chat_completion(
            query=query, context=context, use_rag=use_rag, system=None
        )
    except OpenWebUIError as e:
        logger.error("Upstream error: kind=%s status=%s msg=%s", e.kind, e.status, e.message)
        return _error(e.kind, e.message)

    answer: str = result["answer"]
    sources: List[Dict[str, Any]] = result["sources"]

    grounded = any(bool(s.get("chunks")) for s in sources)
    citations = _extract_citations(answer)
    is_decline = bool(_DECLINE_RE.search(answer))

    # A scoped decline is never "grounded" even if the model produced text.
    if is_decline:
        grounded = False

    return {
        "answer": answer,
        "sources": sources,
        "citations": citations,
        "grounded": grounded,
        "disclaimer": DISCLAIMER,
    }


def _extract_citations(answer: str) -> List[str]:
    """Pull unique bracketed citation references out of the answer text."""
    seen: list[str] = []
    for m in _CITATION_RE.finditer(answer):
        ref = m.group(1).strip()
        if ref and ref not in seen:
            seen.append(ref)
    return seen


def _error(kind: str, message: str) -> Dict[str, Any]:
    return {
        "answer": None,
        "sources": [],
        "citations": [],
        "grounded": False,
        "disclaimer": DISCLAIMER,
        "error": kind,
        "message": message,
    }
