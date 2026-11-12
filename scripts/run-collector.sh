#!/bin/sh
# Run the deployed collector image against one environment's database, after
# scripts/deploy-stack.sh has migrated it (card #497):
#
#   run-collector.sh <dev|prod> <collector-image-ref> repo <name>
#       clone the repo (full history; it is public, so no credentials), catch
#       it up with `collect --trigger backfill`, then `check-repo` on the same
#       clone: a second catch-up finds nothing, and the fixed-commit values
#       match src/code_vitals/expected_values.toml. One step per repo, in
#       parallel; each repo's runs are serialized by its advisory lock.
#   run-collector.sh <dev|prod> <collector-image-ref> status
#       `check-status`, once every repo step is done: every version this
#       build registers is complete, and every repo's last run succeeded.
#
# Runs as the writer, on the project's network, by digest — never :stable. The
# container is also on proxy-backend so its telemetry reaches monitor-alloy,
# with deployment.environment.name set to the environment.
#
# Environment (from Woodpecker secrets; never printed): CODE_VITALS_WRITER_PASSWORD
# Run from the repository root.
#
# Nothing here reads input unless redirected (see scripts/deploy-stack.sh).

set -eu
exec < /dev/null

usage="usage: run-collector.sh <dev|prod> <collector-image-ref> (repo <name> | status)"
env="${1:?$usage}"
image="${2:?$usage}"
mode="${3:?$usage}"

case "$env" in
  prod) project=code-vitals ;;
  dev) project=code-vitals-dev ;;
  *)
    echo "ERROR: environment must be dev or prod (got '$env')" >&2
    exit 2
    ;;
esac

case "$mode" in
  repo)
    repo="${4:?$usage}"
    case "$repo" in
      # The repos of src/code_vitals/repos.py (tests/test_pipelines.py checks).
      v-note | bored | sovereign-config) ;;
      *)
        echo "ERROR: unknown repo '$repo'" >&2
        exit 2
        ;;
    esac
    url="https://github.com/vcheesbrough/$repo.git"
    ;;
  status) ;;
  *)
    echo "$usage" >&2
    exit 2
    ;;
esac

password=$(printenv CODE_VITALS_WRITER_PASSWORD || true)
if [ -z "$password" ]; then
  echo "ERROR: CODE_VITALS_WRITER_PASSWORD is required" >&2
  exit 1
fi
case "$password" in
  *[!A-Za-z0-9._~-]*)
    echo "ERROR: CODE_VITALS_WRITER_PASSWORD may contain only A-Z a-z 0-9 . _ ~ - (it goes into a URL)" >&2
    exit 1
    ;;
esac

CODE_VITALS_DATABASE_URL="postgresql://code_vitals_writer:$password@$project-db:5432/code_vitals"
OTEL_EXPORTER_OTLP_ENDPOINT=http://monitor-alloy:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_RESOURCE_ATTRIBUTES="deployment.environment.name=$env"
export CODE_VITALS_DATABASE_URL OTEL_EXPORTER_OTLP_ENDPOINT OTEL_EXPORTER_OTLP_PROTOCOL \
  OTEL_RESOURCE_ATTRIBUTES
# Woodpecker sets it in every step; the run's span records it.
CI_PIPELINE_NUMBER="${CI_PIPELINE_NUMBER:-}"
export CI_PIPELINE_NUMBER

# `-e NAME` copies each value from this environment, so the password is on no
# command line. create + connect + start, as `docker run` joins one network.
set -- -e CODE_VITALS_DATABASE_URL -e OTEL_EXPORTER_OTLP_ENDPOINT \
  -e OTEL_EXPORTER_OTLP_PROTOCOL -e OTEL_RESOURCE_ATTRIBUTES -e CI_PIPELINE_NUMBER \
  --network "${project}_default"
if [ "$mode" = repo ]; then
  echo "==> $env: collect $repo"
  # The clone lives and dies with the container.
  # shellcheck disable=SC2016  # expanded by the container's shell
  cid=$(docker create "$@" --entrypoint sh "$image" -c '
    set -eu
    git clone --quiet "$1" /tmp/clone
    code-vitals collect --repo "$2" --clone /tmp/clone --trigger backfill
    code-vitals check-repo --repo "$2" --clone /tmp/clone
  ' sh "$url" "$repo")
else
  echo "==> $env: check-status"
  cid=$(docker create "$@" "$image" check-status)
fi
# shellcheck disable=SC2064  # expand cid now, not at trap time
trap "docker rm -f '$cid' > /dev/null 2>&1 || true" EXIT
docker network connect proxy-backend "$cid"
docker start --attach "$cid"
