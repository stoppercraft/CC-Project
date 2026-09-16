"""
measure_loops.py
Measures the convex-hull area (mm²) of switching loop nets on the front copper layer.
Flags any loop exceeding AREA_LIMIT_MM2.

Reads the .kicad_pcb file directly (no pcbnew dependency) using regex.

Usage: python measure_loops.py
"""

import re
import math
import os
import sys

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE    = r"[PROJECT_DIR]\[PROJECT_NAME].kicad_pcb"
REPORTS_DIR = r"[PROJECT_DIR]\Reports"

# Switching loop nets: map  net_number -> descriptive_label
# Find the net numbers from your .kicad_pcb (search for "(net N " in the file)
# Example:
#   LOOP_NETS = {
#       5:  "Buck_SW_NODE",
#       12: "Boost_SW_NODE",
#   }
LOOP_NETS = {
    # net_number: "label",
}

# Area threshold: loops larger than this value are flagged as FAIL
AREA_LIMIT_MM2 = 50.0
# ───────────────────────────────────────────────────────────────────────────

os.makedirs(REPORTS_DIR, exist_ok=True)
REPORT_FILE = os.path.join(REPORTS_DIR, "loop_area_report.txt")
LOG_FILE    = os.path.join(REPORTS_DIR, "loop_measure_log.txt")

report_lines = []

def log(msg):
    print(msg)
    report_lines.append(msg)


def convex_hull(points):
    """Compute convex hull using Graham scan."""
    if len(points) < 3:
        return points
    pivot = min(points, key=lambda p: (p[1], p[0]))
    def angle_key(p):
        dx = p[0] - pivot[0]
        dy = p[1] - pivot[1]
        return math.atan2(dy, dx)
    sorted_pts = sorted(points, key=angle_key)
    hull = []
    for p in sorted_pts:
        while len(hull) >= 2:
            dx1 = hull[-1][0] - hull[-2][0]
            dy1 = hull[-1][1] - hull[-2][1]
            dx2 = p[0] - hull[-2][0]
            dy2 = p[1] - hull[-2][1]
            cross = dx1 * dy2 - dy1 * dx2
            if cross <= 0:
                hull.pop()
            else:
                break
        hull.append(p)
    return hull


def shoelace_area(points):
    """Compute polygon area via shoelace formula."""
    n = len(points)
    if n < 3:
        return 0.0
    area = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def parse_segments(content):
    """Return dict: {net_num: [(x1, y1, x2, y2, layer), ...]}"""
    segs = {}
    pattern = re.compile(
        r'\(segment\s+\(start\s+([\d.\-]+)\s+([\d.\-]+)\)\s+\(end\s+([\d.\-]+)\s+([\d.\-]+)\)'
        r'\s+\(width[^)]+\)\s+\(layer\s+"([^"]+)"\)\s+\(net\s+(\d+)\)'
    )
    for m in pattern.finditer(content):
        x1, y1 = float(m.group(1)), float(m.group(2))
        x2, y2 = float(m.group(3)), float(m.group(4))
        layer  = m.group(5)
        net    = int(m.group(6))
        segs.setdefault(net, []).append((x1, y1, x2, y2, layer))
    return segs


def compute_loop_area(segments):
    """Collect all endpoints and compute convex hull area."""
    points = []
    for (x1, y1, x2, y2, layer) in segments:
        points.append((x1, y1))
        points.append((x2, y2))
    if not points:
        return 0.0
    points = list(set(points))
    hull = convex_hull(points)
    return shoelace_area(hull)


# ── Main ────────────────────────────────────────────────────────────────────
log("=== Switching Loop Area Measurement ===")
log("")

with open(PCB_FILE, 'r', encoding='utf-8') as f:
    content = f.read()

all_segs = parse_segments(content)

loop_results = {}

for net_num, net_label in LOOP_NETS.items():
    segs = all_segs.get(net_num, [])
    fcu_segs = [s for s in segs if s[4] == "F.Cu"]
    n_segs = len(fcu_segs)
    if n_segs == 0:
        log(f"  {net_label} (net {net_num}): No F.Cu segments found")
        loop_results[net_label] = (0.0, n_segs, "NO_SEGMENTS")
        continue

    area = compute_loop_area(fcu_segs)
    status = "PASS" if area <= AREA_LIMIT_MM2 else "FAIL"
    log(f"  {net_label} (net {net_num}): {n_segs} F.Cu segments, loop area = {area:.2f} mm2 -> {status}")
    loop_results[net_label] = (area, n_segs, status)

log("")
log("=== Summary ===")
log(f"Area limit: {AREA_LIMIT_MM2} mm2")
log("")

report_text = ["Switching Loop Area Report", "=" * 40, ""]

for net_label, (area, n_segs, status) in loop_results.items():
    line = f"{net_label}: {area:.2f} mm2  ({n_segs} segs) -- {status}"
    report_text.append(line)
    log(f"  {line}")

report_text.append("")
report_text.append(f"Area limit: <= {AREA_LIMIT_MM2} mm2")
report_text.append("")

all_pass = all(v[2] in ("PASS", "NO_SEGMENTS") for v in loop_results.values())
overall  = "ALL PASS" if all_pass else "FAIL - see above"
report_text.append(f"Overall: {overall}")
log(f"\nOverall: {overall}")

with open(REPORT_FILE, 'w', encoding='utf-8') as f:
    f.write('\n'.join(report_text) + '\n')
log(f"\nReport written to: {REPORT_FILE}")

with open(LOG_FILE, 'a', encoding='utf-8') as f:
    f.write("\n\n=== Loop Area Measurement Run ===\n")
    for line in report_lines:
        f.write(line + '\n')

print("\nLoop measurement complete.")
sys.exit(0)
