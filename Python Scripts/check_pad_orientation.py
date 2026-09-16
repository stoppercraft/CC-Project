"""
check_pad_orientation.py — Pre-flight A: pad orientation blockage check

When a footprint is rotated and elongated pads retain their original
orientation, they can overlap adjacent pads and cause shorting_item DRC
violations that silently freeze FreeRouting.

This script flags every elongated pad (aspect ratio > ASPECT_THRESHOLD)
whose orientation matches the footprint orientation instead of being
corrected relative to it.

Run this before any FreeRouting DSN export. Fix flagged pads with:
    pad.SetOrientation(pcbnew.EDA_ANGLE(-fp_rot, pcbnew.DEGREES_T))
then re-run DRC to confirm shorting_item count reaches 0.

Usage:
    python check_pad_orientation.py

Fill in the PROJECT CONFIG block before running.
"""

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Only check footprints with at least this many pads
MIN_PADS = 4

# Pad aspect ratio (longer / shorter side) above which orientation matters
ASPECT_THRESHOLD = 2.0

# Angular tolerance (degrees): flag if abs(pad_rot - fp_rot) % 360 < this
ANGLE_TOLERANCE_DEG = 1.0
# ─────────────────────────────────────────────────────────────────────────────

import sys
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

board  = pcbnew.LoadBoard(PCB_FILE)
issues = []

for fp in board.GetFootprints():
    fp_rot = fp.GetOrientation().AsDegrees()
    pads   = list(fp.Pads())
    if len(pads) < MIN_PADS:
        continue
    for pad in pads:
        pad_rot = pad.GetOrientation().AsDegrees()
        size    = pad.GetSize()
        longer  = max(pcbnew.ToMM(size.x), pcbnew.ToMM(size.y))
        shorter = min(pcbnew.ToMM(size.x), pcbnew.ToMM(size.y))
        aspect  = longer / shorter if shorter > 0 else 1.0
        if aspect > ASPECT_THRESHOLD and abs((pad_rot - fp_rot) % 360) < ANGLE_TOLERANCE_DEG:
            issues.append(
                f"{fp.GetReference()} pad {pad.GetNumber()}: "
                f"fp_rot={fp_rot:.0f}° pad_rot={pad_rot:.0f}° aspect={aspect:.1f}"
            )

if issues:
    print(f"FLAGGED: {len(issues)} elongated pad(s) may overlap when footprint is rotated:")
    for i in issues:
        print(f"  {i}")
    print()
    print("Fix: pad.SetOrientation(pcbnew.EDA_ANGLE(-fp_rot, pcbnew.DEGREES_T))")
    print("Then re-run DRC — shorting_item violations from these pads must reach 0.")
else:
    print(f"PASS: No pad orientation issues found. Safe to proceed with DSN export.")
