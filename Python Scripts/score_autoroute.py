"""
score_autoroute.py — Autoroute quality scorer

Loads the board after FreeRouting SES import and evaluates three quality metrics:
  1. Via density (vias / mm²) — high density means the router struggled
  2. Long routes — any net with total routed length > LONG_ROUTE_MM
  3. Layer imbalance — any layer carrying > LAYER_IMBALANCE_PCT of all segments

Prints a PASS or FAIL verdict with details. Trigger manual rerouting or
placement optimisation if any threshold is exceeded.

Usage:
    python score_autoroute.py

Fill in the PROJECT CONFIG block before running.
"""

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Score thresholds — any exceeded → FAIL
VIA_DENSITY_THRESHOLD    = 0.15   # vias per mm² of board area
LONG_ROUTE_MM            = 200.0  # mm; net total length above this is flagged
LAYER_IMBALANCE_PCT      = 0.70   # fraction; any layer with > this share of tracks
# ─────────────────────────────────────────────────────────────────────────────

import sys
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew
from collections import defaultdict

board = pcbnew.LoadBoard(PCB_FILE)


def score_routing(board):
    """
    Evaluate autoroute quality. Returns list of issue strings.
    Empty list = PASS.
    """
    issues = []

    # 1. Via density
    via_count  = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_VIA")
    board_area = pcbnew.ToMM(board.GetBoardEdgesBoundingBox().GetArea())
    if board_area > 0:
        via_density = via_count / board_area
        if via_density > VIA_DENSITY_THRESHOLD:
            issues.append(
                f"HIGH_VIA_DENSITY: {via_density:.3f} vias/mm² "
                f"(threshold {VIA_DENSITY_THRESHOLD})"
            )

    # 2. Long routes
    net_lengths = defaultdict(float)
    for t in board.GetTracks():
        if t.GetClass() == "PCB_TRACK":
            net_lengths[t.GetNetname()] += pcbnew.ToMM(t.GetLength())
    long_routes = [n for n, l in net_lengths.items() if l > LONG_ROUTE_MM]
    if long_routes:
        issues.append(
            f"LONG_ROUTES (>{LONG_ROUTE_MM}mm total): {long_routes}"
        )

    # 3. Layer imbalance
    layer_counts = defaultdict(int)
    for t in board.GetTracks():
        if t.GetClass() == "PCB_TRACK":
            layer_counts[board.GetLayerName(t.GetLayer())] += 1
    total = sum(layer_counts.values())
    if total > 0:
        for layer, count in layer_counts.items():
            frac = count / total
            if frac > LAYER_IMBALANCE_PCT:
                issues.append(
                    f"LAYER_IMBALANCE: {layer} has {frac:.0%} of all tracks "
                    f"(threshold {LAYER_IMBALANCE_PCT:.0%})"
                )

    return issues


issues = score_routing(board)

print("=" * 60)
print("AUTOROUTE QUALITY SCORE")
print(f"Board: {PCB_FILE}")
print("=" * 60)

if issues:
    print("RESULT: FAIL — quality issues detected:")
    for issue in issues:
        print(f"  * {issue}")
    print()
    print("Recommended action: review placement (Phase 10.5b) and re-route.")
else:
    print("RESULT: PASS — all quality thresholds met.")
    print("Proceed to DRC and manual HS net routing.")

print("=" * 60)
