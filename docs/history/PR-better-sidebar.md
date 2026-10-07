# Better sidebar

Journal for the sidebar rework. **Parked, not started.**

Mirrors draft PR "Sidebar: the whole virtual filesystem, plus the session
scratchpad". Covers #40 and #41.

## Intent

The sidebar should show what the agent can actually see, not two hardcoded
slices of it.

Today it shows uploaded findings and finished reports. Two things are missing:
the context library the agent is told to read (templates and interpretation
guides), and the agent's own working notes.

## The load-bearing finding

The two halves of this work read **different storage**, and that is the reason
they are two issues rather than one feature.

The filesystem is a composite backend. Only the context prefix is routed to the
persistent store; every other path falls through to an in-memory backend that
keeps its files in a LangGraph state channel. So:

| Path | Where it lives | How to read it |
|---|---|---|
| `/context/**` | store namespace `("filesystem",)` | `list_files` with an empty prefix — already supported |
| `/scratchpad/**` | LangGraph state channel `files` | checkpointer. A second read path. |

A store listing will never return a scratchpad file, at any prefix. Anyone
reaching for a prefix filter to find scratchpad files will conclude the files do
not exist.

## Consequences for the design

- The context half is small: one service method plus a grouped tree. The store
  adapter already supports listing everything.
- Display paths need care. The router strips the context prefix before storing,
  so a naive panel shows a path the agent never says out loud. Re-add the prefix
  for display so panel and narration agree.
- The scratchpad panel is read-only and must not write to the agent's checkpoint
  namespace. The UI keeps its own display copy elsewhere precisely so it does not
  clobber agent state.

## Two decisions not yet made

- Whether the scratchpad panel shows the whole session's scratchpad or only the
  current turn. State-backed files persist across turns rather than clearing per
  run, so these differ.
- How either panel refreshes. The sidebar renders before the chat input is
  handled, so a panel read at render time is stale by the same mechanism as #30.
  Either fix #30 first, or drive refresh from the event stream.

## Also in scope

Fix the prefix stripping in the two existing list methods. They use a global
string replace rather than a prefix strip, so a file whose name contains the
prefix as a substring displays under the wrong name — and a later write using
that displayed name targets a different key.

## Dependencies

| Depends on | Why |
|---|---|
| PR #46 | Conflict source. Must merge first. |
| #30 stale sidebar | Determines the refresh strategy for both panels. |
| #45 session selector remake | Reshapes the same sidebar. |

## Demo consideration

Scratchpad content is agent-authored commentary over the operator's findings.
Anything rendered in that panel is visible in the recording. Worth deciding
explicitly rather than discovering it on camera.