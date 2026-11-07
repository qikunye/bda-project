"""Reads saved S1 (run_s1) and S2 (run_batch / run_agent_sample) outputs into one shape."""

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    member_txn_ids: tuple[str, ...]
    finding_codes: frozenset[str]
    gap_codes: frozenset[str] = frozenset()
    next_check_codes: frozenset[str] = frozenset()
    observations: tuple[dict, ...] = ()
    summary: str = ""
    validation: str | None = None  # valid / repaired / fallback; None for S1.
    observations_dropped: int = 0
    stats: dict = field(default_factory=dict)


def _from_case(raw: dict) -> CaseOutcome:
    result = raw.get("result", raw)  # S2 nests the brief under "result"; S1 does not.
    stats = result.get("stats") or {}
    members = raw.get("member_txn_ids") or [m["alert"]["txn_id"] for m in result.get("members", [])]
    return CaseOutcome(
        case_id=raw.get("case_id") or result.get("case_id"),
        member_txn_ids=tuple(members),
        finding_codes=frozenset(f["code"] for f in result.get("findings", [])),
        gap_codes=frozenset(g["code"] for g in result.get("gaps", [])),
        next_check_codes=frozenset(c["code"] for c in result.get("next_checks", [])),
        observations=tuple(result.get("observations", [])),
        summary=result.get("summary", ""),
        validation=stats.get("validation"),
        observations_dropped=stats.get("observations_dropped", 0),
        stats=stats,
    )


def load_run(path: str | Path) -> list[CaseOutcome]:
    data = json.loads(Path(path).read_text())
    # run_agent_sample and run_s1 write "cases"; run_batch --out writes "results".
    return [_from_case(c) for c in data.get("cases") or data["results"]]
