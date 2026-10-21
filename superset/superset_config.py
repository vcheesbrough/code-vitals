"""Superset configuration for code-vitals (card #499).

Loaded from /app/pythonpath in the code-vitals-superset image. Every value
that differs per environment or is secret comes from the environment, set by
deploy/compose.yaml from the deployment's Woodpecker secrets; a missing one
fails startup rather than falling back to a default.
"""

import os
from datetime import timedelta
from typing import Any

from code_vitals_auth import AUTHENTIK_URL, environment_names, user_info_from_claims
from flask_appbuilder.exceptions import InvalidLoginAttempt
from flask_appbuilder.security.manager import AUTH_OAUTH
from superset.security import SupersetSecurityManager


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


NAMES = environment_names(_required("CODE_VITALS_ENV"))

SECRET_KEY = _required("SUPERSET_SECRET_KEY")
# The metadata database: its own role and database on the environment's
# Postgres (scripts/deploy-stack.sh creates both).
# The database host is the stack's own alias for its Postgres
# (deploy/compose.yaml), never the bare `postgres`: Superset is also on
# Traefik's shared proxy-backend network, where another stack's `postgres`
# could otherwise answer — with this password.
DB_HOST = _required("CODE_VITALS_DB_HOST")
SQLALCHEMY_DATABASE_URI = (
    f"postgresql+psycopg2://superset:{_required('SUPERSET_DB_PASSWORD')}@{DB_HOST}:5432/superset"
)

# Behind Traefik, which terminates TLS: trust its X-Forwarded-* headers so the
# OAuth redirect URI is built as https://<host>/…
ENABLE_PROXY_FIX = True
PREFERRED_URL_SCHEME = "https"
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# Group removal in Authentik takes effect at the next sign-in, so this bounds
# how long a removed user keeps access.
PERMANENT_SESSION_LIFETIME = timedelta(hours=12)

# No Redis: in-process caches are enough for one gunicorn worker.
CACHE_CONFIG = {"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300}
DATA_CACHE_CONFIG = CACHE_CONFIG

# --- sign-in: OpenID Connect through Authentik -------------------------------

AUTH_TYPE = AUTH_OAUTH
AUTH_USER_REGISTRATION = True
# Public holds no permissions, so a user whose groups map to nothing fails
# closed (and Authentik's policy binding refuses non-members before that).
AUTH_USER_REGISTRATION_ROLE = "Public"
AUTH_ROLES_SYNC_AT_LOGIN = True
AUTH_ROLES_MAPPING = {
    NAMES.users_group: ["Alpha"],
    NAMES.admins_group: ["Admin"],
}
OAUTH_PROVIDERS = [
    {
        "name": "authentik",
        "icon": "fa-key",
        "token_key": "access_token",
        "remote_app": {
            "client_id": NAMES.client_id,
            "client_secret": _required("AUTHENTIK_CLIENT_SECRET"),
            "server_metadata_url": NAMES.server_metadata_url,
            "api_base_url": f"{AUTHENTIK_URL}/application/o/",
            "client_kwargs": {
                "scope": "openid email profile",
                "code_challenge_method": "S256",
            },
        },
    }
]


class CodeVitalsSecurityManager(SupersetSecurityManager):
    def get_oauth_user_info(self, provider: str, resp: dict[str, Any]) -> dict[str, Any]:
        if provider != "authentik":
            return super().get_oauth_user_info(provider, resp)
        # FAB's (private) helper verifies the ID token's signature against
        # Authentik's JWKS from the discovery document and returns its claims;
        # scripts/check-superset-image.sh fails the build if a base-image
        # upgrade removes it. Anything but claims is a refused sign-in, never
        # a server error.
        claims = self._get_authentik_token_info(resp["id_token"])
        if not claims:
            raise InvalidLoginAttempt("Authentik ID token could not be verified")
        # Users are keyed on Authentik's username (`preferred_username`), which
        # is stable as long as Authentik's "allow users to change username"
        # stays off (its default).
        return user_info_from_claims(claims)


CUSTOM_SECURITY_MANAGER = CodeVitalsSecurityManager
