"""Lightweight contracts between the external analytics and serving layers.

The Silver transaction table, BQ1 Gold score table, and BQ2 Gold edge table
are external upstream data sources. BQ1 determines where an analyst starts;
BQ2 consumes the selected alert handoff to expand the case. A future AI layer
will consume these deterministic outputs rather than replace their analytics.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AlertHandoff:
    """Metadata passed from a selected BQ1 alert into BQ2 investigation."""

    transaction_id: str
    source_account_key: str
    target_account_key: str
    transaction_timestamp: datetime
