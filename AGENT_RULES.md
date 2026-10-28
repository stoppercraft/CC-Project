# AGENT_RULES.md — Mandatory Rules for All Subagents
# Include this file verbatim in every Agent tool call prompt.

---

## Route and Verify — EXECUTE ALL SIX STEPS IN ORDER, NO EXCEPTIONS

When the user says exactly "Route and Verify" (no other phrasing triggers this workflow):

**Step 1 — Clear board**
```
"C:\Program Files\KiCad\10.0\bin\python.exe" -c "import sys; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages'); import pcbnew; board=pcbnew.LoadBoard(r'E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb'); to_remove=[t for t in board.GetTracks() if not t.IsLocked()]; [board.Remove(t) for t in to_remove]; board.Save(board.GetFileName())"
```

**Step 2a — route_fanout_vias.py --apply (once)**
```
"C:\Program Files\KiCad\10.0\bin\python.exe" "E:\Claude Projects\CC Project Folder\Python Scripts\route_fanout_vias.py" --apply 2>&1
```

**Step 2b — route_critical.py --apply (once)**
```
"C:\Program Files\KiCad\10.0\bin\python.exe" "E:\Claude Projects\CC Project Folder\Python Scripts\route_critical.py" --apply 2>&1
```

**Step 2c — route_highspeed.py --apply (once)**
```
"C:\Program Files\KiCad\10.0\bin\python.exe" "E:\Claude Projects\CC Project Folder\Python Scripts\route_highspeed.py" --apply 2>&1
```

Each script runs ONCE. Never re-run to filter output.

**Step 3 — Run DRC**
```
"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe" pcb drc --output "C:/Temp/drc_fanout.json" --format json "E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb"
```
Parse `C:/Temp/drc_fanout.json`. Report violation counts by type. Zone-fill artifacts are expected. New copper-to-copper or shorting violations are blocking.

**Step 4 — Inspect board programmatically**
Run Python using pcbnew API to verify:
- Via and track counts match script output
- Every fanout escape trace starts at a pad (not free space) and ends dangling
- No pad has both a placed via AND an escape trace
- HS pair tracks on In2.Cu only — no segments on F.Cu or B.Cu
- No HS net has a via mid-trace between its two endpoint fanout anchors

Never present results without completing all steps.

### CRITICAL BEHAVIOR RULES
1. DO NOT ask the user to do anything. Every step is Claude's responsibility.
2. DO NOT skip Step 1 (clear). No exceptions.
3. DO NOT run any script more than once.
4. DO NOT present results without Steps 3 AND 4 complete.
5. DO NOT narrate steps. Execute silently, report when done.

---

## "Run route_highspeed.py" — 4-step sequence

1. Clear all unlocked tracks and vias
2. route_fanout_vias.py --apply
3. route_critical.py --apply
4. route_highspeed.py --apply

Never skip upstream steps. To debug a specific pair: run debug_highspeed.py.
- FULL_SETUP = True (first run): clear all → fanout → critical → lock all tracks/vias → highspeed --pairs. Locks upstream so future clears only remove HS routes.
- FULL_SETUP = False (fast iteration): clear unlocked only → highspeed --pairs. Fanout/critical survive.
Do NOT run DRC or Route and Verify after debug_highspeed.py — board is partial.

---

## All Routing Scripts — Mandatory Pre-Edit Protocol

Before writing or modifying ANY code in ANY routing script in `Python Scripts/` (route_fanout_vias.py, route_critical.py, route_highspeed.py, or any future routing script):

**Step A** — Read the NEVER-AGAIN LIST: call `Read` tool on `E:\Claude Projects\CC Project Folder\SESSION_CONTEXT.md`. Do NOT recall from memory — read the actual file.

**Step B** — Before any code, explicitly state which PROHIBITED patterns (1–8) are relevant to the planned change and one sentence per pattern explaining how the planned code avoids it.

**Step C** — Write the code only after A and B.

### NEVER-AGAIN (PROHIBITED) Patterns Summary — All Routing Scripts
1. `skip_ref` — skipping same-footprint pads from obstacle checks
2. Endpoint via check scoped only to `skip_ref` pads
3. Arbitrary-angle escape directions (must be 0°, 45°, or 90° only)
4. Double-counted clearance in pad obstacle checks
5. Bounding-circle pad geometry (`obs.r = hypot(half_w, half_h) + clearance`) used for clearance checks
6. `GetBoundingBox()` AABB used for rotated pads
7. Incoherent HS pair handoff when one partner gets escape=0
8. `pcbnew.Refresh()` or any GUI-only pcbnew call without `if live:` guard — triggers wxWidgets debug alerts in headless subprocess mode

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
- **Never run the Route and Verify script twice.** Pipe output in one command; never re-run to filter.
- **Always clear board before any routing script**, no exceptions.
- **Always run DRC after writing to board.** Never present results without confirming 0 violations.
- **Board inspection after writing to board is mandatory.** DRC alone is insufficient.
- **Never state facts about code without reading it.** Always use Read tool on the actual source. Code is truth; context files can be stale.
- **Never save backup copies** of the PCB to the backups dir. Live file is the baseline.
- **Never run commands with runaway/destructive potential** (e.g., `find /`, `rm -rf` broad paths). Flag and propose safe alternative instead.
- **Debug script does not replace Route and Verify.** `debug_fanout.py` is algorithm verification only. Never tell user to reload KiCad after a debug script run — run Route and Verify first.
- **Debug workflow and Route and Verify are separate.** Never switch from debug to Route and Verify without explicit instruction. Code change during debug → test with debug script, not Route and Verify.
- **Never declare a problem geometrically/algorithmically impossible.** Find the upstream fix instead.
- **Never describe what code does from memory.** Always read the actual source first.

---

## Board Reference
- Live board: `E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb`
- Known-good reference: root-level `Frameline_Compute_V2.kicad_pcb` from zip `Frameline_Compute_V2-2026-09-21_100203.zip` (115 vias, F.Cu↔B.Cu 0.4mm drill)
- DRC output: `C:/Temp/drc_fanout.json`
- Script: `E:\Claude Projects\CC Project Folder\Python Scripts\route_fanout_vias.py`
- Session context: `E:\Claude Projects\CC Project Folder\SESSION_CONTEXT.md`
- Memory dir: `C:\Users\johnp\.claude\projects\E--Claude-Projects-CC-Project-Folder\memory\`
