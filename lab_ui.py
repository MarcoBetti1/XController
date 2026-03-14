import os
from pathlib import Path
from datetime import datetime

import pandas as pd
import requests
import streamlit as st


API = os.getenv("X_CONTROLLER_LAB_API", "http://127.0.0.1:8010")


def _api_call(method: str, path: str, payload: dict | None = None, expect_json: bool = True):
    try:
        response = requests.request(method, f"{API}{path}", json=payload, timeout=60)
        if not response.ok:
            detail = response.text
            try:
                if "application/json" in response.headers.get("content-type", "").lower():
                    detail = response.json().get("detail", detail)
            except Exception:
                pass
            st.warning(f"API {method} {path} failed [{response.status_code}]: {detail}")
            return None
    except requests.exceptions.RequestException as exc:
        st.error(f"API request failed: {exc}")
        return None
    if not expect_json:
        return response.text
    try:
        return response.json()
    except Exception:
        return response.text


def _default_profile_path() -> str:
    return str((Path.cwd() / "data" / "profiles" / "default_profile").resolve())


def _status_label(running: bool, logged_in: bool | None) -> str:
    if not running:
        return "STOPPED"
    if logged_in is True:
        return "READY"
    if logged_in is False:
        return "LOGIN REQUIRED"
    return "STARTED"


def _extract_action_summary(payload: dict | None) -> dict[str, object]:
    row = payload if isinstance(payload, dict) else {}
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    expected = str(result.get("expected_action") or row.get("expected_action") or row.get("action") or "-")
    actual = str(result.get("actual_action") or row.get("actual_action") or row.get("action") or "-")
    ok_value = result.get("ok", row.get("ok", True))
    ok = bool(ok_value) if isinstance(ok_value, (bool, int)) else str(ok_value).lower() == "true"
    return {
        "at": row.get("at", ""),
        "requested": row.get("action", ""),
        "expected": expected,
        "actual": actual,
        "ok": ok,
        "status": row.get("status", ""),
        "duration_ms": row.get("duration_ms", ""),
        "error": row.get("error", ""),
    }


def _run_action_request(action: str, args_payload: dict[str, object]) -> dict | None:
    return _api_call(
        "POST",
        "/action/run",
        payload={"action": action, "args": args_payload},
    )


st.set_page_config(page_title="X Controller Lab", layout="wide")
st.title("X Controller Lab")
st.caption("Dedicated manual testing UI for x_controller methods and walkthrough flow.")

health = _api_call("GET", "/health")
if not health:
    st.warning(
        "Could not reach x_controller lab API. Start it with: "
        "`python -m x_controller.lab_api --host 127.0.0.1 --port 8010`"
    )
    st.stop()

with st.sidebar:
    st.metric("API", "Connected")
    st.caption(f"Endpoint: {API}")
    st.caption(datetime.now().isoformat(timespec="seconds"))
    if st.button("Refresh", type="primary"):
        st.rerun()
    live_updates = st.checkbox("Live updates (no full page refresh)", value=True)
    live_refresh_seconds = st.number_input("Live poll every (sec)", min_value=1, max_value=30, value=2, step=1)
    lab_log_lines = st.number_input("Lab log lines", min_value=40, max_value=3000, value=300, step=20)

profiles_payload = _api_call("GET", "/profiles") or {}
profiles_rows = profiles_payload.get("profiles") if isinstance(profiles_payload, dict) else []
profiles_rows = profiles_rows if isinstance(profiles_rows, list) else []

session = _api_call("GET", "/session") or {}
walkthrough = _api_call("GET", "/walkthrough/status") or {}

running = bool(session.get("running"))
logged_in = session.get("logged_in") if isinstance(session, dict) else None
walk_running = bool(walkthrough.get("running"))

st.subheader("Session")
default_path = _default_profile_path()
profile_options = [row.get("path") for row in profiles_rows if isinstance(row, dict) and row.get("path")]
selected_profile = profile_options[0] if profile_options else default_path

if "lab_profile_path" not in st.session_state:
    st.session_state["lab_profile_path"] = str(session.get("profile_path") or selected_profile)
if "lab_account_handle" not in st.session_state:
    st.session_state["lab_account_handle"] = str(session.get("account_handle") or "")

if profile_options:
    profile_pick = st.selectbox(
        "Detected profile folders",
        options=profile_options,
        index=profile_options.index(st.session_state["lab_profile_path"]) if st.session_state["lab_profile_path"] in profile_options else 0,
    )
    if st.button("Use selected profile"):
        st.session_state["lab_profile_path"] = profile_pick
        st.rerun()

with st.form("session_form", clear_on_submit=False):
    profile_path = st.text_input("Browser profile path", value=st.session_state["lab_profile_path"])
    account_handle = st.text_input("Account handle (for eval profile scan)", value=st.session_state["lab_account_handle"])
    c1, c2, c3, c4 = st.columns(4)
    start_pressed = c1.form_submit_button("Start Session", disabled=running)
    stop_pressed = c2.form_submit_button("Stop Session", disabled=not running)
    open_login_pressed = c3.form_submit_button("Open Login", disabled=not running)
    refresh_login_pressed = c4.form_submit_button("Refresh Login", disabled=not running)

if start_pressed:
    payload = {
        "profile_path": str(profile_path).strip(),
        "account_handle": str(account_handle).strip() or None,
    }
    started = _api_call("POST", "/session/start", payload=payload)
    if started:
        st.session_state["lab_profile_path"] = str(profile_path).strip()
        st.session_state["lab_account_handle"] = str(account_handle).strip()
        st.success("Session started.")
        st.rerun()
if stop_pressed:
    stopped = _api_call("POST", "/session/stop")
    if stopped:
        st.success("Session stopped.")
        st.rerun()
if open_login_pressed:
    opened = _api_call("POST", "/session/open-login")
    if opened:
        st.success("Login page opened in browser.")
        st.rerun()
if refresh_login_pressed:
    refreshed = _api_call("POST", "/session/refresh-login")
    if refreshed:
        st.success(f"Login status updated: {refreshed.get('logged_in')}")
        st.rerun()

m1, m2, m3, m4 = st.columns(4)
m1.metric("Session", _status_label(running, logged_in))
m2.metric("Logged In", "YES" if logged_in is True else ("NO" if logged_in is False else "UNKNOWN"))
m3.metric("Walkthrough", "RUNNING" if walk_running else "IDLE")
m4.metric("Updated", str(session.get("updated_at") or "-"))

st.caption(f"Profile: `{session.get('profile_path') or '-'}`")
st.caption(f"Account handle: `{session.get('account_handle') or '-'}`")
if session.get("error"):
    st.error(f"Session error: {session.get('error')}")

st.divider()
st.subheader("Walkthrough")
st.caption("Runs a full functional path with slower cooldowns and an eval step.")
st.markdown(
    f"**Live status:** `{walkthrough.get('current_action', 'idle')}` - "
    f"`{walkthrough.get('message', '-')}`"
)

w1, w2, w3, w4 = st.columns(4)
w1.metric("Running", "YES" if walk_running else "NO")
w2.metric("Step", f"{int(walkthrough.get('step_index') or 0)}/{int(walkthrough.get('step_total') or 0)}")
w3.metric("Completed", "YES" if walkthrough.get("completed") else "NO")
w4.metric("Updated", str(walkthrough.get("updated_at") or "-"))
if walkthrough.get("error"):
    st.error(f"Walkthrough error: {walkthrough.get('error')}")

with st.form("walkthrough_form", clear_on_submit=False):
    search_query = st.text_input("Search query", value="unpopular opinion")
    c1, c2, c3, c4 = st.columns(4)
    timeline_limit = c1.number_input("Timeline limit", min_value=6, max_value=40, value=12, step=1)
    cooldown_min = c2.number_input("Cooldown min (sec)", min_value=1, max_value=90, value=8, step=1)
    cooldown_max = c3.number_input("Cooldown max (sec)", min_value=1, max_value=180, value=18, step=1)
    eval_limit = c4.number_input("Eval profile scan limit", min_value=3, max_value=40, value=12, step=1)
    reply_text = st.text_input("Reply text", value="Walkthrough reply check.")
    post_text = st.text_input("Post text override (optional)", value="")
    include_eval = st.checkbox("Include eval step", value=True)

    wf1, wf2 = st.columns(2)
    start_walkthrough = wf1.form_submit_button("Start Walkthrough", disabled=(not running or walk_running))
    stop_walkthrough = wf2.form_submit_button("Stop Walkthrough", disabled=not walk_running)

if start_walkthrough:
    payload = {
        "search_query": str(search_query).strip(),
        "timeline_limit": int(timeline_limit),
        "cooldown_min_seconds": int(cooldown_min),
        "cooldown_max_seconds": int(cooldown_max),
        "reply_text": str(reply_text).strip(),
        "post_text": str(post_text).strip(),
        "include_eval": bool(include_eval),
        "eval_limit": int(eval_limit),
    }
    started = _api_call("POST", "/walkthrough/start", payload=payload)
    if started:
        st.success("Walkthrough started.")
        st.rerun()

if stop_walkthrough:
    stopped = _api_call("POST", "/walkthrough/stop")
    if stopped:
        st.success("Walkthrough stopped.")
        st.rerun()

def _render_live_walkthrough_panel(session_data: dict, walkthrough_data: dict) -> None:
    live_running = bool(session_data.get("running"))
    live_logged_in = session_data.get("logged_in")
    live_walk_running = bool(walkthrough_data.get("running"))
    st.markdown("**Live runtime monitor**")
    lm1, lm2, lm3, lm4 = st.columns(4)
    lm1.metric("Session", _status_label(live_running, live_logged_in))
    lm2.metric("Logged In", "YES" if live_logged_in is True else ("NO" if live_logged_in is False else "UNKNOWN"))
    lm3.metric("Walkthrough", "RUNNING" if live_walk_running else "IDLE")
    lm4.metric("Updated", str(walkthrough_data.get("updated_at") or session_data.get("updated_at") or "-"))
    st.caption(
        f"Live status: `{walkthrough_data.get('current_action', 'idle')}` - "
        f"`{walkthrough_data.get('message', '-')}`"
    )
    if walkthrough_data.get("error"):
        st.error(f"Walkthrough error: {walkthrough_data.get('error')}")

    live_events = walkthrough_data.get("events") if isinstance(walkthrough_data.get("events"), list) else []
    if live_events:
        st.markdown("**Walkthrough events**")
        st.dataframe(pd.DataFrame(live_events[-20:]), use_container_width=True)

    live_eval = walkthrough_data.get("eval") if isinstance(walkthrough_data.get("eval"), dict) else {}
    if live_eval:
        st.markdown("**Eval output**")
        st.json(
            {
                "post_id": live_eval.get("post_id"),
                "profile_handle": live_eval.get("profile_handle"),
                "post_metrics": live_eval.get("post_metrics") or {},
            }
        )
        eval_posts = live_eval.get("posts") if isinstance(live_eval.get("posts"), list) else []
        if eval_posts:
            st.markdown("**Eval posts + metrics**")
            st.dataframe(pd.DataFrame(eval_posts), use_container_width=True)

        processed_posts = live_eval.get("processed_posts") if isinstance(live_eval.get("processed_posts"), list) else []
        if processed_posts:
            st.markdown("**Processed posts (timeline/search)**")
            st.dataframe(pd.DataFrame(processed_posts), use_container_width=True)

        processed_with_metrics = live_eval.get("processed_posts_with_metrics") if isinstance(live_eval.get("processed_posts_with_metrics"), list) else []
        if processed_with_metrics:
            st.markdown("**Processed posts with metrics probe**")
            st.dataframe(pd.DataFrame(processed_with_metrics), use_container_width=True)

        profile_rows = live_eval.get("profile_recent_metrics") if isinstance(live_eval.get("profile_recent_metrics"), list) else []
        if profile_rows:
            st.markdown("**Profile recent metrics rows**")
            st.dataframe(pd.DataFrame(profile_rows), use_container_width=True)


fragment_api = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)
if live_updates and fragment_api:
    @fragment_api(run_every=f"{int(live_refresh_seconds)}s")
    def _live_fragment() -> None:
        live_session = _api_call("GET", "/session") or {}
        live_walk = _api_call("GET", "/walkthrough/status") or {}
        _render_live_walkthrough_panel(
            live_session if isinstance(live_session, dict) else {},
            live_walk if isinstance(live_walk, dict) else {},
        )

    _live_fragment()
elif live_updates:
    st.info("Live fragment updates are unavailable in this Streamlit version. Use Refresh.")
    _render_live_walkthrough_panel(session if isinstance(session, dict) else {}, walkthrough if isinstance(walkthrough, dict) else {})
else:
    _render_live_walkthrough_panel(session if isinstance(session, dict) else {}, walkthrough if isinstance(walkthrough, dict) else {})

st.divider()
st.subheader("Single Action Tests")
st.caption("Run one method at a time to isolate behavior.")
st.dataframe(
    pd.DataFrame(
        [
            {"action": "like_post", "type": "primitive", "expected_actual": "like_post"},
            {"action": "reply_to_post", "type": "primitive", "expected_actual": "reply_to_post"},
            {"action": "view_post", "type": "primitive", "expected_actual": "view_post"},
            {"action": "post_text", "type": "primitive", "expected_actual": "post_text"},
            {"action": "post_metrics", "type": "primitive", "expected_actual": "post_metrics"},
            {"action": "delete_post", "type": "destructive", "expected_actual": "delete_post"},
            {"action": "delete_reply", "type": "destructive", "expected_actual": "delete_reply"},
            {"action": "delete_repost", "type": "destructive", "expected_actual": "delete_repost"},
            {"action": "delete_all_posts", "type": "destructive", "expected_actual": "delete_all_posts"},
            {"action": "delete_all_replies", "type": "destructive", "expected_actual": "delete_all_replies"},
            {"action": "delete_all_reposts", "type": "destructive", "expected_actual": "delete_all_reposts"},
            {"action": "delete_all_content", "type": "destructive", "expected_actual": "delete_all_content"},
            {"action": "like_post_and_measure", "type": "composite", "expected_actual": "like_post+post_metrics"},
            {"action": "reply_to_post_and_measure", "type": "composite", "expected_actual": "reply_to_post+post_metrics"},
        ]
    ),
    use_container_width=True,
)

actions = [
    "is_logged_in",
    "read_timeline",
    "read_visible_posts",
    "search_posts",
    "view_post",
    "like_post",
    "reply_to_post",
    "like_post_and_measure",
    "reply_to_post_and_measure",
    "post_text",
    "profile_recent_metrics",
    "post_metrics",
    "delete_post",
    "delete_reply",
    "delete_repost",
    "delete_all_posts",
    "delete_all_replies",
    "delete_all_reposts",
    "delete_all_content",
    "follow_user",
    "unfollow_user",
    "recover_home",
]
selected_action = st.selectbox("Action", options=actions)

arg_query = ""
arg_post_id = ""
arg_text = ""
arg_username = ""
arg_limit = 12
arg_force_nav = False
arg_target_mode = "post_id"
arg_sample_limit = 20

if selected_action in {"read_timeline", "read_visible_posts", "search_posts", "profile_recent_metrics"}:
    arg_limit = st.number_input("limit", min_value=1, max_value=120, value=12, step=1)
if selected_action == "search_posts":
    arg_query = st.text_input("query", value="unpopular opinion")
if selected_action in {"view_post", "like_post", "reply_to_post", "like_post_and_measure", "reply_to_post_and_measure", "post_metrics"}:
    target_label = st.radio(
        "Target source",
        options=["Specific post ID", "Random visible post (current page)"],
        horizontal=True,
    )
    if target_label == "Specific post ID":
        arg_post_id = st.text_input("post_id")
    else:
        arg_target_mode = "random_visible"
        arg_sample_limit = st.number_input("sample_limit", min_value=3, max_value=60, value=20, step=1)
if selected_action in {"delete_post", "delete_reply", "delete_repost"}:
    arg_post_id = st.text_input("post_id_or_url")
if selected_action.startswith("delete_"):
    st.warning("Destructive action. Double-check the target before running it.")
if selected_action in {"reply_to_post", "reply_to_post_and_measure", "post_text"}:
    arg_text = st.text_area("text", value="", height=100)
if selected_action in {"profile_recent_metrics", "follow_user", "unfollow_user"}:
    arg_username = st.text_input("username")
if selected_action == "recover_home":
    arg_force_nav = st.checkbox("force_nav", value=False)

run_disabled = not running or walk_running
if st.button("Run Selected Action", disabled=run_disabled):
    args_payload: dict[str, object] = {}
    if selected_action in {"read_timeline", "read_visible_posts", "search_posts", "profile_recent_metrics"}:
        args_payload["limit"] = int(arg_limit)
    if selected_action == "search_posts":
        args_payload["query"] = str(arg_query).strip()
    if selected_action in {"view_post", "like_post", "reply_to_post", "like_post_and_measure", "reply_to_post_and_measure", "post_metrics"}:
        if arg_target_mode == "random_visible":
            args_payload["target_mode"] = "random_visible"
            args_payload["sample_limit"] = int(arg_sample_limit)
        else:
            args_payload["post_id"] = str(arg_post_id).strip()
    if selected_action in {"delete_post", "delete_reply", "delete_repost"}:
        args_payload["post_id"] = str(arg_post_id).strip()
    if selected_action in {"reply_to_post", "reply_to_post_and_measure", "post_text"}:
        args_payload["text"] = str(arg_text).strip()
    if selected_action in {"profile_recent_metrics", "follow_user", "unfollow_user"}:
        args_payload["username"] = str(arg_username).strip()
    if selected_action == "recover_home":
        args_payload["force_nav"] = bool(arg_force_nav)

    result = _run_action_request(selected_action, args_payload)
    if result:
        st.session_state["lab_last_action_result"] = result
        result_payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        action_ok = bool(result.get("ok", result_payload.get("ok", True)))
        expected = str(result_payload.get("expected_action") or result.get("expected_action") or selected_action)
        actual = str(result_payload.get("actual_action") or result.get("actual_action") or selected_action)
        if action_ok:
            st.success(f"Action succeeded: expected `{expected}` and actual `{actual}`.")
        else:
            st.warning(f"Action finished with no effect: expected `{expected}`, actual `{actual}`.")
        st.rerun()

st.markdown("**Quick Action Suites**")
st.caption("Run a predefined sequence to verify primitive actions and compare expected vs actual behavior.")
qs1, qs2 = st.columns(2)

if qs1.button("Run Feed Primitive Suite", disabled=run_disabled):
    suite_steps = [
        ("is_logged_in", {}),
        ("read_visible_posts", {"limit": 12}),
        ("like_post", {"target_mode": "random_visible", "sample_limit": 20}),
        ("view_post", {"target_mode": "random_visible", "sample_limit": 20}),
        ("recover_home", {"force_nav": False}),
    ]
    suite_rows: list[dict[str, object]] = []
    for action_name, action_args in suite_steps:
        response = _run_action_request(action_name, action_args) or {}
        summary = _extract_action_summary(response if isinstance(response, dict) else {})
        summary["suite_step"] = action_name
        suite_rows.append(summary)
    st.session_state["lab_suite_rows"] = suite_rows
    st.success("Feed primitive suite finished.")

if qs2.button("Run Reply+Measure Suite", disabled=run_disabled):
    suite_steps = [
        ("is_logged_in", {}),
        ("read_visible_posts", {"limit": 10}),
        ("reply_to_post_and_measure", {"target_mode": "random_visible", "sample_limit": 20, "text": "Walkthrough reply check."}),
        ("recover_home", {"force_nav": False}),
    ]
    suite_rows = []
    for action_name, action_args in suite_steps:
        response = _run_action_request(action_name, action_args) or {}
        summary = _extract_action_summary(response if isinstance(response, dict) else {})
        summary["suite_step"] = action_name
        suite_rows.append(summary)
    st.session_state["lab_suite_rows"] = suite_rows
    st.success("Reply+measure suite finished.")

suite_rows_state = st.session_state.get("lab_suite_rows")
if isinstance(suite_rows_state, list) and suite_rows_state:
    st.markdown("**Suite result: expected vs actual**")
    st.dataframe(pd.DataFrame(suite_rows_state), use_container_width=True)

last_action = st.session_state.get("lab_last_action_result") or session.get("last_action_result")
if last_action:
    summary = _extract_action_summary(last_action if isinstance(last_action, dict) else {})
    la1, la2, la3, la4 = st.columns(4)
    la1.metric("Expected", str(summary.get("expected") or "-"))
    la2.metric("Actual", str(summary.get("actual") or "-"))
    la3.metric("Success", "YES" if bool(summary.get("ok")) else "NO")
    la4.metric("Duration (ms)", str(summary.get("duration_ms") or "-"))
    st.markdown("**Last action result**")
    st.json(last_action)
    rows = None
    result_payload = last_action.get("result") if isinstance(last_action, dict) else {}
    if isinstance(result_payload, dict):
        if isinstance(result_payload.get("posts"), list):
            rows = result_payload.get("posts")
        elif isinstance(result_payload.get("rows"), list):
            rows = result_payload.get("rows")
        elif isinstance(result_payload.get("urls"), list):
            rows = [{"url": row} for row in result_payload.get("urls", [])]
    if isinstance(rows, list) and rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

history_rows = session.get("action_history") if isinstance(session.get("action_history"), list) else []
if history_rows:
    st.markdown("**Recent action history**")
    history_view: list[dict[str, object]] = []
    for row in history_rows[-30:]:
        summary = _extract_action_summary(row if isinstance(row, dict) else {})
        history_view.append(summary)
    st.dataframe(pd.DataFrame(history_view), use_container_width=True)

st.divider()
st.subheader("Lab Logs")
lab_log = _api_call("GET", f"/logs/lab?lines={int(lab_log_lines)}", expect_json=False)
st.text_area("x_controller_lab.log", value=lab_log or "No logs.", height=260, disabled=True)

st.caption(f"Last refresh: {datetime.now().isoformat(timespec='seconds')}")
