#!/usr/bin/env python3
"""
route_fanout_vias.py

Pre-route fanout via placement for all nets that must reach a different copper
layer before trace routing begins. Runs after route_clearance_audit.py,
before route_critical.py.

Placement priority: HS pairs first (never displaced), switching loop nets
second, all other nets last. Multi-pass iterative refinement — no via is
locked between passes. Each pass searches outward from the minimum neckdown
distance until a clear position is found, shuffling lower-priority vias to
make room for higher-priority ones.

Crossing detection: when two stubs would cross, the lower-priority via is
staggered further along its escape axis. Unresolvable crossings are flagged.

Usage:
    python route_fanout_vias.py [--apply] [--max-passes N]
"""

import sys
import os
import math
import argparse
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import routing_config as cfg

sys.path.insert(0, cfg.KICAD_SITE_PKGS)
import pcbnew

# Suppress wxWidgets dialog popups — KiCad's Python bindings set a wx log
# target that converts warnings/errors into modal dialogs.  In headless script
# use these are undesirable; disable the active log target entirely.
try:
    import wx
    wx.Log.EnableLogging(False)
except Exception:
    pass

# ---------------------------------------------------------------------------
STEP_MM = 0.05  # outward search increment when scanning for a clear spot

PRIORITY_HS    = 0   # HS differential pairs — placed first, never displaced
PRIORITY_SW    = 1   # switching loop power nets
PRIORITY_OTHER = 2   # all other signal nets


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PendingVia:
    net_name:        str
    ref:             str
    pad_num:         str
    pad_x:           float      # mm
    pad_y:           float      # mm
    pad_layer_id:    int
    target_layer_id: int
    escape_dx:       float      # cardinal unit vector
    escape_dy:       float
    priority:        int
    via_drill_mm:    float
    via_annular_mm:  float
    neckdown_w_mm:   float
    neckdown_len_mm: float      # minimum pad-to-via distance
    max_search_mm:   float      # give up if no clear spot found within this
    pad_w_mm:        float = 0.0   # pad copper width  — used for via-in-pad fit check
    pad_h_mm:        float = 0.0   # pad copper height — used for via-in-pad fit check
    pad_bbox:        tuple = None  # (left, top, right, bottom) mm world coords from GetBoundingBox()
    via_x:           float = 0.0
    via_y:           float = 0.0
    via_in_pad:            bool  = False  # True → via sits in pad centre; VIPPO required
    implicit_keepout:      bool  = False  # True → no via placed; routing script places via along route
    stub_conflict:         bool  = False  # True → placed ignoring placed_stubs; geometric conflict accepted
    warning:               str   = ""
    corner_lat_offset_mm:  float = 0.0   # lateral offset applied to corner to separate same-column stubs
    netclass:              str   = ""    # KiCad net class name (e.g. "HighSpeed", "Power")
    cluster_real_pads:     list  = None  # [(pad_x, pad_y, neckdown_w_mm), ...] for herringbone cluster primaries
    sandwiched:            bool  = False  # True → non-HS pad between HS pairs on same face; escaped inward


@dataclass
class Obstacle:
    """Circular keep-out in mm-space.  When bbox is set, via-endpoint and stub
    clearance use the exact pad rectangle instead of the circular approximation."""
    cx:       float
    cy:       float
    r:        float
    ref:      str   = ""    # source footprint reference
    bbox:     tuple = None  # (left, top, right, bottom) mm — pad copper boundary
    net_name: str   = ""    # net this pad belongs to
    netclass: str   = ""    # net class name (e.g. "HighSpeed", "Power")


@dataclass
class StubSeg:
    """Committed stub segment used to check clearance of new placements.
    Covers both the pad→corner and corner→via legs of the neckdown trace."""
    x1:      float
    y1:      float
    x2:      float
    y2:      float
    half_w:   float   # neckdown_w/2 + default clearance, already baked in
    ref:      str = ""
    pad_num:  str = ""
    net_name: str = ""  # net this stub belongs to (for same-net skip)
    netclass: str = ""  # net class of this stub's net


# ---------------------------------------------------------------------------
# Per-net-pair clearance rules
# ---------------------------------------------------------------------------

# HV nets requiring 1.0 mm clearance from everything else.
_HV_NETS = frozenset({'PWR_IN_RAW', 'PWR_IN_FUSED', 'USBC_VBUS_RAW', 'USBC_VBUS', 'VMAIN'})

# Tight-pitch connector refs with reduced default clearance.
_TIGHT_PITCH_REFS: Dict[str, float] = {'SOM1': 0.08, 'SOM2': 0.08, 'J_DSI1': 0.10}

# Minimum clearance from any copper to the board edge cuts.
_BOARD_EDGE_CLEARANCE = 0.5


def _pair_clearance(net_a: str, netclass_a: str, ref_a: str,
                    net_b: str, netclass_b: str, ref_b: str) -> float:
    """Return the minimum clearance (mm) between two elements per board DRU rules.

    Hierarchy (first matching rule wins):
      1. Either net is HV → 1.0 mm
      2. Either net is +5V → 0.5 mm
      3. Either element is HighSpeed netclass → 0.254 mm (3W rule)
      4. Tight-pitch connector refs → reduced to ref-specific value
      5. Default → 0.15 mm
    """
    if net_a in _HV_NETS or net_b in _HV_NETS:
        return 1.0
    if netclass_a == 'HighSpeed' or netclass_b == 'HighSpeed':
        return 0.254  # 3W rule for high-speed signals
    cl = 0.15
    for ref in (ref_a, ref_b):
        cl_ref = _TIGHT_PITCH_REFS.get(ref)
        if cl_ref is not None:
            cl = min(cl, cl_ref)
    return cl


# ---------------------------------------------------------------------------
# Net classification
# ---------------------------------------------------------------------------

def _hs_net_layer_map() -> Dict[str, str]:
    """Map every HS P/N net name → its routing layer name."""
    m: Dict[str, str] = {}
    for p, n, layer, _skew in cfg.HS_PAIRS.values():
        m[p] = layer
        m[n] = layer
    return m


def _sw_net_layer_map() -> Dict[str, str]:
    """Map every switching-loop net name → its routing layer name."""
    keys = ("sw_net", "vin_net", "out_net", "gnd_net", "bootstrap_net")
    m: Dict[str, str] = {}
    for loop in cfg.SWITCHING_LOOPS:
        layer = loop.get("layer", cfg.LAYER_SCHEME.get("power", "B.Cu"))
        for k in keys:
            net = loop.get(k)
            if net:
                m[net] = layer
    return m


def classify(net_name: str,
             hs_map: Dict[str, str],
             sw_map: Dict[str, str],
             board) -> Tuple[int, int]:
    """Return (priority, target_layer_id) for net_name."""
    if net_name in hs_map:
        return PRIORITY_HS, board.GetLayerID(hs_map[net_name])
    if net_name in sw_map:
        return PRIORITY_SW, board.GetLayerID(sw_map[net_name])
    return PRIORITY_OTHER, board.GetLayerID(cfg.ROUTING_LAYER_PRIORITY[1])


# ---------------------------------------------------------------------------
# Escape direction
# ---------------------------------------------------------------------------

def escape_direction(fp, pad, neckdown_len_mm: float = 1.0,
                     preferred_dir: Optional[Tuple[float, float]] = None,
                     ) -> Tuple[float, float]:
    """
    Cardinal unit vector pointing away from the component body.

    Primary direction: the axis with the larger displacement from the footprint
    bounding box centre.  If a sibling pad (same footprint, different number)
    lies within neckdown_len_mm along the primary direction, that direction
    would route the neckdown stub straight into the neighbouring pad.  In that
    case the four cardinal directions are tried in preference order until one
    is found that is not immediately blocked by a sibling.  This handles dense
    in-line connector rows where all pads share the same primary axis.

    preferred_dir: if supplied (e.g. unit vector toward routing destination),
    candidates are re-sorted by dot-product alignment with it before the
    sibling check runs.  This keeps HS pair stubs parallel to the route axis
    so P and N exit on the same side and no topological crossing occurs.
    """
    bbox = fp.GetBoundingBox()
    cx   = pcbnew.ToMM(bbox.GetCenter().x)
    cy   = pcbnew.ToMM(bbox.GetCenter().y)
    px   = pcbnew.ToMM(pad.GetPosition().x)
    py   = pcbnew.ToMM(pad.GetPosition().y)
    dx, dy = px - cx, py - cy

    # Ordered candidate directions: primary first, then perpendiculars, then opposite
    if abs(dx) >= abs(dy):
        primary = (1.0 if dx >= 0.0 else -1.0, 0.0)
        candidates = [
            primary,
            (0.0,  1.0 if dy >= 0.0 else -1.0),
            (0.0, -1.0 if dy >= 0.0 else  1.0),
            (-primary[0], 0.0),
        ]
    else:
        primary = (0.0, 1.0 if dy >= 0.0 else -1.0)
        candidates = [
            primary,
            ( 1.0 if dx >= 0.0 else -1.0, 0.0),
            (-1.0 if dx >= 0.0 else  1.0, 0.0),
            (0.0, -primary[1]),
        ]

    # If a preferred direction is given, re-rank candidates by alignment with it.
    if preferred_dir is not None:
        pdx, pdy = preferred_dir
        candidates.sort(key=lambda c: -(c[0] * pdx + c[1] * pdy))

    # All sibling pad centres (same footprint, different pad number), including
    # unconnected pads.  A stub trace passing through an unconnected copper pad
    # is still a DRC clearance violation, so the escape direction must avoid them
    # just as strictly as connected pads.
    pad_num = pad.GetNumber()
    siblings = []
    for other in fp.Pads():
        if other.GetNumber() == pad_num:
            continue
        siblings.append((pcbnew.ToMM(other.GetPosition().x),
                         pcbnew.ToMM(other.GetPosition().y)))

    # Choose first direction not immediately blocked by a sibling pad.
    # A sibling blocks if its centre is within neckdown_len_mm ahead along
    # the escape axis AND within 0.5 mm laterally (one pitch).
    for edx, edy in candidates:
        blocked = False
        for ox, oy in siblings:
            proj    = (ox - px) * edx + (oy - py) * edy
            lateral = abs((ox - px) * edy - (oy - py) * edx)
            if 0.0 < proj <= neckdown_len_mm and lateral < 0.5:
                blocked = True
                break
        if not blocked:
            return edx, edy

    return candidates[0]  # all blocked — return primary as last resort


def _hs_dest_direction(net_name: str, src_ref: str, board,
                       ) -> Optional[Tuple[float, float]]:
    """Unit vector from src_ref footprint centre toward the centroid of all
    other footprints that carry a pad on the same HS pair net.

    Used to bias escape_direction() so HS stubs exit parallel to the route
    axis rather than perpendicular to the footprint body.  Parallel escape
    preserves P/N perpendicular ordering at both source and destination vias,
    avoiding topological crossings on In2.Cu.

    Returns None if the net is not a declared HS pair net, if no destination
    footprint is found, or if the centroid vector is degenerate (< 0.1 mm).
    """
    pair_nets: Optional[set] = None
    for _p_net, _n_net, *_ in cfg.HS_PAIRS.values():
        if net_name in (_p_net, _n_net):
            pair_nets = {_p_net, _n_net}
            break
    if pair_nets is None:
        return None
    src_fp = next((fp for fp in board.GetFootprints()
                   if fp.GetReference() == src_ref), None)
    if src_fp is None:
        return None
    src_bb = src_fp.GetBoundingBox()
    src_cx = pcbnew.ToMM(src_bb.GetCenter().x)
    src_cy = pcbnew.ToMM(src_bb.GetCenter().y)
    dest_cx_sum = dest_cy_sum = 0.0
    dest_count = 0
    for fp in board.GetFootprints():
        if fp.GetReference() == src_ref:
            continue
        if any(p.GetNetname() in pair_nets for p in fp.Pads()):
            bb = fp.GetBoundingBox()
            dest_cx_sum += pcbnew.ToMM(bb.GetCenter().x)
            dest_cy_sum += pcbnew.ToMM(bb.GetCenter().y)
            dest_count += 1
    if dest_count == 0:
        return None
    ddx = dest_cx_sum / dest_count - src_cx
    ddy = dest_cy_sum / dest_count - src_cy
    dist = math.hypot(ddx, ddy)
    return (ddx / dist, ddy / dist) if dist >= 0.1 else None


# ---------------------------------------------------------------------------
# Via and neckdown sizing
# ---------------------------------------------------------------------------

def via_params(priority: int) -> Tuple[float, float]:
    """Return (drill_mm, annular_ring_mm)."""
    a = cfg.CLEARANCE_AUDIT
    if priority == PRIORITY_HS:
        return a["hs_via_drill_mm"], a["hs_via_annular_ring_mm"]
    return a["via_drill_mm"], a["via_annular_ring_mm"]


def neckdown_params(priority: int, net_name: str) -> Tuple[float, float, float]:
    """Return (neckdown_floor_mm, neckdown_len_mm, max_search_mm)."""
    a  = cfg.CLEARANCE_AUDIT
    nw = a["neckdown_stub_w_mm"]
    nl = a["neckdown_length_mm"]
    mx = 15.0
    if priority == PRIORITY_SW:
        sw_keys = {"sw_net", "vin_net", "out_net", "gnd_net", "bootstrap_net"}
        for loop in cfg.SWITCHING_LOOPS:
            if any(loop.get(k) == net_name for k in sw_keys):
                nw = loop.get("neckdown_width_mm", nw)
                mx = loop.get("neckdown_max_mm",   mx)
                break
    return nw, nl, mx


def trace_width_for_net(net_name: str, priority: int) -> float:
    """Return the intended routing trace width for net_name from config."""
    if priority == PRIORITY_HS:
        for pair_name, (p, n, _layer, _skew) in cfg.HS_PAIRS.items():
            if net_name in (p, n):
                w, _gap = cfg.HS_ROUTE_WIDTHS.get(
                    pair_name,
                    (cfg.CLEARANCE_AUDIT["signal_trace_width_mm"], 0.0)
                )
                return w
    if priority == PRIORITY_SW:
        sw_width_keys = {
            "sw_net":        "sw_width_mm",
            "vin_net":       "vin_width_mm",
            "out_net":       "out_width_mm",
            "bootstrap_net": "bootstrap_width_mm",
        }
        for loop in cfg.SWITCHING_LOOPS:
            for net_key, width_key in sw_width_keys.items():
                if loop.get(net_key) == net_name:
                    return loop.get(width_key, cfg.CLEARANCE_AUDIT["signal_trace_width_mm"])
    return cfg.CLEARANCE_AUDIT["signal_trace_width_mm"]


def _collect_routing_widths() -> List[float]:
    """Return sorted list of all routing trace widths defined in config."""
    widths: set = set()
    widths.add(cfg.CLEARANCE_AUDIT["signal_trace_width_mm"])
    widths.add(cfg.CLEARANCE_AUDIT["neckdown_stub_w_mm"])
    for w, _gap in cfg.HS_ROUTE_WIDTHS.values():
        widths.add(w)
    for loop in cfg.SWITCHING_LOOPS:
        for key in ("sw_width_mm", "vin_width_mm", "out_width_mm", "bootstrap_width_mm"):
            if key in loop:
                widths.add(loop[key])
    return sorted(widths)


_ROUTING_WIDTHS: List[float] = _collect_routing_widths()


def neckdown_stub_width(floor_mm: float, pad_w_mm: float,
                         pad_h_mm: float, net_name: str, priority: int) -> float:
    """
    Stub width = routing trace width for the net, capped at the pad's narrow
    dimension, then snapped DOWN to the nearest defined routing width so the
    result is always a clean value from the config.  Never below floor_mm.
    """
    pad_narrow  = min(pad_w_mm, pad_h_mm)
    trace_width = trace_width_for_net(net_name, priority)
    # raw already respects both the floor and the pad-width ceiling.
    raw = min(pad_narrow, max(floor_mm, trace_width))
    # Snap down to the largest defined routing width that fits within raw.
    # Do NOT re-apply floor_mm after snapping — it is already embedded in raw.
    for w in sorted(_ROUTING_WIDTHS, reverse=True):
        if w <= raw + 1e-6:
            return w
    return raw  # fallback: no defined width fits (e.g. pad narrower than floor)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _via_r(via: PendingVia, clearance: float) -> float:
    return via.via_drill_mm / 2.0 + via.via_annular_mm + clearance


def _pad_obstacle(pad, clearance: float, ref: str = "") -> Obstacle:
    px = pcbnew.ToMM(pad.GetPosition().x)
    py = pcbnew.ToMM(pad.GetPosition().y)
    sx = pcbnew.ToMM(pad.GetSizeX()) / 2.0
    sy = pcbnew.ToMM(pad.GetSizeY()) / 2.0
    bb = pad.GetBoundingBox()
    bbox = (pcbnew.ToMM(bb.GetLeft()),  pcbnew.ToMM(bb.GetTop()),
            pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom()))
    net = pad.GetNetname() or ""
    # Retrieve the net class name via the pad's net info object.
    try:
        netclass = pad.GetNet().GetNetClass().GetName() if pad.GetNet() else ""
    except Exception:
        netclass = ""
    return Obstacle(px, py, math.hypot(sx, sy) + clearance, ref, bbox, net, netclass)


def _is_pth(pad) -> bool:
    return pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH


def _pt_to_seg_dist(px: float, py: float,
                    x1: float, y1: float, x2: float, y2: float) -> float:
    """Minimum distance from point (px,py) to line segment (x1,y1)-(x2,y2)."""
    dx, dy = x2 - x1, y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-10:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len_sq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _dist_point_to_bbox(x: float, y: float, bbox: tuple) -> float:
    """Minimum distance from point (x,y) to axis-aligned rectangle bbox=(left,top,right,bottom)."""
    al, at, ar, ab_ = bbox
    dx = max(0.0, max(al - x, x - ar))
    dy = max(0.0, max(at - y, y - ab_))
    return math.hypot(dx, dy)


def _clear_of_obs(x: float, y: float, r: float, obs: List[Obstacle],
                  excl_ref: str = "", excl_cx: float = 1e18,
                  excl_cy: float = 1e18, excl_net: str = "",
                  via_copper: float = 0.0,
                  via_net: str = "", via_netclass: str = "",
                  via_ref: str = "") -> bool:
    """Check that a circle of radius r (or via_copper + per-pair clearance) at (x,y)
    clears all obstacles.

    When via_copper > 0, the required clearance is computed per obstacle using
    _pair_clearance so that net-specific rules (HV, HS 3W, tight-pitch, etc.)
    are respected.  The pre-baked r is used only when via_copper == 0 (legacy
    callers or synthetic obstacles that don't carry net info).
    """
    for o in obs:
        if (excl_ref and o.ref == excl_ref
                and abs(o.cx - excl_cx) < 1e-3
                and abs(o.cy - excl_cy) < 1e-3):
            continue
        # Same-net pads need no DRC clearance — the via is connected to them.
        if excl_net and o.net_name and o.net_name == excl_net:
            continue
        if via_copper > 0.0:
            cl = _pair_clearance(via_net, via_netclass, via_ref,
                                 o.net_name, o.netclass, o.ref)
            check_r = via_copper + cl
            if o.bbox is not None:
                if _dist_point_to_bbox(x, y, o.bbox) < check_r:
                    return False
            else:
                if math.hypot(x - o.cx, y - o.cy) < check_r + o.r:
                    return False
        else:
            if o.bbox is not None:
                if _dist_point_to_bbox(x, y, o.bbox) < r:
                    return False
            else:
                if math.hypot(x - o.cx, y - o.cy) < r + o.r:
                    return False
    return True


def _load_edge_cuts(board) -> list:
    """Return list of ((x1,y1),(x2,y2)) mm segments from the Edge.Cuts layer.
    Arc/circle items are skipped — only straight segments are returned."""
    segs = []
    for item in board.GetDrawings():
        if item.GetLayer() == pcbnew.Edge_Cuts:
            if hasattr(item, 'GetStart') and hasattr(item, 'GetEnd'):
                try:
                    x1 = pcbnew.ToMM(item.GetStart().x)
                    y1 = pcbnew.ToMM(item.GetStart().y)
                    x2 = pcbnew.ToMM(item.GetEnd().x)
                    y2 = pcbnew.ToMM(item.GetEnd().y)
                    # Skip zero-length entries (e.g. circles stored as point+radius)
                    if math.hypot(x2 - x1, y2 - y1) > 1e-6:
                        segs.append(((x1, y1), (x2, y2)))
                except Exception:
                    pass
    return segs


def _clears_edges(x: float, y: float, r: float, edge_segs: list) -> bool:
    """True if a circle of radius r centred at (x,y) clears all board edge segments."""
    for (x1, y1), (x2, y2) in edge_segs:
        if _pt_to_seg_dist(x, y, x1, y1, x2, y2) < r:
            return False
    return True


def _seg_clears_edges(ax1: float, ay1: float, ax2: float, ay2: float,
                      half_w: float, edge_segs: list) -> bool:
    """True if a trace segment (ax1,ay1)→(ax2,ay2) with given half-width
    clears all board edge segments by at least half_w."""
    for (x1, y1), (x2, y2) in edge_segs:
        if _seg_min_dist(ax1, ay1, ax2, ay2, x1, y1, x2, y2) < half_w:
            return False
    return True


def _via_corner(via: "PendingVia") -> Tuple[float, float]:
    """Initial via candidate position: neckdown length along escape, optionally
    shifted laterally so same-column pads diverge from the pad exit."""
    cx = via.pad_x + via.escape_dx * via.neckdown_len_mm - via.escape_dy * via.corner_lat_offset_mm
    cy = via.pad_y + via.escape_dy * via.neckdown_len_mm + via.escape_dx * via.corner_lat_offset_mm
    return cx, cy


def _route_45deg_stub(pad_x: float, pad_y: float,
                      via_x: float, via_y: float,
                      edx: float, edy: float,
                      ) -> List[Tuple[float, float, float, float]]:
    """Decompose pad→via into 1 or 2 segments that each lie on a valid 45° angle.

    Always uses diagonal-first routing: exits the pad at 45° immediately,
    then finishes with a straight segment.  This keeps the stub away from
    neighboring pad rows (the straight segment lands at the via's coordinate,
    not the pad's, so it is further from adjacent vias).

    Returns a list of (x1, y1, x2, y2) tuples; empty list if pad == via.
    """
    dx = via_x - pad_x
    dy = via_y - pad_y
    if math.hypot(dx, dy) < 1e-6:
        return []

    # Project onto escape axis and the perpendicular lateral axis.
    escape_comp = dx * edx  + dy * edy
    lat_comp    = dx * (-edy) + dy * edx

    abs_ec = abs(escape_comp)
    abs_lc = abs(lat_comp)

    # Already a valid 45° direction: pure straight, pure lateral, or exact diagonal.
    if abs_lc < 1e-4 or abs_ec < 1e-4 or abs(abs_ec - abs_lc) < 1e-4:
        return [(pad_x, pad_y, via_x, via_y)]

    ec_sign = 1 if escape_comp > 0 else -1
    lc_sign = 1 if lat_comp    > 0 else -1
    lat_dx  = -edy   # unit lateral vector x  (escape rotated +90°)
    lat_dy  =  edx   # unit lateral vector y

    if abs_ec >= abs_lc:
        # 45° diagonal first (diverges from escape axis immediately), then
        # straight along the escape axis to via.  This keeps the stub away
        # from adjacent pad rows compared to "straight first" which would put
        # a horizontal segment at the pad's exact y-coordinate.
        wx = pad_x + ec_sign * edx * abs_lc + lc_sign * lat_dx * abs_lc
        wy = pad_y + ec_sign * edy * abs_lc + lc_sign * lat_dy * abs_lc
    else:
        # 45° diagonal first, then straight along lateral axis to via.
        wx = pad_x + ec_sign * edx * abs_ec + lc_sign * lat_dx * abs_ec
        wy = pad_y + ec_sign * edy * abs_ec + lc_sign * lat_dy * abs_ec

    return [(pad_x, pad_y, wx, wy), (wx, wy, via_x, via_y)]


def _stub_segs_for(via: "PendingVia", clearance: float) -> List[StubSeg]:
    """Return committed stub segments for via at its current position.

    Uses the same 45-degree routing as _make_neckdown so that clearance
    checks during placement match the geometry actually written to the board.
    Returns empty list for via-in-pad (no stub trace at all).
    """
    if via.via_in_pad:
        return []
    hw  = via.neckdown_w_mm / 2.0 + clearance
    net = via.net_name
    nc  = via.netclass
    raw = _route_45deg_stub(via.pad_x, via.pad_y, via.via_x, via.via_y,
                             via.escape_dx, via.escape_dy)
    return [StubSeg(x1, y1, x2, y2, hw, via.ref, via.pad_num, net, nc)
            for x1, y1, x2, y2 in raw]


def _load_board_tracks(board, clearance: float,
                       layers: Optional[set] = None) -> List[StubSeg]:
    """Return existing board tracks (on F.Cu/B.Cu by default) as StubSeg obstacles.

    Called once before placement so new vias don't land on pre-existing copper.
    Same-net exclusions are handled by _clear_of_stub_segs at check time.
    """
    if layers is None:
        layers = {pcbnew.F_Cu, pcbnew.B_Cu}
    segs: List[StubSeg] = []
    for track in board.GetTracks():
        try:
            # Skip vias — PCB_VIA::GetWidth() in KiCad 10 requires a layer arg;
            # calling it without one fires a wxWidgets assertion.  Vias are not
            # stub segments so they don't belong in this obstacle list anyway.
            if track.Type() == pcbnew.PCB_VIA_T:
                continue
            if track.GetLayer() not in layers:
                continue
            if not hasattr(track, 'GetStart') or not hasattr(track, 'GetWidth'):
                continue
            x1 = pcbnew.ToMM(track.GetStart().x)
            y1 = pcbnew.ToMM(track.GetStart().y)
            x2 = pcbnew.ToMM(track.GetEnd().x)
            y2 = pcbnew.ToMM(track.GetEnd().y)
            hw = pcbnew.ToMM(track.GetWidth()) / 2.0 + clearance
            net_obj  = track.GetNet()
            net_name = net_obj.GetNetname() if net_obj else ""
            segs.append(StubSeg(x1=x1, y1=y1, x2=x2, y2=y2,
                                 half_w=hw, net_name=net_name))
        except Exception:
            pass
    return segs


def _clear_of_stub_segs(px: float, py: float, r: float,
                         segs: List[StubSeg],
                         skip_ref: str = "", skip_pad: str = "",
                         excl_net: str = "",
                         clearance: float = 0.0) -> bool:
    """Return True if circle (px,py,r) clears all placed stub segments.

    r should be the via copper radius (without clearance baked in).
    seg.half_w = bare_stub_half + clearance (baked in by _stub_segs_for).
    Combined threshold = r + seg.half_w gives exactly one clearance gap
    between via copper edge and stub copper edge.

    Skips segments belonging to the same (ref, pad_num) source or same net
    as excl_net (same-net stubs require no DRC clearance).

    clearance: the value baked into seg.half_w — subtract it so the check
    doesn't double-count when the caller already included clearance in r.
    """
    for seg in segs:
        if seg.ref == skip_ref and seg.pad_num == skip_pad:
            continue
        if excl_net and seg.net_name and seg.net_name == excl_net:
            continue
        _d = _dist_to_segment(px, py, seg.x1, seg.y1, seg.x2, seg.y2)
        _thr = r + seg.half_w - clearance
        if _d < _thr:
            return False
    return True


def _dist_to_segment(px: float, py: float,
                     x1: float, y1: float, x2: float, y2: float) -> float:
    """Shortest distance from point (px,py) to finite line segment (x1,y1)-(x2,y2)."""
    dx, dy = x2 - x1, y2 - y1
    len2   = dx*dx + dy*dy
    if len2 < 1e-12:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1)*dx + (py - y1)*dy) / len2))
    return math.hypot(px - (x1 + t*dx), py - (y1 + t*dy))


def _seg_min_dist(ax1: float, ay1: float, ax2: float, ay2: float,
                  bx1: float, by1: float, bx2: float, by2: float) -> float:
    """Minimum distance between two finite line segments."""
    if _segs_intersect((ax1, ay1), (ax2, ay2), (bx1, by1), (bx2, by2)):
        return 0.0
    return min(
        _dist_to_segment(ax1, ay1, bx1, by1, bx2, by2),
        _dist_to_segment(ax2, ay2, bx1, by1, bx2, by2),
        _dist_to_segment(bx1, by1, ax1, ay1, ax2, ay2),
        _dist_to_segment(bx2, by2, ax1, ay1, ax2, ay2),
    )


def _seg_bbox_dist(ax1: float, ay1: float, ax2: float, ay2: float,
                   bbox: tuple) -> float:
    """Minimum distance from finite segment to axis-aligned rectangle.

    Returns 0 if the segment intersects or touches the rectangle.  Otherwise
    returns the shortest distance from any point on the segment to any point
    on the rectangle boundary.  Used in _stub_clear for exact rectangular pad
    clearance instead of the circular-centre approximation.
    """
    # Endpoint distances to bbox
    d = min(_dist_point_to_bbox(ax1, ay1, bbox),
            _dist_point_to_bbox(ax2, ay2, bbox))
    if d == 0.0:
        return 0.0
    # Each bbox edge as a segment — find the closest approach
    al, at, ar, ab_ = bbox
    for bx1, by1, bx2, by2 in (
            (al, at, ar, at),    # top
            (ar, at, ar, ab_),   # right
            (al, ab_, ar, ab_),  # bottom
            (al, at, al, ab_),   # left
    ):
        d = min(d, _seg_min_dist(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2))
        if d == 0.0:
            return 0.0
    return d


def _stub_clear(via: "PendingVia", corner_x: float, corner_y: float,
                obs: List[Obstacle], clearance: float,
                placed_stubs: List[StubSeg] = None,
                edge_segs: list = None) -> bool:
    """
    Return True if the stub trace (pad→corner and corner→via) clears all
    obstacles, placed stubs, and board edge cuts by at least the required margin.

    pad→corner: skips ALL same-ref obstacles — the stub exits through the
    component's own pad copper and may be adjacent to other pads of the same
    dense connector.  escape_direction() already prevents routing directly into
    neighbouring pads.

    corner→via: skips only the source pad (by position).  The corner is
    neckdown_len beyond the pad, so the extension can reach adjacent pads of
    the same connector or other pads of the same multi-pad component.  Those
    must be obstacles so the stub cannot traverse the connector body.

    Checks BOTH segments against committed placed_stubs (pad→corner AND
    corner→via) to prevent stub segments from sweeping through adjacent pads'
    escape corridors.

    Also checks both segments against board edge cuts when edge_segs is provided.
    """
    stub_hw_bare = via.neckdown_w_mm / 2.0   # half-width without clearance baked in
    half_w       = stub_hw_bare + clearance   # default half_w for stub-vs-stub checks
    bent         = abs(via.via_x - corner_x) > 1e-6 or abs(via.via_y - corner_y) > 1e-6

    # Pre-compute AABB for each stub leg so we can cheaply reject obstacles
    # that are nowhere near either leg without calling _dist_to_segment.
    # This is critical for dense connectors (SOM2 etc.) where hundreds of
    # same-component pads are near the escape corridor.
    _leg1_xlo = min(via.pad_x, corner_x)
    _leg1_xhi = max(via.pad_x, corner_x)
    _leg1_ylo = min(via.pad_y, corner_y)
    _leg1_yhi = max(via.pad_y, corner_y)
    if bent:
        _leg2_xlo = min(corner_x, via.via_x)
        _leg2_xhi = max(corner_x, via.via_x)
        _leg2_ylo = min(corner_y, via.via_y)
        _leg2_yhi = max(corner_y, via.via_y)

    for o in obs:
        # Always skip the source pad itself (same position, same ref).
        if (o.ref == via.ref
                and abs(o.cx - via.pad_x) < 1e-3
                and abs(o.cy - via.pad_y) < 1e-3):
            continue
        # Same-net pads need no DRC clearance — the stub is electrically
        # connected to them and a neighbouring same-net pad is not a violation.
        if via.net_name and o.net_name and o.net_name == via.net_name:
            continue
        # Per-pair clearance: use DRU-aware value for this stub↔pad pair.
        cl = _pair_clearance(via.net_name, via.netclass, via.ref,
                             o.net_name, o.netclass, o.ref)
        hw = stub_hw_bare + cl
        _margin = hw + o.r
        # AABB pre-reject for leg 1 (pad→corner) — far cheaper than
        # _dist_to_segment; eliminates side-adjacent pads of dense connectors.
        _in_leg1 = (o.cx >= _leg1_xlo - _margin and o.cx <= _leg1_xhi + _margin
                    and o.cy >= _leg1_ylo - _margin and o.cy <= _leg1_yhi + _margin)
        # AABB pre-reject for leg 2 (corner→via) when stub is bent.
        _in_leg2 = bent and (
            o.cx >= _leg2_xlo - _margin and o.cx <= _leg2_xhi + _margin
            and o.cy >= _leg2_ylo - _margin and o.cy <= _leg2_yhi + _margin)
        if not _in_leg1 and not _in_leg2:
            continue  # obstacle bounding box clearly misses both stub legs

        # For a straight stub (not bent), leg1 exits the connector body along
        # the escape axis and may pass within the clearance envelope of adjacent
        # same-ref pads.  Skip the DRC-clearance check for same-ref pads on
        # leg1 — but still catch actual copper overlaps (which would be shorts).
        # For bent stubs the corner can reach from unexpected directions, so
        # same-ref obstacles ARE checked with full clearance.
        if _in_leg1:
            if not bent and o.ref == via.ref:
                # Same-ref, straight stub: only fail on actual copper overlap
                # (not DRC clearance) — the stub exits through the connector body
                # and escape_direction() prevents routing into adjacent pads.
                # Correct thresholds: track_copper + pad_copper must physically
                # overlap (not just be within DRC clearance of each other).
                if o.bbox is None:
                    d1 = _dist_to_segment(o.cx, o.cy,
                                          via.pad_x, via.pad_y, corner_x, corner_y)
                    if d1 < stub_hw_bare + o.r:
                        return False
                else:
                    if _seg_bbox_dist(via.pad_x, via.pad_y,
                                      corner_x, corner_y, o.bbox) < stub_hw_bare:
                        return False
            else:
                d1 = _dist_to_segment(o.cx, o.cy,
                                      via.pad_x, via.pad_y, corner_x, corner_y)
                if d1 < _margin:
                    if o.bbox is None or _seg_bbox_dist(
                            via.pad_x, via.pad_y, corner_x, corner_y, o.bbox) < hw:
                        return False
        if _in_leg2:
            d2 = _dist_to_segment(o.cx, o.cy,
                                  corner_x, corner_y, via.via_x, via.via_y)
            if d2 < _margin:
                if o.bbox is None or _seg_bbox_dist(
                        corner_x, corner_y, via.via_x, via.via_y, o.bbox) < hw:
                    return False

    if placed_stubs:
        # Check BOTH legs of the new stub against all committed placed stubs.
        # Previously only the corner→via leg was checked, allowing the
        # pad→corner diagonal to sweep through adjacent pads' escape corridors.
        for seg in placed_stubs:
            if seg.ref == via.ref and seg.pad_num == via.pad_num:
                continue
            # Same-net stubs require no DRC clearance.
            if via.net_name and seg.net_name and seg.net_name == via.net_name:
                continue
            # Stub-to-stub clearance uses default (netclass-agnostic) rules.
            # HS fanout stubs are short F.Cu traces — the 3W stripline rule
            # applies to In2.Cu routed signals, not to F.Cu fanout stubs.
            # Passing None for netclass suppresses the HS override while
            # preserving HV/+5V net-name special clearances.
            cl_s = _pair_clearance(via.net_name, None, via.ref,
                                   seg.net_name, None, seg.ref)
            # seg.half_w = bare_half + clearance (baked in by _stub_segs_for).
            # combined_w must be bare_new + cl_s + bare_placed (one clearance
            # gap), so subtract the already-baked clearance from seg.half_w.
            combined_w = stub_hw_bare + cl_s + (seg.half_w - clearance)
            # pad→corner leg vs. committed stub.
            if math.hypot(corner_x - via.pad_x, corner_y - via.pad_y) > 1e-6:
                if _seg_min_dist(via.pad_x, via.pad_y, corner_x, corner_y,
                                 seg.x1, seg.y1, seg.x2, seg.y2) < combined_w:
                    return False
            # corner→via leg vs. committed stub (only when there is a bend).
            if bent:
                if _seg_min_dist(corner_x, corner_y, via.via_x, via.via_y,
                                 seg.x1, seg.y1, seg.x2, seg.y2) < combined_w:
                    return False

    if edge_segs:
        edge_r = half_w + _BOARD_EDGE_CLEARANCE
        if not _seg_clears_edges(via.pad_x, via.pad_y, corner_x, corner_y,
                                  edge_r, edge_segs):
            return False
        if bent and not _seg_clears_edges(corner_x, corner_y,
                                           via.via_x, via.via_y,
                                           edge_r, edge_segs):
            return False

    return True


# ---------------------------------------------------------------------------
# A* stub pathfinder
# ---------------------------------------------------------------------------

def _is_blocked_for_trace(x: float, y: float,
                           trace_hw: float, clearance: float,
                           obstacles: List[Obstacle],
                           placed_stubs: List[StubSeg],
                           edge_segs: list,
                           src_ref: str, src_pad_num: str,
                           src_pad_x: float, src_pad_y: float,
                           net_name: str, netclass: str) -> bool:
    """Return True if placing a trace centre at (x,y) violates any clearance rule."""
    for o in obstacles:
        if (o.ref == src_ref
                and abs(o.cx - src_pad_x) < 1e-3
                and abs(o.cy - src_pad_y) < 1e-3):
            continue
        if net_name and o.net_name and o.net_name == net_name:
            continue
        cl = _pair_clearance(net_name, netclass, src_ref,
                              o.net_name, o.netclass, o.ref)
        required = trace_hw + cl
        if o.bbox is not None:
            if _dist_point_to_bbox(x, y, o.bbox) < required:
                return True
        else:
            if math.hypot(x - o.cx, y - o.cy) < required + o.r:
                return True
    for seg in placed_stubs:
        if seg.ref == src_ref and seg.pad_num == src_pad_num:
            continue
        if net_name and seg.net_name and seg.net_name == net_name:
            continue
        cl_s = _pair_clearance(net_name, netclass, src_ref,
                                seg.net_name, seg.netclass, src_ref)
        seg_copper_hw = max(0.0, seg.half_w - clearance)
        required = trace_hw + cl_s + seg_copper_hw
        if _dist_to_segment(x, y, seg.x1, seg.y1, seg.x2, seg.y2) < required:
            return True
    for (ex1, ey1), (ex2, ey2) in edge_segs:
        if _pt_to_seg_dist(x, y, ex1, ey1, ex2, ey2) < trace_hw + _BOARD_EDGE_CLEARANCE:
            return True
    return False


def _segment_clears_all(x1: float, y1: float, x2: float, y2: float,
                         trace_hw: float, clearance: float,
                         obstacles: List[Obstacle],
                         placed_stubs: List[StubSeg],
                         edge_segs: list,
                         src_ref: str, src_pad_num: str,
                         src_pad_x: float, src_pad_y: float,
                         net_name: str, netclass: str) -> bool:
    """True if segment (x1,y1)→(x2,y2) of trace half-width trace_hw clears all obstacles."""
    for o in obstacles:
        if (o.ref == src_ref
                and abs(o.cx - src_pad_x) < 1e-3
                and abs(o.cy - src_pad_y) < 1e-3):
            continue
        if net_name and o.net_name and o.net_name == net_name:
            continue
        cl = _pair_clearance(net_name, netclass, src_ref,
                              o.net_name, o.netclass, o.ref)
        required = trace_hw + cl
        if o.bbox is not None:
            if _seg_bbox_dist(x1, y1, x2, y2, o.bbox) < required:
                return False
        else:
            if _dist_to_segment(o.cx, o.cy, x1, y1, x2, y2) < required + o.r:
                return False
    for seg in placed_stubs:
        if seg.ref == src_ref and seg.pad_num == src_pad_num:
            continue
        if net_name and seg.net_name and seg.net_name == net_name:
            continue
        cl_s = _pair_clearance(net_name, netclass, src_ref,
                                seg.net_name, seg.netclass, src_ref)
        seg_copper_hw = max(0.0, seg.half_w - clearance)
        required = trace_hw + cl_s + seg_copper_hw
        if _seg_min_dist(x1, y1, x2, y2, seg.x1, seg.y1, seg.x2, seg.y2) < required:
            return False
    if edge_segs:
        if not _seg_clears_edges(x1, y1, x2, y2,
                                  trace_hw + _BOARD_EDGE_CLEARANCE, edge_segs):
            return False
    return True


# ---------------------------------------------------------------------------
# Crossing detection
# ---------------------------------------------------------------------------

def _segs_intersect(p1, p2, p3, p4) -> bool:
    dx1, dy1 = p2[0] - p1[0], p2[1] - p1[1]
    dx2, dy2 = p4[0] - p3[0], p4[1] - p3[1]
    denom = dx1 * dy2 - dy1 * dx2
    if abs(denom) < 1e-9:
        return False
    t = ((p3[0] - p1[0]) * dy2 - (p3[1] - p1[1]) * dx2) / denom
    u = ((p3[0] - p1[0]) * dy1 - (p3[1] - p1[1]) * dx1) / denom
    return 0.0 < t < 1.0 and 0.0 < u < 1.0


def crossing(va: PendingVia, vb: PendingVia, clearance: float = 0.0) -> bool:
    """True if the stubs from va and vb violate the required inter-stub clearance.

    When clearance=0 (default): detects bare copper-to-copper overlap only
    (genuine short).  Used inside _try_stagger to confirm resolution.

    When clearance>0: detects DRC clearance violations — stubs within the
    required keep-away distance of each other.  Used by the crossing pass to
    flag violations that _stub_clear should have prevented during placement.

    The gap between the two stubs is: _seg_min_dist - hw_a - hw_b.
    A violation occurs when that gap < the net-pair clearance requirement.
    Uses _seg_min_dist on all segment pairs so collinear/parallel stubs are
    detected correctly.

    Note: only meaningful for different-net pairs.  Same-net stubs require no
    clearance gap and are never a short — skip those at the call site.
    """
    hw_a = va.neckdown_w_mm / 2.0
    hw_b = vb.neckdown_w_mm / 2.0
    if clearance > 0.0:
        # Net-pair clearance: use default (not HS netclass) — fanout stubs are
        # short F.Cu traces; the HS 3W rule applies to In2.Cu buried routing.
        cl = _pair_clearance(va.net_name, None, va.ref, vb.net_name, None, vb.ref)
    else:
        cl = 0.0
    combined = hw_a + cl + hw_b

    def _get_seg_coords(via: PendingVia):
        cx, cy = _via_corner(via)
        pts = [(via.pad_x, via.pad_y), (cx, cy)]
        if not (abs(via.via_x - cx) < 1e-4 and abs(via.via_y - cy) < 1e-4):
            pts.append((via.via_x, via.via_y))
        result = []
        for i in range(len(pts) - 1):
            if math.hypot(pts[i+1][0] - pts[i][0], pts[i+1][1] - pts[i][1]) > 1e-6:
                result.append((pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1]))
        return result

    segs_a = _get_seg_coords(va)
    segs_b = _get_seg_coords(vb)
    for ax1, ay1, ax2, ay2 in segs_a:
        for bx1, by1, bx2, by2 in segs_b:
            if _seg_min_dist(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2) < combined:
                return True
    return False


# ---------------------------------------------------------------------------
# Crossing resolution
# ---------------------------------------------------------------------------

def _try_stagger(mover: PendingVia, anchor: PendingVia,
                  obs: List[Obstacle], pad_obs: List[Obstacle],
                  clearance: float,
                  placed_stubs: List[StubSeg] = None,
                  edge_segs: list = None,
                  ext_via_list: List["PendingVia"] = None) -> bool:
    """
    Move mover's via until the stubs no longer cross, the via position is
    obstacle-clear, and the full stub trace clears all foreign pads and
    committed placed_stubs.

    Phase 1: push via along primary escape axis (original behaviour).
    Phase 2: apply corner lateral offsets so the pad→corner segment shifts
             sideways.  Required when the crossing is in the pad→corner segment
             itself (e.g. a wide power stub overlapping an HS stub at the
             neckdown exit), which phase 1 cannot fix because it never moves
             the corner.  For each candidate offset the via is searched in the
             five standard directions from the new corner.
    """
    placed_stubs = placed_stubs or []
    edge_segs    = edge_segs or []
    ext_via_list  = ext_via_list or []
    orig_x, orig_y   = mover.via_x, mover.via_y
    orig_offset      = mover.corner_lat_offset_mm
    edx, edy         = mover.escape_dx, mover.escape_dy
    r                = _via_r(mover, clearance)

    def _restore():
        mover.via_x, mover.via_y       = orig_x, orig_y
        mover.corner_lat_offset_mm      = orig_offset

    def _stub_clears_via_circles(nx: float, ny: float) -> bool:
        """Check mover's new stub path against all other via circles."""
        _segs = _route_45deg_stub(mover.pad_x, mover.pad_y, nx, ny, edx, edy)
        if not _segs:
            return True
        cx2, cy2 = _segs[0][2], _segs[0][3]
        bent2 = len(_segs) > 1
        stub_hw = mover.neckdown_w_mm / 2.0
        # Check obs via circles (bbox=None); obs.r already includes clearance.
        for _ob in obs:
            if _ob.bbox is not None:
                continue
            if (mover.net_name and _ob.net_name
                    and _ob.net_name == mover.net_name):
                continue
            thr = stub_hw + _ob.r
            if _dist_to_segment(_ob.cx, _ob.cy,
                                 mover.pad_x, mover.pad_y, cx2, cy2) < thr:
                return False
            if bent2 and _dist_to_segment(_ob.cx, _ob.cy,
                                           cx2, cy2, nx, ny) < thr:
                return False
        return True

    def _test(nx: float, ny: float) -> bool:
        mover.via_x, mover.via_y = nx, ny
        cx, cy = _via_corner(mover)
        if edge_segs and not _clears_edges(nx, ny, _via_r(mover, clearance + _BOARD_EDGE_CLEARANCE),
                                            edge_segs):
            return False
        _mv_copper = mover.via_drill_mm / 2.0 + mover.via_annular_mm
        return (not crossing(mover, anchor)
                and _clear_of_obs(nx, ny, r, obs, mover.ref, mover.pad_x, mover.pad_y,
                                  excl_net=mover.net_name,
                                  via_copper=_mv_copper,
                                  via_net=mover.net_name, via_netclass=mover.netclass,
                                  via_ref=mover.ref)
                and _clear_of_stub_segs(nx, ny, r, placed_stubs, mover.ref, mover.pad_num,
                                        excl_net=mover.net_name, clearance=clearance)
                and _stub_clear(mover, cx, cy, pad_obs, clearance, placed_stubs, edge_segs)
                and _stub_clears_via_circles(nx, ny))

    # Phase 1 — push via along primary escape axis.
    corner_x, corner_y = _via_corner(mover)   # fixed while offset is unchanged
    dist = mover.neckdown_len_mm + STEP_MM
    while dist <= mover.max_search_mm:
        if _test(mover.pad_x + edx * dist, mover.pad_y + edy * dist):
            return True
        dist += STEP_MM

    # Phase 2 — try corner lateral offsets.
    # Shifts the pad→corner segment sideways so it no longer overlaps the
    # anchor's stub.  For each offset the via is searched in 5 directions
    # from the new corner position.
    _spacing = mover.neckdown_w_mm + 2.0 * clearance
    perp_dx, perp_dy = -edy, edx
    s45 = 1.0 / math.sqrt(2.0)
    search_dirs = [
        (edx, edy),
        (perp_dx,  perp_dy), (-perp_dx, -perp_dy),
        (edx * s45 + perp_dx * s45, edy * s45 + perp_dy * s45),
        (edx * s45 - perp_dx * s45, edy * s45 - perp_dy * s45),
    ]
    n_offsets = max(3, int(mover.max_search_mm / _spacing) + 1)
    for sign in (+1.0, -1.0):
        for ki in range(1, n_offsets + 1):
            mover.corner_lat_offset_mm = sign * ki * _spacing
            new_cx, new_cy = _via_corner(mover)
            for sdx, sdy in search_dirs:
                ext = STEP_MM
                while ext <= mover.max_search_mm:
                    if _test(new_cx + sdx * ext, new_cy + sdy * ext):
                        return True
                    ext += STEP_MM

    _restore()
    return False


# ---------------------------------------------------------------------------
# Via-in-pad fallback
# ---------------------------------------------------------------------------

def _try_via_in_pad(via: PendingVia, min_annular_mm: float) -> bool:
    """
    Try to place via at pad centre (VIPPO). Reduces annular ring toward
    min_annular_mm if the pad is too small for the standard ring.
    """
    pad_min = min(via.pad_w_mm, via.pad_h_mm)

    if pad_min >= via.via_drill_mm + 2.0 * via.via_annular_mm:
        via.via_x, via.via_y = via.pad_x, via.pad_y
        via.via_in_pad = True
        return True

    needed = (pad_min - via.via_drill_mm) / 2.0
    if needed >= min_annular_mm:
        via.via_annular_mm   = needed
        via.via_x, via.via_y = via.pad_x, via.pad_y
        via.via_in_pad       = True
        return True

    return False


# ---------------------------------------------------------------------------
# Adjacent same-net pad clustering
# ---------------------------------------------------------------------------

def _cluster_adjacent_pads(pending: List["PendingVia"],
                            clearance: float) -> List["PendingVia"]:
    """
    Detect adjacent same-net pads on the same footprint and merge them into
    a single via placement.

    For each cluster: designate one primary PendingVia (the one whose pad is
    furthest in the escape direction — best-exit position).  Remove the rest
    from pending and record their pad centres in primary.cluster_pads so that
    _make_neckdown() can emit short lateral connector traces linking them to
    the primary's stub.

    Only connector-style pads are eligible (aspect ratio >= cluster_pad_aspect_min,
    default 2.0) to avoid clustering BGA/chip pads that happen to share a net.
    HS pads are excluded — diff-pair integrity takes priority.

    Proximity threshold: cluster_max_dist_factor (default 2.0) × the longer
    pad dimension.  Adjacent FFC/JST pins (0.3 mm wide × 1.3 mm long, 0.5 mm
    pitch) cluster easily (threshold 2.6 mm >> 0.5 mm gap) while BGA pads on
    a 0.85 mm pitch with 0.35 mm squares do not (threshold 0.7 mm < 0.85 mm).
    """
    from collections import defaultdict

    _min_aspect   = cfg.CLEARANCE_AUDIT.get("cluster_pad_aspect_min",   2.0)
    _dist_factor  = cfg.CLEARANCE_AUDIT.get("cluster_max_dist_factor",  2.0)

    # Eligible subset: non-HS, connector-style pads only.
    eligible_ids: set = set()
    for pv in pending:
        if pv.priority == PRIORITY_HS:
            continue
        pad_long  = max(pv.pad_w_mm, pv.pad_h_mm)
        pad_short = min(pv.pad_w_mm, pv.pad_h_mm)
        if pad_short < 1e-6:
            continue
        if pad_long / pad_short < _min_aspect:
            continue
        eligible_ids.add(id(pv))

    # Group eligible pads by (ref, net_name, target_layer_id).
    by_group: dict = defaultdict(list)
    for pv in pending:
        if id(pv) not in eligible_ids:
            continue
        by_group[(pv.ref, pv.net_name, pv.target_layer_id)].append(pv)

    to_suppress: set = set()

    for _key, pvs in by_group.items():
        if len(pvs) < 2:
            continue

        # Build proximity graph: two pads are adjacent if their centre-to-
        # centre distance is less than dist_factor × longer pad dimension.
        n = len(pvs)
        adj: List[List[int]] = [[] for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                threshold = _dist_factor * max(pvs[i].pad_w_mm, pvs[i].pad_h_mm,
                                               pvs[j].pad_w_mm, pvs[j].pad_h_mm)
                d = math.hypot(pvs[i].pad_x - pvs[j].pad_x,
                               pvs[i].pad_y - pvs[j].pad_y)
                if d < threshold:
                    adj[i].append(j)
                    adj[j].append(i)

        # BFS to find connected components.
        visited = [False] * n
        for start in range(n):
            if visited[start]:
                continue
            component: List[int] = []
            queue = [start]
            while queue:
                cur = queue.pop(0)
                if visited[cur]:
                    continue
                visited[cur] = True
                component.append(cur)
                for nb in adj[cur]:
                    if not visited[nb]:
                        queue.append(nb)

            if len(component) < 2:
                continue

            cluster = [pvs[i] for i in component]

            # Primary: pad furthest along its escape axis — the one with the
            # most open exit corridor relative to the footprint centre.
            primary = max(cluster,
                          key=lambda pv: (pv.pad_x * pv.escape_dx
                                          + pv.pad_y * pv.escape_dy))

            secondaries = [pv for pv in cluster if pv is not primary]

            # --- Herringbone via placement ---
            # Store ALL real pad positions (including primary's original pos)
            # before moving the primary to the centroid.
            real_pads = [(pv.pad_x, pv.pad_y, pv.neckdown_w_mm) for pv in cluster]
            primary.cluster_real_pads = real_pads

            # Perpendicular axis (rotated 90° CCW from escape).
            edx, edy = primary.escape_dx, primary.escape_dy
            pdx, pdy = -edy, edx

            # Centroid along the perpendicular axis.
            perp_coords = [px * pdx + py * pdy for px, py, _nw in real_pads]
            centroid_perp = sum(perp_coords) / len(perp_coords)
            max_lat = max(abs(pc - centroid_perp) for pc in perp_coords)

            # Move primary pad to centroid (perpendicular) while keeping its
            # along-axis coordinate fixed.
            orig_along = primary.pad_x * edx + primary.pad_y * edy
            primary.pad_x = centroid_perp * pdx + orig_along * edx
            primary.pad_y = centroid_perp * pdy + orig_along * edy

            # Extend neckdown length enough to clear the widest lateral offset.
            primary.neckdown_len_mm = max(primary.neckdown_len_mm, max_lat + 0.1)

            # Recompute via position: centroid pad + escape * neckdown_len.
            primary.via_x = primary.pad_x + edx * primary.neckdown_len_mm
            primary.via_y = primary.pad_y + edy * primary.neckdown_len_mm

            for sec in secondaries:
                to_suppress.add(id(sec))

            sec_names = ", ".join(f"{pv.ref}/{pv.pad_num}" for pv in secondaries)
            print(f"  [cluster] {primary.ref}/{primary.pad_num} "
                  f"({primary.net_name}): merged {len(secondaries)} "
                  f"adjacent pad(s): {sec_names}")

    return [pv for pv in pending if id(pv) not in to_suppress]


# ---------------------------------------------------------------------------
# Per-component group construction and placement
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Proximity-based via sharing (non-HS only)
# ---------------------------------------------------------------------------

def _pad_edge_dist(a: "PendingVia", b: "PendingVia") -> float:
    """
    Minimum edge-to-edge distance between two pads using their axis-aligned
    bounding boxes (world coordinates, populated from pad.GetBoundingBox()).
    Falls back to centroid distance if either bbox is unavailable.
    """
    if a.pad_bbox is None or b.pad_bbox is None:
        return math.hypot(a.pad_x - b.pad_x, a.pad_y - b.pad_y)
    al, at, ar, ab_ = a.pad_bbox
    bl, bt, br, bb_ = b.pad_bbox
    dx = max(0.0, max(al - br, bl - ar))
    dy = max(0.0, max(at - bb_, bt - ab_))
    return math.hypot(dx, dy)


def _can_side_exit(pv: "PendingVia", pad_obs: List[Obstacle],
                   clearance: float) -> bool:
    """
    Quick test: can this pad place a side-exit via (outside its own pad copper)
    given only pad obstacles?  Tries a few sample points along the escape axis.
    Does not consider existing tracks — this is a first-order feasibility check.
    """
    edx, edy   = pv.escape_dx, pv.escape_dy
    via_copper = pv.via_drill_mm / 2.0 + pv.via_annular_mm
    via_r_clr  = via_copper + clearance

    # Compute minimum distance from pad centre to clear the pad copper bbox.
    if pv.pad_bbox is not None:
        al, at, ar, ab_ = pv.pad_bbox
        half_ext = (max(edx, 0) * (ar - pv.pad_x)
                    + max(-edx, 0) * (pv.pad_x - al)
                    + max(edy, 0) * (ab_ - pv.pad_y)
                    + max(-edy, 0) * (pv.pad_y - at))
    else:
        half_ext = min(pv.pad_w_mm, pv.pad_h_mm) / 2.0
    min_dist = half_ext + via_copper  # must be at least this far from pad centre

    for dist in (min_dist + 0.05, min_dist + 0.3, min_dist + 0.8,
                 min_dist + 1.5, min_dist + 3.0):
        if dist > pv.max_search_mm:
            break
        vx = pv.pad_x + edx * dist
        vy = pv.pad_y + edy * dist
        # Exclude own pad from obstacle check (stub connects them).
        if _clear_of_obs(vx, vy, via_r_clr, pad_obs,
                         pv.ref, pv.pad_x, pv.pad_y,
                         excl_net=pv.net_name,
                         via_copper=via_copper,
                         via_net=pv.net_name, via_netclass=pv.netclass,
                         via_ref=pv.ref):
            return True
    return False


def _suppress_proximity_via_sharing(pending: List["PendingVia"],
                                     clearance: float,
                                     pad_obs: List[Obstacle]) -> List["PendingVia"]:
    """
    For non-HS pads: if two pending vias share the same net and copper layer
    and their pad edges are within via_share_proximity_mm of each other,
    suppress all but one.  The kept pad (primary) provides the layer-transition
    via; the router connects suppressed pads to it via short same-layer traces.

    Distance is measured edge-to-edge using pad bounding boxes from
    pad.GetBoundingBox(), so the threshold directly represents the maximum
    trace length the router needs to draw between pad edge and shared via.

    Primary selection within each connected component:
      1. Cluster primaries (cluster_pads is not None) take precedence.
      2. Otherwise: the first candidate (sorted by ascending min-clear distance)
         that passes a lightweight side-exit feasibility check using pad
         obstacles.  Falling back to the least-constrained geometry if none
         pass (should be rare).

    This resolves cases like bypass caps adjacent to their host IC, or
    bootstrap caps adjacent to the switching IC, where all pads are on the
    same layer and no layer-transition is needed at the secondary.
    """
    _threshold = cfg.CLEARANCE_AUDIT.get("via_share_proximity_mm", 5.0)

    from collections import defaultdict

    # Only non-HS pads are eligible. Sandwiched pads (between HS pairs) are excluded —
    # they get inward escape vias and must not be merged with a distant shared via.
    eligible = [pv for pv in pending if pv.priority != PRIORITY_HS and not pv.sandwiched]

    # Group by (net_name, pad_layer_id).
    by_group: dict = defaultdict(list)
    for pv in eligible:
        by_group[(pv.net_name, pv.pad_layer_id)].append(pv)

    to_suppress: set = set()

    for _key, pvs in by_group.items():
        if len(pvs) < 2:
            continue

        n = len(pvs)
        adj: List[List[int]] = [[] for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                d = _pad_edge_dist(pvs[i], pvs[j])
                if d < _threshold:
                    adj[i].append(j)
                    adj[j].append(i)

        visited = [False] * n
        for start in range(n):
            if visited[start]:
                continue
            component: List[int] = []
            queue = [start]
            while queue:
                cur = queue.pop(0)
                if visited[cur]:
                    continue
                visited[cur] = True
                component.append(cur)
                for nb in adj[cur]:
                    if not visited[nb]:
                        queue.append(nb)

            if len(component) < 2:
                continue

            cluster = [pvs[i] for i in component]

            # Sort by ascending min-clear distance (least constrained by
            # own pad first), then pick the first that can actually
            # side-exit given real pad obstacles.
            def _min_clear(pv: "PendingVia") -> float:
                if pv.pad_bbox is not None:
                    al, at, ar, ab_ = pv.pad_bbox
                    edx, edy = pv.escape_dx, pv.escape_dy
                    half_ext = (max(edx, 0) * (ar - pv.pad_x)
                                + max(-edx, 0) * (pv.pad_x - al)
                                + max(edy, 0) * (ab_ - pv.pad_y)
                                + max(-edy, 0) * (pv.pad_y - at))
                else:
                    half_ext = min(pv.pad_w_mm, pv.pad_h_mm) / 2.0
                return half_ext + pv.via_drill_mm / 2.0 + pv.via_annular_mm
            ranked = sorted(cluster, key=_min_clear)
            primary = next(
                (pv for pv in ranked if _can_side_exit(pv, pad_obs, clearance)),
                ranked[0]  # fallback: least-constrained geometry
            )

            secondaries = [pv for pv in cluster if pv is not primary]
            for sec in secondaries:
                to_suppress.add(id(sec))

            sec_names = ", ".join(f"{pv.ref}/{pv.pad_num}" for pv in secondaries)
            print(f"  [share] {primary.ref}/{primary.pad_num} "
                  f"({primary.net_name}): via shared — suppressed {sec_names}")

    return [pv for pv in pending if id(pv) not in to_suppress]


def _build_component_groups(pending: List[PendingVia]) -> List[List[PendingVia]]:
    """
    Group pending vias by footprint ref.
    Groups sorted by (min_priority, -max_pad_x): HS components first,
    rightmost components first within same priority.
    Within each group, vias sorted right-to-left (descending pad_x).
    """
    from collections import defaultdict
    by_ref: Dict[str, List[PendingVia]] = defaultdict(list)
    for via in pending:
        by_ref[via.ref].append(via)
    groups = list(by_ref.values())
    for g in groups:
        # Sort by priority first so HS vias (priority=0) are processed first
        # within each group.  Within same priority, right-to-left.
        g.sort(key=lambda v: (v.priority, -v.pad_x, v.pad_y))
    groups.sort(key=lambda g: (min(v.priority for v in g),
                                -max(v.pad_x for v in g)))
    return groups



def _place_group(group: List[PendingVia],
                 all_obs: List[Obstacle],
                 pad_obs: List[Obstacle],
                 ext_stubs: List[StubSeg],
                 clearance: float,
                 min_annular_mm: float,
                 pass_n: int,
                 edge_segs: list = None,
                 ext_placed: List[PendingVia] = None) -> Tuple[int, List[str]]:
    """
    Jointly place all vias in a single component group using systematic
    stagger assignment.

    For each escape-direction face:
      1. Sort vias by perpendicular coordinate (spread order along the face).
      2. Compute a stagger step from the via copper diameter and the minimum
         pad pitch on that face — the step is the along-axis offset needed so
         adjacent via circles just clear each other.
      3. Assign via k an initial position at corner_k + k*step along the escape
         axis, then search outward from there until a clear spot is found.

    Fallback chain per via:
      clean position → VIPPO → stub_conflict (circles only) → implicit keepout

    Vias already in a terminal state (via_in_pad, implicit_keepout) from a prior
    pass keep that state and contribute to in-group obstacles without being
    repositioned.
    """
    log: List[str] = []
    orig = [(v.via_x, v.via_y, v.via_in_pad, v.implicit_keepout)
            for v in group]

    def ig_circles_ok(vx: float, vy: float, r: float,
                      placed: List[PendingVia]) -> bool:
        # Use single clearance for in-group via circle checks, matching the
        # stagger assignment model (copper_r_new + copper_r_other + clearance).
        # r is _via_r(via, clearance) = copper_r + clearance; strip clearance out.
        _copper_r = r - clearance
        for other in placed:
            if other.implicit_keepout:
                continue
            _other_copper = other.via_drill_mm / 2.0 + other.via_annular_mm
            if math.hypot(vx - other.via_x, vy - other.via_y) < _copper_r + _other_copper + clearance:
                return False
        return True

    def _ig_stubs(placed: List[PendingVia]) -> List[StubSeg]:
        segs: List[StubSeg] = []
        for other in placed:
            segs.extend(_stub_segs_for(other, clearance))
        return segs

    def is_valid_pos(via: PendingVia, vx: float, vy: float,
                     placed: List[PendingVia]) -> bool:
        r = _via_r(via, clearance)
        # Exclude via's own pad from the circular obstacle check — the stub
        # trace connects them so no DRC clearance is required.  A separate
        # bbox check below enforces that the via copper does not overlap the
        # source pad copper (which would make it an unintended via-in-pad).
        _via_copper = via.via_drill_mm / 2.0 + via.via_annular_mm
        if not _clear_of_obs(vx, vy, r, all_obs,
                              via.ref, via.pad_x, via.pad_y,
                              excl_net=via.net_name,
                              via_copper=_via_copper,
                              via_net=via.net_name, via_netclass=via.netclass,
                              via_ref=via.ref):
            return False
        # Via copper (drill + annular) must not overlap source pad copper.
        # No DRC gap needed (same net), but physical overlap = via-in-pad.
        if via.pad_bbox is not None:
            if _dist_point_to_bbox(vx, vy, via.pad_bbox) < (
                    via.via_drill_mm / 2.0 + via.via_annular_mm):
                return False
        if not ig_circles_ok(vx, vy, r, placed):
            return False
        # Board edge clearance for via copper.
        if edge_segs and not _clears_edges(
                vx, vy, r + _BOARD_EDGE_CLEARANCE, edge_segs):
            return False
        all_stubs = ext_stubs + _ig_stubs(placed)
        if not _clear_of_stub_segs(vx, vy, r, all_stubs, via.ref, via.pad_num,
                                    excl_net=via.net_name, clearance=clearance):
            return False
        via.via_x, via.via_y = vx, vy
        # Use the actual 45° stub waypoint — _via_corner gives the old
        # corner-based path which may differ from what _make_neckdown draws.
        _segs45 = _route_45deg_stub(via.pad_x, via.pad_y, vx, vy,
                                     via.escape_dx, via.escape_dy)
        cx, cy = (_segs45[0][2], _segs45[0][3]) if _segs45 else (vx, vy)
        if not _stub_clear(via, cx, cy, pad_obs, clearance, all_stubs, edge_segs):
            return False
        # Check BOTH stub segments against in-group placed via circles.
        # _stub_clear checks stub segments vs pad obstacles and stub segments,
        # but not against via copper circles of already-placed vias.  This is
        # the dual check: new stub segment must clear all placed via copper.
        stub_hw_bare = via.neckdown_w_mm / 2.0
        bent = len(_segs45) > 1
        # In-group placed via circles + any ext_placed (prior-group vias).
        _all_placed_for_stub_chk = placed + (ext_placed or [])
        for other in _all_placed_for_stub_chk:
            if other.implicit_keepout:
                continue
            if via.net_name and other.net_name and other.net_name == via.net_name:
                continue
            # Stub-to-via-copper: always use the board minimum clearance.
            # Tight-pitch reduced values are only for via-circle-to-via-circle
            # spacing; a stub trace must still maintain the full DRC clearance
            # from any adjacent via copper regardless of component pitch.
            _ocl = clearance
            other_copper = other.via_drill_mm / 2.0 + other.via_annular_mm
            threshold = stub_hw_bare + _ocl + other_copper
            # pad→waypoint leg vs placed via circle
            _d1 = _dist_to_segment(other.via_x, other.via_y,
                                 via.pad_x, via.pad_y, cx, cy)
            if _d1 < threshold:
                return False
            # waypoint→via leg vs placed via circle (only when stub is bent)
            if bent and _dist_to_segment(other.via_x, other.via_y,
                                          cx, cy, vx, vy) < threshold:
                return False
        # Cross-group via circles: all_obs entries with bbox=None are via circles
        # (other_obs constructed from pending vias).  obs.r = copper_r + clearance,
        # so threshold = stub_hw_bare + obs.r covers the full DRC requirement.
        # This catches same-group-but-processed-later vias (e.g. a connector that
        # has both HS and power pads: the HS pads are placed first in all_obs via
        # their CURRENT pending positions before the power pad is evaluated).
        for obs in all_obs:
            if obs.bbox is not None:  # pad obstacle — skip (handled by _stub_clear)
                continue
            if via.net_name and obs.net_name and obs.net_name == via.net_name:
                continue
            threshold = stub_hw_bare + obs.r
            if _dist_to_segment(obs.cx, obs.cy,
                                 via.pad_x, via.pad_y, cx, cy) < threshold:
                return False
            if bent and _dist_to_segment(obs.cx, obs.cy,
                                          cx, cy, vx, vy) < threshold:
                return False
        return True

    def is_valid_pos_no_stubs(via: PendingVia, vx: float, vy: float,
                               placed: List[PendingVia]) -> bool:
        """Relaxed check: via circles only, no stub-segment clearance."""
        r = _via_r(via, clearance)
        _via_copper2 = via.via_drill_mm / 2.0 + via.via_annular_mm
        if not (_clear_of_obs(vx, vy, r, all_obs,
                               via.ref, via.pad_x, via.pad_y,
                               excl_net=via.net_name,
                               via_copper=_via_copper2,
                               via_net=via.net_name, via_netclass=via.netclass,
                               via_ref=via.ref) and
                ig_circles_ok(vx, vy, r, placed)):
            return False
        if via.pad_bbox is not None:
            if _dist_point_to_bbox(vx, vy, via.pad_bbox) < (
                    via.via_drill_mm / 2.0 + via.via_annular_mm):
                return False
        if edge_segs and not _clears_edges(
                vx, vy, r + _BOARD_EDGE_CLEARANCE, edge_segs):
            return False
        return True

    # ------------------------------------------------------------------ #
    # Systematic stagger assignment                                        #
    # ------------------------------------------------------------------ #
    from collections import defaultdict
    face_map = defaultdict(list)
    for via in group:
        key = (round(via.escape_dx, 3), round(via.escape_dy, 3))
        face_map[key].append(via)

    placed: List[PendingVia] = []

    for (edx, edy), face_vias in face_map.items():
        perp_dx, perp_dy = -edy, edx
        face_vias.sort(key=lambda v: v.pad_x * perp_dx + v.pad_y * perp_dy)

        s45 = 1.0 / math.sqrt(2.0)

        # Most-constrained-first: only on pass 1 — subsequent passes re-verify
        # existing positions (most already clear) so the sort overhead is wasted.
        if pass_n == 1:
            def _candidate_count(v):
                _cx, _cy = _via_corner(v)
                _pdx2, _pdy2 = -v.escape_dy, v.escape_dx
                _dirs = [
                    (v.escape_dx, v.escape_dy),
                    (v.escape_dx * s45 + _pdx2 * s45, v.escape_dy * s45 + _pdy2 * s45),
                    (v.escape_dx * s45 - _pdx2 * s45, v.escape_dy * s45 - _pdy2 * s45),
                    (_pdx2, _pdy2), (-_pdx2, -_pdy2),
                ]
                _cnt = 0
                for _tdx, _tdy in _dirs:
                    _ext = 0.0
                    while _ext <= v.max_search_mm:
                        _vx = _cx + _tdx * _ext
                        _vy = _cy + _tdy * _ext
                        if is_valid_pos_no_stubs(v, _vx, _vy, []):
                            _cnt += 1
                            break
                        _ext += STEP_MM
                return _cnt
            face_vias.sort(key=_candidate_count)

        # P-before-N: HS P vias with a pre-assigned lateral offset must be placed
        # before their N partner so step 1e can find the partner in `placed`.
        face_vias.sort(key=lambda v: (
            0 if (v.priority == PRIORITY_HS
                  and abs(v.corner_lat_offset_mm) > 1e-6) else 1
        ))

        # Multi-pad faces: stubs must remain parallel (primary escape direction
        # only) so they never cross each other within the face.  Alt-direction
        # search is reserved for isolated (single-pad) faces.
        multi_pad_face = len(face_vias) > 1

        # Pads that share a perpendicular coordinate cannot be spread along the
        # primary escape axis — their stubs are collinear and will always overlap.
        # Allow alt-direction (perpendicular offset) for these pads specifically.
        # Threshold 0.05mm: also catches connectors rotated near-vertical where
        # adjacent pads appear nearly same-column (e.g. Δlat=0.031mm at J_DSI1).
        _PERP_TOL = 0.05  # mm
        _perp_coords = [v.pad_x * perp_dx + v.pad_y * perp_dy for v in face_vias]
        same_perp_ids: set = set()
        for _i in range(len(face_vias)):
            for _j in range(_i + 1, len(face_vias)):
                # Corner-offset pads already have separated corners — they don't
                # need prefer_perp treatment on top of that.
                if (abs(_perp_coords[_i] - _perp_coords[_j]) < _PERP_TOL
                        and abs(face_vias[_i].corner_lat_offset_mm) < 1e-6
                        and abs(face_vias[_j].corner_lat_offset_mm) < 1e-6):
                    same_perp_ids.add(id(face_vias[_i]))
                    same_perp_ids.add(id(face_vias[_j]))

        for k, via in enumerate(face_vias):
            if via.via_in_pad or via.implicit_keepout:
                placed.append(via)
                continue

            # Same-perp-coord pads share an escape column — they need a
            # perpendicular offset, so alt-direction is allowed for them.
            allow_alt = not multi_pad_face or id(via) in same_perp_ids
            # Same-perp pads try alt-direction FIRST so the first pad claims a
            # perpendicular column and leaves the shared primary column clear for
            # the next pad.  Primary axis is available as a last resort.
            prefer_perp = id(via) in same_perp_ids

            corner_x, corner_y = _via_corner(via)
            # Always start from corner (0 extension). Via-circle clearance in
            # is_valid_pos pushes each via only as far as needed to clear the
            # ones already placed — keeping all stubs as short as possible.
            stagger_dist = 0.0

            save = (via.via_x, via.via_y, via.via_annular_mm,
                    via.via_in_pad, via.implicit_keepout, via.stub_conflict,
                    via.corner_lat_offset_mm)

            def restore(s=save):
                (via.via_x, via.via_y, via.via_annular_mm,
                 via.via_in_pad, via.implicit_keepout, via.stub_conflict,
                 via.corner_lat_offset_mm) = s

            def _clean_place(vx_, vy_):
                via.via_x, via.via_y = vx_, vy_
                via.via_in_pad = False
                via.implicit_keepout = False
                via.stub_conflict = False
                via.warning = ""

            # 1. Clean position (primary axis) — skipped for same-perp pads
            #    which try alt-direction first (step 1b) to keep the shared
            #    column free for the subsequent pads in the group.
            found_clean = False
            if not prefer_perp:
                ext = stagger_dist
                while ext <= via.max_search_mm:
                    vx = corner_x + edx * ext
                    vy = corner_y + edy * ext
                    if is_valid_pos(via, vx, vy, placed):
                        _clean_place(vx, vy)
                        found_clean = True
                        break
                    ext += STEP_MM

            # 1b. Alt-direction ±45° / ±90°.
            #     Same-perp pads: run before primary (prefer_perp).
            #     Single-pad faces: run after primary only if primary failed.
            #     Other multi-pad-face pads: skipped (allow_alt=False).
            if not found_clean and allow_alt:
                perp_dx_v, perp_dy_v = -edy, edx
                if prefer_perp:
                    # Horizontal escape first — gives shorter, cleaner stubs than
                    # diagonal when the primary column is shared with another pad.
                    alt_dirs = [
                        (perp_dx_v,  perp_dy_v),
                        (-perp_dx_v, -perp_dy_v),
                        (edx * s45 + perp_dx_v * s45, edy * s45 + perp_dy_v * s45),
                        (edx * s45 - perp_dx_v * s45, edy * s45 - perp_dy_v * s45),
                    ]
                else:
                    alt_dirs = [
                        (edx * s45 + perp_dx_v * s45, edy * s45 + perp_dy_v * s45),
                        (edx * s45 - perp_dx_v * s45, edy * s45 - perp_dy_v * s45),
                        (perp_dx_v,  perp_dy_v),
                        (-perp_dx_v, -perp_dy_v),
                    ]
                for adx, ady in alt_dirs:
                    ext = STEP_MM
                    while ext <= via.max_search_mm:
                        vx = corner_x + adx * ext
                        vy = corner_y + ady * ext
                        if is_valid_pos(via, vx, vy, placed):
                            _clean_place(vx, vy)
                            found_clean = True
                            break
                        ext += STEP_MM
                    if found_clean:
                        break

            # 1c. Same-perp pads: primary axis as last resort if alt-direction
            #     found nothing (e.g., obstacles block all perpendicular exits).
            if not found_clean and prefer_perp:
                ext = stagger_dist
                while ext <= via.max_search_mm:
                    vx = corner_x + edx * ext
                    vy = corner_y + edy * ext
                    if is_valid_pos(via, vx, vy, placed):
                        _clean_place(vx, vy)
                        found_clean = True
                        break
                    ext += STEP_MM

            # 1d. Multi-pad-face fallback: alt-direction when primary axis fails
            #     completely.  Applies only to multi-pad-face pads that are not
            #     same-perp (those already ran step 1b).  The primary direction
            #     is blocked end-to-end (typically a foreign pad obstacle sweeps
            #     the full escape corridor).  An L-shaped stub via a perpendicular
            #     or diagonal corner exit clears the obstacle.
            #     is_valid_pos still enforces intra-group stub clearance so no
            #     within-face crossing can be introduced.
            #
            #     Extra guard: _stub_clear normally skips all same-ref pad
            #     obstacles (the stub exits through its own pad copper, so adjacent
            #     pads of the same footprint cannot block the exit).  For the
            #     corner→via segment in this fallback, the extension can reach a
            #     completely different part of a large connector or component — a
            #     connector pad traversal is a real DRC clearance violation.
            #     We therefore require that the bent corner→via segment clears all
            #     same-ref pads (except the source pad) before accepting the
            #     alt-direction position.
            if not found_clean and multi_pad_face and not id(via) in same_perp_ids:
                perp_dx_v, perp_dy_v = -edy, edx
                fallback_dirs = [
                    (edx * s45 + perp_dx_v * s45, edy * s45 + perp_dy_v * s45),
                    (edx * s45 - perp_dx_v * s45, edy * s45 - perp_dy_v * s45),
                    (perp_dx_v,  perp_dy_v),
                    (-perp_dx_v, -perp_dy_v),
                ]
                half_w_1d = via.neckdown_w_mm / 2.0 + clearance
                for adx, ady in fallback_dirs:
                    ext = STEP_MM
                    while ext <= via.max_search_mm:
                        vx = corner_x + adx * ext
                        vy = corner_y + ady * ext
                        if not is_valid_pos(via, vx, vy, placed):
                            ext += STEP_MM
                            continue
                        # Guard: corner→via must clear same-ref pads (other than
                        # source) — _stub_clear skips them for normal exits but
                        # the alt-direction stub can traverse a connector body.
                        # Use the actual _route_45deg_stub waypoint, not the
                        # stagger-offset corner, which may lie in a blocked column.
                        _segs_1d = _route_45deg_stub(
                            via.pad_x, via.pad_y, vx, vy,
                            via.escape_dx, via.escape_dy)
                        cx_1d, cy_1d = ((_segs_1d[0][2], _segs_1d[0][3])
                                        if len(_segs_1d) > 1 else (vx, vy))
                        _half_bare_1d = via.neckdown_w_mm / 2.0
                        same_ref_ok = True
                        for _o1d in pad_obs:
                            if _o1d.ref != via.ref:
                                continue
                            if (abs(_o1d.cx - via.pad_x) < 1e-3
                                    and abs(_o1d.cy - via.pad_y) < 1e-3):
                                continue
                            if (via.net_name and _o1d.net_name
                                    and _o1d.net_name == via.net_name):
                                continue
                            _cl_1d = _pair_clearance(via.net_name, via.netclass, via.ref,
                                                     _o1d.net_name, _o1d.netclass, _o1d.ref)
                            if _o1d.bbox is not None:
                                if _seg_bbox_dist(cx_1d, cy_1d, vx, vy, _o1d.bbox) < _half_bare_1d + _cl_1d:
                                    same_ref_ok = False
                                    break
                            else:
                                if _dist_to_segment(_o1d.cx, _o1d.cy, cx_1d, cy_1d, vx, vy) < _half_bare_1d + _cl_1d + _o1d.r:
                                    same_ref_ok = False
                                    break
                        if not same_ref_ok:
                            ext += STEP_MM
                            continue
                        _clean_place(vx, vy)
                        found_clean = True
                        break
                    if found_clean:
                        break

            if found_clean:
                placed.append(via)
                continue

            # 1e. HS Axial Extension — for HS pair members whose P-side partner
            #     is already placed with enough lateral offset to allow a straight
            #     N stub to pass beside the P via.
            #
            #     At 0.400mm pad pitch, stub_hw + clearance + via_r = 0.525mm
            #     exceeds the pad pitch, so no lateral stagger can work (any
            #     diagonal crosses adjacent pad copper).  Instead: keep lat_off=0
            #     (straight stub along escape axis) and extend the stub axially
            #     until the via circle clears the partner via circle.
            #
            #     Conditions to fire:
            #       1. This is an HS priority via.
            #       2. Its P/N partner is already in `placed`.
            #       3. The partner's horizontal separation from this pad's escape
            #          column >= stub_hw + clearance + via_r (c7 threshold).
            #          If this fails the partner didn't get the required offset and
            #          axial extension can't help — fall through to 1f.
            #
            #     Search starts at: partner_axial + sqrt(via_pair_req² - horiz_sep²)
            #     where via_pair_req = via_r_this + clearance + via_r_partner.
            if not found_clean and via.priority == PRIORITY_HS:
                _partner_1e = None
                _my_net_1e = via.net_name
                if _my_net_1e.endswith('_N'):
                    _partner_net_1e = _my_net_1e[:-2] + '_P'
                elif _my_net_1e.endswith('_P'):
                    _partner_net_1e = _my_net_1e[:-2] + '_N'
                elif _my_net_1e.endswith('-'):
                    _partner_net_1e = _my_net_1e[:-1] + '+'
                elif _my_net_1e.endswith('+'):
                    _partner_net_1e = _my_net_1e[:-1] + '-'
                else:
                    _partner_net_1e = None

                if _partner_net_1e:
                    _all_for_1e = placed + (ext_placed or [])
                    for _cand_1e in _all_for_1e:
                        if (_cand_1e.ref == via.ref
                                and _cand_1e.net_name == _partner_net_1e
                                and not _cand_1e.implicit_keepout):
                            _partner_1e = _cand_1e
                            break

                if _partner_1e is not None:
                    # Perpendicular unit vector (lateral axis in board coords).
                    # _via_corner: cx = pad_x + edx*nl - edy*lat_off
                    # Lateral board direction = (-edy, edx).
                    _perp_dx_1e, _perp_dy_1e = -edy, edx
                    _horiz_sep_1e = abs(
                        (_partner_1e.via_x - via.pad_x) * _perp_dx_1e +
                        (_partner_1e.via_y - via.pad_y) * _perp_dy_1e
                    )
                    _via_r_this_1e    = (via.via_drill_mm / 2.0
                                         + via.via_annular_mm)
                    _via_r_partner_1e = (_partner_1e.via_drill_mm / 2.0
                                         + _partner_1e.via_annular_mm)
                    _stub_hw_1e  = via.neckdown_w_mm / 2.0
                    _c7_thr_1e   = _stub_hw_1e + clearance + _via_r_partner_1e

                    if _horiz_sep_1e >= _c7_thr_1e - 1e-6:
                        # Partner has sufficient lateral offset.  Compute
                        # minimum axial distance from partner via needed for
                        # via-circle-to-via-circle clearance (c3).
                        _via_pair_req_1e = (_via_r_this_1e + clearance
                                            + _via_r_partner_1e)
                        _axial_needed_1e = math.sqrt(
                            max(0.0, _via_pair_req_1e**2 - _horiz_sep_1e**2))
                        # Partner axial distance from this pad's origin.
                        _partner_axial_1e = (
                            (_partner_1e.via_x - via.pad_x) * edx +
                            (_partner_1e.via_y - via.pad_y) * edy
                        )
                        _start_1e = max(via.neckdown_len_mm,
                                        _partner_axial_1e + _axial_needed_1e
                                        + 0.005)

                        _orig_lat_1e = via.corner_lat_offset_mm
                        via.corner_lat_offset_mm = 0.0  # straight stub

                        _ext_1e = _start_1e
                        while _ext_1e <= via.max_search_mm:
                            _vx_1e = via.pad_x + edx * _ext_1e
                            _vy_1e = via.pad_y + edy * _ext_1e
                            if is_valid_pos(via, _vx_1e, _vy_1e, placed):
                                via.neckdown_len_mm = _ext_1e
                                _clean_place(_vx_1e, _vy_1e)
                                found_clean = True
                                log.append(
                                    f"  [axial-ext] {via.ref}/{via.pad_num}"
                                    f" ({via.net_name}): neckdown"
                                    f"→{_ext_1e:.3f}mm axial of"
                                    f" {_partner_1e.net_name}")
                                break
                            _ext_1e += STEP_MM

                        if not found_clean:
                            via.corner_lat_offset_mm = _orig_lat_1e

            # 1f. Short-stub fallback: the stagger may have pushed the corner
            #     past the only valid window for this via (e.g. a board obstacle
            #     blocks the far side of the escape corridor while positions
            #     closer to the pad are clear).  Search from STEP_MM out to just
            #     below the stagger corner along the primary escape axis.  A
            #     position found here produces a straight single-segment stub;
            #     update neckdown_len_mm so the corner matches the via.
            if not found_clean:
                _nl_orig_1f = via.neckdown_len_mm
                _max_inner_1f = _nl_orig_1f - STEP_MM
                if _max_inner_1f > STEP_MM:
                    ext = STEP_MM
                    while ext <= _max_inner_1f:
                        vx = via.pad_x + edx * ext
                        vy = via.pad_y + edy * ext
                        if is_valid_pos(via, vx, vy, placed):
                            via.neckdown_len_mm = ext
                            _clean_place(vx, vy)
                            found_clean = True
                            break
                        ext += STEP_MM
                    if not found_clean:
                        via.neckdown_len_mm = _nl_orig_1f

            # 1g. Fine-step primary axis + lateral-from-pad fallback.
            #
            # Fine step: repeats the step-1f range at 0.005mm resolution to
            # catch narrow valid windows that the 0.050mm coarse step skips.
            # (A 7µm-wide window between c3 and c7 constraints is an example
            # that falls through step 1f entirely.)
            #
            # Lateral: searches perpendicular to the escape direction starting
            # from the PAD position itself (not the stagger corner), producing a
            # single straight lateral stub.  Solves cases where the primary
            # escape corridor is fully blocked but the side direction is open
            # (e.g. BtB connector pad whose north escape is blocked by already-
            # placed row-mate vias, but the east/west direction is clear).
            if not found_clean:
                _FINE_STEP_1G = 0.005
                _nl_orig_1g = via.neckdown_len_mm

                _max_fine_1g = _nl_orig_1g - _FINE_STEP_1G
                if _max_fine_1g > _FINE_STEP_1G:
                    ext = _FINE_STEP_1G
                    while ext <= _max_fine_1g:
                        vx = via.pad_x + edx * ext
                        vy = via.pad_y + edy * ext
                        if is_valid_pos(via, vx, vy, placed):
                            via.neckdown_len_mm = ext
                            _clean_place(vx, vy)
                            found_clean = True
                            break
                        ext += _FINE_STEP_1G
                    if not found_clean:
                        via.neckdown_len_mm = _nl_orig_1g

                if not found_clean:
                    # Cap lateral search to neckdown_len_mm — a longer stub
                    # would cross into other components' escape corridors and
                    # create downstream placement regressions.
                    _lat_max_1g = _nl_orig_1g
                    _perp_dx_1g, _perp_dy_1g = -edy, edx
                    for _lat_sign in (1, -1):
                        _ldx_1g = _perp_dx_1g * _lat_sign
                        _ldy_1g = _perp_dy_1g * _lat_sign
                        ext = STEP_MM
                        while ext <= _lat_max_1g:
                            vx = via.pad_x + _ldx_1g * ext
                            vy = via.pad_y + _ldy_1g * ext
                            if is_valid_pos(via, vx, vy, placed):
                                _clean_place(vx, vy)
                                found_clean = True
                                break
                            ext += STEP_MM
                        if found_clean:
                            break

            if found_clean:
                placed.append(via)
                continue

            # 2. VIPPO
            restore()
            if _try_via_in_pad(via, min_annular_mm):
                via.warning = ""
                placed.append(via)
                continue

            # 3. Stub-conflict: via circles only.  Multi-pad faces use primary
            #    direction only; isolated/same-perp pads also try alt directions.
            #
            #    Guard: the pad→corner segment must still clear all same-ref pads
            #    (other than the source pad).  A permanent pad→corner violation
            #    means the corner itself is invalid (e.g. cross-fix sweeps through
            #    another pad on the same IC).  In that case a stub-conflict via
            #    would short two nets — demote to implicit keepout instead.
            restore()
            _sc_corner_x, _sc_corner_y = _via_corner(via)
            _sc_hw = via.neckdown_w_mm / 2.0 + clearance
            _pad_corner_ok = all(
                (_dist_to_segment(o.cx, o.cy,
                                  via.pad_x, via.pad_y,
                                  _sc_corner_x, _sc_corner_y) >= _sc_hw + o.r
                 or (o.bbox is not None
                     and _seg_bbox_dist(via.pad_x, via.pad_y,
                                        _sc_corner_x, _sc_corner_y, o.bbox) >= _sc_hw))
                for o in pad_obs
                if not (o.ref == via.ref
                        and abs(o.cx - via.pad_x) < 1e-3
                        and abs(o.cy - via.pad_y) < 1e-3)
                and not (via.net_name and o.net_name and o.net_name == via.net_name)
            )
            if _pad_corner_ok:
                sc_dirs: List[Tuple[float, float]] = [(edx, edy)]
                if allow_alt:
                    perp_dx_v, perp_dy_v = -edy, edx
                    sc_dirs += [
                        (edx * s45 + (-edy) * s45, edy * s45 + edx * s45),
                        (edx * s45 - (-edy) * s45, edy * s45 - edx * s45),
                        (-edy, edx), (edy, -edx),
                    ]
                for adx, ady in sc_dirs:
                    ext = stagger_dist if (adx == edx and ady == edy) else STEP_MM
                    while ext <= via.max_search_mm:
                        vx = corner_x + adx * ext
                        vy = corner_y + ady * ext
                        if not is_valid_pos_no_stubs(via, vx, vy, placed):
                            ext += STEP_MM
                            continue
                        via.via_x, via.via_y = vx, vy
                        # Guard: stub-conflict via must not cause bare copper
                        # overlap with any already-placed in-group stub — that
                        # would be a physical short, worse than implicit keepout.
                        _sc_no_cross = not any(
                            crossing(via, _op)
                            for _op in placed
                            if not _op.implicit_keepout
                            and not (via.net_name and _op.net_name
                                     and via.net_name == _op.net_name)
                        )
                        if not _sc_no_cross:
                            ext += STEP_MM
                            continue
                        # Stub-to-placed-via-circle clearance guard.
                        # is_valid_pos_no_stubs skips stub checks; compensate here
                        # so a stub-conflict via still honours DRC clearance against
                        # already-placed via copper.
                        _segs_sc = _route_45deg_stub(
                            via.pad_x, via.pad_y, vx, vy,
                            via.escape_dx, via.escape_dy)
                        _stub_hw_sc = via.neckdown_w_mm / 2.0
                        _bent_sc = len(_segs_sc) > 1
                        _cx_sc = (_segs_sc[0][2], _segs_sc[0][3]) if _bent_sc else (vx, vy)
                        _sc_stub_clears = True
                        for _oth in placed:
                            if _oth.implicit_keepout:
                                continue
                            if (via.net_name and _oth.net_name
                                    and _oth.net_name == via.net_name):
                                continue
                            _ocl2 = clearance
                            _oth_r = _oth.via_drill_mm / 2.0 + _oth.via_annular_mm
                            _thr2 = _stub_hw_sc + _ocl2 + _oth_r
                            if _dist_to_segment(
                                    _oth.via_x, _oth.via_y,
                                    via.pad_x, via.pad_y, _cx_sc[0], _cx_sc[1]) < _thr2:
                                _sc_stub_clears = False
                                break
                            if _bent_sc and _dist_to_segment(
                                    _oth.via_x, _oth.via_y,
                                    _cx_sc[0], _cx_sc[1], vx, vy) < _thr2:
                                _sc_stub_clears = False
                                break
                        if _sc_stub_clears:
                            for _obs_sc in all_obs:
                                if _obs_sc.bbox is not None:
                                    continue
                                if (via.net_name and _obs_sc.net_name
                                        and _obs_sc.net_name == via.net_name):
                                    continue
                                _thr_sc = _stub_hw_sc + _obs_sc.r
                                if _dist_to_segment(
                                        _obs_sc.cx, _obs_sc.cy,
                                        via.pad_x, via.pad_y,
                                        _cx_sc[0], _cx_sc[1]) < _thr_sc:
                                    _sc_stub_clears = False
                                    break
                                if _bent_sc and _dist_to_segment(
                                        _obs_sc.cx, _obs_sc.cy,
                                        _cx_sc[0], _cx_sc[1], vx, vy) < _thr_sc:
                                    _sc_stub_clears = False
                                    break
                        # Guard: new via copper must not land on any placed stub.
                        if _sc_stub_clears:
                            _via_cu_sc = via.via_drill_mm / 2.0 + via.via_annular_mm
                            for _pseg in _ig_stubs(placed):
                                if (_pseg.ref == via.ref
                                        and _pseg.pad_num == via.pad_num):
                                    continue
                                if (via.net_name and _pseg.net_name
                                        and _pseg.net_name == via.net_name):
                                    continue
                                _pseg_cu = max(0.0, _pseg.half_w - clearance)
                                _vthr = _via_cu_sc + clearance + _pseg_cu
                                if _dist_to_segment(
                                        vx, vy,
                                        _pseg.x1, _pseg.y1,
                                        _pseg.x2, _pseg.y2) < _vthr:
                                    _sc_stub_clears = False
                                    break
                        # Guard: stub must clear pad obstacles (bbox pads).
                        # The all_obs loop above skips bbox items; check them here.
                        if _sc_stub_clears:
                            if not _stub_clear(via, _cx_sc[0], _cx_sc[1],
                                               pad_obs, clearance, [], edge_segs):
                                _sc_stub_clears = False
                        if not _sc_stub_clears:
                            ext += STEP_MM
                            continue
                        via.via_in_pad = False
                        via.implicit_keepout = False
                        via.stub_conflict = True
                        via.warning = "stub clearance violation — inspect manually"
                        found_clean = True
                        break
                    if found_clean:
                        break

            if found_clean:
                placed.append(via)
                continue

            # 4. Implicit keepout
            restore()
            via.implicit_keepout = True
            placed.append(via)

    moved = 0
    for via, (ox, oy, oip, oik) in zip(group, orig):
        changed = (abs(via.via_x - ox) > 1e-4 or abs(via.via_y - oy) > 1e-4
                   or via.via_in_pad != oip or via.implicit_keepout != oik)
        if not changed:
            continue
        moved += 1
        if via.implicit_keepout:
            log.append(
                f"  pass {pass_n}: {via.ref}/{via.pad_num} ({via.net_name}) "
                f"→ implicit keepout  "
                f"(pad {via.pad_w_mm:.3f}×{via.pad_h_mm:.3f}mm, "
                f"min via copper ⌀{via.via_drill_mm + 2*min_annular_mm:.3f}mm)")
        elif via.via_in_pad:
            log.append(
                f"  pass {pass_n}: {via.ref}/{via.pad_num} ({via.net_name}) "
                f"→ via-in-pad ({via.via_x:.3f}, {via.via_y:.3f})  "
                f"[VIPPO annular={via.via_annular_mm:.3f}mm]")
        else:
            log.append(
                f"  pass {pass_n}: {via.ref}/{via.pad_num} ({via.net_name}) "
                f"→ ({via.via_x:.3f}, {via.via_y:.3f})")
    return moved, log


# ---------------------------------------------------------------------------
# Multi-pass loop
# ---------------------------------------------------------------------------

def run_passes(pending: List[PendingVia],
               pad_obs: List[Obstacle],
               clearance: float,
               min_annular_mm: float,
               max_passes: int,
               edge_segs: list = None,
               board_stubs: List[StubSeg] = None) -> List[str]:
    log: List[str] = []
    edge_segs   = edge_segs   or []
    board_stubs = board_stubs or []
    groups = _build_component_groups(pending)

    for pass_n in range(1, max_passes + 1):
        moved = 0
        placed_stubs: List[StubSeg] = list(board_stubs)
        all_placed_vias: List[PendingVia] = []

        # — Per-component joint placement pass —
        for group in groups:
            ref = group[0].ref
            print(f"  [place] pass {pass_n} {ref}: {len(group)} via(s)", flush=True)
            other_obs = [
                Obstacle(v.via_x, v.via_y, _via_r(v, clearance),
                         net_name=v.net_name)
                for v in pending
                if v.ref != ref and not v.implicit_keepout
            ]
            all_obs = pad_obs + other_obs
            gm, glog = _place_group(
                group, all_obs, pad_obs, placed_stubs,
                clearance, min_annular_mm, pass_n,
                edge_segs=edge_segs,
                ext_placed=all_placed_vias,
            )
            moved += gm
            log.extend(glog)
            for via in group:
                placed_stubs.extend(_stub_segs_for(via, clearance))
                if not via.implicit_keepout:
                    all_placed_vias.append(via)

        # — Crossing pass: detect and resolve stub short-circuit violations —
        # Checks ALL placed-via pairs for bare copper-to-copper overlap between
        # stub traces.  Same-net pairs are skipped (no short possible).
        #
        # Cross-component pairs: attempt stagger resolution (mover is displaced
        # until crossing() returns False).  Resolving these does not conflict
        # with _place_group because each component's vias are independent.
        #
        # Same-component pairs: log WARNING only — no stagger attempted.
        # Staggering would be undone by _place_group on the next pass, causing
        # an infinite oscillation.  Same-component stub geometry is owned by
        # _place_group; a reported WARNING here means _place_group's within-
        # group stub checks missed a case that needs a geometry fix.
        _n_active = sum(1 for v in pending if not v.implicit_keepout)
        print(f"  [crossing-pass] pass {pass_n}: checking {_n_active} active vias "
              f"({_n_active * (_n_active - 1) // 2} pairs)", flush=True)
        _n_cross = 0
        for i in range(len(pending)):
            if pending[i].implicit_keepout:
                continue
            for j in range(i + 1, len(pending)):
                if pending[j].implicit_keepout:
                    continue
                va, vb = pending[i], pending[j]
                # Same-net stubs need no clearance gap — never a short.
                if va.net_name and vb.net_name and va.net_name == vb.net_name:
                    continue
                if not crossing(va, vb):
                    continue
                _n_cross += 1
                print(f"  [crossing] {va.ref}/{va.pad_num} ({va.net_name}) ↔ "
                      f"{vb.ref}/{vb.pad_num} ({vb.net_name})", flush=True)
                log.append(
                    f"  pass {pass_n}: crossing "
                    f"{va.ref}/{va.pad_num} ({va.net_name}) ↔ "
                    f"{vb.ref}/{vb.pad_num} ({vb.net_name})"
                )
                if va.ref == vb.ref:
                    # Same-component: warn only — stagger would oscillate.
                    va.warning = "stub crossing detected — geometry fix needed in _place_group"
                    vb.warning = va.warning
                    log.append("    → WARNING (same-component — manual geometry fix needed)")
                    continue
                mover  = va if va.priority > vb.priority else vb
                anchor = vb if mover is va else va
                other_obs = [
                    Obstacle(v.via_x, v.via_y, _via_r(v, clearance),
                             net_name=v.net_name)
                    for v in pending
                    if v is not va and v is not vb and not v.implicit_keepout
                ]
                cross_stubs: List[StubSeg] = []
                for v in pending:
                    if v is not va and v is not vb and not v.implicit_keepout:
                        cross_stubs.extend(_stub_segs_for(v, clearance))
                if _try_stagger(mover, anchor, pad_obs + other_obs,
                                pad_obs, clearance, cross_stubs,
                                edge_segs=edge_segs,
                                ext_via_list=pending):
                    mover.warning = ""
                    moved += 1
                    log.append("    → stagger resolved")
                else:
                    mover.warning = "crossing unresolved — manual intervention needed"
                    log.append("    → UNRESOLVED")

        print(f"  [crossing-pass] done: {_n_cross} violation(s) found", flush=True)
        log.append(f"pass {pass_n}: {moved} adjustment(s)")
        if moved == 0:
            log.append("stable — no further passes needed")
            break

    return log


# ---------------------------------------------------------------------------
# Board output
# ---------------------------------------------------------------------------

def _make_via(board, via: PendingVia, net) -> pcbnew.PCB_VIA:
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pcbnew.VECTOR2I(
        pcbnew.FromMM(via.via_x),
        pcbnew.FromMM(via.via_y),
    ))
    v.SetDrill(pcbnew.FromMM(via.via_drill_mm))
    v.SetWidth(pcbnew.FromMM(via.via_drill_mm + 2.0 * via.via_annular_mm))
    v.SetLayerPair(via.pad_layer_id, via.target_layer_id)
    v.SetNet(net)
    return v


def _make_neckdown(board, via: PendingVia, net) -> list:
    """Emit stub trace(s) from pad to via on the pad's copper layer.
    All segments lie on valid 45-degree routing angles (0/45/90/135/…).
    """
    def make_seg(x1, y1, x2, y2):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
        t.SetEnd  (pcbnew.VECTOR2I(pcbnew.FromMM(x2), pcbnew.FromMM(y2)))
        t.SetWidth(pcbnew.FromMM(via.neckdown_w_mm))
        t.SetLayer(via.pad_layer_id)
        t.SetNet(net)
        return t

    if via.cluster_real_pads:
        # Herringbone cluster: emit one stub per real pad converging at the via.
        # The primary's pad_x/pad_y was moved to the centroid; emit from each
        # original pad position instead so no stub has a dangling endpoint.
        segs = []
        for px, py, nw in via.cluster_real_pads:
            for seg in _route_45deg_stub(px, py, via.via_x, via.via_y,
                                         via.escape_dx, via.escape_dy):
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(seg[0]), pcbnew.FromMM(seg[1])))
                t.SetEnd  (pcbnew.VECTOR2I(pcbnew.FromMM(seg[2]), pcbnew.FromMM(seg[3])))
                t.SetWidth(pcbnew.FromMM(nw))
                t.SetLayer(via.pad_layer_id)
                t.SetNet(net)
                segs.append(t)
        return segs
    raw = _route_45deg_stub(via.pad_x, via.pad_y, via.via_x, via.via_y,
                             via.escape_dx, via.escape_dy)
    return [make_seg(*seg) for seg in raw]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _run(board, apply: bool, max_passes: int = 20, live: bool = False):
    clearance      = cfg.CLEARANCE_AUDIT["via_clearance_mm"]
    min_annular_mm = cfg.CLEARANCE_AUDIT.get("via_annular_ring_min_mm", 0.10)
    no_via         = set(cfg.CLEARANCE_AUDIT.get("via_keepout_exclude_refs", []))
    skip_nets      = set(getattr(cfg, "FANOUT_VIA_SKIP_NETS", []))

    hs_map = _hs_net_layer_map()
    sw_map = _sw_net_layer_map()

    # ------------------------------------------------------------------
    # 1. Collect pads that need layer-transition vias
    # ------------------------------------------------------------------

    # Pre-scan: identify feedthrough pads to skip.
    # A footprint is feedthrough on HS net N if it has >1 qualifying SMD pad on N.
    # For each such group, keep only the pad that is furthest from any same-net
    # pad in another footprint — that is the exit side needing a layer-transition
    # via.  The closer pad connects directly to its neighbour via a short F.Cu
    # trace and must not get a via (its stub would cross the neighbour's stub).
    import math as _math
    _hs_qualifying: list = []  # (net, ref, pad_num, px, py)
    for _fp in board.GetFootprints():
        _ref = _fp.GetReference()
        if _ref in no_via:
            continue
        for _pad in _fp.Pads():
            if _is_pth(_pad):
                continue
            _net = _pad.GetNetname()
            if not _net or _net.startswith("unconnected") or _net in skip_nets:
                continue
            _pri, _tgt = classify(_net, hs_map, sw_map, board)
            if _pri != PRIORITY_HS:
                continue
            if _pad.GetLayer() == _tgt:
                continue
            _hs_qualifying.append((
                _net, _ref, _pad.GetNumber(),
                pcbnew.ToMM(_pad.GetPosition().x),
                pcbnew.ToMM(_pad.GetPosition().y),
            ))

    # Group by (ref, net) to find feedthrough pairs
    _by_ref_net_pre: Dict = {}
    for _net, _ref, _pnum, _px, _py in _hs_qualifying:
        _by_ref_net_pre.setdefault((_ref, _net), []).append((_pnum, _px, _py))

    # Per-net positions across all footprints for proximity lookup
    _net_positions: Dict = {}
    for _net, _ref, _pnum, _px, _py in _hs_qualifying:
        _net_positions.setdefault(_net, []).append((_ref, _px, _py))

    feedthrough_skip: set = set()  # (ref, pad_num) pairs whose via must be suppressed
    # skipped pad positions by net — used in step 1c to suppress vias whose
    # post-alignment escape corridor passes through the connector-facing entry pad
    feedthrough_skipped_pos: Dict[str, list] = {}

    for (_ref, _net), _pads in _by_ref_net_pre.items():
        if len(_pads) < 2:
            continue
        _others = [(ox, oy) for (oref, ox, oy) in _net_positions.get(_net, []) if oref != _ref]
        if not _others:
            continue
        def _min_d(px, py):
            return min(_math.hypot(px - ox, py - oy) for ox, oy in _others)
        # Sort ascending by proximity to nearest other-footprint same-net pad
        _sorted = sorted(_pads, key=lambda p: _min_d(p[1], p[2]))
        # Keep the furthest (last entry); suppress all closer pads
        for _pnum, _px, _py in _sorted[:-1]:
            feedthrough_skip.add((_ref, _pnum))
            feedthrough_skipped_pos.setdefault(_net, []).append((_px, _py))

    # Pre-scan: fp_hs_target — for every footprint that carries at least one
    # HS pad needing a via, record the HS target layer.  Used below to give
    # PRIORITY_OTHER pads on the same footprint a via to that layer when they
    # would otherwise be skipped (e.g., J_DSI1 MIPI1_D2/D3 lanes).
    fp_hs_target: Dict[str, int] = {}
    for _net, _ref, _pnum, _px, _py in _hs_qualifying:
        fp_hs_target[_ref] = board.GetLayerID(hs_map[_net])

    # Pre-scan: net names present on each footprint — used to detect undeclared
    # diff pairs via _P/_N complement check in the fp_hs_target override below.
    _fp_pad_nets: Dict[str, set] = {}
    for _fp2 in board.GetFootprints():
        _ref2 = _fp2.GetReference()
        _fp_pad_nets[_ref2] = {
            _p2.GetNetname() for _p2 in _fp2.Pads()
            if _p2.GetNetname() and not _p2.GetNetname().startswith("unconnected")
        }

    pending: List[PendingVia] = []

    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref in no_via:
            continue
        for pad in fp.Pads():
            if _is_pth(pad):
                continue
            net_name = pad.GetNetname()
            if not net_name or net_name.startswith("unconnected"):
                continue
            if net_name in skip_nets:
                continue
            if (ref, pad.GetNumber()) in feedthrough_skip:
                continue
            pad_layer_id        = pad.GetLayer()
            priority, tgt_layer = classify(net_name, hs_map, sw_map, board)
            if pad_layer_id == tgt_layer:
                # PRIORITY_OTHER pads on an HS footprint need a via to the HS
                # layer if they are part of a diff pair: net ends in _P or _N
                # and the complement net also appears on the same footprint.
                # This catches lanes like MIPI1_D2/D3 not declared in HS_PAIRS
                # without pulling in single-ended signals (SPI, CEC, CC, RST…).
                if priority == PRIORITY_OTHER:
                    _hs_tgt = fp_hs_target.get(ref)
                    if _hs_tgt is not None and _hs_tgt != pad_layer_id:
                        # Detect diff pair by naming convention: try common
                        # suffix pairs (_P/_N and +/-).  If this net has a
                        # suffix and its complement appears on the same
                        # footprint, it is an undeclared diff pair that needs
                        # a via to the HS layer.
                        _PAIR_SFXS = [("_P", "_N"), ("_N", "_P"),
                                      ("+",  "-"),  ("-",  "+")]
                        _comp = ""
                        for _s, _opp in _PAIR_SFXS:
                            if net_name.endswith(_s):
                                _comp = net_name[:-len(_s)] + _opp
                                break
                        if _comp and _comp in _fp_pad_nets.get(ref, set()):
                            tgt_layer = _hs_tgt
                            priority = PRIORITY_HS  # place/resolve with HS peers
                        else:
                            continue
                    else:
                        continue
                else:
                    continue
            drill, annular  = via_params(priority)
            floor, nl, mx   = neckdown_params(priority, net_name)
            dx, dy          = escape_direction(fp, pad, nl)
            pw              = pcbnew.ToMM(pad.GetSizeX())
            ph              = pcbnew.ToMM(pad.GetSizeY())
            _net_obj  = pad.GetNet()
            _nc_name  = ""
            try:
                _nc_name = _net_obj.GetNetClass().GetName() if _net_obj else ""
            except Exception:
                pass
            nw   = neckdown_stub_width(floor, pw, ph, net_name, priority)
            _bb  = pad.GetBoundingBox()
            _bbox = (pcbnew.ToMM(_bb.GetLeft()),
                     pcbnew.ToMM(_bb.GetTop()),
                     pcbnew.ToMM(_bb.GetRight()),
                     pcbnew.ToMM(_bb.GetBottom()))
            pv = PendingVia(
                net_name        = net_name,
                ref             = ref,
                pad_num         = pad.GetNumber(),
                pad_x           = pcbnew.ToMM(pad.GetPosition().x),
                pad_y           = pcbnew.ToMM(pad.GetPosition().y),
                pad_layer_id    = pad_layer_id,
                target_layer_id = tgt_layer,
                escape_dx       = dx,
                escape_dy       = dy,
                priority        = priority,
                via_drill_mm    = drill,
                via_annular_mm  = annular,
                neckdown_w_mm   = nw,
                neckdown_len_mm = nl,
                max_search_mm   = mx,
                pad_w_mm        = pw,
                pad_h_mm        = ph,
                pad_bbox        = _bbox,
                netclass        = _nc_name,
            )
            pv.via_x = pv.pad_x + dx * nl
            pv.via_y = pv.pad_y + dy * nl
            pending.append(pv)

    # ------------------------------------------------------------------
    # 1b. Diff-pair escape direction alignment
    # ------------------------------------------------------------------
    # If P and N pads of a pair straddle the abs(dx)==abs(dy) tie in
    # escape_direction() they can get perpendicular escape directions,
    # guaranteeing a stub crossing.  For each HS pair on the same ref,
    # align N's escape direction to P's so both stubs are parallel.
    from collections import defaultdict
    _by_ref_net: Dict[str, Dict[str, PendingVia]] = defaultdict(dict)
    for _pv in pending:
        _by_ref_net[_pv.ref][_pv.net_name] = _pv

    def _align_pair(pv_p: PendingVia, pv_n: PendingVia) -> None:
        """Align N's escape direction to P's and update its initial via position."""
        if (pv_p.escape_dx == pv_n.escape_dx and
                pv_p.escape_dy == pv_n.escape_dy):
            return
        pv_n.escape_dx = pv_p.escape_dx
        pv_n.escape_dy = pv_p.escape_dy
        pv_n.via_x = pv_n.pad_x + pv_p.escape_dx * pv_n.neckdown_len_mm
        pv_n.via_y = pv_n.pad_y + pv_p.escape_dy * pv_n.neckdown_len_mm
        print(f"  [align] {pv_n.ref}: {pv_n.net_name} escape → "
              f"({pv_p.escape_dx:+.0f},{pv_p.escape_dy:+.0f}) "
              f"to match {pv_p.net_name}")

    # Pass 1: declared HS pairs from cfg.HS_PAIRS.
    _hs_pairs_aligned: set = set()  # (ref, p_net, n_net) already handled
    for _pair_name, (_p_net, _n_net, _layer, _skew) in cfg.HS_PAIRS.items():
        for _ref, _net_map in _by_ref_net.items():
            _pv_p = _net_map.get(_p_net)
            _pv_n = _net_map.get(_n_net)
            if _pv_p is None or _pv_n is None:
                continue
            _align_pair(_pv_p, _pv_n)
            _hs_pairs_aligned.add((_ref, _p_net, _n_net))

    # Pass 2: undeclared diff pairs detected by suffix convention (_P/_N, +/-).
    # Groups pending vias by (ref, base_name) and aligns any P/N pair not
    # already handled above — covers lanes like MIPI1_D2/D3 missing from HS_PAIRS.
    _PAIR_SFXS = [("_P", "_N"), ("+", "-")]
    _by_ref_base: Dict[str, Dict[str, Dict[str, PendingVia]]] = defaultdict(lambda: defaultdict(dict))
    for _pv in pending:
        for _sfx, _opp in _PAIR_SFXS:
            if _pv.net_name.endswith(_sfx):
                _base = _pv.net_name[:-len(_sfx)]
                _by_ref_base[_pv.ref][_base][_sfx] = _pv
                break
            if _pv.net_name.endswith(_opp):
                _base = _pv.net_name[:-len(_opp)]
                _by_ref_base[_pv.ref][_base][_opp] = _pv
                break

    for _ref, _base_map in _by_ref_base.items():
        for _base, _sfx_map in _base_map.items():
            for _sfx, _opp in _PAIR_SFXS:
                _pv_p = _sfx_map.get(_sfx)
                _pv_n = _sfx_map.get(_opp)
                if _pv_p is None or _pv_n is None:
                    continue
                _p_net = _pv_p.net_name
                _n_net = _pv_n.net_name
                if (_ref, _p_net, _n_net) in _hs_pairs_aligned:
                    break  # already done in pass 1
                _align_pair(_pv_p, _pv_n)
                break

    # ------------------------------------------------------------------
    # 1b-post: min_cross_sep check — warn when P/N via lateral separation
    # is insufficient for a trace to route between them without DRC violation.
    # separation needed = via_pad_diameter + 2*clearance + trace_width
    # where via_pad_diameter = hs_drill + 2*hs_annular.
    # With parallel escape direction the router avoids crossings entirely,
    # but this check surfaces any remaining tight spots for review.
    # ------------------------------------------------------------------
    _ca = cfg.CLEARANCE_AUDIT
    _hs_via_pad_d = _ca["hs_via_drill_mm"] + 2 * _ca["hs_via_annular_ring_mm"]
    _hs_clr       = _ca["via_clearance_mm"]
    for _pair_name, (_p_net, _n_net, _layer, _skew) in cfg.HS_PAIRS.items():
        _tw = cfg.HS_ROUTE_WIDTHS.get(_pair_name, (0.127, 0.1))[0]
        _min_sep = _hs_via_pad_d + 2 * _hs_clr + _tw
        for _ref, _net_map in _by_ref_net.items():
            _pv_p = _net_map.get(_p_net)
            _pv_n = _net_map.get(_n_net)
            if _pv_p is None or _pv_n is None:
                continue
            # Perpendicular separation = component of (P_via - N_via) perpendicular
            # to the shared escape direction.
            _dvx = _pv_p.via_x - _pv_n.via_x
            _dvy = _pv_p.via_y - _pv_n.via_y
            _edx, _edy = _pv_p.escape_dx, _pv_p.escape_dy
            _perp_sep = abs(_dvx * (-_edy) + _dvy * _edx)
            if _perp_sep < _min_sep:
                print(f"  [min_cross_sep] {_ref} {_pair_name}: "
                      f"P/N via lateral sep={_perp_sep:.3f}mm "
                      f"< {_min_sep:.3f}mm needed for cross-routing clearance")

    # ------------------------------------------------------------------
    # 1b-post2: HS pair P-offset pre-assignment.
    # For HS P/N pairs on the same face (same ref + same escape direction),
    # pre-assign the P via a minimum lateral offset away from N's escape
    # column.  This ensures that when the N via is later placed with an
    # axial straight stub (step 1e below), the horizontal separation between
    # N stub and P via is >= stub_hw_N + clearance + via_r_P, so c7 passes.
    #
    # Formula: min_lat = max(0, stub_hw_N + clearance + via_r_P - pad_pitch)
    # Sign convention (_via_corner): cx = pad_x - edy*lat_off
    #   south escape (edy=+1): positive lat_off → via moves WEST
    #                          negative lat_off → via moves EAST
    # Direction rule: P via must move away from N's pad column.
    #   If P_pad_x > N_pad_x (P is east of N): P must go further east → lat_off < 0
    #   If P_pad_x < N_pad_x (P is west of N): P must go further west → lat_off > 0
    # ------------------------------------------------------------------
    _ca_clr = cfg.CLEARANCE_AUDIT["via_clearance_mm"]
    _PAIR_SFXS_OFF = [("_P", "_N"), ("+", "-")]
    _face_pair_map: Dict[tuple, Dict[str, PendingVia]] = defaultdict(dict)
    for _pv in pending:
        if _pv.priority != PRIORITY_HS:
            continue
        _face_key = (_pv.ref, _pv.escape_dx, _pv.escape_dy)
        for _sfx, _opp in _PAIR_SFXS_OFF:
            if _pv.net_name.endswith(_sfx) or _pv.net_name.endswith(_opp):
                _face_pair_map[_face_key][_pv.net_name] = _pv
                break

    for _fkey_off, _net_map_off in _face_pair_map.items():
        _edx_off, _edy_off = _fkey_off[1], _fkey_off[2]
        # Perpendicular unit vector (90° CCW from escape):
        # _via_corner: cx = pad_x + edx*nl - edy*lat_off
        #              cy = pad_y + edy*nl + edx*lat_off
        # So lateral unit in board coords = (-edy, edx).
        _ldx_off, _ldy_off = -_edy_off, _edx_off
        for _sfx_off, _opp_off in _PAIR_SFXS_OFF:
            # Find all P/N base-name pairs in this face group.
            for _net_p, _pv_p_off in list(_net_map_off.items()):
                if not _net_p.endswith(_sfx_off):
                    continue
                _base_off = _net_p[:-len(_sfx_off)]
                _net_n = _base_off + _opp_off
                _pv_n_off = _net_map_off.get(_net_n)
                if _pv_n_off is None:
                    continue
                # Both P and N are on the same face.  Compute pad pitch
                # (perpendicular distance between pad centres).
                _pad_pitch = abs(
                    (_pv_p_off.pad_x - _pv_n_off.pad_x) * _ldx_off +
                    (_pv_p_off.pad_y - _pv_n_off.pad_y) * _ldy_off
                )
                if _pad_pitch < 1e-6:
                    continue  # co-located pads — skip
                _stub_hw_n = _pv_n_off.neckdown_w_mm / 2.0
                _via_r_p   = (_pv_p_off.via_drill_mm / 2.0
                              + _pv_p_off.via_annular_mm)
                _min_lat = max(0.0,
                               _stub_hw_n + _ca_clr + _via_r_p - _pad_pitch
                               + 0.005)  # 5µm margin: avoid zero-clearance FP failures
                if _min_lat < 1e-6:
                    continue  # pitch wide enough — no offset needed
                # Direction: P must move away from N's pad column.
                # Project P_pad onto lateral axis; if > N_pad projection,
                # P is on the positive side → needs positive lat movement.
                # In _via_corner coords: positive lat_off = negative lateral
                # (cx = pad_x - edy*lat_off).  So:
                #   P east of N (P_lat > N_lat) → move P east → lat_off negative
                #   P west of N (P_lat < N_lat) → move P west → lat_off positive
                _p_lat = (_pv_p_off.pad_x * _ldx_off
                          + _pv_p_off.pad_y * _ldy_off)
                _n_lat = (_pv_n_off.pad_x * _ldx_off
                          + _pv_n_off.pad_y * _ldy_off)
                _lat_sign = -1.0 if _p_lat < _n_lat else +1.0
                _pv_p_off.corner_lat_offset_mm = _lat_sign * _min_lat
                _pv_p_off.via_x, _pv_p_off.via_y = _via_corner(_pv_p_off)
                print(f"  [p-offset] {_pv_p_off.ref}/{_pv_p_off.pad_num}"
                      f" ({_pv_p_off.net_name}): lat_off"
                      f" {_lat_sign * _min_lat:+.3f}mm"
                      f" (pitch={_pad_pitch:.3f}mm min_lat={_min_lat:.3f}mm)")

    # ------------------------------------------------------------------
    # Sandwiched non-HS pad detection: non-HS pads sitting between HS
    # pairs on the same chip face cannot escape outward (HS via wall
    # blocks — inter-via gap ~0.08mm at 0.4mm pitch) or laterally
    # (adjacent HS pad gap 0.2mm, trace needs 0.5mm). Flip their escape
    # direction inward so their vias land behind the chip pad inner edge
    # in the chip interior space where there are no obstacles.
    # ------------------------------------------------------------------
    _sandwich_pitch_tol = 0.45  # 0.4mm pitch + 0.05mm tolerance
    for _pv in pending:
        if _pv.priority == PRIORITY_HS or _pv.implicit_keepout:
            continue
        _same_face_hs = [
            _hp for _hp in pending
            if _hp.priority == PRIORITY_HS
            and _hp.ref == _pv.ref
            and abs(_hp.escape_dx - _pv.escape_dx) < 0.01
            and abs(_hp.escape_dy - _pv.escape_dy) < 0.01
        ]
        if not _same_face_hs:
            continue
        # Lateral unit vector (90° CCW from escape direction).
        _s_lat_dx = -_pv.escape_dy
        _s_lat_dy =  _pv.escape_dx
        _pv_lat   = _pv.pad_x * _s_lat_dx + _pv.pad_y * _s_lat_dy
        # Check for HS pad within one pitch on both lateral sides.
        _hs_above = any(
            0.001 < ((_hp.pad_x * _s_lat_dx + _hp.pad_y * _s_lat_dy) - _pv_lat)
                  < _sandwich_pitch_tol
            for _hp in _same_face_hs
        )
        _hs_below = any(
            0.001 < (_pv_lat - (_hp.pad_x * _s_lat_dx + _hp.pad_y * _s_lat_dy))
                  < _sandwich_pitch_tol
            for _hp in _same_face_hs
        )
        if not (_hs_above and _hs_below):
            continue
        # Compute minimum inward depth: via must clear all adjacent HS pad
        # inner corners (pad_inner_edge + geometric clearance from via circle).
        _s_hs_half_len  = max(max(_hp.pad_w_mm, _hp.pad_h_mm) / 2.0
                              for _hp in _same_face_hs)
        _s_adj_lats     = sorted(
            abs((_hp.pad_x * _s_lat_dx + _hp.pad_y * _s_lat_dy) - _pv_lat)
            for _hp in _same_face_hs
        )
        _s_hs_half_narr = min(min(_hp.pad_w_mm, _hp.pad_h_mm) / 2.0
                              for _hp in _same_face_hs)
        _s_lat_clr      = _s_adj_lats[0] - _s_hs_half_narr
        _s_via_r        = _pv.via_drill_mm / 2.0 + _pv.via_annular_mm
        _s_inward_depth = (
            _s_hs_half_len
            + math.sqrt(max(0.0, (_s_via_r + _ca_clr) ** 2 - _s_lat_clr ** 2))
            + 0.05
        )
        _pv.sandwiched      = True
        _pv.escape_dx       = -_pv.escape_dx
        _pv.escape_dy       = -_pv.escape_dy
        _pv.neckdown_len_mm = max(_pv.neckdown_len_mm, _s_inward_depth)
        print(f"  [sandwiched] {_pv.ref}/{_pv.pad_num} ({_pv.net_name}):"
              f" inward escape, min_depth={_s_inward_depth:.3f}mm")

    # HS first, then switching, then other — determines displacement priority.
    # Within HS pairs on the same face: P-suffix vias are processed before
    # N-suffix so that when N's axial extension step fires, the P via is
    # already in `placed` with its lateral offset assigned.
    def _pair_sort_key(v: PendingVia):
        is_n = v.net_name.endswith('_N') or v.net_name.endswith('-')
        return (v.priority, int(is_n), -v.pad_x)
    pending.sort(key=_pair_sort_key)

    # ------------------------------------------------------------------
    # 1c. Post-alignment suppression: remove vias whose escape corridor
    #     (in the final, aligned direction) passes within clearance of a
    #     feedthrough-skipped pad on the same net.  The skipped pad is
    #     the connector-facing entry of a feedthrough device; that side
    #     connects directly on F.Cu and needs no layer-transition via.
    # ------------------------------------------------------------------
    # Proximity threshold for feedthrough-adjacent suppression (configurable).
    _PROX_SUPPRESS_MM = cfg.CLEARANCE_AUDIT.get(
        "feedthrough_proximity_suppress_mm", 5.0
    )

    # Pre-compute which footprint refs already have a pad suppressed by the
    # straight-corridor check.  Proximity suppression is restricted to those
    # refs so it cannot fire on TVS/filter footprints that happen to sit close
    # to a connector entry pad — only the connector itself (which is provably
    # adjacent to the feedthrough device) qualifies.
    def _corridor_hits(pv: PendingVia) -> bool:
        skipped = feedthrough_skipped_pos.get(pv.net_name)
        if not skipped:
            return False
        far_x = pv.pad_x + pv.escape_dx * pv.max_search_mm
        far_y = pv.pad_y + pv.escape_dy * pv.max_search_mm
        return any(_pt_to_seg_dist(sx, sy, pv.pad_x, pv.pad_y, far_x, far_y) < clearance
                   for sx, sy in skipped)

    _corridor_suppressed_refs = {pv.ref for pv in pending if _corridor_hits(pv)}

    def _stub_hits_skipped(pv: PendingVia) -> bool:
        skipped = feedthrough_skipped_pos.get(pv.net_name)
        if not skipped:
            return False
        # Corridor check: straight escape passes through a feedthrough entry pad.
        far_x = pv.pad_x + pv.escape_dx * pv.max_search_mm
        far_y = pv.pad_y + pv.escape_dy * pv.max_search_mm
        if any(_pt_to_seg_dist(sx, sy, pv.pad_x, pv.pad_y, far_x, far_y) < clearance
               for sx, sy in skipped):
            return True
        # Proximity check: pad is physically adjacent to a feedthrough entry pad
        # and can reach the layer-transition via a short bent F.Cu trace.
        # Guard: only applies to footprints whose other pads are already
        # suppressed by the corridor check (proven adjacent to a feedthrough
        # device).  This prevents TVS/filter pads — which sit close to a
        # connector but need their own layer-transition vias — from being
        # erroneously suppressed.
        if pv.ref not in _corridor_suppressed_refs:
            return False
        return any(_math.hypot(pv.pad_x - sx, pv.pad_y - sy) < _PROX_SUPPRESS_MM
                   for sx, sy in skipped)

    def _suppress_reason(pv: PendingVia) -> str:
        skipped = feedthrough_skipped_pos.get(pv.net_name, [])
        far_x = pv.pad_x + pv.escape_dx * pv.max_search_mm
        far_y = pv.pad_y + pv.escape_dy * pv.max_search_mm
        if any(_pt_to_seg_dist(sx, sy, pv.pad_x, pv.pad_y, far_x, far_y) < clearance
               for sx, sy in skipped):
            return "escape corridor hits feedthrough entry pad"
        return "adjacent to feedthrough entry pad — layer transition shared"

    suppressed = [pv for pv in pending if _stub_hits_skipped(pv)]
    if suppressed:
        for pv in suppressed:
            print(f"  [suppress] {pv.ref}/{pv.pad_num} ({pv.net_name}): "
                  f"{_suppress_reason(pv)}")
    pending = [pv for pv in pending if not _stub_hits_skipped(pv)]

    # ------------------------------------------------------------------
    # 1d. Adjacent same-net pad clustering
    # ------------------------------------------------------------------
    # Connector pads on the same net that are side-by-side (e.g. two +5V
    # pins on an FFC connector) each getting separate vias creates redundant
    # stubs.  Merge them: keep one via (the primary), suppress the rest, and
    # emit short lateral traces connecting the secondary pad centres to the
    # primary's stub in _make_neckdown().
    pending = _cluster_adjacent_pads(pending, clearance)

    # ------------------------------------------------------------------
    # 1d2. Proximity-based via sharing
    # ------------------------------------------------------------------
    # Non-HS pads on the same net and layer within via_share_proximity_mm
    # share a single via.  The router connects suppressed pads via short
    # same-layer traces.  Eliminates unnecessary vias on bypass caps and
    # other local-loop components that never need a layer transition.
    #
    # Build pad obstacles once here so the sharing function can do a
    # lightweight side-exit test per candidate when selecting the primary.
    _pad_obs_early: List[Obstacle] = []
    for _fp in board.GetFootprints():
        _fp_ref = _fp.GetReference()
        for _pad in _fp.Pads():
            _pad_obs_early.append(_pad_obstacle(_pad, clearance, _fp_ref))
    pending = _suppress_proximity_via_sharing(pending, clearance, _pad_obs_early)

    # ------------------------------------------------------------------
    # 1e. Same-column corner offset
    # ------------------------------------------------------------------
    # Two or more pads from the same footprint with the same perpendicular
    # coordinate (same x for vertical escape) produce collinear pad→corner
    # stub segments that physically short each other.
    #
    # Fix: assign alternating lateral corner offsets so the pad→corner segments
    # diverge immediately at the pad exit.  The escape direction is UNCHANGED —
    # the via search continues in the same general direction so nearby obstacles
    # are not disturbed.  The offset spreads corners by (neckdown_w + 2×clearance)
    # centre-to-centre, which is exactly the minimum required stub clearance.
    #
    # Only triggers when the along-axis pad spacing is small enough that the
    # stubs would actually overlap (< neckdown_len + clearance).
    _col_groups: Dict = defaultdict(list)
    for _pv in pending:
        _pdx, _pdy = -_pv.escape_dy, _pv.escape_dx          # perpendicular to escape
        _pcrd = round((_pv.pad_x * _pdx + _pv.pad_y * _pdy) / 0.005) * 0.005
        _col_groups[(_pv.ref, _pv.escape_dx, _pv.escape_dy, _pcrd)].append(_pv)

    for (_ref_c, _edx_c, _edy_c, _pcrd_c), _grp in _col_groups.items():
        if len(_grp) < 2:
            continue
        # Check that at least one pair of pads is close enough along the escape
        # axis that their pad→corner segments would touch or overlap.
        _grp.sort(key=lambda v: v.pad_x * _edx_c + v.pad_y * _edy_c)
        _close = False
        for _gi in range(len(_grp) - 1):
            _da = _grp[_gi]; _db = _grp[_gi + 1]
            _along = ((_db.pad_x - _da.pad_x) * _edx_c
                      + (_db.pad_y - _da.pad_y) * _edy_c)
            if abs(_along) < _da.neckdown_len_mm + _db.neckdown_len_mm + clearance:
                _close = True
                break
        if not _close:
            continue
        # Assign alternating lateral offsets centred on the shared column.
        # Spacing = neckdown_w + 2×clearance ensures adjacent stubs are
        # separated by exactly the minimum required clearance.
        _nw = max(_pv.neckdown_w_mm for _pv in _grp)
        _spacing = _nw + 2.0 * clearance
        _n = len(_grp)
        for _ki, _pv in enumerate(_grp):
            _offset = (_ki - (_n - 1) / 2.0) * _spacing
            _pv.corner_lat_offset_mm = _offset
            # Initialise via position to the offset corner.
            _pv.via_x = (_pv.pad_x + _pv.escape_dx * _pv.neckdown_len_mm
                         - _pv.escape_dy * _offset)
            _pv.via_y = (_pv.pad_y + _pv.escape_dy * _pv.neckdown_len_mm
                         + _pv.escape_dx * _offset)
            print(f"  [col-fix] {_pv.ref}/{_pv.pad_num} ({_pv.net_name}): "
                  f"corner offset {_offset:+.3f}mm [shared-column stub fix]")

    # ------------------------------------------------------------------
    # 1f. HS pair crossing correction
    # ------------------------------------------------------------------
    # Detect HS pairs where P and N have opposite lateral ordering at the
    # two routing endpoints (s_a * s_b < 0).  The In2.Cu routes for such
    # pairs would topologically cross.
    #
    # Correction: at one endpoint, adjust corner_lat_offset_mm for P and N
    # so their search corridors swap lateral sides.  run_passes starts
    # every via search from its corner (_via_corner()), so adjusting
    # corner_lat_offset_mm is the only lever that actually affects where
    # vias land.  via_x/via_y are ignored by run_passes (it always
    # recomputes from the corner).
    #
    # The endpoint chosen for correction is the one where the escape
    # direction is most perpendicular to the lateral (routing) axis, i.e.
    # where |k| = |escape × lateral| is largest.  Only at such an endpoint
    # does corner_lat_offset_mm effectively control the lateral via position.
    # Endpoints where |k| < K_MIN (escape nearly parallel to lateral) are
    # skipped — applying the correction there would require impractically
    # large offsets.
    _K_MIN = 0.3   # min |escape × lateral| to attempt correction
    _xnet_pvs: Dict[str, List[PendingVia]] = {}
    for _pv in pending:
        _xnet_pvs.setdefault(_pv.net_name, []).append(_pv)

    for _xpair, (_xp_net, _xn_net, _xlayer, _xskew) in cfg.HS_PAIRS.items():
        _xpvs_p = _xnet_pvs.get(_xp_net, [])
        _xpvs_n = _xnet_pvs.get(_xn_net, [])
        if len(_xpvs_p) < 2 or len(_xpvs_n) < 2:
            continue  # single endpoint on board — crossing not applicable

        # Group by component ref (take last pv if a ref has multiple pads on net)
        _xp_by_ref = {_pv.ref: _pv for _pv in _xpvs_p}
        _xn_by_ref = {_pv.ref: _pv for _pv in _xpvs_n}
        _xcommon   = sorted(set(_xp_by_ref) & set(_xn_by_ref))
        if len(_xcommon) < 2:
            continue

        # Pick the two refs that are farthest apart — these are the actual
        # routing endpoints, not intermediate AC-coupling caps.
        _xref_a, _xref_b = _xcommon[0], _xcommon[1]
        _xbest_d = -1.0
        for _xi in range(len(_xcommon)):
            for _xj in range(_xi + 1, len(_xcommon)):
                _xra, _xrb = _xcommon[_xi], _xcommon[_xj]
                _xppa_ = _xp_by_ref[_xra]; _xpna_ = _xn_by_ref[_xra]
                _xppb_ = _xp_by_ref[_xrb]; _xpnb_ = _xn_by_ref[_xrb]
                _xdx = ((_xppa_.pad_x + _xpna_.pad_x) -
                        (_xppb_.pad_x + _xpnb_.pad_x)) / 2
                _xdy = ((_xppa_.pad_y + _xpna_.pad_y) -
                        (_xppb_.pad_y + _xpnb_.pad_y)) / 2
                _xd  = math.hypot(_xdx, _xdy)
                if _xd > _xbest_d:
                    _xbest_d = _xd
                    _xref_a, _xref_b = _xra, _xrb

        _xpp_a = _xp_by_ref[_xref_a]; _xpn_a = _xn_by_ref[_xref_a]
        _xpp_b = _xp_by_ref[_xref_b]; _xpn_b = _xn_by_ref[_xref_b]

        # Routing axis: vector between the two endpoint pair-midpoints
        _xmx_a = (_xpp_a.pad_x + _xpn_a.pad_x) / 2
        _xmy_a = (_xpp_a.pad_y + _xpn_a.pad_y) / 2
        _xmx_b = (_xpp_b.pad_x + _xpn_b.pad_x) / 2
        _xmy_b = (_xpp_b.pad_y + _xpn_b.pad_y) / 2
        _xrlen = math.hypot(_xmx_b - _xmx_a, _xmy_b - _xmy_a)
        if _xrlen < 1e-6:
            continue

        # Lateral unit vector (perpendicular to routing axis, 90° CCW)
        _xlux = -(_xmy_b - _xmy_a) / _xrlen
        _xluy =  (_xmx_b - _xmx_a) / _xrlen

        def _xlat_sign(pp, pn, _lux=_xlux, _luy=_xluy):
            _mx = (pp.pad_x + pn.pad_x) / 2
            _my = (pp.pad_y + pn.pad_y) / 2
            _d  = (pp.pad_x - _mx) * _lux + (pp.pad_y - _my) * _luy
            return 1 if _d > 0 else -1

        _xsa = _xlat_sign(_xpp_a, _xpn_a)
        _xsb = _xlat_sign(_xpp_b, _xpn_b)
        if _xsa * _xsb >= 0:
            continue  # lateral order is the same at both ends — no crossing

        # Crossing detected.  Choose the endpoint where |k| = |escape × lateral|
        # is largest — the escape there is most perpendicular to the lateral axis,
        # so corner_lat_offset_mm has the strongest effect on lateral via position.
        # k = escape_dx * luy - escape_dy * lux  (signed 2D cross product)
        _xka = _xpp_a.escape_dx * _xluy - _xpp_a.escape_dy * _xlux
        _xkb = _xpp_b.escape_dx * _xluy - _xpp_b.escape_dy * _xlux
        if abs(_xkb) >= abs(_xka):
            _xsw_p, _xsw_n, _xsw_ref, _xk = _xpp_b, _xpn_b, _xref_b, _xkb
        else:
            _xsw_p, _xsw_n, _xsw_ref, _xk = _xpp_a, _xpn_a, _xref_a, _xka

        if abs(_xk) < _K_MIN:
            print(f"  [cross-fix] {_xpair}: sa={_xsa:+d} sb={_xsb:+d} "
                  f"→ SKIP (|k|={abs(_xk):.3f} < {_K_MIN} at both ends — "
                  f"escape too parallel to lateral; route_highspeed must handle)")
            continue

        # Lateral positions of P and N pads at the correction endpoint.
        # Required corner_lat_offset shift: delta = (N_pad_lat - P_pad_lat) / k
        # This moves P's corner from P's lateral → N's lateral, and N's → P's.
        _xp_lat = _xsw_p.pad_x * _xlux + _xsw_p.pad_y * _xluy
        _xn_lat = _xsw_n.pad_x * _xlux + _xsw_n.pad_y * _xluy
        _xoffset = (_xn_lat - _xp_lat) / _xk   # shift to apply to P; negate for N

        # Non-adjacent pair guard: if pads from other nets lie laterally between
        # P and N on the same face, the inward corner offset would sweep stubs
        # through those pads. Skip the correction — straight escape avoids the
        # crossing, and route_highspeed handles the In2.Cu topology.
        _xlat_lo = min(_xp_lat, _xn_lat)
        _xlat_hi = max(_xp_lat, _xn_lat)
        _xsame_esc = lambda _pv: (abs(_pv.escape_dx - _xsw_p.escape_dx) < 1e-6
                                  and abs(_pv.escape_dy - _xsw_p.escape_dy) < 1e-6)
        _xintermed = [_pv for _pv in pending
                      if _pv.ref == _xsw_ref
                      and _pv.net_name not in (_xp_net, _xn_net)
                      and _xsame_esc(_pv)
                      and _xlat_lo < (_pv.pad_x * _xlux + _pv.pad_y * _xluy) < _xlat_hi]
        if _xintermed:
            print(f"  [cross-fix] {_xpair}: SKIP at {_xsw_ref} "
                  f"— {len(_xintermed)} intermediate pad(s) between P/N "
                  f"(non-adjacent pair; straight escape avoids stub conflict)")
            continue

        _xsw_p.corner_lat_offset_mm += _xoffset
        _xsw_n.corner_lat_offset_mm -= _xoffset
        # Refresh initial via positions to the new corners so run_passes logs
        # them as "changed" and the stub generator uses the correct corner.
        _xcx_p, _xcy_p = _via_corner(_xsw_p)
        _xcx_n, _xcy_n = _via_corner(_xsw_n)
        _xsw_p.via_x, _xsw_p.via_y = _xcx_p, _xcy_p
        _xsw_n.via_x, _xsw_n.via_y = _xcx_n, _xcy_n
        print(f"  [cross-fix] {_xpair}: sa={_xsa:+d} sb={_xsb:+d} "
              f"→ corner_lat_offset ±{_xoffset:.3f}mm at {_xsw_ref} "
              f"(|k|={abs(_xk):.3f})")

    # ------------------------------------------------------------------
    # 1g. Greedy per-face via stagger
    # ------------------------------------------------------------------
    # For every group of pads that share the same component ref AND the
    # same escape direction (one component face), assign the minimum
    # neckdown_len_mm to each via so that no two vias on the same face
    # violate their copper-to-copper clearance.
    #
    # Vias stay directly above/below their pads — no lateral (x) shift.
    # Clearance is achieved in the escape direction (y/z): adjacent pads
    # are placed in alternating "close" and "far" rows, like a brick
    # pattern.  For 0.4 mm pitch HS pads the result is:
    #   close row  neckdown = neckdown_min  (~0.50 mm)
    #   far   row  neckdown = neckdown_min + sqrt(req² − dx²)  (~1.25 mm)
    # which is the industry-standard fanout stagger for fine-pitch BGAs
    # and QFPs.
    #
    # Algorithm (greedy, lateral order):
    #   Sort pads by lateral position (pad coord projected onto lateral
    #   axis = perpendicular to escape direction).
    #   For each pad in order:
    #     For every already-placed via on this face:
    #       dx = |pad_lat_curr − pad_lat_nb|
    #       req = via_copper_r_curr + via_copper_r_nb + clearance
    #       If dx < req:
    #         dy_need = sqrt(req² − dx²)
    #         nl_close = nb.neckdown − dy_need   (safe "closer" threshold)
    #         If current nl > nl_close:           (can't be safe below nb)
    #           nl = max(nl, nb.neckdown + dy_need)   (must be safe above)
    #   Apply new neckdown and refresh via position.
    # ------------------------------------------------------------------
    _sg_hvpd = _ca["hs_via_drill_mm"] + 2 * _ca["hs_via_annular_ring_mm"]
    _sg_nvpd = _ca["via_drill_mm"]     + 2 * _ca["via_annular_ring_mm"]
    _sg_clr  = _ca["via_clearance_mm"]

    _face_groups: Dict[tuple, List[PendingVia]] = defaultdict(list)
    for _pv_sg in pending:
        _face_groups[(_pv_sg.ref,
                      _pv_sg.escape_dx,
                      _pv_sg.escape_dy)].append(_pv_sg)

    for _fkey, _fgrp in _face_groups.items():
        if len(_fgrp) < 2:
            continue
        _edx_sg, _edy_sg = _fkey[1], _fkey[2]
        _ldx_sg, _ldy_sg = -_edy_sg, _edx_sg   # lateral unit vector

        # Sort by pad lateral position so the greedy pass is deterministic.
        _fgrp.sort(key=lambda pv: pv.pad_x * _ldx_sg + pv.pad_y * _ldy_sg)

        # _placed: list of (pad_lat, assigned_neckdown, via_copper_d, pv_object)
        # pad_lat is stored (not via_lat) so effective via lateral position can
        # be recomputed as pad_lat + pv.corner_lat_offset_mm at any time.
        _placed_sg: List[tuple] = []

        for _pv_sg in _fgrp:
            _pad_lat_sg = _pv_sg.pad_x * _ldx_sg + _pv_sg.pad_y * _ldy_sg
            # Effective via lateral position accounts for pre-assigned P offset.
            _lat_sg = _pad_lat_sg + _pv_sg.corner_lat_offset_mm
            _vpd_sg = (_sg_hvpd if _pv_sg.priority == PRIORITY_HS
                       else _sg_nvpd)
            _sig_trace_w = _ca.get("signal_trace_width_mm", 0.20)
            # Pad half-extent in escape direction (from pad centre to pad copper edge)
            if _pv_sg.pad_bbox is not None:
                _bbl, _bbt, _bbr, _bbb = _pv_sg.pad_bbox
                _pad_half_esc = max(
                    _edx_sg * (_bbr - _pv_sg.pad_x) + _edy_sg * (_bbb - _pv_sg.pad_y),
                    _edx_sg * (_bbr - _pv_sg.pad_x) + _edy_sg * (_bbt - _pv_sg.pad_y),
                    _edx_sg * (_bbl - _pv_sg.pad_x) + _edy_sg * (_bbb - _pv_sg.pad_y),
                    _edx_sg * (_bbl - _pv_sg.pad_x) + _edy_sg * (_bbt - _pv_sg.pad_y),
                    0.0)
            else:
                _pad_half_esc = (abs(_edx_sg) * _pv_sg.pad_w_mm
                                 + abs(_edy_sg) * _pv_sg.pad_h_mm) / 2.0
            # Floor: pad_edge → clr → trace → clr → via_copper_edge → via_centre
            _nl_sg = max(_pv_sg.neckdown_len_mm,
                         _pad_half_esc + _sg_clr + _sig_trace_w + _sg_clr + _vpd_sg / 2.0)

            for _nb_pad_lat, _nb_nl, _nb_vpd, _nb_pv in _placed_sg:
                # Use effective via lateral (pad_lat + lat_off) for both vias
                # so the geometry reflects actual via circle positions, not pad
                # centres.  This gives accurate axial separation when P has a
                # pre-assigned lateral offset and N is straight (lat_off=0).
                _nb_lat = _nb_pad_lat + _nb_pv.corner_lat_offset_mm
                _dx_sg  = abs(_lat_sg - _nb_lat)
                _req_sg = _vpd_sg / 2.0 + _nb_vpd / 2.0 + _sg_clr
                if _dx_sg >= _req_sg:
                    continue   # laterally clear — no constraint
                _dy_sg    = math.sqrt(max(0.0, _req_sg ** 2 - _dx_sg ** 2))
                _nl_close = _nb_nl - _dy_sg    # max nl to be safely "below"
                if _nl_sg > _nl_close:         # can't safely be below nb
                    _nl_sg = max(_nl_sg, _nb_nl + _dy_sg)

            if _nl_sg > _pv_sg.neckdown_len_mm + 1e-6:
                _old_sg = _pv_sg.neckdown_len_mm
                _pv_sg.neckdown_len_mm = _nl_sg
                _pv_sg.via_x, _pv_sg.via_y = _via_corner(_pv_sg)
                print(f"  [stagger] {_pv_sg.ref}/{_pv_sg.pad_num} "
                      f"({_pv_sg.net_name}): "
                      f"neckdown {_old_sg:.3f}→{_nl_sg:.3f}mm")

            _placed_sg.append((_pad_lat_sg, _pv_sg.neckdown_len_mm, _vpd_sg, _pv_sg))

    hs_c = sum(1 for v in pending if v.priority == PRIORITY_HS)
    sw_c = sum(1 for v in pending if v.priority == PRIORITY_SW)
    ot_c = sum(1 for v in pending if v.priority == PRIORITY_OTHER)
    print(f"Pads needing vias: {len(pending)}  "
          f"(HS={hs_c}  switching={sw_c}  other={ot_c})")

    # ------------------------------------------------------------------
    # 2. Pad obstacles (reuse list built during step 1d2)
    # ------------------------------------------------------------------
    pad_obs = _pad_obs_early

    # ------------------------------------------------------------------
    # 2b. Board edge-cut segments (static obstacles, never change)
    # ------------------------------------------------------------------
    edge_segs = _load_edge_cuts(board)
    if edge_segs:
        print(f"  [edge] {len(edge_segs)} Edge.Cuts segment(s) loaded "
              f"({_BOARD_EDGE_CLEARANCE}mm clearance enforced)")

    # ------------------------------------------------------------------
    # 3. Multi-pass conflict resolution
    # ------------------------------------------------------------------
    board_stubs = _load_board_tracks(board, clearance)
    for line in run_passes(pending, pad_obs, clearance, min_annular_mm, max_passes,
                           edge_segs=edge_segs, board_stubs=board_stubs):
        print(line)

    # ------------------------------------------------------------------
    # 4. Result summary
    # ------------------------------------------------------------------
    vippo    = [v for v in pending if v.via_in_pad]
    keepouts = [v for v in pending if v.implicit_keepout]
    placed   = [v for v in pending if not v.implicit_keepout]
    if vippo:
        print(f"\nVIPPO required — {len(vippo)} via-in-pad placement(s):")
        for v in vippo:
            print(f"  {v.ref}/{v.pad_num}  net={v.net_name}  "
                  f"annular={v.via_annular_mm:.3f}mm")
    if keepouts:
        print(f"\nImplicit keepouts — {len(keepouts)} pad(s) with no viable via position:")
        print(f"  Via for these nets must be placed along the route by a routing script.")
        for v in keepouts:
            print(f"  {v.ref}/{v.pad_num}  net={v.net_name}  "
                  f"pad={v.pad_w_mm:.3f}×{v.pad_h_mm:.3f}mm")
    print(f"\n{len(placed)} via(s) placed  "
          f"({len(vippo)} via-in-pad  "
          f"{len(placed)-len(vippo)} side-exit)  "
          f"{len(keepouts)} implicit keepout(s).")

    if not apply:
        print("\nDry-run complete — pass --apply to write to board.")
        return

    # ------------------------------------------------------------------
    # 5. Remove previously placed fanout vias and stubs
    # ------------------------------------------------------------------
    pending_nets = {v.net_name for v in pending}
    hs_drill_nm  = pcbnew.FromMM(cfg.CLEARANCE_AUDIT["hs_via_drill_mm"])
    std_drill_nm = pcbnew.FromMM(cfg.CLEARANCE_AUDIT["via_drill_mm"])
    max_stub_mm  = cfg.CLEARANCE_AUDIT.get("fanout_max_search_mm",
                   cfg.CLEARANCE_AUDIT.get("hs_fanout_max_search_mm", 5.0)) + 0.5

    all_tracks = list(board.GetTracks())
    to_remove = []
    for track in all_tracks:
        if track.IsLocked():
            continue
        net = track.GetNetname()
        if net not in pending_nets:
            continue
        cls = track.GetClass()
        if cls == "PCB_VIA":
            d = track.GetDrillValue()
            if d == hs_drill_nm or d == std_drill_nm:
                to_remove.append(track)
        elif cls == "PCB_TRACK":
            length_mm = pcbnew.ToMM(track.GetLength())
            if length_mm <= max_stub_mm:
                to_remove.append(track)

    for item in to_remove:
        board.Remove(item)
    print(f"Cleared {len(to_remove)} previous fanout item(s) "
          f"({sum(1 for t in to_remove if t.GetClass()=='PCB_VIA')} vias, "
          f"{sum(1 for t in to_remove if t.GetClass()=='PCB_TRACK')} stubs).")

    # ------------------------------------------------------------------
    # 6. Emit vias and neckdown stubs to board
    # ------------------------------------------------------------------
    n_placed = 0
    n_skip   = 0
    for via in pending:
        if via.implicit_keepout:
            continue
        net = board.FindNet(via.net_name)
        if net is None:
            print(f"  SKIP {via.ref}/{via.pad_num}: net '{via.net_name}' not in board")
            n_skip += 1
            continue
        board.Add(_make_via(board, via, net))
        if not via.via_in_pad:
            for seg in _make_neckdown(board, via, net):
                board.Add(seg)
        n_placed += 1

    pcbnew.Refresh()
    print(f"\nEmitted {n_placed} via(s), skipped {n_skip} (net not found).")
    if not live:
        board.Save(cfg.PCB_FILE)
        print(f"Saved → {cfg.PCB_FILE}")


def run_live(apply: bool = True, max_passes: int = 20):
    """Entry point for KiCad scripting console — operates on the open board."""
    board = pcbnew.GetBoard()
    _run(board, apply=apply, max_passes=max_passes, live=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply",      action="store_true",
                        help="Write vias and neckdown stubs to the board file")
    parser.add_argument("--max-passes", type=int, default=20,
                        help="Maximum conflict-resolution passes (default: 20)")
    args = parser.parse_args()
    board = pcbnew.LoadBoard(cfg.PCB_FILE)
    _run(board, apply=args.apply, max_passes=args.max_passes, live=False)


if __name__ == "__main__":
    main()
