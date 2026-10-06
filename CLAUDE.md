# Project Instructions — CC Project Folder

## Agent Tool Calls — Mandatory Context Injection

Before every Agent tool call, execute these two steps:

**Step 1 — Read memory index.**
Call the Read tool on `C:\Users\johnp\.claude\projects\E--Claude-Projects-CC-Project-Folder\memory\MEMORY.md`. Identify all entries relevant to the subagent's task and read those memory files.

**Step 2 — Include AGENT_RULES.md verbatim in the subagent prompt.**
Read `E:\Claude Projects\CC Project Folder\AGENT_RULES.md` and paste its full contents into the subagent prompt. Then append the relevant memory entries from Step 1.

A subagent prompt that does not include AGENT_RULES.md content is a protocol violation.

---

## --apply Workflow — EXECUTE ALL THREE STEPS EVERY TIME, IN ORDER, WITHOUT EXCEPTION

### CRITICAL BEHAVIOR RULES — VIOLATION OF ANY OF THESE IS A FAILURE

1. **DO NOT ask the user to do anything.** The user saying `--apply` is the only input required. Every step from that point is Claude's responsibility. Asking the user to clear the board, run DRC, confirm anything, or do any other step is WRONG.
2. **DO NOT skip Step 1 (clear).** Running the script on a board that still has tracks from a previous run produces meaningless results. Step 1 is not optional under any circumstance, including dry runs.
3. **DO NOT run Step 2 twice.** Run the script once, capture all output in that single command. Never re-run to filter output.
4. **DO NOT present results without completing Steps 3 AND 4.** The script completing without errors does not mean the board is correct. DRC alone is not sufficient — a passing DRC does not catch logical errors like a pad getting both a placed via AND a spurious escape trace. Always inspect the board programmatically after DRC.
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

Parse `C:/Temp/drc_fanout.json` and report violation counts grouped by type. If there are new non-zone violations (compared to baseline), investigate and fix before proceeding.

---

**Step 4 — Inspect the board programmatically**

After DRC, inspect the board to confirm the script placed exactly what was intended. DRC alone is insufficient — it does not catch logical errors.

**CRITICAL: "Inspect the board" means execute Python code that reads the actual `.kicad_pcb` file and reports facts from it. It does NOT mean:**
- Reasoning from script output text
- Consulting memory files or the known-good reference
- Recalling what the script "should" have done

If the user asks "does pad X have a via?" — run Python to check the board, then answer. Never answer from memory.

Run a Python one-liner or short script using the pcbnew API. At minimum, verify:

- Every escape trace starts at the intended pad (not in free space)
- Every escape trace ends dangling (not on a wrong-net pad/via — that would be a short DRC might miss)
- No pad has both a placed via AND an escape trace (redundant/conflicting placement)
- Track counts match what the script reported emitting

Only after passing both DRC and board inspection may results be presented to the user.

---

## route_fanout_vias.py — Mandatory Pre-Edit Protocol

Before writing or modifying ANY code in `route_fanout_vias.py`, execute these steps in order. Skipping any step is a failure.

**Step A — Read the PROHIBITED list.**
Read the NEVER-AGAIN LIST in `E:\Claude Projects\CC Project Folder\SESSION_CONTEXT.md`. All 7 prohibited patterns must be fresh in context before any code is written.

**Step B — State which PROHIBITED items are relevant to the planned change.**
In the response to the user, before any code, explicitly list:
- Which PROHIBITED patterns (by number) could be violated by the planned change
- One sentence per pattern explaining how the planned code avoids it

If you cannot articulate how the planned code avoids each relevant PROHIBITED pattern, do not write the code. Re-read the requirements and redesign the approach first.

**Step C — Write the code.**
Only after Steps A and B are complete.

This protocol is non-negotiable. A code edit to `route_fanout_vias.py` that is not preceded by Steps A and B in the same response is a protocol violation.

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
