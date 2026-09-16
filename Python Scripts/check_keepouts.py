"""
check_keepouts.py — Pre-flight B: keepout zone coverage check

Detects rule-area (keepout) zones that cover a large fraction of the board.
A keepout covering > COVERAGE_THRESHOLD of the board area will block
FreeRouting from routing most connections.

Run this before any FreeRouting DSN export. If a keepout flags:
  - Delete it and recreate as a narrow 1–2mm band along Edge.Cuts instead.
  - Or reduce scope to the specific area that truly needs protection.

Usage:
    python check_keepouts.py

Fill in the PROJECT CONFIG block before running.
"""

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Keepout zone coverage fraction above which a warning is raised (0.0–1.0)
COVERAGE_THRESHOLD = 0.70
# ─────────────────────────────────────────────────────────────────────────────

import sys
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

board     = pcbnew.LoadBoard(PCB_FILE)
board_box = board.GetBoardEdgesBoundingBox()

bx0 = pcbnew.ToMM(board_box.GetLeft())
bx1 = pcbnew.ToMM(board_box.GetRight())
by0 = pcbnew.ToMM(board_box.GetTop())
by1 = pcbnew.ToMM(board_box.GetBottom())
board_area = (bx1 - bx0) * (by1 - by0)

flagged = 0

for zone in board.Zones():
    if not zone.GetIsRuleArea():
        continue
    zb    = zone.GetBoundingBox()
    zarea = pcbnew.ToMM(zb.GetWidth()) * pcbnew.ToMM(zb.GetHeight())
    frac  = zarea / board_area if board_area > 0 else 0.0
    layer = board.GetLayerName(zone.GetLayer())
    status = "WARNING" if frac > COVERAGE_THRESHOLD else "OK"
    print(
        f"  [{status}] Rule area on {layer}: "
        f"{zarea:.1f} mm² = {frac * 100:.0f}% of board"
    )
    if frac > COVERAGE_THRESHOLD:
        print(
            f"          This keepout will block FreeRouting. "
            f"Delete and replace with a narrow band along Edge.Cuts."
        )
        flagged += 1

if flagged == 0:
    print(
        f"PASS: No keepout zone exceeds {COVERAGE_THRESHOLD * 100:.0f}% board coverage. "
        f"Safe to proceed with DSN export."
    )
else:
    print(
        f"\nFAIL: {flagged} keepout zone(s) exceed {COVERAGE_THRESHOLD * 100:.0f}% coverage. "
        f"Fix before running FreeRouting."
    )
