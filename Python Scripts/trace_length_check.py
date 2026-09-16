"""
trace_length_check.py
Baseline trace length measurement for high-speed differential pair length-match groups.
Computes intra-pair skew (P vs N) and inter-lane spread (across lanes of the same protocol).
Writes a detailed report and a meander planning file for any failing groups.

Usage: python trace_length_check.py
"""

import sys
import math
from collections import defaultdict
from datetime import date
import os

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"[PROJECT_DIR]\[PROJECT_NAME].kicad_pcb"
REPORTS_DIR     = r"[PROJECT_DIR]\Reports"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Intra-pair skew tolerance (P vs N within one diff pair)
INTRA_PAIR_TOL = 0.10   # mm

# Inter-lane skew tolerance (average pair length across lanes of the same protocol)
INTER_LANE_TOL = 5.00   # mm

# Differential pair groups: group_label -> [net_P_name, net_N_name]
# Net names must match exactly what is in your schematic/PCB (leading "/" required if present).
# Example:
# GROUPS = {
#     "USB_HS":    ["/USB_DP", "/USB_DM"],
#     "ETH_TX":    ["/ETH_TXP", "/ETH_TXN"],
#     "MIPI_D0":   ["/MIPI_D0P", "/MIPI_D0N"],
# }
GROUPS = {
    # "group_label": ["/net_P", "/net_N"],
}

# Inter-lane groups: check spread across multiple pairs of the same protocol.
# Each entry maps a set label to a list of group labels defined in GROUPS.
# Example:
# INTER_LANE_GROUPS = {
#     "MIPI_lanes": ["MIPI_D0", "MIPI_D1", "MIPI_D2", "MIPI_D3"],
# }
INTER_LANE_GROUPS = {
    # "set_label": ["group1", "group2", ...],
}
# ───────────────────────────────────────────────────────────────────────────

sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

os.makedirs(REPORTS_DIR, exist_ok=True)
REPORT_PATH  = os.path.join(REPORTS_DIR, "trace_length_report.txt")
MEANDER_PATH = os.path.join(REPORTS_DIR, "meander_plan.txt")

# ── Load board and measure ───────────────────────────────────────────────────
print("Loading board ...")
board = pcbnew.LoadBoard(PCB_FILE)

net_lengths  = defaultdict(float)
net_segments = defaultdict(list)

for track in board.GetTracks():
    if track.GetClass() == "PCB_VIA":
        continue
    net_name = str(track.GetNetname())
    start, end = track.GetStart(), track.GetEnd()
    dx = pcbnew.ToMM(end.x - start.x)
    dy = pcbnew.ToMM(end.y - start.y)
    seg_len = math.sqrt(dx * dx + dy * dy)
    net_lengths[net_name] += seg_len
    start_mm = (round(pcbnew.ToMM(start.x), 4), round(pcbnew.ToMM(start.y), 4))
    end_mm   = (round(pcbnew.ToMM(end.x),   4), round(pcbnew.ToMM(end.y),   4))
    layer    = board.GetLayerName(track.GetLayer())
    net_segments[net_name].append((seg_len, start_mm, end_mm, layer))

print(f"Measured {sum(1 for t in board.GetTracks() if t.GetClass() != 'PCB_VIA')} track segments.")

# ── Analyse groups ───────────────────────────────────────────────────────────
group_results = {}

for group_name, nets in GROUPS.items():
    lengths  = {n: net_lengths.get(n, 0.0) for n in nets}
    routed   = {n: l for n, l in lengths.items() if l > 0}
    unrouted = {n: l for n, l in lengths.items() if l == 0}

    if len(routed) == len(nets):
        status = "ROUTED"
    elif len(routed) == 0:
        status = "UNROUTED"
    else:
        status = "PARTIAL"

    min_l = min(lengths.values()) if routed else 0.0
    max_l = max(lengths.values()) if routed else 0.0
    skew  = max_l - min_l if routed else None

    if status == "ROUTED":
        pass_fail = "PASS" if skew <= INTRA_PAIR_TOL else "FAIL"
    elif status == "PARTIAL":
        pass_fail = "PARTIAL"
    else:
        pass_fail = "UNROUTED"

    deficits = {}
    if status in ("ROUTED", "PARTIAL") and skew is not None and skew > INTRA_PAIR_TOL:
        for n, l in lengths.items():
            if l < max_l and l > 0:
                deficits[n] = max_l - l

    group_results[group_name] = {
        "nets": nets, "lengths": lengths, "routed": routed, "unrouted": unrouted,
        "status": status, "skew": skew, "pass_fail": pass_fail,
        "deficits": deficits, "max_l": max_l, "min_l": min_l,
    }

# ── Inter-lane analysis ──────────────────────────────────────────────────────
inter_results = {}
for il_name, il_groups in INTER_LANE_GROUPS.items():
    pair_avgs = {}
    for g in il_groups:
        r = group_results.get(g)
        if r is None:
            continue
        if r["status"] == "ROUTED":
            pair_avgs[g] = sum(r["lengths"].values()) / len(r["nets"])
        elif r["status"] == "PARTIAL":
            vals = [l for l in r["lengths"].values() if l > 0]
            pair_avgs[g] = sum(vals) / len(vals) if vals else 0.0
        else:
            pair_avgs[g] = None

    available = {g: v for g, v in pair_avgs.items() if v is not None and v > 0}
    if len(available) >= 2:
        min_avg = min(available.values())
        max_avg = max(available.values())
        il_skew = max_avg - min_avg
        il_pass = "PASS" if il_skew <= INTER_LANE_TOL else "FAIL"
    else:
        il_skew = None
        il_pass = "INSUFFICIENT_DATA"

    inter_results[il_name] = {
        "groups": il_groups, "pair_avgs": pair_avgs,
        "skew": il_skew, "pass_fail": il_pass,
    }

# ── Build report ─────────────────────────────────────────────────────────────
section_a = {g: r for g, r in group_results.items() if r["status"] in ("ROUTED", "PARTIAL")}
section_b = {g: r for g, r in group_results.items() if r["status"] == "UNROUTED"}

n_pass    = sum(1 for r in group_results.values() if r["pass_fail"] == "PASS")
n_fail    = sum(1 for r in group_results.values() if r["pass_fail"] == "FAIL")
n_partial = sum(1 for r in group_results.values() if r["pass_fail"] == "PARTIAL")
n_unrtd   = sum(1 for r in group_results.values() if r["pass_fail"] == "UNROUTED")

today = date.today().isoformat()
lines = []
def w(s=""): lines.append(s)

w("=" * 78)
w("TRACE LENGTH BASELINE REPORT")
w("=" * 78)
w(f"Date  : {today}")
w(f"Script: trace_length_check.py")
w()
w("TOLERANCE TARGETS")
w("-" * 40)
w(f"  Intra-pair skew (P vs N)  : <= {INTRA_PAIR_TOL:.2f} mm")
w(f"  Inter-lane skew           : <= {INTER_LANE_TOL:.2f} mm")
w()

w("=" * 78)
w("SECTION A — GROUPS WITH ROUTED TRACES (measurable)")
w("=" * 78)
w()
for group_name, r in sorted(section_a.items()):
    pf = r["pass_fail"]
    w(f"  Group: {group_name}  [{r['status']}]  ->  {pf}")
    w(f"  {'Net':<25} {'Length (mm)':>12}  Note")
    w(f"  {'-'*25} {'-'*12}  {'-'*25}")
    for n in r["nets"]:
        l = r["lengths"][n]
        if l == 0:
            note = "UNROUTED"
        elif n in r["deficits"]:
            note = f"needs +{r['deficits'][n]:.3f} mm meander"
        elif l == r["max_l"] and len(r["routed"]) == len(r["nets"]):
            note = "longest (reference)"
        else:
            note = ""
        tag = "** " if l == 0 else "   "
        w(f"  {tag}{n:<25} {l:>12.3f}  {note}")
    if r["skew"] is not None:
        pf_str = "PASS" if r["skew"] <= INTRA_PAIR_TOL else "FAIL  <-- ACTION REQUIRED"
        w(f"  {'Skew (max-min):':<25} {r['skew']:>12.3f}  {pf_str}")
    else:
        w("  Skew: N/A (partial/unrouted members)")
    w()

if inter_results:
    w("=" * 78)
    w("INTER-LANE SKEW (pair average lengths)")
    w("=" * 78)
    w()
    for il_name, ir in inter_results.items():
        w(f"  Group set: {il_name}  ->  {ir['pass_fail']}")
        w(f"  {'Pair':<25} {'Avg length (mm)':>16}")
        w(f"  {'-'*25} {'-'*16}")
        for g in ir["groups"]:
            v = ir["pair_avgs"].get(g)
            if v is None or v == 0:
                w(f"  {g:<25} {'UNROUTED':>16}")
            else:
                w(f"  {g:<25} {v:>16.3f}")
        if ir["skew"] is not None:
            pf = "PASS" if ir["skew"] <= INTER_LANE_TOL else "FAIL  <-- ACTION REQUIRED"
            w(f"  {'Inter-lane skew:':<25} {ir['skew']:>16.3f}  {pf}")
        else:
            w("  Inter-lane skew: insufficient data (some lanes unrouted)")
        w()

w("=" * 78)
w("SECTION B — GROUPS WITH NO ROUTED TRACES")
w("=" * 78)
w()
if section_b:
    for group_name, r in sorted(section_b.items()):
        nets_str = ", ".join(r["nets"])
        w(f"  {group_name}: {nets_str}")
        w(f"    --> UNROUTED.  Route these nets first, then re-run this script.")
    w()
else:
    w("  (none — all groups have at least one routed trace)")
    w()

w("=" * 78)
w("SUMMARY")
w("=" * 78)
w(f"  Total groups              : {len(group_results)}")
w(f"  PASS (skew <= {INTRA_PAIR_TOL:.2f} mm)    : {n_pass}")
w(f"  FAIL (skew >  {INTRA_PAIR_TOL:.2f} mm)    : {n_fail}  <-- need meanders")
w(f"  PARTIAL (one trace unrouted) : {n_partial}")
w(f"  UNROUTED (both traces 0 mm)  : {n_unrtd}")
w()

w("=" * 78)
w("ACTION REQUIRED — FAIL GROUPS")
w("=" * 78)
w()
any_action = False
for group_name, r in sorted(section_a.items()):
    if r["pass_fail"] == "FAIL":
        any_action = True
        w(f"  [{group_name}]  skew = {r['skew']:.3f} mm")
        for n, deficit in r["deficits"].items():
            w(f"    Add +{deficit:.3f} mm meander to net {n}")
        w()
for group_name, r in sorted(section_a.items()):
    if r["pass_fail"] == "PARTIAL":
        any_action = True
        w(f"  [{group_name}]  PARTIAL — route the unrouted member first:")
        for n in r["unrouted"]:
            w(f"    Route {n} (0 mm currently)")
        w()
if not any_action:
    w("  None — all routed groups PASS.")
    w()

w("=" * 78)
w("END OF REPORT")
w("=" * 78)

report_text = "\n".join(lines)
print(report_text)
with open(REPORT_PATH, "w", encoding="utf-8") as f:
    f.write(report_text)
print(f"\n[OK] Report written to {REPORT_PATH}")

# ── Meander plan for FAIL groups where both nets are routed ──────────────────
fail_both_routed = {g: r for g, r in group_results.items()
                    if r["pass_fail"] == "FAIL" and r["status"] == "ROUTED"}

mlines = []
def mw(s=""): mlines.append(s)

mw("=" * 78)
mw("MEANDER PLAN — Planning Data for Failing Groups")
mw("=" * 78)
mw(f"Date  : {today}")
mw()
mw("For each FAIL group the longest straight segment on the SHORTER net is")
mw("identified as the candidate meander insertion point.")
mw()
mw("Recommended meander type: trombone / accordion serpentine")
mw("Amplitude target: 0.3 mm (3x trace width for 0.1mm traces)")
mw("Clearance: maintain >= 0.15 mm to adjacent copper")
mw()

if fail_both_routed:
    for group_name, r in sorted(fail_both_routed.items()):
        mw("-" * 78)
        mw(f"GROUP: {group_name}")
        mw(f"  Required additional length: +{list(r['deficits'].values())[0]:.3f} mm")
        mw(f"  Net to meander: {list(r['deficits'].keys())[0]}")
        shorter_net = list(r["deficits"].keys())[0]
        segs = net_segments.get(shorter_net, [])
        if segs:
            longest_seg = max(segs, key=lambda x: x[0])
            seg_len, seg_start, seg_end, seg_layer = longest_seg
            mw(f"  Insertion point — longest segment on {shorter_net}:")
            mw(f"    Length : {seg_len:.3f} mm")
            mw(f"    Layer  : {seg_layer}")
            mw(f"    Start  : ({seg_start[0]:.4f}, {seg_start[1]:.4f}) mm")
            mw(f"    End    : ({seg_end[0]:.4f}, {seg_end[1]:.4f}) mm")
            mw(f"  Action : Open PCB in KiCad GUI -> Interactive Router -> Meander")
            mw(f"           tool -> click on this segment -> tune to +{list(r['deficits'].values())[0]:.3f} mm")
        else:
            mw(f"  (No segments found for {shorter_net})")
        mw()
else:
    mw("No FAIL groups with both traces routed — no meander plan entries required.")
    mw()

mw("=" * 78)
mw("WORKFLOW")
mw("=" * 78)
mw()
mw("1. Open the PCB in KiCad GUI")
mw("2. For each group above:")
mw("   a. Navigate to the insertion point coordinates listed")
mw("   b. Select the segment -> right-click -> Interactive Router -> Add Meander")
mw("   c. Tune until length delta matches +X.XXX mm target")
mw("   d. Verify DRC passes for that net")
mw("3. Save PCB")
mw("4. Re-run trace_length_check.py to verify all groups PASS")
mw()
mw("=" * 78)
mw("END OF MEANDER PLAN")
mw("=" * 78)

meander_text = "\n".join(mlines)
with open(MEANDER_PATH, "w", encoding="utf-8") as f:
    f.write(meander_text)
print(f"[OK] Meander plan written to {MEANDER_PATH}")
print("\nTrace length check COMPLETE.")
