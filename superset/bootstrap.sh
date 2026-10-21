#!/bin/sh
# One-off Superset setup, run in a throwaway container of the deployed image on
# every deploy (scripts/deploy-stack.sh). Each step is idempotent:
#   1. migrate Superset's metadata database;
#   2. create/refresh Superset's built-in roles and permissions;
#   3. point the `code-vitals` database connection at the reader role.
#
# CODE_VITALS_READER_URI comes from the environment; it holds the reader's
# password, so it is expanded here inside the container and never appears on
# the docker command line.
set -eu

: "${CODE_VITALS_READER_URI:?CODE_VITALS_READER_URI is required}"

superset db upgrade
superset init
superset set-database-uri --database_name code-vitals --uri "$CODE_VITALS_READER_URI"
echo "superset bootstrap complete"
