#!/usr/bin/env python3
"""
place_components_organized.py — Net-Aware Component Placement for KiCad 10

Places components to minimize total wirelength using a force-directed algorithm
weighted by net connectivity. Components that share nets are attracted to each
other. High-fanout nets (GND, power rails) are excluded from forces so they do
not collapse all components to one point.

Priority hierarchy (highest to lowest):
  1. Locked anchors — never moved under any circumstances
  2. Courtyard clearance — maintained throughout force-directed via periodic
     interleaved overlap resolution; anchors take precedence over force
  3. Diff-pair separation — enforced in convergence loop after clearances pass
  4. Net force attraction — gentle pull toward connected components; cannot
     override clearance constraints

Courtyard gap is component-type-aware (sliding scale):
  Passives (R, C, L, FB, D, Y): 0.025 mm — can nearly touch
  ICs / MOSFETs (U, Q, IC):     COURTYARD_GAP_IC (default 0.15 mm)
  Connectors (J, P, SOM, CN):   0.50 mm — large body, keep routing corridor
  Everything else (TP, etc.):   0.10 mm

After each overlap-resolution cycle, each component is rotated to the
axis-aligned angle (0/90/180/270°) that best orients its connected pads toward
their net partners. Rotation re-runs each convergence cycle so it reflects the
final settled positions, not the raw force-directed output.

Differential pair spacing:
  Nets whose names differ only by a recognised suffix pair (+/-, _P/_N, _DP/_DN,
  _p/_n) are treated as differential pairs. Components sharing a diff pair net
  are pushed apart to at least DIFF_PAIR_MIN_SEP mm edge-to-edge so the router
  has room to insert length-matching meander serpentines. The enforcement pass
  alternates with overlap resolution until both constraints converge.

Algorithm:
  1. Fix connectors flush to their nearest board edge (they anchor the layout)
  2. Seed all other components in a rough functional grid
  3. Pre-force overlap clearance pass — start clean before force begins
  4. Force-directed iterations: net attraction pulls connected components together;
     overlap resolution runs every FORCE_OVERLAP_INTERVAL iterations to maintain
     feasibility and prevent force from piling components into locked corridors
  5. Convergence loop (up to DIFF_PAIR_CYCLES cycles):
       a. Overlap resolution: push apart courtyard violations
       b. Rotation pass: orient each component so connected pads face partners
       c. Diff-pair separation: push apart diff-pair components to meander budget
  6. Clamp all positions within board margin
  7. Write PCB and report

Net weighting:
  weight(net) = 1 / sqrt(fanout - 1)    for fanout <=  MAX_FANOUT_FOR_FORCE
  weight(net) = 0                         for fanout >   MAX_FANOUT_FOR_FORCE
  (2-pin nets have weight 1.0 — strongest pull; weight falls off with fanout)

Usage:
  Human: edit the PROJECT CONFIG block, then run:
    "C:/Program Files/KiCad/10.0/bin/python.exe" place_components_organized.py

  Automated (Claude): pass all parameters on the command line:
    "C:/Program Files/KiCad/10.0/bin/python.exe" place_components_organized.py \
      --pcb "[PROJECT_DIR]/[PROJECT_NAME].kicad_pcb" \
      --report "[REPORTS_DIR]/placement_report.txt" \
      --live \
      --diff-pair-min-sep 6.0

  Set --dry-run first to preview without saving.
"""

import sys
import os
import math
import random
import argparse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ── KiCad 10 setup ───────────────────────────────────────────────────────────
KICAD_BIN = r"C:/Program Files/KiCad/10.0/bin"
sys.path.insert(0, os.path.join(KICAD_BIN, "Lib", "site-packages"))
os.environ["PATH"] = KICAD_BIN + os.pathsep + os.environ.get("PATH", "")
import pcbnew

# ── Shared placement rules ────────────────────────────────────────────────────
# RULES is used by IC rotation to penalise candidate rotations that would assign
# a satellite to a face blocked by a locked component (see _face_viability_score).
_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _script_dir)
try:
    from proximity_rules_config import RULES as _PROXIMITY_RULES
except ImportError:
    _PROXIMITY_RULES  = []

# ═══════════════════════════════════════════════════════════════════════════════
# PROJECT CONFIG — edit before running
# ═══════════════════════════════════════════════════════════════════════════════

PCB_FILE    = r"[PROJECT_DIR]/[PROJECT_NAME].kicad_pcb"
REPORT_FILE = None  # auto-derived from PCB_FILE if not specified via --report

DRY_RUN     = True     # True = preview only, no save
SKIP_LOCKED = True     # True = skip locked footprints entirely

# ── Courtyard gap — sliding scale by component type ───────────────────────────
# Gap between two components = max(gap_A, gap_B) so the stricter type wins.
COURTYARD_GAP_PASSIVE   = 0.025  # R, C, L, FB, D, Y — passives can nearly touch
COURTYARD_GAP_IC        = 0.15   # U, Q, IC — needs routing room on all sides
COURTYARD_GAP_CONNECTOR = 0.50   # J, P, SOM, CN — large body, keep clear
COURTYARD_GAP_DEFAULT   = 0.10   # TP, and anything else not matched above

BOARD_MARGIN   = 3.0   # keep components this far from board edge (mm)

# ── Force-directed algorithm ──────────────────────────────────────────────────
ITERATIONS             = 300   # force-directed iteration count
INITIAL_ALPHA          = 4.0   # starting step size (mm) — gentler than 8.0 to
                                # avoid overshooting into locked-component corridors
FINAL_ALPHA            = 0.05  # ending step size (annealing schedule)
FORCE_OVERLAP_INTERVAL = 25    # run a quick overlap maintenance pass every N iters

# Nets with more pins than this are excluded from attraction forces.
# Set to exclude GND, VCC, VBUS and other high-fanout rails from dominating.
MAX_FANOUT_FOR_FORCE = 12

# ── Starting positions ────────────────────────────────────────────────────────
# True  = use each component's current board position as the force-directed
#         starting point. Recommended when the board is already populated —
#         preserves prior manual placement and converges faster.
# False = seed all unlocked components in a uniform grid (blank-board workflow).
USE_CURRENT_POSITIONS = False

# ── Rotation optimisation ─────────────────────────────────────────────────────
# After each overlap-resolution cycle, rotate each component to the axis-aligned
# angle (0/90/180/270°) that best points its connected pads toward net partners.
# Re-runs every cycle so rotation reflects final settled positions.
ENABLE_ROTATION = True

# Minimum magnitude of the weighted pull vector (mm) required to commit a
# rotation. Below this threshold the pull direction is ambiguous and the
# component keeps its current/default orientation.
MIN_PULL_MAGNITUDE = 1.0

# Minimum distance of the local pad centroid from the component origin (mm)
# required to commit a rotation. Components with pads symmetrically placed
# around their center have no meaningful preferred facing direction.
MIN_PAD_OFFSET = 0.15

# ── Passive grid arrangement ─────────────────────────────────────────────────
# After force-directed convergence, each passive is assigned to the IC or
# connector it is most strongly connected to, then placed in a compact
# rectangular grid abutting that parent's courtyard. This prevents passives
# from piling on top of each other or scattering across the board.
# Only runs on force passes (not refine-only) so user-adjusted positions are
# preserved on subsequent passes.
ARRANGE_PASSIVES       = True  # enable passive grid arrangement
PASSIVE_PARENT_OFFSET  = 0.1   # mm gap between parent courtyard and passive grid edge
PASSIVE_PARENT_RATIO   = 1.5   # top-parent weight must be >= this × second-parent weight
                                # to assign; otherwise the passive is left unassigned
                                # (e.g. a series resistor between two ICs)
CLUSTER_BONUS_MM       = 30.0  # distance credit in pass-2 assignment when IC is in same cluster

# ── Overlap resolution ────────────────────────────────────────────────────────
OVERLAP_ITERATIONS  = 80    # max passes per overlap resolution cycle

# ── Differential pair spacing ─────────────────────────────────────────────────
# Components that share a detected differential pair net are pushed apart to at
# least DIFF_PAIR_MIN_SEP mm edge-to-edge. This leaves room for the router to
# insert length-matching meander serpentines between the pair endpoints.
#
# Set DIFF_PAIR_MIN_SEP to roughly: (expected meander length) / 2
# Example: 10 mm meander budget → 5 mm minimum separation.
DIFF_PAIR_MIN_SEP    = 8.0   # minimum edge-to-edge gap for diff-pair components (mm)
DIFF_PAIR_ITERATIONS = 60    # max push-apart passes per enforcement cycle
DIFF_PAIR_CYCLES     = 6     # outer cycles alternating overlap + diff-pair enforcement
# Net suffix pairs that identify a differential signal.
# Order matters: the positive suffix must come first in each tuple.
DIFF_PAIR_SUFFIXES: list = [
    ("+",   "-"),
    ("_P",  "_N"),
    ("_DP", "_DN"),
    ("_p",  "_n"),
]

# ── Connector edge snapping ───────────────────────────────────────────────────
# Connectors are placed flush to their nearest board edge and fixed in place.
# Prefixes listed here are treated as connectors.
CONNECTOR_PREFIXES = {"J", "P", "CN", "X", "SOM"}

# ── Skip-entirely prefixes ────────────────────────────────────────────────────
# Components with these prefixes are excluded entirely (fiducials, logos, bare holes).
# MH_ (mounting holes) are NOT skipped — they are loaded as fixed obstacles so
# resolve_overlaps pushes other components away from them.
SKIP_PREFIXES = {"FID", "LOGO", "H"}

# ── Anchor overrides ─────────────────────────────────────────────────────────
# Pre-position specific components before optimization. They are treated as
# fixed attractors — the optimizer will pull other components toward them
# but will not move them. Format: "REF": (x_mm, y_mm)
# Example: "SOM1": (35.0, 45.0)
ANCHORS: Dict[str, Tuple[float, float]] = {
    # "U1": (10.0, 20.0),
}

# ═══════════════════════════════════════════════════════════════════════════════


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments that override the PROJECT CONFIG block values."""
    parser = argparse.ArgumentParser(
        description="Net-aware component placement for KiCad 10.",
        add_help=True,
    )
    parser.add_argument("--pcb", metavar="PATH",
                        help="Override PCB_FILE path")
    parser.add_argument("--report", metavar="PATH",
                        help="Override REPORT_FILE path")

    dry_group = parser.add_mutually_exclusive_group()
    dry_group.add_argument("--dry-run", action="store_true", default=False,
                           help="Preview only — do not save PCB (sets DRY_RUN=True)")
    dry_group.add_argument("--live", action="store_true", default=False,
                           help="Save PCB after placement (sets DRY_RUN=False)")

    parser.add_argument("--diff-pair-min-sep", type=float, metavar="FLOAT",
                        help="Override DIFF_PAIR_MIN_SEP (mm)")
    parser.add_argument("--courtyard-gap", type=float, metavar="FLOAT",
                        help="Override COURTYARD_GAP_IC (mm). Passives always use "
                             "0.025 mm; connectors always use 0.50 mm.")
    parser.add_argument("--board-margin", type=float, metavar="FLOAT",
                        help="Override BOARD_MARGIN (mm)")
    parser.add_argument("--iterations", type=int, metavar="INT",
                        help="Override ITERATIONS (force-directed iteration count)")
    parser.add_argument("--cycles", type=int, metavar="INT",
                        help="Override DIFF_PAIR_CYCLES (outer convergence cycles)")
    parser.add_argument("--overlap-iterations", type=int, metavar="INT",
                        help="Override OVERLAP_ITERATIONS (max passes per overlap cycle)")
    parser.add_argument("--use-current-positions", action="store_true", default=False,
                        help="Override USE_CURRENT_POSITIONS to True")
    parser.add_argument("--no-rotation", action="store_true", default=False,
                        help="Override ENABLE_ROTATION to False")
    parser.add_argument("--refine-only", action="store_true", default=False,
                        help="Skip force-directed phase; run overlap + rotation + "
                             "diff-pair separation only. Use when starting from a "
                             "good existing placement.")

    parser.add_argument("--output", metavar="PATH",
                        help="Save placed PCB to this path instead of overwriting the input. "
                             "Use when the input is a scatter file you want to preserve.")

    lock_group = parser.add_mutually_exclusive_group()
    lock_group.add_argument("--skip-locked", action="store_true", default=False,
                            help="Override SKIP_LOCKED to True")
    lock_group.add_argument("--move-locked", action="store_true", default=False,
                            help="Override SKIP_LOCKED to False")

    parser.add_argument("--no-passive-arrange", action="store_true", default=False,
                        help="Disable passive grid arrangement (ARRANGE_PASSIVES=False). "
                             "Passives will remain where force-directed placed them.")

    parser.add_argument("--dump-nets", metavar="PATH",
                        help="Write each component's signal nets to PATH (use '-' for stdout) "
                             "then exit. Use to generate cluster assignments for --clusters.")
    parser.add_argument("--clusters", metavar="PATH",
                        help="JSON file with functional cluster definitions produced by Claude "
                             "from --dump-nets output. Guides component seeding and passive "
                             "parent assignment so functional groups stay together.")

    return parser.parse_args()


def mm(v: float) -> int:
    return pcbnew.FromMM(v)


def to_mm(v: int) -> float:
    return pcbnew.ToMM(v)


def courtyard_size(fp) -> Tuple[float, float]:
    """Return (width_mm, height_mm) from courtyard; fall back to pad bbox."""
    for layer in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
        try:
            cyd = fp.GetCourtyard(layer)
            bbox = cyd.BBox()
            w = to_mm(bbox.GetWidth())
            h = to_mm(bbox.GetHeight())
            if w > 0.05 and h > 0.05:
                return w, h
        except Exception:
            pass
    bbox = fp.GetBoundingBox(False, False)
    w = to_mm(bbox.GetWidth())
    h = to_mm(bbox.GetHeight())
    return max(w, 0.5), max(h, 0.5)


def pad_nets(fp) -> List[str]:
    nets = set()
    for pad in fp.Pads():
        n = pad.GetNetname()
        if n:
            nets.add(n)
    return list(nets)


def pad_net_local_positions(fp) -> Dict[str, List[Tuple[float, float]]]:
    """
    Return {net_name: [(lx, ly), ...]} where (lx, ly) is each pad's position
    in the footprint's local coordinate system (at 0° rotation).

    Uses GetFPRelativePosition() (KiCad 7+). Falls back to computing the delta
    from the footprint origin and un-rotating by the current orientation so the
    result is always in local / 0°-frame coordinates.
    """
    result: Dict[str, List[Tuple[float, float]]] = {}
    fp_pos = fp.GetPosition()
    fp_rot_deg = fp.GetOrientationDegrees()
    cos_r = math.cos(math.radians(-fp_rot_deg))
    sin_r = math.sin(math.radians(-fp_rot_deg))

    for pad in fp.Pads():
        net = pad.GetNetname()
        if not net:
            continue
        try:
            lpos = pad.GetFPRelativePosition()
            lx, ly = to_mm(lpos.x), to_mm(lpos.y)
        except AttributeError:
            # Fallback: un-rotate current board position back to 0° frame
            bpos = pad.GetPosition()
            dx = to_mm(bpos.x - fp_pos.x)
            dy = to_mm(bpos.y - fp_pos.y)
            lx = dx * cos_r - dy * sin_r
            ly = dx * sin_r + dy * cos_r
        result.setdefault(net, []).append((lx, ly))
    return result


def has_prefix(ref: str, prefixes) -> bool:
    ref_up = ref.upper()
    return any(ref_up.startswith(p.upper()) for p in prefixes)


@dataclass
class Comp:
    fp:    object
    ref:   str
    x:     float       # center position mm (updated by algorithm)
    y:     float
    w:     float       # courtyard width mm (may be swapped after rotation)
    h:     float       # courtyard height mm
    fixed: bool        # True = optimizer will not move or rotate this component
    nets:  List[str]   # net names connected to this component
    rot:   float = 0.0 # final rotation degrees (0/90/180/270)


def clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def alpha_schedule(i: int, total: int) -> float:
    """Exponential decay from INITIAL_ALPHA to FINAL_ALPHA."""
    t = i / max(total - 1, 1)
    return INITIAL_ALPHA * (FINAL_ALPHA / INITIAL_ALPHA) ** t


def nearest_edge(cx: float, cy: float,
                 bx: float, by: float, bw: float, bh: float) -> str:
    d = {
        "top"    : cy - by,
        "bottom" : (by + bh) - cy,
        "left"   : cx - bx,
        "right"  : (bx + bw) - cx,
    }
    return min(d, key=d.get)


def edge_position(comp: Comp, edge: str,
                  bx: float, by: float, bw: float, bh: float,
                  margin: float) -> Tuple[float, float]:
    """Return the snapped position for a connector on the given edge."""
    cx, cy = comp.x, comp.y
    half_w, half_h = comp.w / 2, comp.h / 2
    if edge == "top":
        return cx, by + margin + half_h
    elif edge == "bottom":
        return cx, by + bh - margin - half_h
    elif edge == "left":
        return bx + margin + half_w, cy
    else:  # right
        return bx + bw - margin - half_w, cy


def seed_positions_clustered(
    comps: List[Comp],
    clusters: Dict[str, dict],
    ax: float, ay: float, aw: float, ah: float,
) -> None:
    """
    Seed unlocked components inside their cluster's target board region.
    Components not assigned to any cluster fall back to a uniform grid at
    the board centre.

    Cluster JSON format:
      { "CLUSTER_NAME": { "anchor": "REF", "region": "top-right",
                          "members": ["REF1", "REF2", ...] } }

    Region strings (3×3 grid):
      top-left    top-center    top-right
      mid-left    mid-center    mid-right
      bottom-left bottom-center bottom-right
    """
    _REGION_FRAC = {
        "top-left":      (1/6, 1/6), "top-center":    (1/2, 1/6), "top-right":     (5/6, 1/6),
        "mid-left":      (1/6, 1/2), "mid-center":    (1/2, 1/2), "mid-right":     (5/6, 1/2),
        "bottom-left":   (1/6, 5/6), "bottom-center": (1/2, 5/6), "bottom-right":  (5/6, 5/6),
    }
    _CELL = 5.0  # mm spacing between members within a cluster seed grid

    ref_to_cluster: Dict[str, str] = {}
    for name, cdef in clusters.items():
        for ref in cdef.get("members", []):
            ref_to_cluster[ref] = name

    cluster_members: Dict[str, List[Comp]] = {name: [] for name in clusters}
    unassigned: List[Comp] = []
    for comp in comps:
        if comp.fixed:
            continue
        cname = ref_to_cluster.get(comp.ref)
        if cname and cname in cluster_members:
            cluster_members[cname].append(comp)
        else:
            unassigned.append(comp)

    def _place_grid(members: List[Comp], cx: float, cy: float) -> None:
        n = len(members)
        cols = max(1, math.ceil(math.sqrt(n)))
        for i, comp in enumerate(members):
            col = i % cols - cols // 2
            row = i // cols - cols // 2
            comp.x = clamp(cx + col * _CELL, ax + comp.w / 2, ax + aw - comp.w / 2)
            comp.y = clamp(cy + row * _CELL, ay + comp.h / 2, ay + ah - comp.h / 2)

    for name, members in cluster_members.items():
        if not members:
            continue
        region = clusters[name].get("region", "mid-center")
        fx, fy = _REGION_FRAC.get(region, (0.5, 0.5))
        _place_grid(members, ax + aw * fx, ay + ah * fy)

    if unassigned:
        _place_grid(unassigned, ax + aw / 2, ay + ah / 2)


def seed_positions(comps: List[Comp],
                   ax: float, ay: float, aw: float, ah: float,
                   bx: float, by: float, bw: float, bh: float,
                   margin: float) -> None:
    """
    Initial positions before force-directed:
      - Connectors: snap to nearest edge (fixed)
      - Anchors: use user-defined coordinates (fixed)
      - Others: spread in a grid across the usable area with slight jitter
    """
    moveable = [c for c in comps if not c.fixed]
    n = len(moveable)
    if n == 0:
        return

    cols = max(1, math.ceil(math.sqrt(n * (aw / ah))))
    rows = math.ceil(n / cols)
    dx = aw / (cols + 1)
    dy = ah / (rows + 1)

    rng = random.Random(42)  # fixed seed for reproducibility

    for idx, comp in enumerate(moveable):
        col = idx % cols
        row = idx // cols
        jitter_x = rng.uniform(-dx * 0.15, dx * 0.15)
        jitter_y = rng.uniform(-dy * 0.15, dy * 0.15)
        comp.x = ax + (col + 1) * dx + jitter_x
        comp.y = ay + (row + 1) * dy + jitter_y


def force_step(comps: List[Comp],
               net_index: Dict[str, List[Comp]],
               alpha: float,
               ax: float, ay: float, aw: float, ah: float) -> None:
    """
    One iteration of force-directed placement.
    Each non-fixed component is attracted toward the centroid of every other
    component it shares a net with, weighted inversely by net fanout.
    """
    new_positions: List[Tuple[float, float]] = []

    for comp in comps:
        if comp.fixed:
            new_positions.append((comp.x, comp.y))
            continue

        fx, fy = 0.0, 0.0
        total_weight = 0.0

        for net in comp.nets:
            others = net_index.get(net, [])
            fanout = len(others)
            if fanout < 2 or fanout > MAX_FANOUT_FOR_FORCE:
                continue

            weight = 1.0 / max(1.0, math.sqrt(fanout - 1))

            other_comps = [c for c in others if c is not comp]
            if not other_comps:
                continue
            cx = sum(c.x for c in other_comps) / len(other_comps)
            cy = sum(c.y for c in other_comps) / len(other_comps)

            fx += (cx - comp.x) * weight
            fy += (cy - comp.y) * weight
            total_weight += weight

        if total_weight > 0:
            scale = alpha / (total_weight + 1e-9)
            scale = min(scale, alpha)
            new_x = comp.x + fx * scale
            new_y = comp.y + fy * scale
        else:
            new_x, new_y = comp.x, comp.y

        new_x = clamp(new_x, ax + comp.w/2, ax + aw - comp.w/2)
        new_y = clamp(new_y, ay + comp.h/2, ay + ah - comp.h/2)
        new_positions.append((new_x, new_y))

    for comp, (nx, ny) in zip(comps, new_positions):
        comp.x, comp.y = nx, ny


def compute_optimal_rotation(comp: Comp,
                              net_index: Dict[str, List[Comp]],
                              pad_locals: Dict[str, List[Tuple[float, float]]],
                              preferred_parent: Optional[Comp] = None) -> float:
    """
    Return the axis-aligned rotation (0/90/180/270°) that best orients this
    component's connected pads toward their net partners.

    Strategy:
      1. Compute a net-weighted pull vector pointing from this component toward
         the centroids of its connected partners (same weighting as force_step).
      2. Compute a net-weighted local pad centroid: the average position of the
         pads involved in those nets, in the component's 0°-rotation frame.
         This vector tells us which side of the component does the connecting.
      3. Try all four axis-aligned rotations and pick the one that maximises
         the dot product of the rotated pad centroid with the pull vector —
         i.e., the rotation that best points the connected pads toward their
         partners.

    Returns the original rotation unchanged if:
      - The pull magnitude is below MIN_PULL_MAGNITUDE (ambiguous direction).
      - The pad centroid offset is below MIN_PAD_OFFSET (symmetric pads).
    """
    pull_x, pull_y = 0.0, 0.0
    pad_cx, pad_cy = 0.0, 0.0
    total_weight   = 0.0

    for net in comp.nets:
        others = net_index.get(net, [])
        fanout = len(others)
        if fanout < 2 or fanout > MAX_FANOUT_FOR_FORCE:
            continue

        weight = 1.0 / max(1.0, math.sqrt(fanout - 1))
        other_comps = [c for c in others if c is not comp]
        if not other_comps:
            continue

        # Pull direction: toward preferred parent if it is on this net,
        # otherwise toward the centroid of all connected partners.
        # preferred_parent is set for passives so a distant locked component
        # (e.g. SOM2) on the same net does not bias the pull vector away from
        # the passive's actual parent IC.
        if preferred_parent is not None and any(c is preferred_parent for c in other_comps):
            cx = preferred_parent.x
            cy = preferred_parent.y
        else:
            cx = sum(c.x for c in other_comps) / len(other_comps)
            cy = sum(c.y for c in other_comps) / len(other_comps)
        pull_x += (cx - comp.x) * weight
        pull_y += (cy - comp.y) * weight

        # Local pad centroid: where are the connecting pads on this component?
        pads_on_net = pad_locals.get(net, [])
        if pads_on_net:
            lx = sum(p[0] for p in pads_on_net) / len(pads_on_net)
            ly = sum(p[1] for p in pads_on_net) / len(pads_on_net)
            pad_cx += lx * weight
            pad_cy += ly * weight

        total_weight += weight

    if total_weight < 1e-9:
        return comp.rot

    if math.hypot(pull_x, pull_y) < MIN_PULL_MAGNITUDE:
        return comp.rot  # pull direction too ambiguous

    if math.hypot(pad_cx, pad_cy) < MIN_PAD_OFFSET:
        return comp.rot  # pads are near-symmetric — no preferred facing

    # Normalise pad centroid by total weight so we have a true weighted mean
    pad_cx /= total_weight
    pad_cy /= total_weight

    # Try all four axis-aligned rotations; pick the one that maximises
    # dot(rotated_pad_centroid, pull_vector).
    best_rot = comp.rot
    best_dot = -float("inf")

    for rot_deg in (0.0, 90.0, 180.0, 270.0):
        rad = math.radians(rot_deg)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        # Rotate the local pad centroid by rot_deg
        rotated_x = pad_cx * cos_r - pad_cy * sin_r
        rotated_y = pad_cx * sin_r + pad_cy * cos_r
        dot = rotated_x * pull_x + rotated_y * pull_y
        if dot > best_dot:
            best_dot = dot
            best_rot = rot_deg

    return best_rot


def rotation_pass(comps: List[Comp],
                  net_index: Dict[str, List[Comp]],
                  pad_locals_map: Dict[str, Dict[str, List[Tuple[float, float]]]]) -> int:
    """
    Apply optimal rotation to every non-fixed component.
    Returns the number of components that were rotated away from their previous angle.
    """
    rotated = 0
    for comp in comps:
        if comp.fixed:
            continue
        pad_locals = pad_locals_map.get(comp.ref, {})
        new_rot = compute_optimal_rotation(comp, net_index, pad_locals)
        if abs(new_rot - comp.rot) > 0.1:
            rotated += 1
            # Update effective courtyard size: 90° and 270° swap width/height
            if abs(new_rot % 180.0 - 90.0) < 1.0 and abs(comp.rot % 180.0 - 90.0) > 1.0:
                comp.w, comp.h = comp.h, comp.w
            elif abs(new_rot % 180.0 - 90.0) > 1.0 and abs(comp.rot % 180.0 - 90.0) < 1.0:
                comp.w, comp.h = comp.h, comp.w
        comp.rot = new_rot
    return rotated


def _face_viability_score(comp: Comp,
                          candidate_rot_deg: float,
                          pad_locals: Dict[str, List[Tuple[float, float]]],
                          comps_map: Dict[str, "Comp"],
                          proximity_rules: list) -> float:
    """
    Return a penalty (≤ 0) for candidate_rot_deg based on how many of this IC's
    satellite face-group assignments would be blocked by locked components.

    For each rule where this IC is the anchor (ref_b == comp.ref), the function
    simulates which face the satellite would be placed on at candidate_rot_deg,
    then checks whether a locked component occludes that face within the satellite's
    max_dist. A face with < 5 mm clearance scores -10; one with 5–10 mm scores a
    proportional partial penalty. Unblocked faces score 0.

    This is called by rotation_pass_ics() to break ties between rotation candidates
    that have similar net-attraction scores but different face-group viability.
    """
    if not proximity_rules:
        return 0.0

    rad = math.radians(candidate_rot_deg)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    ic_cx, ic_cy = comp.x, comp.y
    penalty = 0.0

    for ref_a, ref_b, max_dist, rtype, desc, net_hint in proximity_rules:
        if ref_b != comp.ref or not net_hint:
            continue
        pads = pad_locals.get(net_hint, [])
        if not pads:
            continue

        # Rotate the anchor's net_hint pad to world frame at candidate_rot_deg.
        lx, ly = pads[0]
        wx = lx * cos_r - ly * sin_r
        wy = lx * sin_r + ly * cos_r

        # Face direction: which face of the anchor does this pad point toward?
        if abs(wx) >= abs(wy):
            fdx, fdy = (1.0 if wx >= 0 else -1.0), 0.0
            ic_face_half = comp.w / 2.0
            ic_stk_half  = comp.h / 2.0
        else:
            fdx, fdy = 0.0, (1.0 if wy >= 0 else -1.0)
            ic_face_half = comp.h / 2.0
            ic_stk_half  = comp.w / 2.0
        stk_dx, stk_dy = -fdy, fdx  # perpendicular to face direction

        ic_face_edge  = ic_cx * fdx + ic_cy * fdy + ic_face_half
        ic_stk_center = ic_cx * stk_dx + ic_cy * stk_dy

        for other in comps_map.values():
            if not other.fixed or other.ref == comp.ref:
                continue
            # Is this locked component on the same face (beyond the IC edge)?
            other_face = other.x * fdx + other.y * fdy
            if other_face <= ic_face_edge:
                continue
            clearance = other_face - ic_face_edge
            if clearance > max_dist:
                continue
            # Is it in the stacking band (would physically block a satellite)?
            other_stk      = other.x * stk_dx + other.y * stk_dy
            other_stk_half = (other.h if abs(fdx) > abs(fdy) else other.w) / 2.0
            if abs(other_stk - ic_stk_center) > ic_stk_half + other_stk_half + 2.0:
                continue
            # Locked component blocks this face — penalise proportionally.
            if clearance < 5.0:
                penalty -= 10.0
            else:
                penalty -= 2.0 * (10.0 - clearance) / 5.0

    return penalty


def rotation_pass_ics(comps: List[Comp],
                      net_index: Dict[str, List[Comp]],
                      pad_locals_map: Dict[str, Dict[str, List[Tuple[float, float]]]],
                      forced_rotations: Optional[Dict[str, float]] = None) -> int:
    """
    Rotate only non-passive, non-fixed components (ICs, connectors, MOSFETs).
    Passives are excluded so their scattered pre-arrangement positions don't
    bias IC rotation decisions. Called during the convergence loop.

    When _PROXIMITY_RULES is available, each candidate rotation is also scored
    for face-group viability: rotations that assign a satellite to a face blocked
    by a locked component receive a penalty, steering the IC toward a rotation
    that Script 2's face-group placement can successfully execute.
    """
    comps_map = {c.ref: c for c in comps}
    _forced = forced_rotations or {}
    rotated = 0
    for comp in comps:
        if comp.fixed or _is_passive(comp.ref) or comp.ref in _forced:
            continue
        pad_locals = pad_locals_map.get(comp.ref, {})

        if not _PROXIMITY_RULES:
            new_rot = compute_optimal_rotation(comp, net_index, pad_locals)
        else:
            # Score all 4 rotations: net-attraction dot product + face viability.
            pull_x, pull_y = 0.0, 0.0
            pad_cx, pad_cy = 0.0, 0.0
            total_weight   = 0.0
            for net in comp.nets:
                others = net_index.get(net, [])
                fanout = len(others)
                if fanout < 2 or fanout > MAX_FANOUT_FOR_FORCE:
                    continue
                weight = 1.0 / max(1.0, math.sqrt(fanout - 1))
                other_comps = [c for c in others if c is not comp]
                if not other_comps:
                    continue
                cx = sum(c.x for c in other_comps) / len(other_comps)
                cy = sum(c.y for c in other_comps) / len(other_comps)
                pull_x += (cx - comp.x) * weight
                pull_y += (cy - comp.y) * weight
                pads_on_net = pad_locals.get(net, [])
                if pads_on_net:
                    lx = sum(p[0] for p in pads_on_net) / len(pads_on_net)
                    ly = sum(p[1] for p in pads_on_net) / len(pads_on_net)
                    pad_cx += lx * weight
                    pad_cy += ly * weight
                total_weight += weight

            if (total_weight < 1e-9
                    or math.hypot(pull_x, pull_y) < MIN_PULL_MAGNITUDE
                    or math.hypot(pad_cx, pad_cy) < MIN_PAD_OFFSET):
                new_rot = comp.rot
            else:
                pad_cx /= total_weight
                pad_cy /= total_weight
                best_rot, best_score = comp.rot, -float("inf")
                for rot_deg in (0.0, 90.0, 180.0, 270.0):
                    rad = math.radians(rot_deg)
                    cos_r, sin_r = math.cos(rad), math.sin(rad)
                    rx = pad_cx * cos_r - pad_cy * sin_r
                    ry = pad_cx * sin_r + pad_cy * cos_r
                    dot      = rx * pull_x + ry * pull_y
                    viab     = _face_viability_score(comp, rot_deg, pad_locals,
                                                     comps_map, _PROXIMITY_RULES)
                    score    = dot + viab
                    if score > best_score:
                        best_score, best_rot = score, rot_deg
                new_rot = best_rot

        if abs(new_rot - comp.rot) > 0.1:
            rotated += 1
            if abs(new_rot % 180.0 - 90.0) < 1.0 and abs(comp.rot % 180.0 - 90.0) > 1.0:
                comp.w, comp.h = comp.h, comp.w
            elif abs(new_rot % 180.0 - 90.0) > 1.0 and abs(comp.rot % 180.0 - 90.0) < 1.0:
                comp.w, comp.h = comp.h, comp.w
        comp.rot = new_rot
    return rotated


def rotation_pass_passives(
    comps: List[Comp],
    net_index: Dict[str, List[Comp]],
    pad_locals_map: Dict[str, Dict[str, List[Tuple[float, float]]]],
    passive_assign: Optional[Dict[int, Tuple[Comp, Comp]]] = None,
) -> int:
    """
    Rotate only passive components. Called after arrange_passives_in_grid so
    each passive is already adjacent to its parent IC; the pull vector reliably
    points toward the parent's connecting pads.

    When passive_assign is provided, each passive's pull vector is computed
    toward its assigned parent IC only (not the centroid of all net partners).
    This prevents a distant locked component on the same net (e.g. SOM2 on
    HDMI0_SCL) from biasing the rotation away from the intended parent.
    """
    rotated = 0
    for comp in comps:
        if comp.fixed or not _is_passive(comp.ref):
            continue
        pad_locals = pad_locals_map.get(comp.ref, {})
        parent = None
        if passive_assign:
            assignment = passive_assign.get(id(comp))
            if assignment:
                parent = assignment[1]
        new_rot = compute_optimal_rotation(comp, net_index, pad_locals,
                                           preferred_parent=parent)
        if abs(new_rot - comp.rot) > 0.1:
            rotated += 1
            if abs(new_rot % 180.0 - 90.0) < 1.0 and abs(comp.rot % 180.0 - 90.0) > 1.0:
                comp.w, comp.h = comp.h, comp.w
            elif abs(new_rot % 180.0 - 90.0) > 1.0 and abs(comp.rot % 180.0 - 90.0) < 1.0:
                comp.w, comp.h = comp.h, comp.w
        comp.rot = new_rot
    return rotated


def face_assign_ics(
    comps: List[Comp],
    pad_locals_map: Dict[str, Dict[str, List[Tuple[float, float]]]],
    net_index: Dict[str, List[Comp]],
    board_bounds: Optional[Tuple[float, float, float, float]] = None,
) -> int:
    """
    Snap non-passive IC-scale satellites to the correct face of their anchor.

    Works through _PROXIMITY_RULES for entries where ref_a is not a passive.
    Target face is determined by pad-centroid voting: rotate the anchor's
    net_hint pads at the anchor's current board rotation, normalise by
    half-extents, and choose the face the centroid falls in (same algorithm
    as _passive_preferred_side).

    Only moves a satellite when it is on the wrong face of its anchor
    (approach-vector check), to avoid fighting the force-directed engine.

    Returns the number of satellites repositioned.
    """
    if not _PROXIMITY_RULES:
        return 0

    comps_map = {c.ref: c for c in comps}
    moved = 0

    for ref_a, ref_b, _max_dist, _rtype, _desc, net_hint in _PROXIMITY_RULES:
        if _is_passive(ref_a):
            continue
        sat = comps_map.get(ref_a)
        anchor = comps_map.get(ref_b)
        if sat is None or anchor is None or sat.fixed or not net_hint:
            continue

        # ── Determine target face via anchor pad-centroid voting ───────────
        pad_locals = pad_locals_map.get(ref_b, {})
        pads = pad_locals.get(net_hint, [])
        if not pads:
            continue
        # KiCad's GetFPRelativePosition() uses a CW rotation convention (Y-down
        # board frame), so negate the angle to get correct world-frame offsets.
        rot_rad = math.radians(-anchor.rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
        gcx = sum(lx * cos_r - ly * sin_r for lx, ly in pads) / len(pads)
        gcy = sum(lx * sin_r + ly * cos_r for lx, ly in pads) / len(pads)
        hw = max(anchor.w / 2, 0.01)
        hh = max(anchor.h / 2, 0.01)
        norm_x = gcx / hw
        norm_y = gcy / hh
        if abs(norm_x) >= abs(norm_y):
            face = "right" if norm_x > 0 else "left"
        else:
            face = "below" if norm_y > 0 else "above"

        # ── Skip if satellite is already on the correct face ────────────────
        dx = sat.x - anchor.x
        dy = sat.y - anchor.y
        if   face == "left":  correct = dx < 0 and abs(dx) >= abs(dy)
        elif face == "right": correct = dx > 0 and abs(dx) >= abs(dy)
        elif face == "above": correct = dy < 0 and abs(dy) >= abs(dx)
        else:                 correct = dy > 0 and abs(dy) >= abs(dx)  # "below"
        if correct:
            continue

        # ── Snap to nominal position on the target face ─────────────────────
        gap = _pair_gap(ref_a, ref_b)
        if   face == "left":
            tx = anchor.x - anchor.w / 2 - gap - sat.w / 2
            ty = anchor.y
        elif face == "right":
            tx = anchor.x + anchor.w / 2 + gap + sat.w / 2
            ty = anchor.y
        elif face == "above":
            tx = anchor.x
            ty = anchor.y - anchor.h / 2 - gap - sat.h / 2
        else:  # "below"
            tx = anchor.x
            ty = anchor.y + anchor.h / 2 + gap + sat.h / 2

        # Clamp within usable board area so the engine doesn't fight board edges.
        if board_bounds is not None:
            bx0, by0, bx1, by1 = board_bounds
            tx = max(bx0 + sat.w / 2, min(bx1 - sat.w / 2, tx))
            ty = max(by0 + sat.h / 2, min(by1 - sat.h / 2, ty))

        print(f"  face_assign_ics: {ref_a} → {ref_b}  face={face}  "
              f"({sat.x:.2f},{sat.y:.2f}) → ({tx:.2f},{ty:.2f})")
        sat.x, sat.y = tx, ty
        moved += 1

    return moved


# ── Per-type courtyard gap helpers ────────────────────────────────────────────

_PASSIVE_PREFIXES    = ("C", "R", "FB", "L", "Y", "D")
_IC_PREFIXES_UPPER   = {"U", "Q", "IC"}
_CONN_PREFIXES_UPPER = {"J", "P", "CN", "X", "SOM"}

# Refs declared as non-passives in clusters.json "non_passive_overrides" list.
# Populated at runtime so D_AUX/D_TX1/D_TX2/D_USB (which have D-prefix but are
# actually redriver/mux ICs) can serve as parent ICs in passive assignment.
_NON_PASSIVE_OVERRIDE_REFS: set = set()


def _is_passive(ref: str) -> bool:
    if ref in _NON_PASSIVE_OVERRIDE_REFS:
        return False
    ref_up = ref.upper()
    return any(ref_up.startswith(p) for p in _PASSIVE_PREFIXES)


def _comp_gap(ref: str) -> float:
    """Return the minimum courtyard gap required on all sides of this component."""
    ref_up = ref.upper()
    if any(ref_up.startswith(p) for p in _CONN_PREFIXES_UPPER):
        return COURTYARD_GAP_CONNECTOR
    if any(ref_up.startswith(p) for p in _IC_PREFIXES_UPPER):
        return COURTYARD_GAP_IC
    if _is_passive(ref):
        return COURTYARD_GAP_PASSIVE
    return COURTYARD_GAP_DEFAULT


def _pair_gap(ref_a: str, ref_b: str) -> float:
    """Required gap between a pair = max of each component's individual gap."""
    return max(_comp_gap(ref_a), _comp_gap(ref_b))


def assign_passives_to_parents(
    comps: List[Comp],
    net_index: Dict[str, List[Comp]],
    ref_to_cluster: Optional[Dict[str, str]] = None,
) -> Dict[int, Tuple[Comp, Comp]]:
    """
    For each passive component, find the non-passive (IC/connector) it is most
    strongly connected to by cumulative net weight.

    A passive is assigned only when its top-ranked parent has weight >=
    PASSIVE_PARENT_RATIO × second-ranked parent. Passives connected equally
    to two different ICs (e.g. a series coupling resistor) are left unassigned
    so the convergence loop can position them freely between the two endpoints.

    Returns {id(passive): (passive_comp, parent_comp)}.
    """
    result: Dict[int, Tuple[Comp, Comp]] = {}
    non_passives = [c for c in comps if not _is_passive(c.ref)]

    # Pass 1: net-weight assignment — signal-coupled passives
    passives = [c for c in comps if _is_passive(c.ref) and not c.fixed]
    for passive in passives:
        parent_weights: Dict[int, Tuple[Comp, float]] = {}
        for net in passive.nets:
            others = net_index.get(net, [])
            fanout = len(others)
            if fanout < 2 or fanout > MAX_FANOUT_FOR_FORCE:
                continue  # exclude power rails from weight calc
            weight = 1.0 / max(1.0, math.sqrt(fanout - 1))
            for other in others:
                if other is passive or _is_passive(other.ref):
                    continue
                oid = id(other)
                prev_w = parent_weights.get(oid, (other, 0.0))[1]
                parent_weights[oid] = (other, prev_w + weight)

        if parent_weights:
            # If the passive is in a cluster, restrict candidates to same-cluster
            # parents so cluster intent overrides raw net-weight rankings.
            # Falls back to full set if no same-cluster parent shares a signal net.
            if ref_to_cluster:
                pc = ref_to_cluster.get(passive.ref)
                if pc:
                    same = {oid: (c, w) for oid, (c, w) in parent_weights.items()
                            if ref_to_cluster.get(c.ref) == pc}
                    if same:
                        parent_weights = same
            ranked = sorted(parent_weights.values(), key=lambda x: -x[1])
            top_parent, top_w = ranked[0]
            if len(ranked) == 1 or top_w >= PASSIVE_PARENT_RATIO * ranked[1][1]:
                result[id(passive)] = (passive, top_parent)

    # Pass 2: proximity assignment — decoupling caps and power-rail passives.
    # Force-directed has already pulled them physically close to their IC.
    # Scoring = distance + locked_penalty - net_bonus:
    #   locked_penalty: prefer unlocked interior ICs over board-edge connectors
    #   net_bonus: prefer ICs that share a signal net with the passive (not GND/power)
    #              over ICs that are merely closer but unrelated. This prevents a
    #              coupling cap from being assigned to a nearby IC it doesn't connect to.
    _LOCKED_PENALTY_MM = 30.0
    _NET_BONUS_MM      = 15.0  # distance credit for sharing a low-fanout signal net
    for passive in passives:
        if id(passive) in result:
            continue
        if not non_passives:
            continue

        def _score(c: Comp, _p=passive) -> float:
            dist = math.hypot(_p.x - c.x, _p.y - c.y)
            penalty = _LOCKED_PENALTY_MM if c.fixed else 0.0
            bonus = 0.0
            for net in _p.nets:
                if len(net_index.get(net, [])) > MAX_FANOUT_FOR_FORCE:
                    continue  # skip GND/power rails
                if any(other is c for other in net_index.get(net, [])):
                    bonus += _NET_BONUS_MM
                    break
            if ref_to_cluster:
                pc = ref_to_cluster.get(_p.ref)
                cc = ref_to_cluster.get(c.ref)
                if pc and pc == cc:
                    bonus += CLUSTER_BONUS_MM
            return dist + penalty - bonus

        best_parent = min(non_passives, key=_score)
        result[id(passive)] = (passive, best_parent)

    return result


def arrange_passives_in_grid(
    assignment: Dict[int, Tuple[Comp, Comp]],
    all_comps: List[Comp],
    pad_locals_map: Dict[str, Dict[str, List[Tuple[float, float]]]],
    net_index: Dict[str, List[Comp]],
    ax: float, ay: float, aw: float, ah: float,
    passive_side_overrides: Optional[Dict[str, str]] = None,
) -> int:
    """
    For each parent IC/connector, collect its assigned passives and place them
    in a compact grid abutting the parent's courtyard boundary.

    Side selection:
      1. Pad-centroid preference — use only low-fanout nets (not GND/power rails)
         so the centroid reflects the specific supply/signal pin positions, not
         the average of all GND pads scattered around the IC.
      2. Clearance scoring — all four sides are scored. The preferred side gets a
         +2 mm bonus; the side with the best combined score wins.

    Grid orientation:
      - Left / right side  → 1 column × n rows  (vertical stack alongside pins)
      - Above / below side → n columns × 1 row  (horizontal row above/below IC)
    This matches the expected layout: decoupling caps stack in a column beside
    the IC's supply pins rather than spreading in a horizontal row.

    Cell size is square (max of passive width and height) so rotated passives
    never exceed their grid cell.

    Returns the number of passives repositioned.
    """
    def _passive_preferred_side(passive: Comp, parent: Comp) -> Optional[str]:
        """Pick which face of parent this passive should sit on, based solely on
        this passive's own nets — not the combined nets of all siblings.

        Pad positions come from the 0°-frame footprint data; we rotate them by
        the parent's current board rotation so the side decision is in global
        (board) coordinates, not the unrotated local frame.
        """
        pad_locals = pad_locals_map.get(parent.ref, {})
        px_sum, py_sum, pad_count = 0.0, 0.0, 0
        for net in passive.nets:
            if len(net_index.get(net, [])) > MAX_FANOUT_FOR_FORCE:
                continue
            for lx, ly in pad_locals.get(net, []):
                px_sum += lx
                py_sum += ly
                pad_count += 1
        if pad_count == 0:
            return None
        # Local centroid in 0° frame
        pcx = px_sum / pad_count
        pcy = py_sum / pad_count
        # Rotate into global frame by the parent's current board rotation.
        # KiCad's GetFPRelativePosition() uses CW convention (Y-down board
        # frame), so negate the angle to get correct world-frame direction.
        rot_rad = math.radians(-parent.rot)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
        gcx = pcx * cos_r - pcy * sin_r
        gcy = pcx * sin_r + pcy * cos_r
        # Normalise by the parent's global half-extents (w/h already swapped for 90/270°)
        hw = max(parent.w / 2, 0.01)
        hh = max(parent.h / 2, 0.01)
        norm_x = gcx / hw
        norm_y = gcy / hh
        if abs(norm_x) >= abs(norm_y):
            return "right" if norm_x > 0 else "left"
        else:
            return "below" if norm_y > 0 else "above"

    # Group by (parent_id, preferred_side) so each face gets its own grid.
    _valid_sides = {"above", "below", "left", "right"}
    groups: Dict[Tuple[int, str], Tuple[Comp, List[Comp]]] = {}
    for passive, parent in assignment.values():
        # Direct override wins — user specifies the side for this passive ref.
        if passive_side_overrides and passive.ref in passive_side_overrides:
            override_val = passive_side_overrides[passive.ref]
            side = override_val if override_val in _valid_sides else "right"
        else:
            side = _passive_preferred_side(passive, parent) or "right"
        print(f"  SIDE  {passive.ref:20s} → {parent.ref:20s}  side={side}"
              + (" [OVERRIDE]" if passive_side_overrides and passive.ref in passive_side_overrides else ""))
        key = (id(parent), side)
        if key not in groups:
            groups[key] = (parent, [])
        groups[key][1].append(passive)

    repositioned = 0

    for (pid, preferred), (parent, passives) in groups.items():
        if not passives:
            continue

        target_ids = {id(p) for p in passives}
        n = len(passives)

        cell_dim = max(max(c.w, c.h) for c in passives) + COURTYARD_GAP_PASSIVE
        cell_w = cell_dim
        cell_h = cell_dim

        half_pw = parent.w / 2
        half_ph = parent.h / 2
        off = PASSIVE_PARENT_OFFSET

        # ── Per-side grid dimensions ──────────────────────────────────────────
        def _dims(side: str):
            if side in ("left", "right"):
                cols_s, rows_s = 1, n          # vertical column
            else:
                cols_s, rows_s = n, 1          # horizontal row
            return cols_s, rows_s, cols_s * cell_w, rows_s * cell_h

        def _center(side: str, gw: float, gh: float):
            if side == "right":
                return parent.x + half_pw + off + gw / 2, parent.y
            if side == "left":
                return parent.x - half_pw - off - gw / 2, parent.y
            if side == "below":
                return parent.x, parent.y + half_ph + off + gh / 2
            return parent.x, parent.y - half_ph - off - gh / 2  # "above"

        side_order = ["right", "left", "below", "above"]
        if preferred and preferred in side_order:
            side_order = [preferred] + [s for s in side_order if s != preferred]

        _PREFERRED_BONUS = 100.0
        best_score  = -float("inf")
        winning_side = side_order[0]

        for side in side_order:
            _, _, gw_s, gh_s = _dims(side)
            cx, cy = _center(side, gw_s, gh_s)

            if not (cx - gw_s / 2 >= ax and cx + gw_s / 2 <= ax + aw and
                    cy - gh_s / 2 >= ay and cy + gh_s / 2 <= ay + ah):
                continue

            min_clr = float("inf")
            for comp in all_comps:
                if id(comp) == id(parent) or id(comp) in target_ids:
                    continue
                sep_x = abs(comp.x - cx) - (comp.w + gw_s) / 2
                sep_y = abs(comp.y - cy) - (comp.h + gh_s) / 2
                min_clr = min(min_clr, max(sep_x, sep_y))

            score = min_clr + (_PREFERRED_BONUS if side == preferred else 0.0)
            if score > best_score:
                best_score   = score
                winning_side = side

        cols, rows, grid_w, grid_h = _dims(winning_side)
        grid_cx, grid_cy = _center(winning_side, grid_w, grid_h)

        if best_score == -float("inf"):
            fb_side = preferred if preferred else "right"
            _, _, gw_fb, gh_fb = _dims(fb_side)
            cx_fb, cy_fb = _center(fb_side, gw_fb, gh_fb)
            cols, rows, grid_w, grid_h = _dims(fb_side)
            grid_cx = clamp(cx_fb, ax + grid_w / 2, ax + aw - grid_w / 2)
            grid_cy = clamp(cy_fb, ay + grid_h / 2, ay + ah - grid_h / 2)

        origin_x = grid_cx - (cols - 1) * cell_w / 2
        origin_y = grid_cy - (rows - 1) * cell_h / 2

        for idx, passive in enumerate(passives):
            col = idx % cols
            row = idx // cols
            passive.x = clamp(origin_x + col * cell_w,
                               ax + passive.w / 2, ax + aw - passive.w / 2)
            passive.y = clamp(origin_y + row * cell_h,
                               ay + passive.h / 2, ay + ah - passive.h / 2)
            repositioned += 1

    return repositioned


# ─────────────────────────────────────────────────────────────────────────────


def _min_clr(comp: Comp, comps: List[Comp]) -> float:
    """
    Minimum signed clearance between comp and all other components.
    Negative means overlap. Accounts for per-pair courtyard gap requirement.
    """
    min_clr = float("inf")
    for other in comps:
        if other is comp:
            continue
        required = _pair_gap(comp.ref, other.ref)
        sep_x = abs(other.x - comp.x) - (other.w + comp.w) / 2
        sep_y = abs(other.y - comp.y) - (other.h + comp.h) / 2
        min_clr = min(min_clr, max(sep_x, sep_y) - required)
    return min_clr


def escape_trapped(
    comps: List[Comp],
    stuck_refs: set,
    ax: float, ay: float, aw: float, ah: float,
) -> int:
    """
    For each moveable component that remained in courtyard violation after a
    full cycle, try 24 candidate positions (8 directions × 3 radii: 10, 15,
    20 mm). Move to the candidate with the highest signed clearance from all
    other components. Subsequent overlap passes in the next cycle clean up the
    landing zone.

    This escapes local minima where a component is trapped between two locked
    components — a situation that micro-nudge overlap resolution cannot resolve.

    Returns the number of components moved.
    """
    moved = 0
    for comp in comps:
        if comp.fixed or comp.ref not in stuck_refs:
            continue
        current_clr = _min_clr(comp, comps)
        best_pos  = None
        best_clr  = current_clr
        for radius in (10.0, 15.0, 20.0):
            for angle_deg in range(0, 360, 45):
                nx = comp.x + radius * math.cos(math.radians(angle_deg))
                ny = comp.y + radius * math.sin(math.radians(angle_deg))
                if not (ax + comp.w / 2 <= nx <= ax + aw - comp.w / 2 and
                        ay + comp.h / 2 <= ny <= ay + ah - comp.h / 2):
                    continue
                orig_x, orig_y = comp.x, comp.y
                comp.x, comp.y = nx, ny
                clr = _min_clr(comp, comps)
                comp.x, comp.y = orig_x, orig_y
                if clr > best_clr:
                    best_clr = clr
                    best_pos = (nx, ny)
        if best_pos:
            comp.x, comp.y = best_pos
            moved += 1
    return moved


def resolve_overlaps(comps: List[Comp],
                     ax: float, ay: float, aw: float, ah: float,
                     max_iters: int) -> int:
    """
    Iteratively push apart overlapping components using per-pair courtyard gaps.
    Fixed components can push others but are not moved themselves.
    Returns number of passes used.
    """
    for iteration in range(max_iters):
        moved = False
        for i, a in enumerate(comps):
            for j in range(i + 1, len(comps)):
                b = comps[j]
                if a.fixed and b.fixed:
                    continue

                gap = _pair_gap(a.ref, b.ref)
                min_sep_x = (a.w + b.w) / 2 + gap
                min_sep_y = (a.h + b.h) / 2 + gap
                dx = a.x - b.x
                dy = a.y - b.y

                if abs(dx) >= min_sep_x or abs(dy) >= min_sep_y:
                    continue

                ovlp_x = min_sep_x - abs(dx)
                ovlp_y = min_sep_y - abs(dy)
                # When one component is fixed, the movable one must carry the full push.
                # Splitting by /2 causes geometric convergence that can stall when a movable
                # component is sandwiched between two fixed obstacles.
                one_fixed = a.fixed != b.fixed
                push    = min(ovlp_x, ovlp_y) + 0.01 if one_fixed else min(ovlp_x, ovlp_y) / 2 + 0.01

                if ovlp_x <= ovlp_y:
                    push_x = push * (1 if dx >= 0 else -1)
                    if not a.fixed:
                        a.x = clamp(a.x + push_x, ax + a.w/2, ax + aw - a.w/2)
                    if not b.fixed:
                        b.x = clamp(b.x - push_x, ax + b.w/2, ax + aw - b.w/2)
                else:
                    push_y = push * (1 if dy >= 0 else -1)
                    if not a.fixed:
                        a.y = clamp(a.y + push_y, ay + a.h/2, ay + ah - a.h/2)
                    if not b.fixed:
                        b.y = clamp(b.y - push_y, ay + b.h/2, ay + ah - b.h/2)

                moved = True

        if not moved:
            return iteration + 1

    return max_iters


def check_courtyard_clearances(comps: List[Comp]) -> Tuple[
        List[Tuple[str, str, float]], List[Tuple[str, str, float]]]:
    """
    After overlap resolution, report component pairs whose courtyard bounding
    boxes are still closer than the per-pair required gap.

    Skips locked-locked pairs: both components are fixed so the script cannot
    resolve them — they are reported separately as user-placement issues.

    Returns (resolvable_violations, locked_locked_violations).
    Each entry is (ref_a, ref_b, actual_gap_mm), sorted worst-first.
    """
    resolvable:    List[Tuple[str, str, float]] = []
    locked_locked: List[Tuple[str, str, float]] = []
    for i, a in enumerate(comps):
        for j in range(i + 1, len(comps)):
            b = comps[j]
            sep_x = abs(a.x - b.x) - (a.w + b.w) / 2
            sep_y = abs(a.y - b.y) - (a.h + b.h) / 2
            actual_gap = max(sep_x, sep_y)
            threshold  = _pair_gap(a.ref, b.ref)
            if actual_gap < threshold:
                if a.fixed and b.fixed:
                    locked_locked.append((a.ref, b.ref, actual_gap))
                else:
                    resolvable.append((a.ref, b.ref, actual_gap))
    resolvable.sort(key=lambda v: v[2])
    locked_locked.sort(key=lambda v: v[2])
    return resolvable, locked_locked


def find_diff_pairs(net_index: Dict[str, List["Comp"]],
                    suffixes: list) -> List[Tuple[str, str]]:
    """
    Scan net names for differential pair partners using the configured suffix
    pairs. Returns a list of (net_pos, net_neg) tuples — each pair appears once.
    """
    net_names = set(net_index.keys())
    pairs: List[Tuple[str, str]] = []
    seen: set = set()
    for net in sorted(net_names):
        for pos_suf, neg_suf in suffixes:
            if net.endswith(pos_suf):
                base    = net[: len(net) - len(pos_suf)]
                partner = base + neg_suf
                if partner in net_names:
                    key = (net, partner)
                    if key not in seen:
                        pairs.append(key)
                        seen.add(key)
                        seen.add((partner, net))
    return pairs


def get_diff_pair_comp_pairs(
    diff_pairs: List[Tuple[str, str]],
    net_index:  Dict[str, List["Comp"]],
    passive_assign: Optional[Dict[int, Tuple["Comp", "Comp"]]] = None,
) -> List[Tuple["Comp", "Comp"]]:
    """
    For every detected differential pair, collect the unique component pairs
    that need the minimum meander-spacing gap enforced between them.

    Excluded pairs (not enforced):
      - Passive ↔ passive: series caps / ESD diodes belong adjacent to each other.
      - Passive ↔ its own parent IC: a cap assigned to an IC's passive grid is
        intentionally placed adjacent to that IC. Enforcing meander spacing between
        them would push the cap away from the IC it decouples, which is incorrect.
        Only applies when passive_assign is provided (force pass with arrangement).
    """
    seen_ids: set = set()
    result:   List[Tuple["Comp", "Comp"]] = []

    # Build reverse lookup: passive_id → parent_id (for assigned passives only)
    assigned_parent: Dict[int, int] = {}
    if passive_assign:
        for passive, parent in passive_assign.values():
            assigned_parent[id(passive)] = id(parent)

    for net_p, net_n in diff_pairs:
        comps_on_pair: Dict[int, "Comp"] = {}
        for net in (net_p, net_n):
            for c in net_index.get(net, []):
                comps_on_pair[id(c)] = c
        members = list(comps_on_pair.values())
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if _is_passive(a.ref) and _is_passive(b.ref):
                    continue  # both passives — skip
                # Skip if a passive is assigned to the IC it is paired with
                if _is_passive(a.ref) and not _is_passive(b.ref):
                    if assigned_parent.get(id(a)) == id(b):
                        continue  # a is in b's grid
                if _is_passive(b.ref) and not _is_passive(a.ref):
                    if assigned_parent.get(id(b)) == id(a):
                        continue  # b is in a's grid
                key = (min(id(a), id(b)), max(id(a), id(b)))
                if key not in seen_ids:
                    seen_ids.add(key)
                    result.append((a, b))
    return result


def enforce_diff_pair_separation(
    dp_comp_pairs: List[Tuple["Comp", "Comp"]],
    min_sep: float,
    ax: float, ay: float, aw: float, ah: float,
    max_iters: int,
) -> Tuple[int, Tuple[List[Tuple[str, str, float]], List[Tuple[str, str, float]]]]:
    """
    Iteratively push diff-pair component pairs apart until every pair has at
    least min_sep mm of edge-to-edge clearance (or max_iters is exhausted).

    Uses the same axis-of-minimum-overlap push logic as resolve_overlaps so the
    two passes are compatible and don't fight each other.

    Returns (passes_used, (resolvable_violations, locked_locked_violations)).
    """
    for iteration in range(max_iters):
        any_moved = False
        for a, b in dp_comp_pairs:
            if a.fixed and b.fixed:
                continue

            min_cx = (a.w + b.w) / 2 + min_sep   # required centre-to-centre X
            min_cy = (a.h + b.h) / 2 + min_sep   # required centre-to-centre Y
            dx = a.x - b.x
            dy = a.y - b.y

            if abs(dx) >= min_cx and abs(dy) >= min_cy:
                continue  # already clear in at least one axis — no violation

            ovlp_x = min_cx - abs(dx)
            ovlp_y = min_cy - abs(dy)

            # If both axes are short, push along whichever needs less movement.
            # This mirrors resolve_overlaps behaviour so passes compose cleanly.
            if ovlp_x <= ovlp_y:
                push = ovlp_x / 2 + 0.01
                sign = 1 if dx >= 0 else -1
                if a.fixed:
                    b.x = clamp(b.x - sign * push * 2, ax + b.w/2, ax + aw - b.w/2)
                elif b.fixed:
                    a.x = clamp(a.x + sign * push * 2, ax + a.w/2, ax + aw - a.w/2)
                else:
                    a.x = clamp(a.x + sign * push, ax + a.w/2, ax + aw - a.w/2)
                    b.x = clamp(b.x - sign * push, ax + b.w/2, ax + aw - b.w/2)
            else:
                push = ovlp_y / 2 + 0.01
                sign = 1 if dy >= 0 else -1
                if a.fixed:
                    b.y = clamp(b.y - sign * push * 2, ay + b.h/2, ay + ah - b.h/2)
                elif b.fixed:
                    a.y = clamp(a.y + sign * push * 2, ay + a.h/2, ay + ah - a.h/2)
                else:
                    a.y = clamp(a.y + sign * push, ay + a.h/2, ay + ah - a.h/2)
                    b.y = clamp(b.y - sign * push, ay + b.h/2, ay + ah - b.h/2)

            any_moved = True

        if not any_moved:
            return iteration + 1, check_diff_pair_clearances(dp_comp_pairs, min_sep)

    return max_iters, check_diff_pair_clearances(dp_comp_pairs, min_sep)


def check_diff_pair_clearances(
    dp_comp_pairs: List[Tuple["Comp", "Comp"]],
    min_sep: float,
) -> Tuple[List[Tuple[str, str, float]], List[Tuple[str, str, float]]]:
    """
    Return (resolvable, locked_locked) for diff-pair component pairs whose
    edge-to-edge clearance is still below min_sep. Sorted worst-first.
    Locked-locked pairs are reported separately — the script cannot move them.
    """
    resolvable:    List[Tuple[str, str, float]] = []
    locked_locked: List[Tuple[str, str, float]] = []
    for a, b in dp_comp_pairs:
        sep_x  = abs(a.x - b.x) - (a.w + b.w) / 2
        sep_y  = abs(a.y - b.y) - (a.h + b.h) / 2
        actual = max(sep_x, sep_y)
        if actual < min_sep:
            if a.fixed and b.fixed:
                locked_locked.append((a.ref, b.ref, actual))
            else:
                resolvable.append((a.ref, b.ref, actual))
    resolvable.sort(key=lambda v: v[2])
    locked_locked.sort(key=lambda v: v[2])
    return resolvable, locked_locked


def total_wirelength(comps: List[Comp],
                     net_index: Dict[str, List[Comp]]) -> float:
    """
    Estimate total wirelength as the sum of half-perimeter bounding box
    for each net (HPWL — standard placement quality metric).
    """
    total = 0.0
    for net, members in net_index.items():
        if len(members) < 2:
            continue
        xs = [c.x for c in members]
        ys = [c.y for c in members]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def main():
    args = parse_args()
    global PCB_FILE, REPORT_FILE, DRY_RUN, DIFF_PAIR_MIN_SEP
    global COURTYARD_GAP_IC, BOARD_MARGIN, ITERATIONS, DIFF_PAIR_CYCLES
    global OVERLAP_ITERATIONS, USE_CURRENT_POSITIONS, ENABLE_ROTATION, SKIP_LOCKED
    global ARRANGE_PASSIVES
    if args.pcb:                               PCB_FILE = args.pcb
    if args.report:                            REPORT_FILE = args.report
    if REPORT_FILE is None:
        import os as _os
        _pcb_dir = _os.path.dirname(_os.path.abspath(PCB_FILE))
        REPORT_FILE = _os.path.join(_pcb_dir, "Reports", "placement_report.txt")
    if args.live:                              DRY_RUN = False
    if args.dry_run:                           DRY_RUN = True
    if args.diff_pair_min_sep is not None:     DIFF_PAIR_MIN_SEP = args.diff_pair_min_sep
    if args.courtyard_gap is not None:         COURTYARD_GAP_IC = args.courtyard_gap
    if args.board_margin is not None:          BOARD_MARGIN = args.board_margin
    if args.iterations is not None:            ITERATIONS = args.iterations
    if args.cycles is not None:                DIFF_PAIR_CYCLES = args.cycles
    if args.overlap_iterations is not None:    OVERLAP_ITERATIONS = args.overlap_iterations
    if args.use_current_positions:             USE_CURRENT_POSITIONS = True
    if args.no_rotation:                       ENABLE_ROTATION = False
    if args.skip_locked:                       SKIP_LOCKED = True
    if args.move_locked:                       SKIP_LOCKED = False
    if args.no_passive_arrange:                ARRANGE_PASSIVES = False

    # ── Cluster definitions (optional) ───────────────────────────────────────
    clusters:            Dict[str, dict] = {}
    ref_to_cluster:      Dict[str, str]  = {}
    forced_rotations:    Dict[str, float] = {}
    passive_side_overrides: Dict[str, str] = {}   # ref → "above"|"below"|"left"|"right"
    if args.clusters:
        import json as _json
        with open(args.clusters) as _f:
            _raw = _json.load(_f)
        forced_rotations = {k: float(v) for k, v in _raw.pop("forced_rotations", {}).items()}
        _npo = _raw.pop("non_passive_overrides", [])
        _NON_PASSIVE_OVERRIDE_REFS.update(_npo)
        passive_side_overrides = _raw.pop("passive_side_overrides", {})
        clusters = _raw
        for _name, _cdef in clusters.items():
            for _ref in _cdef.get("members", []):
                ref_to_cluster[_ref] = _name
        suffix = f"  |  {len(forced_rotations)} forced rotation(s)" if forced_rotations else ""
        npo_suffix = f"  |  non_passive_overrides: {_npo}" if _npo else ""
        pso_suffix = f"  |  passive_side_overrides: {list(passive_side_overrides.keys())}" if passive_side_overrides else ""
        print(f"Clusters loaded: {len(clusters)} ({', '.join(clusters.keys())}){suffix}{npo_suffix}{pso_suffix}")

    print(f"Loading: {PCB_FILE}")
    board = pcbnew.LoadBoard(PCB_FILE)

    # ── Board geometry ────────────────────────────────────────────────────────
    outline = board.GetBoardEdgesBoundingBox()
    bx = to_mm(outline.GetX())
    by = to_mm(outline.GetY())
    bw = to_mm(outline.GetWidth())
    bh = to_mm(outline.GetHeight())
    m  = BOARD_MARGIN
    ax, ay = bx + m, by + m
    aw, ah = bw - 2*m, bh - 2*m

    print(f"Board: {bw:.2f} × {bh:.2f} mm  origin ({bx:.2f}, {by:.2f})")
    print(f"Usable area: {aw:.2f} × {ah:.2f} mm  (margin {m:.1f} mm)")

    # ── Build component list ──────────────────────────────────────────────────
    comps: List[Comp] = []
    skipped_refs: List[str] = []

    # Gather local pad positions now, before any rotation is applied, so the
    # 0°-frame pad centroid is computed from the board's current footprint state.
    pad_locals_map: Dict[str, Dict[str, List[Tuple[float, float]]]] = {}

    for fp in board.GetFootprints():
        ref = fp.GetReference()

        if has_prefix(ref, SKIP_PREFIXES):
            skipped_refs.append(f"{ref} [skip-prefix]")
            continue

        w, h = courtyard_size(fp)
        nets  = pad_nets(fp)
        pad_locals_map[ref] = pad_net_local_positions(fp)
        cx    = to_mm(fp.GetX())
        cy    = to_mm(fp.GetY())
        # courtyard_size returns world-frame bbox (pcbnew already applies rotation),
        # so track the footprint's actual current rotation to correctly update
        # comp.w/comp.h in rotation passes when the rotation changes.
        initial_rot = fp.GetOrientationDegrees()

        # Locked in KiCad = fixed. Period. Never move a locked component.
        if SKIP_LOCKED and fp.IsLocked():
            comps.append(Comp(fp=fp, ref=ref, x=cx, y=cy,
                              w=w, h=h, fixed=True, nets=nets,
                              rot=initial_rot))
            continue

        fixed = False

        if ref in ANCHORS:
            # User-defined mechanical position in config
            cx, cy = ANCHORS[ref]
            fixed = True
        elif has_prefix(ref, CONNECTOR_PREFIXES):
            edge   = nearest_edge(cx, cy, bx, by, bw, bh)
            cx, cy = edge_position(
                Comp(fp=fp, ref=ref, x=cx, y=cy, w=w, h=h, fixed=False, nets=nets),
                edge, bx, by, bw, bh, m
            )
            fixed = True

        comps.append(Comp(fp=fp, ref=ref, x=cx, y=cy,
                          w=w, h=h, fixed=fixed, nets=nets, rot=initial_rot))

    locked_fixed = [c for c in comps if c.fixed and SKIP_LOCKED and c.fp.IsLocked()]
    print(f"Components: {len(comps)} total  |  {len(locked_fixed)} locked (fixed attractors)  |  {len(skipped_refs)} skipped")

    # ── Build net index (net → list of Comp) ─────────────────────────────────
    net_index: Dict[str, List[Comp]] = {}
    for comp in comps:
        for net in comp.nets:
            net_index.setdefault(net, []).append(comp)

    net_fanouts = {n: len(members) for n, members in net_index.items()}
    nets_in_force  = sum(1 for f in net_fanouts.values() if 2 <= f <= MAX_FANOUT_FOR_FORCE)
    nets_excluded  = sum(1 for f in net_fanouts.values() if f > MAX_FANOUT_FOR_FORCE)
    print(f"Nets: {len(net_index)} total  |  {nets_in_force} used for forces  |  {nets_excluded} excluded (fanout > {MAX_FANOUT_FOR_FORCE})")

    # ── --dump-nets early exit ────────────────────────────────────────────────
    if args.dump_nets:
        import sys as _sys, json as _json
        out = open(args.dump_nets, "w") if args.dump_nets != "-" else _sys.stdout
        out.write("ref,fixed,net_count,signal_nets\n")
        for c in sorted(comps, key=lambda x: x.ref):
            sig = [n for n in c.nets
                   if 1 < len(net_index.get(n, [])) <= MAX_FANOUT_FOR_FORCE]
            out.write(f"{c.ref},{1 if c.fixed else 0},{len(c.nets)},{' '.join(sig)}\n")
        if args.dump_nets != "-":
            out.close()
        print("Net dump complete. Exiting.")
        import sys; sys.exit(0)

    # ── Seed initial positions ────────────────────────────────────────────────
    if USE_CURRENT_POSITIONS:
        print("Using current board positions as starting point.")
        # Clamp any out-of-bounds components into the usable area
        for comp in comps:
            if not comp.fixed:
                comp.x = clamp(comp.x, ax + comp.w/2, ax + aw - comp.w/2)
                comp.y = clamp(comp.y, ay + comp.h/2, ay + ah - comp.h/2)
    elif clusters:
        print(f"Seeding components in cluster layout ({len(clusters)} clusters).")
        seed_positions_clustered(comps, clusters, ax, ay, aw, ah)
    else:
        print("Seeding components in grid layout.")
        seed_positions(comps, ax, ay, aw, ah, bx, by, bw, bh, m)

    refine_only = getattr(args, "refine_only", False)

    wl_before = total_wirelength(comps, net_index)
    print(f"Initial HPWL: {wl_before:.1f} mm")

    # ── Seed overlap clearance ────────────────────────────────────────────────
    # After cluster/grid seeding, components may overlap. Resolve before the
    # cascade so IC rotations and passive face decisions start from a clean state.
    if not refine_only:
        print("Seed overlap clearance pass ...")
        resolve_overlaps(comps, ax, ay, aw, ah, OVERLAP_ITERATIONS)

    # ── Passive assignment (always needed — dp-pair exclusion uses it) ───────────
    # Grid placement runs AFTER the convergence loop so that parent IC rotations
    # have fully settled before passives are snapped to the correct face.
    passive_assign: Dict[int, Tuple[Comp, Comp]] = assign_passives_to_parents(
        comps, net_index, ref_to_cluster or None
    )
    if not ARRANGE_PASSIVES:
        print("Passive arrangement disabled (--no-passive-arrange).")

    # ── Detect differential pairs ─────────────────────────────────────────────
    diff_pairs    = find_diff_pairs(net_index, DIFF_PAIR_SUFFIXES)
    dp_comp_pairs = get_diff_pair_comp_pairs(diff_pairs, net_index, passive_assign)
    if diff_pairs:
        print(f"Differential pairs detected: {len(diff_pairs)}  ({len(dp_comp_pairs)} component pairs to separate)")
        for net_p, net_n in diff_pairs[:10]:
            print(f"  {net_p} / {net_n}")
        if len(diff_pairs) > 10:
            print(f"  ... and {len(diff_pairs)-10} more")
    else:
        print("No differential pairs detected (check DIFF_PAIR_SUFFIXES if expected).")

    # ── Iterative convergence: overlap → rotation → diff-pair separation ──────
    # Each cycle: resolve overlaps first (clearance is highest priority after
    # anchors), then re-orient rotations based on settled positions, then enforce
    # diff-pair spacing. Repeat until all constraints converge.
    print(f"\nIterative convergence (up to {DIFF_PAIR_CYCLES} cycles) ...")
    total_overlap_passes = 0
    total_dp_passes      = 0
    total_rotated        = 0
    cyd_violations:    List = []
    cyd_locked_locked: List = []
    dp_violations:     List = []
    dp_locked_locked:  List = []

    prev_stuck_refs: set = set()
    for cycle in range(DIFF_PAIR_CYCLES):
        # ── Escape components trapped between locked parts ────────────────────
        if prev_stuck_refs:
            n_esc = escape_trapped(comps, prev_stuck_refs, ax, ay, aw, ah)
            if n_esc:
                resolve_overlaps(comps, ax, ay, aw, ah, OVERLAP_ITERATIONS // 2)

        # ── Overlap resolution (primary constraint) ───────────────────────────
        ov_passes = resolve_overlaps(
            comps, ax, ay, aw, ah, OVERLAP_ITERATIONS
        )
        total_overlap_passes += ov_passes
        cyd_violations, cyd_locked_locked = check_courtyard_clearances(comps)

        # Track moveable components still in violation for next-cycle escape
        prev_stuck_refs = set()
        for ref_a, ref_b, _ in cyd_violations:
            for c in comps:
                if not c.fixed:
                    if c.ref == ref_a:
                        prev_stuck_refs.add(ref_a)
                    elif c.ref == ref_b:
                        prev_stuck_refs.add(ref_b)

        # ── IC rotation then face assignment (passives handled post-convergence) ─
        rot_this_cycle = 0
        if ENABLE_ROTATION:
            rot_this_cycle = rotation_pass_ics(comps, net_index, pad_locals_map, forced_rotations)
            total_rotated += rot_this_cycle
            if _PROXIMITY_RULES:
                fa_moved = face_assign_ics(comps, pad_locals_map, net_index,
                                           (ax, ay, ax + aw, ay + ah))
                total_rotated += fa_moved

        # ── Diff-pair separation ──────────────────────────────────────────────
        dp_passes     = 0
        dp_violations = []
        dp_locked_locked = []
        if dp_comp_pairs:
            dp_passes, (dp_violations, dp_locked_locked) = enforce_diff_pair_separation(
                dp_comp_pairs, DIFF_PAIR_MIN_SEP,
                ax, ay, aw, ah, DIFF_PAIR_ITERATIONS
            )
            total_dp_passes += dp_passes

        # Convergence considers only resolvable violations (not locked-locked)
        converged = (not cyd_violations) and (not dp_violations)
        print(f"  cycle {cycle+1}/{DIFF_PAIR_CYCLES}  "
              f"overlap_passes={ov_passes}  rotated={rot_this_cycle}  dp_passes={dp_passes}  "
              f"cyd_viol={len(cyd_violations)}(+{len(cyd_locked_locked)} locked)  "
              f"dp_viol={len(dp_violations)}(+{len(dp_locked_locked)} locked)"
              + ("  converged" if converged else ""))
        if converged:
            break

    # ── Post-convergence cascade ──────────────────────────────────────────────
    # Strict one-way pipeline — no circular dependencies:
    #   1. Final IC rotation  — ICs settled by convergence loop; one more pass
    #                           now that unlocked neighbours have stable positions.
    #                           Passives excluded from pull vectors (not yet placed).
    #   2. Passive face placement — snap each passive to the IC face its connecting
    #                           pads exit on, using the IC's now-final rotation.
    #   3. Passive rotation   — orient each passive's pads toward its parent; pull
    #                           vectors are reliable now that passives are adjacent.
    #   4. Passive re-snap    — rebuild grids with updated passive dimensions
    #                           (w/h may have swapped after rotation).
    #   5. Overlap resolution — final clearance cleanup.
    # Runs for BOTH force and refine passes so positions are always fully cascaded.
    # ── Apply forced rotations (overrides algorithm result for specific ICs) ──
    for comp in comps:
        if comp.fixed or comp.ref not in forced_rotations:
            continue
        new_rot = forced_rotations[comp.ref] % 360.0
        if abs(new_rot - comp.rot) > 0.1:
            if abs(new_rot % 180.0 - 90.0) < 1.0 and abs(comp.rot % 180.0 - 90.0) > 1.0:
                comp.w, comp.h = comp.h, comp.w
            elif abs(new_rot % 180.0 - 90.0) > 1.0 and abs(comp.rot % 180.0 - 90.0) < 1.0:
                comp.w, comp.h = comp.h, comp.w
        comp.rot = new_rot
    if forced_rotations:
        applied = [r for r in forced_rotations if any(c.ref == r for c in comps if not c.fixed)]
        print(f"Forced rotations applied: {', '.join(f'{r}={forced_rotations[r]:.0f}°' for r in applied)}")

    if ARRANGE_PASSIVES:
        total_passives = sum(1 for c in comps if _is_passive(c.ref) and not c.fixed)
        assigned_count = len(passive_assign)

        # 1. Final IC rotation
        rot_ics = 0
        if ENABLE_ROTATION:
            rot_ics = rotation_pass_ics(comps, net_index, pad_locals_map, forced_rotations)

        # 1b. IC face assignment — multi-pass: face→rotation→face→rotation.
        # Runs after IC rotation so the anchor orientation is known, then
        # re-rotates so ICs can adapt to the repositioned satellites.
        if ENABLE_ROTATION and _PROXIMITY_RULES:
            print("Cascade: face_assign_ics multi-pass ...")
            _bb = (ax, ay, ax + aw, ay + ah)
            for _fa_pass in range(2):
                fa_moved = face_assign_ics(comps, pad_locals_map, net_index, _bb)
                if fa_moved:
                    rotation_pass_ics(comps, net_index, pad_locals_map, forced_rotations)

        # 2. Place passives on the correct IC face
        print("Cascade: placing passives on parent IC faces ...")
        n_arranged = arrange_passives_in_grid(
            passive_assign, comps, pad_locals_map, net_index, ax, ay, aw, ah,
            passive_side_overrides or None,
        )

        # 3. Rotate passives toward their parent's connecting pads
        rot_pass = 0
        if ENABLE_ROTATION:
            rot_pass = rotation_pass_passives(comps, net_index, pad_locals_map, passive_assign)

        # 4. Re-snap passives into grids (dims may have swapped after rotation)
        n_rearranged = 0
        if rot_pass > 0:
            n_rearranged = arrange_passives_in_grid(
                passive_assign, comps, pad_locals_map, net_index, ax, ay, aw, ah,
                passive_side_overrides or None,
            )

        # 5. Final overlap cleanup
        stab_ov = resolve_overlaps(comps, ax, ay, aw, ah, OVERLAP_ITERATIONS)
        total_overlap_passes += stab_ov

        print(f"  {assigned_count}/{total_passives} passives assigned  "
              f"|  arranged={n_arranged}  rot_ics={rot_ics}  rot_pass={rot_pass}  "
              f"re-arranged={n_rearranged}  overlap_passes={stab_ov}")

        cyd_violations, cyd_locked_locked = check_courtyard_clearances(comps)
        dp_violations = []
        dp_locked_locked = []
        if dp_comp_pairs:
            _, (dp_violations, dp_locked_locked) = enforce_diff_pair_separation(
                dp_comp_pairs, DIFF_PAIR_MIN_SEP, ax, ay, aw, ah, DIFF_PAIR_ITERATIONS
            )

    wl_final = total_wirelength(comps, net_index)
    print(f"Final HPWL: {wl_final:.1f} mm  "
          f"(total overlap passes={total_overlap_passes}  dp passes={total_dp_passes}  "
          f"total rotated={total_rotated})")

    # ── Courtyard clearance report ────────────────────────────────────────────
    if cyd_violations:
        print(f"WARNING: {len(cyd_violations)} courtyard violation(s) remain (resolvable):")
        for ref_a, ref_b, actual in cyd_violations[:10]:
            threshold = _pair_gap(ref_a, ref_b)
            print(f"  {ref_a} ↔ {ref_b}  gap={actual:.3f} mm  (min {threshold:.3f} mm)")
        if len(cyd_violations) > 10:
            print(f"  ... and {len(cyd_violations)-10} more (see report)")
    else:
        print("Courtyard clearance: PASS — no violations")
    if cyd_locked_locked:
        print(f"NOTE: {len(cyd_locked_locked)} locked-locked courtyard violation(s) — user anchor placement issue:")
        for ref_a, ref_b, actual in cyd_locked_locked[:5]:
            threshold = _pair_gap(ref_a, ref_b)
            print(f"  {ref_a} ↔ {ref_b}  gap={actual:.3f} mm  (min {threshold:.3f} mm)")

    # ── Diff-pair clearance report ────────────────────────────────────────────
    if dp_violations:
        print(f"WARNING: {len(dp_violations)} diff-pair separation violation(s) remain (resolvable):")
        for ref_a, ref_b, actual in dp_violations[:10]:
            print(f"  {ref_a} ↔ {ref_b}  gap={actual:.3f} mm  (min {DIFF_PAIR_MIN_SEP:.2f} mm)")
        if len(dp_violations) > 10:
            print(f"  ... and {len(dp_violations)-10} more (see report)")
    elif dp_comp_pairs:
        print(f"Diff-pair separation: PASS — all resolvable pairs >= {DIFF_PAIR_MIN_SEP:.2f} mm")
    if dp_locked_locked:
        print(f"NOTE: {len(dp_locked_locked)} locked-locked diff-pair violation(s) — user anchor placement issue:")
        for ref_a, ref_b, actual in dp_locked_locked[:5]:
            print(f"  {ref_a} ↔ {ref_b}  gap={actual:.3f} mm")

    # ── Apply positions to PCB ────────────────────────────────────────────────
    report_lines = [
        "NET-AWARE PLACEMENT REPORT",
        "=" * 80,
        f"PCB:     {PCB_FILE}",
        f"Board:   {bw:.2f} × {bh:.2f} mm",
        f"Usable:  {aw:.2f} × {ah:.2f} mm  (margin {m:.1f} mm)",
        f"Gap:     passive={COURTYARD_GAP_PASSIVE:.3f} mm | IC={COURTYARD_GAP_IC:.3f} mm | connector={COURTYARD_GAP_CONNECTOR:.3f} mm",
        f"Algorithm: cascade (seed → overlap → IC rotation → passive arrangement → passive rotation → overlap)",
        f"Max net fanout for passive assignment: {MAX_FANOUT_FOR_FORCE}",
        f"Rotation: {'enabled' if ENABLE_ROTATION else 'disabled'}",
        f"Passive arrangement: {'enabled (cascade)' if ARRANGE_PASSIVES else 'disabled'}",
        f"Start:   {'current board positions' if USE_CURRENT_POSITIONS else 'cluster seed' if clusters else 'grid seed'}",
        f"Diff-pair min sep: {DIFF_PAIR_MIN_SEP:.1f} mm  |  pairs: {len(diff_pairs)}  |  comp pairs: {len(dp_comp_pairs)}",
        f"Convergence cycles: {DIFF_PAIR_CYCLES}  |  overlap passes: {total_overlap_passes}  |  dp passes: {total_dp_passes}",
        f"",
        f"HPWL before: {wl_before:.1f} mm",
        f"HPWL after:  {wl_final:.1f} mm  ({100*(wl_before-wl_final)/max(wl_before,1):.1f}% improvement)",
        f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE — PCB saved'}",
        "",
        f"{'REF':<22} {'POS (mm)':<22} {'ROT':>5}  {'SIZE (mm)':<16} {'NETS':<4} {'FIXED'}",
        "-" * 80,
    ]

    # Final board-boundary safety clamp: catch any component that escaped
    # per-step clamping (e.g. via passive arrangement cascades or rotation
    # dimension swaps that changed clamp bounds mid-run).
    off_board_refs = []
    for comp in comps:
        if comp.fixed:
            continue
        clamped_x = clamp(comp.x, ax + comp.w/2, ax + aw - comp.w/2)
        clamped_y = clamp(comp.y, ay + comp.h/2, ay + ah - comp.h/2)
        if abs(clamped_x - comp.x) > 0.01 or abs(clamped_y - comp.y) > 0.01:
            off_board_refs.append(f"{comp.ref} ({comp.x:.2f},{comp.y:.2f}) → ({clamped_x:.2f},{clamped_y:.2f})")
            comp.x, comp.y = clamped_x, clamped_y
    if off_board_refs:
        print(f"WARNING: {len(off_board_refs)} component(s) found off-board — clamped before save:")
        for s in off_board_refs:
            print(f"  {s}")

    fixed_count = 0
    moved_count = 0
    locked_count = 0

    for comp in sorted(comps, key=lambda c: c.ref):
        if comp.fixed and SKIP_LOCKED and comp.fp.IsLocked():
            tag = "LOCKED"
        elif comp.fixed:
            tag = "FIXED"
        else:
            tag = ""
        report_lines.append(
            f"{comp.ref:<22} ({comp.x:7.2f}, {comp.y:7.2f})  "
            f"{comp.rot:5.0f}°  "
            f"{comp.w:.2f}×{comp.h:.2f}        "
            f"{len(comp.nets):<4} {tag}"
        )
        if not DRY_RUN:
            # Never write position/rotation to any locked component.
            if not (SKIP_LOCKED and comp.fp.IsLocked()):
                comp.fp.SetPosition(pcbnew.VECTOR2I(mm(comp.x), mm(comp.y)))
                comp.fp.SetOrientationDegrees(comp.rot)
        if SKIP_LOCKED and comp.fp.IsLocked():
            locked_count += 1
        elif comp.fixed:
            fixed_count += 1
        else:
            moved_count += 1

    if skipped_refs:
        report_lines.append(f"\nSKIPPED ({len(skipped_refs)}):")
        for s in sorted(skipped_refs):
            report_lines.append(f"  {s}")

    report_lines += [
        "",
        f"Placed:  {moved_count}  |  Fixed: {fixed_count}  |  Locked: {locked_count}  |  Skipped: {len(skipped_refs)}",
        f"Rotated: {total_rotated}  |  Overlap passes (total): {total_overlap_passes}  |  DP passes (total): {total_dp_passes}",
        "",
        f"COURTYARD CLEARANCE CHECK (passive={COURTYARD_GAP_PASSIVE:.3f} | IC={COURTYARD_GAP_IC:.3f} | connector={COURTYARD_GAP_CONNECTOR:.3f} mm):",
    ]
    if cyd_violations:
        report_lines.append(f"  FAIL — {len(cyd_violations)} resolvable violation(s):")
        report_lines.append(f"  {'Component A':<22} {'Component B':<22} {'Gap (mm)':>10} {'Min (mm)':>10}")
        report_lines.append(f"  {'-'*68}")
        for ref_a, ref_b, actual in cyd_violations:
            threshold = _pair_gap(ref_a, ref_b)
            flag = "OVERLAP" if actual < 0 else "TOO CLOSE"
            report_lines.append(f"  {ref_a:<22} {ref_b:<22} {actual:>8.3f}  {threshold:>8.3f}  {flag}")
    else:
        report_lines.append(f"  PASS — all resolvable pairs meet minimum courtyard gap")
    if cyd_locked_locked:
        report_lines.append(f"  NOTE — {len(cyd_locked_locked)} locked-locked violation(s) (user anchor placement):")
        for ref_a, ref_b, actual in cyd_locked_locked:
            threshold = _pair_gap(ref_a, ref_b)
            flag = "OVERLAP" if actual < 0 else "TOO CLOSE"
            report_lines.append(f"  {ref_a:<22} {ref_b:<22} {actual:>8.3f}  {threshold:>8.3f}  {flag}  [LOCKED-LOCKED]")

    report_lines += [
        "",
        f"DIFFERENTIAL PAIR SEPARATION CHECK (min sep {DIFF_PAIR_MIN_SEP:.1f} mm edge-to-edge):",
    ]
    if not diff_pairs:
        report_lines.append("  No differential pairs detected.")
    elif dp_violations:
        report_lines.append(f"  FAIL — {len(dp_violations)} resolvable violation(s):")
        report_lines.append(f"  {'Component A':<22} {'Component B':<22} {'Gap (mm)':>10}")
        report_lines.append(f"  {'-'*56}")
        for ref_a, ref_b, actual in dp_violations:
            flag = "OVERLAP" if actual < 0 else "TOO CLOSE"
            report_lines.append(f"  {ref_a:<22} {ref_b:<22} {actual:>8.3f}  {flag}")
    else:
        report_lines.append(f"  PASS — all {len(dp_comp_pairs)} enforced diff-pair pairs >= {DIFF_PAIR_MIN_SEP:.1f} mm")
    if dp_locked_locked:
        report_lines.append(f"  NOTE — {len(dp_locked_locked)} locked-locked diff-pair violation(s) (user anchor placement):")
        for ref_a, ref_b, actual in dp_locked_locked:
            report_lines.append(f"  {ref_a:<22} {ref_b:<22} {actual:>8.3f}  [LOCKED-LOCKED]")

    if diff_pairs:
        report_lines.append(f"\nDETECTED DIFFERENTIAL PAIRS ({len(diff_pairs)}):")
        for net_p, net_n in diff_pairs:
            report_lines.append(f"  {net_p}  /  {net_n}")

    excluded_nets = sorted(
        [(n, f) for n, f in net_fanouts.items() if f > MAX_FANOUT_FOR_FORCE],
        key=lambda x: -x[1]
    )
    if excluded_nets:
        report_lines.append(f"\nEXCLUDED NETS (fanout > {MAX_FANOUT_FOR_FORCE}):")
        for net, fanout in excluded_nets[:20]:
            report_lines.append(f"  {net:<30} {fanout} pins")
        if len(excluded_nets) > 20:
            report_lines.append(f"  ... and {len(excluded_nets)-20} more")

    report_text = "\n".join(report_lines)

    import os as _os2; _os2.makedirs(_os2.path.dirname(REPORT_FILE), exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"\nReport: {REPORT_FILE}")
    print(report_text)

    if not DRY_RUN:
        import shutil as _shutil, datetime as _dt
        _out_path = args.output if (args.output and args.output != PCB_FILE) else None
        def _rm_companion_files(pcb_path: str) -> None:
            stem = _os2.path.splitext(pcb_path)[0]
            for ext in (".kicad_pro", ".kicad_prl", ".lck"):
                candidate = stem + ext
                if _os2.path.exists(candidate):
                    try:
                        _os2.remove(candidate)
                    except OSError:
                        pass
            # Also clean root-dir companions that pcbnew may drop using the stem only
            pcb_dir = _os2.path.dirname(_os2.path.abspath(PCB_FILE))
            bare_stem = _os2.path.splitext(_os2.path.basename(pcb_path))[0]
            for ext in (".kicad_pro", ".kicad_prl"):
                candidate = _os2.path.join(pcb_dir, bare_stem + ext)
                if _os2.path.exists(candidate):
                    try:
                        _os2.remove(candidate)
                    except OSError:
                        pass

        if _out_path:
            # --output specified: write to a separate file, leave the input scatter untouched.
            _os2.makedirs(_os2.path.dirname(_os2.path.abspath(_out_path)), exist_ok=True)
            board.Save(_out_path)
            _rm_companion_files(_out_path)
            print(f"\n  PCB saved to: {_out_path}  (input scatter preserved at {PCB_FILE})")
        else:
            _ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            _backup_dir = _os2.path.join(_os2.path.dirname(_os2.path.abspath(PCB_FILE)), "Backups")
            _os2.makedirs(_backup_dir, exist_ok=True)
            _backup_path = _os2.path.join(_backup_dir, f"{_os2.path.splitext(_os2.path.basename(PCB_FILE))[0]}_backup_{_ts}.kicad_pcb")
            _shutil.copy2(PCB_FILE, _backup_path)
            print(f"\n  Backup saved to: {_backup_path}")
            board.Save(PCB_FILE)
            print(f"  PCB saved to: {PCB_FILE}")
    else:
        print("\nDRY RUN complete — PCB not saved. Set DRY_RUN = False to apply.")

    overall = "PASS" if (not cyd_violations and not dp_violations) else "FAIL"
    print(f"\nRESULT: {overall}  cyd={len(cyd_violations)}  dp={len(dp_violations)}"
          f"  cyd_locked={len(cyd_locked_locked)}  dp_locked={len(dp_locked_locked)}"
          f"  hpwl={wl_final:.1f}mm")


if __name__ == "__main__":
    main()
