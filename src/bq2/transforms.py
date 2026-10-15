"""Pure deterministic transformations for BQ2 investigation data."""

import time

import pandas as pd

SENSITIVITY_WINDOWS_HOURS = (
    6,
    12,
    24,
    30,
    48,
    72,
)


def normalize_transaction_timestamp(timestamp):
    """Normalize a dashboard timestamp or return ``None`` when invalid."""

    try:
        parsed = time.strptime(
            timestamp.strip(),
            "%Y-%m-%d %H:%M:%S",
        )
    except ValueError:
        return None

    return time.strftime(
        "%Y-%m-%d %H:%M:%S",
        parsed,
    )


def build_display_edges(raw_edges: pd.DataFrame) -> pd.DataFrame:
    """Aggregate transaction evidence into currency-safe display edges."""

    if raw_edges.empty:
        return pd.DataFrame()

    df = raw_edges.copy()

    df["amount_paid"] = pd.to_numeric(
        df["amount_paid"],
        errors="coerce",
    )

    df["gap_minutes"] = pd.to_numeric(
        df["gap_minutes"],
        errors="coerce",
    )

    # Currency stays in the grouping key. Never combine different currencies.
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
                "payment_currency",
            ],
            dropna=False,
        )
        .agg(
            transaction_count=("transaction_id", "count"),
            total_amount=("amount_paid", "sum"),
            closest_gap_minutes=("gap_minutes", "min"),
            payment_formats=(
                "payment_format",
                lambda values: ", ".join(
                    sorted(
                        {
                            str(value)
                            for value in values
                            if pd.notna(value)
                        }
                    )
                ),
            ),
        )
        .reset_index()
    )

    role_order = {
        "UPSTREAM": 1,
        "ALERT": 2,
        "DOWNSTREAM": 3,
    }

    grouped["role_order"] = grouped["edge_role"].map(role_order)

    return grouped.sort_values(
        [
            "role_order",
            "closest_gap_minutes",
        ]
    ).drop(columns=["role_order"])


def build_window_sensitivity(
    raw_edges,
    alert_sender,
    alert_receiver,
    queried_window_hours,
):
    """Calculate account and transaction-edge counts for available windows."""

    edges = raw_edges.copy()
    edges["gap_minutes"] = pd.to_numeric(
        edges["gap_minutes"],
        errors="coerce",
    )

    alert_accounts = {
        alert_sender,
        alert_receiver,
    }
    rows = []

    for window_hours in SENSITIVITY_WINDOWS_HOURS:
        if window_hours > queried_window_hours:
            continue

        window_edges = edges[
            edges["gap_minutes"] <= window_hours * 60
        ]

        connected_edges = window_edges[
            window_edges["edge_role"].isin(
                ["UPSTREAM", "DOWNSTREAM"]
            )
        ]

        upstream_accounts = set(
            connected_edges.loc[
                connected_edges["edge_role"] == "UPSTREAM",
                "source_account_key",
            ].dropna()
        ) - alert_accounts

        downstream_accounts = set(
            connected_edges.loc[
                connected_edges["edge_role"] == "DOWNSTREAM",
                "target_account_key",
            ].dropna()
        ) - alert_accounts

        connected_accounts = upstream_accounts | downstream_accounts

        rows.append(
            {
                "Window (hours)": window_hours,
                "Upstream accounts": len(upstream_accounts),
                "Downstream accounts": len(downstream_accounts),
                "Connected accounts": len(connected_accounts),
                # The ALERT transaction is not a connected transaction edge.
                "Connected transaction edges": len(connected_edges),
            }
        )

    return pd.DataFrame(rows)


def build_nodes(
    display_edges,
    alert_sender,
    alert_receiver,
):
    """Build graph-ready node records without performing any rendering."""

    node_data = {}

    for _, edge in display_edges.iterrows():
        source = edge["source_account_key"]
        target = edge["target_account_key"]

        if source not in node_data:
            node_data[source] = {
                "account_key": source,
                "entity_id": edge["source_entity_id"],
                "entity_name": edge["source_entity_name"],
                "roles": set(),
            }

        if target not in node_data:
            node_data[target] = {
                "account_key": target,
                "entity_id": edge["target_entity_id"],
                "entity_name": edge["target_entity_name"],
                "roles": set(),
            }

        role = edge["edge_role"]

        if role == "UPSTREAM":
            node_data[source]["roles"].add("UPSTREAM")
        elif role == "DOWNSTREAM":
            node_data[target]["roles"].add("DOWNSTREAM")

    # Alert roles take precedence visually.
    node_data[alert_sender]["roles"].add("ALERT_SENDER")
    node_data[alert_receiver]["roles"].add("ALERT_RECEIVER")

    return list(node_data.values())


def calculate_case_metrics(
    nodes,
    alert_sender,
    alert_receiver,
):
    """Calculate the distinct account counts shown in the case summary."""

    connected_accounts = set()
    upstream_accounts = set()
    downstream_accounts = set()

    for node in nodes:
        account = node["account_key"]
        roles = node["roles"]

        if account not in [alert_sender, alert_receiver]:
            connected_accounts.add(account)

        if "UPSTREAM" in roles:
            upstream_accounts.add(account)

        if "DOWNSTREAM" in roles:
            downstream_accounts.add(account)

    return {
        "connected_accounts": len(connected_accounts),
        "upstream_accounts": len(upstream_accounts),
        "downstream_accounts": len(downstream_accounts),
    }
