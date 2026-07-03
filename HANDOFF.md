# Carta Integration — LawBey MCP Test Endpoint (Handoff)

**Live server:** https://lawbey-mcp.fly.dev
**Status:** deployed · `/health` → `{"status":"ok","service":"lawbey-mcp"}`
**Repo (private):** https://github.com/salutethegenius/lawbey-mcp

> The Carta **partner API key** is a secret and is **not** in this document.
> It is delivered out-of-band (Slack / 1Password / email). The header value to
> send is `Bearer carta:<PARTNER_KEY>`.

---

## 1. Two ways to connect

### Option A — REST shim (simplest; start here)

```
POST https://lawbey-mcp.fly.dev/debug/query
Authorization: Bearer carta:<PARTNER_KEY>
Content-Type: application/json

{"query":"What are the penalties for smuggling migrants in The Bahamas?"}
```

### Option B — MCP SSE transport (for an MCP-aware agent)

- SSE stream: `GET  https://lawbey-mcp.fly.dev/sse`
- Messages:  `POST https://lawbey-mcp.fly.dev/messages`
- Same `Authorization: Bearer carta:<PARTNER_KEY>` header on both.
- Tool exposed: `search_bahamian_law(query: str, context: str | None = None)`

---

## 2. Tool: `search_bahamian_law`

**Args**
- `query` (str, required): plain-English legal question. Max 2000 chars.
- `context` (str, optional): situational framing. Do **not** include PII.

**Returns**
```json
{
  "answer":      "On summary conviction for basic smuggling: a fine not exceeding $100,000 or imprisonment up to 7 years, or both [§5(3)(a)]...",
  "sources":     [{"document_name": "smuggling_of_migrants_act_2025.md",
                   "collection": "CARTA mcp-test",
                   "chunks": ["..."], "relevance_scores": [0.31]}],
  "citations":   ["§5(3)(a)", "§5(3)(b)", "§5(5)", "§6(1)"],
  "grounded":    true,
  "disclaimer":  "For specific legal matters, please consult with a qualified Bahamian attorney or contact the Bahamas Bar Association."
}
```

| Field | Meaning |
|---|---|
| `answer` | Grounded legal answer with inline `[§x(y)]` citations, or a decline |
| `sources` | Retrieved statute chunks + the KB collection name |
| `citations` | Extracted bracketed references |
| `grounded` | `true` if the answer is based on retrieved docs; `false` if the KB had nothing relevant |
| `disclaimer` | Standard legal disclaimer — always present |

**Error envelope** (non-2xx):
```json
{"answer": null, "sources": [], "citations": [], "grounded": false,
 "disclaimer": "...", "error": "<kind>", "message": "<detail>"}
```

---

## 3. Test cases

| # | Query | Expected |
|---|---|---|
| 1 | `What are the penalties for smuggling migrants in The Bahamas?` | `grounded: true`; citations incl. `§5(3)(a)`; mentions `$100,000` and `7 years` |
| 2 | `What is the jurisdiction of the Smuggling of Migrants Act?` | `grounded: true`; cites `§4` |
| 3 | `What is the US federal corporate tax rate?` | `grounded: false` (clean decline — off-scope) |
| 4 | (omit / wrong `Authorization`) | `401` |
| 5 | > 100 requests in an hour | `429` with a `Retry-After` header |
| 6 | `query` > 2000 chars | `400`, `error: "query_too_long"` |

### Quick curl (Option A)
```bash
curl -s -X POST https://lawbey-mcp.fly.dev/debug/query \
  -H "Authorization: Bearer carta:<PARTNER_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"query":"What are the penalties for smuggling migrants in The Bahamas?"}'
```

---

## 4. Behavior notes

- **Latency:** ~7–19s per query. The server retries the upstream retrieval on a
  decline (upstream RAG is non-deterministic) and returns the first grounded
  result — ~96% of queries come back grounded on the first response.
- **`grounded: false`** means the knowledge base has no relevant content
  (off-topic, or the topic isn't in the library yet). Treat it as "no answer",
  not an error. On the rare occasion a relevant query still returns
  `grounded: false` (~4%, all retries missed), just retry the same query.
- **Determinism:** `temperature=0`, so the answer is deterministic for a given
  retrieval. Identical queries may differ slightly across calls because
  retrieval varies upstream.
- **Scope:** the CARTA knowledge base currently contains the **Smuggling of
  Migrants Act 2025** only. More statutes can be added to the same knowledge
  base with **no MCP code change** — the server is scoped to that KB.
- **Rate limits:** 100 / hour and 1000 / day per partner key (in-memory; resets
  on redeploy).

---

## 5. Security notes for Carta

- Send the partner key only via the `Authorization` header. Never in URLs.
- The upstream LawBey API key is held server-side only and is never
  returned to callers.
- `context` is optional and must not contain PII — it is forwarded to the
  upstream model.
- All traffic is HTTPS (Fly.io forces HTTPS).

---

## 6. Support / escalation

- Outage / `5xx`: check `GET /health` first; contact LawBey ops.
- Wrong answers / `grounded: false` on clearly in-scope questions: retry once,
  then report the query + the `citations` returned.
- Adding statutes to the KB: LawBey ops task (no Carta change needed).
