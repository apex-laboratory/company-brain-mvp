from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class AuthorityAnnotation:
    tier: str    # "high" | "medium" | "low"
    weight: float


class AuthorityAnnotator:
    def __init__(self, config_path: str | Path | None = None) -> None:
        if config_path is None:
            from config import settings
            config_path = settings.source_authority_path
        path = Path(config_path)
        self._config: dict = yaml.safe_load(path.read_text())

    # ── public ──────────────────────────────────────────────────────────────

    def annotate(self, source: str, payload: dict) -> AuthorityAnnotation:
        """Return the highest matching authority tier for a source item."""
        tiers: dict = self._config["tiers"]
        for tier_name, tier_cfg in tiers.items():
            entries = [e for e in tier_cfg["sources"] if e["type"] == source]
            if any(self._match_signals(e.get("signals", []), payload) for e in entries):
                return AuthorityAnnotation(tier=tier_name, weight=tier_cfg["weight"])
        return AuthorityAnnotation(tier="low", weight=self._config["tiers"]["low"]["weight"])

    def sweep_processing_order(self) -> list[str]:
        return self._config["sweep"]["processing_order"]

    def sweep_rate_per_minute(self) -> int:
        return self._config["sweep"]["rate_per_minute"]

    def sweep_semaphore_limit(self) -> int:
        return self._config["sweep"]["semaphore_limit"]

    # ── private ─────────────────────────────────────────────────────────────

    def _match_signals(self, signals: list[str], payload: dict) -> bool:
        """Return True if any signal is satisfied by payload (empty list = catch-all)."""
        if not signals:
            return True
        return any(self._check_signal(s, payload) for s in signals)

    def _check_signal(self, signal: str, payload: dict) -> bool:
        if "=" not in signal:
            return bool(payload.get(signal, False))
        key, value = signal.split("=", 1)
        if key == "path_prefix":
            return str(payload.get("path", "")).startswith(value)
        if key == "tag":
            return value in payload.get("tags", [])
        return str(payload.get(key, "")).lower() == value.lower()


# ── module-level singleton ───────────────────────────────────────────────────

_annotator: AuthorityAnnotator | None = None


def get_annotator() -> AuthorityAnnotator:
    global _annotator
    if _annotator is None:
        _annotator = AuthorityAnnotator()
    return _annotator


def annotate(source: str, payload: dict) -> AuthorityAnnotation:
    return get_annotator().annotate(source, payload)


def sweep_processing_order() -> list[str]:
    return get_annotator().sweep_processing_order()


def sweep_rate_per_minute() -> int:
    return get_annotator().sweep_rate_per_minute()


def sweep_semaphore_limit() -> int:
    return get_annotator().sweep_semaphore_limit()
