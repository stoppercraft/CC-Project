"""
analyze_spurious_vias.py
Audit tool for FANOUT_VIA_SKIP_NETS in routing_config.py.

Scans every net on the board and reports those where all pads share a single
copper layer — these nets never need a layer-transition via. For each candidate,
the script indicates whether it is already handled (in FANOUT_VIA_SKIP_NETS,
SWITCHING_LOOPS, or HS_PAIRS) or is a new addition candidate.

Also reports misconfigured skip nets: nets already in FANOUT_VIA_SKIP_NETS whose
pads span multiple copper layers, meaning a via is actually needed and the skip
entry is wrong.

Run this once when first populating FANOUT_VIA_SKIP_NETS, and re-run after any
schematic change that adds or removes nets. Apply the decision criteria in
Phase 10 Part 0.75a-ii of the design guide before adding any net to the skip list.

Usage:
    python analyze_spurious_vias.py
"""
import sys, math
sys.path.insert(0, r'E:\Claude Projects\CC Project Folder\Python Scripts')
sys.path.insert(0, 'C:/Program Files/KiCad/10.0/bin/Lib/site-packages')

import pcbnew
import routing_config as cfg
from collections import defaultdict

board = pcbnew.LoadBoard(cfg.PCB_FILE)

# ── Collect pad info per net ───────────────────────────────────────────────────
# Build this first so board net names are available for ownership set filtering.
net_pads = defaultdict(list)
for fp in board.GetFootprints():
    for pad in fp.Pads():
        net = pad.GetNetname()
        if not net or net.startswith("unconnected-"):
            continue
        net_pads[net].append({
            "ref":   fp.GetReference(),
            "pad":   pad.GetNumber(),
            "layer": pad.GetLayerName(),
            "x":     pad.GetX() / 1e6,
            "y":     pad.GetY() / 1e6,
        })

all_board_nets = set(net_pads.keys())

# ── Build pipeline ownership sets ─────────────────────────────────────────────
already_skip = set(cfg.FANOUT_VIA_SKIP_NETS)

# Collect every string value from every SWITCHING_LOOPS entry and keep those
# that match a real board net. This is key-name-agnostic: works regardless of
# what fields a project's loop dicts contain (sw_net, vin_net, enable_net, etc.).
switching_nets = set()
for loop in cfg.SWITCHING_LOOPS:
    for v in loop.values():
        if isinstance(v, str) and v in all_board_nets:
            switching_nets.add(v)

# HS_NETS is already a flat list exported by routing_config.py — use it directly
# rather than re-parsing HS_PAIRS, which avoids any dependency on HS_PAIRS structure.
hs_nets = set(cfg.HS_NETS)

# ── Find single-layer nets ─────────────────────────────────────────────────────
candidates = []
for net, pads in sorted(net_pads.items()):
    if len(pads) < 2:
        continue
    layers = set(p["layer"] for p in pads)
    if len(layers) != 1:
        continue
    max_dist = max(
        math.hypot(a["x"] - b["x"], a["y"] - b["y"])
        for i, a in enumerate(pads) for b in pads[i + 1:]
    )
    candidates.append((net, list(layers)[0], max_dist, pads))

# ── Check for misconfigured skip nets ─────────────────────────────────────────
# Nets in FANOUT_VIA_SKIP_NETS whose pads span multiple layers. These are
# misconfigured: the skip entry exempts them from via placement, but they
# actually need a via to connect pads on different layers.
misconfigured = []
for net in sorted(already_skip):
    pads = net_pads.get(net)
    if not pads:
        continue
    layers = set(p["layer"] for p in pads)
    if len(layers) > 1:
        pad_desc = ", ".join(f"{p['ref']}/{p['pad']}({p['layer']})" for p in pads)
        misconfigured.append((net, layers, pad_desc))

# ── Report ─────────────────────────────────────────────────────────────────────
new_count = sum(
    1 for net, _, _, _ in candidates
    if net not in already_skip and net not in switching_nets and net not in hs_nets
)

print(f"Nets where all pads are on a single copper layer: {len(candidates)} total")
print(f"  {new_count} new candidate(s) not yet covered by any pipeline script")
if misconfigured:
    print(f"  {len(misconfigured)} MISCONFIGURED skip net(s) — pads span multiple layers")
print()

if misconfigured:
    print("── MISCONFIGURED SKIP NETS — in FANOUT_VIA_SKIP_NETS but pads span multiple layers ──")
    print("   These nets need a via. Remove them from FANOUT_VIA_SKIP_NETS.")
    print()
    for net, layers, pad_desc in misconfigured:
        print(f"  {net}  layers={sorted(layers)}")
        print(f"    pads: {pad_desc}")
    print()

sections = [
    ("NEW CANDIDATES — not in FANOUT_VIA_SKIP_NETS, SWITCHING_LOOPS, or HS_PAIRS",
     lambda net: net not in already_skip and net not in switching_nets and net not in hs_nets),
    ("ALREADY IN FANOUT_VIA_SKIP_NETS",
     lambda net: net in already_skip),
    ("OWNED BY SWITCHING_LOOPS (route_critical.py places vias)",
     lambda net: net in switching_nets and net not in already_skip),
    ("OWNED BY HS_PAIRS (route_highspeed.py places vias)",
     lambda net: net in hs_nets and net not in already_skip),
]

for heading, predicate in sections:
    section_nets = [(net, layer, dist, pads)
                    for net, layer, dist, pads in candidates if predicate(net)]
    if not section_nets:
        continue
    print(f"── {heading} ({'–' * max(0, 74 - len(heading))})")
    for net, layer, dist, pads in section_nets:
        pad_desc = ", ".join(f"{p['ref']}/{p['pad']}" for p in pads)
        print(f"  {net}  layer={layer}  max_dist={dist:.2f}mm")
        print(f"    pads: {pad_desc}")
    print()
