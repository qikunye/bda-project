"""One deterministic offline rehearsal of the three final demonstration journeys."""

from pathlib import Path
from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from src.ui.pages.batch import DEMO_SOURCE
from src.ui.services.on_demand import DemoOnDemandService
from tests.ui.test_batch_dashboard import select_alert

APP = Path(__file__).resolve().parents[2] / "app.py"


def _button(app, label):
    return next(widget for widget in app.button if widget.label == label)


def _text(app):
    return "\n".join(str(element.value) for kind in ("text", "caption", "info", "warning", "markdown")
                     for element in app.get(kind))


def test_offline_demo_journeys_a_b_c():
    # A: BQ1 order, saved case, native graph, cross-member evidence and provenance.
    app = AppTest.from_file(APP, default_timeout=10).run()
    ranks = app.dataframe[0].value["Daily rank"].tolist()
    assert ranks == sorted(ranks)
    select_alert(app, "txn_samplefanmember")
    _button(app, "Open Selected Investigation").click().run()
    assert app.get("iframe") and app.selectbox(key="ui_batch_member").value == "txn_samplefanmember"
    _button(app, "txn_samplefanup").click().run()
    assert app.selectbox(key="ui_batch_member").value == "txn_samplefan"
    assert "The exact transaction is highlighted" in _text(app)
    app.selectbox(key="ui_batch_member").set_value("txn_samplefanmember").run()
    assert {"How this brief was generated", "Tool trace"} <= {widget.label for widget in app.expander}
    assert "Overall case · 4 supported findings" in _text(app)
    assert not app.exception

    # B: Explicit simulation, independent views and no repeated execution on reruns.
    _button(app, "Back to Alert Queue").click().run()
    select_alert(app, "txn_samplepass")
    _button(app, "Open Selected Investigation").click().run()
    runner = Mock(wraps=DemoOnDemandService())
    app.session_state["ui_on_demand_services"] = {DEMO_SOURCE: runner}
    _button(app, "Run Agent Investigation").click().run()
    assert "SIMULATED ON-DEMAND RESULT" in _text(app)
    views = next(widget for widget in app.radio if widget.label == "Investigation report")
    assert views.value == "New Investigation"
    views.set_value("Previous Investigation").run()
    assert "Saved run:" in _text(app)
    next(widget for widget in app.radio if widget.label == "Investigation report").set_value("New Investigation").run()
    app.run()
    runner.run.assert_called_once_with("txn_samplepass", "2022-09-14", window_h=48)
    assert not app.exception

    # C: Offline session group, explanation, pending state and deliberate refresh.
    app.radio(key="ui_navigation_page").set_value("Real-Time Alert Lookup").run()
    assert not app.dataframe
    _button(app, "Load known alerts").click().run()
    assert len(app.dataframe[0].value) == 3
    assert "Simulated explanation:" in _text(app)
    app.selectbox(key="ui_rt_selected_txn").set_value("demo_rt_pending").run()
    assert "no brief was stored in this lookup" in _text(app)
    _button(app, "Refresh loaded alerts").click().run()
    assert "no brief was stored in this lookup" in _text(app)
    assert "is459-rt-scorer" in _text(app) and "is459-rt-explainer" in _text(app)
    assert not app.exception
