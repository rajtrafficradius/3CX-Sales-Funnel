"""Phase G — recompute `daily_funnel` for a day.

For every in-scope BDE (even those with zero activity) **and** for `ALL`, and for
each track in {fresh, followup, combined}, compute the funnel counts. Recompute,
never append: the day's rows are deleted and re-inserted, so re-running is stable
and `ALL` always equals the sum across in-scope BDEs.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from psycopg_pool import ConnectionPool

from .config import Settings
from .logging import get_logger

log = get_logger(__name__)

TRACKS = ("fresh", "followup", "combined")
_ZERO = {
    "calls_made": 0, "connected": 0, "transcribed": 0, "rpc_connect": 0,
    "full_pitch": 0, "leads": 0, "qualified": 0, "meetings_booked": 0,
    "qualified_booked": 0, "meetings_done": 0,
    "warm": 0, "hot": 0, "super_hot": 0, "pipeline1": 0, "pipeline2": 0,
}

# Per-(scope, track) aggregation via GROUPING SETS:
#   (bde, ff) -> per-BDE per-track ; (bde) -> per-BDE combined
#   (ff)      -> ALL per-track     ; ()    -> ALL combined
_AGG_SQL = """
WITH base AS (
    SELECT
        COALESCE(c.bde_name, c.bde_extension) AS bde,
        c.fresh_or_followup AS ff,
        1 AS calls_made,
        (CASE WHEN c.answered AND c.talk_seconds >= %(rpc_min)s AND COALESCE(cl.call_outcome, '') <> 'voicemail' THEN 1 ELSE 0 END) AS connected,
        (CASE WHEN c.has_transcript THEN 1 ELSE 0 END) AS transcribed,
        -- Funnel stages are STRICTLY NESTED: each counts only within the prior.
        -- Connected = answered AND real talk time AND a live human (NOT a voicemail,
        -- per the classifier's call_outcome). Voicemails are not "connected".
        (CASE WHEN c.answered AND c.talk_seconds >= %(rpc_min)s AND COALESCE(cl.call_outcome, '') <> 'voicemail'
                   AND cl.rpc_connect THEN 1 ELSE 0 END) AS rpc_connect,
        (CASE WHEN c.answered AND c.talk_seconds >= %(rpc_min)s AND COALESCE(cl.call_outcome, '') <> 'voicemail'
                   AND cl.rpc_connect AND cl.full_pitch THEN 1 ELSE 0 END) AS full_pitch,
        (CASE WHEN c.answered AND c.talk_seconds >= %(rpc_min)s AND COALESCE(cl.call_outcome, '') <> 'voicemail'
                   AND cl.rpc_connect AND cl.is_lead THEN 1 ELSE 0 END) AS leads,
        (CASE WHEN c.answered AND c.talk_seconds >= %(rpc_min)s AND COALESCE(cl.call_outcome, '') <> 'voicemail'
                   AND cl.rpc_connect AND cl.is_lead AND cl.qualified THEN 1 ELSE 0 END) AS qualified,
        -- Meeting Booked = the BDE booked a GENUINELY NEW meeting (a real scheduled
        -- session), regardless of decision-maker / qualification — credit the booking
        -- even if the prospect isn't the decision-maker (e.g. an interested gatekeeper).
        -- EXCLUDES both confirmation-only calls (re-confirming an earlier booking) AND
        -- reschedules of an already-booked meeting. It also counts only the FIRST booking
        -- per PROSPECT (dest_number): if the same prospect has an earlier booked call, this
        -- later one is a duplicate/re-touch and must NOT count again — one booking per lead.
        (CASE WHEN c.answered AND c.talk_seconds >= %(rpc_min)s AND COALESCE(cl.call_outcome, '') <> 'voicemail'
                   -- Effective booking honours a BDM booking-outcome override (#4c) over the AI verdict.
                   AND (CASE WHEN qo.booking_outcome='counts' THEN true
                             WHEN qo.booking_outcome IN ('tentative','not_booking','confirmation') THEN false
                             ELSE cl.meeting_booked END)
                   AND NOT (CASE WHEN qo.booking_outcome='confirmation' THEN true
                                 WHEN qo.booking_outcome IN ('counts','tentative','not_booking','rescheduled') THEN false
                                 ELSE COALESCE(cl.meeting_confirmation, false) END)
                   AND NOT (CASE WHEN qo.booking_outcome='rescheduled' THEN true
                                 WHEN qo.booking_outcome IN ('counts','tentative','not_booking','confirmation') THEN false
                                 ELSE COALESCE(cl.meeting_rescheduled, false) END)
                   AND NOT COALESCE(cl.booking_already_exists, false)  -- referral/hand-off to a 2nd contact of an already-booked company
                   AND NOT EXISTS (
                         SELECT 1 FROM calls pc JOIN classifications pcl ON pcl.call_id = pc.call_id
                         WHERE pc.in_scope
                           -- same PROSPECT: same phone number OR same COMPANY (domain/name-slug key)
                           AND (right(regexp_replace(pc.dest_number,'[^0-9]','','g'),9) = right(regexp_replace(c.dest_number,'[^0-9]','','g'),9)
                                OR (cl.company_key IS NOT NULL AND pcl.company_key IS NOT NULL AND pcl.company_key = cl.company_key))
                           AND pc.started_at < c.started_at
                           AND pc.answered AND pc.talk_seconds >= %(rpc_min)s AND COALESCE(pcl.call_outcome,'') <> 'voicemail'
                           AND pcl.meeting_booked AND NOT COALESCE(pcl.meeting_confirmation,false)
                           AND NOT COALESCE(pcl.meeting_rescheduled,false) AND NOT COALESCE(pcl.booking_already_exists,false))
              THEN 1 ELSE 0 END) AS meetings_booked,
        -- Qualified Booked = the strict subset: a new booking where the prospect is
        -- QUALIFIED. Effective qualification = a BDM/admin OVERRIDE if present (#4b),
        -- else the AI verdict. Qualification is a PROSPECT-level fact: it counts if this
        -- call is (effectively) qualified OR ANY other in-scope call to the same number is.
        -- Same exclusion: confirmation/reschedule of an existing booking is not re-counted.
        (CASE WHEN c.answered AND c.talk_seconds >= %(rpc_min)s AND COALESCE(cl.call_outcome, '') <> 'voicemail'
                   AND (CASE WHEN qo.booking_outcome='counts' THEN true
                             WHEN qo.booking_outcome IN ('tentative','not_booking','confirmation') THEN false
                             ELSE cl.meeting_booked END)
                   AND NOT (CASE WHEN qo.booking_outcome='confirmation' THEN true
                                 WHEN qo.booking_outcome IN ('counts','tentative','not_booking','rescheduled') THEN false
                                 ELSE COALESCE(cl.meeting_confirmation, false) END)
                   AND NOT (CASE WHEN qo.booking_outcome='rescheduled' THEN true
                                 WHEN qo.booking_outcome IN ('counts','tentative','not_booking','confirmation') THEN false
                                 ELSE COALESCE(cl.meeting_rescheduled, false) END)
                   AND NOT COALESCE(cl.booking_already_exists, false)
                   AND NOT EXISTS (
                         SELECT 1 FROM calls pc JOIN classifications pcl ON pcl.call_id = pc.call_id
                         WHERE pc.in_scope
                           AND (right(regexp_replace(pc.dest_number,'[^0-9]','','g'),9) = right(regexp_replace(c.dest_number,'[^0-9]','','g'),9)
                                OR (cl.company_key IS NOT NULL AND pcl.company_key IS NOT NULL AND pcl.company_key = cl.company_key))
                           AND pc.started_at < c.started_at
                           AND pc.answered AND pc.talk_seconds >= %(rpc_min)s AND COALESCE(pcl.call_outcome,'') <> 'voicemail'
                           AND pcl.meeting_booked AND NOT COALESCE(pcl.meeting_confirmation,false)
                           AND NOT COALESCE(pcl.meeting_rescheduled,false) AND NOT COALESCE(pcl.booking_already_exists,false))
                   -- Effective qualification is a PROSPECT/COMPANY-level fact: this booking counts
                   -- as qualified if THIS call is (effectively) qualified, OR ANY in-scope call to
                   -- the same NUMBER **or the same COMPANY** is (so a BDM override on a referral /
                   -- reschedule / different contact of the same company still qualifies the booking).
                   AND (COALESCE(qo.qualified, cl.qualified) OR EXISTS (
                         SELECT 1 FROM calls c2 JOIN classifications cl2 ON cl2.call_id = c2.call_id
                         LEFT JOIN qualification_overrides qo2 ON qo2.call_id = c2.call_id
                         WHERE c2.in_scope
                           AND (right(regexp_replace(c2.dest_number,'[^0-9]','','g'),9) = right(regexp_replace(c.dest_number,'[^0-9]','','g'),9)
                                OR (cl.company_key IS NOT NULL AND cl2.company_key IS NOT NULL AND cl2.company_key = cl.company_key))
                           AND COALESCE(qo2.qualified, cl2.qualified)))
              THEN 1 ELSE 0 END) AS qualified_booked,
        -- "Done" is optional/future from calendar/CRM; 0 unless that adapter is wired.
        (CASE WHEN EXISTS (SELECT 1 FROM meetings m
                           WHERE m.call_id = c.call_id AND m.meeting_done)
              THEN 1 ELSE 0 END) AS meetings_done,
        -- Lead quality (interested pipeline) + pipeline routing. Nested under
        -- "connected" (a real live human); temperature is only set for Pipeline 1.
        (CASE WHEN c.answered AND c.talk_seconds >= %(rpc_min)s AND COALESCE(cl.call_outcome, '') <> 'voicemail'
                   AND cl.pipeline = 'pipeline1_interested' THEN 1 ELSE 0 END) AS pipeline1,
        (CASE WHEN c.answered AND c.talk_seconds >= %(rpc_min)s AND COALESCE(cl.call_outcome, '') <> 'voicemail'
                   AND cl.pipeline = 'pipeline2_existing_agency' THEN 1 ELSE 0 END) AS pipeline2,
        (CASE WHEN c.answered AND c.talk_seconds >= %(rpc_min)s AND COALESCE(cl.call_outcome, '') <> 'voicemail'
                   AND cl.lead_temperature = 'warm' THEN 1 ELSE 0 END) AS warm,
        (CASE WHEN c.answered AND c.talk_seconds >= %(rpc_min)s AND COALESCE(cl.call_outcome, '') <> 'voicemail'
                   AND cl.lead_temperature = 'hot' THEN 1 ELSE 0 END) AS hot,
        (CASE WHEN c.answered AND c.talk_seconds >= %(rpc_min)s AND COALESCE(cl.call_outcome, '') <> 'voicemail'
                   AND cl.lead_temperature = 'super_hot' THEN 1 ELSE 0 END) AS super_hot
    FROM calls c
    LEFT JOIN classifications cl ON cl.call_id = c.call_id
    LEFT JOIN qualification_overrides qo ON qo.call_id = c.call_id
    WHERE c.in_scope
      AND c.started_at >= %(start)s AND c.started_at < %(end)s
)
SELECT
    GROUPING(bde) AS g_bde,
    GROUPING(ff)  AS g_ff,
    bde, ff,
    SUM(calls_made)   AS calls_made,
    SUM(connected)    AS connected,
    SUM(transcribed)  AS transcribed,
    SUM(rpc_connect)  AS rpc_connect,
    SUM(full_pitch)   AS full_pitch,
    SUM(leads)        AS leads,
    SUM(qualified)    AS qualified,
    SUM(meetings_booked) AS meetings_booked,
    SUM(qualified_booked) AS qualified_booked,
    SUM(meetings_done)   AS meetings_done,
    SUM(pipeline1)       AS pipeline1,
    SUM(pipeline2)       AS pipeline2,
    SUM(warm)            AS warm,
    SUM(hot)             AS hot,
    SUM(super_hot)       AS super_hot
FROM base
GROUP BY GROUPING SETS ((bde, ff), (bde), (ff), ())
"""


def _inscope_bde_names(pool: ConnectionPool) -> list[str]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT COALESCE(bde_name, extension) AS name "
            "FROM bde_agents WHERE in_scope AND active"
        )
        return sorted(str(r["name"]) for r in cur.fetchall() if r["name"])


def aggregate_day(pool: ConnectionPool, settings: Settings, day: date) -> dict:
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)

    # 1. Aggregate the calls that exist.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            _AGG_SQL,
            {"rpc_min": settings.rpc_min_talk_seconds, "start": start, "end": end},
        )
        agg_rows = cur.fetchall()

    # 2. Index by (bde_name, track).
    computed: dict[tuple[str, str], dict] = {}
    for r in agg_rows:
        bde = "ALL" if r["g_bde"] == 1 else str(r["bde"])
        track = "combined" if r["g_ff"] == 1 else str(r["ff"])
        computed[(bde, track)] = r

    # 3. Ensure every in-scope BDE + ALL has all three tracks (zeros if no activity).
    scopes = [*_inscope_bde_names(pool), "ALL"]
    rows_to_write: list[dict] = []
    for bde in scopes:
        for track in TRACKS:
            src = computed.get((bde, track))
            # SUM over an empty grouping set yields NULL -> coalesce to 0.
            # Every stage (incl. qualified/meetings) is split by track now:
            # fresh + followup == combined for all additive stages.
            vals = {k: int(src[k] or 0) for k in _ZERO} if src else dict(_ZERO)
            rows_to_write.append({"report_date": day, "bde_name": bde, "track": track, **vals})

    # 4. Recompute-and-upsert: clear the day, then insert.
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM daily_funnel WHERE report_date = %s", (day,))
            cur.executemany(
                """
                INSERT INTO daily_funnel (
                    report_date, bde_name, track, calls_made, connected, transcribed,
                    rpc_connect, full_pitch, leads, qualified, meetings_booked, qualified_booked,
                    meetings_done, warm, hot, super_hot, pipeline1, pipeline2)
                VALUES (
                    %(report_date)s, %(bde_name)s, %(track)s, %(calls_made)s, %(connected)s,
                    %(transcribed)s, %(rpc_connect)s, %(full_pitch)s, %(leads)s,
                    %(qualified)s, %(meetings_booked)s, %(qualified_booked)s, %(meetings_done)s,
                    %(warm)s, %(hot)s, %(super_hot)s, %(pipeline1)s, %(pipeline2)s)
                """,
                rows_to_write,
            )
        conn.commit()

    log.info("aggregate_day_done", day=str(day), rows=len(rows_to_write), scopes=len(scopes))
    return {"rows": len(rows_to_write), "scopes": len(scopes)}
