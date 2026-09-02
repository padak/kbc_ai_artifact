---
name: project-error-handling-style
description: How kbc_ai_artifact deliberately degrades vs. silently fails — read before flagging catch blocks/fallbacks in this repo
metadata:
  type: project
---

This codebase (src/main.py, src/auth.py, src/kbclogin.py, src/store.py,
src/pages.py) has an unusually high rate of *intentional* degradation, almost
always with a docstring/comment explaining the why. Do not flag these without
reading the surrounding comment first — the author (padak/team) already
reasoned through the failure mode in most cases.

**Recognizable "considered decision" markers**: a comment naming the specific
scenario ("a frozen PAT scope may name a project that no longer exists"), an
explicit residual-risk ID (e.g. `REL-075-009`, `SEC-075-011`), or a comment
that says what an operator should do to recover by hand (grep for "reaping",
"reap it by hand"). Treat these as reviewed-and-accepted unless you find a
concrete new scenario the comment doesn't cover.

**The one recurring real gap found so far (2026-09-01, login-with-keboola
branch)**: HTTP-level failures against an external Keboola stack are logged
only when the failure is a *transport* error (`httpx.HTTPError` — DNS,
connect, timeout). Any *application-level* non-2xx response (404, 429, 4xx,
5xx from the stack itself) is turned into a typed exception and raised with
no `logger.*` call anywhere in the chain — not in the low-level request
helper, not in the route handler that catches it. Grep for `except.*Error as
exc:\s*(raise HTTPException|return \{"error"` and check whether the
try/except chain ever calls `logger.` before it — if not, and the underlying
call is a network request to an external Keboola stack, that's a real
finding (was: `src/kbclogin.py` `_request`/`_raise_for_error`, confidence
85). This matches CLAUDE.md's "Server Logging for Backtesting" requirement,
which is worth citing directly in the finding since it's an explicit project
rule, not just a general best practice.

**Fields coerced to a safe default (e.g. `int(x) or 0`) are often
cosmetic-only** — trace whether the coerced field actually drives any
downstream logic (scheduling, auth decisions) before flagging it as
high-risk. In this codebase, `Credential.expires_in` and `Credential.user_id`
(src/kbclogin.py `_credential`) are both display-only; proactive session
renewal is not implemented (renewal is reactive, only on a 401), and the UI
never shows `user_id`. Confirmed by grepping every use site in src/pages.py
and src/main.py before concluding "no operational impact."

**JS auth/session code (`src/pages.py` `_ADMIN_JS`/`_REVIEW_JS`/`_LOGIN_JS`)
follows one shared, careful pattern**: `renewSession()` returns false on any
failure and is only ever used as a retry gate before falling through to the
original (still-failing) response, so a genuine 401 is never swallowed.
`endSession()` fire-and-forget with `.catch(function () {})` is intentional
(logout must not block on network) and documented inline. Boot paths
explicitly distinguish 401/403 (credential rejected → clear + re-prompt)
from other failures (transient → keep credential, retry later) with
different user-facing copy — this is the *correct* pattern to expect, not a
smell.
