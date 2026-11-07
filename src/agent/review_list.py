"""Build the agent's review list from decision rows (PLAN.md 7.2, D26).

It reads DecisionRow items, so S1, the LLM agent and DynamoDB all use the same code. Every alert in
the queue stays in the list (rule 3). This only produces a second, reordered list.

Order: raised cases, best representative first, each with its members. Then the remaining alerts by
rank, including not_sure and any alert with no decision. Then dont_raise alerts by rank.
"""

from typing import Literal, TypedDict

from src.agent.contracts import DecisionRow, Verdict


class ReviewItem(TypedDict):
    position: int
    txn_id: str
    # "raised": moved up with its case. "queue": stays in rank order. "not_raised": moved to the bottom.
    source: Literal["raised", "queue", "not_raised"]
    verdict: Verdict | None
    representative_txn_id: str | None
    bq1_position: int  # Position in the BQ1 order.


def build_review_list(
    queue_txn_ids: list[str], rows: list[DecisionRow]
) -> list[ReviewItem]:
    """``queue_txn_ids`` is the day's queue in BQ1 order. Rows outside it are ignored."""

    queue = list(dict.fromkeys(queue_txn_ids))
    bq1_position = {txn_id: i + 1 for i, txn_id in enumerate(queue)}
    by_txn = {
        row["transaction_id"]: row
        for row in rows
        if row["transaction_id"] in bq1_position
    }

    def source_of(txn_id: str) -> str:
        verdict = by_txn[txn_id]["verdict"] if txn_id in by_txn else None
        return {"raise": "raised", "dont_raise": "not_raised"}.get(verdict, "queue")

    def case_rank(txn_id: str) -> int:
        """Members of a raised case sort by their representative's rank, so the case stays together."""

        representative = by_txn[txn_id]["representative_txn_id"]
        return bq1_position.get(representative, bq1_position[txn_id])

    raised = sorted(
        (t for t in queue if source_of(t) == "raised"),
        key=lambda t: (case_rank(t), bq1_position[t]),
    )
    rest = [t for t in queue if source_of(t) == "queue"]
    not_raised = [t for t in queue if source_of(t) == "not_raised"]

    return [
        ReviewItem(
            position=position,
            txn_id=txn_id,
            source=source_of(txn_id),
            verdict=by_txn[txn_id]["verdict"] if txn_id in by_txn else None,
            representative_txn_id=by_txn[txn_id]["representative_txn_id"]
            if txn_id in by_txn
            else None,
            bq1_position=bq1_position[txn_id],
        )
        for position, txn_id in enumerate(raised + rest + not_raised, start=1)
    ]
