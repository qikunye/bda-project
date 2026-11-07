"""Runs the agent on one alert when an analyst clicks Investigate, and returns the result without
saving it. It has no end-of-day cutoff, so its verdict can differ from the batch's.
"""

import threading

from src import config
from src.agent import sql
from src.agent.baseline import Settings
from src.agent.contracts import Alert, InvestigateResult
from src.agent.data import AgentData, athena_runner
from src.agent.llm import AgentLLM, SpendGuard, create_agent_llm
from src.agent.loop import LoopSettings, run_agent, single_alert_case

_lock = threading.Lock()
_shared: dict = {}


def _shared_resources() -> tuple[AgentData, AgentLLM, SpendGuard]:
    """One Athena cache, model client and spend cap for the whole app process."""

    with _lock:
        if not _shared:
            _shared["data"] = AgentData(athena_runner())
            _shared["llm"] = create_agent_llm()
            _shared["spend"] = SpendGuard()
        return _shared["data"], _shared["llm"], _shared["spend"]


def investigate(
    txn_id: str,
    txn_date: str,
    window_h: int = config.AGENT_WINDOW_H_DEFAULT,
    *,
    data: AgentData | None = None,
    llm: AgentLLM | None = None,
    spend: SpendGuard | None = None,
) -> InvestigateResult:
    """Triage one alert. Takes 20 seconds or more, so show a spinner.

    ``txn_date`` is required. ``window_h`` must be 6-72 or ValueError is raised. An ID or date that
    is malformed or not found gives ``status="not_found"``.
    """

    window_h = sql.validate_window_hours(window_h)
    try:
        sql.validate_transaction_id(txn_id)
        sql.validate_date(txn_date)
    except ValueError:
        return InvestigateResult(
            status="not_found",
            txn_id=str(txn_id),
            txn_date=str(txn_date),
            window_h=window_h,
            alert=Alert(
                found=False, txn_id=str(txn_id), txn_date=str(txn_date), ids=[]
            ),
            decision=None,
            rendered_claims=[],
            graph=None,
            neighbours=None,
            related=None,
            members=[],
            stats=None,
            trace=[],
        )

    if data is None or llm is None:
        shared_data, shared_llm, shared_spend = _shared_resources()
        data, llm = data or shared_data, llm or shared_llm
        spend = spend or shared_spend

    return run_agent(
        data,
        llm,
        single_alert_case(txn_id, txn_date),
        settings=LoopSettings(s1=Settings(window_h=window_h)),
        spend=spend,
    )
