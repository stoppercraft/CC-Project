# Fanout Via Refactor Plan
# route_fanout_vias.py — Expand to All Nets + Fix Sandwiched Non-HS Pads
# Updated 2026-09-16

---

## STATUS: PENDING IMPLEMENTATION

---

## PROBLEM STATEMENT

U3 pads 5 (LT_1V2), 8 (LT_3V3), and 11 (LT_1V2) cannot be routed after the fanout
script runs because HS vias from the HDMI0 pairs surround them. The fundamental fix is
to expand the script to process ALL nets — so it places inward-escape vias for pads 5/8/11
before the HS via wall blocks them. The sandwiched detection + inward escape code (already
implemented) will then fire correctly for these pads.

---

## ROOT CAUSE

### Why pads 5/8/11 are never processed

Script output confirms: `Pads needing vias: 70 (HS=59 switching=11 other=0)`. Zero OTHER
priority pads are processed. The cause is a single return value in `classify()`:

**`route_fanout_vias.py` lines 179–188:**
```python
def classify(net_name, hs_map, sw_map, board):
    if net_name in hs_map:
        return PRIORITY_HS, board.GetLayerID(hs_map[net_name])
    if net_name in sw_map:
        return PRIORITY_SW, board.GetLayerID(sw_map[net_name])
    return PRIORITY_OTHER, board.GetLayerID(cfg.ROUTING_LAYER_PRIORITY[0])  # ← BUG
```

`cfg.ROUTING_LAYER_PRIORITY[0]` = `"F.Cu"`. So ALL other-priority nets get target = F.Cu.

**The exclusion gate (lines 2549–2578):**
```python
if pad_layer_id == tgt_layer:          # F.Cu == F.Cu → TRUE for U3/5, U3/8, U3/11
    if priority == PRIORITY_OTHER:     # TRUE
        _hs_tgt = fp_hs_target.get(ref)
        if _hs_tgt is not None and _hs_tgt != pad_layer_id:
            # Only passes if net is an undeclared diff pair (_P/_N)
            # LT_1V2 and LT_3V3 don't end in _P/_N → continue → EXCLUDED
            ...
        else:
            continue                   # excluded
```

Since `pad_layer_id == tgt_layer` (both F.Cu), the gate fires. Since LT_1V2 and LT_3V3
aren't diff-pair-named, pads 5/8/11 are discarded. They never reach `pending.append(pv)`.

**The fix:** Change `classify()` to return `ROUTING_LAYER_PRIORITY[1]` = `"B.Cu"` as the
target for PRIORITY_OTHER. Then U3/5 (on F.Cu) has target B.Cu → `pad_layer_id != tgt_layer`
→ the gate is NOT entered → proceeds to create a PendingVia and enter `pending`.

---

## WHAT IS ALREADY IMPLEMENTED (from prior session — do not re-implement)

These three changes are already in the script and working correctly:

1. **`PendingVia.sandwiched` field** (line ~85): `sandwiched: bool = False`
2. **Sandwiched detection loop** (inserted after p-offset block, ~line 2795–2850):
   Detects non-HS pads with HS pads on both lateral sides within 0.45mm pitch tolerance.
   Flips escape direction to inward (+Y for the HDMI0 face), sets neckdown_len_mm to the
   minimum depth needed to clear HS pad inner corners (~0.71mm for 0.4mm pitch pads).
   Prints `[sandwiched]` diagnostic lines.
3. **Proximity suppression exclusion** (line ~1399):
   `eligible = [pv for pv in pending if pv.priority != PRIORITY_HS and not pv.sandwiched]`
   Sandwiched pads are never proximity-suppressed — each gets its own inward via.

These are correct and only need the classify() fix (Change 1 below) to activate for pads 5/8/11.

---

## IMPLEMENTATION — 1 CODE CHANGE REQUIRED

### Change 1 — Fix `classify()` target layer for PRIORITY_OTHER (line 188)

**File:** `E:\Claude Projects\CC Project Folder\Python Scripts\route_fanout_vias.py`

**Location:** Line 188, the last return statement in `classify()`.

**Old:**
```python
    return PRIORITY_OTHER, board.GetLayerID(cfg.ROUTING_LAYER_PRIORITY[0])
```

**New:**
```python
    return PRIORITY_OTHER, board.GetLayerID(cfg.ROUTING_LAYER_PRIORITY[1])
```

**Why index 1:** `ROUTING_LAYER_PRIORITY = ["F.Cu", "B.Cu", "In2.Cu"]`. Index 0 is F.Cu
(wrong — same as source layer, causes exclusion). Index 1 is B.Cu — the correct general
routing layer for non-HS/SW pads. F.Cu SMD pads will have target B.Cu → need a layer
transition → pass the exclusion gate → enter `pending` as PRIORITY_OTHER.

**No other changes needed.** The entire placement pipeline already handles PRIORITY_OTHER:
- `via_params(PRIORITY_OTHER)` returns standard via geometry
- `neckdown_params(PRIORITY_OTHER, net_name)` returns standard neckdown geometry
- `_suppress_proximity_via_sharing` already covers PRIORITY_OTHER (eligible = non-HS)
- `_place_group` already processes all priorities (sorts by priority: 0, 1, 2)
- The sandwiched detection loop already processes non-HS, non-keepout pads
- The crossing detection and conflict passes already handle all priorities

---

## EXPECTED BEHAVIOR AFTER CHANGE 1

### For U3 pads 5, 8, 11 (the reported problem)

1. U3/5 (LT_1V2, x=153.5, y=112.7954): enters pending with priority=PRIORITY_OTHER,
   escape_dx=0, escape_dy=−1 (same -Y direction as HDMI0 face pads).
2. Sandwiched detection fires: HS pads CLK_P (x=153.9) and TX0_N (x=153.1) are within
   0.4mm on both lateral sides → sandwiched=True.
3. Escape direction flipped to +Y (inward toward chip interior).
4. neckdown_len_mm set to ≥0.71mm (enough to clear HS pad inner corners at y=113.12mm).
5. Via placed at approximately (153.5, 113.5mm) — on the interior side of the chip face.
6. Stub runs from pad (153.5, 112.795) straight inward to via (153.5, ~113.5). Clears
   adjacent HS pad edges by 0.3mm (need 0.25mm). ✓
7. Same logic for pads 8 (x=152.3) and 11 (x=151.1) — each gets its own inward via.

### For the rest of the board

All F.Cu SMD pads on non-HS, non-switching nets will now enter `pending`. Proximity
suppression will cluster same-net pads (e.g., multiple LT_1V2 pads elsewhere will share
vias, with only primaries getting explicit vias). The via count will increase from 63.
Most new vias will be simple outward-escape — no sandwiched condition applies to them.

---

## RISKS AND MITIGATIONS

**Risk 1: Via count explosion.** Hundreds of "other" pads may enter pending.
Mitigation: proximity suppression (`via_share_proximity_mm`) clusters same-net pads.
GND and other skip-nets are already excluded by `skip_nets`. If via count is too high,
check that skip_nets includes appropriate power planes.

**Risk 2: New DRC violations from OTHER pad stubs or via positions.**
Mitigation: the existing conflict passes and keepout fallback handle placement failures.
Run DRC after --apply and count violations by type.

**Risk 3: Sandwiched stub width too wide → clearance violation with adjacent HS pads.**
The stub for pad 5/8/11 has only 0.05mm margin (0.3mm actual − 0.25mm required).
If `neckdown_stub_width()` returns > 0.2mm for OTHER pads, the stub will DRC-fail.
Mitigation: check `[sandwiched]` output — it prints the via positions. If DRC shows
clearance violations on LT_1V2 or LT_3V3 stubs near U3, narrow the stub width for
sandwiched pads: after `_pv.sandwiched = True` in the detection loop, add:
```python
        _pv.neckdown_w_mm = min(_pv.neckdown_w_mm, _ca.get("signal_trace_width_mm", 0.20))
```
(Use `_ca` dict, same variable available in that scope for config lookup.)

**Risk 4: B.Cu pads on the board (bottom-side components) also entering pending.**
B.Cu SMD pads with OTHER priority: target = B.Cu, pad_layer_id = B.Cu → same layer →
enter the exclusion gate → discarded (same as before). B.Cu pads are unaffected. ✓

---

## INVESTIGATION STEP (run BEFORE implementing)

Run dry-run to baseline the current "other=0" state and confirm classify() behavior:

```
"C:\Program Files\KiCad\10.0\bin\python.exe" "E:\Claude Projects\CC Project Folder\Python Scripts\route_fanout_vias.py" 2>&1
```

Record the `Pads needing vias` line. After implementing Change 1, this line should show
other > 0. The `[sandwiched]` lines should appear for U3/5, U3/8, U3/11.

---

## VERIFICATION — FULL APPLY WORKFLOW (CLAUDE.md mandatory sequence)

After implementing Change 1:

**Step 1 — Dry-run first:**
```
"C:\Program Files\KiCad\10.0\bin\python.exe" "E:\Claude Projects\CC Project Folder\Python Scripts\route_fanout_vias.py" 2>&1
```
Confirm:
- `[sandwiched]` lines appear for U3/5, U3/8, U3/11
- `other > 0` in "Pads needing vias" line
- No Python exceptions or stack traces
- Inward via positions for pads 5/8/11 are between y=113.0 and y=115.0mm

**Step 2 — Clear board:**
```
"C:\Program Files\KiCad\10.0\bin\python.exe" "C:/Temp/clear_board.py"
```
(Recreate clear_board.py if missing — loads board, iterates Tracks(), removes all, saves.)

**Step 3 — Apply:**
```
cd "E:\Claude Projects\CC Project Folder\Python Scripts"
"C:\Program Files\KiCad\10.0\bin\python.exe" route_fanout_vias.py --apply 2>&1
```
Run once. Record full output.

**Step 4 — DRC:**
```
"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe" pcb drc --output "C:/Temp/drc_fanout.json" --format json "E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb"
```
Parse `C:/Temp/drc_fanout.json`. Count violations by type.

**Success criteria:**
- 0 non-zone DRC violations (or only pre-existing violations matching the known-good list)
- `[sandwiched]` lines confirmed for U3/5, U3/8, U3/11
- Via count increases from 63 (expected — other pads now included)
- No clearance violations involving LT_1V2 or LT_3V3 stubs near U3

**Known-good pre-existing violations (do not flag these):**
- silk_overlap×199, silk_over_copper×199, track_width×58, via_dangling×28,
  starved_thermal×8, annular_width×1, solder_mask_bridge×1
- Zone artifacts: clearance×273, hole_clearance×174 (unfilled GND zones)

---

## KEY CODE FACTS (for implementation reference)

- **classify() location:** Line ~179–188 in route_fanout_vias.py
- **The exact line to change:** Line 188 (last return of classify)
- **ROUTING_LAYER_PRIORITY:** ["F.Cu", "B.Cu", "In2.Cu"] in routing_config.py line ~197
- **Exclusion gate:** Lines 2549–2578 (if pad_layer_id == tgt_layer: block)
- **Sandwiched detection:** Already at lines ~2797–2851 (after p-offset loop)
- **Proximity suppression:** Line ~1399 already excludes sandwiched pads
- **_ca variable:** Available in scope at the sandwiched detection location as
  `_ca = cfg.CLEARANCE_AUDIT` (or equivalent dict) — use for stub width fallback if needed

---

## FILES INVOLVED

  Primary:  E:\Claude Projects\CC Project Folder\Python Scripts\route_fanout_vias.py
  Config:   E:\Claude Projects\CC Project Folder\Python Scripts\routing_config.py  (read-only)
  Board:    E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb
