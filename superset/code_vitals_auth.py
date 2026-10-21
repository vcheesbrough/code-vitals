"""Pure helpers for Superset's sign-in, kept free of Superset imports so the
collector's test suite can test them (tests/test_superset_auth.py).

superset_config.py imports this module from /app/pythonpath in the image.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

AUTHENTIK_URL = "https://auth.desync.link"


@dataclass(frozen=True)
class EnvironmentNames:
    """Everything that differs between dev and prod, derived from one name."""

    host: str
    client_id: str  # also the Authentik application slug
    users_group: str
    admins_group: str

    @property
    def server_metadata_url(self) -> str:
        return f"{AUTHENTIK_URL}/application/o/{self.client_id}/.well-known/openid-configuration"

    @property
    def redirect_uri(self) -> str:
        return f"https://{self.host}/oauth-authorized/authentik"


def environment_names(env: str) -> EnvironmentNames:
    if env == "prod":
        base = "code-vitals"
    elif env == "dev":
        base = "code-vitals-dev"
    else:
        raise ValueError(f"CODE_VITALS_ENV must be dev or prod, got {env!r}")
    return EnvironmentNames(
        host=f"{base}.desync.link",
        client_id=base,
        users_group=f"{base}-users",
        admins_group=f"{base}-admins",
    )


def user_info_from_claims(claims: Mapping[str, Any]) -> dict[str, Any]:
    """Map verified Authentik ID-token claims to Flask-AppBuilder's user info.

    FAB 5's built-in `authentik` mapping uses `preferred_username` as the email
    and `nickname` as the username; this uses each claim for what it is.
    `groups` become role keys, which AUTH_ROLES_MAPPING turns into roles.
    """
    username = claims.get("preferred_username")
    if not isinstance(username, str) or not username:
        raise ValueError("ID token has no preferred_username")
    groups = claims.get("groups") or []
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        raise ValueError("ID token groups claim is not a list of names")
    full_name = claims.get("name") or claims.get("given_name") or ""
    first, _, last = str(full_name).partition(" ")
    return {
        "username": username,
        "email": claims.get("email") or "",
        "first_name": first or username,
        "last_name": last,
        "role_keys": groups,
    }
