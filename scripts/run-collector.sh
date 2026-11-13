#!/bin/sh
# Run the deployed collector image against one environment's database, after
# scripts/deploy-stack.sh has migrated it (cards #497, #564):
#
#   run-collector.sh <dev|prod> <collector-image-ref> repos
#       every repo registered in the database (`list-repos`), in parallel, one
#       container each: `backfill-repo` clones it, reads its code-vitals
#       plugin settings from .woodpecker/code-vitals.yml at the trunk head,
#       catches it up with trigger=backfill and checks it (a second catch-up
#       finds nothing; its fixed-commit values, if any, match
#       src/code_vitals/expected_values.toml). A repo without the plugin step
#       is skipped, not failed. The repos collected are written to
#       .collected-repos for `status`. Each repo's runs are serialized by its
#       advisory lock.
#   run-collector.sh <dev|prod> <collector-image-ref> status
#       `check-status` over the repos in .collected-repos: every version this
#       build registers is complete, and each of those repos' last run
#       succeeded.
#
# Runs as the writer, on the project's network, by digest — never :stable. Each
# container is also on proxy-backend so its telemetry reaches monitor-alloy,
# with deployment.environment.name set to the environment.
#
# Environment (from Woodpecker secrets; never printed): CODE_VITALS_WRITER_PASSWORD
# Run from the repository root.
#
# Nothing here reads input unless redirected (see scripts/deploy-stack.sh).

set -eu
exec < /dev/null

usage="usage: run-collector.sh <dev|prod> <collector-image-ref> (repos | status)"
env="${1:?$usage}"
image="${2:?$usage}"
mode="${3:?$usage}"
collected=.collected-repos
# backfill-repo's exit status for a repo without a plugin step (cli.SKIPPED_EXIT).
skipped=3

case "$env" in
  prod) project=code-vitals ;;
  dev) project=code-vitals-dev ;;
  *)
    echo "ERROR: environment must be dev or prod (got '$env')" >&2
    exit 2
    ;;
esac

case "$mode" in
  repos) ;;
  status)
    if [ ! -f "$collected" ]; then
      echo "ERROR: $collected is missing: run '$0 $env <image> repos' first" >&2
      exit 1
    fi
    ;;
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

work=$(mktemp -d)
# Every container this script created is removed on any exit.
cleanup() {
  for f in "$work"/cid.*; do
    [ -f "$f" ] && docker rm -f "$(cat "$f")" > /dev/null 2>&1 || true
  done
  rm -rf "$work"
}
trap cleanup EXIT

# run <label> <command…> — the image's command in a container of its own, on
# the project's network and proxy-backend (`docker run` joins one network).
# `-e NAME` copies each value from this environment, so the password is on no
# command line.
run() {
  label=$1
  shift
  # Explicit returns: `set -e` does not apply where run's status is tested.
  cid=$(docker create -e CODE_VITALS_DATABASE_URL -e OTEL_EXPORTER_OTLP_ENDPOINT \
    -e OTEL_EXPORTER_OTLP_PROTOCOL -e OTEL_RESOURCE_ATTRIBUTES -e CI_PIPELINE_NUMBER \
    --network "${project}_default" "$image" "$@") || return
  echo "$cid" > "$work/cid.$label"
  docker network connect proxy-backend "$cid" || return
  docker start --attach "$cid"
}

if [ "$mode" = status ]; then
  set --
  while read -r name; do
    [ -n "$name" ] && set -- "$@" --repo "$name"
  done < "$collected"
  echo "==> $env: check-status $*"
  run status check-status "$@"
  exit
fi

echo "==> $env: list the registered repos"
run list list-repos > "$work/list"
grep -v '^repo ' "$work/list" || true
: > "$collected"
started=0
while read -r tag name url trunk rest; do
  [ "$tag" = repo ] || continue
  # list-repos writes names, URLs and trunks the database's checks allow;
  # refuse anything else rather than pass it to a shell or a file name.
  case "$name$trunk" in
    *[!A-Za-z0-9._/-]*)
      echo "ERROR: unexpected repo line: $tag $name $url $trunk $rest" >&2
      exit 1
      ;;
  esac
  case "$name" in */*)
    echo "ERROR: unexpected repo name: $name" >&2
    exit 1
    ;;
  esac
  echo "==> $env: backfill $name from $url ($trunk)"
  echo "$name" >> "$work/names"
  (
    status=0
    run "$name" backfill-repo --repo "$name" --url "$url" --trunk "$trunk" 2>&1 || status=$?
    echo "$status" > "$work/status.$name"
  ) | sed "s|^|$name: |" &
  started=$((started + 1))
done < "$work/list"
wait

failed=0
touch "$work/names"
while read -r name; do
  status=$(cat "$work/status.$name" 2> /dev/null || echo unknown)
  if [ "$status" = unknown ]; then
    echo "$name" >> "$collected"
    echo "==> $name: FAILED (no exit status)" >&2
    failed=$((failed + 1))
  elif [ "$status" -eq 0 ]; then
    echo "$name" >> "$collected"
    echo "==> $name: collected and checked"
  elif [ "$status" -eq "$skipped" ]; then
    echo "==> $name: skipped (no code-vitals plugin step)"
  else
    echo "$name" >> "$collected"
    echo "==> $name: FAILED (exit $status)" >&2
    failed=$((failed + 1))
  fi
done < "$work/names"
echo "==> $env: $started repo(s): $(wc -l < "$collected" | tr -d ' ') collected, $failed failed"
[ "$failed" -eq 0 ]
