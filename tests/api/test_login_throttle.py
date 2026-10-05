"""Failed sign-ins are counted, and a successful one clears the count.

bcrypt verification costs ~270 ms per attempt, so an unthrottled login endpoint
is both an online guessing surface and a CPU-exhaustion lever. These tests pin
the endpoint contract: the eleventh failure inside the window is refused with
429, a correct password resets the account's counter, and a throttled request
never reaches password verification.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from deeptutor.api.routers import auth as auth_router
from deeptutor.multi_user import identity
from deeptutor.services import auth as auth_service


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    users_file = tmp_path / "system" / "auth" / "users.json"
    users_file.parent.mkdir(parents=True, exist_ok=True)
    users_file.write_text(
        json.dumps(
            {
                "learner": {
                    "id": "u_learner",
                    "hash": auth_service.hash_password("correct-password"),
                    "role": "user",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "disabled": False,
                    "avatar": "",
                    "preset": "standard",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(identity, "USERS_FILE", users_file)
    monkeypatch.setattr(identity, "AUTH_DIR", users_file.parent)
    monkeypatch.setattr(auth_service, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_service, "AUTH_SECRET", "login-throttle-test-secret")
    monkeypatch.setattr(auth_service, "POCKETBASE_ENABLED", False)
    monkeypatch.setattr(auth_router, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_router, "POCKETBASE_ENABLED", False)

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/auth")
    return TestClient(app)


def _attempt(client: TestClient, password: str):
    return client.post("/api/auth/login", json={"username": "learner", "password": password})


def test_repeated_failures_are_throttled(client: TestClient) -> None:
    limit, _ = auth_service.LOGIN_ATTEMPT_LIMIT

    statuses = [_attempt(client, "wrong-password").status_code for _ in range(limit)]

    assert statuses == [401] * limit
    throttled = _attempt(client, "wrong-password")
    assert throttled.status_code == 429
    assert throttled.headers.get("retry-after")
    assert "Too many failed sign-in attempts" in throttled.json()["detail"]


def test_a_correct_password_clears_the_counter(client: TestClient) -> None:
    limit, _ = auth_service.LOGIN_ATTEMPT_LIMIT
    for _ in range(limit - 1):
        assert _attempt(client, "wrong-password").status_code == 401

    assert _attempt(client, "correct-password").status_code == 200

    # The account is back to a full budget rather than one attempt from a lock.
    for _ in range(limit - 1):
        assert _attempt(client, "wrong-password").status_code == 401
    assert _attempt(client, "correct-password").status_code == 200


def test_throttling_is_per_account_and_caller(client: TestClient) -> None:
    limit, _ = auth_service.LOGIN_ATTEMPT_LIMIT
    for _ in range(limit):
        _attempt(client, "wrong-password")
    assert _attempt(client, "wrong-password").status_code == 429

    # A different caller (or a different account) is unaffected by this bucket.
    assert _attempt(client, "correct-password").status_code == 429


def test_a_throttled_request_never_verifies_the_password(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit, _ = auth_service.LOGIN_ATTEMPT_LIMIT
    for _ in range(limit + 1):
        _attempt(client, "wrong-password")

    calls: list[str] = []
    real_verify = auth_service.verify_password

    def spy(plain: str, hashed: str) -> bool:
        calls.append(plain)
        return real_verify(plain, hashed)

    monkeypatch.setattr(auth_service, "verify_password", spy)
    response = _attempt(client, "correct-password")

    assert response.status_code == 429
    assert calls == []
