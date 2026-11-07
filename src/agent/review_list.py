"""Attaches each alert's brief to the day's alert list, keeping the list in its original order.
No alert is ever reordered or removed."""

from typing import TypedDict

from src.agent.contracts import DecisionRow, FindingCode, GapCode, RowRole


class ReviewItem(TypedDict):
    position: int  # Same as the BQ1 position: the agent does not reorder.
    txn_id: str
    # None when the agent has not run on this alert.
    role: RowRole | None
    case_id: str | None
    representative_txn_id: str | None
    # The brief lives on the representative's row. A member has the case's codes, copied from it.
    finding_codes: list[FindingCode]
    gap_codes: list[GapCode]
    has_brief: bool


def build_review_list(
    queue_txn_ids: list[str], rows: list[DecisionRow]
) -> list[ReviewItem]:
    """``queue_txn_ids`` is the day's queue in BQ1 order. Rows outside it are ignored."""

    queue = list(dict.fromkeys(queue_txn_ids))
    by_txn = {row["transaction_id"]: row for row in rows}
    items: list[ReviewItem] = []
    for position, txn_id in enumerate(queue, start=1):
        row = by_txn.get(txn_id)
        if row is None:
            items.append(
                ReviewItem(
                    position=position,
                    txn_id=txn_id,
                    role=None,
                    case_id=None,
                    representative_txn_id=None,
                    finding_codes=[],
                    gap_codes=[],
                    has_brief=False,
                )
            )
            continue
        brief_row = by_txn.get(row["representative_txn_id"], row)
        items.append(
            ReviewItem(
                position=position,
                txn_id=txn_id,
                role=row["role"],
                case_id=row["case_id"],
                representative_txn_id=row["representative_txn_id"],
                finding_codes=list(brief_row.get("finding_codes", [])),
                gap_codes=list(brief_row.get("gap_codes", [])),
                has_brief="finding_codes" in brief_row,
            )
        )
    return items
