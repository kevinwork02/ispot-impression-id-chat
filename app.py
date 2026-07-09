import streamlit as st
import requests

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(page_title="iSpot Impressions Agent", page_icon="📺", layout="centered")
st.title("📺 iSpot Impressions Agent")
st.caption("Ask questions about Locality’s iSpot TV and digital ad impression data.")

# ---------------------------------------------------------------------------
# Secrets (set these in Streamlit Cloud → Settings → Secrets)
# ---------------------------------------------------------------------------
HOST = st.secrets["DATABRICKS_HOST"].rstrip("/")
TOKEN = st.secrets["DATABRICKS_TOKEN"]
ENDPOINT = f"{HOST}/serving-endpoints/ispot-id-impressions-agent/invocations"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def ask_agent(message: str) -> str:
    """Send a message to the iSpot agent endpoint and return the answer."""
    resp = requests.post(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
        json={"messages": [{"role": "user", "content": message}]},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json().get("predictions", "No answer returned.")


# ---------------------------------------------------------------------------
# Chat UI
# ---------------------------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []

# Render conversation history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Accept new user input
if prompt := st.chat_input("Ask about impressions, reach, markets, publishers..."):
    # Show and store user message
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Call the agent and stream back the response
    with st.chat_message("assistant"):
        with st.spinner("Querying iSpot data..."):
            try:
                answer = ask_agent(prompt)
            except Exception as e:
                answer = f"⚠️ Something went wrong: {e}"
        st.markdown(answer)

    st.session_state.messages.append({"role": "assistant", "content": answer})
