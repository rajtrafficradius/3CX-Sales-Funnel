"""Authentication: PBKDF2 password hashing + DB-backed sessions + user seeding.

Self-contained (stdlib only). One account per in-scope BDE (role 'bde', scoped to
their own data) plus manager account(s) (role 'manager', see everyone).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from psycopg_pool import ConnectionPool

from .logging import get_logger

log = get_logger(__name__)

_ROUNDS = 200_000

# --------------------------------------------------------------------------- #
# Roles
#   admin   -> vysakh, raj: full access + user management
#   bdm     -> raven, kiran: see every BDE's data + assign/modify pipeline prospects
#   bde     -> own data only
#   manager -> legacy "see all" (kept working; superseded by admin/bdm)
#   kiosk   -> TV read-only (granted via KIOSK_TOKEN, not a stored account)
# --------------------------------------------------------------------------- #
VALID_ROLES = {"admin", "bdm", "bde", "manager", "kiosk"}
ADMIN_ROLES = {"admin"}
SEE_ALL_ROLES = {"admin", "bdm", "manager", "kiosk"}      # not scoped to own data
PIPELINE_ADMIN_ROLES = {"admin", "bdm", "manager"}        # can assign/modify pipeline prospects


def is_admin(user: dict | None) -> bool:
    return bool(user) and user.get("role") in ADMIN_ROLES


def can_see_all(user: dict | None) -> bool:
    return bool(user) and user.get("role") in SEE_ALL_ROLES


def can_manage_pipeline(user: dict | None) -> bool:
    return bool(user) and user.get("role") in PIPELINE_ADMIN_ROLES


# --------------------------------------------------------------------------- #
# Passwords (PBKDF2-HMAC-SHA256, stdlib)
# --------------------------------------------------------------------------- #
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ROUNDS)
    return f"pbkdf2_sha256${_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _algo, rounds, salt_b, dk_b = stored.split("$")
        salt = base64.b64decode(salt_b)
        expected = base64.b64decode(dk_b)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, int(rounds))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Sessions (DB-backed)
# --------------------------------------------------------------------------- #
def create_session(pool: ConnectionPool, user_id: int, days: int = 30) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    exp = datetime.now(timezone.utc) + timedelta(days=days)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, %s)",
            (token, user_id, exp),
        )
        conn.commit()
    return token, exp


def user_for_session(pool: ConnectionPool, token: str | None) -> dict | None:
    if not token:
        return None
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT u.id, u.email, u.name, u.role, u.bde_name, u.must_change "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = %s AND s.expires_at > now() AND u.active",
            (token,),
        )
        rows = cur.fetchall()
        return rows[0] if rows else None


def delete_session(pool: ConnectionPool, token: str | None) -> None:
    if not token:
        return
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE token = %s", (token,))
        conn.commit()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def get_user_by_email(pool: ConnectionPool, email: str) -> dict | None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE lower(email) = lower(%s)", (email,))
        rows = cur.fetchall()
        return rows[0] if rows else None


def set_password(pool: ConnectionPool, email: str, new_pw: str, *, must_change: bool = False) -> bool:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s, must_change = %s WHERE lower(email) = lower(%s)",
            (hash_password(new_pw), must_change, email),
        )
        changed = cur.rowcount
        conn.commit()
    return bool(changed)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", ".", (name or "").lower()).strip(".")
    return s or "bde"


def _upsert_user(cur, *, email, name, role, bde_name, password_hash) -> bool:
    """Insert a user if the email is new. Returns True if created."""
    cur.execute("SELECT 1 FROM users WHERE lower(email)=lower(%s)", (email,))
    if cur.fetchall():
        return False
    cur.execute(
        "INSERT INTO users (email, name, role, bde_name, password_hash, must_change) "
        "VALUES (%s, %s, %s, %s, %s, true)",
        (email, name, role, bde_name, hash_password(password_hash)),
    )
    return True


def seed_users(pool: ConnectionPool, manager_emails: list[str]) -> list[dict]:
    """Create one account per in-scope BDE + the given manager(s). Idempotent:
    existing emails are left untouched. Returns the newly-created accounts with
    their one-time temporary passwords (print these once, then have users log in).
    """
    created: list[dict] = []
    with pool.connection() as conn, conn.cursor() as cur:
        # One account per distinct canonical BDE name (post-merge), with a login email.
        cur.execute(
            "SELECT COALESCE(bde_name, extension) AS name, max(email) AS email, max(extension) AS ext "
            "FROM bde_agents WHERE in_scope AND active "
            "GROUP BY COALESCE(bde_name, extension) ORDER BY 1"
        )
        for r in cur.fetchall():
            name = str(r["name"])
            email = (r["email"] or f"{_slug(name)}@bde.local").strip()
            temp = secrets.token_urlsafe(6)
            if _upsert_user(cur, email=email, name=name, role="bde", bde_name=name, password_hash=temp):
                created.append({"email": email, "name": name, "role": "bde", "temp_password": temp})
        # Manager account(s).
        for me in manager_emails:
            me = me.strip()
            if not me:
                continue
            temp = secrets.token_urlsafe(6)
            if _upsert_user(cur, email=me, name="Manager", role="manager", bde_name=None, password_hash=temp):
                created.append({"email": me, "name": "Manager", "role": "manager", "temp_password": temp})
        conn.commit()
    return created


# --------------------------------------------------------------------------- #
# User management (admin panel)
# --------------------------------------------------------------------------- #
def list_users(pool: ConnectionPool) -> list[dict]:
    """All accounts for the admin panel, with role + session counts."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT u.id, u.email, u.name, u.role, u.bde_name, u.active, u.must_change, "
            "       u.created_at, "
            "       (SELECT count(*) FROM sessions s WHERE s.user_id = u.id AND s.expires_at > now()) "
            "         AS active_sessions "
            "FROM users u ORDER BY "
            "  CASE u.role WHEN 'admin' THEN 0 WHEN 'bdm' THEN 1 WHEN 'manager' THEN 1 "
            "              WHEN 'bde' THEN 2 ELSE 3 END, lower(u.name)"
        )
        return cur.fetchall()


def create_user(
    pool: ConnectionPool,
    *,
    email: str,
    name: str,
    role: str,
    bde_name: str | None,
    password: str,
    must_change: bool = True,
) -> dict:
    """Create an account. Raises ValueError on bad role or duplicate email."""
    email = (email or "").strip()
    role = (role or "bde").strip()
    if not email:
        raise ValueError("email is required")
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role '{role}'")
    if not password:
        raise ValueError("password is required")
    # BDE accounts must carry their canonical bde_name (used to scope their data);
    # everyone else sees all, so bde_name is irrelevant.
    bde_name = (bde_name or "").strip() or None
    if role == "bde" and not bde_name:
        bde_name = (name or "").strip() or None
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE lower(email)=lower(%s)", (email,))
        if cur.fetchall():
            raise ValueError(f"a user with email '{email}' already exists")
        cur.execute(
            "INSERT INTO users (email, name, role, bde_name, password_hash, must_change, active) "
            "VALUES (%s, %s, %s, %s, %s, %s, true) RETURNING id, email, name, role, bde_name, active, must_change",
            (email, (name or "").strip() or email, role, bde_name, hash_password(password), must_change),
        )
        row = cur.fetchone()
        conn.commit()
    return row


def update_user(
    pool: ConnectionPool,
    user_id: int,
    *,
    name: str | None = None,
    role: str | None = None,
    bde_name: str | None = None,
    active: bool | None = None,
) -> bool:
    """Patch the given fields on a user. Only non-None fields change."""
    sets: list[str] = []
    params: list[object] = []
    if name is not None:
        sets.append("name = %s")
        params.append(name.strip() or None)
    if role is not None:
        role = role.strip()
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role '{role}'")
        sets.append("role = %s")
        params.append(role)
    if bde_name is not None:
        sets.append("bde_name = %s")
        params.append(bde_name.strip() or None)
    if active is not None:
        sets.append("active = %s")
        params.append(bool(active))
    if not sets:
        return False
    params.append(user_id)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", params)
        changed = cur.rowcount
        # Deactivating a user kills their live sessions immediately.
        if active is False:
            cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        conn.commit()
    return bool(changed)


def set_password_by_id(
    pool: ConnectionPool, user_id: int, new_pw: str, *, must_change: bool = False
) -> bool:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s, must_change = %s WHERE id = %s",
            (hash_password(new_pw), must_change, user_id),
        )
        changed = cur.rowcount
        conn.commit()
    return bool(changed)


def delete_user(pool: ConnectionPool, user_id: int) -> bool:
    """Hard-delete a user (and cascade their sessions)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        changed = cur.rowcount
        conn.commit()
    return bool(changed)


def ensure_admin_bdm(
    pool: ConnectionPool,
    *,
    admin_emails: list[str],
    bdm_specs: list[tuple[str, str]],
) -> list[dict]:
    """Promote known admins, and create/promote BDMs. Idempotent.

    `admin_emails`  -> existing accounts promoted to role 'admin'.
    `bdm_specs`     -> (email, name) pairs created as 'bdm' if missing, else promoted.
    Returns any newly-created accounts with their one-time temp passwords.
    """
    created: list[dict] = []
    with pool.connection() as conn, conn.cursor() as cur:
        for em in admin_emails:
            em = (em or "").strip()
            if em:
                cur.execute(
                    "UPDATE users SET role='admin', bde_name=NULL WHERE lower(email)=lower(%s)",
                    (em,),
                )
        for em, nm in bdm_specs:
            em = (em or "").strip()
            if not em:
                continue
            cur.execute("SELECT 1 FROM users WHERE lower(email)=lower(%s)", (em,))
            if cur.fetchall():
                cur.execute(
                    "UPDATE users SET role='bdm', bde_name=NULL WHERE lower(email)=lower(%s)", (em,)
                )
            else:
                temp = secrets.token_urlsafe(6)
                cur.execute(
                    "INSERT INTO users (email, name, role, bde_name, password_hash, must_change) "
                    "VALUES (%s, %s, 'bdm', NULL, %s, true)",
                    (em, nm or "BDM", hash_password(temp)),
                )
                created.append({"email": em, "name": nm or "BDM", "role": "bdm", "temp_password": temp})
        conn.commit()
    return created
