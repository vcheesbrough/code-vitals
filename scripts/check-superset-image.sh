#!/bin/sh
# Assert that the code-vitals-superset image carries what code-vitals needs
# and that its configuration loads exactly when a deployment's variables are
# set:
#   - the Postgres driver and authlib import;
#   - superset_config loads with CODE_VITALS_ENV=dev and dummy secrets, mapping
#     the dev groups to Alpha/Admin and using our security manager;
#   - the private FAB helper the sign-in relies on still exists;
#   - it refuses to load when a required variable is missing.
#
# Usage: check-superset-image.sh <image-ref>
set -eu

IMAGE="${1:?usage: check-superset-image.sh <image-ref>}"

echo "==> superset config in $IMAGE"
out=$(docker run --rm --entrypoint python \
  -e CODE_VITALS_ENV=dev -e SUPERSET_SECRET_KEY=x -e SUPERSET_DB_PASSWORD=y -e AUTHENTIK_CLIENT_SECRET=z \
  -e CODE_VITALS_DB_HOST=db.example \
  "$IMAGE" -c '
import psycopg2, authlib, superset_config as c
assert c.AUTH_TYPE == 4, c.AUTH_TYPE  # AUTH_OAUTH
assert c.AUTH_ROLES_MAPPING == {"code-vitals-dev-users": ["Alpha"], "code-vitals-dev-admins": ["Admin"]}, c.AUTH_ROLES_MAPPING
assert c.AUTH_USER_REGISTRATION_ROLE == "Public"
app = c.OAUTH_PROVIDERS[0]["remote_app"]
assert app["client_id"] == "code-vitals-dev"
assert app["client_kwargs"]["code_challenge_method"] == "S256"
assert c.CUSTOM_SECURITY_MANAGER.__name__ == "CodeVitalsSecurityManager"
assert c.DB_HOST == "db.example" and "@db.example:5432/superset" in c.SQLALCHEMY_DATABASE_URI
# Sign-in relies on this private Flask-AppBuilder helper (signature check).
assert callable(getattr(c.CUSTOM_SECURITY_MANAGER, "_get_authentik_token_info", None)), "FAB _get_authentik_token_info is gone"
print("config ok")
' 2>&1) || { echo "$out" >&2; echo "ERROR: superset config did not load as expected" >&2; exit 1; }
echo "  $out"

if docker run --rm --entrypoint python -e CODE_VITALS_ENV=dev "$IMAGE" -c 'import superset_config' > /dev/null 2>&1; then
  echo "ERROR: superset config loaded without its secrets" >&2
  exit 1
fi
echo "  refuses to load without its secrets"
echo "==> OK"
