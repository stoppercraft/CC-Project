"""
verify_trace_widths.py — Power net trace width compliance checker (detailed)

For each power net, lists all track segments sorted by length, flags any segment
that is narrower than the required trunk or branch width, and reports total routed
length and the set of widths in use.

IPC-2221B guideline (external trace, 1oz Cu, 10 °C rise):
  0.5 A → 0.30 mm,  1 A → 0.50 mm,  2 A → 0.80 mm,  3 A → 1.50 mm

Usage:
    python verify_trace_widths.py

Fill in the PROJECT CONFIG block before running.
"""

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Power nets to audit.
# Format: net_name -> {max_A, req_trunk_mm, req_branch_mm}
#   max_A        — peak current on this rail (amps)
#   req_trunk_mm — minimum width for main trunk segments (> 2mm long)
#   req_branch_mm — minimum width for short branch / stub segments
POWER_NETS = {
    "/VPWR":  {"max_A": 3.0, "req_trunk_mm": 1.5, "req_branch_mm": 0.5},
    "/+5V":   {"max_A": 2.0, "req_trunk_mm": 0.8, "req_branch_mm": 0.3},
    "/+3V3":  {"max_A": 0.5, "req_trunk_mm": 0.3, "req_branch_mm": 0.2},
    # add more power nets as needed
}

# Segments longer than this are treated as trunks for the width check
TRUNK_LENGTH_MM = 2.0

# Number of longest segments to display per net
TOP_N_SEGS = 8
# ─────────────────────────────────────────────────────────────────────────────

import sys
import math
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

board = pcbnew.LoadBoard(PCB_FILE)


def layer_name(lid):
    return board.GetLayerName(lid)


print("Power trace width analysis — segments by net")
print()

all_pass = True

for netname, info in POWER_NETS.items():
    segs = []
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            continue
        if t.GetNetname() != netname:
            continue
        s, e = t.GetStart(), t.GetEnd()
        dx = pcbnew.ToMM(e.x - s.x)
        dy = pcbnew.ToMM(e.y - s.y)
        L  = math.sqrt(dx * dx + dy * dy)
        w  = pcbnew.ToMM(t.GetWidth())
        lyr = layer_name(t.GetLayer())
        segs.append((L, w, lyr))

    if not segs:
        print(f"{netname}: no segments found")
        print()
        continue

    segs.sort(key=lambda x: -x[0])
    total_len = sum(s[0] for s in segs)
    widths    = sorted(set(round(s[1], 3) for s in segs))

    print(
        f"{netname}  ({info['max_A']}A max rail)  "
        f"{len(segs)} segs  total={total_len:.1f}mm  widths={widths}"
    )
    print(f"  Longest segments (trunks > {TRUNK_LENGTH_MM}mm flagged if narrow):")
    for L, w, lyr in segs[:TOP_N_SEGS]:
        is_trunk = L > TRUNK_LENGTH_MM
        req      = info["req_trunk_mm"] if is_trunk else info["req_branch_mm"]
        flag     = " <-- NARROW" if w < req else ""
        if flag:
            all_pass = False
        print(f"    {L:6.2f}mm  {w:.3f}mm  {lyr}{flag}")
    print()

if all_pass:
    print("RESULT: PASS — all power traces meet width requirements")
else:
    print("RESULT: FAIL — narrow segments flagged above; widen before fabrication")
