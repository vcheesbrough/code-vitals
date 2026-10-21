#!/bin/sh
# Prints the release tag for the image this push/manual pipeline builds:
# MAJOR.MINOR from pyproject.toml, PATCH = this pipeline's CI_PIPELINE_NUMBER.
# Ported from bored's scripts/compute-release-tag.sh (bored card #461).
#
# Pipeline numbers are unique and increasing per repo, so no two builds share
# a version and nothing has to be reserved in git first: the git tag is pushed
# only after a successful deploy. Deployments do not use this script — they
# find the build to deploy through the commit.
#
# Usage: compute-release-tag.sh [path/to/pyproject.toml]   (default: ./pyproject.toml)
# Reads CI_PIPELINE_EVENT, CI_PIPELINE_NUMBER.

set -eu

pyproject="${1:-pyproject.toml}"

# MAJOR.MINOR of `[project].version`; its patch is a placeholder. Only that
# table is searched: a tool's or dependency group's `version =` must not be
# picked up.
major_minor="$(
    sed -n '/^\[project\]/,/^\[/{
        s/^version[[:space:]]*=[[:space:]]*"\([0-9][0-9]*\.[0-9][0-9]*\)\.[0-9][0-9]*"[[:space:]]*$/\1/p
    }' "$pyproject"
)"
if [ -z "$major_minor" ]; then
    echo "ERROR: no MAJOR.MINOR.PATCH version in [project] of $pyproject" >&2
    exit 1
fi

case "${CI_PIPELINE_EVENT:-}" in
    push | manual) ;;
    *)
        echo "ERROR: unsupported CI_PIPELINE_EVENT '${CI_PIPELINE_EVENT:-}' (expected push or manual)" >&2
        exit 1
        ;;
esac

build_number="${CI_PIPELINE_NUMBER:-}"
# A positive integer without leading zeros, so the tag is valid semver.
case "$build_number" in
    '' | 0* | *[!0-9]*)
        echo "ERROR: CI_PIPELINE_NUMBER='$build_number' is not a pipeline number" >&2
        exit 1
        ;;
esac

echo "$major_minor.$build_number"
