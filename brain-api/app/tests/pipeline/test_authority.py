"""Unit tests for app/pipeline/authority.py.

Ported from tests/test_authority_annotator.py (services tree, now retired) and
extended for routing_config()/sweep_config() and the fail-soft path. Hermetic:
a trimmed YAML fixture via tmp_path, no singleton, no .env.
"""
from pathlib import Path

import pytest

from app.pipeline.authority import (
    AuthorityAnnotation,
    AuthorityAnnotator,
    RoutingConfig,
    SweepConfig,
)

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


def test_github_docs_path_is_high(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("github", {"path": "/docs/runbook.md"})
    assert result == AuthorityAnnotation(tier="high", weight=1.0)


def test_jira_policy_ticket_is_high(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("jira", {"ticket_type": "policy", "status": "done"})
    assert result == AuthorityAnnotation(tier="high", weight=1.0)


# ── medium tier ──────────────────────────────────────────────────────────────

def test_slack_policy_channel_is_medium(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("slack", {"channel": "policy"})
    assert result == AuthorityAnnotation(tier="medium", weight=0.7)


def test_zendesk_policy_exception_tag_is_medium(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("zendesk", {"tags": ["policy-exception"], "status": "solved"})
    assert result == AuthorityAnnotation(tier="medium", weight=0.7)


# ── low tier / fallback ──────────────────────────────────────────────────────

def test_slack_random_channel_is_low(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("slack", {"channel": "random"})
    assert result == AuthorityAnnotation(tier="low", weight=0.4)


def test_github_comment_is_low(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate("github", {"content_type": "comment"})
    assert result == AuthorityAnnotation(tier="low", weight=0.4)


def test_unknown_source_falls_back_to_low(annotator: AuthorityAnnotator) -> None:
    assert annotator.annotate("gdrive", {"path": "/docs/x"}).tier == "low"


def test_no_matching_signal_falls_back_to_low(annotator: AuthorityAnnotator) -> None:
    result = annotator.annotate(
        "notion", {"designated_policy_page": False, "owner_edited": False}
    )
    assert result.tier == "low"


# ── routing / sweep config ───────────────────────────────────────────────────

def test_routing_config_from_yaml(annotator: AuthorityAnnotator) -> None:
    # The fixture YAML still carries the retired auto_publish_* keys on purpose:
    # a deployment's config file will too, and stale keys must be ignored rather
    # than crash the loader (or quietly resurrect auto-publishing).
    assert annotator.routing_config() == RoutingConfig(review_queue_confidence_floor=0.70)


def test_sweep_config_from_yaml(annotator: AuthorityAnnotator) -> None:
    assert annotator.sweep_config() == SweepConfig(rate_per_minute=10, semaphore_limit=5)


# ── fail-soft ────────────────────────────────────────────────────────────────

def test_missing_file_falls_back_to_defaults(tmp_path: Path) -> None:
    annotator = AuthorityAnnotator(config_path=tmp_path / "nope.yaml")
    assert annotator.annotate("slack", {"channel": "policy"}).tier == "low"
    assert annotator.routing_config() == RoutingConfig()
    assert annotator.sweep_config() == SweepConfig()


def test_malformed_file_falls_back_to_defaults(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("just a string, not a mapping")
    annotator = AuthorityAnnotator(config_path=bad)
    assert annotator.routing_config() == RoutingConfig()
