"""Partner API-key authentication.

Carta authenticates to OUR MCP server using a shared secret we generate
(``Authorization: Bearer carta:SECRET``). The Open WebUI key is never exposed
to partners — it lives only server-side and is used by openwebui.py.
"""

from __future__ import annotations

import logging
from typing import Dict

from fastapi import Header, HTTPException, status

from .config import Settings

logger = logging.getLogger("lawbey_mcp.auth")


def _parse_authorization(authorization: str | None) -> tuple[str, str] | None:
    """Return (partner_name, secret) from a Bearer header, or None if malformed."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if ":" not in token:
        return None
    name, secret = token.split(":", 1)
    name, secret = name.strip(), secret.strip()
    if not name or not secret:
        return None
    return name, secret


def verify_partner_key(
    settings: Settings,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str:
    """FastAPI dependency: validate the partner key, return the partner name.

    Raises 401 on missing / malformed / unknown keys.
    """
    parsed = _parse_authorization(authorization)
    if parsed is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Expected: Bearer <name>:<secret>",
        )
    name, secret = parsed

    partner_keys: Dict[str, str] = settings.partner_keys()
    expected = partner_keys.get(name)
    if not expected or not _constant_time_eq(expected, secret):
        logger.warning("Rejected partner key for name=%r", name)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid partner credentials",
        )
    return name


def _constant_time_eq(a: str, b: str) -> bool:
    """Timing-safe string comparison."""
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())
