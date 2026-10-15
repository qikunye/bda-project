import json
import subprocess

import pytest

from scripts import smoke_test_case_context_aws as cli
from src.context.validation import (
    CaseContextIntegrityError,
    validate_case_context,
    validate_llm_input,
)
from tests.unit.test_case_context_service import (
    RecordingQueryExecutor,
    make_athena_edges,
    make_handoff,
)


def cli_arguments(*extra: str) -> list[str]:
    return [
        "--transaction-id",
        "txn_SyntheticAlert001",
        "--source-account-key",
        "SYNTHETIC_ACCOUNT_A",
        "--target-account-key",
        "SYNTHETIC_ACCOUNT_B",
        "--timestamp",
        "2022-09-03 12:00:00",
        *extra,
    ]


def test_dry_run_executes_zero_aws_operations(monkeypatch, capsys):
    def fail_if_called():
        raise AssertionError("AWS client creation was attempted")

    monkeypatch.setattr(cli, "create_real_query_executor", fail_if_called)

    exit_code = cli.main(cli_arguments("--with-bq1"))

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Execution mode: DRY RUN" in output
    assert "AWS client created: no" in output
    assert "AWS API operations: 0" in output


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("transaction_id", "", "transaction_id"),
        ("source_account_key", " leading-space", "source_account_key"),
        ("target_account_key", "", "target_account_key"),
        ("timestamp", "2022-09-03 12:00", "timestamp"),
        ("timestamp", "2022-09-03 12:00:30", "minute-resolution"),
    ],
)
def test_handoff_input_validation(field_name, value, message):
    values = {
        "transaction_id": "txn_SyntheticAlert001",
        "source_account_key": "SYNTHETIC_ACCOUNT_A",
        "target_account_key": "SYNTHETIC_ACCOUNT_B",
        "timestamp": "2022-09-03 12:00:00",
    }
    values[field_name] = value

    with pytest.raises(ValueError, match=message):
        cli.build_handoff(**values)


def test_builds_exact_four_field_handoff():
    handoff = cli.build_handoff(
        transaction_id="txn_SyntheticAlert001",
        source_account_key="SYNTHETIC_ACCOUNT_A",
        target_account_key="SYNTHETIC_ACCOUNT_B",
        timestamp="2022-09-03 12:00:00",
    )

    assert handoff == make_handoff()


def test_query_plan_converts_hours_to_minutes():
    plan = cli.build_query_plan(
        handoff=make_handoff(),
        queried_window_hours=48,
        with_bq1=False,
    )

    assert "2880\n                AS window_minutes" in plan.bq2_query
    assert plan.queried_window_hours == 48


def test_bq2_query_plan_preserves_partition_pruning():
    plan = cli.build_query_plan(
        handoff=make_handoff(),
        queried_window_hours=48,
        with_bq1=False,
    )

    assert plan.bq2_query.count("g.txn_date") == 3
    assert plan.bq2_query.count("g.txn_date BETWEEN") == 2


def test_bq1_query_plan_preserves_partition_pruning():
    plan = cli.build_query_plan(
        handoff=make_handoff(),
        queried_window_hours=48,
        with_bq1=True,
    )

    assert plan.bq1_query is not None
    assert "txn_date = DATE '2022-09-03'" in plan.bq1_query


def test_bq1_query_is_optional():
    without_bq1 = cli.build_query_plan(
        handoff=make_handoff(),
        queried_window_hours=48,
        with_bq1=False,
    )
    with_bq1 = cli.build_query_plan(
        handoff=make_handoff(),
        queried_window_hours=48,
        with_bq1=True,
    )

    assert without_bq1.bq1_query is None
    assert without_bq1.bq1_query_count == 0
    assert with_bq1.bq1_query_count == 1


def test_real_executor_requires_explicit_execute_flag(monkeypatch):
    calls = []

    def record_call():
        calls.append("created")
        return RecordingQueryExecutor()

    monkeypatch.setattr(cli, "create_real_query_executor", record_call)

    assert cli.main(cli_arguments()) == 0
    assert calls == []


def test_fake_executor_generates_both_valid_json_outputs(tmp_path):
    result = cli.execute_smoke_test(
        handoff=make_handoff(),
        query_executor=RecordingQueryExecutor(),
        output_directory=tmp_path,
        queried_window_hours=48,
        with_bq1=True,
        max_candidates=2,
        max_transactions=4,
    )

    assert result.full_context_path == tmp_path / cli.FULL_CONTEXT_FILENAME
    assert result.llm_input_path == tmp_path / cli.LLM_INPUT_FILENAME
    assert result.full_context_path.is_file()
    assert result.llm_input_path.is_file()
    validate_case_context(
        json.loads(result.full_context_path.read_text(encoding="utf-8"))
    )
    validate_llm_input(
        json.loads(result.llm_input_path.read_text(encoding="utf-8"))
    )


def test_fake_execution_uses_expected_query_counts(tmp_path):
    result = cli.execute_smoke_test(
        handoff=make_handoff(),
        query_executor=RecordingQueryExecutor(),
        output_directory=tmp_path,
        queried_window_hours=48,
        with_bq1=True,
        max_candidates=20,
        max_transactions=100,
    )

    assert result.bq2_query_count == 1
    assert result.bq1_query_count == 1


def test_fake_execution_without_bq1_uses_one_query(tmp_path):
    result = cli.execute_smoke_test(
        handoff=make_handoff(),
        query_executor=RecordingQueryExecutor(),
        output_directory=tmp_path,
        queried_window_hours=48,
        with_bq1=False,
        max_candidates=20,
        max_transactions=100,
    )

    assert result.bq2_query_count == 1
    assert result.bq1_query_count == 0
    assert result.context["bq1"]["status"] == "unavailable"


def test_failed_query_propagates_and_writes_no_output(tmp_path):
    def failing_executor(*, query: str):
        raise RuntimeError(f"synthetic query failure: {len(query)}")

    with pytest.raises(RuntimeError, match="synthetic query failure"):
        cli.execute_smoke_test(
            handoff=make_handoff(),
            query_executor=failing_executor,
            output_directory=tmp_path,
            queried_window_hours=48,
            with_bq1=False,
            max_candidates=20,
            max_transactions=100,
        )

    assert not (tmp_path / cli.FULL_CONTEXT_FILENAME).exists()
    assert not (tmp_path / cli.LLM_INPUT_FILENAME).exists()


def test_integrity_mismatch_prevents_success_and_output(tmp_path):
    raw_edges = make_athena_edges()
    raw_edges.loc[
        raw_edges["edge_role"] == "ALERT",
        "target_account_key",
    ] = "WRONG_TARGET"
    executor = RecordingQueryExecutor(bq2_result=raw_edges)

    with pytest.raises(CaseContextIntegrityError, match="target_account_key"):
        cli.execute_smoke_test(
            handoff=make_handoff(),
            query_executor=executor,
            output_directory=tmp_path,
            queried_window_hours=48,
            with_bq1=False,
            max_candidates=20,
            max_transactions=100,
        )

    assert list(tmp_path.iterdir()) == []


def test_outputs_exclude_ground_truth_material(tmp_path):
    result = cli.execute_smoke_test(
        handoff=make_handoff(),
        query_executor=RecordingQueryExecutor(),
        output_directory=tmp_path,
        queried_window_hours=48,
        with_bq1=True,
        max_candidates=20,
        max_transactions=100,
    )

    combined = result.full_context_path.read_text(
        encoding="utf-8"
    ) + result.llm_input_path.read_text(encoding="utf-8")
    assert "evaluation_is_laundering" not in combined
    assert "data_split" not in combined
    assert "laundering_pattern" not in combined


def test_real_output_directory_is_ignored_by_git():
    completed = subprocess.run(
        [
            "git",
            "check-ignore",
            "artifacts/aws-smoke/case_context.real.json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "artifacts/aws-smoke/case_context.real.json" in completed.stdout


def test_execute_cli_can_use_injected_fake_executor(monkeypatch, tmp_path, capsys):
    executor = RecordingQueryExecutor()
    monkeypatch.setattr(cli, "create_real_query_executor", lambda: executor)
    monkeypatch.setattr(cli, "DEFAULT_OUTPUT_DIRECTORY", tmp_path)

    exit_code = cli.main(
        cli_arguments(
            "--execute",
            "--with-bq1",
        )
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Execution mode: REAL AWS" in output
    assert "CaseContext validation: PASS" in output
    assert "LLM projection validation: PASS" in output
    assert len(executor.queries) == 2


def test_planned_executor_rejects_unexpected_query():
    plan = cli.build_query_plan(
        handoff=make_handoff(),
        queried_window_hours=48,
        with_bq1=False,
    )
    executor = cli.PlannedQueryExecutor(RecordingQueryExecutor(), plan)

    with pytest.raises(cli.SmokeTestError, match="unexpected Athena query"):
        executor(query="SELECT something_unplanned")


def test_dry_run_does_not_print_alert_identifiers(capsys):
    cli.main(cli_arguments("--with-bq1"))

    output = capsys.readouterr().out
    assert "txn_SyntheticAlert001" not in output
    assert "SYNTHETIC_ACCOUNT_A" not in output
    assert "SYNTHETIC_ACCOUNT_B" not in output


@pytest.mark.parametrize(
    "arguments",
    [
        ("--window-hours", "0"),
        ("--max-candidates", "-1"),
        ("--max-transactions", "0"),
    ],
)
def test_cli_rejects_invalid_numeric_limits(arguments):
    with pytest.raises(SystemExit):
        cli.main(cli_arguments(*arguments))


def test_dry_run_reports_telemetry_limitation(capsys):
    assert cli.main(cli_arguments()) == 0

    output = capsys.readouterr().out
    assert "scan telemetry: unavailable" in output
