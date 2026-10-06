# PCB Design Guide (Generic) — KiCad 10, Claude Code Workflow

**Purpose:** A concept-to-manufacturing PCB design workflow for use by Claude Code. The user
describes a new PCB project in Phase 0; Claude records the project-specific parameters into
the `PROJECT_PARAMS` block at the top of this document (or a sidecar file), and every
subsequent phase reads from `PROJECT_PARAMS` rather than any hardcoded value.

This guide preserves the methodology of a successful KiCad 10 6-layer impedance-controlled
board (Loops A–E, DRU rule structure, KiCad 10 Python API patterns, headless FreeRouting
workflow, placement-driven routing optimization) while making all product-specific details
substitutable placeholders.

---

## TOOL VERSION: KiCad 10

All work uses KiCad 10.0 installed at `C:/Program Files/KiCad/10.0/`. Every script, file
format reference, CLI command, layer name, and S-expression syntax is specific to KiCad 10.
Do not apply conventions from KiCad 5/6/7.

### KiCad 10 File Formats
- Schematic: `.kicad_sch` — S-expression header `(kicad_sch (version 20251024) ...)`
- PCB: `.kicad_pcb` — S-expression header `(kicad_pcb (version 20250114) ...)`
- Symbol library: `.kicad_sym`
- Footprint: `.kicad_mod`
- Project: `.kicad_pro` — JSON
- Design rules: `.kicad_dru` — plain text, `(version 1)` header (only the project-named
  DRU file is loaded; any other `.kicad_dru` in the directory is ignored)
- Local settings: `.kicad_prl` — JSON

### KiCad 10 Layer Names (use exact strings — never numeric IDs in scripts)
`F.Cu`, `B.Cu`, `In1.Cu`, `In2.Cu`, `In3.Cu`, `In4.Cu`, `F.Mask`, `B.Mask`,
`F.Silkscreen`, `B.Silkscreen`, `F.Paste`, `B.Paste`, `F.Courtyard`, `B.Courtyard`,
`F.Fab`, `Edge.Cuts`, `User.Drawings`

### KiCad 10 CLI
`"C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"`

Subcommands used: `sch erc`, `sch export bom`, `sch export netlist`, `pcb drc`,
`pcb export gerbers`, `pcb export drill`, `pcb export pos`, `pcb export pdf`,
`pcb export step`, `pcb update-footprints`.

**Netlist sync (schematic → PCB)** has no direct CLI command; use the `pcbnew` Python
module (see Phase 7.5).

### KiCad 10 S-Expression Syntax Notes
- Coordinates in millimeters: `(at X Y)` or `(at X Y ROT)`
- All strings double-quoted: `(net_name "GND")`
- Layer references by name: `(layer "F.Cu")` — never numeric IDs in scripts
- Via: `(via (at X Y) (size PAD) (drill D) (layers "F.Cu" "B.Cu") (net N))`
- Segment: `(segment (start X Y) (end X Y) (width W) (layer "F.Cu") (net N))`
- Zone fill keyword: `(fill yes (mode solid) ...)`

### KiCad 10 pcbnew API Pitfalls
- `PCB_VIA.GetWidth()` with no layer arg asserts — use `GetDrillValue()` and add annular ring
- `board.Zones()` returns a tuple — iterate directly, no `GetCount()`
- Zone UUID: `z.m_Uuid.AsString()` (not `str(z.m_Uuid)`)
- Layer IDs differ from KiCad 6/7 — always resolve at runtime with `board.GetLayerID("In1.Cu")`
- 3D model env variable: `${KICAD8_3DMODEL_DIR}` (name kept from KiCad 8)
- pcbnew site-packages path: `C:/Program Files/KiCad/10.0/bin/Lib/site-packages`
- **`board.Remove(item)` causes SIGSEGV in standalone Python scripts** — the board releases C++ ownership but the SWIG Python wrapper still holds a pointer; when GC or the next Remove() call touches the freed object, the process segfaults. Workaround: do not call `board.Remove()` in standalone scripts. To clear items before re-routing, delete them manually in the KiCad PCB Editor (right-click track → Select → All Tracks in Net → Delete). `board.Remove()` is safe inside the KiCad interactive Python console where the board lifetime is managed by the GUI.
- **Scripted diff-pair routing must use centerline + perpendicular offsets** — routing P pad-to-pad and N pad-to-pad as independent straight lines causes the traces to cross and short whenever the P/N lateral ordering flips between source and destination components. Always compute the centerline between matched P/N pad pairs, offset P and N to opposite sides by cc/2, and use short diagonal stubs at each end to reach the actual pad positions. See `route_highspeed.py` for the reference implementation.

---

## PROJECT_PARAMS BLOCK

Phase 0 fills this in. Fields marked **(USER)** come from the user's answers to Q1–Q5.
Fields marked **(DERIVED)** are determined by Claude from the functional description —
the user is never asked for these. Store in `[PROJECT_DIR]/PROJECT_PARAMS.md`.

Every subsequent phase reads from these fields, not from any hardcoded value.

```
--- USER-PROVIDED (from Q1–Q5) ---

[PROJECT_NAME]              (USER) e.g. "Widget_Controller_v1" — no spaces
[PROJECT_DIR]               (USER) absolute path to project directory
PRODUCT_DESCRIPTION         (USER) plain-English: what it does (Q1)
POWER_SOURCE                (USER) battery / wall adapter / USB / PoE — from Q3
REQUIRED_CONNECTORS         (USER) list of connector types the product must have (Q5)
BOARD_SIZE_CONSTRAINT       (USER) max X/Y mm, or "minimize" (Q4)

--- CLAUDE-DERIVED (from Phase 0 derivation — user never asked for these) ---

[SCHEMATIC_FILE]            [PROJECT_DIR]/[PROJECT_NAME].kicad_sch
[PCB_FILE]                  [PROJECT_DIR]/[PROJECT_NAME].kicad_pcb
[REPORTS_DIR]               [PROJECT_DIR]/Reports
[SCRIPTS_DIR]               [PROJECT_DIR]/python scripts
[BACKUPS_DIR]               [PROJECT_DIR]/Backups
[MANUFACTURING_DIR]         [PROJECT_DIR]/Manufacturing
[CUSTOM_LIBS_DIR]           [PROJECT_DIR]/Part Library
[DATASHEETS_DIR]            [PROJECT_DIR]/Datasheets

BOARD FUNCTION SUMMARY      (DERIVED) 1-3 sentence technical description
BOARD_SHAPE                 (DERIVED) rectangular | polygonal | irregular
[BOARD_WIDTH_MM]            (DERIVED) provisional — minimized in Phase 8
[BOARD_HEIGHT_MM]           (DERIVED) provisional
[BOARD_THICKNESS_MM]        (DERIVED) typically 1.6
[MOUNTING_HOLES]            (DERIVED) count, drill size, hardware, positions

TARGET COMPLIANCE           (DERIVED — inferred category + stricter-value rule)
  Category: [inferred from Q1/Q2 description]
  Declared standards: [list per Compliance Table Step 2 for inferred category]
  Design values: [per Compliance Table Step 3 — stricter value across all applicable]
  e.g. Category=Consumer: EN 62368-1, EN 55032 Class B, EN 55035,
       IEC 61000-4-2 ±8kV/±15kV, IEC 61000-4-3 10V/m, IEC 61000-4-4/5/6/8/11,
       UL 62368-1, FCC Part 15 Class B, RoHS 3
CLASS                       (FIXED) IPC Class 2 minimum; Class 3 tolerances where cost-neutral
SURFACE_FINISH              (FIXED) ENIG always

IC ROSTER (DERIVED — one line per IC: role, ref, MPN, package, selection rationale)
  [POWER_IC_1]              e.g. U1  TPS54360B  SOIC-8  buck converter, 36V in, 3.5A
  [POWER_IC_2]              ...
  [MCU_IC]                  role, ref (this ref value → MCU_REF above), MPN, package
  [HS_MUX_IC]               (if present)
  [VIDEO_BRIDGE_IC]         (if present)
  ...

CONNECTOR ROSTER (DERIVED — specific MPNs selected by Claude)
  [CONN_POWER]              XLR / barrel / USB-C / header — MPN + pin assignments
  [CONN_INPUT_1]            signal in — MPN + pin assignments
  [CONN_OUTPUT_1]           signal out
  [CONN_DEBUG]              JTAG/SWD/UART
  ...

POWER RAIL TABLE (DERIVED — net, source IC, nominal V, max current A, tolerance)
  [POWER_RAIL_MAIN]         e.g. VBATT, external, 12V, 3A, +/-20%
  [POWER_RAIL_5V]
  [POWER_RAIL_3V3]
  ...

HIGH-SPEED SIGNAL INVENTORY (DERIVED — only if HS interfaces present)
  [HS_PAIR_TX_P/N]          e.g. DP_TX0_P/N, differential, 100 ohm, 8.1 Gbps
  [HS_PAIR_RX_P/N]
  Intra-pair skew budget    <= 0.01 mm design target (protocol spec: <= 0.127 mm)
  Inter-lane skew budget    <= 0.45 mm for DP 1.4; <= 5.08 mm for USB SS; <= 10 mm for HDMI TMDS
  All HS pairs shall route on [HS_ROUTING_LAYER]  (typically In2.Cu, buried stripline)

SWITCHING LOOPS (DERIVED — converter role, IC ref, SW node net, target loop area)
  Each switching converter: target enclosed loop area <= 50 mm^2

THERMAL BUDGET (DERIVED — per IC > 0.3W: dissipation W, T_max, T_amb, required theta_ja)
  P_diss = (Vin-Vout)*Iout for LDOs; (1-η)*Pout for switchers (η=0.85 default)

LAYER COUNT DECISION        (DERIVED) 2 | 4 | 6 | 8 — justified in Phase 6
STACKUP                     (DERIVED) filled in Phase 6

FREEROUTING_JAR             absolute path to freerouting-executable.jar
FREEROUTING_TEMP_DIR        short path without spaces, e.g. C:/Temp/freerouting

CONNECTOR LABELS (DERIVED)  exact silkscreen strings, all caps, <= 8 chars

--- DERIVED FIELDS POPULATED DURING DESIGN (phases indicated) ---

MCU_REF                     (DERIVED — Phase 0) reference designator of the MCU, e.g. "U3"
                             Used by: check_decoupling_proximity.py (1 mm proximity limit)

SIGNAL_FLOW_ORDER           (DERIVED — Phase 0) ordered list of Zone A IC refs, input→output
                             e.g. ["J1", "U4", "U5", "J2"]
                             Used by: place_components.py Zone A sort

POWER_RAIL_LOW_V            (DERIVED — Phase 0) low-voltage I/O rail net name, e.g. "VCC_IO"
                             Used by: Phase 9 In4.Cu power plane island

MAX_COMPONENT_HEIGHT_MM     (DERIVED — Phase 0) maximum allowed component height from enclosure
                             clearance analysis, e.g. 5.0
                             Used by: check_component_heights.py in Phase 13

PASSIVE_TABLE               (DERIVED — Phase 2 Step 1a) per-IC passive value table
                             Format: one block per IC, with decoupling C, input C, output C,
                             bootstrap C, inductor L, snubber, and AC coupling values
                             Used by: Phase 3 CHECKs 1, 2, 6; Phase 8 place_passive_near_parent

PROXIMITY_RULES_TABLE       (DERIVED — Phase 2 Step 1a) schematic-aware component proximity rules
                             Format: list of (ref_A, ref_B, max_dist_mm, type, reason) tuples
                             Built by reading schematic + datasheets during Sub-step B2
                             Used by: Phase 8 check_component_proximity.py

CONNECTOR_PROTECTION_TABLE  (DERIVED — Phase 0 Step 4) Per-connector protection audit.
                             One row per external connector. All cells must be ✅ before
                             Phase 3 gate can close.
                             Format: markdown table with columns:
                             Ref | Type | ESD device | CM choke | Polyfuse | Rev-pol | OVP | Notes
                             ✅ = present in schematic  ❌ = missing  N/A = not applicable

GPIO_TABLE                  (DERIVED — Phase 2 Step 1a Sub-step D) MCU GPIO assignments
                             Format: GPIO#, pin name, net name, direction, connected-to ref
                             Used by: Phase 3 CHECKs 1, 6; schematic design

LOOP_NETS                   (POPULATED — Phase 7.5) SW node net numbers for Loop C
                             Format: {net_number: "NET_LABEL"} for every switching node net
                             Populated after netlist sync: search (net N "NET_NAME") in PCB file
                             Used by: measure_loops.py (Loop C)
```

---

## THE SILKSCREEN RULE

The only visible silkscreen text on the board is connector purpose labels. No reference
designators, no component values, no IC names on `F.Silkscreen` or `B.Silkscreen`.
Board identification (version, date, RoHS, CE) goes on `F.Fab`.

Enforcement: Phase 4.5 (or a dedicated step in Phase 11) hides all references and values.
Phase 11 silkscreen script only adds connector labels. Phase 12 checklist confirms zero
reference designators on silkscreen layers.

Connector label format: short, all-caps, >= 1.0mm text height, outside the connector
courtyard. Use exact strings from PROJECT_PARAMS.

---

## THE SCHEMATIC-FIRST RULE

Every component change, addition, or removal must be made in the schematic first. The
schematic is the single source of truth. Adding a test point, an ESD IC, a decoupling
cap, or changing a value or footprint: schematic first → ERC clean → sync to PCB → DRC.

Never edit schematic-controlled properties directly in the PCB — the next sync will
overwrite them. Python scripts in this guide that touch the schematic trigger the full
sequence (ERC clean → Phase 7.5 sync → DRC clean). Scripts that touch only the PCB may
modify non-schematic properties (position, rotation, copper geometry, zones, vias).

---

## FILE STORAGE RULE

All files generated during a project go in their designated subdirectory. Never write
output files to the project root or any other location.

| File type | Destination | Notes |
|---|---|---|
| Reports, logs, audit output | `[REPORTS_DIR]` | Every `.txt` / `.rpt` / `.csv` produced by a script or phase |
| Backup files | `[BACKUPS_DIR]` | See BACKUP FILE RULE below |
| Datasheets (PDF or extracted text) | `[DATASHEETS_DIR]` | One file per component; name as `<MPN>.pdf` or `<MPN>.txt` |
| Manufacturing output | `[MANUFACTURING_DIR]` | Gerbers, drill files, BOM, PnP, stencil spec, README |
| Project-specific scripts | `[SCRIPTS_DIR]` | Scripts written for this project only; do not copy shared library scripts here |
| Custom library assets | `[CUSTOM_LIBS_DIR]` | Symbols, footprints, 3D models from Phase 1.5 |

Create all directories at Phase 2 Step 4 (`os.makedirs(..., exist_ok=True)`). If a
directory does not exist when a file is about to be written, create it immediately —
never silently skip writing the file.

---

## BACKUP FILE RULE

All backup files go in `[BACKUPS_DIR]`. Naming convention:
`<original_filename_without_ext>.<phase_or_description>.bak`

Any script that saves a KiCad file must create the backup first. Never leave `.bak`,
`_bak`, `_backup`, or `_pre_*` files in the project root.

Script template:
```python
import shutil, os
os.makedirs(BACKUPS_DIR, exist_ok=True)
shutil.copy(PCB_FILE, os.path.join(BACKUPS_DIR, f"{PROJECT_NAME}.{tag}.bak"))
```

---

## BACKUP SAFETY RULE — Before Any Destructive File Operation

Before overwriting, restoring, or deleting any project file:

1. **Search the entire project directory tree recursively for all backup files** — not
   just `[BACKUPS_DIR]`. KiCad creates auto-backup zips at:
   `[PROJECT_DIR]/[PROJECT_NAME]-backups/[PROJECT_NAME]-YYYY-MM-DD_HHMMSS.zip`
   These auto-backup zips are separate from `[BACKUPS_DIR]/` and contain the full
   project state at each auto-save. Always check both locations.
2. Identify the most recent backup containing the state to preserve. If it is a
   `.kicad_pcb`, load it with `pcbnew.LoadBoard()` and verify component count / zone
   count / net count match expectations before trusting it.
3. Confirm a verified recovery path exists before the destructive operation.
4. If no verified backup exists: create one NOW before the destructive operation.
5. Never `cp`/`copy` over a file without first verifying what is being overwritten —
   copy to a temp path, verify, then replace.

This rule is invoked whenever a script restores from a backup, deletes an intermediate
file, or overwrites a `.kicad_pcb` / `.kicad_sch`.

---

## SESSION CONTEXT RULE

After every phase completes (all its loops clean), update:
- `[PROJECT_DIR]/SESSION_CONTEXT.md` — full detail per phase
- Any parent-scope `SESSION_CONTEXT.md` if the project sits inside a larger workspace

Record: phase number/name, date, files produced, ERC/DRC status, known-acceptable list,
open items deferred. Never start a new phase until the previous phase's session context
has been written — this is a self-check Claude performs silently, not a user confirmation
step. Do not ask the user whether to proceed after updating the session context.

---

## LESSONS LEARNED PROTOCOL

When any phase encounters an edge case, unexpected error, or workflow gap not covered
by this guide:

1. **Detect** — the current situation is not explicitly handled by any phase, loop,
   or rule in this guide.
2. **Assess root cause** — before surfacing anything to the user, determine:
   - What went wrong (symptom)
   - Why it went wrong (root cause: missing rule, wrong assumption, tool behavior,
     undocumented KiCad behavior)
   - Which phase or loop should have caught it
   - A specific, reusable prevention: a rule, check step, known-acceptable clause,
     or script template that would prevent the same failure on any future project
3. **Formulate the guide addition** — write the proposed addition in the style of
   this guide: a named check, a loop step, a known-acceptable clause, or a new
   phase sub-step. It must be specific enough that a future Claude run following
   the guide would not encounter the same edge case.
4. **Present to user:**
   > "I hit an edge case in Phase N. Root cause: [summary]. Here is the addition
   > I propose adding to the guide to prevent this in future runs: [proposed text].
   > Do you want me to add this?"
5. **On user approval** — write the addition into this guide file immediately, in
   the correct section, matching the existing style. Never add to the guide without
   user approval.

Never swallow an edge case silently. Every novel failure is a guide improvement
opportunity.

---

## DETOUR PROTOCOL

A **detour** occurs when forward progress through phases is interrupted to fix, rework,
or retrieve something from an earlier phase — for example, correcting a footprint during
Phase 8, updating a library item mid-placement, or revisiting Phase 1.5 after schematic
changes. Detours are common and expected. This protocol prevents rule and context loss
when resuming.

### Before leaving the current phase:

1. **Write a Detour Bookmark** into `[PROJECT_DIR]/.claude/CLAUDE.md` under the
   `## DETOUR BOOKMARK` section. Replace the `*(No active detour)*` placeholder with:
   ```
   DETOUR ACTIVE
   Departed from:   Phase N — [phase name], Step M — [step name]
   Reason:          [one sentence describing what triggered the detour]
   Resume at:       Phase N Step M — [specific step, e.g., "Phase 8 Step 4: decoupling loop"]
   Rules in effect: [any non-obvious rules that were actively being applied]
   Open items:      [partial state, e.g., "C12 not yet placed", "Loop D in progress"]
   ```
2. Do not resume the departed phase until the detour is fully complete and its own
   loop exit criteria are met (or the user explicitly confirms completion).

### During the detour:

Follow the appropriate phase(s) of this guide normally. Apply all rules for those phases.
If the detour itself requires a nested detour, append a second indented bookmark entry
below the first.

### Returning from the detour:

1. **Re-read `[PROJECT_DIR]/.claude/CLAUDE.md`** in full.
2. **Re-read the entire departed phase section** of this guide (e.g., re-read `## PHASE 8`
   from top to bottom, not just the specific step). Context fades during detours;
   re-reading the full phase prevents rule amnesia.
3. **Restore state** from the Detour Bookmark: confirm the "Resume at" step and all
   open items.
4. **Clear the bookmark:** replace the `DETOUR ACTIVE` block in `.claude/CLAUDE.md` with:
   ```
   DETOUR COMPLETE — returned to Phase N Step M on [DATE]
   ```
5. **Announce to the user:** "Detour complete. Resuming Phase N at [step name]. Here is
   what I'm about to do next: [one sentence]."

### If the user initiates the resume:

If the user says "get back on track," "resume Phase N," "continue where we left off," or
similar, treat this as a return-from-detour trigger. Execute all five steps above even if
a Detour Bookmark was not written before the detour began. Re-read `.claude/CLAUDE.md`
and the departed phase section before taking any further action.

**Protection audit on detour return (mandatory):**
Before resuming the phase that was interrupted:
1. Re-read CONNECTOR_PROTECTION_TABLE in PROJECT_PARAMS.
2. If any connector was added, removed, or had its signals/power nets changed during the
   detour, update the table and verify all cells are ✅/N/A. Any new ❌ blocks resumption.
3. If gen_schematic.py was modified during the detour, re-run Phase 3 CHECKs 4 and 5
   before continuing, even if the main phase is past Phase 3.
4. Record the protection table state in the DETOUR BOOKMARK in CLAUDE.md.

### If the detour involves schematic changes to decoupling or bypass caps:

If the detour adds, renames, or removes any decoupling or bypass capacitor (C ref) in the
schematic, update `DECOUPLING_RULES` in `[SCRIPTS_DIR]/check_decoupling_proximity.py`
before re-running the check. Each entry in that table explicitly pairs a cap ref with the
IC it was designed to decouple — this is a design-intent table, not auto-discovered, so
it does not self-update. A stale `DECOUPLING_RULES` will silently pass moved or renamed
caps against the wrong IC. Update the table as part of the detour, not after returning.

---

## SESSION START PROTOCOL

At the start of every Claude Code session opened in a project directory, before taking
any other action:

**Step 1 — Read `.claude/CLAUDE.md`.**
Read `[PROJECT_DIR]/.claude/CLAUDE.md` in full. Surface any active Detour Bookmark to
the user before proceeding. If the file does not exist (new project not yet initialized),
proceed directly to Phase 0.

**Step 2 — Directory health check.**
Verify the standard project directory structure. For each expected item, confirm it
exists on disk:

| Item | Expected path | If missing |
|------|---------------|------------|
| Project params | `[PROJECT_DIR]/PROJECT_PARAMS.md` | STOP — announce to user; do not proceed until located or user confirms fresh start |
| Session context | `[PROJECT_DIR]/SESSION_CONTEXT.md` | Warn user; create empty file and note the gap |
| Reports folder | `[PROJECT_DIR]/Reports/` | Create it silently |
| Backups folder | `[PROJECT_DIR]/Backups/` | Create it silently |
| Manufacturing folder | `[PROJECT_DIR]/Manufacturing/` | Create it silently |
| Part Library folder | `[PROJECT_DIR]/Part Library/` | Create it silently |
| Datasheets folder | `[PROJECT_DIR]/Datasheets/` | Create it silently |
| KiCad project file | `[PROJECT_DIR]/[PROJECT_NAME].kicad_pro` | Warn user if Phase ≥ 2 complete per SESSION_CONTEXT |
| Schematic file | `[PROJECT_DIR]/[PROJECT_NAME].kicad_sch` | Warn user if Phase ≥ 2 complete per SESSION_CONTEXT |
| PCB file | `[PROJECT_DIR]/[PROJECT_NAME].kicad_pcb` | Warn user if Phase ≥ 2 complete per SESSION_CONTEXT |

**Step 2b — Sweep misplaced files from project root.**
Scripts and KiCad leave several categories of file in the project root that belong
elsewhere. After confirming `Backups/` and `Reports/` exist (Step 2), sweep the root
and relocate as follows:

*Backup files → `Backups/`*

| Pattern | Source | Action |
|---------|--------|--------|
| `*.kicad_pcb-bak`, `*.kicad_sch-bak` | KiCad auto-save on every save | Move to `Backups/`; rename with last-modified timestamp: `[PROJECT_NAME]_autobak_[YYYYMMDD_HHMMSS].<ext>` |
| `*.bak_*` (e.g., `name.kicad_pcb.bak_drcfix`) | Scripts writing backup to root | Move to `Backups/`; keep descriptive suffix |
| `*.comp_backup`, `*.zone_fix_backup` | Scripts writing backup to root | Move to `Backups/`; keep full filename |
| `*_pre_*_[0-9]*.kicad_pcb` (timestamped backup PCBs) | Scripts writing backup to root | Move to `Backups/`; keep full filename |

*Report files → `Reports/`*

| Pattern | Source | Action |
|---------|--------|--------|
| `compliance_rules_summary.txt` | Phase 5 output written to root | Move to `Reports/` |
| `layer_analysis.txt` | Phase 6 output written to root | Move to `Reports/` |
| `*.rpt`, `*_report.txt`, `fix_log*.txt` | Any phase report written to root | Move to `Reports/` |

*KiCad lock files — leave in place*

`~*.lck` files are KiCad's file-open locks. They disappear when KiCad closes.
Do not move or delete them.

*KiCad project auto-backup directory — leave in place*

KiCad creates `[PROJECT_NAME]-backups/` in the project root and writes timestamped ZIP
backups there on every save. This path is controlled globally in `kicad_common.json` and
cannot be changed per-project. Treat this directory as an expected root-level item — do
not move, rename, or flag it. Never delete it; KiCad will simply recreate it.

Report all moved files in the Step 4 audit summary under "Directory". Never delete
files — move only.

**Step 3 — Read SESSION_CONTEXT.md.**
Read `SESSION_CONTEXT.md` to determine the last completed phase and any open items.
Cross-check against `PROJECT_PARAMS.md` to confirm paths are consistent. If
SESSION_CONTEXT and PROJECT_PARAMS disagree on a key value (e.g., last phase
completed, board dimensions, layer count), flag the conflict to the user — do not
silently resolve it.

**Step 4 — Report and proceed.**
Announce the session start state in one concise block:
```
Session start audit:
  Last phase completed: Phase N — [name] ([date])
  Open items: [list or "none"]
  Directory: [CLEAN / WARNINGS: list any missing items found in Step 2]
  Detour: [ACTIVE: summary / none]
  Ready to continue at: Phase N+1 — [name]
```
Do not ask the user for confirmation unless a STOP condition was triggered in Step 2.
Proceed to the indicated phase immediately.

---

## SCHEMATIC CHANGE RULE

Any modification to a component on the schematic — at any point during any phase —
is a **schematic change event** and immediately stops forward progress. This rule
covers all of the following:

- **MPN substitution** — swapping one part number for another on any component
- **Component addition** — placing a new component symbol on the schematic
- **Component removal** — deleting a component from the schematic
- **Net reassignment** — connecting a pin to a different net than currently assigned
- **Value change** — changing a resistor, capacitor, inductor, or any other parameter
  that affects circuit function or BOM
- **Footprint change** — assigning a different footprint to an existing component
- **Symbol change** — replacing a symbol with a different one (even for the same MPN)
- **Power/ground reassignment** — connecting a component pin to a different power rail
  or GND net

### When a schematic change event occurs:

1. **Stop forward progress immediately.** Do not continue the current phase.
2. **Apply the Schematic-First Rule** — make the change in the schematic before any
   PCB file is touched.
3. **Write a Detour Bookmark** per the DETOUR PROTOCOL, recording:
   - The phase and step where work was interrupted
   - The exact change (ref designator, change type, old value → new value)
4. **Build a change delta table** per Phase 0b rules:
   ```
   | Ref | Change type      | Old value | New value | Phases to re-run      |
   |-----|------------------|-----------|-----------|-----------------------|
   | U3  | MPN substitution | TPS54360B | TPS54340  | 1, 1.5, 3, 4, 4.5, 7.5|
   | C12 | Value change     | 10 µF     | 22 µF     | 2, 3                  |
   ```
5. **Purge stale state — update all affected files before re-running any phase.**
   In-context memory of the old component is now unreliable and must not be used.
   Immediately update these files to reflect the change:
   - `PROJECT_PARAMS.md` — update IC ROSTER, CONNECTOR ROSTER, power rail table,
     thermal budget, or any other field affected by the change
   - `SESSION_CONTEXT.md` — record the change event: what changed, why, and which
     phases will be re-run
   - `[REPORTS_DIR]/library_retrieval.txt` — remove or mark STALE any entry for
     the old component; reset the new component's asset status to unresolved
   - `[REPORTS_DIR]/footprint_audit.txt` — remove the old component's audit entry
   - Any other phase output file that references the changed component

   After updating these files, **treat them — not in-context memory — as the sole
   source of truth** for all subsequent work. Do not rely on anything stated or
   derived earlier in the session about the old component. Re-read each file at the
   start of every re-run phase rather than assuming what it contains.

6. **Re-run all phases in the delta table**, in ascending phase order, before
   resuming the interrupted phase. Every re-run phase must reach its loop exit
   criteria before moving to the next.
7. **Clear the Detour Bookmark** and announce the resume point to the user per
   the DETOUR PROTOCOL return steps.

### Phase re-run rules by change type:

| Change type | Phases to re-run |
|---|---|
| MPN substitution | 1 (lifecycle), 1.5 (library retrieval + pin verify), 3 (ERC), 4 (footprint audit + pin-1 check), 4.5 (3D model), 7.5 (netlist sync) |
| Component added | 1, 1.5, 2, 3, 4, 4.5, 7.5 |
| Component removed | 2, 3, 7.5 |
| Net reassignment or power/ground reassignment | 2, 3, 7.5; also 9 if copper zones are affected |
| Value change (R, C, L, or any design parameter) | 2 (re-check design equations), 3 (ERC) |
| Footprint change only (same MPN, different package) | 1 (confirm lifecycle of new package), 1.5 (footprint retrieval + pin-1 check), 4, 4.5 |
| Symbol change (same MPN, different symbol file) | 1.5 (re-run Step 7 pin verification), 3 |

**Always include Phase 2 (ERC re-run) and Phase 7.5 (netlist sync) for any
schematic change**, regardless of type.

### What this rule does NOT cover:

- **PCB-only changes** (moving a placed component, adjusting a trace, editing copper
  zones) — these do not trigger a schematic re-check unless the PCB change reveals
  a schematic inconsistency.
- **Annotation-only changes** (updating a comment, description, or non-functional
  property field) with no effect on nets, values, or BOM.

### Stale context and false memory

Claude's in-context memory of a changed component is a liability, not an asset.
After a schematic change event, the following apply for the remainder of the session:

- **Treat all prior in-context statements about the old component as void.** Pin
  counts, package dimensions, net assignments, voltage ratings, footprint names,
  and any other detail stated earlier in the conversation about the old component
  may no longer be accurate and must not be used.
- **Re-read every relevant file before acting.** At the start of each re-run phase,
  read `PROJECT_PARAMS.md`, `SESSION_CONTEXT.md`, and the phase's own output files
  from disk. Do not assume their contents match what was written earlier in the
  session.
- **If there is any conflict between an in-context statement and a file on disk,
  the file wins.** Flag the conflict in `SESSION_CONTEXT.md` but proceed with the
  file's value.
- **Do not carry forward derived values** (computed currents, impedance targets,
  thermal budgets, pin tables) that were derived from the old component. Re-derive
  them from the new component's datasheet during the re-run phases.

### If a schematic change occurred earlier without triggering this rule:

If Claude realizes mid-phase that a schematic change happened earlier in the session
without this rule being applied, treat it as a return-from-detour event: execute
step 5 (purge stale state) immediately, build the change delta table retroactively,
determine which phases were skipped, and re-run them in order before continuing. Do
not advance past the current phase until all affected prior phases are clean.

---

## DECISION AUTHORITY TABLE

Defines when Claude proceeds autonomously, documents-and-continues, or stops for
user input. Read this table at the start of every phase and whenever an unexpected
situation arises.

**Default behavior: proceed autonomously.** Unless a situation in this table is marked
**STOP**, Claude continues to the next step or phase immediately after loop exit criteria
are met. Do not pause between steps or phases to announce completion or ask permission
to continue. The completion of a phase's loop exit criteria is full authorization to
begin the next phase. Never ask "shall I proceed with Phase N?" — simply proceed.

**Exception — named GATE sections are mandatory hard stops.** Any section named
`PRE-LAYOUT GATE`, `POST-PLACEMENT GATE`, `PRE-ROUTING GATE`, `PRE-MANUFACTURING GATE`,
or any future named GATE overrides the autonomous progression rule above. Every checkbox
in a gate must be verified before the next phase begins. A gate with unchecked items is
a **STOP** — do not proceed, report which items are incomplete, and wait for the user to
resolve them. Never skip or partially complete a gate.

| Situation | Action | Document where |
|---|---|---|
| Ambiguous footprint (multiple valid variants) | Pick IPC-7351B Nominal variant | `footprint_audit.txt` — chosen variant + reason |
| MPN not specified by user | Run component selection protocol (Phase 1) | `component_status.txt` |
| No prior design to migrate (fresh start) | Use fresh schematic path (Phase 2 Step 2) | SESSION_CONTEXT |
| ERC/DRC violation — known-acceptable | Document and continue | Known-acceptable list in `fix_log` |
| ERC/DRC violation — root cause unclear after 3 Loop B attempts | **STOP, ask user** | Full error text + all attempts in `fix_log` |
| FreeRouting leaves > 10% nets unrouted after `-mp 200` | Proceed to Phase 10b, document | `autoroute_log.txt` |
| FreeRouting quality score poor (see Phase 10a scoring) | Trigger Phase 10b | `autoroute_log.txt` |
| Layer count borderline (e.g., 4 vs 6) | Choose higher count, write proof | `layer_analysis.txt` |
| Conflicting voltage domain assignment | **STOP, ask user** | Full description of conflict |
| Missing enclosure constraint | Mark `[TBD]`, flag for Phase 8, continue | SESSION_CONTEXT open items |
| RED component, no clear substitute after research | **STOP, ask user** | All candidates tried in `component_status.txt` |
| Schematic check FAIL with clear, unambiguous fix | Apply fix, continue | `fix_log_phase3.txt` |
| Schematic check FAIL with ambiguous fix | **STOP, ask user** | Description of ambiguity |
| Part missing from KiCad symbol library | Run Phase 1.5 retrieval first; create custom symbol (Phase 2 Step 2b) only if status is MANUAL | `library_retrieval.txt`, SESSION_CONTEXT |
| Edge case not covered by this guide | Apply Lessons Learned Protocol | Proposed guide addition |
| Phase complete, no STOP condition triggered | Proceed to next phase immediately | — |

---

## THE ITERATION LOOPS (A–F)

Every phase that writes a KiCad file specifies which loops apply. Do not proceed to
the next phase until all applicable loops are clean.

### Loop A — ERC / DRC Iteration
```
1. Run ERC or DRC → save report as [REPORTS_DIR]/<ERC|DRC>_phase<N>_<NN>.rpt (never overwrite)
2. Read full report
3. Classify each violation:
     KNOWN-ACCEPTABLE — documented for this phase
     NEW-REAL-ERROR   — everything else
4. For each NEW-REAL-ERROR:
     a. Identify file / net / component root cause
     b. Write [SCRIPTS_DIR]/fix_<description>.py
     c. Run script (apply Loop B)
     d. Return to step 1
5. Stop when zero NEW-REAL-ERRORS
6. Write [REPORTS_DIR]/fix_log_phase<N>.txt
```

**Never skip an error. Never mark an error acceptable unless it is on that phase's known-acceptable list.**

### Loop B — Python Script Debug Iteration
```
1. Write script
2. Run: python "[SCRIPTS_DIR]/<script>.py"
3. Exit 0 → step 4. Non-zero → read traceback, fix same file, back to step 2.
4. Verify output changed as expected:
     Schematic → run ERC, no new errors
     PCB → run DRC, no new errors
5. Stop when exit 0 AND no new ERC/DRC errors
```
If a script fails 3+ times on the same error, try a completely different approach. If
the different approach also fails 3+ times, apply the Lessons Learned Protocol and
**STOP for user input** — do not continue attempting variations indefinitely.

### Loop C — Switching Loop Area Verification
```
1. Read `Python Scripts/measure_loops.py`. Configure its PROJECT CONFIG block:
   set PCB_FILE and populate LOOP_NETS with the net number and label for each SW node
   net (net numbers are found by searching `(net N "NET_NAME")` in `[PCB_FILE]` after
   Phase 7.5 sync). Run it. For each switching converter in PROJECT_PARAMS it:
     - Reads the PCB
     - Finds pads of: VIN cap, VIN pin, SW pin, inductor, output cap, GND return
     - Computes enclosed polygon area (shoelace formula)
     - Reports area in mm²
2. Apply Loop B
3. Any area > 50 mm^2: move components closer, re-measure
4. Stop when all switching loops <= 50 mm^2
5. Write [REPORTS_DIR]/loop_area_report.txt
```

### Loop D — Trace Current Capacity Verification
```
1. Run `Python Scripts/width_audit.py` (preferred). Configure PCB_FILE and POWER_NETS. For each power net in PROJECT_PARAMS it:
     - Finds all (segment) elements on that net
     - Excludes segments within 0.7 mm of a pad edge (intentional neck-downs — prevents false positives on via fanouts and connector lands)
     - Reports minimum trunk segment width
     - Compares against IPC-2221B minimum for 10 deg C rise, 1oz copper:
         0.5A -> 0.3 mm;  1.0A -> 0.5 mm;  1.5A -> 0.8 mm;  3.0A -> 1.5 mm
   Use `Python Scripts/verify_trace_widths.py` only if width_audit.py misclassifies segments for an unusual layout — it applies no neck-down exclusion.
   **Vacuous PASS check:** if `width_audit.py` reports zero qualifying segments for any power net, the net is either entirely within 0.7 mm of pads (very short connection) or the net name does not match POWER_NETS. In either case run `verify_trace_widths.py` on that net as a cross-check to confirm no under-width trunk segments exist.
2. Apply Loop B
3. Widen any offending segments (fix script), re-run DRC (Loop A), re-run Loop D
4. Stop when every net meets its width requirement
5. Write [REPORTS_DIR]/trace_width_report.txt
```

### Loop E — Thermal Adequacy Verification
```
1. Run `Python Scripts/verify_thermal.py`. Configure THERMAL_TABLE from the THERMAL BUDGET
   section of PROJECT_PARAMS (one row per IC > 0.3W: ref, description, P_diss_W, T_j_max_C,
   n_thermal_vias, drill_mm) and set T_AMB_C to the worst-case operating temperature. It:
     - Computes required θ_ja = (T_j_max - T_amb) / P_diss
     - Estimates achieved θ_via = 70 / (n_vias × drill_mm)  [empirical constant per via]
     - Reports PASS/FAIL per IC and the minimum via count needed to meet budget on failures
2. Apply Loop B
3. Increase via count or drill size in placement script
4. Stop when all ICs meet target
5. Writes [REPORTS_DIR]/thermal_report.txt
```

### Loop F — Design for Manufacturing (DFM) Verification
```
1. Read `Python Scripts/verify_dfm.py`, configure its PROJECT CONFIG block (PCB_FILE,
   REPORT_FILE), and run it. It checks:
     a. Component-to-edge clearance: every courtyard >= 3 mm from Edge.Cuts
        (pick-and-place rail clearance — boards fail PnP if parts are too close
        to the edge)
     b. Component-to-component courtyard gap >= 0.25 mm
     c. Polarized component orientation: flag any zone where polarized passives
        have inconsistent pin-1 axis (mixing left/up orientations increases
        hand-assembly errors and AOI setup time)
     d. Through-hole components: flag any non-mounting-hole THT footprint —
        target is SMD-only for reflow; STOP and confirm with user if any found
     e. Fine-pitch surface finish: if any pad pitch <= 0.5 mm and
        SURFACE_FINISH != ENIG in PROJECT_PARAMS, flag as error
     f. Via-in-pad: flag any via whose center falls within a component pad
        polygon (causes solder wicking and opens during reflow)
     g. Exposed-pad paste aperture: for every QFN/DFN/WQFN exposed pad, verify
        paste aperture area <= 60% of pad area (full coverage causes bridging)
     h. Minimum annular ring >= 0.15 mm on all PTH vias (IPC-2221B)
2. Apply Loop B
3. For each DFM failure: write a targeted fix script, re-run Loop F
4. Stop when all checks pass
5. Write [REPORTS_DIR]/dfm_report.txt
```

Mandatory: Phase 11.5. Optional first pass: after Phase 8 placement to catch
problems before routing rather than after.

**Apply the appropriate loop (A through F) whenever triggered by a phase instruction. Never skip a loop. Never declare a phase done while any loop has unresolved failures.**

---

## PHASE ORDER QUICK REFERENCE

| Phase | Name | Key Action |
|---|---|---|
| 0 | Project Intake | Extract PROJECT_PARAMS from user description |
| 1 | Component Production Status | BOM lifecycle / distributor check |
| 1.5 | Custom Component Library Retrieval | Download missing symbols / footprints / 3D models |
| 2 | Create Project Files | Copy prior version or scaffold new; ERC clean |
| 3 | Schematic Design Verification | Electrical checks per schematic |
| 4 | Footprint Audit + Dimension Validation | Part A name, Part B dimensions |
| 4.5 | 3D Model Assignment | Assign .wrl / .step to every footprint |
| 5 | Compliance Design Rules | Write .kicad_dru |
| 6 | Layer Stack Decision | Prove minimum layer count |
| — | Pre-Layout Gate | — |
| 7 | Board Outline + Layer Stack | Provisional board rectangle, stackup |
| 7.5 | Netlist Sync | Import schematic nets |
| 8 | Component Placement | Zone-based; minimize board size; optional Loop F first pass |
| 9 | Copper Zones | GND planes, power planes, islands, stitching |
| 10 | Critical Pre-routing | Switching loops + HS diff pairs |
| 10a | Headless FreeRouting | Autoroute remaining nets; quality scoring |
| 10b | Placement Optimization from Routing | Rework placement to fix routing symptoms |
| 10c | Pre-Fab Blocker Resolution | GUI-crash, keepout, stub, shorting fixes |
| 10d | Trace Length Matching | Meanders on same-layer pairs |
| 11 | Post-Autoroute Cleanup | Missed nets, return vias, silkscreen |
| 11.5 | Post-Routing Quality Checks | Copper balance, polarity, coating; Loop F |
| 12 | Final DRC + Compliance Checklist | Zero violations, sign-off |
| 13 | Manufacturing Files | Gerbers, drill, BOM, pick-and-place |
| 14 | First-Article Inspection Checklist | Bare-board inspection |
| 15 | Power-Up Test Sequence | Assembled-board test |
| 16 | Update Session Context | Save completed state |

---

## PHASE 0 — Project Intake

**Purpose:** Understand what the product does from the user's perspective, then derive
every engineering parameter from that functional description. The user is not expected
to know any circuit, PCB, or electrical engineering details. All technical parameters
— IC selection, power topology, layer count, impedance targets, thermal budgets,
compliance standards — are derived by Claude from the functional description. The user
answers product-level questions only.

**Inputs:** User's natural-language product description.

**KiCad version notice:** Before asking Q1, tell the user: "This project will be
designed in KiCad 10. Please confirm you have KiCad 10 installed at
`C:/Program Files/KiCad/10.0/` before we begin."

**Is this a revision?** If the user mentions a prior version of the board, follow
**Phase 0b — Revision of Existing Design** instead. Otherwise continue here.

---

### Phase 0 — User Questions (5 questions)

Ask only these questions. Do not ask about circuits, ICs, voltages, frequencies,
impedances, standards, quantities, or any other engineering parameter — those are
Claude's job to derive or are fixed defaults in this guide.

**Q1 — What does the product do?**
Plain English. What problem does it solve? What is the user experience of operating it?
Example: "It takes a balanced audio signal from a microphone and sends it to a camera
over a 3-pin XLR cable, with a gain control knob."

**Q2 — What does it connect to? (inputs and outputs)**
List every physical connection to the outside world. Use product/consumer language.
Example: "One XLR input from a mic, one 3.5mm output to a camera, powered by a 9V
battery or USB-C."
*Claude infers: connector types, signal types, power source type.*

**Q3 — How is it powered?**
Battery (what voltage/size if known), wall adapter (what voltage if known), USB,
PoE, or other. If unknown, say so — Claude will determine the right power architecture.

**Q4 — Are there any size constraints?**
Must it fit inside a specific enclosure or rack space? Maximum dimensions?
If none, answer "as small as possible" — Phase 8 will minimize automatically.

**Q5 — Are there any specific connectors required?**
Only list connectors the product must have (XLR, USB-C, HDMI, specific headers, etc.).
Claude selects the exact MPNs and pin assignments.

---

### Phase 0 — Claude's Derivation Process

After the user answers Q1–Q5, Claude derives all engineering parameters. The user
is **not asked** about any of the following — Claude determines them:

**Step 1 — Functional decomposition.**
Break the product description into functional blocks:
- Signal path (what signal enters, what processing happens, what exits)
- Power architecture (what voltages are needed to run each block)
- Control/intelligence (does anything need a microcontroller, I2C config, etc.)
- Protection (what can go wrong at each connector — ESD, reverse polarity, overvoltage)
- Indicators (LEDs, displays, any user feedback)

**Step 2 — IC selection.**
For each functional block, select specific ICs:
- Identify the function required (e.g., "balanced audio input → instrumentation amp or
  transformer + op-amp; gain control → digital potentiometer or VCA")
- Search for Active-lifecycle parts at Digi-Key/LCSC meeting the functional requirement
- Apply the component selection protocol (Phase 1) to rank and choose
- Assign MPN, package, reference designator, and role placeholder to each IC
- Verify the selected circuit topology is complete (no floating pins, all supplies
  decoupled, all enable pins driven)

**Step 3 — Power architecture.**
From the power source (Q3) and the IC supply requirements (Step 2):
- List every voltage rail needed
- Select topology for each (LDO for low-current low-noise rails; switching buck/boost
  for higher currents or large input-output differentials)
- Select switching converter ICs and supporting passives (inductor, caps)
- Compute: V_out, I_out_max, switching frequency from IC datasheet, thermal dissipation
- All power rail parameters go into PROJECT_PARAMS power rail table

**Step 4 — Connector roster.**
From Q2 and Q7, assign:
- Specific connector MPN for each interface (search by type, current rating, PCB mount)
- Pin assignments (signal, power, GND)
- ESD protection requirements per connector (IEC 61000-4-2 class per interface type)
- Reference designators (J1, J2…)

**Step 5 — High-speed signal assessment.**
If the product includes any of these interfaces, HS inventory is required:
- DisplayPort, HDMI, USB 3.x, Thunderbolt, PCIe, MIPI, LVDS, Ethernet ≥ 1G
- Derive: protocol version, data rate, differential impedance, AC coupling requirement,
  layer assignment, skew budget (from the protocol specification)
- If no HS interfaces: record "HS inventory: N/A" and note 4-layer design likely sufficient

**Step 6 — Compliance determination.**
Infer the product category from Q1–Q2 (do not ask the user). Apply the
COMPLIANCE DETERMINATION TABLE: look up the applicable standards for the inferred
category, then apply the stricter value rule (Step 3 of the table) to set all
design parameters. Record in PROJECT_PARAMS TARGET COMPLIANCE field.

**Step 7 — Mechanical parameters.**
From Q6: derive board dimensions (or mark `[TBD-Phase8]` if "as small as possible").
Select mounting hole hardware based on board size (M2.5 for boards < 100×100 mm,
M3 for larger). Default 4 mounting holes at corners unless enclosure constrains otherwise.

**Step 8 — Thermal budget.**
For every IC dissipating > 0.3 W (computed in Step 3):
- P_diss from Step 3
- T_amb = 40°C default (85°C for industrial environments)
- T_j_max from IC datasheet
- Required θ_ja = (T_j_max − T_amb) / P_diss
- Record in PROJECT_PARAMS thermal budget table

**Step 9 — Write PROJECT_PARAMS.md.**
Populate every field. Mark any field that genuinely cannot be derived as
`[TBD-PhaseN]` with the resolving phase. No field left blank without a reason.

**Step 10 — Write SESSION_CONTEXT.md.**
Record Phase 0 completion, all `[TBD]` items, and the preliminary HS complexity
classification (Simple / Complex).

**Step 11 — Create `.claude/CLAUDE.md`.**

Create `[PROJECT_DIR]/.claude/CLAUDE.md`. This file is loaded automatically into every
future Claude Code session opened in this project directory. It keeps critical invariants
in context permanently, preventing rule loss across long sessions and detours. Write the
file with the following content (fill in the bracketed fields from PROJECT_PARAMS):

```markdown
# [PROJECT_NAME] — Claude Code Project Context

## Guide Location
PCB Design Guide: `E:\Claude Projects\CC Project Folder\PCB_Design_Guide_Generic.md`
Python Scripts:   `E:\Claude Projects\CC Project Folder\Python Scripts\`

## Project Paths
PROJECT_DIR:       [resolved PROJECT_DIR]
SCRIPTS_DIR:       [PROJECT_DIR]/Scripts/
CUSTOM_LIBS_DIR:   [PROJECT_DIR]/Part Library/
SESSION_CONTEXT:   [PROJECT_DIR]/SESSION_CONTEXT.md
PROJECT_PARAMS:    [PROJECT_DIR]/PROJECT_PARAMS.md

## Session Start
On every session open: run SESSION START PROTOCOL (guide § "SESSION START PROTOCOL")
before any other action — read this file, run directory health check, read
SESSION_CONTEXT.md, report audit summary to user.

## Invariant Rules (read before every phase and before resuming from any detour)

- **KiCad version:** KiCad 10 only. All CLI commands, layer names, and Python API calls
  are KiCad 10-specific. Do not apply conventions from KiCad 5/6/7.
- **KiCad CLI path:** `C:/Program Files/KiCad/10.0/bin/kicad-cli.exe`
- **Script usage:** Read every shared script before running it. Set its PROJECT CONFIG
  block. Run it from `Python Scripts/` in-place — never copy scripts to the project
  directory. Write a new script in `[SCRIPTS_DIR]` only if no library script fits.
- **Custom library directory:** `[PROJECT_DIR]/Part Library/` — all custom symbols,
  footprints, and 3D models go here. Never use a different path.
- **THE SILKSCREEN RULE:** Reference designators must NOT appear on F.Silkscreen or
  B.Silkscreen. All ref des go on F.Fab / B.Fab only.
- **Schematic-first rule:** Schematic must be fully verified before any PCB layout
  work begins. Phase 3 gate must be clean.
- **Gate rule:** Never skip or partially complete a named GATE section. Every checkbox
  must be verified before proceeding to the next phase.
- **CONNECTOR PROTECTION RULE:** The CONNECTOR_PROTECTION_TABLE in PROJECT_PARAMS must
  have ✅ or N/A in every cell before Phase 3 closes and before routing begins. On every
  Phase 0b revision and every detour return: re-verify the table. Any ❌ is a schematic
  blocker — do not proceed to layout, placement, or routing with an unresolved ❌.
  Required protection by connector type is in the COMPLIANCE DETERMINATION TABLE Step 4b.
- **STOP conditions in the DECISION AUTHORITY TABLE are intentional.** Do not remove
  or bypass them; they exist to prevent silent errors.
- **Per-asset tracking (Phase 1.5):** Symbol, footprint, and 3D model are tracked
  independently. A part is only MANUAL for the specific asset(s) that could not be
  found automatically — not the whole part.
- **Phase 1.5 Manual Escalation chart format:** markdown table with columns
  Ref | MPN | Manufacturer | Package | Lifecycle | Symbol | Footprint | 3D Model.
  ✅ = retrieved automatically; ❌ = needs manual retrieval.
- **Physical pin-1 check (Phase 4 Part D):** Mandatory for all polarized/asymmetric
  parts. Blocking error if failed. Log in `footprint_audit.txt`.
- **HS sandwich verification:** Detect reference planes dynamically from the board's
  layer stack at runtime. Never hardcode layer names. Skip entirely if no HS tracks
  exist on the designated layer.
- **SCHEMATIC CHANGE RULE:** Any schematic component modification (MPN swap, add,
  remove, net reassignment, value change, footprint change, symbol change,
  power/ground reassignment) stops forward progress immediately. Build a change delta
  table, re-run all affected prior phases in order, then resume. See SCHEMATIC CHANGE
  RULE section of the guide.
- **PCB SYNC PROTOCOL:** Before running "Update PCB from Schematic": back up the
  `.kicad_pcb` file. Enable only: re-link footprints by ref, update ref/value/other
  fields. NEVER enable: Replace footprints with library versions, Update footprint
  positions (unless pre-placement), Delete extra footprints (review list first).
  Prefer `sync_netlist.py` for net-only changes. See PCB SYNC PROTOCOL section of the
  guide.

## DETOUR BOOKMARK

*(No active detour)*
```

After writing this file, record in `SESSION_CONTEXT.md`: "`.claude/CLAUDE.md` created."

---

**Outputs:** `PROJECT_PARAMS.md`, `SESSION_CONTEXT.md`, `.claude/CLAUDE.md`.

**Loop exit criteria:** All PROJECT_PARAMS fields filled or `[TBD-PhaseN]`. All IC
selections made or queued for Phase 1 resolution. Compliance standards determined.
No engineering questions asked of the user.

**SESSION_CONTEXT:** Phase 0 complete, PROJECT_PARAMS written, open items list.

---

## PHASE 0b — Revision of Existing Design

**Purpose:** When updating an existing board (v1 → v1a, v1 → v2, etc.), scope the
work to only the phases affected by the changes. Do not re-run the full sequence.

**Inputs:** User's description of what changed; prior `PROJECT_PARAMS.md` and
`SESSION_CONTEXT.md`.

**Process:**
1. Read prior `PROJECT_PARAMS.md` and `SESSION_CONTEXT.md`.
2. Work through the Phase 0 interview for the *delta only* — what components were
   added, removed, or changed; what nets changed; what mechanical changes occurred.
3. Produce a **change delta table:**
   ```
   | Field             | Old Value | New Value | Phases to re-run |
   |-------------------|-----------|-----------|-----------------|
   | U3 MPN            | TPS54360B | TPS54340  | 1, 3, 4, 4.5    |
   | Added connector J5| —         | USB-C     | 0, 1, 2, 3, 4   |
   | Board width       | 80 mm     | 90 mm     | 7, 8            |
   ```
4. Re-run **only the phases listed in the change delta table**, plus Phase 12 and
   Phase 13 always. Skip all other phases.
5. Update `PROJECT_PARAMS.md` with revised values.
6. Update `SESSION_CONTEXT.md` noting revision letter, date, and scope of changes.

**Phase re-run rules (add to delta table when these changes occur):**
- MPN change on any component → Phase 1 (lifecycle), Phase 1.5 (library check for new MPN), Phase 4 (footprint), Phase 4.5
- New component added → Phase 1, Phase 1.5, 2, 3, 4, 4.5, 7.5
- Component removed → Phase 2, 3, 7.5
- Net name change → Phase 5, 7.5, 9
- Board outline change → Phase 7, 8, 9, 12
- Layer count change → Phase 5, 6, 7, 9, 10, 10a
- Compliance target change → Phase 5, 12
- Any schematic change → Phase 2 (ERC), Phase 7.5 (netlist sync), Phase 12

**Protection re-check (mandatory on every Phase 0b revision):**
Regardless of what changed, re-open CONNECTOR_PROTECTION_TABLE in PROJECT_PARAMS and:
1. Add a row for any new connector added in this revision. Populate all cells. Any ❌ is a schematic blocker.
2. For any connector whose signals or power nets changed, re-verify ESD, CM choke, polyfuse, rev-pol, OVP. Update cells.
3. For any connector removed, delete its row.
4. If any cell was previously N/A and is now applicable (e.g., connector type changed), add the required component.
The CONNECTOR_PROTECTION_TABLE must be fully ✅/N/A before Phase 3 re-verification runs.

**Outputs:** Updated `PROJECT_PARAMS.md`, `SESSION_CONTEXT.md`, change delta table.

**Loop exit criteria:** Change delta table complete, re-run phases identified.

---

## COMPLIANCE DETERMINATION TABLE

Applied automatically in Phase 0 Step 6. Claude infers the product category from
the functional description (Q1–Q5) — the user is never asked which category or
which standards apply.

**Design rule:** When a requirement appears in more than one applicable category,
always design to the stricter value. The lookup determines which certification
frameworks to declare on manufacturing docs. The design itself always exceeds
the minimum pass level for every applicable standard.

All designs target CE marking (EU) + UL equivalent (US) + RoHS as a baseline.
RF/radio adds the RED Directive if applicable.

---

### Step 1 — Infer product category from Q1 functional description

Claude reads Q1 and Q2 and classifies the product. Do not ask the user.

| Functional description signals | Category |
|---|---|
| Home use, consumer electronics, hobbyist, personal device | Consumer A/V / IT |
| Professional audio, broadcast, studio, live sound, AV production | Professional A/V |
| Factory, automation, test equipment, industrial process | Industrial |
| Health monitoring, clinical, patient-adjacent, wellness device | Medical |
| Household appliance, white goods, kitchen/laundry device | Household Appliance |
| Contains WiFi, Bluetooth, cellular, or any intentional RF | Add: Radio/Wireless |

If classification is ambiguous between two categories, apply both and take the
stricter requirement for every design parameter.

---

### Step 2 — Applicable standards by category

**Consumer A/V / IT Equipment:**
```
Safety:    EN 62368-1 (CE) / UL 62368-1 (US/CA)
Emissions: EN 55032 Class B / FCC Part 15 Class B
Immunity:  EN 55035
           IEC 61000-4-2  ESD       ±4 kV contact / ±8 kV air
           IEC 61000-4-3  Radiated  3 V/m
           IEC 61000-4-4  EFT       ±1 kV power / ±0.5 kV signal
           IEC 61000-4-5  Surge     ±1 kV CM / ±0.5 kV DM
           IEC 61000-4-6  Conducted 3 Vrms
           IEC 61000-4-11 Voltage dips — full suite
RoHS:      2011/65/EU + 2015/863/EU
```

**Professional A/V Equipment:**
```
Safety:    EN 62368-1 (CE) / UL 62368-1 (US/CA)
Emissions: EN 55032 Class B / FCC Part 15 Class B
           (Class B even though professional — stricter than Class A)
Immunity:  EN 55035 + same IEC 61000-4-x suite as consumer
RoHS:      Same as consumer
```

**Industrial Equipment:**
```
Safety:    EN 61010-1 (CE) / UL 61010-1 (US/CA)
Emissions: EN 55032 Class B / FCC Part 15 Class B
Immunity:  EN 61000-6-2 (industrial immunity suite)
           IEC 61000-4-2  ESD       ±4 kV contact / ±8 kV air
           IEC 61000-4-3  Radiated  10 V/m
           IEC 61000-4-4  EFT       ±2 kV power / ±1 kV signal
           IEC 61000-4-5  Surge     ±2 kV CM / ±1 kV DM
           IEC 61000-4-6  Conducted 10 Vrms
           IEC 61000-4-8  Mag field 30 A/m
RoHS:      Same as consumer
```

**Medical Device (non-life-sustaining, Class I / IIa):**
```
Safety:    EN 60601-1 (CE) / IEC 60601-1 (global)
EMC:       EN 60601-1-2 (4th edition)
           IEC 61000-4-2  ESD       ±8 kV contact / ±15 kV air
           IEC 61000-4-3  Radiated  10 V/m
           IEC 61000-4-4  EFT       ±2 kV power / ±1 kV signal
           IEC 61000-4-5  Surge     ±2 kV CM / ±1 kV DM
           IEC 61000-4-6  Conducted 10 Vrms
           IEC 61000-4-8  Mag field 30 A/m
           IEC 61000-4-11 Voltage dips — full suite
Creepage:  EN 60601-1 reinforced insulation distances (stricter than 62368-1)
RoHS:      Apply in full despite partial exemptions
NOTE:      Medical classification adds regulatory paperwork (technical file, DoC,
           notified body for Class IIa+) — flag this to the user before Phase 1.
```

**Household Appliance:**
```
Safety:    EN 60335-1 (CE) / UL 60335 (US)
Emissions: EN 55014-1
Immunity:  EN 55014-2
           IEC 61000-4-2/3/4/5/6 at consumer levels
RoHS:      Same as consumer
```

**Radio / Wireless (add to whichever base category applies):**
```
CE:        RED Directive 2014/53/EU (replaces EMC Directive for radio products)
           EN 300 328 (2.4 GHz WiFi / BT)
           EN 301 893 (5 GHz WiFi)
           EN 300 440 (other SRDs)
US:        FCC Part 15 Subpart C — intentional radiator SDoC or certification
NOTE:      RF certification requires antenna characterization and conducted/
           radiated spurious emission testing not automatable by this guide.
           Flag to user immediately when radio is detected in Q1/Q2.
```

---

### Step 3 — Apply stricter value across all applicable categories

For every design parameter that appears in multiple applicable standards, use the
strictest value. Examples:

| Parameter | Consumer | Industrial | Medical | **Use** |
|---|---|---|---|---|
| ESD contact | ±4 kV | ±4 kV | ±8 kV | **±8 kV** |
| Radiated immunity | 3 V/m | 10 V/m | 10 V/m | **10 V/m** |
| Conducted immunity | 3 Vrms | 10 Vrms | 10 Vrms | **10 Vrms** |
| EFT power port | ±1 kV | ±2 kV | ±2 kV | **±2 kV** |
| Surge CM | ±1 kV | ±2 kV | ±2 kV | **±2 kV** |
| Creepage (mains) | 62368-1 | 61010-1 | 60601-1 | **60601-1** |
| Emissions | Class B | Class B | Class B | **Class B** |

Write the "Use" column values into the DRU file and schematic protection checks.
The declared standards on manufacturing docs list all applicable category standards.
The design values always meet or exceed the maximum column.

---

### Step 4 — "Exceed Requirements" design rules (unconditional)

Apply these to every project regardless of category — they go beyond minimum pass.
These rules are BLOCKING: no schematic may be written, and no Phase 3 gate may close,
until every cell of the CONNECTOR_PROTECTION_TABLE is ✅ or N/A.

**4a — Build the CONNECTOR_PROTECTION_TABLE.**
For every external connector in the connector roster (from Step 4 of the derivation process):
1. List the connector Ref and type
2. For each signal/power pin group on that connector, determine:
   - **ESD:** Does every externally-reachable signal pin have a TVS or ESD clamp rated
     ≥ ±8 kV contact per IEC 61000-4-2 Level 4? (Power pins: TVS to GND. Signal pins:
     rail-clamping TVS or ESD IC e.g. PRTR5V0U2X, TPD series.) Mark ✅ or ❌.
   - **CM choke:** Does every power-carrying connector have a common-mode choke on the
     power entry traces, plus ≥ 2 Y-capacitors (100 nF each) from each line to GND,
     sized for the switching frequency? Mark ✅, ❌, or N/A (signal-only connector).
   - **Polyfuse:** Does every DC power input connector have a polyfuse (or equivalent
     resettable overcurrent device) rated for the maximum input current? Mark ✅, ❌,
     or N/A (regulated supply with built-in OCP, or signal-only connector).
   - **Rev-pol:** Is the power entry protected against reverse polarity by a P-FET,
     ideal diode controller, or Schottky diode (not relay-dependent)? Mark ✅, ❌,
     or N/A (USB-C: polarity handled by CC, not applicable; signal-only connector).
   - **OVP:** Is there a TVS, Zener, or crowbar at ≤ 120% of nominal voltage on the
     power rail fed by this connector? Mark ✅, ❌, or N/A.
3. Record in CONNECTOR_PROTECTION_TABLE in PROJECT_PARAMS.
4. Any ❌ cell requires a schematic addition before proceeding.

**4b — Mandatory protection components by connector type.**
These are minimum requirements regardless of cost or board space:
| Connector type | ESD required | CM choke | Polyfuse | Rev-pol | OVP |
|---|---|---|---|---|---|
| USB-C (power in) | TVS on CC1, CC2, VBUS, D+/D− | ✅ on VBUS | N/A (PD source provides OCP) | N/A (CC handles orientation) | TVS on VBUS |
| USB-C (signal out) | TVS on SS TX/RX pairs, AUX, SBU | N/A | N/A | N/A | N/A |
| DC barrel / XLR / terminal block (power in) | TVS on V+ pin | ✅ | ✅ polyfuse | ✅ P-FET or ideal diode | TVS on rail |
| FPC / ribbon (display/camera) | ESD IC on all signal pins | N/A | N/A | N/A | N/A |
| Debug / UART header | ESD IC or TVS on signal pins | N/A | N/A | N/A | N/A |
| RF / antenna | ESD IC on RF line | N/A | N/A | N/A | N/A |

**4c — EMC filtering rules.**
- Common-mode choke: rated ≥ 1.5× max input current; impedance ≥ 90 Ω at switching frequency.
  Typical: ACM2012-900-2P-T (3A, 90 Ω at 100 MHz), or equivalent.
- Y-capacitors: 100 nF X7R, 50 V minimum, one per line to GND, placed after choke on
  board side. Do not omit even if a bulk cap is present on the same rail.
- These are placed between the connector and all other circuitry — nothing except the
  polyfuse sits between the connector and the CM choke.

**4d — IPC Class and surface finish (fixed).**
```
IPC Class:      Class 2 minimum. Design to Class 3 tolerances (annular rings,
                via geometry, pad dimensions) where cost-neutral.
Surface finish: ENIG always. Never HASL.
```

**4e — Additional "exceed" rules (non-connector-specific).**
- **Creepage and clearance:** use EN 60601-1 reinforced insulation distances for any
  isolation barrier or mains-adjacent copper — stricter than EN 62368-1.
- **Conformal coating readiness:** design all non-connector areas to be coatable.
  Add "NO COAT" keepout text on F.Fab at every connector and probe access point.

---

### Step 5 — IPC class and surface finish (fixed)

```
IPC Class:      Class 2 minimum. Design to Class 3 tolerances (annular rings,
                via geometry, pad dimensions) where cost-neutral — this exceeds
                Class 2 without requiring Class 3 paperwork.
Surface finish: ENIG always. Never HASL. ENIG eliminates pad oxidation,
                co-planarity issues, and fine-pitch assembly defects.
```

---

### Step 6 — Record in PROJECT_PARAMS

Write the inferred category and its full standard set into `TARGET COMPLIANCE`.
If multiple categories apply, list all standards and note "design values per
Step 3 stricter-value rule." Update `[MANUFACTURING_DIR]/README_for_manufacturer.txt`
in Phase 13 to reference all standards with their declared test levels.

---

## PHASE 1 — Component Production Status Verification

**Purpose:** Confirm every component in the schematic is currently in production (Active
lifecycle) and available at multiple distributors before locking the design.

**Inputs:** `[SCHEMATIC_FILE]` (source schematic — may be a prior version or new draft),
IC roster and connector roster from PROJECT_PARAMS.

**Loops:** None (research and documentation only; no KiCad files modified).

**Process:**
1. Read the schematic; extract a unique component list (reference, value, footprint).
2. For each unique part determine: manufacturer + MPN, lifecycle (Active / NRND /
   Obsolete / EOL), availability at >= 2 of {Digi-Key, Mouser, LCSC, Arrow, Newark},
   minimum stock for prototype qty, lead time if not in stock.
3. Classify each component:
   - **RED** — NRND or Obsolete: must find substitute before Phase 2
   - **YELLOW** — < 2 distributors OR stock < prototype qty: single-source risk
   - **GREEN** — Active, multi-source, in stock
4. Write `[REPORTS_DIR]/component_status.txt` with a table:
   `| Ref | MPN | Lifecycle | Distributors | Min Stock | Lead Time | Notes |`

**BOM AVAILABILITY RESOLUTION LOOP:**
If any RED component exists, STOP. For each RED item:
1. Propose 2–3 specific substitute MPNs with manufacturer name
2. Confirm each substitute's footprint (identical or document the delta)
3. Note any electrical parameter differences
4. Present to user for approval
5. Only after user approval, mark resolved
Do not proceed to Phase 2 until every RED item is resolved.

---

### COMPONENT SELECTION PROTOCOL

Triggered when PROJECT_PARAMS contains `[TBD-Phase1]` for any component MPN. For
each unspecified part, execute this protocol before proceeding:

1. **Define requirements from context.** From the schematic role and surrounding
   circuit, extract: voltage ratings, current ratings, package constraints, key
   electrical parameters (e.g., Vf for a diode, dropout for an LDO), thermal
   requirements.

2. **Search candidates.** Query Digi-Key and LCSC for parts matching all requirements.
   Start with well-known manufacturer lines (TI, Diodes Inc., Vishay, Murata, Würth)
   for reliability. Prefer parts already used elsewhere in the design (reduces BOM
   line count).

3. **Rank by:** Active lifecycle → ≥ 2 distributors in stock → lowest cost at
   prototype quantity → smallest footprint that meets thermal requirements.

4. **Document the top choice** in `component_status.txt`:
   ```
   | Ref | Role | Chosen MPN | Mfr | Package | Key Specs | Reason | Alt MPN |
   ```
   Include one alternative in case the first choice goes out of stock.

5. **Update PROJECT_PARAMS** with the chosen MPN; clear the `[TBD-Phase1]` marker.

6. **Do not ask the user** unless: (a) two equally valid options exist with
   different footprints (ask which package is preferred), or (b) cost difference
   > 2× (present both and ask). Otherwise proceed autonomously and document the
   choice. The user can override during Phase 14 review if needed.

---

**Outputs:** `[REPORTS_DIR]/component_status.txt`.

**Loop exit criteria:** Zero RED items. YELLOW items documented for manual monitoring.
All `[TBD-Phase1]` MPNs resolved via component selection protocol.

**SESSION_CONTEXT:** Phase 1 complete, RED items (resolved), YELLOW items (open),
selection-protocol choices documented.

---

## PHASE 1.5 — Custom Component Library Retrieval

**Purpose:** For every component in the IC ROSTER and CONNECTOR ROSTER that is not
found in KiCad 10's installed standard library, automatically download a KiCad-compatible
symbol (`.kicad_sym`), footprint (`.kicad_mod`), and 3D model (`.step` or `.wrl`) before
schematic or PCB work begins. Saves all retrieved files to `[CUSTOM_LIBS_DIR]` and
updates `sym-lib-table` / `fp-lib-table` so every subsequent phase can resolve them.

**Inputs:** PROJECT_PARAMS IC ROSTER + CONNECTOR ROSTER, internet access.

**Outputs:** Populated `[CUSTOM_LIBS_DIR]/`, updated `sym-lib-table`, `fp-lib-table`,
`[REPORTS_DIR]/library_retrieval.txt`.

**Loops:** None — run once. Re-run if the BOM changes.

**Directory layout:**
```
[CUSTOM_LIBS_DIR]/
  symbols/          ← .kicad_sym files (one library file per source batch or per part)
  footprints/       ← directories of .kicad_mod files, e.g. custom.pretty/
  3dmodels/         ← .step and .wrl files
```

**Asset tracking — treat symbol, footprint, and 3D model as three independent items:**

For each component, maintain a per-asset status record throughout retrieval:
```
asset_status[mpn] = {
    "symbol":    None,   # None = not yet found; "LOCAL"/"SRC2"/"SRC3"/etc. = found; "MANUAL" = exhausted all sources
    "footprint": None,
    "3dmodel":   None,
}
```

Each source is searched independently for each missing asset. When a source returns a
symbol but not a footprint, record the symbol hit and continue searching remaining sources
for the footprint and 3D model. Only mark an individual asset **MANUAL** after all sources
have been tried for that asset specifically. A part is fully resolved only when all three
assets are non-None and non-MANUAL.

**Retrieval priority order — run for each asset independently, stop per-asset when found:**

### Source 1 — KiCad 10 installed library (check first, costs nothing)

Before any download, verify the part is actually missing from the local install:
```python
import os, glob

KICAD_SYM_ROOT   = "C:/Program Files/KiCad/10.0/share/kicad/symbols"
KICAD_FP_ROOT    = "C:/Program Files/KiCad/10.0/share/kicad/footprints"
KICAD_3D_ROOT    = "C:/Program Files/KiCad/10.0/share/kicad/3dmodels"

def search_local_symbols(keyword):
    """Return (lib_path, symbol_name) if found, else None."""
    for sym_file in glob.glob(f"{KICAD_SYM_ROOT}/*.kicad_sym"):
        content = open(sym_file, encoding="utf-8").read()
        if keyword.lower() in content.lower():
            return sym_file
    return None
```

Check each asset independently. If the symbol resolves locally, record it as **LOCAL** and
skip further symbol searches for that part — but still search Sources 2–6 for the footprint
and 3D model if those are not also present locally.

### Source 2 — KiCad official GitHub repositories (no API key required)

KiCad's canonical libraries are on GitHub. Many parts not bundled in the installer exist
in the repos. Use the GitHub Search API (unauthenticated: 60 req/hr; add a
`Authorization: token <GITHUB_TOKEN>` header from `GITHUB_TOKEN` env var if available
to raise to 5000 req/hr).

```python
import requests, os, pathlib

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

def search_kicad_github(mpn: str, repo: str, extension: str):
    """Search a KiCad GitHub repo for a file matching the MPN."""
    url = (f"https://api.github.com/search/code"
           f"?q={requests.utils.quote(mpn)}+repo:KiCad/{repo}"
           f"+extension:{extension}")
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    items = r.json().get("items", [])
    return items  # each item has a 'html_url' and 'path'

def download_github_raw(repo: str, path: str, dest: pathlib.Path):
    raw_url = f"https://raw.githubusercontent.com/KiCad/{repo}/master/{path}"
    r = requests.get(raw_url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest
```

- Symbol search: `repo=kicad-symbols`, `extension=kicad_sym`
- Footprint search: `repo=kicad-footprints`, `extension=kicad_mod`
- 3D model search: `repo=kicad-packages3D`, `extension=step` or `wrl`

### Source 3 — easyeda2kicad (LCSC database — best coverage, no API key)

`easyeda2kicad` converts LCSC/EasyEDA components to KiCad format directly from
the LCSC part number. Requires pre-installation: `pip install easyeda2kicad`.

```python
import subprocess, pathlib

def fetch_easyeda(lcsc_id: str, dest_dir: pathlib.Path):
    """
    lcsc_id: e.g. "C2040" (the LCSC part number for the component).
    Outputs symbol, footprint, and 3D model into dest_dir.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([
        "easyeda2kicad",
        "--full",                      # symbol + footprint + 3D model
        "--lcsc_id", lcsc_id,
        "--output", str(dest_dir / lcsc_id),
        "--overwrite",
    ], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"easyeda2kicad failed: {result.stderr}")
    return dest_dir
```

To find the LCSC part number: search `lcsc.com` or `jlcpcb.com/parts` for the MPN.
Record the LCSC ID (format: C + digits) in `library_retrieval.txt`.

easyeda2kicad outputs:
- `<lcsc_id>.kicad_sym` → move to `[CUSTOM_LIBS_DIR]/symbols/`
- `<lcsc_id>.pretty/<lcsc_id>.kicad_mod` → move to `[CUSTOM_LIBS_DIR]/footprints/<lcsc_id>.pretty/`
- `<lcsc_id>.step` → move to `[CUSTOM_LIBS_DIR]/3dmodels/`

### Source 4 — SnapEDA API (requires free API key)

Register at `snapeda.com` → API → generate a free key. Store as env var `SNAPEDA_API_KEY`.

```python
import requests, os, zipfile, pathlib, io

SNAPEDA_KEY = os.environ.get("SNAPEDA_API_KEY", "")

def fetch_snapeda(mpn: str, dest_dir: pathlib.Path):
    if not SNAPEDA_KEY:
        raise EnvironmentError("SNAPEDA_API_KEY env var not set")
    # 1. Search for the part
    search_url = "https://www.snapeda.com/api/v1/parts/search/"
    params = {"q": mpn, "which_kicad": "kicad6", "has_symbol": True}
    headers = {"Authorization": f"Token {SNAPEDA_KEY}"}
    r = requests.get(search_url, params=params, headers=headers, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        return None
    part_id = results[0]["slug"]
    # 2. Download KiCad package
    dl_url = f"https://www.snapeda.com/api/v1/assets/download/?snap_id={part_id}&format=kicad6"
    r2 = requests.get(dl_url, headers=headers, timeout=30)
    r2.raise_for_status()
    # 3. Unzip and place files
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(r2.content)) as z:
        z.extractall(dest_dir)
    return dest_dir
```

SnapEDA KiCad 6+ exports are compatible with KiCad 10 — the `.kicad_sym` and `.kicad_mod`
format has been stable since KiCad 6.

### Source 5 — Manufacturer website (WebSearch fallback)

Search `<MPN> kicad symbol footprint filetype:kicad_sym site:ti.com OR site:st.com OR
site:microchip.com OR site:nxp.com OR site:analog.com`. Download any `.kicad_sym` or
`.kicad_mod` file found. Place in `[CUSTOM_LIBS_DIR]/symbols/` and `/footprints/`.

### Source 6 — Manual (fallback of last resort)

Applied per asset, not per part. If all sources have been tried for a specific asset
(symbol, footprint, or 3D model) and none succeeded, mark that individual asset as
**MANUAL** in `asset_status` and in `library_retrieval.txt`. The other assets for the same
part may already be resolved and are unaffected. Do not stop — continue processing
remaining components and remaining assets. All MANUAL assets across all parts are collected
and presented to the user together in Step 8. Do not ask the user for files piecemeal.

---

**Step 6a — Automated datasheet pin table extraction**

Run this for every component before Step 7. It builds the `datasheet_pins` list that
Step 7 requires. If the extraction confidence is LOW, the component is escalated to
MANUAL (no download attempt needed).

```python
import re, pathlib, requests

def fetch_datasheet_text(mpn: str) -> tuple[str, str]:
    """
    Returns (text, source_url) or raises if not found.
    Search order: manufacturer site → DigiKey product page → Octopart.
    """
    import urllib.parse
    # Build search query
    query = urllib.parse.quote(f"{mpn} datasheet filetype:pdf")
    search_url = (
        f"https://www.google.com/search?q={query}"
        f"+site:ti.com+OR+site:st.com+OR+site:microchip.com"
        f"+OR+site:nxp.com+OR+site:analog.com+OR+site:digikey.com"
    )
    # Use WebSearch / WebFetch via Claude's tools — not requests directly.
    # This function is a placeholder; in practice call WebSearch then WebFetch
    # on the first PDF URL returned.
    raise NotImplementedError("Use WebSearch + WebFetch to fetch the datasheet PDF")

def extract_pin_table(text: str, expected_pin_count: int) -> dict:
    """
    text: full text extracted from datasheet PDF (via pdf2text or similar).
    Returns {"confidence": "HIGH"|"MEDIUM"|"LOW", "pins": [{"number":, "name":, "type":}]}
    """
    # Section headings that precede pin tables
    section_re = re.compile(
        r'(?:pin\s+(?:configuration|descriptions?|functions?|assignments?)'
        r'|terminal\s+functions?'
        r'|signal\s+descriptions?)',
        re.IGNORECASE
    )
    # A pin table row: starts with a number, followed by a name, optionally a type
    row_re = re.compile(
        r'^\s*(\d+)\s+([A-Z][A-Z0-9_/\-]{0,19})\s*(I|O|I/O|P|S|Input|Output|Power|Supply|Bidirectional|NC|No\s*Connect)?',
        re.MULTILINE | re.IGNORECASE
    )

    pins = []
    # Find the section
    section_match = section_re.search(text)
    search_text = text[section_match.start():section_match.start() + 4000] if section_match else text

    for m in row_re.finditer(search_text):
        num, name, ptype = m.group(1), m.group(2), m.group(3) or "passive"
        pins.append({"number": num, "name": name.strip(), "type": ptype.strip().lower()})

    # Deduplicate by number, keep first occurrence
    seen = set()
    unique_pins = []
    for p in pins:
        if p["number"] not in seen:
            seen.add(p["number"])
            unique_pins.append(p)

    # Confidence scoring
    count_match = len(unique_pins) == expected_pin_count
    has_types   = sum(1 for p in unique_pins if p["type"] != "passive") > len(unique_pins) // 2
    confidence = "HIGH" if (count_match and has_types) else \
                 "MEDIUM" if count_match else "LOW"

    return {"confidence": confidence, "pins": unique_pins}
```

**Process for each component:**
1. Use WebSearch to find the datasheet: query `<MPN> datasheet site:ti.com OR site:digikey.com`
2. Use WebFetch on the first PDF URL returned
3. Call `extract_pin_table(pdf_text, expected_pin_count)` where `expected_pin_count`
   comes from the IC package (e.g., SOIC-8 → 8, QFN-32 → 32, from PROJECT_PARAMS)
4. Confidence HIGH or MEDIUM → use `pins` list as `datasheet_pins` in Step 7
5. Confidence LOW → log `DATASHEET_PARSE_FAIL` in `library_retrieval.txt`, escalate
   to MANUAL (Step 7 cannot run without a verified pin table)

**Log format addition:**
```
MPN       | LCSC | Source | Status | VERIFY | DATASHEET_CONF | Files
----------|------|--------|--------|--------|----------------|------
TPS54360B | C123 | easyeda| OK     | OK     | HIGH           | ...
RARE_IC_X | —    | SnapEDA| MANUAL | SKIP   | LOW (PARSE_FAIL)| ...
```

---

**Step 7 — Mandatory pin verification for every downloaded symbol**

Run this immediately after each download, before the file is moved to `[CUSTOM_LIBS_DIR]`.
A symbol that fails verification is escalated to **MANUAL** status (→ Phase 2 Step 2b).

**Offline / no-web-access mode:** if `WebSearch` and `WebFetch` are unavailable, all
datasheet extraction steps are skipped and every component defaults to MANUAL status for
symbol verification. The Step 8 holdpoint is released by the user explicitly typing
"accept all MANUAL" — this acknowledges that all unverified symbols will require manual
review in Phase 2 Step 2b before their footprints can be trusted. Do not block
indefinitely waiting for web access that will not arrive.

```python
import re, pathlib, sys

def verify_symbol_pins(kicad_sym_path: pathlib.Path, datasheet_pins: list[dict]) -> dict:
    """
    datasheet_pins: list of {"number": "1", "name": "VIN", "type": "power_in"} dicts.
    Built from the IC datasheet pin table before calling this function.
    Returns {"status": "OK"|"FAIL", "issues": [...]}
    """
    text = kicad_sym_path.read_text(encoding="utf-8")
    # Extract all (pin ...) blocks — use DOTALL to handle multi-line blocks
    # Then extract name and number independently (order may vary by generator)
    pin_blocks = re.findall(r'\(pin\s+\w+\s+\w+.*?\)\s*\)', text, re.DOTALL)
    found_pins = []
    for block in pin_blocks:
        name_m = re.search(r'\(name\s+"([^"]+)"', block)
        num_m  = re.search(r'\(number\s+"([^"Za-z0-9_]*[A-Za-z0-9_]+)"', block)
        if name_m and num_m:
            found_pins.append((name_m.group(1), num_m.group(1)))
    # found_pins: list of (name, number) tuples
    issues = []

    # 1. Pin count must match datasheet
    if len(found_pins) != len(datasheet_pins):
        issues.append(
            f"Pin count mismatch: symbol has {len(found_pins)}, "
            f"datasheet has {len(datasheet_pins)}"
        )

    # 2. No duplicate pin numbers
    nums = [p[1] for p in found_pins]
    dupes = [n for n in nums if nums.count(n) > 1]
    if dupes:
        issues.append(f"Duplicate pin numbers: {set(dupes)}")

    # 3. No empty pin names
    empty = [p[1] for p in found_pins if not p[0].strip()]
    if empty:
        issues.append(f"Empty pin names on pins: {empty}")

    # 4. Cross-check pin numbers against datasheet
    ds_nums = {str(p["number"]) for p in datasheet_pins}
    sym_nums = set(nums)
    missing_in_sym = ds_nums - sym_nums
    extra_in_sym   = sym_nums - ds_nums
    if missing_in_sym:
        issues.append(f"Pins in datasheet but missing from symbol: {missing_in_sym}")
    if extra_in_sym:
        issues.append(f"Pins in symbol but not in datasheet: {extra_in_sym}")

    # 5. Cross-check pin names (case-insensitive, allow / vs _ substitution)
    def normalize(s):
        return re.sub(r"[/_\s-]", "", s).lower()

    ds_names  = {normalize(p["name"]) for p in datasheet_pins}
    sym_names = {normalize(p[0]) for p in found_pins}
    name_mismatches = ds_names.symmetric_difference(sym_names)
    if name_mismatches:
        issues.append(f"Pin name mismatches (normalized): {name_mismatches}")

    return {"status": "FAIL" if issues else "OK", "issues": issues}
```

**Usage in the retrieval script:**

```python
# After downloading a symbol file to a temp path:
datasheet_pins = [
    # Build this table from the component datasheet BEFORE calling verify.
    # Use WebSearch or read the datasheet PDF to extract the full pin table.
    # Example for a 3-pin LDO:
    {"number": "1", "name": "GND",  "type": "power_in"},
    {"number": "2", "name": "VIN",  "type": "power_in"},
    {"number": "3", "name": "VOUT", "type": "power_out"},
]
result = verify_symbol_pins(temp_sym_path, datasheet_pins)
if result["status"] == "FAIL":
    # Escalate to MANUAL — do not copy to Part Library
    log_entry["status"] = "MANUAL"
    log_entry["issues"] = result["issues"]
    print(f"[WARN] {mpn}: symbol verification failed → escalating to Phase 2 Step 2b")
    print("  Issues:", result["issues"])
else:
    # Move to Part Library
    shutil.copy(temp_sym_path, CUSTOM_LIBS_DIR / "symbols" / temp_sym_path.name)
    log_entry["status"] = "OK"
```

**Verification is mandatory.** Never skip it — not even for KiCad official GitHub downloads.
A wrong pin number causes a net connection error that ERC may not catch if the wrong net
is plausibly connected. The datasheet pin table must be read and transcribed before the
retrieval script runs for that component.

**Schematic labeling requirement:** Every pin in the downloaded or custom symbol must be
either connected to a named net or explicitly marked `(no_connect ...)` in the schematic.
A pin that is present in the symbol but absent from the schematic is a Phase 3 ERC error.
There are no floating pins in a correct KiCad schematic.

Log outcome in `library_retrieval.txt` under a **VERIFY** column:
```
MPN       | LCSC_ID | Source        | Status | VERIFY | Files / Issues
----------|---------|---------------|--------|--------|---------------------------
TPS54360B | C15623  | easyeda2kicad | OK     | OK     | symbols/C15623.kicad_sym
BAD_IC    | C99999  | SnapEDA       | MANUAL | FAIL   | Pin count 24≠28; see log
```

---

**Step 8 — Manual Parts Escalation Holdpoint**

After all components have been processed through Sources 1–6 and Step 7, collect every
part still flagged **MANUAL** in `library_retrieval.txt`. Before presenting anything to the
user, run the following pre-escalation checks for each MANUAL part:

**Pre-escalation checks (must all pass before the user is asked to act):**
1. **Exists:** Confirm the MPN returns valid results on Digikey, Mouser, or the
   manufacturer's site. If the MPN resolves to nothing, flag it as an **MPN ERROR** and
   escalate to the user separately — this is a BOM problem, not a library problem.
2. **Active lifecycle:** Confirm the part's lifecycle status is Active or at worst NRND.
   If it is Obsolete or End-of-Life, do not ask the user to find the file — instead pause
   Phase 1.5 entirely and return to Phase 1 to select a replacement MPN.
3. **Correct part:** Confirm the MPN matches the intended function and package from
   PROJECT_PARAMS (value, package, footprint type). If there is a mismatch, resolve it
   before asking the user for files.
4. **Gap audit:** For each MANUAL part, record exactly which assets are missing —
   symbol only, footprint only, 3D model only, or some combination. A part that has a
   valid downloaded symbol but only a missing 3D model requires much less user effort than
   a fully missing part.

Once all four checks pass for every MANUAL part, **stop and present the following chart to
the user before proceeding any further.** Do not begin Phase 2 or any subsequent phase
until the user confirms the files have been placed.

**Chart format (one row per part, three asset status columns):**

| Ref | MPN | Manufacturer | Package | Lifecycle | Symbol | Footprint | 3D Model |
|-----|-----|--------------|---------|-----------|--------|-----------|----------|
| U3  | LTC3025IDC-1 | Analog Devices | 6-DFN (2×3mm) | Active | ❌ | ❌ | ❌ |
| D2  | MMSZ5245B    | ON Semi        | SOD-123        | Active | ✅ | ✅ | ❌ |

Use ✅ for any asset already retrieved automatically. Use ❌ for any asset that must be
provided manually.

Below the chart, include:

```
Drop all files into your project's Part Library folder.
When all files are in place, reply "files placed" and Phase 1.5 will continue.
```

**After user confirms files are placed:**
- Run Step 7 pin verification on every newly placed symbol.
- Run Phase 4 Part D pin-1 orientation check on every newly placed footprint before
  proceeding to Phase 2.
- Update `library_retrieval.txt`: change status from MANUAL to OK (or MANUAL-VERIFIED if
  the source was user-supplied rather than an automated download).
- If any newly placed file fails Step 7 verification, report the specific failure to the
  user with the exact pin mismatch before asking them to source a replacement.

**Never present this chart more than once per phase run.** Collect all MANUAL parts first,
then ask once. Do not interrupt the user mid-phase for individual parts.

---

**After retrieval — register libraries with KiCad:**

Write `[SCRIPTS_DIR]/register_custom_libs.py`:
```python
import pathlib, re

CUSTOM_LIBS_DIR = pathlib.Path(r"[CUSTOM_LIBS_DIR]")
PROJECT_DIR     = pathlib.Path(r"[PROJECT_DIR]")
SYM_LIB_TABLE  = PROJECT_DIR / "sym-lib-table"
FP_LIB_TABLE   = PROJECT_DIR / "fp-lib-table"

# --- sym-lib-table: add one (lib ...) entry per .kicad_sym found ---
sym_entries = []
for sym_file in (CUSTOM_LIBS_DIR / "symbols").glob("*.kicad_sym"):
    lib_name = sym_file.stem
    sym_entries.append(
        f'  (lib (name "{lib_name}")'
        f' (type "KiCad") (uri "${{KIPRJMOD}}/Part Library/symbols/{sym_file.name}")'
        f' (options "") (descr "Custom retrieved symbol"))'
    )

if sym_entries:
    content = SYM_LIB_TABLE.read_text(encoding="utf-8") if SYM_LIB_TABLE.exists() else "(sym_lib_table\n)"
    # insert before closing paren
    content = content.rstrip().rstrip(")")  + "\n" + "\n".join(sym_entries) + "\n)\n"
    SYM_LIB_TABLE.write_text(content, encoding="utf-8")

# --- fp-lib-table: add one (lib ...) entry per .pretty/ directory found ---
fp_entries = []
for pretty_dir in (CUSTOM_LIBS_DIR / "footprints").glob("*.pretty"):
    lib_name = pretty_dir.stem
    fp_entries.append(
        f'  (lib (name "{lib_name}")'
        f' (type "KiCad") (uri "${{KIPRJMOD}}/Part Library/footprints/{pretty_dir.name}")'
        f' (options "") (descr "Custom retrieved footprint"))'
    )

if fp_entries:
    content = FP_LIB_TABLE.read_text(encoding="utf-8") if FP_LIB_TABLE.exists() else "(fp_lib_table\n)"
    content = content.rstrip().rstrip(")") + "\n" + "\n".join(fp_entries) + "\n)\n"
    FP_LIB_TABLE.write_text(content, encoding="utf-8")

print("Library tables updated.")
```

**Important:** Use `${KIPRJMOD}` (not a hardcoded path) in all library table entries so
the project remains portable.

**3D models from Part Library** are referenced in the footprint `.kicad_mod` file
as:
```
(model "${KIPRJMOD}/Part Library/3dmodels/<PARTNAME>.step"
  (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))
```
Phase 4.5 (`assign_3d_models.py`) checks `[CUSTOM_LIBS_DIR]/3dmodels/` before the
system 3D model directory.

**Quality note:** Footprints from all external sources must still pass Phase 4
dimension validation against the component datasheet land pattern. Never skip Phase 4
for downloaded footprints.

**`library_retrieval.txt` format:**
```
MPN            | LCSC_ID | Source        | Status  | Files
---------------|---------|---------------|---------|-------------------------------
TPS54360B      | C15623  | easyeda2kicad | OK      | symbols/C15623.kicad_sym ...
ESP32-S3-WROOM | C2913202| easyeda2kicad | OK      | symbols/C2913202.kicad_sym ...
SY8089AAAC     | —       | SnapEDA       | OK      | symbols/SY8089AAAC.kicad_sym
CUSTOM_IC_X    | —       | ALL_FAILED    | MANUAL  | → Phase 2 Step 2b
```

**SESSION_CONTEXT:** Phase 1.5 complete, N parts retrieved (breakdown by source),
M parts flagged MANUAL for Phase 2 Step 2b.

---

## PHASE 2 — Create Project Files

**Purpose:** Create or scaffold the working project files at `[PROJECT_DIR]`. Achieve
ERC-clean baseline (only known-acceptable warnings remain).

**Inputs:** PROJECT_PARAMS, source project (if migrating a prior version).

**Loops:** Loop A (ERC), Loop B on any fix scripts.

**Process:**
1. If migrating from a prior version: copy root-level files (not subdirs) to
   `[PROJECT_DIR]`, rename to `[PROJECT_NAME]`, update `.kicad_pro` project name field,
   copy any project-local symbol libraries, write sym-lib-table pointing to
   `${KIPRJMOD}/<lib>.kicad_sym`, copy `fp-lib-table`.

1a. **Application circuit derivation — run for every IC in PROJECT_PARAMS before
   writing any schematic.** For each IC:

   **Sub-step A — Locate reference design:**
   Search `<MPN> datasheet typical application` and `<MPN> reference circuit`.
   Find the manufacturer's recommended application schematic (usually in the datasheet
   under "Typical Application", "Application Schematic", or "Reference Design").

   **Sub-step B — Extract passive values:**
   From the reference design, record:
   - Decoupling caps: value, voltage rating, package (X7R/X5R, 10V min for 3.3V rails)
   - Feedback divider resistors: compute R_top, R_bottom for target V_out using
     the datasheet formula (e.g., `V_out = V_ref * (1 + R_top/R_bot)`)
   - Timing / frequency-setting resistors and capacitors (oscillators, soft-start, SS/TR)
   - Bootstrap capacitors (BOOT pin): typical 100 nF per datasheet
   - Snubber networks: value from datasheet if specified, else skip
   - AC coupling on any signal output: 100 nF default unless datasheet specifies otherwise
   Record all values in a **PASSIVE_TABLE** entry in PROJECT_PARAMS for this IC.

   **Sub-step B2 — Build PROXIMITY_RULES_TABLE:**

   For each passive identified in Sub-step B, derive exactly which component it serves
   and encode as a (ref_A, ref_B, max_dist_mm, type, reason) tuple. Use the threshold
   lookup table below — select the relationship type, copy the threshold. Write all
   rules to PROJECT_PARAMS before Step 2. This step runs entirely from schematic and
   datasheet reading — no PCB file exists yet.

   **Derivation procedure:**
   1. For each passive (R, C, L, D, TVS): read its net connections in the schematic.
   2. Identify the owner component — the IC or connector the passive directly serves.
   3. Select the relationship type from the table below.
   4. Write the rule: `(passive_ref, owner_ref, threshold_mm, type, one-sentence reason)`.

   **Common derivation errors — check each:**
   - **AC coupling caps:** owner is the **source IC** whose output the cap is inline with,
     not the destination IC. If C5 is between U3's TX output and J1 pin 2, the rule is
     `(C5, U3, ...)` not `(C5, J1, ...)`.
   - **Pull-up/pull-down resistors:** owner is the **IC whose pin the resistor connects
     to**, not the power rail it pulls toward.
   - **ESD clamps:** owner is the **connector** where the external signal enters, not the
     IC downstream.
   - **Snubbers:** owner is the **switching IC** at the switch node, not the inductor or
     output cap.

   **Threshold lookup table (connecting-pad to connecting-pad distance; centroid-to-centroid fallback when no shared net exists):**

   | Type | Max dist (mm) | Reason |
   |---|---|---|
   | `decoupling_bypass` | 8 | Bypass cap — short switching current loop |
   | `decoupling_bulk` | 15 | Bulk cap — less loop-critical, same power zone sufficient |
   | `esd_clamp` | 8 | Must intercept signal before it travels on the board |
   | `crystal` | 12 | Oscillator stub traces radiate — keep them short |
   | `bootstrap` | 8 | BOOT cap must be directly adjacent to switching IC |
   | `snubber` | 6 | Must be at the switch node — further away kills effectiveness |
   | `ac_coupling` | 10 | Inline on signal path — should be at source output |
   | `filter` | 12 | EMC/power filter at power entry point |
   | `pullup_pulldown` | 20 | Less critical — just needs to be on same bus segment |

   **net_hint — which net to specify:**
   Set `net_hint` to the exact KiCad net name of the pad whose distance matters most.
   For a bypass cap on a VDD pin, use the power net name (e.g. `VDD`, `+3V3`). For an
   ESD clamp on a signal line, use the signal net name. For bulk caps or LDO-to-IC rules
   where no single net is the critical path, set `net_hint` to `None` — the script uses
   the minimum distance across all shared nets. The net name must match the KiCad netlist
   exactly (case-sensitive).

   **PROXIMITY_RULES_TABLE format in PROJECT_PARAMS:**
   ```
   PROXIMITY_RULES_TABLE:
     ref_A | ref_B | max_dist_mm | type              | net_hint | reason
     ------|-------|-------------|-------------------|----------|-------
     C4    | U2    | 8           | decoupling_bypass | VDD      | 100nF bypass cap on U2 VDD pin
     C5    | U1    | 15          | decoupling_bulk   | None     | 10uF bulk input cap for U1 VIN
     D1    | J1    | 8           | esd_clamp         | USB_DP   | TVS on J1 signal pins
     Y1    | U3    | 12          | crystal           | XTAL_IN  | 16MHz crystal for U3 MCU clock
   ```

   **Sub-step C — Resolve strapping pins:**
   Identify all configuration pins (ADDR, MODE, SEL, OE, FREQ, SYNC, EN, SHDN).
   For each: determine the required state from product requirements, then either:
   - Pull HIGH: 100 kΩ to appropriate VDDIO
   - Pull LOW: 100 kΩ to GND
   - Drive directly: connect to MCU GPIO (record in GPIO_TABLE)
   - NC: mark `(no_connect)` in schematic ONLY if datasheet confirms it is safe floating
   No configuration pin may be left without a defined state.

   **Sub-step D — MCU GPIO assignment (if MCU present):**
   Build a GPIO_TABLE in PROJECT_PARAMS:
   1. Assign dedicated peripheral pins first (SPI: CLK/MOSI/MISO/CS; I2C: SDA/SCL;
      USB: D+/D-; UART: TX/RX; ADC: analog-capable pins only) — use IC datasheet
      alternate function table, prefer contiguous GPIO banks for each bus.
   2. Assign PWM / timer outputs (for LED dimming, switching converter sync, motor control).
   3. Assign control GPIO (enable pins, status LEDs, mode selects, interrupt inputs).
   4. Mark remaining GPIO as `SPARE_GPIO_<N>` — bring to debug header.
   Record: GPIO#, pin name, net name, direction, connected-to.

   **Sub-step E — Write PASSIVE_TABLE and GPIO_TABLE to PROJECT_PARAMS before Step 2.**
   No schematic code is written until both tables are complete. Phase 3 CHECKs 1, 2,
   and 6 verify values from these tables.

2. **If starting fresh** — write `[SCHEMATIC_FILE]` from scratch as a `.kicad_sch`
   S-expression file. Use the following primitive templates:

   **File header:**
   ```
   (kicad_sch (version 20251024) (generator "eeschema") (generator_version "10.0")
     (paper "A3")
     (lib_symbols ...)
     (wire ...) ...
     (symbol_instances ...)
   )
   ```

   **Symbol placement** (one per IC/connector from PROJECT_PARAMS IC roster):
   ```
   (symbol (lib_id "Device:R") (at X Y ROT) (unit 1)
     (property "Reference" "R1" (at X Y 0))
     (property "Value" "10k" (at X Y 0))
     (property "Footprint" "Resistor_SMD:R_0402_1005Metric" (at X Y 0))
     (property "MPN" "RC0402FR-0710KL" (at X Y 0))
   )
   ```

   **Wire:**
   ```
   (wire (pts (xy X1 Y1) (xy X2 Y2)))
   ```

   **Power symbol (for each rail in PROJECT_PARAMS):**
   ```
   (symbol (lib_id "power:VCC") (at X Y 0) (unit 1)
     (property "Reference" "#PWR01" (at X Y 0) (do_not_place))
     (property "Value" "+3V3" (at X Y 0))
   )
   ```

   **Net label:**
   ```
   (label "NET_NAME"
     (at X Y 0)
     (effects (font (size 1.27 1.27)))
   )
   ```
   Note: KiCad 10 uses `(label ...)` not `(net_label ...)`. The label text is the first
   positional string, not a `(text ...)` sub-field. For global labels (crossing sheet
   boundaries) use `(global_label "NET_NAME" (shape input) (at X Y 0) ...)`.

   **No-connect:**
   ```
   (no_connect (at X Y))
   ```

   **PWR_FLAG** (required on every power net to suppress ERC "Pin unconnected"
   errors — one per power rail, placed near the power symbol):
   ```
   (symbol (lib_id "power:PWR_FLAG") (at X Y 0) (unit 1)
     (property "Reference" "#FLG01" (at X Y 0) (do_not_place))
     (property "Value" "PWR_FLAG" (at X Y 0))
   )
   ```

   Write the complete schematic for all ICs, connectors, and passive components from
   PROJECT_PARAMS in a single script: `[SCRIPTS_DIR]/create_schematic.py`.

2b. **Custom symbol creation** — only for parts flagged MANUAL in Phase 1.5
   `library_retrieval.txt` (all automated retrieval sources exhausted). If Phase 1.5
   has not run yet, run it first before creating symbols by hand.
   Create the custom symbol in `[PROJECT_DIR]/[PROJECT_NAME].kicad_sym`:
   ```
   (kicad_symbol_lib (version 20231120) (generator "kicad_symbol_editor")
     (symbol "PART_NAME"
       (pin input line (at X Y 0) (length 2.54)
         (name "PIN_NAME" (effects (font (size 1.27 1.27))))
         (number "1" (effects (font (size 1.27 1.27))))
       )
       ... (one pin entry per IC pin from datasheet)
       (rectangle (start -5.08 TOP) (end 5.08 BOTTOM) (stroke (width 0)) (fill (type background)))
     )
   )
   ```
   Update `sym-lib-table` to include `${KIPRJMOD}/[PROJECT_NAME].kicad_sym`.
   Write pin table from datasheet before coding — list every pin with: number, name,
   type (input/output/bidirectional/power_in/power_out/passive/no_connect).

3. Create the PCB stub file `[PCB_FILE]` with a minimal valid KiCad 10 PCB header
   declaring `[LAYER_COUNT]` copper layers plus all user layers listed above. Use the
   layer count decided in Phase 6 (if Phase 6 has not run yet, default to 2 and update
   in Phase 7).

4. Create directories: `[REPORTS_DIR]`, `[SCRIPTS_DIR]`, `[BACKUPS_DIR]`.

4a. **Shared script library — read before use.** A library of reusable scripts exists
   in the `Python Scripts/` folder (sibling to `[PROJECT_DIR]`). When a phase
   requires one of these scripts:

   1. **Read the script in full** from `Python Scripts/` before running it.
      Verify its logic, assumptions, and CONFIG fields are appropriate for this project.
      A script valid for one board may have assumptions that don't hold for another.
   2. **Set its PROJECT CONFIG block** using values from PROJECT_PARAMS — paths,
      layer names, net names, thresholds. Do not run with stale placeholder values.
   3. **Run it directly from `Python Scripts/`** — do not copy it to `[SCRIPTS_DIR]`.
      `[SCRIPTS_DIR]` is for scripts written fresh for this specific project only.

   **Reference table — library scripts by phase:**

   | Script | Phase | Key CONFIG fields to set |
   |---|---|---|
   | `run_freerouting.py` | 10a | FREEROUTING_JAR, TEMP_DIR, MAX_PASSES |
   | `score_autoroute.py` | 10a | PCB_FILE |
   | `assign_3d_models.py` | 4.5 | PCB_FILE, REPORT_FILE |
   | `check_copper_balance.py` | 11.5 | PCB_FILE, REPORT_FILE, TARGET_LAYERS (match stackup) |
   | `check_keepouts.py` | 10c | PCB_FILE |
   | `check_pad_orientation.py` | 10a pre-flight | PCB_FILE |
   | `check_polarity_markers.py` | 11.5 | PCB_FILE, REPORT_FILE |
   | `add_return_vias.py` | 11 | PCB_FILE, REPORT_FILE, GND_NET_NAME, ADD_VIAS |
   | `add_silkscreen.py` | 11 | PCB_FILE |
   | `add_coating_notes.py` | 11.5 | PCB_FILE |
   | `check_lowspeed_bus.py` | 11.5 | PCB_FILE |
   | `check_component_proximity.py` | 8 | PCB_FILE, REPORT_FILE, RULES (from PROXIMITY_RULES_TABLE in PROJECT_PARAMS) |
   | `validate_footprint_dims.py` | 4 | PCB_FILE, REPORT_FILE |
   | `verify_dfm.py` | 12 | PCB_FILE, REPORT_FILE |
   | `width_audit.py` | 11.5 Loop D | PCB_FILE, POWER_NETS — preferred over verify_trace_widths.py; auto-excludes neck-downs near pads |
   | `verify_trace_widths.py` | 11.5 Loop D | PCB_FILE, REPORT_FILE — fallback if width_audit.py misclassifies segments |
   | `find_close_net_refs.py` | 10d debug | PCB_FILE |
   | `trace_length_check.py` | 10d (non-HS nets) | PCB_FILE, REPORT_FILE |
   | `insert_meanders.py` | 10d | PCB_FILE, BACKUPS_DIR, TOLERANCE, MIN_CLEARANCE |
   | `insert_meanders_tight.py` | 10d | PCB_FILE, BACKUPS_DIR |

   **Scripts requiring project-specific data structures** (beyond path config):
   | Script | Phase | Data to set from PROJECT_PARAMS |
   |---|---|---|
   | `verify_highspeed.py` | 10, 11.5 | `HS_LAYER_NAME` = `[HS_ROUTING_LAYER]`; `HS_PAIRS` = one entry per pair `{name: (P_net, N_net)}`; `LANE_GROUPS` = groups with limits from HS inventory |
   | `measure_all_diff_pairs.py` | 10d (HS diff pairs) | `GROUPS` = one entry per pair `(label, P_net, N_net)` from HS inventory; `INTERLANE_SETS` = `(prefix, label, limit_mm)` per lane group; `INTRA_LIMIT_MM` = protocol intra-pair spec |
   | `lock_highspeed_nets.py` | 10 (post HS routing) | `HS_NETS` = all P and N net names from HS_PAIRS in PROJECT_PARAMS |
   | `verify_hs_sandwich.py` | 10, 10b (post re-route) | `HS_LAYER` = `[HS_ROUTING_LAYER]`; `GND_NET_NAME` = GND net name |
   | `verify_thermal.py` | Loop E (Phase 9+) | `THERMAL_TABLE` = one row per IC > 0.3W from THERMAL BUDGET in PROJECT_PARAMS; `T_AMB_C` = worst-case ambient |
   | `compute_impedance.py` | 5 Step 4b | Per-project script written to `[SCRIPTS_DIR]` (not a shared library script). Config: `H_MM`, `T_MM`, `ER` from Phase 6 stackup; `TARGETS` = impedance targets per pair class; `ROUTING_LAYER_TYPE` = `"inner"` or `"outer"` |
   | `measure_loops.py` | 10 Loop C | `LOOP_NETS` = `{net_number: label}` for every SW node net — net numbers from `[PCB_FILE]` after Phase 7.5 sync |

   After Phase 7.5 netlist sync, SW node net numbers and HS pair net names are known
   and all HS-dependent scripts can be fully configured.

   **If no library script fits the task:** write a new one in `[SCRIPTS_DIR]` per the
   phase instructions. Do not force a library script to do something it was not written
   for — read it first, and if it does not fit, write a fresh one.

   **GitHub API rate limit (Phase 1.5 Source 2):** The KiCad GitHub library search
   uses the GitHub API unauthenticated, which allows 60 requests/hour. For typical
   BOMs of 10–30 unique parts this is usually sufficient. If the limit is hit
   mid-retrieval, the script receives a 403 and automatically falls through to the
   next source (easyeda2kicad or SnapEDA) — it is not a hard failure.

5. **Auto-annotation** — ensure every component has a unique reference designator
   before ERC. Write `[SCRIPTS_DIR]/annotate_schematic.py`:
   ```python
   import pcbnew, subprocess
   subprocess.run([KICAD_CLI, "sch", "export", "netlist",
                   "--annotate", "--output", NETLIST_PATH, SCHEMATIC_FILE], check=True)
   ```
   Alternatively, parse the `.kicad_sch` and assign sequential ref designators
   (R1, R2…; C1, C2…; U1, U2…; J1, J2…) to any component with reference `R?`, `C?`,
   `U?`, `J?`. Write back to file atomically.

6. **PWR_FLAG audit** — before running ERC, verify a `PWR_FLAG` symbol is present
   on every unique power net. Write `[SCRIPTS_DIR]/add_pwr_flags.py`:
   - Parse the schematic for all `(lib_id "power:*")` net names
   - For each unique power net, check for a `power:PWR_FLAG` on the same net
   - Add missing PWR_FLAGs adjacent to the power symbol (offset 2.54 mm)
   - Save schematic. This eliminates the most common ERC false-positive.

7. **Loop A — ERC:** Run
   `kicad-cli.exe sch erc --output [REPORTS_DIR]/ERC_phase2_01.rpt [SCHEMATIC_FILE]`.
   KNOWN-ACCEPTABLE: any documented cosmetic warnings from the source project.
   NEW-REAL-ERROR: everything else.
   For each NEW-REAL-ERROR write `fix_erc_<N>.py` (apply Loop B), re-run ERC.

8. Write `[REPORTS_DIR]/fix_log_phase2.txt`.

**Outputs:** `[SCHEMATIC_FILE]`, `[PCB_FILE]` stub, `fix_log_phase2.txt`.

**Loop exit criteria:** ERC exit 0, only known-acceptable warnings remain. All
components annotated with unique reference designators. PWR_FLAGs present on all
power nets.

**SESSION_CONTEXT:** Phase 2 complete, ERC status, custom symbols created (if any).

---

## PHASE 3 — Schematic Design Verification

**Purpose:** Verify the schematic is electrically correct before layout. Any issue found
here is fixed in the schematic (schematic-first rule), ERC re-run clean, and only then
does Phase 4 proceed.

**Gate:** Phase 1.5 must be complete before this phase runs. Every component in the BOM
must have status OK or MANUAL in `library_retrieval.txt`. MANUAL parts must have a
verified custom symbol from Phase 2 Step 2b (pin table checked against datasheet). A
component with missing or unverified library assets blocks Phase 3.

**Inputs:** `[SCHEMATIC_FILE]`, PROJECT_PARAMS (power rails, ICs, HS signals,
connectors, protection requirements), `[REPORTS_DIR]/library_retrieval.txt`.

**Loops:** Loop A after any schematic change; Loop B on fix scripts.

**Process — run each applicable check; each check is a schematic-first fix loop:**

For each check below, report PASS or FAIL with evidence. On FAIL, correct in schematic,
apply Loop A (ERC) before next check.

- **CHECK 1: Regulator output voltage** — For every regulator in PROJECT_PARAMS: verify
  feedback divider or fixed-output MPN yields the target rail voltage per datasheet
  formula. If off, correct resistor values (E96 series).
- **CHECK 2: Overvoltage / overcurrent thresholds** — For any converter with OVP/OCP
  setting resistors: verify divider produces the intended threshold. Document in report.
- **CHECK 3: I2C / control interfaces** — For every I2C-controlled IC: verify SDA/SCL
  nets connect to bus, pull-up resistors present (typical 4.7k to VDDIO). Document
  programming sequences in `[REPORTS_DIR]/i2c_programming_plan.txt`.
- **CHECK 4: ESD protection on exposed I/O** — Open CONNECTOR_PROTECTION_TABLE. For every
  connector row, verify the ESD cell is ✅. For each ✅, confirm the ESD device is present
  in the schematic by net: the ESD clamp must sit between the connector pad net and the
  signal/power net going to the IC, not after the IC. If any cell is ❌, STOP — add the
  device to gen_schematic.py and re-run this check. Do not proceed with any other check
  until all ESD cells are ✅.
  Specifically verify:
  - USB-C CC pins: TVS or ESD IC between connector CC1/CC2 pads and PD controller IC
  - USB-C SS lanes: ESD IC between connector SS pads and signal source/sink IC
  - USB 2.0 D+/D−: ESD IC between connector D+/D− and SoC/hub
  - DC power V+ pin: TVS between connector V+ and first downstream component
  - Any FPC connector: ESD IC on all differential signal pins
  - Any debug header: TVS or ESD series resistor on all signal pins

- **CHECK 5: Reverse polarity + overcurrent protection** — Open CONNECTOR_PROTECTION_TABLE.
  For every DC power connector row, verify:
  - **Polyfuse cell ✅:** A resettable fuse (polyfuse or polymeric PTC) is present
    between the connector V+ pin and the CM choke / power rail. Verify in schematic.
    If ❌: STOP — add polyfuse to gen_schematic.py. Typical spec: rated 1.5× steady-state
    current, ≤ 2× rated voltage.
  - **Rev-pol cell ✅:** A P-FET ideal diode, LTC4412-class controller, or equivalent
    is present and functional. A TVS alone does NOT satisfy this requirement.
    If ❌: STOP — add reverse polarity circuit to gen_schematic.py.
  - USB-C connectors: mark both cells N/A (USB PD source provides OCP; CC handles orientation).
  - Signal-only connectors: mark both cells N/A.
  Both cells must be ✅ or N/A before this check passes.
- **CHECK 6: Enable pin states** — For every IC EN / SHDN / PWM_EN pin: verify a
  defined driven state (never floating). Pull to appropriate rail via 100k if not
  driven.
- **CHECK 7: Test points** — For every power rail in PROJECT_PARAMS and each critical
  signal net (SDA, SCL, GND, control lines): verify a `TestPoint` symbol present in
  schematic. Add if missing.
- **CHECK 8: Bulk input capacitance** — For each switching converter: compute
  `C_in_min = I_load / (f_sw * dV_ripple)`. Verify total input capacitance exceeds this.
  Add ceramic + bulk if short.
- **CHECK 9: AC coupling on HS pairs** — For each `[HS_PAIR_TX_*]` differential pair in
  PROJECT_PARAMS: check the datasheet for both the TX and RX IC. Some ICs include internal
  AC coupling — placing external caps on both sides of the same link double-couples and
  degrades signal integrity. Place caps on the TX output side only unless the TX datasheet
  explicitly requires them on the RX side. Typical values: 100 nF X7R 0402 for DP, per
  DisplayPort PHY datasheet for HDMI. Add only where the combined TX+RX datasheet review
  confirms they are missing.
- **CHECK 10: HS control / configuration strapping** — For each HS mux, retimer, or
  bridge IC: verify every mode/address/OE/SEL pin is driven or strapped. No floats.
- **CHECK 11: MCU decoupling** — For every VDD and VDDA pin on the MCU: 100 nF within
  1 mm plus one shared bulk (4.7 uF typical). VDDA additionally requires a ferrite bead
  or 10 ohm series from VDD.
- **CHECK 12: Level shifting on cross-domain signals** — For every signal crossing a
  voltage domain (5V HPD to 3.3V GPIO, etc.): verify level shifter or divider present
  and target GPIO tolerates the voltage.
- **CHECK 13: Grounding topology** — For designs with analog + digital + power grounds:
  confirm single-point tie (star ground) or continuous plane strategy is consistent with
  PROJECT_PARAMS layer plan.

**Outputs:** `[REPORTS_DIR]/schematic_verification.txt`,
`[REPORTS_DIR]/i2c_programming_plan.txt` (if applicable).

**Loop exit criteria:** Every applicable check PASS, ERC clean.

**SESSION_CONTEXT:** Phase 3 complete, all checks pass.

---

## PHASE 4 — Footprint Audit + Dimension Validation

**Purpose:** Two-part check on every component footprint. Part A verifies the footprint
*name* matches the correct package. Part B verifies the footprint's *land pattern
dimensions* (pad size, pitch, exposed pad, courtyard) match the manufacturer's datasheet.

**Inputs:** `[SCHEMATIC_FILE]`, `fp-lib-table`, KiCad 10 footprint library at
`C:/Program Files/KiCad/10.0/share/kicad/footprints/`, and Phase 1.5 downloads at
`[CUSTOM_LIBS_DIR]/footprints/`.

**Loops:** Loop A after any schematic footprint change, Loop B on fix scripts.

**PART A — Footprint Name Verification:**
For every component: (1) footprint field non-empty, (2) library:footprint resolves via
fp-lib-table, (3) package name matches actual package, (4) reference / value / footprint
fields all present, (5) flag any through-hole component (target: SMD-only for reflow).

For each IC in PROJECT_PARAMS, list the expected library:footprint. Any mismatch:
write `fix_footprints.py`, apply Loop B, then Loop A.

**PART B — Footprint Dimension Validation:**
For each IC, locate the actual `.kicad_mod` file. Read and extract:
- Signal pad width × height
- Pitch (center-to-center)
- Exposed pad size (for QFN/DFN)
- Courtyard bounding box

Compare against datasheet-specified land pattern with IPC-7351B tolerances. Any FAIL:
either substitute a correct footprint from the library or generate a custom footprint
in `[PROJECT_DIR]/custom.pretty/`.

Read `Python Scripts/validate_footprint_dims.py`, configure its PROJECT CONFIG block
(PCB_FILE, REPORT_FILE), and run it to parse and compare.

**PART C — Symbol Pin-to-Footprint Pad Cross-check:**
For every component that uses a downloaded or custom symbol/footprint (status OK or MANUAL
in `library_retrieval.txt`), verify that the symbol's pin numbers map 1-to-1 onto the
footprint's pad numbers, and vice versa. Mismatches cause silent net connection errors
that survive ERC and only manifest as shorts or opens on the assembled board.

**PART D — Physical Pin-1 / Pad-Order Orientation Check (blocking):**
Pad numbering consistency (Part C) does not catch physical reversal — where pad 1 in the
`.kicad_mod` is on the wrong physical side or corner of the package. This is the most
common cause of assembled boards with reversed pinouts despite a passing ERC.

For every polarized or asymmetric component (ICs, diodes, polarized capacitors, MOSFETs,
connectors, SOT/SOD packages):

1. **Fetch the datasheet** for the exact MPN (WebSearch → PDF). Find the package mechanical
   drawing (not the circuit diagram). Note: the physical location of pin 1 (dot, chamfer,
   tab, band, or "1" label) and the pin numbering direction (clockwise vs. counter-clockwise
   for ICs; anode/cathode sides for diodes).

2. **Read the `.kicad_mod` file** and extract pad 1's (X, Y) coordinates. In KiCad's
   coordinate system, Y increases downward. Pad 1 should be at the corner/side that
   matches the datasheet pin-1 marker when the footprint is viewed from the top (component
   side, F.Cu orientation).

3. **Cross-check:** Confirm that the quadrant/side where pad 1 sits in the `.kicad_mod`
   matches the datasheet. Common failures:
   - SOT-23: pad 1 is gate/base — wrong side = MOSFET or BJT wired backwards
   - SOD-123 / SMA diode: pad 1 is cathode — wrong side = diode reversed
   - 2-row IC (SOIC, TSSOP): pin 1 at top-left when notch/dot is at top-left — mirrored
     footprint causes all nets shifted by one pin
   - QFN: pin 1 counter-clockwise from corner dot — rotated footprint shifts all nets

4. **If mismatch found:** Do NOT rotate the placed component on the PCB — that moves the
   silkscreen without fixing the pad-to-net assignment. Instead:
   a. Download the MPN-specific footprint from Phase 1.5 sources and verify it.
   b. If no MPN-specific footprint exists, correct the `.kicad_mod` pad 1 position in
      `[CUSTOM_LIBS_DIR]/footprints/custom.pretty/` by mirroring or renumbering pads.
   c. Re-run Part C after any pad edit to confirm numbering is still consistent.

**Never use a KiCad generic footprint for a part whose pad order you have not verified
against the actual MPN datasheet.** Generic footprints (e.g., `SOT-23`, `SMA`) often
match the IPC standard but individual manufacturers deviate — always confirm with the
specific datasheet.

Log each component's pin-1 check result in `[REPORTS_DIR]/footprint_audit.txt` as:
```
REF | MPN | Pin-1 location (datasheet) | Pad-1 location (kicad_mod) | PASS/FAIL
```

Write `[SCRIPTS_DIR]/check_pin_pad_map.py`:
```python
import re, pathlib, sys

def get_sym_pin_numbers(kicad_sym_path: pathlib.Path, symbol_name: str) -> set:
    text = kicad_sym_path.read_text(encoding="utf-8")
    # Match numeric and alphanumeric pin numbers (e.g. BGA pads A1, B12)
    return set(re.findall(r'\(number\s+"([A-Za-z0-9_]+)"', text))

def get_fp_pad_numbers(kicad_mod_path: pathlib.Path) -> set:
    text = kicad_mod_path.read_text(encoding="utf-8")
    # Extract pad numbers (exclude NPTH pads which have no net)
    # Pattern supports both numeric (1, 2) and alphanumeric (A1, B12) pad numbers
    smd_pads = re.findall(r'\(pad\s+"?([A-Za-z0-9_]+)"?\s+(?:smd|thru_hole)', text)
    return set(smd_pads)

def cross_check(sym_path, sym_name, mod_path, ref):
    sym_pins = get_sym_pin_numbers(sym_path, sym_name)
    fp_pads  = get_fp_pad_numbers(mod_path)
    issues   = []
    in_sym_not_fp = sym_pins - fp_pads
    in_fp_not_sym = fp_pads  - sym_pins
    if in_sym_not_fp:
        issues.append(f"{ref}: symbol pins not in footprint pads: {in_sym_not_fp}")
    if in_fp_not_sym:
        issues.append(f"{ref}: footprint pads not in symbol pins: {in_fp_not_sym}")
    return issues
```

Any mismatch is a **blocking error**. Fix options in priority order:
1. If the footprint is wrong package variant: substitute correct footprint from library.
2. If the symbol pin numbering is wrong: correct it in `[CUSTOM_LIBS_DIR]/symbols/` and
   re-run Phase 1.5 Step 7 pin verification.
3. If the footprint pad numbers are wrong: correct the `.kicad_mod` in
   `[CUSTOM_LIBS_DIR]/footprints/` — never edit system library files.

**Note on custom footprint storage:** Any new or corrected footprint file goes in
`[CUSTOM_LIBS_DIR]/footprints/custom.pretty/` (not `[PROJECT_DIR]/custom.pretty/`).
`fp-lib-table` must reference this path via `${KIPRJMOD}/Part Library/footprints/custom.pretty`.

**Outputs:** `[REPORTS_DIR]/footprint_audit.txt`, `[REPORTS_DIR]/fix_log_phase4.txt`.

**Loop exit criteria:** Every footprint name matches; every dimension within IPC-7351B
tolerance; every polarized/asymmetric component has a confirmed pin-1 physical location
match to its MPN datasheet (Part D PASS for all).

**SESSION_CONTEXT:** Phase 4 complete.

---

## PHASE 4.5 — 3D Model Assignment

**Purpose:** Assign 3D models (.wrl or .step) to every footprint so 3D viewer / STEP
export produces a complete board model. Missing models make height clearance checks
impossible.

**Inputs:** `[PCB_FILE]`, `C:/Program Files/KiCad/10.0/share/kicad/3dmodels/`,
`[CUSTOM_LIBS_DIR]/3dmodels/` (populated by Phase 1.5).

**Loops:** Loop B only (3D models don't affect DRC).

**Process:**
1. Scan `C:/Program Files/KiCad/10.0/share/kicad/3dmodels/` and
   `[CUSTOM_LIBS_DIR]/3dmodels/` (Phase 1.5 downloads) to enumerate available .wrl/.step.
2. Read `Python Scripts/assign_3d_models.py`, configure its PROJECT CONFIG block
   (PCB_FILE, REPORT_FILE), and run it. The script:
   - Uses `${KICAD8_3DMODEL_DIR}` as the path variable (env-portable)
   - For each footprint without a model: match reference prefix + footprint name to
     the best available .wrl or .step
   - Adds via pcbnew API (`fp.Models().push_back(FP_3DMODEL())`)
   - Logs any footprint without a match in REPORT_FILE
3. Save PCB and verify `(model ...)` blocks present in footprints.

**Step 5 — Unmatched model fallback (run for every footprint still without a model):**

Read `[REPORTS_DIR]/3d_models.txt`. For each unmatched footprint, attempt the following
sources in order, stopping per-footprint when a model is found:

**Source A — Manufacturer website.**
Search `<MPN> STEP 3D model site:ti.com OR site:st.com OR site:microchip.com OR
site:nxp.com OR site:analog.com OR site:mouser.com OR site:digikey.com`. Download any
`.step` or `.stp` file found. Place in `[CUSTOM_LIBS_DIR]/3dmodels/<MPN>.step`.

**Source B — GrabCAD / 3DContentCentral.**
Search `<MPN> STEP model site:grabcad.com OR site:3dcontentcentral.com`. These sites
host community-contributed models for common packages. Download and place as above.

**Source C — KiCad package generic model.**
If the footprint is a standard package (0402, 0603, SOT-23, SOIC-8, QFN-N, etc.), the
KiCad 10 system library includes generic package models. Search
`C:/Program Files/KiCad/10.0/share/kicad/3dmodels/` for a `.wrl` or `.step` matching
the package name (e.g. `R_0402_1005Metric.wrl`). A generic model is acceptable for
height clearance checks — it will not show the correct IC markings but the package
outline will be accurate.

**Source D — Datasheet dimensions (last resort).**
If no model exists from any source, check `[DATASHEETS_DIR]/<MPN>.pdf` for the package
mechanical drawing. Record the overall body dimensions (L × W × H in mm). Write a note
in `3d_models.txt`:
```
<REF> <MPN>: NO MODEL FOUND — body approx. L×W×H mm per datasheet page N.
Height clearance check must be performed manually.
```
Mark this footprint as **WAIVER-NO-MODEL** in the report. Do not block phase exit for
this footprint, but flag it in SESSION_CONTEXT as requiring a manual height check in
Phase 14.

After each successful source match: re-run `assign_3d_models.py` with the new file in
`[CUSTOM_LIBS_DIR]/3dmodels/` to confirm the model attaches correctly. Apply Loop B.

**Step 6 — Final model coverage check.**
After all fallback sources are exhausted, re-read `[REPORTS_DIR]/3d_models.txt` and
count:
- **Assigned:** footprints with a `.wrl` or `.step` model attached
- **WAIVER-NO-MODEL:** footprints with no model from any source, documented above

Every footprint must be in one of these two states. A footprint that is simply
unmatched with no documentation is not acceptable — either find a model or explicitly
waive it with the datasheet dimensions note.

**Outputs:** `[REPORTS_DIR]/3d_models.txt`.

**Loop exit criteria:** Script exits 0; every footprint either has a model attached or
is documented as WAIVER-NO-MODEL with body dimensions in `3d_models.txt`; DRC count
unchanged.

**SESSION_CONTEXT:** Phase 4.5 complete, N models assigned, M WAIVER-NO-MODEL
(list refs and MPNs); manual height checks required for any WAIVER-NO-MODEL parts.

---

## PHASE 5 — Compliance Design Rules Setup

**Purpose:** Write the project `.kicad_dru` file. Applies compliance targets and
fabricator rules before any layout.

**Inputs:** PROJECT_PARAMS (compliance targets, voltages, HS impedance targets).

**Loops:** None (writes rules file, tested against PCB in later phases).

**Process:** Write `[PROJECT_DIR]/[PROJECT_NAME].kicad_dru`. Only the project-named DRU
file is loaded by KiCad — any other `.kicad_dru` in the directory is ignored.

Include rule classes:

- **Safety clearance** — For each rail, clearance to any other net based on voltage:
  - > 30V: 1.5 mm (Pollution Degree 2 typical)
  - 15–30V: 1.0 mm
  - 5–15V: 0.5 mm
  - < 5V: 0.15 mm signal minimum
- **Fabrication (IPC-2221B):** signal trace >= 0.15 mm; power widths from PROJECT_PARAMS
  current table; via drill >= 0.3 mm; annular ring >= 0.15 mm; courtyard clearance 0.25 mm;
  copper-to-edge clearance 0.5 mm; silkscreen width 0.1 mm.
  **Edge clearance hierarchy (three distinct rules — all must be met):**
  - 0.5 mm: minimum copper (traces, pours) to Edge.Cuts — DRC-enforced
  - 1.0 mm: routing keepout band inside Edge.Cuts (Phase 7) — prevents autorouter from
    placing traces in the V-score/routing relief zone; wider than the copper rule intentionally
  - 3.0 mm: component courtyard to Edge.Cuts (Loop F item a) — PnP machine rail clearance;
    applies to component bodies, not traces
- **HS differential pair rules** (only if HS signals present):
  - Target impedance (100 ohm / 90 ohm / 85 ohm) per pair class
  - Width and spacing seeded from fab impedance calculator for the Phase 6 stackup
  - Intra-pair skew <= 0.01 mm (design target); protocol spec <= 0.127 mm; inter-lane skew per PROJECT_PARAMS
  - Clearance to any other copper on HS layer >= 2 × trace width (3W rule)
  - Custom rule class "HighSpeed" covering all `[HS_PAIR_*]` nets
- **Component-specific overrides** — for tight-pitch resistor arrays or connectors
  where the geometry cannot meet the default clearance, add per-reference rules:
  ```
  (rule "resistor_array_clearance"
    (constraint clearance (min 0.13mm))
    (condition "A.Reference == 'RN1' || A.Reference == 'RN2'")
  )
  ```
  Note: use `A.Reference` — `A.Footprint` matching does not work reliably in KiCad 10.

**Embedded impedance calculation (IPC-2141A stripline formula):**
> **Deferred step:** This calculation requires Phase 6 stackup values (H_MM, T_MM, εr).
> Skip it on first pass through Phase 5. Return here immediately after Phase 6 completes
> and before the Pre-Layout Gate. Mark Phase 5 as PARTIAL in SESSION_CONTEXT until the
> impedance section is completed.

For each HS pair class, compute the required trace width directly from the Phase 6
stackup rather than relying on an external tool. Given:
- `H` — dielectric thickness between signal layer and adjacent GND plane (mm)
- `T` — copper thickness (mm): 1 oz = 0.035 mm, 2 oz = 0.070 mm
- `εr` — dielectric constant of substrate (FR4: 4.2–4.5; use 4.3 for JLC/PCBWay)
- `Z0` — target impedance (ohm)

**Differential microstrip (outer layer, for escape routing only):**
```
W_single = (5.98 × H) / (exp(Z0 × sqrt(εr) / 87) - 0.8 × T)   [approx]
W_diff ≈ W_single × 0.85   (for 100-ohm differential, empirical correction)
S_diff ≈ W_diff             (gap ≈ width for 100-ohm differential)
```

**Differential stripline (buried, In2.Cu — preferred for HS):**
```
Z0_diff ≈ (2 × 60 / sqrt(εr)) × ln(4 × H / (0.67 × π × (0.8 × W + T)))

Solving for W given Z0_diff, εr, H, T:
  term = exp(Z0_diff × sqrt(εr) / 120)  — note: 120 not 60, for differential
  W = (4 × H / (0.67 × π × term) - T) / 0.8
  S ≈ 2 × W  (for 100-ohm differential on same reference planes)
```

> **Use `compute_impedance.py` as the authoritative source for W and S values.** The
> inline formula above is for reference only — rounding errors in manual evaluation
> produce wrong trace widths. The script performs the full numerical inversion and
> validates the result by back-substituting W into the forward formula.

Run `Python Scripts/compute_impedance.py`. Configure H_MM, T_MM, ER from the Phase 6
stackup, set ROUTING_LAYER_TYPE to `"inner"` for buried stripline (preferred) or
`"outer"` for microstrip (escape routing only), and populate TARGETS with the
differential impedance targets from the HS inventory. The script outputs W and S in mm
and prints the exact `.kicad_dru` constraint line. Record the computed W/S in
PROJECT_PARAMS and use them as the HS trace width/spacing in the DRU file.

**Typical values for 6-layer JLC stackup (JLC06161H-3313 or similar):**
- H (In2.Cu to In1.Cu GND) ≈ 0.21 mm; T = 0.035 mm; εr = 4.3
- 100-ohm differential: W ≈ 0.15 mm, S ≈ 0.20 mm (seed values calibrated for JLCPCB JLC2116 6-layer stackup, 0.21 mm prepreg, Dk ≈ 4.0 — run compute_impedance.py with your fabricator's actual dielectric thickness and Dk values before use; these numbers are wrong for any other stackup)

Validate: matching parens; valid constraint type names; no empty condition strings.

**Outputs:** `[PROJECT_DIR]/[PROJECT_NAME].kicad_dru`,
`[PROJECT_DIR]/compliance_rules_summary.txt`,
`[SCRIPTS_DIR]/compute_impedance.py` with computed W/S values.

**Loop exit criteria:** DRU file syntactically valid.

**SESSION_CONTEXT:** Phase 5 complete, rules classes list.

---

## PHASE 6 — Layer Stack Decision

**Purpose:** Prove the minimum viable layer count before any layout starts. The decision
must be justified in writing, not assumed.

**Inputs:** PROJECT_PARAMS (HS signal inventory, power rail count, IC package density,
compliance targets).

**Loops:** None (analysis only).

**Process:** For the design, write proofs that 2 / 4 / 6 / 8 layers are or are not
required. Standard reasoning:

- **2 layers fail** if any of: multi-Gbps HS signals; more than 3 switching converters;
  any IC in QFN with more than 20 pins; a solid unbroken GND plane cannot coexist with
  routing area.
- **4 layers fail** if HS signals present: both signal layers need adjacent solid GND
  return; with power planes on one inner layer, only one signal layer gets a clean
  GND return.
- **6 layers minimum + sufficient** for multi-Gbps HS: buried stripline on In2.Cu
  sandwiched between GND on In1.Cu and In3.Cu; In4.Cu carries power planes; F.Cu / B.Cu
  handle low-speed signals and escape routing. Provides consistent impedance, clean
  return, EMI shielding of HS from power.
- **8 layers rejected** unless there are DDR memory routing needs, multiple isolated
  impedance domains, or thermal power planes beyond a single In4.Cu can carry.

Write `[PROJECT_DIR]/layer_analysis.txt` with the written proofs and the selected
stackup:

```
LAYER STACK ([LAYER_COUNT] layers):
  F.Cu    35um  role
  In1.Cu  35um  role
  ...
  B.Cu    35um  role
Total thickness: [BOARD_THICKNESS_MM]  +/- 10%
Substrate: FR4 Tg >= 150C IPC-4101C/21
Impedance control on: [HS_ROUTING_LAYER]  (specify target ohms per pair class)
```

**Outputs:** `[PROJECT_DIR]/layer_analysis.txt`, updated PROJECT_PARAMS with final
stackup.

**Loop exit criteria:** Written proof exists and concludes with a specific minimum layer
count.

**Return trigger:** Immediately after this phase completes, return to Phase 5 to
complete the deferred impedance calculation. Update SESSION_CONTEXT for Phase 5 from
PARTIAL to complete after the impedance values are computed and written to the DRU file.

**SESSION_CONTEXT:** Phase 6 complete, chosen layer count.

---

## PRE-LAYOUT GATE

Before Phase 7, verify:
- [ ] Phase 1 complete: zero RED components
- [ ] Phase 1.5 complete: all components have library assets (status OK or MANUAL in `library_retrieval.txt`)
- [ ] Phase 2 complete: ERC clean baseline
- [ ] Phase 3 complete: all schematic checks PASS
- [ ] Phase 4 complete: every footprint name and dimension correct; every polarized/asymmetric component pin-1 orientation verified against MPN datasheet (Part D)
- [ ] Phase 4.5 complete: 3D models assigned
- [ ] Phase 5 complete: DRU file written
- [ ] Phase 6 complete: layer_analysis.txt exists with proof
- [ ] CONNECTOR_PROTECTION_TABLE complete: every cell ✅ or N/A — no ❌ remaining
- [ ] Phase 3 CHECK 4 (ESD per connector) passed with no STOPs
- [ ] Phase 3 CHECK 5 (polyfuse + rev-pol per connector) passed with no STOPs

Do not start Phase 7 if any item is unchecked.

---

## PHASE 7 — Board Outline and Layer Stack

**Purpose:** Set physical board shape and layer stackup in the PCB file. Board size is
PROVISIONAL — it will be minimized in Phase 8 after placement.

**Inputs:** PROJECT_PARAMS (`[BOARD_WIDTH_MM]`, `[BOARD_HEIGHT_MM]`, mounting hole
positions), `layer_analysis.txt`.

**Loops:** Loop A (DRC), Loop B on the setup script.

**Known-acceptable DRC violations this phase:** "Board has no tracks", "Footprint has no
courtyard" on mounting holes, "Unconnected items". No clearance violations acceptable.

**Process:** Write `[SCRIPTS_DIR]/setup_board.py`:

1. **Board outline** — Delete existing Edge.Cuts geometry. Add outline as `gr_line`
   segments per `[BOARD_SHAPE]`:
   - Rectangular: four segments from `(X0, Y0)` to `(X0+W, Y0+H)`
   - Polygonal / irregular: user-provided vertex list
   Anchor origin at (100, 100) so all coordinates are positive.
2. **Layer stack** — Update the PCB `(layers ...)` section to declare
   `[LAYER_COUNT]` copper layers per PROJECT_PARAMS.
3. **Mounting holes** — For each hole in PROJECT_PARAMS: place using footprint
   `MountingHole:MountingHole_<D>mm_M<size>` (NPTH variant), inset by the specified
   margin from the board edge. Add a keepout zone around each hole (no copper / no vias
   / no tracks) at the specified radius.

   **Warning:** Certain library footprint IDs do not exist in KiCad 10 and cause GUI
   DRC to crash (see Phase 10c). For example, `MountingHole_2.7mm_M2.5_Pad_Drill`
   does not exist — use `MountingHole_2.7mm_M2.5`. Verify each mounting hole footprint
   ID resolves in the KiCad 10 library before using it.
4. **Edge keepout** — Add a 1 mm keepout band inside Edge.Cuts (no tracks / no pours /
   no vias in this band). Keep this band narrow — a keepout covering >70% of board
   area will block FreeRouting entirely (see Phase 10a pre-flight).

Apply Loop B on the script. Then Loop A (DRC).

**Outputs:** `[REPORTS_DIR]/board_setup_log.txt`.

**Loop exit criteria:** DRC clean except known-acceptable list.

**SESSION_CONTEXT:** Phase 7 complete, provisional board dims, layer count confirmed.

---

## PHASE 7.5 — Netlist Synchronization

**Purpose:** Import the schematic netlist into the PCB so pads know which nets they
belong to. Without this the PCB has components but no ratsnest.

**When to run:** After Phase 7. Re-run after every schematic change in any later phase.

**Inputs:** `[SCHEMATIC_FILE]`, `[PCB_FILE]`.

**Loops:** Loop A (DRC after sync), Loop B on the script.

**Process:** Write `[SCRIPTS_DIR]/sync_netlist.py`:
1. Export the netlist from the schematic:
   ```
   kicad-cli.exe sch export netlist --format kicadsexpr \
     --output [PROJECT_DIR]/netlist.net [SCHEMATIC_FILE]
   ```
2. Sync footprints and nets into the PCB. `pcbnew.ImportNetlist()` does not exist in
   KiCad 10. Use the two-step approach instead:

   **Step 2a — Update footprints from schematic (kicad-cli):**
   ```
   kicad-cli.exe pcb update-footprints --schematic [SCHEMATIC_FILE] [PCB_FILE]
   ```
   This propagates any footprint changes from the schematic to the PCB.

   **Step 2b — Apply net assignments via S-expression parsing:**
   Parse `netlist.net` (KiCad S-expression format) to extract `(net N "NAME")` entries.
   Load `[PCB_FILE]` as text, update each `(pad ... (net N "NAME"))` entry to match the
   netlist, add or update the `(net ...)` declarations at the top of the file, and write
   the file atomically (write to a temp path then rename). This is the reliable method
   for net assignment in KiCad 10 Python scripts since the pcbnew bindings do not expose
   a direct netlist import function.
3. Report totals: nets imported, pads updated, unassigned pads (should be 0 except for
   mounting holes).

**Known-acceptable DRC:** "Unconnected items", "Footprint not in schematic" (mounting
holes only). No clearance violations acceptable.

**Outputs:** `[REPORTS_DIR]/netlist_sync_log.txt`.

**Loop exit criteria:** All pads have net assigned; DRC has no clearance violations.

**SESSION_CONTEXT:** Phase 7.5 complete, N nets, M pads updated.

---

## PCB SYNC PROTOCOL

**When:** Any time the schematic changes after the PCB exists and components have been
placed. Covers Phase 7.5 re-runs and any mid-layout schematic correction.

**Risk:** KiCad's "Update PCB from Schematic" dialog has options that can silently
destroy placed component positions, swap footprints, or delete footprints entirely.
Read the checklist below before clicking Apply.

### Safe options (enable these)
- ✅ **Re-link footprints by reference designator** — updates pad-to-net assignments
- ✅ **Update reference designator fields** — keeps ref des text in sync
- ✅ **Update value fields** — syncs value strings
- ✅ **Update other fields** — syncs manufacturer/MPN/description properties

### Dangerous options (disable every time)
- ❌ **Replace footprints with library versions** — overwrites custom Part Library
  footprints with whatever is currently in the global library. **Never check this.**
- ❌ **Update footprint positions** — moves every placed component back to origin.
  Only enable this if placement has not yet started (pre-Phase 8).
- ❌ **Delete extra footprints** — removes footprints not in the schematic. Legitimate
  for removed components, but review the list line-by-line before applying. SOM
  connector units and mounting holes are often flagged incorrectly.

### Pre-sync backup (mandatory)
Before opening "Update PCB from Schematic":
```
copy [PCB_FILE] [PCB_FILE].bak_presync
```
If the sync produces unexpected moves or footprint replacements, close without saving
and restore from `.bak_presync`.

### Preferred workflow for net/pad updates only
Use `sync_netlist.py` (Phase 7.5) rather than the KiCad dialog when the only changes
are net name updates, value changes, or new net assignments. The script operates on the
S-expression file directly and never touches footprint geometry or placement.

### Review the proposed-changes list before applying
KiCad shows a diff table before committing. Scan it for:
- Any row showing a footprint in "Schematic" that differs from "PCB" — if that footprint
  is from the custom Part Library, cancel and investigate before proceeding.
- Any SOM connector unit appearing as "Replace" — cancel; units with different footprints
  per unit require manual handling (set footprint field on each unit individually in the
  schematic, then sync).
- Mounting holes appearing in the "delete" list — cancel if they are intentional.

### After sync
1. Run DRC. Known-acceptable: "Unconnected items" only.
2. Verify component count matches schematic BOM.
3. Update `SESSION_CONTEXT.md` with sync outcome and any open items.

---

## PHASE 8 — Component Placement

**Purpose:** Place all components according to signal-integrity, EMC, and power-path constraints. After placement, minimize the board outline to the smallest rectangle containing every courtyard + edge clearance.

**Inputs:** PROJECT_PARAMS (IC roster, proximity rules, net hints, cluster assignments).

**Loops:** Loop A (DRC), Loop B (script), Loop C (switching loop area — first pass from pad centroids; definitive in Phase 10), Loop E (thermal via count).

**Known-acceptable DRC:** "Unconnected items" only.

## Overview: Two Scripts, Two Jobs

Phase 8 placement uses two scripts in sequence. They solve different problems and are not
interchangeable.

| Script | Job | Requires |
|---|---|---|
| `place_components_organized.py` | Coarse zone placement — gets each functional cluster into the correct board region; scores IC rotation candidates against RULES to avoid blocked face assignments | `proximity_rules_config.py` on the Python path |
| `place_by_proximity_rules.py` | Fine satellite placement — places each component exactly where engineering constraints require | `proximity_rules_config.py` on the Python path |

**Run them in this order every time:**
1. Coarse pass (`place_components_organized.py`) — establishes zone layout
2. Fine pass (`place_by_proximity_rules.py`) — tightens each cluster to engineering spec

Skipping the coarse pass is only safe if the major ICs and connectors are already manually
pre-placed in approximately the right board zones (e.g., iterating on a board that was
placed in a prior session). On a bare board, skipping step 1 causes all unlocked anchor ICs
to remain in the origin pile — the fine pass cannot move them and their entire satellite
clusters pile up at the wrong location.

---

## Session Protocol

These rules govern every session that uses the two-script pipeline. Read them before
doing anything else.

### File Management — One Main File, Named Backups

There is exactly one working PCB file. All script runs, all reverts, and all KiCad
edits operate on this single file. No parallel copies are created alongside it.

```
MAIN FILE:    [PROJECT_DIR]/[PROJECT_NAME].kicad_pcb   ← only file Claude touches
BACKUPS DIR:  [PROJECT_DIR]/[PROJECT_NAME]-backups/    ← snapshots only
```

**Snapshot naming convention:**
```
[PROJECT_NAME]_CLEAN_BACKUP_YYYYMMDD.kicad_pcb   — designer's reference layout
[PROJECT_NAME]_SCATTERED_YYYYMMDD.kicad_pcb       — blank-slate test input
```

**The four operations and who initiates each:**

| Step | Who | Action |
|---|---|---|
| Declare clean backup | Designer | Saves desired layout in KiCad, tells Claude |
| Save clean backup | Claude | Copies main file → `CLEAN_BACKUP_<date>.kicad_pcb` in backups dir |
| Scatter and declare | Designer | Scatters in KiCad, saves, tells Claude |
| Save scattered | Claude | Copies main file → `SCATTERED_<date>.kicad_pcb` in backups dir |
| Run scripts | Claude (on request) | Both scripts run in order (Script 1 then Script 2) against main file; result lands in main file |
| Review | Designer | Opens main file in KiCad; discusses results with Claude |
| Revert | Claude (on request) | Copies named backup back over main file |
| Verify revert | Designer | Opens main file in KiCad; confirms positions are correct |

### The Clean Backup

The clean backup snapshot represents the designer's current best understanding of a
good layout. Its purpose is calibration: after every live Script 2 run, compare the
main file against the clean backup in KiCad. Differences reveal whether the scripts
are improving, degrading, or missing placement intent.

**Rule: Claude never passes the clean backup snapshot as input to any script.
It is read-only reference material.**

### The Scattered Version

The scattered snapshot is the blank-slate test input — all movable components
displaced from their intended positions. It proves the script can recover a good
layout from scratch without relying on prior positions.

**Rule: After scripts run, the main file holds the script result. The scattered
snapshot in the backups dir is untouched and remains available for the next
revert-and-rerun iteration.**

### Generalization Requirement

The two scripts (`place_components_organized.py` and `place_by_proximity_rules.py`)
are shared library scripts used across multiple different projects. All project-specific
data — component references, net names, rules tables, face overrides, anchor offsets,
normalization exclusions — lives exclusively in `proximity_rules_config.py`.

**Rule: No project-specific logic is ever added to either script.** A proposed script
change that solves a specific board's layout problem but would be wrong or meaningless
on a different board is not acceptable. When in doubt, ask: "would this change make
sense on a board with completely different components?" If no, it stays in the config.

**Fix suggestions must be framed as script logic improvements, not component patches.**
When a component ends up in the wrong place, the question is always: "what is wrong
with the algorithm's general logic that caused this?" — not "what override can we add
for this specific component?" FACE_OVERRIDES, NO_NORMALIZE_REFS, and
ANCHOR_OFFSET_OVERRIDES are escape hatches for cases the script genuinely cannot
resolve through better logic. The goal is to keep those tables as small as possible
by making the script smarter, not by accumulating per-component workarounds.

### Propose Before Applying

**Rule: Proposed changes to either script are described and discussed before being
written.** Claude will state what the change is, why it is needed, what behavior
it changes, and confirm it is project-neutral — then wait for approval before editing
any script file. Guide updates and `proximity_rules_config.py` updates may be applied
without explicit per-change approval since they do not affect script logic.

### Systematic Iteration

One change at a time. The cycle is:

```
Identify issue (from report or KiCad comparison)
  ↓
Propose fix — describe it, confirm it is project-neutral
  ↓
Approve
  ↓
Apply to script or config
  ↓
Dry-run → review report
  ↓
Live run → compare to clean backup in KiCad
  ↓
Accept result or identify next issue
```

Do not stack multiple script changes in a single iteration. Each change must be
verified against both the proximity report and the clean backup comparison before
the next change is introduced. This makes it possible to attribute any regression
or improvement to a specific change.

---

## Why Each Script Cannot Do the Other's Job

**`place_components_organized.py` cannot do fine placement** because it has no concept of
design intent. It pulls components together by net weight — a bypass cap and its IC end up
adjacent, but not necessarily on the correct face, within the correct distance, or cleared
from board obstacles. It does not know that a bootstrap cap must be within 8 mm of a
switching IC, or that an ESD clamp belongs at the connector rather than at the downstream IC.

**`place_by_proximity_rules.py` cannot do coarse placement from a bare board** because it
treats unlocked anchor ICs (ICs that have satellites but are not satellites of anything
themselves) as permanently fixed at their starting position. On a bare board these ICs are
all stacked at the origin. Their satellites cluster correctly around them — but at the wrong
location. Writing Category 2 rules to place large ICs near modules causes the IC's own
satellite cluster to crowd against a module face rather than spread around the IC naturally.

---

## STEP 1 — Coarse Zone Placement (`place_components_organized.py`)

### When to run

Run the coarse pass exactly once per project: on the very first placement invocation after
F8 (Update PCB from Schematic) has populated the board. On all subsequent sessions the zone
layout persists in the PCB file — skip directly to Step 2.

The presence of `[REPORTS_DIR]/placement_report.txt` is the reliable indicator:

```
State 1 — No footprints in PCB file
  → Run F8 (Update PCB from Schematic) with user, then re-enter here.

State 2 — Footprints present, none locked
  → Go to ANCHOR PLACEMENT below, then re-enter here.

State 3 — Footprints present, some locked, placement_report.txt does not exist
  → First-ever run. Proceed to Step 1A (Cluster Assignment) → Step 1B (Force Pass).

State 4 — Footprints present, some locked, placement_report.txt exists
  → Coarse pass already complete. Skip to STEP 2.
```

### Anchor Placement (States 1 and 2 only)

Before the coarse pass, the user locks the following in the KiCad PCB editor. These become
fixed attractors that the force-directed algorithm clusters everything else around.

**Always lock:**
- Board-to-board connectors (position set by mating PCB mechanical interface)
- Panel/chassis connectors (position set by enclosure cutouts)
- Mounting holes
- SOMs or large modules with fixed pad arrays

**Lock if position is constrained by product requirements:**
- External-facing connectors that must align to enclosure cutouts
- Debug headers required at a specific board edge
- Test points required at specific locations

**Do not lock:**
- ICs, passives, decoupling caps, TVS diodes — the script places these
- Connectors whose edge placement is flexible

**Note — anchors accumulate across passes.** On later refine passes Claude may instruct the
user to lock additional ICs (typically the primary IC of a misplaced cluster) to pull that
cluster into the correct region. These locks persist on all subsequent passes.

### Step 1A — Cluster Assignment (State 3 only, runs once)

Before the first force pass, Claude groups unlocked components into functional clusters and
writes `[PROJECT_DIR]/clusters.json`. The script uses this file to seed each group in the
correct board region before force-directed begins.

**Dump the net list:**
```
"C:/Program Files/KiCad/10.0/bin/python.exe" \
  "[SCRIPTS_DIR_SHARED]/place_components_organized.py" \
  --pcb "[PCB_FILE]" \
  --dump-nets -
```

This prints `ref, fixed, net_count, signal_nets` for every component. `fixed=1` rows are
locked anchors. `signal_nets` omits high-fanout rails (GND, power) — only point-to-point
signal nets appear.

**Assign clusters:** Claude reads the dump and groups every unlocked component (fixed=0) by
functional role. Every unlocked non-connector component must appear in exactly one cluster.
Use net connectivity from `signal_nets` to identify the parent IC (the non-passive that
shares the most signal nets with the component). Use the locked anchor nearest to that
parent IC to determine the board region.

Typical cluster patterns:

| Cluster | Typical members | Region guidance |
|---|---|---|
| HS_BRIDGE | Primary HS bridge IC; its LDOs, crystal, SPI flash; bypass caps; config resistors | Side of board adjacent to HS connector pair |
| HS_SIGNAL | AC coupling caps and ESD diodes on HS signal lines | Between HS bridge and output connector |
| USB_PD | USB-C PD controller; bypass caps; I2C pull-ups; VBUS gate-drive FETs | Near USB-C input connector |
| POWER | Switching converter support: gate-drive FETs, TVS diodes, input/output caps, compensation network, voltage-sense dividers, auxiliary LDOs | Near power input connector |
| DISP | Display/touch I2C pull-ups and reset resistors | Near display connector |

**Write `[PROJECT_DIR]/clusters.json`:**
```json
{
  "forced_rotations": {},
  "non_passive_overrides": [],
  "passive_side_overrides": {},
  "CLUSTER_NAME": {
    "anchor": "PRIMARY_IC_REF",
    "region": "mid-right",
    "members": ["REF1", "REF2", "..."]
  }
}
```

**Top-level control fields:**

| Field | Type | Purpose |
|---|---|---|
| `forced_rotations` | `{ ref: degrees }` | Lock a specific IC to a fixed rotation. Use when the algorithm consistently settles an IC at the wrong angle. |
| `non_passive_overrides` | `[ "REF", ... ]` | Declare D-prefix components (e.g. redriver, mux, ESD array ICs) as non-passives so they can serve as parent ICs. Without this the script treats any D-prefix ref as a diode and it can never be a parent. |
| `passive_side_overrides` | `{ ref: "side" }` | Force a passive to a specific face of its parent IC. Valid sides: `above`, `below`, `left`, `right`. Use when the geometry-computed side is wrong — most often when the parent IC settles at a rotation that maps its pads to an unexpected global face. |

The `anchor` field is informational only. The `region` field controls seeding in the 3×3
board grid. Regions: `top-left`, `top-center`, `top-right`, `mid-left`, `mid-center`,
`mid-right`, `bottom-left`, `bottom-center`, `bottom-right`.

### Step 1B — Determine `DIFF_PAIR_MIN_SEP`

Before every coarse or refine invocation, determine the diff-pair separation value. This
is not a fixed number — Claude adjusts it based on violation feedback from each pass.

**Initial value (first pass):** Use this lookup table, keyed to the interfaces present.
Take the most conservative value across all diff pair interfaces present.

| Interface | Length-match tolerance | Starting `DIFF_PAIR_MIN_SEP` |
|---|---|---|
| USB 2.0 (FS/HS) | ±10 mm | 5.0 mm |
| USB 3.x SuperSpeed | ±3 mm | 6.0 mm |
| PCIe Gen 1/2 | ±5 mm | 6.0 mm |
| PCIe Gen 3+ | ±3 mm | 7.0 mm |
| MIPI CSI-2 / DSI | ±0.5 mm | 7.0 mm |
| LVDS (generic) | ±2 mm | 6.0 mm |
| Gigabit Ethernet | ±5 mm | 5.0 mm |
| DDR3/DDR4 (addr/cmd) | ±25 ps (~4 mm) | 8.0 mm |
| HDMI / DisplayPort | ±0.3 mm | 8.0 mm |
| No differential pairs | — | 0.0 mm (disables enforcement) |

Record the initial value in PROJECT_PARAMS as `[DIFF_PAIR_MIN_SEP]`.

### Script 1 RULES Awareness

`place_components_organized.py` imports `RULES` from `proximity_rules_config.py` and uses
it during `rotation_pass_ics()` — the pass that chooses each IC's final orientation after
force-directed settling. For each candidate rotation (0°, 90°, 180°, 270°), the script:

1. Computes a **net-pull score**: dot product of the rotated net-pad centroid with the
   pull vector toward the IC's net partners. This is the primary score.
2. Computes a **face-viability penalty**: for each satellite the IC would receive at this
   rotation (identified by its rule's `ref_b == ic.ref`), the algorithm infers the natural
   face the satellite would land on, then checks whether any locked component within
   `max_dist` mm blocks that face. Blocked faces incur a penalty (−10 for clearance < 5 mm,
   proportional up to 10 mm).

Final score = `net_pull + viability_penalty`. The rotation with the highest combined score
is chosen. This prevents Script 1 from settling an IC at a rotation that Script 2 cannot
work with — for example, rotating a buck converter so that its bootstrap-cap face is
already occupied by a locked mounting hole.

Script 1 does not otherwise read or act on `RULES`. The face viability check is one-
directional: Script 1 looks at locked obstacles only, not at other unlocked satellites.

**Adjustment on subsequent passes:** After each pass, read the `DIFFERENTIAL PAIR
SEPARATION CHECK` section of `placement_report.txt` and classify each remaining violation:

- **True endpoint violation** — both components are ICs or connectors. These represent real
  routing paths that need meander budget. Do NOT reduce `DIFF_PAIR_MIN_SEP`. The components
  need physical repositioning.
- **Adjacent passive violation** — one component is an IC/connector, the other is a passive.
  These are series coupling caps or ESD diodes intentionally adjacent to their parent IC.
  Reduce `DIFF_PAIR_MIN_SEP` by 0.5 mm per pass until the violation clears. Never go below
  4.0 mm (USB 2.0 lower bound).
- **Locked-locked violation** — both components are locked. The script cannot resolve these.
  Do not count toward RESULT. Flag to user only if the gap is negative (actual overlap).

### Step 1C — Script Invocation

**Force pass (State 3 — first run only):**
```
"C:/Program Files/KiCad/10.0/bin/python.exe" \
  "[SCRIPTS_DIR_SHARED]/place_components_organized.py" \
  --pcb "[PCB_FILE]" \
  --report "[REPORTS_DIR]/placement_report.txt" \
  --dry-run \
  --clusters "[PROJECT_DIR]/clusters.json" \
  --diff-pair-min-sep [DIFF_PAIR_MIN_SEP] \
  --board-margin 3.0 \
  --iterations 300 \
  --cycles 6
```

**Refine pass (all passes after the first):**
```
"C:/Program Files/KiCad/10.0/bin/python.exe" \
  "[SCRIPTS_DIR_SHARED]/place_components_organized.py" \
  --pcb "[PCB_FILE]" \
  --report "[REPORTS_DIR]/placement_report.txt" \
  --dry-run \
  --use-current-positions \
  --refine-only \
  --diff-pair-min-sep [DIFF_PAIR_MIN_SEP] \
  --board-margin 3.0 \
  --cycles 6
```

The force pass runs exactly once. Every pass after that is a refine pass. The refine pass
skips force-directed entirely and only runs overlap resolution, rotation, and diff-pair
separation — it never undoes a manually adjusted anchor position.

**Note on `--courtyard-gap`:** Omit in standard runs. The script uses 0.025 mm for
passives, 0.15 mm for ICs, and 0.50 mm for connectors automatically. Only add
`--courtyard-gap X.X` to override the IC gap on very dense boards.

After dry-run, confirm: (1) locked component count matches user placements; (2) detected
diff pairs match expected interfaces; (3) convergence reached before cycle limit (if not,
increase `--cycles` by 2 and retry).

### Step 1D — RESULT Assessment

Read the `RESULT:` line in the report:

```
RESULT: PASS  cyd=0  dp=0
  → Run live (replace --dry-run with --live). Step 1 complete — proceed to STEP 2.

RESULT: FAIL  cyd=N  dp=0  (courtyard violations only)
  → Go to cluster assessment below.

RESULT: FAIL  cyd=0  dp=N  (diff-pair only)
  → Classify violations per Step 1B adjustment rules.
    All adjacent-passive: reduce --diff-pair-min-sep by 0.5mm, re-run dry-run.
    Any true endpoints: go to cluster assessment below.

RESULT: FAIL  cyd=N  dp=N  (both)
  → Go to cluster assessment. Address courtyard violations first.

cyd_locked / dp_locked > 0
  → User anchor placement issue — do not count toward RESULT.
    Negative gap (actual overlap): flag to user; anchors must be repositioned.
```

### Step 1E — Cluster Assessment and Anchor Adjustment

When violations remain, Claude reads `placement_report.txt` and evaluates the cluster
arrangement:

- **HS signal chain ICs** should be grouped together, close to their input and output
  connectors, away from power ICs.
- **Power ICs** should be near their input connector and thermally accessible.
- **MCU and logic ICs** should be between the HS zone and the debug/interface connector.
- **Diff-pair components** should have clear routing corridors between their endpoints.

**If a cluster is correctly arranged** but has local density violations: Claude instructs
the user to lock 1–2 specific ICs at slightly more separated positions, then re-runs as a
refine pass.

**If a cluster is in the wrong board region:** Claude identifies the primary IC of the
misplaced cluster, determines a target position, and instructs the user to:
1. Open `[PCB_FILE]` in KiCad
2. Move that IC to the target position
3. Lock it and save

Then re-enter at the entry gate (State 4 — refine pass). Anchor adjustment is additive:
each pass may add locked ICs; previously locked components are never unlocked.

**After 3 cluster assessment cycles with unresolvable violations:** surface the full
violation table to the user and offer: (a) accept remaining violations and proceed, (b)
increase board size in PROJECT_PARAMS and restart at State 3, or (c) continue adjustment.

### Step 1 Exit Condition

All functional clusters are in their correct board regions. Exact positions of individual
satellites within each cluster do not need to be correct — Step 2 handles that.

---

## STEP 2 — Fine Satellite Placement (`place_by_proximity_rules.py`)

### When to run

Run on every session after Step 1 is complete. This is the iterative loop: scatter → run
→ review in KiCad → revert to scattered state → adjust rules or algorithm → run again.

### Pre-conditions

The RULES table must be populated. See the Rules Table section below.
The PCB must be the scattered test version, not the clean backup. The clean backup
represents the intended layout and is used for post-run comparison only. Never run the
script against the clean backup.

### Invocation

Dry-run first:
```
"C:/Program Files/KiCad/10.0/bin/python.exe" \
  "[SCRIPTS_DIR_SHARED]/place_by_proximity_rules.py" \
  --pcb "[PCB_FILE]" \
  --output "[PCB_FILE]"
```

`--pcb` sets the input file; `--output` sets where the result is saved. Use the same path
for both so the result overwrites the working file in place (KiCad prompts to reload).

When all rules pass (or remaining failures are accepted), run live:
```
"C:/Program Files/KiCad/10.0/bin/python.exe" \
  "[SCRIPTS_DIR_SHARED]/place_by_proximity_rules.py" \
  --pcb "[PCB_FILE]" \
  --output "[PCB_FILE]" \
  --live
```

Before each live run the script automatically saves a timestamped backup of the current
PCB to `[BACKUPS_DIR]`. Any live result can be reverted by restoring this backup.

### Step 2 Exit Condition

Zero FAIL entries in the proximity report. The courtyard overlap audit at the end of the
report shows zero overlapping pairs. Review the placed PCB visually in KiCad before
proceeding to Loop A/B/C/E checks.

---

## Script 2 Algorithm Internals

Understanding what happens inside each pass helps diagnose why a component ended up where
it did and what the diagnostic tags in the output mean.

### Multi-Pass Convergence

Script 2 runs multiple passes until all rules pass or no new components freeze in a pass.
Each pass:

1. **Compute frozen set** — components whose rules all pass, whose rotation is correct,
   and whose courtyard is clear of overlaps are marked frozen. Frozen components are not
   moved this pass.
2. **Unfreeze as needed** — three mechanisms (see below) can pull components back out of
   the frozen set.
3. **Build face groups** — assign each active (non-frozen) satellite to a face of its
   anchor using net-hint pad geometry. Run the rotation pre-pass.
4. **Place face groups** — place each group atomically in priority order.
5. **Normalize** — snap passives in each group to a shared column or row.
6. **Global overlap pass** — push any remaining courtyard overlaps clear.

If zero new components froze this pass, the algorithm converges and stops.

### Freeze / Unfreeze Mechanisms

Three conditions pull a component back out of the frozen set. All three print a diagnostic
tag to stdout.

**`[overlap-unfreeze]`** — A frozen component is overlapping another component's courtyard.
Even if all its proximity rules pass, an overlapping component cannot stay frozen because
the overlap itself is a placement failure. It re-enters the active set so face-group
placement can find a clear position.

**`[satellite-unfreeze]`** — A frozen anchor IC has a direct satellite whose proximity
rule is currently failing (distance > max_dist). A frozen anchor at its passing distance
can still block its own satellite from reaching it (the anchor occupies the approach path).
Unfreezing the anchor allows it to shift slightly, making room for the satellite.

**`[block-unfreeze]`** — A satellite is failing its proximity rule (distance > max_dist),
and a frozen component is physically adjacent to that satellite (within 5 mm). The frozen
component is occupying space the satellite needs to reach its anchor. Unfreezing it lets it
relocate in the next pass so the satellite can close the gap. This handles the indirect
blocking case — where no direct rule connects the blocker to the satellite, but the blocker
is physically in the way.

### Satellite-Conflict-Aware Rotation Selection

Before building face groups each pass, Script 2 runs a rotation pre-pass over every
movable anchor. For each movable anchor whose current rotation has at least one satellite
face blocked by a locked component, all four rotations (0°, 90°, 180°, 270°) are tried.
The rotation that minimises the number of satellite faces blocked by locked components is
selected.

**`[rot-select]`** — Printed when a movable anchor's rotation is changed. Format:
`[rot-select] REF: OLD° → NEW° (blocked satellite faces: N → M)`.

This is not a hard override — it is a per-pass decision. The rotation chosen is the one
best suited to the current board state (which satellites are active, which locked components
are nearby). A different rotation may be selected in a later pass if the active set changes.

**What "blocked" means:** A locked component is considered to block a satellite face if its
centroid is within 12 mm ahead of the anchor along the face direction and within 5 mm of
the face axis (cross-track). A satellite assigned to that face would land in the blocker's
territory and be unable to reach the required proximity distance.

**Why this matters:** Anchor rotation determines which physical face each net's pads are on,
which in turn determines where all satellites for that anchor go. A rotation that sends one
satellite into a locked component forces every other satellite to either crowd a different
face or overlap the blocker. Choosing the rotation that avoids blocked faces gives the entire
satellite cluster a clear path.

### Perpendicular Axis Redirect

**`[perp-redirect]`** — When a satellite's net-hint face would place it on the same axis as
its anchor's own anchor assignment, the satellite is redirected to the best face on the
perpendicular axis instead.

Example: IC1 is placed at J_PWR_IN1/left (IC1's assigned axis is left-right). If Q2's rule
sends it toward IC1/left (same axis), Q2 would stack directly between IC1 and J_PWR_IN1 —
off the board or into J_PWR_IN1's courtyard. The perp-redirect instead sends Q2 to IC1/up
or IC1/down (the perpendicular axis), where there is board space to place it.

The redirect uses the perpendicular pad centroid to choose which of the two perpendicular
faces (up vs down, or left vs right) is more appropriate based on net geometry.

### Option A — Clearance-First Face Selection for Large IC Satellites

For `power_mgmt`-typed rules where the satellite is itself a large IC (appears as an anchor
for other satellites) and the net-hint DB lookup found no result, Script 2 overrides the
geometry fallback with a clearance-first face score.

All four faces of the anchor are scored by the distance from the anchor centroid to the
board edge in that direction, with a penalty (−100 mm) for each locked component within
12 mm along the face direction and 5 mm cross-track. The face with the highest score is
chosen.

This prevents net-hint voting from sending a large IC to the same crowded face as adjacent
passives — a situation that causes the IC's own satellite cluster to be forced off the board
or into connector courtyards.

Option A does not fire when the DB lookup succeeded (i.e., when the anchor has a clear
net-dominant face from its pad geometry). In that case the DB result is authoritative.

---

## Shared Configuration File — `proximity_rules_config.py`

Both scripts import a single shared configuration file rather than each maintaining their
own copy of the RULES table. Place this file in the same directory as the two placement
scripts (i.e., `[SCRIPTS_DIR_SHARED]/proximity_rules_config.py`).

The file defines four objects:

| Symbol | Used by | Purpose |
|---|---|---|
| `POWER_NET_EXACT` | Script 2 | Set of net names treated as power rails — drives 180° rotation for sym=180 passives |
| `is_power_net(net)` | Script 2 | Helper: returns True if net is in POWER_NET_EXACT (case-insensitive) |
| `RULE_TYPE_PRIORITY` | Script 2 | Dict mapping rule type strings to placement priority integers (lower = placed first) |
| `RULES` | Both scripts | List of proximity rules — single source of truth |

**To adapt for a new project:** copy the shared config from the previous project and
replace `POWER_NET_EXACT` and `RULES` with project-specific entries. `RULE_TYPE_PRIORITY`
is project-neutral and rarely needs editing.

**Import chain:** Script 1 imports `RULES` only (for IC rotation scoring). Script 2 imports
all four symbols. Neither script defines these tables inline.

---

## The Rules Table

The RULES table is the design intent layer for Step 2 — and informs Step 1's IC rotation
scoring. Every rule encodes an engineering reason: EMC, power path integrity, signal
integrity, or thermal. Rules are written in Phase 2 and stored in `PROXIMITY_RULES_TABLE`
in PROJECT_PARAMS, then written into `proximity_rules_config.py` at the start of Phase 8.

### Category 1 — Passive/satellite components near an IC or connector

Every passive that directly serves an IC or connector gets a rule. Use the threshold table:

| Type | Max dist (mm) | Reason |
|---|---|---|
| `decoupling_bypass` | 8 | Short switching current loop |
| `decoupling_bulk` | 15 | Same power zone sufficient |
| `esd_clamp` | 8 | Must intercept before signal travels on board |
| `crystal` | 12 | Oscillator stub traces radiate |
| `bootstrap` | 8 | Must be directly adjacent to switching IC |
| `snubber` | 6 | Must be at the switch node |
| `ac_coupling` | 10 | Inline on signal path at source output |
| `filter` | 12 | EMC/power filter at power entry point |
| `pullup_pulldown` | 20 | Needs to be on the same bus segment |
| `power_mgmt` | 13–25 | IC placed near its primary connector or module; loose distance for satellite spread |

**`esd_clamp` scope:** TVS diodes, ESD arrays, clamping diodes at board entry points.
Never use `filter` for a TVS — `filter` is for passive filter networks (inductors, RC
networks). An ESD clamp that is incorrectly typed as `filter` will be placed with lower
priority than `esd_clamp` requires and may not reach its connector within the rule
distance.

Common derivation errors to check:
- **AC coupling caps:** owner is the source IC whose output the cap is inline with — not
  the destination IC or connector.
- **Pull-up/pull-down resistors:** owner is the IC whose pin the resistor connects to —
  not the power rail it pulls toward.
- **ESD clamps:** owner is the connector where the signal enters the board — not the IC
  downstream. Rule type must be `esd_clamp`, not `filter`.
- **Snubbers:** owner is the switching IC at the switch node — not the inductor or output cap.
- **TVS diodes:** same as ESD clamps — owner is the board-entry connector; rule type is
  `esd_clamp`.

### Category 2 — IC components near their associated connector or module

An IC that only ever appears as a destination in Category 1 rules (receives satellites,
never a satellite itself) is treated as permanently fixed wherever it starts. Write a
Category 2 rule for every such IC.

To identify the anchor for an IC:
1. In the schematic, trace the IC's primary power input net to its source connector.
2. Trace the IC's primary signal output net to its load connector or module.
3. Choose whichever endpoint is physically closest to the IC in the intended layout.
4. Ask Claude Code to read the schematic and confirm: *"For each IC that appears only as
   a rule destination, what connector or module is it most tightly coupled to by net?"*

Category 2 rules use `power_mgmt` type and a max_dist of 15–25 mm (looser than passives
because the IC needs space around it for its own satellite cluster).

**Constraint:** Do not write a Category 2 rule that places a large IC — one with many
bypass caps, inductors, or feedback networks — directly on the face of a large module.
The IC's satellite cluster will have no room to spread and will overlap the module
courtyard. Use a loose max_dist so the IC settles into the zone near the anchor without
being forced onto a specific module face.

### Rule coverage checklist

Before running Step 2, verify:
- Every unlocked non-connector component appears as ref_a in at least one rule.
- Every locked anchor appears as ref_b in at least one rule.
- Every IC that only appears as ref_b in Category 1 rules has a Category 2 rule.
- No large-cluster IC is targeted at a module face with insufficient space.

### Rule format

The rules live in `proximity_rules_config.py`, not inside either script. At the start of
Phase 8 copy the full RULES list (derived in Phase 2) into that file.

```python
# proximity_rules_config.py — project-specific section

RULES = [
    # Category 2 first — IC anchor placement (establishes zone before satellites follow)
    (ic_ref,      anchor_ref,  max_dist_mm, "power_mgmt",    "one-sentence reason", net_hint),

    # Category 1 — passive/satellite proximity, grouped by anchor IC
    (passive_ref, ic_ref,      max_dist_mm, type_from_table, "one-sentence reason", net_hint),
]
```

`net_hint`: exact KiCad net name of the most critical shared net. Use `None` when no
single net is the critical path — the script uses minimum distance across all shared nets.

---

## Additional Configuration Tables

Three user-configurable tables and one DB-derived set in `place_by_proximity_rules.py`
control edge cases that the algorithm cannot infer from net topology alone. The three tables
are empty by default — add entries only when the script produces a wrong result and the root
cause has been identified. The DB-derived set is populated automatically at runtime.

---

### FACE_OVERRIDES

**What it is:** An explicit face assignment for a specific satellite→anchor pair, overriding
the face inferred from pad geometry. A face override says "ref_a always goes on the _side_
face of ref_b, regardless of what the pin positions imply."

```python
FACE_OVERRIDES = {
    ("REF_A", "REF_B"): "left" | "right" | "up" | "down",
}
```

**When to add one:**

- The script places a component on the wrong face because a locked obstacle (connector,
  module, board edge) blocks the inferred face.
- The net hint pad on the anchor is at the centroid, so the algorithm has no directional
  information and defaults to a wrong face.
- The intended face is clear from the schematic or board topology but the net geometry
  points the wrong way (e.g., a power net whose pads are symmetrically placed on the IC).

**How to derive — systematic process:**

1. Run the script with no overrides. Review placed positions in KiCad.
2. For each component on the wrong face: identify which face the component should be on
   from the designer's intent (clean backup positions or schematic topology).
3. Check what blocks each face of the anchor: locked connectors, module edges, already-
   placed large satellites. Confirm the intended face is physically reachable.
4. Add the override with the correct face string.

**Face direction convention:**
- `"left"` = component goes to the left of its anchor (anchor's -X side in world coords when anchor is at 0°)
- `"right"` = component goes to the right of its anchor
- `"up"` = component goes above its anchor (anchor's -Y side; KiCad Y increases downward)
- `"down"` = component goes below its anchor

**Common derivation errors:**
- The face override describes the face of the anchor the satellite sits on — not the
  direction the satellite faces. `("C_BOOT1", "U1"): "down"` means C_BOOT1 goes on U1's
  bottom face, not that C_BOOT1 faces downward.
- When a locked satellite constrains an IC anchor's position (e.g., L1 locks U1 to its
  right side), the FACE_OVERRIDE for that locked satellite relative to the IC anchor
  also controls the direction used by `place_root_anchors` when computing the anchor's
  starting position. Verify both the satellite placement and the anchor offset are correct
  after adding the override.
- **Col-norm → obstacle → global-overlap pushback:** The face group places a satellite past
  an obstacle (leaving enough room). Col-norm then snaps the satellite back to 0.15mm from
  the anchor edge, landing it on top of the obstacle. The global overlap pass then pushes the
  satellite away from the obstacle — but in the direction of the anchor, creating an overlap
  with the anchor. Symptom: a courtyard overlap between a satellite and its anchor that is
  NOT present after col-norm, only after `global_no_overlap_pass`. Fix: redirect the satellite
  to a different face of its anchor (one not blocked by the obstacle) using FACE_OVERRIDE.
  Example: `("C_COMP1","R_COMP1"): "down"` — places the COMP compensation cap below R_COMP1
  rather than between R_COMP1 and U1 (gap only 1.354mm, insufficient for the cap).
  Example: `("C_LT3V3_BULK1","U5"): "right"` — places the LT_3V3 bulk cap on U5's output
  side rather than below U5, where U6 leaves only 0.570mm of clearance.

---

### ANCHOR_OFFSET_OVERRIDES

**What it is:** A per-anchor override of the default `ANCHOR_OFFSET_FACTOR` (0.7). The
factor controls how far from a locked satellite the script places an unlocked anchor IC:
`offset = factor × mean_max_dist`. The default places the anchor at 70% of its mean rule
max distance from the satellite's centroid.

```python
ANCHOR_OFFSET_OVERRIDES = {
    "ANCHOR_REF": factor,   # float, replaces ANCHOR_OFFSET_FACTOR for this anchor only
}
```

**When to add one:**

The default 0.7 factor places the anchor outside a valid corridor — a locked obstacle
(connector, module, board edge) on the far side forces the anchor back via
`global_no_overlap_pass`, causing the anchor to overlap its own satellites on the rebound.
Symptoms: a courtyard overlap between an anchor IC and its satellites that appears only after
the global overlap pass runs, not immediately after face-group placement.

**How to compute the override factor — step by step:**

1. Identify the locked obstacles on both sides of the anchor along the axis of motion.
2. Measure the obstacle's near courtyard edge position (in mm from the board origin).
3. Compute the anchor's half-width along that axis from its courtyard data.
4. Compute the valid centroid range: `[obstacle_near + COURTYARD_GAP_MM + anchor_half,
   obstacle_far - COURTYARD_GAP_MM - anchor_half]`.
5. Choose a target centroid within this range, centered or biased away from the tighter side.
6. Express the target as a distance from the locked satellite centroid.
7. Compute `factor = target_distance / mean_max_dist`, where `mean_max_dist` is the average
   of the `max_dist` values in all rules where this anchor is ref_b.
8. Verify: `factor × mean_max_dist` lands the anchor inside the valid range computed in step 4.

`COURTYARD_GAP_MM` is 0.15 mm. Use the polygon-accurate courtyard bounds from
`component_geometry.json`, not footprint pad extents.

**Note:** This override is inherently board-geometry-specific. If the board layout changes
(obstacle moved, anchor footprint changed), recompute the factor. An override that was
correct for one board version may be wrong after a layout revision.

---

### NO_NORMALIZE_REFS

**What it is:** A set of component refs excluded from the column/row normalization pass.
Normalization snaps each satellite to the column or row defined by nearby group members
— this is correct behavior in most cases, but two failure modes require exemption.

```python
NO_NORMALIZE_REFS = {"REF_A", "REF_B"}
```

**Failure mode 1 — Oscillation:** The anchor_clearance_pass pushes a component away from
a locked obstacle (e.g., a connector). The normalization pass then snaps it back into the
column, which re-creates the clearance violation. On the next iteration the anchor_clearance_
pass pushes it away again — the component oscillates and never settles.

Symptom: a component's position changes back and forth between iterations with no net
improvement. The oscillating ref will show alternating deltas in consecutive run reports.

Resolution: add the ref to NO_NORMALIZE_REFS. The anchor_clearance_pass places it correctly;
normalization is suppressed so the clearance position is preserved.

**Failure mode 2 — Precision gap:** The normalization formula for anchor_edge uses a slightly
different precision model than the face-group placement formula. For components placed right at
the minimum courtyard clearance, the normalization formula may compute the anchor edge as
~0.05 mm short of its actual position, leaving only ~0.10 mm gap instead of the required
0.15 mm. Face-group placement is correct; normalization breaks it.

Symptom: the courtyard audit reports a tiny overlap (~0.05 mm) for a component that was clean
immediately after face-group placement. The same component passes with the oscillation symptom
absent — it does not bounce back and forth, it simply lands slightly wrong after normalization.

Resolution: add the ref to NO_NORMALIZE_REFS. The face-group position is preserved.

**How to identify which failure mode applies:**

Enable debug output for the suspect ref by adding it to `_DBG_REFS`, run the script, and
compare the component's position at each stage: after face-group, after normalization, after
anchor_clearance_pass. If the position is correct after face-group and wrong after
normalization → Failure mode 2. If it alternates between two positions across passes →
Failure mode 1.

**Caution:** NO_NORMALIZE_REFS entries are workarounds for edge cases in the normalization
algorithm. Use them only when the root cause has been confirmed by debug output. A ref added
without diagnosis may mask a rule error or a missing FACE_OVERRIDE.

---

### NO_ROTATE_IN_OVERLAP (DB-derived — do not edit manually)

**What it is:** A set of refs whose rotation the overlap resolver and face fallback are
not permitted to change. It is populated automatically at the start of each run from the
geometry DB:

```python
NO_ROTATE_IN_OVERLAP = {
    ref for ref, entry in geom_db.items()
    if entry.get("rotation_symmetry") == "none"
}
```

**Why it exists:** The overlap resolver (intra-group rotation trials) and the face fallback
(adjacent-face placement) both try alternative rotations (+90°, +180°, +270°) to find a
position that clears obstacles. For components with `rotation_symmetry="none"`, all four
rotations are physically distinct — the preferred rotation from the DB is authoritative and
must be preserved. Allowing the resolver to rotate these components produces wrong board
orientation even when all rules pass.

**How it is derived:** The `rotation_symmetry` field in `component_geometry.json` is set
during Phase 4 (geometry extraction) based on whether the component's courtyard and pad
pattern repeats at 90°, 180°, or not at all:
- `"none"` — ICs, polarized components, connectors, transistors: rotation matters.
- `"180"` — unpolarized 2-pad passives (resistors, non-polarized caps): 0° and 180° are identical.
- `"90"` — 4-fold symmetric components: all four rotations are identical.

Components with `"none"` are added to NO_ROTATE_IN_OVERLAP automatically. Components with
`"180"` or `"90"` are freely rotatable by the resolver.

**What it guards:** Both the intra-group rotation trial loop (lines ~1181–1210) and the
face fallback trial loop (lines ~1254–1288). For refs in this set, only the current rotation
(`p['rot']`) is tried — no alternative rotations are attempted.

**What to do if a component has the wrong rotation in the output:** First confirm
`rotation_symmetry` in `component_geometry.json` is set to `"none"`. If it is, the
component is already in NO_ROTATE_IN_OVERLAP and the resolver will not change its rotation
— the issue is in `db_rotation_for_face()`.

For `rotation_symmetry = "none"` components, `db_rotation_for_face()` scores all four
candidate rotations (0°, 90°, 180°, 270°) by pad alignment toward the anchor face using the
`net_hint` pad in the geometry DB. The rotation that best aligns the hint pad toward the
anchor wins. `preferred_rotation` from the DB is NOT used as a direct return for sym=none
components (it was, historically, but that caused wrong rotations when Script 1 assigned a
component to one anchor face and Script 2 moved it to a different anchor face). If rotation
is still wrong, check:

1. `net_hint` in the rule — wrong net means the scoring uses the wrong pad.
2. `component_geometry.json` — verify the `pads` array contains an entry with the correct
   `net` and `local_xy` coordinates.
3. `face_side` passed to `db_rotation_for_face` — if the satellite is on the wrong face,
   the toward-vector points the wrong direction.

Do not add manual exceptions to NO_ROTATE_IN_OVERLAP.

---

## Failure Triage for Step 2

For each FAIL in the proximity report:

**Scatter artifact** — anchor IC was scattered far from its connector. Verify a Category 2
rule exists for that anchor IC. If yes but still failing, the scatter placed the anchor
geometrically too far — re-scatter and re-run.

**Geometric impossibility** — a module, locked connector, or board edge blocks the
satellite from reaching the required distance. Check the intended layout (clean backup
positions). If the clean backup also shows this pair beyond the rule distance, relax
`max_dist` to match the achievable distance in the intended layout.

**Satellite cluster crowding** — a large-cluster IC placed near a module has no room for
its satellites. Remove or loosen the Category 2 rule and rely on Step 1 to establish the
zone instead.

**Accepted known limitation** — two rules are geometrically incompatible for this board.
Document the failure and accept it.

---

## Using Claude Code to Author the Rules Table

Claude Code can read the `.kicad_sch` file directly and write the entire RULES table
without manual input. The workflow:

1. At Phase 2 Step B2, ask Claude Code: *"Read the schematic. For each passive, identify
   which IC or connector it directly serves and why. For each IC, identify which connector
   or module it is most tightly coupled to by net. Write the complete PROXIMITY_RULES_TABLE
   following the derivation error checklist."*
2. Claude Code reads the S-expression schematic, traces net connections, and returns the
   complete table with types and net hints.
3. Review for any ambiguous assignments and resolve manually.
4. Store in PROJECT_PARAMS. At the start of Phase 8, write the RULES list (and
   POWER_NET_EXACT) into `[SCRIPTS_DIR_SHARED]/proximity_rules_config.py`. Both placement
   scripts import from there — do not copy RULES into either script directly.

The rules table never needs to be re-derived from scratch between sessions. When a
component is added or renamed, Claude Code reads the updated schematic and patches the
affected rules in `proximity_rules_config.py` only.

---

## Complete Phase 8 Placement Flow

```
[F8: Update PCB from Schematic — all components at origin]
        ↓
[User places and locks anchors in KiCad: connectors, SOMs, mounting holes]
        ↓
[placement_report.txt exists in REPORTS_DIR?]
        │
       NO ──→ STEP 1: Coarse pass (place_components_organized.py)
        │       Step 1A: Claude dumps nets → assigns clusters → writes clusters.json
        │       Step 1B: Determine DIFF_PAIR_MIN_SEP from interface lookup table
        │       Step 1C: Force pass (dry-run, --clusters, no --use-current-positions)
        │       Step 1D: RESULT PASS? → run live → placement_report.txt created
        │                RESULT FAIL?
        │                  cyd only → Step 1E cluster assessment
        │                  dp only  → classify violations → adjust DIFF_PAIR_MIN_SEP
        │                  both     → Step 1E first, then dp
        │       Step 1E: Wrong region? → user locks primary IC at target → refine pass
        │                Density?     → user locks 1-2 ICs at wider spacing → refine pass
        │       Repeat refine passes until PASS → run live
        │
       YES ──→ STEP 2: Fine pass (place_by_proximity_rules.py)
                Dry-run → review FAIL list
                Triage each FAIL:
                  Scatter artifact    → add/fix Category 2 rule → re-run
                  Geometric limit     → relax max_dist → re-run
                  Cluster crowding    → remove Category 2 rule → re-run
                  Known limitation    → accept → document
                All resolved or accepted? → run live
                ↓
        [Zero FAIL + zero courtyard overlaps in audit]
                ↓
        [Visual review in KiCad]
                ↓
        [Loop A (DRC) → Loop B/C/E checks → proceed]
```

Each Step 2 iteration follows the scatter/verify cycle: revert PCB to scattered state →
run live → review in KiCad → revert to scattered state → adjust → run again. The
scattered state is always preserved as a timestamped backup before each live run.
```

**Inter-lane skew — solve at placement, not routing.** Inter-lane skew (the length difference between lanes in a multi-lane interface, e.g. DA0 vs DA1 vs DA2 vs DA3) is determined almost entirely by the physical distance between IC pads. It cannot be fixed with meanders in Phase 10d because the amounts required are too large and routed areas too congested.

**Correct order of operations for high-speed routing:**
1. Place ICs so inter-lane skew is within spec BEFORE routing (this phase)
2. Route all traces (Phases 10, 10a)
3. Add intra-pair meanders to match P vs N within each lane (Phase 10d)

Skipping step 1 and going straight to step 3 will match P vs N correctly but still violate inter-lane spec — and fixing it afterward requires moving ICs and redoing all routing and intra-pair tuning from scratch.

**Inter-lane skew limits (from PROJECT_PARAMS HS inventory):**
- DP 1.4 lanes: <= 0.45 mm (very tight — pad positions must be nearly equal path length)
- USB SS TX/RX lanes: <= 5.08 mm
- HDMI 2.1 TMDS lanes: <= 10 mm

**How to check at placement time (before routing):** Measure the straight-line distance from the source IC pad to the destination IC pad for each lane. The difference between longest and shortest must be within the inter-lane limit. Write `[SCRIPTS_DIR]/analyze_interlane_skew_placement.py` to compute this from pad positions if needed.

**Decoupling proximity verification** — after placement loops are clean, write
`[SCRIPTS_DIR]/check_decoupling_proximity.py`. This is a **design-intent check**: each
bypass and bulk cap is explicitly paired with the IC it was designed to decouple in a
`DECOUPLING_RULES` table inside the script. The check measures the connecting-pad to
connecting-pad distance (minimum distance between pads sharing a net) from each cap to
its assigned IC, and reports PASS/FAIL per cap. This approach means a FAIL always
identifies the specific cap that is misplaced relative to its intended IC — not merely
that some cap on the net is far away.

`DECOUPLING_RULES` format: `(cap_ref, ic_ref, reason)` — copy all decoupling and bypass
cap entries from PROXIMITY_RULES_TABLE in PROJECT_PARAMS when setting up. Keep this table
in sync with the schematic: any time a detour adds, renames, or removes a bypass cap,
update `DECOUPLING_RULES` as part of that detour (see DETOUR PROTOCOL).

Limit: 7 mm for all cap types (pad-to-pad on shared net).

Output: `[REPORTS_DIR]/decoupling_proximity_report.txt` — one line per rule:
`PASS  C_VDD_BYP  U1  VDD  1.2mm  100nF bypass at U1 VDD pin`
`FAIL  C_VIN_BULK U1  VIN  10.1mm  input bulk cap at U1 VIN` — move this cap closer.

**Schematic-aware proximity verification** — after decoupling proximity is clean, read
`Python Scripts/check_component_proximity.py`, configure its PROJECT CONFIG block
(PCB_FILE, REPORT_FILE, and RULES copied from PROXIMITY_RULES_TABLE in PROJECT_PARAMS),
and run it. The script measures pad-to-pad distance on the specific net named in
`net_hint` — the pad that actually carries the signal between the two components. When
`net_hint` is `None`, it uses the minimum across all shared nets. When no shared net
exists, it falls back to centroid-to-centroid. The report annotates each row with the
measurement method: `[net:NETNAME]`, `[hint-miss:NETNAME→fallback]`, `[pad:NETNAME]`,
or `[ctr]`.

```python
# PROJECT CONFIG — set from PROJECT_PARAMS before running
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REPORTS_DIR]/component_proximity_report.txt"

# RULES — copy PROXIMITY_RULES_TABLE from PROJECT_PARAMS
# Each entry: (ref_A, ref_B, max_dist_mm, type, reason, net_hint)
# net_hint: exact KiCad net name of the connecting pad (case-sensitive); None = min shared net
RULES = [
    # ("C4", "U2", 8,  "decoupling_bypass", "100nF bypass cap on U2 VDD pin", "VDD"),
    # ("D1", "J1", 8,  "esd_clamp",         "TVS on J1 signal pins",          "USB_DP"),
    # ("C5", "U1", 15, "decoupling_bulk",    "10uF bulk cap for U1 VIN",       None),
]
```

On any FAIL: move the offending component closer using `place_passive_near_parent()`, re-run
`place_components.py` loops, then re-run this check. Do not adjust the threshold — the
thresholds are calibrated design rules, not targets to relax.

Output: `[REPORTS_DIR]/component_proximity_report.txt` — one line per rule:
`PASS | C4↔U2 | 6.23mm (max 8mm) | decoupling_bypass | 100nF bypass cap on U2 VDD pin`

**Polarity marker check** — run `Python Scripts/check_polarity_markers.py` (configure
its PROJECT CONFIG block: PCB_FILE, REPORT_FILE). It checks every polarized component
(ICs, diodes, electrolytic caps) for a pin 1 dot, cathode bar, or + marker on
F.Silkscreen within 3 mm. Add any missing markers now — component orientation is fixed
after this phase and silkscreen changes after routing are low-risk but wasteful.

Output: `[REPORTS_DIR]/polarity_markers_report.txt`.

**Bus endpoint distance pre-screen** — for each low-speed bus (I2C, SPI, UART, etc.)
listed in PROJECT_PARAMS, measure the straight-line distance between the furthest pads
on the bus (source IC pad to destination IC pad). Write
`[SCRIPTS_DIR]/check_bus_endpoint_distance.py` to compute this from pad positions.

Rule: if any bus endpoint pair is > 10 mm apart, GND guard traces will be mandatory in
Phase 11 and must be budgeted into routing. If distance > 25 mm, consider repositioning
the ICs to shorten the bus before proceeding — guard traces that long consume significant
routing space.

Output: `[REPORTS_DIR]/bus_endpoint_distance_report.txt` — one line per bus:
`I2C_SDA: U1 pad 5 → U3 pad 2 = 8.2 mm — OK` or `SPI_MOSI: U1 pad 12 → U5 pad 3 = 14.7 mm — GUARD REQUIRED`.

**Conformal coating keepout annotations** — if PROJECT_PARAMS specifies conformal
coating: run `Python Scripts/add_coating_notes.py` (configure its PROJECT CONFIG block:
PCB_FILE). It adds "NO COAT" text on F.Fab at each connector and probe access area;
"COAT ALL OTHER AREAS" at board center; board note with coating type
(e.g. "Humiseal 1B31 or equiv, 0.05–0.13mm"). Connector positions are final after
Phase 8 — adding these annotations now avoids a post-routing F.Fab edit.

Apply Loop B on the script. Then Loop A. Then Loop C (first-pass loop area estimate
from pad positions). Then Loop E (thermal via count). Then run `check_decoupling_proximity.py` — all pads must PASS before proceeding.

**Board size minimization** (after A, C, E are all clean): write
`[SCRIPTS_DIR]/minimize_board_size.py`:
1. Load PCB, extract every `F.Courtyard` / `B.Courtyard` polygon vertex
2. Compute axis-aligned bounding box (min/max X and Y)
3. Add 0.5 mm IPC-2221B edge clearance on all sides
4. Round UP each dimension to the nearest 1 mm
5. If resulting rectangle < current outline: rewrite Edge.Cuts, reposition mounting
   holes 3 mm inside each new corner, report area reduction
6. Re-run Loop A after outline update

**Label cleanup** (after board size minimization, before proceeding): write
`[SCRIPTS_DIR]/cleanup_labels.py`. The board must be readable during routing — value
text clutters the view and silkscreen reference designators on non-connector parts serve
no purpose at this stage. Apply the following rules to every footprint:

```python
import sys
sys.path.insert(0, "C:/Program Files/KiCad/10.0/bin/Lib/site-packages")
import pcbnew

board = pcbnew.LoadBoard(PCB_FILE)

for fp in board.GetFootprints():
    ref = fp.Reference()
    val = fp.Value()
    is_connector = fp.GetReference().startswith("J") or fp.GetReference().startswith("P")

    if is_connector:
        # Connectors: keep reference on F.Silkscreen for assembly identification;
        # connector purpose labels are added in Phase 11.
        val.SetVisible(False)
    else:
        # All other components: move reference to F.Fab only; hide on silkscreen.
        if ref.GetLayer() == board.GetLayerID("F.Silkscreen"):
            ref.SetLayer(board.GetLayerID("F.Fab"))
        elif ref.GetLayer() == board.GetLayerID("B.Silkscreen"):
            ref.SetLayer(board.GetLayerID("B.Fab"))
        # Hide value everywhere — it adds no information during routing.
        val.SetVisible(False)

board.Save(PCB_FILE)
print("Label cleanup complete.")
```

Result: only connector reference designators remain on silkscreen layers. All other
reference designators move to F.Fab (visible in fabrication view, invisible on the
routing canvas). No value text is visible on any component. This matches THE SILKSCREEN
RULE and makes the routed board significantly easier to read.

**Center board on page** (after label cleanup): write `[SCRIPTS_DIR]/center_board.py`.
The board outline should be centered on the KiCad page so the design is easy to navigate
in the GUI. Read the page dimensions from the `(paper ...)` declaration in `[PCB_FILE]`
and shift all board objects to align the Edge.Cuts bounding box center with the page
center.

```python
import sys, re
sys.path.insert(0, "C:/Program Files/KiCad/10.0/bin/Lib/site-packages")
import pcbnew

# Standard KiCad page sizes (mm)
PAGE_SIZES = {
    "A4": (297, 210), "A3": (420, 297), "A2": (594, 420),
    "A1": (841, 594), "A0": (1189, 841),
    "A":  (279.4, 215.9), "B": (431.8, 279.4),
    "C":  (558.8, 431.8), "D": (863.6, 558.8), "E": (1117.6, 863.6),
}

# Read page size from file header
raw = open(PCB_FILE, encoding="utf-8").read()
m = re.search(r'\(paper\s+"([^"]+)"', raw)
page_name = m.group(1) if m else "A3"
page_w, page_h = PAGE_SIZES.get(page_name, (420, 297))
page_cx = pcbnew.FromMM(page_w / 2)
page_cy = pcbnew.FromMM(page_h / 2)

board = pcbnew.LoadBoard(PCB_FILE)
bbox  = board.GetBoardEdgesBoundingBox()
dx = page_cx - bbox.GetCenter().x
dy = page_cy - bbox.GetCenter().y

if dx == 0 and dy == 0:
    print("Board already centered.")
else:
    for fp in board.GetFootprints():
        fp.SetPosition(pcbnew.VECTOR2I(fp.GetX() + dx, fp.GetY() + dy))
    for t in board.GetTracks():
        t.SetStart(pcbnew.VECTOR2I(t.GetStart().x + dx, t.GetStart().y + dy))
        t.SetEnd(pcbnew.VECTOR2I(t.GetEnd().x + dx, t.GetEnd().y + dy))
    for z in board.Zones():
        outline = z.Outline()
        for i in range(outline.PointCount()):
            pt = outline.CPoint(i)
            outline.SetPoint(i, pcbnew.VECTOR2I(pt.x + dx, pt.y + dy))
    for d in board.GetDrawings():
        d.Move(pcbnew.VECTOR2I(dx, dy))
    board.Save(PCB_FILE)
    print(f"Board centered on {page_name} page (shifted {pcbnew.ToMM(dx):.2f}, {pcbnew.ToMM(dy):.2f} mm).")
```

Apply Loop B on both `cleanup_labels.py` and `center_board.py`. Re-run Loop A after
centering to confirm DRC count is unchanged.

**Visual check required before Phase 9:** Open `[PCB_FILE]` in the KiCad GUI and
visually confirm: (a) all components are in their correct zones with no obvious
misplacements; (b) connectors are on board edges; (c) the board is readable with only
connector references visible on silkscreen; (d) no courtyard overlaps are visible.
This is the one step in Phase 8 Claude cannot perform — it has no visual access to the
board. Do not proceed to Phase 9 until the user has confirmed the placement looks
correct. Record the outcome in SESSION_CONTEXT.

**Outputs:** `[REPORTS_DIR]/board_size_report.txt`,
`[REPORTS_DIR]/placement_rationale.txt`,
`[REPORTS_DIR]/decoupling_proximity_report.txt`,
`[REPORTS_DIR]/component_proximity_report.txt`,
`[REPORTS_DIR]/polarity_markers_report.txt`,
`[REPORTS_DIR]/bus_endpoint_distance_report.txt`.

**Loop exit criteria:** DRC clean; all switching loops <= 50 mm²; all thermal budgets
met; board size minimized; `check_decoupling_proximity.py` all PASS;
`check_component_proximity.py` all PASS; polarity markers all present; bus endpoint
distances documented; label cleanup applied; board centered on page; user visual check
confirmed.

**SESSION_CONTEXT:** Phase 8 complete, final board dims, area reduction, Loop C/E
results, visual check outcome.

---

## POST-PLACEMENT GATE

Before Phase 9, verify every item below. These are not optional — Phase 9 zone fills
and stitching vias depend on correct, finalized component positions. Zones added over
unplaced or incorrectly placed components must be redone.

- [ ] Phase 8 complete: `place_components.py` ran and exited 0
- [ ] `[REPORTS_DIR]/board_size_report.txt` exists and reports final minimized dimensions
- [ ] `[REPORTS_DIR]/decoupling_proximity_report.txt` exists — zero FAIL entries
- [ ] `[REPORTS_DIR]/component_proximity_report.txt` exists — zero FAIL entries
- [ ] `[REPORTS_DIR]/polarity_markers_report.txt` exists — zero missing markers
- [ ] `[REPORTS_DIR]/bus_endpoint_distance_report.txt` exists
- [ ] Loop C complete: all switching loops <= 50 mm²
- [ ] Loop E complete: all thermal budgets met
- [ ] DRC clean (zero violations except known-acceptable unconnected items)
- [ ] `cleanup_labels.py` ran: no value text visible; non-connector references on F.Fab only
- [ ] `center_board.py` ran: board centered on page
- [ ] User has opened `[PCB_FILE]` in KiCad GUI and confirmed placement visually

Do not start Phase 9 if any item is unchecked. If components are stacked at the
origin (Phase 7.5 netlist sync result but Phase 8 not yet run), go back and run
Phase 8 — do not proceed to zones with unplaced components.

---

## PHASE 9 — Copper Zones

**Purpose:** Add GND planes, power planes, local copper islands, stitching vias.

**Inputs:** `board_size_report.txt` (final board rectangle), PROJECT_PARAMS layer
stackup and power rail table.

**Loops:** Loop A (DRC), Loop B (script).

**Known-acceptable DRC:** "Unconnected items", "Zone has no fills" before fill. No
clearance violations.

**Process:** Write `[SCRIPTS_DIR]/add_zones.py`. All zone polygons inset 1 mm from
board outline unless otherwise specified.

**Zones (adjust per PROJECT_PARAMS stackup):**
- **In1.Cu SOLID GND** — full board, no thermal reliefs, unbroken under any HS pair on
  the HS routing layer
- **In3.Cu SOLID GND** (if 6-layer) — mirror of In1.Cu; shields HS from power
- **In4.Cu POWER PLANES** — separate polygons for each major rail (`[POWER_RAIL_MAIN]`
  over Zone B, `[POWER_RAIL_5V]` over remainder, `[POWER_RAIL_LOW_V]` island near
  loads). 0.5 mm gap between adjacent power zones.
- **F.Cu GND FILL** — full board, thermal relief on pads
- **B.Cu GND FILL** — full board, thermal relief on pads, minus switching trace zones
- **Local GND islands under IC exposed pads** — one per IC with EP, no thermal relief

**Stitching vias:** every 5 mm along perimeter, 3 mm inside edge, drill 0.3 mm, pad
0.6 mm, F.Cu to B.Cu, net GND. Dense stitching (2 mm pitch) alongside HS pair channel
edges — but **never directly beneath** an HS pair segment on the HS layer.

Apply Loop B on `add_zones.py`. Then fill all zones before running any DRC:

```python
import sys
sys.path.insert(0, "C:/Program Files/KiCad/10.0/bin/Lib/site-packages")
import pcbnew

board = pcbnew.LoadBoard(PCB_FILE)
filler = pcbnew.ZONE_FILLER(board)
filler.Fill(board.Zones())
board.Save(PCB_FILE)
print(f"Filled {len(board.Zones())} zones.")
```

Save this as `[SCRIPTS_DIR]/fill_zones.py` and run it after every zone geometry change.
Zones must be filled before DRC is meaningful — unfilled zones do not participate in
clearance checks, so DRC on an unfilled board produces false passes. Apply Loop B on
`fill_zones.py`, then apply Loop A.

**Outputs:** `[REPORTS_DIR]/fix_log_phase9.txt`.

**Loop exit criteria:** All zones filled (zero "Zone has no fills" DRC violations);
DRC otherwise clean.

**SESSION_CONTEXT:** Phase 9 complete, zone list.

---

## PRE-ROUTING GATE

Before Phase 10, verify every item below. Routing begins on top of the zone copper —
if zones are unfilled or incomplete, the autorouter and HS pre-routing scripts will
produce incorrect clearance behavior and the DRC results will be meaningless.

- [ ] Phase 9 complete: `add_zones.py` ran and exited 0
- [ ] `fill_zones.py` ran: zero "Zone has no fills" DRC violations
- [ ] All zones present per PROJECT_PARAMS stackup (GND planes on correct layers, power
      planes on correct layer, F.Cu and B.Cu fills, local EP islands)
- [ ] Stitching vias placed at perimeter and alongside HS channel
- [ ] `[REPORTS_DIR]/fix_log_phase9.txt` exists
- [ ] DRC clean (zero violations except known-acceptable unconnected items)
- [ ] Board outline is final — no further minimization will occur after routing starts
- [ ] CONNECTOR_PROTECTION_TABLE re-verified: no connectors added or changed since PRE-LAYOUT GATE without a Phase 0b + Phase 3 re-check
- [ ] All protection components (ESD clamps, CM chokes, polyfuses) present in PCB as placed footprints — verify via footprint count matches schematic BOM
- [ ] **HS_PAIRS completeness verified** — run the check below; every connected `_P`/`_N` net pair must appear in `HS_PAIRS` or be explicitly excluded with rationale

**HS_PAIRS completeness check (required before Phase 10):**

Load the board and collect every net name ending in `_P` or `_N` that is actually connected (net name does not start with `unconnected-(`) and is not in `FANOUT_VIA_SKIP_NETS`. Every such pair must appear in `HS_PAIRS` in `routing_config.py`, or be documented in a comment in `routing_config.py` explaining why it is excluded (e.g. intentionally unrouted, routed by other means). Any pair not accounted for is a STOP — add it to `HS_PAIRS` before proceeding.

```python
import sys; sys.path.insert(0, "C:/Program Files/KiCad/10.0/bin/Lib/site-packages")
import pcbnew
sys.path.insert(0, "[SCRIPTS_DIR]")
import routing_config as cfg

board = pcbnew.LoadBoard("[PCB_FILE]")
all_nets = {str(n) for n in board.GetNetInfo().NetsByName().keys() if str(n)}
connected = {n for n in all_nets if not n.startswith("unconnected-(")}
skip = set(cfg.FANOUT_VIA_SKIP_NETS)

p_nets = {n for n in connected if n.endswith("_P")} - skip
n_nets = {n for n in connected if n.endswith("_N")} - skip
hs_nets = {net for p, n, _l, _s in cfg.HS_PAIRS.values() for net in (p, n)}

missing_p = sorted(p_nets - hs_nets)
missing_n = sorted(n_nets - hs_nets)
if missing_p or missing_n:
    print("NOT IN HS_PAIRS — add or document exclusion:")
    for n in missing_p + missing_n:
        print(f"  {n}")
else:
    print("HS_PAIRS completeness: PASS")
```

Do not start Phase 10 if any item is unchecked. Unfilled zones before routing is the
equivalent of routing on a bare board with no ground planes — impedance control,
clearance checks, and return path verification will all be invalid.

---

## PHASE 10 — Critical Pre-routing

**Purpose:** Route two categories manually before FreeRouting:
1. Switching loops (SW nodes, CFLY nets) — autorouters cannot minimize loop area
2. Multi-Gbps differential pairs — autorouters cannot honor impedance, skew, or layer
   discipline

All other nets remain unrouted for Phase 10a.

**Inputs:** PROJECT_PARAMS switching converter list, HS pair inventory, layer stackup.

**Loops:** Loop A (DRC), Loop B (scripts), Loop C (switching loop area).

**Known-acceptable DRC:** "Unconnected items" for all non-routed nets.

**Process:**

**Part 0 — Pre-route alignment.** Run `[SCRIPTS_DIR]/route_prep_align.py`. Positions satellite passives into alignment groups adjacent to their anchor ICs before routing begins. See Phase10_Alignment_Guide_Addition.md Part 0 for full procedure.

**Part 0.5 — Routing clearance audit.** Run `[SCRIPTS_DIR]/route_clearance_audit.py`. Verifies and corrects component clearances for routing corridors, via keepout zones, HS pair paths, and diff-pair meander budgets. See Phase10_Alignment_Guide_Addition.md Part 0.5 for full procedure.

**Part 0.75 — Fanout via placement.** Run `[SCRIPTS_DIR]/route_fanout_vias.py`. Places layer-transition vias at every pad that needs to reach a different copper layer for routing. Covers all nets — HS pairs, switching loops, and general signals — using the layer priority declared in `routing_config.py::ROUTING_LAYER_PRIORITY`. Must run after clearance audit (so component positions are final) and before any trace routing (so routing scripts receive fixed via endpoints rather than computing via placement themselves). Details to be added once script is complete.

**Part A — Switching loops.** Run `[SCRIPTS_DIR]/route_critical.py`. For each
switching converter:
- SW pin → inductor pad 1 (< 10 mm)
- Inductor pad 2 → rectifier / output node
- Rectifier / output → output cap positive pad
- Output cap negative → GND via
- Input cap positive → VIN pin (< 2 mm)
- Input cap negative → GND via
- Trace widths from Loop D table
- Flying capacitor nets: direct between pins, zero vias
- GND return vias adjacent to each PGND pad

**Part B — HS differential pairs.** Write `[SCRIPTS_DIR]/route_highspeed.py`. For every
`[HS_PAIR_*]` net:
- Route on `[HS_ROUTING_LAYER]` ONLY (typically In2.Cu). No segments on F.Cu / B.Cu.
- Width and spacing per fab impedance calc for the stackup (seed 0.15/0.15 mm at 100 ohm)
- Intra-pair length match within 0.01 mm design target (protocol floor: 0.127 mm); add trombone at pin fanout, not mid-route
- Inter-lane length match per PROJECT_PARAMS budget
- Vias ONLY at endpoint pads (F.Cu → HS layer). Prefer blind vias if fab supports;
  otherwise through-hole with GND return via within 1 mm.
- NO vias mid-trace. NO layer changes mid-trace.
- 3W clearance: any other copper >= 2 × trace width

**HS layer sandwiching — mandatory verification (only if HS inventory is present):**

If `HIGH-SPEED SIGNAL INVENTORY` in PROJECT_PARAMS is `N/A`, skip this entire section.

A buried stripline requires solid, unbroken GND on both copper layers immediately
adjacent to the signal layer in the stackup. A void, non-GND zone, non-GND track, or
non-GND via anti-pad on either reference plane directly beneath a HS trace breaks the
return path, invalidates the impedance, and creates a reflection. The reference planes
are whichever two layers sit immediately above and below `[HS_ROUTING_LAYER]` — they
are determined from the board's actual layer stack at runtime, not assumed by name.

Run `Python Scripts/verify_hs_sandwich.py`. Configure PCB_FILE, REPORT_FILE, HS_LAYER
(from PROJECT_PARAMS `[HS_ROUTING_LAYER]`), and GND_NET_NAME. The script determines the
two reference planes adjacent to HS_LAYER automatically from the board's layer stack at
runtime, then checks four rules: all zones on reference planes are GND; no non-GND tracks
on reference planes; no non-GND via anti-pads pierce reference planes within the HS
channel bounding box; no zone boundaries on reference planes cross the HS channel.

Run after Phase 9 zone fill and again after Phase 10 HS routing. Any FAIL is blocking before Phase 10a:

| Issue type | Fix |
|---|---|
| Non-GND zone on a reference plane | Change zone net to GND; if power must cross that layer, use a narrow routed track, not a flood |
| Non-GND track on a reference plane | Reroute to a non-reference layer |
| Non-GND via in HS channel piercing reference plane | Move the via outside the HS channel bounding box, or add a GND via within 0.5 mm of it |
| Zone boundary crossing HS channel on reference plane | Merge the split zones, or shift HS routing to avoid the zone boundary |

Apply Loop B on both scripts.

**Lock HS nets for FreeRouting exclusion:** Run `Python Scripts/lock_highspeed_nets.py`.
Populate HS_NETS from the HS_PAIRS list in PROJECT_PARAMS (both P and N net names for
every pair). The script sets the KiCad LOCKED flag on every segment and via belonging to
those nets and saves the PCB. Any net reporting "0 segments locked" means Phase 10 HS
routing did not complete for that net — fix before proceeding or FreeRouting will
overwrite the manual pre-route.

**Loop A** (DRC clean).
**Loop C** (measure each switching loop; if any > 50 mm², shorten segments, re-run).

**Outputs:** `[REPORTS_DIR]/fix_log_phase10.txt` with routed nets, widths, loop areas.

**Loop exit criteria:** DRC clean, all switching loops <= 50 mm², all HS pairs routed
on HS layer with vias only at endpoints.

**SESSION_CONTEXT:** Phase 10 complete.

---

## PHASE 10a — Headless FreeRouting

**Purpose:** Autoroute all remaining unrouted connections (low-speed signals, power,
control lines). Pre-routed HS and switching loops are locked and skipped.

**Inputs:** `[PCB_FILE]`, `[FREEROUTING_JAR]`, `[FREEROUTING_TEMP_DIR]` (path without
spaces).

**Loops:** Loop A (DRC), Loop B (scripts).

**Pre-flight checks — required before DSN export:**

**Pre-flight A — Pad orientation blockage.** The most common silent FreeRouting killer.
When a footprint is placed at a rotation that differs from its pad definitions, elongated
pads keep their original orientation and overlap adjacent pads, producing shorting_item
DRC violations that freeze the router locally.

Read `Python Scripts/check_pad_orientation.py`, configure its PROJECT CONFIG block
(PCB_FILE), and run it. It flags any pad where aspect > 2 and `|pad_rot - fp_rot| mod
360 < 1`. Fix: `pad.SetOrientation(pcbnew.EDA_ANGLE(-fp_rot, pcbnew.DEGREES_T))` per
pad, OR swap footprint for a correctly oriented one.

**Pre-flight B — Board-spanning keepout zones.** An edge-clearance keepout sized nearly
board-size blocks all routing. Read `Python Scripts/check_keepouts.py`, configure its
PROJECT CONFIG block (PCB_FILE), and run it. Any keepout
covering > 70% of board area is a bug — delete and recreate as a narrow 1–2 mm band
along the inside of Edge.Cuts.

**Pre-flight C — DRC shorting_item.** Run DRC before DSN export. Zero shorting_item
before proceeding — FreeRouting stalls in any region containing shorts.

**Step 1 — Verify HS locks before export.** Before exporting the DSN, confirm that every HS net actually has locked segments. If any HS net has zero locked segments, re-run the HS routing script for that net before proceeding — otherwise FreeRouting will autoroute it and overwrite the manual pre-routing.

Run `[SCRIPTS_DIR]/lock_highspeed_nets.py` and confirm each locked net is listed in the output. A net listed with "0 segments locked" means the pre-routing script did not run successfully for that net.

**Export DSN — path-with-spaces workaround.** FreeRouting's argument parser breaks on
paths with spaces. Copy PCB to `[FREEROUTING_TEMP_DIR]`, work there, copy result back.

Export DSN via pcbnew (kicad-cli dropped specctra export in KiCad 10):
```python
import pcbnew
board = pcbnew.LoadBoard(TEMP_PCB)
try:
    pcbnew.ExportSpecctraDSN(board, TEMP_DSN)
except AttributeError:
    # Some KiCad 10 builds do not expose ExportSpecctraDSN.
    # Fallback: use the board's own export method if available.
    if hasattr(board, 'ExportSpecctra'):
        board.ExportSpecctra(TEMP_DSN)
    else:
        raise RuntimeError(
            "ExportSpecctraDSN not available in this KiCad 10 build. "
            "Use KiCad GUI: File → Export → Specctra DSN to export manually, "
            "then place the DSN at: " + TEMP_DSN
        )
```

**Run FreeRouting — always via `run_freerouting.py`, never bare `java -jar`.**

FreeRouting shows a donation popup dialog when routing completes. On Windows this
is a modal AWT window that blocks the Java process from exiting, meaning Claude
cannot detect completion until the popup is manually dismissed. Two defences:

1. `-Djava.awt.headless=true` JVM flag — tells the JVM not to render any AWT/Swing
   windows at all. This suppresses the popup entirely in most FreeRouting builds.
2. SES file-watcher fallback — if the process stalls despite the headless flag
   (older FreeRouting builds ignore it), the watcher detects that the output file
   has stopped growing and terminates the process.

**Run FreeRouting — read `Python Scripts/run_freerouting.py`, set its PROJECT CONFIG
block (FREEROUTING_JAR, TEMP_DIR, MAX_PASSES, THREADS, MAX_RUNTIME_SECS), and run it.**
Use this script for every FreeRouting invocation in Phase 10a and any re-runs in
Phase 10b — never invoke `java -jar` directly.

**Invoke it:**
```
python "[SCRIPTS_DIR]/run_freerouting.py" \
    "[FREEROUTING_TEMP_DIR]/autoroute.dsn" \
    "[FREEROUTING_TEMP_DIR]/autoroute.ses" \
    100
```
Apply Loop B on this script before first use. Expected runtime: 1–10 minutes.
Process exits (or is terminated by the watcher) automatically — no manual popup
dismissal required.

**Crash response protocol.** The script prints a `FREEROUTING FAILURE REPORT`
and exits with code 1 on any crash. Read the report and respond:

| Failure type | Remedy |
|---|---|
| `OUT_OF_MEMORY` | Change `-Xmx2g` to `-Xmx4g` in the cmd list; retry |
| `NO_SES_FILE` or `EMPTY_SES_FILE` | Re-run pre-flights A/B/C; export fresh DSN; retry |
| `TRUNCATED_SES_FILE` | SES written then process died — re-run; if recurs, reduce `-mt` to 2 |
| `JAVA_EXCEPTION (NullPointerException)` | Usually a malformed DSN — export fresh DSN; if recurs, strip HS locked nets and re-export |
| `JVM_CRASH_LOG` | Open `hs_err_pidNNNN.log`; note the module name; update Java if JVM bug |
| `BAD_EXIT_CODE` | Read full stderr; if no other pattern matches, file a FreeRouting bug report |
| `SES has no (routes block` | FreeRouting parsed the DSN then crashed immediately — check DSN for zero-length nets or duplicate net names |
| `TIMEOUT` | Board too complex for 20 min — increase `MAX_RUNTIME_SECS`; or reduce net count by merging power rails |

If the same crash recurs after applying the listed remedy, apply the Lessons
Learned Protocol and stop for user input.

If > 0 unrouted after 100 passes: re-run with `200` as the third argument,
or accept partial and route remainder in Phase 11.

**Import SES via pcbnew** (kicad-cli does not support SES import):
```python
board = pcbnew.LoadBoard(TEMP_PCB)
try:
    pcbnew.ImportSpecctraSES(board, SES_PATH)
except AttributeError:
    # Some KiCad 10 builds do not expose ImportSpecctraSES at module level.
    try:
        board.ImportSpecctraSES(SES_PATH)
    except AttributeError:
        raise RuntimeError(
            "ImportSpecctraSES not available in this KiCad 10 build. "
            "Use KiCad GUI: File → Import → Specctra Session to import manually, "
            "then save the PCB."
        )
board.Save(PCB_FILE)
```

**Autoroute quality scoring** — after SES import, before Loop A, run
`[SCRIPTS_DIR]/score_autoroute.py` to assess routing quality. A poor result now
is best fixed by Phase 10b (placement) rather than accepting bad routing.

```python
import pcbnew, math

def score_routing(board):
    issues = []

    # 1. Via count (excessive vias = poor placement)
    via_count = sum(1 for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA))
    board_area_mm2 = board.GetBoardEdgesBoundingBox().GetArea() / 1e12  # nm² → mm²
    via_density = via_count / board_area_mm2 if board_area_mm2 > 0 else 0
    if via_density > 0.15:
        issues.append(f"HIGH_VIA_DENSITY: {via_density:.3f} vias/mm² (threshold 0.15)")

    # 2. Detour ratio per net (routed length vs Manhattan distance)
    from collections import defaultdict
    net_lengths = defaultdict(float)
    for t in board.GetTracks():
        if isinstance(t, pcbnew.PCB_TRACK) and not isinstance(t, pcbnew.PCB_VIA):
            net_lengths[t.GetNetname()] += pcbnew.ToMM(t.GetLength())
    # (Manhattan distance requires pad positions — flag nets > 2× Manhattan)
    long_routes = [n for n, l in net_lengths.items() if l > 200]  # crude: flag > 200mm
    if long_routes:
        issues.append(f"LONG_ROUTES: {long_routes}")

    # 3. Track layer distribution (all routing on one layer = placement problem)
    layer_counts = defaultdict(int)
    for t in board.GetTracks():
        if isinstance(t, pcbnew.PCB_TRACK) and not isinstance(t, pcbnew.PCB_VIA):
            layer_counts[board.GetLayerName(t.GetLayer())] += 1
    total = sum(layer_counts.values())
    for layer, count in layer_counts.items():
        if count / total > 0.70:
            issues.append(f"LAYER_IMBALANCE: {layer} has {count/total:.0%} of all tracks")

    return issues
```

Score thresholds — if ANY of the following, trigger Phase 10b:
- Via density > 0.15 vias/mm²
- Any single layer carries > 70% of all track segments
- Total routed track length > 3 × board area (in mm)
- Unrouted nets > 10% of total net count after `-mp 200`

Write quality score to `[REPORTS_DIR]/autoroute_log.txt`. If score triggers 10b,
note the specific failing metric.

**Loop A** — Run DRC with `--refill-zones` flag after SES import to ensure zone fills are current before checking clearances. Compare unconnected before/after. Known-acceptable: unconnected on HS pair
nets (locked, deferred to length-matching phase); silkscreen cosmetic (Phase 11).
NEW-REAL-ERROR: clearance violation between autorouted tracks, autorouted track on the
HS routing layer (reserved), any track touching a locked HS segment.

**Outputs:** `[REPORTS_DIR]/autoroute_log.txt` with unconnected before/after, %
routed, DRC delta, quality score, list of nets FreeRouting left unrouted.

**Loop exit criteria:** Autoroute completed, DRC has no clearance violations, HS nets
untouched.

**SESSION_CONTEXT:** Phase 10a complete, autoroute stats.

---

## PHASE 10b — Placement Optimization Based on Routing Analysis

**Purpose:** Routing problems reveal placement problems. After FreeRouting, analyze the
routing result to identify placement issues, fix placement, and re-route. Fix the root
cause (placement) rather than the symptom (bad routing).

> **Non-linear phase:** This is the only phase that loops backward. Entry is from Phase
> 10a (poor score or > 10% unrouted). Exit returns to Phase 10a for re-autorouting, then
> forward to Phase 10c. If placement changes are extensive, re-run Phase 9 zone fills
> before re-entering Phase 10a. Document each iteration count in SESSION_CONTEXT.

**Inputs:** `[PCB_FILE]` post-autoroute, `autoroute_log.txt`, `score_autoroute.py`
output. (`trace_length_check.py` is run internally during re-route iterations — it is
not an input to this phase, it does not exist until Phase 10d.)

**Loops:** Loop B on the analysis and fix scripts; Loop A after any placement change.

**Step 1 — Routing quality analysis.** Write
`[SCRIPTS_DIR]/analyze_routing_quality.py`. For each HS differential pair, extract:
- The two endpoint components and their current (X, Y) positions
- The dominant routing layer for P and for N (from per-layer length breakdown)
- Whether P and N are on the same layer (good) or different layers (bad =
  cross-layer pair)
- Intra-pair skew (mm)
- Total routed length vs. straight-line Manhattan distance between endpoints

**Step 2 — Placement problem classification.**

*For HS differential pairs:*
- **CROSS-LAYER** — P and N forced onto different layers. The component pair needs realignment.
- **DENSE-AREA** — Meanders cannot be inserted (Phase 10d reports no clearance). Components are too close in a straight line and need spreading.
- **LONG-ROUTE** — Total routed length > 1.5 × Manhattan distance. Routing had to go around obstacles; move component closer to its HS source.

*For general routing quality (from `score_autoroute.py`):*
- **HIGH VIA DENSITY** (> 0.15 vias/mm²) — The router could not find direct paths and used many layer changes. Fix: spread components in the dense region (typically the MCU zone where many nets converge), or increase the board size by 5–10% to give the router more room.
- **LAYER IMBALANCE** (one layer > 70% of tracks) — All routing is being forced onto one layer, usually because the other side has a continuous pour the router avoids. Fix: verify GND zones are set to solid fill (not hatched), and that the keepout on the sparse layer is not preventing routing there. If one layer genuinely has much more copper, rebalance component placement so that nets are more evenly distributed between both sides.

**Step 3 — Placement fix strategy.**
- **CROSS-LAYER between chip A and chip B:** rotate or translate B so its HS input pads
  face A's output pads in the same orientation. Both pads on the same vertical or
  horizontal axis minimizes forced layer changes at the escape.
- **DENSE-AREA:** increase spacing between components in the dense region — often just
  1–3 mm additional pitch is enough to open a meander corridor.
- **LONG-ROUTE:** move the component closer to its HS source along the signal path
  axis. Do not increase the total zone bounding box unless necessary.

Constraints during any placement change: connectors remain at fixed positions; mounting
holes remain fixed; keep power components in the power zone; keep HS components in the
HS zone. All changes go through a placement-fix script (schematic-first rule does not
apply — position is a PCB-only property).

**Step 4 — Iteration.** After repositioning:
1. Re-export DSN (Phase 10a export step)
2. Re-run FreeRouting via `python "[SCRIPTS_DIR]/run_freerouting.py" ...`
   (same runner script as Phase 10a — never invoke java -jar directly)
3. Re-import SES
4. Re-run `trace_length_check.py`
5. Compare cross-layer pair count vs previous iteration
6. If reduced: placement improvement confirmed
7. If unchanged: try the next classification category
8. Repeat until all HS pairs route on the same dominant layer, or until diminishing
   returns (three iterations without improvement)

**Step 5 — Acceptance criteria.** Preferred: all HS differential pairs have P and N
on the same dominant layer (cross-layer pair count = 0) and intra-pair skew < 2 mm.

**Diminishing-returns exit:** if cross-layer pair count has not decreased across three
consecutive iterations, exit Phase 10b even if count > 0. Flag each remaining
cross-layer pair as Category B in `placement_optimization_log.txt`. Phase 10d will
handle Category B pairs with manual GUI rerouting. Proceeding with Category B pairs is
permitted; proceeding with Category A (same-layer but skew > 2 mm) pairs is not — they
require further placement adjustment.

**Outputs:** `[REPORTS_DIR]/placement_optimization_log.txt` (per-iteration cross-layer
count, dense-area count, long-route count, actions taken).

**Loop exit criteria:** Zero cross-layer HS pairs; DRC clean after each iteration.

**SESSION_CONTEXT:** Phase 10b complete, iterations run, final HS pair layer status.

---

## PHASE 10c — Pre-Fab Blocker Resolution

**Purpose:** Resolve all DRC violations introduced by the routing process — footprint ID
errors, autorouter clearance violations, keepout breaches, copper artifacts, and via
stubs. This phase clears routing-generated DRC to zero. It does not evaluate
compliance-level design rules (trace widths, HS checks, copper coverage) — those are
Phase 12's responsibility.

**Inputs:** `[PCB_FILE]`, DRC report from Phase 10a / 10b.

**Loops:** Loop A, Loop B.

### Known blockers and fixes

**GUI DRC crash (0xc0000005 in _pcbnew.dll).** Symptom: KiCad GUI DRC crashes; CLI DRC
runs fine. Root cause: board contains footprint library IDs that don't exist in the
KiCad 10 library. GUI DRC's lib_footprint_issues check calls `LoadFootprint()`, gets
NULL, dereferences. Fix: for each library ID in the board, verify it exists in
`C:/Program Files/KiCad/10.0/share/kicad/footprints/`. Replace any non-existent ID
with the actual library equivalent.

**Board-spanning keepout zones blocking all routing.** Symptom: hundreds of
`items_not_allowed` violations across the board. Root cause: keepout zones covering the
full board area (one per copper layer). Fix:
```python
for z in board.Zones():
    bbox = z.GetBoundingBox()
    if pcbnew.ToMM(bbox.GetWidth()) > 50 and pcbnew.ToMM(bbox.GetHeight()) > 50:
        board.Remove(z)
```

**Corner keepout zones blocking mounting hole pads.** Symptom: `keepout_area` violations
on NPTH mounting hole pads. Root cause: corner keepout zones with `pads not_allowed`
containing the mounting hole pad. Fix: `z.SetDoNotAllowPads(False)` on small
(< 10 mm) corner zones.

**Fine-pitch resistor/capacitor array clearance.** Symptom: clearance violations on RN /
CN arrays. Root cause: 0.5 mm pitch array cannot meet 0.2 mm default clearance. Fix: add
a per-reference DRU rule (using `A.Reference`, not `A.Footprint`) with tighter
clearance:
```
(rule "array_clearance"
  (constraint clearance (min 0.13mm))
  (condition "A.Reference == 'RN1' || A.Reference == 'CN1'")
)
```

**Dangling stub tracks from FreeRouting.** Symptom: DRC errors on short (< 1 mm) tracks
with one floating endpoint. Fix: check each track endpoint for neighbors within 0.01 mm.
If one endpoint has no neighbor, the track is a dangling stub — remove it.

**Standalone silkscreen text duplicating footprint references.** Symptom: `silk_overlap`
where standalone PCB_TEXT overlaps footprint's own reference. Fix: delete the standalone
PCB_TEXT item.

**Outputs:** `[REPORTS_DIR]/blocker_resolution_log.txt`.

**Loop exit criteria:** DRC has 0 errors. `lib_footprint_mismatch` warnings acceptable
for intentionally customized footprints only.

**SESSION_CONTEXT:** Phase 10c complete.

---

## PHASE 10d — Trace Length Matching

**Purpose:** Ensure every trace within a length-matched group meets the skew budget
required for multi-Gbps signalling.

**Prerequisite — inter-lane skew must pass before this phase begins.** This phase fixes intra-pair skew only (P vs N length within a single lane). Inter-lane skew is set by IC placement and cannot be fixed here with meanders — the amounts required are too large and areas too congested. If inter-lane skew is out of spec when you reach this phase, STOP: return to Phase 8, move the ICs, re-route, then return here. Doing intra-pair tuning before inter-lane is confirmed in-spec wastes all the tuning work — any IC move to fix inter-lane will change all trace lengths and invalidate every meander added here.

**Inputs:** PROJECT_PARAMS skew budgets, autorouted PCB from Phase 10a.

**Loops:** Loop B (script), then Loop A (DRC after meander insertion).

**Standard tolerances** (adjust per PROJECT_PARAMS):

| Group | Intra-pair (protocol spec) | Intra-pair (design target) | Inter-lane |
|---|---|---|---|
| DP 1.4 each lane | <= 0.127 mm (~20 ps) | <= 0.01 mm | -- |
| DP 1.4 all lanes | -- | -- | <= 0.45 mm (~75 ps) |
| USB 3.1 Gen2 SS | <= 0.127 mm | <= 0.01 mm | <= 5.08 mm (~500 ps) |
| USB 2.0 HS | <= 0.5 mm | <= 0.01 mm | -- |
| HDMI 2.1 TMDS | <= 0.127 mm | <= 0.01 mm | <= 10 mm |

Design target of <= 0.01 mm is required — Phase 12 checklist verifies this as a hard pass. The protocol spec (0.127 mm) is an intermediate milestone only: use it as the pass threshold for the first meander pass (`insert_meanders.py`), then run a second tight-meander pass (`insert_meanders_tight.py`) to reach the 0.01 mm design target before exiting this phase.

Propagation delay: 160 ps/mm outer (microstrip), 170 ps/mm inner (stripline).

**Step 1 — Measure lengths.**

For projects with HS differential pairs, run `[SCRIPTS_DIR]/measure_all_diff_pairs.py` first. Configure its `GROUPS` list from the HIGH-SPEED SIGNAL INVENTORY in PROJECT_PARAMS (one entry per pair: group label, P net name, N net name) and `INTERLANE_SETS` from the inter-lane spread limits. The script produces three outputs in one pass:
- Per-pair P/N lengths, intra-pair skew, and PASS/FAIL vs. `INTRA_LIMIT_MM`
- Inter-lane spread per lane group vs. protocol spec
- **Skew adjustment chart** for every failing pair — names the exact net to lengthen/shorten, the required adjustment in mm, and the pad references on the key segment (direct input to Step 3 meander insertion)

Then run `[SCRIPTS_DIR]/trace_length_check.py` for the remaining length-matched nets:
- Sum track lengths per net per layer
- Flag every via on an HS net (stub discontinuity)
- Output PASS/FAIL in mm and ps

**Step 2 — Classify failures.**
- **Category A — Same-layer pairs:** P and N on the same dominant inner layer. Fixable
  by inserting U-shaped meander bumps on the shorter trace.
- **Category B — Cross-layer pairs:** P and N on different layers. Cannot be fixed
  with meanders alone. Return to Phase 10b, or reroute manually in KiCad GUI.

**Skew status reporting format.** When reporting skew gaps (whether from script output or
manual checks), use the following format — one header line per pair followed by an options
table. Both options must include component reference and pad number on the key segment:

```
  PAIR_NAME — gap X.XXXmm (delta note — reason)

  ┌───────────────┬──────────┬───────────────────┬────────────────────────────────────────────┐
  │    Option     │   Net    │      Action       │              Key segment                   │
  ├───────────────┼──────────┼───────────────────┼────────────────────────────────────────────┤
  │ A (preferred) │ /NET_P   │ Lengthen +X.XXXmm │ X.XXXmm seg, REF pad N → REF pad N         │
  ├───────────────┼──────────┼───────────────────┼────────────────────────────────────────────┤
  │ B             │ /NET_N   │ Shorten  -X.XXXmm │ X.XXXmm seg, REF pad N → REF pad N         │
  └───────────────┴──────────┴───────────────────┴────────────────────────────────────────────┘
```

Header delta note examples:
- `≈ same (X.XXXmm before)` — no routing changes detected
- `↓ from X.XXXmm — moving in the right direction`
- `↑ from X.XXXmm — gap grew` (with reason if known)
- `↓ from X.XXXmm — meander inserted, PASS`

Option A is always the shorter net (lengthen it); Option B is the longer net (shorten it).
Mark A as preferred unless routing geometry makes B clearly easier.

**Step 3 — Insert meanders (Category A).** Write / use
`[SCRIPTS_DIR]/insert_meanders.py`:

Core geometry: a U-bump splitting a straight segment adds exactly 2 × amplitude to the
trace length, independent of bump width. To correct a skew delta of `d` mm, use
amplitude `d / 2`.

Algorithm:
1. For each failing pair, iterate candidate segments longest-first on the dominant layer
2. For each segment, try both perpendicular directions
3. Score each direction by minimum clearance to other-net tracks and vias within the
   bump zone
4. Use the direction with best clearance; only insert if clearance >= 0.22 mm
5. If no segment passes, report FAIL for GUI tuning (or return to Phase 10b to spread
   the region)

**Tight-tolerance second pass.** After the first meander pass (protocol spec tolerance), run `[SCRIPTS_DIR]/insert_meanders_tight.py` for a second pass targeting the 0.01 mm design target. This script uses binary-search safe amplitude with full per-segment geometric DRC simulation — slower but achieves the design target in congested areas where the first pass left residuals.

**Debug tool — `find_close_net_refs.py`.** When `insert_meanders.py` cannot find a valid segment to insert on (no clearance in any direction), use `find_close_net_refs.py` to list all pad references and pad numbers for the affected net and its companion. This identifies the exact segment endpoints to target for manual GUI tuning as a fallback.

**Via stubs on HS through-vias.** All HS through-vias leave an unused stub on the opposite copper side — a resonant discontinuity at high frequency. For production: request back-drilling or use blind/buried vias. For a prototype first spin: acceptable; document in the fabrication notes in `[MANUFACTURING_DIR]/README_for_manufacturer.txt`.

**Step 4 — Verify.** Run DRC (no clearance violations from meanders). Re-run
`measure_all_diff_pairs.py` (if used in Step 1) and `trace_length_check.py`. Any remaining
Category B or dense-area failures: document as pending GUI tuning or return to Phase 10b.

**Outputs:** `[REPORTS_DIR]/length_matching_status.txt` (before / after per pair).

**Loop exit criteria:** DRC clean; every same-layer pair at design target (0.01 mm) or
documented with a WAIVER.

**WAIVER procedure (use only when physically impossible to reach 0.01 mm):** if
`insert_meanders_tight.py` has no valid segment to insert on after exhausting all
candidates, record the pair in `length_matching_status.txt` as:
`WAIVER: [PAIR_NAME] residual skew X.XXX mm — no clearance for meander insertion; requires user sign-off before Phase 13`.
Phase 12 checklist item for intra-pair skew will flag WAIVER entries as requiring
explicit user approval before manufacturing files are released.

**Manufacturing notes placeholder.** If `[MANUFACTURING_DIR]/README_for_manufacturer.txt`
does not yet exist, create it now with at minimum the impedance control block (from Phase
5 and Phase 6 stackup) and the via stub note (from this phase). Phase 13 fills in all
remaining fabrication instructions. Creating the file here prevents Phase 12 from failing
its checklist check on a file that does not yet exist.

```
[MANUFACTURING_DIR]/README_for_manufacturer.txt  (placeholder — Phase 13 fills remainder)

IMPEDANCE CONTROL:
  Layer: [HS_ROUTING_LAYER]
  Target: [impedance value] ohm differential ± 10%
  Trace W: [W mm], S: [S mm] (per compute_impedance.py output)
  Dielectric: confirm with fab before production

VIA STUB NOTE:
  All HS through-vias have an unused stub on the opposite copper side.
  For prototype: acceptable — document and monitor.
  For production: request back-drilling or specify blind/buried vias.
```

**SESSION_CONTEXT:** Phase 10d complete.

---

## PHASE 11 — Post-Autoroute Cleanup

**Purpose:** Complete any missed nets, add GND return vias at signal layer transitions,
verify low-speed bus quality, add silkscreen (connector labels only).

**Inputs:** `autoroute_log.txt`, PCB post-length-matching.

**Loops:** Loop A, Loop B.

**Known-acceptable DRC:** none. Zero unconnected and zero clearance violations required.

**Step 1 — Route any missed nets.** Read `autoroute_log.txt`; for each unrouted net,
write `[SCRIPTS_DIR]/route_missed.py`. Widths from Loop D. F.Cu preferred, B.Cu if no
F.Cu path.

**Step 2 — GND return vias for signal layer transitions.** Read
`Python Scripts/add_return_vias.py`, set its PROJECT CONFIG block (PCB_FILE,
REPORT_FILE, GND_NET_NAME, ADD_VIAS, LAYER_STACK_DESC), and run it.
`LAYER_STACK_DESC` format: ordered list of layer names from top to bottom with their
roles, e.g. `[("F.Cu","signal"), ("In1.Cu","GND"), ("In2.Cu","signal-HS"),
("In3.Cu","GND"), ("In4.Cu","power"), ("B.Cu","signal")]`. Derive from the Phase 6
stackup in PROJECT_PARAMS.

Return path rule: for every via on a non-GND signal net, the return current needs
a low-impedance path on every layer the signal passes through. If the via passes
through a solid GND plane, that plane provides the return path — no extra via needed.
If the via transitions between two layers where neither adjacent layer is a continuous
GND plane, add a GND stitching via within 2 mm. Run with ADD_VIAS = False first
to review the report before inserting vias.

**Step 3 — Low-speed bus quality check** (I2C, SPI, UART, etc.). Read
`Python Scripts/check_lowspeed_bus.py`, configure its PROJECT CONFIG block (PCB_FILE),
and run it. For each bus net in PROJECT_PARAMS: measures routed trace length; if > 10 mm,
verifies a GND guard trace exists on each side within 0.3 mm. Bus endpoint distances
were pre-screened in Phase 8 (`bus_endpoint_distance_report.txt`) — any bus flagged
GUARD REQUIRED there must pass this check.

**Step 3.5 — HS diff pair verification.** Read `Python Scripts/verify_highspeed.py`,
configure its PROJECT CONFIG block (PCB_FILE, REPORT_FILE, HS_LAYER_NAME, HS_PAIRS,
LANE_GROUPS), and run it. For every HS pair net:
- All segments on the HS routing layer only
- No vias mid-trace (vias only at endpoint pads)
- Intra-pair length match within budget
- Inter-lane length match within budget
- No GND zone split or keepout under any HS segment
- No stitching via piercing beneath an HS pair
- Pre-routed HS routing unchanged (no FreeRouting modifications)

**Step 4 — Silkscreen (connectors only).** Read `Python Scripts/add_silkscreen.py`,
configure its PROJECT CONFIG block (PCB_FILE), and run it. Enforces the silkscreen rule:

Part A: Hide all non-connector text.
```python
for fp in board.GetFootprints():
    if not fp.GetReference().startswith("J"):
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
```

Part B: Add connector purpose labels from PROJECT_PARAMS. Each label on F.Silkscreen
adjacent to its connector courtyard (outside, not overlapping pads), height >= 1.0 mm.

Part C: Board info on F.Fab (not silkscreen): project name / version / date code / RoHS.

Apply Loop B. Then Loop A to zero.

**Outputs:** `[REPORTS_DIR]/fix_log_phase11.txt`,
`[REPORTS_DIR]/highspeed_verification.txt`.

**Loop exit criteria:** Zero unconnected items, zero clearance violations, HS pairs
verified.

**SESSION_CONTEXT:** Phase 11 complete.

---

## PHASE 11.5 — Post-Routing Quality Checks

**Purpose:** Four DRC-invisible checks affecting manufacturability and reliability,
plus the mandatory DFM pass (Loop F).

**Inputs:** PCB post-Phase-11.

**Loops:** Loop B; Loop A after any copper change; Loop D (trace width — mandatory this phase); Loop F (DFM — mandatory this phase).

**Check 1 — Copper balance (warp prevention).** Read `Python Scripts/check_copper_balance.py`,
configure its PROJECT CONFIG block (PCB_FILE, REPORT_FILE, TARGET_LAYERS matching the
project stackup), and run it. It sums copper area per layer. If F.Cu/B.Cu ratio < 0.4:
add hatched or solid GND pour on the sparse side to reach 40:60.

**Check 2 — Polarity markers.** Confirmed in Phase 8 (`polarity_markers_report.txt`).
Re-run `Python Scripts/check_polarity_markers.py` only if any component was added,
rotated, or replaced after Phase 8.

**Check 3 — Conformal coating keepouts.** Completed in Phase 8 via `add_coating_notes.py`.
Verify F.Fab annotations are present. Re-run only if a connector was added or moved after
Phase 8.

**Check 4 — Panelization decision.** For prototype quantities, individual boards
usually beat panelization. Document decision in
`[MANUFACTURING_DIR]/manufacturing_notes.txt`. Revisit for production runs.

**Check 5 — Loop F (DFM).** Run `verify_dfm.py` (defined in Loop F). All eight
DFM checks must pass. Fix any violations before proceeding to Phase 12.

**Outputs:** `[REPORTS_DIR]/postrouting_checks.txt`, `[REPORTS_DIR]/dfm_report.txt`.

**Loop exit criteria:** All five checks documented; DRC clean; Loop D all pass; Loop F all pass.

**SESSION_CONTEXT:** Phase 11.5 complete.

---

## PHASE 12 — Final DRC + Compliance Checklist

**Purpose:** Zero DRC violations. Full compliance checklist sign-off.

**Inputs:** PROJECT_PARAMS compliance targets, PCB post-Phase 11.5.

**Loops:** Loop A to zero.

**Known-acceptable DRC:** none.

**Compliance checklist (adjust items per PROJECT_PARAMS):**

- [ ] Every rail clearance meets DRU rules for its voltage class
- [ ] All switching loops <= 50 mm² (Loop C)
- [ ] GND plane on every dedicated GND layer in the stackup covers full interior, no gaps
- [ ] Decoupling cap proximity — confirmed PASS in Phase 8 (`decoupling_proximity_report.txt`)
- [ ] Polarity markers — confirmed PASS in Phase 8 (`polarity_markers_report.txt`); re-run `check_polarity_markers.py` if any component was added, rotated, or replaced after Phase 8
- [ ] Every IC with exposed pad has thermal via count per Loop E
- [ ] All copper >= 0.5 mm from Edge.Cuts (DRU)
- [ ] Every net's trace width meets its current requirement (Loop D)
- [ ] No silkscreen overlap on copper pads
- [ ] All mounting holes present with keepout rings
- [ ] Board markings on F.Fab (name / version / date / RoHS / compliance mark)
- [ ] Courtyard clearances >= 0.25 mm (DRU)
- [ ] Zero unconnected items
- [ ] All low-speed buses within 10 mm or guarded
- [ ] *(HS only)* All HS pairs on `[HS_ROUTING_LAYER]` only, no vias mid-trace
- [ ] *(HS only)* Intra-pair skew <= 0.01 mm design target on every HS pair (Phase 10d) — pairs with WAIVER tag in `length_matching_status.txt` require explicit user sign-off here before proceeding to Phase 13
- [ ] *(HS only)* 3W clearance around every HS pair
- [ ] *(HS only)* No zone boundary on any reference plane adjacent to `[HS_ROUTING_LAYER]` crosses the HS channel (`verify_hs_sandwich.py` PASS)
- [ ] *(HS only)* Impedance control declaration present in `[MANUFACTURING_DIR]/README_for_manufacturer.txt`
- [ ] Loop F DFM all pass — confirmed in Phase 11.5 (`dfm_report.txt`); re-run only
      if copper or footprints changed after Phase 11.5: component-to-edge >= 3 mm,
      no via-in-pad, paste aperture <= 60% on EPs, no THT (unless approved),
      ENIG if pitch <= 0.5 mm

**Protection Component Final Audit:**
- [ ] Open CONNECTOR_PROTECTION_TABLE. Confirm row count matches external connector count in schematic.
- [ ] For each ✅ ESD cell: verify the ESD device footprint is placed ≤ 8 mm from the connector it protects (per PROXIMITY_RULES_TABLE threshold for esd_clamp).
- [ ] For each ✅ CM choke cell: verify CM choke footprint is placed between the connector and all downstream circuitry — nothing except the polyfuse is upstream of the choke.
- [ ] For each ✅ polyfuse cell: verify polyfuse is the first component on the power rail after the connector, before the CM choke.
- [ ] For each ✅ rev-pol cell: verify the P-FET or ideal diode controller is downstream of the polyfuse and choke.
- [ ] Confirm RoHS compliance declaration is available for every IC sourced outside Digi-Key/Mouser (e.g., LCSC-only parts). If not available, flag for procurement.
If any item is not met, treat as a DRC blocker — do not generate Gerbers until resolved.

For each FAIL: fix script, Loop B, Loop A, re-evaluate.

**Outputs:** `[REPORTS_DIR]/compliance_checklist.txt` (each item PASS with evidence or
FAIL with fix), `[REPORTS_DIR]/fix_log_phase12.txt`.

**Loop exit criteria:** DRC 0 violations, checklist all PASS.

**SESSION_CONTEXT:** Phase 12 complete, ready for manufacturing files.

---

## PRE-MANUFACTURING GATE

Before Phase 13, verify every item below. Manufacturing files generated from an
incomplete or non-clean design will produce boards that fail — and fab orders cannot
be cancelled once submitted. This gate is the last check before real money is spent.

- [ ] Phase 10d complete: `length_matching_status.txt` exists; all pairs PASS or have
      explicit WAIVER entries with user sign-off recorded in SESSION_CONTEXT
- [ ] Phase 11 complete: zero unconnected items; HS pairs verified; silkscreen applied
- [ ] Phase 11.5 complete: `dfm_report.txt` exists — all Loop F checks PASS;
      `postrouting_checks.txt` exists — all five checks documented; Loop D all PASS
- [ ] Phase 12 complete: DRC reports **zero violations** — not "zero new violations",
      zero total. Run a fresh DRC now and confirm the count.
- [ ] Compliance checklist (`compliance_checklist.txt`) all items PASS
- [ ] Any intra-pair skew WAIVERs in `length_matching_status.txt` have been explicitly
      approved by the user in this session before proceeding
- [ ] `[MANUFACTURING_DIR]/README_for_manufacturer.txt` exists (created in Phase 10d,
      content verified in Phase 12)
- [ ] `[REPORTS_DIR]/manufacturing_ready.txt` does not yet exist — if it does, this is
      a re-run; confirm the user intends to regenerate manufacturing files before continuing

Do not start Phase 13 if any item is unchecked. If DRC shows any violation, return to
the appropriate phase to resolve it — do not generate Gerbers over a non-clean board.

---

## PHASE 13 — Manufacturing File Generation

**Purpose:** Generate every file needed to order the board.

**Inputs:** PCB post-Phase 12, PROJECT_PARAMS fabrication preferences.

**Loops:** Loop B on any failed export; BOM availability loop.

**Process:** Confirm DRC still zero. Read `board_size_report.txt` for final
dimensions. Create `[MANUFACTURING_DIR]/Gerbers/` and `[MANUFACTURING_DIR]/Drills/`.

**Exports (each: check exit code, retry via Loop B):**
1. **Gerbers:** `kicad-cli pcb export gerbers --output Gerbers/ [PCB_FILE]`. Verify
   .gbr count matches layer expectations (copper + mask + silk + edge).
2. **Drill files:** `kicad-cli pcb export drill --output Drills/
   --excellon-separate-th [PCB_FILE]`. PTH and NPTH separate.
3. **Pick-and-place:** `kicad-cli pcb export pos --format csv --units mm`.
4. **BOM:** `kicad-cli sch export bom --output bom.csv [SCHEMATIC_FILE]`.
5. **Fab drawing PDF:** `kicad-cli pcb export pdf --layers
   "F.Cu,B.Cu,Edge.Cuts,F.Courtyard,F.Silkscreen"`.
6. **3D STEP:** `kicad-cli pcb export step`.

**BOM availability verification loop:** For every BOM line: confirm MPN present and
orderable. Flag missing/unverifiable MPNs. Add MPN to schematic Value or custom field,
re-run ERC, regenerate BOM. Stop when all lines have orderable MPNs.

**3D fit verification:** Write `[SCRIPTS_DIR]/check_component_heights.py`. Parse STEP
or 3D model attributes. Flag any component taller than `[MAX_COMPONENT_HEIGHT_MM] -
3 mm` clearance margin. Propose lower-profile alternatives without changing without
user approval.

**Stencil / paste mask verification.** Write
`[SCRIPTS_DIR]/verify_paste_mask.py`. For every QFN/DFN/WQFN exposed pad: check paste
aperture is a 5-window pattern covering ~50–60% of EP area, not full coverage.
Full-coverage paste on EP causes bridging and tombstoning. If full-coverage: write
fix script to split into windows.

Write `[MANUFACTURING_DIR]/stencil_spec.txt` with thickness (0.12 mm typical), aperture
pattern per IC, aperture reduction for fine-pitch pads (10% typical at 0.5 mm pitch).

**Impedance control declaration** (only if HS signals present). Write
`[MANUFACTURING_DIR]/README_for_manufacturer.txt`:
- Board name / version / dimensions / thickness / copper weight / layer count / stackup
- Min trace / min space / min via drill
- Surface finish (ENIG required for pitch <= 0.5 mm)
- Solder mask color / silkscreen color
- Substrate class (FR4 Tg >= 150C IPC-4101C/21)
- IPC class + E-test 100%
- Quantity
- Impedance control block per HS pair class: target ohm, tolerance, fab confirms
  dielectric distances, request TDR test coupon on panel edge

**Outputs:** all files in `[MANUFACTURING_DIR]`,
`[REPORTS_DIR]/manufacturing_ready.txt`.

**Loop exit criteria:** All exports succeed, DRC still zero, BOM lines all orderable.

**SESSION_CONTEXT:** Phase 13 complete, manufacturing package ready.

---

---

## POST-DESIGN PROCEDURES

*Phases 14–16 occur after manufacturing files are released. They are physical and
procedural steps, not design-loop phases. A DRC or compliance failure discovered here
requires re-entering the design loop at the appropriate phase.*

---

## PHASE 14 — First-Article Inspection Checklist

**Purpose:** Physical inspection checklist for bare PCBs arriving from the fabricator,
before assembly.

**Inputs:** `board_size_report.txt`, PROJECT_PARAMS.

**Process:** Write `[MANUFACTURING_DIR]/first_article_checklist.txt`:

**Dimensional inspection (calipers):**
- [ ] Board width at 3 points: [W] mm ± 0.2 mm
- [ ] Board height at 3 points: [H] mm ± 0.2 mm
- [ ] Thickness at 4 corners: [BOARD_THICKNESS_MM] ± 10%
- [ ] Mounting hole positions and diameter within tolerance
- [ ] Corners square, no delamination

**Visual (10× loupe):**
- [ ] Solder mask coverage / no bubbles / no bridging across QFN pads
- [ ] ENIG uniform / no dark spots (nickel corrosion) / no bare copper
- [ ] Silkscreen legible / no bleeding onto pads
- [ ] Edge.Cuts smooth, no delamination
- [ ] Via holes open / no unintended plugging
- [ ] Layer registration (hold to light — inner copper centered)

**Electrical (multimeter):**
- [ ] GND continuity: any GND pad to any mounting hole = 0 Ω
- [ ] Each power rail to GND: open (> 1 MΩ)
- [ ] F.Cu GND to B.Cu GND: 0 Ω (via stitching)

**QFN pad inspection (20–40× microscope):**
- [ ] Each fine-pitch IC's pads visible, no mask bridging
- [ ] EP paste windows in 5-window pattern (not full coverage)

**Stackup verification (if fab provides cross-section coupon):**
- [ ] Layer count matches
- [ ] Impedance test coupon (if requested): TDR within HS pair tolerance

**Accept/reject:** dimensional outside tolerance → REJECT; rail-to-GND < 1 MΩ →
REJECT; QFN mask bridging → REJECT; ENIG dark spots > 2% of pads → REJECT.

**Outputs:** `[MANUFACTURING_DIR]/first_article_checklist.txt`.

**Loop exit criteria:** N/A (physical checklist).

**SESSION_CONTEXT:** Phase 14 complete, checklist written.

---

## PHASE 15 — Power-Up Test Sequence

**Purpose:** Step-by-step first-power-up procedure for the assembled board. Limits
damage exposure from wrong values or solder bridges.

**Inputs:** PROJECT_PARAMS (power rails, ICs, I2C addresses, test points).

**Process:** Write `[MANUFACTURING_DIR]/power_up_test_procedure.txt`:

**Equipment:** current-limited bench supply, DMM, oscilloscope, cables per input
connectors, I2C adapter (if I2C-configured ICs).

**Pre-power visual check:**
- No solder bridges on QFN pads (microscope)
- Polarized components oriented per silkscreen
- No solder balls / debris
- All ICs seated flat

**Step-by-step (one step per rail, current-limited):**
For each power rail in PROJECT_PARAMS:
1. Set supply to expected input V, current limit at 10× quiescent estimate
2. Connect to input connector
3. Apply power; if current hits limit, STOP and check for shorts
4. Measure input rail on its test point: expected V ± tolerance
5. Measure downstream rail: expected V ± tolerance
6. Verify switching waveform on SW pin (oscilloscope, if switching converter)
7. If wrong: check EN pin, inductor continuity, feedback resistors

**Configuration steps (I2C etc.):** send required config sequences per PROJECT_PARAMS
(e.g. set output voltage registers), read back to verify.

**Full system draw:** with all rails active and no load: expected total draw. With
load: expected total draw.

**Pass criteria:** all rail voltages in tolerance; no IC hot to touch within 30 s;
exposed-pad ICs warm but not hot after 5 min.

**Fail actions:** hot IC → cut power immediately, inspect for bridges; missing rail →
check EN pins / inductor / feedback resistors.

**Outputs:** `[MANUFACTURING_DIR]/power_up_test_procedure.txt`.

**Loop exit criteria:** N/A (physical procedure).

**SESSION_CONTEXT:** Phase 15 complete.

---

## PHASE 16 — Update Session Context

**Purpose:** Record completed state for future sessions.

**Inputs:** All prior reports.

**Process:** Update `[PROJECT_DIR]/SESSION_CONTEXT.md`:
- Last phase completed and date
- DRC status: zero violations
- Layer stack chosen (with reference to `layer_analysis.txt`)
- Final board dimensions from `board_size_report.txt`
- Compliance checklist all PASS
- Loop C / D / E results
- Manufacturing files location
- Items needing manual GUI review (visual placement inspection, silkscreen visual check)
- Next steps

Update any parent-scope `SESSION_CONTEXT.md`.

**Directory audit (run after SESSION_CONTEXT update):**

1. **Verify standard subdirectories exist.** Confirm `Reports/`, `Backups/`,
   `Manufacturing/`, `Part Library/`, `Datasheets/` are present. Create any that are
   missing and note them in SESSION_CONTEXT.

2. **Verify phase outputs are present.** For each phase completed this session, confirm
   its declared outputs exist on disk. Flag any that are absent:

   | Phase | Key outputs to verify |
   |-------|-----------------------|
   | 0 | `PROJECT_PARAMS.md`, `SESSION_CONTEXT.md`, `.claude/CLAUDE.md` |
   | 1 | `component_status.txt` |
   | 1.5 | `Reports/library_retrieval.txt` |
   | 2 | `.kicad_pro`, `.kicad_sch`, `.kicad_pcb` |
   | 3 | `Reports/ERC_check.rpt`, `Reports/fix_log_phase3.txt` |
   | 4 | `Reports/footprint_audit.txt` |
   | 5 | `.kicad_dru`, `Reports/compliance_rules_summary.txt` |
   | 6 | `Reports/layer_analysis.txt` |
   | 7 | `Reports/board_size_report.txt` |
   | 12 | `Reports/DRC_check.rpt` |
   | 13 | `Manufacturing/` populated with Gerbers, drill files, BOM, CPL |
   | 15 | `Manufacturing/power_up_test_procedure.txt` |

3. **Sweep misplaced files from project root.** Apply the full sweep table from
   SESSION START PROTOCOL Step 2b: move backup files to `Backups/`, move report files
   to `Reports/`, leave `~*.lck` files in place. Record each move in the directory
   snapshot.

4. **Flag orphaned or unexpected files.** List any files in `[PROJECT_DIR]` root that
   are not `.kicad_pro`, `.kicad_sch`, `.kicad_pcb`, `.kicad_sym`, `.kicad_dru`,
   `sym-lib-table`, `fp-lib-table`, `PROJECT_PARAMS.md`, `SESSION_CONTEXT.md`, or
   known project-specific files. Do not delete them — flag for user awareness only.

5. **Append a directory snapshot to SESSION_CONTEXT.md:**
   ```
   ## Directory Snapshot — [DATE]
   Standard subdirs: [present / MISSING: list]
   Phase outputs verified: [list phases checked]
   Missing outputs: [list or "none"]
   Bak files moved: [list or "none"]
   Unexpected root files: [list or "none"]
   ```

**Outputs:** updated `SESSION_CONTEXT.md` files, directory snapshot appended.

**Loop exit criteria:** SESSION_CONTEXT updated and directory audit complete.

---

## Quick Reference: Validation Commands

```
# ERC
"C:/Program Files/KiCad/10.0/bin/kicad-cli.exe" sch erc \
  --output "[REPORTS_DIR]/ERC_check.rpt" [SCHEMATIC_FILE]

# DRC
"C:/Program Files/KiCad/10.0/bin/kicad-cli.exe" pcb drc \
  --output "[REPORTS_DIR]/DRC_check.rpt" [PCB_FILE]
```

Report naming: always increment within a phase. Never overwrite — iteration history is
useful for debugging.

---

## Standards Reference (cite in `README_for_manufacturer.txt` as applicable)

| Standard | Scope | Key Requirement |
|---|---|---|
| EN 62368-1 | Safety — AV/IT equipment | Clearance / creepage / thermal / fusing |
| EN 55032 | EMC emissions | Class B: 30 m radiated; conducted on power port |
| EN 55035 | EMC immunity | ESD; surge |
| IPC-2221B | PCB fabrication | Trace widths / via sizes / annular rings |
| IPC-7351B | Land patterns | Pad geometry / courtyard / tolerances |
| IPC-2141A | Controlled impedance | Trace w/s vs dielectric for 90/100 ohm diff |
| VESA DP 1.4 | DisplayPort | 100 ohm diff, HBR3 8.1 Gbps, AC coupling, skew |
| HDMI 2.1 | HDMI TMDS | 100 ohm diff, up to ~12 Gbps per lane |
| USB 3.2 | USB SuperSpeed | 85–90 ohm diff, 5–10 Gbps |
| RoHS 3 (2015/863/EU) | Hazardous materials | No Pb / Hg / Cd |
| IEC 61000-4-2 | ESD immunity | ±4 kV contact / ±8 kV air (USB port) |

---

## What Requires Manual KiCad GUI Work

Automate everything file-generatable. Three things still need eyes:
1. Visual placement check — open PCB after Phase 8, confirm visually
2. Switching trace visual check — after Phase 10, confirm loops are compact
3. Final order and compliance sign-off — upload to fab; sign Declaration of Conformity
   after board bring-up

---

## Layer Count Summary (illustrative — Phase 6 selects for the project)

**2-layer** — simple non-switching low-speed only
**4-layer** — mixed-signal, one switcher, no HS
**6-layer buried-pair** — multi-Gbps HS, multiple switchers:
```
F.Cu    — components + escape + slow signals
In1.Cu  — SOLID GND
In2.Cu  — HS differential pairs (buried stripline)
In3.Cu  — SOLID GND (shields HS from power)
In4.Cu  — power planes
B.Cu    — power switching traces + low-speed + GND fill
```
**8-layer** — DDR routing / multiple impedance domains / thermal planes

Phase 6 proves the choice; Phase 7 implements it.
