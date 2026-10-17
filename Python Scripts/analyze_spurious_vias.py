"""
analyze_spurious_vias.py
Audit tool for FANOUT_VIA_SKIP_NETS in routing_config.py.

Scans every net on the board and reports those where all pads share a single
copper layer — these nets never need a layer-transition via. For each candidate,
the script indicates whether it is already handled (in FANOUT_VIA_SKIP_NETS,
SWITCHING_LOOPS, or HS_PAIRS) or is a new addition candidate.

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

# ── Build pipeline ownership sets ─────────────────────────────────────────────
already_skip = set(cfg.FANOUT_VIA_SKIP_NETS)

switching_nets = set()
for loop in cfg.SWITCHING_LOOPS:
    for key in ("sw_net", "vin_net", "out_net", "gnd_net", "bootstrap_net"):
        if loop.get(key):
            switching_nets.add(loop[key])

hs_nets = set()
for iface in cfg.HS_PAIRS.values():
    for p_net, n_net in iface.get("pairs", {}).values():
        hs_nets.add(p_net)
        hs_nets.add(n_net)

# ── Collect pad info per net ───────────────────────────────────────────────────
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

# ── Report ─────────────────────────────────────────────────────────────────────
new_count = sum(
    1 for net, _, _, _ in candidates
    if net not in already_skip and net not in switching_nets and net not in hs_nets
)

print(f"Nets where all pads are on a single copper layer: {len(candidates)} total")
print(f"  {new_count} new candidate(s) not yet covered by any pipeline script")
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
