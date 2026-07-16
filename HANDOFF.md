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
  "sources":     [{"document_name": "smuggling_of_migrants_act_2025_part1.md",
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
| `grounded` | `true` if the answer is based on retrieved docs; `false` if declined / off-scope |
| `disclaimer` | Standard legal disclaimer — always present |

**Error envelope** (when the tool returns an error object, or non-2xx on `/debug/query` for some cases):
```json
{"answer": null, "sources": [], "citations": [], "grounded": false,
 "disclaimer": "...", "error": "<kind>", "message": "<detail>"}
```

Common `error` kinds: `empty_query`, `query_too_long`, `auth_error`,
`upstream_unavailable`, `upstream_error`, `rag_unavailable` (KB retrieval
returned no matching documents).

---

## 3. Test cases

| # | Query | Expected |
|---|---|---|
| 1 | `What are the penalties for smuggling migrants in The Bahamas?` | `grounded: true`; sources include `smuggling_of_migrants_act_2025_*.md`; cites penalties (e.g. §5) |
| 2 | `What are the requirements for incorporating an IBC in The Bahamas?` | `grounded: true`; sources include `ibc_act_*.md` |
| 3 | `What are the economic substance requirements under CESRA?` | `grounded: true`; sources include `cesra_2023_guidelines_*.md` |
| 4 | `What is the US federal corporate tax rate?` | `grounded: false` (clean decline — off-scope) |
| 5 | (omit / wrong `Authorization`) | `401` |
| 6 | > 100 requests in an hour | `429` with a `Retry-After` header |
| 7 | `query` > 2000 chars | `400`, `error: "query_too_long"` |

### Quick curl (Option A)
```bash
curl -s -X POST https://lawbey-mcp.fly.dev/debug/query \
  -H "Authorization: Bearer carta:<PARTNER_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"query":"What are the penalties for smuggling migrants in The Bahamas?"}'
```

---

## 4. Behavior notes

- **Latency:** typically **~30–60s** per query; complex questions can reach
  **~90–120s**. The MCP server pre-retrieves the most relevant statute files,
  then asks the model with only those files attached. Budget agent timeouts
  accordingly (recommend **≥150s** client timeout).
- **How retrieval works:** multi-query rewrite (acronym expand + keyword
  distill) → knowledge-base vector search → top file IDs passed into
  generation. If retrieval finds nothing, the tool returns
  `error: "rag_unavailable"` rather than an ungrounded guess.
- **Decline retry:** if the model declines (“context does not contain…”), the
  server retries once (`rag_max_attempts=2`). Further retries rarely help.
- **`grounded: false`** means the answer was a decline / off-scope, or no
  usable retrieved context. Treat it as “no answer”, not a transport error.
- **Determinism:** `temperature=0`. Identical queries may still differ slightly
  when upstream retrieval ranking varies.
- **Scope:** the CARTA knowledge base is a **multi-act Bahamian legal library**
  (IBC, Companies, CESRA, DARE, FCSP, FTRA, SIA, BTCRA, smuggling of migrants,
  and related guidance — on the order of ~140 files). New statutes can be added
  to the same KB with **no MCP code change**.
- **MCP vs LawBey web UI:** do **not** treat the Open WebUI chat UI as the
  source of truth for Carta. The UI often attaches the whole collection and can
  retrieve irrelevant chunks (decline with “7 sources” even when the KB has the
  right acts). **This MCP endpoint uses a different retrieval path** and is what
  Carta should integrate against.
- **Rate limits:** 100 / hour and 1000 / day per partner key (in-memory; resets
  on redeploy). Suitable for pilot / agent usage, not high-QPS blast traffic.

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
  then report the query + the `sources` / `citations` returned.
- `rag_unavailable`: KB index may be rebuilding — retry later; escalate if
  persistent.
- Adding statutes to the KB: LawBey ops task (no Carta change needed).
