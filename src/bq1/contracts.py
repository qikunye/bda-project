"""Structured BQ1 metadata contracts for downstream investigation context."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

RISK_SCORE_SEMANTICS = "ranking_signal_not_calibrated_probability"
RISK_RANK_SCOPE = "global_all_scored_rows"


class BQ1MetadataStatus(str, Enum):
    """Availability state for optional BQ1 metadata."""

    AVAILABLE = "available"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class BQ1RiskMetadata:
    """Analyst-safe BQ1 risk metadata for one selected transaction.

    ``risk_score`` is a ranking signal, not a calibrated probability.
    ``risk_rank`` is the documented global rank across all scored rows.
    Evaluation labels and data-split fields are deliberately excluded.
    """

    transaction_id: str
    source_account_key: str
    target_account_key: str
    transaction_timestamp: datetime
    risk_score: float | None
    risk_rank: int | None
    model_name: str | None
    model_version: str | None
    source_table: str
    risk_score_semantics: str = field(
        default=RISK_SCORE_SEMANTICS,
        init=False,
    )
    risk_rank_scope: str = field(
        default=RISK_RANK_SCOPE,
        init=False,
    )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""

        return {
            "transaction_id": self.transaction_id,
            "source_account_key": self.source_account_key,
            "target_account_key": self.target_account_key,
            "transaction_timestamp": self.transaction_timestamp.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "risk_score": self.risk_score,
            "risk_rank": self.risk_rank,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "source_table": self.source_table,
            "risk_score_semantics": self.risk_score_semantics,
            "risk_rank_scope": self.risk_rank_scope,
        }


@dataclass(frozen=True)
class BQ1MetadataResult:
    """Optional BQ1 metadata with an explicit availability state."""

    status: BQ1MetadataStatus
    metadata: BQ1RiskMetadata | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.status is BQ1MetadataStatus.AVAILABLE and self.metadata is None:
            raise ValueError("Available BQ1 result requires metadata.")
        if self.status is not BQ1MetadataStatus.AVAILABLE and self.metadata is not None:
            raise ValueError("Non-available BQ1 result cannot contain metadata.")

    @classmethod
    def available(cls, metadata: BQ1RiskMetadata) -> "BQ1MetadataResult":
        return cls(
            status=BQ1MetadataStatus.AVAILABLE,
            metadata=metadata,
        )

    @classmethod
    def not_found(cls, detail: str | None = None) -> "BQ1MetadataResult":
        return cls(
            status=BQ1MetadataStatus.NOT_FOUND,
            detail=detail,
        )

    @classmethod
    def unavailable(cls, detail: str | None = None) -> "BQ1MetadataResult":
        return cls(
            status=BQ1MetadataStatus.UNAVAILABLE,
            detail=detail,
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable service result."""

        return {
            "status": self.status.value,
            "metadata": self.metadata.to_dict() if self.metadata else None,
            "detail": self.detail,
        }
