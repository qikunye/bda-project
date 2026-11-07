"""Groups a day's top alerts into cases, linking alerts that share an account within a time window.
No LLM is involved, so the grouping is the same on every run.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from src import config
from src.agent.claims import case_id_for


@dataclass(frozen=True)
class QueueAlert:
    txn_id: str
    sender: str
    receiver: str
    ts: str
    rank_day: int


@dataclass(frozen=True)
class Case:
    case_id: str
    txn_date: str
    representative_txn_id: str
    member_txn_ids: tuple[
        str, ...
    ]  # Best rank first, starting with the representative.


def queue_alerts_from_frame(frame) -> list[QueueAlert]:
    """Convert a load_day_queue result."""

    return [
        QueueAlert(
            txn_id=row["transaction_id"],
            sender=row["source_account_key"],
            receiver=row["target_account_key"],
            ts=str(row["transaction_timestamp"])[:19],
            rank_day=int(row["risk_rank_day"]),
        )
        for row in frame.to_dict("records")
    ]


def group_into_cases(
    alerts: list[QueueAlert],
    txn_date: str,
    *,
    window_h: int = config.AGENT_WINDOW_H_DEFAULT,
    max_size: int = config.AGENT_MAX_CASE_SIZE,
) -> list[Case]:
    if max_size < 1:
        raise ValueError(f"max_size must be at least 1, got {max_size!r}")

    ordered = sorted(alerts, key=lambda a: (a.rank_day, a.txn_id))
    window = timedelta(hours=window_h)
    times = [datetime.fromisoformat(a.ts) for a in ordered]

    # Each set's root is its best-ranked alert, because the lower index always wins.
    parent = list(range(len(ordered)))
    size = [1] * len(ordered)

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    by_account: dict[str, list[int]] = {}
    for index, alert in enumerate(ordered):
        linked: set[int] = set()
        for account in {alert.sender, alert.receiver}:
            linked.update(
                earlier
                for earlier in by_account.get(account, ())
                if abs(times[index] - times[earlier]) <= window
            )
            by_account.setdefault(account, []).append(index)

        for earlier in sorted(linked):
            a, b = find(index), find(earlier)
            if a == b or size[a] + size[b] > max_size:
                continue
            root, child = min(a, b), max(a, b)
            parent[child] = root
            size[root] += size[child]

    members: dict[int, list[str]] = {}
    for index, alert in enumerate(ordered):
        members.setdefault(find(index), []).append(alert.txn_id)

    return [
        Case(
            case_id=case_id_for(ordered[root].txn_id),
            txn_date=txn_date,
            representative_txn_id=ordered[root].txn_id,
            member_txn_ids=tuple(members[root]),
        )
        for root in sorted(members)
    ]
