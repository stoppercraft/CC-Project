"""
route_clearance_audit.py — Phase 10 script #1.5: routing clearance analysis.

Runs AFTER route_prep_align.py (satellites placed into groups near their anchor
ICs) and BEFORE route_critical.py (switching loops).  Detects placements that
leave too little room for:
    - Trace corridors between component groups and their anchor IC
    - Via keepouts beside pads (for layer transitions)
    - HS differential pair routing envelopes
    - Diff-pair meander separation
    - Neckdown zones where wide traces meet narrow pads
    - Board-edge margin
    - Component-to-component gap based on component type

In --apply mode, applies the minimum-distance correction for each failure while
respecting locked footprints, proximity-rule max_dist limits, and canonical
rotation symmetry.  All checks and moves obey the same safety layers as
route_prep_align.py (Check A intra-group spacing, Check B pre-apply overlap,
Check B-rot rotation overlap, Check C post-apply verification).

Zero hardcoded component refs, net names, or layer names — all project data
comes from routing_config.py.  Works on any KiCad 10 PCB by swapping that file.

Usage:
    python route_clearance_audit.py            # dry run — report only
    python route_clearance_audit.py --apply    # apply corrections, save PCB
"""

import sys
import os
import argparse
import math
import re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import routing_config as cfg
import proximity_rules_config as rules_cfg

sys.path.insert(0, cfg.KICAD_SITE_PKGS)
import pcbnew


# ── Constants ─────────────────────────────────────────────────────────────────

CLEARANCE_MM       = 0.15   # default overlap-check clearance for non-typed pairs
DRC_MIN_CLEARANCE  = 0.10   # DRC trace-to-copper clearance floor

CANONICAL = {
    "180":   [0.0, 180.0],
    "4fold": [0.0, 90.0, 180.0, 270.0],
    "none":  [],
}

# Component-type gap sliding scale — defaults; overridden per-project via
# CLEARANCE_AUDIT keys type_gap_passive_mm / type_gap_ic_mm / etc.
TYPE_GAP_MM = {
    "passive":   0.025,
    "ic":        0.15,
    "connector": 0.50,
    "default":   0.10,
}


def _type_gap_map():
    """Return the effective type-gap map, reading overrides from CLEARANCE_AUDIT."""
    ca = cfg.CLEARANCE_AUDIT
    return {
        "passive":   ca.get("type_gap_passive_mm",   TYPE_GAP_MM["passive"]),
        "ic":        ca.get("type_gap_ic_mm",         TYPE_GAP_MM["ic"]),
        "connector": ca.get("type_gap_connector_mm",  TYPE_GAP_MM["connector"]),
        "default":   ca.get("type_gap_default_mm",    TYPE_GAP_MM["default"]),
    }

DIFF_PAIR_SUFFIXES = [
    ("+",   "-"),
    ("_P",  "_N"),
    ("_DP", "_DN"),
    ("_p",  "_n"),
]


# ── Coordinate / bbox helpers (mirror route_prep_align.py) ────────────────────

def norm360(deg):
    return deg % 360.0


def fp_xy(fp):
    return pcbnew.ToMM(fp.GetPosition().x), pcbnew.ToMM(fp.GetPosition().y)


def fp_courtyard_extents(fp):
    """
    Return the footprint's courtyard bounding box (x0, y0, x1, y1) in world mm.
    Uses courtyard graphics (F_CrtYd / B_CrtYd) when present — the same geometry
    KiCad DRC uses for overlap checking.  Falls back to GetBoundingBox() only when
    no courtyard exists; that fallback includes silkscreen text and will over-report.
    """
    xs, ys = [], []
    for item in fp.GraphicalItems():
        if item.GetLayer() in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
            ib = item.GetBoundingBox()
            xs.extend([pcbnew.ToMM(ib.GetLeft()),  pcbnew.ToMM(ib.GetRight())])
            ys.extend([pcbnew.ToMM(ib.GetTop()),   pcbnew.ToMM(ib.GetBottom())])
    if xs:
        return min(xs), min(ys), max(xs), max(ys)
    # Fallback — full bbox including text (known over-approximation)
    bb  = fp.GetBoundingBox()
    fcx = pcbnew.ToMM(bb.GetCenter().x)
    fcy = pcbnew.ToMM(bb.GetCenter().y)
    hw  = pcbnew.ToMM(bb.GetWidth())  / 2.0
    hh  = pcbnew.ToMM(bb.GetHeight()) / 2.0
    return fcx - hw, fcy - hh, fcx + hw, fcy + hh


def fp_half_extents(fp):
    x0, y0, x1, y1 = fp_courtyard_extents(fp)
    return (x1 - x0) / 2.0, (y1 - y0) / 2.0


def fp_bbox_mm(fp, cx=None, cy=None):
    """
    Return courtyard bbox (x0, y0, x1, y1) at the footprint's current position,
    or offset to (cx, cy) when those are given.
    """
    x0, y0, x1, y1 = fp_courtyard_extents(fp)
    if cx is not None or cy is not None:
        fcx, fcy = fp_xy(fp)
        dx = (cx - fcx) if cx is not None else 0.0
        dy = (cy - fcy) if cy is not None else 0.0
        return x0 + dx, y0 + dy, x1 + dx, y1 + dy
    return x0, y0, x1, y1


def bboxes_overlap(a, b, clearance=0.0):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return (ax0 - clearance < bx1 and ax1 + clearance > bx0 and
            ay0 - clearance < by1 and ay1 + clearance > by0)


def bbox_gap(a, b):
    """
    Return edge-to-edge gap (mm) between two bboxes.
    Positive = separated, 0 = touching (shared edge), negative = overlapping.
    When overlapping, returns the negative of the MINIMUM overlap dimension
    (the shortest escape distance), not the maximum.  Two bboxes that share
    only an edge (one overlap dimension = 0) are touching, not overlapping.
    """
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    dx = max(bx0 - ax1, ax0 - bx1, 0.0)
    dy = max(by0 - ay1, ay0 - by1, 0.0)
    if dx > 0.0 or dy > 0.0:
        return math.hypot(dx, dy)
    ov_x = min(ax1, bx1) - max(ax0, bx0)
    ov_y = min(ay1, by1) - max(ay0, by0)
    if ov_x <= 0.0 or ov_y <= 0.0:
        return 0.0          # shared edge only — touching, not overlapping
    return -min(ov_x, ov_y)


# ── Component-type classification ─────────────────────────────────────────────

def component_type(ref):
    """Classify a reference designator into passive/ic/connector/default."""
    m = re.match(r"^([A-Za-z]+)", ref)
    prefix = m.group(1) if m else ref
    core = prefix.upper()
    passives = {"R", "C", "L", "FB", "D", "Y", "F"}
    ics      = {"U", "Q", "IC"}
    conns    = {"J", "P", "CN", "SOM", "X"}
    if core in passives:
        return "passive"
    if core in ics:
        return "ic"
    if core in conns:
        return "connector"
    return "default"


def pair_gap_required(ref_a, ref_b):
    tgm = _type_gap_map()
    ga = tgm[component_type(ref_a)]
    gb = tgm[component_type(ref_b)]
    return max(ga, gb)


# ── Proximity rule lookup ─────────────────────────────────────────────────────

def build_rule_lookup():
    lookup = {}
    for entry in rules_cfg.RULES:
        ref_a, ref_b, max_dist_mm = entry[0], entry[1], entry[2]
        if ref_a not in lookup:
            lookup[ref_a] = (ref_b, max_dist_mm)
    return lookup


# ── Net / trace-width helpers ─────────────────────────────────────────────────

def build_hs_net_widths():
    """Map each HS net name → its trace width from HS_ROUTE_WIDTHS/HS_PAIRS."""
    m = {}
    for pair_name, (p_net, n_net, _layer, _skew) in cfg.HS_PAIRS.items():
        w = cfg.HS_ROUTE_WIDTHS.get(pair_name)
        if w is None:
            continue
        width_mm = w[0]
        m[p_net] = (pair_name, width_mm, w[1])
        m[n_net] = (pair_name, width_mm, w[1])
    return m


def build_switching_net_widths():
    """Map each switching-loop net → its trace width from SWITCHING_LOOPS."""
    m = {}
    for loop in getattr(cfg, "SWITCHING_LOOPS", []):
        for key_net, key_w in (
            ("sw_net",        "sw_width_mm"),
            ("vin_net",       "vin_width_mm"),
            ("out_net",       "out_width_mm"),
            ("bootstrap_net", "bootstrap_width_mm"),
        ):
            net = loop.get(key_net)
            w   = loop.get(key_w)
            if net and w:
                m[net] = w
    return m


def trace_width_for_net(net, hs_map, sw_map):
    """Look up net's trace width: HS → switching → signal default."""
    if net in hs_map:
        return hs_map[net][1]
    if net in sw_map:
        return sw_map[net]
    return cfg.CLEARANCE_AUDIT["signal_trace_width_mm"]


def net_to_refs(board):
    """Map net_name → set of refs of footprints with any pad on that net."""
    result = {}
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            n = pad.GetNetname()
            if not n:
                continue
            result.setdefault(n, set()).add(ref)
    return result


def shared_nets(fp_a, fp_b):
    a_nets = {pad.GetNetname() for pad in fp_a.Pads() if pad.GetNetname()}
    b_nets = {pad.GetNetname() for pad in fp_b.Pads() if pad.GetNetname()}
    return a_nets & b_nets


# ── Diff-pair net detection ───────────────────────────────────────────────────

def split_diff_net(net):
    """Return (base, side) where side is 'P' or 'N', or None if not a diff net."""
    for p_suf, n_suf in DIFF_PAIR_SUFFIXES:
        if net.endswith(p_suf):
            return net[:-len(p_suf)], "P"
        if net.endswith(n_suf):
            return net[:-len(n_suf)], "N"
    return None


def build_diff_pair_groups(n2r):
    """Return {base_name: {'P': set_of_refs, 'N': set_of_refs}}."""
    groups = {}
    for net, refs in n2r.items():
        sp = split_diff_net(net)
        if sp is None:
            continue
        base, side = sp
        g = groups.setdefault(base, {"P": set(), "N": set()})
        g[side] |= refs
    return {b: g for b, g in groups.items() if g["P"] and g["N"]}


# ── ALIGNMENT_GROUP side / offset extraction ──────────────────────────────────

def group_side_and_offset(group):
    """
    Return (side, offset_mm) describing which side of the anchor a group sits on
    and its declared offset.  side is one of 'left','right','above','below'.
    """
    gtype = group["type"]
    if gtype == "ray_place":
        d = group["direction"]
        mp = {"left": ("left", -group.get("min_offset_mm", 0.0)),
              "right":("right", group.get("min_offset_mm", 0.0)),
              "up":   ("above", -group.get("min_offset_mm", 0.0)),
              "down": ("below", group.get("min_offset_mm", 0.0))}
        return mp.get(d, (None, 0.0))
    if gtype == "column":
        x_off   = group.get("x_offset_mm", 0.0)
        y_start = group.get("y_start_mm",  0.0)
        if x_off == 0.0 or (y_start != 0.0 and abs(y_start) > abs(x_off)):
            return ("above" if y_start < 0 else "below", y_start)
        return ("right" if x_off > 0 else "left", x_off)
    if gtype == "row":
        y_off = group.get("y_offset_mm", 0.0)
        return ("below" if y_off > 0 else "above", y_off)
    if gtype == "pad_track":
        fixed_axis = group.get("fixed_axis", "Y")
        off = group.get("fixed_offset_mm", 0.0)
        if fixed_axis == "Y":
            return ("below" if off > 0 else "above", off)
        return ("right" if off > 0 else "left", off)
    if gtype == "push":
        d = group.get("direction", "right")
        return (d, 0.0)
    return (None, 0.0)


def side_outward_delta(side, deficit):
    """Return (dx, dy) that moves outward on the given side by deficit."""
    if side == "left":  return (-deficit, 0.0)
    if side == "right": return ( deficit, 0.0)
    if side == "above": return ( 0.0, -deficit)
    if side == "below": return ( 0.0,  deficit)
    return (0.0, 0.0)


# ── Group-vs-anchor gap on the group's side ───────────────────────────────────

def group_actual_gap(group_members_fps, anchor_fp, side):
    """
    Return the smallest gap (mm) between the anchor edge on 'side' and each
    member's opposing edge — i.e. how much routing space exists on that side.
    """
    abbox = fp_bbox_mm(anchor_fp)
    ax0, ay0, ax1, ay1 = abbox
    min_gap = float("inf")
    for fp in group_members_fps:
        m = fp_bbox_mm(fp)
        mx0, my0, mx1, my1 = m
        if side == "left":
            g = ax0 - mx1
        elif side == "right":
            g = mx0 - ax1
        elif side == "above":
            g = ay0 - my1
        elif side == "below":
            g = my0 - ay1
        else:
            g = 0.0
        if g < min_gap:
            min_gap = g
    return min_gap


# ── Push group helpers ────────────────────────────────────────────────────────

def _push_auto_members(group, fps):
    """Return refs of all unlocked fps whose centroid lies beyond anchor bbox in direction."""
    anchor_ref = group.get("anchor_ref")
    direction  = group.get("direction", "right")
    if anchor_ref not in fps:
        return []
    ax0, ay0, ax1, ay1 = fp_bbox_mm(fps[anchor_ref])
    members = []
    for ref, fp in fps.items():
        if ref == anchor_ref or fp.IsLocked():
            continue
        cx, cy = fp_xy(fp)
        if   direction == "right"  and cx > ax1:  members.append(ref)
        elif direction == "left"   and cx < ax0:  members.append(ref)
        elif direction == "above"  and cy < ay0:  members.append(ref)
        elif direction == "below"  and cy > ay1:  members.append(ref)
    return members


def _group_member_refs(group, fps):
    """Return member refs for any group type, resolving 'auto' for push groups."""
    raw = group.get("members", [])
    if raw == "auto":
        return _push_auto_members(group, fps)
    return [m["ref"] if isinstance(m, dict) else m for m in raw]


# ── Analysis 1 — Trace corridor check ────────────────────────────────────────

def analyze_corridors(board, fps, hs_map, sw_map):
    """
    For each ALIGNMENT_GROUP: measure actual gap between anchor and group vs.
    the corridor width required to route all shared nets.
    Returns list of dicts.
    """
    margin = cfg.CLEARANCE_AUDIT["corridor_margin_mm"]
    results = []
    for group in cfg.ALIGNMENT_GROUPS:
        anchor_ref = group["anchor_ref"]
        anchor_fp  = fps.get(anchor_ref)
        if anchor_fp is None:
            continue
        member_refs = _group_member_refs(group, fps)
        member_fps  = [fps[r] for r in member_refs if r in fps]
        if not member_fps:
            continue
        if group.get("skip_corridor", False):
            continue
        side, _ = group_side_and_offset(group)
        if side is None:
            continue

        actual_gap = group_actual_gap(member_fps, anchor_fp, side)

        # Shared nets between anchor and any member
        shared = set()
        for mfp in member_fps:
            shared |= shared_nets(anchor_fp, mfp)
        shared.discard("")

        widths = [trace_width_for_net(n, hs_map, sw_map) for n in shared]
        n_nets = len(widths)
        if n_nets == 0:
            required = margin
        else:
            max_w = max(widths)
            required = n_nets * (max_w + DRC_MIN_CLEARANCE) + margin

        passed = actual_gap >= required
        deficit = 0.0 if passed else (required - actual_gap)
        results.append({
            "group":       group["name"],
            "anchor":      anchor_ref,
            "side":        side,
            "actual":      actual_gap,
            "required":    required,
            "n_nets":      n_nets,
            "pass":        passed,
            "deficit":     deficit,
            "member_refs": member_refs,
        })
    return results


# ── Analysis 2 — Via keepout check ───────────────────────────────────────────

def _via_nets(hs_map, sw_map):
    """Return the set of net names that are likely to need a layer-change via."""
    nets = set(hs_map.keys())
    nets.update(sw_map.keys())
    return nets


def _pad_exit_toward(pad, other_fps):
    """
    Return the cardinal direction (dx, dy) from the pad toward the centroid of
    all other footprints that share the same net — i.e. the direction a trace
    would exit the pad.  Falls back to (1, 0) if no other pads found.
    """
    pnet = pad.GetNetname()
    if not pnet:
        return (1.0, 0.0)
    px = pcbnew.ToMM(pad.GetPosition().x)
    py = pcbnew.ToMM(pad.GetPosition().y)
    sx = sy = 0.0
    n = 0
    for fp in other_fps:
        for p in fp.Pads():
            if p.GetNetname() == pnet:
                sx += pcbnew.ToMM(p.GetPosition().x) - px
                sy += pcbnew.ToMM(p.GetPosition().y) - py
                n += 1
    if n == 0 or math.hypot(sx, sy) < 1e-6:
        return (1.0, 0.0)
    mag = math.hypot(sx, sy)
    return (sx / mag, sy / mag)


def analyze_via_keepout(fps, hs_map, sw_map):
    """
    For pads on nets that require layer transitions (HS nets and switching-loop
    nets), check that a via can fit on the trace-exit side of the pad without
    overlapping any neighbor bbox.  Only the exit-side direction is checked —
    not all directions — since the via lands somewhere along the trace, not
    necessarily immediately beside the pad on all sides.
    """
    ca = cfg.CLEARANCE_AUDIT
    via_diam = ca["via_drill_mm"] + 2 * ca["via_annular_ring_mm"] + 2 * ca["via_clearance_mm"]
    layer_nets = _via_nets(hs_map, sw_map)
    vk_excludes = set(ca.get("via_keepout_exclude_refs", []))

    fp_list   = list(fps.values())
    fp_bboxes = {fp.GetReference(): fp_bbox_mm(fp) for fp in fp_list}
    results   = []

    for fp in fp_list:
        if fp.IsLocked():
            continue
        ref = fp.GetReference()
        if ref in vk_excludes:
            continue
        others = [f for f in fp_list if f.GetReference() != ref]
        for pad in fp.Pads():
            pnet = pad.GetNetname()
            if not pnet or pnet not in layer_nets:
                continue
            px = pcbnew.ToMM(pad.GetPosition().x)
            py = pcbnew.ToMM(pad.GetPosition().y)
            psz = pad.GetSize()
            pw  = pcbnew.ToMM(psz.x) / 2.0
            ph  = pcbnew.ToMM(psz.y) / 2.0

            # Direction from this pad toward the net's other endpoint(s)
            edx, edy = _pad_exit_toward(pad, others)

            # Measure free space from pad edge in the exit direction only
            # by stepping to the nearest neighbor bbox in that direction.
            ex = px + edx * pw
            ey = py + edy * ph
            free    = float("inf")
            blocker = None
            for other in others:
                oref = other.GetReference()
                ox0, oy0, ox1, oy1 = fp_bboxes[oref]
                # Project the gap in the exit direction
                if abs(edx) >= abs(edy):
                    # Primarily horizontal exit
                    if oy1 < py - ph or oy0 > py + ph:
                        continue
                    d = (ox0 - ex) if edx > 0 else (ex - ox1)
                else:
                    # Primarily vertical exit
                    if ox1 < px - pw or ox0 > px + pw:
                        continue
                    d = (oy0 - ey) if edy > 0 else (ey - oy1)
                if d < 0:
                    d = 0.0
                if d < free:
                    free    = d
                    blocker = oref

            need   = via_diam
            passed = free >= need
            results.append({
                "ref":     ref,
                "pad":     pad.GetPadName() or str(pad.GetNumber()),
                "net":     pnet,
                "need":    need,
                "free":    free,
                "pass":    passed,
                "blocker": blocker,
            })
    return results


# ── Analysis 3 — HS path envelope check ──────────────────────────────────────

def _pad_positions_on_net(fp_list, net_name):
    out = []
    for fp in fp_list:
        for pad in fp.Pads():
            if pad.GetNetname() == net_name:
                out.append((fp.GetReference(),
                            pcbnew.ToMM(pad.GetPosition().x),
                            pcbnew.ToMM(pad.GetPosition().y)))
    return out


def _cluster_endpoints(points):
    """
    Given [(ref, x, y), ...], split into two clusters (source vs dest) by the
    axis of maximum spread.  Returns (cluster_a, cluster_b, midline_axis).
    """
    if len(points) < 2:
        return points, [], "X"
    xs = [p[1] for p in points]
    ys = [p[2] for p in points]
    if (max(xs) - min(xs)) >= (max(ys) - min(ys)):
        midx = (max(xs) + min(xs)) / 2.0
        a = [p for p in points if p[1] <= midx]
        b = [p for p in points if p[1] >  midx]
        axis = "X"
    else:
        midy = (max(ys) + min(ys)) / 2.0
        a = [p for p in points if p[2] <= midy]
        b = [p for p in points if p[2] >  midy]
        axis = "Y"
    if not a or not b:
        return points[:1], points[1:], axis
    return a, b, axis


def _cluster_centroid(cluster):
    n = len(cluster)
    return (sum(p[1] for p in cluster) / n, sum(p[2] for p in cluster) / n)


def analyze_hs_paths(fps, board):
    """
    For each HS pair, compute path envelope between source-pad centroid and
    dest-pad centroid.  Report any non-locked, non-endpoint fp whose bbox
    intersects AND whose pads have copper on the pair's routing layer.
    SMD footprints on a different layer (e.g. outer-layer passives when the pair
    routes on an inner layer) are not obstacles and are skipped.
    """
    fp_list = list(fps.values())
    results = []
    for pair_name, (p_net, n_net, _layer, _skew) in cfg.HS_PAIRS.items():
        layer_id = board.GetLayerID(_layer)
        w = cfg.HS_ROUTE_WIDTHS.get(pair_name, (0.127, 0.10))
        trace_w, gap = w
        corridor_w = 2 * trace_w + gap + 2 * DRC_MIN_CLEARANCE

        pads = _pad_positions_on_net(fp_list, p_net) + \
               _pad_positions_on_net(fp_list, n_net)
        if len(pads) < 2:
            results.append({"pair": pair_name, "status": "no-pads",
                            "blocker": None, "pass": True})
            continue

        cluster_a, cluster_b, _ax = _cluster_endpoints(pads)
        if not cluster_a or not cluster_b:
            results.append({"pair": pair_name, "status": "single-cluster",
                            "blocker": None, "pass": True})
            continue

        ax_c, ay_c = _cluster_centroid(cluster_a)
        bx_c, by_c = _cluster_centroid(cluster_b)

        # Build an oriented corridor rectangle aligned to the line direction.
        # This avoids flagging components that fall in the axis-aligned bounding
        # box corners but are not actually in the routing path.
        dx, dy = bx_c - ax_c, by_c - ay_c
        length = math.hypot(dx, dy)
        if length < 1e-6:
            results.append({"pair": pair_name, "status": "coincident",
                            "blocker": None, "pass": True})
            continue
        ux, uy = dx / length, dy / length   # unit vector along path
        nx, ny = -uy, ux                    # unit normal (perpendicular)
        half   = corridor_w / 2.0
        # Four corners of the oriented rectangle
        corners = [
            (ax_c + nx * half, ay_c + ny * half),
            (ax_c - nx * half, ay_c - ny * half),
            (bx_c + nx * half, by_c + ny * half),
            (bx_c - nx * half, by_c - ny * half),
        ]
        # Axis-aligned bounding box of the oriented rectangle (for quick rejection)
        env_x0 = min(c[0] for c in corners)
        env_y0 = min(c[1] for c in corners)
        env_x1 = max(c[0] for c in corners)
        env_y1 = max(c[1] for c in corners)
        envelope = (env_x0, env_y0, env_x1, env_y1)

        def _in_oriented_corridor(bx0, by0, bx1, by1):
            """
            Return True if the bbox (bx0,by0,bx1,by1) intersects the oriented
            rectangle by checking all four bbox corners against the corridor.
            A point (px,py) is inside the corridor when:
              |dot((p - a), n)| <= half   (within width)
              0 <= dot((p - a), u) <= length  (within length)
            """
            for cx_ in (bx0, bx1):
                for cy_ in (by0, by1):
                    rel_x, rel_y = cx_ - ax_c, cy_ - ay_c
                    along  = rel_x * ux + rel_y * uy
                    across = rel_x * nx + rel_y * ny
                    if 0.0 <= along <= length and abs(across) <= half:
                        return True
            return False

        hs_excludes = set(cfg.CLEARANCE_AUDIT.get("hs_path_exclude_refs", []))
        endpoint_refs = {p[0] for p in pads}
        blockers = []
        for fp in fp_list:
            ref = fp.GetReference()
            if ref in endpoint_refs:
                continue
            if ref in hs_excludes:
                continue
            if fp.IsLocked():
                continue
            # Only flag footprints that have copper on the pair's routing layer.
            # Outer-layer SMD pads don't block inner-layer (e.g. In2.Cu) routes.
            if not any(pad.GetLayerSet().Contains(layer_id) for pad in fp.Pads()):
                continue
            fb = fp_bbox_mm(fp)
            # Quick reject with AABB first, then precise oriented check
            if not bboxes_overlap(envelope, fb, clearance=0.0):
                continue
            if _in_oriented_corridor(*fb):
                blockers.append(ref)

        results.append({
            "pair":     pair_name,
            "status":   "ok" if not blockers else "blocked",
            "blocker":  blockers[0] if blockers else None,
            "blockers": blockers,
            "envelope": envelope,
            "pass":     not blockers,
        })
    return results


# ── Analysis 4 — Diff-pair separation ────────────────────────────────────────

def analyze_diff_pair_sep(fps, board):
    """
    For each diff-pair base, check minimum edge-to-edge separation between the
    P-side passive component and the N-side passive component.

    Only passive-type components (not ICs or connectors) are compared, and only
    cross-side (P vs N) pairs.  This avoids flagging IC endpoints vs their own
    inline coupling caps, or connector pins vs downstream passives — the 8mm
    meander budget applies between the two discrete passive components that handle
    the P and N signals, not between every component that touches those nets.
    """
    min_sep = cfg.CLEARANCE_AUDIT["diff_pair_min_sep_mm"]
    n2r     = net_to_refs(board)
    groups  = build_diff_pair_groups(n2r)
    results = []
    for base, sides in groups.items():
        # Filter to passive-type components only on each side
        p_passives = [r for r in sides["P"]
                      if r in fps and component_type(r) == "passive"]
        n_passives = [r for r in sides["N"]
                      if r in fps and component_type(r) == "passive"]
        for pref in p_passives:
            for nref in n_passives:
                if pref == nref:
                    continue
                gap    = bbox_gap(fp_bbox_mm(fps[pref]), fp_bbox_mm(fps[nref]))
                passed = gap >= min_sep
                results.append({
                    "base":     base,
                    "p_ref":    pref,
                    "n_ref":    nref,
                    "gap":      gap,
                    "required": min_sep,
                    "pass":     passed,
                    "deficit":  0.0 if passed else (min_sep - gap),
                })
    return results


# ── Analysis 5 — Neckdown zone ───────────────────────────────────────────────

def _pad_exit_side(fp, pad):
    """
    Return which side of the footprint bbox the pad sits closest to:
    'left','right','above','below'.  This is the side a trace would exit.
    """
    fx, fy = fp_xy(fp)
    px = pcbnew.ToMM(pad.GetPosition().x)
    py = pcbnew.ToMM(pad.GetPosition().y)
    dx = px - fx
    dy = py - fy
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "below" if dy > 0 else "above"


def analyze_neckdown(fps, hs_map, sw_map):
    """
    For pads where the incoming trace width > pad width, need neckdown_length_mm
    of clear space on the pad's exit side.

    Free space is measured from the pad's own edge (not the footprint bbox edge)
    outward in the exit direction to the nearest OTHER footprint's bbox.  This
    avoids the false-positive where a multi-pad IC appears to have zero free space
    because its own body fills the measured side.
    """
    need_len  = cfg.CLEARANCE_AUDIT["neckdown_length_mm"]
    fp_list   = list(fps.values())
    fp_bboxes = {fp.GetReference(): fp_bbox_mm(fp) for fp in fp_list}
    results   = []

    for fp in fp_list:
        if fp.IsLocked():
            continue
        ref = fp.GetReference()
        others = [f for f in fp_list if f.GetReference() != ref]

        for pad in fp.Pads():
            pnet = pad.GetNetname()
            if not pnet:
                continue
            trace_w = trace_width_for_net(pnet, hs_map, sw_map)
            psz   = pad.GetSize()
            pad_w = min(pcbnew.ToMM(psz.x), pcbnew.ToMM(psz.y))
            if trace_w <= pad_w:
                continue

            side = _pad_exit_side(fp, pad)

            # Pad position and half-extents (use pad size, not footprint bbox)
            px  = pcbnew.ToMM(pad.GetPosition().x)
            py  = pcbnew.ToMM(pad.GetPosition().y)
            phw = pcbnew.ToMM(psz.x) / 2.0
            phh = pcbnew.ToMM(psz.y) / 2.0

            # Pad edge in the exit direction
            if side == "right":
                pad_edge = px + phw
            elif side == "left":
                pad_edge = px - phw
            elif side == "below":
                pad_edge = py + phh
            else:  # above
                pad_edge = py - phh

            # Measure from pad edge to nearest OTHER footprint bbox on exit side
            free    = float("inf")
            blocker = None
            for other in others:
                oref = other.GetReference()
                ox0, oy0, ox1, oy1 = fp_bboxes[oref]
                if side == "right":
                    if oy1 < py - phh or oy0 > py + phh: continue
                    d = ox0 - pad_edge
                elif side == "left":
                    if oy1 < py - phh or oy0 > py + phh: continue
                    d = pad_edge - ox1
                elif side == "below":
                    if ox1 < px - phw or ox0 > px + phw: continue
                    d = oy0 - pad_edge
                else:  # above
                    if ox1 < px - phw or ox0 > px + phw: continue
                    d = pad_edge - oy1
                if d < 0:
                    d = 0.0
                if d < free:
                    free    = d
                    blocker = oref

            passed = free >= need_len
            results.append({
                "ref":      ref,
                "pad":      pad.GetPadName() or str(pad.GetNumber()),
                "net":      pnet,
                "side":     side,
                "trace_w":  trace_w,
                "pad_w":    pad_w,
                "free":     free,
                "required": need_len,
                "pass":     passed,
                "deficit":  0.0 if passed else (need_len - free),
                "blocker":  blocker,
            })
    return results


# ── Analysis 6 — Board edge margin ───────────────────────────────────────────

def analyze_board_margin(board, fps):
    margin   = cfg.CLEARANCE_AUDIT["board_margin_mm"]
    excludes = cfg.CLEARANCE_AUDIT.get("board_edge_exclude_prefixes", [])
    edge_bb  = board.GetBoardEdgesBoundingBox()
    ex0 = pcbnew.ToMM(edge_bb.GetLeft())
    ey0 = pcbnew.ToMM(edge_bb.GetTop())
    ex1 = pcbnew.ToMM(edge_bb.GetRight())
    ey1 = pcbnew.ToMM(edge_bb.GetBottom())
    results = []
    for ref, fp in fps.items():
        m = re.match(r"^([A-Za-z]+)", ref)
        prefix = m.group(1).upper() if m else ref.upper()
        if prefix in [p.upper() for p in excludes]:
            continue
        x0, y0, x1, y1 = fp_bbox_mm(fp)
        left   = x0 - ex0
        right  = ex1 - x1
        top    = y0 - ey0
        bottom = ey1 - y1
        worst_side, worst_val = min(
            (("left", left), ("right", right),
             ("above", top),  ("below", bottom)),
            key=lambda kv: kv[1])
        passed = worst_val >= margin
        results.append({
            "ref":      ref,
            "side":     worst_side,
            "clear":    worst_val,
            "required": margin,
            "pass":     passed,
            "deficit":  0.0 if passed else (margin - worst_val),
        })
    return results


# ── Analysis 7 — Component-type gap ──────────────────────────────────────────

def analyze_type_gap(fps):
    excludes = set(cfg.CLEARANCE_AUDIT.get("type_gap_exclude_prefixes", []))
    exclude_refs = set(cfg.CLEARANCE_AUDIT.get("type_gap_exclude_refs", []))
    def _excluded(ref):
        if ref in exclude_refs:
            return True
        m = re.match(r"^([A-Za-z]+)", ref)
        return (m.group(1) if m else ref).upper() in {e.upper() for e in excludes}

    fp_list = list(fps.values())
    n = len(fp_list)
    results = []
    for i in range(n):
        a = fp_list[i]
        ar = a.GetReference()
        if _excluded(ar):
            continue
        abb = fp_bbox_mm(a)
        for j in range(i + 1, n):
            b = fp_list[j]
            br = b.GetReference()
            if _excluded(br):
                continue
            bbb = fp_bbox_mm(b)
            gap = bbox_gap(abb, bbb)
            required = pair_gap_required(ar, br)
            if gap < required:
                results.append({
                    "ref_a":    ar,
                    "ref_b":    br,
                    "gap":      gap,
                    "required": required,
                    "pass":     False,
                    "deficit":  required - gap,
                })
    return results


# ── Safety helpers (mirror route_prep_align.py) ──────────────────────────────

def rotation_allowed(ref):
    sym = cfg.ROTATION_SYMMETRY.get(ref, None)
    if sym is None:
        return True
    return sym != "none"


def check_pre_apply_overlaps(all_moves, fps):
    """
    Verify each proposed move's bbox is clear of every other footprint.
    Co-moved components evaluated at their proposed positions.

    Two-pass check:
      Pass 1 — validate each move against all proposed positions (other moves
               that are planned but not yet confirmed safe).
      Pass 2 — re-validate pass-1 approvals against the ACTUAL (current) positions
               of any component whose move was rejected in pass 1.  This prevents
               cascade errors where A is approved because B's proposed position was
               used, but B is then skipped so A's target would land on the real B.

    Push-group exemption: members of the same push group are never checked against
    each other.  A rigid body move preserves all intra-group relative positions, so
    any pre-existing GetBoundingBox() overlap between group members is invariant and
    cannot be resolved by the move — checking it would only block the whole group.

    Returns (safe, skipped).
    """
    # Build push-group co-membership map for the exemption.
    push_group_sets = []
    for grp in cfg.ALIGNMENT_GROUPS:
        if grp.get("type") == "push":
            push_group_sets.append(frozenset(_group_member_refs(grp, fps)))

    def same_push_group(a, b):
        return any(a in pg and b in pg for pg in push_group_sets)

    proposed = {ref: (tx, ty) for ref, cx, cy, tx, ty, src in all_moves}
    safe1, skipped = [], []
    for move in all_moves:
        ref, cx, cy, tx, ty, src = move
        fp = fps.get(ref)
        if fp is None:
            safe1.append(move); continue
        prop_bbox = fp_bbox_mm(fp, tx, ty)
        conflict = None
        for oref, ofp in fps.items():
            if oref == ref: continue
            if same_push_group(ref, oref): continue  # rigid body — skip intra-group
            if oref in proposed:
                ox, oy = proposed[oref]
            else:
                ox, oy = fp_xy(ofp)
            other_bbox = fp_bbox_mm(ofp, ox, oy)
            required = pair_gap_required(ref, oref)
            if bboxes_overlap(prop_bbox, other_bbox, clearance=required):
                conflict = oref; break
        if conflict:
            skipped.append((ref, cx, cy, tx, ty, src,
                            f"proposed bbox overlaps {conflict}"))
        else:
            safe1.append(move)

    # Pass 2: re-validate pass-1 approvals against actual positions of skipped refs
    skipped_refs = {m[0] for m in skipped}
    safe = []
    for move in safe1:
        ref, cx, cy, tx, ty, src = move
        fp = fps.get(ref)
        if fp is None:
            safe.append(move); continue
        prop_bbox = fp_bbox_mm(fp, tx, ty)
        conflict = None
        for oref in skipped_refs:
            if oref == ref: continue
            if same_push_group(ref, oref): continue  # rigid body — skip intra-group
            ofp = fps.get(oref)
            if ofp is None: continue
            ox, oy = fp_xy(ofp)                  # actual position, not proposed
            other_bbox = fp_bbox_mm(ofp, ox, oy)
            required = pair_gap_required(ref, oref)
            if bboxes_overlap(prop_bbox, other_bbox, clearance=required):
                conflict = oref; break
        if conflict:
            skipped.append((ref, cx, cy, tx, ty, src,
                            f"proposed bbox overlaps {conflict} (skipped in pass 1)"))
        else:
            safe.append(move)

    return safe, skipped


def check_post_apply_overlaps(moved_refs, fps_live):
    # Build push-group co-membership sets for the same exemption used in Check B.
    push_group_sets = []
    for grp in cfg.ALIGNMENT_GROUPS:
        if grp.get("type") == "push":
            push_group_sets.append(frozenset(_group_member_refs(grp, fps_live)))

    def same_push_group(a, b):
        return any(a in pg and b in pg for pg in push_group_sets)

    issues = []
    seen = set()
    for ref_a in moved_refs:
        fp_a = fps_live.get(ref_a)
        if fp_a is None: continue
        bbox_a = fp_bbox_mm(fp_a)
        for ref_b, fp_b in fps_live.items():
            if ref_b == ref_a: continue
            if same_push_group(ref_a, ref_b): continue  # rigid body — intra-group silkscreen overlap is invariant
            key = tuple(sorted((ref_a, ref_b)))
            if key in seen: continue
            bbox_b = fp_bbox_mm(fp_b)
            if bboxes_overlap(bbox_a, bbox_b, clearance=0.0):
                seen.add(key)
                ax0, ay0, ax1, ay1 = bbox_a
                bx0, by0, bx1, by1 = bbox_b
                ov_x = min(ax1, bx1) - max(ax0, bx0)
                ov_y = min(ay1, by1) - max(ay0, by0)
                issues.append((ref_a, ref_b, ov_x, ov_y))
    return issues


def enforce_min_spacing_axis(members_xy, fps, track_axis):
    """Reuse of route_prep_align intra-group spacing enforcement (simplified)."""
    if len(members_xy) < 2:
        return members_xy
    axis_idx = 0 if track_axis == "X" else 1
    items = sorted(members_xy, key=lambda t: t[1] if axis_idx == 0 else t[2])

    def get_coord(it): return it[1] if axis_idx == 0 else it[2]
    def set_coord(it, v):
        r, x, y = it
        return (r, v, y) if axis_idx == 0 else (r, x, v)
    def get_half(ref):
        fp = fps.get(ref)
        if fp is None: return 0.75
        hw, hh = fp_half_extents(fp)
        return hw if axis_idx == 0 else hh

    orig_centroid = sum(get_coord(t) for t in items) / len(items)
    for i in range(1, len(items)):
        prev_ref = items[i - 1][0]
        cur_ref  = items[i][0]
        min_gap  = get_half(prev_ref) + CLEARANCE_MM + get_half(cur_ref)
        min_pos  = get_coord(items[i - 1]) + min_gap
        if get_coord(items[i]) < min_pos:
            items[i] = set_coord(items[i], min_pos)
    new_centroid = sum(get_coord(t) for t in items) / len(items)
    shift = orig_centroid - new_centroid
    if abs(shift) > 1e-6:
        items = [set_coord(it, get_coord(it) + shift) for it in items]
    return items


def within_proximity_rule(ref, tx, ty, rule_lookup, fps):
    rule = rule_lookup.get(ref)
    if rule is None:
        return True
    anchor_ref, max_dist = rule
    anchor_fp = fps.get(anchor_ref)
    if anchor_fp is None:
        return True
    ax, ay = fp_xy(anchor_fp)
    return math.hypot(tx - ax, ty - ay) <= max_dist


def within_board_margin(fp, tx, ty, board):
    margin = cfg.CLEARANCE_AUDIT["board_margin_mm"]
    edge_bb = board.GetBoardEdgesBoundingBox()
    ex0 = pcbnew.ToMM(edge_bb.GetLeft())
    ey0 = pcbnew.ToMM(edge_bb.GetTop())
    ex1 = pcbnew.ToMM(edge_bb.GetRight())
    ey1 = pcbnew.ToMM(edge_bb.GetBottom())
    hw, hh = fp_half_extents(fp)
    return ((tx - hw) - ex0 >= margin and
            ex1 - (tx + hw) >= margin and
            (ty - hh) - ey0 >= margin and
            ey1 - (ty + hh) >= margin)


def try_clear_blocker(blocker_ref, envelope, fps, rule_lookup, board):
    """
    Minimum cardinal displacement that removes blocker from envelope, respecting
    proximity rule and no-new-overlap constraint.  Returns (dx, dy) or None.
    """
    fp = fps.get(blocker_ref)
    if fp is None or fp.IsLocked():
        return None
    cx, cy = fp_xy(fp)
    hw, hh = fp_half_extents(fp)
    ex0, ey0, ex1, ey1 = envelope
    eps = 1e-4
    candidates = [
        (ex0 - CLEARANCE_MM - hw - cx - eps, 0.0),
        (ex1 + CLEARANCE_MM + hw - cx + eps, 0.0),
        (0.0, ey0 - CLEARANCE_MM - hh - cy - eps),
        (0.0, ey1 + CLEARANCE_MM + hh - cy + eps),
    ]
    valid = []
    for dx, dy in candidates:
        tx, ty = cx + dx, cy + dy
        if not within_proximity_rule(blocker_ref, tx, ty, rule_lookup, fps):
            continue
        if not within_board_margin(fp, tx, ty, board):
            continue
        prop_bbox = fp_bbox_mm(fp, tx, ty)
        conflict = False
        for oref, ofp in fps.items():
            if oref == blocker_ref: continue
            ox, oy = fp_xy(ofp)
            other_bbox = fp_bbox_mm(ofp, ox, oy)
            required = pair_gap_required(blocker_ref, oref)
            if bboxes_overlap(prop_bbox, other_bbox, clearance=required):
                conflict = True; break
        if conflict:
            continue
        valid.append((math.hypot(dx, dy), dx, dy))
    if not valid:
        return None
    valid.sort(key=lambda t: t[0])
    _, dx, dy = valid[0]
    return dx, dy


# ── Apply-mode: compute proposed moves from failures ─────────────────────────

def build_moves(corridors, vias, hs_paths, diffs, necks, edges, types,
                fps, rule_lookup, board, extra_locked=None):
    """
    Turn each analysis's failures into (ref, cx, cy, tx, ty, source) proposals.
    Aggregates deltas per-ref so different failures don't fight each other.
    Returns (moves, skips) — skips list of (ref, reason).

    extra_locked: optional set of refs to treat as locked when computing
    pair corrections (diff pair, type gap).  Used in the second pass after
    Check B to give the full deficit to partners of Check-B-skipped refs
    rather than half — prevents check-C failures when one side can't move.
    """
    _extra = extra_locked or set()
    delta = {}   # ref -> [dx, dy, [source_strings]]
    skips = []

    def add(ref, dx, dy, source):
        if ref not in fps:
            return
        fp = fps[ref]
        if fp.IsLocked():
            skips.append((ref, f"{source}: LOCKED"))
            return
        if ref not in delta:
            delta[ref] = [0.0, 0.0, []]
        # Take max magnitude per axis so aggregated fixes stay minimal
        if abs(dx) > abs(delta[ref][0]):
            delta[ref][0] = dx
        if abs(dy) > abs(delta[ref][1]):
            delta[ref][1] = dy
        delta[ref][2].append(source)

    # 1. Corridor: move all members outward as a unit by the deficit
    for c in corridors:
        if c["pass"]: continue
        dx, dy = side_outward_delta(c["side"], c["deficit"])
        for ref in c["member_refs"]:
            add(ref, dx, dy, f"corridor[{c['group']}]")

    # 2. Via keepout: informational only — no moves generated.
    # Vias that can't fit immediately beside a pad land 0.5–1mm along the
    # trace instead, which is acceptable routing practice.  Moving components
    # to create pad-edge via room would disturb well-tuned relative positions
    # without a routing benefit.  Failures are reported in the audit summary
    # for awareness but do not drive any placement correction.
    _ = vias  # suppress unused-variable warning; vias is printed in report

    # 3. HS path: displace blocker off envelope
    for h in hs_paths:
        if h["pass"] or h.get("blocker") is None: continue
        blocker = h["blocker"]
        res = try_clear_blocker(blocker, h["envelope"], fps, rule_lookup, board)
        if res is None:
            skips.append((blocker, f"hs[{h['pair']}]: no valid cardinal clear"))
            continue
        add(blocker, res[0], res[1], f"hs[{h['pair']}]")

    # 4. Diff pair sep: push each half the deficit apart (full deficit if partner locked)
    for d in diffs:
        if d["pass"]: continue
        pfp = fps.get(d["p_ref"]); nfp = fps.get(d["n_ref"])
        if pfp is None or nfp is None: continue
        px, py = fp_xy(pfp); nx, ny = fp_xy(nfp)
        vx, vy = px - nx, py - ny
        mag = math.hypot(vx, vy)
        if mag < 1e-6:
            vx, vy, mag = 1.0, 0.0, 1.0
        src  = f"diff[{d['base']}]"
        full = d["deficit"]
        p_locked = pfp.IsLocked() or d["p_ref"] in _extra
        n_locked = nfp.IsLocked() or d["n_ref"] in _extra
        if p_locked and n_locked:
            pass
        elif p_locked:
            add(d["n_ref"], -(vx / mag) * full, -(vy / mag) * full, src)
        elif n_locked:
            add(d["p_ref"],  (vx / mag) * full,  (vy / mag) * full, src)
        else:
            half = full / 2.0
            add(d["p_ref"],  (vx / mag) * half,  (vy / mag) * half, src)
            add(d["n_ref"], -(vx / mag) * half, -(vy / mag) * half, src)

    # 5. Neckdown: move fp outward on constrained side by deficit
    for n in necks:
        if n["pass"]: continue
        dx, dy = side_outward_delta(n["side"], n["deficit"])
        add(n["ref"], dx, dy, f"neck[{n['ref']}.{n['pad']}]")

    # 6. Board edge: move inward by deficit
    for e in edges:
        if e["pass"]: continue
        inward = {"left": "right", "right": "left",
                  "above": "below", "below": "above"}[e["side"]]
        dx, dy = side_outward_delta(inward, e["deficit"])
        # Preserve proximity rule
        fp = fps.get(e["ref"])
        if fp is None: continue
        cx, cy = fp_xy(fp)
        if not within_proximity_rule(e["ref"], cx + dx, cy + dy, rule_lookup, fps):
            skips.append((e["ref"],
                          f"edge[{e['side']}]: SKIP — violates proximity rule"))
            continue
        add(e["ref"], dx, dy, f"edge[{e['side']}]")

    # 7. Type gap: push apart (full deficit to movable side if partner is locked)
    for t in types:
        if t["pass"]: continue
        afp = fps.get(t["ref_a"]); bfp = fps.get(t["ref_b"])
        if afp is None or bfp is None: continue
        ax, ay = fp_xy(afp); bx, by = fp_xy(bfp)
        vx, vy = ax - bx, ay - by
        mag = math.hypot(vx, vy)
        if mag < 1e-6:
            vx, vy, mag = 1.0, 0.0, 1.0
        src  = f"typegap[{t['ref_a']}↔{t['ref_b']}]"
        full = t["deficit"]
        a_locked = afp.IsLocked() or t["ref_a"] in _extra
        b_locked = bfp.IsLocked() or t["ref_b"] in _extra
        if a_locked and b_locked:
            pass
        elif a_locked:
            add(t["ref_b"], -(vx / mag) * full, -(vy / mag) * full, src)
        elif b_locked:
            add(t["ref_a"],  (vx / mag) * full,  (vy / mag) * full, src)
        else:
            half = full / 2.0
            add(t["ref_a"],  (vx / mag) * half,  (vy / mag) * half, src)
            add(t["ref_b"], -(vx / mag) * half, -(vy / mag) * half, src)

    # Materialise into (ref, cx, cy, tx, ty, source) list
    moves = []
    for ref, (dx, dy, sources) in delta.items():
        fp = fps[ref]
        cx, cy = fp_xy(fp)
        tx, ty = cx + dx, cy + dy
        if not within_proximity_rule(ref, tx, ty, rule_lookup, fps):
            skips.append((ref, f"proximity rule violation ({','.join(sources)})"))
            continue
        if not within_board_margin(fp, tx, ty, board):
            skips.append((ref, f"board margin violation ({','.join(sources)})"))
            continue
        moves.append((ref, cx, cy, tx, ty, "+".join(sources)))
    return moves, skips


def apply_lift_group_y_sync(moves, fps):
    """
    For each 'lift' group in ALIGNMENT_GROUPS, enforce a uniform Y delta across
    all members.  When the clearance audit computes different Y corrections for
    different members of a lift group (e.g. one needs more via room than another),
    the largest-magnitude Y delta wins and is applied to every member — including
    those that had no correction of their own.  This keeps the row Y-aligned after
    correction, matching the invariant the lift group was designed to maintain.
    """
    proposed = {ref: [cx, cy, tx, ty, src]
                for ref, cx, cy, tx, ty, src in moves}
    changed = False

    for group in cfg.ALIGNMENT_GROUPS:
        if group.get("type") != "lift":
            continue
        member_refs = [m["ref"] if isinstance(m, dict) else m
                       for m in group["members"]]

        # Collect Y deltas for members that already have a proposed move
        y_deltas = {}
        for ref in member_refs:
            if ref in proposed:
                cx, cy, tx, ty, src = proposed[ref]
                dy = ty - cy
                if abs(dy) > 1e-4:
                    y_deltas[ref] = dy

        if not y_deltas:
            continue

        # Largest-magnitude delta is the most-constrained member's correction —
        # applying it uniformly satisfies every member's constraint.
        uniform_dy = max(y_deltas.values(), key=abs)

        for ref in member_refs:
            fp = fps.get(ref)
            if fp is None or fp.IsLocked():
                continue
            cur_x, cur_y = fp_xy(fp)
            new_ty = cur_y + uniform_dy

            if ref in proposed:
                rec = proposed[ref]
                if abs(rec[3] - new_ty) > 1e-4:
                    rec[3] = new_ty
                    changed = True
            else:
                # Member had no correction — add one to keep it Y-aligned
                proposed[ref] = [cur_x, cur_y, cur_x, new_ty,
                                  f"lift-sync[{group['name']}]"]
                changed = True

    if not changed:
        return moves
    return [(ref, rec[0], rec[1], rec[2], rec[3], rec[4])
            for ref, rec in proposed.items()]


def apply_group_axis_sync(moves, fps):
    """
    For column groups: apply the largest-magnitude X delta uniformly to all
    members (keeps column X-aligned after per-member clearance corrections).
    For row/pad_track groups: apply the largest-magnitude Y delta uniformly
    to all members.

    Then cascade: when the synced group would overlap a neighboring group at
    their proposed positions, push the neighboring group outward by the same
    axis delta.  Repeats up to 10 rounds until stable.

    Lift groups are handled by apply_lift_group_y_sync and are skipped here.
    """
    member_to_group = {}
    for grp in cfg.ALIGNMENT_GROUPS:
        for ref in _group_member_refs(grp, fps):
            member_to_group[ref] = grp

    proposed = {ref: [cx, cy, tx, ty, src]
                for ref, cx, cy, tx, ty, src in moves}

    for _pass in range(10):
        made_change = False

        for group in cfg.ALIGNMENT_GROUPS:
            gtype = group.get("type")
            if gtype == "column":
                axis_idx = 0    # sync X
            elif gtype in ("row", "pad_track"):
                axis_idx = 1    # sync Y
            elif gtype == "push":
                d = group.get("direction", "right")
                axis_idx = 0 if d in ("right", "left") else 1
            else:
                continue

            member_refs = _group_member_refs(group, fps)

            # Collect per-member axis deltas from proposed moves
            deltas = {}
            for ref in member_refs:
                if ref in proposed:
                    d = (proposed[ref][2] - proposed[ref][0] if axis_idx == 0
                         else proposed[ref][3] - proposed[ref][1])
                    if abs(d) > 1e-4:
                        deltas[ref] = d

            if not deltas:
                continue

            # Largest-magnitude delta satisfies every member's constraint
            uniform_d = max(deltas.values(), key=abs)

            # Apply uniform delta to all members in this group
            for ref in member_refs:
                fp = fps.get(ref)
                if fp is None or fp.IsLocked():
                    continue
                cur_x, cur_y = fp_xy(fp)
                if axis_idx == 0:
                    new_tx, new_ty = cur_x + uniform_d, cur_y
                else:
                    new_tx, new_ty = cur_x, cur_y + uniform_d

                new_t = new_tx if axis_idx == 0 else new_ty
                if ref in proposed:
                    rec = proposed[ref]
                    existing = rec[2] if axis_idx == 0 else rec[3]
                    if abs(existing - new_t) > 1e-4:
                        if axis_idx == 0:
                            rec[2] = new_tx
                        else:
                            rec[3] = new_ty
                        made_change = True
                else:
                    proposed[ref] = [cur_x, cur_y, new_tx, new_ty,
                                     f"axis-sync[{group['name']}]"]
                    made_change = True

            # Cascade: push any neighboring group that now conflicts outward
            # by the same axis delta so group alignment is preserved end-to-end.
            group_member_set = set(member_refs)
            for ref in member_refs:
                fp = fps.get(ref)
                if fp is None or ref not in proposed:
                    continue
                rec = proposed[ref]
                bbox_ref = fp_bbox_mm(fp, rec[2], rec[3])

                for other_ref, other_rec in list(proposed.items()):
                    if other_ref in group_member_set:
                        continue
                    other_fp = fps.get(other_ref)
                    if other_fp is None:
                        continue
                    other_grp = member_to_group.get(other_ref)
                    if other_grp is None:
                        continue
                    other_bbox = fp_bbox_mm(other_fp, other_rec[2], other_rec[3])
                    required = pair_gap_required(ref, other_ref)
                    if not bboxes_overlap(bbox_ref, other_bbox, clearance=required):
                        continue
                    # Conflict — push other group's members by the same delta
                    other_member_refs = _group_member_refs(other_grp, fps)
                    for om_ref in other_member_refs:
                        om_fp = fps.get(om_ref)
                        if om_fp is None or om_fp.IsLocked():
                            continue
                        om_x, om_y = fp_xy(om_fp)
                        if om_ref in proposed:
                            om_rec = proposed[om_ref]
                            if axis_idx == 0:
                                om_rec[2] += uniform_d
                            else:
                                om_rec[3] += uniform_d
                        else:
                            proposed[om_ref] = [
                                om_x, om_y,
                                om_x + (uniform_d if axis_idx == 0 else 0.0),
                                om_y + (0.0 if axis_idx == 0 else uniform_d),
                                f"cascade[{group['name']}→{other_grp['name']}]"
                            ]
                        made_change = True

        if not made_change:
            break

    return [(ref, rec[0], rec[1], rec[2], rec[3], rec[4])
            for ref, rec in proposed.items()]


def apply_intra_group_spacing(moves, fps):
    """
    Check A: for each ALIGNMENT_GROUP with ≥ 2 members in the moves list, re-run
    spacing enforcement along the group's track axis and update targets.
    """
    proposed = {r: (tx, ty) for r, cx, cy, tx, ty, s in moves}
    changed = False
    for group in cfg.ALIGNMENT_GROUPS:
        gtype = group["type"]
        if gtype == "column":
            axis = "Y"
        elif gtype == "row":
            axis = "X"
        elif gtype == "pad_track":
            axis = group.get("track_axis", "X")
        elif gtype == "ray_place":
            axis = "Y" if group.get("direction") in ("left", "right") else "X"
        elif gtype == "push":
            continue  # push moves all members as a rigid body; no intra-group spacing
        else:
            continue
        member_refs = _group_member_refs(group, fps)
        group_moves = [(r, proposed[r][0], proposed[r][1])
                       for r in member_refs if r in proposed]
        if len(group_moves) < 2:
            continue
        adjusted = enforce_min_spacing_axis(group_moves, fps, axis)
        for r, tx, ty in adjusted:
            if (abs(tx - proposed[r][0]) > 1e-4 or
                abs(ty - proposed[r][1]) > 1e-4):
                proposed[r] = (tx, ty)
                changed = True
    if not changed:
        return moves
    return [(r, cx, cy, proposed[r][0], proposed[r][1], s)
            for r, cx, cy, tx, ty, s in moves]


# ── Reporting helpers ────────────────────────────────────────────────────────

def _hdr(n, title):
    dashes = "─" * max(1, 72 - len(title) - 4 - len(str(n)))
    print(f"\n── {n}. {title} {dashes}")


def print_summary(corridors, vias, hs_paths, diffs, necks, edges, types,
                  applied=None, skipped=None):
    def n_fail(lst): return sum(1 for x in lst if not x["pass"])
    print()
    print("=" * 72)
    print(f"SUMMARY: {n_fail(corridors)} corridor(s) FAIL   "
          f"{n_fail(vias)} via keepout(s) FAIL   "
          f"{n_fail(hs_paths)} HS path(s) FAIL")
    print(f"         {n_fail(diffs)} diff pair sep(s) FAIL   "
          f"{n_fail(necks)} neckdown(s) FAIL   "
          f"{n_fail(edges)} board edge(s) FAIL")
    print(f"         {n_fail(types)} type-gap(s) FAIL")
    if applied is not None:
        print(f"         Apply: {applied} move(s) applied, {skipped} skipped")
    print("=" * 72)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Routing clearance audit — dry-run by default; --apply to correct.")
    parser.add_argument("--apply", action="store_true",
                        help="Apply corrections and save the PCB.")
    args = parser.parse_args()

    board = pcbnew.LoadBoard(cfg.PCB_FILE)
    fps   = {fp.GetReference(): fp for fp in board.GetFootprints()}

    hs_map = build_hs_net_widths()
    sw_map = build_switching_net_widths()
    rule_lookup = build_rule_lookup()

    print("=" * 72)
    print("ROUTE CLEARANCE AUDIT —",
          "APPLY MODE" if args.apply else "DRY RUN (no changes)")
    print(f"PCB: {cfg.PCB_FILE}")
    print("=" * 72)

    # Run all analyses first
    corridors = analyze_corridors(board, fps, hs_map, sw_map)
    vias      = analyze_via_keepout(fps, hs_map, sw_map)
    hs_paths  = analyze_hs_paths(fps, board)
    diffs     = analyze_diff_pair_sep(fps, board)
    necks     = analyze_neckdown(fps, hs_map, sw_map)
    edges     = analyze_board_margin(board, fps)
    types     = analyze_type_gap(fps)

    # ── 1. Trace corridor check ───────────────────────────────────────────
    _hdr(1, "Trace corridor check")
    if not corridors:
        print("  (no alignment groups)")
    for c in corridors:
        tag = "PASS" if c["pass"] else "FAIL"
        extra = "" if c["pass"] else f"  deficit: {c['deficit']:.2f}mm"
        print(f"  {c['group']}")
        print(f"    anchor: {c['anchor']:<6} side: {c['side']:<6} "
              f"actual_gap: {c['actual']:.2f}mm   "
              f"required: {c['required']:.2f}mm   {tag}{extra}")

    # ── 2. Via keepout check ──────────────────────────────────────────────
    _hdr(2, "Via keepout check")
    fails_v = [v for v in vias if not v["pass"]]
    if not fails_v:
        print(f"  All {len(vias)} pad(s) have via room. PASS")
    else:
        for v in fails_v:
            print(f"  {v['ref']:<12} pad {v['pad']} (net {v['net']}):  "
                  f"via_needed: {v['need']:.2f}mm   "
                  f"clear_space: {v['free']:.2f}mm   FAIL "
                  f"(nearest: {v['blocker']})")

    # ── 3. HS pair path check ─────────────────────────────────────────────
    _hdr(3, "HS pair path check")
    for h in hs_paths:
        if h["pass"]:
            print(f"  {h['pair']:<12} path clear   PASS")
        else:
            blist = ", ".join(h.get("blockers", []) or [])
            print(f"  {h['pair']:<12} blocker: {blist} intersects path envelope   FAIL")

    # ── 4. Diff pair separation check ─────────────────────────────────────
    _hdr(4, "Diff pair separation check")
    fails_d = [d for d in diffs if not d["pass"]]
    if not diffs:
        print("  (no diff-pair components detected)")
    elif not fails_d:
        print(f"  All {len(diffs)} diff pair component pair(s) meet separation. PASS")
    else:
        for d in fails_d:
            print(f"  {d['base']:<16} {d['p_ref']} ↔ {d['n_ref']}   "
                  f"gap: {d['gap']:.2f}mm   required: {d['required']:.2f}mm   "
                  f"FAIL  deficit: {d['deficit']:.2f}mm")

    # ── 5. Neckdown zone check ───────────────────────────────────────────
    _hdr(5, "Neckdown zone check")
    fails_n = [n for n in necks if not n["pass"]]
    if not necks:
        print("  No pads require neckdown. PASS")
    elif not fails_n:
        print(f"  All {len(necks)} neckdown zone(s) OK. PASS")
    else:
        for n in fails_n:
            print(f"  {n['ref']:<12} pad {n['pad']} (net {n['net']}): "
                  f"side {n['side']}  trace {n['trace_w']:.2f}mm > pad {n['pad_w']:.2f}mm  "
                  f"free: {n['free']:.2f}mm  required: {n['required']:.2f}mm  "
                  f"FAIL  deficit: {n['deficit']:.2f}mm")

    # ── 6. Board edge margin check ────────────────────────────────────────
    _hdr(6, "Board edge margin check")
    fails_e = [e for e in edges if not e["pass"]]
    if not fails_e:
        print(f"  All {len(edges)} component(s) within board margin. PASS")
    else:
        for e in fails_e:
            print(f"  {e['ref']:<12} side {e['side']:<6} clear: {e['clear']:.2f}mm   "
                  f"required: {e['required']:.2f}mm   FAIL  deficit: {e['deficit']:.2f}mm")

    # ── 7. Component-type gap check ───────────────────────────────────────
    _hdr(7, "Component-type gap check")
    if not types:
        print("  All component pairs meet type-gap requirement. PASS")
    else:
        for t in types:
            print(f"  {t['ref_a']:<14} ↔ {t['ref_b']:<14} "
                  f"gap: {t['gap']:.3f}mm  required: {t['required']:.3f}mm  "
                  f"FAIL  deficit: {t['deficit']:.3f}mm")

    if not args.apply:
        print_summary(corridors, vias, hs_paths, diffs, necks, edges, types)
        print("\nDRY RUN complete — no changes written.")
        print("Re-run with --apply to execute.")
        return

    # ── Apply mode ────────────────────────────────────────────────────────
    moves, build_skips = build_moves(
        corridors, vias, hs_paths, diffs, necks, edges, types,
        fps, rule_lookup, board)

    moves = apply_intra_group_spacing(moves, fps)
    moves = apply_lift_group_y_sync(moves, fps)
    moves = apply_group_axis_sync(moves, fps)

    _hdr("B", "Pre-apply overlap")
    safe_moves, overlap_skips = check_pre_apply_overlaps(moves, fps)

    # Second pass: if Check B skipped any move, treat those refs as locked
    # so their pair partners receive the full deficit instead of half.
    # This prevents check-C failures where one side of a pair was skipped
    # and the other side only moved half the needed clearance.
    skipped_refs = {m[0] for m in overlap_skips}
    if skipped_refs:
        print(f"  {len(overlap_skips)} move(s) skipped in pass 1 — rebuilding "
              f"with full deficit for their pair partners ...")
        moves, build_skips = build_moves(
            corridors, vias, hs_paths, diffs, necks, edges, types,
            fps, rule_lookup, board, extra_locked=skipped_refs)
        moves = apply_intra_group_spacing(moves, fps)
        moves = apply_lift_group_y_sync(moves, fps)
        moves = apply_group_axis_sync(moves, fps)
        safe_moves, overlap_skips = check_pre_apply_overlaps(moves, fps)

    if overlap_skips:
        print(f"  WARNING: {len(overlap_skips)} move(s) skipped to avoid overlap:")
        for ref, cx, cy, tx, ty, src, reason in overlap_skips:
            print(f"    {ref:<22} SKIPPED — {reason}")
    else:
        print(f"  All {len(safe_moves)} proposed move(s) are clear.")

    if build_skips:
        print(f"\n  Build-phase skips ({len(build_skips)}):")
        for ref, reason in build_skips:
            print(f"    {ref:<22} SKIP — {reason}")

    total_skipped = len(overlap_skips) + len(build_skips)

    if not safe_moves:
        print_summary(corridors, vias, hs_paths, diffs, necks, edges, types,
                      applied=0, skipped=total_skipped)
        print("\nNo safe moves to apply.")
        return

    print("\nApplying corrections ...")
    changed_refs = set()
    for ref, cx, cy, tx, ty, src in safe_moves:
        fps[ref].SetPosition(
            pcbnew.VECTOR2I(pcbnew.FromMM(tx), pcbnew.FromMM(ty)))
        print(f"  MOVED  {ref:<22} ({cx:.3f},{cy:.3f}) → ({tx:.3f},{ty:.3f})   "
              f"[{src}]")
        changed_refs.add(ref)

    board.Save(cfg.PCB_FILE)
    print(f"\nSaved: {cfg.PCB_FILE}")

    # ── Check C: post-apply verification ──────────────────────────────────
    _hdr("C", "Post-apply verification")
    board2   = pcbnew.LoadBoard(cfg.PCB_FILE)
    fps_live = {fp.GetReference(): fp for fp in board2.GetFootprints()}
    issues   = check_post_apply_overlaps(changed_refs, fps_live)
    if issues:
        print(f"  *** {len(issues)} OVERLAP(S) DETECTED after apply ***")
        for ref_a, ref_b, ov_x, ov_y in issues:
            print(f"    {ref_a} ↔ {ref_b}   overlap X={ov_x:.3f}mm  Y={ov_y:.3f}mm")
        print("  Review and revert if necessary.")
    else:
        print(f"  PASS — no overlaps among {len(changed_refs)} changed component(s).")

    print_summary(corridors, vias, hs_paths, diffs, necks, edges, types,
                  applied=len(safe_moves), skipped=total_skipped)


if __name__ == "__main__":
    main()
