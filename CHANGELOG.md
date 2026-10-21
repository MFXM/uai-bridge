# Changelog

All notable changes are documented here. This project uses [Semantic Versioning](https://semver.org/).

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
