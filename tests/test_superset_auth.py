"""superset/code_vitals_auth.py: environment names and the claims mapping.

The module lives with the Superset image (it is copied to /app/pythonpath),
so it is loaded here by path; it imports nothing from Superset.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _load() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "superset" / "code_vitals_auth.py"
    spec = importlib.util.spec_from_file_location("code_vitals_auth", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


auth = _load()


def test_prod_names() -> None:
    names = auth.environment_names("prod")
    assert names.host == "code-vitals.desync.link"
    assert names.client_id == "code-vitals"
    assert names.users_group == "code-vitals-users"
    assert names.admins_group == "code-vitals-admins"
    assert names.redirect_uri == "https://code-vitals.desync.link/oauth-authorized/authentik"
    assert names.server_metadata_url == (
        "https://auth.desync.link/application/o/code-vitals/.well-known/openid-configuration"
    )


def test_dev_names_never_collide_with_prod() -> None:
    dev, prod = auth.environment_names("dev"), auth.environment_names("prod")
    assert dev.host == "code-vitals-dev.desync.link"
    assert dev.users_group == "code-vitals-dev-users"
    for field in ("host", "client_id", "users_group", "admins_group"):
        assert getattr(dev, field) != getattr(prod, field)


def test_an_unknown_environment_is_refused() -> None:
    with pytest.raises(ValueError, match="dev or prod"):
        auth.environment_names("staging")


def test_claims_map_each_to_what_it_is() -> None:
    info = auth.user_info_from_claims(
        {
            "preferred_username": "vincent",
            "nickname": "vin",
            "email": "v@example.com",
            "name": "Vincent Cheesbrough",
            "groups": ["code-vitals-users", "other"],
        }
    )
    assert info == {
        "username": "vincent",
        "email": "v@example.com",
        "first_name": "Vincent",
        "last_name": "Cheesbrough",
        "role_keys": ["code-vitals-users", "other"],
    }


def test_missing_groups_mean_no_roles() -> None:
    info = auth.user_info_from_claims({"preferred_username": "x"})
    assert info["role_keys"] == []
    assert info["first_name"] == "x"


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"preferred_username": ""},
        {"preferred_username": "x", "groups": "code-vitals-users"},
        {"preferred_username": "x", "groups": [1]},
    ],
)
def test_malformed_claims_are_refused(claims: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        auth.user_info_from_claims(claims)
