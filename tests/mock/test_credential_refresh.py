"""Tests for the credential-refresh path behind issue #13.

A rotated API key used to be a hard outage: the chat client captured the key at
construction, so a server holding a stale value 401'd until the key was re-synced
AND the process restarted. These tests cover the recovery mechanism - a retry
middleware that re-reads the credential once when a call fails with an auth
error - and the property that makes it cheap: the live client is updated in
place, so the already-compiled graph picks the new key up without being rebuilt.
"""

import pytest

from agent import report_agent as ra
from agent.report_agent import (
    CredentialRefreshRetryMiddleware,
    _looks_like_auth_error,
)


class _FakeAuthError(Exception):
    """Stands in for the openai SDK's AuthenticationError without importing it."""

    status_code = 401


class _FakeOtherError(Exception):
    status_code = 500


class _FakeClient:
    def __init__(self, api_key):
        self.api_key = api_key


class _FakeModel:
    def __init__(self, api_key):
        self.root_client = _FakeClient(api_key)
        # ChatOpenAI builds a separate async client, and the agent is async
        # end to end, so a fake with only root_client would not catch a refresh
        # that updated the client nobody actually calls.
        self.root_async_client = _FakeClient(api_key)


def _first_text(response) -> str:
    """Pull the text out of the ModelResponse the retry middleware returns."""
    result = getattr(response, "result", None) or []
    if result:
        return str(getattr(result[0], "content", result[0]))
    return str(response)


class TestAuthErrorDetection:
    def test_recognises_an_sdk_auth_error_by_name(self):
        class AuthenticationError(Exception):
            pass

        assert _looks_like_auth_error(AuthenticationError()) is True

    def test_recognises_401_by_status_code(self):
        assert _looks_like_auth_error(_FakeAuthError()) is True

    def test_recognises_403_by_status_code(self):
        err = _FakeOtherError()
        err.status_code = 403
        assert _looks_like_auth_error(err) is True

    def test_ignores_unrelated_failures(self):
        assert _looks_like_auth_error(_FakeOtherError()) is False
        assert _looks_like_auth_error(ValueError("boom")) is False

    def test_sees_through_a_wrapped_cause(self):
        # Middleware and SDK layers wrap freely; a 401 three levels down still
        # has to count, or the refresh never fires in the real stack.
        try:
            try:
                raise _FakeAuthError()
            except _FakeAuthError as inner:
                raise RuntimeError("call failed") from inner
        except RuntimeError as outer:
            assert _looks_like_auth_error(outer) is True

    def test_survives_a_self_referential_exception(self):
        err = _FakeOtherError()
        err.__cause__ = err
        assert _looks_like_auth_error(err) is False


class TestRefreshIsInPlace:
    """The compiled graph binds the model object, so it must not be replaced."""

    def test_updates_the_existing_client_rather_than_rebuilding(self):
        runtime = ra.AgentRuntime()
        runtime.llm_backend = "openai_local"
        model = _FakeModel("OLD")
        runtime.llm = model
        runtime.llm_small = _FakeModel("OLD")

        assert runtime._reload_llm_credential() is True
        # Same object, new key - this is what lets an already-compiled graph
        # see the change without create_agent being called again.
        assert runtime.llm is model
        assert runtime.llm.root_client.api_key != "OLD"

    def test_updates_the_async_client_too(self):
        """Regression guard: the sync client is not the one being called.

        ChatOpenAI keeps a separate `root_async_client`, and the agent runs async
        throughout (LangGraph, and the UI's EventLoopThread). A refresh that
        only touched `root_client` updated an object no request ever uses: the
        key looked refreshed in tests, and every real call kept sending the old
        one. Found by running against a live endpoint - see
        tests/experiments/exp_credential_refresh_live.py.
        """
        runtime = ra.AgentRuntime()
        runtime.llm_backend = "openai_local"
        runtime.llm = _FakeModel("OLD")
        runtime.llm_small = _FakeModel("OLD")

        assert runtime._reload_llm_credential() is True
        assert runtime.llm.root_async_client.api_key != "OLD"
        assert runtime.llm_small.root_async_client.api_key != "OLD"

    def test_updates_both_clients_on_a_real_chatopenai(self):
        # Same check against the real class, so a future change to how
        # langchain-openai names its clients is caught here.
        from langchain_openai import ChatOpenAI

        runtime = ra.AgentRuntime()
        runtime.llm_backend = "openai_local"
        runtime.llm = ChatOpenAI(
            model="x", base_url="http://localhost:1/v1", api_key="OLD", streaming=True
        )
        runtime.llm_small = runtime.llm

        assert runtime._reload_llm_credential() is True
        assert runtime.llm.root_client.api_key != "OLD"
        assert runtime.llm.root_async_client.api_key != "OLD"
        # The header the async client will actually send has to reflect it.
        assert "OLD" not in str(runtime.llm.root_async_client.auth_headers)

    def test_skips_mistral(self):
        # Mistral keeps its key inside its own client with no root_client
        # handle, so there is nothing safe to update; a 401 there falls through
        # to the normal retry path and fails as it did before.
        runtime = ra.AgentRuntime()
        runtime.llm_backend = "mistral"
        runtime.llm = _FakeModel("OLD")
        runtime.llm_small = _FakeModel("OLD")
        assert runtime._reload_llm_credential() is False
        assert runtime.llm.root_client.api_key == "OLD"

    def test_tolerates_a_model_with_no_client(self):
        # A backend whose model exposes no root_client must not crash the
        # middleware; the refresh simply finds nothing to update.
        runtime = ra.AgentRuntime()
        runtime.llm_backend = "openai_local"
        runtime.llm = object()
        runtime.llm_small = None
        assert runtime._reload_llm_credential() is False


class TestRefreshRetryMiddleware:
    def _middleware(self, refresh, **overrides):
        # Zero delays: these tests assert attempt counts, not wall-clock.
        kwargs = dict(
            max_retries=3,
            retry_on=(Exception,),
            on_failure="raise",
            initial_delay=0.0,
            backoff_factor=1.0,
            max_delay=0.0,
            jitter=False,
        )
        kwargs.update(overrides)
        return CredentialRefreshRetryMiddleware(refresh=refresh, **kwargs)

    def _counting_handler(self, failures, exc_factory, succeed_with="ok"):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) <= failures:
                raise exc_factory()
            return succeed_with

        return handler, calls

    def test_recovers_without_waiting_out_the_backoff(self):
        handler, calls = self._counting_handler(1, _FakeAuthError)
        mw = self._middleware(lambda: True)
        assert mw.wrap_model_call(object(), handler) == "ok"
        # The whole point: the request that follows a successful refresh is not
        # delayed by the first backoff interval, because the previous failure
        # was informative rather than a flake.
        assert len(calls) == 2

    def test_refresh_runs_only_on_an_auth_error(self):
        refreshed = []
        mw = self._middleware(lambda: refreshed.append(1) or True)
        handler, _ = self._counting_handler(1, _FakeOtherError, succeed_with="ok")
        assert mw.wrap_model_call(object(), handler) == "ok"
        assert refreshed == []

    def test_preserves_the_stock_attempt_count(self):
        """Regression guard: the loop here must not be shorter than the parent.

        An earlier version tried the request once and delegated, which silently
        cut a 5xx from four attempts to one. Separately, a missing `import time`
        made the first backoff raise NameError, so the middleware gave up after
        a single request in production - where initial_delay is 2.0s - while
        passing every test that used a zero delay.
        """
        from langchain.agents.middleware import ModelRetryMiddleware

        def real_attempts(middleware, exc_factory):
            count = [0]

            def handler(request):
                count[0] += 1
                raise exc_factory()

            try:
                middleware.wrap_model_call(object(), handler)
            except Exception:
                pass
            return count[0]

        stock = ModelRetryMiddleware(
            max_retries=3,
            retry_on=(Exception,),
            on_failure="raise",
            initial_delay=0.0,
            backoff_factor=1.0,
            max_delay=0.0,
            jitter=False,
        )
        mine = self._middleware(lambda: False)

        for exc_factory in (_FakeOtherError, _FakeAuthError):
            assert real_attempts(mine, exc_factory) == real_attempts(stock, exc_factory)

    def test_backoff_path_does_not_raise_name_error(self):
        # Same guard, aimed straight at the missing-import bug: with a non-zero
        # delay the loop has to reach time.sleep and survive it. Before the fix
        # this raised NameError on the first backoff, so the middleware gave up
        # after one request in production (initial_delay is 2.0s there) while
        # every zero-delay test still passed.
        mw = self._middleware(lambda: False, initial_delay=0.01)
        count = [0]

        def handler(request):
            count[0] += 1
            raise _FakeOtherError()

        mw.wrap_model_call(object(), handler)
        assert count[0] == 4

    def test_refresh_adds_at_most_one_extra_attempt_per_slot(self):
        # A refresh that keeps reporting a change must not multiply requests
        # without bound; the configured retry budget still governs.
        count = [0]

        def handler(request):
            count[0] += 1
            raise _FakeAuthError()

        mw = self._middleware(lambda: True, max_retries=1)
        mw.wrap_model_call(object(), handler)
        # 1 initial + 1 immediate + the single configured retry + its immediate.
        assert count[0] == 4

    def test_an_unchanged_credential_still_retries_and_then_reports(self):
        # With a genuinely bad key there is nothing to gain from a refresh, but
        # the retry schedule must run to completion regardless - and then the
        # failure surfaces the way the stock middleware surfaces it.
        count = [0]

        def handler(request):
            count[0] += 1
            raise _FakeAuthError()

        mw = self._middleware(lambda: False, max_retries=3)
        result = mw.wrap_model_call(object(), handler)
        assert count[0] == 4
        assert "4" in str(_first_text(result))

    def test_a_broken_refresh_does_not_change_the_outcome(self):
        count = [0]

        def handler(request):
            count[0] += 1
            raise _FakeAuthError()

        def bad_refresh():
            raise RuntimeError("refresh exploded")

        mw = self._middleware(bad_refresh, max_retries=3)
        result = mw.wrap_model_call(object(), handler)
        # The refresh failure is reported but the original error still governs
        # the retry schedule - it must not be masked or short-circuited.
        assert count[0] == 4
        assert _first_text(result)

    @pytest.mark.asyncio
    async def test_async_path_recovers_without_backoff(self):
        calls = []

        async def handler(request):
            calls.append(1)
            if len(calls) == 1:
                raise _FakeAuthError()
            return "ok"

        mw = self._middleware(lambda: True)
        assert await mw.awrap_model_call(object(), handler) == "ok"
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_async_backoff_path_does_not_raise_name_error(self):
        count = [0]

        async def handler(request):
            count[0] += 1
            raise _FakeOtherError()

        mw = self._middleware(lambda: False, initial_delay=0.01)
        await mw.awrap_model_call(object(), handler)
        assert count[0] == 4

    def test_is_a_model_retry_middleware(self):
        # It replaces the stock middleware in the agent's stack, so it has to
        # keep the parent's contract.
        from langchain.agents.middleware import ModelRetryMiddleware

        assert issubclass(CredentialRefreshRetryMiddleware, ModelRetryMiddleware)

    def test_on_failure_error_reraises_the_underlying_exception(self):
        """The failure mode the previous 'raise' value was hiding.

        _init_middleware used on_failure='raise', which is not a value
        _handle_failure recognises - only 'error' or a callable re-raises.
        Anything else formats the failure into a ModelResponse holding an
        AIMessage, so an exhausted retry budget reached the agent as a
        model-authored error string rather than an exception. That is what made
        a rotated key (#13) look like a strange reply instead of a failure.
        """
        mw = self._middleware(lambda: False, max_retries=0, on_failure="error")

        def handler(request):
            raise _FakeAuthError()

        with pytest.raises(_FakeAuthError):
            mw.wrap_model_call(object(), handler)

    def test_init_middleware_configures_error_not_raise(self):
        import inspect

        source = inspect.getsource(ra.AgentRuntime._init_middleware)
        assert "on_failure='error'" in source or 'on_failure="error"' in source
        assert "on_failure='raise'" not in source


class TestRuntimeWiring:
    def test_init_middleware_builds_the_credential_aware_middleware(self):
        # Guards against _init_middleware silently reverting to the stock
        # class, which would compile fine and simply never refresh - a
        # regression with no other symptom.
        import inspect

        source = inspect.getsource(ra.AgentRuntime._init_middleware)
        assert "CredentialRefreshRetryMiddleware(" in source
        assert "refresh=self._reload_llm_credential" in source
        # The stock class must not still be constructed here as well.
        assert "ModelRetryMiddleware(" not in source.replace(
            "CredentialRefreshRetryMiddleware(", ""
        )

    def test_credential_refresh_is_wired_into_the_middleware(self):
        runtime = ra.AgentRuntime()
        runtime.llm_backend = "openai_local"
        runtime.llm = _FakeModel("OLD")
        runtime.llm_small = _FakeModel("OLD")
        mw = CredentialRefreshRetryMiddleware(
            refresh=runtime._reload_llm_credential,
            max_retries=1,
            retry_on=(Exception,),
            on_failure="raise",
        )
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                raise _FakeAuthError()
            return "ok"

        assert mw.wrap_model_call(object(), handler) == "ok"
        assert runtime.llm.root_client.api_key != "OLD"