# Design Guide Additions — Phase 2 Table Update + Phase 10 Pre-Route Alignment

**Integration note:** This document contains two sections to integrate into
`PCB_Design_Guide_Generic.md`. Section 1 replaces Sub-step B2 in Phase 2.
Section 2 inserts as Part 0 at the start of Phase 10, before the existing Part A
(switching loops). All script paths use `[SCRIPTS_DIR]` per guide convention.

---

## SECTION 1 — Phase 2 Sub-step B2 Replacement

*Replaces the existing "Sub-step B2 — Build PROXIMITY_RULES_TABLE" block in Phase 2.*

---

   **Sub-step B2 — Build PROXIMITY_RULES_TABLE:**

   For each passive identified in Sub-step B, derive exactly which component it
   serves and encode as a tuple in `proximity_rules_config.py::RULES`. Use the
   threshold lookup table below. Write all rules to PROJECT_PARAMS before Step 2.
   This step runs entirely from schematic and datasheet reading — no PCB file
   exists yet.

   **Derivation procedure:**
   1. For each passive (R, C, L, D, TVS): read its net connections in the schematic.
   2. Identify the owner component — the IC or connector the passive directly serves.
   3. Select the relationship type from the threshold table below.
   4. Determine `preferred_side` and `group_type_hint` from the field guidance below.
   5. Write the rule tuple with all eight fields.

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

   **Threshold lookup table:**

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
   For a bypass cap on a VDD pin, use the power net name (e.g. `LT_1V8`). For an
   ESD clamp on a signal line, use the signal net name. For bulk caps or LDO-to-IC
   rules where no single net is the critical path, set `net_hint` to `None` — the
   script uses the minimum distance across all shared nets. The net name must match
   the KiCad netlist exactly (case-sensitive).

   **preferred_side — which side of the anchor IC this satellite belongs on:**

   Set this from schematic topology and board layout intent. The value is used by
   `route_prep_align.py` in Phase 10 to generate ALIGNMENT_GROUPS automatically.

   | Value | Meaning |
   |---|---|
   | `"above"` | Satellite goes above the anchor's top bbox edge (−Y direction in KiCad) |
   | `"below"` | Satellite goes below the anchor's bottom bbox edge (+Y direction) |
   | `"left"` | Satellite goes left of the anchor's left bbox edge (−X direction) |
   | `"right"` | Satellite goes right of the anchor's right bbox edge (+X direction) |
   | `"ray_left"` | Satellite scans leftward from anchor until clear of all other components |
   | `"ray_right"` | Satellite scans rightward from anchor until clear of all other components |
   | `"nearest_pad"` | No fixed side; place adjacent to the shared net pad on whichever face is closest |

   **How to determine preferred_side:**
   - Read the anchor IC's datasheet pad map. Identify which face (top/bottom/left/right in
     the schematic) carries the relevant supply or signal pin.
   - Use `"above"` / `"below"` / `"left"` / `"right"` when the pin face is known and
     there is clearly usable space on that side in the board outline.
   - Use `"ray_left"` or `"ray_right"` when the satellite must scan outward to find a
     clear spot — typical for bulk caps that need to dodge a routing corridor.
   - Use `"nearest_pad"` for components in tight switching-loop topologies (bootstrap caps,
     snubbers, FETs adjacent to ideal diode ICs) where the exact face depends on the
     anchor's final rotation, and for any passive that is not intended to participate in an
     alignment group (`group_type_hint = "none"`).

   **group_type_hint — which ALIGNMENT_GROUPS type to use in Phase 10:**

   | Value | Meaning |
   |---|---|
   | `"column"` | Single X coordinate shared by all members; members stacked vertically |
   | `"row"` | Single Y coordinate shared by all members; members arranged horizontally |
   | `"pad_track"` | Each member's position on one axis tracks a specific pad on the anchor IC |
   | `"ray_place"` | Scan outward from anchor bbox edge until a clear position is found |
   | `"none"` | No alignment group; component placed by Phase 8 proximity rules only |

   **How to determine group_type_hint:**
   - **`"column"`**: two or more satellites on the same side of an IC that stack neatly
     along the IC's height — bypass caps, pull-up resistors, filter networks.
   - **`"row"`**: two or more satellites on the same side of an IC that arrange along the
     IC's width — AC coupling caps for a differential pair bus, touch pull-ups along a
     connector edge.
   - **`"pad_track"`**: AC coupling caps for a multi-lane high-speed bus where each cap
     must align precisely with its specific IC pad X (or Y) coordinate. Use when the pads
     are closely spaced and individual X tracking matters for routing.
   - **`"ray_place"`**: a single component (typically a bulk cap) that must clear a routing
     corridor — exact position is found by scanning outward until no overlap.
   - **`"none"`**: switching-loop components (routed by `route_critical.py`), ICs, ESD
     clamps placed by signal path, connectors, or any component whose Phase 8 placement
     is already correct and should not be disturbed.

   **Rule-type guidance for group_type_hint:**

   | rule_type | Typical group_type_hint | Notes |
   |---|---|---|
   | `crystal` | `"ray_place"` for the crystal itself; `"column"` for load caps | Crystal scans to first clear position; load caps form a column between IC and crystal |
   | `bootstrap` | `"none"` | Part of switching loop; handled by `route_critical.py` |
   | `ac_coupling` | `"row"` or `"pad_track"` | Use `"pad_track"` when pad-to-pad X spacing must be exact; `"row"` when a uniform pitch is acceptable |
   | `esd_clamp` | `"none"` | Placed by signal path between source IC and connector |
   | `decoupling_bypass` | `"column"` or `"ray_place"` or `"none"` | Column for most bypass caps; ray_place for bulk caps that must clear routing corridors; none for switching-loop bypasses |
   | `decoupling_bulk` | `"none"` | Large ICs (LDOs, flash) use proximity rules; bulk caps follow IC placement |
   | `filter` | `"column"` or `"none"` | Filter networks (REXT, VDET dividers) in column; passthrough components (CM chokes, polyfuses) use none |
   | `pullup_pulldown` | `"column"` or `"row"` | Column when stacking vertically beside an IC; row when aligning along a connector edge |
   | `power_mgmt` | `"none"` | ICs placed by Phase 8 proximity rules |

   **Eight-field tuple format in proximity_rules_config.py::RULES:**
   ```python
   RULES = [
       # (ref_a, ref_b, max_dist_mm, rule_type, description, net_hint,
       #  preferred_side, group_type_hint)
       ("C4",  "U2",  8,  "decoupling_bypass", "100nF bypass cap on U2 VDD pin",  "VDD",     "nearest_pad", "column"),
       ("C5",  "U1",  15, "decoupling_bulk",   "10uF bulk input cap for U1 VIN",  None,      "nearest_pad", "none"),
       ("D1",  "J1",  8,  "esd_clamp",         "TVS on J1 signal pins",           "USB_DP",  "nearest_pad", "none"),
       ("Y1",  "U3",  12, "crystal",           "16MHz crystal for U3 MCU clock",  "XTAL_IN", "right",       "ray_place"),
       ("R1",  "U3",  20, "pullup_pulldown",   "SDA I2C pull-up to U3",           "I2C_SDA", "left",        "column"),
       ("C10", "U3",  10, "ac_coupling",       "TX+ AC cap at U3 TX output",      "TX_P",    "above",       "row"),
   ]
   ```

   **PROXIMITY_RULES_TABLE format in PROJECT_PARAMS.md** (human-readable mirror of RULES):
   ```
   PROXIMITY_RULES_TABLE:
     ref_A | ref_B | max_dist_mm | type               | net_hint | preferred_side | group_type_hint | reason
     ------|-------|-------------|--------------------|----------|----------------|-----------------|-------
     C4    | U2    | 8           | decoupling_bypass  | VDD      | nearest_pad    | column          | 100nF bypass cap on U2 VDD pin
     C5    | U1    | 15          | decoupling_bulk    | None     | nearest_pad    | none            | 10uF bulk input cap for U1 VIN
     D1    | J1    | 8           | esd_clamp          | USB_DP   | nearest_pad    | none            | TVS on J1 signal pins
     Y1    | U3    | 12          | crystal            | XTAL_IN  | right          | ray_place       | 16MHz crystal for U3 MCU clock
     R1    | U3    | 20          | pullup_pulldown    | I2C_SDA  | left           | column          | SDA I2C pull-up to U3
     C10   | U3    | 10          | ac_coupling        | TX_P     | above          | row             | TX+ AC cap at U3 TX output
   ```

---

## SECTION 2 — Phase 10 Pre-Route Alignment (new Part 0)

*Inserts as Part 0 at the start of Phase 10, before the existing Part A (switching loops).*

---

**Part 0 — Pre-route alignment.**

Run `[SCRIPTS_DIR]/route_prep_align.py`. This script reads
`routing_config.py::ALIGNMENT_GROUPS` and moves passive satellite components into
precise positions adjacent to their anchor ICs before FreeRouting runs. It
performs three overlap checks and a blocker-clearing pass; it never touches locked
footprints.

**Step 0a — Create or verify routing_config.py.**

`routing_config.py` is the project-specific config file imported by all Phase 10
scripts. If it does not exist, create it from the template below. Fill in the
project-specific values for every field. The file must be in `[SCRIPTS_DIR]`.

```python
# routing_config.py — project-specific Phase 10 config

KICAD_SITE_PKGS = r"C:\Program Files\KiCad\10.0\bin\Lib\site-packages"
PCB_FILE        = r"[FULL_PATH_TO_PROJECT].kicad_pcb"
REPORTS_DIR     = r"[FULL_PATH_TO_REPORTS_DIR]"

# Per-component rotation symmetry — used by route_prep_align.py.
# "none" = never rotate. "180" = 0° and 180° equivalent. "4fold" = all 90° steps.
# Omit locked components — they are never touched.
ROTATION_SYMMETRY = {
    # Example entries — replace with every unlocked passive in the project:
    # "C1": "180",
    # "R1": "180",
    # "L1": "none",
}

# HS diff pair definitions — used by route_highspeed.py and verify_highspeed.py.
# name → {"layer": "...", "pairs": {pair_name: (P_net, N_net)}}
HS_PAIRS = {}   # fill from HIGH-SPEED SIGNAL INVENTORY in PROJECT_PARAMS

# All HS net names (both P and N of every pair) — used by lock_highspeed_nets.py.
HS_NETS = []    # derive from HS_PAIRS

# Lane groups for inter-lane spread verification — used by verify_highspeed.py.
LANE_GROUPS = {}

# Switching loop definitions — used by route_critical.py.
SWITCHING_LOOPS = []    # fill from PROJECT_PARAMS switching converter list

# Pre-route alignment groups — generated in Step 0b below.
ALIGNMENT_GROUPS = []
```

**Step 0b — Generate ALIGNMENT_GROUPS.**

ALIGNMENT_GROUPS is the list of component clusters that `route_prep_align.py`
will position. Generate it once per project using this procedure:

1. Read `proximity_rules_config.py::RULES`. Filter to entries where
   `group_type_hint != "none"`.

2. Group filtered entries by `(ref_b, preferred_side, group_type_hint)`. Each
   unique combination is one alignment group. Components that share the same
   anchor IC, the same side, and the same group type belong in one group.

3. For each group, open the PCB file with `pcbnew` and read:
   - Anchor IC centroid `(cx, cy)` via `fp.GetPosition()`
   - Anchor IC bbox edges via `fp.GetBoundingBox()`
   - Each member's footprint half-extents `(hw, hh)` via `fp.GetBoundingBox()`
     at target rotation (0° for standard passives)

4. Compute config values using the formulas in the **Offset and pitch formulas**
   table below.

5. Order members within each group by their current PCB pad position along the
   group axis (ascending X for row groups; ascending Y for column groups). For
   `pad_track` groups, match each member to its `net_hint` pad on the anchor IC.

6. Write the computed ALIGNMENT_GROUPS entries into `routing_config.py`.

**Offset and pitch formulas:**

Let `C = 0.15` (clearance_mm), `HW` = largest member half-width, `HH` = largest
member half-height, `bbox_edge` = the relevant anchor bbox edge for the side.

| group_type | anchor bbox edge | offset formula | pitch formula |
|---|---|---|---|
| `column`, side=`"above"` | `T` (top) | `y_start_mm = T − cy − HH − C` | `2 × HH + C` |
| `column`, side=`"below"` | `B` (bottom) | `y_start_mm = B − cy + HH + C` | `2 × HH + C` |
| `column`, side=`"left"` | `L` (left) | `x_offset_mm = L − cx − HW − C` | *(Y pitch)* `2 × HH + C` |
| `column`, side=`"right"` | `R` (right) | `x_offset_mm = R − cx + HW + C` | *(Y pitch)* `2 × HH + C` |
| `row`, side=`"above"` | `T` (top) | `y_offset_mm = T − cy − HH − C` | `2 × HW + C` |
| `row`, side=`"below"` | `B` (bottom) | `y_offset_mm = B − cy + HH + C` | `2 × HW + C` |
| `row`, side=`"left"` | `L` (left) | `x_start_mm = L − cx − (n×pitch)` | `2 × HW + C` |
| `row`, side=`"right"` | `R` (right) | `x_start_mm = R − cx + HW + C` | `2 × HW + C` |
| `ray_place`, side=`"ray_left"` | `L` (left) | `direction = "left"`, `min_offset_mm = 0.5` | *(n/a)* |
| `ray_place`, side=`"ray_right"` | `R` (right) | `direction = "right"`, `min_offset_mm = 0.5` | *(n/a)* |
| `pad_track` | pad positions | `fixed_offset_mm` = anchor pad coord − cy (or cx) − HH − C | `2 × HH + C` |

*For column groups with `preferred_side = "left"` or `"right"`, `y_start_mm` is set
so the first member aligns with the anchor centroid or the start of the anchor's
relevant pad cluster — read the anchor pad Y positions from the PCB and use the
topmost pad Y as the reference.*

**ALIGNMENT_GROUPS entry formats:**

```python
# Column — fixed X, stacked vertically
{
    "name":            "descriptive name",
    "type":            "column",
    "anchor_ref":      "U3",
    "x_offset_mm":     6.9,      # positive = right of anchor centroid
    "y_start_mm":     -8.0,      # negative = above anchor centroid
    "pitch_mm":        2.0,      # center-to-center spacing along Y
    "target_rotation": 0.0,
    "members":         ["R_REXT1", "R_VDET_H1", "R_VDET_L1"],
},

# Row — fixed Y, arranged horizontally
{
    "name":            "descriptive name",
    "type":            "row",
    "anchor_ref":      "U3",
    "y_offset_mm":    -6.4,      # negative = above anchor centroid
    "x_start_mm":     -1.43,     # negative = left of anchor centroid
    "pitch_mm":        2.02,     # center-to-center spacing along X
    "target_rotation": 0.0,
    "members":         ["C_TX1N1", "C_TX1P1", "C_TX2N1", "C_TX2P1"],
},

# Ray-place — scan outward until clear
{
    "name":            "descriptive name",
    "type":            "ray_place",
    "anchor_ref":      "U6",
    "direction":       "left",
    "min_offset_mm":   0.5,
    "target_rotation": 0.0,
    "members":         ["C_LT1V8_BULK1"],
},

# Pad-track — each member X tracks its specific anchor pad
{
    "name":            "descriptive name",
    "type":            "pad_track",
    "anchor_ref":      "U3",
    "track_axis":      "X",
    "fixed_axis":      "Y",
    "fixed_offset_mm": -2.5,     # offset from mean pad Y along fixed axis
    "target_rotation": 0.0,
    "members": [
        {"ref": "C_TX1N1", "anchor_pad_net": "LT_TX1_N"},
        {"ref": "C_TX1P1", "anchor_pad_net": "LT_TX1_P"},
    ],
},
```

**Grouping rules — when to split vs. combine:**
- Components on the same side of the same anchor IC with the same `group_type_hint`
  form one group unless a physical obstacle (another IC, board edge, routing
  corridor) forces them apart.
- When a wider-courtyard component cannot fit within the pitch of the surrounding
  group (e.g., a bulk cap among 0402 bypass caps), give it its own group or a
  separate `ray_place` entry. Do not force it into the row — Check A will spread
  it beyond the 0.3mm stop condition.
- Components anchored to ICs (not passives) with `group_type_hint = "none"` are
  never placed in ALIGNMENT_GROUPS; their Phase 8 positions are retained.

**Step 0c — Run the dry run and iterate.**

```
cd [SCRIPTS_DIR]
python route_prep_align.py            # dry run — report only, no changes written
python route_prep_align.py --apply    # apply moves and rotations, save PCB
```

The dry run output has five sections:

| Section | What to look for |
|---|---|
| Pass 1: Alignment groups | SKIP lines — member not placed |
| Blocker-clearing pass | FAIL lines — blocker could not be cleared |
| Check B: Pre-apply overlap | WARNING lines — move suppressed by overlap |
| Check B-rot: Rotation overlap | WARNING lines — rotation suppressed (informational; not a stop condition) |
| Check C: Post-apply overlap | FAIL lines — overlap found after writing (apply mode only) |

**Iteration loop:** Repeat until all stop conditions are met.

1. Run `python route_prep_align.py` (dry run).
2. Read the full output. Identify problems from the table above.
3. Diagnose the root cause in `routing_config.py::ALIGNMENT_GROUPS`:
   - Wrong `x_offset_mm`, `y_offset_mm`, `y_start_mm`, or `x_start_mm` (offset
     places member inside another footprint)
   - Wrong `pitch_mm` (too small for the actual footprint sizes at target rotation)
   - Wrong `group_type_hint` in PROXIMITY_RULES_TABLE — converted to wrong group type
   - Members in the wrong group or wrong order within a group
4. Make the smallest targeted change that fixes the root cause. Config changes
   belong in `routing_config.py`; algorithmic changes belong in
   `route_prep_align.py`. Never hardcode component references or net names in
   `route_prep_align.py` — any project-specific fix goes in `routing_config.py`.
5. Return to step 1.
6. When the dry run is clean, run `python route_prep_align.py --apply`.
7. Read Check C output. If overlaps are reported, diagnose and fix, then return to step 1.

**Stop conditions — all five must be true simultaneously:**

| Condition | What it means |
|---|---|
| Zero SKIP lines | Every ALIGNMENT_GROUPS member was placed successfully |
| Zero Check B suppressions | No position move was blocked by a cross-component overlap |
| Zero blocker-clear FAILs | Every blocker found a valid cleared position |
| Zero Check C overlaps | No physical overlaps after `--apply` |
| Check A adjustments ≤ 0.3 mm | Intra-group spacing corrections are small; pitch values are near correct |

**Generalization rule — enforced at every step:**

Before writing or modifying any line of `route_prep_align.py`, ask: "Would this
change work correctly on a completely different PCB with different component
references, different net names, and a different board topology?" If the answer
is no, the change is a project-specific fix and belongs in `routing_config.py`,
not in the script. This applies even when the general fix is harder to implement.

**What this rules out in route_prep_align.py:**
- Hardcoded component references (`if ref == "C_TX1N1":`)
- Net name string literals outside of config lookups
- Numeric KiCad layer constants (`pcbnew.B_Cu`) — use `board.GetLayerID("B.Cu")`
- Forced rotation overrides for named components
- Any logic that only works because of this board's specific naming or topology

**KiCad API rules (enforced in any script edits):**
- `fp.IsLocked()` — skip any footprint that returns True. No exceptions.
- Layer IDs: resolve at runtime with `board.GetLayerID("F.Cu")` — never numeric IDs.
- `board.Remove(item)` — never call; causes SIGSEGV in standalone scripts.
- Coordinates: `pcbnew.FromMM(x)` → internal units; `pcbnew.ToMM(x)` → mm.
- Move: `fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))`
- Rotate: `fp.SetOrientationDegrees(angle)`

**Script reference:**

| Script | Phase | CONFIG fields to set in routing_config.py |
|---|---|---|
| `route_prep_align.py` | 10 Part 0 | `PCB_FILE`, `KICAD_SITE_PKGS`, `ROTATION_SYMMETRY`, `ALIGNMENT_GROUPS` |
| `route_clearance_audit.py` | 10 Part 0.5 | `PCB_FILE`, `KICAD_SITE_PKGS`, `CLEARANCE_AUDIT`, `ALIGNMENT_GROUPS`, `HS_PAIRS`, `HS_ROUTE_WIDTHS`, `SWITCHING_LOOPS`, `ROTATION_SYMMETRY` |
| `route_critical.py` | 10 Part A | `PCB_FILE`, `KICAD_SITE_PKGS`, `REPORTS_DIR`, `SWITCHING_LOOPS`, `CLEARANCE_AUDIT` |
| `route_highspeed.py` | 10 Part B | `PCB_FILE`, `KICAD_SITE_PKGS`, `HS_PAIRS` |
| `verify_hs_sandwich.py` | 10 Part B | `PCB_FILE`, `KICAD_SITE_PKGS`, `REPORTS_DIR` |
| `verify_highspeed.py` | 10 Part B | `PCB_FILE`, `KICAD_SITE_PKGS`, `HS_PAIRS`, `LANE_GROUPS`, `REPORTS_DIR` |
| `lock_highspeed_nets.py` | 10 Part B | `PCB_FILE`, `KICAD_SITE_PKGS`, `HS_NETS` |

**Component-movement boundary:** `route_prep_align.py` and `route_clearance_audit.py --apply`
are the only Phase 10 scripts that move components. Every script from Part A onward
(`route_critical.py`, `route_highspeed.py`, etc.) places tracks and vias only — no footprint
positions or rotations are changed. If component positions need adjustment after Part 0.5,
re-run Part 0 or Part 0.5, not a routing script.

**Outputs:** Aligned PCB saved in place. Snapshot recommended before proceeding to Part A.

**Loop exit criteria:** All five stop conditions met in the same dry run, then
`--apply` produces zero Check C overlaps.

---

*End of Phase 10 Part 0 insert.*

---

## SECTION 3 — Phase 10 Part 0.5: Routing Clearance Audit (new insert)

*Inserts as Part 0.5 at the start of Phase 10, after Part 0 (pre-route alignment) and before Part A (switching loops).*

---

**Part 0.5 — Routing clearance audit.**

Run `[SCRIPTS_DIR]/route_clearance_audit.py`. This script runs after `route_prep_align.py`
has positioned satellite components into their groups. It verifies that every component
has sufficient physical clearance for its routing requirements — trace corridors, via
keepouts, neckdown zones, high-speed pair paths, and differential pair meander budgets —
and moves any component that fails by the minimum amount needed to pass.

**Step 0.5a — Add CLEARANCE_AUDIT to routing_config.py.**

Add the following block to `routing_config.py` between `HS_ROUTE_WIDTHS` and
`SWITCHING_LOOPS`. Tune the values to match your design rules and DRC settings.

```python
# ── Routing clearance audit — for route_clearance_audit.py ───────────────────
CLEARANCE_AUDIT = {
    "signal_trace_width_mm":   0.20,   # default signal trace width for unlisted nets
    "via_drill_mm":            0.30,   # standard signal/power via drill diameter
    "via_annular_ring_mm":     0.15,   # standard via copper annular ring width
    "via_clearance_mm":        0.15,   # via copper-to-copper clearance
    "hs_via_drill_mm":         0.25,   # HS differential pair layer-change via drill
    "hs_via_annular_ring_mm":  0.15,   # HS via annular ring width
    "neckdown_length_mm":      0.50,   # minimum neckdown transition zone length beside pad
    "diff_pair_min_sep_mm":    8.0,    # minimum edge-to-edge gap between diff-pair components
    "board_margin_mm":         3.0,    # minimum distance from board edge for any component
    "corridor_margin_mm":      0.50,   # extra routing margin added to calculated corridor minimum
}
```

**Key parameters to tune:**

| Parameter | What it controls | Tune when |
|---|---|---|
| `signal_trace_width_mm` | Default trace width for nets not in HS_ROUTE_WIDTHS or SWITCHING_LOOPS | Your fab min trace is not 0.20mm |
| `via_drill_mm` + `via_annular_ring_mm` | Full via keepout footprint | Your fab uses different via specs |
| `neckdown_length_mm` | How much space beside a pad for a trace-to-pad width transition | Traces are unusually wide relative to pads |
| `diff_pair_min_sep_mm` | Minimum gap between components sharing a diff-pair net | Your meander length budget changes |
| `corridor_margin_mm` | Extra buffer added to computed corridor minimum | Routing is tight and you want more breathing room |

**Step 0.5b — Run the dry run and iterate.**

```
cd [SCRIPTS_DIR]
python route_clearance_audit.py            # dry run — report only, no changes written
python route_clearance_audit.py --apply    # apply corrections and save PCB
```

The dry run runs seven analyses and reports each separately:

| Section | What it checks | What to look for |
|---|---|---|
| 1. Trace corridor | Gap between each ALIGNMENT_GROUP and its anchor vs. required width for shared nets | FAIL lines with deficit |
| 2. Via keepout | Room beside each pad for a via to land without overlapping a neighbor | FAIL lines naming the blocking component |
| 3. HS pair path | Whether any component sits inside a high-speed pair's routing envelope | FAIL lines naming the blocker |
| 4. Diff pair separation | Edge-to-edge gap between components sharing a diff-pair net | FAIL lines with deficit |
| 5. Neckdown zone | Clear space on the trace-exit side of a pad where trace width > pad width | FAIL lines with deficit |
| 6. Board edge margin | Minimum distance from each component's bbox to the board outline | FAIL lines naming the closest side |
| 7. Component-type gap | Gap between adjacent components using the type-aware sliding scale | FAIL lines with deficit |

**Iteration loop:** Repeat until all stop conditions are met.

1. Run `python route_clearance_audit.py` (dry run).
2. Read the full output. Identify FAIL lines in any of the seven sections.
3. Diagnose the root cause:
   - **Corridor FAIL**: the group's offset in `routing_config.py::ALIGNMENT_GROUPS` places it
     too close to the anchor for the number of nets that share them. Increase the offset
     (`x_offset_mm`, `y_offset_mm`, etc.) in the relevant group, or increase
     `corridor_margin_mm` in `CLEARANCE_AUDIT` if all corridors are marginally tight.
   - **Via keepout FAIL**: a component is too close to its neighbor for a via to land beside
     its pad. The script's `--apply` mode resolves this automatically; if it cannot (proximity
     rule prevents the move), adjust the component's group offset in `ALIGNMENT_GROUPS`.
   - **HS path FAIL**: a component is sitting in the intended routing path of a differential
     pair. The script's `--apply` mode finds the minimum cardinal displacement to clear it;
     if no valid direction exists, the component's proximity rule `max_dist_mm` in
     `proximity_rules_config.py::RULES` must be increased or its `preferred_side` changed.
   - **Diff pair sep FAIL**: two components sharing a diff-pair net are too close together for
     the router to insert meander serpentines. The script's `--apply` mode pushes them apart.
     If repeated FAILs remain after apply, increase `diff_pair_min_sep_mm` — its current value
     may exceed the component's proximity rule constraint.
   - **Neckdown FAIL**: no room beside a pad for a trace neckdown. Either move the component
     via `ALIGNMENT_GROUPS` offset or reduce `neckdown_length_mm` if your fab process allows
     shorter transitions.
   - **Board edge FAIL**: a component is too close to the board outline. These can indicate
     that an alignment group was pushed off the board by a series of outward moves. Adjust the
     group's offset in `ALIGNMENT_GROUPS` to start further from the anchor.
   - **Type-gap FAIL**: two components of incompatible types (e.g. a connector and a passive)
     are too close. This is typically resolved by the `--apply` mode. If it recurs after apply,
     a group is encroaching on a connector courtyard and needs its offset adjusted.
4. Make the smallest targeted change that fixes the root cause.
   - Config changes (offsets, margins): `routing_config.py`
   - Proximity rule changes: `proximity_rules_config.py`
   - Algorithm changes only if the analysis logic itself is wrong: `route_clearance_audit.py`
5. Return to step 1.
6. When the dry run is clean, run `python route_clearance_audit.py --apply`.
7. Read Check C output. If overlaps are reported, diagnose and fix, then return to step 1.

**Stop conditions — all must be true simultaneously:**

| Condition | What it means |
|---|---|
| Zero corridor FAILs | Every group has enough gap for its nets to route through |
| Zero via keepout FAILs | Every pad has room for a via beside it |
| Zero HS path FAILs | No component is blocking a high-speed pair's routing envelope |
| Zero diff pair sep FAILs | All diff-pair components meet meander separation budget |
| Zero neckdown FAILs | All pads with wide traces have transition room |
| Zero board edge FAILs | All components are within the board margin |
| Zero type-gap FAILs | All component pairs meet the type-aware clearance gap |
| Zero Check C overlaps | No physical overlaps after `--apply` |

**Safety checks enforced on every move (same as route_prep_align.py):**

- Locked components (`fp.IsLocked()`) are never moved or rotated — no exceptions.
- Check A — intra-group spacing is re-enforced after any group move.
- Check B — every proposed move is verified clear of all other footprints before writing.
- Check C — after saving, the PCB is re-read and all moved components are re-checked.
- Proximity rule guard — moved components must stay within their `max_dist_mm` from their anchor.
- Board edge margin — no move places a component within `board_margin_mm` of the board edge.
- `NO_ROTATE` guard — components with `rotation_symmetry = "none"` in `ROTATION_SYMMETRY` are never rotated.
- Component-type-aware gap — Check B uses the same sliding-scale clearance as `place_components_organized.py`.

**Generalization rule — enforced at every step:**

Before writing or modifying any line of `route_clearance_audit.py`, ask: "Would this change
work correctly on a completely different PCB with different component references, different
net names, and a different board topology?" If the answer is no, the change is a
project-specific fix and belongs in `routing_config.py` or `proximity_rules_config.py`,
not in the script.

**Script reference:**

| Script | Phase | CONFIG fields to set in routing_config.py |
|---|---|---|
| `route_clearance_audit.py` | 10 Part 0.5 | `PCB_FILE`, `KICAD_SITE_PKGS`, `CLEARANCE_AUDIT`, `ALIGNMENT_GROUPS`, `HS_PAIRS`, `HS_ROUTE_WIDTHS`, `SWITCHING_LOOPS`, `ROTATION_SYMMETRY` |

**Outputs:** Corrected PCB saved in place. Snapshot recommended before proceeding to Part A:
```
[PROJECT_BACKUPS_DIR]/[PROJECT_NAME]_CLEARANCE_YYYYMMDD.kicad_pcb
```

**Loop exit criteria:** All eight stop conditions met in the same dry run, then `--apply`
produces zero Check C overlaps.

---

*End of Phase 10 Part 0.5 insert.*

---

## SECTION 3.5 — Phase 10 Part 0.75: Fanout Via Placement (new insert)

*Inserts as Part 0.75 at the start of Phase 10, after Part 0.5 (routing clearance audit) and before Part A (switching loops).*

---

**Part 0.75 — Fanout via placement.**

Run `[SCRIPTS_DIR]/route_fanout_vias.py`. This script places layer-transition vias at every pad that needs to reach a different copper layer for routing. It must run after `route_clearance_audit.py` (component positions are final) and before `route_critical.py` and `route_highspeed.py` (routing scripts receive fixed via endpoints rather than computing via placement themselves).

**Purpose:** Pre-placing all fanout vias in one dedicated pass produces more reliable, inspectable via locations than computing via placement inline during trace routing. After this script runs, routing scripts only need to connect via-to-via with traces — they do not need to solve via placement.

**Config field used:** `routing_config.py::ROUTING_LAYER_PRIORITY` — ordered list of routable signal layers, most-preferred first. Nets declared in `HS_PAIRS` use their declared layer. All other nets use the priority list to determine which layer their fanout via should target.

```
cd [SCRIPTS_DIR]
python route_fanout_vias.py            # dry run — report only, no changes written
python route_fanout_vias.py --apply    # place vias and save PCB
```

*Full procedure, stop conditions, and iteration loop to be documented once script is complete.*

---

*End of Phase 10 Part 0.75 insert.*

---

## SECTION 4 — Phase 10 Part A: Switching Loop Routing (replaces existing Part A)

*Replaces the existing "Part A — Switching loops" block in Phase 10, which previously
instructed writing route_critical.py from scratch. The script now lives in [SCRIPTS_DIR]
and is fully generalized — all project data comes from routing_config.py.*

---

**Part A — Switching loop routing.**

Run `[SCRIPTS_DIR]/route_critical.py`. This script routes each switching converter loop
defined in `routing_config.py::SWITCHING_LOOPS`. It places tracks and vias only — it does
not move any components. Component positions are final after Part 0.5.

**Step A1 — Add SWITCHING_LOOPS to routing_config.py.**

Add one entry per switching converter. Every field is required; there are no defaults.

```python
SWITCHING_LOOPS = [
    {
        # Human-readable name for log output
        "name":               "TPS54561 buck — U1",

        # Routing layer for all traces in this loop (typically F.Cu for a buck converter)
        "layer":              "F.Cu",

        # Trace widths in mm — size from the loop's peak current and fab copper weight.
        # Rule of thumb at 1 oz Cu: 1 mm/A for < 10 °C rise; 0.5 mm/A for < 20 °C rise.
        "sw_width_mm":        2.0,   # switching node: IC SW pin → inductor (peak current)
        "out_width_mm":       2.5,   # output rail: inductor → output caps (full load current)
        "vin_width_mm":       2.0,   # input rail: IC VIN pin → input caps
        "bootstrap_width_mm": 0.3,   # bootstrap / control signal (< 0.5 A — narrow is fine)

        # Component references — must match KiCad reference designators exactly
        "ic_ref":             "U1",          # switching IC
        "inductor_ref":       "L1",          # output inductor
        "bootstrap_cap":      "C_BOOT1",     # bootstrap cap (BOOT net)

        # Net names — must match KiCad net names exactly (case-sensitive)
        "sw_net":             "BUCK_SW",     # switching node net
        "vin_net":            "VMAIN",       # input rail net
        "out_net":            "+5V",         # output rail net
        "gnd_net":            "GND",         # ground return net (for GND vias)
        "bootstrap_net":      "BUCK_BOOT",   # bootstrap net

        # Cap lists — may contain more than one ref; each is routed individually
        "input_caps":         ["C_VIN1"],    # input decoupling caps (VIN net)
        "output_caps":        ["C_OUT1"],    # output decoupling caps (OUT net)

        # Neckdown — used when a trace is wider than the pad it must exit.
        # neckdown_width_mm: narrow stub width emitted directly from the pad.
        # neckdown_max_mm:   maximum stub length before transitioning to full width.
        # The script walks from the pad center outward until a full-width trace would
        # clear all adjacent pins on the same footprint. Set neckdown_width_mm to the
        # widest trace that fits between the two closest IC pins. Set neckdown_max_mm
        # to the IC body half-width plus clearance — a good starting value is 2.0 mm.
        # Omit both keys (or set to None) if the IC pads are wider than the trace width.
        "neckdown_width_mm":  1.0,
        "neckdown_max_mm":    2.0,
    },
]
```

**Neckdown field guidance:**

| Condition | Setting |
|---|---|
| Any trace width > min IC pad dimension | Set `neckdown_width_mm` < min IC pad width; set `neckdown_max_mm` ≥ IC body half-width + 0.5 mm |
| All trace widths ≤ all IC pad widths | Omit `neckdown_width_mm` and `neckdown_max_mm` (no neckdown emitted) |
| Mixed (some traces need neckdown, some don't) | Set neckdown fields; the script only applies them when `trace_width > pad_min_dim` |

How the script uses neckdown: for each connection endpoint, if `trace_width > pad_min_dim`, the
script walks outward from the pad center along the primary routing direction until a full-width
trace at that position would clear all adjacent IC pins. It emits a short stub at
`neckdown_width_mm` from the pad to that exit point, then routes the rest of the connection
at full `width_mm`. The neckdown is emitted at both endpoints of each connection independently.

**What the script routes per loop:**

| Connection | Net | Layer | Notes |
|---|---|---|---|
| `ic_ref` → `inductor_ref` | `sw_net` | `layer` | Switching node — minimize length and area |
| `inductor_ref` → each `output_caps` | `out_net` | `layer` | Output rail |
| `ic_ref` → each `input_caps` | `vin_net` | `layer` | Input rail |
| `ic_ref` → `bootstrap_cap` | `bootstrap_net` | `layer` | Control signal — narrow trace |
| GND via beside each `output_caps` | `gnd_net` | `layer` | Short stub + via; via sized from `CLEARANCE_AUDIT` |
| GND via beside each `input_caps` | `gnd_net` | `layer` | Same |

**Step A2 — Run the dry run and iterate.**

```
cd [SCRIPTS_DIR]
python route_critical.py            # dry run — no PCB changes
python route_critical.py --apply    # apply tracks and vias, save PCB
```

The dry run output has one section per loop connection. Each connection reports:

| Output line | Meaning |
|---|---|
| `neckdown A: stub Xmm for Y.YYmm` | Neckdown stub emitted at source pad; Y.YY is stub length |
| `neckdown B: stub Xmm for Y.YYmm` | Neckdown stub emitted at destination pad |
| `[rectilinear] N segment(s)` | H-V or V-H path found — preferred; check KiCad that route looks sensible |
| `[rectilinear blocked] → maze` | Both L-shapes blocked; falling back to A* |
| `[maze] N segment(s)` | A* found a path; more segments = more turns; inspect in KiCad |
| `CONFLICT: no path for NET REF→REF` | Neither router found a clear path; connection skipped |
| `WARNING: REF pad is on B.Cu, routing on F.Cu` | Pad layer mismatch — a via is needed at that endpoint |
| `WARNING: no clear GND via position near REF` | No cardinal-direction clearance found for GND via |
| `SKIP loop — already routed: NET, NET` | Tracks already exist on those nets; loop was skipped |

**CONFLICT diagnosis:**

A CONFLICT means the trace cannot fit between obstacles using either the rectilinear or A* router.
The script logs the obstacle list within ±2 mm of the blocked route. Read those obstacles:
- If they are adjacent IC pins: the neckdown stub is not long enough to escape the IC body. Increase
  `neckdown_max_mm` or reduce `neckdown_width_mm` so the stub has more room.
- If they are nearby capacitors or resistors from a different group: those components are too close
  to the routing corridor. Return to Part 0 and increase the relevant ALIGNMENT_GROUPS offset to
  create more clearance.
- If they are existing tracks from an earlier connection in the same loop: the routing order is
  causing a conflict. Reorder the `input_caps` or `output_caps` lists in `SWITCHING_LOOPS` so
  the most space-constrained connection runs first.

The script exits with code 1 when any CONFLICT occurs. In automation, treat exit code 1 as a
blocking failure — do not proceed to Part B until all connections route cleanly.

**Stop conditions — all must be true simultaneously:**

| Condition | What it means |
|---|---|
| Exit code 0 | No CONFLICT lines — every connection routed successfully |
| Zero layer mismatch warnings | All pads are on the declared routing layer (or vias added) |
| Zero GND via warnings | GND return vias placed beside every cap |
| SW loop area PASS | Switching node pad bounding-box area ≤ 50 mm² (informational heuristic) |
| DRC clean | No copper-to-copper clearance violations after `--apply` |

**Loop C — switching loop area:**

After `--apply`, open the PCB in KiCad and trace each switching loop visually:
IC SW pin → inductor pad 1 → inductor pad 2 → output cap + pad → GND via → IC GND pin → back to IC.
The SW pad bounding-box area reported in the log is a heuristic. For a definitive measurement,
select all segments of the SW net and GND return path in KiCad and read the enclosed polygon area.
Target: enclosed current-loop area ≤ 50 mm². If the loop is larger, shorten the IC-to-inductor
segment or move the inductor closer to the IC (return to Part 0, adjust `ALIGNMENT_GROUPS`).

**Generalization rule — enforced at every step:**

Before writing or modifying any line of `route_critical.py`, ask: "Would this change work
correctly on a completely different PCB with a different switching converter, different component
references, different net names, and a different board topology?" If the answer is no, the change
belongs in `routing_config.py`, not in the script. This includes:
- Any reference to a specific component ref (`"U1"`, `"L1"`, etc.)
- Any hardcoded net name string (`"BUCK_SW"`, `"GND"`)
- Any hardcoded trace width or pad dimension
- Any logic that assumes a specific number of caps, a specific IC package, or a specific layer

**KiCad API rules (enforced in any script edits):**
- `fp.IsLocked()` — route_critical.py does not move footprints, so locking is not relevant.
  Do not add any `SetPosition()` or `SetOrientationDegrees()` calls to this script.
- Layer IDs: resolve at runtime with `board.GetLayerID(loop["layer"])`.
- `board.Remove(item)` — never call; causes SIGSEGV in standalone scripts.
- `board.Save(cfg.PCB_FILE)` — called only in `--apply` mode.

**Script reference:**

| Script | Phase | CONFIG fields to set in routing_config.py |
|---|---|---|
| `route_critical.py` | 10 Part A | `PCB_FILE`, `KICAD_SITE_PKGS`, `REPORTS_DIR`, `SWITCHING_LOOPS`, `CLEARANCE_AUDIT` (for via sizing) |

**Outputs:** Tracks and vias written to `[PCB_FILE]`. Log appended to
`[REPORTS_DIR]/fix_log_phase10.txt`.

**Loop exit criteria:** Exit code 0, zero warnings, DRC clean, all switching loops ≤ 50 mm².

---

*End of Phase 10 Part A insert. Continue with existing Part B — HS differential pairs.*
