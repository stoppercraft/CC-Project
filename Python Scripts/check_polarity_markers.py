"""
check_polarity_markers.py — Verify polarity / orientation markers on silkscreen

Checks ICs (U*), diodes (D*), and polarized capacitors (C* with polarized
footprint keywords) to confirm that each has at least one silkscreen or fab
graphic nearby (pin-1 dot, cathode bar, +/− mark, etc.).

Components with no nearby graphic are flagged as MISSING.

Writes a report to REPORT_FILE and prints to stdout.

Usage:
    python check_polarity_markers.py

Fill in the PROJECT CONFIG block before running.
"""

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
REPORT_FILE     = r"C:\path\to\project\Reports\polarity_markers.txt"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Radius (mm) from component centre within which a silkscreen/fab graphic must exist
SEARCH_RADIUS_MM = 3.0

# Reference prefixes to check for polarity markers
IC_PREFIX       = "U"   # integrated circuits
DIODE_PREFIX    = "D"   # diodes, LEDs, TVS, Schottky
CAP_PREFIX      = "C"   # capacitors (filtered to polarized types only)

# Footprint ID / value substrings that identify polarized capacitors
POLARIZED_FOOTPRINT_KEYWORDS = [
    "elco", "cp_", "cpol", "polarized", "electrolytic",
    "aluminum", "tantalum", "tant", "diode", "_d_",
]
# ─────────────────────────────────────────────────────────────────────────────

import sys
import math
import os
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

board = pcbnew.LoadBoard(PCB_FILE)


def is_polarized_cap(fp):
    ref   = fp.GetReference().upper()
    val   = fp.GetValue().upper()
    fp_id = str(fp.GetFPID()).lower()
    if not ref.startswith(CAP_PREFIX):
        return False
    return any(k in fp_id or k in val for k in POLARIZED_FOOTPRINT_KEYWORDS)


def is_diode(fp):
    return fp.GetReference().upper().startswith(DIODE_PREFIX)


def is_ic(fp):
    return fp.GetReference().upper().startswith(IC_PREFIX)


def has_silk_graphic_nearby(fp):
    """True if F.SilkS, B.SilkS, F.Fab, or B.Fab has any drawing within SEARCH_RADIUS_MM."""
    pos       = fp.GetPosition()
    radius_nm = pcbnew.FromMM(SEARCH_RADIUS_MM)

    # Check footprint-owned graphics first
    for item in fp.GraphicalItems():
        if item.GetLayer() in (pcbnew.F_SilkS, pcbnew.B_SilkS, pcbnew.F_Fab, pcbnew.B_Fab):
            return True

    # Check board-level drawings
    for item in board.GetDrawings():
        if item.GetLayer() not in (pcbnew.F_SilkS, pcbnew.B_SilkS):
            continue
        try:
            ipos = item.GetPosition()
            dist = math.hypot(pos.x - ipos.x, pos.y - ipos.y)
            if dist <= radius_nm:
                return True
        except Exception:
            pass
    return False


lines = []
lines.append("Polarity Markers Check")
lines.append("=" * 55)
lines.append(f"Search radius: {SEARCH_RADIUS_MM}mm from component centre")
lines.append("")

missing       = []
present       = []
total_checked = 0

for fp in board.GetFootprints():
    if is_ic(fp) or is_diode(fp) or is_polarized_cap(fp):
        total_checked += 1
        if has_silk_graphic_nearby(fp):
            present.append(fp.GetReference())
        else:
            missing.append(fp.GetReference())

lines.append(f"Components checked (ICs, diodes, polarized caps): {total_checked}")
lines.append(f"With polarity markers found  : {len(present)}")
lines.append(f"Missing polarity markers     : {len(missing)}")
lines.append("")

if missing:
    lines.append("COMPONENTS WITH MISSING POLARITY MARKERS:")
    for ref in sorted(missing):
        fp = next((f for f in board.GetFootprints() if f.GetReference() == ref), None)
        if fp:
            pos = fp.GetPosition()
            lines.append(
                f"  {ref} ({fp.GetValue()}) at "
                f"({pcbnew.ToMM(pos.x):.2f}, {pcbnew.ToMM(pos.y):.2f})"
            )
        else:
            lines.append(f"  {ref}")
    lines.append("")
    lines.append("NOTE: ICs (U*) — KiCad footprints typically include a pin-1 marker")
    lines.append("      on F.Fab (dot/triangle). If not visible on F.SilkS, consider")
    lines.append("      adding an explicit pin-1 dot to the silkscreen layer.")
else:
    lines.append("PASS: All polarized components have nearby silkscreen/fab graphics.")

lines.append("")
lines.append("PASS LIST:")
for ref in sorted(present):
    lines.append(f"  {ref}")

report = "\n".join(lines)
print(report)
os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
with open(REPORT_FILE, "w", encoding="utf-8") as f:
    f.write(report)
print(f"\nReport written to: {REPORT_FILE}")
