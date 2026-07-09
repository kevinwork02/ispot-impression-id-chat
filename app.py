import streamlit as st
import requests
import time

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(page_title="iSpot Impressions Agent", layout="centered")
st.title("iSpot Impressions Agent")
st.caption("Ask anything about Locality's TV ad delivery in plain English -- no technical knowledge needed.")

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
HOST = st.secrets["DATABRICKS_HOST"].rstrip("/")
TOKEN = st.secrets["DATABRICKS_TOKEN"]
WAREHOUSE_ID = "5e7f9a39557c84c1"
LLM_ENDPOINT = f"{HOST}/serving-endpoints/databricks-meta-llama-3-3-70b-instruct/invocations"

# ---------------------------------------------------------------------------
# SQL generation prompt
# ---------------------------------------------------------------------------
SQL_PROMPT = """You are a SQL expert. Generate a single valid Spark SQL SELECT statement for the table locality_dev.silver.ispot_streaming_impressions.

Table columns:
- locality_hh_id (STRING): household ID
- iuld_uuid (STRING): unique impression ID
- advertiser_name (STRING): always 'CoxReps OTT Partner Integration' -- not a useful filter
- event_timestamp_utc (TIMESTAMP): event time UTC
- media_market (STRING): geographic market e.g. 'Dallas--Fort Worth--Arlington, TX'
- publisher_name_mapped (STRING): publisher name
- device_category (STRING): IMPORTANT -- use 'television' (not 'CTV') for TV; other values: 'Other', 'desktop', 'smartphone', 'tablet', 'console'
- data_date (DATE): date of record
- ott_dsp (STRING): DSP name e.g. 'freewheel', 'tradedesk'
- impression_date_pst (DATE): impression date PST -- preferred for date filters
- delivery_date_pst (DATE): delivery date PST
- ip_address (STRING): IP address

Rules:
- Use current_date() for today's date
- For date ranges use impression_date_pst e.g. WHERE impression_date_pst >= date_sub(current_date(), 30)
- For TV always filter device_category = 'television' never 'CTV'
- For household exclusion use EXCEPT not NOT IN
- Always include LIMIT 200 unless the query is a pure aggregate (COUNT, SUM etc.)

Return ONLY the SQL query. No explanation, no markdown fences."""

SUMMARY_PROMPT = "You are a helpful data analyst at Locality. Summarize the following query results in 2-4 plain English sentences. Be specific with numbers. Do not mention SQL."

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def call_llm(system: str, user: str) -> str:
    resp = requests.post(
        LLM_ENDPOINT,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        json={"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def run_sql(query: str) -> str:
    resp = requests.post(
        f"{HOST}/api/2.0/sql/statements",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={"warehouse_id": WAREHOUSE_ID, "statement": query,
              "wait_timeout": "30s", "on_wait_timeout": "CONTINUE",
              "disposition": "INLINE", "format": "JSON_ARRAY"},
        timeout=60,
    )
    data = resp.json()
    sid = data.get("statement_id")
    for _ in range(30):
        state = data.get("status", {}).get("state", "")
        if state in ("SUCCEEDED", "FAILED", "CANCELED", "CLOSED"):
            break
        time.sleep(2)
        data = requests.get(f"{HOST}/api/2.0/sql/statements/{sid}",
                            headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30).json()
    if data.get("status", {}).get("state") != "SUCCEEDED":
        err = data.get("status", {}).get("error", {}).get("message", "unknown error")
        return f"Query failed: {err}"
    columns = [c["name"] for c in data.get("manifest", {}).get("schema", {}).get("columns", [])]
    rows = data.get("result", {}).get("data_array", [])
    if not rows:
        return "No results returned."
    lines = ["  ".join(columns)] + ["  ".join(str(v) for v in row) for row in rows[:100]]
    return "\n".join(lines)


def answer(question: str) -> str:
    try:
        sql = call_llm(SQL_PROMPT, question)
        sql = sql.strip().strip("`").replace("sql\n", "").strip()
        results = run_sql(sql)
        return call_llm(SUMMARY_PROMPT, f"Question: {question}\n\nResults:\n{results}")
    except Exception as e:
        return f"Something went wrong: {e}"


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

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if not any(m["role"] == "user" for m in st.session_state.messages):
    st.markdown("**Not sure what to ask? Try one of these:**")
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True):
            st.session_state["pending"] = ex
            st.rerun()

prompt = st.session_state.pop("pending", None) or st.chat_input("Ask a question in plain English...")

if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("Looking that up..."):
            reply = answer(prompt)
        st.markdown(reply)
    st.session_state.messages.append({"role": "assistant", "content": reply})
