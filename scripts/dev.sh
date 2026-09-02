#!/usr/bin/env bash
# Run the hub locally on the loopback origin, which is also the only origin a
# Keboola stack accepts as a PKCE callback — so /login offers both sign-in
# flows here, unlike the deployed app which only ever gets the device code.
#
# Secrets come from .env.local (gitignored, never committed). Create it with:
#
#   HUB_STORAGE_TOKEN=<a Storage token for the host project>
#   HUB_STACK_URL=https://connection.<your-stack>.keboola.com
#   HUB_SECRET_KEY=<32+ random characters>
#
# Generate a secret key with:
#   python -c 'import secrets; print(secrets.token_urlsafe(48))'
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env.local ]; then
  echo "Missing .env.local — see the header of $0 for what it needs." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./.env.local
set +a

# Fail fast on the host-project token rather than booting a hub whose every
# storage read 502s. The app itself is deliberately tolerant here (a transient
# Storage outage must not crash the deployed container), which is exactly why
# a bad token locally shows up as a confusing error inside the studio instead
# of at startup.
verify=$(curl -s -o /dev/null -w '%{http_code}'   -H "X-StorageApi-Token: ${HUB_STORAGE_TOKEN}"   "${HUB_STACK_URL%/}/v2/storage/tokens/verify" --max-time 15 || echo 000)
if [ "$verify" != "200" ]; then
  echo "HUB_STORAGE_TOKEN is not usable on ${HUB_STACK_URL} (verify -> ${verify})." >&2
  echo "It must be a Storage API token for the project that holds this hub's" >&2
  echo "own serving copies. Put a real one in .env.local and try again." >&2
  exit 1
fi

PORT="${HUB_DEV_PORT:-8050}"
# Both halves matter: uvicorn must bind the loopback interface, and the hub
# must name that same origin in the redirect URI it registers with a stack.
export HUB_PUBLIC_BASE_URL="http://127.0.0.1:${PORT}"
export HUB_CACHE_DIR="${HUB_CACHE_DIR:-.dev-cache}"

echo "Artifact Hub on ${HUB_PUBLIC_BASE_URL}  (sign in at ${HUB_PUBLIC_BASE_URL}/login)"
exec uv run uvicorn src.main:app --host 127.0.0.1 --port "${PORT}" --reload
