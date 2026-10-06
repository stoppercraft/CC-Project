# AGENT_RULES.md — Mandatory Rules for All Subagents
# Include this file verbatim in every Agent tool call prompt.

---

## --apply Workflow — EXECUTE ALL FOUR STEPS IN ORDER, NO EXCEPTIONS

When `--apply` is said:

**Step 1 — Clear board**
```
"C:\Program Files\KiCad\10.0\bin\python.exe" -c "import sys; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages'); import pcbnew; board=pcbnew.LoadBoard(r'E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb'); [board.Remove(t) for t in list(board.GetTracks()) if not t.IsLocked()]; board.Save(board.GetFileName())"
```

**Step 2 — Run script once, full output captured**
```
"C:\Program Files\KiCad\10.0\bin\python.exe" "E:\Claude Projects\CC Project Folder\Python Scripts\route_fanout_vias.py" --pcb "E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb" --apply 2>&1
```
Run ONCE. Never re-run to filter output.

**Step 3 — Run DRC**
```
"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe" pcb drc --output "C:/Temp/drc_fanout.json" --format json "E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb"
```
Parse `C:/Temp/drc_fanout.json`. Report violation counts by type. Fix new non-zone violations before presenting results.

**Step 4 — Inspect board programmatically**
Run Python using pcbnew API to verify:
- Every escape trace starts at a pad (not free space)
- Every escape trace ends dangling (not on a wrong-net pad/via)
- No pad has both a placed via AND an escape trace
- Track counts match script output

Never present results without completing all four steps.

### CRITICAL BEHAVIOR RULES
1. DO NOT ask the user to do anything. Every step is Claude's responsibility.
2. DO NOT skip Step 1 (clear). No exceptions, including dry runs.
3. DO NOT run Step 2 twice.
4. DO NOT present results without Steps 3 AND 4 complete.
5. DO NOT narrate steps. Execute silently, report when done.

---

## route_fanout_vias.py — Mandatory Pre-Edit Protocol

Before writing or modifying ANY code in `route_fanout_vias.py`:

**Step A** — Read the NEVER-AGAIN LIST: call `Read` tool on `E:\Claude Projects\CC Project Folder\SESSION_CONTEXT.md`. Do NOT recall from memory — read the actual file.

**Step B** — Before any code, explicitly state which PROHIBITED patterns (1–7) are relevant to the planned change and one sentence per pattern explaining how the planned code avoids it.

**Step C** — Write the code only after A and B.

### NEVER-AGAIN (PROHIBITED) Patterns Summary
1. `skip_ref` — skipping same-footprint pads from obstacle checks
2. Endpoint via check scoped only to `skip_ref` pads
3. Arbitrary-angle escape directions (must be 0°, 45°, or 90° only)
4. Double-counted clearance in pad obstacle checks
5. Bounding-circle pad geometry (`obs.r = hypot(half_w, half_h) + clearance`) used for clearance checks
6. `GetBoundingBox()` AABB used for rotated pads
7. Incoherent HS pair handoff when one partner gets escape=0

---

## Git — Automatic Push After Significant Changes

After every significant edit to any file in `Python Scripts/` or any plan/context file:
1. `git add` changed files
2. `git commit` with concise message
3. `git push`

Do this without being asked. "Significant" = modifies behavior, fixes a bug, or adds a feature.

---

## Placement Algorithm Rules

Never propose project-specific workarounds (forced_rotations, hardcoded component lists, named overrides) for algorithmic problems. Always find the general fix that works on any PCB without configuration.

Before proposing any fix, ask: "Would this work on a completely different PCB with different component references?" If no — it's project-specific. Discard it and find the algorithmic root cause.

---

## Feedback Rules (from past sessions)

- **Locked components:** KiCad-locked footprints must NEVER be moved or rotated by any script. No overrides, no exceptions.
- **Never run scripts without explicit user approval.** "Revert" means revert only.
- **Never run --apply scripts twice.** Pipe output in one command; never re-run to filter.
- **Always clear board before any routing script**, dry-run or --apply, no exceptions.
- **Always run DRC after --apply.** Never present results without confirming 0 violations.
- **Board inspection after --apply is mandatory.** DRC alone is insufficient.
- **Never state facts about code without reading it.** Always use Read tool on the actual source. Code is truth; context files can be stale.
- **Never save backup copies** of the PCB to the backups dir. Live file is the baseline.
- **Never run commands with runaway/destructive potential** (e.g., `find /`, `rm -rf` broad paths). Flag and propose safe alternative instead.
- **Debug script does not replace --apply workflow.** `debug_fanout.py` is algorithm verification only. Never tell user to reload KiCad after a debug script run — run --apply workflow first.
- **Debug vs --apply workflows are separate.** Never switch from debug to --apply without explicit instruction. Code change during debug → test with debug script, not --apply.
- **Never declare a problem geometrically/algorithmically impossible.** Find the upstream fix instead.
- **Every task with execution work must be delegated to a subagent.** Never execute directly in the orchestrator.
- **Never describe what code does from memory.** Always read the actual source first.

---

## Board Reference
- Live board: `E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb`
- Known-good reference: root-level `Frameline_Compute_V2.kicad_pcb` from zip `Frameline_Compute_V2-2026-09-21_100203.zip` (115 vias, F.Cu↔B.Cu 0.4mm drill)
- DRC output: `C:/Temp/drc_fanout.json`
- Script: `E:\Claude Projects\CC Project Folder\Python Scripts\route_fanout_vias.py`
- Session context: `E:\Claude Projects\CC Project Folder\SESSION_CONTEXT.md`
- Memory dir: `C:\Users\johnp\.claude\projects\E--Claude-Projects-CC-Project-Folder\memory\`
