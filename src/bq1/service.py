"""AWS-independent service functions for optional BQ1 risk metadata."""

import math
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.bq1.contracts import BQ1MetadataResult, BQ1RiskMetadata
from src.bq1.queries import build_bq1_score_lookup_query
from src.config import BQ1_SCORE_TABLE
from src.shared.contracts import AlertHandoff

_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
)


class BQ1MetadataError(RuntimeError):
    """Base error for BQ1 metadata integration failures."""


class BQ1MetadataIntegrityError(BQ1MetadataError):
    """Raised when BQ1 data is malformed, duplicated or inconsistent."""


class BQ1MetadataQueryError(BQ1MetadataError):
    """Raised when the injected BQ1 query executor fails."""


def _row_to_mapping(row: Mapping[str, Any] | pd.Series) -> Mapping[str, Any]:
    if isinstance(row, pd.Series):
        return row.to_dict()
    if isinstance(row, Mapping):
        return row
    raise TypeError("BQ1 metadata row must be a mapping or Pandas Series.")


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()

    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _required_string(values: Mapping[str, Any], field_name: str) -> str:
    value = values.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise BQ1MetadataIntegrityError(
            f"BQ1 metadata has an invalid {field_name}."
        )
    return value.strip()


def _optional_string(values: Mapping[str, Any], field_name: str) -> str | None:
    value = values.get(field_name)
    if _is_missing(value):
        return None
    if not isinstance(value, str):
        raise BQ1MetadataIntegrityError(
            f"BQ1 metadata has an invalid {field_name}."
        )
    return value.strip()


def _optional_float(values: Mapping[str, Any], field_name: str) -> float | None:
    value = values.get(field_name)
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        raise BQ1MetadataIntegrityError(
            f"BQ1 metadata has an invalid {field_name}."
        )

    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise BQ1MetadataIntegrityError(
            f"BQ1 metadata has an invalid {field_name}."
        ) from error

    if not math.isfinite(parsed):
        raise BQ1MetadataIntegrityError(
            f"BQ1 metadata has an invalid {field_name}."
        )
    return parsed


def _optional_positive_int(
    values: Mapping[str, Any],
    field_name: str,
) -> int | None:
    value = values.get(field_name)
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        raise BQ1MetadataIntegrityError(
            f"BQ1 metadata has an invalid {field_name}."
        )

    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as error:
        raise BQ1MetadataIntegrityError(
            f"BQ1 metadata has an invalid {field_name}."
        ) from error

    if (
        not math.isfinite(numeric_value)
        or not numeric_value.is_integer()
        or numeric_value <= 0
    ):
        raise BQ1MetadataIntegrityError(
            f"BQ1 metadata has an invalid {field_name}."
        )
    return int(numeric_value)


def _normalize_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        stripped = value.strip()
        parsed = None
        for timestamp_format in _TIMESTAMP_FORMATS:
            try:
                parsed = datetime.strptime(stripped, timestamp_format).replace(
                    tzinfo=timezone.utc
                )
                break
            except ValueError:
                continue
        if parsed is None:
            raise BQ1MetadataIntegrityError(
                "BQ1 metadata has an invalid transaction_timestamp."
            )
    else:
        raise BQ1MetadataIntegrityError(
            "BQ1 metadata has an invalid transaction_timestamp."
        )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)

    if parsed.microsecond != 0:
        raise BQ1MetadataIntegrityError(
            "BQ1 transaction_timestamp must have whole-second precision."
        )
    return parsed


def validate_metadata_matches_handoff(
    metadata: BQ1RiskMetadata,
    handoff: AlertHandoff,
) -> None:
    """Reject BQ1 metadata that does not describe the selected alert."""

    expected_timestamp = _normalize_timestamp(handoff.transaction_timestamp)
    checks = (
        ("transaction_id", metadata.transaction_id, handoff.transaction_id),
        (
            "source_account_key",
            metadata.source_account_key,
            handoff.source_account_key,
        ),
        (
            "target_account_key",
            metadata.target_account_key,
            handoff.target_account_key,
        ),
        (
            "transaction_timestamp",
            metadata.transaction_timestamp,
            expected_timestamp,
        ),
    )

    for field_name, actual, expected in checks:
        if actual != expected:
            raise BQ1MetadataIntegrityError(
                f"BQ1 metadata {field_name} does not match the selected alert."
            )


def build_bq1_metadata_from_row(
    row: Mapping[str, Any] | pd.Series,
    *,
    handoff: AlertHandoff | None = None,
    source_table: str = BQ1_SCORE_TABLE,
) -> BQ1RiskMetadata:
    """Parse an existing BQ1 alert row without performing an Athena lookup."""

    values = _row_to_mapping(row)
    metadata = BQ1RiskMetadata(
        transaction_id=_required_string(values, "transaction_id"),
        source_account_key=_required_string(values, "source_account_key"),
        target_account_key=_required_string(values, "target_account_key"),
        transaction_timestamp=_normalize_timestamp(
            values.get("transaction_timestamp")
        ),
        risk_score=_optional_float(values, "risk_score"),
        risk_rank=_optional_positive_int(values, "risk_rank"),
        model_name=_optional_string(values, "model_name"),
        model_version=_optional_string(values, "model_version"),
        source_table=source_table,
    )

    if handoff is not None:
        validate_metadata_matches_handoff(metadata, handoff)

    return metadata


def lookup_bq1_metadata(
    handoff: AlertHandoff,
    *,
    query_executor: Callable[..., pd.DataFrame],
    table_name: str = BQ1_SCORE_TABLE,
) -> BQ1MetadataResult:
    """Look up and validate optional BQ1 metadata for a four-field handoff."""

    handoff_timestamp = _normalize_timestamp(handoff.transaction_timestamp)
    query = build_bq1_score_lookup_query(
        handoff.transaction_id,
        handoff_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        table_name=table_name,
    )

    try:
        result = query_executor(query=query)
    except Exception as error:
        raise BQ1MetadataQueryError("BQ1 metadata query failed.") from error

    if not isinstance(result, pd.DataFrame):
        raise BQ1MetadataIntegrityError(
            "BQ1 metadata query executor must return a Pandas DataFrame."
        )
    if result.empty:
        return BQ1MetadataResult.not_found(
            "No BQ1 metadata row matched the selected alert."
        )
    if len(result.index) != 1:
        raise BQ1MetadataIntegrityError(
            "BQ1 metadata lookup returned multiple matching rows."
        )

    metadata = build_bq1_metadata_from_row(
        result.iloc[0],
        handoff=handoff,
        source_table=table_name,
    )
    return BQ1MetadataResult.available(metadata)


def unavailable_bq1_metadata(
    detail: str | None = None,
) -> BQ1MetadataResult:
    """Represent an investigation for which no BQ1 lookup was attempted."""

    return BQ1MetadataResult.unavailable(detail)
