#!/bin/sh
# Smoke-check a deployed environment's Superset (card #499), from the CI agent:
#
#   1. https://<host>/health answers OK (retried while Traefik picks the
#      container up and Superset starts);
#   2. https://<host>/login/authentik redirects to Authentik's authorize
#      endpoint with this environment's client_id and redirect_uri, and
#      Authentik ACCEPTS that request — it sends an anonymous browser on to its
#      own sign-in flow rather than bouncing back with `error=` (it validates
#      the request before asking anyone to sign in). Sign-in is wired, without
#      needing a user;
#   3. the reader role — Superset's datasource — can read the freshness view
#      and is refused on a base table.
#
# Usage: smoke-superset.sh <dev|prod>
# Environment: CODE_VITALS_READER_PASSWORD (never printed). Needs curl and docker.

set -eu
exec < /dev/null

env="${1:?usage: smoke-superset.sh <dev|prod>}"
case "$env" in
  prod) base="code-vitals" ;;
  dev) base="code-vitals-dev" ;;
  *)
    echo "ERROR: environment must be dev or prod (got '$env')" >&2
    exit 2
    ;;
esac
: "${CODE_VITALS_READER_PASSWORD:?CODE_VITALS_READER_PASSWORD is required}"
host="$base.desync.link"
container="$base-postgres-1"
attempts="${SMOKE_ATTEMPTS:-36}"
delay="${SMOKE_DELAY:-5}"

echo "==> $host/health"
i=0
until [ "$(curl -fsS --max-time 10 "https://$host/health" 2>/dev/null || true)" = OK ]; do
  i=$((i + 1))
  if [ "$i" -ge "$attempts" ]; then
    echo "ERROR: https://$host/health did not answer OK after $attempts attempts" >&2
    exit 1
  fi
  sleep "$delay"
done
echo "  ok"

echo "==> $host/login/authentik redirects to Authentik"
out=$(curl -sS -o /dev/null --max-time 10 -w '%{http_code} %{redirect_url}' "https://$host/login/authentik")
code=${out%% *}
location=${out#* }
expected_redirect="redirect_uri=https%3A%2F%2F$host%2Foauth-authorized%2Fauthentik"
case "$code $location" in
  "302 https://auth.desync.link/application/o/authorize/?"*)
    case "$location" in
      *"client_id=$base&"* | *"client_id=$base") ;;
      *)
        echo "ERROR: sign-in redirect lacks client_id=$base: $location" >&2
        exit 1
        ;;
    esac
    case "$location" in
      *"$expected_redirect"*) ;;
      *)
        echo "ERROR: sign-in redirect lacks $expected_redirect: $location" >&2
        exit 1
        ;;
    esac
    ;;
  *)
    echo "ERROR: expected a 302 to Authentik's authorize endpoint, got: $code $location" >&2
    exit 1
    ;;
esac
echo "  ok"

echo "==> Authentik accepts the authorize request"
answer=$(curl -sS -o /dev/null --max-time 10 -w '%{http_code} %{redirect_url}' "$location")
case "$answer" in
  *"error="*)
    echo "ERROR: Authentik refused the authorize request: $answer" >&2
    exit 1
    ;;
  "302 https://auth.desync.link/"*) ;;
  *)
    echo "ERROR: expected Authentik to send the browser on to its sign-in flow, got: $answer" >&2
    exit 1
    ;;
esac
echo "  ok"

echo "==> the reader role reads views only"
PGPASSWORD="$CODE_VITALS_READER_PASSWORD"
export PGPASSWORD
reader() {
  docker exec -e PGPASSWORD "$container" \
    psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -U code_vitals_reader -d code_vitals -tA -c "$1"
}
if ! reader 'SELECT count(*) FROM freshness' > /dev/null; then
  echo "ERROR: the reader role cannot read the freshness view" >&2
  exit 1
fi
if denied=$(reader 'SELECT 1 FROM repo LIMIT 1' 2>&1); then
  echo "ERROR: the reader role can read a base table" >&2
  exit 1
fi
# Any other error (a missing table, a failed connection) proves nothing.
case "$denied" in
  *"permission denied"*) ;;
  *)
    echo "ERROR: reading a base table as the reader failed, but not with a permission denial: $denied" >&2
    exit 1
    ;;
esac
echo "  ok"
echo "==> $env: superset smoke checks passed"
