"""
u:ai bridge - an OpenAI- and Ollama-compatible gateway for the
University of Vienna u:ai API (Academic AI platform, API docs v1.0.7).

Security model (summary, see README.md):
  * Every endpoint except /health requires a bridge API key
    (Authorization: Bearer <key> or X-API-Key: <key>), unless explicitly disabled.
  * Listens on 127.0.0.1 by default; the u:ai secret never leaves the process.
  * No endpoint reads files from the server's filesystem. Knowledge-base
    features are opt-in (BRIDGE_ENABLE_KB=true) and only accept uploads.
  * Bounded retries (never on read timeouts, to avoid double billing),
    client-side rate limiting, request/upload size limits, no prompt logging.
  * Optional monthly usage counter (tokens + estimated USD): stores only counts,
    model names and a hash of the u:ai client ID - never prompts or answers.
  * Optional MCP endpoint (BRIDGE_ENABLE_MCP=true) at /mcp for AI agents: same key,
    same upstream path and limits, a per-day call cap and Origin checks.

Developed with the help of AI coding assistants (Anthropic Claude Opus, Google Gemini); see README.
"""
from __future__ import annotations

import asyncio
import calendar
import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from pydantic import Field
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

__version__ = "1.2.0"

log = logging.getLogger("uai-bridge")

DEFAULT_BASE_URL = "https://univie-api.academic-ai.at"
DEFAULT_UPLOAD_EXTENSIONS = "pdf,doc,docx,txt,md,csv,html,htm,rtf,odt,pptx,xlsx,json"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v is not None and v.strip() != "":
            return v.strip()
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_num(name: str, default: float, cast=float):
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return cast(default)
    try:
        return cast(v)
    except ValueError as e:
        raise RuntimeError(f"{name} must be a number, got {v!r}") from e


@dataclass(frozen=True)
class Settings:
    client_id: str | None = None
    client_secret: str | None = None
    base_url: str = DEFAULT_BASE_URL
    credential_mode: str = "static"          # static | passthrough
    api_keys: tuple[str, ...] = ()
    allow_no_auth: bool = False
    trusted_cidrs: tuple[Any, ...] = ()
    host: str = "127.0.0.1"
    port: int = 8000
    enable_kb: bool = False
    enable_docs: bool = False
    image_mode: str = "placeholder"          # placeholder | forward
    append_citations: bool = True
    default_model: str = "gpt-4o"
    tailored_base_model: str = "gpt-4o"
    upstream_timeout: float = 300.0
    max_retries: int = 2
    max_concurrency: int = 8
    rate_limit_rpm: int = 90
    max_request_bytes: int = 10 * 1024 * 1024
    max_upload_bytes: int = 50 * 1024 * 1024
    upload_extensions: tuple[str, ...] = tuple(DEFAULT_UPLOAD_EXTENSIONS.split(","))
    ingest_timeout: float = 600.0
    ingest_poll_interval: float = 5.0
    models_cache_ttl: float = 300.0
    cost_unit_tokens: float = 1000.0         # u:ai reports cost per 1 000 tokens (observed live)
    gemini_min_max_tokens: int = 1024        # Gemini fails (u:ai 605) when the token limit is tiny
    enforce_json: bool = True
    tool_repair_retries: int = 1             # re-ask once when an emulated tool call is unusable
    allow_http_upstream: bool = False
    usage_tracking: bool = True
    usage_file: str | None = None            # None = keep the monthly counter in memory only
    usage_tz: str = "UTC"                    # time zone that defines where a month starts
    enable_mcp: bool = False                 # MCP endpoint at /mcp (needs the 'mcp' package)
    mcp_max_calls_per_day: int = 1000        # ask/web_research calls per u:ai client and day (0 = no cap)
    mcp_max_tokens: int = 16000              # default and upper bound for max_tokens of MCP tools
    mcp_research_model: str = "sonar-pro"    # default model of the web_research tool
    mcp_allowed_origins: tuple[str, ...] = ()  # browser origins allowed to call /mcp (others: 403)

    @classmethod
    def from_env(cls) -> "Settings":
        keys = tuple(k.strip() for k in (_env("BRIDGE_API_KEYS", "BRIDGE_API_KEY", default="") or "").split(",")
                     if k.strip())
        cidrs = []
        for c in (_env("BRIDGE_TRUSTED_CIDRS", default="") or "").split(","):
            if c.strip():
                try:
                    cidrs.append(ipaddress.ip_network(c.strip(), strict=False))
                except ValueError as e:
                    raise RuntimeError(f"BRIDGE_TRUSTED_CIDRS contains an invalid network: {c!r}") from e
        exts = tuple(e.strip().lower().lstrip(".")
                     for e in (_env("BRIDGE_UPLOAD_EXTENSIONS", default=DEFAULT_UPLOAD_EXTENSIONS) or "").split(",")
                     if e.strip())
        return cls(
            client_id=_env("UAI_CLIENT_ID", "UAI_ID", "CLIENT_ID"),
            client_secret=_env("UAI_CLIENT_SECRET", "UAI_SECRET", "CLIENT_SECRET"),
            base_url=(_env("UAI_BASE_URL", default=DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/"),
            credential_mode=(_env("BRIDGE_CREDENTIAL_MODE", default="static") or "static").lower(),
            api_keys=keys,
            allow_no_auth=_env_bool("BRIDGE_ALLOW_NO_AUTH"),
            trusted_cidrs=tuple(cidrs),
            host=_env("BRIDGE_HOST", default="127.0.0.1") or "127.0.0.1",
            port=_env_num("BRIDGE_PORT", 8000, int),
            enable_kb=_env_bool("BRIDGE_ENABLE_KB"),
            enable_docs=_env_bool("BRIDGE_ENABLE_DOCS"),
            image_mode=(_env("BRIDGE_IMAGE_MODE", default="placeholder") or "placeholder").lower(),
            append_citations=_env_bool("BRIDGE_APPEND_CITATIONS", True),
            default_model=_env("BRIDGE_DEFAULT_MODEL", default="gpt-4o") or "gpt-4o",
            tailored_base_model=_env("BRIDGE_TAILORED_BASE_MODEL", default="gpt-4o") or "gpt-4o",
            upstream_timeout=_env_num("BRIDGE_UPSTREAM_TIMEOUT_S", 300.0),
            max_retries=_env_num("BRIDGE_MAX_RETRIES", 2, int),
            max_concurrency=_env_num("BRIDGE_MAX_CONCURRENCY", 8, int),
            rate_limit_rpm=_env_num("BRIDGE_RATE_LIMIT_RPM", 90, int),
            max_request_bytes=int(_env_num("BRIDGE_MAX_REQUEST_MB", 10.0) * 1024 * 1024),
            max_upload_bytes=int(_env_num("BRIDGE_MAX_UPLOAD_MB", 50.0) * 1024 * 1024),
            upload_extensions=exts,
            ingest_timeout=_env_num("BRIDGE_INGEST_TIMEOUT_S", 600.0),
            ingest_poll_interval=_env_num("BRIDGE_INGEST_POLL_S", 5.0),
            models_cache_ttl=_env_num("BRIDGE_MODELS_CACHE_S", 300.0),
            cost_unit_tokens=_env_num("BRIDGE_COST_UNIT_TOKENS", 1000.0),
            gemini_min_max_tokens=_env_num("BRIDGE_GEMINI_MIN_MAX_TOKENS", 1024, int),
            enforce_json=_env_bool("BRIDGE_ENFORCE_JSON", True),
            tool_repair_retries=_env_num("BRIDGE_TOOL_REPAIR_RETRIES", 1, int),
            allow_http_upstream=_env_bool("BRIDGE_ALLOW_HTTP_UPSTREAM"),
            usage_tracking=_env_bool("BRIDGE_USAGE_TRACKING", True),
            usage_file=_env("BRIDGE_USAGE_FILE"),
            usage_tz=_env("BRIDGE_USAGE_TZ", default="UTC") or "UTC",
            enable_mcp=_env_bool("BRIDGE_ENABLE_MCP"),
            mcp_max_calls_per_day=_env_num("BRIDGE_MCP_MAX_CALLS_PER_DAY", 1000, int),
            mcp_max_tokens=_env_num("BRIDGE_MCP_MAX_TOKENS", 16000, int),
            mcp_research_model=_env("BRIDGE_MCP_RESEARCH_MODEL", default="sonar-pro") or "sonar-pro",
            mcp_allowed_origins=tuple(o.strip().rstrip("/") for o in
                                      (_env("BRIDGE_MCP_ALLOWED_ORIGINS", default="") or "").split(",")
                                      if o.strip()),
        )

    def validate(self) -> None:
        """Refuse to start with an unsafe or incomplete configuration."""
        if self.credential_mode not in ("static", "passthrough"):
            raise RuntimeError("BRIDGE_CREDENTIAL_MODE must be 'static' or 'passthrough'.")
        if not self.base_url.startswith("https://") and not self.allow_http_upstream:
            raise RuntimeError("UAI_BASE_URL must use https:// "
                               "(set BRIDGE_ALLOW_HTTP_UPSTREAM=true only for local testing).")
        if self.image_mode not in ("placeholder", "forward"):
            raise RuntimeError("BRIDGE_IMAGE_MODE must be 'placeholder' or 'forward'.")
        if self.credential_mode == "static":
            if not self.client_id or not self.client_secret:
                raise RuntimeError("Missing u:ai credentials: set UAI_CLIENT_ID and UAI_CLIENT_SECRET.")
            if not self.api_keys and not self.allow_no_auth:
                raise RuntimeError(
                    "No bridge API key configured. Set BRIDGE_API_KEYS (generate one with "
                    "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"`), "
                    "or set BRIDGE_ALLOW_NO_AUTH=true if the bridge is reachable only by you."
                )
            if any(len(k) < 24 for k in self.api_keys):
                raise RuntimeError("Every key in BRIDGE_API_KEYS must be at least 24 characters long.")
        if self.max_concurrency < 1:
            raise RuntimeError("BRIDGE_MAX_CONCURRENCY must be >= 1.")
        if not 0 <= self.max_retries <= 5:
            raise RuntimeError("BRIDGE_MAX_RETRIES must be between 0 and 5.")
        if not 0 <= self.tool_repair_retries <= 2:
            raise RuntimeError("BRIDGE_TOOL_REPAIR_RETRIES must be between 0 and 2.")
        if self.mcp_max_calls_per_day < 0:
            raise RuntimeError("BRIDGE_MCP_MAX_CALLS_PER_DAY must be >= 0 (0 = no cap).")
        if self.mcp_max_tokens < 1:
            raise RuntimeError("BRIDGE_MCP_MAX_TOKENS must be >= 1.")
        if not MODEL_NAME_RE.match(self.mcp_research_model):
            raise RuntimeError(f"BRIDGE_MCP_RESEARCH_MODEL is not a valid model name: {self.mcp_research_model!r}")
        for o in self.mcp_allowed_origins:
            if not re.match(r"^https?://[^/\s]+$", o):
                raise RuntimeError(f"BRIDGE_MCP_ALLOWED_ORIGINS entries must look like https://host[:port], got {o!r}")
        try:
            ZoneInfo(self.usage_tz)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise RuntimeError(f"BRIDGE_USAGE_TZ is not a known time zone: {self.usage_tz!r} "
                               "(use an IANA name such as 'UTC' or 'Europe/Vienna').") from e


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class BridgeError(Exception):
    def __init__(self, status: int, message: str, type_: str = "bridge_error",
                 code: str | None = None, headers: dict | None = None, extra: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.type, self.code = status, message, type_, code
        self.headers = headers or {}
        self.extra = extra or {}

    def response(self) -> JSONResponse:
        body = {"error": {"message": self.message, "type": self.type, "code": self.code, **self.extra}}
        return JSONResponse(status_code=self.status, content=body, headers=self.headers)


def _upstream_message(r: httpx.Response) -> str:
    try:
        j = r.json()
        if isinstance(j, dict):
            err = (j.get("meta") or {}).get("error") if isinstance(j.get("meta"), dict) else None
            if isinstance(err, dict) and isinstance(err.get("message"), str) and err["message"]:
                code = err.get("internalErrorCode")
                return (f"{err['message']}" + (f" (u:ai code {code})" if code is not None else ""))[:500]
            for k in ("message", "detail", "error"):
                v = j.get(k)
                if isinstance(v, str) and v:
                    return v[:500]
                if isinstance(v, dict) and isinstance(v.get("message"), str):
                    return v["message"][:500]
            return json.dumps(j)[:500]
    except ValueError:
        pass
    return (r.text or "")[:500]


REJECTED_FIELD_RE = re.compile(r"Field '([A-Za-z]+)'")


def map_upstream_error(r: httpx.Response, passthrough: bool) -> BridgeError:
    e = _map_upstream_error(r, passthrough)
    m = REJECTED_FIELD_RE.search(_upstream_message(r)) if r.status_code in (400, 422) else None
    e.rejected_field = m.group(1) if m else None
    return e


def _map_upstream_error(r: httpx.Response, passthrough: bool) -> BridgeError:
    msg = _upstream_message(r)
    s = r.status_code
    if "KB_UNAVAILABLE" in msg:
        return BridgeError(409, f"The knowledge base of this Tailored AI is not ready (KB_UNAVAILABLE): {msg}",
                           "knowledge_base_unavailable", "kb_unavailable")
    if s in (400, 422):
        return BridgeError(400, f"u:ai rejected the request: {msg}", "invalid_request_error", f"upstream_{s}")
    if s in (401, 403):
        if passthrough:
            return BridgeError(s, f"u:ai rejected your credentials or permissions (HTTP {s}): {msg}",
                               "authentication_error", f"upstream_{s}")
        return BridgeError(502, f"u:ai rejected the bridge's credentials or permissions (HTTP {s}). "
                                "Check UAI_CLIENT_ID / UAI_CLIENT_SECRET and the API role.",
                           "upstream_auth_error", f"upstream_{s}")
    if s == 404:
        return BridgeError(404, f"Not found at u:ai: {msg}", "not_found_error", "upstream_404")
    if s == 429:
        h = {"Retry-After": r.headers["Retry-After"]} if "Retry-After" in r.headers else {}
        return BridgeError(429, "u:ai rate limit or cost limit reached. Try again later.",
                           "rate_limit_error", "upstream_429", headers=h)
    return BridgeError(502, f"u:ai returned HTTP {s}: {msg}", "upstream_error", f"upstream_{s}")


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Sliding-window limiter: at most `rpm` upstream requests per 60 s."""

    def __init__(self, rpm: int):
        self.rpm = rpm
        self.events: collections.deque[float] = collections.deque()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.rpm <= 0:
            return
        while True:
            async with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0] >= 60.0:
                    self.events.popleft()
                if len(self.events) < self.rpm:
                    self.events.append(now)
                    return
                wait = 60.0 - (now - self.events[0]) + 0.01
            await asyncio.sleep(wait)


# --------------------------------------------------------------------------- #
# Monthly usage counter
# --------------------------------------------------------------------------- #
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_COUNTERS = ("requests", "prompt_tokens", "completion_tokens", "total_tokens", "requests_without_usage",
             "requests_without_price")


def _empty_bucket() -> dict:
    return {**{k: 0 for k in _COUNTERS}, "estimated_cost_usd": 0.0}


def account_id(client_id: str) -> str:
    """Stable, non-reversible label for a u:ai API client (the client ID itself is never stored)."""
    return hashlib.sha256(client_id.encode()).hexdigest()[:16]


class UsageTracker:
    """Counts requests, tokens and estimated cost per u:ai API client and calendar month.

    The numbers are an estimate built from the token counts u:ai returns and the prices of
    /api/v1/llm/models at the time of each request. Tailored-AI (RAG) replies carry no token
    usage, so they are only counted as `requests_without_usage`.
    """

    def __init__(self, path: str | None, tz: str):
        self.path = path
        self.tz = ZoneInfo(tz)
        self.tz_name = tz
        self.lock = threading.Lock()
        self.persistent = False
        self._warned = False
        self.data: dict = {"version": 1, "accounts": {}}
        if path:
            self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or not isinstance(d.get("accounts"), dict):
                raise ValueError("unexpected structure")
            self.data = d
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            bad = f"{self.path}.corrupt-{int(time.time())}"
            log.error("Usage file %s is unreadable (%s); starting a new counter, old file kept as %s",
                      self.path, type(e).__name__, bad)
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
        self.persistent = self._write()

    def _write(self) -> bool:
        """Atomically write the counter. Never raises: a failing disk must not break chat requests."""
        if not self.path:
            return False
        tmp = f"{self.path}.tmp-{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
            return True
        except OSError as e:
            if not self._warned:
                log.warning("Cannot write usage file %s (%s); the monthly counter is kept in memory only "
                            "and resets on restart. Mount a writable volume there.", self.path, type(e).__name__)
                self._warned = True
            return False

    # -- months --------------------------------------------------------------
    def month_of(self, when: datetime | None = None) -> str:
        return (when or datetime.now(timezone.utc)).astimezone(self.tz).strftime("%Y-%m")

    def bounds(self, month: str) -> tuple[datetime, datetime]:
        y, m = int(month[:4]), int(month[5:7])
        start = datetime(y, m, 1, tzinfo=self.tz)
        end = start + timedelta(days=calendar.monthrange(y, m)[1])
        return start, end.replace(tzinfo=self.tz)

    # -- recording -----------------------------------------------------------
    def record(self, client_id: str, model: str, usage: dict, cost_usd: float | None,
               when: datetime | None = None) -> None:
        now = when or datetime.now(timezone.utc)
        month = self.month_of(now)
        with self.lock:
            acc = self.data["accounts"].setdefault(account_id(client_id), {"months": {}})
            mon = acc["months"].setdefault(month, {"total": _empty_bucket(), "by_model": {},
                                                   "first_request": now.isoformat()})
            mon["last_request"] = now.isoformat()
            for bucket in (mon["total"], mon["by_model"].setdefault(model, _empty_bucket())):
                bucket["requests"] += 1
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    bucket[k] += int(usage.get(k) or 0)
                if not usage.get("total_tokens"):
                    bucket["requests_without_usage"] += 1
                elif cost_usd is None:
                    bucket["requests_without_price"] += 1
                else:
                    bucket["estimated_cost_usd"] = round(bucket["estimated_cost_usd"] + cost_usd, 8)
            self.persistent = self._write() if self.path else False

    # -- reporting -----------------------------------------------------------
    def months(self, client_id: str) -> list[str]:
        acc = self.data["accounts"].get(account_id(client_id)) or {}
        return sorted((acc.get("months") or {}).keys())

    def report(self, client_id: str, month: str | None = None, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        month = month or self.month_of(now)
        start, end = self.bounds(month)
        with self.lock:
            acc = self.data["accounts"].get(account_id(client_id)) or {}
            mon = json.loads(json.dumps((acc.get("months") or {}).get(month) or {}))
        total = mon.get("total") or _empty_bucket()
        out: dict[str, Any] = {
            "month": month, "timezone": self.tz_name,
            "period_start": start.isoformat(), "period_end": end.isoformat(),
            "currency": "USD", **total,
            "estimated_cost_usd": round(total.get("estimated_cost_usd", 0.0), 6),
            "by_model": {k: dict(v, estimated_cost_usd=round(v.get("estimated_cost_usd", 0.0), 6))
                         for k, v in sorted((mon.get("by_model") or {}).items())},
            "first_request": mon.get("first_request"), "last_request": mon.get("last_request"),
            "persistent": self.persistent,
        }
        if start <= now < end:
            elapsed = (now - start).total_seconds()
            span = (end - start).total_seconds()
            out["month_elapsed_fraction"] = round(elapsed / span, 4)
            # A linear projection is meaningless in the first hours of a month.
            if elapsed >= 86400:
                f = span / elapsed
                out["projected_month_cost_usd"] = round(total.get("estimated_cost_usd", 0.0) * f, 6)
                out["projected_month_tokens"] = int(total.get("total_tokens", 0) * f)
            else:
                out["projected_month_cost_usd"] = None
                out["projected_month_tokens"] = None
        return out


class DailyCallBudget:
    """Per-day cap on billed MCP tool calls, per u:ai API client (in memory, resets on restart).

    An AI agent can call a tool in a loop in a way a hand-written client would not; this cap bounds the
    damage. Days follow the usage time zone. Attempts are counted, including ones that fail upstream.
    """

    def __init__(self, limit: int, tz: str):
        self.limit = limit
        self.tz = ZoneInfo(tz)
        self.lock = threading.Lock()
        self.day = ""
        self.counts: dict[str, int] = {}

    def _today(self, now: datetime | None) -> str:
        return (now or datetime.now(timezone.utc)).astimezone(self.tz).strftime("%Y-%m-%d")

    def take(self, client_id: str, now: datetime | None = None) -> bool:
        """Count one call; False (and nothing counted) if the cap is reached."""
        with self.lock:
            today = self._today(now)
            if today != self.day:
                self.day, self.counts = today, {}
            key = account_id(client_id)
            used = self.counts.get(key, 0)
            if self.limit and used >= self.limit:
                return False
            self.counts[key] = used + 1
            return True

    def used(self, client_id: str, now: datetime | None = None) -> int:
        with self.lock:
            return self.counts.get(account_id(client_id), 0) if self._today(now) == self.day else 0


# --------------------------------------------------------------------------- #
# Upstream client
# --------------------------------------------------------------------------- #
Creds = tuple[str, str]
RETRYABLE_STATUS = (429, 502, 503, 504)


class UaiClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self.http = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(settings.upstream_timeout, connect=10.0),
            limits=httpx.Limits(max_connections=settings.max_concurrency * 2,
                                max_keepalive_connections=settings.max_concurrency),
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": f"uai-bridge/{__version__}"},
        )
        self.sem = asyncio.Semaphore(settings.max_concurrency)
        self.limiters: dict[str, RateLimiter] = {}
        self.sleep = asyncio.sleep  # replaceable in tests

    async def close(self) -> None:
        await self.http.aclose()

    def _limiter(self, client_id: str) -> RateLimiter:
        if client_id not in self.limiters:
            self.limiters[client_id] = RateLimiter(self.s.rate_limit_rpm)
        return self.limiters[client_id]

    async def request(self, method: str, path: str, creds: Creds, *, json_body: Any = None,
                      files: Any = None, params: dict | None = None, timeout: float | None = None,
                      retry: bool = True) -> dict:
        headers = {"X-Client-ID": creds[0], "X-Client-Secret": creds[1]}
        attempt = 0
        while True:
            await self._limiter(creds[0]).acquire()
            try:
                async with self.sem:
                    r = await self.http.request(
                        method, path, headers=headers, json=json_body, files=files, params=params,
                        timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                if retry and attempt < self.s.max_retries:
                    attempt += 1
                    await self.sleep(min(2 ** attempt, 20))
                    continue
                raise BridgeError(502, "Could not connect to the u:ai API.", "upstream_unreachable") from e
            except httpx.TimeoutException as e:
                # Never retry a read/write timeout: the request may already be running (and billed).
                raise BridgeError(504, "The u:ai API did not answer in time.", "upstream_timeout") from e
            except httpx.HTTPError as e:
                raise BridgeError(502, f"Error talking to the u:ai API: {type(e).__name__}",
                                  "upstream_error") from e

            if r.status_code in RETRYABLE_STATUS and retry and attempt < self.s.max_retries:
                attempt += 1
                delay = float(min(2 ** attempt, 20))
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        delay = min(max(float(ra), 0.0), 30.0)
                    except ValueError:
                        pass
                log.warning("u:ai %s %s returned %s, retry %d/%d in %.1fs", method, path, r.status_code,
                            attempt, self.s.max_retries, delay)
                await self.sleep(delay)
                continue
            if r.status_code >= 400:
                raise map_upstream_error(r, self.s.credential_mode == "passthrough")
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError as e:
                raise BridgeError(502, "u:ai returned a response that is not JSON.", "upstream_error") from e


# --------------------------------------------------------------------------- #
# Translation: OpenAI / Ollama  ->  u:ai
# --------------------------------------------------------------------------- #
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,100}$")
TAILORED_RE = re.compile(r"^tailored:([0-9a-fA-F\-]{36})(?:@([A-Za-z0-9._\-]{1,100}))?$")
REASONING_RE = re.compile(r"^(o\d|gpt-5)", re.IGNORECASE)


def resolve_model(name: Any, settings: Settings) -> tuple[str, str | None]:
    """Return (u:ai model, tailoredAiId or None). Accepts 'gpt-4o', 'openai/gpt-4o',
    'tailored:<uuid>' and 'tailored:<uuid>@<base-model>'."""
    if name is None or name == "":
        name = settings.default_model
    if not isinstance(name, str):
        raise BridgeError(400, "'model' must be a string.", "invalid_request_error")
    name = name.strip()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    m = TAILORED_RE.match(name)
    if m:
        try:
            tid = str(uuid.UUID(m.group(1)))
        except ValueError as e:
            raise BridgeError(400, "Invalid Tailored AI id in model name.", "invalid_request_error") from e
        return (m.group(2) or settings.tailored_base_model), tid
    if not MODEL_NAME_RE.match(name):
        raise BridgeError(400, f"Invalid model name: {name[:100]!r}", "invalid_request_error")
    return name, None


def is_reasoning_model(model: str) -> bool:
    return bool(REASONING_RE.match(model))


def _num(name: str, v: Any, lo: float, hi: float, integer: bool = False):
    try:
        if isinstance(v, bool):
            raise ValueError
        x = int(v) if integer else float(v)
        if integer and float(v) != x:
            raise ValueError
    except (TypeError, ValueError):
        raise BridgeError(400, f"'{name}' must be a {'integer' if integer else 'number'}.",
                          "invalid_request_error") from None
    if not lo <= x <= hi:
        raise BridgeError(400, f"'{name}' must be between {lo} and {hi}.", "invalid_request_error")
    return x


SUPPORTED_OPENAI_PARAMS = {
    "model", "messages", "stream", "stream_options", "tools", "tool_choice", "temperature", "max_tokens",
    "max_completion_tokens", "frequency_penalty", "presence_penalty", "stop", "seed", "response_format",
    "reasoning_effort", "verbosity", "n", "user", "parallel_tool_calls", "functions", "function_call",
}


def translate_params(model: str, *, temperature=None, max_tokens=None, frequency_penalty=None,
                     presence_penalty=None, stop=None, seed=None, response_format=None,
                     reasoning_effort=None, verbosity=None) -> dict:
    p: dict[str, Any] = {}
    if temperature is not None:
        p["temperature"] = _num("temperature", temperature, 0.0, 2.0)
    if max_tokens is not None:
        n = _num("max_tokens", max_tokens, 1, 1_000_000, integer=True)
        p["maxCompletionTokens" if is_reasoning_model(model) else "maxTokens"] = n
    if frequency_penalty is not None:
        p["frequencyPenalty"] = _num("frequency_penalty", frequency_penalty, -2.0, 2.0)
    if presence_penalty is not None:
        p["presencePenalty"] = _num("presence_penalty", presence_penalty, -2.0, 2.0)
    if stop is not None:
        if isinstance(stop, str):
            stop = [stop]
        if not (isinstance(stop, list) and all(isinstance(x, str) for x in stop) and len(stop) <= 4):
            raise BridgeError(400, "'stop' must be a string or a list of up to 4 strings.",
                              "invalid_request_error")
        if stop:
            p["stop"] = stop
    if seed is not None:
        p["seed"] = _num("seed", seed, -(2 ** 63), 2 ** 63 - 1, integer=True)
    if response_format is not None:
        t = response_format.get("type") if isinstance(response_format, dict) else None
        if t == "text":
            p["responseFormat"] = {"type": "text"}
        elif t in ("json_object", "json_schema"):
            # u:ai documents only text/json_object; a JSON schema is downgraded to JSON mode.
            p["responseFormat"] = {"type": "json_object"}
        else:
            raise BridgeError(400, "'response_format.type' must be text, json_object or json_schema.",
                              "invalid_request_error")
    if reasoning_effort is not None:
        if reasoning_effort not in ("low", "medium", "high"):
            raise BridgeError(400, "'reasoning_effort' must be low, medium or high.", "invalid_request_error")
        p["reasoningEffort"] = reasoning_effort
    if verbosity is not None:
        if verbosity not in ("low", "medium", "high"):
            raise BridgeError(400, "'verbosity' must be low, medium or high.", "invalid_request_error")
        p["verbosity"] = verbosity
    return p


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _to_parts(content: Any, image_mode: str) -> list[dict]:
    if content is None:
        return []
    if isinstance(content, str):
        return [_text(content)] if content.strip() else []
    if isinstance(content, list):
        parts: list[dict] = []
        for p in content:
            if isinstance(p, str):
                if p:
                    parts.append(_text(p))
            elif isinstance(p, dict):
                t = p.get("type")
                if t in ("text", "input_text"):
                    if p.get("text"):
                        parts.append(_text(str(p["text"])))
                elif t in ("image_url", "input_image"):
                    iu = p.get("image_url")
                    url = iu.get("url", "") if isinstance(iu, dict) else str(iu or "")
                    if image_mode == "forward" and url:
                        parts.append({"type": "image_url", "image_url": {"url": url}})
                    else:
                        mime = re.match(r"data:([\w/+.\-]+);base64", url)
                        parts.append(_text(f"[image omitted: {mime.group(1) if mime else 'image'}]"))
                else:
                    parts.append(_text(f"[unsupported content part omitted: {t}]"))
        return parts
    return [_text(str(content))]


def _prefix(parts: list[dict], prefix: str) -> list[dict]:
    if parts and parts[0].get("type") == "text":
        return [_text(prefix + parts[0]["text"])] + parts[1:]
    return [_text(prefix.rstrip("\n"))] + parts


def _collapse(parts: list[dict]) -> str | list[dict]:
    if all(p.get("type") == "text" for p in parts):
        return "\n".join(p["text"] for p in parts)
    return parts


def _args_obj(a: Any) -> Any:
    if isinstance(a, str):
        try:
            return json.loads(a)
        except ValueError:
            return a
    return a


def to_uai_messages(messages: Any, image_mode: str = "placeholder", tool_instruction: str = "",
                    tool_reminder: str = "") -> list[dict]:
    """Convert OpenAI/Ollama messages to u:ai messages: only 'user'/'assistant' roles,
    no consecutive identical roles, no empty messages.

    The tool instruction is placed after the client's leading system messages, so it is read after
    (and takes precedence over) a client prompt that assumes native tool calling. The short
    reminder is appended to the last message when that message is the user's or a tool result."""
    if not isinstance(messages, list):
        raise BridgeError(400, "'messages' must be a list.", "invalid_request_error")
    out: list[list] = []
    has_content = False
    names_by_id: dict[str, str] = {}   # tool_call_id -> tool name, to label tool results
    pending_instruction = tool_instruction

    def emit(role: str, parts: list[dict]) -> None:
        if out and out[-1][0] == role:
            prev = out[-1][1]
            if prev and prev[-1].get("type") == "text" and parts[0].get("type") == "text":
                prev[-1] = _text(prev[-1]["text"] + "\n\n" + parts[0]["text"])
                prev.extend(parts[1:])
            else:
                prev.extend(parts)
        else:
            out.append([role, list(parts)])

    for m in messages:
        if not isinstance(m, dict):
            raise BridgeError(400, "Each message must be an object.", "invalid_request_error")
        role = str(m.get("role", "user")).lower().strip()
        if pending_instruction and role not in ("system", "developer"):
            emit("user", [_text("### SYSTEM INSTRUCTION ###\n" + pending_instruction)])
            pending_instruction = ""
        parts = _to_parts(m.get("content"), image_mode)
        imgs = m.get("images")  # Ollama-style images (base64 strings)
        if isinstance(imgs, list):
            for img in imgs:
                if isinstance(img, str) and img:
                    url = img if img.startswith("data:") else f"data:image/jpeg;base64,{img}"
                    parts += _to_parts([{"type": "image_url", "image_url": {"url": url}}], image_mode)
        if role == "assistant":
            calls = []
            for tc in m.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                if tc and tc.get("id") and fn.get("name"):
                    names_by_id[str(tc["id"])] = str(fn["name"])
                calls.append({"name": fn.get("name"), "arguments": _args_obj(fn.get("arguments", {}))})
            if isinstance(m.get("function_call"), dict):
                fc = m["function_call"]
                calls.append({"name": fc.get("name"), "arguments": _args_obj(fc.get("arguments", {}))})
            if calls:
                parts.append(_text(json.dumps({"tool_calls": calls}, ensure_ascii=False)))
        elif role in ("system", "developer"):
            role = "user"
            parts = _prefix(parts, "### SYSTEM INSTRUCTION ###\n") if parts else []
        elif role in ("tool", "function"):
            cid = str(m.get("tool_call_id") or "")
            name = m.get("name") or names_by_id.get(cid)
            label = f"{name} | {cid}" if name and cid and name != cid else (name or cid or "tool")
            role = "user"
            parts = _prefix(parts or [_text("(empty result)")], f"### TOOL RESULT [{label}] ###\n")
        elif role != "user":
            tag = re.sub(r"[^A-Z0-9_]", "", role.upper())[:20] or "OTHER"
            role = "user"
            parts = _prefix(parts, f"### ADDITIONAL CONTEXT ({tag}) ###\n") if parts else []
        if not parts:
            continue
        has_content = True
        emit(role, parts)
    if not has_content:
        raise BridgeError(400, "'messages' must contain at least one non-empty message.", "invalid_request_error")
    if pending_instruction:          # only system messages were sent
        emit("user", [_text("### SYSTEM INSTRUCTION ###\n" + pending_instruction)])
    if tool_reminder and out and out[-1][0] == "user" and len(out) > 2:
        emit("user", [_text("### SYSTEM INSTRUCTION ###\n" + tool_reminder)])
    return [{"role": r, "content": _collapse(p)} for r, p in out]


# --------------------------------------------------------------------------- #
# Tool-call emulation (u:ai has no native tool calling)
# --------------------------------------------------------------------------- #
TOOL_REMINDER = ("Reminder: to call tools, end your reply with one JSON object "
                 '{"tool_calls": [{"name": ..., "arguments": {...}}]} and write nothing after it. '
                 "Never write a '### TOOL RESULT' block yourself; results are sent to you.")


def build_tool_instruction(tools: Any, tool_choice: Any) -> tuple[str, set[str]]:
    if not tools or tool_choice == "none":
        return "", set()
    if not isinstance(tools, list):
        raise BridgeError(400, "'tools' must be a list.", "invalid_request_error")
    funcs = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        f = t.get("function") if t.get("type", "function") == "function" else None
        if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"]:
            funcs.append(f)
    if not funcs:
        return "", set()
    names = {f["name"] for f in funcs}
    lines = [
        "TOOL PROTOCOL. This conversation has no native tool-calling channel. It overrides any other "
        "instruction about how to invoke tools. To call one or more tools, reply with ONLY one JSON "
        "object and nothing else (no markdown code fences, no explanation before or after):",
        '{"tool_calls": [{"name": "<tool name>", "arguments": {<arguments matching the parameter schema>}}]}',
        "Rules:",
        "- Use the tool names exactly as listed below (no 'functions.' prefix).",
        "- The JSON must be valid: escape backslashes and quotes inside strings, e.g. a Windows path is "
        '"C:\\\\Users\\\\name".',
        "- Independent calls go into the same tool_calls list; do not write several JSON objects.",
        "- After the JSON, stop. Never write a tool result yourself and never guess it. Results are sent "
        "back to you in messages that start with '### TOOL RESULT [<tool name> | <call id>]'.",
        "- If you do not need a tool, answer normally in plain text and do not output JSON of that shape.",
        "Available tools:",
    ]
    for f in funcs:
        lines.append(f"- {f['name']}: {f.get('description', '')}".rstrip())
        lines.append(f"  parameters (JSON Schema): {json.dumps(f.get('parameters', {}), ensure_ascii=False)}")
    if tool_choice == "required":
        lines.append("You MUST call at least one tool in this reply.")
    elif isinstance(tool_choice, dict):
        forced = (tool_choice.get("function") or {}).get("name") or tool_choice.get("name")
        if forced in names:
            lines.append(f"You MUST call the tool '{forced}' in this reply.")
    return "\n".join(lines), names


FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
FENCE_LINE_RE = re.compile(r"^[ \t]*```[A-Za-z0-9_-]*[ \t]*$", re.MULTILINE)
FAKE_RESULT_RE = re.compile(r"^[ \t]*#{2,}[ \t]*TOOL RESULT\b", re.MULTILINE | re.IGNORECASE)
BAD_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})')
NAME_PREFIX_RE = re.compile(r"^(?:functions|function|tools|tool)[.:]", re.IGNORECASE)


@dataclass
class ToolParse:
    """Result of reading a model reply for emulated tool calls.

    calls:   OpenAI tool_calls, or None when the reply is treated as plain text.
    content: text to return alongside the calls (a short preamble), or the full reply.
    error:   set when the reply clearly TRIED to call a tool but the call is unusable
             (invalid JSON, unknown tool, non-object arguments); a repair request can fix it.
    """
    calls: list[dict] | None
    content: str
    error: str | None = None


def _balanced_end(t: str, i: int) -> int | None:
    """Index just after the JSON object that starts at t[i] == '{' (string-aware), or None."""
    depth, in_str, esc = 0, False, False
    for j in range(i, len(t)):
        ch = t[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return j + 1
    return None


def _loads_lenient(seg: str) -> Any:
    try:
        return json.loads(seg)
    except ValueError:
        pass
    # Models often write Windows paths with single backslashes ("C:\Users\..."), which is invalid
    # JSON ("\U", "\D", ...). Double every backslash that does not start a valid escape.
    try:
        return json.loads(BAD_ESCAPE_RE.sub(r"\\\\", seg))
    except ValueError:
        return None


def _raw_calls(obj: Any) -> list | None:
    """The list of call objects in one of the accepted envelopes, else None."""
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("tool_calls"), list):
        return obj["tool_calls"]
    if "tool" in obj and isinstance(obj.get("tool"), str):          # compact {"tool": ..., "args": {...}}
        return [{"name": obj["tool"], "arguments": obj.get("args", obj.get("arguments", {}))}]
    if isinstance(obj.get("name"), str) and ("arguments" in obj or "parameters" in obj):
        return [{"name": obj["name"], "arguments": obj.get("arguments", obj.get("parameters", {}))}]
    if isinstance(obj.get("function"), dict) and isinstance(obj["function"].get("name"), str):
        return [obj]
    return None


def _resolve_tool_name(name: Any, allowed: set[str]) -> str | None:
    if not isinstance(name, str):
        return None
    n = name.strip()
    if n in allowed:
        return n
    n = NAME_PREFIX_RE.sub("", n)
    if n in allowed:
        return n
    low = [a for a in allowed if a.lower() == n.lower()]
    return low[0] if len(low) == 1 else None


def parse_tool_reply(text: str, allowed: set[str]) -> ToolParse:
    """Find emulated tool calls in a model reply.

    Accepted: the reply is a tool-call JSON object, optionally fenced, optionally preceded by a short
    preamble (returned as content), optionally split into several such objects. Not accepted: JSON
    followed by more prose (an example or explanation, not a call). A '### TOOL RESULT' block the
    model invented after its call is cut off before parsing."""
    if not allowed or not text:
        return ToolParse(None, text or "")
    t = text.strip()
    fake = FAKE_RESULT_RE.search(t)
    if fake and "{" in t[:fake.start()]:
        t = t[:fake.start()].rstrip()
    spans: list[tuple[int, int, Any]] = []
    i, broken = 0, False
    while True:
        i = t.find("{", i)
        if i < 0:
            break
        end = _balanced_end(t, i)
        if end is None:
            broken = broken or '"tool_calls"' in t[i:] or '"tool"' in t[i:]
            break
        seg = t[i:end]
        obj = _loads_lenient(seg)
        if _raw_calls(obj) is not None:
            spans.append((i, end, obj))
        elif obj is None and ('"tool_calls"' in seg or '"name"' in seg):
            broken = True
        i = end
    if not spans:
        if broken:
            return ToolParse(None, text, "the tool-call JSON is invalid or incomplete")
        return ToolParse(None, text)
    tail = FENCE_LINE_RE.sub("", t[spans[-1][1]:]).strip()
    if tail:
        return ToolParse(None, text)       # JSON followed by prose: an example, not a call
    between = "".join(FENCE_LINE_RE.sub("", t[a[1]:b[0]]).strip() for a, b in zip(spans, spans[1:]))
    if between:
        return ToolParse(None, text)
    preamble = FENCE_LINE_RE.sub("", t[:spans[0][0]]).strip()
    calls = []
    for _, _, obj in spans:
        for c in _raw_calls(obj):
            if not isinstance(c, dict):
                return ToolParse(None, text, "each entry of tool_calls must be an object")
            fn = c.get("function") if isinstance(c.get("function"), dict) else {}
            raw_name = c.get("name") or fn.get("name")
            name = _resolve_tool_name(raw_name, allowed)
            if name is None:
                return ToolParse(None, text, f"unknown tool {str(raw_name)[:60]!r}; available: "
                                             + ", ".join(sorted(allowed))[:600])
            args = c.get("arguments", c.get("args", c.get("parameters", fn.get("arguments", {}))))
            if isinstance(args, str):
                args = _loads_lenient(args) if args.strip() else {}
            if args is None:
                args = {}
            if not isinstance(args, dict):
                return ToolParse(None, text, f"the arguments of {name!r} must be a JSON object")
            calls.append({"id": f"call_{uuid.uuid4().hex[:24]}", "type": "function",
                          "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}})
    if not calls:
        return ToolParse(None, text)
    return ToolParse(calls, preamble)


def parse_tool_calls(text: str, allowed: set[str]) -> list[dict] | None:
    """Return OpenAI tool_calls if the reply is a tool call naming only offered tools; else None."""
    return parse_tool_reply(text, allowed).calls


def tool_repair_message(error: str | None, required: bool) -> str:
    why = (f"Your previous reply tried to call a tool, but it could not be used: {error}."
           if error else "Your previous reply did not call a tool, but a tool call is required.")
    return (why + " Reply again with ONLY the JSON object "
            '{"tool_calls": [{"name": "<tool name>", "arguments": {...}}]} - valid JSON (escape '
            "backslashes in paths), tool names exactly as listed, no other text.")


JSON_INSTRUCTION = ("Respond with exactly one valid JSON object and nothing else: no markdown code fences, "
                    "no explanation before or after.")


def extract_json_object(text: str) -> str | None:
    """u:ai accepts responseFormat=json_object but does not enforce it (observed live: replies such as
    'Here is the JSON: ```json {...} ```'). Return the first complete JSON object/array in the text."""
    t = (text or "").strip()
    m = FENCE_RE.match(t)
    if m:
        t = m.group(1).strip()
    dec = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch in "{[":
            try:
                obj, _ = dec.raw_decode(t[i:])
            except ValueError:
                continue
            if isinstance(obj, (dict, list)):
                return json.dumps(obj, ensure_ascii=False)
    return None


# --------------------------------------------------------------------------- #
# Response helpers
# --------------------------------------------------------------------------- #
def format_citations(citations: Any) -> tuple[str, list[dict]]:
    """u:ai citations: {"index", "source": {"documentName", "pageNumber", ...}, "sourceType"}."""
    if not isinstance(citations, list):
        return "", []
    lines, clean = [], []
    for c in citations:
        if not isinstance(c, dict):
            continue
        # documents (RAG): {"source": {...}}; web search models (sonar): {"location": {...}, "pageContent": url}
        src = next((c[k] for k in ("source", "location") if isinstance(c.get(k), dict)), c)
        name = src.get("documentName") or "document"
        page = src.get("pageNumber")
        idx = c.get("index")
        url = c.get("pageContent") if str(c.get("pageContent", "")).startswith(("http://", "https://")) else None
        line = (f"- [{idx}] " if idx is not None else "- ") + str(name)
        if page is not None:
            line += f", p. {page}"
        if url:
            line += f" <{url}>"
        lines.append(line)
        clean.append({"index": idx, "type": c.get("type") or c.get("sourceType"), "document_name": name,
                      "page_number": page, "url": url, "document_id": src.get("documentId"),
                      "knowledge_base_id": src.get("knowledgeBaseId")})
    return ("\n\n**Sources:**\n" + "\n".join(lines)) if lines else "", clean


def _usage(d: dict) -> dict:
    u = d.get("usage") or {}
    return {"prompt_tokens": u.get("promptTokens", 0), "completion_tokens": u.get("completionTokens", 0),
            "total_tokens": u.get("totalTokens", 0)}


def _finish(reason: Any) -> str:
    r = str(reason or "stop").lower()
    return {"stop": "stop", "length": "length", "max_tokens": "length",
            "content_filter": "content_filter"}.get(r, "stop")


def _sse(obj: Any) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# --------------------------------------------------------------------------- #
# Body reading with size limits
# --------------------------------------------------------------------------- #
async def read_json(request: Request, limit: int) -> dict:
    too_big = BridgeError(413, f"Request body larger than {limit / (1024 * 1024):g} MB.", "request_too_large")
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > limit:
        raise too_big
    size, chunks = 0, []
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise too_big
        chunks.append(chunk)
    try:
        data = json.loads(b"".join(chunks) or b"{}")
    except ValueError as e:
        raise BridgeError(400, "Request body is not valid JSON.", "invalid_request_error") from e
    if not isinstance(data, dict):
        raise BridgeError(400, "Request body must be a JSON object.", "invalid_request_error")
    return data


SAFE_NAME_RE = re.compile(r"[^\w.\- ()\[\]]+", re.UNICODE)


async def read_upload(f: UploadFile, s: Settings) -> tuple[str, bytes, str]:
    raw_name = os.path.basename((f.filename or "upload").replace("\\", "/"))
    name = SAFE_NAME_RE.sub("_", raw_name).strip(" .")[:150] or "upload"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in s.upload_extensions:
        raise BridgeError(400, f"File type '.{ext}' is not allowed. Allowed: {', '.join(s.upload_extensions)}",
                          "invalid_request_error")
    data = bytearray()
    while True:
        chunk = await f.read(1024 * 1024)
        if not chunk:
            break
        data += chunk
        if len(data) > s.max_upload_bytes:
            raise BridgeError(413, f"File '{name}' is larger than {s.max_upload_bytes / (1024 * 1024):g} MB.",
                              "request_too_large")
    if not data:
        raise BridgeError(400, f"File '{name}' is empty.", "invalid_request_error")
    return name, bytes(data), (f.content_type or "application/octet-stream")


def _uuid(v: Any, what: str) -> str:
    try:
        return str(uuid.UUID(str(v)))
    except ValueError as e:
        raise BridgeError(400, f"Invalid {what} id.", "invalid_request_error") from e


def _check_len(name: str, v: Any, lo: int, hi: int) -> str:
    if not isinstance(v, str) or not lo <= len(v.strip()) <= hi:
        raise BridgeError(400, f"'{name}' must be a string of {lo}-{hi} characters.", "invalid_request_error")
    return v.strip()


# --------------------------------------------------------------------------- #
# MCP endpoint (optional): u:ai models as tools for AI agents
# --------------------------------------------------------------------------- #
MCP_INSTRUCTIONS = (
    "Tools backed by the u:ai API of the University of Vienna (Academic AI platform). 'ask' gets an answer "
    "from another language model (e.g. for a second opinion), 'web_research' answers with web sources "
    "(Perplexity sonar models), 'list_models' shows the available models and prices, 'usage' the estimated "
    "cost of the month. Every ask/web_research call is billed to the bridge operator's u:ai account: call "
    "them deliberately, not in loops, and send all needed context in one prompt (the models see no history)."
)


mt: Any = None       # mcp_types, bound by mount_mcp()
Context: Any = None  # mcp.server.mcpserver.Context, bound by mount_mcp()


class _McpGuard:
    """ASGI wrapper in front of the MCP transport: Origin check, bridge authentication, body limit.

    The resolved u:ai credentials travel to the tools in scope['state'], so static, trusted-CIDR and
    passthrough mode work exactly as on the REST endpoints.
    """

    def __init__(self, app: Any, settings: Settings, authenticate: Any):
        self.app, self.s, self.authenticate = app, settings, authenticate

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        request = Request(scope)
        origin = (request.headers.get("origin") or "").rstrip("/")
        try:
            # DNS-rebinding protection (MCP spec): browsers send Origin, native clients do not.
            if origin and origin not in self.s.mcp_allowed_origins:
                raise BridgeError(403, "Origin not allowed for /mcp (see BRIDGE_MCP_ALLOWED_ORIGINS).",
                                  "forbidden")
            creds = self.authenticate(request)
        except BridgeError as e:
            await e.response()(scope, receive, send)
            return
        scope.setdefault("state", {})["uai_creds"] = creds
        await self.app(scope, receive, send)


def mount_mcp(app: FastAPI, s: Settings, *, authenticate: Any, run_chat: Any, fetch_models: Any,
              price_sheet: Any, usage: UsageTracker | None) -> None:
    """Add a Streamable HTTP MCP endpoint at /mcp (stateless, JSON responses).

    Uses the official MCP Python SDK, which serves the stateless 2026-07-28 protocol and the older
    handshake-based revisions on the same endpoint.
    """
    # Imported only here: the SDK takes seconds to import and is not needed without MCP. The tool
    # signatures below are resolved against module globals (postponed annotations), hence `global`.
    global mt, Context
    try:
        import mcp_types as mt
        from mcp.server.mcpserver import Context, MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.routing import Route
    except ImportError as e:  # pragma: no cover - depends on the installation
        raise RuntimeError("BRIDGE_ENABLE_MCP=true needs the 'mcp' package (pip install -r requirements.txt).") from e

    budget = DailyCallBudget(s.mcp_max_calls_per_day, s.usage_tz)
    server = MCPServer(name="uai-bridge", title="u:ai bridge", version=__version__, instructions=MCP_INSTRUCTIONS)
    ro = mt.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    billed = mt.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)

    def creds_of(ctx: Any) -> Creds:
        creds = ctx.request_context.request.scope.get("state", {}).get("uai_creds")
        if not creds:  # only reachable if the guard was bypassed
            raise ToolError("Not authenticated.")
        return creds

    def spend(creds: Creds) -> None:
        if not budget.take(creds[0]):
            raise ToolError(f"Daily limit of {s.mcp_max_calls_per_day} ask/web_research calls reached for this "
                            f"u:ai client (BRIDGE_MCP_MAX_CALLS_PER_DAY); it resets at midnight {s.usage_tz}.")

    async def token_limit(creds: Creds, model: str, requested: int | None) -> int | None:
        if requested is not None and not 1 <= requested <= s.mcp_max_tokens:
            raise ToolError(f"max_tokens must be between 1 and {s.mcp_max_tokens} (BRIDGE_MCP_MAX_TOKENS).")
        n = requested or s.mcp_max_tokens
        try:  # never ask for more than the model can produce
            info = next((m for m in await fetch_models(creds)
                         if str(m.get("modelName", "")).lower() == model.lower()), None)
        except BridgeError:
            info = None
        limit = (info or {}).get("outputTokenLimit")
        return min(n, int(limit)) if isinstance(limit, int) and limit > 0 else n

    async def complete(creds: Creds, model_in: str | None, messages: list, max_tokens: int | None,
                       temperature: float | None = None, reasoning_effort: str | None = None) -> Any:
        try:
            model, _ = resolve_model(model_in, s)
            params = translate_params(model, temperature=temperature,
                                      max_tokens=await token_limit(creds, model, max_tokens),
                                      reasoning_effort=reasoning_effort)
        except BridgeError as e:
            raise ToolError(e.message) from None
        spend(creds)
        try:
            r = await run_chat(creds, model_in, messages, params)
        except BridgeError as e:
            raise ToolError(f"u:ai error ({e.status}): {e.message}") from None
        text = r["content"] or "(empty answer)"
        if r["finish"] == "length":
            text += "\n\n[The answer was cut off at max_tokens.]"
        data = {"model": r["model"], "answer": r["content"], "finish_reason": r["finish"], "usage": r["usage"],
                "citations": r["citations"]}
        if r["adjusted"]:
            data["adjusted"] = r["adjusted"]
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=text)], structured_content=data)

    def structured(data: dict) -> Any:
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=json.dumps(data, indent=1))],
                                 structured_content=data)

    def check_text(name: str, v: str | None, required: bool = True) -> str | None:
        if v is None or not v.strip():
            if required:
                raise ToolError(f"'{name}' must not be empty.")
            return None
        if len(v.encode()) > s.max_request_bytes:
            raise ToolError(f"'{name}' is larger than BRIDGE_MAX_REQUEST_MB.")
        return v

    @server.tool(annotations=billed, description=(
        "Ask a u:ai language model and return its answer, e.g. for a second opinion or a review. The model "
        "sees only this prompt (and the optional system text), so include all context. Use list_models for "
        "model names; 'tailored:<id>' addresses a Tailored AI with its knowledge base. Billed per call."))
    async def ask(
        prompt: Annotated[str, Field(description="The complete question or task, with all needed context.")],
        ctx: Context,
        model: Annotated[str | None, Field(description="u:ai model name, e.g. gpt-5 or a name from "
                                                       "list_models; default: the bridge default model.")] = None,
        system: Annotated[str | None, Field(description="Optional instructions for the model.")] = None,
        max_tokens: Annotated[int | None, Field(description="Upper bound for the answer length in tokens.")] = None,
        temperature: Annotated[float | None, Field(description="0-2; some models ignore it.")] = None,
        reasoning_effort: Annotated[Literal["low", "medium", "high"] | None,
                                    Field(description="For reasoning models (o-series, gpt-5).")] = None,
    ) -> mt.CallToolResult:
        creds = creds_of(ctx)
        messages = ([{"role": "system", "content": check_text("system", system, False)}] if system else [])
        messages.append({"role": "user", "content": check_text("prompt", prompt)})
        return await complete(creds, model, messages, max_tokens, temperature, reasoning_effort)

    @server.tool(annotations=billed, description=(
        "Research a question on the web with a Perplexity sonar model on u:ai and return the answer with "
        "its web sources (citations with URLs). sonar-pro is fast; sonar-deep-research is thorough but slow "
        "and more expensive. Billed per call."))
    async def web_research(
        query: Annotated[str, Field(description="The research question, with enough context.")],
        ctx: Context,
        model: Annotated[str | None, Field(description="A sonar-* model, e.g. sonar-pro or "
                                                       "sonar-deep-research; default: the bridge setting.")] = None,
        max_tokens: Annotated[int | None, Field(description="Upper bound for the answer length in tokens.")] = None,
    ) -> mt.CallToolResult:
        creds = creds_of(ctx)
        m = (model or s.mcp_research_model).strip()
        if not m.rsplit("/", 1)[-1].lower().startswith("sonar"):
            raise ToolError("web_research needs a sonar-* model; use 'ask' for other models.")
        return await complete(creds, m, [{"role": "user", "content": check_text("query", query)}], max_tokens)

    @server.tool(annotations=ro, description=(
        "List the u:ai models available to this account with context window, output limit and prices "
        "(USD per 1M tokens, plus a per-request fee for web-search models). Free."))
    async def list_models(ctx: Context) -> mt.CallToolResult:
        creds = creds_of(ctx)
        try:
            sheet = await price_sheet(creds)
        except BridgeError as e:
            raise ToolError(f"u:ai error ({e.status}): {e.message}") from None
        return structured({"currency": "USD", "default_model": s.default_model,
                           "research_model": s.mcp_research_model,
                           "models": [{"name": k, **v} for k, v in sorted(sheet.items()) if k]})

    if usage is not None:
        @server.tool(name="usage", annotations=ro, description=(
            "Requests, tokens and estimated USD cost of this u:ai account for a month (default: the "
            "current month, with a linear projection), plus today's MCP calls and the daily cap. Free."))
        async def usage_report(
            ctx: Context,
            month: Annotated[str | None, Field(description="YYYY-MM; default: the current month.")] = None,
        ) -> mt.CallToolResult:
            creds = creds_of(ctx)
            if month is not None and not MONTH_RE.match(month):
                raise ToolError("'month' must look like YYYY-MM.")
            rep = usage.report(creds[0], month)
            rep["mcp_calls_today"] = budget.used(creds[0])
            rep["mcp_daily_limit"] = s.mcp_max_calls_per_day or None
            return structured(rep)

    starlette_app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        max_request_body_size=s.max_request_bytes,
        # Host/Origin are checked by _McpGuard; the SDK's Host allow-list would break LAN and proxy setups.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    endpoint = starlette_app.routes[0].endpoint
    app.router.routes.append(Route("/mcp", endpoint=_McpGuard(endpoint, s, authenticate),
                                   methods=["GET", "POST", "DELETE"]))
    app.state.mcp_session_manager = server.session_manager
    app.state.mcp_budget = budget


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    s = settings or Settings.from_env()
    s.validate()
    uai = UaiClient(s, transport=transport)
    models_cache: dict[str, tuple[float, list]] = {}
    usage = UsageTracker(s.usage_file, s.usage_tz) if s.usage_tracking else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("uai-bridge %s started (mode=%s, kb=%s, upstream=%s)", __version__, s.credential_mode,
                 s.enable_kb, s.base_url)
        if usage is not None:
            log.info("usage counter: %s (month time zone %s)",
                     usage.path if usage.persistent else "in memory only (resets on restart)", s.usage_tz)
        async with AsyncExitStack() as stack:
            mgr = getattr(_app.state, "mcp_session_manager", None)
            if mgr is not None:
                await stack.enter_async_context(mgr.run())
                log.info("MCP endpoint enabled at /mcp (cap %s calls/day per u:ai client)",
                         s.mcp_max_calls_per_day or "no")
            yield
        await uai.close()

    app = FastAPI(title="u:ai bridge", version=__version__, lifespan=lifespan,
                  docs_url="/docs" if s.enable_docs else None, redoc_url=None,
                  openapi_url="/openapi.json" if s.enable_docs else None)
    app.state.settings = s
    app.state.uai = uai
    app.state.usage = usage

    # ---- error handlers ---------------------------------------------------
    @app.exception_handler(BridgeError)
    async def _bridge_err(_req: Request, exc: BridgeError):
        return exc.response()

    @app.exception_handler(StarletteHTTPException)
    async def _http_err(_req: Request, exc: StarletteHTTPException):
        return BridgeError(exc.status_code, str(exc.detail), "http_error").response()

    @app.exception_handler(RequestValidationError)
    async def _val_err(_req: Request, exc: RequestValidationError):
        fields = ", ".join(".".join(str(x) for x in e.get("loc", [])) for e in exc.errors()[:5])
        return BridgeError(400, f"Invalid or missing fields: {fields}", "invalid_request_error").response()

    @app.exception_handler(Exception)
    async def _unhandled(_req: Request, exc: Exception):
        log.error("Unhandled error: %s", type(exc).__name__)
        return BridgeError(500, "Internal bridge error.", "internal_error").response()

    # ---- access log (no bodies, no secrets) --------------------------------
    @app.middleware("http")
    async def _access_log(request: Request, call_next):
        t0 = time.perf_counter()
        rid = uuid.uuid4().hex[:12]
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        log.info("%s %s %s %d %.0fms", rid, request.method, request.url.path, response.status_code,
                 (time.perf_counter() - t0) * 1000)
        return response

    # ---- authentication ----------------------------------------------------
    def _token(request: Request) -> str | None:
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return request.headers.get("x-api-key")

    def _trusted(request: Request) -> bool:
        if not s.trusted_cidrs or request.client is None:
            return False
        try:
            ip = ipaddress.ip_address(request.client.host)
        except ValueError:
            return False
        return any(ip in net for net in s.trusted_cidrs)

    def authenticate(request: Request) -> Creds:
        if s.credential_mode == "passthrough":
            cid, sep, secret = (_token(request) or "").partition(":")
            if not sep or not cid.strip() or not secret.strip():
                raise BridgeError(401, "Send your own u:ai credentials as "
                                       "'Authorization: Bearer <CLIENT_ID>:<CLIENT_SECRET>'.",
                                  "authentication_error", headers={"WWW-Authenticate": "Bearer"})
            return cid.strip(), secret.strip()
        creds = (s.client_id or "", s.client_secret or "")
        if s.allow_no_auth or _trusted(request):
            return creds
        tok = _token(request)
        if tok and any(hmac.compare_digest(tok.encode(), k.encode()) for k in s.api_keys):
            return creds
        raise BridgeError(401, "Missing or invalid bridge API key.", "authentication_error",
                          headers={"WWW-Authenticate": "Bearer"})

    def require_kb() -> None:
        if not s.enable_kb:
            raise BridgeError(404, "Not found (knowledge-base features are disabled; set BRIDGE_ENABLE_KB=true).",
                              "not_found_error")

    # ---- shared chat core --------------------------------------------------
    async def run_chat(creds: Creds, model_in: Any, messages: Any, params: dict, tools: Any = None,
                       tool_choice: Any = None) -> dict:
        model, tailored_id = resolve_model(model_in, s)
        instruction, allowed = build_tool_instruction(tools, tool_choice)
        want_json = s.enforce_json and (params.get("responseFormat") or {}).get("type") == "json_object"
        if want_json and not allowed:
            instruction = JSON_INSTRUCTION
        required = bool(allowed) and (tool_choice == "required" or isinstance(tool_choice, dict))
        conv = to_uai_messages(messages, s.image_mode, instruction, TOOL_REMINDER if allowed else "")
        payload: dict[str, Any] = {"model": model, "messages": conv}
        if tailored_id:
            payload["tailoredAiId"] = tailored_id
        payload.update(params)
        adjusted: list[str] = []
        if model.lower().startswith("gemini") and s.gemini_min_max_tokens > 0:
            if 0 < payload.get("maxTokens", s.gemini_min_max_tokens) < s.gemini_min_max_tokens:
                payload["maxTokens"] = s.gemini_min_max_tokens
                adjusted.append(f"maxTokens->{s.gemini_min_max_tokens}")

        async def post() -> tuple[dict, str, dict]:
            try:
                res = await uai.request("POST", "/api/v1/llm/chat", creds, json_body=payload)
            except BridgeError as e:
                # Some models reject individual parameters (e.g. claude-opus-4-6: 'temperature'). A rejected
                # request was not processed, so retrying once without that field is safe.
                f = getattr(e, "rejected_field", None)
                if e.status == 400 and f and f in payload and f not in ("model", "messages", "tailoredAiId"):
                    payload.pop(f)
                    adjusted.append(f"dropped:{f}")
                    log.info("u:ai rejected field %r for model %s; retrying without it", f, model)
                    res = await uai.request("POST", "/api/v1/llm/chat", creds, json_body=payload)
                else:
                    raise
            d = res.get("data") or {}
            # Plain chat returns data.content; Tailored-AI (RAG) chat returns data.message.content and no
            # usage/finishReason (observed live, differs from the API docs v1.0.7).
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            u = _usage(d)
            if usage is not None:
                usage.record(creds[0], model, u, await estimate_cost(creds, model, u))
            return d, (d.get("content") or msg.get("content") or "").strip(), u

        d, content, u = await post()
        tp = parse_tool_reply(content, allowed)
        tool_status = "none" if not allowed else ("parsed" if tp.calls else ("failed" if tp.error else "text"))
        for _ in range(s.tool_repair_retries):
            # The model tried to call a tool but the call is unusable (invalid JSON, unknown tool, ...), or a
            # required call is missing. Re-asking is a new, deliberate request - it is billed like any other.
            if tp.calls or not (tp.error or required):
                break
            log.info("emulated tool call unusable for model %s (%s); asking for a corrected call",
                     model, "required call missing" if not tp.error else tp.error.split(";")[0][:80])
            payload["messages"] = conv + [{"role": "assistant", "content": content or "(empty reply)"},
                                          {"role": "user", "content": "### SYSTEM INSTRUCTION ###\n"
                                           + tool_repair_message(tp.error, required)}]
            d, content2, u2 = await post()
            u = {k: u.get(k, 0) + u2.get(k, 0) for k in set(u) | set(u2)}
            tp2 = parse_tool_reply(content2, allowed)
            if tp2.calls:
                tp, content, tool_status = tp2, content2, "repaired"
            elif not tp.error:          # required call still missing: return the newer answer
                tp, content = tp2, content2
        tool_calls = tp.calls
        if tool_calls:
            content = tp.content       # a short preamble before the call, if any
        if want_json and not tool_calls:
            content = extract_json_object(content) or content
        cit_text, citations = format_citations(d.get("citations"))
        if cit_text and s.append_citations and not tool_calls and not want_json:
            content += cit_text
        return {"model": model, "content": content, "tool_calls": tool_calls, "citations": citations,
                "finish": "tool_calls" if tool_calls else _finish(d.get("finishReason")), "usage": u,
                "adjusted": adjusted, "tool_status": tool_status}

    # ---- health ------------------------------------------------------------
    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__}

    # ---- models & pricing --------------------------------------------------
    async def fetch_models(creds: Creds) -> list:
        hit = models_cache.get(creds[0])
        if hit and time.monotonic() - hit[0] < s.models_cache_ttl:
            return hit[1]
        res = await uai.request("GET", "/api/v1/llm/models", creds, timeout=30.0)
        data = [m for m in (res.get("data") or []) if isinstance(m, dict)]
        models_cache[creds[0]] = (time.monotonic(), data)
        return data

    @app.get("/v1/models")
    async def list_models(request: Request):
        creds = authenticate(request)
        out = [{"id": m.get("modelName"), "object": "model", "created": 0, "owned_by": "u:ai",
                "context_window": m.get("contextWindow"), "max_output_tokens": m.get("outputTokenLimit")}
               for m in await fetch_models(creds) if m.get("modelName")]
        return {"object": "list", "data": out}

    async def price_sheet(creds: Creds) -> dict:
        factor = 1_000_000 / s.cost_unit_tokens

        def per_m(v):  # u:ai reports USD per BRIDGE_COST_UNIT_TOKENS tokens (1 000, observed live)
            return round(float(v) * factor, 4) if v is not None else None

        sheet = {}
        for m in await fetch_models(creds):
            costs = {c.get("costType"): c.get("cost") for c in m.get("costs") or [] if isinstance(c, dict)}
            sheet[m.get("modelName")] = {
                "context_window": m.get("contextWindow"),
                "output_token_limit": m.get("outputTokenLimit"),
                "input_usd_per_1m_tokens": per_m(costs.get("input_tokens")),
                "output_usd_per_1m_tokens": per_m(costs.get("output_tokens")),
                "usd_per_request": costs.get("per_request"),
            }
        return sheet

    @app.get("/v1/pricing")
    async def pricing(request: Request):
        creds = authenticate(request)
        return {"currency": "USD", "unit": "per 1M tokens", "models": await price_sheet(creds)}

    # ---- monthly usage counter ---------------------------------------------
    async def estimate_cost(creds: Creds, model: str, u: dict) -> float | None:
        """USD for one reply from the (cached) u:ai price list; None if no price is known."""
        if not u.get("total_tokens"):
            return None
        try:
            models = await fetch_models(creds)
        except BridgeError:
            return None
        m = next((x for x in models if str(x.get("modelName", "")).lower() == model.lower()), None)
        if not m:
            return None
        costs = {c.get("costType"): c.get("cost") for c in m.get("costs") or [] if isinstance(c, dict)}
        try:
            pin, pout = float(costs["input_tokens"]), float(costs["output_tokens"])
            # Web-search models (sonar-*) also have a flat fee per request, in USD per single request
            # (observed live: sonar-pro 0.014, matching the provider's 14 USD per 1 000 requests).
            per_req = float(costs.get("per_request") or 0.0)
        except (KeyError, TypeError, ValueError):
            return None
        return ((u.get("prompt_tokens", 0) * pin + u.get("completion_tokens", 0) * pout) / s.cost_unit_tokens
                + per_req)

    def require_usage() -> UsageTracker:
        if usage is None:
            raise BridgeError(404, "Not found (usage tracking is disabled; set BRIDGE_USAGE_TRACKING=true).",
                              "not_found_error")
        return usage

    @app.get("/v1/usage")
    async def usage_month(request: Request, month: str | None = None):
        creds = authenticate(request)
        t = require_usage()
        if month is not None and not MONTH_RE.match(month):
            raise BridgeError(400, "'month' must look like YYYY-MM.", "invalid_request_error")
        return t.report(creds[0], month)

    @app.get("/v1/usage/history")
    async def usage_history(request: Request):
        creds = authenticate(request)
        t = require_usage()
        rows = []
        for m in t.months(creds[0]):
            r = t.report(creds[0], m)
            rows.append({k: r[k] for k in ("month", "requests", "prompt_tokens", "completion_tokens", "total_tokens",
                                           "estimated_cost_usd", "requests_without_usage", "requests_without_price")})
        return {"timezone": t.tz_name, "currency": "USD", "months": rows}

    # ---- OpenAI chat -------------------------------------------------------
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        creds = authenticate(request)
        body = await read_json(request, s.max_request_bytes)
        if body.get("n") not in (None, 1):
            raise BridgeError(400, "'n' > 1 is not supported by u:ai.", "invalid_request_error")
        tools = body.get("tools")
        if not tools and isinstance(body.get("functions"), list):  # legacy function calling
            tools = [{"type": "function", "function": f} for f in body["functions"] if isinstance(f, dict)]
        model, _ = resolve_model(body.get("model"), s)
        params = translate_params(
            model, temperature=body.get("temperature"),
            max_tokens=body.get("max_completion_tokens", body.get("max_tokens")),
            frequency_penalty=body.get("frequency_penalty"), presence_penalty=body.get("presence_penalty"),
            stop=body.get("stop"), seed=body.get("seed"), response_format=body.get("response_format"),
            reasoning_effort=body.get("reasoning_effort"), verbosity=body.get("verbosity"))
        ignored = sorted(k for k in body if k not in SUPPORTED_OPENAI_PARAMS)
        r = await run_chat(creds, body.get("model"), body.get("messages"), params, tools,
                           body.get("tool_choice", body.get("function_call")))
        cid, created = f"chatcmpl-{uuid.uuid4().hex[:24]}", int(time.time())
        label = body.get("model") or r["model"]
        message: dict[str, Any] = {"role": "assistant",
                                   "content": (r["content"] or None) if r["tool_calls"] else r["content"]}
        if r["tool_calls"]:
            message["tool_calls"] = r["tool_calls"]
        headers = {"X-Bridge-Ignored-Params": ",".join(ignored)[:500]} if ignored else {}
        if r["adjusted"]:
            headers["X-Bridge-Adjusted-Params"] = ",".join(r["adjusted"])[:500]
        if r["tool_status"] != "none":
            headers["X-Bridge-Tool-Calls"] = r["tool_status"]

        if body.get("stream"):
            include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

            def chunk(delta: dict, finish: str | None = None) -> dict:
                return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": label,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

            async def gen():
                # u:ai cannot stream; the complete answer is sent as one content chunk.
                yield _sse(chunk({"role": "assistant", "content": ""}))
                if r["tool_calls"]:
                    if r["content"]:
                        yield _sse(chunk({"content": r["content"]}))
                    yield _sse(chunk({"tool_calls": [dict(tc, index=i) for i, tc in enumerate(r["tool_calls"])]}))
                elif r["content"]:
                    yield _sse(chunk({"content": r["content"]}))
                yield _sse(chunk({}, r["finish"]))
                if include_usage:
                    yield _sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                                "model": label, "choices": [], "usage": r["usage"]})
                yield "data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", **headers})

        resp: dict[str, Any] = {"id": cid, "object": "chat.completion", "created": created, "model": label,
                                "choices": [{"index": 0, "message": message, "finish_reason": r["finish"]}],
                                "usage": r["usage"]}
        if r["citations"]:
            resp["citations"] = r["citations"]
        return JSONResponse(resp, headers=headers)

    # ---- Ollama compatibility (clients that speak the Ollama API) ----------
    @app.get("/api/tags")
    async def ollama_tags(request: Request):
        creds = authenticate(request)
        models = [{"name": m.get("modelName"), "model": m.get("modelName"),
                   "modified_at": "1970-01-01T00:00:00Z", "size": 0, "digest": "",
                   "details": {"format": "remote", "family": "u:ai", "families": ["u:ai"],
                               "parameter_size": "unknown", "quantization_level": "none"}}
                  for m in await fetch_models(creds) if m.get("modelName")]
        return {"models": models}

    @app.get("/api/version")
    async def ollama_version():
        return {"version": f"0.0.0-uai-bridge-{__version__}"}

    @app.post("/api/chat")
    async def ollama_chat(request: Request):
        creds = authenticate(request)
        body = await read_json(request, s.max_request_bytes)
        opts = body.get("options") or {}
        if not isinstance(opts, dict):
            raise BridgeError(400, "'options' must be an object.", "invalid_request_error")
        model, _ = resolve_model(body.get("model"), s)
        fmt = body.get("format")
        params = translate_params(
            model, temperature=opts.get("temperature"), max_tokens=opts.get("num_predict"),
            frequency_penalty=opts.get("frequency_penalty"), presence_penalty=opts.get("presence_penalty"),
            stop=opts.get("stop"), seed=opts.get("seed"),
            response_format={"type": "json_object"} if (fmt == "json" or isinstance(fmt, dict)) else None)
        messages = body.get("messages")
        if not messages and body.get("prompt"):
            messages = [{"role": "user", "content": body["prompt"]}]
        r = await run_chat(creds, body.get("model"), messages, params, body.get("tools"), None)
        label = body.get("model") or r["model"]
        msg: dict[str, Any] = {"role": "assistant", "content": r["content"] or ""}
        if r["tool_calls"]:
            msg["tool_calls"] = [{"function": {"name": tc["function"]["name"],
                                               "arguments": json.loads(tc["function"]["arguments"])}}
                                 for tc in r["tool_calls"]]
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        final = {"model": label, "created_at": now, "message": msg, "done": True,
                 "done_reason": "length" if r["finish"] == "length" else "stop",
                 "prompt_eval_count": r["usage"]["prompt_tokens"], "eval_count": r["usage"]["completion_tokens"]}
        if body.get("stream", True) is False:
            return final

        async def gen():  # Ollama streams NDJSON by default
            yield json.dumps({"model": label, "created_at": now, "message": msg, "done": False}) + "\n"
            yield json.dumps(dict(final, message={"role": "assistant", "content": ""})) + "\n"

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    # ---- knowledge bases & Tailored AIs (opt-in) ---------------------------
    async def wait_until_ready(creds: Creds, kb_id: str) -> dict:
        deadline = time.monotonic() + s.ingest_timeout
        while True:
            st = (await uai.request("GET", f"/api/v1/knowledge-base/{kb_id}/status", creds,
                                    timeout=30.0)).get("data") or {}
            state = str(st.get("state", "")).lower()
            if state == "ready":
                return st
            if state in ("failed", "to_delete"):
                raise BridgeError(502, f"Ingestion of knowledge base {kb_id} ended in state '{state}'.",
                                  "ingestion_failed", extra={"knowledge_base_id": kb_id,
                                                             "ingestion_errors": st.get("errors", [])})
            if time.monotonic() > deadline:
                raise BridgeError(504, f"Ingestion not finished after {int(s.ingest_timeout)} s; poll "
                                       f"/v1/uai/knowledge-bases/{kb_id}/status and create/link the "
                                       "Tailored AI when it is ready.",
                                  "ingestion_timeout", extra={"knowledge_base_id": kb_id, "state": state})
            await uai.sleep(s.ingest_poll_interval)

    async def upload_files(creds: Creds, kb_id: str, files: list[UploadFile]) -> list[dict]:
        if not files:
            raise BridgeError(400, "Upload at least one file (form field 'files').", "invalid_request_error")
        done = []
        for f in files:
            name, data, ctype = await read_upload(f, s)
            res = await uai.request("POST", f"/api/v1/knowledge-base/{kb_id}/files", creds,
                                    files={"file": (name, data, ctype)}, timeout=max(s.upstream_timeout, 120.0))
            done.append({"name": name, "document_id": (res.get("data") or {}).get("id")})
        return done

    async def best_effort_delete(creds: Creds, path: str) -> None:
        try:
            await uai.request("DELETE", path, creds, timeout=30.0, retry=False)
        except BridgeError as e:
            log.warning("Cleanup of %s failed: %s", path, e.message)

    @app.get("/v1/uai/knowledge-bases")
    async def kb_list(request: Request):
        require_kb()
        return await uai.request("GET", "/api/v1/knowledge-base/", authenticate(request), timeout=30.0)

    @app.post("/v1/uai/knowledge-bases")
    async def kb_create(request: Request):
        require_kb()
        creds = authenticate(request)
        body = await read_json(request, 64 * 1024)
        name = _check_len("name", body.get("name"), 3, 50)
        return await uai.request("POST", "/api/v1/knowledge-base/", creds, json_body={"name": name},
                                 timeout=30.0, retry=False)

    @app.get("/v1/uai/knowledge-bases/{kb_id}")
    async def kb_get(kb_id: str, request: Request):
        require_kb()
        return await uai.request("GET", f"/api/v1/knowledge-base/{_uuid(kb_id, 'knowledge base')}",
                                 authenticate(request), timeout=30.0)

    @app.get("/v1/uai/knowledge-bases/{kb_id}/status")
    async def kb_status(kb_id: str, request: Request):
        require_kb()
        return await uai.request("GET", f"/api/v1/knowledge-base/{_uuid(kb_id, 'knowledge base')}/status",
                                 authenticate(request), timeout=30.0)

    async def _add_documents(request: Request, kb_id: str, files: list[UploadFile], ingest: bool) -> dict:
        require_kb()
        creds = authenticate(request)
        kb_id = _uuid(kb_id, "knowledge base")
        uploaded = await upload_files(creds, kb_id, files)
        if ingest:
            await uai.request("POST", f"/api/v1/knowledge-base/{kb_id}/ingest", creds, timeout=60.0)
        return {"status": "ingestion_started" if ingest else "uploaded", "knowledge_base_id": kb_id,
                "documents": uploaded, "next": f"GET /v1/uai/knowledge-bases/{kb_id}/status"}

    @app.post("/v1/uai/knowledge-bases/{kb_id}/documents")
    async def kb_add_documents(kb_id: str, request: Request, files: list[UploadFile] = File(...),
                               ingest: bool = Form(True)):
        return await _add_documents(request, kb_id, files, ingest)

    @app.delete("/v1/uai/knowledge-bases/{kb_id}/documents/{doc_id}")
    async def kb_delete_document(kb_id: str, doc_id: str, request: Request):
        require_kb()
        creds = authenticate(request)
        kb_id, doc_id = _uuid(kb_id, "knowledge base"), _uuid(doc_id, "document")
        await uai.request("DELETE", f"/api/v1/knowledge-base/{kb_id}/files/{doc_id}", creds,
                          timeout=30.0, retry=False)
        # The search index is only cleaned up by a new ingestion (API docs v1.0.7).
        await uai.request("POST", f"/api/v1/knowledge-base/{kb_id}/ingest", creds, timeout=60.0)
        return {"status": "deleted_reingesting", "knowledge_base_id": kb_id, "document_id": doc_id}

    @app.delete("/v1/uai/knowledge-bases/{kb_id}")
    async def kb_delete(kb_id: str, request: Request):
        require_kb()
        return await uai.request("DELETE", f"/api/v1/knowledge-base/{_uuid(kb_id, 'knowledge base')}",
                                 authenticate(request), timeout=30.0, retry=False)

    @app.get("/v1/uai/tailored-ais")
    async def tai_list(request: Request):
        require_kb()
        return await uai.request("GET", "/api/v1/tailored-ai/", authenticate(request), timeout=30.0)

    @app.post("/v1/uai/tailored-ais")
    async def tai_create(request: Request):
        require_kb()
        creds = authenticate(request)
        body = await read_json(request, 64 * 1024)
        payload = {"name": _check_len("name", body.get("name"), 1, 50),
                   "summary": _check_len("summary", body.get("summary"), 1, 100),
                   "systemPrompt": _check_len("system_prompt", body.get("system_prompt"), 1, 2000)}
        kb_id = _uuid(body["knowledge_base_id"], "knowledge base") if body.get("knowledge_base_id") else None
        if kb_id:  # the KB must be ready before linking (API docs v1.0.7)
            await wait_until_ready(creds, kb_id)
        res = await uai.request("POST", "/api/v1/tailored-ai/", creds, json_body=payload, timeout=30.0,
                                retry=False)
        tai_id = (res.get("data") or {}).get("id")
        if kb_id and tai_id:
            await uai.request("POST", f"/api/v1/tailored-ai/{tai_id}/knowledge-base", creds,
                              json_body={"knowledgeBaseId": kb_id}, timeout=30.0)
        return {"tailored_ai_id": tai_id, "knowledge_base_id": kb_id, "model": f"tailored:{tai_id}"}

    @app.post("/v1/uai/tailored-ais/{tai_id}/knowledge-base")
    async def tai_link(tai_id: str, request: Request):
        require_kb()
        creds = authenticate(request)
        body = await read_json(request, 64 * 1024)
        kb = body.get("knowledge_base_id")
        kb = _uuid(kb, "knowledge base") if kb else None
        return await uai.request("POST", f"/api/v1/tailored-ai/{_uuid(tai_id, 'Tailored AI')}/knowledge-base",
                                 creds, json_body={"knowledgeBaseId": kb}, timeout=30.0)

    @app.delete("/v1/uai/tailored-ais/{tai_id}")
    async def tai_delete(tai_id: str, request: Request):
        require_kb()
        return await uai.request("DELETE", f"/api/v1/tailored-ai/{_uuid(tai_id, 'Tailored AI')}",
                                 authenticate(request), timeout=30.0, retry=False)

    @app.post("/v1/uai/experts")
    async def create_expert(request: Request, name: str = Form(...), summary: str = Form(...),
                            system_prompt: str = Form(...), files: list[UploadFile] = File(...)):
        """One call: KB -> upload -> ingest -> wait until ready -> Tailored AI -> link (uploads only)."""
        require_kb()
        creds = authenticate(request)
        name = _check_len("name", name, 1, 50)
        summary = _check_len("summary", summary, 1, 100)
        system_prompt = _check_len("system_prompt", system_prompt, 1, 2000)
        kb_name = f"KB {name}"[:50]
        kb = await uai.request("POST", "/api/v1/knowledge-base/", creds, json_body={"name": kb_name},
                               timeout=30.0, retry=False)
        kb_id = (kb.get("data") or {}).get("id")
        if not kb_id:
            raise BridgeError(502, "u:ai did not return a knowledge base id.", "upstream_error")
        tai_id = None
        try:
            uploaded = await upload_files(creds, kb_id, files)
            await uai.request("POST", f"/api/v1/knowledge-base/{kb_id}/ingest", creds, timeout=60.0)
            await wait_until_ready(creds, kb_id)
            tai = await uai.request("POST", "/api/v1/tailored-ai/", creds, timeout=30.0, retry=False,
                                    json_body={"name": name, "summary": summary, "systemPrompt": system_prompt})
            tai_id = (tai.get("data") or {}).get("id")
            if not tai_id:
                raise BridgeError(502, "u:ai did not return a Tailored AI id.", "upstream_error")
            await uai.request("POST", f"/api/v1/tailored-ai/{tai_id}/knowledge-base", creds,
                              json_body={"knowledgeBaseId": kb_id}, timeout=30.0)
        except BridgeError as e:
            if e.type == "ingestion_timeout":
                raise  # keep the KB; finish later with POST /v1/uai/tailored-ais
            if tai_id:  # deleting a Tailored AI also deletes its linked KB (observed live)
                await best_effort_delete(creds, f"/api/v1/tailored-ai/{tai_id}")
            await best_effort_delete(creds, f"/api/v1/knowledge-base/{kb_id}")
            raise
        return {"status": "ready", "tailored_ai_id": tai_id, "knowledge_base_id": kb_id,
                "documents": uploaded, "model": f"tailored:{tai_id}"}

    # ---- MCP endpoint (opt-in) ----------------------------------------------
    if s.enable_mcp:
        mount_mcp(app, s, authenticate=authenticate, run_chat=run_chat, fetch_models=fetch_models,
                  price_sheet=price_sheet, usage=usage)

    return app


# --------------------------------------------------------------------------- #
def main() -> None:
    logging.basicConfig(level=os.getenv("BRIDGE_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = Settings.from_env()
    import uvicorn
    uvicorn.run(create_app(s), host=s.host, port=s.port, proxy_headers=False, server_header=False,
                access_log=False)


if __name__ == "__main__":
    main()
