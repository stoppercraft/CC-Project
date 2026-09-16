"""
width_audit.py — Power net trace width compliance checker
Usage: python width_audit.py [path/to/board.kicad_pcb]
       Defaults to the board file in the same directory as this script's project.

To reuse on another project:
  1. Change PCB_FILE (or pass path as first argument)
  2. Update POWER_NETS with your net names and minimum widths

Logic: segments whose endpoints are within ALL_PAD_PROXIMITY_MM of the edge of
ANY pad (same net or foreign) are classified as intentional neck-downs and
excluded from the audit. Only clear trunk segments are checked.
"""
import sys, os, math
sys.path.insert(0, r'C:\Program Files\KiCad\10.0\bin\Lib\site-packages')
import pcbnew

# ── Project-specific: change these two blocks for a different board ───────────

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE = sys.argv[1] if len(sys.argv) > 1 else (
    r'C:\path\to\project\ProjectName.kicad_pcb'
)
# ─────────────────────────────────────────────────────────────────────────────

# (net_name, minimum_width_mm, description)
POWER_NETS = [
    ('/VBUS',      0.5,  'USB-PD input'),
    ('/+5V',       0.8,  'buck trunk'),
    ('/+1V8',      0.5,  'LDO output'),
    ('/LED_VOUT',  0.5,  'boost output'),
    ('/-6V',       0.5,  'neg rail'),
    ('/+6V',       0.5,  'pos rail'),
    ('/VBUS_CONN', 0.5,  'display VBUS'),
    ('/VBUS_DISP', 0.5,  'display pwr'),
]

# ── Generic logic below — no changes needed for reuse ─────────────────────────

# If a segment endpoint is within this distance of the EDGE of any pad,
# widening it would risk a clearance violation → treat as neck-down, exempt.
ALL_PAD_PROXIMITY_MM = 0.7

board = pcbnew.LoadBoard(PCB_FILE)
net_info = board.GetNetInfo()

all_pads = []
for fp in board.GetFootprints():
    for pad in fp.Pads():
        p = pad.GetPosition()
        sz = pad.GetSize()
        r = pcbnew.ToMM(max(sz.x, sz.y)) / 2
        all_pads.append((pcbnew.ToMM(p.x), pcbnew.ToMM(p.y), r))

def nearest_pad_edge_dist(px, py):
    if not all_pads:
        return float('inf')
    return min(math.sqrt((px-x)**2+(py-y)**2) - r for x, y, r in all_pads)

all_tracks = list(board.GetTracks())
any_fail = False

print('%-16s  %5s  %5s  %5s  %5s  %s' % ('Net', 'Tgt', 'Pass', 'Fail', 'ND', 'Fail detail'))
print('-' * 70)

for net_name, min_w, note in POWER_NETS:
    ni = net_info.GetNetItem(net_name)
    if not ni:
        print('%-16s  N/F' % net_name)
        continue
    nc = ni.GetNetCode()
    passing = failing = nd = 0
    fd = {}
    for t in all_tracks:
        if isinstance(t, pcbnew.PCB_VIA) or not isinstance(t, pcbnew.PCB_TRACK):
            continue
        if t.GetNetCode() != nc:
            continue
        w = round(pcbnew.ToMM(t.GetWidth()), 4)
        s, e = t.GetStart(), t.GetEnd()
        sx, sy = pcbnew.ToMM(s.x), pcbnew.ToMM(s.y)
        ex, ey = pcbnew.ToMM(e.x), pcbnew.ToMM(e.y)
        if nearest_pad_edge_dist(sx, sy) <= ALL_PAD_PROXIMITY_MM or \
           nearest_pad_edge_dist(ex, ey) <= ALL_PAD_PROXIMITY_MM:
            nd += 1
        elif w >= min_w - 0.001:
            passing += 1
        else:
            failing += 1
            fd[w] = fd.get(w, 0) + 1
    det = ', '.join(str(w)+'mm x'+str(c) for w, c in sorted(fd.items())) if fd else 'OK'
    if failing:
        any_fail = True
    print('%-16s  %4.1fmm  %5d  %5d  %5d  %s' % (net_name, min_w, passing, failing, nd, det))

print()
print('OVERALL: ' + ('ISSUES REMAIN' if any_fail else 'ALL PASS'))
