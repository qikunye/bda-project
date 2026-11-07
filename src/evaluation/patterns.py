"""Reads the laundering-attempt blocks of the patterns file and maps them to alerts.

A block runs from ``BEGIN LAUNDERING ATTEMPT - <TYPE>`` to ``END LAUNDERING ATTEMPT - <TYPE>``, with
one 11-field CSV line per payment. The lines carry no transaction ID, so a payment is matched to a
scored transaction by sender, receiver and minute. A key that matches more than one transaction is
left unmatched rather than guessed.
"""

import csv
from collections import defaultdict
from datetime import datetime

BEGIN = "BEGIN LAUNDERING ATTEMPT - "
END = "END LAUNDERING ATTEMPT - "


def _account_key(bank: str, account: str) -> str:
    return f"{bank.strip().lstrip('0') or '0'}|{account.strip()}"


def parse_attempts(text: str) -> list[dict]:
    """Returns ``[{"type": "FAN-OUT", "payments": [(minute, sender_key, receiver_key), ...]}]``."""

    attempts, current = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(BEGIN):
            current = {"type": line[len(BEGIN):].split(":")[0].strip(), "payments": []}
        elif line.startswith(END):
            if current is not None:
                attempts.append(current)
            current = None
        elif current is not None and line:
            fields = next(csv.reader([line]))
            if len(fields) != 11:
                continue  # A malformed line skips that payment only.
            when = datetime.strptime(fields[0].strip(), "%Y/%m/%d %H:%M")
            current["payments"].append(
                (
                    when.strftime("%Y-%m-%d %H:%M"),
                    _account_key(fields[1], fields[2]),
                    _account_key(fields[3], fields[4]),
                )
            )
    return attempts


def transaction_types(attempts: list[dict], scored: list[dict]) -> tuple[dict[str, str], dict]:
    """Maps transaction_id to attempt type. ``scored`` rows need transaction_id, source_account_key,
    target_account_key and transaction_timestamp. Also returns coverage counts."""

    index: dict[tuple, list[str]] = defaultdict(list)
    for row in scored:
        minute = str(row["transaction_timestamp"])[:16].replace("T", " ")
        index[(minute, row["source_account_key"], row["target_account_key"])].append(
            row["transaction_id"]
        )
    mapping, matched, ambiguous, missing = {}, 0, 0, 0
    for attempt in attempts:
        for payment in attempt["payments"]:
            ids = index.get(payment, [])
            if len(ids) == 1:
                mapping[ids[0]] = attempt["type"]
                matched += 1
            elif ids:
                ambiguous += 1
            else:
                missing += 1
    return mapping, {"matched": matched, "ambiguous": ambiguous, "not_in_scored_rows": missing}


def case_types(case_members: dict[str, tuple[str, ...]], txn_type: dict[str, str]) -> dict[str, str]:
    """A case takes the type of its members' attempt; a mix takes the most common."""

    out = {}
    for case_id, members in case_members.items():
        kinds = [txn_type[m] for m in members if m in txn_type]
        if kinds:
            out[case_id] = max(set(kinds), key=kinds.count)
    return out
