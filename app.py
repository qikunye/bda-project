import re
import time

import boto3
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from pyvis.network import Network

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

SENSITIVITY_WINDOWS_HOURS = (
    6,
    12,
    24,
    30,
    48,
    72
)


# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="AML Investigation Network",
    layout="wide"
)


# ============================================================
# AWS CLIENT
# ============================================================

athena = boto3.client(
    "athena",
    region_name=AWS_REGION
)


# ============================================================
# ATHENA HELPER
# ============================================================

def run_athena_query(query: str) -> pd.DataFrame:

    response = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={
            "Database": ATHENA_DATABASE
        },
        ResultConfiguration={
            "OutputLocation": ATHENA_OUTPUT
        }
    )

    query_execution_id = response[
        "QueryExecutionId"
    ]

    while True:

        response = athena.get_query_execution(
            QueryExecutionId=query_execution_id
        )

        status = response[
            "QueryExecution"
        ][
            "Status"
        ][
            "State"
        ]

        if status == "SUCCEEDED":
            break

        if status in [
            "FAILED",
            "CANCELLED"
        ]:

            reason = response[
                "QueryExecution"
            ][
                "Status"
            ].get(
                "StateChangeReason",
                "Unknown Athena error"
            )

            raise RuntimeError(
                f"Athena query {status}: {reason}"
            )

        time.sleep(0.5)


    # --------------------------------------------------------
    # Get results with pagination
    # --------------------------------------------------------

    rows = []

    paginator = athena.get_paginator(
        "get_query_results"
    )

    first_page = True
    columns = []

    for page in paginator.paginate(
        QueryExecutionId=query_execution_id
    ):

        result_set = page["ResultSet"]

        if first_page:

            columns = [
                col["Name"]
                for col in result_set[
                    "ResultSetMetadata"
                ][
                    "ColumnInfo"
                ]
            ]

        page_rows = result_set["Rows"]

        # Athena returns the header as first row
        if first_page:
            page_rows = page_rows[1:]
            first_page = False

        for row in page_rows:

            values = [
                item.get(
                    "VarCharValue",
                    None
                )
                for item in row["Data"]
            ]

            # Athena can omit trailing null columns
            while len(values) < len(columns):
                values.append(None)

            rows.append(values)


    return pd.DataFrame(
        rows,
        columns=columns
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


def normalize_transaction_timestamp(timestamp):

    try:
        parsed = time.strptime(
            timestamp.strip(),
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return None

    return time.strftime(
        "%Y-%m-%d %H:%M:%S",
        parsed
    )


# ============================================================
# GET ALERT TRANSACTION
# ============================================================

@st.cache_data(
    ttl=300,
    show_spinner=False
)
def get_alert(transaction_id):

    query = f"""
    SELECT
        transaction_id,

        source_account_key,
        target_account_key,

        source_entity_id,
        source_entity_name,

        target_entity_id,
        target_entity_name,

        transaction_timestamp,

        amount_paid,
        payment_currency,
        payment_format

    FROM {ATHENA_TABLE}

    WHERE transaction_id = '{transaction_id}'

    LIMIT 1
    """

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

    query = f"""
    WITH parameters AS (

        SELECT
            '{alert_transaction_id}'
                AS alert_transaction_id,

            '{alert_sender}'
                AS alert_sender,

            '{alert_receiver}'
                AS alert_receiver,

            TIMESTAMP '{alert_timestamp}'
                AS alert_timestamp,

            {window_minutes}
                AS window_minutes
    ),

    alert_edge AS (

        SELECT
            'ALERT' AS edge_role,

            g.source_account_key,
            g.target_account_key,

            g.source_entity_id,
            g.source_entity_name,

            g.target_entity_id,
            g.target_entity_name,

            g.transaction_id,
            g.transaction_timestamp,

            0 AS gap_minutes,

            g.amount_paid,
            g.payment_currency,
            g.payment_format

        FROM {ATHENA_TABLE} g

        CROSS JOIN parameters p

        WHERE
            g.transaction_id =
            p.alert_transaction_id

            AND
            g.txn_date =
            CAST(
                p.alert_timestamp
                AS DATE
            )
    ),

    upstream_edges AS (

        SELECT
            'UPSTREAM' AS edge_role,

            g.source_account_key,
            g.target_account_key,

            g.source_entity_id,
            g.source_entity_name,

            g.target_entity_id,
            g.target_entity_name,

            g.transaction_id,
            g.transaction_timestamp,

            DATE_DIFF(
                'minute',
                g.transaction_timestamp,
                p.alert_timestamp
            ) AS gap_minutes,

            g.amount_paid,
            g.payment_currency,
            g.payment_format

        FROM {ATHENA_TABLE} g

        CROSS JOIN parameters p

        WHERE
            g.target_account_key =
            p.alert_sender

            AND g.txn_date BETWEEN

                CAST(
                    DATE_ADD(
                        'minute',
                        -p.window_minutes,
                        p.alert_timestamp
                    )
                    AS DATE
                )

                AND

                CAST(
                    p.alert_timestamp
                    AS DATE
                )

            AND
            g.transaction_timestamp
            <= p.alert_timestamp

            AND
            DATE_DIFF(
                'minute',
                g.transaction_timestamp,
                p.alert_timestamp
            )
            <= p.window_minutes
    ),

    downstream_edges AS (

        SELECT
            'DOWNSTREAM'
                AS edge_role,

            g.source_account_key,
            g.target_account_key,

            g.source_entity_id,
            g.source_entity_name,

            g.target_entity_id,
            g.target_entity_name,

            g.transaction_id,
            g.transaction_timestamp,

            DATE_DIFF(
                'minute',
                p.alert_timestamp,
                g.transaction_timestamp
            ) AS gap_minutes,

            g.amount_paid,
            g.payment_currency,
            g.payment_format

        FROM {ATHENA_TABLE} g

        CROSS JOIN parameters p

        WHERE
            g.source_account_key =
            p.alert_receiver

            AND g.txn_date BETWEEN

                CAST(
                    p.alert_timestamp
                    AS DATE
                )

                AND

                CAST(
                    DATE_ADD(
                        'minute',
                        p.window_minutes,
                        p.alert_timestamp
                    )
                    AS DATE
                )

            AND
            g.transaction_timestamp
            >= p.alert_timestamp

            AND
            DATE_DIFF(
                'minute',
                p.alert_timestamp,
                g.transaction_timestamp
            )
            <= p.window_minutes
    )

    SELECT *
    FROM alert_edge

    UNION ALL

    SELECT *
    FROM upstream_edges

    UNION ALL

    SELECT *
    FROM downstream_edges
    """

    return run_athena_query(query)


# ============================================================
# DISPLAY EDGE AGGREGATION
# ============================================================

def build_display_edges(
    raw_edges: pd.DataFrame
):

    if raw_edges.empty:
        return pd.DataFrame()

    df = raw_edges.copy()

    df["amount_paid"] = pd.to_numeric(
        df["amount_paid"],
        errors="coerce"
    )

    df["gap_minutes"] = pd.to_numeric(
        df["gap_minutes"],
        errors="coerce"
    )

    # --------------------------------------------------------
    # Currency stays in grouping key.
    # Never combine different currencies.
    # --------------------------------------------------------

    grouped = (
        df.groupby(
            [
                "edge_role",

                "source_account_key",
                "target_account_key",

                "source_entity_id",
                "source_entity_name",

                "target_entity_id",
                "target_entity_name",

                "payment_currency"
            ],
            dropna=False
        )
        .agg(
            transaction_count=(
                "transaction_id",
                "count"
            ),

            total_amount=(
                "amount_paid",
                "sum"
            ),

            closest_gap_minutes=(
                "gap_minutes",
                "min"
            ),

            payment_formats=(
                "payment_format",
                lambda x:
                ", ".join(
                    sorted(
                        {
                            str(v)
                            for v in x
                            if pd.notna(v)
                        }
                    )
                )
            )
        )
        .reset_index()
    )

    role_order = {
        "UPSTREAM": 1,
        "ALERT": 2,
        "DOWNSTREAM": 3
    }

    grouped["role_order"] = (
        grouped["edge_role"]
        .map(role_order)
    )

    grouped = (
        grouped
        .sort_values(
            [
                "role_order",
                "closest_gap_minutes"
            ]
        )
        .drop(
            columns=["role_order"]
        )
    )

    return grouped


# ============================================================
# INVESTIGATION WINDOW SENSITIVITY
# ============================================================

def build_window_sensitivity(
    raw_edges,
    alert_sender,
    alert_receiver,
    queried_window_hours
):

    edges = raw_edges.copy()
    edges["gap_minutes"] = pd.to_numeric(
        edges["gap_minutes"],
        errors="coerce"
    )

    alert_accounts = {
        alert_sender,
        alert_receiver
    }
    rows = []

    for window_hours in SENSITIVITY_WINDOWS_HOURS:

        if window_hours > queried_window_hours:
            continue

        window_edges = edges[
            edges["gap_minutes"]
            <= window_hours * 60
        ]

        connected_edges = window_edges[
            window_edges["edge_role"].isin(
                ["UPSTREAM", "DOWNSTREAM"]
            )
        ]

        upstream_accounts = set(
            connected_edges.loc[
                connected_edges["edge_role"]
                == "UPSTREAM",
                "source_account_key"
            ].dropna()
        ) - alert_accounts

        downstream_accounts = set(
            connected_edges.loc[
                connected_edges["edge_role"]
                == "DOWNSTREAM",
                "target_account_key"
            ].dropna()
        ) - alert_accounts

        connected_accounts = (
            upstream_accounts
            | downstream_accounts
        )

        rows.append({
            "Window (hours)": window_hours,
            "Upstream accounts": len(
                upstream_accounts
            ),
            "Downstream accounts": len(
                downstream_accounts
            ),
            "Connected accounts": len(
                connected_accounts
            ),
            # The ALERT transaction is not a connected transaction edge.
            "Connected transaction edges": len(
                connected_edges
            )
        })

    return pd.DataFrame(rows)


# ============================================================
# BUILD NODE DATA
# ============================================================

def build_nodes(
    display_edges,
    alert_sender,
    alert_receiver
):

    node_data = {}

    for _, edge in display_edges.iterrows():

        source = edge[
            "source_account_key"
        ]

        target = edge[
            "target_account_key"
        ]

        if source not in node_data:

            node_data[source] = {
                "account_key": source,
                "entity_id":
                    edge["source_entity_id"],
                "entity_name":
                    edge["source_entity_name"],
                "roles": set()
            }

        if target not in node_data:

            node_data[target] = {
                "account_key": target,
                "entity_id":
                    edge["target_entity_id"],
                "entity_name":
                    edge["target_entity_name"],
                "roles": set()
            }

        role = edge["edge_role"]

        if role == "UPSTREAM":
            node_data[source][
                "roles"
            ].add(
                "UPSTREAM"
            )

        elif role == "DOWNSTREAM":
            node_data[target][
                "roles"
            ].add(
                "DOWNSTREAM"
            )


    # Alert roles take precedence visually
    node_data[
        alert_sender
    ][
        "roles"
    ].add(
        "ALERT_SENDER"
    )

    node_data[
        alert_receiver
    ][
        "roles"
    ].add(
        "ALERT_RECEIVER"
    )


    return list(
        node_data.values()
    )


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

connected_accounts = set()

upstream_accounts = set()

downstream_accounts = set()


for node in nodes:

    account = node[
        "account_key"
    ]

    roles = node[
        "roles"
    ]

    if account not in [
        alert_sender,
        alert_receiver
    ]:

        connected_accounts.add(
            account
        )

    if "UPSTREAM" in roles:

        upstream_accounts.add(
            account
        )

    if "DOWNSTREAM" in roles:

        downstream_accounts.add(
            account
        )


col1, col2, col3, col4 = (
    st.columns(4)
)

with col1:

    st.metric(
        "Connected accounts",
        len(
            connected_accounts
        )
    )

with col2:

    st.metric(
        "Upstream accounts",
        len(
            upstream_accounts
        )
    )

with col3:

    st.metric(
        "Downstream accounts",
        len(
            downstream_accounts
        )
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
