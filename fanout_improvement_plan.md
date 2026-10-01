# Fanout Via Placement Improvement Plan
# Target: match manually-routed reference board (Sep 21 10:02 zip)
# Written: 2026-09-21

---

## Reference Files

- **Live board (currently manually-routed reference):**
  `E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb`
- **Correct reference** is extracted from (root-level .kicad_pcb only, NOT Backups/ subfolder):
  `...\Frameline_Compute_V2-backups\Frameline_Compute_V2-2026-09-21_100203.zip`
- **Script output (saved for comparison):**
  `C:/Temp/Frameline_script_fanout_20260921.kicad_pcb`
- **Script to edit:**
  `E:\Claude Projects\CC Project Folder\Python Scripts\route_fanout_vias.py`
- **PROHIBITED list (mandatory read before any edit):**
  `E:\Claude Projects\CC Project Folder\SESSION_CONTEXT.md` — NEVER-AGAIN LIST

---

## Measured Differences: Manual vs Script (U3 North Face)

U3 is a QFN-64 (LT6711A). Center at **(151.9375, 116.8953) mm**.
North face pads 1–16 sit at y=112.795, x ranging 149.100–155.100 (0.400mm pitch).

### HDMI0 HS Pads — Via Placement Comparison

| Pad | Net | Pad pos (x,y) | Manual via | Manual dist | Manual angle | Script via | Script dist | Script angle |
|----:|-----|---------------|-----------|-------------|-------------|-----------|-------------|-------------|
| 3 | HDMI0_CLK_N | (154.300, 112.795) | (154.614, 111.664) | 1.174mm | −75° | *(keepout — no local via)* | — | — |
| 4 | HDMI0_CLK_P | (153.900, 112.795) | (154.140, 111.082) | 1.730mm | −82° | (153.981, 111.320) | 1.477mm | −87° |
| 6 | HDMI0_TX0_N | (152.500, 112.795) | (153.250, 110.350) | 2.450mm | −87° | (153.241, 110.834) | 1.967mm | −86° |
| 7 | HDMI0_TX0_P | (152.100, 112.795) | (152.600, 109.750) | 3.047mm | −92° | (152.818, 107.570) | 5.226mm | −89° |
| 9 | HDMI0_TX1_N | (151.500, 112.795) | (151.344, 110.135) | 2.718mm | −102° | *(keepout — no local via)* | — | — |
| 10 | HDMI0_TX1_P | (151.100, 112.795) | (150.616, 110.434) | 2.521mm | −111° | (151.618, 107.570) | 5.226mm | −89° |
| 12 | HDMI0_TX2_N | (150.300, 112.795) | (149.595, 111.112) | 2.013mm | −123° | (150.700, 107.379) | 5.417mm | −90° |
| 13 | HDMI0_TX2_P | (149.900, 112.795) | (149.000, 111.600) | 1.766mm | −137° | (150.181, 110.921) | 1.879mm | −94° |

Angle convention: standard math (x=east=0°, y=north=90°), measured from pad center TO via center.

**Summary:**
- Manual: vias 1.2–3.1mm from pad, fanned radially (angles from −75° to −137°)
- Script: vias 1.5–7.5mm from pad, almost all snapped to −90° (pure north) or −87° to −94°

### Manual Escape Stub Geometry for U3/3 and U3/9 (currently false keepouts)

U3/3 (HDMI0_CLK_N, 0.650×0.200mm pad):
- Segment 1: straight south from pad along long axis — (154.300, 112.795) → (154.300, 112.000), length 0.795mm, width 0.127mm
- Segment 2: 45° NE diagonal — (154.300, 112.000) → (154.614, 111.686), length 0.444mm, width 0.127mm
- Segment 3: tiny cap — (154.614, 111.686) → (154.614, 111.664), length 0.022mm
- Via at (154.614, 111.664), dist 1.174mm from pad, drill 0.300mm / OD 0.600mm

U3/9 (HDMI0_TX1_N, 0.650×0.200mm pad):
- Segment 1: straight south from pad — (151.900, 112.795) → (151.900, 111.650), length 1.145mm, width 0.127mm
- Segment 2: 45° SW diagonal — (151.900, 111.650) → (151.350, 111.100), length 0.778mm, width 0.127mm
- Via at (151.344, 110.135), dist 2.718mm from pad

Observation: the escape goes SOUTH (parallel to pad long axis, away from adjacent pads) first, then doglegs 45° to reach the via. This is not a radial escape — it's a "column exit first, then diagonal" escape. The south-first segment passes BETWEEN sibling HS pads (0.400mm pitch → 0.273mm clear corridor after trace + clearance).

### LT_1V2 / LT_3V3 Shared Vias (manual)

Manual uses a small number of shared power vias served by on-copper traces:
- LT_1V2: ONE via at (157.125, 117.295) — east of U3 — serves U3 pads 2, 5, 11, 14, 15, 21, 32, 35, 46, 55 (10 pads)
- LT_3V3: ONE via at (147.075, 114.895) — west of U3 — serves U3 pads 8, 20, 30 (3 pads)
- LT_1V8: ONE via at (153.745, 123.615) — south of U3 — serves U3 pads 38, 43 (2 pads)

Script uses 2–3 smaller clusters per power net with vias in different positions.

---

## Trace Path Analysis — Manual vs Script (U3 North Face Pads 1–16)

**This section was added after the initial plan was written. The trace PATH geometry
is as critical as the via positions. The script's fundamental failure is not just
via placement — it also fails to produce the correct stub shapes.**

### Via Coverage: Manual vs Script

| Board   | Pads with vias | Pads with dangling stub (no via) | Pads with no stub at all |
|---------|---------------|----------------------------------|--------------------------|
| Manual  | 10/16         | 0/16                             | 6/16 (power pads — no transition needed) |
| Script  | 5/16          | 3/16 (pads 7, 10, 12)           | 8/16 (pads 2, 3, 5, 9, 11, 14, 15, 16) |

The script places vias for only 5 of the 16 pads (pads 1, 4, 6, 8, 13). Of those,
pads 7, 10, and 12 have escape traces that extend 5.1–5.4mm northward with NO
terminating via — these are dangling stubs that route_highspeed.py cannot use.
Pads 2, 3, 5, 9, 11, 14, 15, 16 have neither stub nor via.

### The Canonical Manual Stub Shape: "N-jog-N" (North → diagonal → North)

**100% of manual escape stubs exit the pad due north (90°, perpendicular to the
pad's long axis) as the first segment.** No stub in the manual reference uses a
diagonal or angled first segment off the pad.

The dominant 3-segment shape used in the manual routing:
1. **Segment 1:** straight north (due north, +y direction in KiCad = visually upward toward north face edge)
2. **Segment 2:** 45° diagonal (NE or NW depending on which side the via is on)
3. **Segment 3:** tiny cap segment into via (often < 0.05mm, aligns via center exactly)

This "N-jog-N" shape allows vias to be fanned radially (at different x positions)
while always exiting the pad in the perpendicular direction, keeping the escape
stub perpendicular to the pad row. This is what `_route_45deg_stub` already
generates when given a correctly-positioned via — the bug is in WHERE the via
is placed, not in the stub shape function itself.

### Script Stub Geometry vs Manual

| Pad | Net | Script behavior | Manual behavior |
|----:|-----|-----------------|-----------------|
| 1 | HDMI0_CLK_N? | Diagonal stub off pad (NW direction) + via | North stub + 45° jog + via |
| 3 | HDMI0_CLK_N | No stub, no via (implicit keepout) | South exit 0.795mm → NE 0.444mm → via at 1.174mm |
| 4 | HDMI0_CLK_P | Diagonal stub off pad + via | North stub + 45° jog + via |
| 6 | HDMI0_TX0_N | North stub + via (close match) | North stub + NE jog + via |
| 7 | HDMI0_TX0_P | North stub 5.1mm, NO via | North stub + NE jog + via at 3.0mm |
| 9 | HDMI0_TX1_N | No stub, no via (implicit keepout) | South exit 1.145mm → SW 0.778mm → via at 2.718mm |
| 10 | HDMI0_TX1_P | North stub 5.4mm, NO via | North stub + NW jog + via at 2.5mm |
| 12 | HDMI0_TX2_N | North stub 5.4mm, NO via | North stub + NW jog + via at 2.0mm |
| 13 | HDMI0_TX2_P | North stub + via (close match) | North stub + NW jog + via |

**Key findings:**
1. Script pads 1 and 4 use diagonal exits directly off the pad — manual always exits north first.
2. Script pads 7, 10, 12 have 5mm+ north stubs because the 45° snap collapses all vias
   to the same north corridor → stagger pushes them impossibly far → via is never placed,
   leaving a bare dangling stub.
3. Script pads 3 and 9 (0.650×0.200mm narrow pads) are false keepouts. The manual
   handles them with a column-exit-first pattern (south first along the long axis, then
   45° diagonal) — see detailed geometry in the section above.

### Why Script Pads 7, 10, 12 Have Dangling Stubs With No Via

Step-by-step failure:
1. `_radial_escape_direction()` snaps all north-face HS pad escape directions to 90° (due north)
2. `step_1g_stagger` assigns increasing neckdown lengths so each via is further north than the last
3. For pads 7, 10, 12 (HDMI0_TX0_P, TX1_P, TX2_N), the stagger assigns neckdown of ~5mm
4. At 5mm neckdown the search for a clear via position still finds conflict → no via placed
5. But the 5mm escape trace stub IS emitted (section 6 keepout emission)
6. Result: 5mm dangling stub with no terminating via

This is the PROHIBITED 7 failure (incoherent HS pair handoff): TX0_N gets a placed via
at 2.0mm, TX0_P gets a 5mm dangling stub → pair endpoints are completely asymmetric.

### Why the Manual Pattern Works

The manual escape directions are NOT snapped. Each pad in the north face has a different
angular direction from U3 center (ranging −75° to −137° for pads 3→13). The resulting
true radial spread means:
- Each pad occupies a different angular corridor
- Vias are distributed angularly, not pushed along the same north column
- Stagger (if any) separates vias in arc distance, not in the same linear direction
- All vias land within 1.2–3.1mm of their pads

The stub shape ("N-jog-N") is produced naturally by `_route_45deg_stub` when the
via is placed at a non-axial position relative to the pad center — the function
decomposes the (pad→via) vector into axial+diagonal segments. The correct stub
shape is a CONSEQUENCE of the correct via position, not a separate fix.

---

## Root Cause Analysis

### Root Cause 1 — 45° Snap Collapses Radial Fan

`_radial_escape_direction()` computes the true radial angle from U3 center to each pad, then snaps to the nearest 45° multiple.

True radial angles for U3 north-face HS pads range from −60° to −112° continuously. After snapping:
- Pads 3, 4 (radial −60°, −64°): snap to −45°... or to −90°?
  - −60°: |−60−(−45)|=15, |−60−(−90)|=30 → snaps to **−45°**
  - −64°: |−64−(−45)|=19, |−64−(−90)|=26 → snaps to **−45°**
  - Both escape to the same −45° (SE) direction → their via search corridors collide → algorithm pushes one via very far out

- Pads 6, 7, 9, 10, 12 (radial −74° to −107°): snap to **−90°** (pure north)
  - All five escape north → in a 0.400mm pitch row, they immediately hit sibling pad corridors → pushed far north by stagger algorithm

- Pad 13 (radial −112°): |−112−(−90)|=22, |−112−(−135)|=23 → nearly tie, snaps to **−90°** or **−135°** depending on rounding

The snap converts a smooth fan into 2–3 buckets, eliminating the corridor separation the user achieved manually.

**The manual routing does NOT snap escape directions.** Escape directions are continuous arbitrary angles. The resulting traces ARE on 0°/45°/90° because `_route_45deg_stub` converts the (pad→via) vector into a valid 2-segment path regardless of the via's angular position.

### Root Cause 2 — Straight-Radial Search Fails for Narrow-Pad HS Pads

For U3/3 and U3/9 (0.650×0.200mm pads, escape direction nominally ~−60° to −90°):
- Via OD = 0.600mm. Via can't fit inside 0.200mm narrow pad → no VIPPO.
- Script searches outward along escape direction for a clear via position.
- At 0.400mm pitch, any position within ~2mm along the escape direction hits sibling pad copper within 0.150mm clearance. Search fails → implicit keepout.

The manual technique: exit PERPENDICULAR to the row first (along the 0.650mm long axis of the pad, southward — into the space between pad rows, not between pads in the same row). After ~0.8mm the stub clears the sibling pad row. Then dogleg diagonally to the via. This requires a 2-segment "column-exit-first" stub shape, not a straight radial search.

### Root Cause 3 — HS Pair Stagger Pushes _P Vias Very Far North

The `step_1g_stagger` function assigns neckdown lengths so via circles on the same face don't overlap. With all pads snapped to −90°, all vias on the north face share the same escape axis (y). They must be staggered along y. With ~8 HS vias competing for the same northward column, the outermost vias get pushed to y=105mm — ~8mm from the pad at y=112.795mm.

Without the snap, vias fan out angularly and don't share the same escape axis, so stagger only needs to separate adjacent vias by ~0.600mm (one via diameter), naturally placing all vias within ~3mm of their pads.

### Root Cause 4 — Power-Net Cluster Logic Doesn't Cross Faces

The herringbone cluster logic groups same-net pads on the same FACE of a footprint. U3 has LT_1V2 pads on the north face (2, 5, 11, 14, 15), west face (21, 32, 35), east face (46, 55). The cluster logic creates separate clusters per face → 2–3 separate vias instead of 1.

The manual routing places a single via on the east side and routes all faces to it via on-copper traces. The script doesn't implement cross-face power-net consolidation.

---

## Required Code Changes — Full Sequence (Implementation Order)

> **CORRECTION TO SESSION_CONTEXT.md FIX 1:**
> Fix 1 was written when escape directions were used directly as trace directions. It is
> now INCORRECT for via placement. The architecture routes via `_route_45deg_stub` which
> converts any (pad→via) vector into valid 0/45/90 traces. The escape direction is a via
> placement vector, not a trace angle. The correct rule: true radial angles for via
> placement; snap ONLY at direct trace emission sites in section 6. SESSION_CONTEXT.md
> Fix 1 has been updated to reflect this.

> **AUDIT FINDING (2026-09-21):** The plan as written overstated the Phase 1 and Phase 2
> scope. Audit of the actual script revealed that rectangle geometry already dominates all
> key functions, and skip_ref is already gone. The revised plan below reflects actual
> current state. See "Actual Current State vs Plan Assumptions" note in each phase.

---

### Phase 1 — Fix obs.bbox AABB in `_pad_obstacle` [NARROW — ~5 lines]
*(Fixes remaining PROHIBITED 6)*

**AUDIT FINDING:** The three geometry helper functions described in the original plan
(`_rect_dist_rotated`, `_seg_rect_dist`, `_axial_limit_rect`) already exist in the script
under their own names (`_dist_point_to_bbox`, `_seg_bbox_dist`, `_rect_axial_limit`).
All downstream consumers already call the correct rectangle helpers:
- `_clear_of_obs` → `_dist_point_to_bbox` for real pads (not obs.r)
- `_stub_clear` → `_seg_bbox_dist` as authoritative gate
- `_tighten_vias` → calls `_clear_of_obs` → rectangle geometry
- `is_valid_pos` → calls `_clear_of_obs` + `_stub_clear` → rectangle geometry
- `_keepout_escape_length` section 2 → `_rect_axial_limit` with obs.bbox
- PROHIBITED 4 (double clearance): not occurring in real pad path; obs.r not used there

The only remaining PROHIBITED 6 issue: `obs.bbox` is computed from `GetBoundingBox()`
which returns the AABB (axis-aligned bounding box) in board coordinates. For rotated
pads, this AABB is inflated vs the actual copper area.

**Fix 1 — Replace AABB with actual rotated corners in `_pad_obstacle` (~line 473):**

```python
# Replace the GetBoundingBox() line with rotated corner computation:
angle_rad = math.radians(pad.GetOrientation().AsDegrees())
c, s = math.cos(angle_rad), math.sin(angle_rad)
# Rotate all 4 corners of the pad rectangle (half_w, half_h in pad-local frame)
corners_x = [px + half_w*c - half_h*s, px - half_w*c - half_h*s,
             px - half_w*c + half_h*s, px + half_w*c + half_h*s]
corners_y = [py + half_w*s + half_h*c, py - half_w*s + half_h*c,
             py - half_w*s - half_h*c, py + half_w*s - half_h*c]
obs.bbox = (min(corners_x), min(corners_y), max(corners_x), max(corners_y))
```

Also store `obs.half_w`, `obs.half_h`, `obs.angle_deg` (from `pad.GetOrientation().AsDegrees()`)
on the obstacle object. These are needed by the axial limit helper's support function.

**KiCad API note:** `pad.GetOrientation()` returns an `EDA_ANGLE` object in KiCad 10.
Use `pad.GetOrientation().AsDegrees()` — do NOT pass the object directly to `math.radians`.

PROHIBITED 4 and PROHIBITED 5 are already resolved in the existing script. PROHIBITED 6
(AABB) is the only remaining geometry issue, and this 5-line fix closes it.

---

### Phase 2 — Remove skip_ref — **ALREADY DONE**
*(PROHIBITED 1 and PROHIBITED 2)*

**AUDIT FINDING:** `skip_ref` is not present anywhere in `_keepout_escape_length` —
not in the function signature, not in the body, not at any call site. The endpoint check
block (PROHIBITED 2) is also gone. No work required for Phase 2.

If re-reading the code you observe `skip_ref` has reappeared: remove it immediately. It
must never be re-introduced. See NEVER-AGAIN LIST for the full rationale.

---

### Phase 3 — True Radial Escape Direction + Stagger + P-offset [DEPENDS ON PHASE 1]
*(Corrects PROHIBITED 3; also fixes step_1g_stagger and disables P-offset for radial fanout)*

**Note on stagger grouping (critical side-effect of snap removal):** The current
`step_1g_stagger` groups vias by `(ref, escape_dx, escape_dy)`. With the 45° snap in
place, most pads on the same face share the same snapped direction and group together.
After snap removal, each pad gets a unique float radial angle → each group has 1 member
→ stagger does nothing → vias may physically overlap. Fix 3D below addresses this.

Three sub-fixes that must be done together because they interact.

---

#### Fix 3A — Remove snap from `_radial_escape_direction()` (~line 240)

```python
# Remove:
angle = math.atan2(dy, dx)
snapped = round(angle / (math.pi / 4.0)) * (math.pi / 4.0)
return (math.cos(snapped), math.sin(snapped))

# Replace with:
angle = math.atan2(dy, dx)
return (math.cos(angle), math.sin(angle))
```

---

#### Fix 3B — Snap at direct trace emission sites (section 6, ~line 3910+)

At the keepout and sandwiched trace emission blocks, escape direction is used directly as
trace angle. Snap it locally before computing trace endpoints:

```python
_snap_a = round(math.atan2(_edy, _edx) / (math.pi / 4.0)) * (math.pi / 4.0)
_edx_snap = math.cos(_snap_a)
_edy_snap = math.sin(_snap_a)
# Use _edx_snap, _edy_snap for SetStart / SetEnd — NOT the raw _edx, _edy
```

---

#### Fix 3C — Disable P-offset (lat_off) for radial fanout components

The P-offset mechanism assigns a lateral offset to the P via relative to the N via,
where "lateral" is defined relative to a shared escape axis. With true radial escape
directions, P and N escape at different angles — they do not share an escape axis.
Applying `lat_off` in a direction that is no longer perpendicular to both escape
directions produces geometrically incorrect via positions.

**Fix:** In the P-offset assignment loop, check `_is_radial_fanout_fp(fp)`. If True,
set `lat_off = 0.0` for that pad. The natural angular separation between P and N radial
angles (each pad has its own continuous radial angle from the component center) provides
the necessary separation. `step_1g_stagger` (Fix 3D) handles any remaining via circle
overlap.

```python
# In the p-offset loop, before assigning lat_off:
if _is_radial_fanout_fp(_fp_by_ref.get(v.ref)):
    v.lat_off = 0.0
    continue
```

---

#### Fix 3D — Rewrite step_1g_stagger for 2D angular separation

Currently step_1g_stagger staggers vias along a shared face escape axis. With diverse
radial directions, two vias on the same face escape at different angles — the 1D stagger
assumption breaks. Two vias escaping at −87° and −92° can still have overlapping circles
at their minimum neckdown lengths even though they diverge slightly.

**Algorithm:** Replace the 1D stagger with a 2D closed-form constraint solver.
For each via i (in sorted order — innermost first, i.e., smallest distance from pad to
component center), find the minimum neckdown `n_i` satisfying via circle separation
against all already-assigned vias j:

```
Via i position: (pad_x_i + edx_i * n_i,  pad_y_i + edy_i * n_i)
Via j position: (vx_j, vy_j)  [already assigned]
Separation requirement: distance >= r_i + r_j + clearance  (call this sep_ij)

Distance^2 = (pad_x_i + edx_i*n_i - vx_j)^2 + (pad_y_i + edy_i*n_i - vy_j)^2
Let A = pad_x_i - vx_j,  B = pad_y_i - vy_j
= (A + edx_i*n_i)^2 + (B + edy_i*n_i)^2
= n_i^2 + 2*(A*edx_i + B*edy_i)*n_i + (A^2 + B^2)   [since edx^2+edy^2=1]
= n_i^2 + 2*C*n_i + D

If D >= sep^2: pads already separated at zero neckdown — no constraint from via j.
Else: minimum n_i = sqrt(C^2 + sep^2 - D) - C
```

Implementation:
```python
def _stagger_neckdown_2d(v, already_assigned, base_neckdown, clearance):
    """Return minimum neckdown for via v clearing all vias in already_assigned."""
    n_min = base_neckdown
    r_i = v.via_drill_mm / 2.0 + v.via_annular_mm
    for vj in already_assigned:
        r_j = vj.via_drill_mm / 2.0 + vj.via_annular_mm
        sep = r_i + r_j + clearance
        A = v.pad_x - vj.via_x
        B = v.pad_y - vj.via_y
        D = A*A + B*B
        if D >= sep*sep:
            continue   # already separated at pad positions
        C = A * v.escape_dx + B * v.escape_dy
        n_req = math.sqrt(max(0.0, C*C + sep*sep - D)) - C
        n_min = max(n_min, n_req)
    return n_min
```

Replace the existing step_1g_stagger neckdown assignment with calls to
`_stagger_neckdown_2d`. Sort order: innermost-first (ascending `via_axial_distance` or
ascending `hypot(pad_x - fp_cx, pad_y - fp_cy)` — closest to component center first,
as those have the least angular divergence and are most constrained).

---

### Phase 4 — Column-Exit-First Stub for Narrow-Pad HS Pads [DEPENDS ON PHASES 1–3]
*(Fixes false keepouts for U3/3 and U3/9)*

**Problem:** U3/3 (HDMI0_CLK_N) and U3/9 (HDMI0_TX1_N), 0.650×0.200mm pads.
Via OD 0.600mm > 0.200mm narrow dimension → no VIPPO. Straight-radial search northward
hits sibling pads at 0.400mm pitch. After Phase 1, `_axial_limit_rect` will correctly
model the 0.200mm narrow dimension, but the north corridor is genuinely blocked by
sibling HS pads. Column-exit-first is needed.

**Important constraint:** The column exit goes south, toward the component interior (the
thermal pad). The manual routing demonstrates this gap exists, but the thermal pad must
be included in all obstacle checks — not just sibling pads.

**Fix 4 — Column-exit fallback:**

After normal radial search returns escape=0 for a non-VIPPO HS pad, try:

1. **Column exit direction:** `col_dx, col_dy = -escape_dx, -escape_dy` (opposite of face
   normal). For north-face pads: escape = (0, −1), col = (0, +1) = due south. Cardinal
   direction for face-aligned QFN (0°/90°/180°/270° rotation) → PROHIBITED 3 compliant.
   Note: this approach requires the footprint is placed at a 0/90/180/270° rotation. If a
   QFN were placed at 45°, the col exit would be diagonal — flag this as an error and fall
   back to keepout rather than emitting a non-0/45/90 segment.

2. **Column exit length:** `pad_long/2.0 + pitch/2.0 + clearance`. For U3/3:
   `0.325 + 0.200 + 0.150 = 0.675mm`. Search from this minimum upward in STEP_MM
   increments to a maximum of 2.0mm.

3. **Verify the full col exit SEGMENT** (pad → col exit endpoint) is clear of ALL pad
   obstacles including thermal pad, using `_seg_rect_dist`:
   ```python
   for obs in pad_obs:
       if _seg_rect_dist(pad_x, pad_y, col_end_x, col_end_y,
                         obs.cx, obs.cy, obs.half_w, obs.half_h, obs.angle) < clearance:
           col_end_x += col_dx * STEP_MM
           col_end_y += col_dy * STEP_MM
           break  # retry at larger length
   ```

4. **Search for via** from col exit point outward in true radial direction (unchanged
   from normal via search, just starting from a different origin).

5. **If via found:** set `implicit_keepout=False`. Stub shape:
   - Segment 1: `(pad_x, pad_y) → (col_end_x, col_end_y)` — straight in col direction (cardinal ✓)
   - Segments 2+: `_route_45deg_stub(col_end_x, col_end_y, via_x, via_y, escape_dx, escape_dy)` (0/45/90 ✓)

---

### Phase 5 — HS Pair Coherence IN run_passes() [DEPENDS ON PHASES 1–4]
*(Fixes PROHIBITED 7)*

**Critical timing issue in the previous plan:** The previous plan audited escape=0 AFTER
`run_passes()` completes. But `run_passes()` places vias for ALL pads including both HS
partners. By the time escape=0 is detected for one partner, the other partner's via is
already committed to `placed`. Setting `implicit_keepout=True` afterward does not remove
the already-placed via.

**Fix:** The suppression must happen INSIDE `run_passes()`, at the moment when a pad's
placement search fails with no valid position:

```python
# Inside run_passes(), when a pad v finds no valid position:
if no_valid_position_found(v):
    v.implicit_keepout = True
    v.via_x = v.pad_x   # null position
    v.via_y = v.pad_y
    # If this is an HS pad, suppress its partner immediately:
    if v.net_name in hs_map:
        partner = _find_hs_partner(v, pending)
        if partner is not None and not partner.implicit_keepout:
            partner.implicit_keepout = True
            partner.via_x = partner.pad_x
            partner.via_y = partner.pad_y
            print(f"  [pair-error] {v.ref}/{v.pad_num} escape=0 — "
                  f"partner {partner.ref}/{partner.pad_num} also suppressed.")
```

`_find_hs_partner(v, pending)`: find the pad in `pending` with the same `ref` and
the complementary P/N suffix in `net_name` (e.g., `HDMI0_TX1_P` ↔ `HDMI0_TX1_N`).
This is a net-name string comparison — no component-specific logic required.

If the partner has ALREADY been placed (via is in `placed` before the failing pad is
processed), retroactively remove it from `placed` and mark `implicit_keepout=True`.
The sort order (innermost-first, HS before standard) makes this retroactive case rare
but it must be handled.

---

### Phase 6 — Verify Diff-Pair Angular Spread [CHECK ONLY, NO CODE]

After Phase 3, measure P/N via angles for all U3 HDMI0 pairs using the comparison
script. With lat_off disabled (Fix 3C) and true radial angles (Fix 3A), natural angular
separation replaces the explicit P-offset. Verify:
- P and N via pairs for each HDMI0 pair are separated by ≥ 0.600mm (one via diameter)
- No via pair has overlapping copper circles (DRC confirms this)

If any pair is under-separated despite true radial angles (possible for pads 6/7 and
9/10 whose radial angles are nearly equal at −87° to −92°), then re-enable lat_off for
those specific pairs only — but only if angular separation proves insufficient after
measurement. Do not assume it's needed beforehand.

---

### Deferred — Power-Net Shared Via Consolidation [SEPARATE SESSION]

Manual routing places all 10 LT_1V2 U3 pads to one via east of U3. Script places 2–3
clusters. Defer until Phases 1–5 are confirmed working. Design notes in earlier section.

---

## PROHIBITED Patterns — Full Compliance Matrix

Before writing any code, verify the full SESSION_CONTEXT.md PROHIBITED list.

**Current script state (2026-09-21 audit):** P1, P2, P4, P5 are already resolved in the
existing script. P6 has a narrow remaining issue. P3 and P7 are not yet fixed.

| # | Prohibited Pattern | Current State | Fix |
|---|-------------------|--------------|----|
| 1 | `skip_ref` | **ALREADY REMOVED** — not in `_keepout_escape_length` signature or body | Done (no work needed) |
| 2 | Endpoint check scoped to skip_ref | **ALREADY REMOVED** — endpoint check block gone | Done (no work needed) |
| 3 | 45° snap in `_radial_escape_direction()` | **STILL PRESENT** — line 239–241 | Phase 3 Fix 3A |
| 4 | Double-counted clearance | **ALREADY RESOLVED** — real pad path does not use obs.r; clearance applied once | Done (no work needed) |
| 5 | Bounding-circle geometry | **ALREADY REPLACED** — `_clear_of_obs`, `_stub_clear`, etc. use rectangle helpers | Done (no work needed) |
| 6 | GetBoundingBox() AABB | **NARROW REMAINING** — obs.bbox still from `GetBoundingBox()` in `_pad_obstacle` | Phase 1 (~5 lines) |
| 7 | Incoherent HS pair handoff | **NOT FIXED** — run_passes() has no partner suppression | Phase 5 |

Per-phase compliance:

| # | Prohibited Pattern | Phase 1 | Phase 2 | Phase 3 | Phase 4 | Phase 5 |
|---|-------------------|---------|---------|---------|---------|---------|
| 1 | `skip_ref` | ALREADY DONE | — | N/A | N/A | N/A |
| 2 | Endpoint check scoped to skip_ref | ALREADY DONE | — | N/A | N/A | N/A |
| 3 | Arbitrary-angle traces | N/A | N/A | Via placement: true radial ✓; Emission sites: snapped ✓ | Col exit = cardinal ✓; stub via `_route_45deg_stub` ✓; 45° footprint error-check ✓ | N/A |
| 4 | Double-counted clearance | ALREADY DONE | N/A | N/A | One clearance at col-exit seg check ✓ | N/A |
| 5 | Bounding-circle geometry | ALREADY DONE | N/A | N/A | `_seg_bbox_dist` for full segment check ✓ | N/A |
| 6 | GetBoundingBox() AABB | **FIXED** (~5 lines in `_pad_obstacle`, rotated corners) | N/A | N/A | Same helpers ✓ | N/A |
| 7 | Incoherent HS pair handoff | N/A | N/A | N/A | N/A | **FIXED** (suppression inside `run_passes()`, retroactive removal) |

---

## Verification Plan

After each change, run:
```
# Step 1 — clear
"C:\Program Files\KiCad\10.0\bin\python.exe" -c "import sys; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages'); import pcbnew; board=pcbnew.LoadBoard(r'E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb'); [board.Remove(t) for t in list(board.GetTracks()) if not t.IsLocked()]; board.Save(board.GetFileName())"

# Step 2 — run script
"C:\Program Files\KiCad\10.0\bin\python.exe" "E:\Claude Projects\CC Project Folder\Python Scripts\route_fanout_vias.py" --apply 2>&1

# Step 3 — DRC
"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe" pcb drc --output "C:/Temp/drc_fanout.json" --format json "E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb"
```

**Success criteria:**
- shorting_items: 0 (MUST)
- U3/3 and U3/9: local vias placed within 3mm (no longer keepout)
- All U3 north-face HS vias within 3.5mm of their pads (vs current 5–7mm)
- Total via count approximately 128 (same as before)

**Comparison script** (run against both boards):
```python
"C:\Program Files\KiCad\10.0\bin\python.exe" -c "
import sys; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages')
import pcbnew, math
BOARD = r'E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb'
IU = 1e6
board = pcbnew.LoadBoard(BOARD)
vias = [t for t in board.GetTracks() if t.GetClass() == 'PCB_VIA']
for fp in board.GetFootprints():
    if fp.GetReference() != 'U3': continue
    for pad in fp.Pads():
        try: ni = int(pad.GetNumber())
        except: continue
        if not (1 <= ni <= 15): continue
        px = pad.GetX()/IU; py = pad.GetY()/IU; net = pad.GetNetname()
        best = min((v for v in vias if v.GetNetname()==net), key=lambda v: math.hypot(v.GetX()/IU-px, v.GetY()/IU-py), default=None)
        if best:
            vx=best.GetX()/IU; vy=best.GetY()/IU; d=math.hypot(vx-px,vy-py)
            a=round(math.degrees(math.atan2(-(vy-py),(vx-px))))
            print('U3/%-2d %-20s via=(%.3f,%.3f) dist=%.3f angle=%d' % (ni,net[:20],vx,vy,d,a))
        else:
            print('U3/%-2d %-20s NO VIA' % (ni, net[:20]))
"
```

---

## Key Script Architecture Context

**Actual function names from 2026-09-21 audit** (names differ from plan's original proposals):

| Function | Actual name in script | Purpose | Phase |
|----------|----------------------|---------|-------|
| Point-to-rectangle | `_dist_point_to_bbox` | Via position vs pad rectangle — ALREADY EXISTS | — (done) |
| Segment-to-rectangle | `_seg_bbox_dist` | Trace segment vs pad rectangle — ALREADY EXISTS | — (done) |
| Axial corridor limit | `_rect_axial_limit` | Escape distance limit via support fn — ALREADY EXISTS | — (done) |
| `_pad_obstacle()` | `_pad_obstacle` | Builds obstacle — needs half_w, half_h, angle + rotated corners for bbox | **Phase 1** |
| `_clear_of_obs()` | `_clear_of_obs` | Via position vs pad — ALREADY uses `_dist_point_to_bbox` | — (done) |
| `_stub_clear()` | `_stub_clear` | Trace segment vs pad — ALREADY uses `_seg_bbox_dist` | — (done) |
| `_tighten_vias()` | `_tighten_vias` | Via compaction — calls `_clear_of_obs` → rectangle geometry | — (done) |
| `is_valid_pos()` | `is_valid_pos` | Via validity — calls `_clear_of_obs` + `_stub_clear` → rectangle | — (done) |
| `_keepout_escape_length()` | `_keepout_escape_length` | Axial limit — ALREADY uses `_rect_axial_limit`; skip_ref ALREADY GONE | — (done) |
| 2D stagger solver | `_stagger_neckdown_2d` | NEW: closed-form 2D constraint — does not exist yet | **Phase 3** |
| `step_1g_stagger` | in `_run()` ~line 3636 | Grouping by `(ref, edx, edy)` — must change to `ref` alone after snap removal | **Phase 3** |
| `_radial_escape_direction()` | `_radial_escape_direction` | 45° snap STILL PRESENT (~line 239) — must be removed | **Phase 3** |
| `_is_radial_fanout_fp()` | `_is_radial_fanout_fp` | Identifies radial fanout components — unchanged, already works | Phase 3 (consumer) |
| P-offset loop | ~line 3254–3339 | No `_is_radial_fanout_fp()` check — must add to disable lat_off for radial | **Phase 3** |
| Keepout emission (section 6) | ~line 3700 | Add local snap before using escape dir as trace angle | **Phase 3** |
| Sandwiched emission (section 6) | ~line 3726 | Add local snap before using escape dir as trace angle | **Phase 3** |
| `run_passes()` | `run_passes` | No HS partner suppression — must add inside placement loop | **Phase 5** |
| `_route_45deg_stub()` | `_route_45deg_stub` | Generates 0/45/90 trace segments — CORRECT, do not touch | N/A |
| `neckdown_stub_width()` | `neckdown_stub_width` | Neckdown trace width — unchanged | N/A |

The script execution sequence within `_run()`:
1. Classify pads → build `pending` list
2. Assign P-offsets for HS pairs
3. `run_passes()` → placement loop
4. `_tighten_vias()` → compaction
5. Section 5: remove short stubs from board
6. Section 6: emit keepout + sandwiched escape traces
7. Save board

---

## Board State Notes

- The live board at session start should be the manually-routed reference (Sep 21 10:02 zip)
- Script output is saved at `C:/Temp/Frameline_script_fanout_20260921.kicad_pcb`
- When running --apply, always clear unlocked tracks first (CLAUDE.md step 1)
- Do NOT save the script output to the backups directory

---

## Summary of Changes in Priority Order

**Phase 1 — Fix obs.bbox AABB in `_pad_obstacle` (~5 lines) (PROHIBITED 6 remaining)**
- Replace `GetBoundingBox()` AABB with actual rotated corners in `_pad_obstacle`
- Add `obs.half_w`, `obs.half_h`, `obs.angle_deg` fields to obstacle object
- All downstream consumers (`_clear_of_obs`, `_stub_clear`, `_keepout_escape_length`, etc.)
  already use rectangle geometry — only the bbox source needs correcting
- PROHIBITED 1, 2, 4, 5: already done in existing script — no work needed

**Phase 2 — DONE**
- skip_ref and endpoint check already removed. Nothing to do.

**Phase 3 — True radial escape + stagger rewrite + P-offset disable (PROHIBITED 3) — DONE (session 23)**
- Fix 3A: 45° snap removed from `_radial_escape_direction()` — returns true radial angle
- Fix 3B: Local snap at keepout/sandwiched trace emission sites in section 6
- Fix 3C: lat_off P-offset disabled for radial fanout components
- Fix 3D: step_1g_stagger rewritten — `(ref, edx, edy)` → `ref` grouping; 2D `_stagger_neckdown_2d` solver

**Phase 3.5 — Stagger-2D Stub-to-Adjacent-Pad Clearance Check — NOT YET DONE (see below)**
- Root cause: stagger-2d ensures via CIRCLES don't overlap but NOT that the neckdown stub
  SEGMENT clears adjacent pad copper
- Especially severe for U3 corner pads where radial escape angle ≈ 120–125° deviates
  significantly from face-normal, reducing lateral clearance perpendicular to the escape direction
- Full spec and algorithm: see Phase 3.5 section below

**Phase 4 — Column-exit-first stub (fixes false keepouts U3/3, U3/9) — DONE (session 23)**
- Col-exit fallback implemented in keepout escape section for HS keepouts on radial fanout components
- U3/4 (HDMI0_CLK_P) and U3/13 (HDMI0_TX2_P): col 0.475mm → escape 5.000mm ✓
- U3/3 (HDMI0_CLK_N): col 0.475mm → escape 4.520mm ✓
- U3/12 (HDMI0_TX2_N): col-exit also failed — stays keepout (pair suppression fires → U3/13 also keepout)
- Post-computation via endpoint check added: after computing via_x/via_y, validates against all
  pad obstacles using _dist_point_to_bbox; if via endpoint lands in adjacent pad copper, esc_len=0.0

**Phase 5 — HS pair coherence inside run_passes() (PROHIBITED 7) — DONE (session 23)**
- [pair-error] fires inside run_passes() at the point of escape=0 detection
- Immediately suppresses partner via and removes from placed list if already placed
- Confirmed working: U3/4 (HDMI0_CLK_P) escape=0 → U3/3 (HDMI0_CLK_N) also suppressed
  (both then go through keepout col-exit successfully)

**Phase 6 — Verify diff-pair angular spread (check only) — DEFERRED**
- Remaining clearance violations are from stub geometry, not pair separation
- P-offset disable (Fix 3C) confirmed correct; no need to re-enable

**(Deferred) Power-net shared via consolidation — separate session**

---

## Phase 3.5 — Stagger-2D Stub-to-Adjacent-Pad Clearance Check [NOT YET DONE]

### Problem Statement

`_stagger_neckdown_2d` computes the minimum neckdown for via_i such that its copper circle
does not overlap via_j's circle (sep = r_i + r_j + clearance). This ensures no via-to-via DRC
violation. But it does NOT check whether the STUB SEGMENT from pad_i to via_i maintains
clearance against ADJACENT PAD copper (pad_j and other nearby pads).

**Session 24 DRC result:** 6 clearance violations, all involving neckdown stubs vs adjacent U3 pads:

| Track | Adjacent Pad | Actual gap | Deficit |
|-------|-------------|-----------|---------|
| LT_TX2_N stub | U3/33 LT_TX2_P | 0.0082mm | 0.1418mm |
| LT_SPI_MISO stub | U3/51 LT_SPI_MOSI | 0.0601mm | 0.0899mm |
| USB_C_OUT_CC1 stub | U3/29 unconnected | 0.0863mm | 0.0637mm |
| DP_AUX_N stub | U3/21 LT_1V2 | 0.1228mm | 0.0272mm |
| DP_RX2_P stub | U3/43 LT_1V8 | 0.1282mm | 0.0218mm |
| HDMI0_TX0_N stub | U3/5 LT_1V2 | 0.1365mm | 0.0135mm |

### Root Cause Analysis

For QFN-64 (U3), 0.4mm pitch on the west and south faces:
- Pad long axis = 0.650mm (in the escape direction, i.e., perpendicular to face)
- Pad short axis = 0.200mm (parallel to face edge; half = 0.100mm)

For pads on the FLAT FACE SECTIONS (escape direction perpendicular to face):
- Lateral separation between adjacent stub and adjacent pad = 0.4mm pitch - stub half_w
- Required: trace_hw + clearance + pad_short_half = 0.0635 + 0.15 + 0.100 = 0.3135mm
- Available: 0.4 - 0.0635 = 0.3365mm > 0.3135mm → **PASSES** (barely)

For pads NEAR THE U3 CORNERS (escape direction deviates from face-normal by angle θ):
- The component of the 0.4mm face-pitch that is LATERAL to the escape direction:
  `lat_component = 0.4mm × sin(θ_deviation_from_face_normal)`
- At a 33° deviation (escape ≈ 123°, face normal ≈ 90° for west face → deviation = 33°):
  `lat_component = 0.4 × sin(33°) = 0.4 × 0.545 = 0.218mm`
- But the short-axis component that is lateral = 0.100mm × cos(33°) = 0.0839mm
- Total available lateral clearance ≈ 0.218 - 0.0839 = 0.134mm ... rough estimate
- This is less than the required 0.3135mm → **FUNDAMENTAL VIOLATION**

The deviation from face-normal is the key variable. As the escape direction rotates away from
face-perpendicular, the lateral separation to adjacent pads shrinks. At ~15–20° deviation,
the margin reaches zero and any stub violates clearance.

**Critical insight:** For pads where lateral separation is insufficient, simply increasing
the neckdown length does NOT fix the clearance violation. The closest approach of the stub
to the adjacent pad is at the START of the stub (at the pad position), determined purely by
pad geometry and escape direction. Increasing neckdown makes the stub longer but does not
change this minimum lateral distance.

### Fix Algorithm

This is a two-part fix:

**Part A: Inline stub clearance check in step_1g_stagger**

After `_stagger_neckdown_2d` assigns n_min, add a check:

```python
# After computing n_min from _stagger_neckdown_2d:
trace_hw_stagger = v.neckdown_w_mm / 2.0
n_stub = n_min
stub_ok = False
while n_stub <= _MAX_NECKDOWN_MM + 1e-9:
    vx_test = v.pad_x + v.escape_dx * n_stub
    vy_test = v.pad_y + v.escape_dy * n_stub
    ok = True
    for obs in pad_obs:
        if obs.net_name == v.net_name:
            continue
        if obs.bbox is not None:
            if _seg_bbox_dist(v.pad_x, v.pad_y, vx_test, vy_test, obs.bbox) < trace_hw_stagger + clearance:
                ok = False
                break
        else:
            if _pt_to_seg_dist(obs.cx, obs.cy, v.pad_x, v.pad_y, vx_test, vy_test) < obs.r + trace_hw_stagger + clearance:
                ok = False
                break
    if ok:
        stub_ok = True
        n_min = n_stub
        break
    n_stub += STEP_MM
```

But: for corner pads with insufficient lateral separation, `stub_ok` will remain False at ALL n
values (increasing n doesn't help the lateral constraint). The loop exits with stub_ok=False.

**Part B: Col-exit-first fallback when lateral constraint is fundamental**

When the stub clearance check fails at ALL neckdown values (stub_ok=False after loop), the
correct approach is to first exit laterally (get clearance from the adjacent pad), then proceed
radially. This is the same principle as Phase 4 (col-exit-first for HS keepout pads), but
applied here to PLACED vias that fail the stub-pad clearance check.

**Algorithm:**

When stub_ok=False, attempt col-exit:

1. **Determine col direction:** The col direction should maximize lateral separation from
   the violating adjacent pad. For a pad on a face with face_normal direction = face_normal_dx,
   face_normal_dy, the col direction is face_normal (opposite of inward = the direction away
   from component center along the face edge). For cardinal-rotation footprints, this is one
   of (±1, 0) or (0, ±1).

   Compute face_normal by snapping the escape direction to the nearest cardinal:
   ```python
   face_ang = round(math.atan2(v.escape_dy, v.escape_dx) / (math.pi/2)) * (math.pi/2)
   col_dx = round(math.cos(face_ang))  # ±1 or 0
   col_dy = round(math.sin(face_ang))  # ±1 or 0
   ```
   (Same logic as Phase 4 col-exit, already implemented there.)

2. **Col-exit length search:** Try col lengths from `col_min` to `col_max` (same as Phase 4).
   For each col endpoint (col_ex, col_ey):
   a. Verify the col segment (pad → col endpoint) clears all pad_obs (same check as Phase 4).
   b. Verify the col segment also clears placed_vias (already implemented in Phase 4 logic).
   c. From (col_ex, col_ey), search for via at n_min_from_col using `_keepout_escape_length`.
   d. If via found: store `v._col_exit = (col_dx, col_dy, col_len, col_ex, col_ey)`;
      set via_x/via_y to col endpoint + escape*n; mark stub_ok=True; break.

3. **If col-exit also fails:** The pad cannot be placed as a side-exit via. Options:
   a. Mark `v.implicit_keepout=True` and let the keepout escape section handle it
   b. If this is an HS pad, fire the [pair-error] suppression for the partner

**Where to insert this code:**

The stub clearance check (Part A) and col-exit fallback (Part B) belong in `step_1g_stagger`,
inside the innermost-first loop that calls `_stagger_neckdown_2d`. The `pad_obs` list is
available in `_run()` at that point. The `placed_vias` list is NOT yet built at step_1g time
(it's built in section 4a), so the col-exit check cannot use `placed_vias` — use only `pad_obs`
for the stub check.

**IMPORTANT constraint:** `_stagger_neckdown_2d` processes pads INNERMOST-FIRST within a
component group. After this check, the via position must be finalized (via_x/via_y set) so
that subsequent pads can check against it as an obstacle via `already_assigned`.

**Implementation location:**

Find the stagger loop in `_run()`. After `_stagger_neckdown_2d` returns `n_min`, and after
setting `v.neckdown_mm = n_min` and `v.via_x, v.via_y = pad_x + edx*n_min, ...`, add the
stub clearance loop (Part A) followed by the col-exit fallback (Part B) if Part A fails.

**What about the already-placed via position?** When the stub clearance check promotes n_min
or triggers col-exit, the via_x/via_y changes. The `already_assigned` list used by subsequent
pads in the stagger loop must see the UPDATED via position. This requires the stagger loop to
set via_x/via_y AFTER the stub check, not before.

### Stub clearance check in _keepout_escape_length

In addition to the stagger-time check, `_keepout_escape_length` (called for keepout pads in
section 4a) already has a partial check. However, the current implementation does NOT check
stub-to-pad clearance — it only checks stub corridor clearance (trace half-width + clearance
from pad bbox). Adding an explicit `_seg_bbox_dist` call (with `trace_hw + clearance`) would
be correct and consistent.

### Verification target after Phase 3.5

After implementing Phase 3.5:
- 0 short_circuit violations (already achieved)
- 0 non-zone clearance violations from script-placed copper (target)
- 0 solder_mask_bridge from script (pre-existing 2 may remain from tight QFN spacing)
- 126 vias placed (target: same or close)
- Keepout escapes: U3/3, U3/4, U3/13 retain their col-exit vias
- LT_TX2_N/P, LT_AUX_N/P, etc. that currently have clearance violations may gain col-exit
  paths or fall back to keepout + pair suppression

---

## Phase 7 — Co-Optimized Multi-Pass Outside-In Face Fanout [NOT YET DONE]
# Originally written 2026-09-22 as "Column Escape"; superseded same session.
# The greedy outside-in column escape was wrong — replaced with the correct
# multi-pass co-optimized approach described below.

---

### Why the Previous Phase 7 (Greedy Column Escape) Was Wrong

The greedy column escape approach (implemented 2026-09-22, session 26) had two fundamental
failures that required a complete redesign:

1. **Pitch-only trigger misfired on all four faces.** `_needs_column_escape` used
   `min_pitch < via_r + trace_hw + clearance` (0.4 < 0.514mm). This is True for ALL faces
   of U3 since the raw pad pitch is 0.4mm everywhere. But east and west faces of U3 DO NOT
   need column escape — stubs on those faces exit along the pad's short axis, giving a
   stub-to-adjacent-pad-copper gap of 0.300mm (sufficient for a 0.064+0.150=0.214mm
   trace+clearance requirement). The pitch check is not the right condition.

2. **Greedy outside-in placement blocked inner pads.** `_col_escape_position` placed outer
   pads first at their minimum lateral offsets. By the time inner pads were processed, outer
   vias occupied the only valid positions. With no backtracking, inner pads got keepout — the
   same failure mode as the original herringbone, just in a different place.

Both failures require the same architectural fix: replace pitch-only triggering with a
geometric gap check, and replace greedy single-pass placement with a multi-pass approach
where inner-pad failures drive outer-via adjustments.

---

### Problem Statement (Corrected)

For dense faces (stub-to-adjacent-pad-copper gap insufficient for even one trace), the
current herringbone + `_stagger_neckdown_2d` approach fails in two ways:

1. **All stubs pile into the same escape column.** After direction snap to face-perpendicular
   cardinal, all pads on the face share the same escape direction. Stagger-2d pushes each via
   further from the component to avoid the previous one. For 8 pads at 0.4mm pitch all escaping
   due-north, outer vias end up 5–7mm from their pads.

2. **Stub segments cannot pass adjacent-pad copper.** The gap between adjacent pad copper edges
   (along the face direction) is `pitch - pad_width_along_face`. For U3 north pads (0.650mm long
   pad, 0.400mm pitch): `gap = 0.400 - 0.650/2 = 0.075mm`. A trace + clearance requires
   `0.064 + 0.150 = 0.214mm`. The stub physically cannot pass an adjacent pad copper regardless
   of how deep the via is placed.

The reference board (U3 north face) uses a **radial chevron pattern**: each pad's stub exits
straight north but its via is offset laterally, with outer pads having large lateral offsets
and inner pads having small offsets. This produces a V-shape / chevron spread of vias:

```
Via positions from reference (U3 north face, measured session 26):
HDMI0_TX2_P (outer): lat_off=-1.300mm  depth=1.195mm  angle=47.4° from face-perpendicular
HDMI0_TX2_N        : lat_off=-1.105mm  depth=1.683mm  angle=33.3°
HDMI0_TX1_P        : lat_off=-0.884mm  depth=2.361mm  angle=20.5°
HDMI0_TX1_N        : lat_off=-0.556mm  depth=2.660mm  angle=11.8°
HDMI0_TX0_P        : lat_off=-0.100mm  depth=3.045mm  angle= 1.9°
HDMI0_TX0_N        : lat_off=+0.150mm  depth=2.445mm  angle= 3.5° (right)
HDMI0_CLK_P        : lat_off=+0.240mm  depth=1.713mm  angle= 8.0°
HDMI0_CLK_N (outer): lat_off=+0.314mm  depth=1.131mm  angle=15.5°
```

The reference was produced by **multi-pass outside-in refinement**: outer vias are placed at
minimum viable positions; if inner pads cannot find clearance, outer vias are pushed further
outward, freeing room for inner pads. This iterates until stable. Each pass also routes the
trace immediately, because trace segments constrain neighbors just as much as via circles.

---

### Correct Triggering Condition

A face needs co-optimized multi-pass fanout when the stubs cannot physically pass adjacent
pad copper:

```python
# For each face, compute the minimum gap between adjacent pad copper edges along the face
# For N/S faces: along-face direction is X; pad_half_width = pad.size_x / 2
# For E/W faces: along-face direction is Y; pad_half_width = pad.size_y / 2

min_gap = min(
    pitch_i - max(half_w_i, half_w_j)   # gap between copper edges of adjacent pads
    for each adjacent pad pair (i, j) on the face
)
min_trace_total = min_trace_hw + sg_clr   # smallest legal trace half-width + clearance

needs_coopt = min_gap < min_trace_total
```

**Why this condition instead of pitch alone:**
- North/south faces of U3: `gap = 0.400 - 0.325 = 0.075mm < 0.214mm` → True
- East/west faces of U3: pads are 0.200mm wide (short axis), `gap = 0.400 - 0.100 = 0.300mm > 0.214mm` → False
- The condition is purely geometric — works on any PCB regardless of component names or orientation.

**Evaluate after clustering, on the pending list (not raw pads):** power/GND clusters reduce
via count and widen effective pitch. Only pads that will receive individual vias are counted.

---

### New Functions Required

#### `_seg_to_seg_dist(x1,y1,x2,y2, x3,y3,x4,y4)` → float
Minimum distance between two finite line segments. Required for trace-to-trace clearance
checking in the multi-pass loop.

```python
def _seg_to_seg_dist(x1,y1,x2,y2, x3,y3,x4,y4):
    # Compute min of the four endpoint-to-segment distances; also check seg-seg intersection.
    # If segments intersect: return 0.
    # Otherwise: min(_pt_to_seg(x1,y1, x3,y3,x4,y4),
    #                _pt_to_seg(x2,y2, x3,y3,x4,y4),
    #                _pt_to_seg(x3,y3, x1,y1,x2,y2),
    #                _pt_to_seg(x4,y4, x1,y1,x2,y2))
```

#### `_face_needs_coopt(face_pads, sg_clr)` → bool
Returns True if the face needs co-optimized multi-pass fanout.

```python
def _face_needs_coopt(face_pads, sg_clr):
    # face_pads: list of PendingVia objects for this face, after clustering
    # For N/S faces: pad half-width along face = pad_w_mm / 2
    # For E/W faces: pad half-width along face = pad_h_mm / 2
    # min_gap = min(pitch_i - max(half_w_i, half_w_j)) over adjacent pad pairs
    # min_trace_total = min neckdown_w_mm/2 among face pads + sg_clr
    # return min_gap < min_trace_total
```

For U3 north face: `gap = 0.400 - 0.325 = 0.075mm < 0.064+0.150=0.214mm` → True
For U3 east face:  `gap = 0.400 - 0.100 = 0.300mm > 0.214mm` → False

#### `_face_fanout(face_pads, obs_initial, sg_clr, ca)` → `Dict[pad_key, (vx, vy, segs)]`
Main multi-pass outside-in fanout optimizer for a single face.

**Inputs:**
- `face_pads`: list of PendingVia objects sorted by pad position along face (ascending)
- `obs_initial`: list of `(cx, cy, cr)` via-circle obstacles from OTHER already-committed faces
- `sg_clr`: signal clearance (0.150mm)
- `ca`: component assembly — used to access pad obstacle rectangles

**Returns:** dict mapping pad `(ref, pad_num)` → `(vx, vy, trace_segments)`, or raises if unsolvable.

**Algorithm (multi-pass outside-in with backtracking):**

```
STEP_MM = 0.050   # lateral push increment per iteration
MAX_ITER = 200

# 1. Sort pads outside-in (descending |pad_coord - face_center|)
face_center = mean(v.pad_x for v in face_pads)   # or pad_y for E/W
order = sorted(face_pads, key=lambda v: -abs(v.pad_x - face_center))

# 2. Initialize: each pad starts with lat_off = 0 (no lateral deviation)
lat_off = {v: 0.0 for v in face_pads}

# 3. Multi-pass loop
for iteration in range(MAX_ITER):
    obs = list(obs_initial)          # start with external obstacles
    trace_obs = []                   # trace-segment obstacles
    assigned = {}                    # pad → (vx, vy, segs)
    failed_pad = None

    for v in order:                  # outside-in order
        lo = lat_off[v]
        # Fan direction: left half gets negative lo, right half gets positive lo
        # (outer pad → lo already has correct sign from initialization or push)

        # Search: depth from min_depth outward; lat_off is fixed for this pass
        min_depth = v.pad_long_half + via_r + sg_clr
        placed = False
        for depth in frange(min_depth, max_depth=5.0, step=STEP_MM):
            vx = v.pad_x + lo           # for N/S face
            vy = v.pad_y - depth        # KiCad north = decreasing Y

            # Via-to-via clearance
            if any(hypot(vx-cx, vy-cy) < cr + via_r + sg_clr for cx,cy,cr in obs):
                continue

            # Stub segments (straight north + 45° diagonal)
            segs = _route_45deg_stub_segments(v.pad_x, v.pad_y, vx, vy)
            # segs = list of (x1,y1,x2,y2,hw) for each stub segment

            # Stub-to-via clearance
            seg_ok = True
            for x1,y1,x2,y2,hw in segs:
                for cx,cy,cr in obs:
                    if _pt_to_seg_dist(cx,cy, x1,y1,x2,y2) < cr + hw + sg_clr:
                        seg_ok = False; break
                if not seg_ok: break
            if not seg_ok:
                continue

            # Stub-to-stub clearance (trace-to-trace)
            seg_ok = True
            for x1,y1,x2,y2,hw in segs:
                for ox1,oy1,ox2,oy2,ohw in trace_obs:
                    if _seg_to_seg_dist(x1,y1,x2,y2, ox1,oy1,ox2,oy2) < hw + ohw + sg_clr:
                        seg_ok = False; break
                if not seg_ok: break
            if not seg_ok:
                continue

            # Stub-to-pad-copper clearance
            seg_ok = True
            for x1,y1,x2,y2,hw in segs:
                for pad_obs in all_pad_obstacles:   # from obs_initial + face pads
                    if _seg_bbox_dist((x1,y1,x2,y2), pad_obs.bbox) < hw + sg_clr:
                        seg_ok = False; break
                if not seg_ok: break
            if not seg_ok:
                continue

            # Valid — commit
            obs.append((vx, vy, via_r))
            trace_obs.extend(segs)
            assigned[v] = (vx, vy, segs)
            placed = True
            break   # depth loop

        if not placed:
            failed_pad = v
            break   # inner loop

    if failed_pad is None:
        return assigned   # ← success

    # Push the outermost via that is blocking failed_pad
    # Find which already-assigned via is closest to failed_pad's stub path
    blocking_via = _find_blocking_via(failed_pad, assigned, obs_initial, sg_clr)
    if blocking_via is None:
        break   # cannot improve — fall through to keepout
    # Increase lateral offset of blocking via (away from face center) by STEP_MM
    lat_off[blocking_via] += STEP_MM * _fan_sign(blocking_via, face_center)

# Unsolvable: mark affected pads as implicit_keepout
```

**Key invariants:**
- `lat_off[v]` always pushes the via AWAY from face center (outward fanning)
- Depth search always starts at `min_depth` — it is never decreased
- Trace segments are added to `trace_obs` immediately after via is committed
- The obstacle set grows monotonically within each pass

---

### Stub Path Geometry

`_route_45deg_stub` with `axial_first=True` (face-normal axis first) produces for a
north-face pad at (px, py) and via at (vx, vy) where vx ≠ px:

```
seg1: (px, py)              → (px, vy + |vx - px|)   [straight north]
seg2: (px, vy + |vx - px|)  → (vx, vy)               [45° diagonal NW or NE]
```

Both segments are at legal 0° and 45° angles — no arbitrary angles produced. ✓

Edge case: if `|vx - px| > |py - vy|` (via is more lateral than deep), seg1 would be
negative-length. In this case, the via is placed close to the pad and the entire path is
a single 45° segment. Use `axial_first=False` only when the lateral offset exceeds the
depth. `_face_fanout` should detect this and call `_route_45deg_stub` with the correct
`axial_first` argument.

---

### Non-HS Sandwiched Pads (No Via Placed)

Pads between HS pairs (e.g., LT_1V2, LT_3V3 on U3 north face) that are clustered into
power vias placed elsewhere still need their corridors considered during `_face_fanout`:

1. If the adjacent pad copper leaves insufficient gap for any trace: the pad's corridor
   contributes as a pad obstacle in the obstacle set. No stub is routed, no via placed.
2. If there is sufficient gap (corridor ≥ 2×(trace_hw + sg_clr) between neighboring HS
   pads): route a minimal-depth stub (depth = pad_long_half + sg_clr), add its trace
   segments to `trace_obs`, but do NOT add a via circle. This reserves the corridor for
   future routing without placing a competing via.

The decision is made per-pad at the start of `_face_fanout` by checking the actual corridor
width between the non-HS pad and its immediate neighbors in the sorted pad list.

---

### Old Phase 7 Code to REMOVE from Script

The following were added in the previous Phase 7 implementation (session 26) and must be
**deleted** before implementing the new `_face_fanout` approach:

| Item | Where in script |
|------|----------------|
| `PendingVia.col_escape_pending: bool = False` | PendingVia dataclass |
| `PendingVia.col_escape: bool = False` | PendingVia dataclass |
| `_needs_column_escape(face_vias, sg_clr)` | standalone function |
| `_col_escape_position(v, placed, sg_clr)` | standalone function |
| Step 0h block (col_escape_pending marking) | `_run()` before step 1g |
| Step 1g skip: `if _pv_sg.col_escape_pending: continue` | step 1g loop |
| Step 1g-2d skip: `if _pv_r2.col_escape_pending: continue` | step 1g-2d loop |
| Step 1h block (col-escape placement) | `_run()` after step 1g-2d |

---

### New Code to ADD

| Item | Type |
|------|------|
| `_seg_to_seg_dist(x1,y1,x2,y2, x3,y3,x4,y4)` | new function |
| `_face_needs_coopt(face_pads, sg_clr)` | new function |
| `_face_fanout(face_pads, obs_initial, sg_clr, ca)` | new function |
| `PendingVia.face_fanout_assigned: bool = False` | new PendingVia field |
| New step in `_run()` after step 1d (clustering) and before step 1g (herringbone) | `_run()` |

### Integration Into `_run()` — Correct Execution Order

```
Step 1d  — clustering (existing)
NEW STEP — for each radial fp face: if _face_needs_coopt: call _face_fanout,
           set v.face_fanout_assigned=True and v.via_x, v.via_y from result,
           also commit trace segments to board immediately (call _route_45deg_stub)
Step 1g  — herringbone stagger: SKIP any pad where v.face_fanout_assigned=True
Step 1g-2d — 2D stagger:        SKIP any pad where v.face_fanout_assigned=True
run_passes() — placement search: SKIP any pad where v.face_fanout_assigned=True
               and not v.implicit_keepout (positions already final from _face_fanout)
```

**Why this order:** `_face_fanout` commits via positions AND routes trace segments before
the herringbone stagger runs. Steps 1g/1g-2d then see these already-committed positions as
obstacles — exactly as they see positions from other already-placed vias. No position is
ever computed and then overridden.

---

### PROHIBITED Compliance

| # | Prohibited Pattern | This Phase |
|---|-------------------|-----------|
| 3 | Arbitrary-angle traces | Stubs from `_route_45deg_stub` → 0/45 only. `lat_off` search produces legal positions — via positions are placed where traces are 0/45/90. ✓ |
| 5 | Bounding-circle geometry | All checks use `_seg_bbox_dist`, `_pt_to_seg_dist`, `_seg_to_seg_dist`. No bounding circles. ✓ |
| 6 | GetBoundingBox() AABB | `_face_fanout` uses `obs.bbox` from `_pad_obstacle` (rotated corners, already fixed). ✓ |
| 7 | Incoherent HS pair handoff | When `_face_fanout` marks `implicit_keepout`, existing pair-error logic in `run_passes()` fires and suppresses partner. ✓ |
| global | No project-specific fixes | Trigger condition is pure geometry. Face detection uses pad-to-footprint-center position math. Works on any PCB. ✓ |

---

### Interaction with Phase 3.5

Phase 3.5 fixes stub-to-adjacent-pad clearance for the stagger-2d path (non-co-opt faces).
Phase 7 replaces stagger-2d entirely for co-opt faces. These do NOT conflict:
- Faces where `_face_needs_coopt=False`: Phase 3.5 applies (existing herringbone + 2D stagger + stub check)
- Faces where `_face_needs_coopt=True`: `_face_fanout` applies; step 1g/1g-2d skipped for those pads

**Recommended implementation order:**
1. Phase 3.5 — stagger-2d stub-to-pad clearance (non-dense faces, U3 east/west/south, other ICs)
2. Phase 7 (this plan) — co-optimized multi-pass for dense faces (U3 north/south)
3. Phase 1 — obs.bbox AABB fix (currently deferred; already done per session 25 notes)

---

### Success Criteria After Phase 7

- U3 north face: all 8 vias placed, all within 3.5mm of their pad ✓
- All stubs form "N-jog-N" shape: straight face-perpendicular first, then 45° diagonal ✓
- Outer pads have larger lateral offsets than inner pads (V-shape / chevron) ✓
- Via-to-via clearance ≥ 0.750mm for all pairs ✓
- Trace-to-via clearance ≥ 0.150mm for all combinations ✓
- Trace-to-trace clearance ≥ 0.150mm for all combinations ✓
- 0 non-zone DRC violations ✓
- Implicit keepout count equal to or less than the session-25 baseline (12) ✓

---

## Phase 7 Debug Prototype — Validated 2026-09-22 (session 27)

A standalone prototype script `debug_u3_north.py` was written to validate the `_face_fanout`
algorithm before implementation in `route_fanout_vias.py`. It places vias and stubs on U3's
north face, writes results to the board, and prints detailed per-iteration logs.

**Script location:** `E:\Claude Projects\CC Project Folder\Python Scripts\debug_u3_north.py`

The prototype implements the full algorithm including pad classification, bus routing,
sandwiched detection, HS pair coherence, and stub-aware blocker detection. It was validated
on pads `{'14', '15', '1', '13', '2', '12', '3'}` and produced correct results in 10 iterations.

### Validated Results (U3 North Face, Session 27)

```
Pad classification:
  Bus pads (2): [14, 15]  — net=LT_1V2, lat_sign=-1 (both left of U3 center)
  Sandwiched pads (0): []
  Signal pads (5): [1, 2, 3, 13, 12]  — get individual vias

HS partners: {pad13: pad12, pad12: pad13}  (HDMI0_TX2_P/_N)

Final placements:
  pad 1  (HDMI0_CEC)    via=(155.250, 112.295)  lat_off=0.150  depth=0.675mm
  pad 2  (LT_1V2)       via=(154.850, 111.645)  lat_off=0.150  (single on right side)
  pad 15 (LT_1V2)       BUS — lateral stub, no via
  pad 3  (HDMI0_CLK_N)  via=(154.300, 111.120)  lat_off=0.000  depth=1.675mm
  pad 14 (LT_1V2)       BUS — lateral stub, no via
  pad 13 (HDMI0_TX2_P)  via=(150.150, 111.795)  lat_off=0.150  depth=1.000mm
  pad 12 (HDMI0_TX2_N)  via=(150.700, 111.270)  lat_off=0.000  depth=1.525mm
```

Comparison to script baseline: HDMI0_TX2_P improved from 1.879mm to 1.000mm depth;
HDMI0_TX2_N improved from 5.4mm to 1.525mm depth (reference board: 1.195mm, 1.683mm).

---

### Phase 7 Implementation Guide — Exact Algorithm from Prototype

This section specifies the exact algorithm to translate from `debug_u3_north.py` into
`_face_fanout` in `route_fanout_vias.py`. The prototype is the reference implementation.

#### Constants

```python
STEP_MM            = 0.050   # lat_off increment per blocker push
MAX_ITER           = 200
SANDWICH_THRESHOLD = 0.550   # mm — hs_via_copper_r + stub_hw + clearance
```

#### Pad Classification — Full Logic

Run ONCE per face before the main loop. All steps use global face pad indices.

```python
# Step 1: Bus groups — (net, lat_sign) pairs with 2+ pads
from collections import defaultdict
net_side_groups = defaultdict(list)
for i, v in enumerate(face_pads):
    net_side_groups[(v.net, v.lat_sign)].append(i)
bus_pad_set = set()
bus_groups = {}
for key, indices in net_side_groups.items():
    if len(indices) >= 2:
        bus_pad_set.update(indices)
        bus_groups[key] = sorted(indices)

# Step 2: Sandwiched — non-HS pad with HS within SANDWICH_THRESHOLD on BOTH sides.
# Use ALL HS x-positions from the full face (not just the debug subset).
all_face_hs_x = [fp.pad_x for fp in ALL_FACE_PADS if fp.net in HS_NET_SET]
sandwiched_set = set()
for i, v in enumerate(face_pads):
    if v.is_hs or i in bus_pad_set:
        continue
    px = v.pad_x
    left_hs  = any(hx < px and (px - hx) < SANDWICH_THRESHOLD for hx in all_face_hs_x)
    right_hs = any(hx > px and (hx - px) < SANDWICH_THRESHOLD for hx in all_face_hs_x)
    if left_hs and right_hs:
        sandwiched_set.add(i)

# Step 3: HS partners — match by _P/_N suffix
hs_partner = {}   # global_i -> partner global_i
for i, vi in enumerate(face_pads):
    if not vi.is_hs:
        continue
    partner_net = vi.net[:-2] + ('_N' if vi.net.endswith('_P') else '_P')
    for j, vj in enumerate(face_pads):
        if j != i and vj.net == partner_net:
            hs_partner[i] = j; break

# Step 4: Signal indices — pads that get their own via
signal_indices = [i for i in range(n) if i not in bus_pad_set and i not in sandwiched_set]
global_to_si   = {gi: si for si, gi in enumerate(signal_indices)}

# Step 5: lat_sign (for N/S face, N face edy=-1)
# lat_sign = -1 if pad_x < face_center_x else +1
```

#### Bus Obstacle Pre-Computation

```python
bus_obstacles     = []   # {x1, y1, x2, y2, hw, net}
bus_stubs_to_write = []  # (x1, y1, x2, y2, nw, net) — for writing to board

for (bnet, lat_sign), indices in bus_groups.items():
    e0        = face_pads[indices[0]]
    bus_depth = e0.nl          # minimum neckdown length
    nw        = e0.nw          # neckdown trace width
    bus_hw    = nw / 2.0
    pad_y     = e0.py          # all QFN face pads at same y
    bus_y     = pad_y + e0.edy * bus_depth   # edy=-1 for north face

    xs = [face_pads[i].px for i in indices]
    bus_x_min, bus_x_max = min(xs), max(xs)

    # Horizontal bus segment
    bus_obstacles.append({'x1': bus_x_min, 'y1': bus_y, 'x2': bus_x_max, 'y2': bus_y,
                          'hw': bus_hw, 'net': bnet})

    # Vertical drop from each pad to bus
    for i in indices:
        px = face_pads[i].px
        bus_obstacles.append({'x1': px, 'y1': pad_y, 'x2': px, 'y2': bus_y,
                              'hw': bus_hw, 'net': bnet})
```

#### `_check(e, vx, vy, placed)` — Complete Clearance Function

Returns `(desc, d, thr, kind, blk_j)` or `None`.
`blk_j` is the index into `placed[]` for placed-via/stub blockers; `None` for pad/bus blockers.

```python
def _check(e, vx, vy, placed):
    via_r = e.drill/2.0 + e.annular
    stub_hw = e.nw / 2.0
    chk_r = via_r + CLEARANCE

    # 1. via vs pad obstacles (rectangular)
    for obs in all_pad_obs:
        if obs.ref == e.ref and obs.net_name == e.net: continue
        if e.net and obs.net_name == e.net: continue
        d = _dist_point_to_bbox(vx, vy, obs.bbox) if obs.bbox else hypot(vx-obs.cx, vy-obs.cy)
        thr = chk_r if obs.bbox else via_r + obs.r
        if d < thr: return (f"{obs.ref}/{obs.net_name}", d, thr, "via-vs-pad", None)

    # 2. via vs placed vias
    for j, pc in enumerate(placed):
        d = hypot(vx - pc['vx'], vy - pc['vy'])
        thr = via_r + pc['r'] + CLEARANCE
        if d < thr: return (f"via[{pc['pad_num']}]", d, thr, "via-vs-via", j)

    # 3. via vs placed stub segments (different net only)
    for j, pc in enumerate(placed):
        if pc['net'] == e.net: continue
        for x1s,y1s,x2s,y2s in pc['segs']:
            d = _dist_to_segment(vx, vy, x1s, y1s, x2s, y2s)
            thr = via_r + pc['stub_hw'] + CLEARANCE
            if d < thr: return (f"stub[{pc['pad_num']}]", d, thr, "via-vs-stub", j)

    # 4. via vs bus obstacles (different net only)
    for bobs in bus_obstacles:
        if bobs['net'] == e.net: continue
        d = _dist_to_segment(vx, vy, bobs['x1'], bobs['y1'], bobs['x2'], bobs['y2'])
        thr = via_r + bobs['hw'] + CLEARANCE
        if d < thr: return (f"bus/{bobs['net']}", d, thr, "via-vs-bus", None)

    # 5. stub checks — actual routed 45° segments
    segs = _route_45deg_stub(e.px, e.py, vx, vy, e.edx, e.edy, axial_first=True)
    if not segs: segs = [(e.px, e.py, vx, vy)]
    for x1,y1,x2,y2 in segs:
        if hypot(x2-x1, y2-y1) < 1e-6: continue

        # 5a. stub vs pad obstacles
        for obs in all_pad_obs:
            if obs.ref == e.ref and abs(obs.cx-e.px)<0.05 and abs(obs.cy-e.py)<0.05: continue
            if e.net and obs.net_name == e.net: continue
            d = _seg_bbox_dist(x1,y1,x2,y2, obs.bbox) if obs.bbox \
                else _dist_to_segment(obs.cx, obs.cy, x1,y1,x2,y2)
            thr = stub_hw + CLEARANCE if obs.bbox else stub_hw + obs.r
            if d < thr: return (f"{obs.ref}/{obs.net_name}", d, thr, "stub-vs-pad", None)

        # 5b. stub vs placed vias
        for j, pc in enumerate(placed):
            d = _dist_to_segment(pc['vx'], pc['vy'], x1,y1,x2,y2)
            thr = stub_hw + pc['r'] + CLEARANCE
            if d < thr: return (f"via[{pc['pad_num']}]", d, thr, "stub-vs-via", j)

        # 5c. stub vs placed stubs (different net only)
        for j, pc in enumerate(placed):
            if pc['net'] == e.net: continue
            for x1s,y1s,x2s,y2s in pc['segs']:
                d = _seg_to_seg_dist(x1,y1,x2,y2, x1s,y1s,x2s,y2s)
                thr = stub_hw + pc['stub_hw'] + CLEARANCE
                if d < thr: return (f"stub[{pc['pad_num']}]", d, thr, "stub-vs-stub", j)

        # 5d. stub vs bus obstacles (different net only)
        for bobs in bus_obstacles:
            if bobs['net'] == e.net: continue
            d = _seg_to_seg_dist(x1,y1,x2,y2, bobs['x1'],bobs['y1'],bobs['x2'],bobs['y2'])
            thr = stub_hw + bobs['hw'] + CLEARANCE
            if d < thr: return (f"bus/{bobs['net']}", d, thr, "stub-vs-bus", None)

    return None  # clear
```

#### Main Multi-Pass Loop

```python
sig_n        = len(signal_indices)
lat_offs_arr = [0.0] * sig_n       # indexed by si (position in signal_indices)
keepout_set  = set()               # global indices
final_placed = {}                  # global_i -> (vx, vy)

for iteration in range(MAX_ITER):
    placed        = []   # {vx, vy, r, stub_hw, segs, pad_i, net, pad_num}
    pass_result   = {}   # global_i -> (vx, vy)
    first_fail_si = None
    last_blk      = None

    for si, global_i in enumerate(signal_indices):
        if global_i in keepout_set: continue
        e = face_pads[global_i]
        lat_off = lat_offs_arr[si]

        placed_i = False
        depth = e.nl
        while depth <= e.mx + 1e-9:
            vx = e.px + e.lat_sign * lat_off
            vy = e.py + e.edy * depth
            blk = _check(e, vx, vy, placed)
            if blk is None:
                stub_segs = _route_45deg_stub(e.px,e.py,vx,vy,e.edx,e.edy,axial_first=True)
                if not stub_segs: stub_segs = [(e.px,e.py,vx,vy)]
                placed.append({'vx':vx,'vy':vy,'r':via_r,'stub_hw':e.nw/2.0,
                               'segs':stub_segs,'pad_i':global_i,'net':e.net})
                pass_result[global_i] = (vx, vy)
                placed_i = True; break
            last_blk = blk
            depth += DEPTH_STEP

        if not placed_i:
            first_fail_si = si; break

    if first_fail_si is None:
        final_placed = pass_result; break

    # Blocker analysis — use last_blk returned by _check
    name, d, thr, kind, blk_j = last_blk or ('?', 0, 0, '?', None)
    fail_global_i = signal_indices[first_fail_si]

    if kind in ('via-vs-bus', 'stub-vs-bus'):
        # Bus stubs are fixed — pad is genuinely blocked
        keepout_set.add(fail_global_i)
        if fail_global_i in hs_partner:
            keepout_set.add(hs_partner[fail_global_i])

    elif blk_j is not None:
        # placed[blk_j]'s via or stub is blocking → push that via outward
        blocking_gi = placed[blk_j]['pad_i']
        if blocking_gi in global_to_si:
            lat_offs_arr[global_to_si[blocking_gi]] += STEP_MM

    else:
        # Pad obstacle — bump nearest active outer pad, or keepout
        outer_si = next((s for s in range(first_fail_si-1,-1,-1)
                         if signal_indices[s] not in keepout_set), None)
        if outer_si is not None:
            lat_offs_arr[outer_si] += STEP_MM
        else:
            keepout_set.add(fail_global_i)
            if fail_global_i in hs_partner:
                keepout_set.add(hs_partner[fail_global_i])
```

#### Post-Processing

```python
# Ensure all HS partners of keepout pads are suppressed
for global_i in list(keepout_set):
    if global_i in hs_partner:
        partner_i = hs_partner[global_i]
        keepout_set.add(partner_i)
        final_placed.pop(partner_i, None)
```

#### Writing Results to Board

```python
# 1. Write bus stubs (horizontal + vertical drops per bus group)
for x1,y1,x2,y2,nw,bnet in bus_stubs_to_write:
    if hypot(x2-x1,y2-y1) < 1e-6: continue
    # PCB_TRACK from (x1,y1) to (x2,y2) on F.Cu, width=nw, net=bnet

# 2. Write signal vias + 45° stubs
for global_i, (vx, vy) in final_placed.items():
    if global_i in keepout_set: continue
    e = face_pads[global_i]
    # PCB_VIA at (vx,vy), drill=e.drill, OD=e.drill+2*e.annular,
    #   layers F.Cu→target_layer, net=e.net
    segs = _route_45deg_stub(e.px,e.py,vx,vy,e.edx,e.edy,axial_first=True)
    for x1,y1,x2,y2 in segs:
        # PCB_TRACK from (x1,y1)→(x2,y2), width=e.nw, layer=F.Cu, net=e.net
```

#### Integration Points in `_run()`

The `_face_fanout` function is called AFTER step 1d (clustering) and BEFORE step 1g
(herringbone stagger). This ensures:
- Clustered power vias are already committed as obstacles before `_face_fanout` runs
- `_face_fanout`-assigned pads are skipped in steps 1g, 1g-2d, and `run_passes()`

```python
# In _run(), after step 1d, before step 1g:
for fp_ref, face_key, face_pads_list in face_groups:
    if _face_needs_coopt(face_pads_list, sg_clr):
        results = _face_fanout(face_pads_list, current_obs, sg_clr, ca)
        for v in face_pads_list:
            v.face_fanout_assigned = True
            if (v.ref, v.pad_num) in results:
                v.via_x, v.via_y = results[(v.ref, v.pad_num)][:2]
            else:
                v.implicit_keepout = True

# In step 1g and step 1g-2d:
if v.face_fanout_assigned: continue

# In run_passes():
if v.face_fanout_assigned and not v.implicit_keepout:
    pass_result.append(v)  # already placed, just commit
    continue
```

#### Old Phase 7 Code to Remove (Not Yet Done)

Before implementing `_face_fanout`, delete these from `route_fanout_vias.py`:
- `PendingVia.col_escape_pending`, `PendingVia.col_escape` fields
- `_needs_column_escape()`, `_col_escape_position()` functions
- Step 0h block (col_escape_pending marking)
- Step 1g skip block: `if _pv_sg.col_escape_pending: continue`
- Step 1g-2d skip block: `if _pv_r2.col_escape_pending: continue`
- Step 1h block (col-escape placement loop)

#### Reading the Debug Prototype

Before implementing, read the prototype in full:
```
E:\Claude Projects\CC Project Folder\Python Scripts\debug_u3_north.py
```
The prototype is the authoritative reference. When in doubt, the prototype's behavior
is correct.

---

## Phase 7 Addendum — Stub-Only Pads and Stub Endpoint Geometry
### Added 2026-09-22, Session 28

### Rule Summary

On a co-opt face (`_face_needs_coopt` = True):
1. **Every pad gets a radially-correct stub** — no exceptions.
2. **Via/no-via** is determined entirely by existing script tables (no new tables).
3. **Stub-only endpoint depth**: the stub tip must clear the outer copper edge of the
   nearest placed via on the face, so the dangling end can be routed later without
   clearance violations.

---

### Via/No-Via Determination — Existing Tables Only

The following mechanisms, applied in the existing `_run()` sequence, determine whether
a pad gets a via. No new tables are needed.

| Mechanism | Effect on pending | Via? |
|---|---|---|
| `FANOUT_VIA_SKIP_NETS` | pad never enters pending | No via |
| `_cluster_adjacent_pads` | secondary pads removed from pending | No via |
| `_suppress_proximity_via_sharing` | suppressed pads removed from pending | No via |
| Remaining in pending | stays in pending | **Via** |

**Rule:** a pad on a co-opt face gets a via if and only if it is present in the
`pending` list after clustering and proximity-suppression have run.

**Do NOT add nets to `FANOUT_VIA_SKIP_NETS` to control per-face behavior.** That
table is board-wide; adding a net suppresses ALL pads with that net everywhere.

---

### Stub Endpoint Geometry for Stub-Only Pads

After the multi-pass algorithm places all via-bearing pads (producing a set of placed
via positions), each stub-only pad needs its stub depth determined.

**Geometry requirement:**

```
dist(stub_endpoint, via_center_j) >= via_copper_radius_j + trace_hw + clearance
    for all placed vias j on the same face
```

Where:
- `stub_endpoint = (px + lat_off*perp_x + depth*edx, py + lat_off*perp_y + depth*edy)`
  (same radial parametrization as via-bearing pads; `lat_off=0` for stub-only pads)
- `via_copper_radius_j = via_drill_j/2 + annular_ring`
- `trace_hw` = half the neckdown trace width for this pad
- `clearance` = `sg_clr` (signal-to-signal clearance)
- `(edx, edy)` = face-perpendicular outward unit vector
- `(perp_x, perp_y)` = lateral unit vector (90° from `(edx, edy)`)

**Algorithm to find minimum depth:**

```python
def _stub_only_depth(px, py, edx, edy, lat_off,
                     placed_vias,   # list of (vx, vy, via_copper_r) for has_via pads
                     trace_hw, clearance,
                     min_depth=0.3):
    """Return minimum stub depth that clears all placed via copper."""
    depth = min_depth
    for vx, vy, via_r in placed_vias:
        # stub endpoint at current depth
        ex = px + lat_off * (-edy) + depth * edx
        ey = py + lat_off *   edx  + depth * edy
        d = hypot(ex - vx, ey - vy)
        need = via_r + trace_hw + clearance
        if d < need:
            # Project via center onto radial axis to find depth where clearance met
            # Solve: ||(px + lat_off*perp + t*e) - (vx,vy)||^2 = need^2
            # Let A = (px + lat_off*perp_x - vx), B = (py + lat_off*perp_y - vy)
            A = px + lat_off * (-edy) - vx
            B = py + lat_off *   edx  - vy
            # dist^2 = (A + t*edx)^2 + (B + t*edy)^2
            # = t^2 + 2t*(A*edx+B*edy) + A^2+B^2
            # Solve t^2 + 2*p*t + (q - need^2) = 0
            p = A * edx + B * edy
            q = A*A + B*B
            disc = p*p - (q - need*need)
            if disc >= 0:
                t = -p + sqrt(disc)   # larger root = past the via
                depth = max(depth, t + 0.050)  # 50µm margin past clearance
    return depth
```

`placed_vias` is built from the has_via entries in the placed list after the
multi-pass loop completes, before stub-only pads are processed.

**`lat_off=0` for stub-only pads**: since there is no via to push around, stub-only
pads use a straight radial exit. They still participate as stub obstacles in `_check()`
for via-bearing pads (the multi-pass algorithm should check stub clearance against
them), but they do not enter the lat_off bump loop.

---

### Processing Order Inside `_face_fanout`

```
1. Classify pads: has_via = (ref, pad_num) in pending_set
2. Multi-pass loop: place via-bearing pads only (lat_off bumping, _check(), blocker detect)
3. Stub-only pass: for each stub-only pad, call _stub_only_depth() using all placed vias
4. Build placed_list: via-bearing entries from step 2 + stub-only entries from step 3
5. Board write: emit stubs for ALL pads; emit vias only for has_via entries
```

Stub-only pads are NOT in the multi-pass loop. They do not block via placement
(their stubs are thin traces, not fat via obstacles). They are resolved after all
vias are committed.

---

### Function Signature Change

```python
def _face_fanout(face_pads, obs, clearance, ca, pending_set):
    """
    face_pads  : list of dicts — all pads on this face (padded from full fp pad list)
    obs        : existing obstacles (other components' pads, placed vias, etc.)
    clearance  : sg_clr
    ca         : copper annular (for via size)
    pending_set: set of (ref, pad_num) — pads that should receive a via
                 built from pending list AFTER _cluster_adjacent_pads and
                 _suppress_proximity_via_sharing have run
    """
```

---

### PlacedEntry Dataclass (replaces PlacedVia from earlier plan)

```python
@dataclass
class PlacedEntry:
    i:       int         # index into face_pads
    px: float; py: float # pad center
    vx: float; vy: float # via center (or stub endpoint for stub-only)
    lat_off: float
    has_via: bool        # True → emit via at (vx,vy); False → stub only
    blk_j:   int | None = None
```

---

### `_check()` Obstacle Rules

The 8-type `_check()` from the validated prototype is unchanged except:

- **via-vs-via**: check only if `placed[j].has_via` is True
- **stub-vs-via** / **via-vs-stub**: stub obstacles exist for ALL entries regardless
  of `has_via` — a dangling trace end is still a physical conductor

Stub-only entries are added to the obstacle set before the multi-pass loop starts
(with lat_off=0, depth from a first-pass estimate or zero), then their final depth
is resolved in the stub-only pass after vias are committed. This means the multi-pass
loop sees stub-only pads as thin radial obstacles, which is conservative and correct.

---

### Board Write Sequence

```python
# 1. Emit stubs for ALL face pads (via-bearing and stub-only)
for e in placed_list:
    pad = face_pads[e.i]
    segs = _route_45deg_stub(e.px, e.py, e.vx, e.vy,
                             pad['edx'], pad['edy'], axial_first=True)
    for x1, y1, x2, y2 in segs:
        # emit PCB_TRACK on F.Cu, width=pad['nw'], net=pad['net']

# 2. Emit vias only for has_via entries
for e in placed_list:
    if e.has_via:
        pad = face_pads[e.i]
        # emit PCB_VIA at (e.vx, e.vy), drill=pad['drill'],
        #   OD=pad['drill']+2*ca, layers F.Cu→target_layer, net=pad['net']
```

---

### Integration Order in `_run()`

```
step 1a: build pending list (all face pads, excluding FANOUT_VIA_SKIP_NETS)
step 1b: _cluster_adjacent_pads(pending, clearance)
step 1c: _suppress_proximity_via_sharing(pending, ...)
--- build pending_set from pending here ---
step 1d: [existing]
*** NEW: for each co-opt face group:
         pending_set = {(pv.ref,pv.pad_num) for pv in pending if pv.ref==fp_ref and face matches}
         if _face_needs_coopt(face_pads, sg_clr):
             results = _face_fanout(face_pads, obs, sg_clr, ca, pending_set)
             mark all face pads face_fanout_assigned=True ***
step 1g:    if v.face_fanout_assigned: continue
step 1g-2d: if v.face_fanout_assigned: continue
run_passes(): if v.face_fanout_assigned and not v.implicit_keepout: commit directly, continue
```

`pending_set` is built per-face after step 1c so clustering and suppression decisions
are already final before `_face_fanout` runs.

---

### debug_u3_north.py — Required Update

Replace the custom `(net, lat_sign)` bus grouping with a call to
`_cluster_adjacent_pads` from the script:

```python
import sys
sys.path.insert(0, r'E:\Claude Projects\CC Project Folder\Python Scripts')
import route_fanout_vias as rfv

# Build PendingVia objects for north-face entries
pending = [rfv.PendingVia(ref='U3', pad_num=e['pad_num'],
                          pad_x=e['px'], pad_y=e['py'],
                          net=e['net'], ...)
           for e in north_entries]

rfv._cluster_adjacent_pads(pending, clearance=sg_clr)
pending_set = {(pv.ref, pv.pad_num) for pv in pending}

# has_via per pad
for e in north_entries:
    e['has_via'] = ('U3', e['pad_num']) in pending_set
```

In isolated debug scope `_suppress_proximity_via_sharing` cannot run (needs full-board
via list). Pad 2 (LT_1V2, lone pad on right) will incorrectly get a via in the debug
run. This is a known limitation of the isolated scope and acceptable for prototype
purposes. The full-board run will suppress it correctly.

---

## Phase 7 Addendum 2 — Unified Outside-In Loop (session 28)

**SUPERSEDES** the "Processing Order" and "Stub-Only Pass" sections in the Addendum 1
above. The post-loop stub-only pass described there causes clearance violations and must
be replaced with the unified loop below.

---

### Root Cause of Addendum 1's Failure

The Addendum 1 approach processes via-bearing pads in the multi-pass loop, then resolves
stub-only pads afterward. Pads 6 and 10 (via-bearing) are placed without knowing where
the stubs for pads 5 and 11 (stub-only) will land. Result: pad 6 via at x=153.250 vs
pad 5 stub at x=153.500 — distance 0.250mm << required 0.550mm.

The fix: every non-bus pad, regardless of type, participates in ONE unified outside-in
loop. `has_via` controls only whether a via is emitted at the stub endpoint.

---

### The Unified Outside-In Algorithm

**Loop participants:**
```python
all_loop_pads = [p for p in face_pads if p['idx'] not in bus_pad_set]
# sorted outside-in: descending lateral distance from face center
all_loop_pads.sort(key=lambda p: -abs(p['lat_dist']))
```

**Single shared lat_off array:**
```python
lat_offs = [0.0] * len(all_loop_pads)
global_to_li = {p['idx']: li for li, p in enumerate(all_loop_pads)}
```

**Loop body (same structure for ALL pad types):**
```python
for iteration in range(MAX_ITER):
    placed = []
    first_fail = None
    for li, pad in enumerate(all_loop_pads):
        has_via = pad['idx'] in has_via_set   # from pending_set
        lat_off = lat_offs[li]
        placed_ok = False
        for depth in depth_candidates(pad):
            vx = pad['px'] + lat_off * pad['perp_x'] + depth * pad['edx']
            vy = pad['py'] + lat_off * pad['perp_y'] + depth * pad['edy']
            blk = _check(pad, vx, vy, placed, has_via)
            if blk is None:
                placed.append(PlacedEntry(
                    i=li, px=pad['px'], py=pad['py'],
                    vx=vx, vy=vy, lat_off=lat_off,
                    has_via=has_via, blk_j=None
                ))
                placed_ok = True
                break
            else:
                placed[-1] = placed[-1]._replace(blk_j=blk.blk_j)  # record blocker
        if not placed_ok:
            first_fail = li
            break
    if first_fail is None:
        break  # all pads placed — done
    # bump lat_off for the pad that blocked the first failure
    blocker_li = placed[first_fail - 1].blk_j   # or first_fail itself
    lat_offs[blocker_li] += LAT_STEP
```

**`_check()` modifications for stub-only pads:**
```python
def _check(pad, vx, vy, placed, has_via):
    # Via-vs-* checks: only if has_via=True
    if has_via:
        for obs in global_obstacles:
            if via_conflicts_with(vx, vy, obs): return Blocker(...)
        for j, prev in enumerate(placed):
            if prev.has_via and via_vs_via_conflict(vx, vy, prev): return Blocker(j)
            if via_vs_stub_conflict(vx, vy, prev): return Blocker(j)

    # Stub-vs-* checks: ALWAYS run, regardless of has_via
    stub_segs = _route_45deg_stub(pad['px'], pad['py'], vx, vy, ...)
    for obs in global_obstacles:
        if stub_conflicts_with(stub_segs, obs): return Blocker(...)
    for j, prev in enumerate(placed):
        if prev.has_via and stub_vs_via_conflict(stub_segs, prev): return Blocker(j)
        if stub_vs_stub_conflict(stub_segs, prev): return Blocker(j)

    return None  # clear
```

Key rules:
- Via-vs-* checks skipped entirely for stub-only pads (no via to place, no via obstacle)
- Stub-vs-* checks always run — a dangling trace is still a physical conductor
- When checking against placed entries: use `prev.has_via` to skip via obstacle for stub-only entries

---

### What Gets Removed from debug_u3_north.py

Remove these constructs from the current debug script:
- `signal_indices` — separate list of via-bearing pad indices
- `sandwiched_set` treatment as "obstacle only" (the old "no stub" path)
- Post-loop `stub_only_placed` pass
- `_stub_only_depth()` function — no longer needed; depth comes naturally from the loop
- Any split between "multi-pass loop for vias" and "post-loop pass for stubs"

Replace with: a single unified loop over `all_loop_pads`, using `has_via` flag only
to branch between emitting a via vs. just a stub at the endpoint.

---

### stub-only pad depth in the unified loop

In the unified loop, stub-only pads search for `depth` exactly like via-bearing pads:
starting from `min_depth` and stepping outward until `_check()` returns None. The
`_check()` for stub-only pads only runs stub-vs-* tests (no via-vs-* tests). So the
"stub endpoint clears neighboring via copper" requirement is enforced automatically by
`stub_vs_via_conflict()` in `_check()`, not by a post-loop `_stub_only_depth()` call.

This is why the post-loop `_stub_only_depth()` helper is obsolete and should be deleted.

---

### lat_off bumping for stub-only pads

Stub-only pads participate in lat_off bumping exactly like via-bearing pads:
- They occupy a position in `lat_offs[]`
- If an inner pad fails and the blocker is a stub-only pad, that stub-only pad's
  lat_off gets bumped outward
- If a stub-only pad itself fails (stub conflicts), its own lat_off gets bumped
- This ensures the radial fanout geometry is consistent: outer stubs fan wider to make
  room, inner stubs/vias fill the remaining space

The V-shaped chevron pattern from the reference board emerges naturally from this
outside-in lat_off bumping applied uniformly to all pad types.

---

### Corrected Processing Order Inside `_face_fanout`

```
1. Classify pads:
   - bus_pad_set: from adjacency walk (consecutive same-net pads in lat-sorted order)
   - has_via_set: (ref, pad_num) in pending_set
   - all_loop_pads: face_pads not in bus_pad_set, sorted outside-in

2. Pre-compute bus obstacles (unchanged from Addendum 1)

3. Unified outside-in loop (replaces multi-pass + stub-only pass):
   lat_offs = [0.0] * len(all_loop_pads)
   for iteration in MAX_ITER:
       placed = []
       for li, pad in enumerate(all_loop_pads):
           search depth until _check() passes → append PlacedEntry(has_via=...)
       if all placed: break
       bump lat_off of blocker pad

4. Build placed_list from step 3 results

5. Board write:
   - Emit stubs for ALL placed entries (has_via and stub-only alike)
   - Emit vias only for entries where has_via=True
   - Emit bus stubs (lateral traces at bus pad positions)
```

---

### debug_u3_north.py Rewrite Plan

The next session should rewrite `debug_u3_north.py` to implement the unified loop:

1. Keep: bus adjacency grouping (the adjacency walk, not (net,lat_sign) grouping)
2. Keep: pad data extraction (px, py, edx, edy, net, nw, nl, has_via etc.)
3. Keep: bus obstacle pre-computation
4. REPLACE: the split multi-pass + post-loop stub pass with a single unified loop
5. REMOVE: `_stub_only_depth()`, `signal_indices`, `sandwiched_set`, `stub_only_placed`
6. UPDATE: `_check()` to gate via-vs-* tests on `has_via`, always run stub-vs-* tests
7. TARGET: run on all 15 U3 north-face pads, expect 0 clearance violations

After the debug prototype validates on U3 north face:
- Implement in `route_fanout_vias.py` (must diagnose/revert the session-26 regression first)
- The script currently has 504 clearance + 51 track_width violations from a failed
  `_face_fanout` attempt that never triggered correctly
- May need to revert to session-25 baseline before implementing the new algorithm


(not the pseudocode above) is the spec. The pseudocode above is translated from it.


---

## Phase 8 — Iterative Joint Compaction [NEXT STEP]
*(Updated 2026-09-30, session 27)*

### Background

`_face_fanout` is fully implemented (Phases 1–7). The existing compaction pass (section 7)
runs a single sweep outside-in. This freezes earlier via positions before later pads are
evaluated: when CLK_P (outer) is placed at 2.825mm depth, it does not know that CLK_N
(inner) will later compact to 1.531mm — which would have allowed CLK_P to compact further.

User validated on manual board edit (2026-09-30): after manually moving all U3 north face vias,
HS pairs converged to matching depths:
- CLK_N: 1.531mm → ~1.32mm; CLK_P: 2.825mm → ~1.35mm
- TX2_N: 1.647mm → ~1.35mm; TX2_P: 0.997mm → ~1.35mm

Matching axial depths is the natural side effect of iterative joint minimization — both vias
in a pair face symmetric constraints and converge to similar minimum depths when each pass
can use the updated positions from the previous pass.

### What to change

**File:** `route_fanout_vias.py`
**Location:** Section 7 of `_face_fanout` (the compaction loop)

**Add constant** near top of `_face_fanout` or at module level:
```python
MAX_COMPACT_ROUNDS = 20
```

**Wrap existing inner loop:**
```python
# Section 7 — Iterative joint compaction
for _round in range(MAX_COMPACT_ROUNDS):
    _improved = False
    for si, gi in enumerate(signal_indices):
        if gi in keepout_set or gi not in final_placed:
            continue
        vx, vy = final_placed[gi]
        v = face_pads[gi]
        cur_lat   = vx * ldx + vy * ldy - (v.pad_x * ldx + v.pad_y * ldy)
        cur_axial = (vx - v.pad_x) * edx + (vy - v.pad_y) * edy
        cur_cost  = math.hypot(cur_lat, cur_axial)

        # other_placed: all placed vias except gi (already done this way in current code)
        other_placed = {gj: final_placed[gj] for gj in final_placed if gj != gi}

        best_vx, best_vy = vx, vy
        best_cost = cur_cost
        best_lat_off = cur_lat

        # Strategy A: sweep depth at current lat_off
        # ... (existing strategy A code, unchanged) ...

        # Strategy B: increase lat_off outward, use minimum own-pad depth floor
        # ... (existing strategy B code, unchanged) ...

        if best_cost < cur_cost - 1e-9:
            final_placed[gi] = (best_vx, best_vy)
            lat_offs_arr[gi]  = best_lat_off
            _improved = True

    if not _improved:
        break
```

**Critical rule:** Only update `final_placed[gi]` when strictly better (`< cur_cost - 1e-9`).
This prevents oscillation. The epsilon is safe because STEP_MM = 0.05mm >> 1e-9.

**No other changes needed:** `other_placed` is already rebuilt from `final_placed` inside
each pad's search — so round N+1 automatically sees positions updated by round N.

### Skip-net stub extension (already implemented 2026-09-30)

Pad 2 (LT_1V2) stub now extends east past the outer via (pad 1 CEC). Logic in the
return loop of `_face_fanout` and emitted in section 6c of `_run()`. Extension endpoint
stored as `pv.stub_ext_vx`, `pv.stub_ext_vy`. Extension fires only when outer via depth
< own-pad clearance floor — correctly suppressed for pads 8 and 11.

### Verification

After implementing Phase 8, run debug_u3_north.py and check:
- CLK_N and CLK_P depths similar (within ~0.1mm of each other)
- TX2_N and TX2_P depths similar
- Via count still 9, keepout count still 0
- No new DRC violations

Then run full `--apply` workflow per CLAUDE.md.
