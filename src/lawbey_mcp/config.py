"""Application configuration loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Dict

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the LawBey MCP server.

    All values are read from environment variables (or a local .env file).
    The Open WebUI API key is a server-side secret — it must never appear in
    client code, responses, or git.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Upstream LawBey Open WebUI (Railway) instance.
    openwebui_base_url: str = "https://lawbey-railway-production.up.railway.app"
    openwebui_api_key: str = ""
    openwebui_model_id: str = "gpt-4o-mini-2024-07-18"
    openwebui_kb_id: str = "cda0f2ba-88d1-4c73-8a58-50d14180ba98"

    # Sampling temperature sent to Open WebUI. 0.0 = deterministic answers for a
    # given retrieval (the retrieval itself is non-deterministic; see retries).
    openwebui_temperature: float = 0.0

    # RAG retrieval on the upstream instance is bimodal/non-deterministic
    # (some requests retrieve the relevant chunks, others retrieve unrelated
    # ones). Retry on a decline up to this many total attempts so a grounded
    # answer is almost always returned. P(grounded) = 1 - (1-p)^n.
    rag_max_attempts: int = 3

    # Partner authentication. Format: "name:secret,name:secret".
    partner_api_keys: str = ""

    # Server config.
    log_level: str = "INFO"
    port: int = 8000

    # Rate limits per partner key.
    rate_limit_per_hour: int = 100
    rate_limit_per_day: int = 1000

    @field_validator("openwebui_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("partner_api_keys")
    @classmethod
    def _normalize_partner_keys(cls, v: str) -> str:
        # Tolerate whitespace / empty entries.
        parts = [p.strip() for p in v.split(",") if p.strip()]
        return ",".join(parts)

    def partner_keys(self) -> Dict[str, str]:
        """Return a {partner_name: secret} map from the PARTNER_API_KEYS env var."""
        if not self.partner_api_keys:
            return {}
        out: Dict[str, str] = {}
        for entry in self.partner_api_keys.split(","):
            if ":" not in entry:
                continue
            name, secret = entry.split(":", 1)
            name, secret = name.strip(), secret.strip()
            if name and secret:
                out[name] = secret
        return out


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor — use Depends(get_settings) in routes."""
    return Settings()
