"""Sync the BDE roster from 3CX `/xapi/v1/Users` into `bde_agents`.

The roster is authoritative: refreshed each run so new hires/leavers track
automatically. `in_scope` is decided by a config rule (explicit extension
allow-list, else group/department names, else default false until reviewed).
"""

from __future__ import annotations

from datetime import datetime, timezone

from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger
from .threecx.api import ThreeCXClient

log = get_logger(__name__)


def _full_name(user: dict) -> str:
    first = (user.get("FirstName") or "").strip()
    last = (user.get("LastName") or "").strip()
    return (first + " " + last).strip() or (user.get("Number") or "")


def _groups(user: dict) -> list[dict]:
    groups = user.get("Groups")
    return groups if isinstance(groups, list) else []


def _group_name(user: dict) -> str:
    names = [g.get("Name") or g.get("GroupName") or "" for g in _groups(user)]
    names = [n for n in names if n]
    return ", ".join(names)


def _role_name(user: dict) -> str:
    for g in _groups(user):
        rights = g.get("Rights") or {}
        role = rights.get("RoleName")
        if role:
            return role
    return user.get("Role") or ""


def decide_in_scope(user: dict, settings: Settings) -> bool:
    """Apply the configured in-scope rule to a single user."""
    ext = str(user.get("Number") or "").strip()
    if ext in settings.exclude_extensions:
        return False  # explicit exclude (e.g. admins) overrides group membership
    if settings.inscope_extensions:
        return ext in settings.inscope_extensions
    if settings.inscope_groups:
        wanted = {g.lower() for g in settings.inscope_groups}
        have = {(g.get("Name") or g.get("GroupName") or "").lower() for g in _groups(user)}
        return bool(wanted & have)
    return False  # default: review the roster, then set ROSTER_INSCOPE_*


def sync_roster(pool: ConnectionPool, client: ThreeCXClient, settings: Settings) -> dict:
    """Upsert every user into bde_agents; mark departed users inactive."""
    now = datetime.now(timezone.utc)
    seen: list[str] = []
    upserts = 0

    with pool.connection() as conn:
        with conn.cursor() as cur:
            for user in client.iter_users():
                ext = str(user.get("Number") or "").strip()
                if not ext:
                    continue
                seen.append(ext)
                cur.execute(
                    """
                    INSERT INTO bde_agents
                        (extension, bde_name, email, group_name, role_name,
                         in_scope, active, synced_at)
                    VALUES (%(ext)s, %(name)s, %(email)s, %(group)s, %(role)s,
                            %(in_scope)s, true, %(synced)s)
                    ON CONFLICT (extension) DO UPDATE SET
                        bde_name   = EXCLUDED.bde_name,
                        email      = EXCLUDED.email,
                        group_name = EXCLUDED.group_name,
                        role_name  = EXCLUDED.role_name,
                        in_scope   = EXCLUDED.in_scope,
                        active     = true,
                        synced_at  = EXCLUDED.synced_at
                    """,
                    {
                        "ext": ext,
                        "name": _full_name(user),
                        "email": user.get("EmailAddress"),
                        "group": _group_name(user),
                        "role": _role_name(user),
                        "in_scope": decide_in_scope(user, settings),
                        "synced": now,
                    },
                )
                upserts += 1

            # Anyone not returned this run is no longer active in 3CX.
            if seen:
                cur.execute(
                    "UPDATE bde_agents SET active = false, synced_at = %s "
                    "WHERE extension <> ALL(%s)",
                    (now, seen),
                )
                deactivated = cur.rowcount
            else:
                deactivated = 0

            # Merge secondary lines into their primary BDE (config-driven), so a
            # BDE's landline / 2nd extension reports under the same name and its
            # activity rolls into one person. Runs every sync, so it's stable.
            for sec, prim in settings.merge_map.items():
                cur.execute(
                    "UPDATE bde_agents s SET bde_name = p.bde_name "
                    "FROM bde_agents p WHERE p.extension = %s AND s.extension = %s",
                    (prim, sec),
                )
        conn.commit()

    in_scope = _count_in_scope(pool)
    log.info("roster_synced", upserts=upserts, deactivated=deactivated, in_scope=in_scope)
    return {"upserts": upserts, "deactivated": deactivated, "in_scope": in_scope}


def _count_in_scope(pool: ConnectionPool) -> int:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM bde_agents WHERE in_scope AND active")
        row = cur.fetchone()
        return int(row["n"]) if row else 0
