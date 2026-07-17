"""Sweep processing order — stable import path for the sweep jobs.

The actual parse of ``source_authority.yaml`` (and its fail-soft fallback to the
built-in default order) lives in :mod:`app.pipeline.authority`, so the file is read
by exactly one loader with one fallback policy. This module just re-exports it for
``onboarding_sweep`` / ``sweep_extract`` (which patch ``processing_order`` by name).
"""
from __future__ import annotations

from app.pipeline.authority import processing_order

__all__ = ["processing_order"]
