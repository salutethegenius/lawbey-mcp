# LawBey MCP Server

A standalone FastAPI MCP (Model Context Protocol) server that exposes
LawBey's Bahamian legal RAG system as a callable tool for AI agents.
The first integration partner is **Carta** (Marvin / truth.attorney).

It is a thin wrapper over LawBey's Open WebUI (Railway) instance — it does
**not** modify LawBey's pipeline. It opens a second front door into the same
system that LawBey's UI users already use.

## Architecture

```
Carta Agent
   |  Authorization: Bearer carta:SECRET
   v
LawBey MCP Server (FastAPI, this repo)        ->  Open WebUI (Railay)
   |  validates partner key                      POST /api/chat/completions
   |  enforces per-partner rate limit            with files=[{collection, CARTA KB}]
   |  forwards query via OpenWebUIClient         (server-side Open WebUI key)
   v
search_bahamian_law MCP tool -> structured {answer, sources, citations, grounded, disclaimer}
```

Carta never sees the Open WebUI API key — only the partner key we issue.

## Tools

- `search_bahamian_law(query, context=None)` — grounded Q&A over the CARTA
  knowledge base. Returns `{answer, sources, citations, grounded, disclaimer}`.
  `grounded: true` means the answer used retrieved KB chunks (real RAG).
- `list_available_statutes()` — phase 2 stub (not yet wired to a KB listing).

## Configuration

All config is via environment / `.env` (see `.env.example`):

| Var | Purpose |
|---|---|
| `OPENWEBUI_BASE_URL` | LawBey Open WebUI base URL |
| `OPENWEBUI_API_KEY` | Open WebUI API key (`sk-...`) — server-side secret |
| `OPENWEBUI_MODEL_ID` | Model id (default `gpt-4o-mini-2024-07-18`) |
| `OPENWEBUI_KB_ID` | CARTA knowledge base collection UUID |
| `PARTNER_API_KEYS` | `name:secret,name:secret` map of partner keys |
| `LOG_LEVEL` | `INFO` (default) |
| `PORT` | `8000` (default) |
| `RATE_LIMIT_PER_HOUR` / `RATE_LIMIT_PER_DAY` | per-partner limits (100 / 1000) |

Generate a partner key:
```bash
python -c "import secrets; print('carta:' + secrets.token_urlsafe(32))"
```

## Local development

```bash
git clone <repo>
cd lawbey-mcp
cp .env.example .env       # then fill in OPENWEBUI_API_KEY and PARTNER_API_KEYS
uv sync
uv run uvicorn lawbey_mcp.main:app --reload --port 8000
```

### Health check
```bash
curl http://localhost:8000/health
# {"status":"ok","service":"lawbey-mcp"}
```

### Fast iteration (bypass MCP)
```bash
curl -X POST http://localhost:8000/debug/query \
  -H "Authorization: Bearer carta:YOUR_PARTNER_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"query":"What are the penalties for smuggling migrants in The Bahamas?"}'
```

### Test the MCP protocol
Use the MCP Inspector:
```bash
npx @modelcontextprotocol/inspector
# point it at http://localhost:8000/sse
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | `{"status":"ok","service":"lawbey-mcp"}` |
| `POST` | `/debug/query` | Dev REST shim (auth + rate limit) |
| `GET` | `/sse` | MCP SSE transport endpoint (Carta connects here) |
| `POST` | `/messages` | MCP message handler |

## Deploy (Fly.io, Miami region)

```bash
brew install flyctl && flyctl auth login
fly launch --region mia        # accept defaults; generates fly.toml
fly secrets set OPENWEBUI_API_KEY=sk-...
fly secrets set PARTNER_API_KEYS=carta:...
fly deploy
```

The app listens on `$PORT` (Fly sets this automatically). The Dockerfile or
`fly.toml` should run `uv run uvicorn lawbey_mcp.main:app --host 0.0.0.0 --port ${PORT:-8000}`.

## Notes

- The CARTA KB UUID (`cda0f2ba-...`) on Railway is currently named
  **"LawBey Knowledge Base"**, not "CARTA mcp-test". The UUID is correct and
  the server works; only the `sources[].collection` label differs. Renaming the
  KB on Railway requires explicit sign-off from Kenneth.

- **RAG quality flag for Kenneth (important):** the server is working correctly,
  but the CARTA knowledge base's `smuggling_of_migrants_act_2025.md` does not
  retrieve the §5 penalty section for penalty questions — the chunks that come
  back are jurisdiction/scope sections. As a result, `search_bahamian_law` on
  "What are the penalties for smuggling migrants in The Bahamas?" returns
  `grounded: true` (chunks were retrieved) but the answer is weaker than the
  model's own knowledge: with RAG off, gpt-4o-mini cites `[§5(3)(a)]`
  ($100,000 / 7 years) correctly; with RAG on, it sometimes gives a different
  figure or declines. This is a LawBey-side data/indexing issue (the penalty
  section appears not to be in the indexed chunks, or is poorly chunked), not
  an MCP server bug. Fixing it requires re-indexing the source document on the
  Railway Open WebUI instance — which needs explicit sign-off per the brief.
  Do NOT add a system prompt or citation instruction to "fix" this: any system
  message makes gpt-4o-mini reject the retrieved context entirely ("the provided
  context does not contain..."), which is worse.

- No UI, no conversation history, no response caching, no streaming in Phase 1.
- All LawBey API keys live only in `.env` / Fly secrets — never in code or git.
