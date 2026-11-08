"""Rebuilds the three agent tables for the configured days, and fails if an earlier day now ranks differently.
Dry run unless you pass --yes, because it deletes the old tables and their S3 data."""

import argparse
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import boto3

from src import config
from src.aws.athena import (
    AthenaQueryError,
    create_athena_client,
    execute_query_with_stats,
)

SQL_DIR = Path(__file__).resolve().parent.parent / "sql" / "agent"
# In build order: the day queue is built from the scores table.
TABLES = (
    ("agent_scores_v2", "04_agent_scores_v2.sql"),
    ("agent_edges_v2", "05_agent_edges_v2.sql"),
    ("agent_day_queue_v2", "06_agent_day_queue_v2.sql"),
)
_RANGE = re.compile(r"BETWEEN DATE '\d{4}-\d{2}-\d{2}' AND DATE '\d{4}-\d{2}-\d{2}'")
FINGERPRINT_SQL = """
SELECT CAST(txn_date AS varchar) AS txn_date, count(*) AS n,
       to_hex(checksum(transaction_id || ':' || coalesce(CAST(risk_rank_day AS varchar), '-'))) AS fp
FROM is459_aml_gold.agent_scores_v2
GROUP BY txn_date
ORDER BY txn_date
"""


def date_ranges() -> dict[str, tuple[str, str]]:
    """Scores cover the scored days. Edges start earlier, so a 72 h look-back finds its payments."""

    first, last = min(config.AGENT_SCORED_DATES), max(config.AGENT_SCORED_DATES)
    lookback = timedelta(hours=config.AGENT_WINDOW_H_MAX)
    return {
        "scores": (first, last),
        "edges": ((date.fromisoformat(first) - lookback).isoformat(), last),
    }


def _executable(text: str) -> str:
    """The statement without whole-line comments (they mention DROP and rm) or the final semicolon."""

    lines = [line for line in text.splitlines() if not line.lstrip().startswith("--")]
    return "\n".join(lines).strip().rstrip(";")


def render_sql(text: str, start: str, end: str) -> str:
    """Put the config dates into a CTAS file and make it ready for Athena."""

    if not _RANGE.search(text):
        raise ValueError("No date range found in the SQL to replace.")
    return _executable(_RANGE.sub(f"BETWEEN DATE '{start}' AND DATE '{end}'", text))


def table_prefix(name: str) -> str:
    """The S3 prefix of one agent table. Refuses anything outside agent/tables/."""

    if name not in {t for t, _ in TABLES}:
        raise ValueError(f"Not an agent table: {name!r}")
    prefix = f"{config.AGENT_TABLES_S3_PREFIX}{name}/"
    assert (
        prefix.startswith("s3://is459-aml-g1t5/agent/tables/")
        and len(prefix) > len(config.AGENT_TABLES_S3_PREFIX) + 1
    )
    return prefix


def statements() -> list[tuple[str, str]]:
    """(table, CTAS SQL) in build order, with the config dates filled in."""

    ranges = date_ranges()
    out = []
    for name, filename in TABLES:
        text = (SQL_DIR / filename).read_text(encoding="utf-8")
        if (
            name == "agent_day_queue_v2"
        ):  # Built from the scores table, so it has no dates.
            out.append((name, _executable(text)))
            continue
        span = ranges["edges"] if name == "agent_edges_v2" else ranges["scores"]
        out.append((name, render_sql(text, *span)))
    return out


def compare_fingerprints(before: dict, after: dict) -> list[str]:
    """Days present both times whose ranks changed. New days are expected to differ."""

    return [
        f"{day}: {before[day]} -> {after[day]}"
        for day in sorted(set(before) & set(after))
        if before[day] != after[day]
    ]


def fingerprints(query) -> dict[str, tuple[str, str]]:
    frame = query(FINGERPRINT_SQL)
    return {r["txn_date"]: (r["n"], r["fp"]) for r in frame.to_dict("records")}


def tables_present(query) -> bool:
    """The edges and queue tables exist. A rebuild that failed part way leaves only the scores."""

    try:
        for name in ("agent_edges_v2", "agent_day_queue_v2"):
            query(f"SELECT 1 AS present FROM is459_aml_gold.{name} LIMIT 1")
    except AthenaQueryError:
        return False
    return True


def rebuild(*, execute, query, list_keys, delete_keys, log=print, force=False) -> list[str]:
    """Drop, clear and rebuild the tables. Returns the days whose ranks changed (should be none)."""

    plan = (
        statements()
    )  # Build everything first, so a bad SQL file fails before anything is deleted.
    try:
        before = fingerprints(query)
    except AthenaQueryError:
        before = {}
        log("No existing scores table to fingerprint.")
    log(f"Fingerprinted {len(before)} existing day(s).")

    # The new-day path calls this for every ETL day. A day the tables already hold needs nothing, and
    # a rebuild that would drop a later day is refused, so a replayed old day never shrinks them.
    wanted = set(config.AGENT_SCORED_DATES)
    if not force and before and wanted <= set(before) and tables_present(query):
        log("The tables already hold every scored day; nothing rebuilt.")
        return []
    later = sorted(d for d in before if d not in wanted)
    if later:
        raise RuntimeError(
            f"The tables hold {later}, after AGENT_LAST_DAY {config.AGENT_LAST_SCORED_DAY}. "
            "Refusing to drop them; set AGENT_LAST_DAY to the latest day."
        )

    for name, _ in reversed(plan):
        execute(f"DROP TABLE IF EXISTS is459_aml_gold.{name}")
        keys = list_keys(table_prefix(name))
        if keys:
            delete_keys(keys)
        log(f"Dropped {name} and removed {len(keys)} S3 object(s).")
    for name, sql in plan:
        started = time.monotonic()
        execute(sql)
        log(f"Built {name} in {time.monotonic() - started:.0f} s.")

    changed = compare_fingerprints(before, fingerprints(query))
    log(
        "Existing days rank identically."
        if not changed
        else f"RANKS CHANGED: {changed}"
    )
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="Actually drop and rebuild.")
    parser.add_argument("--force", action="store_true", help="Rebuild even if the tables already hold every day.")
    args = parser.parse_args()

    ranges = date_ranges()
    print(f"Scored dates: {config.AGENT_SCORED_DATES}")
    print(f"Scores {ranges['scores']}, edges {ranges['edges']}")
    for name, _ in TABLES:
        print(f"  {name}: drop, then delete {table_prefix(name)}, then rebuild")
    print(
        "About 2.7 GB scanned, a few cents. The agent tools fail while the tables are down."
    )
    statements()  # Fail now on a bad SQL file.
    if not args.yes:
        print("Dry run: nothing changed. Pass --yes to rebuild.")
        return 0

    athena = create_athena_client(config.AWS_REGION)
    s3 = boto3.client("s3", region_name=config.AWS_REGION)

    def execute(sql: str) -> None:
        run = athena.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": config.ATHENA_DATABASE},
            ResultConfiguration={"OutputLocation": config.AGENT_ATHENA_OUTPUT},
        )["QueryExecutionId"]
        while True:
            status = athena.get_query_execution(QueryExecutionId=run)["QueryExecution"][
                "Status"
            ]
            if status["State"] == "SUCCEEDED":
                return
            if status["State"] in {"FAILED", "CANCELLED"}:
                raise AthenaQueryError(
                    f"{status['State']}: {status.get('StateChangeReason')}"
                )
            time.sleep(1)

    def query(sql: str):
        return execute_query_with_stats(
            athena,
            query=sql,
            database=config.ATHENA_DATABASE,
            output_location=config.AGENT_ATHENA_OUTPUT,
        )[0]

    bucket, _, _ = config.AGENT_TABLES_S3_PREFIX.removeprefix("s3://").partition("/")

    def list_keys(prefix: str) -> list[str]:
        key_prefix = prefix.removeprefix(f"s3://{bucket}/")
        pages = s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=key_prefix
        )
        return [o["Key"] for page in pages for o in page.get("Contents", [])]

    def delete_keys(keys: list[str]) -> None:
        for start in range(0, len(keys), 1000):
            s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in keys[start : start + 1000]]},
            )

    changed = rebuild(
        execute=execute, query=query, list_keys=list_keys, delete_keys=delete_keys, force=args.force
    )
    return 1 if changed else 0


if __name__ == "__main__":
    sys.exit(main())
