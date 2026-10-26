# route_highspeed.py — Baseline Results
**Date:** 2026-10-26  
**Commit:** fcee29f / ba66a44  
**Board:** Frameline_Compute_V2.kicad_pcb  
**Pipeline:** clear → route_fanout_vias.py → route_critical.py → route_highspeed.py --apply

---

## Routing Status — 18 Pairs

| Pair | Layer | P_len (mm) | N_len (mm) | Δ (mm) | Limit (mm) | Status |
|---|---|---|---|---|---|---|
| HDMI0_CLK | In2.Cu | — | — | — | 0.127 | NOT ROUTED |
| HDMI0_TX0 | In2.Cu | 5.512 | 5.512 | 0.000 | 0.127 | PASS |
| HDMI0_TX1 | In2.Cu | 5.753 | 5.753 | 0.000 | 0.127 | PASS |
| HDMI0_TX2 | In2.Cu | 7.478 | 8.496 | 1.018 | 0.127 | REVIEW |
| LT_TX1 | In2.Cu | 3.958 | 4.957 | 0.999 | 0.127 | REVIEW |
| LT_TX2 | In2.Cu | 4.647 | 2.770 | 1.877 | 0.127 | REVIEW |
| LT_AUX | In2.Cu | 5.982 | 5.809 | 0.173 | 0.127 | REVIEW |
| DP_TX1 | In2.Cu | 4.345 | 1.930 | 2.416 | 0.127 | REVIEW |
| DP_TX2 | In2.Cu | 23.703 | 23.612 | 0.091 | 0.127 | PASS |
| DP_AUX | In2.Cu | 7.281 | 53.068 | 45.787 | 0.127 | ANOMALY — N detour via crystal area |
| MIPI1_CLK | In2.Cu | — | — | — | 0.127 | NOT ROUTED |
| MIPI1_D0 | In2.Cu | — | — | — | 0.127 | NOT ROUTED |
| MIPI1_D1 | In2.Cu | 74.163 | 73.736 | 0.427 | 0.127 | REVIEW |
| MIPI1_D2 | In2.Cu | 64.885 | 64.949 | 0.064 | 0.127 | PASS |
| MIPI1_D3 | In2.Cu | — | — | — | 0.127 | NOT ROUTED |
| DP_RX1 | In2.Cu | — | — | — | 0.127 | NOT ROUTED |
| DP_RX2 | In2.Cu | — | — | — | 0.127 | NOT ROUTED |
| USB2 | F.Cu | — | — | — | 0.500 | NOT ROUTED |

**Score: 4 PASS / 7 REVIEW / 1 ANOMALY / 6 NOT ROUTED (In2.Cu) / 1 NOT ROUTED (F.Cu)**

---

## DRC Summary

| Violation Type | Count | Notes |
|---|---|---|
| shorting_items | 14 | CRITICAL — pairs crossing wrong nets |
| track_width | 1 | DP_AUX_N 0.100mm < 0.127mm minimum |
| clearance | 255 | Mix of zone artifacts + HS routing violations |
| hole_clearance | 122 | Via drill spacing |
| via_dangling | 63 | Fanout vias with no HS route (not-routed pairs) |
| track_dangling | 28 | Partial routes |
| solder_mask_bridge | 11 | Pre-existing |
| starved_thermal | 5 | Pre-existing |
| lib_footprint_mismatch | 4 | Pre-existing |
| silk_overlap | 1 | Pre-existing |
| holes_co_located | 1 | — |
| hole_to_hole | 1 | — |
| **Unconnected** | **0** | All nets connected |
| **Total** | **506** | |

---

## Short Circuits (14) — Detail

| Short | Nets Involved | Root Cause |
|---|---|---|
| J_USB_OUT1 B3 ↔ Via | DP_TX2_N ↔ DP_TX2_P | P/N vias placed too close at connector |
| J_USB_OUT1 B2 ↔ Via | DP_TX2_P ↔ DP_TX2_N | Same — lateral order issue |
| Via ↔ Via | LT_TX1_N ↔ (unknown) | LT_TX1 via overlapping adjacent copper |
| Via ↔ Via ×2 | LT_TX1_P ↔ (unknown) | Same |
| Via ↔ Via | LT_TX2_N ↔ (unknown) | LT_TX2 via overlapping |
| Via ↔ Track ×2 | DP_TX1_P ↔ +5V trunk (In4.Cu) | DP_TX1 via landing on +5V power trunk |
| Track ↔ Pad | LT_AUX_N ↔ U3/22 (DP_AUX_N) | LT_AUX trace routing over U3 pad |
| Track ↔ Pad | DP_AUX_N ↔ C_XTAL_IN1 (LT_XTAL_IN) | DP_AUX_N 53mm detour crosses crystal cap |
| Via ↔ Track | LT_AUX_N ↔ LT_1V2 track | LT_AUX via landing on power trace |
| Via ↔ Track | LT_AUX_N ↔ LT_3V3 track | Same via, different power trace |
| Via ↔ (unknown) | LT_AUX_P | LT_AUX_P via placement issue |

---

## Key Anomaly: DP_AUX_N = 53mm vs P = 7mm

DP_AUX_N took a 45mm detour away from the direct path. The maze router found no direct path on In2.Cu and routed around all obstacles, passing through the crystal oscillator area (C_XTAL_IN1 at LT_XTAL_IN net), causing a short. The P route is 7mm and direct — N route failure is a routing obstacle model problem.

---

## Work Categories Identified

### Category 1 — 7 Pairs Not Routing
HDMI0_CLK, MIPI1_CLK, MIPI1_D0, MIPI1_D3, DP_RX1, DP_RX2, USB2 — all blocked. Need algorithmic fixes for the pairs that face tight-pitch obstacles (HDMI0/MIPI connector area) and USB2 on F.Cu.

### Category 2 — 14 Shorts
Via placement landing on wrong-net copper, and traces routing through crystal/power areas. Obstacle model missing some board copper.

### Category 3 — Skew on 7 Routed Pairs
HDMI0_TX2, LT_TX1, LT_TX2, LT_AUX, DP_TX1, DP_AUX, MIPI1_D1 — all exceed 0.127mm limit (range: 0.173mm to 1.877mm excluding DP_AUX anomaly). Route algorithm not constraining P/N path lengths.

---

## Trace Widths

All routed pairs use correct widths per routing_config.HS_ROUTE_WIDTHS:
- In2.Cu HDMI/LT/DP/MIPI: 0.127mm ✓ (except DP_AUX_N: 0.100mm — 1 violation)
- In2.Cu DP_TX1/TX2: 0.150mm ✓
- F.Cu USB2: 0.200mm (not routed)

---

## Clearances

Not individually verified — DRC shows 255 clearance violations, predominantly:
- HS tracks vs GND zone fill (zone artifact — disappears after Fill All Zones)
- HS vias vs neighboring pads (obstacle model gaps)
The 14 shorts are the priority; clearance violations without shorts will be re-evaluated after shorts are eliminated.
