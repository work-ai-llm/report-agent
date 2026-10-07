# Reasoning traces visible

Journal for surfacing model reasoning in the chat. **Parked, not started.**

Mirrors draft PR "Reasoning traces visible in the chat". Covers #18, scoped to
visibility only.

## Intent

Show what the agent was doing while it was thinking. The agent reasons heavily
— a trivial question produced thirty-six reasoning deltas for a single line of
answer — and today all of it is discarded before it reaches the UI.

## Why this branch goes first

Reasoning is worth having whether or not tokens ever stream, so this work is
deliberately sequenced ahead of the streaming branch rather than after it. Build
it here and the reasoning is on screen immediately; streaming then makes an
already-visible thing incremental instead of introducing it.

That also lowers the risk. Streaming is the highest-risk item on the list and
sits downstream of a channel problem that has already failed twice. This is not.

## Correction to the filed issue

#18 is scoped to the wrong backend for the demo, which changes the size of the
job substantially.

The filed issue describes the local OpenAI-compatible chat model discarding the
field at the model layer. That is accurate for the local backend. It does not
describe the backend the recording runs on: the OpenRouter integration already
surfaces the field, mapping the provider's reasoning into the message's
additional kwargs on both the streaming and non-streaming paths, and reporting
reasoning token counts in usage metadata.

So on the recorded backend the model layer is done and what remains is a
renderer.

## Why it does not need streaming work

With streaming off, the runner still yields node updates, and those carry the
completed assistant message with its reasoning attached. Render it from there and
reasoning is visible today, with no channel rework.

## Work left

- Render reasoning from the messages the runner already emits, collapsed by
  default so it does not compete with the answer.
- Decide whether the local-backend half of #18 stays in scope here or splits off.
  It does not affect the recorded backend.
- Decide whether reasoning reaches the checkpointer, so the session inspection
  tool can show it too, or stays display-only.

## Dependencies

| Depends on | Why |
|---|---|
| PR #46 | Conflict source. Same render path as its sibling branches. |
| Streaming integration | Must land *after* this one. Sequencing, not a blocker. |

## Note

#22 (grouping traces per turn) is closed, so the observability cutaway is
unblocked. Worth checking that the grouped trace and the rendered reasoning agree
about what a turn contains.

Reasoning text is the agent's own working commentary over the operator's
findings. If it is rendered, it is on camera.