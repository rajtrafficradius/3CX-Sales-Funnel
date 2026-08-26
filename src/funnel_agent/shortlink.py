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
        cur.execute("ALTER TABLE short_links ADD COLUMN IF NOT EXISTS dest9 text")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_short_links_target ON short_links(md5(target))")
        conn.commit()


def short_code(pool, target: str, dest9: str | None = None) -> str | None:
    """Mint (or reuse) a short code. When dest9 is given the code is PER-PROSPECT (so an SMS click can be
    attributed to that booking even for a shared asset like the brand doc); without it, reuse by target."""
    from . import lisa as _l
    try:
        ensure_shortlinks(pool)
        if dest9:
            r = _l._fetch(pool, "SELECT code FROM short_links WHERE target=%s AND dest9=%s LIMIT 1", (target, dest9))
        else:
            r = _l._fetch(pool, "SELECT code FROM short_links WHERE target=%s AND dest9 IS NULL LIMIT 1", (target,))
        if r:
            return r[0]["code"]
        for _ in range(6):
            code = "".join(secrets.choice(_ALPHABET) for _ in range(6))
            with pool.connection() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO short_links (code,target,dest9) VALUES (%s,%s,%s) "
                            "ON CONFLICT (code) DO NOTHING RETURNING code", (code, target, dest9))
                got = cur.fetchone()
                conn.commit()
            if got:
                return code
    except Exception:
        pass
    return None


def short_url(pool, target: str, base: str = _BASE, dest9: str | None = None) -> str:
    """Return a short branded URL for `target`, or the target itself if minting fails."""
    c = short_code(pool, target, dest9)
    return f"{base.rstrip('/')}/s/{c}" if c else target


def resolve(pool, code: str) -> str | None:
    from . import lisa as _l
    try:
        r = _l._fetch(pool, "SELECT target FROM short_links WHERE code=%s", (code,))
        return r[0]["target"] if r else None
    except Exception:
        return None


def resolve_row(pool, code: str) -> dict | None:
    """Resolve a code to {target, dest9} so the redirect can attribute the click to a booking."""
    from . import lisa as _l
    try:
        r = _l._fetch(pool, "SELECT target, dest9 FROM short_links WHERE code=%s", (code,))
        return dict(r[0]) if r else None
    except Exception:
        return None
