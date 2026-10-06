# PCB Design Review Guide — KiCad 10, Claude Code Workflow

**Purpose:** A structured design review workflow for *existing* KiCad 10 projects. An AI
agent uses this guide to audit a completed schematic and PCB, produce a set of structured
findings, and deliver a consolidated review report. No project files are modified by this
guide. Every action is read-only against the project under review.

This guide is a companion to `PCB_Design_Guide_Generic.md`. Where that guide describes
how to build a board, this guide describes how to verify one. Rule definitions, check
criteria, and pass/fail thresholds are not duplicated here — they are cited by phase and
check number from `PCB_Design_Guide_Generic.md` and applied in read/report mode.

---

## TOOL VERSION: KiCad 10

All work uses KiCad 10.0 installed at `C:/Program Files/KiCad/10.0/`. Every CLI command,
layer name, file format reference, and Python API pattern is specific to KiCad 10.
Do not apply conventions from KiCad 5/6/7.

Layer names, CLI path, and file format headers are as defined in `PCB_Design_Guide_Generic.md`
(TOOL VERSION section). When in doubt, refer to that section — do not re-derive them here.

---

## Purpose and Scope

This guide is for performing a **design review** of an existing KiCad 10 project — one
where the schematic and PCB layout are already complete (or substantially complete) and
the goal is to assess quality, correctness, and readiness for fabrication.

**Difference from the main design guide.** `PCB_Design_Guide_Generic.md` is a
design-forward workflow: it creates, modifies, and progressively builds a project from
zero. This guide is a read-forward workflow: it opens existing files, runs analysis
scripts in report mode, and documents what it finds. The review agent never writes to
`[SCHEMATIC_FILE]` or `[PCB_FILE]`. All output goes to `[REVIEW_DIR]`.

**When to use this guide:**
- **Pre-production gate** — before submitting Gerbers to a fab house, confirm no
  CRITICAL or un-waivered MAJOR findings remain
- **Peer review** — a second agent (or human reviewer) audits a design produced by
  a different agent or engineer
- **Post-spin audit** — after receiving bare boards or assembled units, retroactively
  confirm the design matched intent; identify what to fix in the next revision
- **Supplier qualification** — a contract manufacturer or EMS house requests a formal
  design review package before accepting a build

**What this guide does not do.** It does not fix anything. It does not modify any project
file. It does not propose alternative component values, reroute traces, or adjust
placement. Every finding is documented for the designer to act on — either by re-running
the main guide's relevant phase, or by raising a waiver with documented rationale.

---

## Inputs Required

Before starting any review phase, confirm all inputs are available and record their
resolved paths. If a required input is absent, stop and report — do not begin review
with incomplete inputs.

```
[SCHEMATIC_FILE]       Absolute path to the existing .kicad_sch file
                       e.g. C:/Projects/Widget_v1/Widget_v1.kicad_sch

[PCB_FILE]             Absolute path to the existing .kicad_pcb file
                       e.g. C:/Projects/Widget_v1/Widget_v1.kicad_pcb

[PROJECT_PARAMS_FILE]  Absolute path to the project's PROJECT_PARAMS.md, if present.
                       Optional — checks that require it degrade gracefully when absent
                       (see CANNOT EVALUATE rules per phase). If absent, record:
                       "PROJECT_PARAMS not provided — some checks will report CANNOT EVALUATE"

[REVIEW_DIR]           Absolute path to the output directory for this review session.
                       Must NOT be inside the project's own directory tree (to avoid
                       contaminating the project's Reports/ folder or triggering
                       inadvertent KiCad project re-scans).
                       e.g. C:/Reviews/Widget_v1_Review_2026-07-18/
                       Create this directory before running any phase.

[DESIGN_GUIDE_DIR]     Absolute path to the Python Scripts/ directory from the main
                       guide's project. Required to run shared analysis scripts.
                       e.g. C:/Projects/Widget_v1/Python Scripts/
                       If absent: scripts that depend on it cannot run; report each
                       affected check as CANNOT EVALUATE — DESIGN_GUIDE_DIR not provided.

[PROJECT_NAME]         The project name string (no spaces), derived from [SCHEMATIC_FILE]
                       stem if not explicitly provided.
                       e.g. "Widget_v1"

[REVIEW_DATE]          ISO date of this review session: YYYY-MM-DD

[REVIEWER]             "Claude" plus the model ID string returned by the active session.
```

**Session integrity step (run before input validation):**

If `[PROJECT_DIR]/.claude/CLAUDE.md` exists, read it in full before proceeding. It
contains the project's invariant rules and may contain an active Detour Bookmark. If a
`DETOUR ACTIVE` bookmark is present, report it to the user and ask whether to resume
the interrupted workflow first or proceed with the review anyway.

If the review session is interrupted mid-phase and later resumed (whether in the same
session or a new one), apply the DETOUR PROTOCOL from `PCB_Design_Guide_Generic.md`:
re-read the project's `.claude/CLAUDE.md` and the interrupted review phase section
before taking any further action. Do not continue from memory alone.

---

**Input validation step (run before Phase 1):**
1. Confirm `[SCHEMATIC_FILE]` exists and is readable.
2. Confirm `[PCB_FILE]` exists and is readable.
3. Create `[REVIEW_DIR]` if it does not yet exist.
4. Confirm `[DESIGN_GUIDE_DIR]` exists and contains the expected scripts
   (`verify_dfm.py`, `width_audit.py`, etc.). Record which scripts are present.
5. If `[PROJECT_PARAMS_FILE]` is provided, confirm it is readable and extract:
   `[PROJECT_NAME]`, `MCU_REF`, `POWER_NETS`, `HS_ROUTING_LAYER`, `THERMAL BUDGET`,
   `HIGH-SPEED SIGNAL INVENTORY`, `SIGNAL_FLOW_ORDER`. Record any missing fields as
   CANNOT EVALUATE for the checks that depend on them.
6. Write `[REVIEW_DIR]/review_inputs.txt` confirming each input status:
   `PRESENT | ABSENT | CANNOT EVALUATE` per item.

---

## Review Conventions

### Finding Severity Levels

| Severity | Meaning |
|---|---|
| **CRITICAL** | Would cause board failure, safety hazard, or non-functional unit. Must be resolved before fabrication. |
| **MAJOR** | Violates a design rule from `PCB_Design_Guide_Generic.md` or an applicable IPC/compliance standard. Likely causes functional failure or compliance failure. Should be resolved before fabrication; may be waivered with documented rationale. |
| **MINOR** | Best-practice deviation with low functional risk. Recommended correction; does not block fabrication. |
| **OBSERVATION** | Informational note. No action required. Provided for the designer's situational awareness. |

### Finding Format

Every finding is written as a single self-contained record:

```
[SEVERITY] <REF or NET or LOCATION> — <description of what is wrong>
  Evidence: <exact value, distance, net name, script output line, or DRC message quoted>
  Rule: per PCB_Design_Guide_Generic.md Phase <N> <CHECK or rule name>
  Recommended action: <specific corrective action>
```

Example:
```
[MAJOR] U3 / VDD net — decoupling capacitor C14 is 2.3 mm from U3 VDD pad (pin 4)
  Evidence: check_decoupling_proximity.py: "C14 distance to U3.pin4 = 2.30 mm (limit 1.00 mm for MCU)"
  Rule: per PCB_Design_Guide_Generic.md Phase 8 decoupling proximity rule (MCU 1 mm limit)
  Recommended action: Move C14 closer to U3 pin 4; re-run check_decoupling_proximity.py
```

### Phase Pass Criteria

A review phase is recorded as **PASS** if it produces zero CRITICAL and zero open
(un-waivered) MAJOR findings. A phase with only MINOR or OBSERVATION findings is PASS.

A phase is recorded as **FAIL** if it produces one or more CRITICAL findings, or one or
more MAJOR findings that have not been explicitly waivered.

### Waivers

A waiver is a finding the designer has explicitly accepted. A waiver must include:
- The finding record (full text as above)
- **Waiver rationale:** why the finding is acceptable for this specific project
- **Designer acknowledgment:** name and date
- **Risk accepted:** what could go wrong if the finding is not corrected

Waivers are recorded in `[REVIEW_DIR]/waiver_register.txt` and referenced by finding ID
in the phase report. A finding with a valid waiver is reported as **WAIVER** in the phase
summary, not FAIL.

### Script Invocation Convention

All scripts from `[DESIGN_GUIDE_DIR]` are invoked with `[PCB_FILE]` or
`[SCHEMATIC_FILE]` as input and `[REVIEW_DIR]/<output_file>` as the report destination.
**No script writes to the project directory.** Before invoking each script, confirm it
accepts a report-mode flag or confirm that it is inherently read-only (i.e., it reads
the board and writes only to its configured REPORT_FILE, with no `board.Save()` or
file-write to the project path).

```python
# Pattern for invoking a shared script in review mode:
# 1. Copy or import the script; override its PROJECT CONFIG block variables:
PCB_FILE    = r"[PCB_FILE]"                   # read-only source
REPORT_FILE = r"[REVIEW_DIR]/<phase_report>"  # review output, not project output
# 2. Confirm the script does not call board.Save() or write to [PROJECT_DIR].
# 3. Run. Capture stdout + return code. Append to phase report.
```

---

## REVIEW PHASE 1 — Project Completeness Check

**Purpose:** Verify the project files are in a reviewable state and establish a baseline
finding count before any substantive checks begin. If the project fails to load or
produces tool errors at this stage, further review phases cannot be trusted.

**Inputs:** `[SCHEMATIC_FILE]`, `[PCB_FILE]`, KiCad 10 CLI.

**Output:** `[REVIEW_DIR]/DR1_completeness.txt`

**Gate:** If either file fails to load (Steps 1 or 2), stop the review immediately.
Write `[REVIEW_DIR]/DR1_completeness.txt` with the CRITICAL blocker and halt. Do not
proceed to Phase 2.

---

**Step 1 — Schematic load test.**

Export the netlist using `kicad-cli` as a non-destructive load test:

```
"C:/Program Files/KiCad/10.0/bin/kicad-cli.exe" sch export netlist \
  --output "[REVIEW_DIR]/netlist_loadtest.xml" \
  "[SCHEMATIC_FILE]"
```

If exit code is non-zero or the output file is not created: record
`[CRITICAL] SCHEMATIC — schematic fails to load or export; kicad-cli exit non-zero`.
Halt review.

If exit code 0: record `PASS — schematic loads and exports netlist successfully`.
Note the KiCad format version from the first line of `[SCHEMATIC_FILE]`
(`(kicad_sch (version XXXXXXXX)`).

**Step 2 — PCB load test.**

Verify the PCB file parses without error using the pcbnew Python module:

```python
import sys
sys.path.insert(0, "C:/Program Files/KiCad/10.0/bin/Lib/site-packages")
import pcbnew

board = pcbnew.LoadBoard(r"[PCB_FILE]")
print(f"Footprints: {len(board.GetFootprints())}")
print(f"Nets: {board.GetNetCount()}")
print(f"Zones: {len(board.Zones())}")
```

Write this as `[REVIEW_DIR]/pcb_loadtest.py` and run it. If it raises an exception:
record `[CRITICAL] PCB — PCB fails to load via pcbnew`. Halt review.

If it succeeds: record footprint count, net count, and zone count in the phase report.
Note the KiCad format version from the first line of `[PCB_FILE]`
(`(kicad_pcb (version XXXXXXXX)`).

**Step 3 — ERC on schematic.**

Run ERC and save the report to `[REVIEW_DIR]`:

```
"C:/Program Files/KiCad/10.0/bin/kicad-cli.exe" sch erc \
  --output "[REVIEW_DIR]/ERC_DR1.rpt" \
  --exit-code-violations \
  "[SCHEMATIC_FILE]"
```

Read `[REVIEW_DIR]/ERC_DR1.rpt`. Count violations by severity (Error / Warning).
Record: `ERC: <N> Errors, <M> Warnings`. Do not suppress or classify violations here —
that is done in Review Phase 2. Record the raw count only.

**Step 4 — DRC on PCB.**

Run DRC and save the report to `[REVIEW_DIR]`:

```
"C:/Program Files/KiCad/10.0/bin/kicad-cli.exe" pcb drc \
  --output "[REVIEW_DIR]/DRC_DR1.rpt" \
  --exit-code-violations \
  --schematic-parity \
  "[PCB_FILE]"
```

Read `[REVIEW_DIR]/DRC_DR1.rpt`. Count violations by category:
- Clearance violations
- Unconnected nets
- Footprint errors
- Schematic parity errors

Record counts. Do not classify yet — substantive DRC analysis is done per phase.

**Step 5 — Design rules file check.**

Search the directory containing `[PCB_FILE]` for a file named
`[PROJECT_NAME].kicad_dru`. If absent: record
`[MAJOR] DRU — no project design rules file found; DRC is running with KiCad
defaults only, not project-specific impedance or clearance rules`.

If present: confirm it loads without parse error by including `--rules [PROJECT_NAME].kicad_dru`
in the DRC call above. Record: `DRU present — [PROJECT_NAME].kicad_dru`.

**Step 6 — Phase 1 report.**

Write `[REVIEW_DIR]/DR1_completeness.txt` with:
```
PROJECT:         [PROJECT_NAME]
REVIEW DATE:     [REVIEW_DATE]
REVIEWER:        [REVIEWER]

SCHEMATIC FILE:  [SCHEMATIC_FILE]
  Format version: (kicad_sch version XXXXXXXX)
  Load test:      PASS / CRITICAL-FAIL

PCB FILE:        [PCB_FILE]
  Format version: (kicad_pcb version XXXXXXXX)
  Load test:      PASS / CRITICAL-FAIL
  Footprints:     N
  Nets:           N
  Zones:          N

DRU FILE:        PRESENT / ABSENT

ERC BASELINE:    N Errors, M Warnings
DRC BASELINE:
  Clearance violations:    N
  Unconnected nets:        N
  Footprint errors:        N
  Schematic parity errors: N

PHASE 1 VERDICT: PASS / FAIL
FINDINGS:
  [list all findings with severity]
```

---

## REVIEW PHASE 2 — Schematic Review

**Purpose:** Verify schematic electrical correctness by running through the Phase 3
checklist from `PCB_Design_Guide_Generic.md`. Each check is evaluated in read-only
mode: assess the schematic as-is and report findings; do not modify anything.

**Inputs:** `[SCHEMATIC_FILE]`, `[PROJECT_PARAMS_FILE]` (if available),
`[REVIEW_DIR]/netlist_loadtest.xml` (from Phase 1), `[REVIEW_DIR]/ERC_DR1.rpt`.

**Output:** `[REVIEW_DIR]/DR2_schematic_review.txt`

---

**Step 1 — ERC violation classification.**

Read `[REVIEW_DIR]/ERC_DR1.rpt`. For each ERC violation:
- Classify as CRITICAL, MAJOR, MINOR, or OBSERVATION using the severity definitions above.
- Quote the exact ERC message as evidence.
- "Pin unconnected" on a pin not marked NC in schematic: **MAJOR**.
- "Net has no driving source": **MAJOR**.
- "PWR_FLAG missing": **MAJOR** (per `PCB_Design_Guide_Generic.md` Phase 2 PWR_FLAG requirement).
- "Pin type conflict": **MAJOR** unless the combination is a known-acceptable pull-up/open-drain pattern.
- Warnings on NC-marked pins: **OBSERVATION**.

**Step 2 — Schematic CHECKs 1–13.**

Run each check from `PCB_Design_Guide_Generic.md` Phase 3 in evaluation-only mode.
For each check, record: check name, finding (PASS / FAIL / WAIVER / CANNOT EVALUATE),
evidence, and recommended action if FAIL.

- **CHECK 1: Regulator output voltage** — Read feedback divider resistor values from
  schematic. Apply the datasheet formula for each regulator in PROJECT_PARAMS. Compare
  computed V_out to target rail voltage. FAIL if deviation exceeds ±2%. If
  PROJECT_PARAMS absent: CANNOT EVALUATE — power rail table not provided.

- **CHECK 2: Overvoltage / overcurrent thresholds** — Locate OVP/OCP setting resistors
  for each converter with programmable thresholds. Compute threshold from divider.
  Report computed value and compare to intended threshold. FAIL if threshold is outside
  ±5% of intended value or if setting pins are floating.

- **CHECK 3: I2C / control interfaces** — For every I2C bus net (SDA, SCL): confirm
  pull-up resistors present. Expected: 4.7 kΩ to VDDIO per bus. Confirm all I2C device
  addresses are unique (check address pin strapping). FAIL if pull-ups absent. MAJOR
  per missing pull-up.

- **CHECK 4: ESD protection on exposed I/O** — For each connector in the netlist: verify
  a TVS or ESD protection IC symbol appears between the connector pin net and GND.
  FAIL if any connector signal pin (non-GND, non-PWR) has no ESD device. MAJOR per
  unprotected connector signal pin.

- **CHECK 5: Reverse polarity protection** — For DC power input connectors: verify a
  P-channel MOSFET ideal diode, Schottky series device, or equivalent protection symbol
  is present on the positive rail between the connector and the rest of the circuit.
  FAIL if absent. CRITICAL — reverse polarity can destroy the board.

- **CHECK 6: Enable pin states** — Read every IC EN / SHDN / RESET / PWM_EN pin in the
  netlist. Flag any such pin that: (a) is connected to a net with no driving symbol
  (no pull resistor, no GPIO, no logic output), or (b) appears in ERC as "pin
  unconnected." FAIL per floating enable pin. MAJOR.

- **CHECK 7: Test points** — Verify a `TestPoint` symbol is present on each power rail
  net and on GND. If PROJECT_PARAMS lists critical signal nets (SDA, SCL, SW nodes):
  verify test points on those nets. MINOR per missing test point (low assembly impact
  but degrades bring-up ability).

- **CHECK 8: Bulk input capacitance** — For each switching converter: compute
  `C_in_min = I_load / (f_sw × dV_ripple)` using values from PROJECT_PARAMS or the IC
  datasheet. Sum capacitor values on the VIN net within the converter sub-circuit.
  FAIL if total C_in < C_in_min. MAJOR. If PROJECT_PARAMS absent: CANNOT EVALUATE.

- **CHECK 9: AC coupling on HS pairs** — If HIGH-SPEED SIGNAL INVENTORY is present in
  PROJECT_PARAMS: for each differential TX pair, verify AC coupling capacitors are
  present on the correct side (TX output, not RX input, unless RX datasheet requires
  them). Verify no pair is double-coupled. FAIL if double-coupled or required caps
  absent. MAJOR per violation.

- **CHECK 10: HS control / configuration strapping** — For each HS mux, retimer, or
  bridge IC: read every SEL, OE, MODE, ADDR pin in the netlist. Flag any pin not driven
  and not pull-resistored. FAIL per floating HS control pin. MAJOR.

- **CHECK 11: MCU decoupling** — Locate the MCU reference from PROJECT_PARAMS (or
  identify the MCU from the netlist if PROJECT_PARAMS absent). For each VDD/VDDA pin:
  verify at least one 100 nF capacitor on the same net within the sub-circuit. Verify
  VDDA has a ferrite bead or 10 Ω series resistor from VDD. FAIL if decoupling absent
  on any supply pin. MAJOR per missing cap.

- **CHECK 12: Level shifting on cross-domain signals** — Identify signals that cross
  voltage domains (use PROJECT_PARAMS POWER RAIL TABLE, or infer from net names and
  connected IC supply pins). For each cross-domain signal: verify a level shifter,
  resistor divider, or compatible open-drain pull configuration is present. FAIL per
  unprotected cross-domain signal. MAJOR.

- **CHECK 13: Grounding topology** — Verify the GND net is a single net (no split GND
  nets unless intentional star-ground is documented in PROJECT_PARAMS). If analog and
  digital GND are split, confirm a documented single tie point exists. OBSERVATION if
  split GND appears without PROJECT_PARAMS confirmation of intent.

**Step 3 — Additional schematic checks (not in Phase 3 but relevant for review).**

- **Differential pair completeness** — For every net name ending in `_P` or `_N`: verify
  the complementary net exists and both are assigned to the same net class. MAJOR if
  one leg is missing.

- **HS_PAIRS coverage** — For every connected `_P`/`_N` net pair (net name does not start
  with `unconnected-(`) that is not in `FANOUT_VIA_SKIP_NETS`: verify the pair appears in
  `HS_PAIRS` in `routing_config.py`, or is documented in a comment there with explicit
  rationale for exclusion. MAJOR per uncovered pair — an omitted pair will be routed at
  signal trace width instead of impedance-controlled width, and `route_highspeed.py` will
  skip it entirely. Use the completeness check script from the PRE-ROUTING GATE in
  `PCB_Design_Guide_Generic.md` to enumerate any missing pairs programmatically.

- **Switching converter application circuit completeness** — For each switching converter:
  verify bootstrap capacitor (if required by datasheet), feedback network, compensation
  network (if external), and soft-start component (if present in datasheet) are all
  present in the schematic. MAJOR per missing mandatory application component.

- **GPIO assignment uniqueness** — If GPIO_TABLE is present in PROJECT_PARAMS: verify no
  GPIO number is assigned to two different functions. MAJOR per conflict. If absent:
  CANNOT EVALUATE.

- **Configuration / strapping pin states** — For every IC with ADDRESS or MODE strapping
  pins: confirm each pin is driven to a defined logic level (pulled high, pulled low, or
  connected to a logic driver). Any pin left floating that is not explicitly NC per the
  datasheet: MAJOR.

**Step 4 — Write phase report.**

Write `[REVIEW_DIR]/DR2_schematic_review.txt` with:
```
PHASE 2 — SCHEMATIC REVIEW
Date: [REVIEW_DATE]

ERC CLASSIFICATION:
  [list each ERC finding with assigned severity]

PHASE 3 CHECKS:
  CHECK 1:  [PASS/FAIL/CANNOT EVALUATE] — [evidence]
  CHECK 2:  [PASS/FAIL/CANNOT EVALUATE] — [evidence]
  ...
  CHECK 13: [PASS/FAIL/CANNOT EVALUATE] — [evidence]

ADDITIONAL CHECKS:
  [list additional checks with findings]

FINDING SUMMARY:
  CRITICAL: N
  MAJOR:    N
  MINOR:    N
  OBSERVATION: N

PHASE 2 VERDICT: PASS / FAIL
```

---

## REVIEW PHASE 3 — Footprint and Library Audit

**Purpose:** Verify that every component's footprint is present, correct for its
package, and properly annotated. Maps to `PCB_Design_Guide_Generic.md` Phase 4.

**Inputs:** `[PCB_FILE]`, `[PROJECT_PARAMS_FILE]` (if available),
`[DESIGN_GUIDE_DIR]/check_polarity_markers.py` (if available).

**Output:** `[REVIEW_DIR]/DR3_footprint_audit.txt`

---

**Step 1 — Courtyard presence check.**

Load the PCB via pcbnew. For every footprint: check whether any pad or graphic on layer
`F.Courtyard` or `B.Courtyard` belongs to that footprint's courtyard. A footprint with
zero courtyard geometry cannot participate in DRC courtyard spacing checks.

```python
for fp in board.GetFootprints():
    has_courtyard = any(
        item.GetLayer() in (board.GetLayerID("F.Courtyard"), board.GetLayerID("B.Courtyard"))
        for item in fp.GraphicalItems()
    )
    if not has_courtyard:
        print(f"[MAJOR] {fp.GetReference()} — no courtyard geometry")
```

MAJOR per footprint with no courtyard.

**Step 2 — Polarity marker check.**

Run `check_polarity_markers.py` from `[DESIGN_GUIDE_DIR]` in report mode (confirm
it does not write to `[PCB_FILE]` before invoking):

```python
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REVIEW_DIR]/polarity_markers.txt"
```

Per `PCB_Design_Guide_Generic.md` Phase 8 polarity check: every polarized component
(capacitors, diodes, LEDs, connectors with a pin 1) must have a pin-1 dot, cathode
bar, or `+` marker on `F.Silkscreen` within 3 mm of the component origin. MAJOR per
missing marker.

If `check_polarity_markers.py` is absent from `[DESIGN_GUIDE_DIR]`: perform a manual
check by reading `F.Silkscreen` graphics near each polarized footprint from the PCB
S-expression. Report CANNOT EVALUATE if neither method is available.

**Step 3 — Package-to-footprint name cross-check.**

If PROJECT_PARAMS is available and lists MPN and package for each component: for each
IC reference, compare the footprint name in the PCB (`fp.GetFPID().GetFootprintName()`)
against the expected package string from PROJECT_PARAMS.

Flag any case where the footprint name contains a different package family:
- Expected `SOT-23`, footprint name contains `TO-252`: MAJOR
- Expected `QFN-32`, footprint name contains `QFN-48`: MAJOR
- Package families match but pin count differs: MAJOR

If PROJECT_PARAMS is absent: report CANNOT EVALUATE for this step — no reference
package data to compare against.

**Step 4 — Custom footprint layer completeness.**

For every footprint that is not from the standard KiCad library (i.e., sourced from
`[CUSTOM_LIBS_DIR]` or a project-local `.pretty`): verify all four mandatory layers
are populated:
- `F.Courtyard` or `B.Courtyard`: at least one closed rectangle or polygon
- `F.Silkscreen` or `B.Silkscreen`: at least one reference text or pin-1 marker
- `F.Fab` or `B.Fab`: at least one component outline graphic
- At least one copper pad (no footprint should exist with zero pads)

MAJOR per missing mandatory layer on a custom footprint.

**Step 5 — Write phase report.**

Write `[REVIEW_DIR]/DR3_footprint_audit.txt` listing every finding with the standard
finding format. Include a summary table:

```
PHASE 3 — FOOTPRINT AND LIBRARY AUDIT
Date: [REVIEW_DATE]

COURTYARD CHECK:
  [list any footprints missing courtyard]

POLARITY MARKER CHECK:
  [list any polarized components missing markers]

PACKAGE CROSS-CHECK:
  [list any footprint/package mismatches or CANNOT EVALUATE]

CUSTOM FOOTPRINT LAYER CHECK:
  [list any custom footprints with missing layers]

FINDING SUMMARY:
  CRITICAL: N
  MAJOR:    N
  MINOR:    N
  OBSERVATION: N

PHASE 3 VERDICT: PASS / FAIL
```

---

## REVIEW PHASE 4 — Placement Review

**Purpose:** Verify component placement meets the rules from
`PCB_Design_Guide_Generic.md` Phase 8.

**Inputs:** `[PCB_FILE]`, `[PROJECT_PARAMS_FILE]` (if available),
scripts from `[DESIGN_GUIDE_DIR]`:
`check_decoupling_proximity.py`, `check_bus_endpoint_distance.py`,
`verify_dfm.py`, `verify_thermal.py`.

**Output:** `[REVIEW_DIR]/DR4_placement_review.txt`

---

**Step 1 — Decoupling proximity.**

Run `check_decoupling_proximity.py` in report mode:

```python
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REVIEW_DIR]/decoupling_proximity.txt"
# Confirm MCU_REF is set from PROJECT_PARAMS before running.
# MCU_REF determines whether the 1 mm (MCU) or 1.5 mm (other ICs) limit applies.
```

Per `PCB_Design_Guide_Generic.md` Phase 8 decoupling proximity rule:
any decoupling capacitor more than 1.5 mm from its parent IC power pad is a MAJOR
finding. For the MCU (identified by `MCU_REF` in PROJECT_PARAMS), the limit is 1.0 mm.
MAJOR per violation. If PROJECT_PARAMS absent: use 1.5 mm limit for all ICs and note
that MCU-specific limit could not be applied.

**Step 2 — Bus endpoint distance.**

Run `check_bus_endpoint_distance.py` in report mode:

```python
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REVIEW_DIR]/bus_endpoint_distance.txt"
```

Per `PCB_Design_Guide_Generic.md` Phase 11 routing rules: any bus segment longer than
10 mm without documented guard traces is a MAJOR finding. If guard traces are present:
MINOR (best-practice deviation, low risk). Report the measured distance and presence
or absence of guard traces for each flagged bus.

If `check_bus_endpoint_distance.py` is absent: report CANNOT EVALUATE.

**Step 3 — Courtyard clearance (DRC).**

The DRC run from Phase 1 (`[REVIEW_DIR]/DRC_DR1.rpt`) contains courtyard violations.
Read and extract all courtyard-category violations. MAJOR per courtyard overlap.

**Step 4 — Component-to-edge clearance.**

Run `verify_dfm.py` items (a) and (b) only (component-to-edge and courtyard gap).
Override the report output path:

```python
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REVIEW_DIR]/dfm_placement.txt"
# Run only checks (a) and (b) — component-to-edge >= 3 mm, courtyard gap >= 0.25 mm.
# Do not run other dfm checks here; they belong in Phase 6.
```

Per `PCB_Design_Guide_Generic.md` Loop F items (a) and (b):
- Any courtyard within 3 mm of `Edge.Cuts`: MAJOR
- Any component-to-component courtyard gap < 0.25 mm: MAJOR

If `verify_dfm.py` is absent: report CANNOT EVALUATE and manually check a sample of
edge-adjacent footprints.

**Step 5 — Thermal via count.**

Run `verify_thermal.py` in report mode:

```python
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REVIEW_DIR]/thermal_report.txt"
# Configure THERMAL_TABLE from PROJECT_PARAMS THERMAL BUDGET section.
# If PROJECT_PARAMS absent: CANNOT EVALUATE.
```

Per `PCB_Design_Guide_Generic.md` Loop E: for every IC dissipating > 0.3 W, compute
required θ_ja and compare against achieved θ_via. MAJOR per IC with insufficient
thermal via count for its rated dissipation. OBSERVATION if dissipation is < 0.3 W
and no thermal vias are present.

If PROJECT_PARAMS THERMAL BUDGET is absent: report CANNOT EVALUATE for all thermal
checks.

**Step 6 — Signal flow order.**

If PROJECT_PARAMS contains `SIGNAL_FLOW_ORDER` (ordered list of Zone A IC refs,
input-to-output): read the X/Y centroid of each footprint from the PCB and verify the
refs appear in the correct spatial order (left-to-right or top-to-bottom without
backtracking). An IC out of order relative to its neighbors creates a backtracking
signal path. OBSERVATION per out-of-order IC (signal flow is a quality metric, not a
functional failure). If PROJECT_PARAMS absent: CANNOT EVALUATE.

**Step 7 — Write phase report.**

Write `[REVIEW_DIR]/DR4_placement_review.txt`:

```
PHASE 4 — PLACEMENT REVIEW
Date: [REVIEW_DATE]

DECOUPLING PROXIMITY:
  [list findings or "All caps within limits"]

BUS ENDPOINT DISTANCE:
  [list findings or CANNOT EVALUATE]

COURTYARD CLEARANCE:
  [list DRC courtyard violations from DR1]

COMPONENT-TO-EDGE CLEARANCE:
  [list findings or CANNOT EVALUATE]

THERMAL VIA COUNT:
  [list findings or CANNOT EVALUATE]

SIGNAL FLOW ORDER:
  [list findings or CANNOT EVALUATE]

FINDING SUMMARY:
  CRITICAL: N
  MAJOR:    N
  MINOR:    N
  OBSERVATION: N

PHASE 4 VERDICT: PASS / FAIL
```

---

## REVIEW PHASE 5 — Routing Review

**Purpose:** Verify routing completeness, trace widths, high-speed signal integrity,
return current paths, and switching loop areas. Maps to
`PCB_Design_Guide_Generic.md` Phases 10, 10d, and 11.

**Inputs:** `[PCB_FILE]`, `[PROJECT_PARAMS_FILE]` (if available),
`[REVIEW_DIR]/DRC_DR1.rpt`, scripts from `[DESIGN_GUIDE_DIR]`:
`width_audit.py`, `verify_highspeed.py`, `measure_all_diff_pairs.py`,
`add_return_vias.py`, `verify_hs_sandwich.py`, `measure_loops.py`.

**Output:** `[REVIEW_DIR]/DR5_routing_review.txt`

---

**Step 1 — Unconnected nets.**

Read `[REVIEW_DIR]/DRC_DR1.rpt`. Extract any violation in the "Unconnected items"
category. Each unconnected net is a **CRITICAL** finding — the board will not function
with an open net. List every unconnected net, its expected source and destination
footprint references, and the net name.

**Step 2 — Trace width audit.**

Run `width_audit.py` in report mode:

```python
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REVIEW_DIR]/trace_width_report.txt"
# Configure POWER_NETS from PROJECT_PARAMS POWER RAIL TABLE (net names + max currents).
# If PROJECT_PARAMS absent: attempt to infer power nets from net names (VBATT, VCC,
# VDD, GND, VSYS variants). Report any net that could not be evaluated as CANNOT EVALUATE.
```

Per `PCB_Design_Guide_Generic.md` Loop D IPC-2221B width table:
- Any power net trunk segment below its current-rated minimum width: MAJOR
- Any power net where `width_audit.py` reports zero qualifying segments: cross-check
  with `verify_trace_widths.py` to confirm no under-width trunk segments
- Any signal trace narrower than 0.1 mm on internal layers (fabrication limit): MAJOR

**Step 3 — High-speed pair routing verification.**

If HIGH-SPEED SIGNAL INVENTORY is present in PROJECT_PARAMS (or HS pairs can be
identified from net names with `_P`/`_N` suffixes):

Run `verify_highspeed.py` in report mode:

```python
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REVIEW_DIR]/hs_routing_report.txt"
# Set HS_ROUTING_LAYER from PROJECT_PARAMS (typically "In2.Cu").
```

Per `PCB_Design_Guide_Generic.md` Phase 10 HS routing rules:
- Any HS segment not on `[HS_ROUTING_LAYER]`: MAJOR
- Any via mid-trace on a HS net: MAJOR (discontinuity in reference plane)
- 3W rule violation (HS pair to adjacent copper < 3× trace width): MAJOR

If HIGH-SPEED SIGNAL INVENTORY absent: report CANNOT EVALUATE — HS net list not
provided.

**Step 4 — Intra-pair skew.**

Run `measure_all_diff_pairs.py` in report mode:

```python
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REVIEW_DIR]/diff_pair_skew.txt"
```

Per `PCB_Design_Guide_Generic.md` PROJECT_PARAMS HS skew budgets:
- Intra-pair skew > 0.127 mm (protocol spec): MAJOR
- Intra-pair skew > 0.01 mm but ≤ 0.127 mm (design target exceeded, within protocol
  spec): MINOR

Report the measured skew for each pair.

**Step 5 — Inter-lane skew.**

Read `[REVIEW_DIR]/diff_pair_skew.txt` for lane-to-lane length differences. Compare
against PROJECT_PARAMS inter-lane skew limits:

```
DP 1.4:   <= 0.45 mm inter-lane
USB SS:   <= 5.08 mm inter-lane
HDMI TMDS:<= 10 mm inter-lane
```

MAJOR if any lane group exceeds its protocol limit. If PROJECT_PARAMS absent but HS
pairs are identifiable: report measured values without pass/fail verdict and note
CANNOT EVALUATE — protocol limit unknown.

**Step 6 — GND return vias.**

Run `add_return_vias.py` in report mode (analysis only — no vias added):

```python
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REVIEW_DIR]/return_vias_report.txt"
ADD_VIAS    = False   # report mode — do not write to PCB
```

Per `PCB_Design_Guide_Generic.md` Phase 11 return via rule: any signal that transitions
between copper layers should have a GND return via within 2 mm of the transition. Report
each layer transition missing a nearby GND via. MAJOR per missing return via on a
power or HS net; MINOR per missing return via on a low-speed signal net.

If `add_return_vias.py` is absent: report CANNOT EVALUATE.

**Step 7 — HS sandwich integrity.**

Run `verify_hs_sandwich.py` in report mode:

```python
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REVIEW_DIR]/hs_sandwich_report.txt"
```

Per `PCB_Design_Guide_Generic.md` (HS sandwich verification, Phase 10/11.5 guidance):
the GND reference plane(s) adjacent to `[HS_ROUTING_LAYER]` must form a continuous
copper fill with no gap, slot, or zone boundary crossing the HS channel. Any
discontinuity in the reference plane under a HS trace: MAJOR.

If `verify_hs_sandwich.py` is absent or HIGH-SPEED SIGNAL INVENTORY is absent:
CANNOT EVALUATE.

**Step 8 — Switching loop area.**

Run `measure_loops.py` in report mode:

```python
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REVIEW_DIR]/loop_area_report.txt"
# Configure LOOP_NETS from PROJECT_PARAMS SWITCHING LOOPS section, or by searching
# [PCB_FILE] for (net N "NET_NAME") entries matching known SW node net names.
```

Per `PCB_Design_Guide_Generic.md` Loop C: any switching loop area > 50 mm²: MAJOR.
Report the measured loop area for every switching converter.

If SWITCHING LOOPS data is absent from PROJECT_PARAMS and SW node nets cannot be
identified from net names: CANNOT EVALUATE.

**Step 9 — Write phase report.**

Write `[REVIEW_DIR]/DR5_routing_review.txt`:

```
PHASE 5 — ROUTING REVIEW
Date: [REVIEW_DATE]

UNCONNECTED NETS:
  [list each unconnected net as CRITICAL, or "None found"]

TRACE WIDTH AUDIT:
  [list violations or "All power nets pass"]

HS PAIR ROUTING:
  [list violations or CANNOT EVALUATE or "All HS pairs pass"]

INTRA-PAIR SKEW:
  [list per-pair measured skew and finding]

INTER-LANE SKEW:
  [list per-protocol-group measured skew and finding]

GND RETURN VIAS:
  [list missing return vias or CANNOT EVALUATE]

HS SANDWICH:
  [list reference plane gaps or CANNOT EVALUATE]

SWITCHING LOOP AREAS:
  [list per-converter measured area and finding]

FINDING SUMMARY:
  CRITICAL: N
  MAJOR:    N
  MINOR:    N
  OBSERVATION: N

PHASE 5 VERDICT: PASS / FAIL
```

---

## REVIEW PHASE 6 — Post-Routing Quality and DFM

**Purpose:** Verify manufacturability, copper balance, silkscreen quality, and
board markings. Maps to `PCB_Design_Guide_Generic.md` Phases 11.5 and 12.

**Inputs:** `[PCB_FILE]`, `[PROJECT_PARAMS_FILE]` (if available),
scripts from `[DESIGN_GUIDE_DIR]`:
`check_copper_balance.py`, `verify_dfm.py`.
DRC report from Phase 1: `[REVIEW_DIR]/DRC_DR1.rpt`.

**Output:** `[REVIEW_DIR]/DR6_dfm_quality.txt`

---

**Step 1 — Copper balance.**

Run `check_copper_balance.py` in report mode:

```python
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REVIEW_DIR]/copper_balance.txt"
```

Per `PCB_Design_Guide_Generic.md` Phase 11.5: the ratio of copper area on `F.Cu` to
copper area on `B.Cu` must be ≥ 0.4 (and ≤ 2.5 for symmetry). Outside this range,
the board is at elevated risk of warping during reflow. MAJOR if F.Cu/B.Cu ratio < 0.4
or > 2.5. Report the computed ratio.

If `check_copper_balance.py` is absent: approximate using the zone count and fill
coverage reported from Phase 1 PCB load. Note the approximation in the report.

**Step 2 — Full DFM check.**

Run `verify_dfm.py` for all 8 items in report mode:

```python
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REVIEW_DIR]/dfm_full.txt"
```

Per `PCB_Design_Guide_Generic.md` Loop F, all 8 checks:
- (a) Component-to-edge courtyard >= 3 mm: MAJOR per violation
- (b) Component-to-component courtyard gap >= 0.25 mm: MAJOR per violation
- (c) Polarized passive orientation consistency: MINOR per zone with mixed orientations
- (d) Through-hole components present (non-mounting-hole THT): MAJOR — target is SMD-only
- (e) Fine-pitch pad with non-ENIG surface finish (if PROJECT_PARAMS surface finish is
  not ENIG): MAJOR
- (f) Via-in-pad: MAJOR per occurrence
- (g) Exposed-pad paste aperture > 60% of pad area: MAJOR per pad
- (h) Annular ring < 0.15 mm on PTH vias: MAJOR per via

If `verify_dfm.py` is absent: report CANNOT EVALUATE for this step and note that DFM
verification requires the script.

**Step 3 — Silkscreen / copper overlap.**

Read `[REVIEW_DIR]/DRC_DR1.rpt` for silkscreen-related violations. Extract any
DRC finding of type "Silkscreen clipped by solder mask" or "Text not completely within
board". Additionally, check for silkscreen text overlapping copper pads (text on
`F.Silkscreen` whose bounding box intersects a pad polygon). MINOR per occurrence —
silkscreen over pads lifts during soldering and reduces pad wetting.

Also verify per THE SILKSCREEN RULE in `PCB_Design_Guide_Generic.md`: confirm that
no reference designators appear on `F.Silkscreen` or `B.Silkscreen`. If any are
present: MINOR (cosmetic violation, not functional).

**Step 4 — Board markings on F.Fab.**

Load the PCB and read all text items on `F.Fab`. Verify the following are present:
- Project name (matching `[PROJECT_NAME]`)
- Version string (e.g., "v1.0" or "Rev A")
- Date string (ISO format preferred)
- RoHS mark (the RoHS symbol or text "RoHS")

MINOR per missing marking. These markings appear in the Fab layer and are referenced
by board assembly documentation; they do not affect electrical function.

**Step 5 — Mounting holes.**

Verify at least one mounting hole footprint is present (typically 4, one per corner).
Verify each mounting hole footprint has an appropriate keepout zone (no copper within
the mechanical clearance radius from the hole). MINOR if mounting holes are absent
(boards without mounting holes cannot be reliably fixtured for assembly). MINOR if
keepout zones are absent.

**Step 6 — Conformal coating annotations.**

If PROJECT_PARAMS specifies a conformal coating requirement: verify `F.Fab` contains:
- `NO COAT` annotation at each connector and test-point access area
- `COAT ALL OTHER AREAS` at the board center or a suitable location

MINOR per missing annotation. These markings drive the conformal coating mask at the
assembly house.

If PROJECT_PARAMS is absent or does not specify coating: OBSERVATION — coating
requirement unknown; confirm with designer.

**Step 7 — Write phase report.**

Write `[REVIEW_DIR]/DR6_dfm_quality.txt`:

```
PHASE 6 — POST-ROUTING QUALITY AND DFM
Date: [REVIEW_DATE]

COPPER BALANCE:
  F.Cu / B.Cu ratio: [computed value] — [PASS / MAJOR]

DFM CHECKS (a-h):
  (a) Component-to-edge: [PASS / MAJOR / CANNOT EVALUATE]
  (b) Courtyard gap:     [PASS / MAJOR / CANNOT EVALUATE]
  (c) Polarity orient.:  [PASS / MINOR / CANNOT EVALUATE]
  (d) THT components:    [PASS / MAJOR / CANNOT EVALUATE]
  (e) Fine-pitch finish: [PASS / MAJOR / CANNOT EVALUATE]
  (f) Via-in-pad:        [PASS / MAJOR / CANNOT EVALUATE]
  (g) Paste aperture:    [PASS / MAJOR / CANNOT EVALUATE]
  (h) Annular ring:      [PASS / MAJOR / CANNOT EVALUATE]

SILKSCREEN QUALITY:
  [list findings or "No silkscreen/copper overlaps found"]

BOARD MARKINGS:
  Project name: PRESENT / ABSENT
  Version:      PRESENT / ABSENT
  Date:         PRESENT / ABSENT
  RoHS:         PRESENT / ABSENT

MOUNTING HOLES:
  Count: N  Keepouts: PRESENT / ABSENT

CONFORMAL COATING ANNOTATIONS:
  [findings or CANNOT EVALUATE or OBSERVATION]

FINDING SUMMARY:
  CRITICAL: N
  MAJOR:    N
  MINOR:    N
  OBSERVATION: N

PHASE 6 VERDICT: PASS / FAIL
```

---

## REVIEW PHASE 7 — Compliance Checklist

**Purpose:** Walk through the Phase 12 compliance checklist from
`PCB_Design_Guide_Generic.md` and produce a PASS / FAIL / WAIVER / CANNOT EVALUATE
verdict for each item based on the existing design.

**Inputs:** `[PCB_FILE]`, `[SCHEMATIC_FILE]`, `[PROJECT_PARAMS_FILE]` (if available),
`[REVIEW_DIR]/DRC_DR1.rpt`, all prior phase outputs.

**Output:** `[REVIEW_DIR]/DR7_compliance_checklist.txt`

---

**Step 1 — Load compliance checklist.**

Open `PCB_Design_Guide_Generic.md` and locate **PHASE 12 — Final DRC + Compliance
Checklist**. Walk through every numbered item in that checklist.

**Step 2 — Evaluate each item.**

For each checklist item, determine the verdict:

| Verdict | Condition |
|---|---|
| **PASS** | The design meets the requirement; evidence available |
| **FAIL** | The design violates the requirement; evidence available |
| **WAIVER** | A FAIL that the designer has explicitly accepted (see waiver_register.txt) |
| **CANNOT EVALUATE** | The check requires PROJECT_PARAMS data that is absent, or requires a tool that is not available |

**Evidence sources for each check:**
- ERC/DRC reports from Phase 1 (`[REVIEW_DIR]/ERC_DR1.rpt`, `[REVIEW_DIR]/DRC_DR1.rpt`)
- Phase 2 schematic findings (`[REVIEW_DIR]/DR2_schematic_review.txt`)
- Phase 3 footprint findings (`[REVIEW_DIR]/DR3_footprint_audit.txt`)
- Phase 4 placement findings (`[REVIEW_DIR]/DR4_placement_review.txt`)
- Phase 5 routing findings (`[REVIEW_DIR]/DR5_routing_review.txt`)
- Phase 6 DFM findings (`[REVIEW_DIR]/DR6_dfm_quality.txt`)
- Direct inspection of `[PCB_FILE]` and `[SCHEMATIC_FILE]`

**CANNOT EVALUATE rules:**
- Any compliance check that requires `TARGET COMPLIANCE`, `SURFACE_FINISH`,
  `CLASS`, or any other PROJECT_PARAMS field that is absent: record
  `CANNOT EVALUATE — PROJECT_PARAMS [field_name] not provided`
- Do not guess at the applicable standard if it is not stated in PROJECT_PARAMS.
  Report CANNOT EVALUATE; do not assume the strictest or most lenient standard.

**Step 3 — Write phase report.**

Write `[REVIEW_DIR]/DR7_compliance_checklist.txt`:

```
PHASE 7 — COMPLIANCE CHECKLIST
Date: [REVIEW_DATE]

Source: PCB_Design_Guide_Generic.md Phase 12

[For each Phase 12 checklist item:]
Item N: [checklist item text]
  Verdict:  PASS / FAIL / WAIVER / CANNOT EVALUATE
  Evidence: [specific quote from phase reports or direct observation]

FINDING SUMMARY:
  FAIL items:              N
  CANNOT EVALUATE items:   N
  WAIVER items:            N
  PASS items:              N

PHASE 7 VERDICT: PASS / FAIL
  (FAIL if any item is FAIL without a waiver; PASS if all items are PASS, WAIVER,
   or CANNOT EVALUATE — CANNOT EVALUATE items do not cause a FAIL verdict but are
   flagged in the final report as requiring follow-up)
```

---

## FINAL REVIEW REPORT

**Purpose:** Consolidate all phase outputs into a single executive summary. This document
is the primary deliverable of the review — it is what the designer, project lead, or
supplier qualification engineer reads to understand the design's state.

**Inputs:** All phase reports (`DR1_completeness.txt` through `DR7_compliance_checklist.txt`),
`waiver_register.txt` (if any waivers were issued during the review).

**Output:** `[REVIEW_DIR]/DESIGN_REVIEW_REPORT.md`

---

**Write `[REVIEW_DIR]/DESIGN_REVIEW_REPORT.md` with the following structure:**

```markdown
# PCB Design Review Report

**Project:**       [PROJECT_NAME]
**Review date:**   [REVIEW_DATE]
**Reviewer:**      [REVIEWER]
**Schematic:**     [SCHEMATIC_FILE]
**PCB:**           [PCB_FILE]
**Review output:** [REVIEW_DIR]

---

## Phase Summary

| Phase | Name                     | CRITICAL | MAJOR | MINOR | OBS | Verdict |
|-------|--------------------------|----------|-------|-------|-----|---------|
| DR1   | Project Completeness     | N        | N     | N     | N   | PASS/FAIL |
| DR2   | Schematic Review         | N        | N     | N     | N   | PASS/FAIL |
| DR3   | Footprint & Library Audit| N        | N     | N     | N   | PASS/FAIL |
| DR4   | Placement Review         | N        | N     | N     | N   | PASS/FAIL |
| DR5   | Routing Review           | N        | N     | N     | N   | PASS/FAIL |
| DR6   | DFM Quality              | N        | N     | N     | N   | PASS/FAIL |
| DR7   | Compliance Checklist     | N        | N     | N     | N   | PASS/FAIL |
| **TOTAL** |                   | **N**    | **N** | **N** | **N** | — |

---

## CRITICAL Findings

[List every CRITICAL finding using the standard finding format.
 If none: "No CRITICAL findings."]

---

## MAJOR Findings

[List every open (un-waivered) MAJOR finding using the standard finding format.
 If none: "No open MAJOR findings."]

---

## MINOR Findings

[List every MINOR finding.
 If none: "No MINOR findings."]

---

## Observations

[List every OBSERVATION.
 If none: "No observations."]

---

## Waiver Register

[If any waivers were issued during the review:]

| ID | Finding | Severity | Rationale | Designer | Date |
|----|---------|----------|-----------|----------|------|
| W1 | [finding text] | MAJOR | [rationale] | [name] | [date] |

[If no waivers: "No waivers issued."]

---

## CANNOT EVALUATE Items

[List every check that returned CANNOT EVALUATE, with the reason.
 These require follow-up — they are not findings, but they are gaps in the review.]

| Phase | Check | Reason |
|-------|-------|--------|
| DR2   | CHECK 8: Bulk input capacitance | PROJECT_PARAMS POWER RAIL TABLE absent |
| ...   | ...   | ...    |

---

## Overall Verdict

**APPROVED / APPROVED WITH CONDITIONS / NOT APPROVED**

Determined as follows:
- **APPROVED** — Zero CRITICAL findings. Zero open (un-waivered) MAJOR findings.
- **APPROVED WITH CONDITIONS** — Zero CRITICAL findings. One or more MAJOR findings
  with accepted waivers in the waiver register. Conditions: [list waivered findings
  and required follow-up before next revision].
- **NOT APPROVED** — One or more CRITICAL findings, OR one or more MAJOR findings
  without an accepted waiver. The board must not proceed to fabrication until all
  CRITICAL and un-waivered MAJOR findings are resolved.

[State the verdict and the specific findings that determined it.]
```

---

## Appendix A — Script Availability Checklist

Before beginning a review, confirm which scripts are present in `[DESIGN_GUIDE_DIR]`
and record the result in `[REVIEW_DIR]/review_inputs.txt`. A CANNOT EVALUATE finding
is issued for each check that requires an absent script — it is not a FAIL of the design.

| Script | Used in Phase | Present? |
|---|---|---|
| `check_decoupling_proximity.py` | DR4 Step 1 | Y / N |
| `check_bus_endpoint_distance.py` | DR4 Step 2 | Y / N |
| `verify_dfm.py` | DR4 Step 4, DR6 Step 2 | Y / N |
| `verify_thermal.py` | DR4 Step 5 | Y / N |
| `width_audit.py` | DR5 Step 2 | Y / N |
| `verify_trace_widths.py` | DR5 Step 2 (fallback) | Y / N |
| `verify_highspeed.py` | DR5 Step 3 | Y / N |
| `measure_all_diff_pairs.py` | DR5 Step 4 | Y / N |
| `add_return_vias.py` | DR5 Step 6 | Y / N |
| `verify_hs_sandwich.py` | DR5 Step 7 | Y / N |
| `measure_loops.py` | DR5 Step 8 | Y / N |
| `check_copper_balance.py` | DR6 Step 1 | Y / N |
| `check_polarity_markers.py` | DR3 Step 2 | Y / N |

---

## Appendix B — Finding ID Convention

Assign each finding a unique ID for cross-referencing between phase reports and the
final report. Format: `<PHASE>-<SEVERITY>-<NNN>` where NNN is a three-digit sequence
number within the phase.

Examples:
- `DR2-MAJOR-001` — first MAJOR finding in Phase 2
- `DR5-CRITICAL-001` — first CRITICAL finding in Phase 5
- `DR6-MINOR-003` — third MINOR finding in Phase 6

Reference this ID when issuing a waiver (`W1 → DR4-MAJOR-002`). Reference this ID
in the final report finding lists so the designer can locate the detailed evidence
in the phase report.

---

## Appendix C — Review Phase Execution Order

Run phases strictly in order. Each phase may reference outputs from earlier phases.
Do not run a later phase if an earlier phase produced a CRITICAL finding that halts
review (see DR1 gate).

```
Input validation
    ↓
DR1 — Project Completeness Check        [HALT if PCB or schematic fails to load]
    ↓
DR2 — Schematic Review
    ↓
DR3 — Footprint and Library Audit
    ↓
DR4 — Placement Review
    ↓
DR5 — Routing Review
    ↓
DR6 — Post-Routing Quality and DFM
    ↓
DR7 — Compliance Checklist
    ↓
FINAL REVIEW REPORT
```

No phase writes to `[SCHEMATIC_FILE]`, `[PCB_FILE]`, or any file inside `[PROJECT_DIR]`.
All outputs go to `[REVIEW_DIR]` only.
