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
SQL_PROMPT = """You are a SQL expert. The user may ask follow-up questions referring to previous results -- use the conversation history to understand what 'those households', 'that market', 'those publishers' etc. refer to, and build the correct WHERE clause.

Generate a single valid Spark SQL SELECT statement for locality_dev.silver.ispot_streaming_impressions.

Table columns:
- locality_hh_id (STRING): household ID
- iuld_uuid (STRING): unique impression ID
- advertiser_name (STRING): always 'CoxReps OTT Partner Integration' -- ignore as a filter
- event_timestamp_utc (TIMESTAMP): event time UTC
- media_market (STRING): full DMA name e.g. 'Dallas--Fort Worth--Arlington, TX'
- publisher_name_mapped (STRING): publisher name
- device_category (STRING): see semantic mappings below
- ott_dsp (STRING): DSP name e.g. 'freewheel', 'tradedesk'
- impression_date_pst (DATE): preferred for date filters
- delivery_date_pst (DATE): delivery date PST
- locality_hh_id (STRING): household identifier

Semantic mappings -- ALWAYS apply, no exceptions:
- ANY of: 'TV', 'CTV', 'connected TV', 'television', 'big screen', 'OTT', 'streaming TV', 'broadcast TV', 'linear TV' -> device_category = 'television'
- ANY of: 'mobile', 'phone', 'cell', 'cellular', 'handset' -> device_category = 'smartphone'
- ANY of: 'computer', 'laptop', 'PC', 'web', 'browser' -> device_category = 'desktop'
- ANY of: 'tablet', 'iPad' -> device_category = 'tablet'
- ANY of: 'Freewheel', 'FW', 'free wheel' -> ott_dsp LIKE '%freewheel%'
- ANY of: 'Trade Desk', 'TTD', 'the trade desk' -> ott_dsp LIKE '%tradedesk%'
- ANY of: 'households', 'HH', 'homes', 'unique households', 'unique viewers', 'unique homes', 'addresses' -> COUNT(DISTINCT locality_hh_id) or DISTINCT locality_hh_id
- ANY of: 'impressions', 'ads', 'spots', 'views', 'exposures', 'ad plays' -> COUNT(*) or COUNT(DISTINCT iuld_uuid)

Rules:
1. Use current_date() for today. For date ranges: impression_date_pst >= date_sub(current_date(), N).
2. Named entity matching: ALWAYS use LOWER(column) LIKE LOWER('%term%'). NEVER exact equality for markets, cities, publishers, or DSPs. 'Dallas' -> LOWER(media_market) LIKE '%dallas%'.
3. For EXCLUSIVE reach ('only reached through X', 'exclusively via X', 'TV-only households', 'only on mobile') use this EXACT CTE pattern:
   WITH group_a AS (SELECT DISTINCT locality_hh_id FROM locality_dev.silver.ispot_streaming_impressions WHERE <condition_a> AND locality_hh_id IS NOT NULL),
   group_b AS (SELECT DISTINCT locality_hh_id FROM locality_dev.silver.ispot_streaming_impressions WHERE <condition_b> AND locality_hh_id IS NOT NULL),
   exclusive AS (SELECT locality_hh_id FROM group_a EXCEPT SELECT locality_hh_id FROM group_b)
   SELECT COUNT(*) AS exclusive_hh_count FROM exclusive
   For 'TV-only': condition_a = device_category = 'television', condition_b = device_category != 'television'.
   NEVER use NOT IN for household exclusion -- it silently returns zero when NULLs are present.
4. For MULTIPLE time windows ('last 7 days and last 15 days', 'this week vs last month'): use FILTER expressions in one SELECT:
   SELECT COUNT(*) FILTER (WHERE impression_date_pst >= date_sub(current_date(), 7)) AS last_7_days,
          COUNT(*) FILTER (WHERE impression_date_pst >= date_sub(current_date(), 15)) AS last_15_days
   FROM locality_dev.silver.ispot_streaming_impressions WHERE ...
5. For LIST/SHOW questions: SELECT DISTINCT or GROUP BY with ORDER BY, no LIMIT.
6. For counts/percentages/totals: pure aggregate, no LIMIT.
7. All other queries: LIMIT 200.

Return ONLY the SQL. No explanation, no markdown fences."""

SUMMARY_PROMPT = """You are a helpful data analyst at Locality. Given the user's question and SQL results, respond as follows:

- If the question asks to LIST, SHOW, or ENUMERATE items (e.g. 'list the markets', 'show publishers', 'what markets are there'): reproduce ALL rows as a clean markdown table with column headers. Do not summarise -- show every row.
- If the question asks for a TOP N: show the items as a numbered markdown list with their metric.
- If the question asks for a count, total, percentage, or comparison: answer in 2-3 plain English sentences with specific numbers.

Never say 'the data shows' or 'the results indicate'. Be direct and specific."""

CLARIFY_PROMPT = """You are a helpful data analyst. A user asked a question about ad impression data but the query returned no data or zero.

IMPORTANT rules before suggesting alternatives:
- device_category = 'television' is ALWAYS the correct value for TV/CTV/OTT. NEVER suggest 'broadcast', 'video', 'streaming' or any other value as an alternative for TV.
- 'freewheel' and 'tradedesk' are ALWAYS the correct DSP values. Do not suggest alternatives.
- Only suggest name/spelling alternatives for geographic markets (city names, DMA names) and publisher names -- those are free-text fields where names vary.

Your response:
1. If the question was a FOLLOW-UP (uses words like 'those', 'them', 'that', 'of those'): gently note that you may have lost the context from the previous question and ask them to restate it in full (e.g. 'Could you ask this as a complete question? For example: how many TV-only households were there in Dallas in the last 30 days?').
2. If a geographic market or publisher name was used: suggest it might be stored under a slightly different DMA name, and invite them to first ask 'list all media markets' to find the exact name.
3. For all other cases: ask if they meant something slightly different and invite a rephrasing.

Be warm, concise (2-3 sentences), and never suggest wrong technical alternatives."""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def call_llm(system: str, messages: list) -> str:
    """Call LLM with a system prompt and a list of {role, content} messages."""
    resp = requests.post(
        LLM_ENDPOINT,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        json={"messages": [{"role": "system", "content": system}] + messages},
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


SQL_RETRY_PROMPT = """The SQL query below failed with an error. Fix it and return only the corrected SQL query.
No explanation, no markdown fences."""


def is_empty_or_zero(results: str) -> bool:
    """Detect when SQL returned genuinely empty results or a single aggregate of zero."""
    if results == "No results returned.":
        return True
    # Parse markdown table rows, skip header and separator
    data_lines = [
        l for l in results.strip().split("\n")
        if l.startswith("|") and "---" not in l
    ][1:]  # [1:] skips header row
    if len(data_lines) == 1:
        values = [v.strip() for v in data_lines[0].strip("|").split("|")]
        if all(v in ("0", "0.0", "", "null", "None") for v in values):
            return True
    return False


def clean_sql(raw: str) -> str:
    return raw.strip().strip("`").replace("sql\n", "").replace("```", "").strip()


def answer(question: str, history: list) -> str:
    """history is a list of {role, content} dicts representing the conversation so far."""
    # Include last 6 messages (3 turns) so follow-up questions have context
    context = history[-6:]
    current = context + [{"role": "user", "content": question}]

    try:
        # Step 1: Generate SQL with full conversation context
        sql = clean_sql(call_llm(SQL_PROMPT, current))
        results = run_sql(sql)

        # Step 2: Retry once if SQL failed
        if results.startswith("Query failed:"):
            retry_messages = current + [
                {"role": "user", "content": f"That SQL failed: {results}\nPlease fix and return only the corrected SQL."}
            ]
            sql = clean_sql(call_llm(SQL_PROMPT, retry_messages))
            results = run_sql(sql)

        # Step 3: Still failing -- friendly message
        if results.startswith("Query failed:"):
            return "I had trouble building a valid query for that. Could you try rephrasing it? For example, break it into two separate questions if you're asking about multiple time windows."

        # Step 4: Zero/empty -- ask for clarification with context
        if is_empty_or_zero(results):
            return call_llm(
                CLARIFY_PROMPT,
                [{"role": "user", "content": f"Conversation so far:\n{history}\n\nLatest question: {question}\nSQL used: {sql}\nResult: {results}"}]
            )

        # Step 5: Good results -- summarise with context
        return call_llm(
            SUMMARY_PROMPT,
            [{"role": "user", "content": f"Question: {question}\n\nData:\n{results}"}]
        )
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
            # Pass all prior messages as history so follow-ups have context
            history = st.session_state.messages[:-1]
            reply = answer(prompt, history)
        st.markdown(reply)
    st.session_state.messages.append({"role": "assistant", "content": reply})
