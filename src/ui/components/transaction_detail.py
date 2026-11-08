"""Human-readable display of supplied transaction fields; no data retrieval."""

from datetime import datetime

import streamlit as st

from src.ui.components.formatting import format_number, numeric_value

UNAVAILABLE = "Not supplied"


def amount_text(amount, currency) -> str:
    if numeric_value(amount) is None:
        return "Amount not supplied"
    formatted = format_number(amount, digits=2, grouping=True)
    formatted = formatted.removesuffix(".00")
    return f"US${formatted}" if currency in {"US Dollar", "USD"} else f"{formatted} {currency or '(currency not supplied)'}"


def time_text(value) -> str:
    if not value:
        return UNAVAILABLE
    try:
        return datetime.fromisoformat(str(value)).strftime("%d %b %Y, %H:%M:%S UTC")
    except ValueError:
        return str(value)


def render_transaction_details(fields: dict, *, title: str = "Selected supporting transaction") -> None:
    """Callers provide roles and offsets only when established by source context."""
    st.subheader(title)
    with st.container(border=True):
        st.text(fields.get("txn_id") or UNAVAILABLE)
        st.markdown(f"**{amount_text(fields.get('amount'), fields.get('currency'))}**")
        st.text(f"{fields.get('sender') or UNAVAILABLE} → {fields.get('receiver') or UNAVAILABLE}")
        st.text(f"Currency: {fields.get('currency') or UNAVAILABLE}")
        st.text(f"Time: {time_text(fields.get('ts'))}")
        st.text(f"Type: {fields.get('role') or UNAVAILABLE}")
        offset = numeric_value(fields.get("offset_minutes"))
        if offset is None:
            st.caption("Time relative to flagged transaction: not supplied")
        elif offset == 0:
            st.text("Time relative to flagged transaction: 0 minutes (same time)")
        else:
            st.text(f"Time {'before' if offset < 0 else 'after'} flagged transaction: {format(abs(offset).normalize(), 'f')} minutes")
        if fields.get("payment_format"):
            st.text(f"Payment format: {fields['payment_format']}")
