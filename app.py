import streamlit as st
import requests
import time
import os

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(page_title="iSpot Impressions Agent", page_icon="TV", layout="centered")
st.title("iSpot Impressions Agent")
st.caption("Ask anything about Locality's TV ad delivery in plain English -- no technical knowledge needed.")

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
HOST = st.secrets["DATABRICKS_HOST"].rstrip("/")
TOKEN = st.secrets["DATABRICKS_TOKEN"]
WAREHOUSE_ID = "5e7f9a39557c84c1"
ENDPOINT = f"{HOST}/serving-endpoints/ispot-id-impressions-agent/invocations"

# ---------------------------------------------------------------------------
# SQL execution via Statement Execution API (no Spark required)
# ---------------------------------------------------------------------------
def run_sql(query: str) -> str:
    """Execute a SQL query via the Databricks Statement Execution API."""
    resp = requests.post(
        f"{HOST}/api/2.0/sql/statements",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "warehouse_id": WAREHOUSE_ID,
            "statement": query,
            "wait_timeout": "30s",
            "on_wait_timeout": "CONTINUE",
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
        },
        timeout=60,
    )
    data = resp.json()
    statement_id = data.get("statement_id")

    # Poll if still running
    for _ in range(30):
        state = data.get("status", {}).get("state", "")
        if state in ("SUCCEEDED", "FAILED", "CANCELED", "CLOSED"):
            break
        time.sleep(2)
        data = requests.get(
            f"{HOST}/api/2.0/sql/statements/{statement_id}",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=30,
        ).json()

    if data.get("status", {}).get("state") != "SUCCEEDED":
        return f"Query failed: {data.get('status', {}).get('error', {}).get('message', 'unknown error')}"

    columns = [c["name"] for c in data.get("manifest", {}).get("schema", {}).get("columns", [])]
    rows = data.get("result", {}).get("data_array", [])
    if not rows:
        return "The query returned no results."

    # Format as plain text table
    lines = ["  ".join(columns)]
    lines += ["  ".join(str(v) for v in row) for row in rows[:50]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent call
# ---------------------------------------------------------------------------
def ask_agent(message: str) -> str:
    """Send a message to the iSpot agent endpoint and return the answer."""
    resp = requests.post(
        ENDPOINT,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        json={"messages": [{"role": "user", "content": message}]},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    pred = data.get("predictions", data)

    if isinstance(pred, str):
        return pred
    if isinstance(pred, dict):
        msgs = pred.get("messages", [])
        if msgs:
            return msgs[-1].get("content", str(pred))
    if isinstance(pred, list) and pred:
        item = pred[0]
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            msgs = item.get("messages", [])
            if msgs:
                return msgs[-1].get("content", str(item))
            return item.get("content", str(item))
    return str(data)


# ---------------------------------------------------------------------------
# Example questions
# ---------------------------------------------------------------------------
EXAMPLES = [
    "How many total impressions have been delivered?",
    "What percentage came from TV vs other devices?",
    "Which cities saw the most impressions in the last 30 days?",
    "What share of households were only reached through TV?",
    "Who are the top 5 publishers by impression volume?",
    "How does Freewheel compare to other DSPs?",
]

# ---------------------------------------------------------------------------
# Chat UI
# ---------------------------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": "Hi! I can answer questions about Locality's iSpot TV and streaming ad impressions. What would you like to know?"}
    ]

# Render conversation history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Show example buttons before first user message
if not any(m["role"] == "user" for m in st.session_state.messages):
    st.markdown("**Not sure what to ask? Try one of these:**")
    for example in EXAMPLES:
        if st.button(example, use_container_width=True):
            st.session_state["pending"] = example
            st.rerun()

# Accept input (button click or typed)
prompt = st.session_state.pop("pending", None) or st.chat_input("Ask a question in plain English...")

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Looking that up..."):
            try:
                answer = ask_agent(prompt)
            except Exception as e:
                answer = f"Something went wrong: {e}"
        st.markdown(answer)

    st.session_state.messages.append({"role": "assistant", "content": answer})
