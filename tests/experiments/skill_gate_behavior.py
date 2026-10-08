"""
Skill behaviour experiment - report-generation skill (Scratchpad Assembly)
=========================================================================

Experiment 1 (done, results in docs/history/report-generation-skill.md):
    skill vs no-skill at small scale. The skill lost on every output metric;
    the gate (small report -> skip skill) was verified.

Experiment 1 rerun (DONE - results in docs/history/report-generation-skill.md):
    same procedure, cross-check phase added, 5 finding files / 60 facts.
    Result: the skill kept 56/60 facts vs 51/60 without it, dropped a
    hallucinated CVE, and covered 9/9 template sections against 5/9. Its own
    losses were 4 file names dropped while reformatting a section to the
    template - after drafting, and missed by its own grep-based audit.

Experiment 2 (PREPARED, NOT RUN):
    the same two arms at 7 finding files / 70 facts. Files dns_recon.txt and
    gpo_report.txt plus their 10 facts exist, but were never seeded into the
    store and never run. Its prediction is pre-registered in
    docs/history/report-generation-skill.md. To run it:
        python dev_tools/store_cli.py --sqlite seed --dir agent/context
        python dev_tools/store_cli.py --sqlite seed --dir results
        python tests/experiments/skill_gate_behavior.py --only large-with
        python dev_tools/store_cli.py --sqlite wipe --namespace filesystem
    IMPORTANT: one arm per store. Both arms share a store by design, so the
    second arm finds the first arm's finished report in /context/reports/ and
    will score it instead of writing its own (this happened once). Either wipe
    between arms or point REPORT_AGENT_DB_PATH at a fresh file per arm.

Store
-----
Both arms share ONE store (agent/agent_session.db, the default), which is
seeded by the real dev tool - this script no longer seeds anything itself:

    python dev_tools/store_cli.py --sqlite seed --dir agent/context
    python dev_tools/store_cli.py --sqlite seed --dir results
    python dev_tools/store_cli.py --sqlite inspect --show-size

Both arms write the same target report file, so the report is read and scored
immediately after each run, before the next run overwrites it. Clean up when
done:

    python dev_tools/store_cli.py --sqlite wipe --namespace filesystem

Measurements per run
--------------------
  skill_loaded       read_file("/skills/report-generation/SKILL.md") happened?
  scratchpad_writes  write_file() calls into /scratchpad/
  exec_summary_pos   position of the executive-summary write (skill: LAST)
  post_draft_reads   /context/findings reads AFTER the last scratchpad write -
                     the cross-check phase, if the agent ran it
  facts              curated checklist: how many source facts reached the report
  invented           IPs / CVE ids in the report that appear in no source file
  report_chars, tool_calls, wall_time_s

Run
---
    python tests/experiments/skill_gate_behavior.py --only large-with
    python tests/experiments/skill_gate_behavior.py --only large-without
"""
import asyncio
import os
import re
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

EXP_DIR = Path(__file__).parent
TMP = EXP_DIR / ".exp_tmp"
TMP.mkdir(exist_ok=True)

FINDINGS_DIR = ROOT / "results" / "findings"
FIRST_FINDING = "nmap_scans.txt"

from agent.report_agent import AgentRuntime
import agent.report_agent as agent_mod

REPORTS_MD = (ROOT / "agent" / "reports.md").read_text(encoding="utf-8")

FS_PROMPT_NO_SKILLS = (
    "You have access to a persistent virtual filesystem. "
    "Use 'ls' to explore directories like '/context/templates/' or '/context/interpretations/' "
    "to discover available phases. Use 'read_file' to retrieve the exact content you need."
)

LARGE_PROMPT = (
    "Generate the complete reconnaissance-phase pentest report for target 192.168.1.50 "
    "(session sess_001). The findings are uploaded under /context/findings/. "
    "Use the template /context/templates/recon_report_template.md and the methodology guide "
    "/context/interpretations/recon.md. Produce the full report (host inventory, Active Directory "
    "infrastructure, security findings, executive summary, next steps) and save the final compiled "
    "report to /context/reports/sess_001_recon_final.md."
)

SMALL_PROMPT = (
    "Create a small report for session sess_002 summarizing the most important findings from the "
    "files in /context/findings/. Keep it short - a few lines per key finding plus a recommended "
    "priority. This is a quick summary, not a full assessment report."
)

# ---------------------------------------------------------------------------
# ground truth: curated per-file fact checklist.
#
# Trivia is deliberately excluded (uptime latencies). Numeric facts match on
# digit boundaries so "638" does not match inside "16380". Everything else is a
# case-insensitive substring. This is a RECALL measure - what the report failed
# to carry over. Fidelity (numbers silently changed) and invention (content
# with no source) are covered by the invented-token scan plus manual reading.
# ---------------------------------------------------------------------------
FACTS = {
    "nmap_scans.txt": [
        "192.168.1.50", "22/tcp", "OpenSSH 8.2p1", "80/tcp", "Apache httpd 2.4.41",
        "445/tcp", "Samba smbd 4.11.6", "3389/tcp", "192.168.1.51", "53/tcp",
        "ISC BIND 9.11.3", "88/tcp", "389/tcp",
    ],
    "bloodhound_dump.txt": [
        "CORP.LOCAL", "145", "42", "svc_backup", "Backup Operators", "Domain Admins",
        "SeBackupPrivilege", "192.168.1.50", "jdoe", "AS-REP",
    ],
    "smb_shares.txt": [
        "Samba smbd 4.11.6", "37 files", "org_chart.pdf", "vpn-guide.pdf", "118 files",
        "salaries_2026.xlsx", "payroll_runlist.csv", "4 files", "1.4 GB",
        "ntds.dit.bak", "2026-06-28", "nobody", "SeBackupPrivilege",
    ],
    "web_enum.txt": [
        "/server-status", "mod_status", "/backup/", "TRACE", "PHP/7.4.33",
        "/timesheet/", "v3.1", "admin:timesheet", "CVE-2021-26691", "HSTS", "443",
    ],
    "auth_logs.txt": [
        "4625", "638", "203.0.113.42", "svc_backup", "91", "192.168.1.77", "jdoe",
        "lockout", "4624", "571", "type 10", "198.51.100.9", "03:12",
    ],
    # --- experiment 2 only (not part of the 1-rerun checklist) ---
    "dns_recon.txt": [
        "2026091801", "ns1.corp.local", "fileserver.corp.local", "192.168.1.52", "192.168.1.53",
    ],
    "gpo_report.txt": [
        "8 characters", "Password complexity", "Password reuse", "svc_report", "DES encryption",
    ],
}

IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)
NUMERIC_RE = re.compile(r"[0-9][0-9.,:/#-]*$")

RUNS = {
    "large-with":    {"task": "LARGE", "with_skill": True,  "prompt": LARGE_PROMPT},
    "large-without": {"task": "LARGE", "with_skill": False, "prompt": LARGE_PROMPT},
    "small-with":    {"task": "SMALL", "with_skill": True,  "prompt": SMALL_PROMPT},
    "small-without": {"task": "SMALL", "with_skill": False, "prompt": SMALL_PROMPT},
}

CONTINUE_PROMPT = (
    "Continue the workflow from where you stopped. Do not summarize your work and do not "
    "ask questions - continue with the remaining phases until the final compiled report is "
    "saved to /context/reports/sess_001_recon_final.md."
)


def source_token_whitelist():
    ips, cves = set(), set()
    for fp in sorted(FINDINGS_DIR.glob("*.txt")):
        text = fp.read_text(encoding="utf-8")
        ips.update(IP_RE.findall(text))
        cves.update(c.upper() for c in CVE_RE.findall(text))
    return ips, cves


def fact_present(fact: str, text: str) -> bool:
    low = text.lower()
    if NUMERIC_RE.fullmatch(fact):
        pat = re.escape(fact).replace(r"\ ", r"\s+")
        return re.search(rf"(?<![0-9]){pat}(?![0-9])", low) is not None
    return fact.lower() in low


def score_report(text: str):
    per_file, missing = {}, []
    for fname, facts in FACTS.items():
        absent = [f for f in facts if not fact_present(f, text)]
        per_file[fname] = len(facts) - len(absent)
        missing.extend((fname, f) for f in absent)
    total = sum(len(v) for v in FACTS.values())
    kept = sum(per_file.values())

    src_ips, src_cves = source_token_whitelist()
    rep_ips = set(IP_RE.findall(text))
    rep_cves = {c.upper() for c in CVE_RE.findall(text)}
    return {
        "facts_kept": kept,
        "facts_total": total,
        "facts_per_file": per_file,
        "missing": missing,
        "invented_ips": sorted(rep_ips - src_ips),
        "invented_cves": sorted(rep_cves - src_cves),
        "report_chars": len(text),
    }


def strip_skills_from_prompt(text: str) -> str:
    """Remove every skill reference so the WITHOUT baseline never hears of skills."""
    text = re.sub(r"## SKILLS:.*?(?=\n## )", "", text, flags=re.S)
    text = re.sub(r"\n2\. LOAD and FOLLOW the `report-generation` skill[^\n]*", "", text)
    text = re.sub(
        r"3\. CONSTRUCT the report part-by-part using the skill's Scratchpad Assembly workflow\.",
        "3. CONSTRUCT the report part-by-part.", text,
    )
    return text


async def make_runtime(with_skill: bool, run_name: str):
    rt = AgentRuntime()
    await rt.initialize(debug_mode=False, force_sqlite=True)

    if await rt.store.aget(("filesystem",), f"/findings/{FIRST_FINDING}") is None:
        await close_runtime(rt)
        raise RuntimeError(
            "No findings in the store. Seed them first:\n"
            "  python dev_tools/store_cli.py --sqlite seed --dir agent/context\n"
            "  python dev_tools/store_cli.py --sqlite seed --dir results"
        )

    rt.system_prompt_text = REPORTS_MD if with_skill else strip_skills_from_prompt(REPORTS_MD)
    if not with_skill:
        rt.skills_middleware = None
        rt.fs_middleware._custom_system_prompt = FS_PROMPT_NO_SKILLS
    await rt._create_agent(debug_mode=False)
    return rt


async def close_runtime(rt):
    for attr in ("checkpointer", "store"):
        obj = getattr(rt, attr, None)
        conn = getattr(obj, "conn", None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------

async def tool_events(rt, thread_id):
    state = await rt.agent.aget_state({"configurable": {"thread_id": thread_id}})
    events = []
    for m in state.values.get("messages", []):
        if getattr(m, "type", "") == "ai":
            for tc in (getattr(m, "tool_calls", None) or []):
                events.append((tc.get("name"), tc.get("args") or {}))
    return events


async def grab_report(rt):
    """Read the report the run just wrote (both arms use the same target key)."""
    key = "/reports/sess_001_recon_final.md"
    item = await rt.store.aget(("filesystem",), key)
    if item is None:
        hits = [i for i in await rt.store.asearch(("filesystem",), prefix="/reports/")
                if i.key.endswith(".md")]
        if not hits:
            return None, None
        key = hits[0].key
        item = await rt.store.aget(("filesystem",), key)
    content = item.value.get("content", "")
    if isinstance(content, list):
        content = "\n".join(content)
    return key, content


def analyze(events, answer):
    skill_reads, sp_writes, rep_writes, ctx_reads = [], [], [], []
    seq = []
    for name, args in events:
        if name == "read_file":
            p = str(args.get("file_path", ""))
            if p.startswith("/skills/"):
                skill_reads.append(p)
            elif p.startswith("/context/"):
                ctx_reads.append(p)
            seq.append(("read", p))
        elif name == "write_file":
            p = str(args.get("file_path", ""))
            if p.startswith("/scratchpad/"):
                sp_writes.append(p)
            elif p.startswith("/context/"):
                rep_writes.append(p)
            seq.append(("write", p))

    exec_pos = next((i for i, p in enumerate(sp_writes) if "executive" in p.lower()), None)
    last_sp = max((i for i, (k, p) in enumerate(seq)
                   if k == "write" and p.startswith("/scratchpad/")), default=-1)
    post_draft = sum(1 for i, (k, p) in enumerate(seq)
                     if k == "read" and i > last_sp and p.startswith("/context/findings/"))
    return {
        "skill_loaded": bool(skill_reads),
        "tool_calls": len(events),
        "scratchpad_writes": len(sp_writes),
        "scratchpad_paths": sp_writes,
        "exec_summary_pos": exec_pos,
        "exec_summary_last": (exec_pos == len(sp_writes) - 1) if exec_pos is not None else None,
        "post_draft_finding_reads": post_draft,
        "report_writes": len(rep_writes),
        "report_paths": rep_writes,
        "findings_reads": sum(1 for p in ctx_reads if "/findings/" in p),
        "answer_chars": len(str(answer)),
    }


def print_trace(run_name, events, answer, metrics, score):
    print("\n" + "=" * 78)
    print(f"RUN {run_name}  —  tool trace ({len(events)} tool calls)")
    print("=" * 78)
    for i, (name, args) in enumerate(events, 1):
        if name in ("read_file", "write_file"):
            extra = f"  ({len(str(args.get('content', '')))} chars)" if name == "write_file" else ""
            print(f"  {i:>2}. {name:<10} {args.get('file_path', '')}{extra}")
        elif name == "ls":
            print(f"  {i:>2}. {name:<10} {args.get('path', '')}")
        else:
            print(f"  {i:>2}. {name:<10} {args}")
    print(f"  final answer ({metrics['answer_chars']} chars): "
          f"{str(answer)[:300].replace(chr(10), ' ')}{' ...' if metrics['answer_chars'] > 300 else ''}")
    if score:
        print(f"\n  FACTS {score['facts_kept']}/{score['facts_total']} kept "
              f"(report {score['report_chars']} chars)")
        for fname, kept in score["facts_per_file"].items():
            print(f"    {fname:<26} {kept}/{len(FACTS[fname])}")
        if score["missing"]:
            print("  MISSING:")
            for fname, f in score["missing"]:
                print(f"    {fname:<26} {f!r}")
        inv = score["invented_ips"] + score["invented_cves"]
        print(f"  INVENTED tokens (in report, in no source): {inv if inv else 'none'}")


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

async def run_one(run_name, spec, resume=False):
    print("\n" + "#" * 78)
    print(f"# {run_name}: task={spec['task']}  with_skill={spec['with_skill']}  resume={resume}")
    print("#" * 78, flush=True)

    rt = await make_runtime(spec["with_skill"], run_name)
    config = {"configurable": {"thread_id": run_name}}
    prompt = CONTINUE_PROMPT if resume else spec["prompt"]
    t0 = time.time()
    result = await rt.agent.ainvoke(
        {"messages": [{"role": "user", "content": prompt}]}, config=config,
    )
    dt = time.time() - t0
    answer = str(result["messages"][-1].content)

    events = await tool_events(rt, run_name)
    metrics = analyze(events, answer)

    report_key, report_text = await grab_report(rt)
    score = score_report(report_text) if report_text else None
    if report_text:
        (TMP / f"{run_name}_report.md").write_text(report_text, encoding="utf-8")

    metrics["wall_time_s"] = round(dt, 1)
    print_trace(run_name, events, answer, metrics, score)
    await close_runtime(rt)
    return {"run": run_name,
            **{k: v for k, v in metrics.items() if k != "report_paths"},
            "report_key": report_key, "score": score}


def print_summary(rows):
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    header = (f"{'run':<16}{'skill':<7}{'tools':<7}{'scratch':<9}{'exec@':<7}"
              f"{'xchk':<7}{'facts':<9}{'invented':<10}{'chars':<8}{'time'}")
    print(header)
    print("-" * len(header))
    for r in rows:
        s = r.get("score") or {}
        exec_at = "-" if r["exec_summary_pos"] is None else str(r["exec_summary_pos"])
        facts = f"{s.get('facts_kept', '-')}/{s.get('facts_total', '-')}"
        inv = len(s.get("invented_ips", [])) + len(s.get("invented_cves", []))
        print(f"{r['run']:<16}{'YES' if r['skill_loaded'] else 'no':<7}"
              f"{r['tool_calls']:<7}{r['scratchpad_writes']:<9}{exec_at:<7}"
              f"{r['post_draft_finding_reads']:<7}{facts:<9}{inv:<10}"
              f"{s.get('report_chars', '-'):<8}{r['wall_time_s']}s")
    print("-" * len(header))
    print("exec@ = position of the executive-summary write among scratchpad writes "
          "(skill: LAST). xchk = /context/findings reads after the last scratchpad write "
          "(the cross-check phase). invented = report IPs/CVEs found in no source file.")
    print()


async def main(only=None, resume=False):
    names = [only] if only else list(RUNS.keys())
    rows, failures = [], []
    for name in names:
        try:
            rows.append(await run_one(name, RUNS[name], resume=resume))
        except Exception as e:
            failures.append(name)
            print(f"\n!! RUN {name} FAILED: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    if rows:
        print_summary(rows)
    if failures:
        print(f"\nFailed runs: {failures}")
    return rows, failures


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=list(RUNS.keys()))
    ap.add_argument("--resume", action="store_true",
                    help="continue the named run from its checkpointed state instead of starting over")
    args = ap.parse_args()
    try:
        asyncio.run(main(only=args.only, resume=args.resume))
    except KeyboardInterrupt:
        print("\nINTERRUPTED")
        os._exit(130)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)   # aiosqlite/httpx threads would otherwise keep the process alive