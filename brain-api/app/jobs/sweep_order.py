"""Sweep processing order from ``source_authority.yaml`` (KAN-2).

The onboarding sweep processes sources in authority-priority order (high-authority
sources first) so the review queue fills with the most trustworthy skills first.
The order lives in ``source_authority.yaml`` (``sweep.processing_order``); a missing
or malformed file falls back to the built-in default so the sweep never blocks on
config. Providers the file doesn't know about (e.g. gmail) are appended at the end
rather than skipped.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from app.config.settings import settings

log = logging.getLogger(__name__)

_DEFAULT_ORDER = ["notion", "google_drive", "gmail", "github", "jira", "slack", "zendesk"]


def processing_order() -> list[str]:
    """Provider names in sweep priority order (always includes every known provider)."""
    try:
        config = yaml.safe_load(Path(settings.source_authority_path).read_text())
        order = [str(p) for p in config["sweep"]["processing_order"]]
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        log.warning(
            "source_authority.yaml missing/malformed at %s — using default sweep order",
            settings.source_authority_path,
        )
        return list(_DEFAULT_ORDER)
    return order + [p for p in _DEFAULT_ORDER if p not in order]
