%%writefile /Workspace/Users/kevin.lynch@locality.com/app.py
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
CLIENT_ID = st.secrets["SP_CLIENT_ID"]
CLIENT_SECRET = st.secrets["SP_CLIENT_SECRET"]
ENDPOINT = f"{HOST}/serving-endpoints/ispot-id-impressions-agent/invocations"
TOKEN_URL = f"{HOST}/oidc/v1/token"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def get_token() -> str:
    """Mint a short-lived Databricks OAuth M2M token."""
    resp = requests.post(
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "client_credentials", "scope": "all-apis"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def ask_agent(message: str) -> str:
    """Send a message to the iSpot agent endpoint and return the answer."""
    token = get_token()
    resp = requests.post(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {token}",
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
