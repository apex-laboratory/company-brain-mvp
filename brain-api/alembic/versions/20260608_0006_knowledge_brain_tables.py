"""Knowledge + Brain tables: decisions, reviews, builds, conversations, messages, activity, companies

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-08

Adds the knowledge governance and brain interaction plane:

decisions        — projection of AI-service decisions; workspace-scoped, soft-deleted.
                   Carries ai_decision_id (UUID) to link back to the AI service.
decision_pins    — user-pinned decisions; composite PK (decision_id, user_id).
reviews          — human-in-the-loop review queue (replaces review_queue from 0001).
                   Carries ai_review_id (UUID). Resolved reviews may produce a decision_id.
brain_builds     — job status mirror for AI-service extraction runs (triggered via brain/builds).
brain_conversations + brain_messages — full conversation history; messages are append-only.
activity_events  — lightweight dashboard feed; one row per notable system event.
companies        — global marketing waitlist (pre-workspace, not tenant-scoped, no RLS).

New enums added here (all others were created in 0003):
  decision_status  — approved | active | review
  review_kind      — policy_change | new_decision | contradiction | exception
  review_status    — pending | approved | rejected
  build_status     — queued | running | completed | failed | canceled
  message_role     — user | assistant

RLS is enabled on all tenant-scoped tables. Policies come in migration 0007.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID, ENUM

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ── Enums ──────────────────────────────────────────────────────────────────────
_ENUMS = [
    ("decision_status", ["approved", "active", "review"]),
    ("review_kind",     ["policy_change", "new_decision", "contradiction", "exception"]),
    ("review_status",   ["pending", "approved", "rejected"]),
    ("build_status",    ["queued", "running", "completed", "failed", "canceled"]),
    ("message_role",    ["user", "assistant"]),
]


def upgrade() -> None:
    # ── Create enums ───────────────────────────────────────────────────────────
    for name, values in _ENUMS:
        op.execute(f"CREATE TYPE {name} AS ENUM ({', '.join(repr(v) for v in values)})")

    # ── decisions ──────────────────────────────────────────────────────────────
    # Projection of AI-service decisions. The AI service is the system of record;
    # ai_decision_id links back to it. Soft-deleted (deleted_at IS NULL filter
    # applied in all reads).
    op.create_table(
        "decisions",
        sa.Column("id",              sa.Text, primary_key=True),              # dec_…
        sa.Column("workspace_id",    sa.Text,
                                     sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                     nullable=False),
        sa.Column("ai_decision_id",  UUID(as_uuid=True)),                     # link to AI service
        sa.Column("title",           sa.Text, nullable=False),
        sa.Column("source_provider", ENUM("slack", "notion", "github", "jira", "zendesk",
                                             name="source_provider", create_type=False)),
        sa.Column("source_location", sa.Text),
        sa.Column("status",          ENUM("approved", "active", "review",
                                             name="decision_status", create_type=False),
                                     nullable=False, server_default=sa.text("'review'")),
        sa.Column("confidence",      sa.Integer),                             # 0–100
        sa.Column("category",        sa.Text),
        sa.Column("owner_user_id",   sa.Text,
                                     sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("monthly_uses",    sa.Integer,
                                     nullable=False, server_default=sa.text("0")),
        sa.Column("summary",         sa.Text),
        sa.Column("rule",            sa.Text),
        sa.Column("provenance",      JSONB),                                  # {sourceProvider, url, extractedAt, …}
        sa.Column("deleted_at",      sa.DateTime(timezone=True)),
        sa.Column("updated_at",      sa.DateTime(timezone=True),
                                     nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at",      sa.DateTime(timezone=True),
                                     nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(None, "decisions", ["workspace_id", "status"])
    op.create_index(None, "decisions", ["workspace_id", "category"])
    op.create_index(None, "decisions", ["workspace_id", "source_provider"])
    # Full-text search on title + summary (used by GET /decisions?q=)
    op.execute(
        "CREATE INDEX decisions_search ON decisions "
        "USING gin (to_tsvector('english', coalesce(title,'') || ' ' || coalesce(summary,'')))"
    )
    op.execute("ALTER TABLE decisions ENABLE ROW LEVEL SECURITY")

    # ── decision_pins ──────────────────────────────────────────────────────────
    # User-pinned decisions. Composite PK so there's at most one pin per
    # (decision, user) pair. workspace_id is denormalised for the RLS predicate.
    op.create_table(
        "decision_pins",
        sa.Column("workspace_id",  sa.Text,
                                   sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                   nullable=False),
        sa.Column("decision_id",   sa.Text,
                                   sa.ForeignKey("decisions.id", ondelete="CASCADE"),
                                   nullable=False),
        sa.Column("user_id",       sa.Text,
                                   sa.ForeignKey("users.id", ondelete="CASCADE"),
                                   nullable=False),
        sa.Column("created_at",    sa.DateTime(timezone=True),
                                   nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("decision_id", "user_id",
                                name="decision_pins_pkey"),
    )
    op.execute("ALTER TABLE decision_pins ENABLE ROW LEVEL SECURITY")

    # ── reviews ────────────────────────────────────────────────────────────────
    # Human-in-the-loop review queue, replacing review_queue from 0001.
    # decision_id is set when an 'approved' verdict produces a decisions row.
    # skill_id links to a skill that may be updated as part of the review.
    op.create_table(
        "reviews",
        sa.Column("id",               sa.Text, primary_key=True),             # rev_…
        sa.Column("workspace_id",     sa.Text,
                                      sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                      nullable=False),
        sa.Column("ai_review_id",     UUID(as_uuid=True)),                    # AI-service queue id
        sa.Column("title",            sa.Text, nullable=False),
        sa.Column("kind",             ENUM("policy_change", "new_decision",
                                              "contradiction", "exception",
                                              name="review_kind", create_type=False),
                                      nullable=False),
        sa.Column("source_provider",  ENUM("slack", "notion", "github", "jira", "zendesk",
                                              name="source_provider", create_type=False)),
        sa.Column("source_location",  sa.Text),
        sa.Column("before_text",      sa.Text),
        sa.Column("after_text",       sa.Text),
        sa.Column("evidence_quote",   sa.Text),
        sa.Column("evidence_author",  sa.Text),
        sa.Column("confidence",       sa.Integer),                            # 0–100
        sa.Column("status",           ENUM("pending", "approved", "rejected",
                                              name="review_status", create_type=False),
                                      nullable=False, server_default=sa.text("'pending'")),
        sa.Column("verdict",          sa.Text),                               # 'approve' | 'reject'
        sa.Column("comment",          sa.Text),
        sa.Column("resolved_by",      sa.Text,
                                      sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("decision_id",      sa.Text,
                                      sa.ForeignKey("decisions.id", ondelete="SET NULL")),
        sa.Column("skill_id",         sa.Text,
                                      sa.ForeignKey("skills.id", ondelete="SET NULL")),
        sa.Column("merged_into_brain", sa.Boolean,
                                      nullable=False, server_default=sa.text("FALSE")),
        sa.Column("created_at",       sa.DateTime(timezone=True),
                                      nullable=False, server_default=sa.text("now()")),
        sa.Column("resolved_at",      sa.DateTime(timezone=True)),
    )
    op.create_index(None, "reviews", ["workspace_id", "status"])
    op.execute("ALTER TABLE reviews ENABLE ROW LEVEL SECURITY")

    # ── brain_builds ───────────────────────────────────────────────────────────
    # Status mirror for AI-service extraction jobs.  The backend creates a row
    # (queued), enqueues an ARQ job that calls the AI service, and polls/receives
    # progress to update it. ai_job_id correlates to the AI-service job.
    op.create_table(
        "brain_builds",
        sa.Column("id",           sa.Text, primary_key=True),                 # bld_…
        sa.Column("workspace_id", sa.Text,
                                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                  nullable=False),
        sa.Column("status",       ENUM("queued", "running", "completed",
                                          "failed", "canceled",
                                          name="build_status", create_type=False),
                                  nullable=False, server_default=sa.text("'queued'")),
        sa.Column("progress",     sa.Integer,
                                  nullable=False, server_default=sa.text("0")),   # 0–100
        sa.Column("current_step", sa.Text),
        sa.Column("time_range",   sa.Text),                                   # '30d' | '90d' | '6mo' | 'all'
        sa.Column("source_ids",   sa.ARRAY(sa.Text),
                                  nullable=False, server_default=sa.text("'{}'")),
        sa.Column("extract",      sa.ARRAY(sa.Text),
                                  nullable=False, server_default=sa.text("'{}'")),  # ['decisions','skills',…]
        sa.Column("counts",       JSONB,
                                  nullable=False, server_default=sa.text("'{}'")),  # {sourcesRead, …}
        sa.Column("ai_job_id",    sa.Text),
        sa.Column("triggered_by", sa.Text,
                                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at",   sa.DateTime(timezone=True),
                                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at",   sa.DateTime(timezone=True),
                                  nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(None, "brain_builds", ["workspace_id", "status"])
    op.execute("ALTER TABLE brain_builds ENABLE ROW LEVEL SECURITY")

    # ── brain_conversations ────────────────────────────────────────────────────
    op.create_table(
        "brain_conversations",
        sa.Column("id",           sa.Text, primary_key=True),                 # cnv_…
        sa.Column("workspace_id", sa.Text,
                                  sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                  nullable=False),
        sa.Column("user_id",      sa.Text,
                                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("title",        sa.Text),
        sa.Column("created_at",   sa.DateTime(timezone=True),
                                  nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at",   sa.DateTime(timezone=True),
                                  nullable=False, server_default=sa.text("now()")),
    )
    # Sort by recency for the conversation list sidebar
    op.execute(
        "CREATE INDEX ON brain_conversations (workspace_id, updated_at DESC)"
    )
    op.execute("ALTER TABLE brain_conversations ENABLE ROW LEVEL SECURITY")

    # ── brain_messages ─────────────────────────────────────────────────────────
    # Append-only: no UPDATE policy will be created in 0007.
    # sources JSONB: [{provider, label, sourceItemId, url, excerpt}]
    op.create_table(
        "brain_messages",
        sa.Column("id",              sa.Text, primary_key=True),              # msg_…
        sa.Column("workspace_id",    sa.Text,
                                     sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                     nullable=False),
        sa.Column("conversation_id", sa.Text,
                                     sa.ForeignKey("brain_conversations.id", ondelete="CASCADE"),
                                     nullable=False),
        sa.Column("role",            ENUM("user", "assistant",
                                             name="message_role", create_type=False),
                                     nullable=False),
        sa.Column("content",         sa.Text, nullable=False),
        sa.Column("confidence",      sa.Integer),                             # 0–100; assistant turns only
        sa.Column("sources",         JSONB),
        sa.Column("created_at",      sa.DateTime(timezone=True),
                                     nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(None, "brain_messages",
                    ["workspace_id", "conversation_id", "created_at"])
    op.execute("ALTER TABLE brain_messages ENABLE ROW LEVEL SECURITY")

    # ── activity_events ────────────────────────────────────────────────────────
    # Lightweight dashboard feed. Written by the backend on significant events;
    # read for the activity timeline. No updates — effectively append-only.
    op.create_table(
        "activity_events",
        sa.Column("id",              sa.Text, primary_key=True),              # evt_…
        sa.Column("workspace_id",    sa.Text,
                                     sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
                                     nullable=False),
        sa.Column("type",            sa.Text, nullable=False),               # 'skill' | 'decision' | 'review' | 'source' …
        sa.Column("title",           sa.Text, nullable=False),
        sa.Column("detail",          sa.Text),
        sa.Column("source_provider", ENUM("slack", "notion", "github", "jira", "zendesk",
                                             name="source_provider", create_type=False)),
        sa.Column("created_at",      sa.DateTime(timezone=True),
                                     nullable=False, server_default=sa.text("now()")),
    )
    op.execute(
        "CREATE INDEX ON activity_events (workspace_id, created_at DESC)"
    )
    op.execute("ALTER TABLE activity_events ENABLE ROW LEVEL SECURITY")

    # ── companies ──────────────────────────────────────────────────────────────
    # Global marketing / design-partner waitlist. Pre-dates any workspace, so
    # there is no workspace_id here and RLS is NOT enabled — it is written by the
    # public POST /api/companies route with no auth required.
    op.create_table(
        "companies",
        sa.Column("id",           sa.Text, primary_key=True),                 # cmp_…
        sa.Column("contact_name", sa.Text, nullable=False),
        sa.Column("email",        sa.Text, nullable=False, unique=True),      # lower()-normalised in app
        sa.Column("company",      sa.Text, nullable=False),
        sa.Column("company_size", sa.Text),
        sa.Column("role",         sa.Text),
        sa.Column("created_at",   sa.DateTime(timezone=True),
                                  nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("companies")
    op.drop_table("activity_events")
    op.drop_table("brain_messages")
    op.drop_table("brain_conversations")
    op.drop_table("brain_builds")
    op.drop_table("reviews")
    op.drop_table("decision_pins")
    op.drop_table("decisions")

    for name, _ in reversed(_ENUMS):
        op.execute(f"DROP TYPE IF EXISTS {name}")
