# Streaming integration

Journal for the live-streaming work in the Streamlit chat. **Parked, not started.**

Mirrors draft PR "Streaming integration: live tokens and tool calls in the chat
window". Covers the streaming half of milestone M1-1: #17 and #19, plus the
streaming half of #18.

## Intent

Tokens, tool calls and tool results should render while the model is still
working, rather than the answer arriving whole at the end.

Today the flag is plumbed but inert. Tool calls already appear live because the
UI consumes plain node-diff updates from the agent. Tokens do not, because the
runner invokes the agent's stream without a stream mode, which leaves the
renderer's token branch unreachable.

## Why it is parked

PR #46 rewrites the session-syncing and streaming render path in Streamlit, and
adds over two thousand lines. Both this branch and the sidebar branch sit
exactly in that area. Starting before it merges means rebasing twice for no
gain.

The same dependency applies to the reasoning-visibility branch, which also
touches the render path.

## What is already known

The original failure is understood and was not a middleware bug. The consumer
never received the v2 stream format, so LangGraph emitted the older multiplexed
shape. The consumer unpacked the mode name as if it were a message chunk, the
type guard failed, and both channels were dropped — including the tool-call
updates that had been arriving correctly all along.

The fix was verified live against the local model server: the v2 format emits
token, tool-call and tool-result events as expected.

`AGENTS.md` carries a warning not to retry this blind. Honour it — re-verify
against the current stack before building on the method.

## Work left

- Replace the runner's node-diff stream with a combined-mode stream.
- Dispatch on the typed event rather than inferring from tuple shapes.
- Emit token, tool-call and tool-result events to the existing renderer.
- Emit one event per tool call when calls arrive batched in one message.
- Do not double-draw the tool-result message, which also appears on the
  messages channel.

## Dependencies

| Depends on | Why |
|---|---|
| PR #46 | Conflict source. Must merge first. |
| Reasoning visibility (#18, split) | Lands first by intent, so this makes an already-visible thing incremental. |
| #20 widget click during streaming | Guards the widgets this branch makes more interactive; same run loop. |
| #29, #30 | Cheap, and make a failed or completed run visible — which is what streaming gets judged against. |