# Security policy

## Reporting a vulnerability
Please do not open a public issue. Report privately instead:
- GitHub: "Security" tab → "Report a vulnerability" (private vulnerability reporting).
- Or e-mail the maintainer: martin.mauser@univie.ac.at.
Include the version (`GET /health`), your configuration (without secrets) and steps to reproduce.

## Design rules this project keeps
- No endpoint reads or writes files on the server's filesystem; documents arrive only as uploads.
- Every endpoint except `/health` requires authentication unless the operator explicitly disables it.
- The service listens on `127.0.0.1` by default and refuses to start without a bridge API key.
- u:ai credentials are only sent to `UAI_BASE_URL` over HTTPS, as request headers.
- Prompts, answers and uploaded documents are never logged.
- Upstream requests that may already have been processed (read timeouts) are never retried.

## If a secret may have leaked
Ask for a new u:ai client secret (an API client can have two secrets for zero-downtime rotation),
deploy it, then have the old one deactivated. Rotate `BRIDGE_API_KEYS` by adding the new key, updating
clients, and removing the old key.
