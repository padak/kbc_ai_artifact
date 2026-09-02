---
name: project-kbc-ai-artifact
description: Recurring review patterns specific to kbc_ai_artifact (Keboola artifact hub) — what's already solved vs. what tends to be genuinely unverified
metadata:
  type: project
---

This repo (`/home/zajca/Code/keboola/kbc_ai_artifact`) has unusually mature,
deliberate infrastructure for problems that look like fresh bugs at first
glance. Before flagging one of these as a new risk in a diff, check whether
the new code already reuses the existing mechanism — it usually does:

- **Per-client-IP rate limiting is centralized and trusted-proxy-aware**
  (SEC-075-011/SEC-100-003, `src/main.py` `_client_ip` /
  `_forwarded_client_trusted` / `HUB_TRUSTED_PROXY_CIDRS`). A new counter
  that calls `_client_ip(request)` inherits real production hardening
  (nginx -> platform proxy chain walk), not naive `request.client.host`.
  Don't flag "shared bucket behind the proxy" without first checking
  whether the counter uses this helper.
- **Process-local in-memory registries are a deliberate, documented pattern**
  under the "exactly one instance, ever" invariant (`acquire_instance_lock`,
  see project `CLAUDE.md`). A registry scoped to a loopback-only dev flow
  (e.g. `PkceRegistry` in `src/kbclogin.py`) losing state on `--reload` is a
  real but self-healing edge case if the code already returns a generic
  "start again" message for every other lost/expired/replayed-state case —
  check the actual error path before scoring this >=80.
- **`kbcstorage` version is pinned exactly in `uv.lock`**, and
  `tests/test_kbc.py::TestHeadersOnTheWire` asserts against a real loopback
  HTTP server what lands on the wire, not just an in-memory header dict —
  this would catch a private-attribute-reach-in (`_apply_auth` mutating
  `endpoint._auth_header`) breaking on a library upgrade. Reaching into a
  private attribute is still worth naming, but it is not an unmitigated
  risk here.

Where this PR's actual gaps turned out to be (2026-09-02, "Sign in with
Keboola" review, branch `login-with-keboola`, commit `e424fbc`):

1. **Whether the Keboola Data App platform proxy forwards the
   `Authorization` header is genuinely unverified** — the README and
   `/context` manifest both point at a manual `GET /health/headers` check
   the operator has to remember to run after deploy; nothing in code
   degrades the `/login` UI or gives a distinguishing error if the header
   never arrives (a stripped `Authorization` produces the same "Missing
   X-StorageApi-Token header" 401 as a caller who sent nothing at all —
   `src/auth.py:164`, reached via `src/main.py` `client_credential`).
   General lesson: any *new* header this app starts depending on for auth,
   behind this specific proxy, is unverified until someone greps for
   evidence of it being tested live — don't assume the existing `X-Kbc-*`
   stripping knowledge generalizes to other headers.
2. **A Keboola programmatic bearer (`kbc_at_*`/`kbc_pat_*`) is scoped to
   the signing-in admin's whole account, not to one project** — confirmed
   via `BearerTokenAuthenticator` semantics in the Connection source (see
   `/home/zajca/Code/keboola/connection`): `X-KBC-ProjectId` only selects
   which project a given call acts as, with no hub-side enforcement tying
   it to what a "project picker" UI displayed. `src/kbclogin.py`'s device
   flow (`start_device`) has no scope-narrowing parameter at all, and the
   PKCE flow's `pick_project` narrowing (`authorize_url(..., pick_project=)`)
   exists in the API but the shipped `/login` page (`src/pages.py:5076`)
   never passes it — so in practice every sign-in this hub offers is
   account-wide. This is a real widening of blast radius versus a pasted,
   project-scoped Storage API token, independent of the sessionStorage
   token-lifetime question. General lesson for this codebase: when a
   Keboola bearer/PAT is introduced anywhere, check whether the "project"
   attached to it is enforced by the stack (real boundary) or just a
   header the hub trusts the caller to set correctly (UI convenience only).
