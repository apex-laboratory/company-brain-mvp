# `raw_content` Mapping Contract

This document defines the canonical contract for normalizing all connector output into the `raw_content` table.

It covers:

- the shared row contract
- source-specific mappings for Zendesk, Slack, Notion, GitHub, and Jira
- sample rows
- the decision on where normalization should happen

## Decision

Normalization should happen in a post-sync normalization step, not inside Airbyte destination mapping.

Why:

- Airbyte should stay responsible for reliable extraction and loading into source-specific raw tables.
- Cross-source normalization logic is product logic and should live in version-controlled code or SQL we own.
- A post-sync step is testable, reviewable, and easier to evolve as sources add fields or new entity types.
- The same contract can support future sources such as GitHub and Jira without depending on manual Airbyte UI configuration.

Recommended flow:

1. Airbyte syncs each source into its own raw staging tables.
2. A normalization job transforms staged rows into canonical `raw_content` rows.
3. The ingestion pipeline classifies and processes `raw_content`.

## Canonical Row Contract

Each `raw_content` row must represent one atomic source artifact that can be classified independently.

Current table:

- `source`: stable source system name
- `source_id`: stable unique identifier for the artifact within `source`
- `content`: plain-text body optimized for downstream classification and extraction
- `metadata`: structured context required for traceability, filtering, and re-processing

### Field Rules

#### `source`

Allowed values for this contract:

- `zendesk`
- `slack`
- `notion`
- `github`
- `jira`

#### `source_id`

`source_id` must be:

- stable across re-syncs
- unique within a source
- human-debuggable

Format:

```text
<entity_type>:<primary_id>
```

For nested artifacts:

```text
<entity_type>:<primary_id>:<child_id>
```

Examples:

- `ticket:12345`
- `ticket_comment:12345:67890`
- `slack_message:C024BE91L:1717428734.000200`
- `notion_page:15f1c1e2b6d680f8a7b3d9a1d6ef0012`
- `github_issue_comment:company-brain-mvp:42:99118822`
- `jira_comment:SUP-123:10001`

#### `content`

`content` must be:

- plain text, not HTML or markdown blobs when avoidable
- self-contained enough for a classifier to understand the artifact without another join
- trimmed of transport-only wrapper data

`content` should include the highest-signal text for the artifact:

- ticket subject plus comment body for support artifacts
- message text for chat artifacts
- page title plus page body for docs artifacts
- issue title plus body for tracker artifacts

#### `metadata`

`metadata` is JSONB and must contain a shared core shape across all sources.

Required metadata keys for every row:

- `entity_type`: source-specific artifact type such as `ticket`, `message`, `page`, `issue`
- `record_url`: canonical URL back to the source record
- `title`: short human-readable title if available, otherwise `null`
- `author`: object with stable identity fields when available
- `created_at`: source-system creation timestamp in ISO 8601 format
- `updated_at`: source-system update timestamp in ISO 8601 format
- `tags`: array of labels, channel names, components, or equivalent routing markers
- `airbyte_stream`: Airbyte stream name that produced the raw record

Recommended metadata keys when available:

- `parent_source_id`: parent artifact identifier, for example a comment's ticket or issue
- `thread_id`: thread or conversation identifier
- `status`: workflow state such as `open`, `closed`, `published`
- `priority`: source priority or severity
- `assignee`: assigned user identity
- `source_timestamp`: event timestamp when different from `created_at`
- `raw_id`: original source-native primary key when it differs from the canonical `source_id`

### Author Shape

When `author` is available, use:

```json
{
  "id": "string-or-null",
  "name": "string-or-null",
  "email": "string-or-null",
  "handle": "string-or-null"
}
```

Use `null` for unavailable fields. Do not omit `author`.

## Source Mappings

## Zendesk

Airbyte streams in scope:

- `tickets`
- `ticket_comments`
- `ticket_events`
- optionally `ticket_fields` as metadata enrichment, not standalone `raw_content`

Mapping:

| Artifact | `source` | `source_id` | `content` | Required metadata additions |
| --- | --- | --- | --- | --- |
| Ticket | `zendesk` | `ticket:<ticket_id>` | Ticket subject + two newlines + latest public description/body | `entity_type=ticket`, `status`, `priority`, `assignee`, `tags`, `requester`, `ticket_form_id` |
| Ticket comment | `zendesk` | `ticket_comment:<ticket_id>:<comment_id>` | Comment body text | `entity_type=ticket_comment`, `parent_source_id=ticket:<ticket_id>`, `is_public`, `via`, `comment_author_role` |
| Ticket event | `zendesk` | `ticket_event:<ticket_id>:<event_id>` | Concise event sentence such as `status changed from pending to solved` | `entity_type=ticket_event`, `parent_source_id=ticket:<ticket_id>`, `event_type`, `field_name`, `previous_value`, `new_value`, `source_timestamp` |

Notes:

- `ticket_fields` should enrich ticket metadata, not create standalone rows.
- Ticket events are especially important for PM4Py, so their metadata should preserve transition semantics.

## Slack

Airbyte stream in scope:

- channel message stream for selected channels

Mapping:

| Artifact | `source` | `source_id` | `content` | Required metadata additions |
| --- | --- | --- | --- | --- |
| Channel message | `slack` | `slack_message:<channel_id>:<ts>` | Message text with user mentions resolved when possible | `entity_type=message`, `channel_id`, `channel_name`, `thread_id`, `parent_source_id`, `is_thread_root`, `author.handle`, `permalink` in `record_url` |
| Thread reply | `slack` | `slack_message:<channel_id>:<ts>` | Reply text | `entity_type=thread_reply`, `thread_id`, `parent_source_id=slack_message:<channel_id>:<thread_ts>`, `channel_id`, `channel_name` |

Notes:

- Use one row per message, including replies.
- Ignore reactions and join/leave system noise unless we later prove they carry policy signal.

## Notion

Airbyte streams in scope:

- pages
- databases when exposed as records with rich text properties

Mapping:

| Artifact | `source` | `source_id` | `content` | Required metadata additions |
| --- | --- | --- | --- | --- |
| Page | `notion` | `notion_page:<page_id>` | Page title + two newlines + flattened page body text | `entity_type=page`, `workspace_id`, `last_edited_by`, `parent_type`, `parent_id`, `status`, `tags` |
| Database row | `notion` | `notion_database_row:<page_id>` | Primary title property + two newlines + selected rich text properties joined in readable form | `entity_type=database_row`, `database_id`, `parent_source_id=notion_database:<database_id>`, `property_snapshot`, `status`, `tags` |

Notes:

- Preserve enough metadata to rebuild structured properties later.
- `content` should flatten rich text but avoid dumping full low-level Notion block JSON.

## GitHub

Airbyte streams in scope for this contract:

- issues
- issue comments
- pull requests
- pull request review comments

Mapping:

| Artifact | `source` | `source_id` | `content` | Required metadata additions |
| --- | --- | --- | --- | --- |
| Issue | `github` | `github_issue:<repo_name>:<issue_number>` | Issue title + two newlines + issue body | `entity_type=issue`, `repo`, `state`, `labels`, `author.handle`, `assignee`, `milestone` |
| Issue comment | `github` | `github_issue_comment:<repo_name>:<issue_number>:<comment_id>` | Comment body | `entity_type=issue_comment`, `parent_source_id=github_issue:<repo_name>:<issue_number>`, `repo`, `author.handle` |
| Pull request | `github` | `github_pr:<repo_name>:<pr_number>` | PR title + two newlines + PR body | `entity_type=pull_request`, `repo`, `state`, `labels`, `base_branch`, `head_branch`, `merged_at` |
| PR review comment | `github` | `github_pr_review_comment:<repo_name>:<pr_number>:<comment_id>` | Review comment body | `entity_type=pr_review_comment`, `parent_source_id=github_pr:<repo_name>:<pr_number>`, `repo`, `path`, `line`, `side` |

Notes:

- GitHub artifacts are useful both for product-change policy and engineering workflow knowledge.
- Keep repository identity in both `source_id` and metadata to avoid collisions across repos.

## Jira

Airbyte streams in scope for this contract:

- issues
- comments
- changelog events when available

Mapping:

| Artifact | `source` | `source_id` | `content` | Required metadata additions |
| --- | --- | --- | --- | --- |
| Issue | `jira` | `jira_issue:<issue_key>` | Issue summary + two newlines + description | `entity_type=issue`, `project_key`, `issue_type`, `status`, `priority`, `labels`, `assignee` |
| Comment | `jira` | `jira_comment:<issue_key>:<comment_id>` | Comment body | `entity_type=comment`, `parent_source_id=jira_issue:<issue_key>`, `project_key`, `author.handle` |
| Changelog event | `jira` | `jira_event:<issue_key>:<history_id>` | Concise event sentence such as `priority changed from Medium to High` | `entity_type=changelog_event`, `parent_source_id=jira_issue:<issue_key>`, `field_name`, `previous_value`, `new_value`, `source_timestamp` |

Notes:

- Jira changelog rows should follow the same pattern as Zendesk events because they carry process signal.

## Sample Canonical Rows

These examples are intentionally short, but each one conforms to the contract.

### Zendesk ticket comment

```json
{
  "source": "zendesk",
  "source_id": "ticket_comment:12345:67890",
  "content": "Customer is eligible for a one-time refund because the shipment was delayed more than 10 days.",
  "metadata": {
    "entity_type": "ticket_comment",
    "record_url": "https://acme.zendesk.com/agent/tickets/12345/comments/67890",
    "title": "Refund request for delayed shipment",
    "author": {
      "id": "9981",
      "name": "Jamie Support",
      "email": "[email protected]",
      "handle": null
    },
    "created_at": "2026-05-01T09:12:33Z",
    "updated_at": "2026-05-01T09:12:33Z",
    "tags": ["refund", "shipping-delay"],
    "airbyte_stream": "ticket_comments",
    "parent_source_id": "ticket:12345",
    "status": "open",
    "priority": "normal",
    "is_public": true,
    "via": "web"
  }
}
```

### Slack message

```json
{
  "source": "slack",
  "source_id": "slack_message:C024BE91L:1717428734.000200",
  "content": "For invoices under $500, support can approve a courtesy credit without finance review.",
  "metadata": {
    "entity_type": "message",
    "record_url": "https://workspace.slack.com/archives/C024BE91L/p1717428734000200",
    "title": null,
    "author": {
      "id": "U12345",
      "name": "Afnan",
      "email": null,
      "handle": "afnan"
    },
    "created_at": "2026-05-03T14:12:14Z",
    "updated_at": "2026-05-03T14:12:14Z",
    "tags": ["ops-announcements"],
    "airbyte_stream": "messages",
    "channel_id": "C024BE91L",
    "channel_name": "ops-announcements",
    "thread_id": "1717428734.000200",
    "parent_source_id": null,
    "is_thread_root": true
  }
}
```

### Notion page

```json
{
  "source": "notion",
  "source_id": "notion_page:15f1c1e2b6d680f8a7b3d9a1d6ef0012",
  "content": "Refund Escalation Policy\n\nIf the customer is enterprise tier and the refund exceeds $2,000, route to the account team before approval.",
  "metadata": {
    "entity_type": "page",
    "record_url": "https://www.notion.so/15f1c1e2b6d680f8a7b3d9a1d6ef0012",
    "title": "Refund Escalation Policy",
    "author": {
      "id": "notion-user-17",
      "name": "Policy Ops",
      "email": null,
      "handle": null
    },
    "created_at": "2026-04-12T10:00:00Z",
    "updated_at": "2026-05-04T18:45:00Z",
    "tags": ["refunds", "enterprise"],
    "airbyte_stream": "pages",
    "workspace_id": "workspace-1",
    "parent_type": "page",
    "parent_id": "abc-parent",
    "status": "published"
  }
}
```

### GitHub issue

```json
{
  "source": "github",
  "source_id": "github_issue:company-brain-mvp:42",
  "content": "Normalize raw_content mapping contract\n\nWe need a single contract for Zendesk, Slack, Notion, GitHub, and Jira before connector work expands.",
  "metadata": {
    "entity_type": "issue",
    "record_url": "https://github.com/acme/company-brain-mvp/issues/42",
    "title": "Normalize raw_content mapping contract",
    "author": {
      "id": "9912",
      "name": "Teammate",
      "email": null,
      "handle": "teammate"
    },
    "created_at": "2026-05-07T08:00:00Z",
    "updated_at": "2026-05-08T11:20:00Z",
    "tags": ["ingestion", "data-contract"],
    "airbyte_stream": "issues",
    "repo": "company-brain-mvp",
    "state": "open",
    "assignee": "afnan",
    "milestone": null
  }
}
```

### Jira issue

```json
{
  "source": "jira",
  "source_id": "jira_issue:SUP-123",
  "content": "VIP refund escalation\n\nWhen a VIP customer requests a refund after renewal, route the case to retention before processing.",
  "metadata": {
    "entity_type": "issue",
    "record_url": "https://acme.atlassian.net/browse/SUP-123",
    "title": "VIP refund escalation",
    "author": {
      "id": "jira-user-88",
      "name": "Support Lead",
      "email": null,
      "handle": "support.lead"
    },
    "created_at": "2026-05-02T09:00:00Z",
    "updated_at": "2026-05-09T16:15:00Z",
    "tags": ["vip", "retention"],
    "airbyte_stream": "issues",
    "project_key": "SUP",
    "issue_type": "Task",
    "status": "In Progress",
    "priority": "High",
    "assignee": "ops-manager"
  }
}
```

## Validation Checklist

Any normalization implementation should enforce:

- `source` is one of the five allowed values
- `source_id` is non-null and stable
- `content` is non-empty after trimming
- `metadata.entity_type` is present
- `metadata.record_url` is present
- `metadata.author` exists, even if fields are null
- `metadata.created_at` and `metadata.updated_at` are present
- `metadata.airbyte_stream` is present

## Implementation Guidance

Recommended implementation order:

1. Keep Airbyte writing raw source tables unchanged.
2. Add one normalization SQL model or job per source family.
3. Union those normalized outputs into `raw_content`.
4. Add row-shape tests against the validation checklist above.

This keeps connector configuration simple while moving normalization into code we can review, diff, and test.

### Current implementations

| Source | Module | Endpoints |
| --- | --- | --- |
| Zendesk | [`brain-api/services/zendesk_normalizer.py`](../brain-api/services/zendesk_normalizer.py) | `POST /ingest/zendesk[/tickets|/comments|/events|/sample]` |
| Slack | _pending_ | — |
| Notion | _pending_ | — |
| GitHub | _pending_ | — |
| Jira | _pending_ | — |

The Zendesk normalizer enforces this contract via `validate_row` and upserts
on `UNIQUE (source, source_id)`. See
[`airbyte/zendesk-runbook.md`](./zendesk-runbook.md) for the end-to-end flow.
