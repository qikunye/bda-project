"""Check stored shapes and identities without rerunning analytical validation."""

import math
from decimal import Decimal
from types import UnionType
from typing import Literal, Union, get_args, get_origin, get_type_hints, is_typeddict

from src import config
from src.agent.contracts import CaseRow, DecisionRow, InvestigateResult


class IncompatibleResult(ValueError):
    """Saved data does not match the current brief contract or run."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise IncompatibleResult(message)


def native(value):
    """Convert DynamoDB Decimal scalars before checking Python contracts."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: native(item) for key, item in value.items()}
    if isinstance(value, list):
        return [native(item) for item in value]
    return value


def label_free(value) -> None:
    if isinstance(value, dict):
        keys = {str(key).casefold() for key in value}
        require(not keys & (config.AGENT_FORBIDDEN_COLUMNS | {"verdict", "confidence", "reasons"}),
                "Saved data uses an unsupported or excluded field.")
        for item in value.values():
            label_free(item)
    elif isinstance(value, list):
        for item in value:
            label_free(item)


def check_shape(value, schema) -> None:
    """Validate nested TypedDicts, including optional Alert/DecisionRow fields."""
    origin, args = get_origin(schema), get_args(schema)
    if origin in (Union, UnionType):
        for option in args:
            try:
                check_shape(value, option)
                return
            except IncompatibleResult:
                pass
        raise IncompatibleResult("Saved data has an invalid optional field.")
    if origin is Literal:
        require(value in args, "Saved data has an unknown code or status.")
    elif is_typeddict(schema):
        require(isinstance(value, dict), "Saved data must contain a record.")
        fields = get_type_hints(schema)
        require(schema.__required_keys__ <= value.keys() <= fields.keys(),
                "Saved data has missing or unsupported fields.")
        for key, item in value.items():
            check_shape(item, fields[key])
    elif origin is list:
        require(isinstance(value, list), "Saved data must contain a list.")
        for item in value:
            check_shape(item, args[0])
    elif origin is dict:
        require(isinstance(value, dict), "Saved data must contain a mapping.")
        for key, item in value.items():
            check_shape(key, args[0])
            check_shape(item, args[1])
    elif schema is float:
        require(type(value) in (int, float) and math.isfinite(value), "Invalid saved numeric value.")
    elif schema in (int, bool, str, dict, type(None)):
        require(type(value) is schema, "Saved data has an invalid field type.")
    else:
        raise IncompatibleResult("Unsupported saved field schema.")


def validate_decision(row: dict, day: str) -> None:
    label_free(row)
    check_shape(row, DecisionRow)
    base = {"transaction_id", "day", "run_id", "trigger", "role", "case_id",
            "representative_txn_id", "created_at"}
    require(base <= row.keys(), "Saved decision lacks current identity fields.")
    require(row["day"] == day and row["trigger"] == "B", "Saved decision is not this day's batch.")
    require(bool(row["run_id"]) and bool(row["representative_txn_id"]), "Missing saved run identity.")
    if row["role"] == "representative":
        require(DecisionRow.__annotations__.keys() <= row.keys(), "Representative metadata is incomplete.")
        require(row["representative_txn_id"] == row["transaction_id"], "Invalid representative pointer.")


def validate_saved(selected: dict, representative: dict, case: dict, envelope: dict) -> dict:
    """Reject mixed publications and old verdict-based objects before displaying them."""
    day = selected["day"]
    validate_decision(selected, day)
    validate_decision(representative, day)
    label_free(case)
    check_shape(case, CaseRow)
    require(isinstance(envelope, dict) and {"run_id", "created_at", "result"} <= envelope.keys(),
            "Saved trace envelope is incomplete.")
    result = envelope["result"]
    label_free(result)
    check_shape(result, InvestigateResult)
    rep_id = representative["transaction_id"]
    require(representative["role"] == "representative" and selected["representative_txn_id"] == rep_id,
            "Selected alert points to a different representative.")
    for record in (selected, case, envelope):
        require(record["run_id"] == representative["run_id"] and
                record["created_at"] == representative["created_at"],
                "Saved records belong to different publications; refresh the queue.")
    require(case["day"] == day and case["representative_txn_id"] == rep_id and
            case["case_id"] == f"case_{rep_id}" and
            case["trace_key"] == representative["trace_key"], "Case metadata does not match the decision.")
    require(result["status"] == "ok" and result["stats"] is not None and
            result["txn_id"] == rep_id and result["txn_date"] == day,
            "Saved trace does not describe this batch result.")
    members = [member["alert"].get("txn_id") for member in result["members"]]
    require(bool(members) and members[0] == rep_id and len(members) == len(set(members)) and
            members == case["member_txn_ids"] and case["size"] == len(members) and
            selected["transaction_id"] in members, "Case membership is inconsistent.")
    expected_case_id = case["case_id"] if len(members) > 1 else None
    require(selected["case_id"] == representative["case_id"] == result["case_id"] == expected_case_id,
            "Saved case identity is inconsistent.")
    for member in result["members"]:
        alert = member["alert"]
        require({"txn_id", "txn_date", "ts", "sender", "receiver", "found"} <= alert.keys() and
                alert["found"] and alert["txn_date"] == day and
                member["graph"]["txn_id"] == alert["txn_id"], "Member alert or graph is incomplete.")
    require(result["alert"] == result["members"][0]["alert"] and
            [flow["txn_id"] for flow in result["money_flow"]] == members,
            "Saved alert or money-flow membership is inconsistent.")
    codes = [finding["code"] for finding in result["findings"]]
    require(codes == representative["finding_codes"] == case["finding_codes"] and
            [gap["code"] for gap in result["gaps"]] == representative["gap_codes"] and
            [check["code"] for check in result["next_checks"]] == representative["next_check_codes"] and
            result["fallback"] == result["stats"]["fallback"] == representative["fallback"] == case["fallback"],
            "Saved brief metadata does not match its trace.")
    require(result["summary"] == representative["summary"] and
            len(result["observations"]) == representative["observation_count"] and
            all(result["stats"][key] == representative[key] for key in (
                "model", "steps", "tokens_in", "tokens_out", "latency_ms", "repair_used",
            )), "Saved execution metadata does not match its trace.")
    return result
