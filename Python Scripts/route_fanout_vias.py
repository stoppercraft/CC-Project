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
    face_fanout_assigned:  bool  = False  # True → via position assigned by _face_fanout; skip 1g/1g-2d/run_passes search


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

def _is_radial_fanout_fp(fp) -> bool:
    """True if the footprint is a compact multi-sided IC needing radial escape.

    Requires pads on all 4 cardinal faces AND a roughly square bounding box
    (aspect ratio < 1.5).  This correctly identifies QFN/QFP packages while
    excluding elongated connectors (SOM, FPC, USB-C) that happen to have pads
    on multiple faces but should keep cardinal escape directions.
    """
    bbox = fp.GetBoundingBox()
    cx = pcbnew.ToMM(bbox.GetCenter().x)
    cy = pcbnew.ToMM(bbox.GetCenter().y)
    w = pcbnew.ToMM(bbox.GetWidth())
    h = pcbnew.ToMM(bbox.GetHeight())
    if min(w, h) < 1e-3:
        return False
    if max(w, h) / min(w, h) > 1.5:
        return False  # elongated connector — keep cardinal escape
    faces: set = set()
    for pad in fp.Pads():
        px = pcbnew.ToMM(pad.GetPosition().x)
        py = pcbnew.ToMM(pad.GetPosition().y)
        dx, dy = px - cx, py - cy
        if abs(dx) >= abs(dy):
            faces.add('E' if dx >= 0.0 else 'W')
        else:
            faces.add('S' if dy >= 0.0 else 'N')
    return len(faces) == 4  # all 4 faces required for true radial fanout


def _radial_escape_direction(fp, pad) -> Tuple[float, float]:
    """Face-perpendicular cardinal escape for each pad on a radial-fanout component.

    Uses the same face-classification as _is_radial_fanout_fp: whichever axis
    displacement (|dx| vs |dy|) is larger determines the face, and the escape
    is the cardinal direction perpendicular to that face.  This guarantees
    0°/90° escape angles for all face pads regardless of their offset within
    the face, which ensures Phase 3.5 stub-to-pad distances stay at the full
    face pitch rather than collapsing to a diagonal projection.
    """
    bbox = fp.GetBoundingBox()
    cx = pcbnew.ToMM(bbox.GetCenter().x)
    cy = pcbnew.ToMM(bbox.GetCenter().y)
    px = pcbnew.ToMM(pad.GetPosition().x)
    py = pcbnew.ToMM(pad.GetPosition().y)
    dx, dy = px - cx, py - cy
    if abs(dx) >= abs(dy):
        return (-1.0, 0.0) if dx < 0 else (1.0, 0.0)
    else:
        return (0.0, -1.0) if dy < 0 else (0.0, 1.0)


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
    # Multi-sided components (QFN/QFP with pads on 3+ faces) use radial escape
    # so each pad gets its own angular corridor instead of sharing a face column.
    if _is_radial_fanout_fp(fp):
        return _radial_escape_direction(fp, pad)

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
    angle_rad = math.radians(pad.GetOrientation().AsDegrees())
    c_a, s_a = math.cos(angle_rad), math.sin(angle_rad)
    corners_x = [px + sx*c_a - sy*s_a, px - sx*c_a - sy*s_a,
                 px - sx*c_a + sy*s_a, px + sx*c_a + sy*s_a]
    corners_y = [py + sx*s_a + sy*c_a, py - sx*s_a + sy*c_a,
                 py - sx*s_a - sy*c_a, py + sx*s_a - sy*c_a]
    bbox = (min(corners_x), min(corners_y), max(corners_x), max(corners_y))
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
                      axial_first: bool = True,
                      ) -> List[Tuple[float, float, float, float]]:
    """Decompose pad→via into 1 or 2 segments that each lie on a valid 45° angle.

    When axial_first=True (default, used for single-pad stubs): exits the pad
    straight along the escape direction first, then finishes with a 45° diagonal.
    The straight segment stays at pad_x, preserving inter-pad corridors for
    sandwiched non-HS traces.

    When axial_first=False (used for herringbone cluster member stubs): exits the
    pad at 45° toward the cluster via first, then straight.  Cluster member stubs
    converge to a centroid via, so the lateral convergence is the primary geometry;
    keeping the stub at the original pad_x for a long axial segment would run it
    into adjacent vias.

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

    if axial_first and abs_ec >= abs_lc:
        # Axial-first: straight along escape direction, then 45° diagonal to via.
        # Waypoint is at the end of the straight segment (pad_x stays constant).
        wx = pad_x + ec_sign * edx * (abs_ec - abs_lc)
        wy = pad_y + ec_sign * edy * (abs_ec - abs_lc)
    elif abs_ec >= abs_lc:
        # Herringbone diagonal-first (used for cluster member stubs).
        # 45° diagonally toward via_x first (abs_lc steps), then straight along
        # escape.  Both cluster pads converge to the same waypoint at the via's
        # lateral position, creating a herringbone that avoids crossing stubs.
        wx = pad_x + ec_sign * edx * abs_lc + lc_sign * lat_dx * abs_lc
        wy = pad_y + ec_sign * edy * abs_lc + lc_sign * lat_dy * abs_lc
    else:
        # Lateral-dominant: 45° diagonal first, then straight along lateral axis.
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
    _sss_a = round(math.atan2(via.escape_dy, via.escape_dx) / (math.pi / 4.0)) * (math.pi / 4.0)
    _sss_edx = math.cos(_sss_a)
    _sss_edy = math.sin(_sss_a)
    raw = _route_45deg_stub(via.pad_x, via.pad_y, via.via_x, via.via_y,
                             _sss_edx, _sss_edy)
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


def _sandwiched_trace_stubs(
    board,
    sw_real_pads: list,
    centroid_by_face: dict,
    clearance: float,
    reach_mm: float = 5.0,
):
    """Create in-memory StubSeg obstacles AND real PCB_TRACKs for sandwiched pad corridors.

    sw_real_pads: list of (ref, pad_num, net_name, netclass, pad_x, pad_y,
                           escape_dx, escape_dy, neckdown_w_mm, pad_layer_id)
    centroid_by_face: (ref, edx_r, edy_r) -> centroid lateral value, for innermost-first ordering.

    Returns (stub_segs, pcb_tracks).  Caller must:
      - add stub_segs to board_stubs before run_passes
      - add pcb_tracks to board if apply=True (before run_passes, and again in section 6)
    """
    if not sw_real_pads:
        return [], []

    by_face: Dict[tuple, list] = {}
    for rec in sw_real_pads:
        _ref, _pad, _net, _nc, _px, _py, _edx, _edy, _nw, _lid = rec
        key = (_ref, round(_edx, 4), round(_edy, 4))
        by_face.setdefault(key, []).append(rec)

    stubs: List[StubSeg] = []
    tracks = []

    for (ref, edx_r, edy_r), face_recs in by_face.items():
        lat_dx = -edy_r
        lat_dy =  edx_r
        centroid_lat = centroid_by_face.get((ref, edx_r, edy_r), 0.0)
        face_recs.sort(key=lambda r: abs(
            (r[4] * lat_dx + r[5] * lat_dy) - centroid_lat
        ))
        for rec in face_recs:
            _ref, _pad, _net, _nc, _px, _py, _edx, _edy, _nw, _lid = rec
            hw = _nw / 2.0 + clearance
            stubs.append(StubSeg(
                x1=_px, y1=_py,
                x2=_px + _edx * reach_mm,
                y2=_py + _edy * reach_mm,
                half_w=hw,
                ref=_ref, pad_num=_pad,
                net_name=_net, netclass=_nc,
            ))
            net_obj = board.FindNet(_net)
            if net_obj is None:
                continue
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(_px), pcbnew.FromMM(_py)))
            t.SetEnd(pcbnew.VECTOR2I(
                pcbnew.FromMM(_px + _edx * reach_mm),
                pcbnew.FromMM(_py + _edy * reach_mm),
            ))
            t.SetWidth(pcbnew.FromMM(_nw))
            t.SetLayer(_lid)
            t.SetNet(net_obj)
            tracks.append(t)

    return stubs, tracks


def _tighten_vias(pending: List["PendingVia"], pad_obs: List[Obstacle],
                  board_stubs: List[StubSeg], clearance: float,
                  edge_segs: list = None) -> int:
    """Compaction pass: pull each placed via as close to its pad as clearance allows.

    Scans from the minimum allowed axial distance outward, stopping at the first
    position that passes all DRC checks (via circles, stub-to-via, stub-to-stub,
    edge clearance). Returns the number of vias repositioned.
    """
    edge_segs = edge_segs or []
    tightened = 0

    placed = [v for v in pending if not v.implicit_keepout and not v.via_in_pad]

    for via in placed:
        edx, edy   = via.escape_dx, via.escape_dy
        lat_dx     = -edy
        lat_dy     =  edx
        lat_off    = via.corner_lat_offset_mm

        cur_ax = (via.via_x - via.pad_x) * edx + (via.via_y - via.pad_y) * edy
        min_ax = via.neckdown_len_mm
        if cur_ax <= min_ax + 1e-6:
            continue

        other_vias = [v for v in placed if v is not via]

        # Stubs from all other placed vias + board stubs (exclude this via's own).
        ext_stubs: List[StubSeg] = list(board_stubs)
        for other in other_vias:
            ext_stubs.extend(_stub_segs_for(other, clearance))

        via_r      = _via_r(via, clearance)
        via_copper = via.via_drill_mm / 2.0 + via.via_annular_mm
        stub_hw    = via.neckdown_w_mm / 2.0

        # Scan from minimum axial outward; stop at first valid position.
        best_ax = cur_ax
        ax = min_ax
        while ax <= cur_ax - 1e-6:
            vx = via.pad_x + edx * ax + lat_dx * lat_off
            vy = via.pad_y + edy * ax + lat_dy * lat_off
            ok = True

            # Via circle vs pad obstacles
            if not _clear_of_obs(vx, vy, via_r, pad_obs,
                                  via.ref, via.pad_x, via.pad_y,
                                  excl_net=via.net_name,
                                  via_copper=via_copper,
                                  via_net=via.net_name, via_netclass=via.netclass,
                                  via_ref=via.ref):
                ok = False

            # Via circle vs other placed via copper
            if ok:
                for other in other_vias:
                    oc = other.via_drill_mm / 2.0 + other.via_annular_mm
                    if math.hypot(vx - other.via_x, vy - other.via_y) < via_copper + oc + clearance:
                        ok = False
                        break

            # Via circle vs board edges
            if ok and edge_segs:
                if not _clears_edges(vx, vy, via_r + _BOARD_EDGE_CLEARANCE, edge_segs):
                    ok = False

            # Via circle vs all stubs (other vias + board)
            if ok:
                if not _clear_of_stub_segs(vx, vy, via_r, ext_stubs,
                                            via.ref, via.pad_num,
                                            excl_net=via.net_name, clearance=clearance):
                    ok = False

            # Stub segments vs pad obstacles, stubs, and edges
            if ok:
                _old_vx, _old_vy = via.via_x, via.via_y
                via.via_x, via.via_y = vx, vy
                if via.cluster_real_pads:
                    _old_px, _old_py = via.pad_x, via.pad_y
                    for _rpx, _rpy, _rnw in via.cluster_real_pads:
                        _rsegs = _route_45deg_stub(_rpx, _rpy, vx, vy,
                                                   via.escape_dx, via.escape_dy,
                                                   axial_first=False)
                        _rcx = _rsegs[0][2] if _rsegs else vx
                        _rcy = _rsegs[0][3] if _rsegs else vy
                        via.pad_x, via.pad_y = _rpx, _rpy
                        if not _stub_clear(via, _rcx, _rcy, pad_obs, clearance,
                                           ext_stubs, edge_segs):
                            ok = False
                            break
                    via.pad_x, via.pad_y = _old_px, _old_py
                else:
                    segs45 = _route_45deg_stub(via.pad_x, via.pad_y, vx, vy,
                                                via.escape_dx, via.escape_dy)
                    cx, cy = (segs45[0][2], segs45[0][3]) if segs45 else (vx, vy)
                    if not _stub_clear(via, cx, cy, pad_obs, clearance,
                                       ext_stubs, edge_segs):
                        ok = False
                via.via_x, via.via_y = _old_vx, _old_vy

            # Stub segments vs other via copper circles
            if ok:
                if via.cluster_real_pads:
                    for _rpx, _rpy, _rnw in via.cluster_real_pads:
                        _rsegs = _route_45deg_stub(_rpx, _rpy, vx, vy,
                                                   via.escape_dx, via.escape_dy,
                                                   axial_first=False)
                        _rcx = _rsegs[0][2] if _rsegs else vx
                        _rcy = _rsegs[0][3] if _rsegs else vy
                        _rbent = len(_rsegs) > 1
                        for other in other_vias:
                            oc  = other.via_drill_mm / 2.0 + other.via_annular_mm
                            thr = stub_hw + clearance + oc
                            if _dist_to_segment(other.via_x, other.via_y,
                                                _rpx, _rpy, _rcx, _rcy) < thr:
                                ok = False
                                break
                            if ok and _rbent and _dist_to_segment(other.via_x, other.via_y,
                                                                   _rcx, _rcy, vx, vy) < thr:
                                ok = False
                                break
                        if not ok:
                            break
                else:
                    segs45 = _route_45deg_stub(via.pad_x, via.pad_y, vx, vy,
                                                via.escape_dx, via.escape_dy)
                    cx, cy = (segs45[0][2], segs45[0][3]) if segs45 else (vx, vy)
                    bent = len(segs45) > 1
                    for other in other_vias:
                        oc  = other.via_drill_mm / 2.0 + other.via_annular_mm
                        thr = stub_hw + clearance + oc
                        if _dist_to_segment(other.via_x, other.via_y,
                                            via.pad_x, via.pad_y, cx, cy) < thr:
                            ok = False
                            break
                        if ok and bent and _dist_to_segment(other.via_x, other.via_y,
                                                             cx, cy, vx, vy) < thr:
                            ok = False
                            break

            if ok:
                best_ax = ax
                break  # found the minimum valid axial distance

            ax += STEP_MM

        if best_ax < cur_ax - 1e-6:
            via.via_x = via.pad_x + edx * best_ax + lat_dx * lat_off
            via.via_y = via.pad_y + edy * best_ax + lat_dy * lat_off
            tightened += 1

    return tightened


def _keepout_escape_length(
    pad_x: float, pad_y: float, edx: float, edy: float,
    trace_half_w: float, clearance: float,
    placed_vias: List["PendingVia"],
    via_copper_keepout: float = 0.0,
    pad_obs: list = None,
    board_stubs: list = None,
    max_reach_mm: float = 5.0,
    net_name: str = "",
) -> float:
    """Compute the maximum safe escape trace length for a keepout pad.

    Checks placed via copper circles, neckdown stubs, pad obstacles, and
    board stubs.  Returns the axial distance at which the trace must stop
    to maintain clearance against all obstacles.

    via_copper_keepout: copper radius of the via that will be placed at the
    trace endpoint.  When > 0, an additional via-endpoint clearance check
    ensures the endpoint via does not land inside any placed via's copper.
    """
    lat_dx = -edy
    lat_dy =  edx
    max_L = max_reach_mm
    min_L = 0.0  # minimum escape required so via endpoint clears all obstacles

    def _point_limit(cx: float, cy: float, total_r: float) -> float:
        """Axial limit: escape endpoint must be < this to clear circular obstacle."""
        ax = (cx - pad_x) * edx + (cy - pad_y) * edy
        if ax <= 1e-6:
            return max_reach_mm
        lx = abs((cx - pad_x) * lat_dx + (cy - pad_y) * lat_dy)
        if lx >= total_r - 1e-6:
            return max_reach_mm
        gap = math.sqrt(max(0.0, total_r * total_r - lx * lx))
        return ax - gap

    def _rect_axial_limit(bbox: tuple, total_w: float) -> float:
        """Max axial escape distance before trace corridor (half-width total_w)
        intersects the pad rectangle bbox=(left,top,right,bottom).

        Transforms all 4 rectangle corners to (axial, lateral) coordinates
        relative to the escape axis. Uses exact pad rectangle geometry —
        not obs.r (bounding circle). total_w = trace_half_w + clearance,
        applied exactly once by the caller. Never double-counts clearance.
        """
        lat_dx = -edy
        lat_dy = edx
        left, top, right, bottom = bbox
        corners = ((left, top), (right, top), (right, bottom), (left, bottom))
        ax_vals  = tuple((cx - pad_x) * edx   + (cy - pad_y) * edy   for cx, cy in corners)
        lat_vals = tuple((cx - pad_x) * lat_dx + (cy - pad_y) * lat_dy for cx, cy in corners)
        ax_max  = max(ax_vals)
        lat_min = min(lat_vals)
        lat_max = max(lat_vals)
        # Rectangle entirely outside the lateral corridor — no constraint
        if lat_min >= total_w - 1e-6 or lat_max <= -(total_w - 1e-6):
            return max_reach_mm
        # Rectangle entirely behind the start position — no constraint
        if ax_max <= 1e-6:
            return max_reach_mm
        ax_min = min(ax_vals)
        # Rectangle straddles start position and overlaps laterally
        if ax_min <= 1e-6:
            return 0.0
        return ax_min

    def _seg_limit(seg: "StubSeg") -> float:
        """Axial max-L limit imposed by a stub segment (trace corridor check)."""
        total_r = seg.half_w + trace_half_w
        best = max_reach_mm
        for t in (0.0, 1.0):
            px = seg.x1 + t * (seg.x2 - seg.x1)
            py = seg.y1 + t * (seg.y2 - seg.y1)
            best = min(best, _point_limit(px, py, total_r))
        dx, dy = seg.x2 - seg.x1, seg.y2 - seg.y1
        dlx = dx * lat_dx + dy * lat_dy
        if abs(dlx) > 1e-9:
            t_z = max(0.0, min(1.0, -((seg.x1 - pad_x) * lat_dx + (seg.y1 - pad_y) * lat_dy) / dlx))
            px = seg.x1 + t_z * dx
            py = seg.y1 + t_z * dy
            best = min(best, _point_limit(px, py, total_r))
        return best

    def _apply_via_point(cx: float, cy: float, total_r: float) -> None:
        """Update min_L/max_L for a single point obstacle (via endpoint clearance).

        For a circle obstacle at (cx,cy) with radius total_r, the escape via
        must be placed at axial distance L < (ax-gap) OR L > (ax+gap).
        If the 'before' option is already excluded by min_L, use 'after'.
        This avoids falsely rejecting positions that lie past the obstacle.
        """
        nonlocal min_L, max_L
        ax = (cx - pad_x) * edx + (cy - pad_y) * edy
        if ax <= 1e-6:
            return
        lx = abs((cx - pad_x) * lat_dx + (cy - pad_y) * lat_dy)
        if lx >= total_r - 1e-6:
            return
        gap = math.sqrt(max(0.0, total_r * total_r - lx * lx))
        before = ax - gap
        after  = ax + gap
        if before <= min_L:
            # 'before' region already excluded — must place via past the obstacle
            min_L = max(min_L, after)
        else:
            max_L = min(max_L, before)

    def _seg_via_constraints_parallel(seg: "StubSeg", total_r: float):
        """Return (seg_min_L, seg_max_L) for a segment parallel to escape direction."""
        lx = abs((seg.x1 - pad_x) * lat_dx + (seg.y1 - pad_y) * lat_dy)
        if lx >= total_r - 1e-6:
            return (0.0, max_reach_mm)
        ax_t0 = (seg.x1 - pad_x) * edx + (seg.y1 - pad_y) * edy
        ax_t1 = (seg.x2 - pad_x) * edx + (seg.y2 - pad_y) * edy
        if ax_t0 > ax_t1:
            ax_t0, ax_t1 = ax_t1, ax_t0
        gap = math.sqrt(max(0.0, total_r * total_r - lx * lx))
        entry = ax_t0 - gap
        exit_ = ax_t1 + gap
        if entry <= 0.0:
            return (exit_, max_reach_mm)
        else:
            return (0.0, entry)

    def _apply_seg(seg: "StubSeg") -> None:
        nonlocal max_L, min_L
        max_L = min(max_L, _seg_limit(seg))
        if via_copper_keepout <= 0.0:
            return
        total_r = seg.half_w + via_copper_keepout
        dx, dy = seg.x2 - seg.x1, seg.y2 - seg.y1
        dlx = dx * lat_dx + dy * lat_dy
        if abs(dlx) < 1e-9:
            # Parallel segment: full body-intersection logic
            seg_min, seg_max = _seg_via_constraints_parallel(seg, total_r)
            min_L = max(min_L, seg_min)
            max_L = min(max_L, seg_max)
        else:
            # Angled segment: check endpoints and lateral-closest point using
            # context-aware before/after logic so vias past the obstacle are valid
            for t in (0.0, 1.0):
                px = seg.x1 + t * dx
                py = seg.y1 + t * dy
                _apply_via_point(px, py, total_r)
            t_z = max(0.0, min(1.0, -((seg.x1 - pad_x) * lat_dx +
                                       (seg.y1 - pad_y) * lat_dy) / dlx))
            _apply_via_point(seg.x1 + t_z * dx, seg.y1 + t_z * dy, total_r)
            # Interior quadratic: find exact [L1, L2] where via endpoint is within
            # total_r of the segment interior.  The t_z check above covers the
            # lateral-axis crossing but misses the case where the perpendicular
            # foot at the critical L is at a different t than t_z.  Solve
            # dist²(P(L), segment)² = total_r² as A·L²+B·L+C=0 and restrict to
            # the sub-range where the foot t_perp(L) is inside [0,1].
            _seg_s2 = dx * dx + dy * dy
            if _seg_s2 > 1e-12:
                _qa = seg.x1 - pad_x; _qb = seg.y1 - pad_y
                _g_s = _qa * dx + _qb * dy    # (seg.start - pad) · seg_dir
                _h_s = edx * dx + edy * dy    # esc · seg_dir
                _a_q = 1.0 - _h_s * _h_s / _seg_s2
                if _a_q > 1e-9:
                    _gh_s = _g_s * _h_s / _seg_s2
                    _b_q = 2.0 * (-edx * _qa - edy * _qb + _gh_s)
                    _c_q = (_qa * _qa + _qb * _qb
                            - _g_s * _g_s / _seg_s2 - total_r * total_r)
                    _disc_q = _b_q * _b_q - 4.0 * _a_q * _c_q
                    if _disc_q >= 0.0:
                        _sq_q = math.sqrt(_disc_q)
                        _l1_q = (-_b_q - _sq_q) / (2.0 * _a_q)
                        _l2_q = (-_b_q + _sq_q) / (2.0 * _a_q)
                        # Restrict to L-range where foot t_perp in [0,1]:
                        # t_perp(L) = (-g_s + L*h_s) / seg_s2
                        if abs(_h_s) > 1e-9:
                            _lt0 = _g_s / _h_s           # t_perp=0
                            _lt1 = (_seg_s2 + _g_s) / _h_s  # t_perp=1
                            if _lt0 > _lt1:
                                _lt0, _lt1 = _lt1, _lt0
                            _l1_eff = max(_l1_q, _lt0)
                            _l2_eff = min(_l2_q, _lt1)
                        else:
                            _l1_eff, _l2_eff = _l1_q, _l2_q
                        if _l1_eff < _l2_eff - 1e-9:
                            if _l1_eff <= min_L:
                                min_L = max(min_L, _l2_eff)
                            else:
                                max_L = min(max_L, _l1_eff)

    # 1. Placed via copper circles and their neckdown stubs
    for ov in placed_vias:
        via_copper = ov.via_drill_mm / 2.0 + ov.via_annular_mm
        # Trace corridor constraint
        max_L = min(max_L, _point_limit(ov.via_x, ov.via_y,
                                         via_copper + trace_half_w + clearance))
        # Endpoint via constraint: the via placed at the trace end must also clear
        # placed via copper (via-to-via clearance, not trace-to-via).
        # Use _apply_via_point (context-aware before/after) so vias placed PAST
        # the obstacle are not falsely rejected when before < min_L.
        if via_copper_keepout > 0.0:
            _apply_via_point(ov.via_x, ov.via_y,
                             via_copper + via_copper_keepout + clearance)
        for seg in _stub_segs_for(ov, clearance):
            _apply_seg(seg)

    # 2. Pad obstacles (exclude same-net pads).
    # Uses exact rectangular pad geometry via _rect_axial_limit (obs.bbox) — not
    # the bounding-circle obs.r.  Clearance applied once: total_w = trace_half_w
    # + clearance.  No same-footprint filtering — the correct geometry makes it
    # unnecessary: adjacent same-face pads at 0.5mm pitch have sufficient lateral
    # clearance from the escape axis and do not constrain max_L.
    if pad_obs:
        total_w = trace_half_w + clearance
        for obs in pad_obs:
            if obs.net_name == net_name:
                continue
            if obs.bbox is not None:
                max_L = min(max_L, _rect_axial_limit(obs.bbox, total_w))
            else:
                # Synthetic obstacle without pad geometry: fall back to circle.
                max_L = min(max_L, _point_limit(obs.cx, obs.cy,
                                                 obs.r + total_w))

    # 3. Board stubs / pre-stubs already on the board (exclude same-net)
    if board_stubs:
        for seg in board_stubs:
            if seg.net_name == net_name:
                continue
            _apply_seg(seg)

    if min_L > max_L:
        return 0.0  # no valid via position along this escape direction

    return max_L


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

            # After centroid move pad_bbox still refers to the primary's original
            # pad position, not the centroid.  Clearing it forces _own_depth_floor
            # in _face_fanout to use the pad's axial half-extent formula, which
            # always requires the via to clear the pad row regardless of lateral offset.
            primary.pad_bbox = None

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
                                     pad_obs: List[Obstacle],
                                     fp_by_ref: dict = None) -> List["PendingVia"]:
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

            # Sort by ascending min-clear (original behaviour: least-constrained
            # pad first), then pick the first that can actually side-exit.
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

            # Post-selection fix: if the chosen primary cannot physically host any
            # via (pad_min < via_drill — the drill itself won't fit), and a
            # VIPPO-capable pad exists on the same net IMMEDIATELY ADJACENT to the
            # primary, prefer it.  This handles the case where a narrow IC pad
            # (e.g. 0.250mm QFN signal pad) cannot receive a via, while an
            # immediately-adjacent bypass cap can via-in-pad with reduced annular.
            #
            # The adjacency guard (_vippo_adj_mm) prevents this from firing on
            # distant same-net pads (e.g. another IC 4-5mm away on the same signal
            # net) — those should each resolve independently, not inherit the via.
            _min_ann_s = cfg.CLEARANCE_AUDIT.get("via_annular_ring_min_mm", 0.10)
            _vippo_adj_mm = 2.0
            if min(primary.pad_w_mm, primary.pad_h_mm) < primary.via_drill_mm:
                _vippo_alt = next(
                    (pv for pv in ranked
                     if pv is not primary
                     and min(pv.pad_w_mm, pv.pad_h_mm) >= pv.via_drill_mm + 2.0 * _min_ann_s
                     and _pad_edge_dist(pv, primary) <= _vippo_adj_mm),
                    None
                )
                if _vippo_alt is not None:
                    primary = _vippo_alt

            # A secondary is suppressed if it is within threshold of the primary directly
            # AND the same-layer trace between them is physically viable.
            #
            # Exception: if the secondary is on a radial fanout footprint (QFN/QFP) AND
            # is on a DIFFERENT component than the primary, require it to be DIRECTLY
            # within threshold.  Cross-component traces between QFN faces would have to
            # cross under IC bodies — not routable.  Without this guard, the
            # connected-component algorithm creates cross-face chains (e.g. U3/20 LT_3V3
            # suppressed by R_RST1/1 via an intermediate hop) that suppress vias the
            # reference board places independently.
            #
            # Same-component pads (sec.ref == pri.ref) always use chain-suppress:
            # on-layer traces within a single footprint's pad ring are always routable
            # (going around the IC body), so sharing is valid regardless of distance.
            #
            # Non-radial-fanout secondaries (bypass caps, connectors, resistors) retain
            # full chain-suppress behavior — a central bypass cap can suppress a chain of
            # nearby power-supply pads even if the outermost is >2×threshold from the primary.
            def _should_suppress(sec: "PendingVia", pri: "PendingVia") -> bool:
                if fp_by_ref is not None and sec.ref != pri.ref:
                    fp_sec = fp_by_ref.get(sec.ref)
                    if fp_sec is not None and _is_radial_fanout_fp(fp_sec):
                        return _pad_edge_dist(sec, pri) < _threshold
                return True  # same-component or non-radial: chain-suppress

            secondaries = [pv for pv in cluster if pv is not primary
                           and _should_suppress(pv, primary)]
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

            if via.face_fanout_assigned:
                # Via position assigned by _face_fanout — accept as-is, no search.
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
            # Phase 5: suppress HS partner when one pad in the pair can't escape.
            # Fires here (not post-run_passes) so partner is caught before its via
            # is committed.  Retroactively marks an already-placed partner as keepout.
            if via.priority == PRIORITY_HS:
                _partner5 = None
                for _sfx5, _opp5 in (("_P", "_N"), ("_N", "_P"), ("+", "-"), ("-", "+")):
                    if via.net_name.endswith(_sfx5):
                        _pnet5 = via.net_name[:-len(_sfx5)] + _opp5
                        for _pg5 in group:
                            if (_pg5.ref == via.ref and _pg5.net_name == _pnet5
                                    and not _pg5.implicit_keepout):
                                _partner5 = _pg5
                                break
                    if _partner5:
                        break
                if _partner5 is not None:
                    _partner5.implicit_keepout = True
                    _partner5.via_x = _partner5.pad_x
                    _partner5.via_y = _partner5.pad_y
                    print(f"  [pair-error] {via.ref}/{via.pad_num} ({via.net_name}): "
                          f"escape=0 → partner {_partner5.ref}/{_partner5.pad_num} "
                          f"({_partner5.net_name}) also suppressed")

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

    _mn_snap_a = round(math.atan2(via.escape_dy, via.escape_dx) / (math.pi / 4.0)) * (math.pi / 4.0)
    _mn_edx_s  = math.cos(_mn_snap_a)
    _mn_edy_s  = math.sin(_mn_snap_a)
    if via.cluster_real_pads:
        # Bus topology: each pad exits straight axially to the via's depth level,
        # then a single lateral bus connects all stubs to the via.
        # Multiple pads on the same side of the via always produce overlapping
        # diagonals with any 45° approach — axial+lateral avoids this entirely.
        _cl_edx = _mn_edx_s
        _cl_edy = _mn_edy_s
        _cl_ldx = -_cl_edy   # lateral unit vector x
        _cl_ldy =  _cl_edx   # lateral unit vector y
        # Axial coordinate of the via along the escape direction
        _via_along = via.via_x * _cl_edx + via.via_y * _cl_edy
        # Lateral coordinates of all real pads + via
        _lat_coords = ([px * _cl_ldx + py * _cl_ldy
                        for px, py, _nw in via.cluster_real_pads]
                       + [via.via_x * _cl_ldx + via.via_y * _cl_ldy])
        _lat_min = min(_lat_coords)
        _lat_max = max(_lat_coords)
        segs = []
        # Lateral bus at via's axial position
        _bx1 = _lat_min * _cl_ldx + _via_along * _cl_edx
        _by1 = _lat_min * _cl_ldy + _via_along * _cl_edy
        _bx2 = _lat_max * _cl_ldx + _via_along * _cl_edx
        _by2 = _lat_max * _cl_ldy + _via_along * _cl_edy
        t_bus = pcbnew.PCB_TRACK(board)
        t_bus.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(_bx1), pcbnew.FromMM(_by1)))
        t_bus.SetEnd  (pcbnew.VECTOR2I(pcbnew.FromMM(_bx2), pcbnew.FromMM(_by2)))
        t_bus.SetWidth(pcbnew.FromMM(via.neckdown_w_mm))
        t_bus.SetLayer(via.pad_layer_id)
        t_bus.SetNet(net)
        segs.append(t_bus)
        # Axial stub from each real pad to the bus level
        for px, py, nw in via.cluster_real_pads:
            _pad_lat = px * _cl_ldx + py * _cl_ldy
            _sx2 = _pad_lat * _cl_ldx + _via_along * _cl_edx
            _sy2 = _pad_lat * _cl_ldy + _via_along * _cl_edy
            if math.hypot(_sx2 - px, _sy2 - py) < 1e-6:
                continue
            t_stub = pcbnew.PCB_TRACK(board)
            t_stub.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(px),   pcbnew.FromMM(py)))
            t_stub.SetEnd  (pcbnew.VECTOR2I(pcbnew.FromMM(_sx2), pcbnew.FromMM(_sy2)))
            t_stub.SetWidth(pcbnew.FromMM(nw))
            t_stub.SetLayer(via.pad_layer_id)
            t_stub.SetNet(net)
            segs.append(t_stub)
        return segs
    if hasattr(via, '_col_exit'):
        # Phase 3.5 col-exit: segment 1 is straight cardinal to col endpoint,
        # segments 2+ are _route_45deg_stub from col endpoint to via.
        _mn_cdx, _mn_cdy, _mn_cl, _mn_cex, _mn_cey = via._col_exit
        segs = [make_seg(via.pad_x, via.pad_y, _mn_cex, _mn_cey)]
        for seg in _route_45deg_stub(_mn_cex, _mn_cey, via.via_x, via.via_y,
                                      _mn_edx_s, _mn_edy_s):
            segs.append(make_seg(*seg))
        return segs
    raw = _route_45deg_stub(via.pad_x, via.pad_y, via.via_x, via.via_y,
                             _mn_edx_s, _mn_edy_s)
    return [make_seg(*seg) for seg in raw]


# ---------------------------------------------------------------------------
# Co-optimized face-fanout helpers (module-level)
# ---------------------------------------------------------------------------

def _seg_to_seg_dist(x1: float, y1: float, x2: float, y2: float,
                     x3: float, y3: float, x4: float, y4: float) -> float:
    """Minimum distance between two finite line segments."""
    def _cross2d(ax, ay, bx, by):
        return ax * by - ay * bx
    dx1, dy1 = x2 - x1, y2 - y1
    dx2, dy2 = x4 - x3, y4 - y3
    denom = _cross2d(dx1, dy1, dx2, dy2)
    if abs(denom) > 1e-10:
        t = _cross2d(x3 - x1, y3 - y1, dx2, dy2) / denom
        u = _cross2d(x3 - x1, y3 - y1, dx1, dy1) / denom
        if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
            return 0.0
    return min(
        _pt_to_seg_dist(x1, y1, x3, y3, x4, y4),
        _pt_to_seg_dist(x2, y2, x3, y3, x4, y4),
        _pt_to_seg_dist(x3, y3, x1, y1, x2, y2),
        _pt_to_seg_dist(x4, y4, x1, y1, x2, y2),
    )


def _face_needs_coopt(face_pads: list, sg_clr: float) -> bool:
    """True if stub-to-adjacent-pad-copper gap is too narrow for any trace.

    Uses the physical along-face extent from pad_bbox (which accounts for pad
    rotation) rather than unrotated pad_w/pad_h dimensions.
    Gap = pitch_ij - max(half_extent_i, half_extent_j) for each adjacent pair.
    Needs co-opt when min_gap < min(neckdown_w/2) + sg_clr.
    """
    if len(face_pads) < 2:
        return False
    edx = face_pads[0].escape_dx
    edy = face_pads[0].escape_dy
    if abs(edy) > abs(edx):  # N/S face — along face = X
        # Use bbox center X (accounts for rotation and clustering repositioning)
        coords = [(v.pad_bbox[0] + v.pad_bbox[2]) / 2.0 if v.pad_bbox else v.pad_x
                  for v in face_pads]
        # Axial (Y/escape-direction) half-extent: determines how far the pad copper
        # extends into the escape corridor beside each adjacent pad.  A stub escaping
        # north must travel past this extent before it clears the adjacent pad copper,
        # so this is the correct dimension for the coopt gap check on N/S faces.
        halves = [(v.pad_bbox[3] - v.pad_bbox[1]) / 2.0 if v.pad_bbox else v.pad_h_mm / 2.0
                  for v in face_pads]
    else:  # E/W face — along face = Y
        # Use bbox center Y (accounts for rotation and clustering repositioning)
        coords = [(v.pad_bbox[1] + v.pad_bbox[3]) / 2.0 if v.pad_bbox else v.pad_y
                  for v in face_pads]
        # Physical along-face (Y) half-extent from rotated bbox
        halves = [(v.pad_bbox[3] - v.pad_bbox[1]) / 2.0 if v.pad_bbox else v.pad_h_mm / 2.0
                  for v in face_pads]
    order = sorted(range(len(face_pads)), key=lambda i: coords[i])
    min_gap = float('inf')
    for k in range(len(order) - 1):
        i, j = order[k], order[k + 1]
        pitch = abs(coords[j] - coords[i])
        gap = pitch - max(halves[i], halves[j])
        if gap < min_gap:
            min_gap = gap
    min_trace_total = min(v.neckdown_w_mm / 2.0 for v in face_pads) + sg_clr
    return min_gap < min_trace_total


def _build_skip_net_pvs(fp, face_grp: list, edx: float, edy: float,
                        skip_nets: set) -> list:
    """Build PendingVia objects for skip-net pads on a face.

    Skip-net pads are excluded from pending (no via needed) but _face_fanout
    must see them as obstacles and generate lateral bus stubs for them.
    Returns a list of PendingVia objects, one per qualifying skip-net pad.
    """
    if not fp or not face_grp:
        return []
    tgt_layer  = face_grp[0].target_layer_id
    nl_ref     = face_grp[0].neckdown_len_mm
    face_axial = (sum(v.pad_x * edx + v.pad_y * edy for v in face_grp)
                  / len(face_grp))
    existing_nums = {pv.pad_num for pv in face_grp}
    skip_pvs: list = []
    for sk_pad in fp.Pads():
        if _is_pth(sk_pad):
            continue
        sk_net = sk_pad.GetNetname()
        if not sk_net or sk_net.startswith("unconnected"):
            continue
        if sk_net not in skip_nets:
            continue
        sk_px  = pcbnew.ToMM(sk_pad.GetPosition().x)
        sk_py  = pcbnew.ToMM(sk_pad.GetPosition().y)
        if abs(sk_px * edx + sk_py * edy - face_axial) > 1.0:
            continue
        sk_num = sk_pad.GetNumber()
        if sk_num in existing_nums:
            continue
        sk_drill, sk_ann        = via_params(PRIORITY_OTHER)
        sk_floor, sk_nl, sk_mx  = neckdown_params(PRIORITY_OTHER, sk_net)
        sk_pw  = pcbnew.ToMM(sk_pad.GetSizeX())
        sk_ph  = pcbnew.ToMM(sk_pad.GetSizeY())
        sk_nw  = neckdown_stub_width(sk_floor, sk_pw, sk_ph, sk_net, PRIORITY_OTHER)
        sk_bb  = sk_pad.GetBoundingBox()
        skip_pvs.append(PendingVia(
            net_name        = sk_net,
            ref             = fp.GetReference(),
            pad_num         = sk_num,
            pad_x           = sk_px,
            pad_y           = sk_py,
            pad_layer_id    = sk_pad.GetLayer(),
            target_layer_id = tgt_layer,
            escape_dx       = edx,
            escape_dy       = edy,
            priority        = PRIORITY_OTHER,
            via_drill_mm    = sk_drill,
            via_annular_mm  = sk_ann,
            neckdown_w_mm   = sk_nw,
            neckdown_len_mm = sk_nl,
            max_search_mm   = sk_mx,
            pad_w_mm        = sk_pw,
            pad_h_mm        = sk_ph,
            pad_bbox        = (pcbnew.ToMM(sk_bb.GetLeft()),
                               pcbnew.ToMM(sk_bb.GetTop()),
                               pcbnew.ToMM(sk_bb.GetRight()),
                               pcbnew.ToMM(sk_bb.GetBottom())),
        ))
    return skip_pvs


def _find_tight_subgroups(face_pads: list, sg_clr: float) -> list:
    """Find contiguous tight-pitch sub-groups within a face, expanded by one boundary pad.

    Returns a list of pad sub-lists.  Each sub-list is a candidate for _face_fanout.
    A pair is tight when gap < min_trace_half + sg_clr (same threshold as
    _face_needs_coopt).  Each sub-group is expanded by one pad on each side so the
    outermost tight pads get lateral anchors in the outside-in sort.
    Sub-groups that overlap after expansion are merged into one.
    """
    if len(face_pads) < 2:
        return []

    edx = face_pads[0].escape_dx
    edy = face_pads[0].escape_dy

    if abs(edy) > abs(edx):  # N/S face — lateral = X
        coords = [(v.pad_bbox[0] + v.pad_bbox[2]) / 2.0 if v.pad_bbox else v.pad_x
                  for v in face_pads]
        halves = [(v.pad_bbox[3] - v.pad_bbox[1]) / 2.0 if v.pad_bbox else v.pad_h_mm / 2.0
                  for v in face_pads]
    else:  # E/W face — lateral = Y
        coords = [(v.pad_bbox[1] + v.pad_bbox[3]) / 2.0 if v.pad_bbox else v.pad_y
                  for v in face_pads]
        halves = [(v.pad_bbox[3] - v.pad_bbox[1]) / 2.0 if v.pad_bbox else v.pad_h_mm / 2.0
                  for v in face_pads]

    order = sorted(range(len(face_pads)), key=lambda i: coords[i])
    min_trace_total = min(v.neckdown_w_mm / 2.0 for v in face_pads) + sg_clr

    tight = [False] * max(0, len(order) - 1)
    for k in range(len(tight)):
        i, j = order[k], order[k + 1]
        gap = abs(coords[j] - coords[i]) - max(halves[i], halves[j])
        tight[k] = gap < min_trace_total

    if not any(tight):
        return []

    # Build contiguous tight runs as (start_ord, end_ord) index pairs (inclusive)
    raw_groups = []
    k = 0
    while k < len(order):
        if k < len(tight) and tight[k]:
            start = k
            while k < len(tight) and tight[k]:
                k += 1
            raw_groups.append((start, k))
        else:
            k += 1

    # Expand each run by one pad on each side
    expanded = []
    for start, end in raw_groups:
        exp_start = max(0, start - 1)
        exp_end = min(len(order) - 1, end + 1)
        expanded.append([face_pads[order[i]] for i in range(exp_start, exp_end + 1)])

    # Merge overlapping sub-groups (possible when expansion bridges a gap)
    merged: list = []
    for sg in expanded:
        sg_ids = {id(pv) for pv in sg}
        if merged and sg_ids & {id(pv) for pv in merged[-1]}:
            seen = {id(pv): pv for pv in merged[-1]}
            for pv in sg:
                if id(pv) not in seen:
                    merged[-1].append(pv)
                    seen[id(pv)] = pv
        else:
            merged.append(list(sg))

    return merged


def _stub_only_depth(px: float, py: float, edx: float, edy: float,
                     lat_off: float,
                     placed_vias: list, trace_hw: float, clearance: float,
                     min_depth: float = 0.3) -> float:
    """Return minimum radial depth for a stub-only pad that clears all placed via copper.

    placed_vias: list of (vx, vy, via_copper_r) for already-committed vias.
    """
    perp_x, perp_y = -edy, edx
    depth = min_depth
    for vx_n, vy_n, via_r_n in placed_vias:
        need = via_r_n + trace_hw + clearance
        # Check current depth
        ex = px + lat_off * perp_x + depth * edx
        ey = py + lat_off * perp_y + depth * edy
        if math.hypot(ex - vx_n, ey - vy_n) < need:
            # Solve ||(px+lat_off*perp + t*e) - (vx,vy)||^2 = need^2
            A = px + lat_off * perp_x - vx_n
            B = py + lat_off * perp_y - vy_n
            p = A * edx + B * edy
            q = A * A + B * B
            disc = p * p - (q - need * need)
            if disc >= 0.0:
                t = -p + math.sqrt(disc)
                depth = max(depth, t + 0.050)
    return depth


def _face_fanout(
    face_pads: list,
    pad_obs_list: list,
    pending_set: set,
    sg_clr: float,
    ca: float,
    edx: float,
    edy: float,
    step_mm: float = 0.050,
    max_iter: int = 200,
) -> list:
    """Co-optimized multi-pass outside-in fanout for a single dense IC face.

    Implements the signal_indices-only algorithm validated in session 27.

    face_pads   : PendingVia objects for this face (sorted outside-in by caller)
    pad_obs_list: Obstacle objects for all pads on this component
    pending_set : set of (ref, pad_num) for pads that get a via
    sg_clr      : signal clearance (mm)
    ca          : annular ring (mm) — used to compute via_copper_r per pad
    edx, edy    : face escape unit cardinal vector (e.g. 0,-1 for north face)

    Returns list of (v, vx, vy) for placed via-bearing pads.
    Side-effects:
      - Sets v.implicit_keepout = True on blocked pads.
      - Sets v.stub_only_vx / v.stub_only_vy on stub-only pads.
      - Sets v.stub_ext_vx / v.stub_ext_vy on skip-net pads that get a lateral extension.
      - _face_fanout._last_bus_stubs = [(x1,y1,x2,y2,nw,net), ...] for this face.
      - _face_fanout._last_keepout_set = set of global indices declared keepout.
    """
    STEP_MM    = step_mm
    MAX_ITER   = max_iter
    DEPTH_STEP = 0.025
    CLEARANCE  = sg_clr

    if not face_pads:
        _face_fanout._last_bus_stubs   = []
        _face_fanout._last_keepout_set = set()
        return []

    n   = len(face_pads)
    ldx, ldy = -edy, edx
    _connected = [v for v in face_pads
                  if v.net_name and not v.net_name.startswith('unconnected')]
    face_center = (sum(v.pad_x * ldx + v.pad_y * ldy for v in _connected) / len(_connected)
                   if _connected else
                   sum(v.pad_x * ldx + v.pad_y * ldy for v in face_pads) / n)

    def _lat_sign(v):
        return 1.0 if (v.pad_x * ldx + v.pad_y * ldy) >= face_center else -1.0

    # ------------------------------------------------------------------
    # 0. Spatial pre-filter: keep only obstacles within the fanout envelope.
    #    pad_obs_list may be the entire board; clip it so _check() stays O(local).
    #    Fanout vias are placed within a few mm of the face — use face lateral
    #    half-extent plus FANOUT_DEPTH_CAP (not max_search_mm which is 15 mm).
    # ------------------------------------------------------------------
    FANOUT_DEPTH_CAP = 3.5   # mm — reference board's deepest via is 3.045mm
    _lat_coords = [v.pad_x * ldx + v.pad_y * ldy for v in face_pads]
    _lat_half   = (max(_lat_coords) - min(_lat_coords)) / 2.0
    _search_r   = _lat_half + FANOUT_DEPTH_CAP + max(v.via_drill_mm / 2.0 + ca
                                                      for v in face_pads) + sg_clr + 1.0
    _cx = sum(v.pad_x for v in face_pads) / n
    _cy = sum(v.pad_y for v in face_pads) / n
    pad_obs_list = [
        _o for _o in pad_obs_list
        if abs(_o.cx - _cx) < _search_r and abs(_o.cy - _cy) < _search_r
    ]

    # ------------------------------------------------------------------
    # 1. Bus groups: adjacency walk in lateral order.
    #    Consecutive pads sharing the same net form a bus group.
    #    Non-adjacent same-net pads do NOT form a bus.
    # ------------------------------------------------------------------
    _lat_sorted = sorted(range(n),
                         key=lambda i: face_pads[i].pad_x * ldx + face_pads[i].pad_y * ldy)
    bus_pad_set = set()
    bus_groups  = {}   # net_name -> [global_i, ...]
    _bi = 0
    while _bi < len(_lat_sorted):
        _cur_net  = face_pads[_lat_sorted[_bi]].net_name
        _run_idx  = [_lat_sorted[_bi]]
        _bi += 1
        while (_bi < len(_lat_sorted)
               and face_pads[_lat_sorted[_bi]].net_name == _cur_net):
            _run_idx.append(_lat_sorted[_bi])
            _bi += 1
        if len(_run_idx) >= 2 and _cur_net:
            bus_pad_set.update(_run_idx)
            bus_groups[_cur_net] = _run_idx

    # Outermost pad of each bus group escapes via signal_indices (stub only, no via).
    # Remove it from bus_pad_set so signal_indices picks it up.
    bus_escape_indices = set()
    for _bgrp in bus_groups.values():
        _outer = max(_bgrp, key=lambda i: abs(
            face_pads[i].pad_x * ldx + face_pads[i].pad_y * ldy - face_center))
        bus_escape_indices.add(_outer)
    bus_pad_set -= bus_escape_indices

    # ------------------------------------------------------------------
    # 2. HS partner map
    # ------------------------------------------------------------------
    hs_net_set = set()
    for v in face_pads:
        if v.net_name and ('_P' in v.net_name or '_N' in v.net_name):
            hs_net_set.add(v.net_name)

    hs_partner = {}
    for i, vi in enumerate(face_pads):
        if vi.net_name not in hs_net_set:
            continue
        partner_net = (vi.net_name[:-2] + '_N' if vi.net_name.endswith('_P')
                       else vi.net_name[:-2] + '_P')
        for j, vj in enumerate(face_pads):
            if j != i and vj.net_name == partner_net:
                hs_partner[i] = j
                break

    # ------------------------------------------------------------------
    # 3. Signal indices: all pads in pending_set that are not bus pads.
    #    Includes HS pairs and fill-connected pads (FANOUT_VIA_SKIP_NETS).
    #    Skip-net pads run through the full placement algorithm so their
    #    lat_off is pushed past neighbor vias; a stub is emitted but no via.
    #    Sorted outside-in (descending lateral distance from face center).
    # ------------------------------------------------------------------
    _skip_nets = set(getattr(cfg, "FANOUT_VIA_SKIP_NETS", []))
    signal_indices = [
        i for i in range(n)
        if (face_pads[i].ref, face_pads[i].pad_num) in pending_set
        and i not in bus_pad_set
        and face_pads[i].net_name  # skip no-net pads
    ]
    # Cluster representatives first so their via positions and stubs are in
    # `placed` before adjacent signal pads are evaluated.  Within each group
    # the original outside-in (descending lateral distance) order is kept.
    signal_indices.sort(
        key=lambda i: (
            0 if face_pads[i].cluster_real_pads else 1,
            -abs(face_pads[i].pad_x * ldx + face_pads[i].pad_y * ldy - face_center),
        ),
    )
    global_to_si = {gi: si for si, gi in enumerate(signal_indices)}

    # ------------------------------------------------------------------
    # 4. Bus obstacle pre-computation
    # ------------------------------------------------------------------
    bus_obstacles      = []
    bus_stubs_to_write = []

    for bnet, indices in bus_groups.items():
        v0     = face_pads[indices[0]]
        nw     = v0.neckdown_w_mm
        bus_hw = nw / 2.0
        # Bus line must clear all pad copper in the group — use the maximum pad
        # half-extent across the face (conservative: longer pad dimension) so the
        # horizontal connector doesn't short adjacent non-GND pads.
        _max_half_esc = max(
            (max(fp.pad_w_mm, fp.pad_h_mm) / 2.0 for fp in face_pads), default=0.0
        )
        bus_depth = max(v0.neckdown_len_mm,
                        _max_half_esc + CLEARANCE + nw / 2.0)

        if abs(edy) > abs(edx):  # N/S face — escape is Y
            bus_esc = v0.pad_y + edy * bus_depth
            xs      = [face_pads[i].pad_x for i in indices]
            bx_min, bx_max = min(xs), max(xs)
            bus_obstacles.append({'x1': bx_min, 'y1': bus_esc,
                                   'x2': bx_max, 'y2': bus_esc,
                                   'hw': bus_hw, 'net': bnet})
            bus_stubs_to_write.append((bx_min, bus_esc, bx_max, bus_esc, nw, bnet))
            for i in indices:
                px_i = face_pads[i].pad_x
                py_i = face_pads[i].pad_y
                bus_obstacles.append({'x1': px_i, 'y1': py_i,
                                       'x2': px_i, 'y2': bus_esc,
                                       'hw': bus_hw, 'net': bnet})
                # Suppress axial stub only when signal_indices will route the pad.
                # Skip-net pads (not in pending_set) never enter signal_indices, so
                # they must always get their axial stub or they go unconnected.
                _in_pending = (face_pads[i].ref, face_pads[i].pad_num) in pending_set
                if i not in bus_escape_indices or not _in_pending:
                    bus_stubs_to_write.append((px_i, py_i, px_i, bus_esc, nw, bnet))
        else:  # E/W face — escape is X
            bus_esc = v0.pad_x + edx * bus_depth
            ys      = [face_pads[i].pad_y for i in indices]
            by_min, by_max = min(ys), max(ys)
            bus_obstacles.append({'x1': bus_esc, 'y1': by_min,
                                   'x2': bus_esc, 'y2': by_max,
                                   'hw': bus_hw, 'net': bnet})
            bus_stubs_to_write.append((bus_esc, by_min, bus_esc, by_max, nw, bnet))
            for i in indices:
                px_i = face_pads[i].pad_x
                py_i = face_pads[i].pad_y
                bus_obstacles.append({'x1': px_i, 'y1': py_i,
                                       'x2': bus_esc, 'y2': py_i,
                                       'hw': bus_hw, 'net': bnet})
                _in_pending = (face_pads[i].ref, face_pads[i].pad_num) in pending_set
                if i not in bus_escape_indices or not _in_pending:
                    bus_stubs_to_write.append((px_i, py_i, bus_esc, py_i, nw, bnet))

    # ------------------------------------------------------------------
    # 5. Clearance checker
    # ------------------------------------------------------------------
    def _check(v, vx, vy, placed):
        via_copper_r = v.via_drill_mm / 2.0 + ca
        stub_hw      = v.neckdown_w_mm / 2.0
        chk_r        = via_copper_r + CLEARANCE

        # 1. via vs pad obstacles
        for obs in pad_obs_list:
            if obs.ref == v.ref and obs.net_name == v.net_name:
                continue
            if v.net_name and obs.net_name == v.net_name:
                continue
            if obs.bbox is not None:
                d   = _dist_point_to_bbox(vx, vy, obs.bbox)
                thr = chk_r
            else:
                d   = math.hypot(vx - obs.cx, vy - obs.cy)
                thr = via_copper_r + obs.r
            if d < thr:
                return (f"{obs.ref}/{obs.net_name}", d, thr, "via-vs-pad", None)

        # 2. via vs placed vias
        for j, pc in enumerate(placed):
            d   = math.hypot(vx - pc['vx'], vy - pc['vy'])
            thr = via_copper_r + pc['r'] + CLEARANCE
            if d < thr:
                return (f"via[{pc['pad_num']}]", d, thr, "via-vs-via", j)

        # 3. via vs placed stub segments (different net only)
        for j, pc in enumerate(placed):
            if pc['net'] == v.net_name:
                continue
            for x1s, y1s, x2s, y2s in pc['segs']:
                d   = _dist_to_segment(vx, vy, x1s, y1s, x2s, y2s)
                thr = via_copper_r + pc['stub_hw'] + CLEARANCE
                if d < thr:
                    return (f"stub[{pc['pad_num']}]", d, thr, "via-vs-stub", j)

        # 4. via vs bus obstacles (different net only)
        for bobs in bus_obstacles:
            if bobs['net'] == v.net_name:
                continue
            d   = _dist_to_segment(vx, vy,
                                   bobs['x1'], bobs['y1'], bobs['x2'], bobs['y2'])
            thr = via_copper_r + bobs['hw'] + CLEARANCE
            if d < thr:
                return (f"bus/{bobs['net']}", d, thr, "via-vs-bus", None)

        # 4b. via circle vs stub-only minimum stubs (different net only)
        for _x1s, _y1s, _x2s, _y2s, _shw, _snet in _stub_only_segs:
            if _snet == v.net_name:
                continue
            _d   = _dist_to_segment(vx, vy, _x1s, _y1s, _x2s, _y2s)
            _thr = via_copper_r + _shw + CLEARANCE
            if _d < _thr:
                return ("stub_only", _d, _thr, "via-vs-stub-only", None)

        # 5. stub checks — actual routed 45° segments
        segs = _route_45deg_stub(v.pad_x, v.pad_y, vx, vy, edx, edy, axial_first=True)
        if not segs:
            segs = [(v.pad_x, v.pad_y, vx, vy)]

        for x1, y1, x2, y2 in segs:
            if math.hypot(x2 - x1, y2 - y1) < 1e-6:
                continue

            # 5a. stub vs pad obstacles
            for obs in pad_obs_list:
                if (obs.ref == v.ref
                        and abs(obs.cx - v.pad_x) < 0.05
                        and abs(obs.cy - v.pad_y) < 0.05):
                    continue
                if v.net_name and obs.net_name == v.net_name:
                    continue
                if obs.bbox is not None:
                    d   = _seg_bbox_dist(x1, y1, x2, y2, obs.bbox)
                    thr = stub_hw + CLEARANCE
                else:
                    d   = _dist_to_segment(obs.cx, obs.cy, x1, y1, x2, y2)
                    thr = stub_hw + obs.r
                if d < thr:
                    return (f"{obs.ref}/{obs.net_name}", d, thr, "stub-vs-pad", None)

            # 5b. stub vs placed vias
            for j, pc in enumerate(placed):
                d   = _dist_to_segment(pc['vx'], pc['vy'], x1, y1, x2, y2)
                thr = stub_hw + pc['r'] + CLEARANCE
                if d < thr:
                    return (f"via[{pc['pad_num']}]", d, thr, "stub-vs-via", j)

            # 5c. stub vs placed stubs (different net only)
            for j, pc in enumerate(placed):
                if pc['net'] == v.net_name:
                    continue
                for x1s, y1s, x2s, y2s in pc['segs']:
                    d   = _seg_to_seg_dist(x1, y1, x2, y2, x1s, y1s, x2s, y2s)
                    thr = stub_hw + pc['stub_hw'] + CLEARANCE
                    if d < thr:
                        return (f"stub[{pc['pad_num']}]", d, thr, "stub-vs-stub", j)

            # 5d. stub vs bus obstacles (different net only)
            for bobs in bus_obstacles:
                if bobs['net'] == v.net_name:
                    continue
                d   = _seg_to_seg_dist(x1, y1, x2, y2,
                                       bobs['x1'], bobs['y1'],
                                       bobs['x2'], bobs['y2'])
                thr = stub_hw + bobs['hw'] + CLEARANCE
                if d < thr:
                    return (f"bus/{bobs['net']}", d, thr, "stub-vs-bus", None)

        return None  # clear

    # ------------------------------------------------------------------
    # 6. Main multi-pass loop — signal pads only, outside-in
    # ------------------------------------------------------------------

    def _cluster_segs(pv, vx, vy):
        """Stub segments for pv at via position (vx,vy).
        Cluster primaries get bus topology (real per-pad axial stubs + lateral bus)
        so clearance checks see the actual copper footprint, not just the centroid stub."""
        if pv.cluster_real_pads:
            _via_along = vx * edx + vy * edy
            _lat_coords = ([_px * ldx + _py * ldy for _px, _py, _ in pv.cluster_real_pads]
                           + [vx * ldx + vy * ldy])
            _lat_min, _lat_max = min(_lat_coords), max(_lat_coords)
            _bx1 = _lat_min * ldx + _via_along * edx
            _by1 = _lat_min * ldy + _via_along * edy
            _bx2 = _lat_max * ldx + _via_along * edx
            _by2 = _lat_max * ldy + _via_along * edy
            _segs = []
            if math.hypot(_bx2 - _bx1, _by2 - _by1) >= 1e-6:
                _segs.append((_bx1, _by1, _bx2, _by2))
            for _px, _py, _ in pv.cluster_real_pads:
                _pad_lat = _px * ldx + _py * ldy
                _sx2 = _pad_lat * ldx + _via_along * edx
                _sy2 = _pad_lat * ldy + _via_along * edy
                if math.hypot(_sx2 - _px, _sy2 - _py) >= 1e-6:
                    _segs.append((_px, _py, _sx2, _sy2))
            return _segs
        _segs = _route_45deg_stub(pv.pad_x, pv.pad_y, vx, vy, edx, edy, axial_first=True)
        if not _segs:
            _segs = [(pv.pad_x, pv.pad_y, vx, vy)]
        return [(x1, y1, x2, y2) for x1, y1, x2, y2 in _segs
                if math.hypot(x2 - x1, y2 - y1) >= 1e-6]

    # Stub-only pads are excluded from signal_indices, so their minimum
    # stubs are not in `placed` by default.  Pre-compute them as fixed
    # obstacles so via placement respects the space they occupy.
    _stub_only_segs = []
    for _i, _v in enumerate(face_pads):
        if not _v.net_name or _v.net_name.startswith('unconnected'):
            continue
        if _i in bus_pad_set:
            continue
        if (_v.ref, _v.pad_num) in pending_set:
            continue  # via-bearing pad — not stub-only
        _sx2 = _v.pad_x + edx * _v.neckdown_len_mm
        _sy2 = _v.pad_y + edy * _v.neckdown_len_mm
        _stub_only_segs.append((_v.pad_x, _v.pad_y, _sx2, _sy2,
                                 _v.neckdown_w_mm / 2.0, _v.net_name))

    # Pre-add cluster primaries' real-pad axial columns to _stub_only_segs.
    # Outside-in ordering can place a signal pad before a cluster primary that
    # is laterally closer to face center.  The cluster primary's physical stubs
    # aren't in `placed` yet, so the earlier pad can't be checked against them.
    # Adding each real pad's full axial column as a pre-obstacle ensures pads
    # placed before the cluster primary still avoid its copper footprint.
    for _i, _v in enumerate(face_pads):
        if not _v.cluster_real_pads:
            continue
        if (_v.ref, _v.pad_num) not in pending_set:
            continue
        for _px, _py, _pnw in _v.cluster_real_pads:
            _ex = _px + edx * FANOUT_DEPTH_CAP
            _ey = _py + edy * FANOUT_DEPTH_CAP
            _stub_only_segs.append((_px, _py, _ex, _ey,
                                    _pnw / 2.0, _v.net_name))

    sig_n        = len(signal_indices)
    lat_offs_arr = [0.0] * sig_n
    # Bus escape pads start with a minimum lateral offset so _route_45deg_stub
    # produces a 45° corner rather than a straight axial line.
    for _si, _gi in enumerate(signal_indices):
        if _gi in bus_escape_indices:
            lat_offs_arr[_si] = 3 * STEP_MM
    keepout_set  = set()
    final_placed = {}

    for iteration in range(MAX_ITER):
        placed        = []
        pass_result   = {}
        first_fail_si = None
        last_blk      = None

        for si, global_i in enumerate(signal_indices):
            if global_i in keepout_set:
                continue
            v            = face_pads[global_i]
            via_copper_r = v.via_drill_mm / 2.0 + ca
            lat_off      = lat_offs_arr[si]
            ls           = _lat_sign(v)

            placed_i   = False
            if v.pad_bbox is not None:
                _pah = (v.pad_y - v.pad_bbox[1] if edy < 0 else
                        v.pad_bbox[3] - v.pad_y  if edy > 0 else
                        v.pad_x - v.pad_bbox[0]  if edx < 0 else
                        v.pad_bbox[2] - v.pad_x)
            else:
                _pah = (v.pad_h_mm if abs(edy) > 0.5 else v.pad_w_mm) / 2.0
            depth      = max(v.neckdown_len_mm, _pah + via_copper_r + CLEARANCE)
            depth_ceil = min(v.max_search_mm, FANOUT_DEPTH_CAP)
            while depth <= depth_ceil + 1e-9:
                vx  = v.pad_x + ls * lat_off * ldx
                vy  = v.pad_y + ls * lat_off * ldy + edy * depth
                blk = _check(v, vx, vy, placed)
                if blk is None:
                    stub_segs = _cluster_segs(v, vx, vy)
                    placed.append({
                        'vx':      vx,
                        'vy':      vy,
                        'r':       via_copper_r,
                        'stub_hw': v.neckdown_w_mm / 2.0,
                        'segs':    stub_segs,
                        'pad_i':   global_i,
                        'net':     v.net_name,
                        'pad_num': v.pad_num,
                    })
                    pass_result[global_i] = (vx, vy)
                    placed_i = True
                    break
                last_blk = blk
                depth += DEPTH_STEP

            if not placed_i:
                first_fail_si = si
                break

        if first_fail_si is None:
            final_placed = pass_result
            break

        # Blocker analysis
        name, d, thr, kind, blk_j = last_blk or ('?', 0, 0, '?', None)
        fail_global_i = signal_indices[first_fail_si]
        if kind in ('via-vs-bus', 'stub-vs-bus'):
            keepout_set.add(fail_global_i)
            if fail_global_i in hs_partner:
                keepout_set.add(hs_partner[fail_global_i])

        elif blk_j is not None:
            blocking_gi = placed[blk_j]['pad_i']
            if blocking_gi in global_to_si:
                lat_offs_arr[global_to_si[blocking_gi]] += STEP_MM
            else:
                keepout_set.add(fail_global_i)
                if fail_global_i in hs_partner:
                    keepout_set.add(hs_partner[fail_global_i])

        elif kind in ('via-vs-pad', 'stub-vs-pad', 'via-vs-stub-only'):
            # Fixed obstacle (board pad or stub-only minimum stub) — push the
            # failing pad's own lat_off so the axial stub segment shortens and
            # clears the obstacle laterally.  Cap at _PAD_OBS_LAT_CAP steps
            # (~2.5 mm, matching the reference board's max lat_off of 1.3 mm).
            _PAD_OBS_LAT_CAP = 50
            if lat_offs_arr[first_fail_si] < _PAD_OBS_LAT_CAP * STEP_MM - 1e-9:
                lat_offs_arr[first_fail_si] += STEP_MM
            else:
                lat_offs_arr[first_fail_si] = 0.0
                keepout_set.add(fail_global_i)
                if fail_global_i in hs_partner:
                    keepout_set.add(hs_partner[fail_global_i])

        else:
            outer_si = next(
                (s for s in range(first_fail_si - 1, -1, -1)
                 if signal_indices[s] not in keepout_set),
                None,
            )
            if outer_si is not None:
                lat_offs_arr[outer_si] += STEP_MM
            else:
                keepout_set.add(fail_global_i)
                if fail_global_i in hs_partner:
                    keepout_set.add(hs_partner[fail_global_i])

        if all(gi in keepout_set for gi in signal_indices):
            break

    else:
        final_placed = pass_result

    # ------------------------------------------------------------------
    # 7. Iterative compaction: compact → equalize → compact → skip-net 7c
    #    Outer loop repeats until skip-net stub shallowing makes no further
    #    changes to final_placed (signal vias converge with updated obstacles).
    # ------------------------------------------------------------------
    for _outer_pass in range(5):
        MAX_COMPACT_ROUNDS = 20
        for _round in range(MAX_COMPACT_ROUNDS):
            _improved = False
            for si, gi in enumerate(signal_indices):
                if gi not in final_placed or gi in keepout_set:
                    continue
                vx_cur, vy_cur = final_placed[gi]
                v         = face_pads[gi]
                if v.net_name in _skip_nets:
                    continue  # skip-net: stub only, no via emitted — axial position from section 6 is correct
                ls        = _lat_sign(v)
                lat_off   = lat_offs_arr[si]
                cur_axial = abs((vx_cur - v.pad_x) * edx + (vy_cur - v.pad_y) * edy)

                other_placed = []
                for sj, gj in enumerate(signal_indices):
                    if gj == gi or gj not in final_placed or gj in keepout_set:
                        continue
                    vxj, vyj = final_placed[gj]
                    pvj  = face_pads[gj]
                    if pvj.net_name in _skip_nets:
                        # Skip-net pads have a position in final_placed but no via is emitted.
                        # Use r=0 (no via circle) so via-vs-via check doesn't block real vias,
                        # but keep the full stub geometry so stub-vs-stub checks still fire.
                        other_placed.append({
                            'vx': vxj, 'vy': vyj, 'r': 0.0,
                            'stub_hw': pvj.neckdown_w_mm / 2.0,
                            'segs': _cluster_segs(pvj, vxj, vyj),
                            'pad_i': gj, 'net': pvj.net_name, 'pad_num': pvj.pad_num,
                        })
                        continue
                    pcrj = pvj.via_drill_mm / 2.0 + ca
                    other_placed.append({
                        'vx': vxj, 'vy': vyj, 'r': pcrj,
                        'stub_hw': pvj.neckdown_w_mm / 2.0,
                        'segs': _cluster_segs(pvj, vxj, vyj),
                        'pad_i': gj,
                        'net': pvj.net_name,
                        'pad_num': pvj.pad_num,
                    })

                _vcr = v.via_drill_mm / 2.0 + ca
                _thr = _vcr + CLEARANCE

                def _own_depth_floor(lo, _v=v, _ls=ls, _thr=_thr, _ldx=ldx, _ldy=ldy,
                                     _edy=edy, _edx=edx):
                    """Min axial depth at lateral offset lo so via copper clears own pad."""
                    vx_t = _v.pad_x + _ls * lo * _ldx
                    if _v.pad_bbox is None:
                        return (_v.pad_h_mm if abs(_edy) > 0.5 else _v.pad_w_mm) / 2.0 + _thr
                    L, T, R, B = _v.pad_bbox
                    xd = max(0.0, L - vx_t, vx_t - R)
                    if xd >= _thr:
                        return 0.0
                    yn = math.sqrt(max(0.0, _thr * _thr - xd * xd))
                    if   _edy < 0: return max(0.0, _v.pad_y - T + yn)
                    elif _edy > 0: return max(0.0, B - _v.pad_y + yn)
                    elif _edx < 0: return max(0.0, _v.pad_x - L + yn)
                    else:          return max(0.0, R - _v.pad_x + yn)

                cur_cost  = math.hypot(lat_off, cur_axial)
                best_vx, best_vy = vx_cur, vy_cur
                best_cost = cur_cost

                # Pair depth floor: prevent going shallower than equalized partner,
                # preserving depth equality established by 7b across outer passes.
                _partner_gi_1p = hs_partner.get(gi)
                _partner_depth_1p = 0.0
                if (_partner_gi_1p is not None
                        and _partner_gi_1p in final_placed
                        and _partner_gi_1p not in keepout_set):
                    _pvp1 = face_pads[_partner_gi_1p]
                    _vxp1, _vyp1 = final_placed[_partner_gi_1p]
                    _partner_depth_1p = abs((_vxp1 - _pvp1.pad_x) * edx
                                            + (_vyp1 - _pvp1.pad_y) * edy)

                # Strategy A: reduce axial depth at current lat_off
                depth_A = max(v.neckdown_len_mm, _own_depth_floor(lat_off),
                              _partner_depth_1p)
                while depth_A < cur_axial - 1e-9:
                    vx_A = v.pad_x + ls * lat_off * ldx
                    vy_A = v.pad_y + ls * lat_off * ldy + edy * depth_A
                    if _check(v, vx_A, vy_A, other_placed) is None:
                        c = math.hypot(lat_off, depth_A)
                        if c < best_cost:
                            best_cost, best_vx, best_vy = c, vx_A, vy_A
                        break
                    depth_A += DEPTH_STEP

                # Strategy B: push lateral offset outward at minimum own-pad depth
                for _lsn in range(1, 31):
                    lat_B = lat_off + _lsn * STEP_MM
                    df    = max(_own_depth_floor(lat_B), _partner_depth_1p)
                    vx_B  = v.pad_x + ls * lat_B * ldx
                    vy_B  = v.pad_y + ls * lat_B * ldy + edy * df
                    if _check(v, vx_B, vy_B, other_placed) is None:
                        c = math.hypot(lat_B, df)
                        if c < best_cost:
                            best_cost, best_vx, best_vy = c, vx_B, vy_B
                            lat_offs_arr[si] = lat_B
                        if df <= 1e-9:
                            break  # depth=0; cost=lat_B only grows from here

                # Strategy C: pull lateral offset inward, find minimum passing depth at each
                # lat. After outside-in placement, a via may have been pushed far outward to
                # avoid a neighbor that has since compacted inward — this strategy retreats it.
                _lat_C_steps = max(0, int(round(lat_off / STEP_MM)))
                for _lsm in range(1, _lat_C_steps + 1):
                    lat_C = lat_off - _lsm * STEP_MM
                    if lat_C < -1e-9:
                        lat_C = 0.0
                    df_C = max(v.neckdown_len_mm, _own_depth_floor(lat_C),
                               _partner_depth_1p)
                    depth_C = df_C
                    while True:
                        _cost_C = math.hypot(lat_C, depth_C)
                        if _cost_C >= best_cost - 1e-9:
                            break
                        if depth_C > cur_axial + 1e-9:
                            break
                        vx_C = v.pad_x + ls * lat_C * ldx
                        vy_C = v.pad_y + ls * lat_C * ldy + edy * depth_C
                        if _check(v, vx_C, vy_C, other_placed) is None:
                            c = math.hypot(lat_C, depth_C)
                            if c < best_cost:
                                best_cost, best_vx, best_vy = c, vx_C, vy_C
                                lat_offs_arr[si] = lat_C
                            break  # found minimum depth at this lat_C
                        depth_C += DEPTH_STEP
                    if lat_C <= 1e-9:
                        break

                # Strategy D: try inward offsets (negative lat_off — opposite of outward).
                # A via blocked on its outward side may clear that neighbor by stepping
                # inward (toward face center). Scan depths at each inward step, same as
                # Strategy A does at the current lat_off.
                for _lsn in range(1, 21):
                    lat_D = -_lsn * STEP_MM
                    df_D  = max(v.neckdown_len_mm, _own_depth_floor(lat_D),
                                _partner_depth_1p)
                    if math.hypot(abs(lat_D), df_D) >= best_cost - 1e-9:
                        break
                    depth_D = df_D
                    while True:
                        _cost_D = math.hypot(abs(lat_D), depth_D)
                        if _cost_D >= best_cost - 1e-9:
                            break
                        if depth_D > FANOUT_DEPTH_CAP + 1e-9:
                            break
                        vx_D = v.pad_x + ls * lat_D * ldx
                        vy_D = v.pad_y + ls * lat_D * ldy + edy * depth_D
                        if _check(v, vx_D, vy_D, other_placed) is None:
                            if _cost_D < best_cost:
                                best_cost, best_vx, best_vy = _cost_D, vx_D, vy_D
                                lat_offs_arr[si] = lat_D
                            break  # found minimum depth at this lat_D; continue outer loop
                        depth_D += DEPTH_STEP

                if best_cost < cur_cost - 1e-9:
                    final_placed[gi] = (best_vx, best_vy)
                    _improved = True

            if not _improved:
                break

        # ------------------------------------------------------------------
        # 7b. Pair depth equalization: for each HS pair both vias get the
        #     minimum shared depth where both clear all obstacles simultaneously.
        #     If current lateral separation is less than the via-to-via minimum,
        #     the outer via is first pushed outward to achieve minimum separation
        #     before the shared depth scan.
        # ------------------------------------------------------------------
        def _pair_depth_floor(pv, ls_, lat_):
            """Own-pad depth floor for an arbitrary pad at given lateral offset."""
            pcr_ = pv.via_drill_mm / 2.0 + ca
            thr_ = pcr_ + CLEARANCE
            vx_t = pv.pad_x + ls_ * lat_ * ldx
            if pv.pad_bbox is None:
                return (pv.pad_h_mm if abs(edy) > 0.5 else pv.pad_w_mm) / 2.0 + thr_
            L, T, R, B = pv.pad_bbox
            xd = max(0.0, L - vx_t, vx_t - R)
            if xd >= thr_:
                return 0.0
            yn = math.sqrt(max(0.0, thr_ * thr_ - xd * xd))
            if   edy < 0: return max(0.0, pv.pad_y - T + yn)
            elif edy > 0: return max(0.0, B - pv.pad_y + yn)
            elif edx < 0: return max(0.0, pv.pad_x - L + yn)
            else:         return max(0.0, R - pv.pad_x + yn)

        _processed_pairs = set()
        for _si, _gi in enumerate(signal_indices):
            _gj = hs_partner.get(_gi)
            if _gj is None:
                continue
            _sj = global_to_si.get(_gj)
            if _sj is None:
                continue
            _pair_key = (min(_gi, _gj), max(_gi, _gj))
            if _pair_key in _processed_pairs:
                continue
            _processed_pairs.add(_pair_key)

            if _gi not in final_placed or _gj not in final_placed:
                continue
            if _gi in keepout_set or _gj in keepout_set:
                continue

            vi = face_pads[_gi]
            vj = face_pads[_gj]
            vxi_c, vyi_c = final_placed[_gi]
            vxj_c, vyj_c = final_placed[_gj]
            ls_i  = _lat_sign(vi)
            ls_j  = _lat_sign(vj)
            lat_i = lat_offs_arr[_si]
            lat_j = lat_offs_arr[_sj]
            pcr_i = vi.via_drill_mm / 2.0 + ca
            pcr_j = vj.via_drill_mm / 2.0 + ca

            di = abs((vxi_c - vi.pad_x) * edx + (vyi_c - vi.pad_y) * edy)
            dj = abs((vxj_c - vj.pad_x) * edx + (vyj_c - vj.pad_y) * edy)
            d_max = max(di, dj)

            # Lateral coordinates of the two vias (signed projection onto ldx/ldy axis)
            via_lat_i = vi.pad_x * ldx + vi.pad_y * ldy + ls_i * lat_i
            via_lat_j = vj.pad_x * ldx + vj.pad_y * ldy + ls_j * lat_j
            cur_lat_sep = abs(via_lat_i - via_lat_j)
            min_lat_sep = pcr_i + pcr_j + CLEARANCE

            # Working lateral offsets — may be increased for the outer via if the
            # pair's current lateral separation is less than the via-to-via minimum.
            lat_i_w, lat_j_w = lat_i, lat_j
            if cur_lat_sep < min_lat_sep - 1e-9:
                extra = min_lat_sep - cur_lat_sep
                # Push the outer via (further from face_center) outward
                dist_i = abs(via_lat_i - face_center)
                dist_j = abs(via_lat_j - face_center)
                if dist_i >= dist_j:
                    lat_i_w = lat_i + extra
                else:
                    lat_j_w = lat_j + extra

            # Pre-scan: ensure intra-pair stub clearance is satisfied analytically.
            # The distance from each stub's axial-segment endpoint to the partner
            # via is depth-independent (= hypot(delta_lat, axial_offset)), so a
            # depth scan can never resolve it.  Compute the minimum lat needed to
            # clear, and bump the outer via's lat_w before computing d_floor.
            _stub_lat_i  = vi.pad_x * ldx + vi.pad_y * ldy
            _stub_lat_j  = vj.pad_x * ldx + vj.pad_y * ldy
            _dpad_ax_ij  = (vj.pad_x - vi.pad_x)*edx + (vj.pad_y - vi.pad_y)*edy
            # vi stub axial endpoint vs vj trial via
            _hax_ij = _dpad_ax_ij + lat_i_w
            if _hax_ij > 0.0:
                _via_lat_j_w_t = _stub_lat_j + ls_j * lat_j_w
                _dlat_ij = abs(_via_lat_j_w_t - _stub_lat_i)
                _thr_ij  = vi.neckdown_w_mm / 2.0 + pcr_j + CLEARANCE
                if _dlat_ij < _thr_ij and _hax_ij**2 + _dlat_ij**2 < _thr_ij**2 - 1e-9:
                    lat_i_w = max(lat_i_w, math.sqrt(_thr_ij**2 - _dlat_ij**2) - _dpad_ax_ij + 1e-6)
            # vj stub axial endpoint vs vi trial via (dpad_ax flipped)
            _hax_ji = -_dpad_ax_ij + lat_j_w
            if _hax_ji > 0.0:
                _via_lat_i_w_t = _stub_lat_i + ls_i * lat_i_w   # use possibly bumped lat_i_w
                _dlat_ji = abs(_via_lat_i_w_t - _stub_lat_j)
                _thr_ji  = vj.neckdown_w_mm / 2.0 + pcr_i + CLEARANCE
                if _dlat_ji < _thr_ji and _hax_ji**2 + _dlat_ji**2 < _thr_ji**2 - 1e-9:
                    lat_j_w = max(lat_j_w, math.sqrt(_thr_ji**2 - _dlat_ji**2) + _dpad_ax_ij + 1e-6)

            d_floor_i = max(vi.neckdown_len_mm, _pair_depth_floor(vi, ls_i, lat_i_w))
            d_floor_j = max(vj.neckdown_len_mm, _pair_depth_floor(vj, ls_j, lat_j_w))
            d_floor   = max(d_floor_i, d_floor_j)

            # Base other_placed: all signal vias except both pair members
            _base_op = []
            for _sk2, _gk2 in enumerate(signal_indices):
                if _gk2 == _gi or _gk2 == _gj:
                    continue
                if _gk2 not in final_placed or _gk2 in keepout_set:
                    continue
                vxk, vyk = final_placed[_gk2]
                pvk      = face_pads[_gk2]
                segs_k   = _route_45deg_stub(pvk.pad_x, pvk.pad_y, vxk, vyk,
                                             edx, edy, axial_first=True)
                if not segs_k:
                    segs_k = [(pvk.pad_x, pvk.pad_y, vxk, vyk)]
                segs_k = [(x1, y1, x2, y2) for x1, y1, x2, y2 in segs_k
                          if math.hypot(x2 - x1, y2 - y1) >= 1e-6]
                if pvk.net_name in _skip_nets:
                    _base_op.append({'vx': vxk, 'vy': vyk, 'r': 0.0,
                                      'stub_hw': pvk.neckdown_w_mm / 2.0,
                                      'segs': segs_k, 'pad_i': _gk2,
                                      'net': pvk.net_name, 'pad_num': pvk.pad_num})
                else:
                    _base_op.append({'vx': vxk, 'vy': vyk,
                                      'r': pvk.via_drill_mm / 2.0 + ca,
                                      'stub_hw': pvk.neckdown_w_mm / 2.0,
                                      'segs': segs_k, 'pad_i': _gk2,
                                      'net': pvk.net_name, 'pad_num': pvk.pad_num})

            # Scan upward from d_floor to find minimum shared depth
            best_shared    = None
            best_lat_i_out = lat_i_w
            best_lat_j_out = lat_j_w
            _d_try = d_floor
            while _d_try <= FANOUT_DEPTH_CAP + 1e-9:
                vx_i_t = vi.pad_x + ls_i * lat_i_w * ldx + edx * _d_try
                vy_i_t = vi.pad_y + ls_i * lat_i_w * ldy + edy * _d_try
                vx_j_t = vj.pad_x + ls_j * lat_j_w * ldx + edx * _d_try
                vy_j_t = vj.pad_y + ls_j * lat_j_w * ldy + edy * _d_try

                segs_j_t = _route_45deg_stub(vj.pad_x, vj.pad_y, vx_j_t, vy_j_t,
                                             edx, edy, axial_first=True)
                if not segs_j_t:
                    segs_j_t = [(vj.pad_x, vj.pad_y, vx_j_t, vy_j_t)]
                segs_j_t = [(x1, y1, x2, y2) for x1, y1, x2, y2 in segs_j_t
                            if math.hypot(x2 - x1, y2 - y1) >= 1e-6]
                op_for_i = _base_op + [{'vx': vx_j_t, 'vy': vy_j_t, 'r': pcr_j,
                                         'stub_hw': vj.neckdown_w_mm / 2.0,
                                         'segs': segs_j_t, 'pad_i': _gj,
                                         'net': vj.net_name, 'pad_num': vj.pad_num}]

                segs_i_t = _route_45deg_stub(vi.pad_x, vi.pad_y, vx_i_t, vy_i_t,
                                             edx, edy, axial_first=True)
                if not segs_i_t:
                    segs_i_t = [(vi.pad_x, vi.pad_y, vx_i_t, vy_i_t)]
                segs_i_t = [(x1, y1, x2, y2) for x1, y1, x2, y2 in segs_i_t
                            if math.hypot(x2 - x1, y2 - y1) >= 1e-6]
                op_for_j = _base_op + [{'vx': vx_i_t, 'vy': vy_i_t, 'r': pcr_i,
                                         'stub_hw': vi.neckdown_w_mm / 2.0,
                                         'segs': segs_i_t, 'pad_i': _gi,
                                         'net': vi.net_name, 'pad_num': vi.pad_num}]

                _ci = _check(vi, vx_i_t, vy_i_t, op_for_i)
                _cj = _check(vj, vx_j_t, vy_j_t, op_for_j)
                if _ci is None and _cj is None:
                    best_shared = _d_try
                    break
                _d_try += DEPTH_STEP

            if best_shared is not None and best_shared < d_max - DEPTH_STEP:
                final_placed[_gi] = (vi.pad_x + ls_i * lat_i_w * ldx + edx * best_shared,
                                      vi.pad_y + ls_i * lat_i_w * ldy + edy * best_shared)
                final_placed[_gj] = (vj.pad_x + ls_j * lat_j_w * ldx + edx * best_shared,
                                      vj.pad_y + ls_j * lat_j_w * ldy + edy * best_shared)
                lat_offs_arr[_si] = lat_i_w
                lat_offs_arr[_sj] = lat_j_w

        # ------------------------------------------------------------------
        # 7 (second pass): Re-compact after pair depth equalization so all
        #     via-bearing pads benefit from the updated pair positions.
        # ------------------------------------------------------------------
        for _round in range(MAX_COMPACT_ROUNDS):
            _improved = False
            for si, gi in enumerate(signal_indices):
                if gi not in final_placed or gi in keepout_set:
                    continue
                vx_cur, vy_cur = final_placed[gi]
                v         = face_pads[gi]
                if v.net_name in _skip_nets:
                    continue  # skip-net: stub only, no via emitted — axial position from section 6 is correct
                ls        = _lat_sign(v)
                lat_off   = lat_offs_arr[si]
                cur_axial = abs((vx_cur - v.pad_x) * edx + (vy_cur - v.pad_y) * edy)

                other_placed = []
                for sj, gj in enumerate(signal_indices):
                    if gj == gi or gj not in final_placed or gj in keepout_set:
                        continue
                    vxj, vyj = final_placed[gj]
                    pvj  = face_pads[gj]
                    if pvj.net_name in _skip_nets:
                        other_placed.append({
                            'vx': vxj, 'vy': vyj, 'r': 0.0,
                            'stub_hw': pvj.neckdown_w_mm / 2.0,
                            'segs': _cluster_segs(pvj, vxj, vyj),
                            'pad_i': gj, 'net': pvj.net_name, 'pad_num': pvj.pad_num,
                        })
                        continue
                    pcrj = pvj.via_drill_mm / 2.0 + ca
                    other_placed.append({
                        'vx': vxj, 'vy': vyj, 'r': pcrj,
                        'stub_hw': pvj.neckdown_w_mm / 2.0,
                        'segs': _cluster_segs(pvj, vxj, vyj),
                        'pad_i': gj,
                        'net': pvj.net_name,
                        'pad_num': pvj.pad_num,
                    })

                _vcr = v.via_drill_mm / 2.0 + ca
                _thr = _vcr + CLEARANCE

                def _own_depth_floor(lo, _v=v, _ls=ls, _thr=_thr, _ldx=ldx, _ldy=ldy,
                                     _edy=edy, _edx=edx):
                    vx_t = _v.pad_x + _ls * lo * _ldx
                    if _v.pad_bbox is None:
                        return (_v.pad_h_mm if abs(_edy) > 0.5 else _v.pad_w_mm) / 2.0 + _thr
                    L, T, R, B = _v.pad_bbox
                    xd = max(0.0, L - vx_t, vx_t - R)
                    if xd >= _thr:
                        return 0.0
                    yn = math.sqrt(max(0.0, _thr * _thr - xd * xd))
                    if   _edy < 0: return max(0.0, _v.pad_y - T + yn)
                    elif _edy > 0: return max(0.0, B - _v.pad_y + yn)
                    elif _edx < 0: return max(0.0, _v.pad_x - L + yn)
                    else:          return max(0.0, R - _v.pad_x + yn)

                cur_cost  = math.hypot(lat_off, cur_axial)
                best_vx, best_vy = vx_cur, vy_cur
                best_cost = cur_cost

                # Pair depth floor: prevent this via from going shallower than its
                # equalized partner, preserving the depth equality 7b just established.
                _partner_gi_2p = hs_partner.get(gi)
                _partner_depth_2p = 0.0
                if (_partner_gi_2p is not None
                        and _partner_gi_2p in final_placed
                        and _partner_gi_2p not in keepout_set):
                    _pvp2 = face_pads[_partner_gi_2p]
                    _vxp2, _vyp2 = final_placed[_partner_gi_2p]
                    _partner_depth_2p = abs((_vxp2 - _pvp2.pad_x) * edx
                                            + (_vyp2 - _pvp2.pad_y) * edy)

                # Strategy A: reduce axial depth at current lat_off
                depth_A = max(v.neckdown_len_mm, _own_depth_floor(lat_off),
                              _partner_depth_2p)
                while depth_A < cur_axial - 1e-9:
                    vx_A = v.pad_x + ls * lat_off * ldx
                    vy_A = v.pad_y + ls * lat_off * ldy + edy * depth_A
                    if _check(v, vx_A, vy_A, other_placed) is None:
                        c = math.hypot(lat_off, depth_A)
                        if c < best_cost:
                            best_cost, best_vx, best_vy = c, vx_A, vy_A
                        break
                    depth_A += DEPTH_STEP

                # Strategy B: push lateral offset outward at minimum own-pad depth
                for _lsn in range(1, 31):
                    lat_B = lat_off + _lsn * STEP_MM
                    df    = max(_own_depth_floor(lat_B), _partner_depth_2p)
                    vx_B  = v.pad_x + ls * lat_B * ldx
                    vy_B  = v.pad_y + ls * lat_B * ldy + edy * df
                    if _check(v, vx_B, vy_B, other_placed) is None:
                        c = math.hypot(lat_B, df)
                        if c < best_cost:
                            best_cost, best_vx, best_vy = c, vx_B, vy_B
                            lat_offs_arr[si] = lat_B
                        if df <= 1e-9:
                            break

                # Strategy C: pull lateral offset inward, find minimum passing depth
                _lat_C_steps = max(0, int(round(lat_off / STEP_MM)))
                for _lsm in range(1, _lat_C_steps + 1):
                    lat_C = lat_off - _lsm * STEP_MM
                    if lat_C < -1e-9:
                        lat_C = 0.0
                    df_C = max(v.neckdown_len_mm, _own_depth_floor(lat_C),
                               _partner_depth_2p)
                    depth_C = df_C
                    while True:
                        _cost_C = math.hypot(lat_C, depth_C)
                        if _cost_C >= best_cost - 1e-9:
                            break
                        if depth_C > cur_axial + 1e-9:
                            break
                        vx_C = v.pad_x + ls * lat_C * ldx
                        vy_C = v.pad_y + ls * lat_C * ldy + edy * depth_C
                        if _check(v, vx_C, vy_C, other_placed) is None:
                            c = math.hypot(lat_C, depth_C)
                            if c < best_cost:
                                best_cost, best_vx, best_vy = c, vx_C, vy_C
                                lat_offs_arr[si] = lat_C
                            break
                        depth_C += DEPTH_STEP
                    if lat_C <= 1e-9:
                        break

                if best_cost < cur_cost - 1e-9:
                    final_placed[gi] = (best_vx, best_vy)
                    _improved = True

            if not _improved:
                break

        # ------------------------------------------------------------------
        # 7c. Skip-net stub lateral extension: extend each skip-net stub past
        #     the nearest outer signal via group (adjacent HS pair or single via)
        #     at a depth that clears their copper, then add a lateral segment to
        #     just past the outermost via in that group.
        # ------------------------------------------------------------------
        _7c_any_changed = False
        for _si7c, _gi7c in enumerate(signal_indices):
            _pv7c = face_pads[_gi7c]
            if _pv7c.net_name not in _skip_nets:
                continue
            if _gi7c not in final_placed or _gi7c in keepout_set:
                continue

            _ls7c    = _lat_sign(_pv7c)
            _v_lat7c = _pv7c.pad_x * ldx + _pv7c.pad_y * ldy
            _shw7c   = _pv7c.neckdown_w_mm / 2.0
            _vx7c, _vy7c = final_placed[_gi7c]
            _cur_d7c = (_vx7c - _pv7c.pad_x) * edx + (_vy7c - _pv7c.pad_y) * edy
            _lo7c    = lat_offs_arr[_si7c]

            # Collect outer signal vias (not skip-net, not keepout), sorted nearest first
            _outers7c = []  # (gap, via_lat, depth, pcr, annular, net_name, gi)
            for _gj7c in signal_indices:
                if _gj7c == _gi7c or _gj7c not in final_placed or _gj7c in keepout_set:
                    continue
                _pvj7c = face_pads[_gj7c]
                if _pvj7c.net_name in _skip_nets or not _pvj7c.net_name:
                    continue
                _vxj7c, _vyj7c = final_placed[_gj7c]
                _vlat_j7c = _vxj7c * ldx + _vyj7c * ldy
                _gap7c    = _ls7c * (_vlat_j7c - _v_lat7c)
                if _gap7c <= 0:
                    continue
                _dj7c    = (_vxj7c - _pvj7c.pad_x) * edx + (_vyj7c - _pvj7c.pad_y) * edy
                _pcr_j7c = _pvj7c.via_drill_mm / 2.0 + ca
                # Skip vias the axial stub cannot laterally clear — the stub at pad_lat
                # going straight axially would violate clearance with this via.
                if abs(_vlat_j7c - _v_lat7c) < _pcr_j7c + _shw7c + CLEARANCE - 1e-9:
                    continue
                _outers7c.append((_gap7c, _vlat_j7c, _dj7c, _pcr_j7c,
                                   _pvj7c.via_annular_mm, _pvj7c.net_name, _gj7c))

            if not _outers7c:
                continue

            _outers7c.sort(key=lambda x: x[0])  # nearest first

            # Build adjacent group: nearest via + its HS partner if present
            _nearest_net7c = _outers7c[0][5]
            _group7c = [_outers7c[0]]
            if _nearest_net7c.endswith('_P') or _nearest_net7c.endswith('_N'):
                _pnet7c = (_nearest_net7c[:-2] + '_N' if _nearest_net7c.endswith('_P')
                           else _nearest_net7c[:-2] + '_P')
                for _ov7c in _outers7c[1:]:
                    if _ov7c[5] == _pnet7c:
                        _group7c.append(_ov7c)
                        break

            # Effective obstacle radius for each group via in the escape direction:
            # max(via_copper_r, pad_axial_half) — pads whose AABB extends farther
            # than their via copper ring in the escape direction set the constraint.
            # Use pad_bbox (board-coordinate AABB) so pad rotation is handled correctly.
            def _pad_axial_half7c(_pv):
                _bb = _pv.pad_bbox  # (left, top, right, bottom) board coords
                return (abs(edx) * (_bb[2] - _bb[0]) + abs(edy) * (_bb[3] - _bb[1])) / 2.0
            _gr7c = [max(_ov[3], _pad_axial_half7c(face_pads[_ov[6]]))
                     for _ov in _group7c]
            _req_d7c  = max(_ov[2] + _gr + _shw7c + CLEARANCE
                            for _ov, _gr in zip(_group7c, _gr7c))
            _ext_d7c  = min(max(_req_d7c, _pv7c.neckdown_len_mm), FANOUT_DEPTH_CAP)

            # Only extend when shallowing: if the outer via is deeper than the
            # stub, the stub already clears it axially and no extension is needed.
            if _ext_d7c >= _cur_d7c - 1e-9:
                continue

            # Outermost via in group clearable at actual _ext_d7c → extension endpoint
            _clearable7c = [_ov for _ov, _gr in zip(_group7c, _gr7c)
                            if _ext_d7c - _ov[2] - _gr - _shw7c >= CLEARANCE - 1e-9]
            if not _clearable7c:
                continue
            _outmost7c  = max(_clearable7c, key=lambda x: x[0])
            _ext_lat7c  = _outmost7c[1] + _ls7c * _outmost7c[4]

            # Clip extension: if any signal via in the lateral sweep path cannot be
            # cleared vertically at _ext_d7c, stop before the nearest such via.
            _blocked_lat7c = None
            for _gj7c_sw in signal_indices:
                if (_gj7c_sw == _gi7c or _gj7c_sw not in final_placed
                        or _gj7c_sw in keepout_set):
                    continue
                _pvj_sw = face_pads[_gj7c_sw]
                if _pvj_sw.net_name in _skip_nets or not _pvj_sw.net_name:
                    continue
                _vxj_sw, _vyj_sw = final_placed[_gj7c_sw]
                _vlat_sw = _vxj_sw * ldx + _vyj_sw * ldy
                _dj_sw   = ((_vxj_sw - _pvj_sw.pad_x) * edx
                            + (_vyj_sw - _pvj_sw.pad_y) * edy)
                _pcr_sw  = _pvj_sw.via_drill_mm / 2.0 + ca
                if _ls7c * (_vlat_sw - _v_lat7c) < 1e-9:
                    continue
                if _ls7c * (_ext_lat7c - _vlat_sw) < -1e-9:
                    continue
                if abs(_ext_d7c - _dj_sw) - _pcr_sw - _shw7c >= CLEARANCE - 1e-9:
                    continue
                _clip = _vlat_sw - _ls7c * (_pcr_sw + _shw7c + CLEARANCE)
                if _blocked_lat7c is None or _ls7c * (_blocked_lat7c - _clip) > 1e-9:
                    _blocked_lat7c = _clip
            if _blocked_lat7c is not None:
                if _ls7c * (_blocked_lat7c - _v_lat7c) < 1e-9:
                    continue
                _ext_lat7c = _blocked_lat7c

            # Skip if clipping prevented reaching the outermost clearable via
            if _ls7c * (_ext_lat7c - _outmost7c[1]) < -1e-9:
                continue

            # Only extend if endpoint is actually further out than current stub tip
            _stub_lat7c = _v_lat7c + _ls7c * _lo7c
            if _ls7c * (_ext_lat7c - _stub_lat7c) <= 1e-6:
                continue

            # Update final_placed if depth changed (deepen or shallow to _req_d7c)
            if abs(_ext_d7c - _cur_d7c) > 1e-9:
                final_placed[_gi7c] = (
                    _pv7c.pad_x + _ls7c * _lo7c * ldx + edx * _ext_d7c,
                    _pv7c.pad_y + _ls7c * _lo7c * ldy + edy * _ext_d7c,
                )
                _7c_any_changed = True

            # Store lateral extension endpoint
            _dlat7c = _ext_lat7c - _v_lat7c
            _pv7c.stub_ext_vx = _pv7c.pad_x + _dlat7c * ldx + edx * _ext_d7c
            _pv7c.stub_ext_vy = _pv7c.pad_y + _dlat7c * ldy + edy * _ext_d7c

        if not _7c_any_changed:
            break

    # ------------------------------------------------------------------
    # Post-processing: HS partner suppression
    # ------------------------------------------------------------------
    for global_i in list(keepout_set):
        if global_i in hs_partner:
            partner_i = hs_partner[global_i]
            keepout_set.add(partner_i)
            final_placed.pop(partner_i, None)

    # Mark implicit keepouts on the PendingVia objects
    for global_i in keepout_set:
        if hasattr(face_pads[global_i], 'implicit_keepout'):
            face_pads[global_i].implicit_keepout = True

    # ------------------------------------------------------------------
    # Stub-only pads: keep stub short, capped BEFORE any laterally-close via
    # ------------------------------------------------------------------
    for i, v in enumerate(face_pads):
        if not v.net_name or v.net_name.startswith('unconnected'):
            continue  # no-net or unconnected pad — not routable, skip entirely
        if i in bus_pad_set:
            continue
        if (v.ref, v.pad_num) in pending_set:
            continue  # via-bearing pad, handled above

        stub_hw   = v.neckdown_w_mm / 2.0
        v_lat     = v.pad_x * ldx + v.pad_y * ldy
        axial_ext = v.neckdown_len_mm  # minimum; keep as short as possible

        for gi, (vx, vy) in final_placed.items():
            pv        = face_pads[gi]
            placed_cr = pv.via_drill_mm / 2.0 + ca
            via_lat   = vx * ldx + vy * ldy
            if abs(via_lat - v_lat) < placed_cr + stub_hw + CLEARANCE:
                via_axial   = abs((vx - v.pad_x) * edx + (vy - v.pad_y) * edy)
                axial_limit = via_axial - placed_cr - stub_hw - CLEARANCE
                if axial_limit > v.neckdown_len_mm:
                    axial_ext = min(axial_ext, axial_limit)
                # else via is within neckdown range — unavoidable at minimum stub

        v.stub_only_vx = v.pad_x
        v.stub_only_vy = v.pad_y + edy * axial_ext

    # Store side-effect data for _run() and debug harness
    _face_fanout._last_bus_stubs   = bus_stubs_to_write
    _face_fanout._last_keepout_set = keepout_set

    # Return (v, vx, vy) for via-bearing pads; skip-net pads get stub_only attrs.
    result = []
    for gi, (vx, vy) in final_placed.items():
        if gi in keepout_set:
            continue
        pv = face_pads[gi]
        if pv.net_name in _skip_nets or gi in bus_escape_indices:
            # Fill-connected or bus-escape pad: emit stub to the
            # compaction-computed endpoint but place no via.
            pv.stub_only_vx = vx
            pv.stub_only_vy = vy
        else:
            result.append((pv, vx, vy))

    return result


# ---------------------------------------------------------------------------
# Stagger
# ---------------------------------------------------------------------------

def _stagger_vias(pending: List[PendingVia], ca: dict, clearance: float) -> None:
    """Greedy per-face via stagger (step 1g).

    For every group of pads sharing the same component ref and escape direction,
    assigns the minimum neckdown_len_mm so no two vias on the same face violate
    copper-to-copper clearance.  Updates pv.neckdown_len_mm and pv.via_x/via_y
    in-place.  Skips pads with face_fanout_assigned=True.

    Callable independently so debug scripts and the main _run() share one
    implementation.
    """
    from collections import defaultdict
    _sg_hvpd = ca["hs_via_drill_mm"] + 2 * ca["hs_via_annular_ring_mm"]
    _sg_nvpd = ca["via_drill_mm"]     + 2 * ca["via_annular_ring_mm"]
    _sg_clr  = ca["via_clearance_mm"]

    _face_groups: Dict[tuple, List[PendingVia]] = defaultdict(list)
    for _pv_sg in pending:
        if _pv_sg.face_fanout_assigned:
            continue
        _face_groups[(_pv_sg.ref,
                      _pv_sg.escape_dx,
                      _pv_sg.escape_dy)].append(_pv_sg)

    for _fkey, _fgrp in _face_groups.items():
        _edx_sg, _edy_sg = _fkey[1], _fkey[2]
        _ldx_sg, _ldy_sg = -_edy_sg, _edx_sg   # lateral unit vector

        _fgrp.sort(key=lambda pv: pv.pad_x * _ldx_sg + pv.pad_y * _ldy_sg)

        # _placed: list of (pad_lat, assigned_neckdown, via_copper_d, pv_object)
        _placed_sg: List[tuple] = []

        for _pv_sg in _fgrp:
            _pad_lat_sg = _pv_sg.pad_x * _ldx_sg + _pv_sg.pad_y * _ldy_sg
            _lat_sg = _pad_lat_sg + _pv_sg.corner_lat_offset_mm
            _vpd_sg = (_sg_hvpd if _pv_sg.priority == PRIORITY_HS
                       else _sg_nvpd)
            _sig_trace_w = ca.get("signal_trace_width_mm", 0.20)

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

            if _pv_sg.cluster_real_pads:
                # Cluster via: herringbone stubs, no external trace threads between
                # pad edge and via — skip the trace-passage margin.
                _nl_sg = max(_pv_sg.neckdown_len_mm,
                             _pad_half_esc + _sg_clr + _vpd_sg / 2.0)
            else:
                _nl_sg = max(_pv_sg.neckdown_len_mm,
                             _pad_half_esc + _sg_clr + _sig_trace_w + _sg_clr + _vpd_sg / 2.0)

            for _nb_pad_lat, _nb_nl, _nb_vpd, _nb_pv in _placed_sg:
                _nb_lat = _nb_pad_lat + _nb_pv.corner_lat_offset_mm
                _dx_sg  = abs(_lat_sg - _nb_lat)
                _req_sg = _vpd_sg / 2.0 + _nb_vpd / 2.0 + _sg_clr
                if _dx_sg >= _req_sg:
                    continue
                _dy_sg    = math.sqrt(max(0.0, _req_sg ** 2 - _dx_sg ** 2))
                _nl_close = _nb_nl - _dy_sg
                if _nl_sg > _nl_close:
                    _nl_sg = max(_nl_sg, _nb_nl + _dy_sg)

            if _nl_sg > _pv_sg.neckdown_len_mm + 1e-6:
                _old_sg = _pv_sg.neckdown_len_mm
                _pv_sg.neckdown_len_mm = _nl_sg
                _pv_sg.via_x, _pv_sg.via_y = _via_corner(_pv_sg)
                print(f"  [stagger] {_pv_sg.ref}/{_pv_sg.pad_num} "
                      f"({_pv_sg.net_name}): "
                      f"neckdown {_old_sg:.3f}→{_nl_sg:.3f}mm")

            _placed_sg.append((_pad_lat_sg, _pv_sg.neckdown_len_mm, _vpd_sg, _pv_sg))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _run(board, apply: bool, max_passes: int = 20, live: bool = False,
         _debug_return_pending: bool = False,
         _debug_return_computed: bool = False):
    clearance      = cfg.CLEARANCE_AUDIT["via_clearance_mm"]
    min_annular_mm = cfg.CLEARANCE_AUDIT.get("via_annular_ring_min_mm", 0.10)
    no_via         = set(cfg.CLEARANCE_AUDIT.get("via_keepout_exclude_refs", []))
    skip_nets      = set(getattr(cfg, "FANOUT_VIA_SKIP_NETS", []))

    # Reset per-run accumulators (populated by _face_fanout call site)
    _run._all_bus_stubs        = []
    _run._all_bus_stubs_by_ref = {}   # same segments, keyed by footprint ref
    _run._all_skip_net_pvs     = []   # PendingVia stubs for skip-net pads on co-opt faces

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
    #
    # Exception: radial fanout components — P and N each have their own correct
    # radial direction.  Aligning N to P would overwrite N's direction and cause
    # both to escape the same way, colliding.
    from collections import defaultdict
    _fp_by_ref: Dict[str, object] = {
        fp.GetReference(): fp for fp in board.GetFootprints()
    }
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
            _fp_1b = _fp_by_ref.get(_ref)
            if _fp_1b is not None and _is_radial_fanout_fp(_fp_1b):
                _hs_pairs_aligned.add((_ref, _p_net, _n_net))
                continue  # radial escape — each pad keeps its own direction
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
                _fp_2b = _fp_by_ref.get(_ref)
                if _fp_2b is not None and _is_radial_fanout_fp(_fp_2b):
                    break  # radial escape — each pad keeps its own direction
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
    # Sandwiched non-HS pad detection: non-HS pads flanked by HS pairs
    # on the same chip face cannot escape outward through the HS via wall
    # (insufficient lateral gap at 0.4mm pitch). Mark them sandwiched=True
    # so they are excluded from proximity via sharing suppression — they
    # still need their own via attempt (or become implicit keepouts). Their
    # traces must route around flanking HS vias via route_critical.py.
    #
    # Runs BEFORE p-offset so the p-offset direction can be flipped when
    # the natural offset direction would push a P-via toward a sandwiched pad.
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
        _pv.sandwiched = True
        print(f"  [sandwiched] {_pv.ref}/{_pv.pad_num} ({_pv.net_name}): sandwiched")

    # Save real pad positions now (before cluster merging at step 1f changes pad_x to centroid).
    _sw_real_pads = [
        (pv.ref, pv.pad_num, pv.net_name, pv.netclass,
         pv.pad_x, pv.pad_y, pv.escape_dx, pv.escape_dy,
         pv.neckdown_w_mm, pv.pad_layer_id)
        for pv in pending if pv.sandwiched
    ]
    # HS centroid per face — for innermost-first ordering in _sandwiched_trace_stubs.
    _sw_centroid_by_face: Dict[tuple, float] = {}
    for _pv2 in pending:
        if _pv2.priority != PRIORITY_HS:
            continue
        _fk2 = (_pv2.ref, round(_pv2.escape_dx, 4), round(_pv2.escape_dy, 4))
        _lat_proj2 = (_pv2.pad_x * (-_pv2.escape_dy)
                      + _pv2.pad_y * _pv2.escape_dx)
        _sw_centroid_by_face.setdefault(_fk2, []).append(_lat_proj2)
    _sw_centroid_by_face = {k: sum(v) / len(v)
                             for k, v in _sw_centroid_by_face.items()}

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
        _fp_pv = _fp_by_ref.get(_pv.ref)
        if _fp_pv and _is_radial_fanout_fp(_fp_pv):
            continue  # radial fanout: P and N escape at distinct radial angles; lat_off undefined
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
                # If the natural p-offset direction would push P toward a physically
                # sandwiched pad, flip it so P moves away.  Only fires for pads
                # that sandwiched detection has already flagged — these are non-HS
                # pads trapped between HS pairs with no step-1e escape.
                # HS N pads are NOT included: they use step-1e and do not need the
                # P via to move; including them causes cascade flips that block
                # adjacent N pads (J_DSI1 0.5mm pitch geometry is fundamentally
                # unroutable for every N pad simultaneously at these via sizes).
                _sw_in_pdir = any(
                    _sw.sandwiched
                    and _sw.ref == _pv_p_off.ref
                    and abs(_sw.escape_dx - _pv_p_off.escape_dx) < 0.01
                    and abs(_sw.escape_dy - _pv_p_off.escape_dy) < 0.01
                    and 0 < _lat_sign * (
                        (_sw.pad_x * _ldx_off + _sw.pad_y * _ldy_off) - _p_lat
                    ) < _pad_pitch * 2
                    for _sw in pending
                    if _sw is not _pv_p_off
                )
                if _sw_in_pdir:
                    _lat_sign = -_lat_sign
                    print(f"  [p-offset-flip] {_pv_p_off.ref}/{_pv_p_off.pad_num} "
                          f"({_pv_p_off.net_name}): direction flipped away from sandwiched pad")
                _pv_p_off.corner_lat_offset_mm = _lat_sign * _min_lat
                _pv_p_off.via_x, _pv_p_off.via_y = _via_corner(_pv_p_off)
                print(f"  [p-offset] {_pv_p_off.ref}/{_pv_p_off.pad_num}"
                      f" ({_pv_p_off.net_name}): lat_off"
                      f" {_lat_sign * _min_lat:+.3f}mm"
                      f" (pitch={_pad_pitch:.3f}mm min_lat={_min_lat:.3f}mm)")

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
    pending = _suppress_proximity_via_sharing(pending, clearance, _pad_obs_early, _fp_by_ref)

    if _debug_return_pending:
        return pending, _pad_obs_early, _fp_by_ref, clearance, skip_nets

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
    # 1f-co. Co-optimized multi-pass face fanout for dense faces
    # ------------------------------------------------------------------
    # For each radial-fanout component face, check if the stub-to-adjacent-
    # pad-copper gap is too narrow for any trace to pass.  If so, call
    # _face_fanout to assign via positions using the multi-pass outside-in
    # algorithm.  Marks v.face_fanout_assigned=True and sets v.via_x,
    # v.via_y.  Steps 1g, 1g-2d, and run_passes() skip these pads.
    _coopt_sg_clr = _ca["via_clearance_mm"]

    _coopt_by_face: Dict[tuple, List[PendingVia]] = defaultdict(list)
    for _pv_co in pending:
        _fp_co = _fp_by_ref.get(_pv_co.ref)
        if (_fp_co and _is_radial_fanout_fp(_fp_co)
                and not _pv_co.implicit_keepout
                and not _pv_co.via_in_pad):
            _fkey_co = (_pv_co.ref,
                        round(_pv_co.escape_dx, 4),
                        round(_pv_co.escape_dy, 4))
            _coopt_by_face[_fkey_co].append(_pv_co)

    for _fkey_co, _face_grp_co in _coopt_by_face.items():
        _co_ref  = _fkey_co[0]
        _co_edx  = _fkey_co[1]
        _co_edy  = _fkey_co[2]
        if not _face_needs_coopt(_face_grp_co, _coopt_sg_clr):
            continue

        # Build pending_set for this call: pads in pending that get a via, PLUS
        # skip-net pads augmented below so they enter signal_indices and get
        # stub_only_vx/vy set by _face_fanout.  Being in pending_set does not
        # cause a via to be placed — the net_name-in-skip_nets check inside
        # _face_fanout redirects them to stub_only output regardless.
        _co_pending_set = {(_pv.ref, _pv.pad_num) for _pv in pending}
        # ca = annular ring for the first pad's via size
        _co_ca = _face_grp_co[0].via_annular_mm

        # Augment face group with skip-net pads on this face.
        _co_skip_pvs = _build_skip_net_pvs(
            _fp_by_ref.get(_co_ref), _face_grp_co, _co_edx, _co_edy, skip_nets
        )

        _face_grp_aug = _face_grp_co + _co_skip_pvs
        # Add skip-net pvs to pending_set so _face_fanout includes them in
        # signal_indices and sets stub_only_vx/vy on them.
        _co_pending_set.update((_pv.ref, _pv.pad_num) for _pv in _co_skip_pvs)

        print(f"  [face-fanout] {_co_ref} face ({_co_edx:.0f},{_co_edy:.0f}): "
              f"{len(_face_grp_co)} signal + {len(_co_skip_pvs)} skip-net pads"
              f" → co-optimized multi-pass")

        # Sort augmented face group outside-in before passing to _face_fanout
        _co_ldx, _co_ldy = -_co_edy, _co_edx
        _co_fc = sum(v.pad_x * _co_ldx + v.pad_y * _co_ldy for v in _face_grp_aug) / len(_face_grp_aug)
        _face_grp_aug.sort(key=lambda v: -abs(v.pad_x * _co_ldx + v.pad_y * _co_ldy - _co_fc))

        _co_assignments = _face_fanout(
            _face_grp_aug, _pad_obs_early, _co_pending_set,
            _coopt_sg_clr, _co_ca, _co_edx, _co_edy,
        )

        # Collect bus stubs and keepout set from side-effect storage
        _co_bus_stubs = getattr(_face_fanout, '_last_bus_stubs', [])
        _co_keepout_set = getattr(_face_fanout, '_last_keepout_set', set())

        for _pv_co, _vx_co, _vy_co in _co_assignments:
            _pv_co.via_x = _vx_co
            _pv_co.via_y = _vy_co
            _pv_co.face_fanout_assigned = True
            _pv_co.neckdown_len_mm = math.hypot(
                _vx_co - _pv_co.pad_x, _vy_co - _pv_co.pad_y)

        # Pads that were NOT placed and NOT keepout go through normal 1g/1g-2d stagger.
        # (These are sandwiched pads that _face_fanout classifies as obstacles only.)
        # face_fanout_assigned is set ONLY on placed pads and keepout pads here.
        for _pv_co in _face_grp_co:
            if _pv_co.implicit_keepout:
                _pv_co.face_fanout_assigned = True  # keepout — skip all further stagger

        # Store skip-net stub pvs for section 6c emission
        _run._all_skip_net_pvs.extend(_co_skip_pvs)

        # Store bus stubs for section 6 emission
        _run._all_bus_stubs.extend(_co_bus_stubs)
        _run._all_bus_stubs_by_ref.setdefault(_co_ref, []).extend(_co_bus_stubs)

        _placed_co = len(_co_assignments)
        _ko_co = sum(1 for v in _face_grp_co if v.implicit_keepout)
        print(f"  [face-fanout] {_co_ref} face ({_co_edx:.0f},{_co_edy:.0f}): "
              f"{_placed_co} placed, {_ko_co} keepout, "
              f"{len(_co_bus_stubs)} bus-stub segments")

    # ------------------------------------------------------------------
    # 1f-sub. Tight-pitch sub-group fanout (all footprint types)
    # ------------------------------------------------------------------
    # For faces not handled by _face_needs_coopt above, detect contiguous
    # tight-pitch sub-groups and apply _face_fanout to each one.
    # Trigger: tight pad pitch (general), not component type or HS nets.

    _sub_by_face: Dict[tuple, List[PendingVia]] = defaultdict(list)
    for _pv_sub in pending:
        if _pv_sub.face_fanout_assigned or _pv_sub.implicit_keepout or _pv_sub.via_in_pad:
            continue
        _fkey_sub = (_pv_sub.ref,
                     round(_pv_sub.escape_dx, 4),
                     round(_pv_sub.escape_dy, 4))
        _sub_by_face[_fkey_sub].append(_pv_sub)

    for _fkey_sub, _face_grp_sub in _sub_by_face.items():
        _sub_ref = _fkey_sub[0]
        _sub_edx = _fkey_sub[1]
        _sub_edy = _fkey_sub[2]

        _sub_groups = _find_tight_subgroups(_face_grp_sub, _coopt_sg_clr)
        if not _sub_groups:
            continue

        for _sg_pads in _sub_groups:
            if len(_sg_pads) < 2:
                continue

            _sg_pending_set = {(_pv.ref, _pv.pad_num) for _pv in pending}
            _sg_ca = _sg_pads[0].via_annular_mm

            _sg_ldx, _sg_ldy = -_sub_edy, _sub_edx

            # Augment with skip-net pads that are laterally adjacent to the
            # sub-group (within one pad-pitch of its edge) so they serve as
            # boundary anchors.  A loose margin keeps pads far along the face
            # from generating bus stubs that reach unrelated pads.
            _fp_sg = _fp_by_ref.get(_sub_ref)
            _sg_all_skip = _build_skip_net_pvs(
                _fp_sg, _sg_pads, _sub_edx, _sub_edy, skip_nets)
            _sg_min_lat = min(v.pad_x * _sg_ldx + v.pad_y * _sg_ldy for v in _sg_pads)
            _sg_max_lat = max(v.pad_x * _sg_ldx + v.pad_y * _sg_ldy for v in _sg_pads)
            _sg_lat_margin = 1.0  # mm — roughly 2× max expected pad pitch
            _sg_skip_pvs = [
                pv for pv in _sg_all_skip
                if (_sg_min_lat - _sg_lat_margin
                    <= pv.pad_x * _sg_ldx + pv.pad_y * _sg_ldy
                    <= _sg_max_lat + _sg_lat_margin)
            ]
            _sg_pads_aug = _sg_pads + _sg_skip_pvs
            _sg_fc = sum(v.pad_x * _sg_ldx + v.pad_y * _sg_ldy for v in _sg_pads_aug) / len(_sg_pads_aug)
            _sg_pads_aug.sort(key=lambda v: -abs(v.pad_x * _sg_ldx + v.pad_y * _sg_ldy - _sg_fc))

            print(f"  [sub-fanout] {_sub_ref} face ({_sub_edx:.0f},{_sub_edy:.0f}): "
                  f"{len(_sg_pads)} signal + {len(_sg_skip_pvs)} skip-net pad(s) → tight-pitch sub-group")

            _sg_assignments = _face_fanout(
                _sg_pads_aug, _pad_obs_early, _sg_pending_set,
                _coopt_sg_clr, _sg_ca, _sub_edx, _sub_edy,
            )
            _sg_bus_stubs = getattr(_face_fanout, '_last_bus_stubs', [])

            for _pv_sg, _vx_sg, _vy_sg in _sg_assignments:
                _pv_sg.via_x = _vx_sg
                _pv_sg.via_y = _vy_sg
                _pv_sg.face_fanout_assigned = True
                _pv_sg.neckdown_len_mm = math.hypot(
                    _vx_sg - _pv_sg.pad_x, _vy_sg - _pv_sg.pad_y)

            for _pv_sg in _sg_pads_aug:
                if _pv_sg.implicit_keepout:
                    _pv_sg.face_fanout_assigned = True

            _run._all_bus_stubs.extend(_sg_bus_stubs)
            _run._all_bus_stubs_by_ref.setdefault(_sub_ref, []).extend(_sg_bus_stubs)

            _placed_sg_n = len(_sg_assignments)
            _ko_sg_n = sum(1 for v in _sg_pads_aug if v.implicit_keepout)
            print(f"  [sub-fanout] {_sub_ref} face ({_sub_edx:.0f},{_sub_edy:.0f}): "
                  f"{_placed_sg_n} placed, {_ko_sg_n} keepout, "
                  f"{len(_sg_bus_stubs)} bus-stub segments")

    # ------------------------------------------------------------------
    # 1g. Greedy per-face via stagger (delegated to _stagger_vias)
    # ------------------------------------------------------------------
    _stagger_vias(pending, _ca, clearance)

    # ------------------------------------------------------------------
    # 1g-2d. 2D stagger for radial fanout components
    # ------------------------------------------------------------------
    # After snap removal, each radial fanout pad has a unique float escape
    # direction → 1-member face groups above → existing stagger does nothing.
    # This pass groups them by ref alone and uses a closed-form 2D constraint
    # so no two via circles on the same component overlap.
    #
    # For via i vs already-assigned via j at position (vx_j, vy_j):
    #   A = pad_xi - vx_j,  B = pad_yi - vy_j,  D = A²+B²
    #   sep = r_i + r_j + clearance
    #   If D >= sep²: no constraint from j.
    #   C = A*edx_i + B*edy_i
    #   min_n_i = sqrt(max(0, C²+sep²−D)) − C
    # Process innermost-first (ascending pad distance from component centre).
    # ------------------------------------------------------------------

    def _stagger_neckdown_2d(vi: PendingVia, assigned: list) -> float:
        n_min = vi.neckdown_len_mm
        r_i = vi.via_drill_mm / 2.0 + vi.via_annular_mm
        for vj in assigned:
            r_j = vj.via_drill_mm / 2.0 + vj.via_annular_mm
            sep = r_i + r_j + _hs_clr
            vx_j, vy_j = vj.via_x, vj.via_y  # use actual position (col-exit vias != _via_corner)
            A = vi.pad_x - vx_j
            B = vi.pad_y - vy_j
            D = A * A + B * B
            C = A * vi.escape_dx + B * vi.escape_dy
            # Minimum squared distance along the escape path: at n=−C when C<0 (path
            # passes near vj), else at n=0.  Only skip when that minimum clears sep.
            _d2_min = D - C * C if C < 0.0 else D
            if _d2_min >= sep * sep:
                continue
            n_req = math.sqrt(max(0.0, C * C + sep * sep - D)) - C
            if n_req > n_min:
                n_min = n_req
        return n_min

    _radial_face_groups: Dict[str, List[PendingVia]] = defaultdict(list)
    for _pv_r2 in pending:
        if _pv_r2.face_fanout_assigned:
            continue
        _fp_r2 = _fp_by_ref.get(_pv_r2.ref)
        if _fp_r2 and _is_radial_fanout_fp(_fp_r2):
            _radial_face_groups[_pv_r2.ref].append(_pv_r2)

    for _rref, _rgrp in _radial_face_groups.items():
        if len(_rgrp) < 2:
            continue
        _rfp = _fp_by_ref.get(_rref)
        if _rfp:
            _rbbox = _rfp.GetBoundingBox()
            _rcx = pcbnew.ToMM(_rbbox.GetCenter().x)
            _rcy = pcbnew.ToMM(_rbbox.GetCenter().y)
            _rgrp.sort(key=lambda pv: math.hypot(pv.pad_x - _rcx, pv.pad_y - _rcy))
        _assigned_r2: List[PendingVia] = []
        for _pv_r2 in _rgrp:
            _nl_r2 = _stagger_neckdown_2d(_pv_r2, _assigned_r2)
            if _nl_r2 > _pv_r2.neckdown_len_mm + 1e-6:
                _old_r2 = _pv_r2.neckdown_len_mm
                _pv_r2.neckdown_len_mm = _nl_r2
                _pv_r2.via_x, _pv_r2.via_y = _via_corner(_pv_r2)
                print(f"  [stagger-2d] {_pv_r2.ref}/{_pv_r2.pad_num} "
                      f"({_pv_r2.net_name}): neckdown {_old_r2:.3f}→{_nl_r2:.3f}mm")

            # Phase 3.5 Part A: verify the neckdown stub segment clears all
            # adjacent pad copper.  Increasing neckdown does NOT help for
            # corner pads (the minimum lateral distance to the adjacent pad is
            # at the stub START and is determined by geometry, not length).
            # Scan from n_min upward to confirm; if all n fail → mark keepout.
            # IMPORTANT: check using the snapped escape direction (same direction
            # _make_neckdown actually emits the stub), not the raw radial direction.
            _35_trace_hw = _pv_r2.neckdown_w_mm / 2.0
            _35_snap_a = (round(math.atan2(_pv_r2.escape_dy, _pv_r2.escape_dx)
                                / (math.pi / 4.0)) * (math.pi / 4.0))
            _35_edx_s = math.cos(_35_snap_a)
            _35_edy_s = math.sin(_35_snap_a)
            _35_n = _pv_r2.neckdown_len_mm
            _35_stub_ok = False
            _35_max_n = 15.0
            while _35_n <= _35_max_n + 1e-9:
                _35_vx = _pv_r2.pad_x + _35_edx_s * _35_n
                _35_vy = _pv_r2.pad_y + _35_edy_s * _35_n
                _35_ok = True
                for _obs35 in _pad_obs_early:
                    if _obs35.net_name == _pv_r2.net_name:
                        continue
                    if _obs35.ref != _pv_r2.ref:
                        continue  # only check adjacent pads on same footprint
                    if _obs35.bbox is not None:
                        if _seg_bbox_dist(_pv_r2.pad_x, _pv_r2.pad_y,
                                          _35_vx, _35_vy,
                                          _obs35.bbox) < _35_trace_hw + _hs_clr:
                            _35_ok = False
                            break
                    else:
                        if (_pt_to_seg_dist(_obs35.cx, _obs35.cy,
                                            _pv_r2.pad_x, _pv_r2.pad_y,
                                            _35_vx, _35_vy)
                                < _obs35.r + _35_trace_hw + _hs_clr):
                            _35_ok = False
                            break
                if _35_ok:
                    if _35_n > _pv_r2.neckdown_len_mm + 1e-6:
                        _pv_r2.neckdown_len_mm = _35_n
                        _pv_r2.via_x = _35_vx
                        _pv_r2.via_y = _35_vy
                        print(f"  [stagger-2d-stub] {_pv_r2.ref}/{_pv_r2.pad_num} "
                              f"({_pv_r2.net_name}): neckdown extended to {_35_n:.3f}mm "
                              f"for stub clearance")
                    _35_stub_ok = True
                    break
                _35_n += STEP_MM

            if not _35_stub_ok:
                # Stub clearance geometrically insufficient — mark as implicit keepout.
                # Section 4a will attempt col-exit with the full obstacle set.
                print(f"  [stagger-2d-ko] {_pv_r2.ref}/{_pv_r2.pad_num} "
                      f"({_pv_r2.net_name}): stub clearance failed, marking keepout")
                _pv_r2.implicit_keepout = True
                _pv_r2.via_x = _pv_r2.pad_x
                _pv_r2.via_y = _pv_r2.pad_y
                if _pv_r2.priority == PRIORITY_HS:
                    for _sfx35, _opp35 in (("_P", "_N"), ("_N", "_P"),
                                           ("+", "-"), ("-", "+")):
                        if _pv_r2.net_name.endswith(_sfx35):
                            _pnet35 = _pv_r2.net_name[:-len(_sfx35)] + _opp35
                            for _pg35 in _rgrp:
                                if (_pg35.ref == _pv_r2.ref
                                        and _pg35.net_name == _pnet35
                                        and not _pg35.implicit_keepout):
                                    _pg35.implicit_keepout = True
                                    _pg35.via_x = _pg35.pad_x
                                    _pg35.via_y = _pg35.pad_y
                                    print(f"  [pair-error] {_pv_r2.ref}/{_pv_r2.pad_num} "
                                          f"({_pv_r2.net_name}): stagger stub fail "
                                          f"→ partner {_pg35.ref}/{_pg35.pad_num} "
                                          f"({_pg35.net_name}) also suppressed")
                                    break
                continue  # do not add to _assigned_r2

            _assigned_r2.append(_pv_r2)

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

    # Emit real PCB_TRACKs for sandwiched pad escape corridors and inject
    # matching StubSeg obstacles.  Using real positions saved before cluster
    # merging so corridor centres are correct.  Tracks are written to board
    # now (apply mode) so run_passes sees them as physical obstacles; they
    # are re-emitted in section 6 after section 5 removes them.
    # Corridor-width filter: skip sandwiched stubs that would permanently
    # block adjacent HS via placement.  A 5mm straight stub at lateral position X
    # blocks HS vias whose pad is within (stub_hw_bare + hs_via_copper_r + clearance)
    # of X (_clear_of_stub_segs threshold).  For U3 north face at 0.400mm pitch,
    # threshold = 0.100 + 0.300 + 0.150 = 0.550mm > 0.400mm — every stub blocks
    # its immediate HS neighbours.  Skip such stubs; stagger-2d places HS vias
    # in the now-clear corridor.  Skipped sandwiched pads remain in _sw_real_pads
    # so section 6 handles their escape trace + via independently.
    _hs_pads_by_face: Dict[tuple, list] = {}
    for _pv_sf in pending:
        if _pv_sf.priority == PRIORITY_HS:
            _fk_sf = (_pv_sf.ref,
                      round(_pv_sf.escape_dx, 4),
                      round(_pv_sf.escape_dy, 4))
            _hs_pads_by_face.setdefault(_fk_sf, []).append(_pv_sf)
    _hs_drill_sf, _hs_annular_sf = via_params(PRIORITY_HS)
    _hs_via_copper_r_sf = _hs_drill_sf / 2.0 + _hs_annular_sf
    _sw_real_pads_for_stubs = []
    for _rec_sf in _sw_real_pads:
        (_rref_sf, _rpad_sf, _rnet_sf, _rnc_sf,
         _rpx_sf, _rpy_sf, _redx_sf, _redy_sf,
         _rnw_sf, _rlid_sf) = _rec_sf
        _fk_sf = (_rref_sf,
                  round(_redx_sf, 4),
                  round(_redy_sf, 4))
        _hs_face_sf = _hs_pads_by_face.get(_fk_sf, [])
        _lat_dx_sf, _lat_dy_sf = -_redy_sf, _redx_sf  # 90° CCW from escape
        _sw_lat_pos_sf = _rpx_sf * _lat_dx_sf + _rpy_sf * _lat_dy_sf
        _corridor_thr_sf = _rnw_sf / 2.0 + _hs_via_copper_r_sf + clearance
        _corridor_clear_sf = all(
            abs((_hp_sf.pad_x * _lat_dx_sf + _hp_sf.pad_y * _lat_dy_sf)
                - _sw_lat_pos_sf) >= _corridor_thr_sf
            for _hp_sf in _hs_face_sf
        )
        if _corridor_clear_sf:
            _sw_real_pads_for_stubs.append(_rec_sf)
        else:
            print(f"  [sandwich-skip] {_rref_sf}/{_rpad_sf} ({_rnet_sf}): "
                  f"corridor <{_corridor_thr_sf:.3f}mm — stub skipped, "
                  f"HS vias placed by stagger-2d")

    sw_stubs, sw_tracks = _sandwiched_trace_stubs(
        board, _sw_real_pads_for_stubs, _sw_centroid_by_face, clearance, reach_mm=5.0
    )
    if sw_stubs:
        n_sw_pads = len({r[0] + '/' + r[1] for r in _sw_real_pads_for_stubs})
        print(f"  [sandwiched-pre] {len(sw_stubs)} corridor stub(s) for "
              f"{n_sw_pads} sandwiched pad(s)")
        board_stubs = sw_stubs + board_stubs
        if apply and sw_tracks:
            for _t in sw_tracks:
                board.Add(_t)
            board_stubs = _load_board_tracks(board, clearance)
            print(f"  [sandwiched-pre] {len(sw_tracks)} escape trace(s) written to board")

    for line in run_passes(pending, pad_obs, clearance, min_annular_mm, max_passes,
                           edge_segs=edge_segs, board_stubs=board_stubs):
        print(line)

    n_tight = _tighten_vias(pending, pad_obs, board_stubs, clearance, edge_segs)
    if n_tight:
        print(f"  [tighten] {n_tight} via(s) pulled closer to component.")

    # ------------------------------------------------------------------
    # 4a. Compute keepout escape lengths (after all vias are in final positions)
    #
    # Process sequentially: after computing each keepout's escape length and
    # via position, add that via to placed_vias so subsequent keepouts treat
    # it as an obstacle.  This prevents keepout vias from shorting each other.
    # ------------------------------------------------------------------
    placed_vias = [v for v in pending if not v.implicit_keepout and not v.via_in_pad]
    _keepout_escape_data: list = []  # (via, escape_len_mm) for non-sandwiched keepouts
    _ko_trace_segs: list = []  # (x1,y1,x2,y2,trace_hw,net_name) — keepout escape trace segs
    keepouts = [v for v in pending if v.implicit_keepout]
    for v in keepouts:
        if v.sandwiched:
            continue  # sandwiched pads handled separately via _sw_real_pads
        trace_hw = v.neckdown_w_mm / 2.0
        ko_via_copper = v.via_drill_mm / 2.0 + v.via_annular_mm
        esc_len = _keepout_escape_length(
            v.pad_x, v.pad_y, v.escape_dx, v.escape_dy,
            trace_hw, clearance, placed_vias,
            via_copper_keepout=ko_via_copper,
            pad_obs=pad_obs, board_stubs=board_stubs,
            net_name=v.net_name,
        )
        # Snap direction for via position registration (must match _make_neckdown emission)
        _ko_snap_a = (round(math.atan2(v.escape_dy, v.escape_dx) / (math.pi / 4.0))
                      * (math.pi / 4.0))
        _ko_edx_s = math.cos(_ko_snap_a)
        _ko_edy_s = math.sin(_ko_snap_a)
        if esc_len < 0.05 and v.priority == PRIORITY_HS:
            # Phase 4: column-exit-first fallback for HS keepouts on radial fanout components.
            # For narrow pads where the straight radial corridor is blocked by sibling pads,
            # first exit in the face-normal direction (nearest 90° cardinal to escape), then
            # search radially from that exit point for a clear via position.
            _fp_v4 = _fp_by_ref.get(v.ref)
            if _fp_v4 and _is_radial_fanout_fp(_fp_v4):
                # Face normal = nearest 90° cardinal to escape
                _face_ang4 = round(math.atan2(v.escape_dy, v.escape_dx) / (math.pi / 2.0)) * (math.pi / 2.0)
                _col_dx4 = round(math.cos(_face_ang4))  # 0, 1, or -1
                _col_dy4 = round(math.sin(_face_ang4))  # 0, 1, or -1
                # Only proceed if footprint is at 0/90/180/270° rotation (±1° tolerance).
                # A diagonal footprint would make the col exit non-cardinal (PROHIBITED 3).
                _rot4 = _fp_v4.GetOrientation().AsDegrees() % 90.0
                if _rot4 > 1.0 and _rot4 < 89.0:
                    print(f"  [col-exit] {v.ref}/{v.pad_num}: footprint at non-cardinal "
                          f"rotation ({_rot4:.1f}°) — col-exit skipped")
                else:
                    _col_min4 = max(v.pad_w_mm / 2.0, v.pad_h_mm / 2.0) + clearance
                    _col_max4 = 2.0
                    _col_len4 = _col_min4
                    _found_col4 = False
                    while _col_len4 <= _col_max4 + 1e-9:
                        _col_ex4 = v.pad_x + _col_dx4 * _col_len4
                        _col_ey4 = v.pad_y + _col_dy4 * _col_len4
                        _seg_ok4 = True
                        for _obs4 in pad_obs:
                            if _obs4.net_name == v.net_name:
                                continue
                            if _obs4.bbox is not None:
                                if _seg_bbox_dist(v.pad_x, v.pad_y, _col_ex4, _col_ey4,
                                                  _obs4.bbox) < clearance:
                                    _seg_ok4 = False
                                    break
                            else:
                                if _pt_to_seg_dist(_obs4.cx, _obs4.cy,
                                                   v.pad_x, v.pad_y, _col_ex4, _col_ey4) < _obs4.r:
                                    _seg_ok4 = False
                                    break
                        if _seg_ok4:
                            # Also verify col exit segment clears placed via copper
                            for _pv4 in placed_vias:
                                if _pv4.net_name == v.net_name:
                                    continue
                                _pv4_r = (_pv4.via_drill_mm / 2.0 + _pv4.via_annular_mm
                                          + trace_hw + clearance)
                                if _pt_to_seg_dist(_pv4.via_x, _pv4.via_y,
                                                   v.pad_x, v.pad_y, _col_ex4, _col_ey4) < _pv4_r:
                                    _seg_ok4 = False
                                    break
                        if _seg_ok4:
                            # Also verify col exit segment clears placed via escape tracks.
                            # The col segment may cross a diagonal stub even when both via copper
                            # circles are far enough away — _seg_min_dist handles exact crossings.
                            for _pv4s in placed_vias:
                                if _pv4s.net_name == v.net_name:
                                    continue
                                for _stub4s in _stub_segs_for(_pv4s, clearance):
                                    if _seg_min_dist(v.pad_x, v.pad_y, _col_ex4, _col_ey4,
                                                     _stub4s.x1, _stub4s.y1,
                                                     _stub4s.x2, _stub4s.y2) < _stub4s.half_w + trace_hw:
                                        _seg_ok4 = False
                                        break
                                if not _seg_ok4:
                                    break
                        if _seg_ok4:
                            _col_esc_len4 = _keepout_escape_length(
                                _col_ex4, _col_ey4, v.escape_dx, v.escape_dy,
                                trace_hw, clearance, placed_vias,
                                via_copper_keepout=ko_via_copper,
                                pad_obs=pad_obs, board_stubs=board_stubs,
                                net_name=v.net_name,
                            )
                            if _col_esc_len4 >= 0.05:
                                esc_len = _col_esc_len4
                                v._col_exit = (_col_dx4, _col_dy4, _col_len4, _col_ex4, _col_ey4)
                                print(f"  [col-exit] {v.ref}/{v.pad_num} ({v.net_name}): "
                                      f"col {_col_len4:.3f}mm → escape {_col_esc_len4:.3f}mm")
                                _found_col4 = True
                                break
                        _col_len4 += STEP_MM
                    if not _found_col4:
                        print(f"  [col-exit] {v.ref}/{v.pad_num} ({v.net_name}): "
                              f"col-exit also failed — staying keepout")
        # PROHIBITED 7: when an HS keepout pad ends up with escape=0 after all
        # col-exit attempts, retroactively suppress any already-processed partner
        # keepout pad that placed a via.  Placing the partner via while leaving
        # this pad unresolved creates an incoherent pair handoff to route_highspeed.py.
        if esc_len < 0.05 and v.priority == PRIORITY_HS:
            for _sfx7k, _opp7k in (("_P", "_N"), ("_N", "_P"), ("+", "-"), ("-", "+")):
                if v.net_name.endswith(_sfx7k):
                    _pnet7k = v.net_name[:-len(_sfx7k)] + _opp7k
                    for _ki7k in range(len(_keepout_escape_data)):
                        _kv7k, _ke7k = _keepout_escape_data[_ki7k]
                        if (_kv7k.ref == v.ref
                                and _kv7k.net_name == _pnet7k
                                and _ke7k > 0.0):
                            _keepout_escape_data[_ki7k] = (_kv7k, 0.0)
                            if _kv7k in placed_vias:
                                placed_vias.remove(_kv7k)
                            _ko_trace_segs[:] = [_s for _s in _ko_trace_segs
                                                 if _s[5] != _pnet7k]
                            print(f"  [ko-pair-suppress] {v.ref}/{v.pad_num} "
                                  f"({v.net_name}): escape=0 → partner "
                                  f"{_kv7k.ref}/{_kv7k.pad_num} ({_pnet7k}) "
                                  f"also suppressed")
                            break
                    break
        _keepout_escape_data.append((v, esc_len))
        if esc_len >= 0.05:
            # Register this keepout's via as an obstacle for subsequent keepouts.
            # Use snapped escape direction — _make_neckdown emits in snapped direction,
            # so placed_vias must reflect the actual emitted via position.
            _ko_snap_a4 = (round(math.atan2(v.escape_dy, v.escape_dx) / (math.pi / 4.0))
                           * (math.pi / 4.0))
            _ko_edx_s4 = math.cos(_ko_snap_a4)
            _ko_edy_s4 = math.sin(_ko_snap_a4)
            if hasattr(v, '_col_exit'):
                _cdx4, _cdy4, _cl4, _cex4, _cey4 = v._col_exit
                v.via_x = _cex4 + _ko_edx_s4 * esc_len
                v.via_y = _cey4 + _ko_edy_s4 * esc_len
            else:
                v.via_x = v.pad_x + _ko_edx_s4 * esc_len
                v.via_y = v.pad_y + _ko_edy_s4 * esc_len
            # Post-check: via endpoint copper must clear all pad obstacles (not just trace
            # corridor). _keepout_escape_length only checks corridor width; the via annular
            # ring is larger and can land inside an adjacent pad even when the trace cleared.
            _ko_ep_r = ko_via_copper + clearance
            _ko_ep_ok = True
            for _obs_ep in pad_obs:
                if _obs_ep.net_name == v.net_name:
                    continue
                if _obs_ep.bbox is not None:
                    if _dist_point_to_bbox(v.via_x, v.via_y, _obs_ep.bbox) < _ko_ep_r:
                        _ko_ep_ok = False
                        break
                else:
                    if math.hypot(v.via_x - _obs_ep.cx, v.via_y - _obs_ep.cy) < _obs_ep.r + _ko_ep_r:
                        _ko_ep_ok = False
                        break
            # Trace-segment check: via copper must also clear escape traces of
            # previously-placed keepout pads (not in pad_obs — those are pads, not tracks).
            if _ko_ep_ok:
                for _seg_ko in _ko_trace_segs:
                    _skx1, _sky1, _skx2, _sky2, _sktw, _sknet = _seg_ko
                    if _sknet == v.net_name:
                        continue
                    if _pt_to_seg_dist(v.via_x, v.via_y,
                                       _skx1, _sky1, _skx2, _sky2) < ko_via_copper + _sktw + clearance:
                        _ko_ep_ok = False
                        # PROHIBITED 7: if this is an HS pad, retroactively suppress the
                        # partner keepout whose trace caused the conflict so neither ends
                        # up with a dangling via pointing at a phantom partner position.
                        if v.priority == PRIORITY_HS:
                            for _sfx7b, _opp7b in (("_P", "_N"), ("_N", "_P"),
                                                    ("+", "-"), ("-", "+")):
                                if v.net_name.endswith(_sfx7b):
                                    _pnet7b = v.net_name[:-len(_sfx7b)] + _opp7b
                                    for _ki7b in range(len(_keepout_escape_data)):
                                        _kv7b, _ke7b = _keepout_escape_data[_ki7b]
                                        if (_kv7b.ref == v.ref
                                                and _kv7b.net_name == _pnet7b
                                                and _ke7b > 0.0):
                                            _keepout_escape_data[_ki7b] = (_kv7b, 0.0)
                                            if _kv7b in placed_vias:
                                                placed_vias.remove(_kv7b)
                                            _ko_trace_segs[:] = [_s for _s in _ko_trace_segs
                                                                  if _s[5] != _pnet7b]
                                            print(f"  [ko-pair-suppress] "
                                                  f"{v.ref}/{v.pad_num} ({v.net_name}) "
                                                  f"via conflicts with "
                                                  f"{_kv7b.ref}/{_kv7b.pad_num} "
                                                  f"({_pnet7b}) trace — both suppressed")
                                            break
                                    break
                        break
            if _ko_ep_ok:
                placed_vias.append(v)
                # Register escape trace segments so subsequent keepout via-vs-trace checks
                # can detect conflicts. Uses snapped via position (same as section 4a set).
                _ko_tw_reg = v.neckdown_w_mm / 2.0
                if hasattr(v, '_col_exit'):
                    _, _, _, _cex_ks, _cey_ks = v._col_exit
                    _ko_trace_segs.append((v.pad_x, v.pad_y,
                                           _cex_ks, _cey_ks, _ko_tw_reg, v.net_name))
                    _ko_trace_segs.append((_cex_ks, _cey_ks,
                                           v.via_x, v.via_y, _ko_tw_reg, v.net_name))
                else:
                    _ko_trace_segs.append((v.pad_x, v.pad_y,
                                           v.via_x, v.via_y, _ko_tw_reg, v.net_name))
            else:
                esc_len = 0.0
                # Update the stored tuple — replace last entry
                if _keepout_escape_data and _keepout_escape_data[-1][0] is v:
                    _keepout_escape_data[-1] = (v, 0.0)

    # ------------------------------------------------------------------
    # 4. Result summary
    # ------------------------------------------------------------------
    vippo    = [v for v in pending if v.via_in_pad]
    placed   = [v for v in pending if not v.implicit_keepout]
    if vippo:
        print(f"\nVIPPO required — {len(vippo)} via-in-pad placement(s):")
        for v in vippo:
            print(f"  {v.ref}/{v.pad_num}  net={v.net_name}  "
                  f"annular={v.via_annular_mm:.3f}mm")
    if keepouts:
        print(f"\nImplicit keepouts — {len(keepouts)} pad(s) — escape traces will be emitted:")
        for v, esc_len in _keepout_escape_data:
            print(f"  {v.ref}/{v.pad_num}  net={v.net_name}  "
                  f"pad={v.pad_w_mm:.3f}×{v.pad_h_mm:.3f}mm  "
                  f"escape={esc_len:.3f}mm")
        sw_ko = [v for v in keepouts if v.sandwiched]
        for v in sw_ko:
            print(f"  {v.ref}/{v.pad_num}  net={v.net_name}  "
                  f"pad={v.pad_w_mm:.3f}×{v.pad_h_mm:.3f}mm  (sandwiched — handled separately)")
    print(f"\n{len(placed)} via(s) placed  "
          f"({len(vippo)} via-in-pad  "
          f"{len(placed)-len(vippo)} side-exit)  "
          f"{len(keepouts)} implicit keepout(s).")

    if _debug_return_computed:
        return (pending, _pad_obs_early, _fp_by_ref, clearance, skip_nets,
                dict(getattr(_run, '_all_bus_stubs_by_ref', {})),
                list(getattr(_run, '_all_skip_net_pvs', [])),
                _keepout_escape_data)

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

    # Emit escape traces + vias for keepout pads (non-sandwiched).
    if _keepout_escape_data:
        _ko_emit_count = 0
        for _kv, _klen in _keepout_escape_data:
            if _klen < 0.05:
                print(f"  [keepout-escape] {_kv.ref}/{_kv.pad_num}: escape length {_klen:.3f}mm too short, skipping")
                continue
            _ko_net = board.FindNet(_kv.net_name)
            if _ko_net is None:
                continue
            if hasattr(_kv, '_col_exit'):
                # Phase 4: two-segment col-exit stub
                _cdx_ko, _cdy_ko, _cl_ko, _cex_ko, _cey_ko = _kv._col_exit
                # Segment 1: pad → col exit point (cardinal direction, verified clear)
                _kt1 = pcbnew.PCB_TRACK(board)
                _kt1.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(_kv.pad_x), pcbnew.FromMM(_kv.pad_y)))
                _kt1.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(_cex_ko), pcbnew.FromMM(_cey_ko)))
                _kt1.SetWidth(pcbnew.FromMM(_kv.neckdown_w_mm))
                _kt1.SetLayer(_kv.pad_layer_id)
                _kt1.SetNet(_ko_net)
                board.Add(_kt1)
                # Segments 2+: col exit → via using _route_45deg_stub (0/45/90 compliant)
                # Use snapped escape direction — must match section 4a via_x/via_y so the
                # post-endpoint check and the emitted via land at the same position.
                _ko_snap_a_em = (round(math.atan2(_kv.escape_dy, _kv.escape_dx)
                                       / (math.pi / 4.0)) * (math.pi / 4.0))
                _ko_via_x = _cex_ko + math.cos(_ko_snap_a_em) * _klen
                _ko_via_y = _cey_ko + math.sin(_ko_snap_a_em) * _klen
                for _seg_xy in _route_45deg_stub(_cex_ko, _cey_ko, _ko_via_x, _ko_via_y,
                                                  _kv.escape_dx, _kv.escape_dy):
                    _kts = pcbnew.PCB_TRACK(board)
                    _kts.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(_seg_xy[0]), pcbnew.FromMM(_seg_xy[1])))
                    _kts.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(_seg_xy[2]), pcbnew.FromMM(_seg_xy[3])))
                    _kts.SetWidth(pcbnew.FromMM(_kv.neckdown_w_mm))
                    _kts.SetLayer(_kv.pad_layer_id)
                    _kts.SetNet(_ko_net)
                    board.Add(_kts)
                _kv.via_x = _ko_via_x
                _kv.via_y = _ko_via_y
                board.Add(_make_via(board, _kv, _ko_net))
            else:
                # Normal single-segment keepout escape (Phase 3B: snap trace angle)
                _ko_ang = round(math.atan2(_kv.escape_dy, _kv.escape_dx) / (math.pi / 4.0)) * (math.pi / 4.0)
                _ko_edx = math.cos(_ko_ang)
                _ko_edy = math.sin(_ko_ang)
                _ko_end_x = _kv.pad_x + _ko_edx * _klen
                _ko_end_y = _kv.pad_y + _ko_edy * _klen
                _kt = pcbnew.PCB_TRACK(board)
                _kt.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(_kv.pad_x), pcbnew.FromMM(_kv.pad_y)))
                _kt.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(_ko_end_x), pcbnew.FromMM(_ko_end_y)))
                _kt.SetWidth(pcbnew.FromMM(_kv.neckdown_w_mm))
                _kt.SetLayer(_kv.pad_layer_id)
                _kt.SetNet(_ko_net)
                board.Add(_kt)
                _kv.via_x = _ko_end_x
                _kv.via_y = _ko_end_y
                board.Add(_make_via(board, _kv, _ko_net))
            _ko_emit_count += 1
        if _ko_emit_count:
            print(f"Emitted {_ko_emit_count} keepout pad escape trace(s) + via(s).")

    # Re-emit sandwiched escape traces + vias (section 5 removed them along with other stubs).
    # Only emit for pads that ended up as implicit keepouts — pads that got vias placed
    # already have a neckdown stub and do not need a separate escape trace.
    if _sw_real_pads:
        _sw_emit_count = 0
        _sw_drill, _sw_annular = via_params(PRIORITY_OTHER)
        _sw_via_r = _sw_drill / 2.0 + _sw_annular
        _sw_tgt_layer = cfg.CLEARANCE_AUDIT.get("default_target_layer_id",
                                                  pending[0].target_layer_id if pending else pcbnew.In2_Cu)
        _placed_refs = {(v.ref, v.pad_num) for v in pending if not v.implicit_keepout}
        # Copper circles of all placed (non-keepout) fanout vias for conflict checking.
        _placed_via_circles = [
            (v.via_x, v.via_y, v.via_drill_mm / 2.0 + v.via_annular_mm)
            for v in pending if not v.implicit_keepout
        ]
        _SW_REACH_START = 5.0
        _SW_REACH_MAX = 12.0
        for _rec in _sw_real_pads:
            _ref, _pad, _net, _nc, _px, _py, _edx, _edy, _nw, _lid = _rec
            # Phase 3B: snap trace direction to nearest 45° (PROHIBITED 3 — must be 0/45/90)
            _sw_ang = round(math.atan2(_edy, _edx) / (math.pi / 4.0)) * (math.pi / 4.0)
            _edx = math.cos(_sw_ang)
            _edy = math.sin(_sw_ang)
            if (_ref, _pad) in _placed_refs:
                continue  # via placed — neckdown already present, skip escape trace
            _net_obj = board.FindNet(_net)
            if _net_obj is None:
                continue
            # Pre-check: verify the trace corridor itself is clear of placed via copper.
            # Lateral distance from a via to the escape axis is fixed regardless of reach —
            # if it conflicts at any axial position, extending reach cannot resolve it.
            # Clearance applied once: lat_dist >= trace_hw + via_r + clearance.
            _trace_hw = _nw / 2.0
            _lat_dx = -_edy   # unit vector perpendicular to escape
            _lat_dy = _edx
            _corridor_blocked = False
            for _vx, _vy, _vr in _placed_via_circles:
                _axial = (_vx - _px) * _edx + (_vy - _py) * _edy
                if _axial <= 0:
                    continue  # via is behind the pad — not in the escape corridor
                _lat_dist = abs((_vx - _px) * _lat_dx + (_vy - _py) * _lat_dy)
                if _lat_dist < _trace_hw + _vr + clearance:
                    _corridor_blocked = True
                    break
            if _corridor_blocked:
                continue  # trace corridor permanently blocked — skip this sandwiched pad
            # Extend reach until via endpoint is clear of all placed via copper.
            # Clearance applied once: center_dist >= sw_via_r + placed_via_r + clearance.
            _sw_reach = _SW_REACH_START
            while _sw_reach <= _SW_REACH_MAX:
                _sw_end_x = _px + _edx * _sw_reach
                _sw_end_y = _py + _edy * _sw_reach
                _conflict = any(
                    math.hypot(_sw_end_x - _vx, _sw_end_y - _vy) < _sw_via_r + _vr + clearance
                    for _vx, _vy, _vr in _placed_via_circles
                )
                if not _conflict:
                    break
                _sw_reach += STEP_MM
            _sw_end_x = _px + _edx * _sw_reach
            _sw_end_y = _py + _edy * _sw_reach
            _nt = pcbnew.PCB_TRACK(board)
            _nt.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(_px), pcbnew.FromMM(_py)))
            _nt.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(_sw_end_x), pcbnew.FromMM(_sw_end_y)))
            _nt.SetWidth(pcbnew.FromMM(_nw))
            _nt.SetLayer(_lid)
            _nt.SetNet(_net_obj)
            board.Add(_nt)
            if _sw_reach <= _SW_REACH_MAX:
                _sw_v = pcbnew.PCB_VIA(board)
                _sw_v.SetPosition(pcbnew.VECTOR2I(
                    pcbnew.FromMM(_sw_end_x), pcbnew.FromMM(_sw_end_y)))
                _sw_v.SetDrill(pcbnew.FromMM(_sw_drill))
                _sw_v.SetWidth(pcbnew.FromMM(_sw_drill + 2.0 * _sw_annular))
                _sw_v.SetLayerPair(_lid, _sw_tgt_layer)
                _sw_v.SetNet(_net_obj)
                board.Add(_sw_v)
                _sw_emit_count += 1
        if _sw_emit_count:
            print(f"Emitted {_sw_emit_count} sandwiched pad escape trace(s) + via(s).")

    # ------------------------------------------------------------------
    # 6b. Emit bus stubs from co-optimized face fanout
    # ------------------------------------------------------------------
    _bus_stubs_all = getattr(_run, '_all_bus_stubs', [])
    if _bus_stubs_all:
        _bus_emit_count = 0
        for _bx1, _by1, _bx2, _by2, _bnw, _bnet in _bus_stubs_all:
            if math.hypot(_bx2 - _bx1, _by2 - _by1) < 1e-6:
                continue
            _bnet_obj = board.FindNet(_bnet)
            if _bnet_obj is None:
                continue
            _bt = pcbnew.PCB_TRACK(board)
            _bt.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(_bx1), pcbnew.FromMM(_by1)))
            _bt.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(_bx2), pcbnew.FromMM(_by2)))
            _bt.SetWidth(pcbnew.FromMM(_bnw))
            _bt.SetLayer(pcbnew.F_Cu)
            _bt.SetNet(_bnet_obj)
            board.Add(_bt)
            _bus_emit_count += 1
        if _bus_emit_count:
            print(f"Emitted {_bus_emit_count} co-opt bus stub segment(s).")

    # ------------------------------------------------------------------
    # 6c. Emit stub-only traces from co-optimized face fanout.
    #     Covers two sources:
    #     (a) pending pads with face_fanout_assigned + stub_only_vx/vy
    #     (b) skip-net pads augmented into the face group (not in pending)
    # ------------------------------------------------------------------
    _so_all_pvs = ([pv for pv in pending
                    if pv.face_fanout_assigned
                    and hasattr(pv, 'stub_only_vx') and hasattr(pv, 'stub_only_vy')]
                   + [pv for pv in getattr(_run, '_all_skip_net_pvs', [])
                      if hasattr(pv, 'stub_only_vx') and hasattr(pv, 'stub_only_vy')])
    _so_emit_count = 0
    for _so_pv in _so_all_pvs:
        if not (hasattr(_so_pv, 'stub_only_vx') and hasattr(_so_pv, 'stub_only_vy')):
            continue
        _so_net_obj = board.FindNet(_so_pv.net_name)
        if _so_net_obj is None:
            continue
        _so_segs = _route_45deg_stub(
            _so_pv.pad_x, _so_pv.pad_y,
            _so_pv.stub_only_vx, _so_pv.stub_only_vy,
            _so_pv.escape_dx, _so_pv.escape_dy, axial_first=True)
        if not _so_segs:
            _so_segs = [(_so_pv.pad_x, _so_pv.pad_y,
                         _so_pv.stub_only_vx, _so_pv.stub_only_vy)]
        for _sx1, _sy1, _sx2, _sy2 in _so_segs:
            if math.hypot(_sx2 - _sx1, _sy2 - _sy1) < 1e-6:
                continue
            _st = pcbnew.PCB_TRACK(board)
            _st.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(_sx1), pcbnew.FromMM(_sy1)))
            _st.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(_sx2), pcbnew.FromMM(_sy2)))
            _st.SetWidth(pcbnew.FromMM(_so_pv.neckdown_w_mm))
            _st.SetLayer(_so_pv.pad_layer_id)
            _st.SetNet(_so_net_obj)
            board.Add(_st)
        if hasattr(_so_pv, 'stub_ext_vx'):
            _ex1, _ey1 = _so_pv.stub_only_vx, _so_pv.stub_only_vy
            _ex2, _ey2 = _so_pv.stub_ext_vx,  _so_pv.stub_ext_vy
            if math.hypot(_ex2 - _ex1, _ey2 - _ey1) >= 1e-6:
                _et = pcbnew.PCB_TRACK(board)
                _et.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(_ex1), pcbnew.FromMM(_ey1)))
                _et.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(_ex2), pcbnew.FromMM(_ey2)))
                _et.SetWidth(pcbnew.FromMM(_so_pv.neckdown_w_mm))
                _et.SetLayer(_so_pv.pad_layer_id)
                _et.SetNet(_so_net_obj)
                board.Add(_et)
        _so_emit_count += 1
    if _so_emit_count:
        print(f"Emitted {_so_emit_count} co-opt stub-only trace(s).")

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
