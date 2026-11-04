# HS Routing Chain Terminal Reference

## Chain Terminal Coordinates — session 52 (2026-11-03)

Single clean run from snapshot baseline (397 fanout+critical tracks → route_highspeed.py --apply once).

**Method:** Degree-1 endpoint analysis on In2.Cu tracks. A chain terminal is the degree-1 node
in the track connectivity graph — the actual dangling end of a route. NOT GetStart/GetEnd of
individual segments (those include intermediate junctions). Gap = distance from terminal to
nearest same-net via nm coordinate.

---

### MIPI1_CLK — NOT ROUTED (0 In2.Cu tracks in full pipeline run)

---

### MIPI1_D2 — P: 24 In2.Cu tracks, N: 23 In2.Cu tracks

| Net | End | mm coordinates | nm coordinates | Gap to via |
|---|---|---|---|---|
| MIPI1_D2_P | SOM2 source | (155.8000, 98.6648) | (155799999, 98664800) | 0 nm |
| MIPI1_D2_P | J_DSI1 dest | (148.9174, 151.3387) | (148917400, 151338734) | 0 nm |
| MIPI1_D2_N | SOM2 source | (155.0500, 98.6648) | (155049999, 98664800) | 0 nm |
| MIPI1_D2_N | J_DSI1 dest | (148.1674, 151.3387) | (148167400, 151338734) | 0 nm |

---

### HDMI0_TX0 — P: 5 In2.Cu tracks, N: 4 In2.Cu tracks

| Net | End | mm coordinates | nm coordinates | Gap to via |
|---|---|---|---|---|
| HDMI0_TX0_P | SOM2 source | (152.3500, 105.5448) | (152350000, 105544800) | 0 nm |
| HDMI0_TX0_P | U3 dest | (152.9000, 110.5067) | (152899999, 110506651) | 0 nm |
| HDMI0_TX0_N | SOM2 source | (153.1000, 105.5448) | (153100000, 105544800) | 0 nm |
| HDMI0_TX0_N | U3 dest | (153.6500, 110.5067) | (153650000, 110506651) | 0 nm |

---

### HDMI0_CLK — P: 5 In2.Cu tracks, N: 4 In2.Cu tracks

| Net | End | mm coordinates | nm coordinates | Gap to via |
|---|---|---|---|---|
| HDMI0_CLK_P | SOM2 source | (153.7000, 103.8170) | (153700000, 103817013) | 0 nm |
| HDMI0_CLK_P | U3 dest | (154.0500, 111.6482) | (154050000, 111648186) | 0 nm |
| HDMI0_CLK_N | SOM2 source | (154.4500, 103.8170) | (154450000, 103817013) | 0 nm |
| HDMI0_CLK_N | U3 dest | (154.8500, 111.6482) | (154850000, 111648186) | 0 nm |

---

## Notes

- All routed pair terminals have gap = 0 nm (exact via centers, confirmed by _snap_track_endpoints_to_vias post-pass).
- nm values like 155799999 vs 155800000 are 1 nm floating-point rounding — well within snap tolerance.
- MIPI1_CLK assigned VH corridor crossing but routes 0 In2.Cu tracks in full pipeline — blocked.
- Board state at measurement: 397 baseline + single highspeed run. Commit: f0b4f77 era.
