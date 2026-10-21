# Contributing

1. Fork, create a branch, make your change.
2. Add or update a test in `tests/test_bridge.py` (the tests use a mocked u:ai API; no credentials needed).
3. Run `pip install -r requirements-dev.txt && pytest -q`.
4. Open a pull/merge request describing what changed and why.

Rules:
- Never commit `.env` files or real credentials, and never paste them into issues or logs.
- No endpoint may read files from the server's filesystem; uploads only.
- New endpoints must require authentication (`authenticate(request)`).
- Keep the bridge a single file with few dependencies.

`tests/live_check.py` runs end-to-end checks against the real API (costs a few cents); run it only
with your own API account.
