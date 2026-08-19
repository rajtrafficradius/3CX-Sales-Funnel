"""Short branded links for outbound SMS.

Plain SMS can't carry true anchor text, so the next best thing is a SHORT, clean link instead of a long raw
URL. `/s/{code}` 302-redirects to the real public URL. When trmatrix.com.au goes live these become
trmatrix.com.au/s/xxxxxx — genuinely tidy. Idempotent: the same target reuses its code.
"""
import secrets

_BASE = "https://www.trmatrix.com.au"
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no ambiguous 0/o/1/l/i


def ensure_shortlinks(pool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS short_links ("
                    "  code text PRIMARY KEY, target text NOT NULL, created_at timestamptz DEFAULT now())")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_short_links_target ON short_links(md5(target))")
        conn.commit()


def short_code(pool, target: str) -> str | None:
    from . import lisa as _l
    try:
        ensure_shortlinks(pool)
        r = _l._fetch(pool, "SELECT code FROM short_links WHERE target=%s LIMIT 1", (target,))
        if r:
            return r[0]["code"]
        for _ in range(6):
            code = "".join(secrets.choice(_ALPHABET) for _ in range(6))
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO short_links (code,target) VALUES (%s,%s) ON CONFLICT (code) DO NOTHING RETURNING code",
                            (code, target))
                got = cur.fetchone()
                conn.commit()
            if got:
                return code
    except Exception:
        pass
    return None


def short_url(pool, target: str, base: str = _BASE) -> str:
    """Return a short branded URL for `target`, or the target itself if minting fails."""
    c = short_code(pool, target)
    return f"{base.rstrip('/')}/s/{c}" if c else target


def resolve(pool, code: str) -> str | None:
    from . import lisa as _l
    try:
        r = _l._fetch(pool, "SELECT target FROM short_links WHERE code=%s", (code,))
        return r[0]["target"] if r else None
    except Exception:
        return None
