"""Tests for uai_bridge against a mocked u:ai API (no network, no credit used)."""
import json
import os
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import uai_bridge as b  # noqa: E402

KEY = "test-key-0123456789abcdefghijklmnop"
AUTH = {"Authorization": f"Bearer {KEY}"}
KB = "8083de83-ce52-43e0-bbe6-234ef7d7dbcf"
TAI = "7c26da0a-71cc-4e8f-8773-9937dda8de15"


class Upstream:
    """Scriptable fake of the u:ai API."""

    def __init__(self):
        self.calls = []
        self.chat_reply = {"content": "Hello!", "finishReason": "stop",
                           "usage": {"promptTokens": 3, "completionTokens": 2, "totalTokens": 5}}
        self.queue = {}        # path -> list of responses/exceptions to return first
        self.kb_states = ["processing", "ready"]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = None
        if req.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(req.content or b"null")
        self.calls.append((req.method, req.url.path, body, dict(req.headers)))
        q = self.queue.get(req.url.path)
        if q:
            item = q.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        p = req.url.path
        if p == "/api/v1/llm/chat":
            if body.get("tailoredAiId"):  # RAG replies have a different shape (observed live)
                d = {"model": body["model"], "message": {"role": "assistant", "content": self.chat_reply["content"]},
                     "citations": self.chat_reply.get("citations", []),
                     "citationCount": len(self.chat_reply.get("citations", []))}
                return httpx.Response(200, json={"data": d})
            return httpx.Response(200, json={"data": dict(self.chat_reply, model=body["model"], role="assistant")})
        if p == "/api/v1/llm/models":
            return httpx.Response(200, json={"data": [
                {"modelName": "gpt-4o", "contextWindow": 128000, "outputTokenLimit": 16384,
                 "costs": [{"cost": 0.0025, "costType": "input_tokens"},      # USD per 1 000 tokens
                           {"cost": 0.01, "costType": "output_tokens"}]}]})
        if p == "/api/v1/knowledge-base/" and req.method == "POST":
            return httpx.Response(201, json={"data": {"id": KB, "name": body["name"], "state": "created"}})
        if p == f"/api/v1/knowledge-base/{KB}/files":
            return httpx.Response(201, json={"data": {"id": "11111111-1111-1111-1111-111111111111", "name": "x"}})
        if p == f"/api/v1/knowledge-base/{KB}/ingest":
            return httpx.Response(202, json={"message": "started"})
        if p == f"/api/v1/knowledge-base/{KB}/status":
            st = self.kb_states.pop(0) if len(self.kb_states) > 1 else self.kb_states[0]
            return httpx.Response(200, json={"data": {"id": KB, "state": st, "errors": [], "progress": None}})
        if p == "/api/v1/tailored-ai/" and req.method == "POST":
            return httpx.Response(201, json={"data": {"id": TAI, **body}})
        if p == f"/api/v1/tailored-ai/{TAI}/knowledge-base":
            return httpx.Response(200, json={"message": "ok"})
        if req.method == "DELETE":
            return httpx.Response(200, json={"message": "deleted"})
        return httpx.Response(404, json={"message": "not found"})

    def chat_payloads(self):
        return [c[2] for c in self.calls if c[1] == "/api/v1/llm/chat"]


def make(**overrides):
    up = Upstream()
    cfg = dict(client_id="cid", client_secret="csecret", api_keys=(KEY,), max_retries=2, rate_limit_rpm=0)
    cfg.update(overrides)
    app = b.create_app(b.Settings(**cfg), transport=httpx.MockTransport(up))

    async def no_sleep(_):
        return None
    app.state.uai.sleep = no_sleep
    return TestClient(app), up


def chat(client, **body):
    body.setdefault("model", "gpt-4o")
    body.setdefault("messages", [{"role": "user", "content": "hi"}])
    return client.post("/v1/chat/completions", json=body, headers=AUTH)


# ---------------------------------------------------------------- config / auth
def test_refuses_to_start_without_bridge_key():
    with pytest.raises(RuntimeError, match="BRIDGE_API_KEYS"):
        b.Settings(client_id="a", client_secret="b").validate()


def test_refuses_short_key_and_http_upstream():
    with pytest.raises(RuntimeError, match="24 characters"):
        b.Settings(client_id="a", client_secret="b", api_keys=("short",)).validate()
    with pytest.raises(RuntimeError, match="https"):
        b.Settings(client_id="a", client_secret="b", api_keys=(KEY,), base_url="http://x").validate()


def test_default_host_is_loopback():
    assert b.Settings().host == "127.0.0.1"


def test_health_is_public_everything_else_needs_key():
    c, up = make()
    assert c.get("/health").json()["status"] == "ok"
    assert c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}).status_code == 401
    r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]},
               headers={"Authorization": "Bearer wrong-key-wrong-key-wrong-key"})
    assert r.status_code == 401
    assert c.get("/v1/models").status_code == 401
    assert c.get("/api/tags").status_code == 401
    assert up.calls == []  # nothing reached u:ai
    assert c.get("/v1/models", headers={"X-API-Key": KEY}).status_code == 200


def test_docs_disabled_by_default():
    c, _ = make()
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404


def test_secret_sent_upstream_only_as_headers():
    c, up = make()
    assert chat(c).status_code == 200
    _, _, body, headers = up.calls[0]
    assert headers["x-client-id"] == "cid" and headers["x-client-secret"] == "csecret"
    assert "csecret" not in json.dumps(body)


def test_passthrough_mode_uses_callers_credentials():
    c, up = make(credential_mode="passthrough", client_id=None, client_secret=None, api_keys=())
    r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]},
               headers={"Authorization": "Bearer my-id:my-secret"})
    assert r.status_code == 200
    h = up.calls[0][3]
    assert h["x-client-id"] == "my-id" and h["x-client-secret"] == "my-secret"
    assert c.post("/v1/chat/completions", json={"messages": []}, headers=AUTH).status_code == 401


# ---------------------------------------------------------------- no filesystem access
def test_no_endpoint_accepts_server_file_paths():
    c, up = make(enable_kb=True)
    r = c.post("/v1/tools/read_file", json={"path": "/proc/self/environ"},
               headers=AUTH)
    assert r.status_code == 404
    assert up.calls == []


def test_kb_endpoints_disabled_by_default():
    c, up = make()
    assert c.get("/v1/uai/knowledge-bases", headers=AUTH).status_code == 404
    r = c.post("/v1/uai/experts", headers=AUTH, data={"name": "n", "summary": "s", "system_prompt": "p"},
               files=[("files", ("a.txt", b"hello", "text/plain"))])
    assert r.status_code == 404
    assert up.calls == []


# ---------------------------------------------------------------- translation
def test_roles_are_translated_and_merged():
    c, up = make()
    msgs = [{"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "1", "type": "function", "function": {"name": "f", "arguments": "{\"a\": 1}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "42"},
            {"role": "user", "content": [{"type": "text", "text": "Q2"}]}]
    assert chat(c, messages=msgs).status_code == 200
    sent = up.chat_payloads()[0]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"].startswith("### SYSTEM INSTRUCTION ###\nBe brief.") and "Q1" in sent[0]["content"]
    assert json.loads(sent[1]["content"]) == {"tool_calls": [{"name": "f", "arguments": {"a": 1}}]}
    assert "### TOOL RESULT [1] ###\n42" in sent[2]["content"] and sent[2]["content"].endswith("Q2")


def test_empty_messages_rejected_without_upstream_call():
    c, up = make()
    assert chat(c, messages=[]).status_code == 400
    assert chat(c, messages=[{"role": "user", "content": "   "}]).status_code == 400
    assert up.calls == []


def test_parameter_mapping():
    c, up = make()
    chat(c, max_tokens=50, temperature=0.2, response_format={"type": "json_object"}, stop="END", seed=7,
         frequency_penalty=0.1, top_p=0.9)
    chat(c, model="gpt-5-mini", max_completion_tokens=99, reasoning_effort="low")
    p1, p2 = up.chat_payloads()
    assert p1["maxTokens"] == 50 and p1["temperature"] == 0.2 and p1["stop"] == ["END"] and p1["seed"] == 7
    assert p1["responseFormat"] == {"type": "json_object"} and p1["frequencyPenalty"] == 0.1
    assert "top_p" not in p1
    assert p2["maxCompletionTokens"] == 99 and "maxTokens" not in p2 and p2["reasoningEffort"] == "low"


def test_ignored_params_reported_and_invalid_rejected():
    c, up = make()
    r = chat(c, top_p=0.5)
    assert r.headers.get("x-bridge-ignored-params") == "top_p"
    assert chat(c, n=2).status_code == 400
    assert chat(c, temperature=5).status_code == 400
    assert chat(c, model="bad model name!").status_code == 400
    assert len(up.chat_payloads()) == 1


def test_provider_prefix_and_tailored_model():
    c, up = make()
    chat(c, model="openai/gpt-4o-mini")
    chat(c, model=f"tailored:{TAI}")
    chat(c, model=f"tailored:{TAI}@gpt-5")
    p = up.chat_payloads()
    assert p[0]["model"] == "gpt-4o-mini" and "tailoredAiId" not in p[0]
    assert p[1]["model"] == "gpt-4o" and p[1]["tailoredAiId"] == TAI
    assert p[2]["model"] == "gpt-5" and p[2]["tailoredAiId"] == TAI


def test_images_placeholder_by_default_and_forward_optional():
    msg = [{"role": "user", "content": [{"type": "text", "text": "what is this"},
                                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]
    c, up = make()
    chat(c, messages=msg)
    assert up.chat_payloads()[0]["messages"][0]["content"] == "what is this\n[image omitted: image/png]"
    c, up = make(image_mode="forward")
    chat(c, messages=msg)
    parts = up.chat_payloads()[0]["messages"][0]["content"]
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


# ---------------------------------------------------------------- tool calls
TOOLS = [{"type": "function", "function": {"name": "search", "description": "Search",
                                           "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}},
         {"type": "function", "function": {"name": "calc", "parameters": {}}}]


def test_parse_tool_calls_nested_and_multiple():
    t = '{"tool_calls": [{"name": "search", "arguments": {"q": "x", "filter": {"year": 2024}}}, ' \
        '{"name": "calc", "arguments": {}}]}'
    calls = b.parse_tool_calls(t, {"search", "calc"})
    assert [c["function"]["name"] for c in calls] == ["search", "calc"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"q": "x", "filter": {"year": 2024}}


def test_parse_tool_calls_rejects_prose_unknown_and_accepts_fences_and_legacy():
    allowed = {"search"}
    assert b.parse_tool_calls('Example: {"tool_calls": [{"name": "search", "arguments": {}}]} done', allowed) is None
    assert b.parse_tool_calls('{"tool_calls": [{"name": "rm_rf", "arguments": {}}]}', allowed) is None
    assert b.parse_tool_calls('{"answer": 42}', allowed) is None
    assert b.parse_tool_calls('```json\n{"tool_calls": [{"name": "search", "arguments": {"q": "a"}}]}\n```', allowed)
    legacy = b.parse_tool_calls('{"tool": "search", "args": {"q": "a"}}', allowed)
    assert json.loads(legacy[0]["function"]["arguments"]) == {"q": "a"}


def test_tool_call_roundtrip_openai():
    c, up = make()
    up.chat_reply = dict(up.chat_reply, content='{"tool_calls": [{"name": "search", "arguments": {"q": "uni wien"}}]}')
    r = chat(c, tools=TOOLS, tool_choice="required").json()
    ch = r["choices"][0]
    assert ch["finish_reason"] == "tool_calls" and ch["message"]["content"] is None
    assert ch["message"]["tool_calls"][0]["function"]["name"] == "search"
    instr = up.chat_payloads()[0]["messages"][0]["content"]
    assert "Available tools" in instr and "You MUST call at least one tool" in instr


def test_tool_choice_none_disables_emulation():
    c, up = make()
    up.chat_reply = dict(up.chat_reply, content='{"tool_calls": [{"name": "search", "arguments": {}}]}')
    r = chat(c, tools=TOOLS, tool_choice="none").json()
    assert r["choices"][0]["finish_reason"] == "stop"
    assert "Available tools" not in up.chat_payloads()[0]["messages"][0]["content"]


# ---------------------------------------------------------------- errors & retries
def test_upstream_errors_keep_their_meaning():
    c, up = make()
    up.queue["/api/v1/llm/chat"] = [httpx.Response(400, json={"message": "Invalid model"})]
    r = chat(c)
    assert r.status_code == 400 and "Invalid model" in r.json()["error"]["message"]
    up.queue["/api/v1/llm/chat"] = [httpx.Response(401, json={"message": "bad creds"})]
    r = chat(c)
    assert r.status_code == 502 and r.json()["error"]["type"] == "upstream_auth_error"
    assert "csecret" not in r.text


def test_retry_on_429_then_success():
    c, up = make()
    up.queue["/api/v1/llm/chat"] = [httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(503)]
    assert chat(c).status_code == 200
    assert len(up.chat_payloads()) == 3


def test_retries_are_bounded():
    c, up = make(max_retries=2)
    up.queue["/api/v1/llm/chat"] = [httpx.Response(503)] * 5
    assert chat(c).status_code == 502
    assert len(up.chat_payloads()) == 3


def test_read_timeout_is_not_retried():
    c, up = make()
    up.queue["/api/v1/llm/chat"] = [httpx.ReadTimeout("slow")]
    r = chat(c)
    assert r.status_code == 504
    assert len(up.chat_payloads()) == 1


def test_connect_error_is_retried():
    c, up = make()
    up.queue["/api/v1/llm/chat"] = [httpx.ConnectError("down")]
    assert chat(c).status_code == 200
    assert len(up.chat_payloads()) == 2


def test_kb_unavailable_maps_to_409():
    c, up = make()
    up.queue["/api/v1/llm/chat"] = [httpx.Response(404, json={"message": "KB_UNAVAILABLE (603)"})]
    assert chat(c, model=f"tailored:{TAI}").status_code == 409


def test_request_size_limit():
    c, up = make(max_request_bytes=1000)
    r = chat(c, messages=[{"role": "user", "content": "x" * 5000}])
    assert r.status_code == 413 and up.calls == []


# ---------------------------------------------------------------- responses
def test_streaming_is_well_formed_sse():
    c, _ = make()
    r = chat(c, stream=True, stream_options={"include_usage": True})
    events = [line[6:] for line in r.text.split("\n") if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert "".join(ch["choices"][0]["delta"].get("content", "") for ch in chunks if ch["choices"]) == "Hello!"
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["total_tokens"] == 5


def test_citations_use_nested_source_fields():
    c, up = make()
    up.chat_reply = dict(up.chat_reply, citations=[
        {"index": 1, "source": {"documentName": "Handbook.pdf", "pageNumber": 2, "documentId": "d"},
         "sourceType": "document"}])
    r = chat(c, model=f"tailored:{TAI}").json()
    assert r["choices"][0]["message"]["content"].endswith("**Sources:**\n- [1] Handbook.pdf, p. 2")
    assert r["citations"][0]["document_name"] == "Handbook.pdf"


def test_models_and_pricing_per_million():
    c, _ = make()
    assert c.get("/v1/models", headers=AUTH).json()["data"][0]["id"] == "gpt-4o"
    p = c.get("/v1/pricing", headers=AUTH).json()
    assert p["models"]["gpt-4o"]["input_usd_per_1m_tokens"] == 2.5
    assert p["models"]["gpt-4o"]["output_usd_per_1m_tokens"] == 10.0


def test_models_are_cached():
    c, up = make()
    c.get("/v1/models", headers=AUTH)
    c.get("/api/tags", headers=AUTH)
    assert sum(1 for x in up.calls if x[1] == "/api/v1/llm/models") == 1


def test_ollama_chat_stream_and_tools():
    c, up = make()
    r = c.post("/api/chat", headers=AUTH, json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}],
                                                "options": {"num_predict": 20, "temperature": 0.1}})
    lines = [json.loads(x) for x in r.text.strip().split("\n")]
    assert lines[0]["message"]["content"] == "Hello!" and lines[-1]["done"] is True
    assert up.chat_payloads()[0]["maxTokens"] == 20
    up.chat_reply = dict(up.chat_reply, content='{"tool_calls": [{"name": "search", "arguments": {"q": "a"}}]}')
    r = c.post("/api/chat", headers=AUTH, json={"model": "gpt-4o", "stream": False, "tools": TOOLS,
                                                "messages": [{"role": "user", "content": "find a"}]}).json()
    assert r["message"]["tool_calls"][0]["function"] == {"name": "search", "arguments": {"q": "a"}}


# ---------------------------------------------------------------- knowledge base (opt-in)
def test_expert_creation_full_chain():
    c, up = make(enable_kb=True)
    r = c.post("/v1/uai/experts", headers=AUTH,
               data={"name": "Physics helper", "summary": "Answers from notes", "system_prompt": "Be precise."},
               files=[("files", ("notes.pdf", b"%PDF-1.4 test", "application/pdf"))])
    assert r.status_code == 200, r.text
    assert r.json()["model"] == f"tailored:{TAI}"
    paths = [(m, p) for m, p, _, _ in up.calls]
    assert paths.index(("POST", "/api/v1/tailored-ai/")) > paths.index(("GET", f"/api/v1/knowledge-base/{KB}/status"))
    tai_body = next(bd for m, p, bd, _ in up.calls if p == "/api/v1/tailored-ai/")
    assert tai_body == {"name": "Physics helper", "summary": "Answers from notes", "systemPrompt": "Be precise."}


def test_expert_cleanup_when_ingestion_fails():
    c, up = make(enable_kb=True)
    up.kb_states = ["failed"]
    r = c.post("/v1/uai/experts", headers=AUTH, data={"name": "X", "summary": "s", "system_prompt": "p"},
               files=[("files", ("a.txt", b"hello", "text/plain"))])
    assert r.status_code == 502 and r.json()["error"]["type"] == "ingestion_failed"
    assert ("DELETE", f"/api/v1/knowledge-base/{KB}") in [(m, p) for m, p, _, _ in up.calls]


def test_upload_validation():
    c, up = make(enable_kb=True, max_upload_bytes=10)
    r = c.post(f"/v1/uai/knowledge-bases/{KB}/documents", headers=AUTH,
               files=[("files", ("big.txt", b"x" * 100, "text/plain"))])
    assert r.status_code == 413
    r = c.post(f"/v1/uai/knowledge-bases/{KB}/documents", headers=AUTH,
               files=[("files", ("evil.sh", b"rm", "text/plain"))])
    assert r.status_code == 400
    r = c.post("/v1/uai/knowledge-bases/not-a-uuid/documents", headers=AUTH,
               files=[("files", ("a.txt", b"x", "text/plain"))])
    assert r.status_code == 400
    assert up.calls == []


def test_upload_filename_is_sanitised():
    c, up = make(enable_kb=True)
    r = c.post(f"/v1/uai/knowledge-bases/{KB}/documents", headers=AUTH,
               files=[("files", ("../../etc/pass wd.txt", b"x", "text/plain"))])
    assert r.status_code == 200
    assert r.json()["documents"][0]["name"] == "pass wd.txt"


def test_tailored_ai_field_limits():
    c, up = make(enable_kb=True)
    r = c.post("/v1/uai/tailored-ais", headers=AUTH, json={"name": "x" * 51, "summary": "s", "system_prompt": "p"})
    assert r.status_code == 400 and up.calls == []


# ---------------------------------------------------------------- behaviour observed against the live API
def test_tailored_reply_shape_message_content():
    c, up = make()
    up.chat_reply = dict(up.chat_reply, content="The code is ZEBRA-1.")
    r = chat(c, model=f"tailored:{TAI}").json()
    assert r["choices"][0]["message"]["content"] == "The code is ZEBRA-1."
    assert r["choices"][0]["finish_reason"] == "stop" and r["usage"]["total_tokens"] == 0


def test_json_mode_is_enforced_by_bridge():
    c, up = make()
    up.chat_reply = dict(up.chat_reply, content='Here is the JSON:\n```json\n{"a": {"b": 1}}\n```')
    r = chat(c, response_format={"type": "json_object"}).json()
    assert json.loads(r["choices"][0]["message"]["content"]) == {"a": {"b": 1}}
    assert "valid JSON object" in up.chat_payloads()[0]["messages"][0]["content"]


def test_extract_json_object():
    assert b.extract_json_object('{"x": [1, 2]}') == '{"x": [1, 2]}'
    assert b.extract_json_object('blah {not json} then {"ok": true} end') == '{"ok": true}'
    assert b.extract_json_object("no json here") is None


def test_upstream_meta_error_message_is_shown():
    c, up = make()
    up.queue["/api/v1/llm/chat"] = [httpx.Response(500, json={"message": "API request failed", "meta": {"error": {
        "internalErrorCode": 605, "message": "API Client Error: Chat invocation failed"}}})] * 3
    r = chat(c)
    assert r.status_code == 502 and "Chat invocation failed (u:ai code 605)" in r.json()["error"]["message"]


def test_rejected_field_is_dropped_and_retried_once():
    c, up = make()
    up.queue["/api/v1/llm/chat"] = [httpx.Response(400, json={"message": "API request failed", "meta": {"error": {
        "internalErrorCode": 400, "message": "API Client Error: Validation failed: Field 'temperature' for model"}}})]
    r = chat(c, model="claude-opus-4-6", temperature=0)
    assert r.status_code == 200 and r.headers["x-bridge-adjusted-params"] == "dropped:temperature"
    p1, p2 = up.chat_payloads()
    assert "temperature" in p1 and "temperature" not in p2


def test_gemini_min_max_tokens():
    c, up = make()
    r = chat(c, model="gemini-2.5-pro", max_tokens=20)
    assert up.chat_payloads()[0]["maxTokens"] == 1024 and "maxTokens->1024" in r.headers["x-bridge-adjusted-params"]
    chat(c, model="gemini-2.5-pro", max_tokens=4000)
    assert up.chat_payloads()[1]["maxTokens"] == 4000


def test_web_citations_format():
    text, clean = b.format_citations([{"index": 0, "location": {"documentName": "britannica.com"},
                                       "pageContent": "https://www.britannica.com/place/Austria", "type": "website"}])
    assert "- [0] britannica.com <https://www.britannica.com/place/Austria>" in text
    assert clean[0]["url"].startswith("https://") and clean[0]["type"] == "website"
