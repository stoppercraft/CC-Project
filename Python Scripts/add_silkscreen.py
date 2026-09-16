"""
add_silkscreen.py — Silkscreen and fab-layer annotation

Three operations in one pass:
  A. Hide Reference and Value text for all non-connector footprints (reduces
     silkscreen clutter; connectors keep their labels).
  B. Add a human-readable label above (or below) each connector on F.Silkscreen.
  C. Add board name, revision, and compliance marks to F.Fab.

Saves the board and runs DRC to confirm no new violations.

Usage:
    python add_silkscreen.py

Fill in the PROJECT CONFIG block before running.
"""

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Connector reference prefix — footprints whose ref starts with this keep their
# silkscreen text; all others get ref+value hidden.
CONNECTOR_REF_PREFIX = "J"

# Explicit labels for known connectors.
# Key = exact reference designator, Value = silkscreen label string.
# Any connector not listed here gets an auto-generated label from its ref.
CONNECTOR_LABELS = {
    "J1": "PWR IN",
    "J2": "PWR OUT",
    "J3": "SIGNAL",
    # add more as needed
}

# Board name / revision string for F.Fab
BOARD_NAME = "MY PROJECT V1.0"

# Compliance/certification marks to place on F.Fab (e.g. "RoHS", "CE", "UL")
COMPLIANCE_MARKS = ["RoHS"]

# Offset (mm) between compliance mark and board name on F.Fab
COMPLIANCE_OFFSET_MM = 38.0

# Text geometry for silkscreen labels
SILK_TEXT_HEIGHT_MM    = 1.0
SILK_TEXT_THICK_MM     = 0.15

# Text geometry for fab layer
FAB_TEXT_HEIGHT_MM     = 1.0
FAB_TEXT_THICK_MM      = 0.15
FAB_COMP_HEIGHT_MM     = 0.7
FAB_COMP_THICK_MM      = 0.12

# Margin (mm) between label and component bounding box
LABEL_MARGIN_MM = 3.0

# kicad-cli path for optional DRC run after save
KICAD_CLI       = r"C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"
DRC_REPORT_FILE = r"C:\path\to\project\Reports\DRC_silkscreen.rpt"
# ─────────────────────────────────────────────────────────────────────────────

import sys
import re
import subprocess
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

board = pcbnew.LoadBoard(PCB_FILE)

# ── PART A: Hide non-connector refs and values ────────────────────────────────
print("Part A: Hiding non-connector footprint refs and values...")
hidden_count = 0
for fp in board.GetFootprints():
    if not fp.GetReference().startswith(CONNECTOR_REF_PREFIX):
        fp.Reference().SetVisible(False)
        fp.Value().SetVisible(False)
        hidden_count += 1
print(f"  Hidden {hidden_count} non-connector footprints")

# ── PART B: Connector labels on F.Silkscreen ──────────────────────────────────
print("Part B: Adding connector labels...")

TEXT_H  = pcbnew.FromMM(SILK_TEXT_HEIGHT_MM)
TEXT_TH = pcbnew.FromMM(SILK_TEXT_THICK_MM)

added_labels = []

for fp in board.GetFootprints():
    ref = fp.GetReference()
    if not ref.startswith(CONNECTOR_REF_PREFIX):
        continue

    if ref in CONNECTOR_LABELS:
        label = CONNECTOR_LABELS[ref]
    else:
        # Auto-generate: strip trailing digits and clean up
        label = re.sub(r'\d+$', '', ref).replace('_', ' ').strip() or ref

    pos = fp.GetPosition()
    fp_x_mm = pcbnew.ToMM(pos.x)
    fp_y_mm = pcbnew.ToMM(pos.y)

    bb = fp.GetBoundingBox()
    bb_top_mm    = pcbnew.ToMM(bb.GetTop())
    bb_bottom_mm = pcbnew.ToMM(bb.GetBottom())

    # Prefer placing label above the component
    label_x_mm = fp_x_mm
    label_y_mm = bb_top_mm - LABEL_MARGIN_MM

    # Guard against going off the top board edge
    board_bbox    = board.GetBoardEdgesBoundingBox()
    board_top_mm  = pcbnew.ToMM(board_bbox.GetTop())
    if label_y_mm < board_top_mm + 1.5:
        label_y_mm = bb_bottom_mm + LABEL_MARGIN_MM

    txt = pcbnew.PCB_TEXT(board)
    txt.SetText(label)
    txt.SetLayer(pcbnew.F_SilkS)
    txt.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(label_x_mm), pcbnew.FromMM(label_y_mm)))
    txt.SetTextSize(pcbnew.VECTOR2I(TEXT_H, TEXT_H))
    txt.SetTextThickness(TEXT_TH)
    txt.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
    board.Add(txt)
    added_labels.append(f"  {ref} -> '{label}' at ({label_x_mm:.2f}, {label_y_mm:.2f})")

print(f"  Added {len(added_labels)} connector labels:")
for s in added_labels:
    print(s)

# ── PART C: Board info on F.Fab ───────────────────────────────────────────────
print("Part C: Adding board info to F.Fab...")

bbox     = board.GetBoardEdgesBoundingBox()
bottom_y = bbox.GetBottom()
left_x   = bbox.GetLeft() + pcbnew.FromMM(5)
fab_y    = bottom_y - pcbnew.FromMM(6)

def add_fab_text(text, x, y, h_mm=FAB_TEXT_HEIGHT_MM, th_mm=FAB_TEXT_THICK_MM, align=pcbnew.GR_TEXT_H_ALIGN_LEFT):
    t = pcbnew.PCB_TEXT(board)
    t.SetText(text)
    t.SetLayer(pcbnew.F_Fab)
    t.SetPosition(pcbnew.VECTOR2I(x, y))
    t.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(h_mm), pcbnew.FromMM(h_mm)))
    t.SetTextThickness(pcbnew.FromMM(th_mm))
    t.SetHorizJustify(align)
    board.Add(t)
    return t

add_fab_text(BOARD_NAME, left_x, fab_y)
print(f"  Added '{BOARD_NAME}' on F.Fab")

offset = pcbnew.FromMM(COMPLIANCE_OFFSET_MM)
for mark in COMPLIANCE_MARKS:
    add_fab_text(mark, left_x + offset, fab_y, h_mm=FAB_COMP_HEIGHT_MM, th_mm=FAB_COMP_THICK_MM)
    offset += pcbnew.FromMM(10)
    print(f"  Added '{mark}' on F.Fab")

# Save
board.Save(PCB_FILE)
print("\nBoard saved.")

# DRC
result = subprocess.run([
    KICAD_CLI, "pcb", "drc",
    "--output", DRC_REPORT_FILE,
    "--format", "report", "--units", "mm",
    PCB_FILE,
], capture_output=True, text=True)
out = result.stdout + result.stderr
for line in out.splitlines():
    if any(k in line for k in ["violations", "Found", "Saved"]):
        print(line)
print("DRC complete.")
