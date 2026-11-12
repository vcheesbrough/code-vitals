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
#      and is refused on a base table;
#   4. the dashboard (superset/dashboard/, card #500) is imported with every
#      chart of the bundle, every one of its datasets reads through
#      Superset's own `code-vitals` connection, in the running container, and
#      no chart's query is past 80% of its row limit (card #552: Superset
#      would truncate it without an error).
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
superset="$base-superset-1"
charts_dir="$(dirname "$0")/../superset/dashboard/charts"
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
  # Only Authentik's sign-in flow counts: any other page of Authentik's (an
  # access-denied flow, an error page) means the request was turned away.
  "302 https://auth.desync.link/if/flow/"* | "302 https://auth.desync.link/flows/-/default/authentication/"*) ;;
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
unset PGPASSWORD
echo "  ok"

echo "==> the dashboard is imported and its datasets read"
expected=$(find "$charts_dir" -name '*.yaml' | wc -l | tr -d ' ')
# Superset's metadata database, as the owner over the container's socket.
charts=$(docker exec "$container" psql -v ON_ERROR_STOP=1 -U code_vitals -d superset -tA -c \
  "SELECT count(*) FROM dashboard_slices s JOIN dashboards d ON d.id = s.dashboard_id WHERE d.slug = 'code-vitals'")
if [ "$expected" -eq 0 ] || [ "$charts" != "$expected" ]; then
  echo "ERROR: the code-vitals dashboard has ${charts:-no} chart(s), the bundle $expected" >&2
  exit 1
fi
if ! read_out=$(docker exec "$superset" python -m code_vitals_dashboard --check /app/code-vitals-dashboard 2>&1); then
  echo "$read_out" >&2
  echo "ERROR: the dashboard check failed in Superset's container: a dataset cannot be read through its connection, a dropped object is left, or a chart is near its row limit" >&2
  exit 1
fi
echo "  ok: $charts charts; $(echo "$read_out" | tail -n 1)"
echo "==> $env: superset smoke checks passed"
