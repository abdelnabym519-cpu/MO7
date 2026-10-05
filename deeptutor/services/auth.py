"""
Authentication service for DeepTutor.

Disabled by default (auth.enabled=false) so localhost users are unaffected.
When enabled, guards all API routes with JWT bearer tokens.

Quick setup (single user via data/user/settings/auth.json):
    1. Set enabled=true
    2. Set username=<your username>
    3. Generate a password hash:
           python -c "from deeptutor.services.auth import hash_password; print(hash_password('yourpassword'))"
       Paste the output into password_hash=<hash>

Multi-user setup (recommended):
    Enable auth and leave username/password_hash empty.
    Navigate to /register in the browser. The first user to register is granted
    admin privileges and can manage other users from /admin/users.

    Users are stored in data/user/auth_users.json:
        {
            "alice": {"hash": "$2b$12$...", "role": "admin", "created_at": "2026-..."},
            "bob":   {"hash": "$2b$12$...", "role": "user",  "created_at": "2026-..."}
        }
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from deeptutor.multi_user.models import AccountPreset, Role
from deeptutor.services.config import load_auth_settings, load_integrations_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — read once at import time from runtime JSON settings
# ---------------------------------------------------------------------------

_AUTH_SETTINGS = load_auth_settings()
_INTEGRATIONS_SETTINGS = load_integrations_settings()

AUTH_ENABLED: bool = bool(_AUTH_SETTINGS["enabled"])
AUTH_USERNAME: str = str(_AUTH_SETTINGS["username"])
AUTH_PASSWORD_HASH: str = str(_AUTH_SETTINGS["password_hash"])
AUTH_SECRET: str = ""
TOKEN_EXPIRE_HOURS: int = int(_AUTH_SETTINGS["token_expire_hours"])

# PocketBase auth mode — active when integrations.pocketbase_url is set and auth is enabled.
# When enabled, login/register proxy to PocketBase and token validation uses
# PocketBase's auth-refresh endpoint (cached in memory — no static secret needed).
POCKETBASE_BASE_URL: str = str(_INTEGRATIONS_SETTINGS["pocketbase_url"]).rstrip("/")
POCKETBASE_ENABLED: bool = bool(POCKETBASE_BASE_URL) and AUTH_ENABLED

_ALGORITHM = "HS256"


if AUTH_ENABLED and not POCKETBASE_ENABLED and not AUTH_SECRET:
    from deeptutor.multi_user.identity import load_or_create_auth_secret

    AUTH_SECRET = load_or_create_auth_secret()


# ---------------------------------------------------------------------------
# Token payload
# ---------------------------------------------------------------------------


@dataclass
class TokenPayload:
    """Decoded JWT payload."""

    username: str
    role: str
    user_id: str = ""
    device_credential_id: str = ""
    device_session_nonce: str = ""


# ---------------------------------------------------------------------------
# Password hashing — uses bcrypt directly (passlib is unmaintained for bcrypt 4+)
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    """Hash a plaintext password. Use this to generate password hashes."""
    import bcrypt

    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a stored bcrypt hash."""
    import bcrypt

    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# User store — multi-user JSON store plus optional auth.json bootstrap user
# ---------------------------------------------------------------------------


def _make_user_record(
    hashed: str,
    role: str = "user",
    created_at: str = "",
    preset: str = "standard",
) -> dict[str, Any]:
    """Build a canonical user record dict for legacy callers/tests."""
    from deeptutor.multi_user.identity import new_user_id

    return {
        "id": new_user_id(),
        "hash": hashed,
        "role": role,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "disabled": False,
        "avatar": "",
        "preset": preset,
    }


def _load_users() -> dict[str, dict]:
    """
    Load the user store, migrating old flat format if needed.

    Priority:
      1. multi-user identity store
      2. auth.json username + password_hash — single-user bootstrap user

    Old format: {"alice": "$2b$12$..."}
    New format: {"alice": {"hash": "...", "role": "admin", "created_at": "..."}}
    """
    from deeptutor.multi_user.identity import load_users

    return load_users(AUTH_USERNAME, AUTH_PASSWORD_HASH)


def is_first_user() -> bool:
    """Return True when no users exist yet (first registration will become admin)."""
    return len(_load_users()) == 0


def add_user(
    username: str,
    plain_password: str,
    role: Role = "user",
    preset: AccountPreset = "standard",
) -> None:
    """
    Add or update a user in data/user/auth_users.json.

    The role defaults to 'user'. Pass role='admin' to elevate. When the store
    is empty the first user is automatically promoted to 'admin' regardless of
    the role argument.

    Creates the file (and parent directories) if they don't exist.
    """
    from deeptutor.multi_user.identity import save_user

    record = save_user(
        username,
        hash_password(plain_password),
        role=role,
        preset=preset,
    )
    logger.info(
        "User '%s' saved with role=%r preset=%r",
        username,
        record.get("role", "user"),
        record.get("preset", "standard"),
    )


def list_users() -> list[dict]:
    """Return a list of user info dicts (username, role, created_at) — no hashes."""
    from deeptutor.multi_user.identity import list_user_info

    return list_user_info(AUTH_USERNAME, AUTH_PASSWORD_HASH)


def delete_user(username: str) -> bool:
    """
    Remove a user from the store. Returns True if the user existed.

    """
    from deeptutor.multi_user.identity import delete_user as _delete_user

    if not _delete_user(username):
        return False
    logger.info("User '%s' deleted", username)
    return True


def set_role(username: str, role: str) -> bool:
    """
    Change the role for an existing user. Returns True on success.

    Valid roles are the entries of ``VALID_ROLES`` (currently 'admin', 'user').
    """
    from deeptutor.multi_user.models import VALID_ROLES

    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role: {role!r}. Must be one of {sorted(VALID_ROLES)}.")

    from deeptutor.multi_user.identity import set_role as _set_role

    if not _set_role(username, role):  # type: ignore[arg-type]
        return False
    logger.info(f"User '{username}' role updated to {role!r}")
    return True


def set_avatar(username: str, avatar: str) -> bool:
    """
    Update the avatar marker for an existing user. Returns True on success.

    The marker is either '' (deterministic fallback), 'icon:<name>:<color>',
    or 'img:<version>' (managed by the avatar upload endpoint).
    """
    from deeptutor.multi_user.identity import set_avatar as _set_avatar

    if not _set_avatar(username, avatar):
        return False
    logger.info("User '%s' avatar updated", username)
    return True


def get_user_info(username: str) -> dict | None:
    """Return the public info dict for a single user, or None if unknown."""
    for item in list_users():
        if item.get("username") == username:
            return item
    return None


def get_learner_profile(username: str) -> dict[str, Any] | None:
    """Return the structured learner profile for an existing account."""
    from deeptutor.multi_user.identity import get_learner_profile as _get_profile

    return _get_profile(username)


def set_learner_profile(username: str, profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """Replace the structured learner profile for an existing account."""
    from deeptutor.multi_user.identity import set_learner_profile as _set_profile

    return _set_profile(username, profile)


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


def create_token(
    username: str,
    role: str = "user",
    user_id: str | None = None,
    device_credential_id: str = "",
    device_session_nonce: str = "",
) -> str:
    """Create a signed JWT for the given username and role."""
    from jose import jwt

    if not user_id:
        record = _load_users().get(username) or {}
        user_id = str(record.get("id") or "")

    payload = {
        "sub": username,
        "role": role,
        "uid": user_id,
        "dcid": device_credential_id,
        "dcs": device_session_nonce,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, AUTH_SECRET, algorithm=_ALGORITHM)


def decode_token(token: str) -> TokenPayload | None:
    """
    Validate a token and return a TokenPayload, or None if invalid.

    - PocketBase mode: calls PocketBase's auth-refresh endpoint (cached in
      memory for 60 s, so only the first request per token per minute makes
      a network call). No static JWT secret required.
    - Standard mode: local in-memory jwt.decode() using AUTH_SECRET — zero
      network calls, same as before.
    """
    if not token:
        return None

    if POCKETBASE_ENABLED:
        from deeptutor.services.pocketbase_client import validate_pb_token

        payload = validate_pb_token(token)
        if payload is None:
            return None
        return TokenPayload(
            username=payload["username"],
            role=payload.get("role", "user"),
            user_id=str(payload.get("id") or payload.get("uid") or payload.get("user_id") or ""),
        )

    # Standard JWT + bcrypt mode
    from jose import JWTError, jwt

    if not AUTH_SECRET:
        return None

    try:
        # ``require_exp`` rejects a correctly signed token that carries no
        # expiry at all — without it such a token would never expire. Every
        # minter in this codebase sets ``exp`` (see ``create_token``).
        payload = jwt.decode(
            token,
            AUTH_SECRET,
            algorithms=[_ALGORITHM],
            options={"require_exp": True},
        )
        username = payload.get("sub")
        if not username:
            return None
        # The stored account record is authoritative for identity, role and
        # availability, so deleting, demoting or disabling an account takes
        # effect on the next request instead of at token expiry (12 h by
        # default). The signature check above still proves this deployment
        # issued the token; the record decides what it may still do.
        record = _load_users().get(str(username))
        if record is None or bool(record.get("disabled")):
            return None
        user_id = str(record.get("id") or payload.get("uid") or "")
        if not user_id:
            return None
        device_credential_id = str(payload.get("dcid") or "")
        device_session_nonce = str(payload.get("dcs") or "")
        if device_credential_id:
            from deeptutor.multi_user.device_credentials import validate_device_token

            if not validate_device_token(
                user_id,
                device_credential_id,
                device_session_nonce,
            ):
                return None
        return TokenPayload(
            username=username,
            # The stored role, not the claim: a demotion must not survive in an
            # already-issued token.
            role=str(record.get("role") or payload.get("role") or "user"),
            user_id=user_id,
            device_credential_id=device_credential_id,
            device_session_nonce=device_session_nonce,
        )
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# Failed-login throttle
# ---------------------------------------------------------------------------

#: Failed sign-ins allowed for one (account, caller) pair per window, seconds.
#: bcrypt costs ~270 ms per verification, so an unthrottled endpoint is both an
#: online password-guessing surface and a CPU-exhaustion lever. Ten attempts
#: per five minutes leaves a person who mistyped their password multiple tries
#: while making guessing impractical; a successful sign-in clears the counter.
LOGIN_ATTEMPT_LIMIT = (10, 300)


class LoginThrottled(Exception):
    """Raised when an account has too many recent failed sign-in attempts."""


def _login_throttle_path():
    """Counter database beside the other auth state (``data/system/auth``)."""
    from pathlib import Path

    from deeptutor.multi_user.identity import AUTH_DIR

    return Path(AUTH_DIR) / "login_attempts.sqlite3"


def _login_throttle_connect():
    import sqlite3

    path = _login_throttle_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            bucket TEXT PRIMARY KEY,
            window_start INTEGER NOT NULL,
            count INTEGER NOT NULL
        )
        """
    )
    return connection


def _login_bucket(username: str, client_key: str) -> str:
    return f"{str(username).strip().lower()}|{client_key}"


def check_login_throttle(username: str, client_key: str) -> None:
    """Raise :class:`LoginThrottled` once the failure window is exhausted.

    A counter that cannot be read does not block sign-in: this is a secondary
    control behind bcrypt, and an unwritable counter file must not lock every
    account out of a running deployment. The failure is logged instead.
    """
    import time

    limit, window = LOGIN_ATTEMPT_LIMIT
    now = int(time.time())
    window_start = now - (now % window)
    try:
        connection = _login_throttle_connect()
    except Exception as exc:  # pragma: no cover - filesystem failure path
        logger.warning("Login throttle unavailable (%s); allowing the attempt", exc)
        return
    try:
        row = connection.execute(
            "SELECT window_start, count FROM login_attempts WHERE bucket = ?",
            (_login_bucket(username, client_key),),
        ).fetchone()
    except Exception as exc:  # pragma: no cover - corrupt counter file
        logger.warning("Login throttle read failed (%s); allowing the attempt", exc)
        return
    finally:
        connection.close()
    if row is None or int(row["window_start"]) != window_start:
        return
    if int(row["count"]) >= limit:
        raise LoginThrottled("Too many failed sign-in attempts. Try again in a few minutes.")


def record_failed_login(username: str, client_key: str) -> None:
    """Count one failed sign-in; throttling read is the next request's job."""
    import time

    _, window = LOGIN_ATTEMPT_LIMIT
    now = int(time.time())
    window_start = now - (now % window)
    try:
        connection = _login_throttle_connect()
    except Exception as exc:  # pragma: no cover - filesystem failure path
        logger.warning("Login throttle unavailable (%s); failure not counted", exc)
        return
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO login_attempts(bucket, window_start, count)
            VALUES (?, ?, 1)
            ON CONFLICT(bucket) DO UPDATE SET
                window_start=excluded.window_start,
                count=CASE
                    WHEN login_attempts.window_start != excluded.window_start
                    THEN 1 ELSE login_attempts.count + 1
                END
            """,
            (_login_bucket(username, client_key), window_start),
        )
        connection.execute("DELETE FROM login_attempts WHERE window_start + 86400 < ?", (now,))
        connection.commit()
    except Exception as exc:  # pragma: no cover - filesystem failure path
        logger.warning("Login throttle write failed (%s)", exc)
    finally:
        connection.close()


def clear_login_throttle(username: str, client_key: str) -> None:
    """Drop the counter after a successful sign-in."""
    try:
        connection = _login_throttle_connect()
    except Exception as exc:  # pragma: no cover - filesystem failure path
        logger.warning("Login throttle unavailable (%s); counter not cleared", exc)
        return
    try:
        connection.execute(
            "DELETE FROM login_attempts WHERE bucket = ?",
            (_login_bucket(username, client_key),),
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("Login throttle clear failed (%s)", exc)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# PocketBase auth helpers
# ---------------------------------------------------------------------------


def authenticate_pb(username: str, password: str) -> tuple[TokenPayload, str] | None:
    """
    Authenticate against PocketBase and return (TokenPayload, raw_pb_token).

    Only called when POCKETBASE_ENABLED=True.
    Returns None on failure.
    The raw token is the PocketBase JWT string to be stored in the cookie.

    PocketBase requires an email address; plain usernames are mapped to
    <username>@deeptutor.local to match the email used at registration.
    """
    try:
        from deeptutor.services.pocketbase_client import get_pb_client

        pb = get_pb_client()
        result = pb.collection("users").auth_with_password(username, password)
        token: str = result.token
        record = result.record
        username = (
            getattr(record, "email", None)
            or getattr(record, "name", None)
            or getattr(record, "id", "unknown")
        )
        # PocketBase has no built-in "role" field by default; treat all as "user".
        # Admins authenticated via PocketBase admin panel use a separate endpoint.
        role = getattr(record, "role", "user") or "user"
        user_id = str(getattr(record, "id", "") or "")
        return TokenPayload(username=str(username), role=str(role), user_id=user_id), token
    except Exception as exc:
        logger.warning(f"PocketBase authentication failed: {exc}")
        return None


def register_pb(username: str, email: str, password: str) -> dict | None:
    """
    Create a new user in PocketBase.

    Returns the created user record dict or None on failure.
    """
    try:
        from deeptutor.services.pocketbase_client import get_pb_client

        pb = get_pb_client()
        record = pb.collection("users").create(
            {
                "username": username,
                "email": email,
                "password": password,
                "passwordConfirm": password,
            }
        )
        return {"id": record.id, "username": username, "email": email}
    except Exception as exc:
        logger.warning(f"PocketBase registration failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Main auth entry point
# ---------------------------------------------------------------------------


def authenticate(username: str, password: str) -> TokenPayload | None:
    """
    Validate credentials. Returns a TokenPayload on success, None on failure.

    When auth is disabled, always returns a dummy admin payload so that
    callers don't need to special-case the disabled state.
    """
    if not AUTH_ENABLED:
        return TokenPayload(username=username or "local", role="admin", user_id="local-admin")

    users = _load_users()
    if not users:
        logger.warning(
            "No users configured — login will always fail. "
            "Navigate to /register to create your first account."
        )
        return None

    record = users.get(username)
    if not record:
        return None

    hashed = record.get("hash", "") if isinstance(record, dict) else record
    if not verify_password(password, hashed):
        return None

    role = record.get("role", "user") if isinstance(record, dict) else "user"
    user_id = str(record.get("id") or "") if isinstance(record, dict) else ""
    return TokenPayload(username=username, role=role, user_id=user_id)


def authenticate_device(pairing_code: str, pin: str) -> TokenPayload | None:
    """Exchange a learner device credential for the account's normal JWT identity."""

    if not AUTH_ENABLED or POCKETBASE_ENABLED:
        return None
    from deeptutor.multi_user.device_credentials import begin_device_session

    session = begin_device_session(pairing_code, pin)
    if session is None:
        return None
    _view, username, role, user_id, session_nonce = session
    return TokenPayload(
        username=username,
        role=role,
        user_id=user_id,
        device_credential_id=str(_view["id"]),
        device_session_nonce=session_nonce,
    )
