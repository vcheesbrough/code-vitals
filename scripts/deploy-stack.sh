#!/bin/sh
# Deploy code-vitals' stack for one environment on the CI agent's docker host.
#
#   1. Postgres up (deploy/compose.yaml), waiting until healthy.
#   2. dev only: recreate the dev database and fill it from a fresh dump of
#      prod, so dev starts from a copy of prod on every deploy — the migration
#      is tested on prod-shaped data, and no row a previous dev deploy wrote
#      survives (writes are idempotent, so stale rows would mask a fix). While
#      prod is not running, dev starts empty.
#   3. `code-vitals migrate` from the image being deployed, run on the
#      project's network: migrations, then role grants, then the writer and
#      reader logins.
#
# Usage: deploy-stack.sh <dev|prod> <collector-image-ref>
# Environment (from Woodpecker secrets; never printed):
#   POSTGRES_PASSWORD, CODE_VITALS_WRITER_PASSWORD, CODE_VITALS_READER_PASSWORD
# Run from the repository root.
#
# Nothing here reads input: Woodpecker hands the step's script to the shell on
# stdin, and a docker command that read it would eat the rest of the step
# (bored deploys 385, 390, 391). stdin is detached for everything below.

set -eu
exec < /dev/null

env="${1:?usage: deploy-stack.sh <dev|prod> <collector-image-ref>}"
image="${2:?usage: deploy-stack.sh <dev|prod> <collector-image-ref>}"

case "$env" in
  prod) project=code-vitals ;;
  dev) project=code-vitals-dev ;;
  *)
    echo "ERROR: environment must be dev or prod (got '$env')" >&2
    exit 2
    ;;
esac
prod_container=code-vitals-postgres-1
container="$project-postgres-1"
PG_VOLUME="code-vitals-$env-pg"
export PG_VOLUME

for name in POSTGRES_PASSWORD CODE_VITALS_WRITER_PASSWORD CODE_VITALS_READER_PASSWORD; do
  value=$(printenv "$name" || true)
  if [ -z "$value" ]; then
    echo "ERROR: $name is required" >&2
    exit 1
  fi
  # They go into a connection URL or a role's password; generate them as hex.
  case "$value" in
    *[!A-Za-z0-9._~-]*)
      echo "ERROR: $name may contain only A-Z a-z 0-9 . _ ~ - (it goes into a URL)" >&2
      exit 1
      ;;
  esac
done

echo "==> $env: postgres ($project, volume $PG_VOLUME)"
docker volume create "$PG_VOLUME" > /dev/null
docker compose -p "$project" -f deploy/compose.yaml up -d --pull always --wait --wait-timeout 120 postgres

if [ "$env" = dev ]; then
  echo "==> dev: recreating the database"
  docker exec "$container" psql -v ON_ERROR_STOP=1 -q -U code_vitals -d postgres \
    -c 'DROP DATABASE IF EXISTS code_vitals WITH (FORCE)' -c 'CREATE DATABASE code_vitals'
  if [ -n "$(docker ps -q --filter "name=^/$prod_container\$" --filter status=running)" ]; then
    echo "==> dev: restoring a fresh dump of prod"
    dump=$(mktemp)
    # shellcheck disable=SC2064  # expand dump now, not at trap time
    trap "rm -f '$dump'" EXIT
    docker exec "$prod_container" pg_dump -U code_vitals -Fc code_vitals > "$dump"
    docker exec -i "$container" pg_restore --no-owner --no-acl -U code_vitals -d code_vitals < "$dump"
  else
    echo "==> dev: prod is not running; dev starts empty"
  fi
fi

echo "==> $env: migrate"
CODE_VITALS_DATABASE_URL="postgresql://code_vitals:$POSTGRES_PASSWORD@postgres:5432/code_vitals"
export CODE_VITALS_DATABASE_URL
# `-e NAME` copies each value from this environment, so no secret appears on a
# command line (`ps` on the agent host) or in the step log.
docker run --rm --network "${project}_default" \
  -e CODE_VITALS_DATABASE_URL -e CODE_VITALS_WRITER_PASSWORD -e CODE_VITALS_READER_PASSWORD \
  "$image" migrate
echo "==> $env: deployed"
