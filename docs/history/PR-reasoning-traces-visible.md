# Reasoning traces visible

Journal for surfacing model reasoning in the chat. **Delivered.**

Mirrors draft PR "Reasoning traces visible in the chat". Covers #18, scoped to
visibility only.

## Intent

Show what the agent was doing while it was thinking. The agent reasons heavily
— a trivial question produced thirty-six reasoning deltas for a single line of
answer — and all of it was discarded before it reached the UI.

## Why this branch went first

Reasoning is worth having whether or not tokens ever stream, so this work is
deliberately sequenced ahead of the streaming branch rather than after it.
Building it here put the reasoning on screen immediately; streaming then makes an
already-visible thing incremental instead of introducing it.

That also lowers the risk. Streaming is the highest-risk item on the list and
sits downstream of a channel problem that has already failed twice. This was not.

## Correction to the filed issue

#18 was scoped to the wrong backend for the demo, which changed the size of the
job substantially.

The filed issue describes the local OpenAI-compatible chat model discarding the
field at the model layer. That is accurate for the local backend. It did not
describe the backend the recording runs on: the OpenRouter integration already
surfaces the field, mapping the provider's reasoning into the message's
additional kwargs on both the streaming and non-streaming paths, and reporting
reasoning token counts in usage metadata.

**That premise turned out to be half wrong, and the recording proved it.** The
Langfuse trace for the recorded turn reports `model_provider: openai` with
`additional_kwargs: {}` on every AI message — it ran on llama.cpp, not
OpenRouter, so its reasoning was destroyed at the model layer rather than merely
unrendered. A renderer alone would have shown nothing.

## Why it did not need streaming work

With streaming off, the runner still yields node updates, and those carry the
completed assistant message with its reasoning attached. Render it from there and
reasoning is visible today, with no channel rework.

## What shipped

- `ReasoningChatOpenAI` keeps `reasoning_content` on the local backend.
- The run registry collects it per message, concatenating the deltas a streamed
  turn produces, and attaches it to the row the renderer draws.
- `render_chat_history` draws it collapsed, per message.
- `store_cli inspect-session` shows it, one line per message by default and in
  full under `--full-reasoning`.

Two of the three open questions from the original journal resolved themselves
once the code existed:

- The local-backend half of #18 stayed in scope and turned out to be necessary.
- Reasoning does reach the checkpointer — it always did. `store_cli` simply
  never printed `additional_kwargs`, so it looked absent rather than hidden.

It is display-only in the sense that matters: the agent never re-reads it.

One thing this work had to fix first, recorded here because it was found on the
way: assistant text was batched into a single block at the end of every turn
(#51). Reasoning sits behind that same path, so it would have been batched too.

## Dependencies

| Depends on | Why |
|---|---|
| PR #46 | Conflict source. Same render path as its sibling branches. |
| Streaming integration | Must land *after* this one. Sequencing, not a blocker. |

## Note

#22 (grouping traces per turn) is closed, so the observability cutaway is
unblocked. Worth checking that the grouped trace and the rendered reasoning agree
about what a turn contains — they do, both carrying the same
`additional_kwargs.reasoning_content`.

Reasoning text is the agent's own working commentary over the operator's
findings. It is now rendered, and therefore on camera.