# Phase 10 Routing — Session Guide

**Project:** Frameline Generator PCB Version 2 (CM5 carrier board)
**Status:** Active — Phase 8 placement signed off, Phase 9 zones complete, Phase 10 in progress.

---

## What We Are Doing

Phase 10 uses a pipeline of scripts for pre-route alignment, critical trace routing, HS
differential pair routing, and verification. All project-specific data lives in
`routing_config.py`. Scripts must contain zero hardcoded component references, net names,
or layer names — the same scripts must work on any project.

**Scripts 0, 1, and 1.5 are complete and verified. Work continues on scripts 2–3.**

### Script Pipeline (execution order)

| # | Script | Job | Location | Status |
|---|---|---|---|---|
| 0 | `routing_config.py`          | Project-specific config — imported by all scripts below | CC Project Folder/Python Scripts/ | Complete |
| 1 | `route_prep_align.py`        | Pre-route placement alignment — columns, rows, rotation optimization | CC Project Folder/Python Scripts/ | Complete |
| 1.5 | `route_clearance_audit.py` | Routing clearance analysis and correction — corridors, via keepout, HS paths, diff-pair sep, neckdowns, board edge, type gap | CC Project Folder/Python Scripts/ | Complete — all 5 dry-run fixes applied 2026-08-07 |
| 1.75 | `route_fanout_vias.py`    | Fanout via placement — places layer-transition vias at every pad that needs to reach a different copper layer, for all nets, before any trace routing begins. Uses `routing_config.py::ROUTING_LAYER_PRIORITY` for layer assignment. | CC Project Folder/Python Scripts/ | In development |
| 2 | `route_critical.py`          | Switching converter loop pre-routing (SW, VIN, OUT + PGND vias) | V2/Python Scripts/ | Written — not behaving satisfactorily |
| 3 | `route_highspeed.py`         | HS differential pair routing, centerline + offset algorithm | V2/Python Scripts/ | Written — not behaving satisfactorily |
| 4 | `verify_hs_sandwich.py`      | Verify HS reference planes | CC Project Folder/Python Scripts/ | Existing |
| 5 | `verify_highspeed.py`        | Verify HS layer, mid-via, skew, inter-lane spread | CC Project Folder/Python Scripts/ | Existing |
| 6 | `lock_highspeed_nets.py`     | Lock HS segments before FreeRouting | CC Project Folder/Python Scripts/ | Existing |
| 7 | `run_freerouting.py`         | Phase 10a autoroute | CC Project Folder/Python Scripts/ | Existing |
| 8 | `score_autoroute.py`         | Phase 10a quality gate | CC Project Folder/Python Scripts/ | Existing |

**Script directories:**
- Generalized scripts: `E:\Claude Projects\CC Project Folder\Python Scripts\`
- V2 project scripts: `E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Python Scripts\`
- `route_critical.py` and `route_highspeed.py` currently live in the V2 project directory and
  contain hardcoded paths and references that should be eliminated in favor of importing from
  `routing_config.py`. This is a known issue to fix, not a completed migration.

---

## Session Workflow

Read the seven files listed below, confirm the four knowledge checks, then report current
script issues and propose fixes. All proposed fixes are discussed before applying.

| Step | Who | Action |
|---|---|---|
| Declare snapshot | Designer | Saves in KiCad, tells Claude the snapshot name |
| Save backup | Claude | Copies main → named backup in backups dir |
| Run scripts | Claude | Runs against main PCB file |
| Review | Designer | Opens main in KiCad, reports what is wrong |
| Discuss | Both | Diagnose root cause; propose general fix |
| Apply fix | Claude | Only after designer approves |
| Revert | Claude | Copies named backup back over main on request |

**Backup directory:**
`E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2-backups\`

**Snapshot naming:**
```
Frameline_Compute_V2_PRE_ROUTE_YYYYMMDD.kicad_pcb     — unrouted, post-Phase-8 placement
Frameline_Compute_V2_ALIGNED_YYYYMMDD.kicad_pcb       — after route_prep_align.py
Frameline_Compute_V2_HS_ROUTED_YYYYMMDD.kicad_pcb     — after route_critical + route_highspeed
```

**Rules:**
- Never touch named backup files — only the main PCB file is modified by scripts.
- `route_critical.py` and `route_highspeed.py` always run together in order.
- All proposed script changes are discussed before applying.
- Locked components (KiCad LOCKED flag, `fp.IsLocked()`) are never moved or rotated.

---

## Generalization Requirement (All Scripts)

Before proposing any fix, ask: "Would this work on a completely different PCB with different
component references?" If the answer is no, it is a project-specific fix — discard it and
find the algorithmic root cause instead. This applies even when the algorithmic fix is harder.

---

## Files to Read at Session Start

Read all seven files before doing anything else. After reading, confirm you can answer
from memory: (a) every component ref and its function, (b) every row of
PROXIMITY_RULES_TABLE, (c) the rotation_symmetry value for every component in
ROTATION_SYMMETRY (routing_config.py), (d) every HS interface name, layer, and pair net
names. Do not proceed until you can.

| # | File | What to extract |
|---|---|---|
| 1 | `E:\Claude Projects\CC Project Folder\SESSION_CONTEXT.md` | Current phase status and what is complete |
| 2 | `E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\PROJECT_PARAMS.md` | IC ROSTER (every ref, package, function), CONNECTOR ROSTER, POWER RAIL TABLE (every net name), HIGH-SPEED SIGNAL INVENTORY (interface names, layers, impedances, pin assignments), PROXIMITY_RULES_TABLE (all rows — ref_A, ref_B, max_dist_mm, type, net_hint), CM5 SIGNAL MAPPING (SOM1/SOM2 pin numbers per interface) |
| 3 | `E:\Claude Projects\CC Project Folder\Python Scripts\proximity_rules_config.py` | Full RULES list (all entries), POWER_NET_EXACT, FACE_OVERRIDES, NO_FORCE_REFS, NO_NORMALIZE_REFS |
| 4 | `E:\Claude Projects\CC Project Folder\Python Scripts\routing_config.py` | ROTATION_SYMMETRY (per-component symmetry class), HS_PAIRS (all interface names, layers, net names), HS_NETS, LANE_GROUPS, SWITCHING_LOOPS, ALIGNMENT_GROUPS |
| 5 | `E:\Claude Projects\CC Project Folder\Python Scripts\route_prep_align.py` | Current implementation — group types supported, overlap-check layers, what config keys it reads |
| 6 | `E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Python Scripts\route_critical.py` | Current implementation — what nets and refs are hardcoded vs read from config |
| 7 | `E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Python Scripts\route_highspeed.py` | Current implementation — HS_PAIRS definition, layer references (check for numeric layer constants vs runtime lookups), centerline algorithm |

**Note on component_geometry.json:**
The file at `Frameline Generator PCB Version 2\component_geometry.json` is 330 KB and
cannot be read in one shot. Rotation symmetry for all unlocked components is already
extracted in `routing_config.py::ROTATION_SYMMETRY` (file 4 above). To check locked
fields or current positions for a specific component, use Grep with an offset read —
do not attempt to read the whole file.

---

## Context

Phase 8 placement is signed off. Phase 9 zones are complete. We are in Phase 10.

`routing_config.py` contains all project-specific data: paths, ROTATION_SYMMETRY,
HS_PAIRS, HS_NETS, LANE_GROUPS, SWITCHING_LOOPS, and ALIGNMENT_GROUPS. Scripts should
import from it rather than defining these structures inline.

**route_critical.py and route_highspeed.py** currently live in the V2 project scripts
directory and still hardcode paths, net names, and KiCad layer constants that must be
eliminated. Known API issue: the V2 `route_highspeed.py` uses `pcbnew.B_Cu` (a numeric
constant) instead of `board.GetLayerID("B.Cu")` — this is a bug per the KiCad API rules
below.

---

## KiCad 10 API Rules

- pcbnew path: `C:\Program Files\KiCad\10.0\bin\Lib\site-packages`
- `board.Remove(item)` causes SIGSEGV in standalone scripts — never call it. Scripts only
  add or modify items. Clearing existing routing is done manually in KiCad GUI.
- **Locked components:** `fp.IsLocked()` — skip any footprint that returns True. No exceptions.
- **Layer IDs:** always resolve at runtime with `board.GetLayerID("F.Cu")` — never numeric
  IDs or `pcbnew.B_Cu` / `pcbnew.F_Cu` constants. These are not stable across KiCad builds.
- Coordinates: `pcbnew.FromMM(x)` → internal; `pcbnew.ToMM(x)` → mm.
- Move footprint: `fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))`
- Set rotation: `fp.SetOrientationDegrees(angle)`
- **HS routing:** use centerline + perpendicular offset — never route P and N pad-to-pad
  independently (causes crossing/shorts when lateral ordering flips).

---

## V2 Routing Targets (reference for route_critical.py and route_highspeed.py)

### Switching Loop
- TPS54561 U1 buck, inductor L1 (SRR6038-100Y 10µH)
- SW net: `BUCK_SW`, VIN net: `VMAIN`, OUT net: `+5V`, GND: `GND`
- Bootstrap: `BUCK_BOOT`, soft-start: `BUCK_SS`, comp: `BUCK_COMP`/`COMP_MID`
- Input caps: `C_VIN1`, output caps: `C_OUT1`, bootstrap cap: `C_BOOT1`
- Layer: F.Cu, power trace widths: 1.5–2.5mm, control: 0.3mm
- Loop C target: enclosed SW loop area ≤ 50 mm²

### High-Speed Interfaces (all in routing_config.py::HS_PAIRS)
| Interface | Layer | Impedance | Intra-pair skew limit |
|---|---|---|---|
| HDMI0 CLK + TX0–TX2 (SOM2 → U3) | In2.Cu | 100Ω | 0.127mm |
| DP TX source: LT_TX1/TX2/AUX (U3 → AC caps) | In2.Cu | 100Ω | 0.127mm |
| DP TX connector: DP_TX1/TX2/AUX (ESD → J_USB_OUT1) | In2.Cu | 100Ω | 0.127mm |
| MIPI1 CLK + D0/D1 (SOM2 → J_DSI1) | In2.Cu | 100Ω | 0.127mm |
| USB 2.0 D+/D− (J_USB_IN1 → D_USB1 → SOM2) | F.Cu | 90Ω | 0.500mm |

Inter-lane limits: HDMI0 0.50mm, DP TX source 0.45mm, DP TX conn 0.45mm, MIPI1 0.50mm.
