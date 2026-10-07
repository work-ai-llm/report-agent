# Better sidebar

Journal for the sidebar rework: the agent filesystem panel and the scratchpad
panel. Landed in draft PR #49.

## Intent

The sidebar should show what the agent can actually see, not two hardcoded
slices of it. Two issues, #40 and #41, against two different storage backends.

## The load-bearing finding

The two halves of this work read **different storage**, which is why they were
two issues rather than one feature.

The filesystem is a composite backend. Only the context prefix is routed to the
persistent store; every other path falls through to an in-memory backend.

The first attempt assumed the in-memory half kept its files in a state channel
in the agent's checkpoint namespace, which is what the backend's own source
suggests. **That assumption was wrong for this project.** The graph is built
with langchain's `create_agent`, so that state channel is never declared, and no
`files` channel appears in any checkpoint or pending write — verified against a
real SQLite database for a session that wrote four scratchpad files and read
them all back mid-run. The scratchpad exists only for the life of a run and
cannot be read back from the store or the checkpointer at any prefix.

That read path was removed rather than left in place. Dead code with passing
tests is worse than no code, because it hides the fact that the feature does
not work.

## What was built instead

The scratchpad is reconstructed by replaying the session's own persisted tool
calls. Two details made that work:

- Persisted history entries are **flat** — `name` and `args` at the top level —
  where the live event nests them under `data`. Reading only the nested shape
  is why the panel rendered empty against real data while the tests passed.
- Replaying only `write_file` and `edit_file` calls, filtered to the scratchpad
  prefix, keeps the panel honest: a substitution-style edit with no known prior
  content is dropped rather than rendered as invented text.

Because the history is persisted and saved incrementally, the panel survives a
reload, a session switch and a restart, and it updates as a turn completes.

## Also in scope

Fixed the prefix stripping in the two existing list methods. They used a global
string replace rather than a prefix strip, so a file whose name contains the
prefix as a substring displayed under the wrong name — and a later write using
that displayed name targeted a different key.

Display paths carry the `/context/` root back. The routing layer strips it
before storing, so a naive panel shows a path the agent never says out loud when
it lists that directory.

## Dependencies, as they landed

- **PR #46** rewrote the sidebar render path and was merged first, as planned.
  The rebase was clean and additive.
- **#30** landed in the same merge train, which made the sidebar repaint after a
  turn. That removed the staleness this work would otherwise have inherited.

## What it still does not do

The scratchpad panel does not update *during* a run. Live-turn rendering is a
`st.fragment`, and fragments do not re-execute the rest of the script, so the
sidebar renders once and stays put until the rerun at turn end. Putting the
panel in its own fragment would fix it.