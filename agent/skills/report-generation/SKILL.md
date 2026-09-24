---
name: report-generation
description: Builds a complete pentest report from uploaded findings using the Scratchpad Assembly workflow. Use when creating, assembling, or exporting a pentest report.
compatibility: Requires the virtual filesystem tools (ls, read_file, write_file) and the /context/ store.
allowed-tools: ls read_file write_file
---

# Report Generation (Scratchpad Assembly)

## When to Use
- The user asks to create, assemble, or export a pentest report.
- Findings are available under `/context/findings/` and a phase template under `/context/templates/`.

## Critical Rules
1. **Static vs dynamic files**:
   - `/context/templates/` and `/context/interpretations/` are static reference files — read them only.
   - `/context/findings/` are dynamic, read-only source data. NEVER write to or modify them.
2. **Never write the final report in a single step.** Build it part-by-part to keep high quality and avoid context limits.
3. **Write the Executive Summary LAST**, after the technical sections are drafted.

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

### Phase 4: Final Assembly & Export
1. `read_file` all files from `/scratchpad/` in numerical order.
2. Concatenate them exactly according to the template structure.
3. Save the final compiled report to the persistent store: `write_file("/context/reports/<target>_<date>_final.md", concatenated_content)`.

## Constraints
- Do NOT execute bash commands.
- Do NOT modify or delete the original finding files in `/context/findings/`.
- Follow the template structure EXACTLY.
- Use evidence from findings verbatim where possible.
- Execute the entire Scratchpad Assembly workflow autonomously in a single continuous flow. DO NOT stop to ask the user for confirmation between steps. Continue calling tools until the final report is saved to `/context/reports/`.