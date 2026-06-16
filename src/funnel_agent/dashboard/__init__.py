"""Live web dashboard (FastAPI + ECharts) over the analytics Postgres.

Serves the funnel per BDE + overall, a per-BDE leaderboard with stage conversion,
trend lines, transcript-coverage, the human-review queue, and call drill-down
(BDE -> number -> call -> transcript -> AI evidence). Read-only over `daily_funnel`,
`calls`, `transcripts`, `classifications`.
"""
