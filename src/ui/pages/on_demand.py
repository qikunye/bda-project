"""Explicit execution controls and session-retained attempts, separate from saved batch data."""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

import streamlit as st

from src.agent.contracts import InvestigateResult
from src.ui.components.formatting import format_number
from src.ui.pages.batch import DEMO_SOURCE
from src.ui.services.contracts import check_shape, label_free, require
from src.ui.services.errors import ServiceFailure, describe_failure
from src.ui.services.on_demand import DemoOnDemandService, NoDemoResult, OnDemandService


@dataclass
class Attempt:
    generation: int
    completed_at: str
    result: dict | None = None
    failure: ServiceFailure | None = None
    notice: str | None = None


def render_on_demand(selection: dict, source: str) -> tuple[Attempt | None, str]:
    """Only the enabled execution button can reach service.run()."""
    st.subheader("Run an investigation")
    txn_id, day = selection["txn_id"], selection["day"]
    entry = selection["entry"]
    st.text(f"Request alert: {txn_id} · Date: {day} · Source: {source}")
    st.caption(f"BQ1 daily rank: {entry.get('risk_rank_day', 'Unavailable')} · "
               f"BQ1 ranking score: {format_number(entry.get('risk_score'))}")
    target = (source, txn_id, day)
    windows = st.session_state.setdefault("ui_on_demand_windows", {})
    target_id = sha256(repr(target).encode()).hexdigest()[:16]
    window = st.slider("On-demand window (hours)", 6, 72, value=windows.get(target, 48),
                       key=f"ui_on_demand_window_{target_id}")
    windows[target] = window
    request = (*target, window)
    request_id = sha256(repr(request).encode()).hexdigest()[:16]
    attempts = st.session_state.setdefault("ui_on_demand_attempts", {})
    attempt = attempts.get(request)
    if source == DEMO_SOURCE:
        st.info("Simulated on-demand execution uses only an existing matching single-alert fixture. "
                "No AWS or real agent runs. Fixtures support their original 48-hour window only.")
        acknowledged = True
    else:
        st.caption("This action may take 20 seconds or longer and may incur Athena and Bedrock charges. "
                   "Backend execution limits apply; this screen cannot forcibly cancel an active request.")
        acknowledged = st.checkbox(
            "I acknowledge that this investigation may invoke Athena and Bedrock and incur charges.",
            key=f"ui_on_demand_ack_{request_id}",
        )
    running = st.session_state.get("ui_on_demand_running")
    if running:
        st.info("An investigation is already running in this session. Wait for it to finish.")
    submitted = st.button(
        "Run again" if attempt else "Run Agent Investigation",
        key=f"ui_on_demand_execute_{request_id}_{attempt.generation if attempt else 0}",
        disabled=not acknowledged or bool(running), type="primary",
    )
    if submitted and acknowledged and not running:
        st.session_state["ui_on_demand_running"] = request
        completed = Attempt((attempt.generation if attempt else 0) + 1,
                            datetime.now(timezone.utc).isoformat(timespec="seconds"))
        try:
            services = st.session_state.setdefault("ui_on_demand_services", {})
            if source not in services:
                services[source] = DemoOnDemandService() if source == DEMO_SOURCE else OnDemandService()
            with st.spinner("Investigating this alert… this may take 20 seconds or longer."):
                result = services[source].run(txn_id, day, window_h=window)
            check_shape(result, InvestigateResult)
            label_free(result)
            require((result["txn_id"], result["txn_date"], result["window_h"]) == (txn_id, day, window),
                    "The returned result does not match the submitted request.")
            require(result["status"] == "not_found" or (
                result["case_id"] is None and len(result["members"]) == 1 and
                result["members"][0]["alert"]["txn_id"] == txn_id
            ), "On-demand execution must return this single alert's investigation.")
            completed.result = result
        except NoDemoResult as error:
            completed.notice = str(error)
        except Exception as error:  # noqa: BLE001 -- UI execution boundary; preserve saved evidence.
            completed.failure = describe_failure(error, "On-demand investigation")
        finally:
            st.session_state.pop("ui_on_demand_running", None)
        completed.completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        attempts[request] = attempt = completed
        if completed.result is not None:
            st.session_state[f"ui_on_demand_view_{request_id}"] = "On-Demand Brief"
            st.session_state.pop("ui_on_demand_display_member", None)
            st.session_state.pop("ui_on_demand_display_evidence", None)
        # Replace the submitted button with a fresh, explicit Run again action.
        st.rerun()
    if attempt is not None:
        if attempt.notice:
            st.info(attempt.notice)
        if attempt.failure:
            st.error(attempt.failure.message)
            st.caption(f"Debug reference: {attempt.failure.reference}. No automatic retry was started.")
    return attempt, request_id
