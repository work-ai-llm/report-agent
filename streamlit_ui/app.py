"""Streamlit UI Application for Penetration Test Reporting Agent.

This is the main entry point for the Streamlit web interface that allows users
to interact with the pen-test reporting agent through a chat-based UI.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     Streamlit Browser UI                     │
├─────────────────────────────────────────────────────────────┤
│  Sidebar                        │  Main Chat Area           │
│  ─────────                      │  ───────────────          │
│  [Session Selector]             │                           │
│  [New Session]                  │  [User Messages]          │
│                                 │  [Agent Responses]        │
│  ───── Upload ─────             │  [Tool Call Visualizations]
│  [📁 File Uploader]             │                           │
│  • finding1.txt                 │  [Chat Input + Send]      │
│  • finding2.txt                 │                           │
│                                 │                           │
│  ───── Reports ─────            │                           │
│  [📄 Report Selector]           │                           │
│  [Download Button]              │                           │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              PenTestAgentService (Service Layer)             │
│  - upload_finding()                                          │
│  - send_message() → yields tool calls                        │
│  - download_report()                                         │
│  - list_findings() / list_reports()                          │
└────────┬──────────────────────┬──────────────────────────────┘
         │                      │
         ▼                      ▼
┌─────────────────┐    ┌──────────────────┐
│ Checkpointer    │    │ FileStore        │
│ (PostgresSaver) │    │ (PostgresStore)  │
└─────────────────┘    └──────────────────┘
         │
         ▼
┌─────────────────┐
│ AgentRunner     │
│ (LangGraph)     │
└─────────────────┘
```

## Data Flow Examples

### 1. User Sends Message
```
User types "Scan target" 
  → st.session_state.chat_messages appended
  → service.send_message("Scan target")
  → Yields tool_call events
  → render_tool_call() displays each tool
  → Yields final message
  → render_message() displays response
  → service.save_chat_history() persists to DB
```

### 2. User Uploads Finding
```
User selects file
  → render_file_uploader() receives bytes
  → Callback: service.upload_finding(filename, content)
  → file_store.write_file("findings/filename", content)
  → st.session_state.findings updated
  → Sidebar refreshes showing new file
```

### 3. User Downloads Report
```
User clicks download button
  → render_report_downloader() called
  → service.download_report("report.md")
  → file_store.read_file("reports/report.md")
  → st.download_button() triggers browser download
```

## Session State Management

The app uses `st.session_state` to maintain UI state across reruns:
- `current_thread_id`: Active session identifier
- `chat_messages`: List of conversation messages
- `findings`: List of uploaded finding filenames
- `service`: PenTestAgentService instance (cached)

## Running the Application

```bash
streamlit run streamlit_ui/app.py
```

Or via the launcher (same command; add `--debug` to print the runtime
connectivity report - checkpointer / file store / llm api / agent /
observability and whether each is connected - before the server starts):

```bash
python -m streamlit_ui run streamlit_ui/app.py          # normal launch
python -m streamlit_ui --debug run streamlit_ui/app.py  # prints debug report
```

## Runtime Modes (environment variables)

- Default (no env): **production** - full real integration (persistence + real
  agent). Requires model keys in `.env` (e.g. `MISTRAL_API_KEY`).
- `STREAMLIT_USE_MOCKS=1`: full in-memory mock suite (checkpointer, file store,
  agent runner). No DB, no API keys. State is lost on restart.
- `STREAMLIT_USE_REAL_BACKEND=1`: partial swap - real checkpointer + virtual
  filesystem (SQLite by default, Postgres when `POSTGRES_URI` is reachable),
  still the mock agent. State survives server restarts.
- `STREAMLIT_USE_REAL_AGENT=1`: full swap - real persistence + the real agent
  (streams tool calls + replies; prints LLM output to the terminal when
  `STREAMLIT_AGENT_PRINT=1`).
- Persistence DB is `agent/agent_session.db` (override with
  `REPORT_AGENT_DB_PATH`, or set `POSTGRES_URI` for Postgres).

## Environment Variables (Production)

- `DATABASE_URL`: PostgreSQL connection string for session store
- `AGENT_MODEL`: LLM model name for agent execution
"""

import os
import time

import streamlit as st
from typing import Optional, Callable, List, Dict, Any
from streamlit_ui.services.agent_service import PenTestAgentService
from streamlit_ui.components.chat_display import (
    render_chat_history,
    render_message,
    render_tool_call,
)
from streamlit_ui.components.file_uploader import render_file_uploader
from streamlit_ui.components.report_downloader import (
    render_report_downloader,
    render_report_selector,
)

# Sentinel option shown in the session selector when no session is active.
_NO_SESSION = "(no active session)"
from tests.mock.mock_objects_streamlit import (
    MockCheckpointer,
    MockFileStore,
    MockAgentRunner,
)


@st.cache_resource
def _shared_mock_checkpointer() -> MockCheckpointer:
    """Shared in-memory checkpointer for mock mode.

    Cached so all sessions created during a session-of-the-app share one
    checkpointer - which is what makes saved chats listable and switchable
    from the session selector.
    """
    return MockCheckpointer()


@st.cache_resource
def _shared_mock_file_store() -> MockFileStore:
    """Shared in-memory file store for mock mode.

    Findings/reports are global by design (see upload_finding), so every
    service in mock mode writes to the same store. A demo report is seeded so
    the download path can be exercised without a real agent run.
    """
    store = MockFileStore()
    store.prepopulate_with_report(
        "demo_pentest_report.md",
        b"# Demo Pentest Report\n\nSeeded by mock mode so the store + download "
        b"path can be tested without a real agent run.\n",
    )
    store.prepopulate_with_report(
        "demo_pentest_report_2.md",
        b"# Demo check 2\n\nSeeded by mock mode so the store + download "
        b"\n I want to check how download selector works.",
    )
    
    return store


@st.cache_resource
def _shared_mock_agent_runner() -> MockAgentRunner:
    """Shared in-memory agent runner for mock mode.

    Pre-loaded with plausible tool calls and a small per-event delay so the
    chat visibly streams (tool call blocks, then the reply) like a real run.

    The shared mock checkpointer is injected so the runner can persist each turn
    itself and expose the rerun-safe start/poll pair. Without it the runner has
    nowhere to save, and mock mode would silently lose history on every reload -
    the exact failure #20 is about, just in the sandbox.

    ``STREAMLIT_MOCK_DELAY`` overrides the per-event delay in seconds. Tests need
    a wide window to interrupt a turn mid-stream, and they cannot reach this
    cached instance from outside a script run (``st.cache_resource`` does not
    dedupe outside one), so the knob has to be an env var.
    """
    try:
        delay = float(os.environ.get("STREAMLIT_MOCK_DELAY", "0.35"))
    except (TypeError, ValueError):
        delay = 0.35
    runner = MockAgentRunner(
        delay_seconds=delay,
        checkpointer=_shared_mock_checkpointer(),
    )
    runner.set_tool_calls([
        {"name": "nmap_scan", "args": {"target": "10.0.0.1"}},
        {"name": "nikto_scan", "args": {"url": "http://10.0.0.1"}},
    ])
    return runner


def _build_mock_service(thread_id: str) -> PenTestAgentService:
    """Build a service backed by in-memory mock dependencies.

    Used when STREAMLIT_USE_MOCKS=1 - lets you run the full UI without Postgres,
    an LLM, or a live agent. All state is lost between server restarts.
    """
    return PenTestAgentService(
        thread_id=thread_id,
        checkpointer=_shared_mock_checkpointer(),
        file_store=_shared_mock_file_store(),
        agent_runner=_shared_mock_agent_runner(),
    )


def _build_production_service(thread_id: str) -> PenTestAgentService:
    """Build the full real production service (default when no env is set).

    Real persistence (SQLite or Postgres) + the real LangGraph agent running
    through the live LLM. Requires model keys in ``.env`` (e.g.
    ``MISTRAL_API_KEY``). Run with ``STREAMLIT_USE_MOCKS=1`` for the full
    in-memory dev sandbox or ``STREAMLIT_USE_REAL_BACKEND=1`` for the partial
    swap (real persistence, mock agent).
    """
    from streamlit_ui.services.real_backend import build_real_backend_service

    return build_real_backend_service(thread_id, force_full=True)


def _build_real_backend_service(thread_id: str) -> PenTestAgentService:
    """Build a real-backend service: partial swap or full real agent.

    - ``STREAMLIT_USE_REAL_BACKEND=1`` (partial): real checkpointer + store,
      mock agent (no API keys needed).
    - ``STREAMLIT_USE_REAL_AGENT=1`` (full): real persistence + the real agent
      (needs model keys in ``.env``).
    """
    from streamlit_ui.services.real_backend import build_real_backend_service

    return build_real_backend_service(thread_id)


def get_or_create_service(thread_id: str) -> PenTestAgentService:
    """Get or create PenTestAgentService instance for current thread.

    Modes (first match wins):
    - ``STREAMLIT_USE_MOCKS=1``       → in-memory mock dependencies (dev sandbox)
    - ``STREAMLIT_USE_REAL_BACKEND=1`` → real persistence + mock agent (partial)
    - ``STREAMLIT_USE_REAL_AGENT=1``   → real persistence + real agent (full)
    - no env                           → production (full real agent)

    Args:
        thread_id: Current session/thread identifier

    Returns:
        Configured PenTestAgentService instance

    Data Flow:
        App startup → get_or_create_service() → Initialize dependencies → Cache in session_state
    """
    if "service" not in st.session_state or st.session_state.service.thread_id != thread_id:
        from streamlit_ui.services.real_backend import (
            is_real_backend_mode,
            is_real_agent_mode,
        )

        if os.environ.get("STREAMLIT_USE_MOCKS", "").lower() in ("1", "true", "yes"):
            service = _build_mock_service(thread_id)
        elif is_real_backend_mode() or is_real_agent_mode():
            service = _build_real_backend_service(thread_id)
        else:
            service = _build_production_service(thread_id)

        st.session_state.service = service

    return st.session_state.service


def render_sidebar(service: PenTestAgentService) -> None:
    """Render sidebar with session controls, file upload, and reports.
    
    Args:
        service: PenTestAgentService instance for operations
        
    Data Flow:
        Page load → render_sidebar() → Display UI elements → Handle user actions
    """
    with st.sidebar:
        st.title("🤖 Pen-Test Agent")
        
        # Session selection
        st.markdown("### 📋 Sessions")
        existing_sessions = service.list_threads()
        current = st.session_state.current_thread_id

        if existing_sessions:
            # The selector lists a "(no active session)" placeholder whenever no
            # thread is currently active (fresh load or after "New Session").
            if current in existing_sessions:
                options = existing_sessions
                cur_value = current
            else:
                options = [_NO_SESSION] + existing_sessions
                cur_value = _NO_SESSION

            def _select_session():
                # on_change: bind the active thread to the selection, or clear
                # it when the placeholder is chosen.
                val = st.session_state["session_selector"]
                st.session_state.current_thread_id = (
                    None if val == _NO_SESSION else val
                )
                st.session_state.chat_messages = []
                st.session_state._service_thread = None

            st.selectbox(
                "Select Session",
                options,
                index=options.index(cur_value),
                key="session_selector",
                on_change=_select_session,
            )

        def _new_session():
            # "New Session" just clears the current chat. A brand-new session
            # is only created (and persisted) when the first message is sent,
            # so an empty/interrupted session never shows up in the selector.
            st.session_state.current_thread_id = None
            st.session_state.chat_messages = []
            st.session_state._service_thread = None
            st.session_state["session_selector"] = _NO_SESSION
            st.session_state["confirm_delete_session"] = False

        st.button(
            "➕ New Session",
            on_click=_new_session,
            use_container_width=True,
        )

        # Delete the currently selected session (two-step: confirm checkbox).
        current_tid = st.session_state.current_thread_id
        if current_tid and current_tid in existing_sessions:
            st.caption("Deletes the session selected above.")
            if st.checkbox(
                "⚠️ I understand - this permanently deletes the selected session",
                key="confirm_delete_session",
            ):
                def _delete_session():
                    # on_click: runs before the session widgets are instantiated,
                    # so widget keys can be reset safely (same as New Session).
                    tid = st.session_state.current_thread_id
                    if service.delete_thread(tid):
                        st.session_state.current_thread_id = None
                        st.session_state.chat_messages = []
                        st.session_state._service_thread = None
                        st.session_state["session_selector"] = _NO_SESSION
                        st.session_state["confirm_delete_session"] = False
                        # No st.rerun() here: it is a no-op inside a callback.
                        # The sidebar refresh happens on the next interaction.
                        st.session_state["_delete_notice"] = (
                            "success",
                            f"Deleted session `{tid}`.",
                        )
                    else:
                        st.session_state["_delete_notice"] = (
                            "error",
                            f"Session `{tid}` could not be found.",
                        )

                st.button(
                    "🗑 Delete Session",
                    on_click=_delete_session,
                    use_container_width=True,
                )

        notice = st.session_state.pop("_delete_notice", None)
        if notice:
            kind, text = notice
            if kind == "success":
                st.success(text)
            else:
                st.error(text)

        load_error = st.session_state.pop("_session_load_error", None)
        if load_error:
            st.error(f"Session list unavailable ({load_error}).")
        
        st.divider()
        
        # File upload section
        def handle_upload(
            filename: str, content: bytes, replace: bool = False
        ) -> Optional[str]:
            """Callback for file upload component.

            Returns ``"__COLLISION__"`` when the file name already exists and
            the user has not yet chosen to replace it. On success returns a
            status message. Returns None when the file has already been
            processed this session (dedupe).

            Args:
                filename: Uploaded file name.
                content: Uploaded file bytes.
                replace: True when the user chose to overwrite an existing file.
            """
            if filename in service.list_findings() and not replace:
                return "__COLLISION__"

            if filename in st.session_state.processed_findings:
                return None

            service.upload_finding(filename, content)
            st.session_state.processed_findings.add(filename)
            st.session_state.findings = service.list_findings()
            if replace:
                return f"Replaced `{filename}` with the new upload."
            return f"Uploaded `{filename}` ({len(content)} bytes)."
        
        render_file_uploader(handle_upload)
        
        # Display uploaded findings
        st.divider()
        st.markdown("### 📁 Uploaded Findings")
        st.session_state.findings = service.list_findings()
        
        if st.session_state.findings:
            for finding in st.session_state.findings:
                st.text(f"• {finding}")
        else:
            st.info("No findings uploaded")
        
        st.divider()
        
        # Reports section
        st.markdown("### 📄 Reports")
        selected_report = render_report_selector(service.list_reports)

        if selected_report:
            render_report_downloader(service.download_report, selected_report)


def _history_from_stored(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert stored history entries into a flat event list.

    The stored history (from checkpointer) may contain plain role/content
    entries (written before structured events were introduced) or structured
    events (``{"type": ...}``). This helper normalises the former into the
    structured form so ``render_chat_history`` can render either.

    Args:
        messages: Entries as stored in the checkpointer.

    Returns:
        Normalised flat event list.
    """
    normalised: List[Dict[str, Any]] = []
    for m in messages:
        if "type" in m:
            normalised.append(m)
        elif m.get("role") == "user":
            normalised.append({"type": "user", "content": m["content"]})
        elif m.get("role") == "assistant":
            normalised.append({"type": "assistant", "content": m["content"]})
        else:
            normalised.append({"type": "assistant", "content": str(m)})
    return normalised


RUN_STATUS_POLL_SECONDS = 1.0


def render_run_status(service: PenTestAgentService) -> None:
    """A live strip that keeps reporting while a turn is in flight.

    Streamlit has no push channel. A script run is over the moment it returns, so
    "is the agent still working?" cannot be answered by the run that started the
    work - which is exactly the gap this fills. After an interrupted turn the app
    used to look completely idle while the agent kept working in the background,
    because nothing re-rendered to say otherwise.

    The fix is a fragment that re-runs on a timer and reads the run registry -
    the one piece of state that outlives a script run. It is deliberately cheap
    when idle: a single in-memory lookup, and it renders nothing.

    When a turn finishes while we are watching, one full rerun is triggered so the
    result is folded into the ordinary history and the strip disappears. That is a
    one-shot transition, not a loop: the flag that drives it is popped on use.
    """

    @st.fragment(run_every=RUN_STATUS_POLL_SECONDS)
    def _status() -> None:
        running = service.is_run_active()
        if running:
            snapshot = service.active_run_snapshot() or {}
            steps = len(snapshot.get("events", []))
            with st.status("Agent is running…", expanded=True):
                st.caption(
                    f"{steps} step(s) so far. The sidebar stays usable — this "
                    "turn keeps going in the background."
                )
                # An explicit Stop, because the browser's Stop button cannot be
                # used for this (see PenTestAgentService.cancel_run).
                if st.button("🛑 Stop this turn", key="stop_active_run"):
                    if service.cancel_run():
                        st.info("Stopping. The turn will not be saved as finished.")
        elif st.session_state.pop("_run_was_active", False):
            # The turn ended while this fragment was watching. Force one full
            # rerun: reconciliation picks up the finished transcript, and the strip
            # stops rendering because the run is no longer active.
            st.session_state["_seen_run_revision"] = None
            st.rerun()
        st.session_state["_run_was_active"] = running

    _status()


def render_main_chat(service: PenTestAgentService) -> None:
    """Render main chat area with message history and input.
    
    Args:
        service: PenTestAgentService instance for agent interaction
        
    Data Flow:
        User input → Append to chat_messages → service.send_message() 
        → Render streaming events → Save history
    """
    st.title("💬 Chat with Pen-Test Agent")
    
    # Display chat history from the structured event list
    render_chat_history(
        _history_from_stored(st.session_state.chat_messages)
    )

    # Live "agent is running" strip. Rendered after the history so the finished
    # turns stay above it.
    render_run_status(service)
    
    # Chat input
    if prompt := st.chat_input("Ask the agent to analyze findings..."):
        # Create session on first message (not on page load)
        created_session = False
        # Snapshot the reports list *before* the turn. The sidebar was rendered
        # above, from a store scan taken before this turn ran, so a report the
        # agent writes during the turn cannot appear in its selector until the
        # script runs again (#30). Comparing before/after lets us rerun only when
        # the turn actually changed something, instead of unconditionally - an
        # unconditional rerun costs a list_threads scan per turn, which is
        # ~0.5s on Postgres.
        reports_before = service.list_reports() if service is not None else []
        if st.session_state.current_thread_id is None:
            import uuid
            st.session_state.current_thread_id = f"session_{uuid.uuid4().hex[:8]}"
            service = get_or_create_service(st.session_state.current_thread_id)
            service.save_chat_history([])
            # The sidebar selector (driven by current_thread_id) will show the
            # new session on the rerun below.
            created_session = True

        # One turn per thread. A second prompt sent while a turn is still running
        # would start a concurrent agent run on the same LangGraph thread, and
        # both runs would write the same UI-namespace checkpoint row - their
        # writes interleave and the stored transcript ends up with duplicated or
        # missing tool calls. Observed live: two overlapping Langfuse traces seven
        # seconds apart on one session. So refuse early, before the user bubble is
        # appended, since the refused turn is never persisted and appending it
        # here would put the session ahead of the checkpointer again.
        # services/run_registry.RunBusyError is the real guard; this is just the
        # clear UX for it.
        if service.is_run_active():
            st.warning(
                "The agent is still working on this session. Wait for it to "
                "finish, or switch to another session - the turn keeps running."
            )
            return

        # Add user message
        st.session_state.chat_messages.append({"type": "user", "content": prompt})
        render_message("user", prompt)
        
        # Stream agent response
        with st.chat_message("assistant"):
            # Render tool-call blocks first, then accumulate the final text
            # below them so the answer text appears underneath the expanders.
            tool_events: List[Dict[str, Any]] = []
            full_response = ""
            # Live token streaming: "token" events arrive per model delta; we
            # render them into a placeholder with light throttling so long
            # replies don't re-render Streamlit on every single token.
            stream_ph: Optional[Any] = None
            stream_acc = ""
            flushed_len = 0
            last_flush = 0.0

            def _flush_stream() -> None:
                nonlocal flushed_len, last_flush
                now = time.monotonic()
                if now - last_flush >= 0.12 or (len(stream_acc) - flushed_len) >= 24:
                    stream_ph.markdown(f"{stream_acc}▌")
                    flushed_len = len(stream_acc)
                    last_flush = now

            # Persisting is NOT done here. The agent runner owns the UI-namespace
            # conversation and writes it from the event-loop thread as events
            # arrive, so an interrupted script run (a queued rerun unwinds this
            # stack without warning) costs the rendering of an event but never
            # the event itself. See services/run_registry.py.
            # Rows appended below are display-only, for the in-progress turn; a
            # later rerun reconciles them against the persisted truth.
            def _save_chat() -> None:
                """No-op shim retained so the event loop below reads unchanged.

                Deliberately does not persist. The runner already has.
                """
                return None

            try:
                # Process streaming events
                for event in service.send_message(prompt):
                    if event["type"] == "tool_call":
                        name = event["data"]["name"]
                        args = event["data"]["args"]
                        entry = {"type": "tool_call", "name": name, "args": args}
                        tool_events.append(entry)
                        st.session_state.chat_messages.append(entry)
                        render_tool_call(name, args)
                        _save_chat()
                    elif event["type"] == "tool_result":
                        name = event["data"].get("name", "")
                        output = event["data"].get("output", "")
                        # Attach the result to the oldest tool_call still
                        # awaiting one (FIFO, matches tool-call execution
                        # order), so the persisted history keeps args +
                        # results (store_cli reads them) without breaking
                        # chat rendering.
                        for te in tool_events:
                            if te.get("type") == "tool_call" and "result" not in te:
                                te["result"] = str(output)
                                break
                        if output:
                            st.caption(f"✅ {name} → {str(output)[:120]}")
                        _save_chat()
                    elif event["type"] == "token":
                        stream_acc += event["data"]
                        if stream_ph is None:
                            stream_ph = st.empty()
                        _flush_stream()
                    elif event["type"] == "message":
                        full_response = event["data"]
                        st.session_state.chat_messages.append({
                            "type": "assistant",
                            "content": full_response,
                        })
                        if stream_ph is not None:
                            stream_ph.markdown(full_response or stream_acc)
                        else:
                            st.write(full_response)
                        _save_chat()
                    elif event["type"] in ("busy", "error"):
                        # Previously these were dropped on the floor. A refused
                        # turn and a failed run are both things the user needs to
                        # see, and neither is persisted into the transcript.
                        note = str(event.get("data", "Agent run failed."))
                        icon = "⏳" if event["type"] == "busy" else "⚠️"
                        if stream_ph is not None:
                            stream_ph.empty()
                        st.warning(f"{icon} {note}")
            except Exception as exc:
                # Surface run failures instead of silently freezing the chat.
                note = f"⚠️ Agent run failed: {exc}"
                st.session_state.chat_messages.append({"type": "assistant", "content": note})
                if stream_ph is not None:
                    stream_ph.markdown(note)
                else:
                    st.write(note)
            finally:
                _save_chat()

        # Rerun once at the end of a turn so the sidebar - which rendered before this
        # turn ran - reflects it. Two reasons, both requiring the rerun:
        #
        # - a brand-new session must show up in the selector immediately, and
        # - a report the agent wrote during the turn must appear in the Reports
        #   selector (#30). Without this the agent narrates "report saved" while the
        #   UI still reads "No reports available yet", and Download stays disabled
        #   at exactly the moment the report exists.
        #
        # Gated on the reports list actually changing, because the rerun re-runs
        # the whole script including list_threads (~0.5s on Postgres) and most
        # turns write no report.
        reports_after = service.list_reports() if service is not None else []
        if created_session or reports_after != reports_before:
            st.rerun()


def _reconcile_with_persisted(service: PenTestAgentService) -> None:
    """Bring ``chat_messages`` up to date with what the agent actually recorded.

    A widget click mid-stream aborts THIS script run, but not the agent turn: the
    runner keeps streaming on the event-loop thread and keeps persisting. So on
    the next rerun the persisted conversation is ahead of what this session is
    holding. The run registry's revision counter says whether that happened,
    cheaply, without a checkpointer round-trip on every rerun.

    Rehydrating from the checkpointer (rather than patching the in-memory list) is
    what makes the UI converge on the same transcript the agent recorded, instead
    of accumulating a second, drifting copy.

    ``main()`` calls this BEFORE ``render_sidebar``. A successful file upload ends
    in ``st.rerun()`` from inside the sidebar, which unwinds the script before
    anything placed after the sidebar is reached - so reconciliation placed there
    would be skipped, and an upload could leave the chat area blank.
    """
    if st.session_state.current_thread_id is None:
        return
    revision = service.run_revision()
    if revision != st.session_state.get("_seen_run_revision"):
        st.session_state.chat_messages = service.get_chat_history()
        st.session_state._seen_run_revision = revision


def main() -> None:
    """Main application entry point.
    
    Initializes session state and renders UI components.
    
    Data Flow:
        App start → Initialize session_state → render_sidebar() → render_main_chat()
    """
    st.set_page_config(
        page_title="Pen-Test Reporting Agent",
        page_icon="🤖",
        layout="wide",
    )
    
    # Session is only created when the first message is sent (not on page load).
    if "current_thread_id" not in st.session_state:
        st.session_state.current_thread_id = None

    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []

    if "findings" not in st.session_state:
        st.session_state.findings = []

    if "processed_findings" not in st.session_state:
        st.session_state.processed_findings = set()

    if "_service_thread" not in st.session_state:
        st.session_state._service_thread = None

    if "confirm_delete_session" not in st.session_state:
        st.session_state.confirm_delete_session = False

    # Sidebar needs a service to list existing sessions even before a thread
    # is active.  Use the latest existing thread or a placeholder.
    if st.session_state.current_thread_id is not None:
        service = get_or_create_service(st.session_state.current_thread_id)
    else:
        tmp_id = "___placeholder___"
        try:
            tmp_threads = get_or_create_service(tmp_id).list_threads()
            tmp_id = tmp_threads[-1] if tmp_threads else tmp_id
        except Exception as exc:  # noqa: BLE001 - never let a slow/down DB crash boot
            # POSTGRES/robustness: list_threads scans the checkpointer; if the
            # backend is slow or unreachable, boot with the placeholder session
            # instead of blowing up the whole UI.
            st.session_state["_session_load_error"] = str(exc)
        service = get_or_create_service(tmp_id)

    # Reconcile against the run that owns the turn, BEFORE the sidebar - see
    # _reconcile_with_persisted for why the ordering matters.
    _reconcile_with_persisted(service)

    # Render sidebar — its handlers may change the active thread
    render_sidebar(service)

    # If a New Session / session switch happened above, rebind the service for
    # the new thread and load that thread's saved history into the UI.
    if (
        st.session_state.current_thread_id is not None
        and st.session_state._service_thread != st.session_state.current_thread_id
    ):
        service = get_or_create_service(st.session_state.current_thread_id)
        st.session_state.chat_messages = service.get_chat_history()
        st.session_state._service_thread = st.session_state.current_thread_id
        st.session_state._seen_run_revision = service.run_revision()

    # Render UI
    render_main_chat(service)


if __name__ == "__main__":
    main()
