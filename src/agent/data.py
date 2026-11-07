"""Runs the agent's Athena queries, with a label check and a log of data scanned. Results are
cached, so repeating a query costs nothing.
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from src import config
from src.agent.sql import LabelLeakError, assert_label_free
from src.aws.athena import (
    AthenaQueryError,
    QueryStats,
    create_athena_client,
    execute_query_with_stats,
)

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
    # The batch runs several cases at once, and they share this cache and log.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def query(self, sql: str, label: str) -> pd.DataFrame:
        assert_label_free(sql)
        with self._lock:
            if sql in self._cache:
                self._cache.move_to_end(sql)
                self.scan_log.append(ScanLogEntry(label, 0, 0, cached=True))
                return self._cache[sql].copy()

        # The query runs outside the lock. Two cases asking for the same SQL at once both run it.
        frame, stats = self.runner(sql)
        leaked = {c.lower() for c in frame.columns} & config.AGENT_FORBIDDEN_COLUMNS
        if leaked:
            raise LabelLeakError(f"Result contains forbidden columns: {sorted(leaked)}")

        with self._lock:
            self.scan_log.append(
                ScanLogEntry(label, stats.bytes_scanned, stats.engine_ms, cached=False)
            )
            self._cache[sql] = frame
            if len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return frame.copy()

    @property
    def bytes_scanned(self) -> int:
        with self._lock:
            return sum(entry.bytes_scanned for entry in self.scan_log)


RETRIES = 3  # Attempts for a query that Athena says failed with an internal error.
RETRY_WAIT_S = 3.0


def athena_runner(client=None, *, sleep: Callable[[float], None] = time.sleep) -> Runner:
    """A runner backed by real Athena, writing results under the agent's prefix.

    Athena sometimes fails a query with "an internal error" and asks you to try again. One such
    failure would stop a whole day's save, so those are retried. Any other failure is raised.
    """

    client = client or create_athena_client(config.AWS_REGION)

    def run(sql: str) -> tuple[pd.DataFrame, QueryStats]:
        for attempt in range(1, RETRIES + 1):
            try:
                return execute_query_with_stats(
                    client,
                    query=sql,
                    database=config.ATHENA_DATABASE,
                    output_location=config.AGENT_ATHENA_OUTPUT,
                )
            except AthenaQueryError as error:
                if "internal error" not in str(error).lower() or attempt == RETRIES:
                    raise
                sleep(RETRY_WAIT_S * attempt)
        raise AssertionError("unreachable")

    return run
