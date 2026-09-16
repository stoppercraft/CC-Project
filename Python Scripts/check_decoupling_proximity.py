"""
check_decoupling_proximity.py  —  Phase 8 check

Design-intent check: each bypass and bulk cap is explicitly paired with the IC it was
designed to decouple in DECOUPLING_RULES. Measures connecting-pad to connecting-pad
distance on the shared net (centroid fallback when no shared net exists).

IMPORTANT: Keep DECOUPLING_RULES in sync with the schematic. If a detour adds, renames,
or removes a bypass cap, update this table as part of that detour — see DETOUR PROTOCOL
in the design guide.
"""

import sys, os, math, pathlib

KICAD_BIN = "C:/Program Files/KiCad/10.0/bin"
os.environ["PATH"] = KICAD_BIN + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, KICAD_BIN + "/Lib/site-packages")
import pcbnew

# PROJECT CONFIG — set from PROJECT_PARAMS before running
PCB_FILE    = r"[PCB_FILE]"
REPORT_FILE = r"[REPORTS_DIR]/decoupling_proximity_report.txt"

DIST_LIMIT = 7.0  # mm

# DECOUPLING_RULES — (cap_ref, ic_ref, reason)
# Copy all decoupling/bypass entries from PROXIMITY_RULES_TABLE in PROJECT_PARAMS.
# Each cap is paired with the IC it was designed to decouple.
DECOUPLING_RULES = [
    # ("C_VDD_BYP", "U1", "100nF bypass at U1 VDD pin"),
    # ("C_VIN_BULK", "U1", "input bulk cap at U1 VIN"),
]

board = pcbnew.LoadBoard(PCB_FILE)

centroids = {}
pad_map = {}
for fp in board.GetFootprints():
    ref = fp.GetReference()
    pos = fp.GetPosition()
    centroids[ref] = (pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y))
    pads = []
    for pad in fp.Pads():
        net = pad.GetNetname()
        ppos = pad.GetPosition()
        pads.append((net, pcbnew.ToMM(ppos.x), pcbnew.ToMM(ppos.y)))
    pad_map[ref] = pads


def measure_dist(ref_a, ref_b):
    """Min connecting-pad distance on shared net; centroid fallback."""
    nets_a = {}
    for net, x, y in pad_map.get(ref_a, []):
        if net:
            nets_a.setdefault(net, []).append((x, y))
    min_dist, min_net = None, None
    for net, bx, by in pad_map.get(ref_b, []):
        if net and net in nets_a:
            for ax, ay in nets_a[net]:
                d = math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)
                if min_dist is None or d < min_dist:
                    min_dist = d
                    min_net = net
    if min_dist is not None:
        return min_dist, min_net, "pad"
    ax, ay = centroids.get(ref_a, (0, 0))
    bx, by = centroids.get(ref_b, (0, 0))
    return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2), None, "ctr"


lines = [
    "Decoupling Proximity Report",
    "=" * 40, "",
    f"Check: each bypass/bulk cap to its designed IC (pad-to-pad on shared net).",
    f"Limit: {DIST_LIMIT} mm",
    "",
    f"  {'Status':<6}  {'Cap':<16}  {'IC':<6}  {'Net':<12}  {'Dist':>7}  Reason",
    "  " + "-" * 80,
]

n_pass = n_fail = n_miss = 0
fails = []

for cap_ref, ic_ref, reason in DECOUPLING_RULES:
    if cap_ref not in pad_map:
        lines.append(f"  MISS    {cap_ref:<16}  {ic_ref:<6}  {'':12}  {'':>7}  {reason} (cap not in PCB)")
        n_miss += 1
        continue
    if ic_ref not in pad_map:
        lines.append(f"  MISS    {cap_ref:<16}  {ic_ref:<6}  {'':12}  {'':>7}  {reason} (IC not in PCB)")
        n_miss += 1
        continue

    dist, net, method = measure_dist(cap_ref, ic_ref)
    net_str = net if net else "[ctr]"
    method_tag = " [ctr]" if method == "ctr" else ""
    status = "PASS" if dist <= DIST_LIMIT else "FAIL"
    if status == "PASS":
        n_pass += 1
    else:
        n_fail += 1
        fails.append((cap_ref, ic_ref, dist, net_str, reason))
    lines.append(
        f"  {status:<6}  {cap_ref:<16}  {ic_ref:<6}  {net_str:<12}  {dist:>6.2f}mm  {reason}{method_tag}"
    )

lines += [
    "",
    f"PASS: {n_pass}   FAIL: {n_fail}   MISSING: {n_miss}",
    f"Limit: {DIST_LIMIT} mm",
]

if fails:
    lines += ["", "FAIL summary:"]
    for cap_ref, ic_ref, dist, net_str, reason in sorted(fails, key=lambda x: x[2], reverse=True):
        lines.append(f"  {cap_ref} → {ic_ref} ({net_str}): {dist:.2f}mm — {reason}")

out = "\n".join(lines)
pathlib.Path(REPORT_FILE).parent.mkdir(parents=True, exist_ok=True)
pathlib.Path(REPORT_FILE).write_text(out + "\n", encoding="utf-8")
print(out)
print(f"\nReport: {REPORT_FILE}")
