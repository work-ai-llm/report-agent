"""Error rendering for the A2A CLI (issue #33).

A protocol-level refusal escaped ``main()`` unhandled, so an ordinary, correct
server rejection rendered as a Python traceback. The server's answer here is one
line of text; the terminal showed a stack trace.

The tests drive the real ``main()`` - real argparse, real ``asyncio.run``, real
subcommand - and stub only ``A2AClient.create``, which is where a server refusal
actually enters the client. Nothing opens a socket: the point is what the
terminal shows, not that the server refuses correctly.
"""

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


async def _return(value):
    return value