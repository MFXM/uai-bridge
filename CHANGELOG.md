# Changelog

All notable changes are documented here. This project uses [Semantic Versioning](https://semver.org/).

## 1.1.0 (2026-10-27)
**Monthly usage counter**
- `GET /v1/usage` (optionally `?month=YYYY-MM`) and `GET /v1/usage/history`: requests, prompt/completion/total
  tokens and an estimated USD cost per calendar month and per model, plus a linear projection to the end of the
  current month. Costs are computed from the u:ai price list at request time, including the flat
  `per_request` fee of web-search models (`sonar-*`). u:ai itself reports no cost per reply, and its
  `/api/v1/cost` endpoint is admin-only.
- `GET /v1/pricing` also returns `usd_per_request` where a model has one.
- Tailored-AI replies (no usage upstream) and models without a price are counted separately
  (`requests_without_usage`, `requests_without_price`) instead of being guessed.
- Stored in `BRIDGE_USAGE_FILE` (Docker image: `/data/usage.json`, new `VOLUME /data`; `compose.example.yml`
  mounts a named volume), written atomically. Holds only counts, model names and a hash of the client ID.
  An unwritable or corrupt file never breaks requests.
- Months follow `BRIDGE_USAGE_TZ` (default `UTC`); new dependency `tzdata` so IANA zones work in slim images.
- Per u:ai client: in `passthrough` mode every caller sees only their own usage. `BRIDGE_USAGE_TRACKING=false`
  turns it off.
- 12 new unit tests (55 in total); `live_check.py` checks the counter (29 checks).

## 1.0.0 (2026-10-21)
First public release.

**Gateway**
- OpenAI-compatible `GET /v1/models` and `POST /v1/chat/completions`, with emulated streaming (standard SSE
  chunks, optional usage chunk) and emulated tool calling (several calls per reply, `tool_choice`,
  strict JSON parsing that only accepts replies naming offered tools).
- Ollama-compatible `GET /api/tags`, `POST /api/chat` (NDJSON streaming, `options.*`, tool calls) and `GET /api/version`.
- Tailored-AI chat via `model: "tailored:<id>"` / `"tailored:<id>@<model>"`, with document and web citations.
- Optional knowledge-base and Tailored-AI management under `/v1/uai/*` (`BRIDGE_ENABLE_KB`), including a one-call
  `POST /v1/uai/experts` that waits for ingestion and cleans up on failure.
- `GET /v1/pricing` in USD per 1M tokens.
- Two credential modes: `static` (bridge holds one API client) and `passthrough` (each caller sends their own).

**Security**
- Bridge API key required on every endpoint except `/health` and `/api/version`; the service refuses to start
  without one. Default bind `127.0.0.1`. No endpoint reads server files; uploads are size- and type-limited.
- Bounded retries (never after read timeouts), client-side rate limiting, request size limit, no prompt logging,
  Swagger UI off by default. Container runs non-root and read-only.

**Workarounds for u:ai API behaviour** (verified against the live API, partly differing from the API docs v1.0.7)
- Only `user`/`assistant` roles: system, developer and tool messages are converted; consecutive turns merged.
- Tailored-AI replies are read from `data.message.content`.
- Cost values are USD per 1 000 tokens (`BRIDGE_COST_UNIT_TOKENS`).
- JSON mode is enforced by the bridge (`BRIDGE_ENFORCE_JSON`), as u:ai does not enforce `responseFormat`.
- Error details are taken from `meta.error.message`.
- A parameter rejected for a specific model (e.g. `temperature` on `claude-opus-4-6`) is dropped and the request
  retried once; Gemini token limits are raised to `BRIDGE_GEMINI_MIN_MAX_TOKENS`.
  Both are reported in `X-Bridge-Adjusted-Params`.

**Quality**
- 43 unit tests against a mocked API; `tests/live_check.py` with 28 end-to-end checks against the real API.
- GitHub Actions CI; multi-arch image (amd64/arm64) on GHCR, rebuilt monthly with a fresh base image; Dependabot.
