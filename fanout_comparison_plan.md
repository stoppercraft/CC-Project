# Fanout Script vs Reference Board Comparison Plan
# Generated 2026-09-22, session 25

## DRC Status (post Phase 3.5 fixes, session 25)
- **0 signal violations** ✓ (0 shorts, 0 non-zone clearance)
- 118 vias placed (8 VIPPO + 110 side-exit), 23 keepout escape=0
- Previous baseline (session 24): 126 vias, 6 clearance violations
- Phase 3.5 suppressed 8 additional keepout pads (3 HS pairs)

---

## Comparison: Script Output vs Manually-Routed Reference

Reference: `Frameline_Compute_V2-backups/Frameline_Compute_V2-2026-09-21_100203.zip`
Reference board: 124 vias, 196 track segments

---

## Class A: Missing Vias (5 nets — Requirement 1 violations)

These nets have vias in the reference but escape=0 in the script.

| Net | Ref via pos | Nearest ref pad | Escape dist | Script status |
|-----|-------------|-----------------|-------------|---------------|
| LT_TX1_P | (151.417, 122.298) | U3/39 d=1.705mm | ~1.7mm | escape=0 (pair-suppressed) |
| LT_TX2_N | (149.500, 121.720) | U3/34 d=1.125mm | ~1.1mm | escape=0 (ko-pair-suppress) |
| LT_TX2_P | (148.982, 122.262) | U3/33 d=1.671mm | ~1.7mm | escape=0 (ko-pair-suppress) |
| LT_XTAL_OUT | (146.025, 114.095) | U3/18 d=2.175mm | ~2.2mm | escape=0 (keepout) |
| STUSB_2V7 | (139.343, 108.641) | C_STUSB_2V7/1 d=0.0mm | 0mm | U2/23 escape=0; C_STUSB_2V7 suppressed |

**Root causes:**

- **LT_TX1_P, LT_TX2_N/P**: HS pair ko-pair-suppress logic. Phase 3.5 marks these as stagger-2d
  keepouts. In section 4a, one partner gets a col-exit, the other's via conflicts with the first
  partner's trace → ko-pair-suppress fires → BOTH escape=0. Reference author found diagonal escape
  corridors for both partners without conflict. The script's col-exit approach cannot produce the
  same geometry.

- **LT_XTAL_OUT (U3/18)**: 0.200×0.650mm pad. `_keepout_escape_length` fails to find a clear
  corridor in the radial direction. Reference has via 2.2mm from pad — the reference author manually
  found an escape route that the algorithm is blocking.

- **STUSB_2V7**: U2/23 is the STUSB_2V7 IC pad (0.250×0.825mm, keepout, escape=0). C_STUSB_2V7/1
  is a 0603 capacitor on the same net that DOES have a viable large pad and should get the via.
  But in the via-sharing logic, U2/23 is either chosen as representative (then becomes keepout) or
  C_STUSB_2V7 is shared/suppressed away. The reference correctly places the via at C_STUSB_2V7.

**Proposed fix A1 (STUSB_2V7 — easiest)**: Debug via-sharing for STUSB_2V7 net. C_STUSB_2V7/1
is a 0.6mm capacitor pad that can take a via directly. The script should pick it as representative
instead of U2/23. The share logic selects the first pad in distance-from-center order. Likely
C_STUSB_2V7/1 is further from the board center than U2/23, causing U2/23 to be chosen first.
Fix: in the via-sharing representative selection, prefer pads that are NOT keepout candidates
(i.e., large enough for a via). If the current representative becomes keepout, fall back to the
next-closest pad that can accept a via.

**Proposed fix A2 (LT_XTAL_OUT — medium)**: U3/18 has escape=0 because the corridor is blocked.
The reference via is 2.2mm from the pad — further than any current search attempts. Need to verify
whether increasing the max search distance in `_keepout_escape_length` reveals a clear corridor at
that distance, OR whether the obstacle check is using an over-conservative pad approximation.

**Proposed fix A3 (LT_TX1/TX2 pair — hardest)**: The reference places independent escape vias for
both HS partners without conflict. The script's ko-pair-suppress fires because one partner's via
lands on the other's escape trace. The fundamental issue: both partners escape in nearly the same
direction (they're adjacent pads). A solution requires either:
- Computing both partners' escape paths jointly so their vias and traces don't overlap
- Allowing angled (vs col-exit-perpendicular) escape traces that diverge more
This is a new algorithm (joint HS keepout escape placement).

---

## Class B: Via Count Differences (12 nets)

These nets have different numbers of vias between ref and live.

### Live has 1, ref has 2 (script is missing U3-side via):
| Net | Missing U3 via pos | Note |
|-----|-------------------|------|
| HDMI0_CLK_N | (154.614, 111.664) | U3/3 pair-suppressed |
| HDMI0_CLK_P | (154.140, 111.082) | U3/4 stagger-2d-ko |
| HDMI0_TX2_N | (149.595, 111.112) | U3/12 ko-pair-suppress |
| HDMI0_TX2_P | (149.000, 111.600) | U3/13 stagger-2d-ko |
| LT_3V3 | (147.075, 114.895) | U3/20 not in pending? |
| LT_SPI_CLK | (158.175, 119.295) | U3/50 keepout escape=0 |
| LT_SPI_MISO | (157.125, 118.495) | U3/52 stagger/neckdown diff? |
| MIPI1_D1_P | (147.386, 151.763) | J_DSI1/7 pair-suppressed |

For the HDMI0 pairs (CLK, TX2): reference has vias at BOTH the SOM2 side AND the U3 side. Script
pair-suppresses the U3-side vias. The reference author successfully placed both sides independently.
Fix: same as A3 — need joint keepout escape that doesn't cause pair suppression.

For LT_3V3: ref has via near U3/20 (d=1.125mm) at (147.075, 114.895). Script places LT_3V3 via
only at R_RST1/1. U3/20 (LT_3V3) is on a face with 0.650×0.200mm pads. This via must be a
separate via the reference author added for the U3 LT_3V3 pad. Script suppresses U3/20 via
`[share] R_RST1/1 (LT_3V3): via shared — suppressed U3/20,...`. Fix: LT_3V3 pads on U3 need
their own keepout escape, not sharing with R_RST1 which is far away.

### Live has 2, ref has 1 (script placing extra via):
| Net | Extra live via | Note |
|-----|----------------|------|
| HDMI0_HPD | live: (159.155,115.455) + (148.150,98.155) | ref only has R_HPD1 via |
| MIPI1_D0_N | live: SOM2 + J_DSI1 side | ref only has SOM2 |
| MIPI1_D2_P | live: SOM2 + J_DSI1 side | ref only has J_DSI1 |
| STUSB_SCL | live: R_STSCL1 + SOM1 side | ref only has R_STSCL1 |
| TOUCH_SCL | live: R_TSCL1 + SOM1 side | ref only has R_TSCL1 |

For MIPI1_D0_N and MIPI1_D2_P: both J_DSI1 and SOM2 are endpoints of the same net. Script places
vias at BOTH ends. Reference only places one via (at whichever end needed the layer transition).
This is not necessarily wrong — having both may be needed for routing. Not a violation.

For STUSB_SCL and TOUCH_SCL: script places vias near SOM1 pads AND near R_STSCL1/R_TSCL1.
Reference only places one. Extra vias are not DRC violations but may cause routing complications.

---

## Class C: Position Differences

### C1: Exact 1.1mm shift (SOM2 HS N-partner vias)
| Net | Ref | Live | Delta |
|-----|-----|------|-------|
| MIPI1_C_N | (153.9, 98.79) | (153.9, 97.69) | -1.1mm y |
| MIPI1_D1_N | (152.7, 98.79) | (152.7, 97.69) | -1.1mm y |
| HDMI0_CLK_N | (154.3, 104.17) | (154.3, 105.27) | +1.1mm y |
| HDMI0_TX0_N | (153.1, 104.17) | (153.1, 105.27) | +1.1mm y |
| HDMI0_TX1_N | (151.9, 104.17) | (151.9, 105.27) | +1.1mm y |

MIPI pairs escape upward (-y), HDMI pairs escape downward (+y). In both cases the N-via in live
is 1.1mm FURTHER from the component than reference. The stagger computes minimum neckdown, but
reference uses a shorter neckdown. Investigation needed: is the stagger minimum over-conservative,
or did the reference author use sub-minimum neckdowns (potentially violating via clearance)?

Likely cause: the stagger uses `neckdown 0.500→1.370mm` for these N-vias. Ref appears to use
~0.27mm neckdown (SOM2 pads at 0.4mm pitch, N-via placed very close to pad center). This would
be less than the 0.5mm initial value — ref may have manually placed vias with different parameters.

### C2: Large diffs on U3 HDMI TX pair vias (1.4-2.3mm)
| Net | Ref | Live | Delta |
|-----|-----|------|-------|
| HDMI0_TX0_P | (152.6, 109.75) | (152.593, 112.015) | +2.265mm y |
| HDMI0_TX1_N | (151.344, 110.135) | (151.89, 111.745) | +1.700mm y |
| HDMI0_TX1_P | (150.616, 110.434) | (151.139, 111.770) | +1.435mm y |

stagger-2d algorithm finds equilibrium at different (larger) neckdown than reference.
Reference vias are much closer to the component pads.

### C3: LT_1V2 wrong representative (9.7mm)
Script picks U3/32 as LT_1V2 representative; reference uses U3/55.
Both are on LT_1V2 net. U3/55 is a pad on a different face of U3. Fix: via-sharing
representative should account for which pad is MOST CONSTRAINED (U3/55 may be a better
candidate). Or: both should get their own vias since they are far apart (9.7mm).

### C4: TOUCH_INT wrong component (80mm)
Ref places TOUCH_INT via near SOM1/54 (y≈71). Live places via near J_DSI1/16 (y≈151).
SOM1/54 is 0.200×1.140mm keepout in script (escape=0); J_DSI1/16 gets the via instead.
Reference author manually placed a TOUCH_INT via near SOM1/54 (found the escape corridor).

### C5: Minor diffs 0.1-0.3mm
Affects: BUCK_BOOT, BUCK_SS, BUCK_SW, CM5_3V3 (×3), HDMI0_SCL, HDMI0_SDA, COMP_MID, etc.
These are tighten-pass and stagger rounding differences. Acceptable variation unless routing
requires exact positions.

---

## Priority Order for Fixes

**Fix 1 (this session)**: STUSB_2V7 — fallback representative to a non-keepout pad.
**Fix 2 (this session)**: LT_3V3 — U3/20 should not be shared with R_RST1 (too far).
**Fix 3 (this session)**: LT_1V2 — U3/32 and U3/55 are 9.7mm apart, should get independent vias.
**Fix 4 (next session)**: SOM2 N-partner neckdown — investigate if stagger minimum is over-conservative.
**Fix 5 (future)**: U3 HS keepout pair joint-escape — needed for HDMI0_CLK, TX2, LT_TX1, LT_TX2.
**Fix 6 (future)**: U3 HDMI TX stagger-2d distance — closer equilibrium to match reference.

---

## Iteration 1 — What's Fixed vs Reference After Session 25

✓ 0 DRC violations
✗ 5 missing vias (Class A)
✗ 8 nets with wrong via count (Class B — missing U3 side)
✗ 5 nets with extra vias (Class B — extra side)
✗ 1 extra via (GPIO15 — not in reference)
✗ Multiple position diffs (Class C)

---

## Iteration 2 — Session 26 (Fixes 1 and 3: STUSB_2V7, LT_1V2)

**Changes applied:**
1. Post-selection strict-keepout VIPPO fallback: if primary has pad_min < via_drill, swap to VIPPO-capable
   alternative. Fixed STUSB_2V7 (U2/23 strict-keepout → C_STUSB_2V7/1 VIPPO via). No proximity guard
   initially → caused LT_XTAL_IN and LT_SPI_MOSI regressions.
2. Same-component guard in `_should_suppress`: `sec.ref != pri.ref` prevents cross-component chain-suppress
   for radial-fanout secondaries. Fixed LT_1V2 (U3/55 now correctly suppressed by U3/32 same-component).

**Result (before regression fix): 122 vias, 0 DRC violations, 18 diffs**

---

## Iteration 3 — Session 26 (Proximity guard on VIPPO fallback)

**Root cause of regressions in iteration 2:**
- VIPPO fallback (no proximity guard) activated for U3/19 (LT_XTAL_IN) → C_XTAL_IN1/1 (4.647mm edge)
  became primary → 1 extra via (ref=0)
- VIPPO fallback activated for U3/51 (LT_SPI_MOSI) → U8/5 (4.88mm edge) became primary → 1 extra via
  (ref=0)
- Correct case: U2/23 (STUSB_2V7) → C_STUSB_2V7/1 at 0.853mm edge (immediately adjacent bypass cap)

**Fix applied:** Added `_pad_edge_dist(pv, primary) <= 2.0mm` guard to VIPPO fallback. Only pads within
2mm edge distance of the strict-keepout primary qualify as VIPPO alternatives. This captures the
"adjacent bypass cap" case (0.853mm) but rejects "distant same-net pad" cases (4.6-4.9mm).

**Script confirmation:**
- `[share] U3/51 (LT_SPI_MOSI): via shared — suppressed U8/5` — U3/51 primary, escape=0, 0 vias ✓
- `[share] U3/18 (LT_XTAL_OUT): via shared — suppressed C_XTAL_OUT1/1, Y1/2` — U3/18 primary, escape=0
- `[share] U3/19 (LT_XTAL_IN): via shared — suppressed C_XTAL_IN1/1, Y1/1` — U3/19 primary, escape=0 ✓
- `[stagger-2d-ko]` fires for U3/18, U3/19, U3/51 — all fail stub clearance check

**Result: 119 vias, 0 DRC violations, 17 net count diffs**

| Fixed | Net | Change |
|-------|-----|--------|
| ✓ | STUSB_2V7 | live=0→1, via at C_STUSB_2V7/1 VIPPO ✓ |
| ✓ | LT_1V2 | live=3→2 (U3/55 same-component suppressed) ✓ |
| ✓ | LT_XTAL_IN regression | live=1→0 (C_XTAL_IN1/1 not selected, ref=0) ✓ |
| ✓ | LT_SPI_MOSI regression | live=1→0 (U8/5 not selected, ref=0) ✓ |
| ✗ | LT_XTAL_OUT | live=1→0 (C_XTAL_OUT1/1 was coincidental in iter2; U3/18 escape=0, ref=1) |

**Remaining diffs (17 nets):**

Extra vias (live > ref): GPIO15 (+1), HDMI0_HPD (+1), MIPI1_D0_N (+1), MIPI1_D2_P (+1),
STUSB_SCL (+1), TOUCH_SCL (+1)

Missing vias (live < ref): HDMI0_CLK_N (-1), HDMI0_CLK_P (-1), HDMI0_TX2_N (-1),
HDMI0_TX2_P (-1), LT_3V3 (-1 U3/20 side), LT_SPI_CLK (-1 U3/50),
LT_TX1_P (-1), LT_TX2_N (-1), LT_TX2_P (-1), LT_XTAL_OUT (-1), MIPI1_D1_P (-1)

**Root causes of remaining missing vias:**
- U3/18, U3/20, U3/50, J_DSI1/7: all stagger-2d-ko or keepout-escape=0 (Phase 3.5 needed)
- LT_TX1/TX2: ko-pair-suppress — joint HS keepout escape algorithm needed
- HDMI0_CLK/TX2: joint HS keepout escape algorithm needed

**Root causes of remaining extra vias:**
- STUSB_SCL, TOUCH_SCL, MIPI1_D0_N, MIPI1_D2_P: script places vias at both ends of net;
  reference routes some nets single-layer without a via at one end
- GPIO15, HDMI0_HPD: extra via at secondary pad (reference only places one)

**Priority for next iteration:**
- Fix A (iter 4): Investigate extra vias (STUSB_SCL, TOUCH_SCL) — can these be suppressed
  without affecting nets that genuinely need both vias?
- Fix B (iter 4): Phase 3.5 col-exit for LT_XTAL_OUT and LT_3V3 — but risks regressions for
  LT_XTAL_IN and LT_SPI_MOSI if col-exit succeeds there too
