"""Explicit synchronous access to the existing agent, with an offline fixture runner."""

from collections.abc import Callable

from src.agent import sql
from src.agent.contracts import InvestigateResult


class NoDemoResult(LookupError):
    """There is no unchanged single-alert fixture for this exact request."""


class OnDemandService:
    def __init__(self, runner: Callable | None = None):
        self.runner = runner

    def run(self, txn_id: str, txn_date: str, window_h: int = 48) -> InvestigateResult:
        txn_id = sql.validate_transaction_id(txn_id)
        txn_date = sql.validate_date(txn_date)
        window_h = sql.validate_window_hours(window_h)
        runner = self.runner
        if runner is None:
            # Importing/creating the agent's shared AWS resources occurs only on explicit run().
            from src.agent.investigate import investigate

            runner = investigate
        return runner(txn_id, txn_date, window_h=window_h)


class DemoOnDemandService(OnDemandService):
    def __init__(self):
        super().__init__(self._fixture)

    @staticmethod
    def _fixture(txn_id: str, txn_date: str, *, window_h: int) -> InvestigateResult:
        from src.agent.sample_results import sample_investigations

        for result in sample_investigations().values():
            if (result["txn_id"], result["txn_date"], result["window_h"]) != (txn_id, txn_date, window_h):
                continue
            if len(result["members"]) > 1 or (
                result["status"] == "ok" and result["alert"]["txn_date"] != txn_date
            ):
                continue
            return result
        raise NoDemoResult("No simulated single-alert result is available for this alert, date and window. No agent was called.")
