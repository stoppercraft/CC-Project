"""
add_coating_notes.py — Add conformal coating notes to F.Fab

Places "NO COAT" annotations near connectors that must stay uncoated, and
a "COAT ALL OTHER AREAS" note at the board centre. Also adds a coating
specification line near the bottom edge.

Saves the board when done.

Usage:
    python add_coating_notes.py

Fill in the PROJECT CONFIG block before running.
"""

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Connectors / areas that must NOT be coated.
# List exact reference designators. A "NO COAT" text will be placed
# NO_COAT_OFFSET_MM above each connector's centre.
NO_COAT_REFS = [
    "J1",   # replace with your connector refs that need no coating
    "J2",
]

# Offset (mm) above the connector centre for the "NO COAT" label
NO_COAT_OFFSET_MM = 4.0

# Conformal coating specification text (placed near bottom edge of board)
COAT_SPEC = "Conformal coat: IPC-CC-830 approved material, 0.05-0.13mm thickness"

# Distance (mm) from board bottom edge for the coating spec line
SPEC_MARGIN_FROM_BOTTOM_MM = 3.0

# Text geometry
TEXT_HEIGHT_MM   = 0.8
TEXT_THICK_MM    = 0.12
SPEC_HEIGHT_MM   = 0.7
SPEC_THICK_MM    = 0.10
# ─────────────────────────────────────────────────────────────────────────────

import sys
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

board = pcbnew.LoadBoard(PCB_FILE)

bbox     = board.GetBoardEdgesBoundingBox()
center_x = (bbox.GetLeft() + bbox.GetRight()) // 2
center_y = (bbox.GetTop() + bbox.GetBottom()) // 2
bottom_y = bbox.GetBottom()


def add_text(text, x, y, height_mm=TEXT_HEIGHT_MM, thickness_mm=TEXT_THICK_MM,
             align=pcbnew.GR_TEXT_H_ALIGN_CENTER):
    t = pcbnew.PCB_TEXT(board)
    t.SetText(text)
    t.SetLayer(pcbnew.F_Fab)
    t.SetPosition(pcbnew.VECTOR2I(x, y))
    t.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(height_mm), pcbnew.FromMM(height_mm)))
    t.SetTextThickness(pcbnew.FromMM(thickness_mm))
    t.SetHorizJustify(align)
    board.Add(t)
    return t


# "NO COAT" near each listed connector
ref_map = {fp.GetReference(): fp for fp in board.GetFootprints()}
for ref in NO_COAT_REFS:
    fp = ref_map.get(ref)
    if fp:
        pos = fp.GetPosition()
        lbl_y = pos.y - pcbnew.FromMM(NO_COAT_OFFSET_MM)
        add_text("NO COAT", pos.x, lbl_y)
        print(
            f"Added 'NO COAT' near {ref} at "
            f"({pcbnew.ToMM(pos.x):.2f}, {pcbnew.ToMM(lbl_y):.2f})"
        )
    else:
        print(f"WARNING: {ref} not found in board — 'NO COAT' not placed")

# "COAT ALL OTHER AREAS" at board centre
add_text("COAT ALL OTHER AREAS", center_x, center_y)
print(
    f"Added 'COAT ALL OTHER AREAS' at board centre "
    f"({pcbnew.ToMM(center_x):.2f}, {pcbnew.ToMM(center_y):.2f})"
)

# Coating specification near bottom edge
spec_y = bottom_y - pcbnew.FromMM(SPEC_MARGIN_FROM_BOTTOM_MM)
add_text(COAT_SPEC, center_x, spec_y, height_mm=SPEC_HEIGHT_MM, thickness_mm=SPEC_THICK_MM)
print(f"Added coating spec near bottom of board")

board.Save(PCB_FILE)
print("\nBoard saved.")
