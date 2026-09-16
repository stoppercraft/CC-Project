# Project Instructions — CC Project Folder

## --apply Workflow — EXECUTE ALL THREE STEPS EVERY TIME, IN ORDER, WITHOUT EXCEPTION

### CRITICAL BEHAVIOR RULES — VIOLATION OF ANY OF THESE IS A FAILURE

1. **DO NOT ask the user to do anything.** The user saying `--apply` is the only input required. Every step from that point is Claude's responsibility. Asking the user to clear the board, run DRC, confirm anything, or do any other step is WRONG.
2. **DO NOT skip Step 1 (clear).** Running the script on a board that still has tracks from a previous run produces meaningless results. Step 1 is not optional under any circumstance, including dry runs.
3. **DO NOT run Step 2 twice.** Run the script once, capture all output in that single command. Never re-run to filter output.
4. **DO NOT present results without completing Step 3 (DRC).** The script completing without errors does not mean the board is correct. DRC is the only valid measure of correctness. Never tell the user the result is good until DRC reports 0 non-zone violations.
5. **DO NOT narrate the steps.** Do not say "I'll clear the board now" or "Running DRC..." — just execute. Report results when all three steps are done.

### WHAT TO DO WHEN `--apply` IS SAID

Execute steps 1, 2, and 3 in sequence immediately. No confirmation. No pausing. No asking.

---

**Step 1 — Clear all unlocked tracks and vias from the PCB**

```
"C:\Program Files\KiCad\10.0\bin\python.exe" -c "import sys; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages'); import pcbnew; board=pcbnew.LoadBoard(r'E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb'); [board.Remove(t) for t in list(board.GetTracks()) if not t.IsLocked()]; board.Save(board.GetFileName())"
```

If this command fails, diagnose and fix it. Do not proceed to Step 2 until the board is confirmed cleared.

---

**Step 2 — Run the routing script with --apply (once, full output captured)**

```
"C:\Program Files\KiCad\10.0\bin\python.exe" "E:\Claude Projects\CC Project Folder\Python Scripts\route_fanout_vias.py" --pcb "E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb" --apply 2>&1
```

Run this command exactly once. Capture and retain the full output. Do not re-run it.

---

**Step 3 — Run DRC and parse results**

```
"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe" pcb drc --output "C:/Temp/drc_fanout.json" --format json "E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb"
```

Parse `C:/Temp/drc_fanout.json` and report violation counts grouped by type. Only after this step may results be presented to the user. If there are non-zone violations, report them — do not claim success.

---

## Placement Algorithm Rules

### No project-specific fixes for algorithmic placement problems

When a placement algorithm produces wrong results, never propose project-specific workarounds (e.g., forced_rotations, hardcoded component lists, inline face hints, named overrides) as the solution. Always find and implement the general algorithmic fix that works for any project without configuration.

**Why:** The scripts in this folder are designed to be reusable across projects. A project-specific fix silently breaks for any future project that doesn't share the same component names or topology.

**How to apply:** Before proposing any fix, ask: "Would this work on a completely different PCB with different component references?" If the answer is no, it's a project-specific fix — discard it and find the algorithmic root cause instead. This applies even when the algorithmic fix is harder to implement.
