"""Orchestrate retrieved BQ1/BQ2 data into a validated CaseContext."""

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.bq1.contracts import BQ1MetadataResult
from src.bq1.service import (
    build_bq1_metadata_from_row,
    lookup_bq1_metadata,
    unavailable_bq1_metadata,
)
from src.bq2.queries import build_case_edges_query
from src.config import BQ1_SCORE_TABLE, BQ2_EDGE_TABLE
from src.context.builder import build_case_context
from src.context.validation import validate_case_context
from src.shared.contracts import AlertHandoff

QueryExecutor = Callable[..., pd.DataFrame]


class CaseContextServiceError(ValueError):
    """Raised when CaseContext retrieval inputs are missing or contradictory."""


def _validate_queried_window_hours(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CaseContextServiceError(
            "queried_window_hours must be a positive integer."
        )
    return value


def _format_handoff_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("handoff.transaction_timestamp must be a datetime.")
    if value.tzinfo is None:
        normalized = value.replace(tzinfo=timezone.utc)
    else:
        normalized = value.astimezone(timezone.utc)
    if normalized.second != 0 or normalized.microsecond != 0:
        raise CaseContextServiceError(
            "Alert timestamps must follow the source's minute-resolution convention."
        )
    return normalized.strftime("%Y-%m-%d %H:%M:%S")


def _require_query_executor(
    query_executor: QueryExecutor | None,
) -> QueryExecutor:
    if query_executor is None or not callable(query_executor):
        raise CaseContextServiceError(
            "A callable query_executor is required for requested retrieval."
        )
    return query_executor


def _validate_bq1_inputs(
    *,
    bq1_result: BQ1MetadataResult | None,
    bq1_row: Mapping[str, Any] | pd.Series | None,
    fetch_bq1: bool,
) -> None:
    supplied_sources = sum(
        (
            bq1_result is not None,
            bq1_row is not None,
            fetch_bq1,
        )
    )
    if supplied_sources > 1:
        raise CaseContextServiceError(
            "Provide only one BQ1 source: bq1_result, bq1_row, or fetch_bq1=True."
        )
    if bq1_result is not None and not isinstance(
        bq1_result,
        BQ1MetadataResult,
    ):
        raise TypeError("bq1_result must be a BQ1MetadataResult.")
    if bq1_row is not None and not isinstance(bq1_row, (Mapping, pd.Series)):
        raise TypeError("bq1_row must be a mapping or Pandas Series.")


def _resolve_bq1_result(
    *,
    handoff: AlertHandoff,
    query_executor: QueryExecutor | None,
    bq1_result: BQ1MetadataResult | None,
    bq1_row: Mapping[str, Any] | pd.Series | None,
    fetch_bq1: bool,
    bq1_table: str,
) -> BQ1MetadataResult:
    if bq1_result is not None:
        return bq1_result
    if bq1_row is not None:
        metadata = build_bq1_metadata_from_row(
            bq1_row,
            handoff=handoff,
            source_table=bq1_table,
        )
        return BQ1MetadataResult.available(metadata)
    if fetch_bq1:
        return lookup_bq1_metadata(
            handoff,
            query_executor=_require_query_executor(query_executor),
            table_name=bq1_table,
        )
    return unavailable_bq1_metadata("BQ1 lookup was not requested.")


def load_case_context(
    *,
    handoff: AlertHandoff,
    query_executor: QueryExecutor | None = None,
    raw_edges: pd.DataFrame | None = None,
    bq1_result: BQ1MetadataResult | None = None,
    bq1_row: Mapping[str, Any] | pd.Series | None = None,
    fetch_bq1: bool = False,
    queried_window_hours: int = 48,
    bq1_table: str = BQ1_SCORE_TABLE,
    bq2_table: str = BQ2_EDGE_TABLE,
) -> dict[str, Any]:
    """Load or reuse case evidence and return one complete CaseContext.

    Supplied ``raw_edges`` avoids a BQ2 query. For BQ1, exactly one of an
    existing result, an existing row, or explicit lookup may be selected. If
    none is selected, BQ1 is represented as unavailable without a query.

    ``query_executor`` is dependency-injected and must accept ``query=...``.
    The service never creates an AWS client and never mutates input frames.
    """

    if not isinstance(handoff, AlertHandoff):
        raise TypeError("handoff must be an AlertHandoff.")
    hours = _validate_queried_window_hours(queried_window_hours)
    if not isinstance(fetch_bq1, bool):
        raise TypeError("fetch_bq1 must be a boolean.")
    _validate_bq1_inputs(
        bq1_result=bq1_result,
        bq1_row=bq1_row,
        fetch_bq1=fetch_bq1,
    )

    if raw_edges is None:
        executor = _require_query_executor(query_executor)
        query = build_case_edges_query(
            handoff.source_account_key,
            handoff.target_account_key,
            _format_handoff_timestamp(handoff.transaction_timestamp),
            handoff.transaction_id,
            hours * 60,
            table_name=bq2_table,
        )
        case_edges = executor(query=query)
        if not isinstance(case_edges, pd.DataFrame):
            raise CaseContextServiceError(
                "BQ2 query executor must return a Pandas DataFrame."
            )
    else:
        if not isinstance(raw_edges, pd.DataFrame):
            raise TypeError("raw_edges must be a Pandas DataFrame.")
        case_edges = raw_edges

    resolved_bq1 = _resolve_bq1_result(
        handoff=handoff,
        query_executor=query_executor,
        bq1_result=bq1_result,
        bq1_row=bq1_row,
        fetch_bq1=fetch_bq1,
        bq1_table=bq1_table,
    )
    context = build_case_context(
        handoff=handoff,
        raw_edges=case_edges,
        bq1_result=resolved_bq1,
        queried_window_hours=hours,
    )
    validate_case_context(context)
    return context
