
class TestV2DispatchAgainstRealLangGraph:
    """The v2 StreamPart shape, taken from LangGraph itself rather than a fake.

    The other tests here hand-write ``StreamPart`` dicts, so they only prove the
    adapter reads what those dicts say. This drives a real ``create_agent`` graph
    and feeds its actual ``astream(stream_mode=[...], version="v2")`` output
    through the same two helpers, so a langgraph upgrade that changed the shape
    fails here instead of silently rendering nothing in production.

    Uses only langchain's own fake model - no network, no keys.
    """

import asyncio

import pytest

from streamlit_ui.services import real_backend as rb


class TestV2DispatchAgainstRealLangGraph:
    """The v2 StreamPart shape, taken from LangGraph itself rather than a fake.

    The other tests here hand-write ``StreamPart`` dicts, so they only prove the
    adapter reads what those dicts say. This drives a real ``create_agent`` graph
    and feeds its actual ``astream(stream_mode=[...], version="v2")`` output
    through the same two helpers, so a langgraph upgrade that changed the shape
    fails here instead of silently rendering nothing in production.

    Uses only langchain's own fake model - no network, no keys.
    """

    def test_both_channels_arrive_from_one_stream(self):
        from langchain.agents import create_agent
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage, AIMessageChunk

        def get_weather(city: str) -> str:
            """Get weather for a given city."""
            return f"It's always sunny in {city}!"

        class _Fake(GenericFakeChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

        async def drain():
            model = _Fake(messages=iter([
                AIMessage(content="Let me check that."),
                AIMessage(content="It is always sunny in SF!"),
            ]))
            agent = create_agent(model=model, tools=[get_weather])

            counts: dict = {}
            tokens: list = []
            node_dicts: list = []
            async for part in agent.astream(
                {"messages": [{"role": "user", "content": "weather in SF?"}]},
                stream_mode=["messages", "updates"],
                version="v2",
            ):
                kind, payload = rb._classify_part(part)
                counts[kind] = counts.get(kind, 0) + 1
                if kind == "messages":
                    token = payload[0]
                    if isinstance(token, AIMessageChunk):
                        tokens.extend(rb._streamed_text(token))
                elif kind == "updates":
                    node_dicts.append(
                        isinstance(payload, dict) and bool(payload)
                        and all(isinstance(v, dict) for v in payload.values())
                    )
            return counts, tokens, node_dicts

        counts, tokens, node_dicts = asyncio.run(drain())

        assert counts.get("messages", 0) > 0, "no token parts arrived"
        assert counts.get("updates", 0) > 0, "no state updates arrived"
        assert "".join(tokens).strip(), f"no text deltas extracted from {counts}"
        # The updates payload must be the node-dict shape _iter_messages walks.
        assert all(node_dicts), "updates parts were not node-dicts"
