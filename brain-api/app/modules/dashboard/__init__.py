"""Dashboard module (KAN-58).

Powers the home screen: an aggregated ``GET /overview`` (workspace, greeting,
sync status, KPIs, recent items, source health, recent activity) and a
cursor-paginated ``GET /activity`` feed. Read-only; every query is
workspace-scoped under RLS.
"""
