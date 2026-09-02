---
name: project-kbc-ai-artifact
description: Architecture and recurring review focus areas for the kbc_ai_artifact (Keboola Artifact Hub) FastAPI service
metadata:
  type: project
---

kbc_ai_artifact is a public FastAPI service deployed as a Keboola Data App
behind a platform proxy (see repo `CLAUDE.md`, authoritative). It keeps no
server-side session for any Keboola credential — every credential (Storage
API token, or a `kbc_at_*`/`kbc_pat_*` programmatic bearer since v0.12.0's
"Sign in with Keboola" feature) is request-scope only and is handed back to
the visitor's own browser tab (`sessionStorage`), never persisted, logged, or
echoed. `src/builder.py::_scrub` and `src/kbc.py::_safe_message` are the
established scrubbing choke points for anything that could contain a token.

**Recurring review focus for this repo:**
- Every stack URL a caller supplies must go through `src/auth.py::resolve_stack`
  (allowlists `*.keboola.com` + `HUB_EXTRA_STACKS`) before any outbound call —
  this is the SSRF boundary. Check every new endpoint that accepts a `stack`
  param or header.
- Any new unauthenticated endpoint that makes an outbound call to a Keboola
  stack on the caller's behalf (the hub "relays" calls because browsers get no
  CORS from a stack — see `src/kbclogin.py`) needs a per-IP rate limit via
  `_claim_login`/`_read_counter`/`_bump_counter` in `src/main.py`, or it
  becomes an open relay/amplifier onto Keboola's own auth infrastructure. In
  the v0.12.0 login review, `/login/device` and `/login/pkce/start` were
  correctly budgeted this way but three sibling endpoints
  (`/login/device/token`, `/login/refresh`, `/login/signout`) were left
  unbudgeted despite doing the exact same kind of unauthenticated outbound
  relay call — watch for this same gap pattern (a security mechanism applied
  to some but not all endpoints of the same shape) in future diffs.
- `src/main.py::_client_ip` / `_is_trusted_proxy` / `HUB_TRUSTED_PROXY_CIDRS`
  is the SEC-100-003 rate-limit-bucket infrastructure (attacker-controlled
  `X-Forwarded-For`/`X-Real-IP` are only trusted from a configured trusted-proxy
  CIDR). Pre-existing and solid; no need to re-audit unless it changes.
- `src/main.py::base_url` / the `public_origin` middleware only trust
  `X-Forwarded-Host`/`-Proto` when `HUB_TRUST_FORWARDED_HEADERS` is set AND no
  `HUB_PUBLIC_BASE_URL` is configured — production always sets
  `HUB_PUBLIC_BASE_URL` per `CLAUDE.md`, so header spoofing of the origin is
  not reachable in the deployed topology. The PKCE loopback-only gate
  (`_loopback_origin`) relies on this staying true.
- Script-injection into served HTML: this codebase's pattern for inlining
  server/stack data into a `<script>` block is `json.dumps(...).replace("<",
  "\\u003c")` (see `src/pages.py::_login_result_json`) — correct and
  sufficient, since JSON already escapes quotes/backslashes/control chars and
  the only HTML-breakout vector inside a `<script>` context is `</script>`.
  Any new inlining should follow the same pattern.
- `SEC-075-011` = `HUB_DESTRUCTIVE_TOKEN_POLICY` gating destructive routes on
  token-level claims (`is_master_token`, `admin_role`, `token_id`, ...) parsed
  in `src/auth.py::verify_token` onto `Owner`. `verify_token` also cross-checks
  that a bearer's resolved project matches the caller-claimed
  `X-Storage-Project` — refuses rather than silently trusting a different
  resolved identity.
