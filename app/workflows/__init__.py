"""Multi-step financial workflows run as LangGraph graphs over the existing INDmoney tools.

``select(message)`` picks a workflow for a Portfolio Analyzer account-data request whose wording clearly
matches one (keyword rules, never a model call); everything else stays on the ordinary agent loop.
``FINANCIAL_WORKFLOWS_ENABLED=false`` turns the whole layer off (the agent then behaves as before).
"""

from __future__ import annotations

import os
import re
from typing import Final

from . import nifty
from .base import Registry, Workflow
from .condor import IronCondor
from .health import HealthCheck
from .hedge import PutSpreadHedge
from .scanner import WatchlistScanner

_CONDOR: Final = re.compile(r"iron[\s-]*condor", re.IGNORECASE)
_PUT_SPREAD: Final = re.compile(r"put[\s-]*spread|bear[\s-]*put", re.IGNORECASE)
_HEDGE: Final = re.compile(r"hedg|protect|downside|insur", re.IGNORECASE)
_WATCHLIST: Final = re.compile(r"watch[\s-]*list", re.IGNORECASE)
_SCAN: Final = re.compile(r"\bscan|breakout|support|swing|\d+[\s-]*(?:day|session)s?[\s-]*high", re.IGNORECASE)
_HEALTH: Final = re.compile(
    r"health[\s-]*check|portfolio(?:'s)?\s+health|over[\s-]*concentrat|"
    r"underperform\w*[^.?!]{0,60}benchmark|benchmark[^.?!]{0,60}underperform", re.IGNORECASE)

REGISTRY: Final = Registry([
    (lambda m: bool(_CONDOR.search(m)) and nifty.mentions_nifty(m), IronCondor()),
    (lambda m: bool(_PUT_SPREAD.search(m)) and bool(_HEDGE.search(m)) and nifty.mentions_nifty(m), PutSpreadHedge()),
    (lambda m: bool(_WATCHLIST.search(m)) and bool(_SCAN.search(m)), WatchlistScanner()),
    (lambda m: bool(_HEALTH.search(m)), HealthCheck()),
])


def enabled() -> bool:
    return os.getenv("FINANCIAL_WORKFLOWS_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def select(message: str) -> Workflow | None:
    """The workflow this message asks for, or ``None`` (also when the layer is turned off)."""
    return REGISTRY.select(message) if enabled() else None


__all__ = ["REGISTRY", "Workflow", "enabled", "select"]
