"""
route_prep_align.py — Pre-route placement alignment for Phase 10.

Reads all configuration from routing_config.py and proximity_rules_config.py.
Contains zero hardcoded component refs, net names, or layer names.

Two passes:
  1. Alignment groups (routing_config.ALIGNMENT_GROUPS) — position + rotation.
  2. General rotation fix — any unlocked component whose current rotation is
     not canonical for its symmetry class is rotated to the canonical angle
     that places its primary signal pad closest to its proximity-rule anchor.

Overlap checks (three layers):
  A. Intra-group spacing — after resolving pad_track/column targets, detect if
     consecutive members sorted by track_axis would overlap using real footprint
     bbox widths; spread them iteratively from their centroid while preserving
     mean position.
  B. Pre-apply cross-component — before writing any move, compare every proposed
     bbox against all non-moving footprints; emit a WARNING and skip moves that
     would create an overlap (does not block rotation changes).
  C. Post-apply verification — after saving, re-check all moved footprints'
     bboxes against all other footprints and report any remaining overlaps.

Usage:
    python route_prep_align.py            # dry run — report only, no changes
    python route_prep_align.py --apply    # apply all changes and save PCB

Always review the dry-run report before applying.
"""

import sys
import os
import argparse
import math

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import routing_config as cfg
import proximity_rules_config as rules_cfg

sys.path.insert(0, cfg.KICAD_SITE_PKGS)
import pcbnew

# Minimum courtyard clearance assumed when no courtyard data is available (mm).
CLEARANCE_MM = 0.15
# Small epsilon added to Check-A min_gap so the enforced gap always exceeds
# CLEARANCE_MM by a safe margin, preventing floating-point ties in Check-B.
SPACING_EPSILON = 1e-4
# DRC trace-to-copper clearance floor — matches route_clearance_audit.py.
DRC_MIN_CLEARANCE = 0.10


# ── Canonical rotation sets ───────────────────────────────────────────────────

CANONICAL = {
    "180":   [0.0, 180.0],
    "4fold": [0.0, 90.0, 180.0, 270.0],
    "none":  [],
}


def norm360(deg):
    return deg % 360.0


def is_canonical(rot_deg, symmetry):
    candidates = CANONICAL.get(symmetry, [])
    r = norm360(rot_deg)
    return any(min(abs(r - c), 360.0 - abs(r - c)) < 0.5 for c in candidates)


def nearest_canonical(rot_deg, symmetry):
    candidates = CANONICAL.get(symmetry, [])
    if not candidates:
        return None
    r = norm360(rot_deg)
    return min(candidates, key=lambda c: min(abs(r - c), 360.0 - abs(r - c)))


# ── Coordinate helpers ────────────────────────────────────────────────────────

def fp_xy(fp):
    return pcbnew.ToMM(fp.GetPosition().x), pcbnew.ToMM(fp.GetPosition().y)


def fp_half_extents(fp):
    """Return (half_w, half_h) of footprint bbox in mm at the footprint's current rotation."""
    bb = fp.GetBoundingBox()
    hw = pcbnew.ToMM(bb.GetWidth())  / 2.0
    hh = pcbnew.ToMM(bb.GetHeight()) / 2.0
    return hw, hh


def fp_half_at_target_rot(fp, target_rot, axis):
    """
    Return half-extent along axis ('X' or 'Y') as if the footprint were at target_rot.
    Works by reading the bbox at the current rotation and swapping W↔H when the
    rotation class changes by 90° (horizontal↔vertical for rectangular packages).
    """
    hw, hh = fp_half_extents(fp)
    cur_mod = round(norm360(fp.GetOrientationDegrees())) % 180
    tgt_mod = round(norm360(target_rot)) % 180
    if cur_mod != tgt_mod:
        hw, hh = hh, hw  # 90° class change — swap axes
    return hw if axis == "X" else hh


def fp_bbox_mm(fp, cx=None, cy=None):
    """
    Return (x_min, y_min, x_max, y_max) of fp at position (cx, cy).
    If cx/cy are None, uses the footprint's current position.
    """
    hw, hh = fp_half_extents(fp)
    if cx is None or cy is None:
        cx, cy = fp_xy(fp)
    return cx - hw, cy - hh, cx + hw, cy + hh


def bboxes_overlap(a, b, clearance=0.0):
    """Return True if bbox a and b overlap (including clearance gap)."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return (ax0 - clearance < bx1 and ax1 + clearance > bx0 and
            ay0 - clearance < by1 and ay1 + clearance > by0)


def pad_local_xy(fp, pad):
    """Return pad offset (lx, ly) in the footprint's local frame at any rotation."""
    cx, cy = fp_xy(fp)
    px = pcbnew.ToMM(pad.GetPosition().x)
    py = pcbnew.ToMM(pad.GetPosition().y)
    dx, dy = px - cx, py - cy
    r = math.radians(norm360(fp.GetOrientationDegrees()))
    lx =  dx * math.cos(r) + dy * math.sin(r)
    ly = -dx * math.sin(r) + dy * math.cos(r)
    return lx, ly


def pad_world_at_rot(fp, pad, new_rot_deg):
    """Return (x, y) world position of pad if footprint were at new_rot_deg."""
    cx, cy = fp_xy(fp)
    lx, ly = pad_local_xy(fp, pad)
    r = math.radians(norm360(new_rot_deg))
    wx = cx + lx * math.cos(r) - ly * math.sin(r)
    wy = cy + lx * math.sin(r) + ly * math.cos(r)
    return wx, wy


def find_pad_by_net(fp, net_name):
    """Return the pad on fp carrying net_name, or None."""
    for pad in fp.Pads():
        if pad.GetNetname() == net_name:
            return pad
    return None


def find_anchor_pad_world(anchor_fp, net_name, near_pos=None, prefer_x=None):
    """
    Return (x, y) of the pad on anchor_fp with net_name.
    When prefer_x is given, selects the matching pad whose X is closest to prefer_x.
    Otherwise when near_pos=(x,y) is given, returns the matching pad closest to that point.
    When neither is given, returns the first matching pad.
    """
    matching = []
    for pad in anchor_fp.Pads():
        if pad.GetNetname() == net_name:
            px = pcbnew.ToMM(pad.GetPosition().x)
            py = pcbnew.ToMM(pad.GetPosition().y)
            matching.append((px, py))
    if not matching:
        return None
    if prefer_x is not None:
        return min(matching, key=lambda p: abs(p[0] - prefer_x))
    if near_pos is None:
        return matching[0]
    return min(matching, key=lambda p: math.hypot(p[0] - near_pos[0], p[1] - near_pos[1]))


# ── Intra-group overlap check / spacing enforcement ───────────────────────────

def enforce_min_spacing_axis(members_xy, fps, track_axis, target_rots=None):
    """
    Given a list of (ref, tx, ty) proposals, spread them along track_axis so
    that no two consecutive members (when sorted by track coordinate) are closer
    than their combined half-extents + CLEARANCE_MM.

    target_rots: optional dict {ref: planned_rotation_degrees}.  When supplied,
    half-extents are computed at the planned rotation rather than the current one.
    This is critical when a group move also changes component rotation (e.g. a
    component at 90° being moved and rotated to 0° is wider in X than it is now).

    Algorithm: forward pass (push each element to clear the previous), then shift
    the whole group to restore the original centroid.  Converges in one pass.

    Returns an adjusted list of (ref, tx, ty).
    """
    if len(members_xy) < 2:
        return members_xy

    axis_idx = 0 if track_axis == "X" else 1
    items = sorted(members_xy, key=lambda t: t[1] if axis_idx == 0 else t[2])

    def get_coord(item):
        return item[1] if axis_idx == 0 else item[2]

    def set_coord(item, val):
        r, x, y = item
        return (r, val, y) if axis_idx == 0 else (r, x, val)

    def get_half(ref):
        fp = fps.get(ref)
        if fp is None:
            return 0.75  # 0402 fallback
        tgt_rot = (target_rots or {}).get(ref)
        if tgt_rot is not None:
            return fp_half_at_target_rot(fp, tgt_rot, track_axis)
        hw, hh = fp_half_extents(fp)
        return hw if axis_idx == 0 else hh

    orig_centroid = sum(get_coord(t) for t in items) / len(items)

    # Forward pass: each element must clear the one before it
    for i in range(1, len(items)):
        prev_ref = items[i - 1][0]
        cur_ref  = items[i][0]
        min_gap  = get_half(prev_ref) + CLEARANCE_MM + SPACING_EPSILON + get_half(cur_ref)
        min_pos  = get_coord(items[i - 1]) + min_gap
        if get_coord(items[i]) < min_pos:
            items[i] = set_coord(items[i], min_pos)

    # Restore original centroid
    new_centroid = sum(get_coord(t) for t in items) / len(items)
    shift = orig_centroid - new_centroid
    if abs(shift) > 1e-6:
        items = [set_coord(item, get_coord(item) + shift) for item in items]

    return items


# ── Cross-component pre-apply overlap check ───────────────────────────────────

def check_pre_apply_overlaps(all_moves, fps, pending_rotations=None):
    """
    For each proposed move in all_moves [(ref, cx, cy, tx, ty, source)],
    check the proposed bbox against all other footprints.

    Co-moved components are evaluated at their PROPOSED positions (not current),
    so that intra-group moves that are already spaced correctly by Check A do not
    falsely block each other.  External (non-moved) components are evaluated at
    their current positions.

    pending_rotations: optional dict {ref: target_rot_deg}.  When supplied,
    components with a pending rotation are evaluated at the target rotation's
    bbox dimensions rather than their current rotation, preventing a position
    move from being falsely blocked by the component's pre-rotation footprint.

    Returns (safe_moves, skipped_moves).
    """
    pr = pending_rotations or {}

    # Push-group members are moved by route_clearance_audit.py, not here.
    # Their current (pre-push) positions must not block pad_track/column placements.
    push_member_set = set()
    for _grp in cfg.ALIGNMENT_GROUPS:
        if _grp.get("type") == "push":
            push_member_set |= set(_group_member_refs(_grp, fps))

    # Map ref → group name so intra-group co-moves are not re-checked here.
    # Intra-group spacing was already handled (or intentionally bypassed) by Check A.
    ref_to_group_name = {}
    for _grp in cfg.ALIGNMENT_GROUPS:
        for _m in _group_member_refs(_grp, fps):
            ref_to_group_name[_m] = _grp["name"]

    # All proposed final positions — used for other moving components
    all_proposed = {ref: (tx, ty) for ref, cx, cy, tx, ty, source in all_moves}

    safe    = []
    skipped = []

    for move in all_moves:
        ref, cx, cy, tx, ty, source = move
        fp = fps.get(ref)
        if fp is None:
            safe.append(move)
            continue

        if ref in pr:
            hw = fp_half_at_target_rot(fp, pr[ref], 'X')
            hh = fp_half_at_target_rot(fp, pr[ref], 'Y')
            prop_bbox = (tx - hw, ty - hh, tx + hw, ty + hh)
        else:
            prop_bbox = fp_bbox_mm(fp, tx, ty)

        # Current bbox of the footprint being evaluated (before any moves).
        cur_bbox = fp_bbox_mm(fp)

        conflict = None
        for other_ref, other_fp in fps.items():
            if other_ref == ref:
                continue
            # Push-group members will be moved by route_clearance_audit.py — skip them.
            if other_ref in push_member_set:
                continue
            # Co-moved: use its proposed position so intra-group spacing is respected
            if other_ref in all_proposed:
                ox, oy = all_proposed[other_ref]
                # Intra-group co-moves: spacing already handled by Check A (or
                # intentionally bypassed via skip_check_a) — don't re-check here.
                grp_name = ref_to_group_name.get(ref)
                if grp_name and grp_name == ref_to_group_name.get(other_ref):
                    continue
            else:
                ox, oy = fp_xy(other_fp)
            if other_ref in pr:
                ohw = fp_half_at_target_rot(other_fp, pr[other_ref], 'X')
                ohh = fp_half_at_target_rot(other_fp, pr[other_ref], 'Y')
                other_bbox = (ox - ohw, oy - ohh, ox + ohw, oy + ohh)
            else:
                other_bbox = fp_bbox_mm(other_fp, ox, oy)
            if bboxes_overlap(prop_bbox, other_bbox, clearance=CLEARANCE_MM):
                # Pre-existing check: if ref already violates clearance with
                # other_ref at current positions, the move does not introduce a
                # new conflict — allow it so pre-existing proximity doesn't block
                # intentional repositioning of tightly-packed components.
                other_cur_bbox = fp_bbox_mm(other_fp)
                if bboxes_overlap(cur_bbox, other_cur_bbox, clearance=CLEARANCE_MM):
                    continue
                conflict = other_ref
                break

        if conflict:
            skipped.append((ref, cx, cy, tx, ty, source,
                            f"proposed bbox overlaps {conflict}"))
        else:
            safe.append(move)

    return safe, skipped


# ── Post-apply overlap verification ──────────────────────────────────────────

def check_post_apply_overlaps(moved_refs, fps_live, orig_bboxes=None):
    """
    After the board has been saved, re-read each moved footprint's bbox and
    check against all other footprints.  Returns list of (ref_a, ref_b, overlap_mm).
    fps_live must be re-built from the just-saved board.
    orig_bboxes, if provided, is {ref: (x0,y0,x1,y1)} captured BEFORE apply;
    overlaps that already existed at original positions are suppressed.
    """
    issues = []
    seen   = set()

    # Push-group members will be moved by route_clearance_audit.py after this script.
    # Suppress Check-C overlaps against them — their pre-push positions are expected
    # to be close to newly placed pad_track/column components.
    push_member_refs = set()
    for _grp in cfg.ALIGNMENT_GROUPS:
        if _grp.get("type") == "push":
            push_member_refs |= set(_group_member_refs(_grp, fps_live))

    for ref_a in moved_refs:
        fp_a = fps_live.get(ref_a)
        if fp_a is None:
            continue
        bbox_a = fp_bbox_mm(fp_a)
        for ref_b, fp_b in fps_live.items():
            if ref_b == ref_a or (ref_a, ref_b) in seen or (ref_b, ref_a) in seen:
                continue
            if ref_b in push_member_refs:
                continue
            if bboxes_overlap(bbox_a, fp_bbox_mm(fp_b), clearance=0.0):
                # Suppress if this overlap already existed before our moves.
                if orig_bboxes is not None:
                    ob_a = orig_bboxes.get(ref_a)
                    ob_b = orig_bboxes.get(ref_b)
                    if ob_a is not None and ob_b is not None:
                        if bboxes_overlap(ob_a, ob_b, clearance=0.0):
                            seen.add((ref_a, ref_b))
                            continue
                seen.add((ref_a, ref_b))
                # Estimate overlap magnitude on both axes
                ax0, ay0, ax1, ay1 = bbox_a
                bx0, by0, bx1, by1 = fp_bbox_mm(fp_b)
                ov_x = min(ax1, bx1) - max(ax0, bx0)
                ov_y = min(ay1, by1) - max(ay0, by0)
                issues.append((ref_a, ref_b, ov_x, ov_y))
    return issues


# ── Rotation overlap check (Check B-rot) ─────────────────────────────────────

def check_rotation_overlaps(all_rotates, fps, proposed_positions,
                             pending_rotations=None):
    """
    For each proposed rotation, compute the component's bbox at the target rotation
    (at its current or proposed position) and check for overlap with all other
    footprints.  Components with pending position changes are evaluated at their
    proposed positions.

    proposed_positions:  {ref: (tx, ty)} from safe moves — used so rotations are
                         evaluated at the final state, not mid-flight state.
    pending_rotations:   {ref: tgt_rot_deg} — when set, other components whose
                         rotation is also changing are evaluated at their TARGET
                         rotation bbox instead of their current rotation bbox.
                         Omitting this causes false-positive suppressions when two
                         co-rotated components are moving from a large bbox to a
                         small one.

    Returns (safe_rotates, skipped_rotates) where each entry in skipped has an
    extra 'conflict_reason' string appended.
    """
    pr      = pending_rotations or {}
    safe    = []
    skipped = []

    for rot_entry in all_rotates:
        ref, cur_rot, tgt_rot, reason = rot_entry
        fp = fps.get(ref)
        if fp is None:
            safe.append(rot_entry)
            continue

        # Use proposed position if this ref is also being moved
        if ref in proposed_positions:
            cx, cy = proposed_positions[ref]
        else:
            cx, cy = fp_xy(fp)

        # Compute bbox at target rotation using rotated half-extents
        hw = fp_half_at_target_rot(fp, tgt_rot, "X")
        hh = fp_half_at_target_rot(fp, tgt_rot, "Y")
        prop_bbox = (cx - hw, cy - hh, cx + hw, cy + hh)

        conflict = None
        for other_ref, other_fp in fps.items():
            if other_ref == ref:
                continue
            if other_ref in proposed_positions:
                ox, oy = proposed_positions[other_ref]
            else:
                ox, oy = fp_xy(other_fp)
            # Use target rotation for other component if it has a pending rotation,
            # to avoid false-positive overlap from its pre-rotation (larger) bbox.
            if other_ref in pr:
                ohw = fp_half_at_target_rot(other_fp, pr[other_ref], "X")
                ohh = fp_half_at_target_rot(other_fp, pr[other_ref], "Y")
                other_bbox = (ox - ohw, oy - ohh, ox + ohw, oy + ohh)
            else:
                other_bbox = fp_bbox_mm(other_fp, ox, oy)
            if bboxes_overlap(prop_bbox, other_bbox, clearance=CLEARANCE_MM):
                conflict = other_ref
                break

        if conflict:
            skipped.append((ref, cur_rot, tgt_rot, reason,
                            f"rotated bbox overlaps {conflict}"))
        else:
            safe.append(rot_entry)

    return safe, skipped


# ── Helpers shared by ray_place and blocker-clearing ─────────────────────────

def _member_half(fp, tgt_rot, axis):
    """Half-extent of fp along axis at target rotation (or current rotation if None)."""
    if tgt_rot is not None:
        return fp_half_at_target_rot(fp, tgt_rot, axis)
    hw, hh = fp_half_extents(fp)
    return hw if axis == "X" else hh


# ── Corridor-clearance helpers ────────────────────────────────────────────────

def build_hs_map():
    """Return {net_name: (pair_name, width_mm)} for all HS pair nets."""
    hs_map = {}
    for pair_name, (p_net, n_net, _layer, _skew) in cfg.HS_PAIRS.items():
        w, _ = cfg.HS_ROUTE_WIDTHS.get(pair_name, (0.127, 0.10))
        for net in (p_net, n_net):
            hs_map[net] = (pair_name, w)
    return hs_map


def build_sw_map():
    """Return {net_name: width_mm} for all switching-loop nets."""
    sw_map = {}
    for loop in cfg.SWITCHING_LOOPS:
        sw_map[loop["sw_net"]]  = loop["sw_width_mm"]
        sw_map[loop["vin_net"]] = loop["vin_width_mm"]
        sw_map[loop["out_net"]] = loop["out_width_mm"]
        if "bootstrap_net" in loop:
            sw_map[loop["bootstrap_net"]] = loop.get("bootstrap_width_mm", 0.20)
    return sw_map


def _trace_w(net, hs_map, sw_map):
    """Look up net trace width: HS → switching → signal default."""
    if net in hs_map:
        return hs_map[net][1]
    if net in sw_map:
        return sw_map[net]
    return cfg.CLEARANCE_AUDIT.get("signal_trace_width_mm", 0.20)


def _shared_nets(fp_a, fp_b):
    """Return set of non-empty net names shared between fp_a and fp_b pads."""
    nets_a = {p.GetNetname() for p in fp_a.Pads() if p.GetNetname()}
    nets_b = {p.GetNetname() for p in fp_b.Pads() if p.GetNetname()}
    return nets_a & nets_b


def _corridor_required(anchor_fp, member_fps, hs_map, sw_map):
    """
    Minimum routing corridor gap between anchor edge and member near-edge.
    Formula: n_shared_nets × (max_trace_width + DRC_MIN_CLEARANCE) + corridor_margin_mm.
    Matches route_clearance_audit.py::analyze_corridors.
    """
    margin = cfg.CLEARANCE_AUDIT.get("corridor_margin_mm", 0.50)
    shared = set()
    for mfp in member_fps:
        shared |= _shared_nets(anchor_fp, mfp)
    if not shared:
        return margin
    widths = [_trace_w(n, hs_map, sw_map) for n in shared]
    return len(widths) * (max(widths) + DRC_MIN_CLEARANCE) + margin


def _enforce_corridor(configured, anchor_fp, side, member_fps, tgt_rot, hs_map, sw_map):
    """
    Adjust configured fixed-axis coordinate outward so the gap between the
    anchor's edge on 'side' and the nearest member edge meets the required
    routing corridor width.

    configured: fixed-axis target coord (col_x for left/right groups,
                first y_cur for above/below groups and rows)
    side:       'left', 'right', 'above', 'below'
    Returns (adjusted_coord, delta_mm) — delta_mm > 0 when an adjustment was made.
    """
    if not member_fps:
        return configured, 0.0
    required = _corridor_required(anchor_fp, member_fps, hs_map, sw_map)
    ax0, ay0, ax1, ay1 = fp_bbox_mm(anchor_fp)

    if side in ('left', 'right'):
        max_half = max(_member_half(fp, tgt_rot, 'X') for fp in member_fps)
    else:
        max_half = max(_member_half(fp, tgt_rot, 'Y') for fp in member_fps)

    if side == 'right':
        floor = ax1 + required + max_half
        if configured < floor:
            return floor, floor - configured
    elif side == 'left':
        ceil_ = ax0 - required - max_half
        if configured > ceil_:
            return ceil_, configured - ceil_
    elif side == 'above':
        ceil_ = ay0 - required - max_half
        if configured > ceil_:
            return ceil_, configured - ceil_
    elif side == 'below':
        floor = ay1 + required + max_half
        if configured < floor:
            return floor, floor - configured
    return configured, 0.0


# ── Intermediate-pad gap helper ───────────────────────────────────────────────

def _intermediate_pad_gap(anchor_fp, track_axis, low, high, hs_map, sw_map,
                          skip_nets=None):
    """
    Return the minimum body-to-body routing gap required so that traces from
    any anchor pad whose coordinate along track_axis falls strictly between
    low and high can route through the gap between two adjacent group members.

    track_axis: "Y" for column groups (members stacked vertically),
                "X" for row groups (members stacked horizontally).

    skip_nets: set of net names to exclude.  Pass the union of both adjacent
    members' pad nets so that anchor pads which connect directly to one of the
    two members are not counted — those traces go straight to their destination
    and do not need to pass through the inter-member gap.

    gap = trace_width + 2 * DRC_MIN_CLEARANCE + corridor_margin_mm.

    Only pads carrying a non-empty net are considered.  Returns 0.0 when no
    such pads exist in the range (no extra gap needed beyond bbox clearance).
    """
    margin = cfg.CLEARANCE_AUDIT.get("corridor_margin_mm", 0.50)
    max_gap = 0.0
    for pad in anchor_fp.Pads():
        pos = pad.GetPosition()
        coord = pcbnew.ToMM(pos.y if track_axis == "Y" else pos.x)
        if low < coord < high:
            net = pad.GetNetname()
            if not net:
                continue
            if skip_nets and net in skip_nets:
                continue
            w = _trace_w(net, hs_map, sw_map)
            gap = w + 2 * DRC_MIN_CLEARANCE + margin
            if gap > max_gap:
                max_gap = gap
    return max_gap


# ── Proximity rule lookup ─────────────────────────────────────────────────────

def build_rule_lookup():
    """Return {ref_a: (anchor_ref, max_dist_mm)} — first rule per ref_a."""
    lookup = {}
    for ref_a, ref_b, max_dist_mm, _rt, _desc, _net, *_ in rules_cfg.RULES:
        if ref_a not in lookup:
            lookup[ref_a] = (ref_b, max_dist_mm)
    return lookup


def try_clear_blocker(blocker_ref, blocked_bboxes, fps, rule_lookup,
                      all_proposed=None):
    """
    Find the minimum cardinal displacement for blocker_ref that:
      (a) places it clear of ALL blocked_bboxes (with CLEARANCE_MM gap)
      (b) keeps it within its proximity rule max_dist_mm from its anchor centroid
      (c) does not overlap any other footprint at the new position

    blocked_bboxes: list of (x0,y0,x1,y1) proposed bboxes of blocked group members.
    all_proposed:   {ref: (tx,ty)} of all pending moves — used to evaluate
                    co-moved components at their final positions, not current ones.
    Returns (dx, dy) if a valid displacement exists, else None.
    """
    fp = fps.get(blocker_ref)
    if fp is None or fp.IsLocked():
        return None

    cx, cy = fp_xy(fp)
    hw, hh = fp_half_extents(fp)
    ap = all_proposed or {}

    # Union bounding box of all blocked targets
    u_x0 = min(b[0] for b in blocked_bboxes)
    u_y0 = min(b[1] for b in blocked_bboxes)
    u_x1 = max(b[2] for b in blocked_bboxes)
    u_y1 = max(b[3] for b in blocked_bboxes)

    # Four candidates: minimum displacement to place blocker just clear of union
    # on each side (left / right / above / below).
    eps = 1e-4
    candidates = [
        (u_x0 - CLEARANCE_MM - hw - cx - eps, 0.0),  # clear to LEFT  of union
        (u_x1 + CLEARANCE_MM + hw - cx + eps, 0.0),  # clear to RIGHT of union
        (0.0, u_y0 - CLEARANCE_MM - hh - cy - eps),  # clear ABOVE union
        (0.0, u_y1 + CLEARANCE_MM + hh - cy + eps),  # clear BELOW union
    ]

    rule = rule_lookup.get(blocker_ref)
    valid = []
    for dx, dy in candidates:
        new_cx, new_cy = cx + dx, cy + dy

        # (b) Proximity rule check
        if rule is not None:
            anchor_ref, max_dist_mm = rule
            anchor_fp = fps.get(anchor_ref)
            if anchor_fp is not None:
                ax, ay = fp_xy(anchor_fp)
                if math.hypot(new_cx - ax, new_cy - ay) > max_dist_mm:
                    continue

        # (c) Full footprint overlap check — reject directions that create new conflicts.
        # Co-moved components are evaluated at their proposed positions so we don't
        # falsely block against group members that are also moving away.
        prop_bbox = (new_cx - hw, new_cy - hh, new_cx + hw, new_cy + hh)
        conflict = False
        for other_ref, other_fp in fps.items():
            if other_ref == blocker_ref:
                continue
            ox, oy = ap[other_ref] if other_ref in ap else fp_xy(other_fp)
            if bboxes_overlap(prop_bbox, fp_bbox_mm(other_fp, ox, oy),
                              clearance=CLEARANCE_MM):
                conflict = True
                break
        if conflict:
            continue

        valid.append((math.hypot(dx, dy), dx, dy))

    if not valid:
        return None
    valid.sort(key=lambda t: t[0])
    _, dx, dy = valid[0]
    return dx, dy


# ── Rotation target lookup (from proximity rules) ─────────────────────────────

def build_rotation_targets():
    """
    For each ref_a in RULES, record its anchor (ref_b) and the net_hint.
    Returns {ref_a: (anchor_ref, net_hint), ...} — first rule per ref_a.
    """
    targets = {}
    for entry in rules_cfg.RULES:
        ref_a, ref_b, _dist, _rtype, _desc, net_hint, *_ = entry
        if ref_a not in targets and net_hint:
            targets[ref_a] = (ref_b, net_hint)
    return targets


# ── Push group helpers ────────────────────────────────────────────────────────

def _push_auto_members(group, fps):
    """
    Return list of refs for all unlocked footprints whose centroid lies beyond
    the anchor's bbox edge in the group's push direction.
    """
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
    """Return the list of member refs for any group type, resolving 'auto'."""
    raw = group.get("members", [])
    if raw == "auto":
        return _push_auto_members(group, fps)
    return [m["ref"] if isinstance(m, dict) else m for m in raw]


# ── Alignment group processing ────────────────────────────────────────────────

def process_pad_track(group, fps):
    """
    Resolve a pad_track group.
    Returns (results, error) where results is a list of
    (ref, tgt_x, tgt_y, tgt_rot_or_None, error_str_or_None).
    """
    anchor_ref = group["anchor_ref"]
    if anchor_ref not in fps:
        return [], f"anchor {anchor_ref!r} not in PCB"

    anchor_fp  = fps[anchor_ref]
    track_axis = group["track_axis"]   # "X" or "Y"
    fixed_axis = group["fixed_axis"]   # "X" or "Y"
    offset     = group["fixed_offset_mm"]
    tgt_rot    = group.get("target_rotation")
    members    = group["members"]

    fixed_vals = []
    track_vals = {}
    for m in members:
        ref, net = m["ref"], m["anchor_pad_net"]
        member_fp = fps.get(ref)
        near      = fp_xy(member_fp) if member_fp else None
        prefer_x  = m.get("anchor_pad_x_prefer")
        pos = find_anchor_pad_world(anchor_fp, net, near_pos=near, prefer_x=prefer_x)
        if pos is None:
            return [], f"net {net!r} not found on {anchor_ref}"
        px, py = pos
        track_vals[ref] = px if track_axis == "X" else py
        fixed_vals.append(py if fixed_axis == "Y" else px)

    target_fixed = sum(fixed_vals) / len(fixed_vals) + offset

    raw_results = []
    for m in members:
        ref = m["ref"]
        if ref not in fps:
            raw_results.append((ref, None, None, None, f"{ref!r} not in PCB"))
            continue
        tv = track_vals[ref]
        if track_axis == "X":
            raw_results.append((ref, tv, target_fixed, tgt_rot, None))
        else:
            raw_results.append((ref, target_fixed, tv, tgt_rot, None))

    # ── Check A: intra-group spacing enforcement ──────────────────────────────
    valid_xy   = [(r, tx, ty) for r, tx, ty, _, err in raw_results if err is None]
    # Build planned-rotation map so spacing uses POST-ROTATION bbox dimensions
    planned_rots = {r: rot for r, _, _, rot, err in raw_results
                    if err is None and rot is not None}
    if len(valid_xy) >= 2:
        adjusted = enforce_min_spacing_axis(valid_xy, fps, track_axis,
                                            target_rots=planned_rots)
        adj_map  = {r: (tx, ty) for r, tx, ty in adjusted}
    else:
        adj_map = {}

    results = []
    for ref, tx, ty, rot, err in raw_results:
        if err is not None:
            results.append((ref, tx, ty, rot, err))
        elif ref in adj_map:
            atx, aty = adj_map[ref]
            if abs(atx - tx) > 1e-4 or abs(aty - ty) > 1e-4:
                spacing_note = (f"spacing-adjusted: "
                                f"({tx:.3f},{ty:.3f}) → ({atx:.3f},{aty:.3f})")
            else:
                spacing_note = None
            results.append((ref, atx, aty, rot, spacing_note))
        else:
            results.append((ref, tx, ty, rot, None))

    return results, None


def process_column(group, fps, hs_map, sw_map, pending_moves=None):
    """
    Resolve a column group.
    Returns (results, error) where results is a list of
    (ref, tgt_x, tgt_y, tgt_rot_or_None, error_str_or_None).
    pending_moves: {ref: (tx, ty)} of moves proposed by prior groups — used so
    a column can anchor to its anchor's proposed position rather than its
    current PCB position.
    """
    anchor_ref = group["anchor_ref"]
    if anchor_ref not in fps:
        return [], f"anchor {anchor_ref!r} not in PCB"

    anchor_fp = fps[anchor_ref]
    pm = pending_moves or {}
    if anchor_ref in pm:
        ax, ay = pm[anchor_ref]
    else:
        ax, ay  = fp_xy(anchor_fp)
    x_off   = group.get("x_offset_mm", 0.0)
    y_off   = group.get("y_start_mm", 0.0)
    col_x        = ax + x_off
    y_cur        = ay + y_off
    pitch        = group.get("pitch_mm", 1.65)
    first_pitch  = group.get("first_pitch_mm")    # overrides pitch for member[0]→member[1] step only
    tgt_rot      = group.get("target_rotation")
    preserve_y   = group.get("preserve_y_spacing", False)

    member_fps  = [fps[r] for r in group["members"] if r in fps]
    skip_corr   = group.get("skip_corridor", False)

    # Enforce minimum corridor clearance between anchor edge and member near-edge.
    # Primary direction: y when |y_off| > |x_off| (diagonally-placed column), else x.
    # Set skip_corridor: true in the group config when the gap between anchor and
    # members is not a routing corridor (e.g. two components on the same power rail).
    if skip_corr:
        adj = 0.0
    elif x_off == 0.0 or (y_off != 0.0 and abs(y_off) > abs(x_off)):
        side = "above" if y_off < 0.0 else "below"
        y_cur, adj = _enforce_corridor(y_cur, anchor_fp, side, member_fps, tgt_rot, hs_map, sw_map)
        if adj > 1e-4:
            print(f"    [corridor] y_start expanded by +{adj:.3f}mm "
                  f"(required corridor = {_corridor_required(anchor_fp, member_fps, hs_map, sw_map):.2f}mm)")
    else:
        side = "right" if x_off > 0.0 else "left"
        col_x, adj = _enforce_corridor(col_x, anchor_fp, side, member_fps, tgt_rot, hs_map, sw_map)
        if adj > 1e-4:
            print(f"    [corridor] x_offset expanded by +{adj:.3f}mm "
                  f"(required corridor = {_corridor_required(anchor_fp, member_fps, hs_map, sw_map):.2f}mm)")

    if preserve_y:
        # Preserve existing Y spacing from the clean backup state — same rigid-body
        # approach as push groups.  Read each member's current Y (or pending Y),
        # translate the group so the member nearest y_cur lands exactly at y_cur,
        # then set every member's X to col_x.  Check A is implicitly skipped so
        # the existing inter-member gaps are preserved without expansion.
        member_ys = {}
        for ref in group["members"]:
            if ref not in fps:
                continue
            member_ys[ref] = pm[ref][1] if ref in pm else fp_xy(fps[ref])[1]
        raw_results = []
        if member_ys:
            ref_nearest = min(member_ys, key=lambda r: abs(member_ys[r] - y_cur))
            delta = y_cur - member_ys[ref_nearest]
            if abs(delta) > 1e-4:
                print(f"    [preserve-y] translating group by Δy={delta:+.3f}mm "
                      f"(ref member: {ref_nearest})")
            for ref in group["members"]:
                if ref not in fps:
                    raw_results.append((ref, None, None, None, f"{ref!r} not in PCB"))
                else:
                    new_y = member_ys.get(ref, fp_xy(fps[ref])[1]) + delta
                    raw_results.append((ref, col_x, new_y, tgt_rot, None))
    else:
        raw_results = []
        for i, ref in enumerate(group["members"]):
            if ref not in fps:
                raw_results.append((ref, None, None, None, f"{ref!r} not in PCB"))
            else:
                raw_results.append((ref, col_x, y_cur, tgt_rot, None))
            y_cur += first_pitch if (i == 0 and first_pitch is not None) else pitch

    # ── Check A: intra-group spacing enforcement (Y axis) ────────────────────
    # Skipped when preserve_y_spacing is True (existing gaps are intentional) or
    # when skip_check_a is explicitly set (fixed pitch below clearance threshold).
    valid_xy     = [(r, tx, ty) for r, tx, ty, _, err in raw_results if err is None]
    planned_rots = {r: rot for r, _, _, rot, err in raw_results
                    if err is None and rot is not None}
    skip_check_a = preserve_y or group.get("skip_check_a", False)
    if not skip_check_a and len(valid_xy) >= 2:
        adjusted = enforce_min_spacing_axis(valid_xy, fps, "Y",
                                            target_rots=planned_rots)
        adj_map  = {r: (tx, ty) for r, tx, ty in adjusted}
    else:
        adj_map  = {r: (tx, ty) for r, tx, ty in valid_xy}

    # ── Intermediate-pad gap enforcement ─────────────────────────────────────
    # For each consecutive pair (sorted by Y), check if anchor pads whose Y
    # falls between the two member centers require a larger body-to-body gap
    # than bbox clearance alone provides.  Shift all subsequent members down.
    # Set skip_pad_gap: true in the group config when column members connect
    # directly to the anchor pads between them (traces go straight to the pad,
    # not through the inter-member gap).
    skip_pad_gap = group.get("skip_pad_gap", False)
    if not skip_pad_gap and len(adj_map) >= 2:
        sorted_refs = sorted(adj_map, key=lambda r: adj_map[r][1])
        for i in range(1, len(sorted_refs)):
            ref_prev = sorted_refs[i - 1]
            ref_curr = sorted_refs[i]
            _, y_prev = adj_map[ref_prev]
            x_curr, y_curr = adj_map[ref_curr]
            fp_prev = fps.get(ref_prev)
            fp_curr = fps.get(ref_curr)
            hh_prev = fp_half_extents(fp_prev)[1] if fp_prev else 0.5
            hh_curr = fp_half_extents(fp_curr)[1] if fp_curr else 0.5
            current_body_gap = (y_curr - hh_curr) - (y_prev + hh_prev)
            skip_nets = set()
            for mr in (ref_prev, ref_curr):
                mfp = fps.get(mr)
                if mfp:
                    skip_nets |= {p.GetNetname() for p in mfp.Pads() if p.GetNetname()}
            req = _intermediate_pad_gap(
                anchor_fp, "Y", y_prev, y_curr, hs_map, sw_map, skip_nets=skip_nets)
            if req > current_body_gap + 1e-4:
                extra = req - current_body_gap
                print(f"    [pad-gap] {ref_prev}↔{ref_curr}: "
                      f"intermediate anchor pad needs {req:.2f}mm body gap "
                      f"(current {current_body_gap:.2f}mm) → shift +{extra:.3f}mm")
                for j in range(i, len(sorted_refs)):
                    rj = sorted_refs[j]
                    xj, yj = adj_map[rj]
                    adj_map[rj] = (xj, yj + extra)

    results = []
    for ref, tx, ty, rot, err in raw_results:
        if err is not None:
            results.append((ref, tx, ty, rot, err))
        elif ref in adj_map:
            atx, aty = adj_map[ref]
            if abs(atx - tx) > 1e-4 or abs(aty - ty) > 1e-4:
                spacing_note = (f"spacing-adjusted: "
                                f"({tx:.3f},{ty:.3f}) → ({atx:.3f},{aty:.3f})")
            else:
                spacing_note = None
            results.append((ref, atx, aty, rot, spacing_note))
        else:
            results.append((ref, tx, ty, rot, None))

    return results, None


def process_lift(group, fps, pending_moves=None):
    """
    Resolve a lift group — moves all members to a fixed Y while preserving
    their current X coordinates.  Use when members should shift vertically as a
    unit without disturbing their relative horizontal positions or spacing.

    Y target = anchor_centroid_Y + y_offset_mm.
    X is read from each member's current PCB position (or pending position if a
    prior group already proposed an X move for that member).

    No pitch, no x_start, no corridor check — the lift only changes Y.

    pending_moves: {ref: (tx, ty)} of moves proposed by prior groups.
    """
    anchor_ref = group["anchor_ref"]
    if anchor_ref not in fps:
        return [], f"anchor {anchor_ref!r} not in PCB"

    anchor_fp = fps[anchor_ref]
    pm = pending_moves or {}
    if anchor_ref in pm:
        _, ay = pm[anchor_ref]
    else:
        _, ay = fp_xy(anchor_fp)

    row_y = ay + group.get("y_offset_mm", 0.0)

    results = []
    for ref in group["members"]:
        if ref not in fps:
            results.append((ref, None, None, None, f"{ref!r} not in PCB"))
        else:
            cur_x = pm[ref][0] if ref in pm else fp_xy(fps[ref])[0]
            results.append((ref, cur_x, row_y, None, None))

    return results, None


def process_row(group, fps, hs_map, sw_map):
    """
    Resolve a row group — horizontal analog of process_column.
    All members share the same Y = anchor_centroid_Y + y_offset_mm.
    Members are placed left-to-right at X = anchor_centroid_X + x_start_mm,
    then + pitch_mm, etc.  Spacing enforcement runs on the X axis.
    """
    anchor_ref = group["anchor_ref"]
    if anchor_ref not in fps:
        return [], f"anchor {anchor_ref!r} not in PCB"

    anchor_fp = fps[anchor_ref]
    ax, ay  = fp_xy(anchor_fp)
    y_off   = group.get("y_offset_mm", 0.0)
    row_y   = ay + y_off
    x_cur   = ax + group.get("x_start_mm", 0.0)
    pitch   = group["pitch_mm"]
    tgt_rot = group.get("target_rotation")

    member_fps = [fps[r] for r in group["members"] if r in fps]
    skip_corr  = group.get("skip_corridor", False)

    # Enforce minimum corridor clearance between anchor edge and member near-edge.
    side = "above" if y_off < 0.0 else "below"
    if not skip_corr:
        row_y, adj = _enforce_corridor(row_y, anchor_fp, side, member_fps, tgt_rot, hs_map, sw_map)
        if adj > 1e-4:
            print(f"    [corridor] y_offset expanded by +{adj:.3f}mm "
                  f"(required corridor = {_corridor_required(anchor_fp, member_fps, hs_map, sw_map):.2f}mm)")

    raw_results = []
    for ref in group["members"]:
        if ref not in fps:
            raw_results.append((ref, None, None, None, f"{ref!r} not in PCB"))
        else:
            raw_results.append((ref, x_cur, row_y, tgt_rot, None))
        x_cur += pitch

    valid_xy     = [(r, tx, ty) for r, tx, ty, _, err in raw_results if err is None]
    planned_rots = {r: rot for r, _, _, rot, err in raw_results
                    if err is None and rot is not None}
    if len(valid_xy) >= 2:
        adjusted = enforce_min_spacing_axis(valid_xy, fps, "X",
                                            target_rots=planned_rots)
        adj_map  = {r: (tx, ty) for r, tx, ty in adjusted}
    else:
        adj_map = {}

    # ── Intermediate-pad gap enforcement (X axis) ─────────────────────────────
    skip_pad_gap = group.get("skip_pad_gap", False)
    if not skip_pad_gap and len(adj_map) >= 2:
        sorted_refs = sorted(adj_map, key=lambda r: adj_map[r][0])
        for i in range(1, len(sorted_refs)):
            ref_prev = sorted_refs[i - 1]
            ref_curr = sorted_refs[i]
            x_prev, _ = adj_map[ref_prev]
            x_curr, y_curr = adj_map[ref_curr]
            fp_prev = fps.get(ref_prev)
            fp_curr = fps.get(ref_curr)
            hw_prev = fp_half_extents(fp_prev)[0] if fp_prev else 0.5
            hw_curr = fp_half_extents(fp_curr)[0] if fp_curr else 0.5
            current_body_gap = (x_curr - hw_curr) - (x_prev + hw_prev)
            skip_nets = set()
            for mr in (ref_prev, ref_curr):
                mfp = fps.get(mr)
                if mfp:
                    skip_nets |= {p.GetNetname() for p in mfp.Pads() if p.GetNetname()}
            req = _intermediate_pad_gap(
                anchor_fp, "X", x_prev, x_curr, hs_map, sw_map, skip_nets=skip_nets)
            if req > current_body_gap + 1e-4:
                extra = req - current_body_gap
                print(f"    [pad-gap] {ref_prev}↔{ref_curr}: "
                      f"intermediate anchor pad needs {req:.2f}mm body gap "
                      f"(current {current_body_gap:.2f}mm) → shift +{extra:.3f}mm")
                for j in range(i, len(sorted_refs)):
                    rj = sorted_refs[j]
                    xj, yj = adj_map[rj]
                    adj_map[rj] = (xj + extra, yj)

    results = []
    for ref, tx, ty, rot, err in raw_results:
        if err is not None:
            results.append((ref, tx, ty, rot, err))
        elif ref in adj_map:
            atx, aty = adj_map[ref]
            if abs(atx - tx) > 1e-4 or abs(aty - ty) > 1e-4:
                spacing_note = (f"spacing-adjusted: "
                                f"({tx:.3f},{ty:.3f}) → ({atx:.3f},{aty:.3f})")
            else:
                spacing_note = None
            results.append((ref, atx, aty, rot, spacing_note))
        else:
            results.append((ref, tx, ty, rot, None))

    return results, None


# ── Ray-place group processing ───────────────────────────────────────────────

def process_ray_place(group, fps, rule_lookup, pending_moves=None):
    """
    Resolve a ray_place group.

    Scans outward from the anchor's bounding-box edge in the specified direction,
    placing each member at the first position that is:
      (a) clear of all footprints (CLEARANCE_MM gap)
      (b) within the member's proximity rule max_dist_mm from the anchor centroid

    The track-axis coordinate (perpendicular to scan direction) comes from the
    anchor's anchor_pad_net pad.  If omitted, the anchor centroid is used.

    pending_moves: optional {ref: (tx, ty)} of moves already resolved by prior
    groups in the same pass.  When the anchor_ref itself has a pending move, its
    proposed position is used for the frontier calculation, so that a ray_place
    group moves together with a co-moved anchor.

    Members are placed sequentially; each placed member advances the frontier so
    the next member starts just past it.

    Returns (results, error) — results is [(ref, tx, ty, rot, note_or_None)].
    """
    anchor_ref = group["anchor_ref"]
    if anchor_ref not in fps:
        return [], f"anchor {anchor_ref!r} not in PCB"

    anchor_fp  = fps[anchor_ref]
    direction  = group["direction"]
    pad_net    = group.get("anchor_pad_net")
    min_off    = group.get("min_offset_mm", 0.0)
    tgt_rot    = group.get("target_rotation")
    members    = group["members"]
    step_mm    = 0.05  # scan resolution
    pm         = pending_moves or {}

    dir_map = {
        "left":  ("X", -1, "Y"),
        "right": ("X", +1, "Y"),
        "up":    ("Y", -1, "X"),
        "down":  ("Y", +1, "X"),
    }
    if direction not in dir_map:
        return [], f"unknown direction {direction!r}"
    ray_axis, ray_sign, track_axis = dir_map[direction]

    # Use anchor's proposed position if it is being moved by a prior group.
    if anchor_ref in pm:
        acx, acy = pm[anchor_ref]
    else:
        acx, acy = fp_xy(anchor_fp)

    # Track-axis coordinate from anchor pad (or anchor centroid)
    if pad_net:
        pad_pos = find_anchor_pad_world(anchor_fp, pad_net)
        if pad_pos is None:
            return [], f"net {pad_net!r} not found on {anchor_ref}"
        track_coord = pad_pos[0] if track_axis == "X" else pad_pos[1]
    else:
        track_coord = acx if track_axis == "X" else acy

    # Anchor bounding-box edge at effective (possibly proposed) position.
    abbox = fp_bbox_mm(anchor_fp, acx, acy)
    if ray_axis == "X":
        anchor_edge = abbox[0] if ray_sign < 0 else abbox[2]
    else:
        anchor_edge = abbox[1] if ray_sign < 0 else abbox[3]

    # Frontier: furthest occupied coordinate along the ray.
    # Advances as each member is placed.
    frontier = anchor_edge + ray_sign * min_off

    results = []
    for ref in members:
        if ref not in fps:
            results.append((ref, None, None, None, f"{ref!r} not in PCB"))
            continue

        fp  = fps[ref]
        hw  = _member_half(fp, tgt_rot, "X")
        hh  = _member_half(fp, tgt_rot, "Y")
        rh  = hw if ray_axis == "X" else hh  # half-extent along scan direction

        rule     = rule_lookup.get(ref)
        max_dist = rule[1] if rule else float("inf")

        # How much of max_dist is used by the perpendicular (track) offset
        anchor_track = acx if track_axis == "X" else acy
        track_offset = abs(track_coord - anchor_track)
        ray_budget   = math.sqrt(max(max_dist ** 2 - track_offset ** 2, 0.0))

        # Maximum reachable center along ray axis (from anchor centroid)
        anchor_ray = acx if ray_axis == "X" else acy
        max_ray_center = anchor_ray + ray_sign * ray_budget

        # Starting candidate: just past frontier
        start     = frontier + ray_sign * (CLEARANCE_MM + rh)
        max_steps = max(1, int(abs(max_ray_center - start) / step_mm) + 2)

        placed = False
        for i in range(max_steps):
            candidate = start + ray_sign * i * step_mm
            if ray_sign * (candidate - max_ray_center) > 1e-6:
                break

            cx_c = candidate    if ray_axis == "X" else track_coord
            cy_c = track_coord  if ray_axis == "X" else candidate

            if math.hypot(cx_c - acx, cy_c - acy) > max_dist:
                break

            prop_bbox = (cx_c - hw, cy_c - hh, cx_c + hw, cy_c + hh)
            conflict = False
            for other_ref, other_fp in fps.items():
                if other_ref == ref:
                    continue
                ox, oy = fp_xy(other_fp)
                if bboxes_overlap(prop_bbox, fp_bbox_mm(other_fp, ox, oy),
                                  clearance=CLEARANCE_MM):
                    conflict = True
                    break

            if not conflict:
                placed   = True
                frontier = candidate + ray_sign * rh  # advance past placed member
                results.append((ref, cx_c, cy_c, tgt_rot, None))
                break

        if not placed:
            results.append((ref, None, None, None,
                            f"no clear position found within {max_dist:.1f}mm "
                            f"along '{direction}'"))

    return results, None


# ── Push group processing ─────────────────────────────────────────────────────

def process_push(group, fps, hs_map, sw_map, pending_moves=None):
    """
    Rigid-body push: shift all group members as a unit so the cluster's leading
    edge is at least corridor_required away from the anchor's edge in the push
    direction.  Members already clear of the required gap are not moved.

    Preserves relative positions among members — every member shifts by the
    same (dx, dy) delta.  Only translates along the push axis (X for left/right,
    Y for above/below); the perpendicular axis is unchanged.
    """
    anchor_ref = group.get("anchor_ref")
    direction  = group.get("direction", "right")
    if anchor_ref not in fps:
        return [], f"anchor {anchor_ref!r} not in PCB"

    anchor_fp   = fps[anchor_ref]
    pm          = pending_moves or {}
    member_refs = _group_member_refs(group, fps)

    member_fps = [fps[r] for r in member_refs if r in fps and not fps[r].IsLocked()]
    if not member_fps:
        print(f"    [push] no unlocked members")
        return [], None

    required = _corridor_required(anchor_fp, member_fps, hs_map, sw_map)
    ax0, ay0, ax1, ay1 = fp_bbox_mm(anchor_fp)

    # Corridor occupants: components declared to sit physically inside the routing
    # corridor between the anchor and this cluster.  Widen the required gap so the
    # cluster also clears their bodies (with CLEARANCE_MM), not just the trace corridor.
    corridor_occ_refs = group.get("corridor_occupants", [])
    if corridor_occ_refs:
        occ_far_edges = []
        for occ_ref in corridor_occ_refs:
            occ_fp = fps.get(occ_ref)
            if occ_fp is None:
                continue
            ox, oy = pm.get(occ_ref, fp_xy(occ_fp))
            hw_occ, hh_occ = fp_half_extents(occ_fp)
            if direction == "left":
                occ_far_edges.append(ox - hw_occ)   # occupant left edge faces cluster
            elif direction == "right":
                occ_far_edges.append(ox + hw_occ)   # occupant right edge faces cluster
            elif direction == "above":
                occ_far_edges.append(oy - hh_occ)   # occupant top edge faces cluster
            elif direction == "below":
                occ_far_edges.append(oy + hh_occ)   # occupant bottom edge faces cluster
        if occ_far_edges:
            if direction == "left":
                required_occ = ax0 - min(occ_far_edges) + CLEARANCE_MM
            elif direction == "right":
                required_occ = max(occ_far_edges) - ax1 + CLEARANCE_MM
            elif direction == "above":
                required_occ = ay0 - min(occ_far_edges) + CLEARANCE_MM
            elif direction == "below":
                required_occ = max(occ_far_edges) - ay1 + CLEARANCE_MM
            else:
                required_occ = required
            if required_occ > required:
                print(f"    [push] corridor occupants constrain gap: "
                      f"{required:.3f}mm → {required_occ:.3f}mm")
                required = required_occ

    if direction == "left":
        cluster_edge = max(fp_bbox_mm(fp)[2] for fp in member_fps)
        current_gap  = ax0 - cluster_edge
        shift_x = min(0.0, current_gap - required)
        shift_y = 0.0
    elif direction == "right":
        cluster_edge = min(fp_bbox_mm(fp)[0] for fp in member_fps)
        current_gap  = cluster_edge - ax1
        shift_x = max(0.0, required - current_gap)
        shift_y = 0.0
    elif direction == "above":
        cluster_edge = max(fp_bbox_mm(fp)[3] for fp in member_fps)
        current_gap  = ay0 - cluster_edge
        shift_x = 0.0
        shift_y = min(0.0, current_gap - required)
    elif direction == "below":
        cluster_edge = min(fp_bbox_mm(fp)[1] for fp in member_fps)
        current_gap  = cluster_edge - ay1
        shift_x = 0.0
        shift_y = max(0.0, required - current_gap)
    else:
        return [], f"unknown direction {direction!r}"

    if abs(shift_x) < 1e-4 and abs(shift_y) < 1e-4:
        print(f"    [push] cluster already clear "
              f"(gap={current_gap:.3f}mm >= required={required:.2f}mm) — no move")
    else:
        print(f"    [push] gap={current_gap:.3f}mm < required={required:.2f}mm "
              f"→ rigid shift Δ({shift_x:+.3f},{shift_y:+.3f})mm")

    results = []
    for ref in member_refs:
        if ref not in fps:
            results.append((ref, None, None, None, f"{ref!r} not in PCB"))
            continue
        fp = fps[ref]
        cx, cy = pm.get(ref, fp_xy(fp))
        results.append((ref, cx + shift_x, cy + shift_y, None, None))
    return results, None


# ── General rotation fix ──────────────────────────────────────────────────────

def general_rotation_pass(fps, skip_refs, rot_targets):
    """
    For every unlocked component in ROTATION_SYMMETRY that is NOT already
    handled by an alignment group: if the current rotation is non-canonical,
    determine the best canonical angle by computing which option places the
    component's primary signal pad (from its proximity rule net_hint) closest
    to its anchor component.  Falls back to nearest_canonical if no rule exists.

    Returns list of (ref, cur_rot, tgt_rot, reason).
    """
    proposals = []
    for ref, sym in cfg.ROTATION_SYMMETRY.items():
        if ref in skip_refs:
            continue
        if ref not in fps:
            continue
        fp = fps[ref]
        if fp.IsLocked():
            continue
        rot = fp.GetOrientationDegrees()
        if is_canonical(rot, sym):
            continue

        candidates = CANONICAL.get(sym, [])
        if not candidates:
            continue

        best_rot = None
        if ref in rot_targets:
            anchor_ref, net_hint = rot_targets[ref]
            signal_pad = find_pad_by_net(fp, net_hint)
            if signal_pad is not None and anchor_ref in fps:
                anchor_cx, anchor_cy = fp_xy(fps[anchor_ref])
                best_dist = float("inf")
                for c in candidates:
                    wx, wy = pad_world_at_rot(fp, signal_pad, c)
                    d = math.hypot(wx - anchor_cx, wy - anchor_cy)
                    if d < best_dist:
                        best_dist = d
                        best_rot = c

        if best_rot is None:
            best_rot = nearest_canonical(rot, sym)
            reason = f"non-canonical rot={rot:.1f}° → nearest canonical (no rule found)"
        else:
            anchor_ref, net_hint = rot_targets[ref]
            reason = (f"non-canonical rot={rot:.1f}°; {net_hint!r} pad faces "
                      f"{anchor_ref} at {best_rot:.1f}°")

        proposals.append((ref, rot, best_rot, reason))
    return proposals


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pre-route alignment: position and rotation cleanup before routing."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply all proposed changes and save PCB. Default: dry-run only."
    )
    args = parser.parse_args()

    board = pcbnew.LoadBoard(cfg.PCB_FILE)
    fps   = {fp.GetReference(): fp for fp in board.GetFootprints()}

    print("=" * 72)
    print("ROUTE PREP ALIGN —", "APPLY MODE" if args.apply else "DRY RUN (no changes)")
    print(f"PCB: {cfg.PCB_FILE}")
    print("=" * 72)

    all_moves_raw = []   # (ref, cx, cy, tx, ty, source_label) — before pre-apply check
    all_rotates   = []   # (ref, cur_rot, tgt_rot, reason)
    group_refs    = set()
    spacing_notes = []   # (ref, note) — log of spacing adjustments made in Check A

    rule_lookup = build_rule_lookup()
    hs_map      = build_hs_map()
    sw_map      = build_sw_map()

    # ── Pass 1: alignment groups ──────────────────────────────────────────────
    print("\n── Pass 1: Alignment groups ─────────────────────────────────────────────")

    for group in cfg.ALIGNMENT_GROUPS:
        gname = group["name"]
        gtype = group["type"]
        print(f"\n  [{gtype}] {gname}")

        # Pending moves resolved so far — passed to ray_place so its anchor
        # can use a proposed position if the anchor is being moved by a prior group.
        pending_so_far = {ref: (tx, ty)
                          for ref, cx, cy, tx, ty, src in all_moves_raw}

        if gtype == "pad_track":
            results, err = process_pad_track(group, fps)
        elif gtype == "column":
            results, err = process_column(group, fps, hs_map, sw_map,
                                          pending_moves=pending_so_far)
        elif gtype == "row":
            results, err = process_row(group, fps, hs_map, sw_map)
        elif gtype == "lift":
            results, err = process_lift(group, fps, pending_moves=pending_so_far)
        elif gtype == "ray_place":
            results, err = process_ray_place(group, fps, rule_lookup,
                                             pending_moves=pending_so_far)
        elif gtype == "push":
            results, err = process_push(group, fps, hs_map, sw_map,
                                        pending_moves=pending_so_far)
        else:
            print(f"  ERROR: unknown group type {gtype!r}")
            continue

        if err:
            print(f"  ERROR: {err}")
            continue

        tgt_rot_group = group.get("target_rotation")

        for ref, tx, ty, tgt_rot, note in results:
            # 'note' is either None (all good), a spacing-adjustment string, or an error string.
            is_error   = note is not None and not note.startswith("spacing-adjusted")
            is_spacing = note is not None and note.startswith("spacing-adjusted")

            if is_error:
                print(f"    {ref:<22} SKIP: {note}")
                continue

            if ref not in fps:
                print(f"    {ref:<22} SKIP: not in PCB")
                continue

            fp = fps[ref]
            if fp.IsLocked():
                print(f"    {ref:<22} LOCKED — skip")
                group_refs.add(ref)
                continue

            group_refs.add(ref)
            cx, cy   = fp_xy(fp)
            cur_rot  = fp.GetOrientationDegrees()
            sym      = cfg.ROTATION_SYMMETRY.get(ref, "none")

            move_x = abs(tx - cx) > 0.001
            move_y = abs(ty - cy) > 0.001
            needs_move = move_x or move_y

            needs_rot = False
            if tgt_rot is not None and sym != "none":
                needs_rot = not (
                    min(abs(norm360(cur_rot) - tgt_rot),
                        360.0 - abs(norm360(cur_rot) - tgt_rot)) < 0.5
                )

            parts = []
            if is_spacing:
                parts.append(f"[CHECK-A] {note}")
            if needs_move:
                parts.append(
                    f"pos ({cx:.3f},{cy:.3f}) → ({tx:.3f},{ty:.3f})"
                    f"  Δ({tx-cx:+.3f},{ty-cy:+.3f})"
                )
                spacing_notes.append((ref, note)) if is_spacing else None
            else:
                parts.append(f"pos OK  ({cx:.3f},{cy:.3f})")

            if needs_rot:
                parts.append(f"rot {cur_rot:.1f}° → {tgt_rot:.1f}°")
            elif tgt_rot is not None and sym != "none":
                parts.append(f"rot OK  ({cur_rot:.1f}°)")

            print(f"    {ref:<22} " + "   ".join(parts))

            if needs_move:
                all_moves_raw.append((ref, cx, cy, tx, ty, gname))
            if needs_rot:
                all_rotates.append((ref, cur_rot, tgt_rot, f"group: {gname}"))

    # ── Pass 2: general rotation fix ──────────────────────────────────────────
    print("\n── Pass 2: General rotation fix (non-canonical angles) ──────────────────")

    rot_targets = build_rotation_targets()
    rot_fixes   = general_rotation_pass(fps, group_refs, rot_targets)

    if not rot_fixes:
        print("  All non-group components are at canonical rotations.")
    else:
        for ref, cur_rot, tgt_rot, reason in rot_fixes:
            print(f"  {ref:<22} rot {cur_rot:.1f}° → {tgt_rot:.1f}°   ({reason})")
            all_rotates.append((ref, cur_rot, tgt_rot, reason))

    # ── Blocker-clearing pass ─────────────────────────────────────────────────
    # For each non-locked, non-group component blocking a group target, find the
    # minimum cardinal displacement that clears the conflict while satisfying the
    # component's own proximity rule.  Cleared blockers are prepended to the move
    # list so Check B evaluates group members with the blocker already repositioned.
    print("\n── Blocker-clearing pass ────────────────────────────────────────────────")

    # Build target-rotation map for all pending moves so blocker detection and
    # Check B evaluate components at their post-rotation size, not pre-rotation.
    pending_rotations = {ref: tgt_rot
                         for ref, cur_rot, tgt_rot, reason in all_rotates}

    all_proposed = {ref: (tx, ty) for ref, cx, cy, tx, ty, source in all_moves_raw}

    # Collect blockers: map blocker_ref → [(blocked_group_ref, proposed_target_bbox)]
    blocker_map = {}
    for ref, cx, cy, tx, ty, source in all_moves_raw:
        fp = fps.get(ref)
        if fp is None:
            continue
        if ref in pending_rotations:
            _hw = fp_half_at_target_rot(fp, pending_rotations[ref], 'X')
            _hh = fp_half_at_target_rot(fp, pending_rotations[ref], 'Y')
            prop_bbox = (tx - _hw, ty - _hh, tx + _hw, ty + _hh)
        else:
            prop_bbox = fp_bbox_mm(fp, tx, ty)
        for other_ref, other_fp in fps.items():
            if other_ref == ref:
                continue
            if other_ref in all_proposed:
                ox, oy = all_proposed[other_ref]
            else:
                ox, oy = fp_xy(other_fp)
            other_bbox = fp_bbox_mm(other_fp, ox, oy)
            if bboxes_overlap(prop_bbox, other_bbox, clearance=CLEARANCE_MM):
                # Never move a group member as a blocker — its position is
                # intentional (either a pending move already in all_proposed,
                # or already at its group target / pos-OK).  Only non-group
                # external components are valid blocker candidates.
                if other_ref in group_refs:
                    continue
                if other_ref not in all_proposed:  # skip co-moved group members
                    if other_ref not in blocker_map:
                        blocker_map[other_ref] = []
                    if not any(r == ref for r, _ in blocker_map[other_ref]):
                        blocker_map[other_ref].append((ref, prop_bbox))

    blocker_moves = []
    if not blocker_map:
        print("  No blockers found — all group targets are clear.")
    else:
        for blocker_ref, blocked_list in blocker_map.items():
            blocked_bboxes = [bbox for _, bbox in blocked_list]
            blocked_refs   = [r   for r, _  in blocked_list]
            bcx, bcy = fp_xy(fps[blocker_ref])
            result = try_clear_blocker(
                blocker_ref, blocked_bboxes, fps, rule_lookup, all_proposed)
            if result is None:
                print(f"  [blocker-clear FAIL] {blocker_ref:<22} cannot clear "
                      f"{', '.join(blocked_refs)} without violating proximity "
                      f"rule — blocked move(s) will be suppressed by Check B")
            else:
                dx, dy = result
                btx, bty = bcx + dx, bcy + dy
                blocker_moves.append(
                    (blocker_ref, bcx, bcy, btx, bty, "blocker-clear"))
                print(f"  [blocker-clear] {blocker_ref:<22} "
                      f"({bcx:.3f},{bcy:.3f}) → ({btx:.3f},{bty:.3f})  "
                      f"Δ({dx:+.3f},{dy:+.3f})  "
                      f"clears: {', '.join(blocked_refs)}")

    # Prepend blocker moves so Check B evaluates group members with blockers
    # already at their cleared positions.
    all_moves_raw = blocker_moves + all_moves_raw

    # ── Group conflict resolution pass ───────────────────────────────────────
    # When two co-proposed components conflict and one belongs to a group whose
    # anchor is also in all_proposed (i.e. it can be pushed further), shift that
    # component and its entire anchor chain by the minimum X delta needed to
    # clear the conflict.  Iterates until stable (max 10 rounds).
    print("\n── Group conflict resolution ─────────────────────────────────────────────")

    member_to_group = {}
    for grp in cfg.ALIGNMENT_GROUPS:
        for m_ref in _group_member_refs(grp, fps):
            member_to_group[m_ref] = grp

    all_proposed_gcr = {ref: (tx, ty) for ref, cx, cy, tx, ty, src in all_moves_raw}
    gcr_messages = []

    for _gcr_iter in range(10):
        gcr_found = False
        checked_pairs = set()

        for ref_a in list(all_proposed_gcr):
            fp_a = fps.get(ref_a)
            if fp_a is None:
                continue
            ax, ay = all_proposed_gcr[ref_a]
            bbox_a = (fp_bbox_mm(fp_a, ax, ay) if ref_a not in pending_rotations
                      else (ax - fp_half_at_target_rot(fp_a, pending_rotations[ref_a], 'X'),
                            ay - fp_half_at_target_rot(fp_a, pending_rotations[ref_a], 'Y'),
                            ax + fp_half_at_target_rot(fp_a, pending_rotations[ref_a], 'X'),
                            ay + fp_half_at_target_rot(fp_a, pending_rotations[ref_a], 'Y')))

            for ref_b in list(all_proposed_gcr):
                if ref_b == ref_a:
                    continue
                pair = tuple(sorted((ref_a, ref_b)))
                if pair in checked_pairs:
                    continue
                checked_pairs.add(pair)

                fp_b = fps.get(ref_b)
                if fp_b is None:
                    continue
                bx, by = all_proposed_gcr[ref_b]
                bbox_b = (fp_bbox_mm(fp_b, bx, by) if ref_b not in pending_rotations
                          else (bx - fp_half_at_target_rot(fp_b, pending_rotations[ref_b], 'X'),
                                by - fp_half_at_target_rot(fp_b, pending_rotations[ref_b], 'Y'),
                                bx + fp_half_at_target_rot(fp_b, pending_rotations[ref_b], 'X'),
                                by + fp_half_at_target_rot(fp_b, pending_rotations[ref_b], 'Y')))

                if not bboxes_overlap(bbox_a, bbox_b, clearance=CLEARANCE_MM):
                    continue

                # Intra-group pairs: spacing was already resolved by Check A (or
                # intentionally bypassed via skip_check_a) — GCR must not re-introduce
                # a conflict that the group processor deliberately accepted.
                grp_a = member_to_group.get(ref_a)
                grp_b = member_to_group.get(ref_b)
                if grp_a is not None and grp_a is grp_b:
                    continue

                # Pre-existing check: if the two components already overlapped at
                # their original PCB positions, this is not a conflict we introduced
                # — skip it so we don't oscillate trying to resolve a pre-existing gap.
                orig_bbox_a = fp_bbox_mm(fp_a)
                orig_bbox_b = fp_bbox_mm(fp_b)
                if bboxes_overlap(orig_bbox_a, orig_bbox_b, clearance=CLEARANCE_MM):
                    continue

                # Conflict — try each side as the pusher.  The pusher is the one
                # whose group anchor is also in all_proposed_gcr (can move further),
                # OR both groups share the same fixed anchor (e.g. two columns on
                # the same locked IC) — in which case push the outer group (farther
                # from the shared anchor) outward.
                pushed = False
                for (pusher_ref, pusher_bbox, pusher_cx, pusher_cy,
                     fixed_ref,  fixed_bbox,  fixed_cx,  fixed_cy) in [
                    (ref_b, bbox_b, bx, by, ref_a, bbox_a, ax, ay),
                    (ref_a, bbox_a, ax, ay, ref_b, bbox_b, bx, by),
                ]:
                    grp = member_to_group.get(pusher_ref)
                    if grp is None:
                        continue
                    anchor_ref_grp = grp.get("anchor_ref")
                    anchor_in_proposed = anchor_ref_grp in all_proposed_gcr
                    # Also allow pushing when both groups share the same fixed anchor.
                    fixed_grp = member_to_group.get(fixed_ref)
                    same_fixed_anchor = (
                        not anchor_in_proposed and
                        fixed_grp is not None and
                        fixed_grp.get("anchor_ref") == anchor_ref_grp
                    )
                    if not anchor_in_proposed and not same_fixed_anchor:
                        continue
                    if same_fixed_anchor:
                        # Only push the outer group (farther from the shared anchor)
                        # so we never push a column back toward its own anchor.
                        anchor_fp = fps.get(anchor_ref_grp)
                        if anchor_fp is not None:
                            anc_x, anc_y = fp_xy(anchor_fp)
                            if math.hypot(pusher_cx - anc_x, pusher_cy - anc_y) < \
                               math.hypot(fixed_cx  - anc_x, fixed_cy  - anc_y):
                                continue  # inner group — let other orientation handle it

                    px0, py0, px1, py1 = pusher_bbox
                    fx0, fy0, fx1, fy1 = fixed_bbox
                    if pusher_cx <= fixed_cx:
                        delta_x = fx0 - CLEARANCE_MM - SPACING_EPSILON - px1
                    else:
                        delta_x = fx1 + CLEARANCE_MM + SPACING_EPSILON - px0

                    if abs(delta_x) < 1e-6:
                        continue

                    # Apply delta to all members of pusher's group — including
                    # members not yet in all_proposed_gcr (those that were already
                    # at their column/row target and had no proposed move), so the
                    # whole group shifts together and stays aligned.
                    for m_ref in _group_member_refs(grp, fps):
                        if m_ref in all_proposed_gcr:
                            mx, my = all_proposed_gcr[m_ref]
                            all_proposed_gcr[m_ref] = (mx + delta_x, my)
                        else:
                            mfp = fps.get(m_ref)
                            if mfp is not None and not mfp.IsLocked():
                                mx, my = fp_xy(mfp)
                                all_proposed_gcr[m_ref] = (mx + delta_x, my)

                    # Cascade delta up the anchor chain (only when the anchor
                    # is also being moved — skip for same-fixed-anchor cases
                    # where the anchor is a locked IC not in all_proposed_gcr).
                    if anchor_in_proposed:
                        visited_cascade = set()
                        cascade_queue = [anchor_ref_grp]
                        while cascade_queue:
                            curr = cascade_queue.pop()
                            if curr in visited_cascade or curr not in all_proposed_gcr:
                                continue
                            visited_cascade.add(curr)
                            cx_, cy_ = all_proposed_gcr[curr]
                            all_proposed_gcr[curr] = (cx_ + delta_x, cy_)
                            curr_grp = member_to_group.get(curr)
                            if curr_grp:
                                curr_anchor = curr_grp.get("anchor_ref")
                                if curr_anchor:
                                    cascade_queue.append(curr_anchor)

                    gcr_messages.append(
                        f"{pusher_ref} shifted {delta_x:+.3f}mm in X to clear {fixed_ref}")
                    gcr_found = True
                    pushed = True
                    break

                if pushed:
                    break
            if gcr_found:
                break
        if not gcr_found:
            break

    if gcr_messages:
        for msg in gcr_messages:
            print(f"  [group-resolve] {msg}")
    else:
        print("  No group-member conflicts to resolve.")

    # Propagate resolved positions back into all_moves_raw.
    all_moves_raw = [(ref, cx, cy,
                      all_proposed_gcr.get(ref, (tx, ty))[0],
                      all_proposed_gcr.get(ref, (tx, ty))[1],
                      src)
                     for ref, cx, cy, tx, ty, src in all_moves_raw]

    # Also promote any GCR-created entries that weren't already in all_moves_raw.
    # This happens when GCR pushes a group member that was pos-OK (not proposed
    # to move) but now needs to shift to keep the group aligned.
    gcr_in_moves = {ref for ref, *_ in all_moves_raw}
    for ref, (gcr_tx, gcr_ty) in all_proposed_gcr.items():
        if ref in gcr_in_moves:
            continue
        fp = fps.get(ref)
        if fp is None:
            continue
        cx, cy = fp_xy(fp)
        if abs(gcr_tx - cx) > 0.001 or abs(gcr_ty - cy) > 0.001:
            all_moves_raw.append((ref, cx, cy, gcr_tx, gcr_ty, "group-resolve"))

    # ── Check B: cross-component pre-apply overlap (position moves) ──────────
    print("\n── Check B: Cross-component pre-apply overlap ───────────────────────────")

    if all_moves_raw:
        safe_moves, skipped_moves = check_pre_apply_overlaps(all_moves_raw, fps, pending_rotations)
        if skipped_moves:
            print(f"  WARNING: {len(skipped_moves)} move(s) skipped to avoid overlap:")
            for ref, cx, cy, tx, ty, source, reason in skipped_moves:
                print(f"    {ref:<22} SKIPPED — {reason}")
        else:
            print(f"  All {len(safe_moves)} proposed move(s) are clear of other components.")
        all_moves = safe_moves
    else:
        all_moves = []
        print("  No moves proposed — nothing to check.")

    # ── Check B-rot: rotation overlap check ───────────────────────────────────
    print("\n── Check B-rot: Rotation overlap check ──────────────────────────────────")

    # Pass accepted move destinations so rotations are evaluated at final positions
    proposed_positions = {ref: (tx, ty) for ref, cx, cy, tx, ty, source in all_moves}

    if all_rotates:
        safe_rotates, skipped_rotates = check_rotation_overlaps(
            all_rotates, fps, proposed_positions, pending_rotations)
        if skipped_rotates:
            print(f"  WARNING: {len(skipped_rotates)} rotation(s) skipped to avoid overlap:")
            for ref, cur_rot, tgt_rot, reason, conflict in skipped_rotates:
                print(f"    {ref:<22} SKIPPED {cur_rot:.1f}°→{tgt_rot:.1f}° — {conflict}")
        else:
            print(f"  All {len(safe_rotates)} proposed rotation(s) are clear.")
        all_rotates = safe_rotates
    else:
        print("  No rotations proposed — nothing to check.")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 72)
    print(f"SUMMARY: {len(all_moves)} position change(s),  "
          f"{len(all_rotates)} rotation change(s)")
    if all_moves_raw and len(all_moves) < len(all_moves_raw):
        print(f"         {len(all_moves_raw) - len(all_moves)} move(s) suppressed by "
              f"Check B (position overlap)")
    print("=" * 72)

    if not all_moves and not all_rotates:
        print("Nothing to do — PCB is already aligned.")
        return

    if not args.apply:
        print("\nDRY RUN complete — no changes written.")
        print("Re-run with --apply to execute.")
        return

    # ── Apply ─────────────────────────────────────────────────────────────────
    print("\nApplying changes ...")

    # Snapshot all bboxes before mutating footprint positions so Check C can
    # distinguish overlaps we introduced from ones that pre-existed in the PCB.
    orig_bboxes = {ref: fp_bbox_mm(fp) for ref, fp in fps.items()}

    changed_refs = set()

    for ref, cx, cy, tx, ty, source in all_moves:
        fps[ref].SetPosition(
            pcbnew.VECTOR2I(pcbnew.FromMM(tx), pcbnew.FromMM(ty))
        )
        print(f"  MOVED    {ref:<22} ({cx:.3f},{cy:.3f}) → ({tx:.3f},{ty:.3f})")
        changed_refs.add(ref)

    for ref, cur_rot, tgt_rot, reason in all_rotates:
        fps[ref].SetOrientationDegrees(tgt_rot)
        print(f"  ROTATED  {ref:<22} {cur_rot:.1f}° → {tgt_rot:.1f}°")
        changed_refs.add(ref)

    board.Save(cfg.PCB_FILE)
    print(f"\nSaved: {cfg.PCB_FILE}")
    print(f"Total applied: {len(all_moves)} move(s), {len(all_rotates)} rotation(s).")

    # ── Check C: post-apply overlap verification ──────────────────────────────
    print("\n── Check C: Post-apply overlap verification ─────────────────────────────")

    board2   = pcbnew.LoadBoard(cfg.PCB_FILE)
    fps_live = {fp.GetReference(): fp for fp in board2.GetFootprints()}
    issues   = check_post_apply_overlaps(changed_refs, fps_live, orig_bboxes=orig_bboxes)

    if issues:
        print(f"  *** {len(issues)} OVERLAP(S) DETECTED after apply ***")
        for ref_a, ref_b, ov_x, ov_y in issues:
            print(f"    {ref_a} ↔ {ref_b}   overlap X={ov_x:.3f}mm  Y={ov_y:.3f}mm")
        print("  Review and revert if necessary before continuing.")
    else:
        print(f"  PASS — no overlaps detected among {len(changed_refs)} changed component(s).")


if __name__ == "__main__":
    main()
