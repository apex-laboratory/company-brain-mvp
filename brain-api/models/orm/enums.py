"""Shared SQLAlchemy enum types.

``source_provider`` is referenced by columns across several tables
(source_connections/channels, webhook_subscriptions, source_events, decisions,
reviews, brain_*). It was previously redefined in each model module, so adding a
provider meant editing every copy — and missing one made the ORM raise
``LookupError`` when reading a row whose value the stale copy didn't list. Define
it once here so the member list stays the single source of truth, matched by the
``source_provider`` Postgres type the migrations manage (``create_type=False``).
"""
from sqlalchemy import Enum

# Keep in lockstep with the DB ``source_provider`` type (alembic) and the
# ``SourceProvider`` literal in app/integrations/source_oauth.py.
SOURCE_PROVIDER_VALUES = ("slack", "notion", "github", "jira", "zendesk", "google_drive")

source_provider_enum = Enum(
    *SOURCE_PROVIDER_VALUES, name="source_provider", create_type=False
)
