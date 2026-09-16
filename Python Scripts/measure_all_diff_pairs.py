"""
Measure all high-speed differential pair lengths and produce a skew adjustment chart.

For each pair: reports P/N lengths, intra-pair skew, and PASS/FAIL vs. INTRA_LIMIT_MM.
For each lane group: reports inter-lane spread vs. the group's spec limit.
Prints a skew adjustment chart (failing pairs only) with pad locators to guide
meander insertion or GUI tuning.

Configure GROUPS and INTERLANE_SETS from the HIGH-SPEED SIGNAL INVENTORY in PROJECT_PARAMS.
"""

import sys

# ── PROJECT CONFIG ─────────────────────────────────────────────────────────────
KICAD_SITE_PKGS = r"C:\Program Files\KiCad\10.0\bin\Lib\site-packages"
PCB_FILE        = r"[PROJECT_DIR]\[PROJECT_NAME].kicad_pcb"

# Intra-pair skew limit — set to protocol spec (e.g. 0.127 mm for HDMI/USB3/DP).
# Design target is typically tighter (e.g. 0.01 mm); the chart shows all pairs
# that exceed this threshold.
INTRA_LIMIT_MM = 0.127

# One entry per differential pair: (group_label, P_net_name, N_net_name)
# Populate from the HIGH-SPEED SIGNAL INVENTORY in PROJECT_PARAMS.
# Net names must match exactly as they appear in the KiCad schematic/PCB.
GROUPS = [
    # Example — replace with your project's pairs:
    # ("USB_SS_TX1", "/SS_TX1P", "/SS_TX1N"),
    # ("USB_SS_TX2", "/SS_TX2P", "/SS_TX2N"),
]

# Inter-lane spread limits per lane group.
# Each entry: (group_prefix, display_label, limit_mm)
# group_prefix must match the start of the group_label strings above.
# limit_mm comes from the protocol spec in PROJECT_PARAMS (e.g. 10 mm for HDMI TMDS,
# 5.08 mm for USB3 TX/RX, 0.45 mm for DP within a lane group).
INTERLANE_SETS = [
    # Example — replace with your project's groups:
    # ("USB_SS_TX", "USB SS TX", 5.08),
    # ("USB_SS_RX", "USB SS RX", 5.08),
]
# ── END CONFIG ─────────────────────────────────────────────────────────────────

sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

board = pcbnew.LoadBoard(PCB_FILE)


def track_length_mm(net_name):
    """Sum all track/arc segment lengths on the given net."""
    net = board.FindNet(net_name)
    if net is None:
        return None
    total = 0
    for t in board.GetTracks():
        if t.GetNetname() == net_name:
            total += t.GetLength()
    return pcbnew.ToMM(total)


def find_pad(net_name):
    """Return list of (ref, pad_num) for all pads on net."""
    pads = []
    for fp in board.GetFootprints():
        for p in fp.Pads():
            if p.GetNetname() == net_name:
                pads.append((fp.GetReference(), p.GetNumber()))
    return pads


# ── Intra-pair measurements ────────────────────────────────────────────────────
print("=" * 72)
print("HIGH-SPEED DIFFERENTIAL PAIR MEASUREMENTS")
print("=" * 72)

results = []
for grp, pnet, nnet in GROUPS:
    plen = track_length_mm(pnet)
    nlen = track_length_mm(nnet)

    if plen is None or nlen is None:
        missing = []
        if plen is None:
            missing.append(pnet)
        if nlen is None:
            missing.append(nnet)
        print(f"  {grp}: NET NOT FOUND — {', '.join(missing)}")
        results.append((grp, pnet, nnet, None, None, None, None, None))
        continue

    skew   = abs(plen - nlen)
    adj    = skew / 2.0
    longer  = pnet if plen >= nlen else nnet

    p_pads = find_pad(pnet)
    n_pads = find_pad(nnet)

    status = "FAIL" if skew > INTRA_LIMIT_MM else "OK"
    print(f"  {grp:<14} {pnet:<14} = {plen:7.3f} mm   {nnet:<14} = {nlen:7.3f} mm   "
          f"skew={skew:.3f} mm  [{status}]")

    results.append((grp, pnet, nnet, plen, nlen, skew, adj, (longer, p_pads, n_pads)))


# ── Inter-lane spread ──────────────────────────────────────────────────────────
print()
print("=" * 72)
print("INTER-LANE LENGTH SPREAD (longest lane average minus shortest lane average)")
print("=" * 72)


def inter_spread(group_prefix):
    lanes = []
    for grp, pnet, nnet, plen, nlen, *_ in results:
        if grp.startswith(group_prefix) and plen is not None and nlen is not None:
            lanes.append((plen + nlen) / 2.0)
    if not lanes:
        return None
    return max(lanes) - min(lanes)


for prefix, label, limit_mm in INTERLANE_SETS:
    sp = inter_spread(prefix)
    if sp is None:
        print(f"  {label}: no data")
        continue
    status = "OK" if sp <= limit_mm else "FAIL"
    print(f"  {label:<24} spread={sp:.3f} mm   spec ≤{limit_mm} mm  [{status}]")


# ── Skew adjustment chart ──────────────────────────────────────────────────────
print()
print("=" * 72)
print(f"DIFFERENTIAL PAIR SKEW ADJUSTMENT CHART")
print(f"(intra-pair limit: {INTRA_LIMIT_MM} mm — only failing pairs shown)")
print("=" * 72)

failing = [(r[0], r[1], r[2], r[5], r[6], r[7])
           for r in results if r[5] is not None and r[5] > INTRA_LIMIT_MM]
failing.sort(key=lambda x: x[3], reverse=True)

if not failing:
    print("  All pairs within spec — no adjustments needed.")
else:
    for grp, pnet, nnet, skew, adj, extra in failing:
        longer_net, p_pads, n_pads = extra
        p_longer = (pnet == longer_net)

        def pad_str(pad_list):
            if not pad_list:
                return "?"
            return "  →  ".join(f"{r} pad {n}" for r, n in pad_list[:2])

        p_pad_str = pad_str(p_pads)
        n_pad_str = pad_str(n_pads)

        if p_longer:
            shorten_net, shorten_pads = pnet, p_pad_str
            lengthen_net, lengthen_pads = nnet, n_pad_str
        else:
            shorten_net, shorten_pads = nnet, n_pad_str
            lengthen_net, lengthen_pads = pnet, p_pad_str

        print(f"\nGroup: {grp}")
        print(f"  {shorten_net:<10}  {shorten_pads:<55}  SHORTEN {adj:.3f} mm")
        print(f"  {lengthen_net:<10}  {lengthen_pads:<55}  lengthen {adj:.3f} mm")

print()
print("=" * 72)
print("Nets not found (net name may differ in PCB):")
for r in results:
    if r[3] is None:
        print(f"  {r[0]}: {r[1]}, {r[2]}")
print("=" * 72)
