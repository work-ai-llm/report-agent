---
name: report-generation
description: Builds a pentest report from uploaded findings using the Scratchpad Assembly workflow. USE ONLY for larger reports (many findings, multi-section, needs an executive summary). SKIP for small/quick reports (a few findings, one-off summary) — handle those directly without loading this skill.
compatibility: Requires the virtual filesystem tools (ls, read_file, write_file) and the /context/ store.
allowed-tools: ls read_file write_file
---

# Report Generation (Scratchpad Assembly)

## When to Use
- The user asks to create, assemble, or export a **larger** pentest report.
- The report is multi-section (host inventory, AD infrastructure, findings, etc.) and an executive summary is expected.
- Findings are available under `/context/findings/` and a phase template under `/context/templates/`.

## When NOT to Use (SKIP this skill)
- **Small/quick reports** — a few findings files, a one-off summary, or a short answer. The scratchpad partitioning buys nothing on a short context; answer directly or assemble a simple single-file report without the multi-phase workflow.
- A single question about findings, templates, or the store — just answer, do not build a report.

## Critical Rules
1. **Static vs dynamic files**:
   - `/context/templates/` and `/context/interpretations/` are static reference files — read them only.
   - `/context/findings/` are dynamic, read-only source data. NEVER write to or modify them.
2. **Never write the final report in a single step.** Build it part-by-part to keep high quality and avoid context limits.
3. **Write the Executive Summary LAST**, after the technical sections are drafted.
4. **Cross-check against the sources before assembly.** A missing fact is a defect.

## Workflow

### Phase 1: Preparation
1. `ls("/context/findings")` to identify the uploaded data.
2. `read_file` each relevant finding file under `/context/findings/`.
3. `read_file` the template (`/context/templates/<phase>_report_template.md`) and the interpretation guide (`/context/interpretations/<phase>.md`).

### Phase 2: Draft Technical Sections (Out of Order)
Write each major technical section as a SEPARATE file in the ephemeral `/scratchpad/` directory:
- `write_file("/scratchpad/1_host_inventory.md", "...")`
- `write_file("/scratchpad/2_ad_infrastructure.md", "...")`
- `write_file("/scratchpad/3_security_findings.md", "...")`
DO NOT WRITE THE EXECUTIVE SUMMARY YET.

### Phase 3: The Executive Summary (Context-Aware)
1. `read_file` the technical sections you just created in `/scratchpad/`.
2. Now that you have the full context, write a highly accurate Executive Summary.
3. `write_file("/scratchpad/0_executive_summary.md", "...")`.

### Phase 4: Cross-Check Against the Sources
Before assembling anything:
1. Take **every** file under `/context/findings/` and list its distinctive
   evidence: numbers and counts, event ids, CVE ids, IP addresses, hostnames,
   account names, share and file names.
2. Confirm each item appears somewhere in the drafted sections in `/scratchpad/`.
   Re-read the source file whenever you are unsure - never verify from memory.
3. Fix every gap by rewriting the affected section file. Do not proceed until
   each source file is covered.
4. Do not add detail that no source file supports. If the template asks for
   something the findings do not cover, say so instead of inventing it.

### Phase 5: Final Assembly & Export
1. `read_file` all files from `/scratchpad/` in numerical order.
2. Concatenate them exactly according to the template structure.
3. Check the result: no empty heading, no duplicated heading level, no missing
   section.
4. Save the final compiled report to the persistent store: `write_file("/context/reports/<target>_<date>_final.md", concatenated_content)`.

## Constraints
- Do NOT execute bash commands.
- Do NOT modify or delete the original finding files in `/context/findings/`.
- Follow the template structure EXACTLY.
- Use evidence from findings verbatim where possible. Numbers, event ids and CVE
  ids must appear exactly as the source has them.
- Execute the entire Scratchpad Assembly workflow autonomously in a single continuous flow. DO NOT stop to ask the user for confirmation between steps. Continue calling tools until the final report is saved to `/context/reports/`.