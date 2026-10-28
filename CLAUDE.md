# Project Instructions — CC Project Folder

## Agent Tool Calls — Mandatory Context Injection

Before every Agent tool call, execute these two steps:

**Step 1 — Read memory index.**
Call the Read tool on `C:\Users\johnp\.claude\projects\E--Claude-Projects-CC-Project-Folder\memory\MEMORY.md`. Identify all entries relevant to the subagent's task and read those memory files.

**Step 2 — Include AGENT_RULES.md verbatim in the subagent prompt.**
Read `E:\Claude Projects\CC Project Folder\AGENT_RULES.md` and paste its full contents into the subagent prompt. Then append the relevant memory entries from Step 1.

A subagent prompt that does not include AGENT_RULES.md content is a protocol violation.

---

## Route and Verify Workflow — EXECUTE ALL SIX STEPS EVERY TIME, IN ORDER, WITHOUT EXCEPTION

**Trigger:** Only the exact phrase "Route and Verify" from the user starts this workflow. No other phrasing ("run the script", "apply it", "run route_fanout_vias.py on the board", etc.) triggers it.

### CRITICAL BEHAVIOR RULES — VIOLATION OF ANY OF THESE IS A FAILURE

1. **DO NOT ask the user to do anything.** Every step is Claude's responsibility.
2. **DO NOT skip Step 1 (clear).** Never route on a board with leftover tracks.
3. **DO NOT run any script more than once.** Run each once, capture full output.
4. **DO NOT present results without completing Steps 4 AND 5.** DRC alone is insufficient.
5. **DO NOT narrate the steps.** Execute silently, report when done.

### WHAT TO DO WHEN "Route and Verify" IS SAID

Execute steps 1–6 in sequence immediately. No confirmation. No pausing. No asking.

---

**Step 1 — Clear all unlocked tracks and vias**

```
"C:\Program Files\KiCad\10.0\bin\python.exe" -c "import sys; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages'); import pcbnew; board=pcbnew.LoadBoard(r'E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb'); to_remove=[t for t in board.GetTracks() if not t.IsLocked()]; [board.Remove(t) for t in to_remove]; board.Save(board.GetFileName())"
```

---

**Step 2a — Run route_fanout_vias.py --apply (once)**

```
"C:\Program Files\KiCad\10.0\bin\python.exe" "E:\Claude Projects\CC Project Folder\Python Scripts\route_fanout_vias.py" --apply 2>&1
```

---

**Step 2b — Run route_critical.py --apply (once)**

```
"C:\Program Files\KiCad\10.0\bin\python.exe" "E:\Claude Projects\CC Project Folder\Python Scripts\route_critical.py" --apply 2>&1
```

---

**Step 2c — Run route_highspeed.py --apply (once)**

```
"C:\Program Files\KiCad\10.0\bin\python.exe" "E:\Claude Projects\CC Project Folder\Python Scripts\route_highspeed.py" --apply 2>&1
```

---

**Step 3 — Run DRC and parse results**

```
"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe" pcb drc --output "C:/Temp/drc_fanout.json" --format json "E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb"
```

Parse `C:/Temp/drc_fanout.json` and report violation counts grouped by type. Zone-fill artifacts (track vs GND zone) are expected and not a stop condition. New copper-to-copper or shorting violations are blocking.

---

**Step 4 — Inspect the board programmatically**

**"Inspect the board" means execute Python that reads the actual `.kicad_pcb` file. Never reason from script output, memory, or what the script "should" have done.**

Run pcbnew API Python. At minimum verify:
- Via and track counts match script output
- Every fanout escape trace starts at a pad (not free space) and ends dangling
- No pad has both a via AND an escape trace
- HS pair tracks are on In2.Cu only — no segments on F.Cu or B.Cu
- No HS net has a via between its fanout anchor and its destination anchor (no mid-trace vias)

Only after passing both DRC and board inspection may results be presented.

---

## "Run route_fanout_vias.py on the entire board"

Execute exactly two steps: (1) clear, (2) fanout --apply. No DRC, no inspection.

---

## "Run route_critical.py" (or equivalent phrasing)

Execute exactly three steps in order:
1. Clear all unlocked tracks and vias
2. `route_fanout_vias.py --apply`
3. `route_critical.py --apply`

Never run route_critical.py alone — it needs fanout vias as routing anchors.

---

## "Run route_highspeed.py" (or equivalent phrasing)

Execute exactly four steps in order:
1. Clear all unlocked tracks and vias
2. `route_fanout_vias.py --apply`
3. `route_critical.py --apply`
4. `route_highspeed.py --apply`

Never skip the upstream steps — HS routing uses fanout vias as endpoints and power vias from route_critical as obstacles.

**To debug a specific pair** (algorithm verification): run `debug_highspeed.py`.

Two modes — set `FULL_SETUP` at the top of the file:
- `FULL_SETUP = True` (first run): clear all → fanout → critical → **snapshot all track fingerprints to hs_debug_baseline.json** → highspeed --pairs TARGET_PAIRS. No locking — fanout/critical tracks stay unlocked.
- `FULL_SETUP = False` (fast iteration): load snapshot → remove all tracks NOT in snapshot (removes HS routes, keeps fanout/critical unlocked) → highspeed --pairs TARGET_PAIRS.

Workflow: run once with `FULL_SETUP = True`, inspect, change algorithm, switch to `FULL_SETUP = False`, iterate quickly without re-running fanout/critical.

Do NOT run DRC or Route and Verify after debug_highspeed.py — the board shows only TARGET_PAIRS routed, not the full HS set.

---

---

## All Routing Scripts — Mandatory Pre-Edit Protocol

Before writing or modifying ANY code in ANY routing script in `Python Scripts/`, execute these steps in order. Skipping any step is a failure. This applies to route_fanout_vias.py, route_critical.py, route_highspeed.py, and any future routing script.

**Step A — Read the PROHIBITED list.**
Read the NEVER-AGAIN LIST in `E:\Claude Projects\CC Project Folder\SESSION_CONTEXT.md`. Every prohibited pattern must be fresh in context before any code is written.

**Step B — State which PROHIBITED items are relevant to the planned change.**
In the response to the user, before any code, explicitly list:
- Which PROHIBITED patterns (by number) could be violated by the planned change
- One sentence per pattern explaining how the planned code avoids it

If you cannot articulate how the planned code avoids each relevant PROHIBITED pattern, do not write the code. Re-read the requirements and redesign the approach first.

**Step C — Write the code.**
Only after Steps A and B are complete.

This protocol is non-negotiable. A code edit to any routing script that is not preceded by Steps A and B in the same response is a protocol violation.

---

## Git — Automatic Push After Significant Changes

After every significant edit to any file in `Python Scripts/` or any plan/context file, automatically:
1. `git add` the changed files
2. `git commit` with a concise message describing the change
3. `git push`

Do this without being asked. A "significant change" is any edit that modifies behavior, fixes a bug, or adds a feature. Do NOT commit after trivial read operations or failed experiments that were reverted.

---

## Placement Algorithm Rules

### No project-specific fixes for algorithmic placement problems

When a placement algorithm produces wrong results, never propose project-specific workarounds (e.g., forced_rotations, hardcoded component lists, inline face hints, named overrides) as the solution. Always find and implement the general algorithmic fix that works for any project without configuration.

**Why:** The scripts in this folder are designed to be reusable across projects. A project-specific fix silently breaks for any future project that doesn't share the same component names or topology.

**How to apply:** Before proposing any fix, ask: "Would this work on a completely different PCB with different component references?" If the answer is no, it's a project-specific fix — discard it and find the algorithmic root cause instead. This applies even when the algorithmic fix is harder to implement.
