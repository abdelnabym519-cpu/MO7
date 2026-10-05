"""A token stops working when the account behind it does.

``decode_token`` resolves the account record on every request, so deleting,
demoting or disabling an account takes effect immediately instead of lasting
until the token expires (12 h by default). These tests pin that contract
against the real service functions and the real user store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeptutor.multi_user import identity
from deeptutor.services import auth as auth_service


@pytest.fixture
def user_store(tmp_path: Path, monkeypatch) -> Path:
    """An isolated identity store with one admin and one regular account."""
    users_file = tmp_path / "system" / "auth" / "users.json"
    monkeypatch.setattr(identity, "USERS_FILE", users_file)
    monkeypatch.setattr(identity, "AUTH_DIR", users_file.parent)
    monkeypatch.setattr(identity, "SECRET_FILE", users_file.parent / "auth_secret")
    monkeypatch.setattr(identity, "LEGACY_USERS_FILE", tmp_path / "no-legacy.json")
    monkeypatch.setattr(auth_service, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_service, "AUTH_SECRET", "revocation-test-secret")
    monkeypatch.setattr(auth_service, "POCKETBASE_ENABLED", False)
    users_file.parent.mkdir(parents=True, exist_ok=True)
    users_file.write_text(
        json.dumps(
            {
                "root": {
                    "id": "u_root",
                    "hash": auth_service.hash_password("root-password"),
                    "role": "admin",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "disabled": False,
                    "avatar": "",
                    "preset": "standard",
                },
                "learner": {
                    "id": "u_learner",
                    "hash": auth_service.hash_password("learner-password"),
                    "role": "user",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "disabled": False,
                    "avatar": "",
                    "preset": "standard",
                },
            }
        ),
        encoding="utf-8",
    )
    return users_file


def _write(users_file: Path, username: str, **changes) -> None:
    users = json.loads(users_file.read_text(encoding="utf-8"))
    users[username].update(changes)
    users_file.write_text(json.dumps(users), encoding="utf-8")


def test_token_resolves_the_current_role(user_store: Path) -> None:
    token = auth_service.create_token("learner", "user", "u_learner")

    payload = auth_service.decode_token(token)

    assert payload is not None
    assert (payload.username, payload.role, payload.user_id) == ("learner", "user", "u_learner")


def test_demoted_account_loses_admin_on_its_existing_token(user_store: Path) -> None:
    """Promotion, the token it issued, then the demotion an admin performed."""
    _write(user_store, "learner", role="admin")
    token = auth_service.create_token("learner", "admin", "u_learner")
    assert auth_service.decode_token(token).role == "admin"  # type: ignore[union-attr]

    _write(user_store, "learner", role="user")

    payload = auth_service.decode_token(token)
    assert payload is not None
    assert payload.role == "user"


def test_a_claim_alone_cannot_grant_a_role(user_store: Path) -> None:
    """A correctly signed token that claims admin for a plain account."""
    token = auth_service.create_token("learner", "admin", "u_learner")

    payload = auth_service.decode_token(token)

    assert payload is not None
    assert payload.role == "user"


def test_deleted_account_token_is_refused(user_store: Path) -> None:
    token = auth_service.create_token("learner", "user", "u_learner")
    assert auth_service.decode_token(token) is not None

    users = json.loads(user_store.read_text(encoding="utf-8"))
    users.pop("learner")
    user_store.write_text(json.dumps(users), encoding="utf-8")

    assert auth_service.decode_token(token) is None


def test_disabled_account_token_is_refused(user_store: Path) -> None:
    token = auth_service.create_token("learner", "user", "u_learner")

    _write(user_store, "learner", disabled=True)

    assert auth_service.decode_token(token) is None


def test_token_for_an_account_that_never_existed_is_refused(user_store: Path) -> None:
    """The store, not the token claim, decides which identities exist."""
    forged = auth_service.create_token("ghost", "admin", "u_ghost")

    assert auth_service.decode_token(forged) is None


def test_a_correctly_signed_token_without_expiry_is_refused(user_store: Path) -> None:
    """A token with no ``exp`` would never expire; decode requires the claim."""
    from jose import jwt

    no_expiry = jwt.encode(
        {"sub": "learner", "role": "user", "uid": "u_learner"},
        auth_service.AUTH_SECRET,
        algorithm=auth_service._ALGORITHM,
    )

    assert auth_service.decode_token(no_expiry) is None


def test_signature_still_decides_whether_a_token_is_ours(user_store: Path) -> None:
    """Record lookup must not turn any signed payload into an identity."""
    from jose import jwt

    foreign = jwt.encode(
        {"sub": "learner", "role": "admin", "uid": "u_learner", "exp": 4_000_000_000},
        "some-other-deployments-secret",
        algorithm="HS256",
    )

    assert auth_service.decode_token(foreign) is None
