"""Error rendering for the A2A CLI (issues #33 and #34).

The same defect seen from two sides: a protocol-level refusal and a Ctrl-C both
escaped ``main()`` unhandled, so ordinary outcomes rendered as Python
tracebacks. #33 covers the A2AError path, #34 the KeyboardInterrupt one.

The tests drive the real ``main()`` - real argparse, real ``asyncio.run``, real
subcommand - and stub only ``A2AClient.create``, which is where a server refusal
actually enters the client. Nothing opens a socket: the point is what the
terminal shows, not that the server refuses correctly.
"""

import asyncio
import sys

import pytest
from a2a.utils.errors import (
    A2AError,
    InternalError,
    InvalidParamsError,
    UnsupportedOperationError,
)

from dev_tools import a2a_cli


class _FakeClient:
    """Stands in for A2AClient; every method here is a no-op that succeeds."""

    def __init__(self, subscribe_error=None):
        self._subscribe_error = subscribe_error

    async def create(self, *a, **k):  # pragma: no cover - replaced in the fixture
        return self

    async def close(self):
        return None

    async def subscribe_to_task(self, task_id):
        if self._subscribe_error is not None:
            raise self._subscribe_error
        return task_id, "ctx", "artifact text"


@pytest.fixture
def run_cli(monkeypatch):
    """Invoke a2a_cli.main() with a stubbed client; return the process exit code.

    SystemExit is consumed here and turned into a return value, so tests can
    assert on the code. Anything else - a real bug - propagates.
    """

    def _run(argv, *, client=None):
        fake = client or _FakeClient()
        monkeypatch.setattr(a2a_cli.A2AClient, "create", staticmethod(
            lambda *a, **k: _async_return(fake)
        ))
        monkeypatch.setattr(
            sys, "argv", ["a2a_cli.py", "--server", "http://127.0.0.1:1", *argv]
        )
        try:
            a2a_cli.main()
        except SystemExit as exc:
            return exc.code
        return 0

    async def _async_return(value):
        return value

    return _run


def _code_for(run_cli, capsys, argv, error):
    """Run one command whose client raises `error`; return (exit code, stderr)."""
    code = run_cli(argv, client=_FakeClient(subscribe_error=error))
    return code, capsys.readouterr().err


class TestA2AErrorRendering:
    """#33: a protocol refusal is an answer, not a crash."""

    def test_prints_one_line_and_exits_non_zero(self, run_cli, capsys):
        code, err = _code_for(
            run_cli, capsys, ["subscribe", "--task", "t1"], A2AError("boom")
        )
        assert code == 1
        assert "[server] A2AError: boom" in err
        # The whole defect: no traceback on camera.
        assert "Traceback" not in err
        assert "most recent call last" not in err

    def test_error_goes_to_stderr(self, run_cli, capsys):
        code, err = _code_for(
            run_cli, capsys, ["subscribe", "--task", "t1"], A2AError("boom")
        )
        assert code == 1
        assert "[server] A2AError: boom" in err

    @pytest.mark.parametrize(
        "error_cls", [InvalidParamsError, UnsupportedOperationError, InternalError]
    )
    def test_any_a2a_error_subclass_is_handled(self, run_cli, capsys, error_cls):
        code, err = _code_for(
            run_cli, capsys, ["subscribe", "--task", "t1"], error_cls("nope")
        )
        assert code == 1
        assert f"[server] {error_cls.__name__}: nope" in err

    @pytest.mark.parametrize(
        "error_cls", [InvalidParamsError, UnsupportedOperationError]
    )
    def test_terminal_state_subscribe_gets_a_hint(self, run_cli, capsys, error_cls):
        # The server's own message ("not supported") does not say what to do
        # next; the hint names the command that works.
        # a2a-sdk 1.1.0 answers a terminal-state subscribe with
        # InvalidParamsError, not the documented UnsupportedOperationError, so
        # both are mapped.
        _code, err = _code_for(
            run_cli, capsys, ["subscribe", "--task", "t1"], error_cls("terminal")
        )
        assert "hint:" in err
        assert "get --task" in err

    def test_hint_is_scoped_to_the_command_that_hit_it(self, capsys):
        # The same error class on another command is not a terminal-state
        # problem, so it must not be described as one.
        from dev_tools.a2a_cli import _A2A_ERROR_HINTS

        assert ("get", "InvalidParamsError") not in _A2A_ERROR_HINTS

    def test_non_a2a_errors_are_not_swallowed(self, run_cli):
        # Only protocol errors are rendered. A genuine bug must still surface as
        # a traceback rather than being dressed up as a server message.
        with pytest.raises(ValueError, match="a real bug"):
            run_cli(["subscribe", "--task", "t1"], client=_BoomClient(ValueError("a real bug")))


class _BoomClient(_FakeClient):
    def __init__(self, exc):
        super().__init__()
        self._exc = exc

    async def subscribe_to_task(self, task_id):
        raise self._exc


class TestSuccessPath:
    def test_success_does_not_exit_non_zero(self, monkeypatch, capsys):
        fake = _FakeClient()
        monkeypatch.setattr(a2a_cli.A2AClient, "create", staticmethod(
            lambda *a, **k: _return(fake)
        ))
        monkeypatch.setattr(
            sys, "argv", ["a2a_cli.py", "--server", "http://127.0.0.1:1",
                          "subscribe", "--task", "t1"]
        )
        try:
            a2a_cli.main()
        except SystemExit as exc:
            # SystemExit(0) or None are both fine; only a failure code is not.
            assert exc.code in (0, None)
        out = capsys.readouterr().out
        assert "artifact text" in out
        assert "[server]" not in out


class TestKeyboardInterruptRendering:
    """#34: Ctrl-C is a disconnect, not a crash."""

    def test_prints_one_clean_line_and_exits_130(self, run_cli, capsys):
        code = run_cli(
            ["subscribe", "--task", "t1"],
            client=_BoomClient(KeyboardInterrupt()),
        )
        err = capsys.readouterr().err
        # 130 is the conventional shell code for SIGINT.
        assert code == 130
        assert "Traceback" not in err
        assert "KeyboardInterrupt" not in err

    def test_message_states_that_the_server_keeps_working(self, run_cli, capsys):
        # The property being demonstrated is that disconnecting does not kill the
        # work, so the terminal should say so rather than leave it implied. A
        # bare "disconnected" reads like the work was cancelled.
        run_cli(["subscribe", "--task", "t1"], client=_BoomClient(KeyboardInterrupt()))
        err = capsys.readouterr().err.lower()
        assert "disconnected" in err
        assert "server" in err

    def test_is_not_reported_as_a_server_error(self, run_cli, capsys):
        # A local Ctrl-C is not something the server said, so it must not share
        # the [server] prefix or the exit code of a protocol refusal.
        run_cli(["subscribe", "--task", "t1"], client=_BoomClient(KeyboardInterrupt()))
        assert "[server]" not in capsys.readouterr().err

    def test_goes_to_stderr(self, run_cli, capsys):
        run_cli(["subscribe", "--task", "t1"], client=_BoomClient(KeyboardInterrupt()))
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "disconnected" in captured.err.lower()

    def test_handles_interrupt_raised_inside_a_running_task(self):
        """The real shape of a Ctrl-C: raised while a coroutine is mid-flight.

        asyncio.run() cancels the main task and re-raises KeyboardInterrupt out
        of itself, so the handler has to sit *outside* the asyncio.run call. A
        handler wrapped around the individual protocol calls would never see it.
        """
        import inspect

        source = inspect.getsource(a2a_cli.main)
        run_call = source.index("asyncio.run")
        handler = source.index("except KeyboardInterrupt")
        assert handler > run_call, "KeyboardInterrupt must be caught outside asyncio.run"

        async def interrupted():
            raise KeyboardInterrupt

        # Precondition for the ordering above to matter: the interrupt does
        # escape asyncio.run rather than being swallowed inside it.
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(interrupted())


async def _return(value):
    return value