"""FastAPI application entrypoint for the LawBey MCP server.

Routes:
  GET  /health        — liveness probe
  POST /debug/query   — dev-only REST shim that calls the legal query directly
  GET  /sse           — MCP SSE transport endpoint (Carta connects here)
  POST /messages      — MCP message handler

The MCP SDK's SSE app is mounted at "/" so its internal /sse and /messages
routes resolve at the root, while /health and /debug/query are declared first
so they win the route match.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .mcp_server import create_mcp_server, run_legal_query
from .openwebui import OpenWebUIClient
from .ratelimit import RateLimiter, enforce_rate_limit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("lawbey_mcp")


class QueryRequest(BaseModel):
    """Body for POST /debug/query."""

    query: str = Field(..., description="The legal question to ask.")
    context: str | None = Field(
        default=None, description="Optional framing context (no PII)."
    )
    use_rag: bool = Field(
        default=True,
        description=(
            "If False, query Open WebUI without the CARTA collection — used to "
            "prove the files/RAG parameter is actually being applied."
        ),
    )


def create_app() -> FastAPI:
    """Application factory — wires settings, MCP server, SSE mount, routes."""
    settings = get_settings()
    logging.getLogger().setLevel(settings.log_level.upper())

    if not settings.openwebui_api_key or settings.openwebui_api_key.startswith("sk-replace"):
        logger.warning("OPENWEBUI_API_KEY is not set — upstream calls will fail.")
    if not settings.partner_keys():
        logger.warning("PARTNER_API_KEYS is empty — no partner will be able to authenticate.")

    mcp = create_mcp_server(settings)
    sse_app = mcp.sse_app()  # Starlette app with /sse + /messages routes

    # FastAPI app. The mounted SSE sub-app owns the MCP session manager lifespan.
    app = FastAPI(
        title="LawBey MCP Server",
        description="Bahamian legal RAG exposed as an MCP tool for Carta.",
        version="0.1.0",
        lifespan=sse_app.lifespan if hasattr(sse_app, "lifespan") else None,
    )

    # Shared state.
    app.state.settings = settings
    app.state.mcp = mcp
    app.state.client = OpenWebUIClient(settings)
    app.state.rate_limiter = RateLimiter(settings)

    @app.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok", "service": "lawbey-mcp"}

    @app.post("/debug/query")
    async def debug_query(
        body: QueryRequest,
        request: Request,
        partner: str = Depends(verify_partner_key_dep),
    ) -> Dict[str, Any]:
        """Dev-only REST shim that runs the legal query without the MCP layer.

        Auth + rate limit apply. Useful for fast iteration and the acceptance
        tests. Disable or gate behind a flag for production if desired.
        """
        settings: Settings = request.app.state.settings
        limiter: RateLimiter = request.app.state.rate_limiter
        enforce_rate_limit(settings, limiter, partner)

        client: OpenWebUIClient = request.app.state.client
        result = await run_legal_query(
            settings, client, body.query, body.context, use_rag=body.use_rag
        )

        # Surface query-too-long as an explicit 400 to match the brief.
        if result.get("error") == "query_too_long":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result["message"])
        return result

    # IMPORTANT: mount the SSE app AFTER the explicit routes above so that
    # /health and /debug/query match before the catch-all "/" mount.
    app.mount("/", sse_app)

    return app


def verify_partner_key_dep(request: Request) -> str:
    """FastAPI dependency: validate the partner Bearer key against app settings.

    Returns the partner name on success; raises 401 on missing/malformed/invalid.
    Settings are read from app.state so no global is needed.
    """
    import hmac

    from .auth import _parse_authorization

    settings: Settings = request.app.state.settings
    parsed = _parse_authorization(request.headers.get("authorization"))
    if parsed is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Expected: Bearer <name>:<secret>",
        )
    name, secret = parsed
    expected = settings.partner_keys().get(name)
    if not expected or not hmac.compare_digest(expected.encode(), secret.encode()):
        logger.warning("Rejected partner key for name=%r", name)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid partner credentials"
        )
    return name


# Module-level app for `uvicorn lawbey_mcp.main:app`.
app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "lawbey_mcp.main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
