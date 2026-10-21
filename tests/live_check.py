"""Live end-to-end check of a running bridge against the REAL u:ai API.

Uses a few cents of API credit. Run where the bridge is reachable:
    BRIDGE_URL=http://127.0.0.1:8000 BRIDGE_KEY=... python tests/live_check.py [--kb]
--kb also creates a tiny Tailored AI + knowledge base and deletes them again.
Prints only PASS/FAIL lines and short answer snippets, never credentials.
"""
import json
import os
import sys
import time
import uuid

import httpx

URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8000")
KEY = os.environ["BRIDGE_KEY"]
H = {"Authorization": f"Bearer {KEY}"}
c = httpx.Client(base_url=URL, timeout=300)
results = []


def check(name, ok, info=""):
    ok = bool(ok)
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  | {info}" if info else ""), flush=True)


def snip(s, n=90):
    return (s or "").replace("\n", " ")[:n]


def chat(**body):
    body.setdefault("messages", [{"role": "user", "content": "hi"}])
    return c.post("/v1/chat/completions", json=body, headers=H)


def step(name, fn):
    try:
        fn()
    except Exception as e:  # keep going, report
        check(name, False, f"exception {type(e).__name__}: {snip(str(e), 150)}")


# ---------------------------------------------------------------- basics
def t_health():
    r = c.get("/health")
    check("health", r.status_code == 200, json.dumps(r.json()))


def t_auth():
    r1 = c.get("/v1/models")
    r2 = c.get("/v1/models", headers={"Authorization": "Bearer wrong-key-wrong-key-wrong-key"})
    r3 = c.post("/v1/tools/read_file", json={"path": "/proc/self/environ"},
                headers=H)
    check("no key -> 401, wrong key -> 401", r1.status_code == 401 and r2.status_code == 401,
          f"{r1.status_code}/{r2.status_code}")
    check("no server-file-path endpoint (404)", r3.status_code == 404, str(r3.status_code))


MODELS = []


def t_models():
    r = c.get("/v1/models", headers=H)
    MODELS.extend(m["id"] for m in r.json().get("data", []))
    check("models listed", r.status_code == 200 and len(MODELS) > 0, ", ".join(MODELS))


def pick(*prefs):
    for p in prefs:
        if p in MODELS:
            return p
    return MODELS[0]


def t_simple():
    m = pick("gpt-4o-mini", "gpt-4o")
    r = chat(model=m, messages=[{"role": "user", "content": "Reply with exactly the word PONG and nothing else."}],
             max_tokens=10, temperature=0)
    j = r.json()
    txt = j.get("choices", [{}])[0].get("message", {}).get("content", "") if r.status_code == 200 else r.text
    check(f"simple chat ({m})", r.status_code == 200 and "PONG" in txt.upper() and j["usage"]["total_tokens"] > 0,
          f"{snip(txt)} | usage={j.get('usage')}")


def t_system_and_multiturn():
    m = pick("gpt-4o-mini", "gpt-4o")
    r = chat(model=m, temperature=0, max_tokens=30, messages=[
        {"role": "system", "content": "You always answer in German, in at most five words."},
        {"role": "user", "content": "My favourite number is 17."},
        {"role": "user", "content": "Remember it."},
        {"role": "assistant", "content": "Okay, gemerkt."},
        {"role": "user", "content": "What is my favourite number?"}])
    txt = r.json()["choices"][0]["message"]["content"] if r.status_code == 200 else r.text
    check("system role + merged user turns + history", r.status_code == 200 and "17" in txt, snip(txt))


def t_reasoning_tokens():
    m = next((x for x in ("gpt-5-mini", "gpt-5-nano", "gpt-5", "o3-mini", "o3") if x in MODELS), None)
    if not m:
        check("reasoning model max_tokens mapping", True, "skipped: no reasoning model listed")
        return
    r = chat(model=m, max_tokens=400, reasoning_effort="low",
             messages=[{"role": "user", "content": "What is 6*7? Answer with the number only."}])
    txt = r.json()["choices"][0]["message"]["content"] if r.status_code == 200 else r.text
    check(f"reasoning model + maxCompletionTokens ({m})", r.status_code == 200 and "42" in (txt or ""),
          snip(txt))


def t_json_mode():
    m = pick("gpt-4o-mini", "gpt-4o")
    r = chat(model=m, response_format={"type": "json_object"}, temperature=0, max_tokens=60,
             messages=[{"role": "user", "content": 'Return a JSON object {"city": "Vienna", "country": ...}.'}])
    ok, info = False, r.text[:120]
    if r.status_code == 200:
        txt = r.json()["choices"][0]["message"]["content"]
        try:
            ok = json.loads(txt).get("city") == "Vienna"
            info = snip(txt)
        except ValueError:
            info = "not JSON: " + snip(txt)
    check("response_format json_object", ok, info)


TOOLS = [{"type": "function", "function": {
    "name": "get_weather", "description": "Current weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"},
                                                    "unit": {"type": "string", "enum": ["C", "F"]}},
                   "required": ["city"]}}}]


def t_tools():
    m = pick("gpt-4o", "gpt-4o-mini")
    msgs = [{"role": "user", "content": "What's the weather in Vienna in Celsius?"}]
    r = chat(model=m, tools=TOOLS, temperature=0, messages=msgs)
    j = r.json()
    ch = j.get("choices", [{}])[0]
    calls = ch.get("message", {}).get("tool_calls") or []
    ok = r.status_code == 200 and ch.get("finish_reason") == "tool_calls" and calls and \
        calls[0]["function"]["name"] == "get_weather" and \
        "vienna" in json.loads(calls[0]["function"]["arguments"]).get("city", "").lower()
    check(f"tool call emitted ({m})", bool(ok),
          snip(json.dumps(calls[0]["function"]) if calls else ch.get("message", {}).get("content", r.text), 120))
    if not ok:
        return
    msgs += [ch["message"], {"role": "tool", "tool_call_id": calls[0]["id"], "content": '{"temp_c": 11, "sky": "cloudy"}'}]
    r2 = chat(model=m, tools=TOOLS, temperature=0, messages=msgs)
    m2 = r2.json()["choices"][0]
    txt = m2["message"].get("content") or ""
    check("tool result used in final answer", r2.status_code == 200 and m2["finish_reason"] == "stop" and "11" in txt,
          snip(txt))


def t_no_tool_needed():
    m = pick("gpt-4o", "gpt-4o-mini")
    r = chat(model=m, tools=TOOLS, temperature=0, max_tokens=30,
             messages=[{"role": "user", "content": "Say hello to Alex in one short sentence."}])
    ch = r.json()["choices"][0]
    check("tools offered but not needed -> plain text", r.status_code == 200 and ch["finish_reason"] == "stop"
          and ch["message"].get("content"), snip(ch["message"].get("content")))


def t_stream():
    m = pick("gpt-4o-mini", "gpt-4o")
    with c.stream("POST", "/v1/chat/completions", headers=H, json={
            "model": m, "stream": True, "stream_options": {"include_usage": True}, "max_tokens": 20,
            "messages": [{"role": "user", "content": "Count from 1 to 5."}]}) as r:
        lines = [l[6:] for l in r.iter_lines() if l.startswith("data: ")]
    text = "".join(json.loads(x)["choices"][0]["delta"].get("content", "") for x in lines[:-1]
                   if json.loads(x)["choices"])
    usage = [json.loads(x).get("usage") for x in lines[:-1] if json.loads(x).get("usage")]
    check("streaming (SSE)", lines and lines[-1] == "[DONE]" and "3" in text and usage,
          f"{len(lines)} events | {snip(text)}")


def t_errors():
    r = chat(model="no-such-model-xyz")
    check("unknown model -> 4xx (not 500)", 400 <= r.status_code < 500,
          f"{r.status_code} {snip(r.json()['error']['message'], 100)}")
    r = chat(messages=[])
    check("empty messages -> 400 before upstream", r.status_code == 400, str(r.status_code))


def t_ollama():
    m = pick("gpt-4o-mini", "gpt-4o")
    t = c.get("/api/tags", headers=H)
    check("ollama /api/tags", t.status_code == 200 and len(t.json()["models"]) == len(MODELS),
          f"{len(t.json()['models'])} models")
    r = c.post("/api/chat", headers=H, json={"model": m, "stream": False, "options": {"num_predict": 10},
                                             "messages": [{"role": "user", "content": "Reply with OK."}]})
    check("ollama /api/chat (non-stream)", r.status_code == 200 and r.json()["done"] is True,
          snip(r.json()["message"]["content"]))
    r = c.post("/api/chat", headers=H, json={"model": m, "options": {"num_predict": 10},
                                             "messages": [{"role": "user", "content": "Reply with OK."}]})
    lines = [json.loads(x) for x in r.text.strip().split("\n")]
    check("ollama /api/chat (NDJSON stream)", r.status_code == 200 and lines[-1]["done"] is True, f"{len(lines)} lines")


def t_other_vendors():
    for m in ("Mistral-Large-3", "gemini-3.1-flash-lite", "claude-opus-4-6", "sonar-pro"):
        if m not in MODELS:
            continue
        r = chat(model=m, max_tokens=20, temperature=0,
                 messages=[{"role": "system", "content": "Answer with one word."},
                           {"role": "user", "content": "What is the capital of Austria?"}])
        txt = r.json()["choices"][0]["message"]["content"] if r.status_code == 200 else r.text
        check(f"non-OpenAI model with max_tokens ({m})", r.status_code == 200 and any(w in (txt or "").lower() for w in ("vienna", "wien")),
              snip(txt))


def t_pricing():
    p = c.get("/v1/pricing", headers=H).json()["models"]
    vals = {k: (v["input_usd_per_1m_tokens"], v["output_usd_per_1m_tokens"]) for k, v in p.items()}
    sane = all(x is None or 0 < x < 200 for pair in vals.values() for x in pair)
    check("pricing per 1M tokens plausible", sane, json.dumps(vals)[:300])


# ---------------------------------------------------------------- knowledge base (optional)
def t_kb():
    code = f"ZEBRA-{uuid.uuid4().hex[:6].upper()}"
    doc = (f"Internal note for the bridge test.\nThe secret code word of Project Danube is {code}.\n"
           "It was chosen in October 2026.\n").encode()
    t0 = time.time()
    r = c.post("/v1/uai/experts", headers=H, data={
        "name": "uai-bridge-live-test", "summary": "Temporary test, safe to delete",
        "system_prompt": "Answer only from the provided documents."},
        files=[("files", ("bridge_test_note.txt", doc, "text/plain"))], timeout=900)
    j = r.json()
    check("expert creation (KB -> upload -> ingest -> ready -> Tailored AI -> link)", r.status_code == 200,
          f"{r.status_code} in {time.time() - t0:.0f}s | {snip(json.dumps(j), 160)}")
    tai, kb = j.get("tailored_ai_id"), j.get("knowledge_base_id") or (j.get("error") or {}).get("knowledge_base_id")
    try:
        if r.status_code == 200:
            q = chat(model=j["model"], temperature=0, max_tokens=60,
                     messages=[{"role": "user", "content": "What is the secret code word of Project Danube?"}])
            qj = q.json()
            txt = qj["choices"][0]["message"]["content"] if q.status_code == 200 else q.text
            check("tailored chat retrieves the document fact", q.status_code == 200 and code in txt, snip(txt, 160))
            check("citations returned", bool(qj.get("citations")), snip(json.dumps(qj.get("citations")), 160))
    finally:
        if tai:
            d = c.delete(f"/v1/uai/tailored-ais/{tai}", headers=H)
            check("cleanup: delete Tailored AI", d.status_code == 200, str(d.status_code))
        if kb:  # deleting the Tailored AI already deletes its linked KB, so 404 is expected
            d = c.delete(f"/v1/uai/knowledge-bases/{kb}", headers=H)
            check("cleanup: knowledge base gone", d.status_code in (200, 404), str(d.status_code))
        left = c.get("/v1/uai/knowledge-bases", headers=H).json().get("data", [])
        check("no test knowledge bases left", not any("uai-bridge-live-test" in (k.get("name") or "") for k in left),
              f"{len(left)} KBs on account")


tests = [t_health, t_auth, t_models, t_simple, t_system_and_multiturn, t_reasoning_tokens, t_json_mode,
         t_tools, t_no_tool_needed, t_stream, t_errors, t_ollama, t_other_vendors, t_pricing]
if "--kb" in sys.argv:
    tests.append(t_kb)
for t in tests:
    step(t.__name__, t)
print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
