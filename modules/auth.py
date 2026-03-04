"""
modules/auth.py  —  Google OAuth gate for Form 15CB
Any valid Google account gets full access.

SETUP:
  1. requirements.txt  →  streamlit-google-auth==1.1.4

  2. .streamlit/secrets.toml (local):
        [google_oauth]
        client_id     = "....apps.googleusercontent.com"
        client_secret = "GOCSPX-..."
        redirect_uri  = "http://localhost:8501"

     Streamlit Cloud Secrets (production):
        [google_oauth]
        client_id     = "....apps.googleusercontent.com"
        client_secret = "GOCSPX-..."
        redirect_uri  = "https://form15cb-app.streamlit.app"

USAGE in app.py (right after set_page_config):
    from modules.auth import require_login, render_logout_button
    if not require_login():
        st.stop()
    render_logout_button()
"""

import json, tempfile
import streamlit as st


# ── Build authenticator ──────────────────────────────────────────────────────

def _get_authenticator():
    from streamlit_google_auth import Authenticate
    cfg = st.secrets["google_oauth"]

    credentials = {
        "web": {
            "client_id":     cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "redirect_uris": [cfg.get("redirect_uri", "http://localhost:8501")],
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token"
        }
    }
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(credentials, tmp)
    tmp.close()

    return Authenticate(
        secret_credentials_path=tmp.name,
        cookie_name="form15cb_auth",
        cookie_key="form15cb_cookie_key_2026",
        redirect_uri=cfg.get("redirect_uri", "http://localhost:8501"),
    )


# ── Login page — plain Streamlit only ───────────────────────────────────────

def _render_login_page(authenticator):
    _, col, _ = st.columns([1, 1, 1])

    with col:
        st.title("Form 15CB Generator")
        st.caption("AI-powered batch XML generation for CA teams")
        st.divider()
        st.subheader("Sign in")
        st.write("Use your Google account to access the platform.")
        authenticator.login()
        st.divider()
        st.caption("© 2026 Anand S & Associates · Form 15CB v3.0")


# ── Public API ───────────────────────────────────────────────────────────────

def require_login() -> bool:
    if st.session_state.get("auth_user"):
        return True

    try:
        authenticator = _get_authenticator()
    except KeyError:
        st.error("⚠️ Google OAuth secrets not configured. "
                 "Add [google_oauth] to Streamlit secrets.")
        st.stop()

    authenticator.check_authentification()

    if st.session_state.get("connected"):
        st.session_state["auth_user"]    = st.session_state.get("email", "")
        st.session_state["auth_name"]    = st.session_state.get("name", "")
        st.session_state["auth_picture"] = st.session_state.get("picture", "")
        st.session_state["_auth_obj"]    = authenticator
        return True

    _render_login_page(authenticator)
    return False


def render_logout_button():
    name    = st.session_state.get("auth_name", "User")
    email   = st.session_state.get("auth_user", "")
    picture = st.session_state.get("auth_picture", "")

    with st.sidebar:
        st.divider()
        if picture:
            st.image(picture, width=40)
        st.write(f"**{name}**")
        st.caption(email)
        if st.button("Sign Out", use_container_width=True):
            auth_obj = st.session_state.get("_auth_obj")
            if auth_obj:
                try:
                    auth_obj.logout()
                except Exception:
                    pass
            for key in ["auth_user", "auth_name", "auth_picture", "_auth_obj",
                        "connected", "email", "name", "picture"]:
                st.session_state.pop(key, None)
            st.rerun()