# Frame Line Device — Session Context
# Updated 2026-09-21

Read this file at the start of each session for a quick re-briefing.

---

## What This Project Is
Electronic frameline projector that mounts to a cinema camera's lens mount in
place of a lens. Projects configurable aspect ratio framelines directly onto
the camera's sensor or film plane. Used during camera prep, once per
production. No commercial equivalent exists.

## Key Files
- TDL (open questions + device description + buy list): `Frameline- TDL.txt`
  NOTE: filename has a dash and space — `Frameline- TDL.txt`, not `Frameline TDL.txt`
- Current software: `framing_chart_ui_v242.html`
- Next software version to create: `framing_chart_ui_v243.html`
- Auto-restart script: `claude_autorestart.py`
- KiCad V1 project: `Frameline Generator PCB Version 1\Frameline Generator Version 1.kicad_pro`
- KiCad V2 project (active): `E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pro`

## Software Version Rule
ALWAYS create a new numbered file for each HTML change.
Current: v242 → next change saves as v243, etc. Never overwrite.

## Current Hardware Decisions
- Projecting display: BOE VS055QUM-NH0-6KP1 (5.5", 4K, 806 PPI, 100 cd/m²)
  - Sold as panel+HDMI-to-MIPI board bundle by YOURITECH (youritech.com)
  - KP1 chosen over KP0 for bundle convenience; KP0 is 150 cd/m² but panel-only
  - YOURITECH bundle does NOT include backlight driver — power board must provide it
- Power input: 11.5–17V DC, 4-pin male XLR (Neutrik NC4MD-L-B-1)
- External connection (V1): HDMI with Neutrik NAHDMI-W-B locking connector on device
- Relay lens: three candidate options (see TDL RELAY LENS section)

## Lens Mounts (with specs)
- ARRI PL: 52mm FFD, 36×24mm gate
- ARRI LPL: 44mm FFD, 54.1×25.6mm gate
- Panavision PV: 57.15mm FFD, 36×24mm gate
- Panavision SP70: 40mm FFD, 40.96×21.60mm gate
- IMAX: FFD proprietary/unknown — future support only

---

## PCB Version 2 — Compute Module (Frameline_Compute_V2)

**Phase 10 — Routing IN PROGRESS. route_fanout_vias.py has unresolved violations (see fanout section below).**

Raspberry Pi CM5 (no-WiFi) carrier board. CM5 HDMI0 → LT6711A → J_USB_OUT1: DP 1.2 Alt Mode 4K@30fps + 5V VBUS to V1b. CM5 MIPI1 → J_DSI1 (22-pin FPC, Hirose FH12-22S-0.5SH) → Waveshare 8.8" 480×1920 DSI touch display. Dual power input (LEMO 0B 2-pin 11–34V or USB-C PD 15/20V) → ideal diode OR → VMAIN → TPS54561 → +5V.

V2 project folder: `E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\`
V2 session context: `Frameline Generator PCB Version 2\SESSION_CONTEXT.md` (full detail — read at session start)
V2 project params: `Frameline Generator PCB Version 2\PROJECT_PARAMS.md` (IC/connector roster, proximity rules, power rails)
V2 canonical schematic source: `Frameline Generator PCB Version 2\Python Scripts\gen_schematic.py` (rev 0.13, 85 components, 93 PCB footprints)
V2 design guide (project copy): `Frameline Generator PCB Version 2\PCB_Design_Guide_Generic.md`

**V2 board: 58.16 × 86.18 mm, 6-layer 1.6mm ENIG, 93 footprints, 224 nets, 247 unconnected (all pending routing).**

Key ICs: TPS54561 (U1, 5V/5A buck), STUSB4500QTR (U2, USB-C PD 3.0 sink), LT6711A (U3, HDMI→DP bridge, QFN-64), LTC4412HVIS6 (IC1/IC2, ideal diode controllers), PMV50EPEAR (Q2/Q3, ideal diode FETs), AP7333-33 (U5), AP7333-1.8 (U6), NCP1117-1.2 (U7), W25Q32JV (U8, SPI flash). Protection: PRTR5V0U2X ×5 (D_TX1/TX2/AUX/USB/D_CC1), SMAJ36A (D_TVS), 0ZCF0300AF2B polyfuse (F_PWR1), Würth 744232090 CM choke ×2 (L_CM_PWR1/L_CM_USB1).

---

## ⚡ IMMEDIATE NEXT ACTION

**Working baseline: commit `3b4162b` — --apply confirmed clean 2026-10-06.**
**Script HEAD: commit `9afe94d` — corridor phantom depth fix, debug verified.**
# Updated 2026-10-06 (session 27).

134 vias, 0 clearance violations at baseline. Script has corridor phantom fix not yet applied.

### What to do next

1. **Route and Verify** — apply corridor phantom fix (9afe94d), run DRC, inspect board.
   Expected: U3 south face DP_RX2_P at (152.850, 121.720) instead of old (153.700, 121.995).
   No new DRC violations expected.
2. After clean --apply: fix compaction phantom clearance for skip-net pads (r=0 → full extension radius)
3. Rewrite section 7c (axial + 45° diagonal — code-level plan in memory/project_u3_section7c_state.md)
4. Run Route and Verify after section 7c verified in debug

Script execution order: route_fanout_vias.py → route_critical.py → route_highspeed.py

---

## Phase 10 Routing Status (2026-10-06, commit 3b4162b)

**route_fanout_vias.py**: 0 clearance violations. Working baseline confirmed.
- 134 vias total (8 VIPPO + 126 side-exit), 5 implicit keepouts (escape=0)
- 0 clearance violations after zone fill ✓
- DRC total 617: silk (398), via_dangling (126), track_width (68), thermals/annular (17), track_dangling (7), solder_mask (1) — all pre-existing or expected
- Section 7c skip-net stub extension not firing (order-of-operations issue, see IMMEDIATE NEXT ACTION)

**Fix applied this session (session 25):**
Corridor-width pre-filter added before `_sandwiched_trace_stubs` call (~line 4042).
When any adjacent HS pad is within `stub_hw_bare + hs_via_copper_r + clearance` (0.550mm)
of a sandwiched pad's lateral position, the stub is skipped so stagger-2d can place HS vias.
All 3 sandwiched pads (U3/5, U3/8, U3/11) triggered the filter.
Result: U3/6, U3/7, U3/9, U3/10, U3/12, U3/13 all got vias placed.

**PROHIBITED status (2026-09-22 — post session 26):**
- PROHIBITED 1 (skip_ref): ALREADY REMOVED
- PROHIBITED 2 (endpoint check): ALREADY REMOVED
- PROHIBITED 3 (45° snap): **FIXED** — _radial_escape_direction() returns face-perpendicular cardinal; emission sites snap locally; col exit is cardinal
- PROHIBITED 4 (double clearance): ALREADY RESOLVED
- PROHIBITED 5 (bounding circle): ALREADY REPLACED — rectangle helpers wired throughout
- PROHIBITED 6 (AABB): **FIXED** — _pad_obstacle uses rotated corners for obs.bbox (Phase 1 done)
- PROHIBITED 7 (incoherent pair handoff): **FIXED** — [pair-error] fires inside run_passes()

**Implicit keepouts with escape=0 (12 pads, all genuinely blocked):**
- U3/3 (HDMI0_CLK_N), U3/4 (HDMI0_CLK_P) — HDMI CLK pair; U3/4 has 0.687mm escape+via ✓
- U3/11 (LT_1V2, sandwiched) — corridor blocked by U3/10 via at 0.400mm
- U3/18 (LT_XTAL_OUT), U3/22 (DP_AUX_N), U3/25 (DP_AUX_P), U3/50 (LT_SPI_CLK)
- J_DSI1/6 (MIPI1_D1_N), J_DSI1/7 (MIPI1_D1_P), J_DSI1/10 (MIPI1_D3_N)
- SOM1/54 (TOUCH_INT), SOM2/194 (MIPI1_D3_N)

- Live board = script output saved on Frameline_Compute_V2.kicad_pcb (NOT reference zip)
- Reference zip: `...\Frameline_Compute_V2-backups\Frameline_Compute_V2-2026-09-21_100203.zip`

**route_critical.py**: not yet run. Run after fanout script 0 violations achieved.
**route_highspeed.py**: not yet run. Run after route_critical.py.

---

## PCB Version 1b Status (2026-07-04 — Post-GUI-routing measurement + meander attempt)
**Phase 16 + Blocker Fixes + Silkscreen Rule + Zone Outline Fix + ROUTING + Phase 10.7 Meander Insertion + Post-GUI-routing re-measurement (2026-07-04).**
Board saved from KiCad GUI and re-measured. 8/20 diff-pair groups PASS skew <= 0.10 mm. 6/20 FAIL (meander-blocked — all segments too densely packed for scripted insertion). DRC: 18 violations, 22 unconnected.

**Post-GUI-routing measurement (2026-07-04):**
20 pairs measured: 8 PASS (skew = 0.000mm), 6 FAIL (meander blocked), 5 PARTIAL (one net missing), 1 UNROUTED.

V1b project folder: `Frameline Generator PCB Version 1b\`

## Open TBD Items
1. Relay lens final approach and element selection per mount (V1)
2. Touchscreen panel for Version 2 (V2)
3. Power board PCB layout (V1b started — routing needed)
4. TPS65132 I²C programming: pre-programmed at ±5.4V; may need I²C to reach ±6V
5. Mountable stacking design (mechanical)

---

# Fanout Via Script — Requirements and Refactor Plan
# route_fanout_vias.py
# Updated 2026-09-21

---

## !! NEVER-AGAIN LIST — PROHIBITED APPROACHES !!

The following patterns have been introduced and removed (or must be removed) from the
script. They are WRONG. Do NOT re-introduce any of them under any circumstances,
including "just for this board" or "as a temporary workaround."

---

### PROHIBITED 1: `skip_ref` — skipping same-footprint pads from obstacle checks

**What it did:** Added a `skip_ref` parameter to `_keepout_escape_length` that caused
all pads on the same footprint as the escaping pad to be ignored entirely in the pad
obstacle check (section 2).

**Why it was added:** `_point_limit` uses `obs.r = hypot(half_w, half_h) + clearance`
(the bounding circle diagonal), which is far larger than the actual narrow dimension of
QFN pads (e.g. 0.340mm for a 0.650×0.200mm pad vs the actual 0.100mm narrow half).
Adjacent same-face pads at 0.5mm pitch appeared as obstacles that blocked the trace
even though the trace physically fits between them. `skip_ref` was added to bypass this.

**Why it is wrong:**
1. It is a project-specific workaround that silently fails for any other PCB.
2. It allowed vias to be placed directly on adjacent pad copper, causing 3 DRC
   `shorting_items` violations (U3/22 DP_AUX_N, U3/51 LT_SPI_MOSI).
3. It required a second layered patch (endpoint check restricted to skip_ref pads) to
   partially recover, which itself caused false escape=0 results for other pads.

**The correct fix:** Replace `_point_limit`'s bounding-circle pad approximation with
actual rectangular pad geometry. Compute minimum distance from the trace to the
rectangle [left, top, right, bottom], not from trace to bounding-circle center.
When the correct geometry is used, same-face adjacent pads have sufficient lateral
clearance and are not false obstacles. `skip_ref` becomes unnecessary and must be
removed entirely from the function signature and all call sites.

---

### PROHIBITED 2: Endpoint via check scoped to `skip_ref` pads

**What it did:** Added a post-computation check in `_keepout_escape_length` that
validated the via endpoint position against pad obstacles — but only for same-footprint
pads (`getattr(obs, 'ref', '') == skip_ref`).

**Why it was wrong:** It was a patch on top of PROHIBITED 1. Once the bounding-circle
problem is fixed properly (PROHIBITED 1 fix), the endpoint check is not needed for
same-footprint pads (they are correctly handled by section 2). If a genuine endpoint
overlap is possible, it should be caught by `_apply_via_point` called with the correct
rectangular pad radius, not by a special-cased same-footprint filter.

---

### PROHIBITED 3: Arbitrary-angle escape directions

**What it did:** `_radial_escape_direction()` returned a normalized float vector
`(dx / dist, dy / dist)` — an arbitrary angle from the component center to the pad.
For a QFN corner pad this produces traces at angles like 50.3°, 67.1°, etc.

**Why it is wrong:**
1. PCB traces must be at 0°, 45°, or 90° ONLY. No other angles are acceptable.
2. Arbitrary-angle traces produce non-rectangular solder mask apertures that overlap
   adjacent pad openings, causing `solder_mask_bridge` DRC violations.
3. Arbitrary angles make the `_point_limit` rectangular-vs-circle geometry ill-defined
   and harder to correct — the lateral component of adjacent pads changes continuously
   with direction instead of taking one of the well-defined values it has for 0/45/90.

**The correct fix:** After computing the raw radial vector `(dx, dy)`, snap it to the
nearest of the 8 allowed directions:
  `(±1, 0)`, `(0, ±1)`, `(±1/√2, ±1/√2)`

Snapping rule: `atan2(dy, dx)` → round to nearest multiple of 45°.
Pads near the face center snap to the cardinal direction perpendicular to that face;
pads near a corner snap to the 45° diagonal that bisects the two adjacent faces.

This produces a true radial fanout shape with legal trace angles and eliminates the
bounding-circle ambiguity because lateral distances from adjacent pads become exact.

---

### PROHIBITED 4: Double-counted clearance in pad obstacle checks

**What it did:** `_pad_obstacle` stores `obs.r = hypot(half_w, half_h) + clearance` —
clearance is already baked into `obs.r`. Section 2 of `_keepout_escape_length` (and other
callers) then passes `obs.r + trace_half_w + clearance` to `_point_limit`, adding clearance
a second time.

**Why it is wrong:** The effective check is `bounding_circle + trace_half_w + 2×clearance`
instead of `bounding_circle + trace_half_w + clearance`. Every pad obstacle is treated as
~0.15mm larger than it should be, causing legitimate escape corridors to be rejected. This
compounds the bounding-circle inflation from PROHIBITED 5.

**The correct fix:** When `_point_limit` is replaced with rectangle geometry (Fix 2), the
clearance must be applied exactly once: `rect_distance ≥ trace_half_w + clearance`. Do not
pre-bake clearance into `obs.r` and also add it again at the call site.

---

### PROHIBITED 5: Bounding-circle pad geometry used throughout the script

**What it did:** `obs.r = hypot(half_w, half_h) + clearance` is used not only in
`_keepout_escape_length` section 2, but in every function that checks via or trace positions
against pad obstacles: `_clear_of_obs`, `_stub_clear`, `_tighten_vias`, `is_valid_pos`, and
anywhere else `obs.r` drives a clearance decision.

**Why it is wrong:** For a 0.650×0.200mm QFN pad, `obs.r = hypot(0.325, 0.100) = 0.340mm`
before clearance is added — 3.4× the actual narrow half (0.100mm). This systematically pushes
vias further out than necessary across the entire script, not just in the keepout length
computation. Consequence: pads that physically have a clear corridor report escape=0 (violation
of Requirement 1), and vias are placed further from pads than they need to be everywhere.

**The correct fix:** Replace `obs.r`-based circle approximation with actual rectangle geometry
at EVERY call site — `_clear_of_obs`, `_stub_clear`, `_tighten_vias`, `is_valid_pos`, and any
other function that checks pad clearance. Use `obs.bbox` (left, top, right, bottom) and compute
minimum distance from point or segment to the rectangle.

---

### PROHIBITED 6: `GetBoundingBox()` AABB used for rotated pads

**What it did:** `_pad_obstacle` calls `pad.GetBoundingBox()` to get `obs.bbox` for rectangle
distance checks. `GetBoundingBox()` returns the axis-aligned bounding box of the pad shape in
board coordinates — for a rotated pad, this AABB is larger than the actual copper footprint
along both axes.

**Why it is wrong:** A pad rotated to an intermediate angle has an inflated AABB. Any distance
check using `obs.bbox` overstates the effective pad size for rotated pads, producing false
obstacle rejections. This is especially likely on connectors with pads at various orientations.

**The correct fix:** For the trace-corridor check (Fix 2), compute the minimum distance from
the trace centerline to the pad rectangle in the pad's LOCAL coordinate frame (accounting for
pad rotation), then compare against `trace_half_w + clearance`. Do not use the global AABB
directly for clearance decisions on rotated pads.

---

### PROHIBITED 7: Incoherent HS pair handoff when one partner gets escape=0

**What it did:** When a keepout HS pad gets escape=0 (no via, no trace emitted), its
differential partner still gets a placed via at a position computed by step 1e axial extension
and step 1g stagger — both of which assume the partner will also have a via at a known position.

**Why it is wrong:** `route_highspeed.py` receives a pair where one endpoint is a placed via
and the other is a raw pad center. The pair-aware routing logic in the fanout script computed
the placed via's position relative to a phantom partner that has no physical representation on
the board. This violates differential pair routing requirements and produces an incoherent
handoff: the next script cannot route the pair with proper differential separation because the
two endpoints are not symmetrically positioned.

**The correct fix:** When a keepout pad's escape=0 is genuine (all corridors physically blocked),
the partner via placement must also be adjusted — either the partner also reports escape=0 and
neither gets a via, or the script raises an explicit error indicating the pair cannot be fanned
out and requires board-level intervention. Placing one partner via while leaving the other
unresolved is never acceptable.

---

## CORE REQUIREMENTS (user-stated, non-negotiable)

### 1. Every pad gets a via
Every pad requiring a layer transition must get a via placed on the board.
- **Pad large enough for via at exit:** place via at pad exit + neckdown stub.
- **Pad too small for via at exit ("keepout"):** emit escape trace from pad far enough
  to clear surrounding copper, then place the via at the END of that trace.
  `route_critical.py` does NOT place fanout vias — the fanout script places ALL of them.
- escape=0.000mm with no trace and no via is only acceptable when a pad is GENUINELY
  geometrically impossible (adjacent copper physically fills every escape corridor). It
  must never be the result of an over-conservative obstacle check or a workaround patch.

### 2. The script gives every pad space to escape
Via and stub positions must be computed so no pad's escape corridor is blocked.
Sandwiched pads (non-HS pads flanked by HS pairs at tight pitch) must have their
corridors reserved before HS vias are placed.

### 3. Zero DRC harmful violations
No shorts, no clearance errors caused by anything the script places.

### 4. Innermost-first placement order
Most-constrained pads (innermost, most-flanked) get their escape traces placed first.

### 5. Traces at 0°, 45°, or 90° only
No trace — stub, escape, or neckdown — may be placed at any other angle.

### 6. No project-specific fixes
Every algorithmic decision must work on any PCB without configuration. If a fix only
works because of a specific component name, reference, or board topology, it is
project-specific and must be replaced with the general algorithmic solution.

---

## WHAT NEEDS TO BE FIXED (in priority order)

Full implementation plan with exact algorithms: `fanout_improvement_plan.md`

### Phase 1: Fix obs.bbox AABB in `_pad_obstacle` — **DONE**

Rotated-corners obs.bbox is already in the script. No further work needed.

### Phase 2: Remove skip_ref — ALREADY DONE

**AUDIT FINDING:** skip_ref is not present anywhere in the script. No work needed.

### Phase 3: True radial + stagger rewrite + P-offset disable (PROHIBITED 3) — after Phase 1

Four sub-fixes done together (they interact — cannot be done independently):
1. Remove snap from `_radial_escape_direction()` (~line 239) — return true radial angle.
2. Add local snap at keepout/sandwiched trace emission sites in section 6 ONLY (~lines 3700, 3726).
3. Disable lat_off P-offset for radial fanout components — add `_is_radial_fanout_fp()` check
   in P-offset loop (~line 3254); lat_off undefined when P and N have different escape directions.
4. Rewrite step_1g_stagger — MANDATORY when snap is removed:
   Current grouping key `(ref, escape_dx, escape_dy)` gives each pad a unique group after snap
   removal (float angles → 1-member groups → stagger does nothing → vias overlap).
   Change grouping key to `ref` alone. Replace 1D neckdown with closed-form 2D solver:
     A=pad_xi-vx_j, B=pad_yi-vy_j, sep=r_i+r_j+clearance, C=A*edx_i+B*edy_i
     If A²+B² >= sep²: no constraint.
     Else: min n_i = sqrt(C² + sep² - A² - B²) - C

### Phase 4: Column-exit-first stub (false keepouts U3/3, U3/9) — after Phases 1–3

col_dx, col_dy = -escape_dx, -escape_dy (cardinal for 0/90/180/270° footprint rotation).
Verify FULL SEGMENT with `_seg_rect_dist` against ALL obstacles including thermal pad.
If footprint at non-0/90/180/270° rotation: flag error, fall back to keepout.

### Phase 5: HS pair coherence inside run_passes() (PROHIBITED 7) — after Phases 1–4

Suppression fires DURING run_passes() when placement search fails — not after.
Immediately set partner.implicit_keepout=True and retroactively remove from `placed`
if already there. Partner lookup: match ref + complementary P/N suffix in net_name.

---

## WHAT TO PRESERVE (already working correctly)

- Axial-first stub routing in `_route_45deg_stub` (session 15)
- Herringbone cluster stubs with `axial_first=False` (session 16)
- P-offset direction flip away from sandwiched pads (session 20)
- Real PCB_TRACK escape traces for detected sandwiched pads (session 20)
- `_tighten_vias` compaction pass (session 19)
- `_is_radial_fanout_fp()` detection (session 22) — keep, but combine with Fix 1
- Skip `_align_pair` for radial fanout components (session 22) — keep
- All VIPPO logic
- All via sizing (HS vs standard)
- Section 6 keepout escape trace + via emission (session 23)
- step 1e axial extension for N-vias
- step 1g stagger assignment

---

## BOARD INSPECTION SCRIPT (run after --apply)

```python
import sys; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages')
import pcbnew, math
board = pcbnew.LoadBoard(r'E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb')

# Check U3 via positions — should be radially spread, not in columns
u3_vias = []
for t in board.GetTracks():
    if t.Type() == pcbnew.PCB_VIA_T:
        x = pcbnew.ToMM(t.GetPosition().x)
        y = pcbnew.ToMM(t.GetPosition().y)
        if abs(x - 152) < 5 and abs(y - 110) < 5:   # adjust to actual U3 position
            u3_vias.append((x, y))
print(f"U3 region vias: {len(u3_vias)}")
for v in sorted(u3_vias):
    print(f"  {v[0]:.3f}, {v[1]:.3f}")

# Check keepout pads have both trace AND via
tracks = [t for t in board.GetTracks() if t.Type() != pcbnew.PCB_VIA_T]
vias   = [t for t in board.GetTracks() if t.Type() == pcbnew.PCB_VIA_T]
print(f"Total tracks: {len(tracks)}, Total vias: {len(vias)}")
```

---

## KEY LINE NUMBERS (approximate — verify before editing)

  `escape_direction()`:           ~line 195
  `_is_radial_fanout_fp()`:       ~line 193 (before escape_direction)
  `_radial_escape_direction()`:   ~line 193 (after _is_radial_fanout_fp)
  `_pad_obstacle()`:              ~line 466
  `_point_limit()`:               near _pad_obstacle
  `_keepout_escape_length()`:     section 2 ~line 1025, endpoint check ~line 1046
  `_clear_of_obs()`:              search for def _clear_of_obs
  `_stub_clear()`:                search for def _stub_clear
  `_tighten_vias()`:              ~line 705
  `is_valid_pos()`:               search for def is_valid_pos
  `_make_via()`:                  ~line 2661
  `step 1b _align_pair`:          ~line 2918–2953 (two call sites)
  `_fp_by_ref dict`:              in _run() before step 1b (~line 2893)
  `p-offset loop`:                ~line 3068
  `section 6 keepout emit`:       ~line 3700
  `section 6 sandwiched emit`:    ~line 3726

---

## FILES

  Script:       E:\Claude Projects\CC Project Folder\Python Scripts\route_fanout_vias.py
  Config:       E:\Claude Projects\CC Project Folder\Python Scripts\routing_config.py
  Board:        E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb
  This file:    E:\Claude Projects\CC Project Folder\SESSION_CONTEXT.md
  Memory dir:   C:\Users\johnp\.claude\projects\E--Claude-Projects-CC-Project-Folder\memory\
