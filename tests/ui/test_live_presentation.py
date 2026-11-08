"""Reproduce Athena string-score presentation without any AWS operations."""

from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from src.ui.components.agent_brief import UNAVAILABLE as METRIC_UNAVAILABLE
from src.ui.components.agent_brief import money_flow_rows
from src.ui.components.formatting import format_number
from src.ui.components.transaction_detail import amount_text
from src.ui.pages.batch import LIVE_SOURCE
from src.ui.services.queue import LiveBatchService, enrich_queue
from src.ui.services.saved_results import DecisionBatch, SavedBrief
from tests.ui.test_batch_dashboard import APP, content, dashboard, open_alert

VERIFIED_ID = "txn_613ac569efe8fc2e024adfbeae71b746"
PAGES = ["Alert Queue", "Investigation", "Real-Time Monitoring", "Agent Examples"]


def live_frame(score="0.9958", *, omit_score=False):
    rows = [{
        "transaction_id": VERIFIED_ID if rank == 1 else f"txn_mocklive{rank:02d}",
        "transaction_timestamp": "2022-09-14 10:00:00",
        "source_account_key": "100|MOCK", "target_account_key": "200|MOCK",
        "risk_rank_day": str(rank), "risk_rank_global": str(rank * 10),
        **({} if omit_score else {"risk_score": score}),
    } for rank in range(1, 21)]
    return pd.DataFrame(rows)


@pytest.mark.parametrize(("value", "expected"), [
    ("0.9958", "0.9958"), (0.9958, "0.9958"), (Decimal("0.9958"), "0.9958"),
    (1, "1.0000"), (0, "0.0000"), (" 0.9958 ", "0.9958"),
    (None, "Unavailable"), ("", "Unavailable"), ("  ", "Unavailable"),
    ("bad score", "Unavailable"), (float("nan"), "Unavailable"),
    (float("inf"), "Unavailable"), (Decimal("NaN"), "Unavailable"),
    (Decimal("Infinity"), "Unavailable"), (Decimal("sNaN"), "Unavailable"),
    (True, "Unavailable"), ([], "Unavailable"),
])
def test_finite_score_formatter(value, expected):
    assert format_number(value) == expected


@pytest.mark.parametrize(("score", "expected", "omit_score"), [
    ("0.9958", "0.9958", False), (0.9958, "0.9958", False),
    (Decimal("0.9958"), "0.9958", False), (1, "1.0000", False),
    (None, "Unavailable", False), ("", "Unavailable", False),
    ("malformed", "Unavailable", False), (float("nan"), "Unavailable", False),
    (None, "Unavailable", True),
])
def test_twenty_live_rows_render_select_and_open_without_format_errors(monkeypatch, score, expected, omit_score):
    frame = live_frame(score, omit_score=omit_score)
    before = frame.copy(deep=True)
    load = Mock(return_value=frame)
    monkeypatch.setattr("src.ui.services.queue.tools.load_day_queue", load)
    saved = Mock()
    saved.resolve_decisions.return_value = DecisionBatch()
    saved.open.return_value = SavedBrief("missing")
    backend = LiveBatchService(data=object(), saved=saved)
    app = dashboard(backend, LIVE_SOURCE)
    next(button for button in app.button if button.label == "Load / refresh queue").click().run()
    assert not app.exception
    table = app.dataframe[0].value
    assert len(table) == 20
    assert table["Transaction ID"].tolist() == frame["transaction_id"].tolist()
    assert table["Daily rank"].tolist() == list(range(1, 21))
    assert set(table["Risk score"]) == {expected}
    assert f"Risk score {expected}" in content(app)
    open_alert(app, VERIFIED_ID)
    assert f"Risk score {expected}" in content(app)
    assert "No batch result is stored" in content(app)
    assert not app.exception
    load.assert_called_once()
    saved.open.assert_called_once()
    assert saved.open.call_args.args[0] == VERIFIED_ID
    pd.testing.assert_frame_equal(frame, before)


def test_queue_enrichment_preserves_original_score_and_identifier_values():
    frame = live_frame(Decimal("0.9958"))
    rows = enrich_queue(frame, DecisionBatch(), "2022-09-14")
    assert rows[0]["risk_score"] == Decimal("0.9958")
    assert rows[0]["txn_id"] == VERIFIED_ID
    assert [row["risk_rank_day"] for row in rows] == list(range(1, 21))


def test_demo_score_still_has_four_decimals_and_no_queue_dropdown():
    app = dashboard()
    assert set(app.dataframe[0].value["Risk score"]) == {"0.7500", "0.2500"}
    assert "Risk score 0.7500" in content(app)
    assert all(widget.key != "ui_batch_queue_alert" for widget in app.selectbox)
    open_alert(app, "txn_samplepass")
    assert app.get("iframe")


@pytest.mark.parametrize("page", PAGES)
def test_exact_four_routes_stop_before_legacy_ui(page):
    app = AppTest.from_file(APP)
    app.session_state["ui_navigation_page"] = page
    app.run()
    assert app.radio(key="ui_navigation_page").options == PAGES
    assert app.radio(key="ui_navigation_page").value == page
    assert not app.text_input  # No root BQ2 controls from a fall-through.
    assert "AML Transaction Network" not in [heading.value for heading in app.title]
    assert not app.exception


@pytest.mark.parametrize(("obsolete", "expected"), [
    ("BQ2 Money Trail (Legacy)", "Alert Queue"),
    ("Legacy BQ2", "Alert Queue"), ("unknown", "Alert Queue"),
    ("Agent Evidence Brief (Demo)", "Agent Examples"),
])
def test_obsolete_navigation_is_corrected_before_keyed_radio(obsolete, expected):
    app = AppTest.from_file(APP)
    app.session_state["ui_navigation_page"] = obsolete
    app.run()
    assert app.radio(key="ui_navigation_page").options == PAGES
    assert app.radio(key="ui_navigation_page").value == expected
    assert not app.exception


def test_supplied_currency_metrics_accept_numeric_strings_without_mutating_source():
    flow = {"per_currency": {"USD": {
        "upstream_total": "95000", "downstream_total": Decimal("125.5"),
        "quick_onward_share": "bad", "fastest_onward_gap_minutes": "25",
        "median_onward_gap_minutes": None, "fastest_onward_txn_id": None,
    }}}
    before = deepcopy(flow)
    row = money_flow_rows(flow)[0]
    assert row["Upstream total"] == "95,000.00"
    assert row["Downstream total"] == "125.50"
    assert row["Quick onward share"] == METRIC_UNAVAILABLE
    assert row["Fastest onward (minutes)"] == "25"
    assert flow == before
    assert amount_text(Decimal(95000), "USD") == "US$95,000"
    assert amount_text(float("nan"), "USD") == "Amount not supplied"
