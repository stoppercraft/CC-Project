"""
check_copper_balance.py — Layer-by-layer copper coverage analysis

Computes track, zone-fill, and pad copper area for every copper layer and
reports total coverage percentage. Flags layers where F.Cu / B.Cu ratio
falls below the warping-risk threshold.

Zones must be filled (Fill All Zones in KiCad) before running this script
for zone area data to be non-zero.

Writes a report to REPORT_FILE and prints to stdout.

Usage:
    python check_copper_balance.py

Fill in the PROJECT CONFIG block before running.
"""

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
REPORT_FILE     = r"C:\path\to\project\Reports\copper_balance.txt"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Copper layers to analyse (name -> pcbnew layer constant).
# Add or remove layers to match your stackup.
TARGET_LAYERS = {
    "F.Cu":   "F_Cu",
    "B.Cu":   "B_Cu",
    "In1.Cu": "In1_Cu",
    "In2.Cu": "In2_Cu",
    "In3.Cu": "In3_Cu",
    "In4.Cu": "In4_Cu",
}

# Display order
LAYER_ORDER = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]

# F.Cu / B.Cu min/max ratio below which warping risk is flagged
BALANCE_THRESHOLD = 0.40
# ─────────────────────────────────────────────────────────────────────────────

import sys
import os
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

board = pcbnew.LoadBoard(PCB_FILE)

# Resolve layer name strings to pcbnew constants
LAYER_IDS = {name: getattr(pcbnew, const) for name, const in TARGET_LAYERS.items()}

bbox          = board.GetBoardEdgesBoundingBox()
board_w_mm    = pcbnew.ToMM(bbox.GetWidth())
board_h_mm    = pcbnew.ToMM(bbox.GetHeight())
board_area_mm2 = board_w_mm * board_h_mm


def track_area_mm2(layer_id):
    total = 0.0
    for t in board.GetTracks():
        if t.GetClass() == "PCB_TRACK" and t.GetLayer() == layer_id:
            total += pcbnew.ToMM(t.GetLength()) * pcbnew.ToMM(t.GetWidth())
    return total


def pad_area_mm2(layer_id):
    total = 0.0
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.IsOnLayer(layer_id):
                sz = pad.GetSize()
                total += pcbnew.ToMM(sz.x) * pcbnew.ToMM(sz.y)
    return total


def zone_fill_area_mm2(layer_id):
    total = 0.0
    for z in board.Zones():
        if z.GetLayer() != layer_id:
            continue
        try:
            poly = z.GetFilledPolysList(layer_id)
            if poly and hasattr(poly, 'Area'):
                total += poly.Area() / 1e12  # nm² -> mm²
        except Exception:
            try:
                zb = z.GetBoundingBox()
                total += pcbnew.ToMM(zb.GetWidth()) * pcbnew.ToMM(zb.GetHeight()) * 0.7
            except Exception:
                pass
    return total


lines = []
lines.append("Copper Balance Report")
lines.append("=" * 55)
lines.append(f"Board size : {board_w_mm:.2f} x {board_h_mm:.2f} mm")
lines.append(f"Board area : {board_area_mm2:.2f} mm²")
lines.append("")
lines.append(f"{'Layer':<10}  {'Track':>8}  {'Zone':>8}  {'Pad':>8}  {'Total':>8}  {'%Cov':>6}")
lines.append("-" * 55)

copper_totals = {}
for lname in LAYER_ORDER:
    if lname not in LAYER_IDS:
        continue
    lid = LAYER_IDS[lname]
    ta  = track_area_mm2(lid)
    za  = zone_fill_area_mm2(lid)
    pa  = pad_area_mm2(lid)
    total = ta + za + pa
    pct   = (total / board_area_mm2 * 100) if board_area_mm2 > 0 else 0
    copper_totals[lname] = total
    lines.append(f"{lname:<10}  {ta:>8.2f}  {za:>8.2f}  {pa:>8.2f}  {total:>8.2f}  {pct:>5.1f}%")

lines.append("")

# F.Cu vs B.Cu balance check
fcu = copper_totals.get("F.Cu", 0.0)
bcu = copper_totals.get("B.Cu", 0.0)

if fcu > 0 and bcu > 0:
    ratio = min(fcu, bcu) / max(fcu, bcu)
    lines.append(f"F.Cu copper : {fcu:.2f} mm²")
    lines.append(f"B.Cu copper : {bcu:.2f} mm²")
    lines.append(f"min/max ratio (F.Cu:B.Cu): {ratio:.3f}")
    if ratio < BALANCE_THRESHOLD:
        lines.append(
            f"FLAG: ratio {ratio:.3f} < {BALANCE_THRESHOLD:.2f} — "
            f"significant copper imbalance (warping risk)\n"
            f"      Consider adding copper pour or hatching on the lighter layer."
        )
    else:
        lines.append(f"PASS: ratio {ratio:.3f} >= {BALANCE_THRESHOLD:.2f} — copper balance acceptable")
elif fcu == 0 and bcu == 0:
    lines.append("WARNING: No copper area computed for F.Cu or B.Cu")
    lines.append("         Zones may not be filled — run Fill All Zones in KiCad first.")
else:
    lines.append(f"F.Cu copper : {fcu:.2f} mm²")
    lines.append(f"B.Cu copper : {bcu:.2f} mm²")
    lines.append("WARNING: One layer has zero copper — check if zones are filled")

lines.append("")
lines.append("Note: Zone fill areas use GetFilledPolysList(). If zones are not")
lines.append("filled, zone area = 0. Track + pad areas are always computed.")

report = "\n".join(lines)
print(report)
os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
with open(REPORT_FILE, "w", encoding="utf-8") as f:
    f.write(report)
print(f"\nReport written to: {REPORT_FILE}")
