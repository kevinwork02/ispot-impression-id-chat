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
# Prompts
# ---------------------------------------------------------------------------
SQL_PROMPT = """You are a SQL expert. Generate a single valid Spark SQL SELECT statement for
the table locality_dev.silver.ispot_streaming_impressions.

Table columns:
- locality_hh_id (STRING): household ID
- iuld_uuid (STRING): unique impression ID
- advertiser_name (STRING): always 'CoxReps OTT Partner Integration' -- ignore as a filter
- event_timestamp_utc (TIMESTAMP): event time UTC
- media_market (STRING): geographic media market e.g. 'Dallas--Fort Worth--Arlington, TX'
- publisher_name_mapped (STRING): publisher name
- device_category (STRING): see semantic mappings below
- data_date (DATE): date of record
- ott_dsp (STRING): DSP name e.g. 'freewheel', 'tradedesk'
- impression_date_pst (DATE): preferred for date filters
- delivery_date_pst (DATE): delivery date PST
- ip_address (STRING): IP address

Semantic mappings -- ALWAYS apply these regardless of how the user phrases it:
- 'TV', 'CTV', 'connected TV', 'television', 'big screen', 'OTT', 'streaming TV' -> device_category = 'television'
- 'mobile', 'phone', 'cell', 'cellular' -> device_category = 'smartphone'
- 'computer', 'laptop', 'PC', 'web' -> device_category = 'desktop'
- 'tablet', 'iPad' -> device_category = 'tablet'
- 'Freewheel', 'FW', 'free wheel' -> ott_dsp LIKE '%freewheel%'
- 'Trade Desk', 'TTD', 'the trade desk' -> ott_dsp LIKE '%tradedesk%'
- 'households', 'HH', 'homes', 'unique viewers' -> COUNT(DISTINCT locality_hh_id)
- 'impressions', 'ads', 'spots', 'views' -> COUNT(*) or COUNT(DISTINCT iuld_uuid)

Rules:
1. Use current_date() for today. For date ranges use impression_date_pst >= date_sub(current_date(), N).
2. For household exclusion queries use EXCEPT, never NOT IN.
3. If asked to LIST or SHOW items (e.g. 'list the markets', 'show publishers'), use SELECT DISTINCT or GROUP BY with ORDER BY -- do NOT add LIMIT.
4. If asked for a count, percentage, or total, write a pure aggregate (no LIMIT needed).
5. For all other queries add LIMIT 200.
6. Named entity matching (markets, cities, publishers, DSPs): ALWAYS use LOWER(column) LIKE LOWER('%term%'). NEVER use exact equality. media_market stores full DMA names like 'Dallas--Fort Worth--Arlington, TX' so a search for 'Dallas' should use LOWER(media_market) LIKE '%dallas%'.

Return ONLY the SQL. No explanation, no markdown fences."""

SUMMARY_PROMPT = """You are a helpful data analyst at Locality. Given the user's question and SQL results, respond as follows:

- If the question asks to LIST, SHOW, or ENUMERATE items (e.g. 'list the markets', 'show publishers', 'what markets are there'): reproduce ALL rows as a clean markdown table with column headers. Do not summarise -- show every row.
- If the question asks for a TOP N: show the items as a numbered markdown list with their metric.
- If the question asks for a count, total, percentage, or comparison: answer in 2-3 plain English sentences with specific numbers.

Never say 'the data shows' or 'the results indicate'. Be direct and specific."""

CLARIFY_PROMPT = """You are a helpful data analyst. A user asked a question about ad impression data but the query returned no data or a count of zero.

Your job:
1. Never just say '0 results' or repeat the error -- always be conversational and helpful.
2. Acknowledge the result warmly.
3. Suggest that the name or term they used might not exactly match what's in the database.
4. Give 1-2 concrete rephrasing suggestions based on the question (e.g. if they said 'Utica', suggest trying 'Utica-Rome' or just confirming Utica is in their target market list).
5. End with a friendly question asking if they'd like to try a different phrasing or if they meant something else.

Keep it to 3-4 sentences. Be warm, specific, and helpful."""

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
              "wait_timeout": "50s", "on_wait_timeout": "CONTINUE",
              "disposition": "INLINE", "format": "JSON_ARRAY"},
        timeout=90,
    )
    data = resp.json()
    sid = data.get("statement_id")
    for _ in range(40):
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
    # Return as markdown table so the LLM can reproduce it cleanly
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = ["| " + " | ".join(str(v) if v is not None else "" for v in row) + " |" for row in rows]
    return "\n".join([header, sep] + body)


def is_empty_or_zero(results: str) -> bool:
    """Detect when SQL returned no data or a single aggregate of zero."""
    if results == "No results returned.":
        return True
    lines = [l for l in results.strip().split("\n") if l.strip() and not l.startswith("|".ljust(3, "-"))]
    data_lines = [l for l in lines if l.startswith("|") and "---" not in l][1:]  # skip header
    if len(data_lines) == 1:
        values = [v.strip() for v in data_lines[0].strip("|").split("|")]
        if all(v in ("0", "0.0", "", "null", "None") for v in values):
            return True
    return False


def answer(question: str) -> str:
    try:
        sql = call_llm(SQL_PROMPT, question)
        sql = sql.strip().strip("`").replace("sql\n", "").strip()
        results = run_sql(sql)

        if results.startswith("Query failed:") or is_empty_or_zero(results):
            return call_llm(
                CLARIFY_PROMPT,
                f"User question: {question}\nSQL generated: {sql}\nResult: {results}"
            )

        return call_llm(SUMMARY_PROMPT, f"Question: {question}\n\nData:\n{results}")
    except Exception as e:
        return f"Something went wrong: {e}"


# ---------------------------------------------------------------------------
# Example questions
# ---------------------------------------------------------------------------
EXAMPLES = [
    "How many total impressions have been delivered?",
    "What percentage came from TV vs other devices?",
    "List all media markets",
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
