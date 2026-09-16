"""
verify_dfm.py
Design-for-Manufacturability (DFM) checks for a KiCad PCB.

Checks performed:
  1. Component-to-edge clearance >= MIN_COMP_EDGE_MM
  2. No via-in-pad (SMD pads)
  3. Paste aperture <= PASTE_APERTURE_MAX on large (EP) pads
  4. Through-hole inventory (warns; all-SMD preferred)
  5. ENIG finish recommendation (smallest pitch detection)
  6. Minimum drill size >= MIN_DRILL_MM
  7. Minimum trace width >= MIN_TRACE_MM
  8. Silkscreen not overlapping pads

Usage: python verify_dfm.py
"""

import sys
import math
import os

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"[PROJECT_DIR]\[PROJECT_NAME].kicad_pcb"
REPORTS_DIR     = r"[PROJECT_DIR]\Reports"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Board outline bounding box in mm (read from KiCad PCB Properties)
EDGE_X_MIN = 0.0    # leftmost edge X coordinate
EDGE_X_MAX = 100.0  # rightmost edge X coordinate
EDGE_Y_MIN = 0.0    # top edge Y coordinate
EDGE_Y_MAX = 100.0  # bottom edge Y coordinate

# DFM thresholds
MIN_COMP_EDGE_MM   = 3.0    # minimum component-to-board-edge clearance (mm)
PASTE_APERTURE_MAX = 0.60   # maximum paste aperture as fraction of pad area (60%)
MIN_DRILL_MM       = 0.2    # minimum drill diameter (mm)
MIN_TRACE_MM       = 0.1    # minimum trace width (mm)
ENIG_PITCH_THRESH  = 0.5    # pitch <= this value triggers ENIG recommendation (mm)
# ───────────────────────────────────────────────────────────────────────────

sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

REPORT_PATH = os.path.join(REPORTS_DIR, "dfm_report.txt")

board  = pcbnew.LoadBoard(PCB_FILE)
vias   = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"]

lines = []
lines.append("DFM Report")
lines.append("=" * 60)
lines.append("")

pass_count = 0
fail_count = 0
warn_count = 0

def record(status, check_num, name, evidence):
    global pass_count, fail_count, warn_count
    tag = f"[{status}]"
    lines.append(f"{tag} Check {check_num}: {name}")
    for line in evidence:
        lines.append(f"       {line}")
    lines.append("")
    if status == "PASS":
        pass_count += 1
    elif status == "FAIL":
        fail_count += 1
    else:
        warn_count += 1

# ── CHECK 1: Component-to-edge clearance ─────────────────────────────────────
def comp_to_edge_dist(fp):
    bb = fp.GetBoundingBox()
    l = pcbnew.ToMM(bb.GetLeft())
    r = pcbnew.ToMM(bb.GetRight())
    t = pcbnew.ToMM(bb.GetTop())
    b = pcbnew.ToMM(bb.GetBottom())
    dist_left   = l - EDGE_X_MIN
    dist_right  = EDGE_X_MAX - r
    dist_top    = t - EDGE_Y_MIN
    dist_bottom = EDGE_Y_MAX - b
    return min(dist_left, dist_right, dist_top, dist_bottom)

edge_violations = []
for fp in board.GetFootprints():
    ref = fp.GetReference()
    if ref.startswith("MH") or ref.startswith("H_"):
        continue
    d = comp_to_edge_dist(fp)
    if d < MIN_COMP_EDGE_MM:
        edge_violations.append(f"{ref} ({d:.2f}mm)")

if edge_violations:
    record("FAIL", 1, f"Component-to-edge clearance >= {MIN_COMP_EDGE_MM}mm",
           [f"{len(edge_violations)} violation(s): {', '.join(edge_violations[:8])}"])
else:
    record("PASS", 1, f"Component-to-edge clearance >= {MIN_COMP_EDGE_MM}mm",
           [f"All components >= {MIN_COMP_EDGE_MM}mm from board edge"])

# ── CHECK 2: No via-in-pad ────────────────────────────────────────────────────
vip_hits = []
for fp in board.GetFootprints():
    for pad in fp.Pads():
        if pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
            continue
        px = pcbnew.ToMM(pad.GetX())
        py = pcbnew.ToMM(pad.GetY())
        pw = pcbnew.ToMM(pad.GetSizeX()) / 2
        ph = pcbnew.ToMM(pad.GetSizeY()) / 2
        for via in vias:
            vx = pcbnew.ToMM(via.GetX())
            vy = pcbnew.ToMM(via.GetY())
            if abs(vx - px) < pw and abs(vy - py) < ph:
                vip_hits.append(f"{fp.GetReference()}.{pad.GetNumber()}")

if vip_hits:
    record("FAIL", 2, "No via-in-pad",
           [f"{len(vip_hits)} via(s) inside SMD pads: {', '.join(vip_hits[:8])}"])
else:
    record("PASS", 2, "No via-in-pad",
           ["No vias found inside SMD pad areas"])

# ── CHECK 3: Paste aperture on large (EP) pads ───────────────────────────────
paste_issues = []
for fp in board.GetFootprints():
    for pad in fp.Pads():
        if pad.GetAttribute() != pcbnew.PAD_ATTRIB_SMD:
            continue
        sz = pad.GetSize()
        pad_area = pcbnew.ToMM(sz.x) * pcbnew.ToMM(sz.y)
        if pad_area < 2.0:
            continue
        paste_offset = pad.GetLocalSolderPasteMargin()
        if paste_offset is None:
            paste_offset = 0
        paste_offset_mm = pcbnew.ToMM(paste_offset) if paste_offset != 0 else 0.0
        paste_w = pcbnew.ToMM(sz.x) + 2 * paste_offset_mm
        paste_h = pcbnew.ToMM(sz.y) + 2 * paste_offset_mm
        paste_area = max(0, paste_w) * max(0, paste_h)
        if pad_area > 0:
            ratio = paste_area / pad_area
            if ratio > PASTE_APERTURE_MAX:
                paste_issues.append(f"{fp.GetReference()}.{pad.GetNumber()} paste={ratio*100:.0f}%")

if paste_issues:
    record("WARN", 3, f"Paste aperture <= {int(PASTE_APERTURE_MAX*100)}% on EP pads",
           [f"Consider reducing: {', '.join(paste_issues[:6])}",
            "Use paste mask relief in footprint properties for large exposed pads."])
else:
    record("PASS", 3, f"Paste aperture <= {int(PASTE_APERTURE_MAX*100)}% on EP pads",
           ["No oversized paste apertures detected on large pads"])

# ── CHECK 4: Through-hole inventory ──────────────────────────────────────────
tht_list = []
for fp in board.GetFootprints():
    for pad in fp.Pads():
        if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
            tht_list.append(fp.GetReference())
            break

if tht_list:
    record("WARN", 4, "No through-hole components (all SMD preferred)",
           [f"THT exceptions ({len(tht_list)}): {', '.join(sorted(set(tht_list)))}",
            "Verify THT components are intentional (connectors, test points, etc.)"])
else:
    record("PASS", 4, "No through-hole components",
           ["All pads are SMD — fully SMT assembly confirmed"])

# ── CHECK 5: ENIG finish recommendation ──────────────────────────────────────
min_pitch_ref = None
min_pitch_val = 999.0
for fp in board.GetFootprints():
    pads = list(fp.Pads())
    if len(pads) < 2:
        continue
    positions = [(pcbnew.ToMM(p.GetX()), pcbnew.ToMM(p.GetY())) for p in pads]
    if len(positions) >= 2:
        min_d = min(math.hypot(positions[i][0] - positions[j][0],
                               positions[i][1] - positions[j][1])
                    for i in range(len(positions))
                    for j in range(i + 1, min(i + 2, len(positions))))
        if min_d < min_pitch_val and min_d > 0.05:
            min_pitch_val = min_d
            min_pitch_ref = fp.GetReference()

if min_pitch_val <= ENIG_PITCH_THRESH:
    record("PASS", 5, f"ENIG finish recommended for pitch <= {ENIG_PITCH_THRESH}mm",
           [f"Smallest pitch found: {min_pitch_val:.3f}mm on {min_pitch_ref}",
            "ENIG (Electroless Nickel Immersion Gold) finish is REQUIRED for reliable soldering"])
else:
    record("PASS", 5, "ENIG finish (pitch check)",
           [f"Smallest pitch found: {min_pitch_val:.3f}mm on {min_pitch_ref}",
            f"Pitch > {ENIG_PITCH_THRESH}mm — HASL acceptable but ENIG recommended"])

# ── CHECK 6: Minimum drill size ───────────────────────────────────────────────
drill_sizes = {}
for via in vias:
    d = round(pcbnew.ToMM(via.GetDrillValue()), 3)
    drill_sizes[d] = drill_sizes.get(d, 0) + 1
for fp in board.GetFootprints():
    for pad in fp.Pads():
        if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
            d = round(pcbnew.ToMM(pad.GetDrillSizeX()), 3)
            drill_sizes[d] = drill_sizes.get(d, 0) + 1

if drill_sizes:
    min_drill = min(drill_sizes.keys())
    drill_summary = sorted(drill_sizes.items())
    if min_drill >= MIN_DRILL_MM:
        record("PASS", 6, f"Minimum drill size >= {MIN_DRILL_MM}mm",
               [f"Min drill: {min_drill}mm  Distribution: {drill_summary}"])
    else:
        record("FAIL", 6, f"Minimum drill size >= {MIN_DRILL_MM}mm",
               [f"Min drill: {min_drill}mm (BELOW minimum!)  Distribution: {drill_summary}"])
else:
    record("WARN", 6, f"Minimum drill size >= {MIN_DRILL_MM}mm",
           ["No drills found — board may be fully SMD"])

# ── CHECK 7: Minimum trace width ─────────────────────────────────────────────
narrow_tracks = []
for t in tracks:
    w = pcbnew.ToMM(t.GetWidth())
    if w < MIN_TRACE_MM:
        narrow_tracks.append(f"{w:.4f}mm")

if narrow_tracks:
    record("FAIL", 7, f"Minimum trace width >= {MIN_TRACE_MM}mm",
           [f"{len(narrow_tracks)} track(s) below minimum: {narrow_tracks[:5]}"])
else:
    widths = sorted(set(round(pcbnew.ToMM(t.GetWidth()), 3) for t in tracks))
    record("PASS", 7, f"Minimum trace width >= {MIN_TRACE_MM}mm",
           [f"Min width: {widths[0] if widths else 'N/A'}mm  Widths used: {widths[:10]}"])

# ── CHECK 8: Silkscreen not on pads ──────────────────────────────────────────
silk_on_pad = []
silk_items  = [item for item in board.GetDrawings() if item.GetLayer() == pcbnew.F_SilkS]
smd_pads    = []
for fp in board.GetFootprints():
    for pad in fp.Pads():
        if pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD:
            smd_pads.append(pad)

for item in silk_items:
    try:
        item_pos_x = pcbnew.ToMM(item.GetPosition().x)
        item_pos_y = pcbnew.ToMM(item.GetPosition().y)
        for pad in smd_pads:
            px = pcbnew.ToMM(pad.GetX())
            py = pcbnew.ToMM(pad.GetY())
            pw = pcbnew.ToMM(pad.GetSizeX()) / 2
            ph = pcbnew.ToMM(pad.GetSizeY()) / 2
            if abs(item_pos_x - px) < pw and abs(item_pos_y - py) < ph:
                silk_on_pad.append(
                    f"silk@({item_pos_x:.1f},{item_pos_y:.1f}) on "
                    f"{pad.GetParentAsString()}.{pad.GetNumber()}"
                )
                break
    except Exception:
        pass

if silk_on_pad:
    record("WARN", 8, "Silkscreen not on pads",
           [f"{len(silk_on_pad)} silkscreen item(s) overlap pad areas: {', '.join(silk_on_pad[:4])}",
            "Silkscreen on pads degrades solder joint quality."])
else:
    record("PASS", 8, "Silkscreen not on pads",
           ["No silkscreen items found overlapping pad areas"])

# ── Summary ───────────────────────────────────────────────────────────────────
lines.append("=" * 60)
lines.append(f"SUMMARY: {pass_count} PASS / {warn_count} WARN / {fail_count} FAIL  (of 8 checks)")
if fail_count == 0:
    lines.append("RESULT: PASS — no blocking DFM issues")
else:
    lines.append(f"RESULT: FAIL — {fail_count} blocking issue(s) require resolution")
lines.append("=" * 60)

report = "\n".join(lines)
print(report)
os.makedirs(REPORTS_DIR, exist_ok=True)
with open(REPORT_PATH, "w", encoding="utf-8") as f:
    f.write(report)
print(f"\nReport written to: {REPORT_PATH}")
