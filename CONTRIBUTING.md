# Contributing

Guidelines for opening pull requests in this repository so every change is easy to
review and trace. If a rule doesn't fit your case, use judgment and keep it simple —
these are conventions, not gatekeepers.

## Branch naming

Use a descriptive, kebab-case branch per unit of work:

```
pr-a2a-client
pr-phoenix-tracing
pr-push-receiver
```

- Prefix every feature/work branch with `pr-`.
- Follow with a short kebab-case slug of the feature. No numbering needed.
- Create the branch off `master` unless it explicitly builds on another open PR.

## Draft workflow

Park large or unfinished work as a **Draft PR**. This is the repo's normal cadence:

1. Open the PR as a **Draft** (GitHub: "Create draft pull request").
2. The body records the idea and current state (see below) even if nothing runs yet.
3. Keep it a Draft until it's truly ready — flip to "Ready for review" only when the
   checklist is done and acceptance tests pass.

A draft can gather reference code, document intent, and track dependencies long before
it becomes mergeable. That is expected and encouraged here.

## PR title

Use a short, **descriptive title** — no `type:` prefix, no `PR #N:` numbering:

```
A2A Client - one client, full lifecycle (send/subscribe/get/cancel/push)
Arize Phoenix as tracing with sqlite
Streamlit UI Integration: main app, components, service layer (mock mode)
```

Say what the PR is/does in plain words. If it's related to a numbered PR you can
reference that number in the *body* instead of the title.

## PR body

Keep the body to roughly **three concise paragraphs** — enough to convey the idea, the
current state, and where it's going, without turning the PR into documentation. Use the
section headings as a guide; adapt them as needed. Three to five short sections using
the shared rhythm works well in practice:

1. **The idea** — what this PR represents / a short summary.
2. **Current state** — what is actually on the branch right now (files, status, what's
   explicitly not done yet).
3. **The direction** — end goal / work left / dependencies.

Reference useful headings (pick, don't use all):

- `## What this PR represents` / `## Summary`
- `## Current state on this branch` / `## What is in it`
- `## End goal (done-when)` / `## Work left`
- `## Dependencies`

Always reflect **dependencies and logic** somewhere in the body. If anything is a
placeholder or not wired yet, say so — a draft that states what's unfinished is far more
useful than one that hides it.

### Writing style: readable prose, not code-splurge

Say what the PR is in sentences a human can skim. The anti-pattern we hit (see PR #12)
is **code-splurge**: identifier-heavy, run-on prose that reads like pasted grep/diff
output — every file, line number, and method name crammed into a single wall of text,
with no topic sentence, no backticks, no paragraph rhythm.

Bad (anti-pattern — real excerpt from PR #12):

```
Receiver catches POST {url} json=MessageToDict(to_stream_response(event))
from SDK BasePushNotificationSender (agent/a2a_connector.py:369 +
InMemoryPushNotificationConfigStore). Sender is already complete on server;
this PR is the receiver the user client runs (port=0, terminal-only notify).
```

Why it's unreadable: it names every symbol but explains nothing; line-number
citations (`:369`) and camelCase calls sit mid-sentence; there is no first sentence
telling you what the paragraph is about, so the eye can't find the nouns.

Good (the same facts, written readably):

```
This PR is the client-side half of A2A push notifications: a webhook that
receives push events. The server already sends them via the SDK's
`BasePushNotificationSender` (`agent/a2a_connector.py`); what was missing was
the receiver a user client runs in-process.
```

Rules that make it readable:

**Structure (the skeleton):**

1. Every section and paragraph opens with a plain-English **topic sentence** — no
   identifiers, no `file:line`, no backticks. State the point first, add details after.
2. **One bullet = one idea.** The idea comes first; the file reference goes at the end
   in parentheses. Never open a bullet with `client/a2a_client.py:44`.
3. If a sentence can't be read aloud without pausing at every colon, it's several
   sentences — **split it**.

**Detail (the symbols):**

4. Use **backticks** around any identifier (`wait_for_completion`, not bare text).
5. Drop line-number citations and spec version numbers from prose (`:369`, `3.1.1`);
   they add no reading value. Keep enough reference for the reader to find the file.
6. **No chained symbol stacks.** Turn
   `create/send_message(3.1.1)/send_streaming_message(3.1.2)/subscribe/get/list/...`
   into a normal list, and name the *capability*, not every method.
7. You do not need to name every method to be accurate — name the ones that matter.

**Bullet discipline (one new rule to cap the list):**

8. Bullets are for **parallel, distinct claims only — max 3-5 per section**. If the
   same subject owns several bullets, merge them into one paragraph under a single lead
   bullet. Every list opens with a prose sentence stating the takeaway.

**Mechanism preservation (one new rule to keep the how):**

9. Before cutting a detail, classify it. **Decorative** — versions, line numbers,
   commit hashes — drops. **Load-bearing** — the transport (`httpx.ASGITransport`),
   the classes (`ReportA2AExecutor`, `DefaultRequestHandler`), env vars
   (`POSTGRES_URI`), entry points (`A2AClient.create`) — a developer needs to build or
   avoid it, so **preserve it, but relocate**: dense identifiers belong in a compact
   `Wiring / Key references` block at the end. In prose, a mechanism becomes one normal
   sentence, e.g. "the in-memory run goes over `httpx.ASGITransport`, same pattern as
   the mock suite."

Rule 8 stops the bullet-wall; rule 9 stops the silent loss of mechanism detail. Both
fix the two ways a rewrite heals the prose but damages the content.

## Draft PR docs

Long-lived draft PRs (work parked for later) keep a short `docs/PR-<name>.md` that
records intent, current state, remaining work, and dependencies. Mirror its key points
into the PR description. See `docs/PR-a2a-client.md` for the pattern.

## Checklist before "Ready for review"

- [ ] Branch follows `pr-<feature>`.
- [ ] Title is descriptive, plain language.
- [ ] Body reflects the idea, current state, and direction (3-paragraph rhythm).
- [ ] Body is readable prose — topic sentences, backticks on identifiers, no code-splurge.
- [ ] Bullets capped (3-5/section); mechanisms preserved in a `Wiring / Key references` block.
- [ ] Draft status + `docs/PR-*.md` used for parked/large work.
- [ ] No secrets committed; `.env` stays out of the repo.
