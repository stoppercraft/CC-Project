"""
add_return_vias.py — GND return via analysis for signal layer transitions

Checks each non-GND signal via and reports whether a GND via exists within
SEARCH_RADIUS_MM. On boards with solid GND planes this script operates in
REPORT-ONLY mode by default — the planes already provide the return path and
adding isolated stitching vias would cause via_dangling DRC warnings.

Set ADD_VIAS = True only if your board does NOT have continuous GND planes
and you want the script to actually insert GND vias adjacent to signal vias.

Writes a report to REPORT_FILE and prints to stdout.

Usage:
    python add_return_vias.py

Fill in the PROJECT CONFIG block before running.
"""

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
REPORT_FILE     = r"C:\path\to\project\Reports\return_vias.txt"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Radius within which a GND via must exist to satisfy the return-path rule
SEARCH_RADIUS_MM = 2.0

# Set True to insert GND vias where missing. False = report only.
# Leave False if your board has solid GND planes (planes provide return path).
ADD_VIAS = False

# GND net name (exact string as it appears in the board)
GND_NET_NAME = "GND"

# Brief description of layer stack for the report
LAYER_STACK_DESC = "e.g. F.Cu | GND (In1.Cu) | Signal (In2.Cu) | ... | B.Cu"

# kicad-cli path for optional DRC run after modification
KICAD_CLI       = r"C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"
DRC_REPORT_FILE = r"C:\path\to\project\Reports\DRC_return_vias.rpt"
# ─────────────────────────────────────────────────────────────────────────────

import sys
import math
import os
import subprocess
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

board = pcbnew.LoadBoard(PCB_FILE)

all_vias     = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
gnd_via_pos  = [(pcbnew.ToMM(v.GetX()), pcbnew.ToMM(v.GetY()))
                for v in all_vias if v.GetNetname() == GND_NET_NAME]
non_gnd_vias = [(pcbnew.ToMM(v.GetX()), pcbnew.ToMM(v.GetY()), v.GetNetname(),
                 v.TopLayer(), v.BottomLayer())
                for v in all_vias if v.GetNetname() != GND_NET_NAME]


def nearest_gnd(x, y):
    if not gnd_via_pos:
        return float("inf")
    return min(math.hypot(x - gx, y - gy) for gx, gy in gnd_via_pos)


needs_return = [(vx, vy, net, tl, bl) for vx, vy, net, tl, bl in non_gnd_vias
                if nearest_gnd(vx, vy) > SEARCH_RADIUS_MM]
has_return   = len(non_gnd_vias) - len(needs_return)

print(f"Total non-GND vias:           {len(non_gnd_vias)}")
print(f"Have GND via within {SEARCH_RADIUS_MM}mm:  {has_return}")
print(f"Need GND return via:          {len(needs_return)}")

if ADD_VIAS and needs_return:
    print("\nADD_VIAS=True — inserting GND stitching vias...")
    gnd_net = board.FindNet(GND_NET_NAME)
    if gnd_net is None:
        print(f"ERROR: Net '{GND_NET_NAME}' not found in board.")
    else:
        added = 0
        for vx, vy, net, tl, bl in needs_return:
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(vx + SEARCH_RADIUS_MM * 0.7),
                                            pcbnew.FromMM(vy)))
            via.SetNet(gnd_net)
            via.SetWidth(pcbnew.FromMM(0.8))
            via.SetDrill(pcbnew.FromMM(0.4))
            board.Add(via)
            added += 1
        board.Save(PCB_FILE)
        print(f"  Inserted {added} GND stitching vias. Board saved.")
else:
    print("\nADD_VIAS=False — report only, board not modified.")

# Build report
lines = [
    "GND Return Via Analysis",
    "=" * 55,
    "",
    f"Board: {PCB_FILE}",
    f"Layer stack: {LAYER_STACK_DESC}",
    "",
    f"Total non-GND signal vias:          {len(non_gnd_vias)}",
    f"Already near a GND via (< {SEARCH_RADIUS_MM}mm):  {has_return}",
    f"No adjacent GND via found (> {SEARCH_RADIUS_MM}mm): {len(needs_return)}",
    "",
    f"RESULT: {'VIAS ADDED' if ADD_VIAS else 'NO VIAS ADDED (report only)'}",
    "",
    "SIGNAL VIAS THAT LACK ADJACENT GND VIA:",
]
for vx, vy, net, tl, bl in needs_return[:50]:
    lines.append(f"  net={net:35s}  pos=({vx:.2f},{vy:.2f})  top={tl} bot={bl}")
if len(needs_return) > 50:
    lines.append(f"  ... and {len(needs_return) - 50} more")

os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
with open(REPORT_FILE, "w") as f:
    f.write("\n".join(lines))
print(f"\nReport written to: {REPORT_FILE}")

# Optional DRC
result = subprocess.run([
    KICAD_CLI, "pcb", "drc",
    "--output", DRC_REPORT_FILE,
    "--format", "report", "--units", "mm",
    PCB_FILE,
], capture_output=True, text=True)
for line in (result.stdout + result.stderr).splitlines():
    if any(k in line.lower() for k in ["violations", "unconnected", "found"]):
        print(line)
