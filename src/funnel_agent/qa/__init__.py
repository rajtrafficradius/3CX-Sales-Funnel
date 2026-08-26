"""Quality-Assurance subsystem (Phase 0 foundation + Phase 1 gates).

The QA layer NEVER touches the dial loop, call placement, dial pacing, the stuck-call reaper,
or Lisa's Retell prompt / what she speaks. It only:

  * VERIFIES outcomes at the booking-write points (in the Retell WEBHOOK handlers, off the dial loop),
  * SCRUBS / fills brief dynamic-variables in memory before they are handed to Retell (pure CPU), and
  * SANITISES outbound SMS/email at the single send chokepoint (off the dial loop).

Operator directive (#1, firm): STOP trusting Retell's analytics. `meeting_agreed` / `call_outcome` /
`custom_analysis_data` are NEVER authoritative for a decision — only the TRANSCRIPT (or our own
transcription) is. Every gate here derives its verdict from the transcript + hard telephony facts
(disconnect reason, duration), never from Retell's post-call analysis flags.

Modules
-------
- ``audit``   — the ``qa_audit`` marker table + a guarded, never-raising event logger (Phase 0).
- ``gates``   — G1 booking gate (pure): honour a "booked" only for a genuine two-party conversation
                with a concrete agreed time.
- ``dynvars`` — G8 residual ``{{ }}`` scrub, G9 no-website invariant, G10 clean prospect name (all
                pure, in-memory).
- ``outbound``— G11/G12 send-chokepoint sanitizer: scrub banned phrases + force URLs through the
                branded shortlink.

Every function in this package is written to be side-effect-safe: on any internal error it returns
its input unchanged (or a safe default) and never raises into the caller's flow.
"""

from __future__ import annotations

__all__ = ["audit", "gates", "dynvars", "outbound"]

# Package version marker — bumped as gates are added across phases.
QA_PHASE = "0+1"
