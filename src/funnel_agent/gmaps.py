"""Google Maps (Places API New) prospect sweep — Lisa-4's OWN data source, separate from D&B.

Target: SMALL professional-services businesses in AU, decision-maker-reachable (listed MOBILE preferred —
for micro firms the listed 04xx number IS the owner), never big business, never anyone the humans or
Lisa-1 already touch. Rows land in `companies` with source='gmaps' (filterable on the Prospect DB page);
reserve_lisa4_pool prefers this stock for dialing.
"""

from __future__ import annotations

import json
import time
import urllib.parse
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


# --------------------------------------------------------------------------- AUTOPILOT sweep ---
# Trades have a far higher NO-WEBSITE rate than professional services, so the auto-sweep targets them to keep
# Lisa-4's "you have no website" pitch stocked. Region-rotated so each run finds fresh businesses.
_TRADE_CATS = [
    "plumber", "electrician", "handyman", "landscaper", "gardener", "painter", "carpenter", "roofer", "tiler",
    "fencing contractor", "concreter", "bricklayer", "plasterer", "pest control", "removalist", "locksmith",
    "mobile mechanic", "car detailing", "dog grooming", "cleaning service", "lawn mowing", "tree removal service",
    "gutter cleaning", "paving contractor", "decking", "welding", "pool cleaning", "air conditioning installer",
    "blinds and curtains", "auto electrician", "windscreen repair", "mobile locksmith",
]
_TRADE_AREAS = [
    # a large AU pool (outer-metro + regional across every state) — the auto-sweep rotates through it by day
    "Craigieburn VIC", "Pakenham VIC", "Cranbourne VIC", "Melton VIC", "Werribee VIC", "Sunbury VIC",
    "Frankston VIC", "Geelong VIC", "Ballarat VIC", "Bendigo VIC", "Shepparton VIC", "Wodonga VIC",
    "Traralgon VIC", "Mildura VIC", "Wangaratta VIC", "Sale VIC", "Horsham VIC", "Warrnambool VIC",
    "Penrith NSW", "Blacktown NSW", "Campbelltown NSW", "Liverpool NSW", "Newcastle NSW", "Wollongong NSW",
    "Wagga Wagga NSW", "Albury NSW", "Tamworth NSW", "Dubbo NSW", "Orange NSW", "Bathurst NSW",
    "Coffs Harbour NSW", "Port Macquarie NSW", "Lismore NSW", "Nowra NSW", "Griffith NSW", "Maitland NSW",
    "Ipswich QLD", "Logan QLD", "Caboolture QLD", "Toowoomba QLD", "Cairns QLD", "Townsville QLD",
    "Mackay QLD", "Rockhampton QLD", "Bundaberg QLD", "Hervey Bay QLD", "Gladstone QLD", "Maryborough QLD",
    "Elizabeth SA", "Mount Gambier SA", "Whyalla SA", "Murray Bridge SA", "Gawler SA", "Port Augusta SA",
    "Rockingham WA", "Mandurah WA", "Bunbury WA", "Geraldton WA", "Albany WA", "Kalgoorlie WA",
    "Launceston TAS", "Devonport TAS", "Burnie TAS",
]


def _autosweep_stock(pool) -> int:
    from . import lisa as _l
    r = _l._fetch(pool,
        "SELECT count(*) n FROM companies co WHERE co.source='gmaps' AND co.phone_is_mobile "
        "AND (co.domain IS NULL OR co.domain='') "
        "AND left(right(regexp_replace(co.phone,'[^0-9]','','g'),9),1)='4' "
        "AND NOT EXISTS (SELECT 1 FROM lisa4_pool p WHERE p.dest9=right(regexp_replace(co.phone,'[^0-9]','','g'),9)) "
        "AND NOT EXISTS (SELECT 1 FROM lisa_calls lc WHERE lc.dest9=right(regexp_replace(co.phone,'[^0-9]','','g'),9))")
    return int(r[0]["n"]) if r else 0


def _autosweep_worker(pool, settings, areas, max_searches):
    try:
        res = sweep_gmaps(pool, settings, categories=_TRADE_CATS, areas=areas,
                          max_searches=max_searches, pages_per_query=2, sleep_s=0.12)
        log.info("gmaps_autosweep_done", stock=_autosweep_stock(pool), **{k: res.get(k) for k in ("kept", "mobile", "searches")})
    except Exception as exc:
        log.warning("gmaps_autosweep_worker_failed", error=str(exc)[:160])


def run_gmaps_autosweep(pool, settings, *, stock_floor: int = 500, min_gap_hours: float = 4.0,
                        max_searches: int = 90) -> dict:
    """AUTOPILOT — keep Lisa-4's NO-WEBSITE stock topped up so 'ready for calls' never runs dry (Vysakh: top
    priority, must be autopilot). Demand-driven: only sweeps when available no-website stock < stock_floor, at
    most once per min_gap_hours, region-rotated by day. Runs in a BACKGROUND THREAD so the dialer never blocks.
    Guarded; no-op without GOOGLE_PLACES_API_KEY."""
    from datetime import datetime, timezone
    try:
        if not (getattr(settings, "google_places_api_key", "") or ""):
            return {"skipped": "no places key"}
        from . import lisa as _l
        r = _l._fetch(pool, "SELECT v FROM crm_config WHERE k='gmaps_autosweep_at'")
        if r and (r[0].get("v") or "").strip():
            try:
                last = datetime.fromisoformat(r[0]["v"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last).total_seconds() < min_gap_hours * 3600:
                    return {"skipped": "throttled"}
            except Exception:
                pass
        stock = _autosweep_stock(pool)
        if stock >= stock_floor:
            return {"skipped": "stock ok", "stock": stock}
        # stamp NOW so a concurrent tick can't double-fire, then sweep in the background
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO crm_config (k,v) VALUES ('gmaps_autosweep_at',%s) "
                        "ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v", (datetime.now(timezone.utc).isoformat(),))
            conn.commit()
        yday = datetime.now(timezone.utc).timetuple().tm_yday          # rotate the region window each day
        span = 22
        start = (yday * span) % max(1, len(_TRADE_AREAS))
        areas = (_TRADE_AREAS + _TRADE_AREAS)[start:start + span]
        import threading
        threading.Thread(target=_autosweep_worker, args=(pool, settings, areas, max_searches), daemon=True).start()
        log.info("gmaps_autosweep_started", stock=stock, areas=len(areas))
        return {"started": True, "stock": stock, "areas": len(areas)}
    except Exception as exc:
        log.warning("gmaps_autosweep_failed", error=str(exc)[:160])
        return {"error": "guarded"}


# --------------------------------------------------------------------------- PLACE PHOTOS ---
# A NO-WEBSITE prospect (e.g. a nail salon with only a Google listing) gets a rebuilt site with no real
# imagery. Their Google Business Profile is the one place their OWN photos live — shopfront, interior, work,
# team. Pull them so the rebuild shows the real business instead of stock. Every hop is guarded: no key /
# no match / any API error -> [] (never raises), so a photo miss never blocks a build.

_PHOTO_MAX_PX = 1200          # size cap on each fetched image (keeps the data URI a sane size)


def _places_key(settings: Settings) -> str:
    """Same Places API key resolution the sweep uses."""
    return getattr(settings, "google_places_api_key", "") or ""


def _place_id_from_phone(settings: Settings, phone: str | None) -> str:
    """Find Place From Phone Number -> best-match place_id ('' on any miss). For micro firms the listed
    number is exact, so this beats a name search when a phone is available."""
    key = _places_key(settings)
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not (key and digits):
        return ""
    # normalise to E.164 AU (listed 04xx / 03xx / 02xx etc.)
    if digits.startswith("61"):
        e164 = "+" + digits
    elif digits.startswith("0"):
        e164 = "+61" + digits[1:]
    else:
        e164 = "+61" + digits
    try:
        url = ("https://maps.googleapis.com/maps/api/place/findplacefromtext/json?input="
               + urllib.parse.quote(e164) + "&inputtype=phonenumber&fields=place_id&key="
               + urllib.parse.quote(key))
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        cands = data.get("candidates") or []
        return (cands[0].get("place_id") or "") if cands else ""
    except Exception as exc:
        log.warning("gmaps_phone_lookup_failed", error=str(exc)[:120])
        return ""


def _photo_names_for_place_id(settings: Settings, place_id: str) -> list[str]:
    """Place Details (New) -> photo resource names ('places/PID/photos/REF') for a place_id."""
    key = _places_key(settings)
    if not (key and place_id):
        return []
    try:
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places/" + urllib.parse.quote(place_id),
            method="GET",
            headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "id,displayName,photos"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode())
        return [p.get("name") for p in (data.get("photos") or []) if p.get("name")]
    except Exception as exc:
        log.warning("gmaps_details_failed", error=str(exc)[:120])
        return []


def _photo_names_from_search(settings: Settings, name: str, address: str | None,
                             suburb: str | None) -> list[str]:
    """Places Text Search (New): name + address/suburb -> the best match's photo resource names.
    Text Search returns results in relevance order, so the first place is the best match."""
    key = _places_key(settings)
    q = " ".join(str(x).strip() for x in (name, address or suburb) if x and str(x).strip())
    if not (key and q):
        return []
    try:
        body = {"textQuery": q, "regionCode": "AU", "pageSize": 5}
        req = urllib.request.Request(
            "https://places.googleapis.com/v1/places:searchText",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                     "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.photos"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode())
        places = data.get("places") or []
        if not places:
            return []
        best = places[0]
        return [p.get("name") for p in (best.get("photos") or []) if p.get("name")]
    except Exception as exc:
        log.warning("gmaps_photo_search_failed", error=str(exc)[:120])
        return []


def _fetch_photo_data_uri(settings: Settings, photo_name: str) -> str:
    """Places Photo (New) media endpoint -> a data: URI ('' on any error). Follows the 302 to the actual
    image bytes and reads the returned Content-Type; size-capped via maxWidthPx."""
    key = _places_key(settings)
    if not (key and photo_name):
        return ""
    try:
        url = ("https://places.googleapis.com/v1/" + photo_name
               + "/media?maxWidthPx=" + str(_PHOTO_MAX_PX) + "&maxHeightPx=" + str(_PHOTO_MAX_PX))
        req = urllib.request.Request(url, method="GET", headers={"X-Goog-Api-Key": key})
        with urllib.request.urlopen(req, timeout=30) as resp:       # follows redirect to image bytes
            ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower()
            raw = resp.read()
        if not raw or not ctype.startswith("image/"):
            return ""
        import base64
        return "data:" + ctype + ";base64," + base64.b64encode(raw).decode("ascii")
    except Exception as exc:
        log.warning("gmaps_photo_media_failed", error=str(exc)[:120])
        return ""


def place_photos(settings: Settings, name: str, address: str | None = None, phone: str | None = None,
                 suburb: str | None = None, limit: int = 12) -> list[str]:
    """Real Google-listing photos for a business, as data: URIs (up to `limit`).

    For NO-WEBSITE prospects the Google Business Profile is the only place their genuine photos live, so a
    rebuilt site can show the actual shopfront/interior/work instead of stock. Resolves the place by PHONE
    (Find Place From Phone) when one is given — most precise for micro firms — else by NAME + address/suburb
    (Text Search), takes the best match, pulls its photo references (Place Details `photos`), and fetches up
    to `limit` of them through the Places Photo endpoint (size-capped), converting each to a data URI.

    Fully guarded: no key / no match / any error -> []. Never raises."""
    try:
        if not _places_key(settings) or not (name or "").strip():
            return []
        photo_names: list[str] = []
        pid = _place_id_from_phone(settings, phone) if phone else ""
        if pid:
            photo_names = _photo_names_for_place_id(settings, pid)
        if not photo_names:                                   # no phone / no phone match -> name search
            photo_names = _photo_names_from_search(settings, name, address, suburb)
        n = max(0, int(limit or 0))
        out: list[str] = []
        for pn in photo_names[:n]:
            uri = _fetch_photo_data_uri(settings, pn)
            if uri:
                out.append(uri)
        return out
    except Exception as exc:
        log.warning("gmaps_place_photos_failed", error=str(exc)[:120])
        return []
