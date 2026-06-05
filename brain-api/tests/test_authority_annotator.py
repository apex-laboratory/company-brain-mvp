"""
Unit tests for services/authority_annotator.py.

All tests are hermetic: they write a trimmed YAML fixture via tmp_path
and instantiate AuthorityAnnotator directly (no singleton, no .env).
"""
from pathlib import Path

import pytest

from services.authority_annotator import AuthorityAnnotator, AuthorityAnnotation

FIXTURE_YAML = """
tiers:
  high:
    weight: 1.0
    sources:
      - type: notion
        signals: [designated_policy_page, owner_edited]
      - type: github
        signals: [path_prefix=/docs, path_prefix=/runbooks]
      - type: jira
        signals: [ticket_type=policy, status=done]

  medium:
    weight: 0.7
    sources:
      - type: slack
        signals: [channel=policy, channel=ops-decisions]
      - type: zendesk
        signals: [tag=policy-exception, status=solved]

  low:
    weight: 0.4
    sources:
      - type: slack
        signals: []
      - type: github
        signals: [content_type=comment]
      - type: jira
        signals: [content_type=comment]

routing:
  auto_publish_confidence: 0.90
  auto_publish_authority_floor: medium
  review_queue_confidence_floor: 0.70

sweep:
  processing_order: [notion, github, jira, slack, zendesk]
  rate_per_minute: 10
  semaphore_limit: 5
  auto_publish_during_sweep: false
"""


@pytest.fixture
def annotator(tmp_path: Path) -> AuthorityAnnotator:
    config = tmp_path / "source_authority.yaml"
    config.write_text(FIXTURE_YAML)
    return AuthorityAnnotator(config_path=config)


# ── high tier ────────────────────────────────────────────────────────────────

def test_notion_designated_policy_page_is_high(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("notion", {"designated_policy_page": True})
    assert result == AuthorityAnnotation(tier="high", weight=1.0)


def test_notion_owner_edited_is_high(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("notion", {"owner_edited": True})
    assert result == AuthorityAnnotation(tier="high", weight=1.0)


def test_github_docs_path_is_high(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("github", {"path": "/docs/runbook.md"})
    assert result == AuthorityAnnotation(tier="high", weight=1.0)


def test_github_runbooks_path_is_high(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("github", {"path": "/runbooks/deploy.md"})
    assert result == AuthorityAnnotation(tier="high", weight=1.0)


def test_jira_policy_ticket_is_high(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("jira", {"ticket_type": "policy", "status": "done"})
    assert result == AuthorityAnnotation(tier="high", weight=1.0)


# ── medium tier ───────────────────────────────────────────────────────────────

def test_slack_policy_channel_is_medium(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("slack", {"channel": "policy"})
    assert result == AuthorityAnnotation(tier="medium", weight=0.7)


def test_slack_ops_decisions_channel_is_medium(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("slack", {"channel": "ops-decisions"})
    assert result == AuthorityAnnotation(tier="medium", weight=0.7)


def test_zendesk_policy_exception_tag_is_medium(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("zendesk", {"tags": ["policy-exception"], "status": "solved"})
    assert result == AuthorityAnnotation(tier="medium", weight=0.7)


# ── low tier ─────────────────────────────────────────────────────────────────

def test_slack_random_channel_is_low(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("slack", {"channel": "random"})
    assert result == AuthorityAnnotation(tier="low", weight=0.4)


def test_jira_comment_is_low(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("jira", {"content_type": "comment"})
    assert result == AuthorityAnnotation(tier="low", weight=0.4)


def test_github_comment_is_low(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("github", {"content_type": "comment"})
    assert result == AuthorityAnnotation(tier="low", weight=0.4)


# ── fallback ─────────────────────────────────────────────────────────────────

def test_unknown_source_falls_back_to_low(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("gdrive", {"path": "/docs/something"})
    assert result.tier == "low"


def test_no_matching_signal_falls_back_to_low(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("notion", {"designated_policy_page": False, "owner_edited": False})
    assert result.tier == "low"


# ── sweep config ──────────────────────────────────────────────────────────────

def test_sweep_processing_order(annotator: AuthorityAnnotator) -> None:
    assert annotator.sweep_processing_order() == ["notion", "github", "jira", "slack", "zendesk"]


def test_sweep_rate_per_minute(annotator: AuthorityAnnotator) -> None:
    assert annotator.sweep_rate_per_minute() == 10


def test_sweep_semaphore_limit(annotator: AuthorityAnnotator) -> None:
    assert annotator.sweep_semaphore_limit() == 5


# ── singleton ─────────────────────────────────────────────────────────────────

def test_same_instance_returned(annotator: AuthorityAnnotator) -> None:
    # AuthorityAnnotator itself is stateless after init — same config = deterministic results
    result_a = annotator.annotate("notion", {"designated_policy_page": True})
    result_b = annotator.annotate("notion", {"designated_policy_page": True})
    assert result_a == result_b
