"""Multi-user accounts for the suchen/FULIN web service.

Accounts live in the same database as jobs and knowledge entries, so the
cloud deployment keeps every user-visible record in one place (PostgreSQL on
the free tier, SQLite when running locally).

Passwords are hashed with Werkzeug's PBKDF2 helper -- the same helper the
legacy single-account login already uses, so no new crypto dependency is
introduced and existing password hashes keep working.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from uuid import uuid4

from werkzeug.security import check_password_hash, generate_password_hash

from .db import connect

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8

# Public sign-up can be switched off for a private company deployment.
ALLOW_SIGNUP = os.getenv("FULIN_ALLOW_SIGNUP", "1").strip().lower() not in {"0", "false", "no"}

# Role given to accounts created through the sign-up form. A small team that
# shares one deployment can set FULIN_DEFAULT_ROLE=admin so every colleague
# may maintain the knowledge base; keep "member" for read-only staff accounts.
DEFAULT_ROLE = os.getenv("FULIN_DEFAULT_ROLE", "member").strip().lower() or "member"

USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'member',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login_at TEXT
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    with connect() as connection:
        connection.executescript(USERS_SCHEMA)


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def validate_credentials(email: str, password: str) -> None:
    email = normalize_email(email)
    if not EMAIL_PATTERN.match(email):
        raise ValueError("请输入有效的邮箱地址")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")


def _public_user(row: dict) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "role": row["role"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def count_users() -> int:
    ensure_schema()
    with connect() as connection:
        row = connection.execute("SELECT COUNT(*) AS total FROM users").fetchone()
    return int(row["total"] if row else 0)


def count_admins() -> int:
    ensure_schema()
    with connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM users WHERE role = 'admin' AND enabled = 1"
        ).fetchone()
    return int(row["total"] if row else 0)


def register_user(email: str, password: str, display_name: str = "") -> dict:
    """Create an account. Raises ValueError on invalid or duplicate input."""
    if not ALLOW_SIGNUP:
        raise ValueError("当前站点未开放注册，请联系管理员开通账号")
    ensure_schema()
    email = normalize_email(email)
    validate_credentials(email, password)
    now = _now()
    user_id = uuid4().hex
    password_hash = generate_password_hash(password)
    # A migrated database may already contain ordinary members from an older
    # build. The first enabled administrator still needs a safe bootstrap path.
    # Public registration never grants operations access. Existing owners grant it explicitly.
    role = "admin" if count_admins() == 0 else "member"
    try:
        with connect() as connection:
            connection.execute(
                """
                INSERT INTO users (id, email, password_hash, display_name, role, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    user_id,
                    email,
                    password_hash,
                    (display_name or "").strip()[:60],
                    role,
                    now,
                ),
            )
    except Exception as exc:  # noqa: BLE001 - unique violation on either backend
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise ValueError("该邮箱已注册，请直接登录") from exc
        raise
    return {
        "id": user_id,
        "email": email,
        "display_name": (display_name or "").strip()[:60],
        "role": role,
        "created_at": now,
        "last_login_at": None,
    }


def authenticate(email: str, password: str) -> dict | None:
    """Return the public user record when the credentials match."""
    ensure_schema()
    email = normalize_email(email)
    if not email or not password:
        return None
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE email = ? AND enabled = 1", (email,)
        ).fetchone()
    if row is None:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    with connect() as connection:
        connection.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?", (_now(), row["id"])
        )
    return _public_user(dict(row))


def get_user(user_id: str) -> dict | None:
    ensure_schema()
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE id = ? AND enabled = 1", (str(user_id or ""),)
        ).fetchone()
    return _public_user(dict(row)) if row else None


def get_user_by_email(email: str) -> dict | None:
    ensure_schema()
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE email = ? AND enabled = 1",
            (normalize_email(email),),
        ).fetchone()
    return _public_user(dict(row)) if row else None


def list_users(*, query: str = "", page: int = 1, page_size: int = 20) -> dict:
    """Return a paginated administrator-safe account list.

    Password hashes are deliberately never selected or returned.
    """

    ensure_schema()
    page = max(1, int(page))
    page_size = min(50, max(1, int(page_size)))
    value = f"%{str(query or '').strip().lower()}%"
    where = "WHERE lower(email) LIKE ? OR lower(display_name) LIKE ?" if query.strip() else ""
    params: tuple[object, ...] = (value, value) if where else ()
    with connect() as connection:
        total_row = connection.execute(f"SELECT COUNT(*) AS total FROM users {where}", params).fetchone()
        rows = connection.execute(
            f"""
            SELECT id, email, display_name, role, enabled, created_at, last_login_at
            FROM users {where}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, (page - 1) * page_size),
        ).fetchall()
    return {
        "items": [
            {
                "id": row["id"],
                "email": row["email"],
                "display_name": row["display_name"],
                "role": row["role"],
                "enabled": bool(row["enabled"]),
                "created_at": row["created_at"],
                "last_login_at": row["last_login_at"],
                "password_state": "已加密保存，不可查看",
            }
            for row in rows
        ],
        "page": page,
        "page_size": page_size,
        "total": int(total_row["total"] if total_row else 0),
    }


def reset_user_password(user_id: str, new_password: str) -> None:
    """Replace a password hash without ever exposing the previous password."""

    if len(new_password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"新密码至少 {MIN_PASSWORD_LENGTH} 位")
    ensure_schema()
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(new_password), str(user_id or "")),
        )
        if cursor.rowcount != 1:
            raise LookupError("账号不存在")


def update_user_access(
    user_id: str,
    *,
    role: str | None = None,
    enabled: bool | None = None,
    actor_user_id: str = "",
) -> dict:
    """Update one account without allowing lockout of the last administrator."""

    ensure_schema()
    if role is not None and role not in {"admin", "member"}:
        raise ValueError("权限只能设为负责人或成员")
    with connect() as connection:
        row = connection.execute("SELECT * FROM users WHERE id = ?", (str(user_id),)).fetchone()
        if row is None:
            raise LookupError("账号不存在")
        current = dict(row)
        next_role = role if role is not None else str(current["role"])
        next_enabled = bool(enabled) if enabled is not None else bool(current["enabled"])
        removes_admin = current["role"] == "admin" and bool(current["enabled"]) and (
            next_role != "admin" or not next_enabled
        )
        if str(user_id) == str(actor_user_id) and (not next_enabled or next_role != "admin"):
            raise ValueError("不能在当前会话中取消自己的负责人权限或停用自己")
        if removes_admin:
            count = connection.execute(
                "SELECT COUNT(*) AS total FROM users WHERE role = 'admin' AND enabled = 1"
            ).fetchone()
            if int(count["total"]) <= 1:
                raise ValueError("至少需要保留一个已启用的负责人账号")
        connection.execute(
            "UPDATE users SET role = ?, enabled = ? WHERE id = ?",
            (next_role, int(next_enabled), str(user_id)),
        )
    user = get_user(str(user_id)) if next_enabled else None
    return user or {
        "id": current["id"], "email": current["email"],
        "display_name": current["display_name"], "role": next_role,
        "enabled": False, "created_at": current["created_at"],
        "last_login_at": current["last_login_at"],
    }
