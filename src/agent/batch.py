"""Runs the agent on a day's top alerts, grouped into cases, and saves the results. A rerun replaces
that day's saved rows instead of adding to them.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from src import config
from src.agent import baseline, tools
from src.agent.cases import Case, group_into_cases, queue_alerts_from_frame
from src.agent.contracts import CaseRow, DecisionRow, InvestigateResult
from src.agent.data import AgentData
from src.agent.decisions import build_case_row, build_decision_rows, trace_key
from src.agent.llm import AgentLLM, SpendGuard
from src.agent.loop import LoopSettings, run_agent
from src.agent.sql import LabelLeakError


class BatchIncomplete(RuntimeError):
    """Some cases failed, so nothing was saved. Saving half a day would mix old and new rows."""


@dataclass
class BatchOutput:
    day: str
    run_id: str
    created_at: str
    runs: list[tuple[Case, InvestigateResult]]  # In case order, best rank first.
    failures: list[dict]
    decision_rows: list[DecisionRow]
    case_rows: list[CaseRow]

    @property
    def trace_keys(self) -> set[str]:
        return {row["trace_key"] for row in self.decision_rows if "trace_key" in row}


@dataclass(frozen=True)
class PublishReport:
    decisions_written: int
    cases_written: int
    traces_written: int
    decisions_deleted: int
    cases_deleted: int
    traces_deleted: int


def default_run_id(day: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"batch-{day}-{now:%Y%m%dT%H%M%SZ}"


def run_day(
    data: AgentData,
    llm: AgentLLM,
    day: str,
    *,
    top_n: int = config.AGENT_BATCH_TOP_N,
    concurrency: int = config.AGENT_BATCH_CONCURRENCY,
    settings: LoopSettings | None = None,
    spend: SpendGuard | None = None,
    run_id: str | None = None,
    created_at: str | None = None,
    on_case_done=None,
) -> BatchOutput:
    """Group the day's top alerts into cases and run the agent on each, ``concurrency`` at a time.

    A case that raises goes to ``failures`` and the others finish. A label leak stops everything.
    The tools see data only up to the end of ``day``, because that is all a daily run can know.
    """

    settings = settings or LoopSettings()
    settings = replace(
        settings, s1=replace(settings.s1, as_of=baseline.end_of_day(day))
    )
    now = datetime.now(timezone.utc)
    run_id = run_id or default_run_id(day, now)
    created_at = created_at or now.isoformat(timespec="seconds")

    frame = tools.load_day_queue(data, day, top_n)
    cases = group_into_cases(
        queue_alerts_from_frame(frame), day, window_h=settings.s1.window_h
    )

    def work(case: Case) -> InvestigateResult:
        return run_agent(data, llm, case, settings=settings, spend=spend)

    outcomes: list[InvestigateResult | Exception | None] = [None] * len(cases)
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(work, case) for case in cases]
        try:
            for index, future in enumerate(futures):
                try:
                    outcomes[index] = future.result()
                except LabelLeakError:
                    raise  # Rule 2: stop the whole batch.
                except Exception as error:  # noqa: BLE001 - recorded, then the rest go on.
                    outcomes[index] = error
                if on_case_done:
                    on_case_done(index + 1, len(cases), cases[index], outcomes[index])
        except LabelLeakError:
            for future in futures:
                future.cancel()
            raise

    runs: list[tuple[Case, InvestigateResult]] = []
    failures: list[dict] = []
    for case, outcome in zip(cases, outcomes):
        if isinstance(outcome, Exception):
            failures.append(
                {
                    "case_id": case.case_id,
                    "error": f"{type(outcome).__name__}: {outcome}",
                }
            )
        elif outcome is None or outcome["status"] != "ok":
            failures.append(
                {"case_id": case.case_id, "error": "A queue alert was not found."}
            )
        else:
            runs.append((case, outcome))

    # Each alert is in exactly one case, so every transaction gets one row.
    decision_rows = [
        row
        for case, result in runs
        for row in build_decision_rows(
            result,
            run_id=run_id,
            created_at=created_at,
            member_txn_ids=case.member_txn_ids,
        )
    ]
    return BatchOutput(
        day=day,
        run_id=run_id,
        created_at=created_at,
        runs=runs,
        failures=failures,
        decision_rows=decision_rows,
        case_rows=[
            build_case_row(case, result, run_id=run_id, created_at=created_at)
            for case, result in runs
        ],
    )


def publish(output: BatchOutput, dynamo, traces) -> PublishReport:
    """Save a finished batch, then remove that day's rows and traces it did not rewrite.

    New rows go in first, so a reader never sees an empty day. Raises ``BatchIncomplete`` before
    writing anything if any case failed.
    """

    if output.failures:
        raise BatchIncomplete(
            f"{len(output.failures)} case(s) failed, so nothing was saved: "
            f"{[f['case_id'] for f in output.failures][:5]}"
        )

    trace_by_key = {
        trace_key(case.txn_date, case.representative_txn_id): {
            "run_id": output.run_id,
            "created_at": output.created_at,
            "result": result,
        }
        for case, result in output.runs
    }
    for key, body in trace_by_key.items():
        traces.put(key, body)
    dynamo.put_decisions(output.decision_rows)
    dynamo.put_cases(output.case_rows)

    new_decisions = {row["transaction_id"] for row in output.decision_rows}
    new_cases = {row["case_id"] for row in output.case_rows}
    stale_decisions = dynamo.decision_keys_for_day(output.day) - new_decisions
    stale_cases = dynamo.case_ids_for_day(output.day) - new_cases
    stale_traces = traces.keys(f"agent/traces/{output.day}/") - set(trace_by_key)
    if stale_decisions:
        dynamo.delete_decisions(stale_decisions)
    if stale_cases:
        dynamo.delete_cases(output.day, stale_cases)
    if stale_traces:
        traces.delete(stale_traces)

    return PublishReport(
        decisions_written=len(output.decision_rows),
        cases_written=len(output.case_rows),
        traces_written=len(trace_by_key),
        decisions_deleted=len(stale_decisions),
        cases_deleted=len(stale_cases),
        traces_deleted=len(stale_traces),
    )
