"""
Streamlit UI — Solana Wallet Historical Balance Checker
Run with:  streamlit run app.py

Requires HELIUS_API_KEY in a .env file (see .env.example).
USD prices from DexScreener (free, no key). Values are at current prices.
"""

import os
from datetime import date, datetime, timezone

import streamlit as st
from dotenv import load_dotenv

from solana_client import WSOL_MINT, SolanaClientError, SolanaHistoricalClient

load_dotenv()

# ─────────────────────────────────────── page config
st.set_page_config(
    page_title="Solana Balance Checker",
    page_icon="◎",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────── helpers
def fmt_price(price: float) -> str:
    """Human-readable USD price with appropriate decimal depth."""
    if price == 0:
        return "$0.00"
    if price >= 1_000:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:.2f}"
    if price >= 0.01:
        return f"${price:.4f}"
    if price >= 0.000_001:
        return f"${price:.8f}"
    return f"${price:.2e}"

def fmt_usd(value: float) -> str:
    return f"${value:,.2f}"

def fmt_balance(balance: float) -> str:
    return f"{balance:,.4f}"

# ─────────────────────────────────────── API key (env only)
api_key: str = os.getenv("HELIUS_API_KEY", "").strip()

# ─────────────────────────────────────── sidebar
with st.sidebar:
    st.markdown("## ◎ Solana Balance Checker")
    st.markdown("---")
    # if api_key:
    #     st.success("Helius API key loaded.", icon="✅")
    # else:
    #     st.error("Helius API key missing.", icon="🔑")
    #     st.markdown(
    #         "**Setup:**\n"
    #         "1. Get a free key at [helius.dev](https://helius.dev)\n"
    #         "2. Copy `.env.example` → `.env`\n"
    #         "3. Add `HELIUS_API_KEY=your_key`\n"
    #         "4. Restart the app"
    #     )
    st.markdown("---")
    st.markdown(
        "**How it works**\n\n"
        "Transaction history is scanned backwards to find the wallet state "
        "at the end of the selected date. Each token account is checked the same way.\n\n"
        "**Prices** are fetched live from DexScreener — they reflect current "
        "market rates, not rates on the historical date.\n\n"
        "**Limitations**\n"
        "- Wallets with many tokens will take longer.\n"
        "- Archival data older than ~18 months may be unavailable on the free Helius tier."
    )

# ─────────────────────────────────────── main header
st.markdown("# ◎ Solana Wallet Balance Checker")
st.markdown("Look up **SOL and SPL token balances** for any wallet on any historical date.")

if not api_key:
    st.warning(
        "No Helius API key found. Add `HELIUS_API_KEY=your_key` to a `.env` file and restart.",
        icon="🔑",
    )
    st.stop()

# ─────────────────────────────────────── inputs
col_addr, col_date = st.columns([3, 1])
with col_addr:
    wallet = st.text_input(
        "Wallet Address",
        placeholder="e.g. 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
    )
with col_date:
    target_date: date = st.date_input(
        "Date",
        value=date.today(),
        min_value=date(2020, 3, 16),
        max_value=date.today(),
        help="Snapshot taken at 23:59:59 UTC on this date.",
    )

run = st.button("Check Balance", type="primary", use_container_width=True)

if run:
    if not wallet:
        st.error("Please enter a wallet address.")
        st.stop()
    if not SolanaHistoricalClient.is_valid_address(wallet):
        st.error("Invalid Solana address — please double-check it.")
        st.stop()

# ─────────────────────────────────────── main flow
if not (run and wallet and SolanaHistoricalClient.is_valid_address(wallet)):
    st.stop()

client = SolanaHistoricalClient(api_key)
short  = f"{wallet[:6]}…{wallet[-4:]}"

st.markdown("---")

# ── Step 1: SOL balance
with st.spinner(f"Fetching SOL balance for {short}…"):
    try:
        sol_result = client.get_sol_balance_at_date(wallet, target_date)
    except SolanaClientError as exc:
        st.error(f"Could not fetch SOL balance: {exc}")
        st.stop()

# ── Step 2: Token balances
st.markdown("**Scanning token accounts…**")
progress = st.progress(0.0, text="Starting…")

def _cb(pct: float, msg: str) -> None:
    progress.progress(min(pct, 1.0), text=msg)

try:
    tokens = client.get_token_balances_at_date(wallet, target_date, progress_callback=_cb)
except SolanaClientError as exc:
    progress.empty()
    st.error(f"Could not fetch token balances: {exc}")
    st.stop()

progress.empty()

# ── Step 3: Prices + metadata
is_today = target_date >= date.today()
price_spinner_msg = (
    "Fetching live prices and token metadata…"
    if is_today else
    f"Fetching historical prices for {target_date} (end-of-day) and token metadata…"
)
with st.spinner(price_spinner_msg):
    token_mints = [t["mint"] for t in tokens]
    market = client.fetch_market_data(token_mints, target_date)

sol_mkt   = market.get(WSOL_MINT, {})
sol_price = sol_mkt.get("price", 0.0)

# ── Attach price/symbol/name/availability to every token
for t in tokens:
    m = market.get(t["mint"], {})
    t["price"]           = m.get("price", 0.0)
    t["usd_value"]       = (t["balance"] or 0.0) * t["price"]
    t["price_available"] = m.get("price_available", False)
    t["price_source"]    = m.get("price_source", "")
    t["symbol"]          = m.get("symbol") or t.get("symbol") or ""
    t["name"]            = m.get("name")   or t.get("name")   or ""

sol_bal = sol_result.get("balance") or 0.0
sol_usd = sol_bal * sol_price

# Tokens with > $1 value, sorted by USD value descending
visible = sorted(
    [t for t in tokens if t["usd_value"] > 1.0],
    key=lambda x: x["usd_value"],
    reverse=True,
)
hidden = [t for t in tokens if t["usd_value"] <= 1.0]

total_token_usd = sum(t["usd_value"] for t in visible)
grand_total     = sol_usd + total_token_usd

# ─────────────────────────────────────── snapshot header
snap_label = f"Snapshot date: {target_date}"
if is_today:
    snap_label = "Live snapshot (today)"
if sol_result.get("actual_tx_time"):
    last_tx = datetime.fromtimestamp(sol_result["actual_tx_time"], tz=timezone.utc)
    snap_label += f"  ·  last tx before date: {last_tx.strftime('%Y-%m-%d %H:%M')} UTC"

# Price note
sol_src = sol_mkt.get("price_source", "")
if is_today:
    price_note = f"Prices: live ({sol_src or 'DexScreener'})"
else:
    price_note = f"Prices: historical end-of-day for {target_date} (CryptoCompare)"

st.markdown(f"### Portfolio for `{wallet}`")
st.caption(f"{snap_label}  ·  {price_note}")

# ─────────────────────────────────────── summary cards
c1, c2, c3 = st.columns(3)
c1.metric("SOL Holdings",   f"{fmt_balance(sol_bal)} SOL",  fmt_usd(sol_usd))
c2.metric("Token Holdings", f"{len(visible)} token(s)",     fmt_usd(total_token_usd))
c3.metric("Portfolio Total", "",                             fmt_usd(grand_total))

st.markdown("---")

# ─────────────────────────────────────── holdings table
st.markdown("### Holdings")

def _price_cell(price: float, available: bool) -> str:
    """Format price, showing '—' when no historical data was found."""
    if not available and not is_today:
        return "—"
    return fmt_price(price)

def _value_cell(usd: float, available: bool) -> str:
    if not available and not is_today:
        return "—"
    return fmt_usd(usd)

# Build rows: SOL first, then tokens sorted by USD value
rows = []

sol_price_ok = sol_mkt.get("price_available", False) or is_today
rows.append({
    "Asset":       "SOL",
    "Name":        "Solana",
    "Balance":     fmt_balance(sol_bal),
    "Price (USD)": _price_cell(sol_price, sol_price_ok),
    "Value (USD)": _value_cell(sol_usd,   sol_price_ok),
    "_usd":        sol_usd,
})

for t in visible:
    sym  = t["symbol"] or t["mint"][:8] + "…"
    name = t["name"]   or "—"
    rows.append({
        "Asset":       sym,
        "Name":        name,
        "Balance":     fmt_balance(t["balance"] or 0.0),
        "Price (USD)": _price_cell(t["price"],     t["price_available"]),
        "Value (USD)": _value_cell(t["usd_value"], t["price_available"]),
        "_usd":        t["usd_value"],
    })

# Build clean HTML table for full display control
def build_table(rows: list) -> str:
    th_style = (
        "padding: 10px 14px; text-align: {align}; "
        "border-bottom: 2px solid #444; white-space: nowrap; font-size: 0.85rem; "
        "color: #aaa; text-transform: uppercase; letter-spacing: 0.05em;"
    )
    td_style = (
        "padding: 9px 14px; text-align: {align}; "
        "border-bottom: 1px solid #2a2a2a; font-size: 0.95rem; white-space: nowrap;"
    )

    header = (
        "<tr>"
        f"<th style='{th_style.format(align='left')}'>Asset</th>"
        f"<th style='{th_style.format(align='left')}'>Name</th>"
        f"<th style='{th_style.format(align='right')}'>Balance</th>"
        f"<th style='{th_style.format(align='right')}'>Price (USD)</th>"
        f"<th style='{th_style.format(align='right')}'>Value (USD)</th>"
        "</tr>"
    )

    body = ""
    for i, r in enumerate(rows):
        bg = "#1a1a1a" if i % 2 == 0 else "#141414"
        is_sol = r["Asset"] == "SOL"
        weight = "font-weight: 600;" if is_sol else ""
        body += (
            f"<tr style='background:{bg};'>"
            f"<td style='{td_style.format(align='left')} {weight}'>{r['Asset']}</td>"
            f"<td style='{td_style.format(align='left')} color:#888;'>{r['Name']}</td>"
            f"<td style='{td_style.format(align='right')}'>{r['Balance']}</td>"
            f"<td style='{td_style.format(align='right')} color:#888;'>{r['Price (USD)']}</td>"
            f"<td style='{td_style.format(align='right')} {weight}'>{r['Value (USD)']}</td>"
            "</tr>"
        )

    return (
        "<div style='overflow-x: auto; border-radius: 8px; border: 1px solid #2a2a2a;'>"
        "<table style='width:100%; border-collapse: collapse; font-family: monospace;'>"
        f"<thead>{header}</thead>"
        f"<tbody>{body}</tbody>"
        "</table></div>"
    )

if rows:
    st.markdown(build_table(rows), unsafe_allow_html=True)
else:
    st.info("No holdings found for this wallet on this date.")

# ─────────────────────────────────────── hidden tokens
if hidden:
    with st.expander(f"Tokens under $1  ({len(hidden)})"):
        small_rows = []
        for t in sorted(hidden, key=lambda x: x["usd_value"], reverse=True):
            sym  = t["symbol"] or t["mint"][:8] + "…"
            name = t["name"]   or "—"
            small_rows.append({
                "Asset":       sym,
                "Name":        name,
                "Balance":     fmt_balance(t["balance"] or 0.0),
                "Price (USD)": fmt_price(t["price"]),
                "Value (USD)": fmt_usd(t["usd_value"]),
                "_usd":        t["usd_value"],
            })
        st.markdown(build_table(small_rows), unsafe_allow_html=True)

# ─────────────────────────────────────── explorer links
st.markdown("---")
st.markdown(
    f"[View on Solscan ↗](https://solscan.io/account/{wallet})"
    f"&nbsp; · &nbsp;"
    f"[View on Solana Explorer ↗](https://explorer.solana.com/address/{wallet})"
)
