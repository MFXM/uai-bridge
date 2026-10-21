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
7. [Endpoints](#endpoints)
8. [Tailored AIs and knowledge bases](#tailored-ais-and-knowledge-bases)
9. [Configuration reference](#configuration-reference)
10. [Security](#security)
11. [Several users](#several-users)
12. [Limitations of the u:ai API](#limitations-of-the-uai-api)
13. [Development and tests](#development-and-tests)
14. [Troubleshooting](#troubleshooting)
15. [Related projects](#related-projects)
16. [Acknowledgements](#acknowledgements)

---

## Features
- **OpenAI-compatible:** `GET /v1/models`, `POST /v1/chat/completions`, including `stream: true`
  (emulated) and **tool calling** (emulated, since u:ai has no native tool calls).
- **Ollama-compatible:** `GET /api/tags`, `POST /api/chat`, `GET /api/version`, for tools that only support Ollama-style backends.
- **Tailored AI chat:** use `model: "tailored:<id>"` to chat with a Tailored AI and its knowledge base.
  Answers include the sources.
- **Optional knowledge-base management:** create, upload, ingest and link a Tailored AI in one call.
  Uploads only.
- **Secure by default:** API key required, binds to `127.0.0.1`, no filesystem access, size limits,
  bounded retries, client-side rate limiting, no prompt logging, non-root read-only container.
- **One file** (`uai_bridge.py`), four dependencies, tested: 43 unit tests against a mocked API plus a
  28-check live test against the real u:ai API.

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

## Endpoints
| Method & path | Auth | Purpose |
|---|---|---|
| `GET /health` | no | liveness check, returns version |
| `GET /v1/models` | yes | models available to your API client |
| `POST /v1/chat/completions` | yes | OpenAI chat (tools, `stream`, `response_format`, …) |
| `GET /v1/pricing` | yes | price per 1M tokens in USD (u:ai reports USD per 1 000 tokens) |
| `GET /api/tags`, `POST /api/chat`, `GET /api/version` | yes (`/api/version` no) | Ollama compatibility |
| `/v1/uai/...` | yes | knowledge bases and Tailored AIs, only with `BRIDGE_ENABLE_KB=true` |

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
| `BRIDGE_GEMINI_MIN_MAX_TOKENS` | `1024` | lower bound for `max_tokens` on Gemini models (0 = off) |
| `BRIDGE_COST_UNIT_TOKENS` | `1000` | number of tokens the u:ai `cost` values refer to (for `/v1/pricing`) |
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
  bounded, and a client-side rate limiter keeps you under the API limit.
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
  object `{"tool_calls": [...]}`. Only a reply that is *entirely* such an object naming an offered tool
  becomes a tool call. Reliability depends on the model (gpt-4o / gpt-5 work best).
- **JSON mode is not enforced upstream:** u:ai accepts `responseFormat: json_object`, but models still
  answer e.g. "Here is the JSON: \`\`\`json {…} \`\`\`". The bridge adds an instruction and extracts the JSON
  object (`BRIDGE_ENFORCE_JSON`).
- **Tailored-AI replies have another shape:** `data.message.content` instead of `data.content`, and
  no token usage. The bridge handles both shapes; `usage` is reported as 0 for Tailored-AI chats.
- **Web-search models** (`sonar-*`) return website citations; the bridge appends them with their URLs.
- **Images:** not documented; disabled by default (`BRIDGE_IMAGE_MODE`).
- **Stateless:** conversation history must be sent with each request (clients do this anyway).
- **Prices:** the API documentation says "USD per token", but the live values are USD per 1 000 tokens
  (e.g. `gpt-5` = 0.00138, i.e. 1.38 USD per 1M input tokens). `/v1/pricing` converts to per 1M tokens.
- **Error details** are in `meta.error.message` of u:ai's error response; the bridge forwards that text.

## Development and tests
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q          # 43 tests against a mocked u:ai API, no network or credits needed
```

**End-to-end check against the real API** (uses a few cents of your credit; creates and deletes a tiny
Tailored AI with `--kb`):
```bash
BRIDGE_URL=http://127.0.0.1:8000 BRIDGE_KEY=<bridge key> python tests/live_check.py --kb
# in Docker:  docker exec -e BRIDGE_KEY=<key> uai-bridge python /app/live_check.py --kb
#   (copy tests/live_check.py into the container first, or run it from the host)
```
Last result (1.0.0, 21 Oct 2026, arm64 Linux host): 28/28 checks passed. The checks cover auth,
models, chat, system role and history, reasoning models, JSON mode, tool calling round trip,
streaming, error mapping, Ollama endpoints, Mistral/Gemini/Claude/Sonar models, pricing, and
Tailored AI creation, retrieval with citations, and cleanup.
The tests cover authentication, the absence of any server-file-path endpoint, message and parameter translation,
tool-call parsing, error mapping and retry rules, streaming format, citations, pricing, Ollama
compatibility and the knowledge-base workflow including cleanup.

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
| Tool calls come back as plain text | the model did not follow the JSON format; try `tool_choice: "required"` or a stronger model |

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
