"""Team logins: accounts, passwords and sessions.

Only active on the team server (config/server.yaml `enabled: true`). A copy
run on someone's own computer has no logins.

  * Passwords are stored as salted scrypt hashes (standard library), never
    as text.
  * A login creates a random session token sent as an HttpOnly cookie. The
    database keeps only a SHA-256 of it, so a copy of the database can't be
    used to log in as anyone.
  * After `max_failed_logins` wrong passwords the account locks for
    `lockout_minutes`. Messages never say whether the username or the
    password was wrong.
  * Roles: `admin` (price lists, users, updates) and `member` (quoting and
    orders).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from core import config, db, utcnow

COOKIE = "webuilt_session"
ROLES = ("admin", "member")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")

# scrypt cost: ~16 MB and a few tens of ms per check. Stored with each hash,
# so it can be raised later without breaking existing passwords.
_N, _R, _P = 2 ** 14, 8, 1


class AuthError(Exception):
    """A message that is safe to show to the person logging in."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings() -> dict:
    return config["server"]


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                             n=int(n), r=int(r), p=int(p), dklen=len(digest) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


_DUMMY: list[str] = []


def _dummy_hash() -> str:
    """A fixed hash to check unknown usernames against, made once, so a
    missing account costs the same single scrypt as a real one."""
    if not _DUMMY:
        _DUMMY.append(hash_password(secrets.token_hex(8)))
    return _DUMMY[0]


def check_new_password(password: str) -> None:
    need = int(_settings().get("min_password_length", 8))
    if len(password or "") < need:
        raise AuthError(f"Passwords need at least {need} characters.")


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

def _public(row) -> dict:
    return {k: row[k] for k in ("id", "username", "display_name", "role", "active",
                                "created_at", "last_login")}


def create_user(username: str, password: str, role: str = "member",
                display_name: str | None = None) -> dict:
    username = (username or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise AuthError("Usernames are 3-32 characters: a-z, 0-9, dot, dash or "
                        "underscore, starting with a letter or digit.")
    if role not in ROLES:
        raise AuthError(f"Role must be one of {', '.join(ROLES)}.")
    check_new_password(password)
    with db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise AuthError(f"There is already a user called {username}.")
        cur = conn.execute(
            "INSERT INTO users (username, display_name, pw_hash, role, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (username, (display_name or "").strip() or username,
             hash_password(password), role, utcnow()))
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _public(row)


def list_users() -> list[dict]:
    with db() as conn:
        return [_public(r) for r in conn.execute("SELECT * FROM users ORDER BY username")]


def get_user(user_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _public(row) if row else None


def user_count() -> int:
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def _active_admins(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1").fetchone()[0]


def update_user(user_id: int, *, role: str | None = None, active: bool | None = None,
                display_name: str | None = None, password: str | None = None,
                keep_session: str | None = None) -> dict:
    """Change a user. Refuses to leave the team without an active admin.

    A new password signs the account out everywhere, except the session
    `keep_session` (the person changing their own password stays in)."""
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise AuthError("No such user.")
        losing_admin = row["role"] == "admin" and row["active"] and (
            (role is not None and role != "admin") or active is False)
        if losing_admin and _active_admins(conn) <= 1:
            raise AuthError("This is the only active admin. Make someone else an "
                            "admin first.")
        if role is not None:
            if role not in ROLES:
                raise AuthError(f"Role must be one of {', '.join(ROLES)}.")
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        if display_name is not None:
            conn.execute("UPDATE users SET display_name = ? WHERE id = ?",
                         (display_name.strip() or row["username"], user_id))
        if password is not None:
            check_new_password(password)
            conn.execute("UPDATE users SET pw_hash = ?, failed_logins = 0,"
                         " locked_until = NULL WHERE id = ?",
                         (hash_password(password), user_id))
            conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                         (user_id, _token_hash(keep_session) if keep_session else ""))
        if active is not None:
            conn.execute("UPDATE users SET active = ? WHERE id = ?", (int(active), user_id))
            if not active:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _public(row)


def change_own_password(user_id: int, current: str, new: str,
                        keep_session: str | None = None) -> None:
    with db() as conn:
        row = conn.execute("SELECT pw_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or not verify_password(current or "", row["pw_hash"]):
        raise AuthError("Your current password is not right.")
    update_user(user_id, password=new, keep_session=keep_session)


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def login(username: str, password: str) -> tuple[str, dict]:
    """Returns (session token, user). Raises AuthError with a safe message."""
    s = _settings()
    username = (username or "").strip().lower()
    wrong = AuthError("Wrong username or password.")
    # Decide inside the transaction, raise after it: raising inside would roll
    # back the failed-attempt count, and the lockout would never trigger.
    failure: AuthError | None = None
    token = None
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            verify_password(password or "", _dummy_hash())   # same time either way
            failure = wrong
        elif row["locked_until"] and datetime.fromisoformat(row["locked_until"]) > _now():
            failure = AuthError("Too many wrong passwords. Try again in a few minutes, "
                                "or ask an admin to reset it.")
        elif not verify_password(password or "", row["pw_hash"]):
            fails = row["failed_logins"] + 1
            locked = None
            if fails >= int(s.get("max_failed_logins", 5)):
                locked = (_now() + timedelta(minutes=int(s.get("lockout_minutes", 10)))
                          ).isoformat(timespec="seconds")
                fails = 0
            conn.execute("UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                         (fails, locked, row["id"]))
            failure = wrong
        elif not row["active"]:
            failure = wrong
        else:
            token = secrets.token_urlsafe(32)
            now = utcnow()
            expires = (_now() + timedelta(days=int(s.get("session_days", 14)))
                       ).isoformat(timespec="seconds")
            conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at,"
                         " last_seen) VALUES (?, ?, ?, ?, ?)",
                         (_token_hash(token), row["id"], now, expires, now))
            conn.execute("UPDATE users SET last_login = ?, failed_logins = 0,"
                         " locked_until = NULL WHERE id = ?", (now, row["id"]))
    if failure:
        raise failure
    return token, _public(row)


def user_for_token(token: str | None) -> dict | None:
    """The logged-in user for a session cookie, sliding its expiry forward."""
    if not token:
        return None
    th = _token_hash(token)
    with db() as conn:
        row = conn.execute(
            "SELECT u.*, s.expires_at, s.last_seen FROM sessions s"
            " JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (th,)).fetchone()
        if row is None:
            return None
        now = _now()
        if datetime.fromisoformat(row["expires_at"]) <= now or not row["active"]:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
            return None
        # Refresh at most every 10 minutes, not on every request.
        if now - datetime.fromisoformat(row["last_seen"]) > timedelta(minutes=10):
            expires = (now + timedelta(days=int(_settings().get("session_days", 14)))
                       ).isoformat(timespec="seconds")
            conn.execute("UPDATE sessions SET last_seen = ?, expires_at = ?"
                         " WHERE token_hash = ?", (utcnow(), expires, th))
    return _public(row)


def logout(token: str | None) -> None:
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def purge_expired_sessions() -> int:
    with db() as conn:
        return conn.execute("DELETE FROM sessions WHERE expires_at <= ?",
                            (utcnow(),)).rowcount
