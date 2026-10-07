# History

Parked-work notes. **Not current truth.**

These are PR journals kept for provenance: what was attempted, in what order, and
why a decision was made at the time. They are preserved because that reasoning
is sometimes the only record of why the code looks the way it does.

They are also, by construction, stale. They describe the code as it stood on a
particular branch at a particular moment. File paths, line numbers, and claims
about which symbols exist have all drifted since.

Do not use them to learn how the system works. Use:

| Question | Read |
|---|---|
| What does this project do? | [../overview.md](../overview.md) |
| How is it built? | [../architecture.md](../architecture.md) |
| What can go wrong? | [../troubleshooting.md](../troubleshooting.md) |
| What are the traps and invariants? | [../../AGENTS.md](../../AGENTS.md) |
| What changed when? | [../changelog.md](../changelog.md) |

---

## Contents

- **`PR-a2a-client.md`** — the journal for building `client/a2a_client.py`: the
  task-lifecycle coverage, and the reasoning behind the non-streaming `send`
  path. Predates the client's landing on master; it still refers to a push
  receiver and several test files that were never merged.
- **`PR-a2a-acceptance.md`** — the journal for the A2A acceptance campaign.
  Superseded in practice by `tests/acceptance/7_a2a_client_acceptance.py`, which
  covers the same lifecycle more cleanly.