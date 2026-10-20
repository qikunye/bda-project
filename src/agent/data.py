"""Athena access for the agent: one runner with a result cache, a scan log and a label check.

Tools call ``AgentData.query``. Repeated statements are served from the cache, so reruns and
repeated tool calls do not re-bill Athena (PLAN.md 5.2).
"""

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from src import config
from src.agent.sql import LabelLeakError, assert_label_free
from src.aws.athena import QueryStats, create_athena_client, execute_query_with_stats

Runner = Callable[[str], tuple[pd.DataFrame, QueryStats]]


@dataclass(frozen=True)
class ScanLogEntry:
    label: str
    bytes_scanned: int
    engine_ms: int
    cached: bool


@dataclass
class AgentData:
    """Runs agent SQL through ``runner`` and caches results by statement text."""

    runner: Runner
    cache_size: int = 512
    scan_log: list[ScanLogEntry] = field(default_factory=list)
    _cache: OrderedDict = field(default_factory=OrderedDict, repr=False)

    def query(self, sql: str, label: str) -> pd.DataFrame:
        assert_label_free(sql)
        if sql in self._cache:
            self._cache.move_to_end(sql)
            self.scan_log.append(ScanLogEntry(label, 0, 0, cached=True))
            return self._cache[sql].copy()

        frame, stats = self.runner(sql)
        leaked = {c.lower() for c in frame.columns} & config.AGENT_FORBIDDEN_COLUMNS
        if leaked:
            raise LabelLeakError(f"Result contains forbidden columns: {sorted(leaked)}")

        self.scan_log.append(
            ScanLogEntry(label, stats.bytes_scanned, stats.engine_ms, cached=False)
        )
        self._cache[sql] = frame
        if len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return frame.copy()

    @property
    def bytes_scanned(self) -> int:
        return sum(entry.bytes_scanned for entry in self.scan_log)


def athena_runner(client=None) -> Runner:
    """A runner backed by real Athena, writing results under the agent's prefix."""

    client = client or create_athena_client(config.AWS_REGION)

    def run(sql: str) -> tuple[pd.DataFrame, QueryStats]:
        return execute_query_with_stats(
            client,
            query=sql,
            database=config.ATHENA_DATABASE,
            output_location=config.AGENT_ATHENA_OUTPUT,
        )

    return run
