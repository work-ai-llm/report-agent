# The report-generation skill (Scratchpad Assembly)

Not current truth — a record of an approach and the experiments run against it.
For how the agent is wired today see [../architecture.md](../architecture.md).

## The approach

The skill lives in `agent/skills/report-generation/SKILL.md`. The agent sees only
its `name` and `description` in the system prompt; the body is read on demand
through `read_file`, so a skill costs nothing until it is chosen.

The workflow it prescribes:

1. **Preparation** — `ls` and `read_file` every finding file under
   `/context/findings/`, plus the phase template and the interpretation guide.
2. **Technical sections, out of order** — each major section is drafted as its own
   file under `/scratchpad/`. No executive summary yet.
3. **Executive summary last** — the drafts are read back first, so the summary is
   written against finished sections instead of from memory.
4. **Cross-check** — every source file's distinctive evidence is verified present in
   the drafts; gaps are fixed by rewriting the affected section.
5. **Assembly** — the parts are concatenated per the template and saved to
   `/context/reports/`.

Scope gating lives in the `description`: the skill is for larger reports, and says
explicitly to skip it for small or quick ones.

## The hypothesis: read→write proximity

Stated as intuition, not as a result: **a fact is attended to better when it is
recent in the token stream.** A monolithic `write_file` of a whole report puts
finding #1 tens of thousands of generated tokens away from the sentences that
need it. Drafting section by section re-reads the source immediately before
writing, shortening that distance, and it lets the executive summary be written
with the finished sections in hand instead of reconstructed from memory.

Caveat, stated once and applying to everything below: n=1 per arm, one local 9B
model (`ornith`), synthetic findings. Exploratory, not conclusive.

## Experiment 1 — with and without the skill, small scale

Design: 2 task shapes × 2 agent configs. WITH = the real runtime (skills
middleware + gated prompt). WITHOUT = same runtime, skills middleware removed and
every skill reference stripped from the prompt. Each run got its own store so no
run could read another's report. Both arms saw the same seeded context: 5 finding
files, the recon template, and the guide.

| run | skill loaded | tools | scratchpad writes | exec summary | report chars | facts kept | time |
|---|---|---|---|---|---|---|---|
| small WITH | no — gate worked | 8 | 0 | — | 3 457 | 10/10 | 4.9m |
| small WITHOUT | n/a | 13 | 0 | — | 4 450 | 10/10 | 6.4m |
| large WITH | yes | 21 | 4 | written last | 14 155 | 9/10 | 24.2m |
| large WITHOUT | n/a | 13 | 0 | — | 16 136 | 10/10 | 14.3m |

Observations:

- **The gate works.** The small run announced that it skipped the skill and
  answered directly — and was leaner for it (8 tool calls against 13).
- **The large run followed the workflow exactly**: skill read first, sections
  drafted out of order, executive summary written after the sections, then
  assembly.
- **On output quality at this scale the skill lost on every measured axis**:
  shorter report, one fact fewer (the RDP brute-force counts — 412 attempts,
  203 attempts, event 4625 — were dropped somewhere during part drafting), an
  empty `## Executive Summary` heading left behind by concatenation, 1.7× the
  wall time, 62% more tool calls.
- Template adherence was identical (5 of 10 sections in both).

At roughly 2 000 words the split-and-reassemble overhead is pure cost: the
hypothesis never activated at this scale, which is exactly what the gate encodes.

## What was missing: cross-checking

The original workflow had no verification step at all. Phase 3 read the *drafts*
back, not the sources, to write the summary; phase 4 concatenated and saved.
Nothing ever asked whether a fact present in a source file survived into the
report. The dropped numbers above are what that gap predicts.

The cross-check phase (step 4 above) was added to close it: walk every source
file, list its distinctive evidence — numbers, counts, CVE ids, IP addresses,
hostnames, account names, file names — confirm each appears in the drafts, re-read
the source whenever unsure, rewrite the affected section, and refuse to invent
anything the sources do not support.

## Experiment 1 rerun — cross-check added

Same procedure, cross-check phase in place, run against a second synthetic
scenario: 5 finding files, 60 curated facts, seeded into the filestore with
`dev_tools/store_cli.py` and cleaned with it afterwards.

One caveat about the method: both arms share one store, and the first attempt at
the WITHOUT arm found the WITH arm's finished report sitting in
`/context/reports/`, verified it, and scored *it* — no write of its own. The
baseline was re-run against a freshly seeded store. The store is shared state,
not a sandbox.

| | WITH skill | WITHOUT skill |
|---|---|---|
| tool calls | 40 | 17 |
| facts kept | **56/60** | 51/60 |
| per source file | nmap 13/13, bloodhound 10/10, smb 9/13, web 11/11, auth 13/13 | nmap 13/13, bloodhound 10/10, smb 11/13, **web 4/11**, auth 13/13 |
| invented tokens | `192.168.1.0` (the `/24` target-network notation — benign) | `192.168.1.0` + **`CVE-2021-40438`** — a CVE in no source file |
| report | 28 055 chars / 4 130 words | 17 035 chars / 2 349 words |
| template sections | 9/9 | 5/9 |
| evidence blocks | 14 | 10 |
| wall time | ~150 min (first attempt cut at 90 min, resumed from checkpoint) | 18 min |

What changed:

- **The ranking flipped.** In experiment 1 the skill lost on facts, 9/10 against
  10/10. With cross-checking it keeps more: 93% against 85%.
- **The baseline's loss is an entire source file.** The WITHOUT arm kept only 4
  of 11 web-enumeration facts — `/server-status`, `mod_status`, `/backup/`,
  `TRACE`, `PHP/7.4.33`, `HSTS`, `443` never made it into the report. It kept the
  juicy parts (default credentials, the CVE) and dropped the texture. That is the
  predicted failure mode of a single monolithic pass, and it appeared already at
  5 files and ~2 300 words — earlier than expected.
- **The baseline also invented a CVE.** `CVE-2021-40438` appears in no finding
  file. The WITH run invented nothing beyond the `/24` notation.
- **The skill's own losses were self-inflicted.** Its first draft of the findings
  section listed every share file name; the later rewrite into the template's
  finding-table format dropped four of them (`org_chart.pdf`, `vpn-guide.pdf`,
  `salaries_2026.xlsx`, `payroll_runlist.csv`). It then audited the assembled
  report by grepping 16 tokens it had chosen itself — those four were not in its
  pattern, so it passed its own check. A check whose coverage the agent picks
  cannot catch what it did not think to look for.
- **The cross-check was implemented differently than written.** The skill tells
  the agent to re-read each source file; it instead grepped the finished report
  for a token list. Cheaper, and it did catch structural problems.
- **The structural defect is milder but not gone.** One empty heading survives
  (`## Executive Summary`, its subsections sitting at the same level), and the
  severity headings sit at the same level as their parent. Content intact,
  structure still not clean.
- **Cost:** roughly 8× the wall time of the baseline.

Read: at 5 files the skill finally earns its keep on fact retention, on template
coverage and on not inventing CVEs — but it pays in time, and a chunk of its own
losses come from rework it performed *after* drafting. That argues for doing the
template-conformance pass before the content pass, not after.

## Decision

Gate the skill, which is done — the gate lives in the `description`, mirrored in
the `reports.md` skills section. Treat the value claim above ~2 200 words as
untested: at that scale the workflow is overhead, and the honest conclusion from
experiment 1 is that it should not run.

## Experiment 2 — pre-registered, not yet run

More finding files: 7 files, 60–70 facts, drawn from the synthetic scenario in
`results/findings/`. Fact retention is the headline metric, alongside an
invention scan for report tokens (IP addresses, CVE ids) that trace to no source
file.

**Pre-registered prediction: 2nd experiment expects a flip in fact retention on
higher findings.**

Prepared, not run. See the script docstring in
`tests/experiments/skill_gate_behavior.py` for the exact commands.

## What would change the conclusion

- The skill wins once a single generation is too long to hold every fact in
  active attention while composing — beyond that, expect recall and fidelity
  gains, not speed gains.
- Invention is a different failure mode from omission. It is driven by template
  pressure (a section demanding content no finding covers) rather than by memory
  decay, and a presence checklist does not catch it. If the rerun invents facts,
  the cross-check needs a negative rule.
- Every number here comes from one small model. A larger model shifts all of
  these thresholds to the right.