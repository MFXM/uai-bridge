# u:ai bridge

An OpenAI- and Ollama-compatible gateway for the **University of Vienna u:ai API**
(Academic AI platform, API documentation v1.0.7). It lets any tool or library that speaks the OpenAI
or Ollama API use your u:ai API access, including Tailored AIs with knowledge
bases (RAG).

```
 your tools ──(OpenAI / Ollama API + bridge key)──▶ uai-bridge ──(X-Client-ID / X-Client-Secret, HTTPS)──▶ univie-api.academic-ai.at
```

> **Community project, not an official ZID service.** It is maintained independently. The ZID does not provide support for it. It is maintained by a single
> person in their spare time, so **support is provided only on a very limited, best-effort basis**, with no
> guaranteed response times or fixes. Your use of the u:ai API is governed by the u:ai terms of use and the
> conditions of your API account.

---

## Contents
1. [Features](#features)
2. [Before you start: u:ai API access](#before-you-start-uai-api-access)
3. [Quick start with Docker](#quick-start-with-docker)
4. [Quick start without Docker](#quick-start-without-docker)
5. [Adding the bridge to an existing Docker Compose stack](#adding-the-bridge-to-an-existing-docker-compose-stack)
6. [Connecting clients](#connecting-clients)
7. [MCP endpoint for AI agents](#mcp-endpoint-for-ai-agents)
8. [Endpoints](#endpoints)
9. [Tailored AIs and knowledge bases](#tailored-ais-and-knowledge-bases)
10. [Monthly usage](#monthly-usage)
11. [Configuration reference](#configuration-reference)
12. [Security](#security)
13. [Several users](#several-users)
14. [Limitations of the u:ai API](#limitations-of-the-uai-api)
15. [Development and tests](#development-and-tests)
16. [Troubleshooting](#troubleshooting)
17. [Related projects](#related-projects)
18. [Acknowledgements](#acknowledgements)

---

## Features
- **OpenAI-compatible:** `GET /v1/models`, `POST /v1/chat/completions`, including `stream: true`
  (emulated) and **tool calling** (emulated, since u:ai has no native tool calls).
- **Ollama-compatible:** `GET /api/tags`, `POST /api/chat`, `GET /api/version`, for tools that only support Ollama-style backends.
- **Tailored AI chat:** use `model: "tailored:<id>"` to chat with a Tailored AI and its knowledge base.
  Answers include the sources.
- **Optional knowledge-base management:** create, upload, ingest and link a Tailored AI in one call.
  Uploads only.
- **Monthly usage counter:** `GET /v1/usage` shows requests, tokens and the estimated cost of the
  current month, so you can see how much of your allowance is left.
- **Optional MCP endpoint:** `/mcp` (Model Context Protocol) lets AI agents and assistants that support
  MCP use u:ai models as tools: ask another model for a second opinion, research the web with sources,
  check models, prices and usage. Off by default (`BRIDGE_ENABLE_MCP=true`).
- **Secure by default:** API key required, binds to `127.0.0.1`, no filesystem access, size limits,
  bounded retries, client-side rate limiting, no prompt logging, non-root read-only container.
- **One file** (`uai_bridge.py`), six dependencies (the MCP SDK is only loaded when the endpoint is
  enabled), tested: 69 unit tests against a mocked API plus a live test against the real u:ai API.

## Before you start: u:ai API access
1. **Request an API account** through the ZID Servicedesk; see
   <https://zid.univie.ac.at/en/uai/uai-api-access>. You receive a **Client ID** and a **Client Secret**.
   The API account is separate from your personal u:ai login. For employees it is assigned to their
   organisational unit.
2. Check the permissions (role) of your API client:
   - `USE_API_LLM`: normal chat.
   - `USE_API_TAILORED_AI`: chat **plus** Tailored AIs and knowledge bases. You need this role for
     `BRIDGE_ENABLE_KB=true`.
3. Note your **rate limit** and **cost limit**. They are set per API client. Community projects report
   100 requests/minute and a monthly budget of about 20 EUR for University of Vienna API clients. Ask
   the ZID for your actual values. Set `BRIDGE_RATE_LIMIT_RPM` a little below your rate limit.
4. The API is only reachable from the EU/EFTA.

> Keep the Client Secret like a password. Never commit it, paste it into chats, or put it into
> `docker-compose.yml` directly. Use an `.env` file that is excluded from Git (already in `.gitignore`).

## Quick start with Docker
Requirements: Docker with the Compose plugin (`docker compose version`). Works on amd64 and arm64.

There are two ways to run the bridge. Both use the same `compose.yml` and `.env`.

| | Option A: build from source **(recommended)** | Option B: prebuilt image |
|---|---|---|
| What runs | an image you build yourself from code you can read | an image built by this repository's GitHub Actions |
| Trust | you trust the source code, the pinned PyPI packages and the official Python base image | you additionally trust the maintainer's GitHub account and CI |
| Effort | `git clone` + one build (about a minute, nothing to compile) | download two files, no build |

The bridge holds your u:ai secret, so building it yourself is the safer choice. It is recommended, and it takes hardly longer.

### Option A: build from source (recommended)
```bash
git clone https://github.com/MFXM/uai-bridge.git
cd uai-bridge
cp .env.example .env
cp compose.example.yml compose.yml
# in compose.yml, uncomment the line "build: ."

# 1) generate a bridge API key (this is what your tools will use, NOT the u:ai secret)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
#    (no Python? use:  openssl rand -base64 32)

# 2) edit .env: set UAI_CLIENT_ID, UAI_CLIENT_SECRET and BRIDGE_API_KEYS=<key from step 1>
nano .env
chmod 600 .env

# 3) build and start
docker compose up -d --build
docker compose ps            # STATUS should become "healthy"
docker compose logs -f       # Ctrl+C to leave
```
Optionally check what you are running first: `git log`, read `uai_bridge.py`, and for releases
`git tag -v v1.0.0` if the tag is signed.

Update: `git pull && docker compose up -d --build`.

### Option B: prebuilt image (quickest)
Every release is published as a ready-to-run image for amd64 and arm64:
`ghcr.io/mfxm/uai-bridge`. You only need two files, no Git checkout:

```bash
mkdir uai-bridge && cd uai-bridge
curl -fsSL https://raw.githubusercontent.com/MFXM/uai-bridge/main/compose.example.yml -o compose.yml
curl -fsSL https://raw.githubusercontent.com/MFXM/uai-bridge/main/.env.example -o .env
nano .env && chmod 600 .env        # as in option A, steps 1-2
docker compose up -d
```

Update to a new release: `docker compose pull && docker compose up -d`.
The tag `:1` always points to the newest 1.x release (and is rebuilt monthly with a patched base
image). Pin an exact version such as `:1.0.0` in `compose.yml` if you prefer to update manually.

### Test it
```bash
export BRIDGE_KEY=<your bridge key>
curl -s http://127.0.0.1:8000/health
curl -s -H "Authorization: Bearer $BRIDGE_KEY" http://127.0.0.1:8000/v1/models
curl -s -H "Authorization: Bearer $BRIDGE_KEY" -H "Content-Type: application/json" \
     http://127.0.0.1:8000/v1/chat/completions \
     -d '{"model":"gpt-5-mini","messages":[{"role":"user","content":"Hello!"}],"max_tokens":50}'
```

## Quick start without Docker
Requirements: Python 3.10 or newer.

```bash
git clone https://github.com/MFXM/uai-bridge.git && cd uai-bridge
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env && nano .env                      # fill in as above
set -a && source .env && set +a                        # load variables (bash/zsh)
python uai_bridge.py                                    # listens on 127.0.0.1:8000
```
On Windows PowerShell, set variables with `$env:UAI_CLIENT_ID="..."` etc. before `python uai_bridge.py`.

To run it permanently on Linux, use a systemd user service:
```ini
# ~/.config/systemd/user/uai-bridge.service
[Unit]
Description=u:ai bridge
After=network-online.target

[Service]
WorkingDirectory=%h/uai-bridge
EnvironmentFile=%h/uai-bridge/.env
ExecStart=%h/uai-bridge/.venv/bin/python uai_bridge.py
Restart=on-failure

[Install]
WantedBy=default.target
```
```bash
systemctl --user daemon-reload && systemctl --user enable --now uai-bridge
loginctl enable-linger "$USER"      # keep running after logout
```

## Adding the bridge to an existing Docker Compose stack
If other services already run in a Compose project, add the bridge as one more
service. Other containers reach it by its service name, so it does not need a published port at all.

1. Copy `uai_bridge.py`, `requirements.txt` and `Dockerfile` into a folder of your stack, e.g.
   `./services/uai-bridge/`.
2. Put the variables from `.env.example` (at least `UAI_CLIENT_ID`, `UAI_CLIENT_SECRET`,
   `BRIDGE_API_KEYS`) into an env file of the stack that is excluded from Git, e.g. `.env.uai`.
3. Add the service:
   ```yaml
   services:
     uai-bridge:
       build:
         context: ./services/uai-bridge
       # or skip building and use the prebuilt image:  image: ghcr.io/mfxm/uai-bridge:1
       container_name: uai-bridge
       restart: unless-stopped
       env_file: .env.uai
       # No "ports:" - only containers on the same networks can reach the bridge.
       # To use it from the Docker host as well:  ports: ["127.0.0.1:8000:8000"]
       networks: [backend, internet]   # use your stack's network names
       read_only: true
       tmpfs: [/tmp]
       volumes: [uai-bridge-data:/data]   # monthly usage counter; declare it under top-level "volumes:"
       cap_drop: [ALL]
       security_opt: ["no-new-privileges:true"]
       mem_limit: 256m
   ```
4. `docker compose up -d --build uai-bridge`, then check `docker compose ps uai-bridge` for "healthy".
5. Point the other services at `http://uai-bridge:8000/v1` (OpenAI style) or `http://uai-bridge:8000`
   (Ollama style) and give them the bridge key (see [Connecting clients](#connecting-clients)).

If a client cannot send an API key (some Ollama-style integrations have no API-key field), use its
OpenAI-compatible option instead, or allow the internal network without a key. Find the network's subnet
with `docker network inspect <project>_<network> --format '{{(index .IPAM.Config 0).Subnet}}'` and set
`BRIDGE_TRUSTED_CIDRS=<subnet>`. **Only do this if the bridge has no published port.** Otherwise
forwarded traffic can appear to come from that network's gateway address.

## Connecting clients
Any tool or library that can talk to an OpenAI-compatible or Ollama-compatible server can use the bridge.
It needs three settings:

| Setting | Value |
|---|---|
| Base URL (OpenAI-style clients) | `http://<host>:8000/v1` |
| Server URL (Ollama-style clients) | `http://<host>:8000` |
| API key | your bridge key (sent as `Authorization: Bearer <key>` or `X-API-Key: <key>`) |

`<host>` is `127.0.0.1` on the same machine, or the service name (e.g. `uai-bridge`) for other containers
in the same Docker network. Model names are the u:ai model names available to your API client; list them
with `GET /v1/models` (for example `gpt-4o`, `gpt-5`, `gpt-5-mini`). Tailored AIs are addressed as
`tailored:<id>`.

Many OpenAI-compatible tools read the environment variables `OPENAI_BASE_URL` (in some tools
`OPENAI_API_BASE`) and `OPENAI_API_KEY`, so this is often enough:
```bash
export OPENAI_BASE_URL=http://127.0.0.1:8000/v1
export OPENAI_API_KEY=<bridge key>
```

**OpenAI Python SDK**
```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="<bridge key>")
r = client.chat.completions.create(model="gpt-5-mini",
                                   messages=[{"role": "user", "content": "Hello"}])
print(r.choices[0].message.content)
```
(see `examples/openai_sdk.py`)

**curl / any HTTP client**
```bash
curl -H "Authorization: Bearer <bridge key>" -H "Content-Type: application/json" \
     http://127.0.0.1:8000/v1/chat/completions \
     -d '{"model":"gpt-5-mini","messages":[{"role":"user","content":"Hello"}]}'
```

Tools that support function/tool calling work through the bridge's emulated tool calls (see
[Limitations](#limitations-of-the-uai-api)).

## MCP endpoint for AI agents
The bridge can also act as an [MCP](https://modelcontextprotocol.io) server. The REST endpoints above
are for programs you configure yourself; the MCP endpoint is for **AI agents** (desktop assistants,
coding agents, agent frameworks) that discover tools at runtime and decide themselves when to call
them. The agent's own model stays in charge, and u:ai models become tools it can consult.

Enable it in `.env` and restart:
```bash
BRIDGE_ENABLE_MCP=true
```
The endpoint is `http://<host>:8000/mcp` (Streamable HTTP, stateless, JSON responses). It uses the same
bridge key as the REST API (`Authorization: Bearer <key>`), and in `passthrough` mode the caller's own
u:ai credentials. It speaks the current stateless MCP revision (2026-07-28) and the older
handshake-based revisions (2024-11-05 to 2025-11-25) on the same URL, so older and newer clients both work.

**Tools**

| Tool | Billed | Purpose |
|---|---|---|
| `ask` | yes | answer from a u:ai model (`prompt`, optional `model`, `system`, `max_tokens`, `temperature`, `reasoning_effort`); `model: "tailored:<id>"` asks a Tailored AI |
| `web_research` | yes | web research with a `sonar-*` model (default `BRIDGE_MCP_RESEARCH_MODEL`); returns the answer and its sources with URLs |
| `list_models` | no | available models with context window, output limit and prices |
| `usage` | no | estimated cost of a month (as `GET /v1/usage`) plus today's MCP calls and the daily cap; only with usage tracking on |

Every tool result has the answer as text (what the agent reads) and the details (model, finish reason,
token usage, citations) as structured content. Upstream errors come back as tool errors with the u:ai
message, so the agent can react instead of failing.

**Guard rails.** An agent can call a tool far more often than a person would, so the billed tools have
extra limits on top of the bridge's rate limit:
- `BRIDGE_MCP_MAX_CALLS_PER_DAY` (default 1000) caps `ask` and `web_research` calls per u:ai client and
  day (days follow `BRIDGE_USAGE_TZ`; `0` = no cap). The counter is kept in memory and resets on restart.
  Set it to what fits your own allowance.
- `BRIDGE_MCP_MAX_TOKENS` (default 16000) is the default answer length and the largest `max_tokens` a
  tool call may request. It is lowered automatically to the model's own output limit.
- Requests that carry a browser `Origin` header are rejected unless the origin is listed in
  `BRIDGE_MCP_ALLOWED_ORIGINS` (protection against DNS rebinding, as the MCP specification requires).
  Desktop and command-line clients send no `Origin` and are not affected.

**Connecting a client.** Most MCP clients take a server URL and a header. A typical JSON configuration:
```json
{
  "mcpServers": {
    "uai": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": { "Authorization": "Bearer <bridge key>" }
    }
  }
}
```
The exact keys differ between clients; look for "Streamable HTTP" or "HTTP" MCP servers in your
client's documentation. Clients that only support local (stdio) servers can reach the endpoint
through an MCP proxy that forwards stdio to Streamable HTTP. A quick check with curl:
```bash
curl -s http://127.0.0.1:8000/mcp -H "Authorization: Bearer <bridge key>" \
     -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
     -H "MCP-Protocol-Version: 2026-07-28" -H "Mcp-Method: tools/list" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

Notes:
- Remote MCP clients that run in the cloud (not on your machine) need a public HTTPS URL. Do not
  publish the bridge for this without a TLS reverse proxy and a strong key, and keep in mind that the
  calls are billed to your u:ai account.
- The MCP SDK adds about 15 MB to the Docker image. It is imported only when `BRIDGE_ENABLE_MCP=true`,
  so it costs nothing at runtime otherwise.

## Endpoints
| Method & path | Auth | Purpose |
|---|---|---|
| `GET /health` | no | liveness check, returns version |
| `GET /v1/models` | yes | models available to your API client |
| `POST /v1/chat/completions` | yes | OpenAI chat (tools, `stream`, `response_format`, …) |
| `GET /v1/pricing` | yes | price per 1M tokens in USD (u:ai reports USD per 1 000 tokens), plus the per-request fee where a model has one |
| `GET /v1/usage`, `GET /v1/usage/history` | yes | requests, tokens and estimated cost per month (see [Monthly usage](#monthly-usage)) |
| `GET /api/tags`, `POST /api/chat`, `GET /api/version` | yes (`/api/version` no) | Ollama compatibility |
| `/v1/uai/...` | yes | knowledge bases and Tailored AIs, only with `BRIDGE_ENABLE_KB=true` |
| `POST /mcp` | yes | MCP endpoint for AI agents, only with `BRIDGE_ENABLE_MCP=true` (see [MCP endpoint](#mcp-endpoint-for-ai-agents)) |

Supported chat parameters: `temperature`, `max_tokens` / `max_completion_tokens` (mapped to
`maxTokens` or, for o-series and gpt-5 models, `maxCompletionTokens`), `frequency_penalty`,
`presence_penalty`, `stop` (Mistral only, according to the API docs), `seed`, `response_format`
(`text` / `json_object`; `json_schema` is downgraded to `json_object`), `reasoning_effort`, `verbosity`,
`tools`, `tool_choice`, `stream`, `stream_options.include_usage`.
Other parameters (e.g. `top_p`) are ignored and listed in the response header `X-Bridge-Ignored-Params`.
`n > 1` is rejected. Model-specific workarounds are reported in `X-Bridge-Adjusted-Params`:
- Gemini models fail upstream with very small token limits, so `max_tokens` below
  `BRIDGE_GEMINI_MIN_MAX_TOKENS` (default 1024) is raised to it. You pay only for tokens actually generated.
- If u:ai rejects a single parameter for a model (e.g. `claude-opus-4-6` rejects `temperature`), the
  bridge retries once without it. A rejected request was not processed, so this cannot double-bill.
- u:ai accepts `reasoning_effort` `low`, `medium` and `high`. Agent frameworks also send the wider
  OpenAI-compatible values, which are mapped: `minimal` → `low`, `xhigh` and `max` → `high`, and
  `none` / `default` leave the field out (the model's default applies), e.g.
  `X-Bridge-Adjusted-Params: reasoning_effort:xhigh->high`. Other values are rejected with 400.

Errors use the OpenAI shape `{"error": {"message", "type", "code"}}`. u:ai status codes are mapped:
400/422 → 400, 404 → 404, 429 → 429 (with `Retry-After`), knowledge base not ready → 409,
timeouts → 504, other upstream failures → 502. Bridge credentials rejected by u:ai → 502
(`upstream_auth_error`).

## Tailored AIs and knowledge bases
Set `BRIDGE_ENABLE_KB=true` (requires the `USE_API_TAILORED_AI` role). Then:

**One call: documents → ready-to-use expert**
```bash
curl -H "Authorization: Bearer $BRIDGE_KEY" http://127.0.0.1:8000/v1/uai/experts \
  -F name="Course helper" -F summary="Answers questions about the course notes" \
  -F system_prompt="Answer only from the provided documents and cite them." \
  -F files=@notes.pdf -F files=@slides.pdf
# {"status":"ready","tailored_ai_id":"…","knowledge_base_id":"…","model":"tailored:…"}
```
The bridge creates the knowledge base, uploads the files, starts ingestion, waits until the knowledge
base is `ready` (up to `BRIDGE_INGEST_TIMEOUT_S`), creates the Tailored AI and links them. If a step
fails, it deletes the half-created resources. If ingestion takes longer than the timeout, the
knowledge base is kept and the error contains its id. Finish later with
`POST /v1/uai/tailored-ais` (body: `name`, `summary`, `system_prompt`, `knowledge_base_id`).

Limits from the API: name ≤ 50 characters, summary ≤ 100, system prompt ≤ 2000, files ≤ 50 MB,
one knowledge base per Tailored AI. **Deleting a Tailored AI also deletes its linked knowledge base**
(observed on the live API).

**Chat with it:** `"model": "tailored:<id>"` (base model `BRIDGE_TAILORED_BASE_MODEL`) or
`"model": "tailored:<id>@gpt-5"`. The Tailored AI's own system prompt is always applied by u:ai.
Sources are appended as `**Sources:**` (disable with `BRIDGE_APPEND_CITATIONS=false`) and returned in
the extra `citations` field.

**Management endpoints**
| Method & path | Purpose |
|---|---|
| `GET /v1/uai/knowledge-bases` | list |
| `POST /v1/uai/knowledge-bases` | create (`{"name": "..."}`, 3–50 characters) |
| `GET /v1/uai/knowledge-bases/{id}` / `.../{id}/status` | details / ingestion state |
| `POST /v1/uai/knowledge-bases/{id}/documents` | upload `files` (multipart) and re-ingest |
| `DELETE /v1/uai/knowledge-bases/{id}/documents/{doc}` | delete a document and re-ingest |
| `DELETE /v1/uai/knowledge-bases/{id}` | delete knowledge base |
| `GET /v1/uai/tailored-ais` · `POST /v1/uai/tailored-ais` | list · create (optionally linked) |
| `POST /v1/uai/tailored-ais/{id}/knowledge-base` | link (`{"knowledge_base_id": "..."}`), or unlink with `null` |
| `DELETE /v1/uai/tailored-ais/{id}` | delete Tailored AI |

Allowed file types: `BRIDGE_UPLOAD_EXTENSIONS` (default
pdf, doc, docx, txt, md, csv, html, htm, rtf, odt, pptx, xlsx, json).

## Monthly usage
The bridge counts what goes through it per calendar month: requests, prompt/completion tokens and an
**estimated** cost in USD. Use it to see how much of your monthly allowance you have already used. The
allowance itself is not configured in the bridge, because it differs between accounts and can change.

```bash
curl -s -H "Authorization: Bearer $BRIDGE_KEY" http://127.0.0.1:8000/v1/usage            # this month
curl -s -H "Authorization: Bearer $BRIDGE_KEY" "http://127.0.0.1:8000/v1/usage?month=2026-09"
curl -s -H "Authorization: Bearer $BRIDGE_KEY" http://127.0.0.1:8000/v1/usage/history    # one line per month
```
Example response (shortened, numbers made up):
```json
{"month": "2026-10", "timezone": "UTC", "period_start": "2026-10-01T00:00:00+00:00",
 "period_end": "2026-11-01T00:00:00+00:00", "currency": "USD",
 "requests": 412, "prompt_tokens": 903311, "completion_tokens": 120877, "total_tokens": 1024188,
 "estimated_cost_usd": 3.218804, "requests_without_usage": 7, "requests_without_price": 0,
 "month_elapsed_fraction": 0.8312, "projected_month_cost_usd": 3.872478, "projected_month_tokens": 1232180,
 "by_model": {"gpt-5": {"requests": 120, "total_tokens": 610233, "estimated_cost_usd": 2.41, "...": "..."}},
 "first_request": "...", "last_request": "...", "persistent": true}
```
- **Why an estimate:** u:ai replies contain token counts but no cost, and the endpoint that reports an
  account's spend (`/api/v1/cost`) is restricted to platform administrators.
- **Cost** = prompt tokens × input price + completion tokens × output price (+ the flat per-request fee of
  web-search models such as `sonar-pro`), using the u:ai price list (`/v1/pricing`) at the time of each
  request. Prices are cached for `BRIDGE_MODELS_CACHE_S`.
- `projected_month_*` extrapolates the month so far linearly to its end (shown from the second day on).
- **Not counted:** Tailored-AI replies carry no token usage upstream; they appear only as
  `requests_without_usage`. Knowledge-base ingestion and anything you do *outside* this bridge (other
  tools, the u:ai web interface) are not seen either. `requests_without_price` counts replies for
  models without a listed price. Treat the figures as a lower bound, not an official statement.
- **Months** start and end in `BRIDGE_USAGE_TZ` (default `UTC`, e.g. `Europe/Vienna`).
- **Storage:** the Docker image keeps the counter in `/data/usage.json`. `compose.example.yml`
  mounts a named volume there, so it survives updates; `"persistent": false` means the file could
  not be written and the counter resets on restart. Outside Docker set `BRIDGE_USAGE_FILE` to a path.
  The file holds only counts, model names and a hash of the u:ai client ID, no prompts or answers.
- In `passthrough` mode each caller sees only the usage of their own u:ai client.
- `BRIDGE_USAGE_TRACKING=false` switches the counter and `/v1/usage` off.

## Configuration reference
All settings are environment variables; see `.env.example` for a commented template.

| Variable | Default | Meaning |
|---|---|---|
| `UAI_CLIENT_ID`, `UAI_CLIENT_SECRET` | – | u:ai API credentials (aliases: `CLIENT_ID`/`CLIENT_SECRET`, as in the ZID example code) |
| `UAI_BASE_URL` | `https://univie-api.academic-ai.at` | u:ai endpoint (must be https) |
| `BRIDGE_API_KEYS` | – (required) | comma-separated bridge keys, each ≥ 24 characters |
| `BRIDGE_CREDENTIAL_MODE` | `static` | `static` or `passthrough` (see [Several users](#several-users)) |
| `BRIDGE_TRUSTED_CIDRS` | – | networks allowed without a key (only with an unpublished port) |
| `BRIDGE_ALLOW_NO_AUTH` | `false` | disable authentication completely |
| `BRIDGE_HOST` / `BRIDGE_PORT` | `127.0.0.1` / `8000` | listen address (the Docker image uses `0.0.0.0` inside the container) |
| `BRIDGE_ENABLE_KB` | `false` | enable `/v1/uai/*` |
| `BRIDGE_ENABLE_DOCS` | `false` | Swagger UI at `/docs` |
| `BRIDGE_IMAGE_MODE` | `placeholder` | `placeholder` replaces images by a note; `forward` sends `image_url` parts (undocumented upstream) |
| `BRIDGE_APPEND_CITATIONS` | `true` | append sources to Tailored AI answers |
| `BRIDGE_DEFAULT_MODEL` | `gpt-4o` | used when a request has no `model` |
| `BRIDGE_TAILORED_BASE_MODEL` | `gpt-4o` | model for `tailored:<id>` |
| `BRIDGE_UPSTREAM_TIMEOUT_S` | `300` | per-request timeout towards u:ai |
| `BRIDGE_MAX_RETRIES` | `2` | retries for connection errors and 429/502/503/504 (0–5) |
| `BRIDGE_MAX_CONCURRENCY` | `8` | parallel upstream requests |
| `BRIDGE_RATE_LIMIT_RPM` | `90` | client-side requests/minute per u:ai client (0 = off) |
| `BRIDGE_MAX_REQUEST_MB` | `10` | max JSON body size |
| `BRIDGE_MAX_UPLOAD_MB` | `50` | max file size |
| `BRIDGE_UPLOAD_EXTENSIONS` | see above | allowed upload types |
| `BRIDGE_INGEST_TIMEOUT_S` / `BRIDGE_INGEST_POLL_S` | `600` / `5` | waiting for knowledge-base ingestion |
| `BRIDGE_MODELS_CACHE_S` | `300` | cache for the model list |
| `BRIDGE_ENFORCE_JSON` | `true` | with `response_format: json_object`, instruct the model and extract the JSON from the reply |
| `BRIDGE_TOOL_REPAIR_RETRIES` | `1` | extra (billed) requests when an emulated tool call is unusable or a required call is missing (0-2, 0 = off) |
| `BRIDGE_GEMINI_MIN_MAX_TOKENS` | `1024` | lower bound for `max_tokens` on Gemini models (0 = off) |
| `BRIDGE_COST_UNIT_TOKENS` | `1000` | number of tokens the u:ai `cost` values refer to (for `/v1/pricing`) |
| `BRIDGE_USAGE_TRACKING` | `true` | monthly usage counter and `/v1/usage` |
| `BRIDGE_USAGE_FILE` | – (Docker: `/data/usage.json`) | where the counter is stored; unset = in memory only |
| `BRIDGE_USAGE_TZ` | `UTC` | time zone in which months start (IANA name, e.g. `Europe/Vienna`) |
| `BRIDGE_ENABLE_MCP` | `false` | MCP endpoint at `/mcp` |
| `BRIDGE_MCP_MAX_CALLS_PER_DAY` | `1000` | billed MCP tool calls (`ask`, `web_research`) per u:ai client and day (0 = no cap) |
| `BRIDGE_MCP_MAX_TOKENS` | `16000` | default and maximum `max_tokens` of MCP tool calls (capped further at the model's output limit) |
| `BRIDGE_MCP_RESEARCH_MODEL` | `sonar-pro` | default model of the `web_research` tool |
| `BRIDGE_MCP_ALLOWED_ORIGINS` | – | comma-separated browser origins allowed to call `/mcp` (e.g. `https://chat.example.org`); requests with any other `Origin` get 403 |
| `BRIDGE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, … |

## Security
- **Authentication:** every endpoint except `/health` and `/api/version` needs a bridge key. Keys are
  compared in constant time. The bridge refuses to start without one (or without an explicit
  `BRIDGE_ALLOW_NO_AUTH=true`).
- **Exposure:** default bind `127.0.0.1`. In Docker, expose the port only on `127.0.0.1` or not at all.
  Use the bridge's built-in HTTP only on a trusted network. To reach it from other machines, put it
  behind a reverse proxy with TLS (Caddy, nginx, Traefik), or use a private VPN.
- **No filesystem access:** documents only arrive as uploads. File names are sanitised, types and sizes
  limited.
- **Secrets:** the u:ai secret is sent only to `UAI_BASE_URL` over HTTPS and never appears in responses
  or logs. Logs contain only method, path, status and duration, never prompts, answers or documents.
- **Cost protection:** read timeouts are never retried (the request may already be billed). Retries are
  bounded, and a client-side rate limiter keeps you under the API limit. The optional MCP endpoint adds a
  daily cap on billed tool calls and an upper bound on answer length.
- **MCP endpoint:** off by default; same key check as every other endpoint, browser `Origin`s rejected
  unless allowed, no tools that touch files, knowledge bases or settings.
- **Container:** unprivileged user, read-only filesystem, all capabilities dropped, memory limit.
- **Data isolation:** the u:ai API is stateless. All users of one bridge in `static` mode share one
  API client, and with it its usage, costs, knowledge bases and Tailored AIs. Conversations are not
  stored by the bridge.

See `SECURITY.md` for reporting vulnerabilities and rotating secrets.

## Several users
- **Recommended: one bridge per person or group**, each with its own u:ai API client. Costs and
  limits then stay with the right account.
- **Shared gateway with own credentials:** set `BRIDGE_CREDENTIAL_MODE=passthrough`. Each caller then
  sends their own u:ai credentials as `Authorization: Bearer <CLIENT_ID>:<CLIENT_SECRET>`, and the bridge
  holds no credentials itself. Because callers' secrets pass through the bridge, run it **only behind
  HTTPS** and only if the users trust the operator. Rate limiting and the model cache are kept per
  client ID.
- Do not share a single API client among many people without agreement from the ZID; it is assigned
  to a person or organisational unit.

## Limitations of the u:ai API
These come from the API (documentation v1.0.7 and community experience) and are worked around where
possible:
- **No streaming:** with `stream: true` the complete answer arrives as one SSE content chunk once it
  is ready.
- **Only `user` and `assistant` roles:** `system`/`developer` messages are sent as user messages
  starting with `### SYSTEM INSTRUCTION ###`; tool results start with `### TOOL RESULT [...] ###`;
  consecutive messages of the same role are merged.
- **No native tool calling:** tools are described in the prompt, and the model must answer with a JSON
  object `{"tool_calls": [...]}`. A reply that ends with such an object (optionally after a short
  sentence) and names offered tools becomes a tool call; JSON inside prose stays text. Common slips
  (single backslashes in Windows paths, `functions.` prefixes, several objects) are tolerated, and an
  unusable call is re-requested once (`BRIDGE_TOOL_REPAIR_RETRIES`). Reliability still depends on the
  model; the `X-Bridge-Tool-Calls` response header shows what happened.
- **JSON mode is not enforced upstream:** u:ai accepts `responseFormat: json_object`, but models still
  answer e.g. "Here is the JSON: \`\`\`json {…} \`\`\`". The bridge adds an instruction and extracts the JSON
  object (`BRIDGE_ENFORCE_JSON`).
- **Tailored-AI replies have another shape:** `data.message.content` instead of `data.content`, and
  no token usage. The bridge handles both shapes; `usage` is reported as 0 for Tailored-AI chats
  (and these chats are missing from the cost estimate of `/v1/usage`).
- **Web-search models** (`sonar-*`) return website citations; the bridge appends them with their URLs.
- **Images:** not documented; disabled by default (`BRIDGE_IMAGE_MODE`).
- **Stateless:** conversation history must be sent with each request (clients do this anyway).
- **Prices:** the API documentation says "USD per token", but the live values are USD per 1 000 tokens
  (e.g. `gpt-5` = 0.00138, i.e. 1.38 USD per 1M input tokens). `/v1/pricing` converts to per 1M tokens.
  Web-search models additionally list a `per_request` fee, which is USD per single request.
- **Error details** are in `meta.error.message` of u:ai's error response; the bridge forwards that text.

## Development and tests
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q          # 69 tests against a mocked u:ai API, no network or credits needed
```

**End-to-end check against the real API** (uses a few cents of your credit; creates and deletes a tiny
Tailored AI with `--kb`):
```bash
BRIDGE_URL=http://127.0.0.1:8000 BRIDGE_KEY=<bridge key> python tests/live_check.py --kb
# add --mcp to also check the MCP endpoint (needs BRIDGE_ENABLE_MCP=true; one short billed call)
# in Docker:  docker exec -e BRIDGE_KEY=<key> uai-bridge python /app/live_check.py --kb
#   (copy tests/live_check.py into the container first, or run it from the host)
```
Last result (1.0.0, 21 Oct 2026, arm64 Linux host): 28/28 checks passed. The checks cover auth,
models, chat, system role and history, reasoning models, JSON mode, tool calling round trip,
streaming, error mapping, Ollama endpoints, Mistral/Gemini/Claude/Sonar models, pricing, and
Tailored AI creation, retrieval with citations, and cleanup.
The tests cover authentication, the absence of any server-file-path endpoint, message and parameter translation,
tool-call parsing, error mapping and retry rules, streaming format, citations, pricing, Ollama
compatibility, the knowledge-base workflow including cleanup, the monthly usage counter, and the MCP
endpoint (both protocol revisions, auth and Origin checks, tools, daily cap, token limits, passthrough).

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| Container exits: `No bridge API key configured` | set `BRIDGE_API_KEYS` in the env file |
| Container exits: `Missing u:ai credentials` | set `UAI_CLIENT_ID` / `UAI_CLIENT_SECRET` |
| `401 authentication_error` | client sends no key or a wrong key (`Authorization: Bearer <key>`) |
| `502 upstream_auth_error` | u:ai rejected the bridge's credentials or role: check ID/secret, ask the ZID about the role |
| `403` from u:ai only for `/v1/uai/*` | your API client lacks `USE_API_TAILORED_AI` |
| `429 rate_limit_error` | rate or cost limit of your API client reached; lower `BRIDGE_RATE_LIMIT_RPM` or wait |
| `409 knowledge_base_unavailable` | knowledge base not yet `ready`; check `/v1/uai/knowledge-bases/{id}/status` |
| `504 upstream_timeout` | very long generation; raise `BRIDGE_UPSTREAM_TIMEOUT_S` |
| HTTP 403 "only possible from within EU and EFTA" | you are outside the EU/EFTA (e.g. a non-EU VPN exit) |
| A client shows an empty model list | the bridge key is missing in the client's settings, or the bridge can't reach u:ai (`docker compose logs uai-bridge`) |
| Tool calls come back as plain text | check the `X-Bridge-Tool-Calls` header: `failed` = the call stayed unusable after the repair request, `text` = the model answered without calling a tool. Try a stronger model or `tool_choice: "required"`; the bridge log names the reason (never the content) |

## Related projects
- ZID u:ai API access page and example code: <https://zid.univie.ac.at/en/uai/uai-api-access>
- `apiuniwien`: a lightweight OpenAI proxy for the u:ai API (University of Vienna, MIT):
  <https://gitlab.com/dietersuess/apiuniwien>
- Academic AI API demos (University of Innsbruck ZID, same platform):
  <https://git.uibk.ac.at/zid/dmlt/academicai/academicai-api-demos>

## Maintainer and support
This is a community project maintained by one individual. **Support is very limited and best-effort.**
Issues and pull requests are welcome and are looked at when time allows. There is no guarantee of a
response, a fix or continued maintenance. For anything time-critical, please do not rely on this project.

- Maintainer: **Martin F. X. Mauser** (GitHub: [@MFXM](https://github.com/MFXM))
- Bugs and feature requests: [GitHub issues](https://github.com/MFXM/uai-bridge/issues).
- Security problems: **not** as a public issue; see `SECURITY.md`.
- Questions about your API account, role, limits or costs: the ZID Servicedesk (this project cannot help there).

Contributions are welcome. Please run `pytest -q` before opening a pull/merge request, and never include
real credentials in issues, logs or test files.

## Acknowledgements
This project was developed with the help of AI coding assistants: **Anthropic's Claude Opus** and
**Google's Gemini** were used to generate large parts of the code, tests and documentation. The
maintainer directed the work and is responsible for the published code. As with any software, please
review it before you rely on it, and report problems through the issue tracker.

## License
MIT, see `LICENSE`.
