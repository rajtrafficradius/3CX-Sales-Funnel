"""Google Maps (Places API New) prospect sweep — Lisa-4's OWN data source, separate from D&B.

Target: SMALL professional-services businesses in AU, decision-maker-reachable (listed MOBILE preferred —
for micro firms the listed 04xx number IS the owner), never big business, never anyone the humans or
Lisa-1 already touch. Rows land in `companies` with source='gmaps' (filterable on the Prospect DB page);
reserve_lisa4_pool prefers this stock for dialing.
"""

from __future__ import annotations

import json
import time
import urllib.request

from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger

log = get_logger(__name__)

# Professional-service categories (the pitch fits consult-style businesses that live on trust + Google)
GMAPS_CATEGORIES = [
    "accountant", "bookkeeper", "tax agent", "financial adviser", "mortgage broker",
    "insurance broker", "conveyancer", "solicitor", "family lawyer", "architect",
    "building designer", "land surveyor", "town planner", "business consultant",
    "marketing consultant", "recruitment agency", "buyers agent", "property manager",
    "physiotherapist", "chiropractor", "osteopath", "podiatrist", "psychologist",
    "naturopath", "dietitian", "optometrist", "dental clinic", "vet clinic",
]

# Suburb grid — diversified so text search doesn't keep returning the same CBD names
GMAPS_AREAS = [
    # Melbourne
    "Richmond VIC", "Hawthorn VIC", "Brunswick VIC", "Footscray VIC", "Preston VIC",
    "Box Hill VIC", "Dandenong VIC", "Frankston VIC", "Werribee VIC", "Craigieburn VIC",
    "Ringwood VIC", "Moonee Ponds VIC", "Cheltenham VIC", "Sunshine VIC", "Epping VIC",
    "Geelong VIC", "Ballarat VIC", "Bendigo VIC",
    # Sydney
    "Parramatta NSW", "Penrith NSW", "Liverpool NSW", "Blacktown NSW", "Chatswood NSW",
    "Bondi Junction NSW", "Hurstville NSW", "Bankstown NSW", "Castle Hill NSW", "Manly NSW",
    "Campbelltown NSW", "Newcastle NSW", "Wollongong NSW", "Gosford NSW",
    # Brisbane / QLD
    "Chermside QLD", "Ipswich QLD", "Logan Central QLD", "Southport QLD", "Maroochydore QLD",
    "Toowoomba QLD", "Cairns QLD", "Townsville QLD",
    # Perth / WA
    "Fremantle WA", "Joondalup WA", "Midland WA", "Rockingham WA", "Bunbury WA",
    # Adelaide / SA
    "Glenelg SA", "Norwood SA", "Elizabeth SA", "Mount Barker SA",
    # ACT / TAS / NT
    "Belconnen ACT", "Woden ACT", "Hobart TAS", "Launceston TAS", "Darwin NT",
]

# Obvious chains/franchises — never worth the "we built you a site" pitch
_CHAIN_BLOCK = {
    "h&r block", "itp", "specsavers", "opsm", "ray white", "lj hooker", "mcgrath", "harcourts",
    "raine & horne", "century 21", "first national", "belle property", "barry plant", "jellis craig",
    "hockingstuart", "elders", "prd", "aussie", "mortgage choice", "smartline", "loan market",
    "yellow brick road", "greencross", "petbarn vet", "myhealth", "laverty", "back in motion",
    "kieser", "lifecare", "guardian pharmacy", "terry white", "national dental care", "pacific smiles",
    "1300smiles", "maurice blackburn", "slater and gordon", "shine lawyers", "turner freeman",
}

_MAX_REVIEWS = 150          # small-business proxy: big practices/chains have hundreds of reviews
_FIELDS = ("places.id,places.displayName,places.nationalPhoneNumber,places.internationalPhoneNumber,"
           "places.websiteUri,places.userRatingCount,places.rating,places.types,"
           "places.formattedAddress,places.addressComponents,places.businessStatus,nextPageToken")


def _places_search(settings: Settings, query: str, page_token: str | None = None) -> dict:
    key = getattr(settings, "google_places_api_key", "") or ""
    if not key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY not set")
    body: dict = {"textQuery": query, "regionCode": "AU", "pageSize": 20}
    if page_token:
        body["pageToken"] = page_token
    req = urllib.request.Request(
        "https://places.googleapis.com/v1/places:searchText",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key, "X-Goog-FieldMask": _FIELDS})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _norm_name(name: str) -> str:
    return " ".join((name or "").lower().replace("&amp;", "&").split())


def _addr_part(place: dict, kind: str) -> str:
    for c in place.get("addressComponents") or []:
        if kind in (c.get("types") or []):
            return c.get("longText") or c.get("shortText") or ""
    return ""


def _domain_of(url: str | None) -> str:
    u = (url or "").strip().lower()
    if not u:
        return ""
    u = u.split("//")[-1].split("/")[0].split("?")[0]
    if u.startswith("www."):
        u = u[4:]
    # social/aggregator links are NOT a real website of their own
    if any(s in u for s in ("facebook.", "instagram.", "linkedin.", "google.", "wixsite.com",
                            "business.site", "yellowpages", "localsearch", "hipages", "airtasker")):
        return ""
    return u


def ensure_gmaps_columns(pool: ConnectionPool) -> None:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE companies ADD COLUMN IF NOT EXISTS gmaps_place_id text")
        cur.execute("ALTER TABLE companies ADD COLUMN IF NOT EXISTS gmaps_rating numeric")
        cur.execute("ALTER TABLE companies ADD COLUMN IF NOT EXISTS gmaps_reviews integer")
        cur.execute("ALTER TABLE companies ADD COLUMN IF NOT EXISTS phone_is_mobile boolean")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_gmaps_pid ON companies (gmaps_place_id) "
                    "WHERE gmaps_place_id IS NOT NULL")
        conn.commit()


def sweep_gmaps(pool: ConnectionPool, settings: Settings, *, categories: list[str] | None = None,
                areas: list[str] | None = None, max_searches: int = 400, pages_per_query: int = 2,
                sleep_s: float = 0.15) -> dict:
    """Run the sweep: category × area text searches → filter → upsert companies(source='gmaps').
    Collection is pure-API (no DB in the hot loop); exclusion checks run as a FEW set-based queries at the
    end (a per-candidate query over the cloud proxy took hours). Idempotent via place_id / phone dedupe."""
    ensure_gmaps_columns(pool)
    cats = categories or GMAPS_CATEGORIES
    locs = areas or GMAPS_AREAS
    seen_names: dict[str, int] = {}        # chain detection within the sweep
    stats = {"searches": 0, "places": 0, "kept": 0, "mobile": 0, "dup": 0, "big": 0, "chain": 0, "nophone": 0}

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT COALESCE(phone_norm,'') p FROM companies WHERE source='gmaps'")
        have_phones = {r["p"][-9:] for r in cur.fetchall() if r["p"]}
        cur.execute("SELECT gmaps_place_id FROM companies WHERE gmaps_place_id IS NOT NULL")
        have_pids = {r["gmaps_place_id"] for r in cur.fetchall()}

    # ---- phase A: collect from Places (API-only, fast) --------------------------------------------
    import random
    combos = [(c, a) for c in cats for a in locs]
    random.Random(42).shuffle(combos)   # seeded: even category coverage under any search cap
    cands: list[dict] = []
    for cat, area in combos:
        if stats["searches"] >= max_searches:
            break
        token = None
        for _page in range(pages_per_query):
            try:
                r = _places_search(settings, f"{cat} in {area}", token)
            except Exception as exc:
                log.warning("gmaps_search_failed", q=f"{cat} in {area}", error=str(exc)[:120])
                break
            stats["searches"] += 1
            for pl in r.get("places") or []:
                stats["places"] += 1
                if (pl.get("businessStatus") or "OPERATIONAL") != "OPERATIONAL":
                    continue
                pid = pl.get("id") or ""
                name = (pl.get("displayName") or {}).get("text") or ""
                nn = _norm_name(name)
                phone = pl.get("nationalPhoneNumber") or pl.get("internationalPhoneNumber") or ""
                digits = "".join(ch for ch in phone if ch.isdigit())
                d9 = digits[-9:] if len(digits) >= 9 else ""
                reviews = int(pl.get("userRatingCount") or 0)
                if not d9:
                    stats["nophone"] += 1
                    continue
                if pid in have_pids or d9 in have_phones:
                    stats["dup"] += 1
                    continue
                if reviews > _MAX_REVIEWS:
                    stats["big"] += 1
                    continue
                if any(b in nn for b in _CHAIN_BLOCK):
                    stats["chain"] += 1
                    continue
                seen_names[nn] = seen_names.get(nn, 0) + 1
                if seen_names[nn] > 2:              # same trading name in 3+ areas = franchise
                    stats["chain"] += 1
                    continue
                have_pids.add(pid)
                have_phones.add(d9)
                cands.append({
                    "name": name, "cat": cat, "addr": pl.get("formattedAddress"),
                    "suburb": _addr_part(pl, "locality"), "state": _addr_part(pl, "administrative_area_level_1"),
                    "pc": _addr_part(pl, "postal_code"), "dom": _domain_of(pl.get("websiteUri")) or None,
                    "phone": phone, "d9": d9, "pid": pid,
                    "rating": pl.get("rating"), "reviews": reviews, "mobile": d9.startswith("4"),
                })
            token = r.get("nextPageToken")
            if not token:
                break
            time.sleep(sleep_s)

    # ---- checkpoint: never lose paid API results to a DB hiccup -----------------------------------
    import tempfile, os
    ckpt = os.path.join(tempfile.gettempdir(), "gmaps_cands_checkpoint.json")
    try:
        json.dump(cands, open(ckpt, "w"))
        log.info("gmaps_checkpoint_saved", path=ckpt, n=len(cands))
    except Exception:
        pass
    # ---- phase B: exclusion via full-set scans + python set-difference (fast, timeout-proof) ------
    keep = _exclude_taken(pool, cands, stats)
    keep = _exclude_owner_rule(pool, keep, stats)
    stats["kept"] = len(keep)
    stats["mobile"] = sum(1 for c in keep if c["mobile"])

    # ---- phase C: one batch insert -----------------------------------------------------------------
    if keep:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO companies (source, company_name, industry, address, suburb, state, postal_code, "
                "  country, domain, phone, phone_norm, gmaps_place_id, gmaps_rating, gmaps_reviews, phone_is_mobile) "
                "VALUES ('gmaps', %s, %s, %s, %s, %s, %s, 'Australia', %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (gmaps_place_id) WHERE gmaps_place_id IS NOT NULL DO NOTHING",
                [(c["name"], c["cat"], c["addr"], c["suburb"], c["state"], c["pc"], c["dom"],
                  c["phone"], "61" + c["d9"], c["pid"], c["rating"], c["reviews"], c["mobile"]) for c in keep])
            conn.commit()
    log.info("gmaps_sweep_done", **stats)
    return stats


def _exclude_owner_rule(pool: ConnectionPool, cands: list[dict], stats: dict) -> list[dict]:
    """Owner's standing exclusion rule at the IMPORT door: agencies/SEO/web-design, franchises,
    real-estate, portal/directory domains never enter the gmaps stock (lisa4.lisa4_exclusion_class
    is the single source of truth; imported lazily — lisa4 imports this module at load time)."""
    from .lisa4 import lisa4_exclusion_class, lisa4_portal_domains
    portal = lisa4_portal_domains(pool, [c.get("dom") for c in cands])
    keep = [c for c in cands if not lisa4_exclusion_class(c.get("name"), c.get("cat"), c.get("dom"), portal)]
    stats["owner_rule_excluded"] = len(cands) - len(keep)
    return keep


def _exclude_taken(pool: ConnectionPool, cands: list[dict], stats: dict) -> list[dict]:
    """Drop candidates whose number ANYONE already touches (human calls, Lisa-1, Lisa-4). Full-set scans
    into python sets — one cheap pass each, immune to per-row regexp blowups."""
    taken: set[str] = set()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT dest9 AS d FROM lisa_pool UNION SELECT dest9 FROM lisa4_pool "
                    "UNION SELECT dest9 FROM lisa_calls")
        taken |= {r["d"] for r in cur.fetchall() if r["d"]}
        cur.execute("SELECT DISTINCT right(regexp_replace(COALESCE(dest_number,''),'[^0-9]','','g'),9) AS d "
                    "FROM calls")
        taken |= {r["d"] for r in cur.fetchall() if r["d"]}
    keep = [c for c in cands if c["d9"] not in taken]
    stats["dup"] = stats.get("dup", 0) + len(cands) - len(keep)
    return keep


def flush_checkpoint(pool: ConnectionPool, path: str | None = None) -> dict:
    """Resume from a saved collection checkpoint: exclusion + insert only (no new API spend)."""
    import tempfile, os
    path = path or os.path.join(tempfile.gettempdir(), "gmaps_cands_checkpoint.json")
    cands = json.load(open(path))
    ensure_gmaps_columns(pool)
    stats = {"from_checkpoint": len(cands), "dup": 0}
    keep = _exclude_taken(pool, cands, stats)
    keep = _exclude_owner_rule(pool, keep, stats)
    stats["kept"] = len(keep)
    stats["mobile"] = sum(1 for c in keep if c["mobile"])
    if keep:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO companies (source, company_name, industry, address, suburb, state, postal_code, "
                "  country, domain, phone, phone_norm, gmaps_place_id, gmaps_rating, gmaps_reviews, phone_is_mobile) "
                "VALUES ('gmaps', %s, %s, %s, %s, %s, %s, 'Australia', %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (gmaps_place_id) WHERE gmaps_place_id IS NOT NULL DO NOTHING",
                [(c["name"], c["cat"], c["addr"], c["suburb"], c["state"], c["pc"], c["dom"],
                  c["phone"], "61" + c["d9"], c["pid"], c["rating"], c["reviews"], c["mobile"]) for c in keep])
            conn.commit()
    log.info("gmaps_checkpoint_flushed", **stats)
    return stats
