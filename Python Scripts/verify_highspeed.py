"""
verify_highspeed.py — High-speed differential pair routing verification

Checks every diff pair in HS_PAIRS for:
  (a) All segments on the designated HS routing layer
  (b) No mid-trace vias (vias not at a pad)
  (c) Intra-pair length skew within SKEW_FAIL_MM

Also computes inter-lane length spread for each lane group and compares to limits.

Writes a report to REPORT_FILE and prints to stdout.

Usage:
    python verify_highspeed.py

Fill in the PROJECT CONFIG block before running.
"""

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
REPORT_FILE     = r"C:\path\to\project\Reports\verify_highspeed.txt"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Layer that all high-speed diff pairs must route on
HS_LAYER_NAME   = "In2.Cu"

# Intra-pair skew failure threshold (mm)
SKEW_FAIL_MM    = 0.127

# Differential pair definitions: name -> (P_net, N_net)
HS_PAIRS = {
    "PAIR_0": ("/NET0P", "/NET0N"),
    "PAIR_1": ("/NET1P", "/NET1N"),
    # add all high-speed diff pairs for your project
}

# Inter-lane spread groups: group_label -> (pair_name_list, max_spread_mm)
# Each group checks that max(pair_avg_length) - min(pair_avg_length) <= limit.
LANE_GROUPS = {
    "TX lanes (limit 5mm)":  (["PAIR_0"], 5.0),
    "RX lanes (limit 10mm)": (["PAIR_1"], 10.0),
    # define your lane groups and spread limits
}
# ─────────────────────────────────────────────────────────────────────────────

import sys
import math
import os
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

board = pcbnew.LoadBoard(PCB_FILE)


def get_net_tracks(net_name):
    return [t for t in board.GetTracks()
            if t.GetClass() in ("PCB_TRACK", "PCB_ARC") and t.GetNetname() == net_name]

def get_net_vias(net_name):
    return [t for t in board.GetTracks()
            if t.GetClass() == "PCB_VIA" and t.GetNetname() == net_name]

def net_length_mm(net_name):
    return pcbnew.ToMM(sum(t.GetLength() for t in get_net_tracks(net_name)))

def net_layers(net_name):
    return set(board.GetLayerName(t.GetLayer()) for t in get_net_tracks(net_name))

def via_mid_trace(net_name):
    """Return (x_mm, y_mm) for vias on this net that are NOT at a footprint pad."""
    pad_positions = set()
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname() == net_name:
                pad_positions.add((pad.GetPosition().x, pad.GetPosition().y))

    mid_trace = []
    for v in get_net_vias(net_name):
        pos = (v.GetX(), v.GetY())
        is_at_pad = any(
            abs(pos[0] - px) < pcbnew.FromMM(0.1) and abs(pos[1] - py) < pcbnew.FromMM(0.1)
            for px, py in pad_positions
        )
        if not is_at_pad:
            mid_trace.append((pcbnew.ToMM(v.GetX()), pcbnew.ToMM(v.GetY())))
    return mid_trace


lines = []
lines.append("=" * 70)
lines.append("HIGH-SPEED DIFF-PAIR VERIFICATION")
lines.append(f"Expected HS routing layer : {HS_LAYER_NAME}")
lines.append(f"Intra-pair skew threshold : {SKEW_FAIL_MM} mm")
lines.append("=" * 70)
lines.append("")

pair_lengths     = {}  # pair_name -> (len_P, len_N)
total_violations = 0
skew_violations  = 0

for pair_name, (net_p, net_n) in sorted(HS_PAIRS.items()):
    p_exists = board.FindNet(net_p) is not None
    n_exists = board.FindNet(net_n) is not None
    if not p_exists and not n_exists:
        lines.append(f"[SKIP] {pair_name}: nets not found ({net_p}, {net_n})")
        continue

    lines.append(f"--- {pair_name} ({net_p} / {net_n}) ---")

    # (a) Layer check
    layers_p = net_layers(net_p) if p_exists else set()
    layers_n = net_layers(net_n) if n_exists else set()
    all_layers = layers_p | layers_n
    if all_layers:
        non_hs = [l for l in all_layers if l != HS_LAYER_NAME]
        if non_hs:
            lines.append(f"  [a] WARN: segments NOT on {HS_LAYER_NAME}: {non_hs}")
            total_violations += 1
        else:
            lines.append(f"  [a] PASS: all segments on {HS_LAYER_NAME}")
    else:
        lines.append(f"  [a] INFO: no routed segments found")

    # (b) Mid-trace via check
    vias_p = via_mid_trace(net_p) if p_exists else []
    vias_n = via_mid_trace(net_n) if n_exists else []
    if vias_p or vias_n:
        lines.append(f"  [b] WARN: mid-trace vias found — P: {vias_p}, N: {vias_n}")
        total_violations += 1
    else:
        lines.append(f"  [b] PASS: no mid-trace vias")

    # (c) Intra-pair skew
    len_p = net_length_mm(net_p) if p_exists else 0.0
    len_n = net_length_mm(net_n) if n_exists else 0.0
    skew  = abs(len_p - len_n)
    pair_lengths[pair_name] = (len_p, len_n)
    skew_status = "PASS" if skew <= SKEW_FAIL_MM else "FAIL"
    if skew_status == "FAIL":
        skew_violations  += 1
        total_violations += 1
    lines.append(f"  [c] Skew: P={len_p:.4f}mm  N={len_n:.4f}mm  delta={skew:.4f}mm  [{skew_status}]")
    lines.append("")

# Inter-lane spread analysis
lines.append("=" * 70)
lines.append("INTER-LANE SPREAD ANALYSIS")
lines.append("=" * 70)

for group_label, (group_pairs, limit_mm) in LANE_GROUPS.items():
    data = [(pn, sum(pair_lengths[pn]) / 2) for pn in group_pairs if pn in pair_lengths]
    if not data:
        lines.append(f"  {group_label}: no data")
        continue
    vals   = [v for _, v in data]
    spread = max(vals) - min(vals)
    status = "PASS" if spread <= limit_mm else "FAIL"
    lines.append(f"  {group_label}: spread={spread:.4f}mm (limit={limit_mm}mm) [{status}]")
    for pn, avg in data:
        lines.append(f"    {pn}: avg pair length = {avg:.4f}mm")
    lines.append("")

lines.append("=" * 70)
lines.append(f"SUMMARY: {total_violations} violation(s) total, {skew_violations} skew FAIL(s)")
lines.append("RESULT: " + ("PASS" if total_violations == 0 else "FAIL — see warnings above"))
lines.append("=" * 70)

report = "\n".join(lines)
print(report)
os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
with open(REPORT_FILE, "w", encoding="utf-8") as f:
    f.write(report)
print(f"\nReport written to: {REPORT_FILE}")
