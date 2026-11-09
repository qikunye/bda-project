"""Offline reproducibility after moving the historical BQ2 experiments."""

import importlib
from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

EVAL_DIR = Path(__file__).resolve().parents[2] / "evaluations" / "bq2"


@pytest.mark.parametrize(
    "name",
    ["evaluate_bq2", "evaluate_bq2_athena", "evaluate_bq2_windows"],
)
def test_paths_are_independent_of_working_directory(name, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    module = importlib.import_module(f"evaluations.bq2.{name}")
    paths = [value for key, value in vars(module).items() if key.endswith("_PATH")]
    assert paths
    for path in paths:
        assert path.is_absolute()
        assert path.parent == EVAL_DIR
        assert path.is_file()
    if hasattr(module, "load_cases"):
        cases = module.load_cases()
        assert len(cases) == 176
        builder = getattr(module, "build_query", None) or module.build_batch_query
        assert "bq2_graph_edges_medium_v1" in builder(cases.iloc[:2])
        # Preserve the explicit input override for callers with a different case set.
        alternate = tmp_path / "cases.csv"
        pd.read_csv(module.INPUT_PATH).iloc[:2].to_csv(alternate, index=False)
        assert len(module.load_cases(alternate)) == 2


def test_saved_window_summary_matches_checked_in_case_results():
    from evaluations.bq2.evaluate_bq2_windows import build_window_summary

    cases = pd.read_csv(EVAL_DIR / "bq2_window_case_results.csv")
    expected = pd.read_csv(EVAL_DIR / "bq2_window_tradeoff_evaluation.csv")
    assert_frame_equal(
        build_window_summary(cases), expected, check_dtype=False, rtol=1e-12, atol=1e-12
    )
