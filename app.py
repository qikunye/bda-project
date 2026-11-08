import re

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from pyvis.network import Network

from src.aws.athena import create_athena_client, execute_query
from src.bq2.queries import build_alert_lookup_query, build_case_edges_query
from src.bq2.transforms import (
    build_display_edges,
    build_nodes,
    build_window_sensitivity,
    calculate_case_metrics,
    normalize_transaction_timestamp,
)

# ============================================================
# CONFIG
# ============================================================

AWS_REGION = "us-east-1"

ATHENA_DATABASE = "is459_aml_gold"

ATHENA_TABLE = (
    "is459_aml_gold."
    "bq2_graph_edges_medium_v1"
)

ATHENA_OUTPUT = (
    "s3://is459-aml-g1t5/"
    "athena-results/bq2/"
)

DEFAULT_TRANSACTION_ID = (
    "txn_630dea1dda9482dbd765a966355d6708"
)

DEFAULT_ALERT_SENDER = "120075|81294D7A0"
DEFAULT_ALERT_RECEIVER = "15126|8127C1990"
DEFAULT_ALERT_TIMESTAMP = "2022-09-03 12:27:00"

BQ1_HANDOFF_MODE = "BQ1 alert handoff"
TRANSACTION_LOOKUP_MODE = "Transaction ID lookup"

# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="AML Investigation Dashboard",
    layout="wide"
)

PAGE_LABELS = {
    "Alert Queue": "Alert Queue",
    "Investigation": "Investigation",
    "Real-Time Monitoring": "Real-Time Monitoring",
    "Agent Examples": "Agent Examples",
}
PAGE_GUIDANCE = {
    "Alert Queue": "Find transactions to review.",
    "Investigation": "Explore money flow and supporting evidence.",
    "Real-Time Monitoring": "Check stored incoming alerts and their Lambda explanations.",
    "Agent Examples": "Explore offline examples of investigation patterns.",
}

# Migrate old example bookmarks and reject removed/unknown navigation values before
# instantiating the keyed widget. The legacy root UI below is never routed to.
previous_page = st.session_state.get("ui_navigation_page", "Alert Queue")
if previous_page == "Agent Evidence Brief (Demo)":
    st.session_state["ui_navigation_page"] = "Agent Examples"
elif previous_page not in PAGE_LABELS:
    st.session_state["ui_navigation_page"] = "Alert Queue"

page = st.sidebar.radio(
    "Application",
    list(PAGE_LABELS),
    key="ui_navigation_page",
    format_func=PAGE_LABELS.get,
)
st.sidebar.caption(PAGE_GUIDANCE[page])
with st.sidebar.expander("Explore the dashboard", expanded=False):
    for name, description in PAGE_GUIDANCE.items():
        st.caption(f"{PAGE_LABELS[name]} — {description}")

if page == "Alert Queue":
    from src.ui.pages.queue import render_queue

    render_queue()
    st.stop()

if page == "Investigation":
    from src.ui.pages.investigation import render_investigation

    render_investigation()
    st.stop()

if page == "Real-Time Monitoring":
    from src.ui.pages.realtime import render_realtime

    render_realtime()
    st.stop()

if page == "Agent Examples":
    from src.ui.pages.agent_demo import render_agent_demo

    render_agent_demo()
    st.stop()


# Fail closed: no supported page can fall through into the retained legacy UI.
st.stop()

# ============================================================
# AWS CLIENT
# ============================================================

@st.cache_resource
def get_athena_client():

    return create_athena_client(
        AWS_REGION
    )


# ============================================================
# ATHENA HELPER
# ============================================================

def run_athena_query(query: str) -> pd.DataFrame:

    return execute_query(
        get_athena_client(),
        query=query,
        database=ATHENA_DATABASE,
        output_location=ATHENA_OUTPUT
    )


# ============================================================
# INPUT VALIDATION
# ============================================================

def valid_transaction_id(transaction_id):

    return bool(
        re.fullmatch(
            r"txn_[A-Za-z0-9]+",
            transaction_id
        )
    )


# ============================================================
# GET ALERT TRANSACTION
# ============================================================

@st.cache_data(
    ttl=300,
    show_spinner=False
)
def get_alert(transaction_id):

    query = build_alert_lookup_query(
        transaction_id,
        table_name=ATHENA_TABLE
    )

    return run_athena_query(query)


# ============================================================
# GET CASE TRANSACTION EDGES
# ============================================================

@st.cache_data(
    ttl=300,
    show_spinner=False
)
def get_case_edges(
    alert_sender,
    alert_receiver,
    alert_timestamp,
    alert_transaction_id,
    window_minutes
):

    query = build_case_edges_query(
        alert_sender,
        alert_receiver,
        alert_timestamp,
        alert_transaction_id,
        window_minutes,
        table_name=ATHENA_TABLE
    )

    return run_athena_query(query)


# ============================================================
# GRAPH RENDERING
# ============================================================

def render_network(
    nodes,
    edges
):

    network = Network(
        height="650px",
        width="100%",
        directed=True,
        bgcolor="#ffffff",
        font_color="#222222"
    )


    for node in nodes:

        roles = node["roles"]

        if "ALERT_SENDER" in roles:

            color = "#ff9f1c"
            size = 36
            level = 1

        elif "ALERT_RECEIVER" in roles:

            color = "#e71d36"
            size = 36
            level = 2

        elif "UPSTREAM" in roles:

            color = "#2ec4b6"
            size = 24
            level = 0

        else:

            color = "#4361ee"
            size = 24
            level = 3


        entity_name = (
            node["entity_name"]
            if pd.notna(
                node["entity_name"]
            )
            else "Unknown entity"
        )

        role_text = ", ".join(
            sorted(roles)
        )

        title = (
            f"<b>{entity_name}</b><br>"
            f"Account: "
            f"{node['account_key']}<br>"
            f"Role: {role_text}"
        )

        label = (
            f"{entity_name}\n"
            f"{node['account_key']}"
        )

        network.add_node(
            node["account_key"],
            label=label,
            title=title,
            color=color,
            size=size,
            level=level,
            shape="dot",
            borderWidth=2
        )


    for _, edge in edges.iterrows():

        role = edge[
            "edge_role"
        ]

        amount = edge[
            "total_amount"
        ]

        currency = edge[
            "payment_currency"
        ]

        if role == "ALERT":

            title = (
                "<b>FLAGGED TRANSACTION</b>"
                "<br>"
                f"Transactions: "
                f"{edge['transaction_count']}"
                "<br>"
                f"Amount: "
                f"{amount:,.2f} "
                f"{currency}"
                "<br>"
                f"Payment format: "
                f"{edge['payment_formats']}"
            )

            width = 6
            edge_color = "#e71d36"

        else:

            title = (
                f"<b>{role}</b><br>"
                f"Transactions: "
                f"{edge['transaction_count']}"
                "<br>"
                f"Total amount: "
                f"{amount:,.2f} "
                f"{currency}"
                "<br>"
                f"Payment formats: "
                f"{edge['payment_formats']}"
                "<br>"
                f"Closest gap: "
                f"{int(edge['closest_gap_minutes'])} "
                "minutes"
            )

            width = 2
            edge_color = "#777777"


        network.add_edge(
            edge[
                "source_account_key"
            ],

            edge[
                "target_account_key"
            ],

            title=title,
            width=width,
            color=edge_color,
            arrows="to"
        )


    network.set_options(
        """
        {
          "layout": {
            "hierarchical": {
              "enabled": true,
              "direction": "LR",
              "sortMethod": "directed",
              "levelSeparation": 280,
              "nodeSpacing": 200,
              "treeSpacing": 250,
              "blockShifting": true,
              "edgeMinimization": true,
              "parentCentralization": true
            }
          },

          "physics": {
            "enabled": false
          },

          "nodes": {
            "font": {
              "size": 15,
              "face": "Arial",
              "align": "center"
            },

            "borderWidth": 2,

            "shadow": {
              "enabled": true,
              "size": 5,
              "x": 1,
              "y": 1
            }
          },

          "edges": {
            "arrows": {
              "to": {
                "enabled": true,
                "scaleFactor": 0.9
              }
            },

            "smooth": {
              "enabled": true,
              "type": "cubicBezier",
              "forceDirection": "horizontal",
              "roundness": 0.35
            }
          },

          "interaction": {
            "hover": true,
            "navigationButtons": true,
            "keyboard": true,
            "zoomView": true,
            "dragView": true
          }
        }
        """
    )


    # Use generated HTML directly
    html = network.generate_html()

    components.html(
        html,
        height=680,
        scrolling=False
    )


# ============================================================
# PAGE HEADER
# ============================================================

st.title(
    "AML Transaction Network"
)

st.caption(
    "Time-bounded upstream and downstream "
    "transaction relationships around a "
    "selected flagged transaction."
)


# ============================================================
# INVESTIGATION CONTROLS
# ============================================================

with st.sidebar:

    st.header("Investigation")

    input_mode = st.radio(
        "Investigation mode",
        [
            BQ1_HANDOFF_MODE,
            TRANSACTION_LOOKUP_MODE
        ]
    )

    if input_mode == BQ1_HANDOFF_MODE:

        st.caption(
            "Using BQ1 alert metadata to avoid a full "
            "transaction-ID lookup and directly prune "
            "Athena by transaction date."
        )

    else:

        st.caption(
            "Standalone lookup mode may scan more data "
            "because transaction_id is not a partition key."
        )

    with st.form("investigation_form"):

        if input_mode == BQ1_HANDOFF_MODE:

            input_transaction_id = st.text_input(
                "Transaction ID",
                value=DEFAULT_TRANSACTION_ID,
                key="handoff_transaction_id"
            )

            input_sender = st.text_input(
                "Sender account key",
                value=DEFAULT_ALERT_SENDER
            )

            input_receiver = st.text_input(
                "Receiver account key",
                value=DEFAULT_ALERT_RECEIVER
            )

            input_timestamp = st.text_input(
                "Transaction timestamp",
                value=DEFAULT_ALERT_TIMESTAMP,
                help="Use YYYY-MM-DD HH:MM:SS"
            )

        else:

            input_transaction_id = st.text_input(
                "Transaction ID",
                value=DEFAULT_TRANSACTION_ID,
                key="lookup_transaction_id"
            )

        input_window_hours = st.slider(
            "Investigation window (hours)",
            min_value=6,
            max_value=72,
            value=48,
            step=6
        )

        st.caption(
            "48h default selected from project evaluation: "
            "89.2% both-neighbour recovery with a median of "
            "3 connected accounts across 176 RANDOM-chain "
            "evaluation cases."
        )

        submitted = st.form_submit_button(
            "Run investigation",
            type="primary",
            use_container_width=True
        )


# ============================================================
# HANDLE SUBMISSION
# ============================================================

if submitted:

    submitted_transaction_id = (
        input_transaction_id.strip()
    )

    if not valid_transaction_id(
        submitted_transaction_id
    ):

        st.error("Invalid transaction ID format.")
        st.stop()

    if input_mode == BQ1_HANDOFF_MODE:

        submitted_alert_sender = input_sender.strip()
        submitted_alert_receiver = input_receiver.strip()
        submitted_alert_timestamp = (
            normalize_transaction_timestamp(
                input_timestamp
            )
        )

        if (
            not submitted_alert_sender
            or not submitted_alert_receiver
        ):

            st.error(
                "Sender and receiver account keys are required."
            )
            st.stop()

        if (
            "'" in submitted_alert_sender
            or "'" in submitted_alert_receiver
        ):

            st.error(
                "Account keys cannot contain single quotes."
            )
            st.stop()

        if submitted_alert_timestamp is None:

            st.error(
                "Invalid timestamp. Use YYYY-MM-DD HH:MM:SS."
            )
            st.stop()

    else:

        try:

            with st.spinner(
                "Loading selected transaction..."
            ):

                submitted_alert_df = get_alert(
                    submitted_transaction_id
                )

        except Exception as e:  # noqa: BLE001 - surface query errors in the UI

            st.error(
                f"Unable to query Athena: {e}"
            )

            st.stop()

        if submitted_alert_df.empty:

            st.warning(
                "Transaction not found in "
                "the LI-Medium Gold table."
            )

            st.stop()

        submitted_alert = submitted_alert_df.iloc[0]
        submitted_alert_sender = submitted_alert[
            "source_account_key"
        ]
        submitted_alert_receiver = submitted_alert[
            "target_account_key"
        ]
        submitted_alert_timestamp = submitted_alert[
            "transaction_timestamp"
        ]

    try:

        with st.spinner(
            "Expanding transaction network..."
        ):

            submitted_raw_edges = get_case_edges(
                submitted_alert_sender,
                submitted_alert_receiver,
                submitted_alert_timestamp,
                submitted_transaction_id,
                input_window_hours * 60
            )

    except Exception as e:  # noqa: BLE001 - surface query errors in the UI

        st.error(
            f"Unable to expand network: {e}"
        )

        st.stop()

    submitted_alert_edges = submitted_raw_edges[
        submitted_raw_edges["edge_role"]
        == "ALERT"
    ]

    if submitted_alert_edges.empty:

        st.warning(
            "Alert transaction not found in the supplied "
            "transaction-date partition. Check the alert metadata."
        )

        st.stop()

    st.session_state["investigation"] = {
        "mode": input_mode,
        "transaction_id": submitted_transaction_id,
        "window_hours": input_window_hours,
        "alert_sender": submitted_alert_sender,
        "alert_receiver": submitted_alert_receiver,
        "alert_timestamp": submitted_alert_timestamp,
        "raw_edges": submitted_raw_edges
    }


# ============================================================
# REQUIRE AN ACTIVE INVESTIGATION
# ============================================================

investigation = st.session_state.get(
    "investigation"
)

required_investigation_values = {
    "mode",
    "transaction_id",
    "window_hours",
    "alert_sender",
    "alert_receiver",
    "alert_timestamp",
    "raw_edges"
}

if (
    not investigation
    or not required_investigation_values.issubset(
        investigation
    )
):

    st.info(
        "Enter the investigation details and click "
        "'Run investigation' to begin."
    )

    st.stop()


# ============================================================
# ACTIVE INVESTIGATION VALUES
# ============================================================

transaction_id = (
    investigation["transaction_id"]
)

window_hours = (
    investigation["window_hours"]
)

raw_edges = investigation["raw_edges"]

alert = raw_edges[
    raw_edges["edge_role"]
    == "ALERT"
].iloc[0]

alert_sender = investigation["alert_sender"]

alert_receiver = investigation["alert_receiver"]

alert_timestamp = investigation["alert_timestamp"]

alert_amount = float(
    alert["amount_paid"]
)

alert_currency = alert[
    "payment_currency"
]

alert_payment_format = alert[
    "payment_format"
]


# ============================================================
# BUILD GRAPH DATA
# ============================================================

display_edges = (
    build_display_edges(
        raw_edges
    )
)

nodes = build_nodes(
    display_edges,
    alert_sender,
    alert_receiver
)


# ============================================================
# ALERT SUMMARY
# ============================================================

st.subheader(
    "Selected alert"
)

st.markdown(
    f"""
`{alert_sender}` **→** `{alert_receiver}`

**Transaction ID:** `{transaction_id}`  
**Timestamp:** {alert_timestamp}  
**Amount:** {alert_amount:,.2f} {alert_currency}  
**Payment format:** {alert_payment_format}  
**Investigation window:** ±{window_hours} hours
"""
)


# ============================================================
# LEGEND
# ============================================================

st.markdown(
    """
**Legend:**  
🟠 Alert sender &nbsp;&nbsp;
🔴 Alert receiver &nbsp;&nbsp;
🟢 Upstream account &nbsp;&nbsp;
🔵 Downstream account
"""
)


# ============================================================
# NETWORK
# ============================================================

render_network(
    nodes,
    display_edges
)


# ============================================================
# CASE SUMMARY
# ============================================================

st.subheader(
    "Case summary"
)

case_metrics = calculate_case_metrics(
    nodes,
    alert_sender,
    alert_receiver
)


col1, col2, col3, col4 = (
    st.columns(4)
)

with col1:

    st.metric(
        "Connected accounts",
        case_metrics["connected_accounts"]
    )

with col2:

    st.metric(
        "Upstream accounts",
        case_metrics["upstream_accounts"]
    )

with col3:

    st.metric(
        "Downstream accounts",
        case_metrics["downstream_accounts"]
    )

with col4:

    st.metric(
        "Display edges",
        len(
            display_edges
        )
    )


# ============================================================
# INVESTIGATION WINDOW SENSITIVITY
# ============================================================

st.subheader(
    "Investigation window sensitivity"
)

sensitivity = build_window_sensitivity(
    raw_edges,
    alert_sender,
    alert_receiver,
    window_hours
)

st.dataframe(
    sensitivity,
    use_container_width=True,
    hide_index=True
)

chart_data = sensitivity[
    ["Window (hours)", "Connected accounts"]
].copy()
chart_data["Investigation window"] = (
    chart_data["Window (hours)"].map(
        lambda hours: f"{hours}h"
    )
)

st.bar_chart(
    chart_data.set_index(
        "Investigation window"
    )["Connected accounts"]
)


# ============================================================
# RELATIONSHIP SUMMARY
# ============================================================

st.subheader(
    "Connected relationships"
)


for _, edge in display_edges.iterrows():

    if edge["edge_role"] == "ALERT":
        continue

    st.markdown(
        f"""
**{edge['edge_role']}**

`{edge['source_account_key']}`
→
`{edge['target_account_key']}`

{edge['transaction_count']} transaction(s) ·
{edge['total_amount']:,.2f}
{edge['payment_currency']} ·
{edge['payment_formats']} ·
{int(edge['closest_gap_minutes'])} minute closest gap
"""
    )


# ============================================================
# OPTIONAL TRANSACTION DRILL-DOWN
# ============================================================

with st.expander(
    "Underlying transactions"
):

    columns_to_show = [
        "edge_role",
        "source_account_key",
        "target_account_key",
        "transaction_timestamp",
        "amount_paid",
        "payment_currency",
        "payment_format",
        "gap_minutes",
        "transaction_id"
    ]

    st.dataframe(
        raw_edges[
            columns_to_show
        ],
        use_container_width=True,
        hide_index=True
    )
