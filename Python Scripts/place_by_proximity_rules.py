"""
place_by_proximity_rules.py — Phase 8 placement (v2)
Sequential greedy placement driven by PROXIMITY_RULES_TABLE. Assigns satellites to face
groups, ranks by priority, places each group atomically in IC pin order with rotation selection.
Components already satisfying ALL their proximity rules are frozen in place and excluded from
movement. Only groups with at least one violating member are replanned with uniform spacing.
"""

import sys, math, os, pathlib, argparse, datetime, shutil, collections, json

KICAD_BIN = "C:/Program Files/KiCad/10.0/bin"
os.environ["PATH"] = KICAD_BIN + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, KICAD_BIN + "/Lib/site-packages")
import pcbnew

# ── CONFIG ────────────────────────────────────────────────────────────────────
PCB_FILE    = r"E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb"
REPORT_FILE = r"E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Reports\placement_proximity_report.txt"
BACKUPS_DIR = r"E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Backups"
GEOM_DB_FILE = r"E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\component_geometry.json"

FACE_GAP_MM          = 0.25
COURTYARD_GAP_MM     = 0.15
OVERLAP_ITERS        = 8
ANCHOR_OFFSET_FACTOR = 0.7

_DBG_REFS = set()

# Refs whose rotation must not be changed by the intra-group overlap resolver or
# face fallback. Derived from the geometry DB at load time: any component with
# rotation_symmetry="none" has a unique preferred orientation that the resolver
# must not override. Components with symmetry="90" or "180" are freely rotatable.
NO_ROTATE_IN_OVERLAP: set = set()  # populated in main() after geom_db is loaded

# ── Shared placement config ───────────────────────────────────────────────────
# All project-specific placement tables live in proximity_rules_config.py so
# both scripts share a single source of truth.
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from proximity_rules_config import (
    POWER_NET_EXACT, is_power_net, RULE_TYPE_PRIORITY, RULES,
    ANCHOR_OFFSET_OVERRIDES, NO_NORMALIZE_REFS,
    NO_FORCE_PREFIXES, NO_FORCE_REFS,
)

# Refs that appear as ref_a (satellites) but only with net_hint=None rules.
# build_face_groups skips net_hint=None rules, so no face group will ever place these.
# They behave like movable anchors: repositioned by place_root_anchors and pinned during
# resolve_overlaps so their own satellite groups can place reliably around them.
_sat_refs           = {rule[0] for rule in RULES}
_sat_refs_with_net  = {rule[0] for rule in RULES if rule[5] is not None}
NO_FACE_GROUP_REFS  = _sat_refs - _sat_refs_with_net

# Auto-exclusion from normalization: any ref that is itself an anchor (appears as ref_b)
# is excluded from the column/row snap so it sits at its natural face_comp rather than
# being forced to the same column as the small passives it shares a face group with.
NO_NORMALIZE_AUTO = {rule[1] for rule in RULES}

_RULE_PAIRS = {frozenset((rule[0], rule[1])) for rule in RULES}

# Adjacent faces to try (in priority order) when a satellite can't fit on its primary face.
ADJACENT_FACES = {
    'left':  ['down', 'up', 'right'],
    'right': ['up',   'down', 'left'],
    'up':    ['left', 'right', 'down'],
    'down':  ['right', 'left', 'up'],
}

# ── GEOMETRY HELPERS ──────────────────────────────────────────────────────────

def rot2d(x, y, deg):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return x*c - y*s, x*s + y*c

def norm2d(x, y):
    m = math.sqrt(x*x + y*y)
    return (x/m, y/m) if m > 1e-9 else (0.0, 0.0)

def perp2d(x, y):
    return -y, x

def quantize_face(fx, fy):
    if abs(fx) >= abs(fy):
        return 'right' if fx >= 0 else 'left'
    return 'down' if fy >= 0 else 'up'

CARDINAL = {'left': (-1,0), 'right': (1,0), 'up': (0,-1), 'down': (0,1)}

def cardinal_dir(face_side):
    return CARDINAL[face_side]


# ── GEOMETRY DATABASE ─────────────────────────────────────────────────────────

def load_geometry_db(path):
    try:
        with open(path) as f:
            db = json.load(f)
        print(f"  Geometry DB loaded: {len(db)} components from {path}")
        return db
    except FileNotFoundError:
        print(f"  WARNING: geometry DB not found at {path} — using pad inference fallback")
        return {}




def _perp_face_for_net(anchor_ref, net_hint, geom_db, fp_rotations, occupied_axis):
    """Return the best face on the axis perpendicular to occupied_axis.

    Used when a satellite's computed face would land on the same axis as its
    anchor's own face relative to the anchor's parent.  Stacking along the same
    axis risks chaining components off the board edge.

    occupied_axis: frozenset({'up','down'}) or frozenset({'left','right'})
    Returns (face_side, face_dir) constrained to the perpendicular axis, or None.
    """
    if not net_hint or anchor_ref not in geom_db:
        return None
    entry = geom_db[anchor_ref]
    anchor_rot = fp_rotations.get(anchor_ref, 0.0)
    pads_on_net = [p for p in entry.get("pads", []) if p.get("net") == net_hint]
    if not pads_on_net:
        return None
    wx_sum = wy_sum = 0.0
    for pad in pads_on_net:
        lx, ly = pad["local_xy"]
        wx, wy = rot2d(lx, ly, anchor_rot)
        wx_sum += wx
        wy_sum += wy
    n = len(pads_on_net)
    wx_avg, wy_avg = wx_sum / n, wy_sum / n
    if occupied_axis == frozenset({'up', 'down'}):
        face_side = 'right' if wx_avg >= 0 else 'left'
    else:
        face_side = 'down' if wy_avg >= 0 else 'up'
    return face_side, cardinal_dir(face_side)


def db_face_for_net(anchor_ref, net_hint, geom_db, fp_rotations):
    """Return (face_side, cardinal_face_dir) for anchor/net using geometry DB.

    Rotates each pad's LOCAL xy offset to world frame first, then quantizes.
    This correctly handles anchors at non-0° rotations where diagonal pads
    (|local_x| == |local_y|) would snap to the wrong face if quantized before
    rotating (e.g. USB-C connectors at -90° with ±45° pad rows).
    Returns None if anchor or net not in DB.
    """
    if not net_hint or anchor_ref not in geom_db:
        return None
    entry = geom_db[anchor_ref]
    anchor_rot = fp_rotations.get(anchor_ref, 0.0)

    # Vote on world face using each pad's world-space offset.
    # Skip pads at local (0,0) — the anchor footprint origin — because they have no
    # directional information and would win by quantize_face tie-breaking alone.
    pads_on_net = [p for p in entry.get("pads", []) if p.get("net") == net_hint]
    if pads_on_net:
        votes = {'left': 0, 'right': 0, 'up': 0, 'down': 0}
        for pad in pads_on_net:
            lx, ly = pad["local_xy"]
            if abs(lx) < 1e-6 and abs(ly) < 1e-6:
                continue   # pad at footprint origin — no directional information
            wx, wy = rot2d(lx, ly, anchor_rot)
            votes[quantize_face(wx, wy)] += 1
        if sum(votes.values()) > 0:
            world_face = max(votes, key=votes.get)
            return world_face, cardinal_dir(world_face)

    # Fall back to stored net_dominant_face (rotated to world).
    # net_dominant_face is pre-computed at 0° from pad layout; apply current rotation.
    dominant = entry.get("net_dominant_face", {}).get(net_hint)
    if not dominant:
        return None
    lx, ly = cardinal_dir(dominant)
    wx, wy = rot2d(lx, ly, anchor_rot)
    world_face = quantize_face(wx, wy)
    return world_face, cardinal_dir(world_face)


def db_rotation_for_face(ref, net_hint, face_side, geom_db):
    """Pick satellite rotation by aligning its shared-net pad toward the anchor face.

    For sym=none (diodes, asymmetric ICs): always score all 4 rotations so the
    shared-net pad faces the anchor. preferred_rotation from the DB reflects the
    designer's reference backup — it's correct for a clean board but is stale
    after scatter and would override the optimal face-alignment rotation.

    For sym=180: net-type heuristic (power→180°, signal→0°) is sufficient;
    both orientations are electrically equivalent and the heuristic picks the
    conventional one without needing pad-level scoring.

    For sym=90: all orientations are equivalent; return 0.
    """
    if ref not in geom_db:
        return None

    entry = geom_db[ref]
    sym = entry.get("rotation_symmetry", "none")

    if sym == "90":
        return 0
    if sym == "180":
        return 180 if is_power_net(net_hint) else 0

    # sym=none: score all 4 candidates — place shared-net pad closest to anchor face.
    fd = cardinal_dir(face_side)
    toward = (-fd[0], -fd[1])          # direction from satellite toward anchor
    pads_on_net = [p for p in entry.get("pads", []) if p["net"] == net_hint] if net_hint else []
    if not pads_on_net:
        # No net-specific pads to align: fall back to DB preferred rotation.
        return entry.get("preferred_rotation", 0)
    best_rot, best_score = 0, -999.0
    for rot in [0, 90, 180, 270]:
        for pad in pads_on_net:
            wx, wy = rot2d(pad["local_xy"][0], pad["local_xy"][1], rot)
            nx, ny = norm2d(wx, wy)
            score = nx * toward[0] + ny * toward[1]
            if score > best_score + 1e-6:
                best_score, best_rot = score, rot
    return best_rot


def _bbox_corners(b):
    """Return the four corners of a local_bbox dict as a list of (x, y) tuples."""
    return [(b["x0"], b["y0"]), (b["x1"], b["y0"]),
            (b["x1"], b["y1"]), (b["x0"], b["y1"])]


def db_aabb(ref, positions, fp_rotations, geom_db):
    """Courtyard-accurate AABB from geometry DB. Falls back to 1mm pad margin."""
    cx, cy = positions[ref]
    rot = fp_rotations.get(ref, 0.0)

    if ref in geom_db and geom_db[ref].get("courtyard"):
        b = geom_db[ref]["courtyard"]["local_bbox"]
        corners = _bbox_corners(b)
        wxs, wys = [], []
        for lx, ly in corners:
            wx, wy = rot2d(lx, ly, rot)
            wxs.append(cx + wx); wys.append(cy + wy)
        return min(wxs), min(wys), max(wxs), max(wys)

    return cx - 1.0, cy - 1.0, cx + 1.0, cy + 1.0  # fallback


def db_polygon_world(ref, positions, fp_rotations, geom_db):
    """World-space courtyard polygon. Uses actual polygon vertices when available;
    falls back to AABB rectangle corners, then to a 1mm pad margin square."""
    cx, cy = positions[ref]
    rot = fp_rotations.get(ref, 0.0)

    if ref in geom_db and geom_db[ref].get("courtyard"):
        cyd = geom_db[ref]["courtyard"]
        local_verts = cyd.get("polygon") or _bbox_corners(cyd["local_bbox"])
        return [(cx + rot2d(lx, ly, rot)[0], cy + rot2d(lx, ly, rot)[1])
                for lx, ly in local_verts]

    return [(cx-1.0, cy-1.0), (cx+1.0, cy-1.0), (cx+1.0, cy+1.0), (cx-1.0, cy+1.0)]


def _seg_intersect(ax, ay, bx, by, cx, cy, dx, dy):
    """True if segment AB strictly intersects segment CD (excludes shared endpoints)."""
    def cross(ox, oy, px, py, qx, qy):
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)
    d1 = cross(cx, cy, dx, dy, ax, ay)
    d2 = cross(cx, cy, dx, dy, bx, by)
    d3 = cross(ax, ay, bx, by, cx, cy)
    d4 = cross(ax, ay, bx, by, dx, dy)
    return ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
           ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0))


def _point_in_poly(px, py, poly):
    """Ray-casting point-in-polygon test."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi + 1e-20) + xi):
            inside = not inside
        j = i
    return inside


def _polys_strictly_overlap(poly_a, poly_b):
    """True if two simple (possibly non-convex) polygons physically overlap.
    Checks edge-edge intersections and vertex containment — correct for non-convex shapes."""
    if not poly_a or not poly_b:
        return False
    n_a, n_b = len(poly_a), len(poly_b)
    for i in range(n_a):
        ax, ay = poly_a[i]; bx, by = poly_a[(i + 1) % n_a]
        for j in range(n_b):
            cx, cy = poly_b[j]; dx, dy = poly_b[(j + 1) % n_b]
            if _seg_intersect(ax, ay, bx, by, cx, cy, dx, dy):
                return True
    if _point_in_poly(poly_a[0][0], poly_a[0][1], poly_b):
        return True
    if _point_in_poly(poly_b[0][0], poly_b[0][1], poly_a):
        return True
    return False


def sat_overlap_check(poly_a, poly_b, gap=0.0):
    """True if poly_a and poly_b overlap or are within gap of each other.
    Handles non-convex polygons correctly via edge intersection + point-in-polygon."""
    if _polys_strictly_overlap(poly_a, poly_b):
        return True
    if gap > 0.0:
        return polygon_min_distance(poly_a, poly_b) < gap
    return False


def rotation_ok(current_rot, optimal_rot, symmetry):
    """True if current_rot is equivalent to optimal_rot given the component's rotation symmetry.

    4fold: component is identical every 90° → any rotation that is a multiple of 90° away is fine.
    180:   component is identical every 180° → 0°/180° and 90°/270° are equivalent pairs.
    none:  require an exact match (within 1°).
    """
    diff = (current_rot - optimal_rot) % 360
    if symmetry == '4fold':
        rem = diff % 90
        return rem < 1.0 or rem > 89.0
    if symmetry == '180':
        rem = diff % 180
        return rem < 1.0 or rem > 179.0
    return diff < 1.0 or diff > 359.0


def _pt_to_seg_dist(px, py, ax, ay, bx, by):
    """Minimum distance from point (px, py) to segment (ax, ay)-(bx, by)."""
    dx, dy = bx - ax, by - ay
    denom = dx*dx + dy*dy
    t = ((px - ax)*dx + (py - ay)*dy) / denom if denom > 1e-20 else 0.0
    t = max(0.0, min(1.0, t))
    qx, qy = ax + t*dx, ay + t*dy
    return math.sqrt((px - qx)**2 + (py - qy)**2)


def polygon_min_distance(poly_a, poly_b):
    """Minimum edge-to-edge distance between two polygons. Returns 0.0 if they overlap."""
    if _polys_strictly_overlap(poly_a, poly_b):
        return 0.0
    n_a, n_b = len(poly_a), len(poly_b)
    min_d = float('inf')
    for px, py in poly_a:
        for j in range(n_b):
            ax, ay = poly_b[j]; bx, by = poly_b[(j+1) % n_b]
            d = _pt_to_seg_dist(px, py, ax, ay, bx, by)
            if d < min_d:
                min_d = d
    for px, py in poly_b:
        for j in range(n_a):
            ax, ay = poly_a[j]; bx, by = poly_a[(j+1) % n_a]
            d = _pt_to_seg_dist(px, py, ax, ay, bx, by)
            if d < min_d:
                min_d = d
    return min_d


def get_board_bbox(board):
    """Return (x_min, y_min, x_max, y_max) of the board outline in mm, or None."""
    try:
        bb = board.GetBoardEdgesBoundingBox()
        return (pcbnew.ToMM(bb.GetLeft()),  pcbnew.ToMM(bb.GetTop()),
                pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom()))
    except Exception:
        return None


def _clamp_to_board(ref, positions, fp_rotations, geom_db, board_bbox):
    """Shift a component's centroid so its courtyard AABB stays within board_bbox."""
    if not board_bbox:
        return
    bx0, by0, bx1, by1 = board_bbox
    ax0, ay0, ax1, ay1 = db_aabb(ref, positions, fp_rotations, geom_db)
    cx, cy = positions[ref]
    dx = dy = 0.0
    if   ax0 < bx0: dx = bx0 - ax0
    elif ax1 > bx1: dx = bx1 - ax1
    if   ay0 < by0: dy = by0 - ay0
    elif ay1 > by1: dy = by1 - ay1
    if dx or dy:
        positions[ref] = (cx + dx, cy + dy)


# ── BOARD LOADING ─────────────────────────────────────────────────────────────

def load_board(pcb_file):
    board = pcbnew.LoadBoard(pcb_file)
    fp_map       = {}
    positions    = {}
    world_pads   = {}
    local_pads   = {}
    fp_rotations = {}
    locked_set   = set()

    for fp in board.GetFootprints():
        ref = fp.GetReference()
        fp_map[ref] = fp

        pos = fp.GetPosition()
        cx = pcbnew.ToMM(pos.x)
        cy = pcbnew.ToMM(pos.y)
        positions[ref] = (cx, cy)

        rot = fp.GetOrientationDegrees()
        fp_rotations[ref] = rot

        if fp.IsLocked():
            locked_set.add(ref)

        w_nets = {}
        l_nets = {}
        for pad in fp.Pads():
            net = pad.GetNetname()
            if not net:
                continue
            ppos = pad.GetPosition()
            world_dx = pcbnew.ToMM(ppos.x) - cx
            world_dy = pcbnew.ToMM(ppos.y) - cy
            local_dx, local_dy = rot2d(world_dx, world_dy, -rot)
            w_nets.setdefault(net, []).append((world_dx, world_dy))
            l_nets.setdefault(net, []).append((local_dx, local_dy))

        world_pads[ref] = w_nets
        local_pads[ref] = l_nets

    return board, fp_map, positions, world_pads, local_pads, fp_rotations, locked_set


# ── CLASSIFICATION ────────────────────────────────────────────────────────────

def is_no_force(ref):
    return ref in NO_FORCE_REFS or any(ref.startswith(p) for p in NO_FORCE_PREFIXES)

def build_movable_set(fp_map, locked_set):
    return {ref for ref in fp_map if ref not in locked_set and not is_no_force(ref)}


# ── FROZEN REF DETECTION ──────────────────────────────────────────────────────

def compute_frozen_refs(before, movable_set, locked_set, positions, world_pads, local_pads, fp_rotations, geom_db=None, board_bbox=None):
    """Return refs where every evaluated rule passes on distance AND rotation AND no overlaps.

    A component is NOT frozen if any of these hold:
      1. Any of its anchors is movable (anchor may still be repositioned).
      2. Its current distance to any anchor exceeds max_dist.
      3. Its current rotation is not the geometrically optimal one for any anchor.
      4. Its courtyard AABB physically intersects any other component's courtyard AABB
         (scattered to a wrong position that happens to satisfy the distance rule).
      5. Its centroid is outside the board outline (off-board components must always
         be re-placed regardless of whether distance rules happen to pass).
    """
    anchors_of = {}
    for ref_a, ref_b, *_ in RULES:
        anchors_of.setdefault(ref_a, set()).add(ref_b)

    eligible = {}   # ref_a -> count of rules evaluated
    all_ok   = {}   # ref_a -> count of rules that fully pass (distance + rotation)

    for ref_a, ref_b, max_dist, _, _, net_hint in RULES:
        key = (ref_a, ref_b)
        if key not in before:
            continue
        if any(a in movable_set for a in anchors_of.get(ref_a, set())):
            continue

        eligible[ref_a] = eligible.get(ref_a, 0) + 1

        d, _, _ = before[key]
        if d > max_dist:
            continue  # distance fails

        # Rotation check: current rotation must match the optimal rotation
        anchor_cx, anchor_cy = positions[ref_b]
        if net_hint and net_hint in world_pads.get(ref_b, {}):
            wdx, wdy = world_pads[ref_b][net_hint][0]
            fx, fy = norm2d(wdx, wdy)
        else:
            sat_cx, sat_cy = positions[ref_a]
            fx, fy = norm2d(anchor_cx - sat_cx, anchor_cy - sat_cy)

        stacking_dir = perp2d(fx, fy)
        optimal_rot  = select_rotation(ref_a, net_hint, (fx, fy), stacking_dir, local_pads, fp_rotations[ref_a])

        sym = geom_db.get(ref_a, {}).get("rotation_symmetry", "none") if geom_db else "none"
        if not rotation_ok(fp_rotations[ref_a], optimal_rot, sym):
            continue  # rotation is suboptimal — re-place this component

        all_ok[ref_a] = all_ok.get(ref_a, 0) + 1

    candidates = {
        ref for ref in eligible
        if eligible[ref] == all_ok.get(ref, 0)
        and ref in movable_set
    }

    # Remove candidates whose centroid is outside the board outline (condition 5).
    # A component that landed off-board must be re-placed even if distance rules pass.
    if board_bbox:
        bx0, by0, bx1, by1 = board_bbox
        off_board = {ref for ref in candidates
                     if not (bx0 <= positions[ref][0] <= bx1 and by0 <= positions[ref][1] <= by1)}
        for ref in off_board:
            print(f"  [off-board-unfreeze] {ref} centroid ({positions[ref][0]:.1f}, {positions[ref][1]:.1f}) outside board — will re-place")
        candidates -= off_board

    # Remove candidates whose courtyard overlaps any other component (condition 4).
    # Raw AABB intersection (no gap) — overlap means a scatter landed the component
    # in a physically wrong position that coincidentally satisfied the distance rule.
    all_refs = list(positions.keys())
    confirmed = set()
    for ref in candidates:
        ax0, ay0, ax1, ay1 = db_aabb(ref, positions, fp_rotations, geom_db or {})
        ref_poly = db_polygon_world(ref, positions, fp_rotations, geom_db or {})
        overlapping = False
        for other in all_refs:
            if other == ref:
                continue
            bx0, by0, bx1, by1 = db_aabb(other, positions, fp_rotations, geom_db or {})
            if ax1 > bx0 and bx1 > ax0 and ay1 > by0 and by1 > ay0:
                if sat_overlap_check(ref_poly,
                                     db_polygon_world(other, positions, fp_rotations, geom_db or {})):
                    overlapping = True
                    print(f"  [overlap-unfreeze] {ref} overlaps {other} — will re-place")
                    break
        if not overlapping:
            confirmed.add(ref)

    # Unfreeze anchors whose satellites are currently failing —
    # a frozen anchor at a passing distance can still block its own satellites.
    satellites_of = {}
    for rule in RULES:
        ref_a, ref_b, max_dist = rule[0], rule[1], rule[2]
        satellites_of.setdefault(ref_b, []).append((ref_a, max_dist))
    sat_fail_unfreeze = set()
    for ref in list(confirmed):
        for sat, max_d in satellites_of.get(ref, []):
            key = (sat, ref)
            if key in before:
                d = before[key][0]
                if d > max_d:
                    sat_fail_unfreeze.add(ref)
                    print(f"  [satellite-unfreeze] {ref} — {sat}->{ref} failing at {d:.1f}mm (max {max_d}mm) — will re-place")
                    break
    confirmed -= sat_fail_unfreeze

    # Blocking unfreeze: if a satellite is still failing its rule, any frozen component
    # physically near that satellite may be occupying the space the satellite needs.
    # Unfreeze those nearby frozen components so they can relocate.
    failing_sats = set()
    for rule in RULES:
        ref_a, ref_b, max_dist = rule[0], rule[1], rule[2]
        key = (ref_a, ref_b)
        if (key in before and before[key][0] > max_dist
                and ref_a in movable_set and ref_a in positions):
            failing_sats.add(ref_a)

    if failing_sats:
        blocking_unfreeze = set()
        BLOCK_THRESHOLD = 5.0
        for ref in list(confirmed):
            rx0, ry0, rx1, ry1 = db_aabb(ref, positions, fp_rotations, geom_db or {})
            for fsat in failing_sats:
                if fsat not in positions:
                    continue
                fx0, fy0, fx1, fy1 = db_aabb(fsat, positions, fp_rotations, geom_db or {})
                if (rx1 + BLOCK_THRESHOLD > fx0 and fx1 + BLOCK_THRESHOLD > rx0 and
                        ry1 + BLOCK_THRESHOLD > fy0 and fy1 + BLOCK_THRESHOLD > ry0):
                    blocking_unfreeze.add(ref)
                    print(f"  [block-unfreeze] {ref} near failing satellite {fsat} — will re-place")
                    break
        confirmed -= blocking_unfreeze

    return confirmed


# ── FACE-GROUP CONFLICT RESOLUTION ───────────────────────────────────────────

def _resolve_face_group_conflicts(groups, positions, fp_rotations, geom_db, locked_set):
    """Reassign lower-priority groups whose face direction opposes a higher-priority group."""
    def group_priority(g):
        anchor = g['anchor']
        return (
            anchor not in locked_set,
            min(m[2] for m in g['members']),
            min(RULE_TYPE_PRIORITY.get(m[3], 99) for m in g['members']),
        )
    def anchor_face_half(anchor, face_dir):
        if geom_db and anchor in geom_db and geom_db[anchor].get("courtyard"):
            a_cyd = geom_db[anchor]["courtyard"]
            a_local = a_cyd.get("polygon") or _bbox_corners(a_cyd["local_bbox"])
            a_rot = fp_rotations.get(anchor, 0.0)
            return max(
                rot2d(lx, ly, a_rot)[0]*face_dir[0] + rot2d(lx, ly, a_rot)[1]*face_dir[1]
                for lx, ly in a_local
            )
        return 2.0
    def stacking_range(g, face_dir, stacking_dir):
        anchor = g['anchor']
        ax, ay = positions[anchor]
        stk_center = ax*stacking_dir[0] + ay*stacking_dir[1]
        total_half = sum(
            _compute_sat_dims(m[0], fp_rotations.get(m[0], 0.0), face_dir, stacking_dir, geom_db)[2]
            for m in g['members'] if m[0] in positions
        ) + COURTYARD_GAP_MM * max(len(g['members']) - 1, 0)
        return stk_center - total_half, stk_center + total_half
    def sat_near_max(g, face_dir, stacking_dir):
        best = 0.0
        for m in g['members']:
            if m[0] not in positions:
                continue
            nh, *_ = _compute_sat_dims(m[0], fp_rotations.get(m[0], 0.0), face_dir, stacking_dir, geom_db)
            if nh > best:
                best = nh
        return best
    def conflicts_with(g, others, fdA, sdA):
        count = 0
        ax2, ay2 = positions[g['anchor']]
        for other in others:
            if other['anchor'] == g['anchor']:
                continue
            fdO = norm2d(*other['face_dir'])
            if fdA[0]*fdO[0] + fdA[1]*fdO[1] > -0.5:
                continue
            if other['anchor'] not in positions:
                continue
            ox2, oy2 = positions[other['anchor']]
            vec_AB = norm2d(ox2 - ax2, oy2 - ay2)
            if vec_AB[0]*fdA[0] + vec_AB[1]*fdA[1] < 0.5:
                continue
            sdO = perp2d(*fdO)
            sA0, sA1 = stacking_range(g, fdA, sdA)
            sO0, sO1 = stacking_range(other, fdO, sdO)
            if sA1 + 2.0 < sO0 or sO1 + 2.0 < sA0:
                continue
            count += 1
        return count

    reassigned = 0
    n = len(groups)
    for i in range(n):
        gA = groups[i]
        if gA['anchor'] not in positions:
            continue
        fdA = norm2d(*gA['face_dir'])
        sdA = perp2d(*fdA)
        axA, ayA = positions[gA['anchor']]
        for j in range(n):
            if i == j:
                continue
            gB = groups[j]
            if gB['anchor'] == gA['anchor']:
                continue
            if gB['anchor'] not in positions:
                continue
            fdB = norm2d(*gB['face_dir'])
            if fdA[0]*fdB[0] + fdA[1]*fdB[1] > -0.5:
                continue
            axB, ayB = positions[gB['anchor']]
            vec_AB = norm2d(axB - axA, ayB - ayA)
            if vec_AB[0]*fdA[0] + vec_AB[1]*fdA[1] < 0.5:
                continue
            sdB = perp2d(*fdB)
            # Project both groups' stacking ranges onto the SAME axis (sdA) so ranges are
            # comparable. perp2d produces opposite signs for opposing face dirs, making the
            # raw sdB projection incomparable to sdA.
            sA0, sA1 = stacking_range(gA, fdA, sdA)
            sB0, sB1 = stacking_range(gB, fdB, sdA)
            if sA1 + 2.0 < sB0 or sB1 + 2.0 < sA0:
                continue
            afh_A = anchor_face_half(gA['anchor'], fdA)
            gA_face_pos = axA*fdA[0] + ayA*fdA[1] + afh_A + COURTYARD_GAP_MM + sat_near_max(gA, fdA, sdA)
            # gB's satellites land between gB's anchor and gA. Estimate their position in fdA:
            # gB_anchor (in fdA) - gB anchor half (in fdA) - gap - gB max sat near half.
            afh_B = anchor_face_half(gB['anchor'], fdA)
            gB_sat_pos = axB*fdA[0] + ayB*fdA[1] - afh_B - COURTYARD_GAP_MM - sat_near_max(gB, fdB, sdB)
            if gA_face_pos < gB_sat_pos - 2.0:
                continue
            lower_g = gB if group_priority(gA) <= group_priority(gB) else gA
            old_face = lower_g['face_side']
            best_alt  = None
            best_score = None
            lo_fd = norm2d(*lower_g['face_dir'])
            lo_sd = perp2d(*lo_fd)
            for alt_face in ADJACENT_FACES[old_face]:
                alt_fd = norm2d(*cardinal_dir(alt_face))
                alt_sd = perp2d(*alt_fd)
                score  = conflicts_with(lower_g, groups, alt_fd, alt_sd)
                if best_score is None or score < best_score:
                    best_score = score
                    best_alt   = alt_face
            if best_alt is None or best_alt == old_face:
                continue
            lower_g['face_side'] = best_alt
            lower_g['face_dir']  = cardinal_dir(best_alt)
            print(f"  [face-conflict] {lower_g['anchor']}/{old_face} → {best_alt} "
                  f"(conflicts with {gA['anchor'] if lower_g is gB else gB['anchor']})")
            reassigned += 1
    return reassigned


# ── RULE-VIOLATION FACE RETRY ─────────────────────────────────────────────────

def _retry_violated_rules(rules, positions, local_pads, fp_rotations, geom_db, effective_movable):
    """Try adjacent anchor faces for satellites whose proximity rule is still violated."""
    def anchor_far_edge(anchor, face_dir):
        ax, ay = positions[anchor]
        proj = ax*face_dir[0] + ay*face_dir[1]
        if geom_db and anchor in geom_db and geom_db[anchor].get("courtyard"):
            a_cyd = geom_db[anchor]["courtyard"]
            a_local = a_cyd.get("polygon") or _bbox_corners(a_cyd["local_bbox"])
            a_rot = fp_rotations.get(anchor, 0.0)
            proj += max(
                rot2d(lx, ly, a_rot)[0]*face_dir[0] + rot2d(lx, ly, a_rot)[1]*face_dir[1]
                for lx, ly in a_local
            )
        return proj

    n_retried = 0
    immovable_refs = [r for r in positions if r not in effective_movable]
    for ref_a, ref_b, max_dist, rtype, reason, net_hint in rules:
        if ref_a not in effective_movable or ref_a not in positions:
            continue
        if ref_b not in positions:
            continue
        result = measure_dist(ref_a, ref_b, net_hint, positions, local_pads, fp_rotations, geom_db)
        if result is None:
            continue
        cur_dist = result[0]
        if cur_dist <= max_dist:
            continue
        sx, sy = positions[ref_a]
        ax, ay = positions[ref_b]
        cur_face = quantize_face(*norm2d(sx - ax, sy - ay))
        rot_a = fp_rotations.get(ref_a, 0.0)
        best_pos  = None
        best_dist = cur_dist
        for alt_face in ADJACENT_FACES[cur_face]:
            alt_fd = norm2d(*cardinal_dir(alt_face))
            alt_sd = perp2d(*alt_fd)
            near_h, _far_h, _stk_h = _compute_sat_dims(ref_a, rot_a, alt_fd, alt_sd, geom_db)
            face_comp = anchor_far_edge(ref_b, alt_fd) + COURTYARD_GAP_MM + near_h
            stk_comp  = ax*alt_sd[0] + ay*alt_sd[1]
            tx = face_comp*alt_fd[0] + stk_comp*alt_sd[0]
            ty = face_comp*alt_fd[1] + stk_comp*alt_sd[1]
            orig_poly  = db_polygon_world(ref_a, positions, fp_rotations, geom_db or {})
            dx, dy     = tx - sx, ty - sy
            trial_poly = [(vx + dx, vy + dy) for vx, vy in orig_poly]
            overlap = False
            for imm in immovable_refs:
                imm_poly = db_polygon_world(imm, positions, fp_rotations, geom_db or {})
                if sat_overlap_check(trial_poly, imm_poly, COURTYARD_GAP_MM):
                    overlap = True
                    break
            if overlap:
                continue
            old_pos = positions[ref_a]
            positions[ref_a] = (tx, ty)
            res2 = measure_dist(ref_a, ref_b, net_hint, positions, local_pads, fp_rotations, geom_db)
            positions[ref_a] = old_pos
            if res2 is None:
                continue
            new_dist = res2[0]
            if new_dist < best_dist:
                best_dist = new_dist
                best_pos  = (tx, ty)
        if best_pos is not None:
            positions[ref_a] = best_pos
            print(f"  [rule-retry] {ref_a} → {ref_b}: dist {cur_dist:.2f} → {best_dist:.2f} mm")
            n_retried += 1
    return n_retried


# ── CROSS-GROUP OVERLAP RESOLVER ──────────────────────────────────────────────

def _resolve_cross_group_overlaps(rules, ranked_groups, positions, local_pads,
                                   fp_rotations, geom_db, effective_movable,
                                   board_bbox=None):
    """
    After global overlap elimination, find satellites from DIFFERENT face groups that
    still overlap. For each such pair, try relocating the satellite whose proximity rule
    is satisfied (the 'free' one) to an adjacent face of its anchor, clearing the collision
    without disturbing the satellite that needs its current position to satisfy its rule.
    Skips any alternative face whose trial position would place the component outside
    the board boundary (board_bbox).
    """
    sat_group = {}
    for group in ranked_groups:
        for m in group['members']:
            if m[0] not in sat_group:
                sat_group[m[0]] = (group['anchor'], group['face_side'])

    rule_lookup = {}
    for ref_a, ref_b, max_dist, rtype, reason, net_hint in rules:
        if ref_a not in rule_lookup:
            rule_lookup[ref_a] = (ref_b, max_dist, net_hint)

    def rule_passes(ref):
        if ref not in rule_lookup or ref not in positions:
            return True
        rb, md, nh = rule_lookup[ref]
        if rb not in positions:
            return True
        d, *_ = measure_dist(ref, rb, nh, positions, local_pads, fp_rotations, geom_db)
        return d <= md

    def anchor_far_edge(anchor, face_dir):
        ax, ay = positions[anchor]
        proj = ax*face_dir[0] + ay*face_dir[1]
        if geom_db and anchor in geom_db and geom_db[anchor].get("courtyard"):
            a_cyd = geom_db[anchor]["courtyard"]
            a_local = a_cyd.get("polygon") or _bbox_corners(a_cyd["local_bbox"])
            a_rot = fp_rotations.get(anchor, 0.0)
            proj += max(rot2d(lx, ly, a_rot)[0]*face_dir[0] +
                        rot2d(lx, ly, a_rot)[1]*face_dir[1] for lx, ly in a_local)
        return proj

    polys = {r: db_polygon_world(r, positions, fp_rotations, geom_db or {}) for r in positions}
    movable_sats = [r for r in sat_group if r in effective_movable and r in positions]
    relocated = 0

    for ref_x in movable_sats:
        px = polys.get(ref_x)
        if not px:
            continue
        for ref_y in sat_group:
            if ref_y == ref_x or ref_y not in positions:
                continue
            if sat_group[ref_x][0] == sat_group[ref_y][0]:
                continue  # Same anchor group
            if not sat_overlap_check(px, polys.get(ref_y, []), COURTYARD_GAP_MM):
                continue
            # Decide which to move: move the one whose rule passes
            x_ok = rule_passes(ref_x)
            y_ok = rule_passes(ref_y)
            if x_ok and not y_ok:
                to_move = ref_x
            elif y_ok and not x_ok:
                continue  # ref_x violates, can't move it
            elif x_ok and y_ok:
                to_move = ref_x
            else:
                continue  # Both violating
            if to_move not in effective_movable:
                continue

            anchor, cur_face = sat_group[to_move]
            if anchor not in positions:
                continue
            ax, ay = positions[anchor]
            cur_rot = fp_rotations.get(to_move, 0.0)
            best_pos, best_alt = None, None

            # Sort candidate faces so the one whose trial centroid is furthest
            # from any board edge is tried first.  On boards where the anchor sits
            # near an edge this naturally prefers the interior-facing direction
            # without requiring any project-specific knowledge.
            def _trial_centroid_for_face(f):
                afd = norm2d(*cardinal_dir(f))
                asd = perp2d(*afd)
                nh, _, _ = _compute_sat_dims(to_move, cur_rot, afd, asd, geom_db)
                fc = anchor_far_edge(anchor, afd) + COURTYARD_GAP_MM + nh
                sc = ax*asd[0] + ay*asd[1]
                return fc*afd[0] + sc*asd[0], fc*afd[1] + sc*asd[1]

            def _edge_clearance(tx, ty):
                if board_bbox is None:
                    return 0.0
                bx0, by0, bx1, by1 = board_bbox
                return min(tx - bx0, bx1 - tx, ty - by0, by1 - ty)

            sorted_faces = sorted(
                ADJACENT_FACES[cur_face],
                key=lambda f: -_edge_clearance(*_trial_centroid_for_face(f))
            )

            for alt_face in sorted_faces:
                alt_fd = norm2d(*cardinal_dir(alt_face))
                alt_sd = perp2d(*alt_fd)
                near_h, _, _ = _compute_sat_dims(to_move, cur_rot, alt_fd, alt_sd, geom_db)
                face_comp = anchor_far_edge(anchor, alt_fd) + COURTYARD_GAP_MM + near_h
                stk_comp  = ax*alt_sd[0] + ay*alt_sd[1]
                tx = face_comp*alt_fd[0] + stk_comp*alt_sd[0]
                ty = face_comp*alt_fd[1] + stk_comp*alt_sd[1]
                dx, dy = tx - positions[to_move][0], ty - positions[to_move][1]
                trial = [(vx+dx, vy+dy) for vx, vy in polys[to_move]]
                if board_bbox is not None:
                    bx0, by0, bx1, by1 = board_bbox
                    old_pos = positions[to_move]
                    positions[to_move] = (tx, ty)
                    ax0, ay0, ax1, ay1 = db_aabb(to_move, positions, fp_rotations, geom_db or {})
                    positions[to_move] = old_pos
                    if ax0 < bx0 or ax1 > bx1 or ay0 < by0 or ay1 > by1:
                        continue
                if any(sat_overlap_check(trial, polys.get(r, []), COURTYARD_GAP_MM)
                       for r in positions if r != to_move):
                    continue
                old = positions[to_move]
                positions[to_move] = (tx, ty)
                still_ok = rule_passes(to_move)
                positions[to_move] = old
                if still_ok:
                    best_pos, best_alt = (tx, ty), alt_face
                    break

            if best_pos is not None:
                positions[to_move] = best_pos
                polys[to_move] = db_polygon_world(to_move, positions, fp_rotations, geom_db or {})
                sat_group[to_move] = (anchor, best_alt)
                print(f"  [cross-group] {to_move}: {anchor}/{cur_face} → {best_alt}")
                relocated += 1
    return relocated


# ── FACE GROUP BUILDING ───────────────────────────────────────────────────────

def _best_face_for_ic_placement(ref_a, ref_b, positions, board_bbox, group_map,
                                 locked_positions=None):
    """Return the anchor face that maximises available board clearance for a large IC satellite.

    Evaluates all 4 faces of ref_b. Scores each by distance from the anchor centre to the
    board edge in that direction, with a small penalty per face group already assigned to that
    face (to spread ICs away from crowded faces). Used for power_mgmt Category 2 rules where
    the satellite is itself an anchor (IC-scale), so net-hint pad voting is overridden.

    locked_positions: dict of ref -> (cx, cy) for all locked components. Faces with a locked
    component in the path are penalised so the IC satellite avoids blocked directions.
    """
    if board_bbox is None or ref_b not in positions:
        return None
    bx0, by0, bx1, by1 = board_bbox
    ax, ay = positions[ref_b]

    # A locked component is "blocking" a face if its centroid is within this cone ahead.
    BLOCK_ALONG_MAX = 12.0   # mm; only count blockers within this range ahead of anchor
    BLOCK_CROSS_MAX =  5.0   # mm; half-width of the blocking cone (cross-track)
    BLOCK_PENALTY   = 100.0  # mm score penalty per blocking locked component

    best_face, best_score = 'left', -float('inf')
    for face in ('left', 'right', 'up', 'down'):
        fd = cardinal_dir(face)
        if   fd[0] > 0:  edge_dist = bx1 - ax
        elif fd[0] < 0:  edge_dist = ax  - bx0
        elif fd[1] > 0:  edge_dist = by1 - ay
        else:             edge_dist = ay  - by0
        # Penalise faces that already host other members of this anchor.
        existing = sum(1 for k in group_map if k[0] == ref_b and k[1] == face)
        score = edge_dist - existing * 5.0

        # Penalise faces where a locked component sits in the path, blocking IC placement.
        if locked_positions:
            for lref, (lx, ly) in locked_positions.items():
                if lref == ref_b:
                    continue
                dx, dy = lx - ax, ly - ay
                along = dx * fd[0] + dy * fd[1]
                if 0 < along <= BLOCK_ALONG_MAX:
                    cross = abs(dx * fd[1] - dy * fd[0])
                    if cross <= BLOCK_CROSS_MAX:
                        score -= BLOCK_PENALTY
                        if _DBG_REFS & {ref_a, ref_b}:
                            print(f"    [blocker] {face} from {ref_b}: {lref} "
                                  f"along={along:.1f} cross={cross:.1f} → penalty")

        if score > best_score:
            best_score, best_face = score, face
    return best_face


def _score_anchor_rotation(anchor_ref, trial_rot, rules, positions, geom_db,
                           fp_rotations, locked_positions,
                           block_along=12.0, block_cross=5.0):
    """Count satellites whose net-hint face is blocked by a locked component at trial_rot."""
    if anchor_ref not in positions or not locked_positions:
        return 0
    ax, ay = positions[anchor_ref]
    trial_rots = dict(fp_rotations)
    trial_rots[anchor_ref] = trial_rot
    blocks = 0
    seen_faces = set()
    for ref_a, ref_b, _max_dist, _rtype, _reason, net_hint in rules:
        if ref_b != anchor_ref or not net_hint:
            continue
        result = db_face_for_net(anchor_ref, net_hint, geom_db, trial_rots)
        if not result:
            continue
        face_side, _ = result
        if face_side in seen_faces:
            continue  # already counted this face
        fd = cardinal_dir(face_side)
        for lx, ly in locked_positions.values():
            dx, dy = lx - ax, ly - ay
            along = dx * fd[0] + dy * fd[1]
            if 0 < along <= block_along:
                cross = abs(dx * fd[1] - dy * fd[0])
                if cross <= block_cross:
                    blocks += 1
                    seen_faces.add(face_side)
                    break
    return blocks


def build_face_groups(rules, positions, world_pads, movable_set, fp_rotations, geom_db,
                      board_bbox=None, locked_positions=None):
    group_map = {}   # (anchor, face_side) -> group dict
    assigned_face = {}  # ref_a -> face_side; tracks the face each satellite was assigned

    # Rotation pre-pass: for each movable anchor, try all 4 rotations and pick the one
    # that minimises the number of satellite faces blocked by locked components.
    # Only fires when the current rotation already has at least one blocked face.
    if locked_positions and geom_db:
        movable_anchors = {rule[1] for rule in rules
                           if rule[1] in movable_set and rule[1] in positions}
        for anchor in movable_anchors:
            current_rot = fp_rotations.get(anchor, 0.0)
            current_score = _score_anchor_rotation(
                anchor, current_rot, rules, positions, geom_db,
                fp_rotations, locked_positions)
            if current_score == 0:
                continue
            best_rot, best_score = current_rot, current_score
            for trial_rot in (0.0, 90.0, 180.0, 270.0):
                if trial_rot == current_rot:
                    continue
                score = _score_anchor_rotation(
                    anchor, trial_rot, rules, positions, geom_db,
                    fp_rotations, locked_positions)
                if score < best_score:
                    best_score, best_rot = score, trial_rot
            if best_rot != current_rot:
                fp_rotations[anchor] = best_rot
                print(f"  [rot-select] {anchor}: {current_rot:.0f}° → {best_rot:.0f}° "
                      f"(blocked satellite faces: {current_score} → {best_score})")

    for ref_a, ref_b, max_dist, rtype, reason, net_hint in rules:
        if ref_a not in movable_set:
            continue
        if ref_a not in positions or ref_b not in positions:
            continue
        # Without a net hint we can only infer face from current geometry, which is
        # unreliable (e.g. U8 sits to the lower-left of U3 but the vector quantizes
        # to "right", polluting the XTAL/AUX stacking column).  Skip these — they are
        # placed by resolve_overlaps instead.
        if not net_hint:
            continue

        db_result = db_face_for_net(ref_b, net_hint, geom_db, fp_rotations) if net_hint else None

        if db_result:
            face_side, face_dir = db_result
        else:
            # Fallback: infer from world pad offset
            anchor_cx, anchor_cy = positions[ref_b]
            sat_cx,    sat_cy    = positions[ref_a]
            if net_hint and net_hint in world_pads.get(ref_b, {}):
                wdx, wdy = world_pads[ref_b][net_hint][0]
                fx, fy = norm2d(wdx, wdy)
            else:
                fx, fy = norm2d(sat_cx - anchor_cx, sat_cy - anchor_cy)
            face_side = quantize_face(fx, fy)
            face_dir  = cardinal_dir(face_side)

        # Option A: power_mgmt IC-scale satellites use clearance-best face instead of net hint.
        # Net-hint voting sends large ICs to the same face as adjacent passives (e.g. U2 and
        # D_CC1 both vote to J_USB_IN1/down), which causes cross-group bumps and off-board
        # placement. Clearance-first assigns the IC to whichever face has the most board room.
        if (rtype == 'power_mgmt'
                and ref_a in NO_NORMALIZE_AUTO and board_bbox is not None
                and db_result is None):
            best = _best_face_for_ic_placement(ref_a, ref_b, positions, board_bbox, group_map,
                                               locked_positions=locked_positions)
            if best:
                face_side = best
                face_dir  = cardinal_dir(best)

        # If ref_b (the anchor) is itself a satellite already assigned to a face,
        # avoid placing ref_a on the same axis.  Stacking along the same axis
        # (e.g. Q2 above IC1 which is above J_PWR_IN1) can chain components off
        # the board edge.  Redirect ref_a to the best face on the perpendicular
        # axis instead, using the pad centroid projected onto that axis.
        if ref_b in assigned_face:
            anchor_axis = frozenset({'up', 'down'}) if assigned_face[ref_b] in ('up', 'down') \
                          else frozenset({'left', 'right'})
            if face_side in anchor_axis:
                perp = _perp_face_for_net(ref_b, net_hint, geom_db, fp_rotations, anchor_axis)
                if perp:
                    face_side, face_dir = perp
                    print(f"  [perp-redirect] {ref_a} → {ref_b}: axis {assigned_face[ref_b]} "
                          f"occupied → {face_side}")

        assigned_face[ref_a] = face_side

        # IC-scale members (refs that are themselves anchors for satellite groups) are
        # split into a separate group so they don't mix into the passive staircase.
        # They are placed at their natural pin-based position rather than a shared cursor.
        is_ic = ref_a in NO_NORMALIZE_AUTO and ref_a in NO_FACE_GROUP_REFS
        key = (ref_b, face_side, is_ic)
        if key not in group_map:
            group_map[key] = {
                'anchor':      ref_b,
                'face_side':   face_side,
                'face_dir':    face_dir,
                'members':     [],
                'is_ic_group': is_ic,
            }
        group_map[key]['members'].append((ref_a, net_hint, max_dist, rtype))

    return list(group_map.values())


# ── ROOT ANCHOR PLACEMENT ─────────────────────────────────────────────────────

def place_root_anchors(anchor_only_refs, positions, fp_map, locked_set, current,
                       fp_rotations=None, geom_db=None):
    """Reposition root anchors that need to move.

    Two cases handled:
      1. Locked satellites: locked component is the higher-priority anchor.
         Root anchor is placed at ANCHOR_OFFSET_FACTOR * mean_max_dist away from the
         satellite centroid, along the vector from centroid toward current anchor pos.
         This moves the anchor close to its locked satellite without overlapping it.
         Triggers when the distance rule fails OR when a courtyard overlap exists even
         though the distance rule passes (courtyard overlap = anchor too close).
      2. Scattered with no locked satellites: if ALL of the anchor's rules are
         violating by > 2× max_dist, the anchor is probably scattered off-board.
         Pull it to the weighted centroid of all its satellites.
    """
    moved = []
    for anchor in sorted(anchor_only_refs):
        if anchor not in fp_map or anchor not in positions:
            continue

        locked_pulls = [
            (ref_a, max_dist)
            for ref_a, ref_b, max_dist, _, _, _ in RULES
            if ref_b == anchor and ref_a in locked_set and ref_a in positions
        ]

        if locked_pulls:
            # Skip if all locked-satellite rules already pass AND no courtyard overlaps.
            # Courtyard overlap can happen even when the distance rule passes (the rule
            # measures center-to-center, but two large components can overlap courtyards
            # while still within max_dist of each other).
            all_pass = all(
                current.get((ref_a, anchor), (999.0,))[0] <= max_dist
                for ref_a, max_dist in locked_pulls
            )
            if all_pass and fp_rotations is not None and geom_db is not None:
                has_cyd_overlap = any(
                    sat_overlap_check(
                        db_polygon_world(anchor, positions, fp_rotations, geom_db),
                        db_polygon_world(ref_a, positions, fp_rotations, geom_db),
                        COURTYARD_GAP_MM
                    )
                    for ref_a, _ in locked_pulls if ref_a in positions
                )
                if not has_cyd_overlap:
                    continue
            elif all_pass:
                continue

            # Weighted centroid of locked satellites
            total_w      = sum(1.0 / md for _, md in locked_pulls)
            tx           = sum(positions[ref_a][0] / md for ref_a, md in locked_pulls) / total_w
            ty           = sum(positions[ref_a][1] / md for ref_a, md in locked_pulls) / total_w
            mean_max_dist = sum(md for _, md in locked_pulls) / len(locked_pulls)

            # Place at ANCHOR_OFFSET_FACTOR * mean_max_dist from centroid,
            # in the direction the anchor was already relative to the centroid.
            ax, ay = positions[anchor]
            dx, dy = norm2d(ax - tx, ay - ty)
            _offset_factor = ANCHOR_OFFSET_OVERRIDES.get(anchor, ANCHOR_OFFSET_FACTOR)
            new_x     = tx + dx * mean_max_dist * _offset_factor
            new_y     = ty + dy * mean_max_dist * _offset_factor

            old = positions[anchor]
            positions[anchor] = (new_x, new_y)
            sat_list = ', '.join(r for r, _ in locked_pulls)
            moved.append(f"  {anchor}: ({old[0]:.1f}, {old[1]:.1f}) → ({new_x:.1f}, {new_y:.1f})  [locked sats: {sat_list}]")

        # No locked satellites: leave anchor in place. The face group will move
        # satellites toward the anchor. Pulling the anchor toward scattered
        # satellites would move it away from its correct design position.

    if moved:
        print("  Root anchors repositioned:")
        for m in moved:
            print(m)
    else:
        print("  No root anchors needed repositioning.")


# ── RANKING ───────────────────────────────────────────────────────────────────

def rank_face_groups(face_groups, movable_set):
    def sort_key(g):
        tier      = 0 if g['anchor'] not in movable_set else 1
        min_dist  = min(m[2] for m in g['members'])
        best_type = min(RULE_TYPE_PRIORITY.get(m[3], 99) for m in g['members'])
        return (tier, min_dist, best_type)

    return sorted(face_groups, key=sort_key)


# ── ROTATION SELECTION ────────────────────────────────────────────────────────

def select_rotation(ref, net_hint, face_dir, stacking_dir, local_pads, current_rot,
                    geom_db=None, face_side=None):
    """Pick rotation using geometry DB when available; fall back to scoring."""

    if geom_db and face_side:
        rot = db_rotation_for_face(ref, net_hint, face_side, geom_db)
        if rot is not None:
            return rot

    # Fallback: score all 4 candidates against face/stacking direction
    best_rot, best_score = current_rot, None
    for candidate in [0, 90, 180, 270]:
        # Alignment: hint pad pointing toward anchor (-face_dir)
        if net_hint and net_hint in local_pads.get(ref, {}):
            lx, ly = local_pads[ref][net_hint][0]
            hx, hy = rot2d(lx, ly, candidate)
            hxn, hyn = norm2d(hx, hy)
            align = hxn * (-face_dir[0]) + hyn * (-face_dir[1])
        else:
            align = 0.0
        # Compactness: minimize pad spread along stacking axis
        vals = []
        for offsets in local_pads.get(ref, {}).values():
            for lx2, ly2 in offsets:
                wx, wy = rot2d(lx2, ly2, candidate)
                vals.append(wx * stacking_dir[0] + wy * stacking_dir[1])
        compact = 1.0 / ((max(vals) - min(vals) if vals else 0.0) + 0.5)
        score = 2.0 * align + compact
        if best_score is None or score > best_score:
            best_score, best_rot = score, candidate
        elif score == best_score and candidate == current_rot:
            best_rot = candidate
    return best_rot


# ── FACE GROUP PLACEMENT ──────────────────────────────────────────────────────

def _compute_sat_dims(ref, rot, face_dir, stacking_dir, geom_db):
    """Courtyard face/stacking half-extents for a satellite at a given rotation."""
    if geom_db and ref in geom_db and geom_db[ref].get("courtyard"):
        cyd = geom_db[ref]["courtyard"]
        local = cyd.get("polygon") or _bbox_corners(cyd["local_bbox"])
        face_projs = [rot2d(lx, ly, rot)[0]*face_dir[0] + rot2d(lx, ly, rot)[1]*face_dir[1]
                      for lx, ly in local]
        stk_projs  = [rot2d(lx, ly, rot)[0]*stacking_dir[0] + rot2d(lx, ly, rot)[1]*stacking_dir[1]
                      for lx, ly in local]
        return -min(face_projs), max(face_projs), (max(stk_projs) - min(stk_projs)) / 2.0
    return 1.0, 1.0, 1.0



def place_face_group(group, positions, world_pads, local_pads, fp_rotations, movable_set, placed_refs,
                     geom_db=None, locked_set=None, board_bbox=None):
    anchor       = group['anchor']
    face_dir     = norm2d(*group['face_dir'])   # already cardinal from build_face_groups
    stacking_dir = perp2d(*face_dir)
    face_side    = group['face_side']

    members = [
        m for m in group['members']
        if m[0] in movable_set and m[0] not in placed_refs
    ]
    if not members:
        return

    anchor_cx, anchor_cy = positions[anchor]

    # Anchor's courtyard far edge in face_dir — satellites must clear this.
    # Computed once per group from the anchor's DB courtyard bbox.
    anchor_edge_face_world = anchor_cx * face_dir[0] + anchor_cy * face_dir[1]  # default: anchor center
    if geom_db and anchor in geom_db and geom_db[anchor].get("courtyard"):
        a_cyd = geom_db[anchor]["courtyard"]
        a_local = a_cyd.get("polygon") or _bbox_corners(a_cyd["local_bbox"])
        a_rot = fp_rotations.get(anchor, 0.0)
        anchor_edge_face_world += max(
            rot2d(lx, ly, a_rot)[0] * face_dir[0] + rot2d(lx, ly, a_rot)[1] * face_dir[1]
            for lx, ly in a_local
        )

    placements = []

    for ref, net_hint, max_dist, rtype in members:
        if net_hint and net_hint in world_pads.get(anchor, {}):
            wdx, wdy = world_pads[anchor][net_hint][0]
            ic_pin = (anchor_cx + wdx, anchor_cy + wdy)
        else:
            ic_pin = (anchor_cx, anchor_cy)

        # IC pin stacking coordinate — used only for sort order (preserves pin sequence)
        stk_pos = ic_pin[0] * stacking_dir[0] + ic_pin[1] * stacking_dir[1]

        rot = select_rotation(ref, net_hint, face_dir, stacking_dir, local_pads, fp_rotations[ref],
                              geom_db=geom_db, face_side=face_side)

        # Hint pad world offset at chosen rotation — prefer DB local coords, fall back to load_board data
        hx, hy = 0.0, 0.0
        if net_hint:
            if geom_db and ref in geom_db:
                db_pads = [p for p in geom_db[ref]["pads"] if p["net"] == net_hint]
                if db_pads:
                    lx, ly = db_pads[0]["local_xy"]
                    hx, hy = rot2d(lx, ly, rot)
            elif net_hint in local_pads.get(ref, {}):
                lx, ly = local_pads[ref][net_hint][0]
                hx, hy = rot2d(lx, ly, rot)

        # Position hint pad at FACE_GAP beyond the IC pin along the face direction
        target_px = ic_pin[0] + face_dir[0] * FACE_GAP_MM
        target_py = ic_pin[1] + face_dir[1] * FACE_GAP_MM
        cx = target_px - hx
        cy = target_py - hy

        # Face-direction projection of this centroid
        face_comp = cx * face_dir[0] + cy * face_dir[1]

        # Clamp face_comp so satellite clears the anchor's courtyard.
        # Compute near and far face projections (toward / away from anchor).
        if geom_db and ref in geom_db and geom_db[ref].get("courtyard"):
            s_cyd = geom_db[ref]["courtyard"]
            s_local = s_cyd.get("polygon") or _bbox_corners(s_cyd["local_bbox"])
            local_face_projs = [rot2d(lx2, ly2, rot)[0] * face_dir[0] + rot2d(lx2, ly2, rot)[1] * face_dir[1]
                                for lx2, ly2 in s_local]
            sat_near_half = -min(local_face_projs)   # distance from centroid to near edge (toward anchor)
            sat_far_half  =  max(local_face_projs)   # distance from centroid to far edge (away from anchor)
        else:
            sat_near_half = 1.0
            sat_far_half  = 1.0

        # near_edge_face = face_comp - sat_near_half must be >= anchor_edge + COURTYARD_GAP
        min_face_comp = anchor_edge_face_world + COURTYARD_GAP_MM + sat_near_half
        face_comp = max(face_comp, min_face_comp)

        # Stacking half-extent from DB courtyard; fall back to pad extent
        if geom_db and ref in geom_db and geom_db[ref].get("courtyard"):
            s_cyd2 = geom_db[ref]["courtyard"]
            s_local2 = s_cyd2.get("polygon") or _bbox_corners(s_cyd2["local_bbox"])
            stk_proj = [rot2d(lx2, ly2, rot)[0] * stacking_dir[0] +
                        rot2d(lx2, ly2, rot)[1] * stacking_dir[1]
                        for lx2, ly2 in s_local2]
            stk_half = (max(stk_proj) - min(stk_proj)) / 2.0
        else:
            stk_vals = []
            for offsets in local_pads.get(ref, {}).values():
                for lx2, ly2 in offsets:
                    wx, wy = rot2d(lx2, ly2, rot)
                    stk_vals.append(wx * stacking_dir[0] + wy * stacking_dir[1])
            stk_half = ((max(stk_vals) - min(stk_vals)) / 2.0 + 0.15) if stk_vals else 0.5

        placements.append({
            'ref':            ref,
            'rot':            rot,
            'face_comp':      face_comp,
            'min_face_comp':  min_face_comp,
            'face_near_half': sat_near_half,
            'face_far_half':  sat_far_half,
            'stk_pos':        stk_pos,
            'stk_half':       stk_half,
            'max_dist':       max_dist,
        })

    # Intra-group face-direction separation: sort by face_comp so members closer to the
    # anchor are processed first, then push any member whose near face edge would overlap
    # the previous member's far face edge outward along the face direction.
    placements.sort(key=lambda p: p['face_comp'])
    max_far_fc = anchor_edge_face_world
    for p in placements:
        near_fc = p['face_comp'] - p['face_near_half']
        min_near_fc = max_far_fc + COURTYARD_GAP_MM
        if near_fc < min_near_fc:
            p['face_comp'] = min_near_fc + p['face_near_half']
        max_far_fc = p['face_comp'] + p['face_far_half']

    if group.get('is_ic_group'):
        # IC-scale satellites: place each at its natural pin-based stacking position.
        # Body-size-aware stacking: sort by stk_pos with larger bodies first on ties,
        # then walk a cursor so each member sits at its natural position or is pushed
        # further along the stacking axis just enough to clear the previous member's
        # courtyard (stk_half gap).  This preserves pin-order intent while respecting
        # every component's actual body width (stk_half) as the spacing unit.
        placements.sort(key=lambda p: (p['stk_pos'], -p['stk_half']))
        cursor = None
        for p in placements:
            if cursor is None:
                stk = p['stk_pos']
            else:
                stk = max(p['stk_pos'], cursor + p['stk_half'])
            p['cx'] = p['face_comp'] * face_dir[0] + stk * stacking_dir[0]
            p['cy'] = p['face_comp'] * face_dir[1] + stk * stacking_dir[1]
            cursor = stk + p['stk_half'] + COURTYARD_GAP_MM
    else:
        # Passive staircase: sort by IC pin stacking coordinate to preserve pin order.
        # Tiebreaker: larger stk_half first — bigger components (crystals) go to the
        # outer position where there is more room, preventing small caps from being
        # sandwiched between a large body and an anchor or locked ref.
        # Body size is already encoded in stk_half (courtyard half-extent) and face_comp
        # (min_face_comp = anchor_edge + gap + sat_near_half), so larger bodies naturally
        # take more stacking space and sit further from the anchor in the face direction.
        placements.sort(key=lambda p: (p['stk_pos'], -p['stk_half']))

        mean_stk     = sum(p['stk_pos'] for p in placements) / len(placements)
        total_extent = sum(2 * p['stk_half'] for p in placements) + COURTYARD_GAP_MM * (len(placements) - 1)
        cursor       = mean_stk - total_extent / 2.0

        for p in placements:
            new_stk = cursor + p['stk_half']
            p['cx'] = p['face_comp'] * face_dir[0] + new_stk * stacking_dir[0]
            p['cy'] = p['face_comp'] * face_dir[1] + new_stk * stacking_dir[1]
            cursor += 2 * p['stk_half'] + COURTYARD_GAP_MM

    # Shift group in stacking direction to clear all other refs at the same face level.
    # Uses group's full face AABB (not just center) to catch large components like crystals.
    g_stk_min = min(p['cx']*stacking_dir[0] + p['cy']*stacking_dir[1] - p['stk_half'] for p in placements)
    g_stk_max = max(p['cx']*stacking_dir[0] + p['cy']*stacking_dir[1] + p['stk_half'] for p in placements)
    # Maximum face-direction extent of this satellite group — components further out cannot overlap
    group_face_max = max(p['face_comp'] for p in placements) + max(p['stk_half'] for p in placements)
    occupied = []  # (stk_min, stk_max, face_min, face_max) of already-placed refs in the satellite zone
    # Include ALL refs currently in positions except the satellites being placed right now.
    # This ensures anchors (not KiCad-locked, not yet in placed_refs) are treated as obstacles.
    _current_group = {p['ref'] for p in placements}
    _occupied_refs = set(positions.keys()) - _current_group
    for nr in list(_occupied_refs):
        if nr not in positions:
            continue
        nr_world = db_polygon_world(nr, positions, fp_rotations, geom_db or {})
        # Only consider refs that protrude into the satellite zone (beyond anchor's far face edge).
        nr_face_projs = [v[0]*face_dir[0] + v[1]*face_dir[1] for v in nr_world]
        nm_face_max = max(nr_face_projs)
        if nm_face_max <= anchor_edge_face_world:
            continue
        # Skip components whose near face edge is beyond the satellite group's far extent.
        nm_face_min = min(nr_face_projs)
        if nm_face_min > group_face_max + COURTYARD_GAP_MM:
            continue
        nm_stk_vals = [v[0]*stacking_dir[0] + v[1]*stacking_dir[1] for v in nr_world]
        occupied.append((min(nm_stk_vals), max(nm_stk_vals), nm_face_min, nm_face_max))

    # Board boundary: add the board's far edge in this face direction as a virtual obstacle
    # with infinite stacking extent. This makes the 2D face clamp (Option A) push satellites
    # back toward the anchor rather than off the board when the anchor sits at the board edge.
    if board_bbox:
        bx0, by0, bx1, by1 = board_bbox
        corners = [(bx0, by0), (bx1, by0), (bx0, by1), (bx1, by1)]
        board_far_face = max(c[0]*face_dir[0] + c[1]*face_dir[1] for c in corners)
        _INF = 1e9
        occupied.append((-_INF, _INF, board_far_face, _INF))

    # Debug: trace occupied list and satellite positions for watched refs
    _watched_in_group = [p['ref'] for p in placements if p['ref'] in _DBG_REFS]
    if _watched_in_group:
        print(f"  [DBG place_face_group] anchor={group['anchor']} face={group['face_side']} "
              f"anchor_edge_face_world={anchor_edge_face_world:.3f}")
        for p in placements:
            if p['ref'] in _DBG_REFS:
                print(f"  [DBG]   {p['ref']}: cx={p['cx']:.3f} cy={p['cy']:.3f} "
                      f"face_comp={p['face_comp']:.3f} stk={p['cx']*stacking_dir[0]+p['cy']*stacking_dir[1]:.3f}")
        print(f"  [DBG]   occupied ({len(occupied)}):")
        for o in occupied:
            print(f"  [DBG]     stk=[{o[0]:.3f},{o[1]:.3f}] face=[{o[2]:.3f},{o[3]:.3f}]")
        # Check specifically for J_PWR_IN1
        if "J_PWR_IN1" in positions:
            jw = db_polygon_world("J_PWR_IN1", positions, fp_rotations, geom_db or {})
            j_face = [v[0]*face_dir[0]+v[1]*face_dir[1] for v in jw]
            j_stk  = [v[0]*stacking_dir[0]+v[1]*stacking_dir[1] for v in jw]
            print(f"  [DBG]   J_PWR_IN1 face=[{min(j_face):.3f},{max(j_face):.3f}] "
                  f"stk=[{min(j_stk):.3f},{max(j_stk):.3f}] "
                  f"anchor_edge={anchor_edge_face_world:.3f} "
                  f"in_occupied={'IN' if max(j_face)>anchor_edge_face_world else 'EXCLUDED (behind anchor)'}")

    if occupied:
        # Check if group overlaps any occupied interval (with courtyard gap)
        conflict = any(
            min(g_stk_max + COURTYARD_GAP_MM, om) - max(g_stk_min - COURTYARD_GAP_MM, on) > 0
            for on, om, _f0, _f1 in occupied
        )
        if conflict:
            # Find the gap in occupied intervals (sorted) that can fit the group
            # and is closest to the current group center.
            half_g = (g_stk_max - g_stk_min) / 2.0
            group_center = (g_stk_min + g_stk_max) / 2.0
            sorted_occ = sorted(occupied)
            # Candidate gap centers: before first, between each pair, after last
            gap_centers = [sorted_occ[0][0] - COURTYARD_GAP_MM - half_g]  # before first
            for i in range(len(sorted_occ) - 1):
                gap_start = sorted_occ[i][1] + COURTYARD_GAP_MM + half_g
                gap_end   = sorted_occ[i+1][0] - COURTYARD_GAP_MM - half_g
                if gap_end >= gap_start:  # gap is wide enough
                    gap_centers.append((gap_start + gap_end) / 2.0)
            gap_centers.append(sorted_occ[-1][1] + COURTYARD_GAP_MM + half_g)  # after last
            best_center = min(gap_centers, key=lambda gc: abs(gc - group_center))
            shift = best_center - group_center
            total_extent = g_stk_max - g_stk_min
            # Only shift if the best gap is reasonably close. If it requires moving the group
            # by more than half its own width, no adjacent gap fits — skip the shift and let
            # the overlap-resolution sweep nudge individual members instead.
            # Using half-width (not full width): a shift equal to total_extent means the
            # group just barely clears its nearest obstacle, but total_extent/2 is the point
            # at which the best gap no longer overlaps the group's natural stacking range.
            if abs(shift) <= total_extent:
                for p in placements:
                    p['cx'] += shift * stacking_dir[0]
                    p['cy'] += shift * stacking_dir[1]

    # 2D face-direction obstacle clamp: if a satellite's face_comp puts its far edge inside
    # an obstacle that also overlaps in the stacking direction, push the satellite toward
    # the anchor so it fits between the anchor and the obstacle.
    for p in placements:
        p_stk = p['cx'] * stacking_dir[0] + p['cy'] * stacking_dir[1]
        p_face = p['cx'] * face_dir[0] + p['cy'] * face_dir[1]
        for obs_stk_min, obs_stk_max, obs_face_min, _obs_face_max in occupied:
            # Only check obstacles whose stk range overlaps this satellite (with gap)
            if p_stk + p['stk_half'] < obs_stk_min - COURTYARD_GAP_MM:
                continue
            if p_stk - p['stk_half'] > obs_stk_max + COURTYARD_GAP_MM:
                continue
            # If satellite's far face edge would be inside or too close to obstacle's near face,
            # push face_comp toward anchor so the satellite fits between anchor and obstacle.
            sat_far_edge = p_face + p['face_far_half']
            if sat_far_edge + COURTYARD_GAP_MM > obs_face_min:
                new_face = obs_face_min - COURTYARD_GAP_MM - p['face_far_half']
                new_face = max(new_face, p['min_face_comp'])  # never crowd the anchor
                if new_face < p_face:
                    delta = new_face - p_face
                    p['cx'] += delta * face_dir[0]
                    p['cy'] += delta * face_dir[1]
                    p_face = new_face

    # 2D face clamp — Option B (fit-beyond): for satellites that couldn't be pushed toward
    # the anchor (stuck at min_face_comp) because the obstacle sits at a deeper face depth,
    # try pushing the satellite BEYOND all such blocking obstacles' far faces.
    # This is a SEPARATE pass so it cannot interact with the Option A pass above.
    for p in placements:
        p_stk  = p['cx'] * stacking_dir[0] + p['cy'] * stacking_dir[1]
        p_face = p['cx'] * face_dir[0]     + p['cy'] * face_dir[1]
        # Only eligible when satellite is AT min_face_comp (Option A cannot help).
        if p_face > p['min_face_comp'] + 1e-6:
            continue
        b_face_mins = []
        b_face_maxs = []
        for obs_stk_min, obs_stk_max, obs_face_min, obs_face_max in occupied:
            if p_stk + p['stk_half'] < obs_stk_min - COURTYARD_GAP_MM:
                continue
            if p_stk - p['stk_half'] > obs_stk_max + COURTYARD_GAP_MM:
                continue
            if p_face + p['face_far_half'] + COURTYARD_GAP_MM > obs_face_min:
                b_face_mins.append(obs_face_min)
                b_face_maxs.append(obs_face_max)
        if not b_face_mins:
            continue
        # Guard: obstacle must be genuinely beyond the satellite's own anchor clearance.
        # Block Option B only when obs_face_min is at or below anchor_near_face — meaning
        # the obstacle is actually between the anchor edge and the satellite's clearance
        # position (pushing beyond it would be moving backwards toward the anchor).
        # The original +COURTYARD_GAP_MM was too conservative: it blocked Option B for
        # obstacles that are at the satellite's near edge level (e.g. SOM2 ≈ 0.003mm
        # beyond anchor_near_face), which are legitimate targets for Option B placement.
        anchor_near_face = p['min_face_comp'] - p['face_near_half']
        if max(b_face_maxs) <= anchor_near_face:
            continue
        face_B = max(b_face_maxs) + COURTYARD_GAP_MM + p['face_near_half']
        # Guard: don't use Option B if it would move the satellite beyond its rule's max_dist.
        # face_B - anchor_face_center approximates center-to-center face distance. If this
        # exceeds max_dist, Option B would create a proximity-rule violation — skip it and
        # let the global overlap pass handle the residual conflict instead.
        anchor_face_center = anchor_cx * face_dir[0] + anchor_cy * face_dir[1]
        if face_B - anchor_face_center > p['max_dist']:
            continue
        # Verify face_B doesn't create a new overlap with any stacking-overlapping obstacle.
        stk_occ = [
            (obs_face_min, obs_face_max)
            for obs_stk_min, obs_stk_max, obs_face_min, obs_face_max in occupied
            if (p_stk + p['stk_half'] >= obs_stk_min - COURTYARD_GAP_MM and
                p_stk - p['stk_half'] <= obs_stk_max + COURTYARD_GAP_MM)
        ]
        fits_B = not any(
            (face_B - p['face_near_half'] < _fmax + COURTYARD_GAP_MM and
             face_B + p['face_far_half'] > _fmin - COURTYARD_GAP_MM)
            for _fmin, _fmax in stk_occ
        )
        if fits_B:
            delta = face_B - p_face
            p['cx'] += delta * face_dir[0]
            p['cy'] += delta * face_dir[1]

    # Rotation fallback: for each satellite still overlapping an occupied obstacle after
    # Options A and B, try the other 3 axis-aligned rotations. Accept the trial with the
    # smallest face depth that clears all obstacles AND does not overlap the anchor itself.
    anchor_poly = db_polygon_world(anchor, positions, fp_rotations, geom_db or {})

    def _trial_world_poly(ref, trial_cx, trial_cy, trial_rot):
        if geom_db and ref in geom_db and geom_db[ref].get("courtyard"):
            cyd = geom_db[ref]["courtyard"]
            local = cyd.get("polygon") or _bbox_corners(cyd["local_bbox"])
            return [(trial_cx + rot2d(lx, ly, trial_rot)[0],
                     trial_cy + rot2d(lx, ly, trial_rot)[1])
                    for lx, ly in local]
        return [(trial_cx-1, trial_cy-1), (trial_cx+1, trial_cy-1),
                (trial_cx+1, trial_cy+1), (trial_cx-1, trial_cy+1)]

    def _overlaps_any(stk_c, face_c, stk_h, near_h, far_h):
        for obs_sn, obs_sm, obs_fn, obs_fm in occupied:
            if (stk_c + stk_h + COURTYARD_GAP_MM > obs_sn and
                stk_c - stk_h - COURTYARD_GAP_MM < obs_sm and
                face_c + far_h  + COURTYARD_GAP_MM > obs_fn and
                face_c - near_h - COURTYARD_GAP_MM < obs_fm):
                return True
        return False

    for p in placements:
        p_stk  = p['cx'] * stacking_dir[0] + p['cy'] * stacking_dir[1]
        p_face = p['cx'] * face_dir[0]     + p['cy'] * face_dir[1]
        if not _overlaps_any(p_stk, p_face, p['stk_half'], p['face_near_half'], p['face_far_half']):
            continue
        # Don't rotate components in NO_ROTATE_IN_OVERLAP — their preferred_rotation is
        # correct; let stacking-offset and face-fallback resolve overlaps instead.
        if p['ref'] in NO_ROTATE_IN_OVERLAP:
            continue
        best_face = float('inf')
        best_trial = None
        for trial_rot in [(p['rot'] + 90) % 360, (p['rot'] + 180) % 360, (p['rot'] + 270) % 360]:
            near_h, far_h, stk_h = _compute_sat_dims(p['ref'], trial_rot, face_dir, stacking_dir, geom_db)
            new_min_face = anchor_edge_face_world + COURTYARD_GAP_MM + near_h
            trial_face = max(p_face, new_min_face)
            if _overlaps_any(p_stk, trial_face, stk_h, near_h, far_h):
                continue
            trial_cx = trial_face * face_dir[0] + p_stk * stacking_dir[0]
            trial_cy = trial_face * face_dir[1] + p_stk * stacking_dir[1]
            if sat_overlap_check(_trial_world_poly(p['ref'], trial_cx, trial_cy, trial_rot),
                                 anchor_poly, COURTYARD_GAP_MM):
                continue
            if trial_face < best_face:
                best_face = trial_face
                best_trial = (trial_rot, trial_face, near_h, far_h, stk_h, trial_cx, trial_cy)
        if best_trial:
            trial_rot, trial_face, near_h, far_h, stk_h, trial_cx, trial_cy = best_trial
            p['rot'] = trial_rot
            p['face_near_half'] = near_h; p['face_far_half'] = far_h; p['stk_half'] = stk_h
            p['cx'] = trial_cx; p['cy'] = trial_cy

    # ── Stacking offset search ────────────────────────────────────────────────
    # For each satellite still overlapping after A/B/rotation, slide it along
    # the stacking axis (perpendicular to face direction) in 0.5mm steps up to
    # 12mm in each direction until it clears all occupied obstacles.
    # The satellite stays on the same face and keeps its current rotation —
    # it moves AWAY from the conflict rather than away from the anchor.
    _STK_STEP = 0.5
    _STK_MAX  = 12.0
    _stk_offsets = []
    for _i in range(1, int(_STK_MAX / _STK_STEP) + 1):
        _stk_offsets.append( _i * _STK_STEP)
        _stk_offsets.append(-_i * _STK_STEP)

    for p in placements:
        p_stk  = p['cx'] * stacking_dir[0] + p['cy'] * stacking_dir[1]
        p_face = p['cx'] * face_dir[0]     + p['cy'] * face_dir[1]
        if not _overlaps_any(p_stk, p_face, p['stk_half'], p['face_near_half'], p['face_far_half']):
            continue
        for delta in _stk_offsets:
            trial_stk = p_stk + delta
            if not _overlaps_any(trial_stk, p_face, p['stk_half'], p['face_near_half'], p['face_far_half']):
                p['cx'] += delta * stacking_dir[0]
                p['cy'] += delta * stacking_dir[1]
                break

    # ── Face fallback ─────────────────────────────────────────────────────────
    # For each satellite that STILL overlaps after all above (including stacking
    # offset search), try placing it on the 3 other faces of the same anchor.
    # Uses polygon-accurate overlap checks against all placed/locked refs.
    # The satellite moves away from the conflict while staying adjacent to its anchor.
    _poly_cache = {}
    for _nr in list(_occupied_refs):
        if _nr in positions:
            _poly_cache[_nr] = db_polygon_world(_nr, positions, fp_rotations, geom_db or {})

    for p in placements:
        p_stk  = p['cx'] * stacking_dir[0] + p['cy'] * stacking_dir[1]
        p_face = p['cx'] * face_dir[0]     + p['cy'] * face_dir[1]
        if not _overlaps_any(p_stk, p_face, p['stk_half'], p['face_near_half'], p['face_far_half']):
            continue
        best_alt = None
        best_dist = float('inf')
        for _try_face_side in ADJACENT_FACES[face_side]:
            _tfd = cardinal_dir(_try_face_side)
            _tsd = perp2d(*_tfd)
            # Anchor's far courtyard edge in the new face direction
            _try_ae = anchor_cx * _tfd[0] + anchor_cy * _tfd[1]
            if geom_db and anchor in geom_db and geom_db[anchor].get("courtyard"):
                _a_cyd = geom_db[anchor]["courtyard"]
                _a_loc = _a_cyd.get("polygon") or _bbox_corners(_a_cyd["local_bbox"])
                _a_rot = fp_rotations.get(anchor, 0.0)
                _try_ae += max(
                    rot2d(_lx, _ly, _a_rot)[0] * _tfd[0] + rot2d(_lx, _ly, _a_rot)[1] * _tfd[1]
                    for _lx, _ly in _a_loc
                )
            # NO_ROTATE_IN_OVERLAP refs must keep their preferred rotation even on fallback faces
            _fb_rots = ([p['rot']] if p['ref'] in NO_ROTATE_IN_OVERLAP
                        else [p['rot'], (p['rot']+90)%360, (p['rot']+180)%360, (p['rot']+270)%360])
            for _trial_rot in _fb_rots:
                _near_h, _far_h, _stk_h = _compute_sat_dims(p['ref'], _trial_rot, _tfd, _tsd, geom_db)
                _try_fc = _try_ae + COURTYARD_GAP_MM + _near_h
                # Place at anchor center along new stacking axis
                _try_stk = anchor_cx * _tsd[0] + anchor_cy * _tsd[1]
                _tcx = _try_fc * _tfd[0] + _try_stk * _tsd[0]
                _tcy = _try_fc * _tfd[1] + _try_stk * _tsd[1]
                _trial_poly = _trial_world_poly(p['ref'], _tcx, _tcy, _trial_rot)
                if sat_overlap_check(_trial_poly, anchor_poly, COURTYARD_GAP_MM):
                    continue
                if any(sat_overlap_check(_trial_poly, _poly, COURTYARD_GAP_MM)
                       for _poly in _poly_cache.values()):
                    continue
                _dist = math.sqrt((_tcx - anchor_cx)**2 + (_tcy - anchor_cy)**2)
                if _dist < best_dist:
                    best_dist = _dist
                    best_alt = (_trial_rot, _tcx, _tcy, _near_h, _far_h, _stk_h)
        if best_alt:
            _trial_rot, _tcx, _tcy, _near_h, _far_h, _stk_h = best_alt
            p['rot'] = _trial_rot
            p['cx'] = _tcx; p['cy'] = _tcy
            p['face_near_half'] = _near_h; p['face_far_half'] = _far_h; p['stk_half'] = _stk_h

    # ── Final SAT verification ────────────────────────────────────────────────
    # After all 1D resolution steps, confirm each satellite clears all obstacles
    # using polygon-accurate SAT (including other group members now placed).
    # 1D _overlaps_any can pass while 2D polygons still touch; this catches that.
    # If overlap found, retry stacking offsets with full SAT guard.
    for p in placements:
        p_face = p['cx'] * face_dir[0] + p['cy'] * face_dir[1]
        p_stk  = p['cx'] * stacking_dir[0] + p['cy'] * stacking_dir[1]
        p_poly = _trial_world_poly(p['ref'], p['cx'], p['cy'], p['rot'])
        sat_ok = (not any(sat_overlap_check(p_poly, _pl, COURTYARD_GAP_MM)
                          for _pl in _poly_cache.values())
                  and not any(sat_overlap_check(
                                  p_poly,
                                  _trial_world_poly(p2['ref'], p2['cx'], p2['cy'], p2['rot']),
                                  COURTYARD_GAP_MM)
                              for p2 in placements if p2['ref'] != p['ref']))
        if sat_ok:
            continue
        for delta in _stk_offsets:
            trial_stk = p_stk + delta
            tcx = p_face * face_dir[0] + trial_stk * stacking_dir[0]
            tcy = p_face * face_dir[1] + trial_stk * stacking_dir[1]
            tpoly = _trial_world_poly(p['ref'], tcx, tcy, p['rot'])
            if sat_overlap_check(tpoly, anchor_poly, COURTYARD_GAP_MM):
                continue
            if any(sat_overlap_check(tpoly, _pl, COURTYARD_GAP_MM) for _pl in _poly_cache.values()):
                continue
            if any(sat_overlap_check(tpoly,
                                     _trial_world_poly(p2['ref'], p2['cx'], p2['cy'], p2['rot']),
                                     COURTYARD_GAP_MM)
                   for p2 in placements if p2['ref'] != p['ref']):
                continue
            p['cx'] = tcx; p['cy'] = tcy
            break

    # Debug: report final placement state for watched refs
    for p in placements:
        if p['ref'] in _DBG_REFS:
            p_stk  = p['cx'] * stacking_dir[0] + p['cy'] * stacking_dir[1]
            p_face = p['cx'] * face_dir[0]     + p['cy'] * face_dir[1]
            still_over = _overlaps_any(p_stk, p_face, p['stk_half'], p['face_near_half'], p['face_far_half'])
            print(f"  [DBG post-resolve] {p['ref']} anchor={anchor} face={face_side}: "
                  f"pos=({p['cx']:.3f},{p['cy']:.3f}) rot={p['rot']:.0f} "
                  f"stk={p_stk:.3f} face={p_face:.3f} still_overlaps={still_over}")

    for p in placements:
        positions[p['ref']]    = (p['cx'], p['cy'])
        fp_rotations[p['ref']] = p['rot']
        placed_refs.add(p['ref'])
        if p['ref'] in _DBG_REFS:
            print(f"  [DBG final-write] {p['ref']}: pos=({p['cx']:.3f},{p['cy']:.3f}) rot={p['rot']:.1f}")


# ── GLOBAL OVERLAP ELIMINATION ────────────────────────────────────────────────

def global_no_overlap_pass(positions, fp_rotations, broad_movable, geom_db, n_iters=50, board_bbox=None):
    """Priority-aware global courtyard overlap elimination.

    broad_movable = movable_set - anchor_stable: includes frozen satellites so overlaps
    between already-passing components (frozen from a prior pass) are still resolved.

    Priority 0 — immovable: locked, anchor-stable (true IC anchors), or no-force refs.
    Priority 1 — IC-anchor: in broad_movable AND in NO_NORMALIZE_AUTO (is itself an
                 anchor for other satellites, e.g. U5/U6/U7/U8, L1, R_COMP1).
    Priority 2 — satellite: in broad_movable AND not in NO_NORMALIZE_AUTO.

    Unequal-priority pair: lower priority takes the full displacement, higher never moves.
    Equal-priority pair: displacement split equally between both.
    Pairs connected by a direct proximity rule are skipped — their overlap is intentional.
    Iterates until convergence or n_iters sweeps.
    """
    refs = list(positions.keys())
    gap  = COURTYARD_GAP_MM

    def _priority(ref):
        if ref not in broad_movable:
            return 0
        return 1 if ref in NO_NORMALIZE_AUTO else 2

    for _ in range(n_iters):
        changed = False
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                ra, rb = refs[i], refs[j]
                pa, pb = _priority(ra), _priority(rb)
                if pa == 0 and pb == 0:
                    continue

                ax0, ay0, ax1, ay1 = db_aabb(ra, positions, fp_rotations, geom_db)
                bx0, by0, bx1, by1 = db_aabb(rb, positions, fp_rotations, geom_db)

                ox = min(ax1, bx1) - max(ax0, bx0) + gap
                oy = min(ay1, by1) - max(ay0, by0) + gap
                if ox <= 0 or oy <= 0:
                    continue
                # SAT confirmation: polygon-accurate check eliminates false positives
                # from AABB corners that don't correspond to actual courtyard material.
                if not sat_overlap_check(
                        db_polygon_world(ra, positions, fp_rotations, geom_db),
                        db_polygon_world(rb, positions, fp_rotations, geom_db), gap):
                    continue

                changed = True
                # When overlaps are very close, prefer X: Y-direction pushes risk
                # oscillation against anchor boundaries in face direction.
                if ox < oy - 0.05:
                    push = ox
                    d = 1.0 if positions[rb][0] >= positions[ra][0] else -1.0
                    if pa < pb:
                        x, y = positions[rb]; positions[rb] = (x + d * push, y)
                        _clamp_to_board(rb, positions, fp_rotations, geom_db, board_bbox)
                    elif pb < pa:
                        x, y = positions[ra]; positions[ra] = (x - d * push, y)
                        _clamp_to_board(ra, positions, fp_rotations, geom_db, board_bbox)
                    else:
                        half = push / 2.0
                        x, y = positions[ra]; positions[ra] = (x - d * half, y)
                        x, y = positions[rb]; positions[rb] = (x + d * half, y)
                        _clamp_to_board(ra, positions, fp_rotations, geom_db, board_bbox)
                        _clamp_to_board(rb, positions, fp_rotations, geom_db, board_bbox)
                elif oy < ox - 0.05:
                    push = oy
                    d = 1.0 if positions[rb][1] >= positions[ra][1] else -1.0
                    if pa < pb:
                        x, y = positions[rb]; positions[rb] = (x, y + d * push)
                        _clamp_to_board(rb, positions, fp_rotations, geom_db, board_bbox)
                    elif pb < pa:
                        x, y = positions[ra]; positions[ra] = (x, y - d * push)
                        _clamp_to_board(ra, positions, fp_rotations, geom_db, board_bbox)
                    else:
                        half = push / 2.0
                        x, y = positions[ra]; positions[ra] = (x, y - d * half)
                        x, y = positions[rb]; positions[rb] = (x, y + d * half)
                        _clamp_to_board(ra, positions, fp_rotations, geom_db, board_bbox)
                        _clamp_to_board(rb, positions, fp_rotations, geom_db, board_bbox)
                else:
                    # Near-tie: prefer X so satellites aren't pushed into anchor boundaries.
                    push = ox
                    d = 1.0 if positions[rb][0] >= positions[ra][0] else -1.0
                    if pa < pb:
                        x, y = positions[rb]; positions[rb] = (x + d * push, y)
                        _clamp_to_board(rb, positions, fp_rotations, geom_db, board_bbox)
                    elif pb < pa:
                        x, y = positions[ra]; positions[ra] = (x - d * push, y)
                        _clamp_to_board(ra, positions, fp_rotations, geom_db, board_bbox)
                    else:
                        half = push / 2.0
                        x, y = positions[ra]; positions[ra] = (x - d * half, y)
                        x, y = positions[rb]; positions[rb] = (x + d * half, y)
                        _clamp_to_board(ra, positions, fp_rotations, geom_db, board_bbox)
                        _clamp_to_board(rb, positions, fp_rotations, geom_db, board_bbox)

        if not changed:
            break


# ── PER-TYPE GAP CONSTANTS ────────────────────────────────────────────────────
# Minimum required courtyard clearance by component category.
# Derived from IPC-7351 / KiCad reference designator conventions — not project-specific.
PASSIVE_MIN_GAP   = 0.025   # C, R, L, Y, TP — small SMD passives
IC_MIN_GAP        = COURTYARD_GAP_MM  # 0.15 — ICs, transistors, diodes, fuses
CONNECTOR_MIN_GAP = 0.50    # J, SOM — connectors and system-on-modules

# Prefixes that indicate a connector (J) or SOM footprint → need 0.5mm clearance.
_CONNECTOR_PREFIXES = ('J', 'SOM')
# Prefixes that indicate a passive → need only 0.025mm clearance.
_PASSIVE_PREFIXES   = ('C', 'R', 'L', 'Y', 'TP', 'MH')


def _ref_min_gap(ref):
    """Return the minimum courtyard gap required around this component."""
    r = ref.upper()
    for pfx in _CONNECTOR_PREFIXES:
        if r.startswith(pfx):
            return CONNECTOR_MIN_GAP
    for pfx in _PASSIVE_PREFIXES:
        if r.startswith(pfx):
            return PASSIVE_MIN_GAP
    return IC_MIN_GAP


def _pair_min_gap(ra, rb):
    """Return the minimum required gap between two components (max of their individual gaps)."""
    return max(_ref_min_gap(ra), _ref_min_gap(rb))


def push_until_clear(positions, fp_rotations, movable_set, geom_db, board_bbox,
                     max_iters=200):
    """Final overlap elimination loop — runs until every component pair meets its
    per-type minimum courtyard gap or max_iters is reached.

    Unlike global_no_overlap_pass (fixed 0.15mm gap, bounded inner loop), this
    function uses per-type gaps (passive=0.025, IC=0.15, connector=0.5) and iterates
    outer sweeps until no pair reports an overlap. This resolves residual violations
    that the fixed-gap pass misses — e.g. a transistor 0.4mm from a connector
    clears the 0.15mm threshold but still violates the 0.5mm connector rule.

    Priority model (same as global_no_overlap_pass):
      0 = immovable (not in movable_set)
      1 = IC anchor (movable AND in NO_NORMALIZE_AUTO)
      2 = satellite (movable AND not in NO_NORMALIZE_AUTO)
    Higher-priority component is never moved; lower takes the full displacement.
    Equal-priority pairs split the displacement evenly.
    """
    refs = list(positions.keys())

    def _priority(ref):
        if ref not in movable_set:
            return 0
        return 1 if ref in NO_NORMALIZE_AUTO else 2

    total_pushes = 0
    for iteration in range(max_iters):
        changed = False
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                ra, rb = refs[i], refs[j]
                pa, pb = _priority(ra), _priority(rb)
                if pa == 0 and pb == 0:
                    continue

                gap = _pair_min_gap(ra, rb)

                ax0, ay0, ax1, ay1 = db_aabb(ra, positions, fp_rotations, geom_db)
                bx0, by0, bx1, by1 = db_aabb(rb, positions, fp_rotations, geom_db)
                ox = min(ax1, bx1) - max(ax0, bx0) + gap
                oy = min(ay1, by1) - max(ay0, by0) + gap
                if ox <= 0 or oy <= 0:
                    continue
                if not sat_overlap_check(
                        db_polygon_world(ra, positions, fp_rotations, geom_db),
                        db_polygon_world(rb, positions, fp_rotations, geom_db), gap):
                    continue

                changed = True
                total_pushes += 1

                if ox < oy - 0.05:
                    push = ox
                    d = 1.0 if positions[rb][0] >= positions[ra][0] else -1.0
                    if pa < pb:
                        x, y = positions[rb]; positions[rb] = (x + d * push, y)
                        _clamp_to_board(rb, positions, fp_rotations, geom_db, board_bbox)
                    elif pb < pa:
                        x, y = positions[ra]; positions[ra] = (x - d * push, y)
                        _clamp_to_board(ra, positions, fp_rotations, geom_db, board_bbox)
                    else:
                        half = push / 2.0
                        x, y = positions[ra]; positions[ra] = (x - d * half, y)
                        x, y = positions[rb]; positions[rb] = (x + d * half, y)
                        _clamp_to_board(ra, positions, fp_rotations, geom_db, board_bbox)
                        _clamp_to_board(rb, positions, fp_rotations, geom_db, board_bbox)
                elif oy < ox - 0.05:
                    push = oy
                    d = 1.0 if positions[rb][1] >= positions[ra][1] else -1.0
                    if pa < pb:
                        x, y = positions[rb]; positions[rb] = (x, y + d * push)
                        _clamp_to_board(rb, positions, fp_rotations, geom_db, board_bbox)
                    elif pb < pa:
                        x, y = positions[ra]; positions[ra] = (x, y - d * push)
                        _clamp_to_board(ra, positions, fp_rotations, geom_db, board_bbox)
                    else:
                        half = push / 2.0
                        x, y = positions[ra]; positions[ra] = (x, y - d * half)
                        x, y = positions[rb]; positions[rb] = (x, y + d * half)
                        _clamp_to_board(ra, positions, fp_rotations, geom_db, board_bbox)
                        _clamp_to_board(rb, positions, fp_rotations, geom_db, board_bbox)
                else:
                    push = ox  # near tie: prefer X
                    d = 1.0 if positions[rb][0] >= positions[ra][0] else -1.0
                    if pa < pb:
                        x, y = positions[rb]; positions[rb] = (x + d * push, y)
                        _clamp_to_board(rb, positions, fp_rotations, geom_db, board_bbox)
                    elif pb < pa:
                        x, y = positions[ra]; positions[ra] = (x - d * push, y)
                        _clamp_to_board(ra, positions, fp_rotations, geom_db, board_bbox)
                    else:
                        half = push / 2.0
                        x, y = positions[ra]; positions[ra] = (x - d * half, y)
                        x, y = positions[rb]; positions[rb] = (x + d * half, y)
                        _clamp_to_board(ra, positions, fp_rotations, geom_db, board_bbox)
                        _clamp_to_board(rb, positions, fp_rotations, geom_db, board_bbox)

        if not changed:
            break

    return total_pushes, iteration + 1


# ── DISTANCE MEASUREMENT ──────────────────────────────────────────────────────

def measure_dist(ref_a, ref_b, net_hint, positions, local_pads, fp_rotations, geom_db=None):
    def world_pad_pos(ref, net, pad_idx):
        lx, ly = local_pads[ref][net][pad_idx]
        wx, wy = rot2d(lx, ly, fp_rotations[ref])
        cx, cy = positions[ref]
        return (cx + wx, cy + wy)

    def min_dist_nets(net, ra, rb):
        pads_a = local_pads.get(ra, {}).get(net, [])
        pads_b = local_pads.get(rb, {}).get(net, [])
        best = None
        for i in range(len(pads_a)):
            ax, ay = world_pad_pos(ra, net, i)
            for k in range(len(pads_b)):
                bx, by = world_pad_pos(rb, net, k)
                d = math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)
                if best is None or d < best:
                    best = d
        return best

    if net_hint:
        in_a = net_hint in local_pads.get(ref_a, {})
        in_b = net_hint in local_pads.get(ref_b, {})
        if in_a and in_b:
            return min_dist_nets(net_hint, ref_a, ref_b), "net_hint", net_hint
        fallback_method = "hint-miss"
    else:
        fallback_method = "pad"

    shared  = set(local_pads.get(ref_a, {}).keys()) & set(local_pads.get(ref_b, {}).keys())
    best_d  = None
    best_net = None
    for net in shared:
        d = min_dist_nets(net, ref_a, ref_b)
        if d is not None and (best_d is None or d < best_d):
            best_d   = d
            best_net = net

    if best_d is not None:
        return best_d, fallback_method, best_net

    # No shared net — use polygon edge-to-edge distance when DB is available
    # (more physically meaningful than centroid-to-centroid for large bodies).
    if geom_db:
        poly_a = db_polygon_world(ref_a, positions, fp_rotations, geom_db)
        poly_b = db_polygon_world(ref_b, positions, fp_rotations, geom_db)
        return polygon_min_distance(poly_a, poly_b), "poly-edge", None

    cx_a, cy_a = positions[ref_a]
    cx_b, cy_b = positions[ref_b]
    return math.sqrt((cx_a - cx_b) ** 2 + (cy_a - cy_b) ** 2), "ctr", None


def format_tag(method, net_hint, net_used):
    if method == "net_hint":
        return f"[net:{net_used}]"
    if method == "hint-miss":
        return f"[hint-miss:{net_hint}->{net_used or 'ctr'}]"
    if method == "pad":
        return f"[pad:{net_used}]" if net_used else "[pad]"
    if method == "poly-edge":
        return "[poly-edge]"
    return "[ctr]"


# ── REPORT ────────────────────────────────────────────────────────────────────

def build_report(before, after, fp_map, live, frozen_refs,
                 positions=None, fp_rotations=None, geom_db=None):
    lines = [
        "PLACEMENT BY PROXIMITY RULES REPORT",
        f"Mode: {'LIVE (PCB saved)' if live else 'DRY-RUN (no save)'}",
        "Algorithm: sequential greedy face-group placement (v2)",
        f"Face gap: {FACE_GAP_MM}mm  Courtyard gap: {COURTYARD_GAP_MM}mm  Overlap iters: {OVERLAP_ITERS}",
        f"Frozen (all rules pass, not moved): {len(frozen_refs)} component(s)",
        "Method: [net:X]=hint used; [hint-miss:X->Y]=hint fallback; [pad:X]=min shared; [ctr]=centroid",
        "=" * 130,
        "",
        f"  {'Status':<6}  {'ref_A':<16}  {'ref_B':<14}  {'before':>8}  {'after':>8}  {'max':>5}  {'delta':>7}  reason",
        "  " + "-" * 126,
    ]

    n_pass     = 0
    n_fail     = 0
    n_miss     = 0
    n_improved = 0
    n_frozen   = 0
    fails      = []

    for ref_a, ref_b, max_dist, _rtype, reason, net_hint in RULES:
        if ref_a not in fp_map:
            lines.append(f"  MISS    {ref_a:<16}  {'(not in PCB)':<14}  {'':>8}  {'':>8}  {max_dist:>4}mm  {'':>7}  {reason}")
            n_miss += 1
            continue
        if ref_b not in fp_map:
            lines.append(f"  MISS    {ref_a:<16}  {(ref_b + ' ?'):<14}  {'':>8}  {'':>8}  {max_dist:>4}mm  {'':>7}  {reason}")
            n_miss += 1
            continue

        key = (ref_a, ref_b)
        d_before, _bm, _bn = before[key]
        d_after, method, net_used = after[key]

        tag    = format_tag(method, net_hint, net_used)
        status = "PASS" if d_after <= max_dist else "FAIL"
        delta  = d_after - d_before
        delta_str = f"{'+' if delta >= 0 else ''}{delta:.1f}"

        frozen_marker = " [frozen]" if ref_a in frozen_refs else ""

        if status == "FAIL":
            n_fail += 1
            fails.append((ref_a, ref_b, d_after, max_dist, reason, method, net_hint, net_used))
        else:
            n_pass += 1
            if ref_a in frozen_refs:
                n_frozen += 1

        if delta < -0.05:
            n_improved += 1

        lines.append(
            f"  {status:<6}  {ref_a:<16}  {ref_b:<14}  {d_before:>7.1f}mm  {d_after:>7.1f}mm  {max_dist:>4}mm"
            f"  {delta_str:>6}mm  {reason}  {tag}{frozen_marker}"
        )

    lines += [
        "",
        "=" * 130,
        f"  PASS: {n_pass}   FAIL: {n_fail}   MISSING: {n_miss}   IMPROVED: {n_improved}   FROZEN: {n_frozen}",
    ]

    if fails:
        lines += ["", "  FAIL summary (sorted by excess distance, worst first):"]
        for ref_a, ref_b, d_after, max_dist, reason, method, net_hint, net_used in sorted(
            fails, key=lambda x: x[2] - x[3], reverse=True
        ):
            tag    = format_tag(method, net_hint, net_used)
            excess = d_after - max_dist
            lines.append(
                f"    {ref_a} -> {ref_b}: {d_after:.1f}mm (max {max_dist}mm, over by {excess:.1f}mm) -- {reason}  {tag}"
            )

    # Courtyard overlap audit — polygon-accurate SAT check on ALL component pairs.
    # No pairs are excluded: every courtyard represents a real physical object.
    if positions and fp_rotations and geom_db is not None:
        lines += ["", "  COURTYARD OVERLAP AUDIT (polygon-accurate SAT, all pairs):"]
        overlap_pairs = []
        all_pos_refs = sorted(positions.keys())
        for i in range(len(all_pos_refs)):
            for j in range(i + 1, len(all_pos_refs)):
                ra, rb = all_pos_refs[i], all_pos_refs[j]
                pa = db_polygon_world(ra, positions, fp_rotations, geom_db)
                pb = db_polygon_world(rb, positions, fp_rotations, geom_db)
                if sat_overlap_check(pa, pb, 0.0):
                    overlap_pairs.append((ra, rb))
        if overlap_pairs:
            for ra, rb in overlap_pairs:
                lines.append(f"    OVERLAP: {ra} <-> {rb}")
            lines.append(f"    Total: {len(overlap_pairs)} overlapping pair(s)")
        else:
            lines.append("    None — all courtyards clear of non-rule-pair overlaps.")

    return "\n".join(lines), n_fail


# ── MAIN ──────────────────────────────────────────────────────────────────────

def run_placement_pass(pass_num, positions, world_pads, local_pads, fp_rotations,
                       fp_map, movable_set, locked_set, anchor_only_refs, cumulative_frozen,
                       geom_db=None, targeted=False, board_bbox=None):
    """Execute one placement pass. Returns (after_distances, frozen_refs_this_pass)."""
    print(f"\n{'─'*20} PASS {pass_num} {'─'*20}")

    current = {}
    for ref_a, ref_b, max_dist, _rtype, reason, net_hint in RULES:
        if ref_a in fp_map and ref_b in fp_map:
            current[(ref_a, ref_b)] = measure_dist(ref_a, ref_b, net_hint, positions, local_pads, fp_rotations, geom_db)

    frozen_refs = compute_frozen_refs(current, movable_set, locked_set, positions, world_pads, local_pads, fp_rotations, geom_db=geom_db, board_bbox=board_bbox)
    new_frozen  = frozen_refs - cumulative_frozen
    print(f"  Frozen: {len(frozen_refs)} total, {len(new_frozen)} newly frozen this pass")
    if new_frozen:
        print(f"  Newly frozen: {sorted(new_frozen)}")

    print("  Repositioning root anchors toward locked satellites...")
    # Include no_face_group_refs: refs whose rules have net_hint=None (face groups skip them)
    # but that are themselves anchors for satellite groups. They need the same repositioning
    # treatment as anchor_only_refs.
    place_root_anchors(anchor_only_refs | (NO_FACE_GROUP_REFS & set(fp_map)), positions, fp_map, locked_set, current,
                       fp_rotations=fp_rotations, geom_db=geom_db or {})

    locked_positions = {r: positions[r] for r in locked_set if r in positions}
    face_groups   = build_face_groups(RULES, positions, world_pads, movable_set, fp_rotations, geom_db,
                                      board_bbox=board_bbox, locked_positions=locked_positions)
    ranked_groups = rank_face_groups(face_groups, movable_set)

    placed_refs = (set(locked_set)
                   | {r for r in fp_map if is_no_force(r)}
                   | (anchor_only_refs & set(fp_map))
                   | (NO_FACE_GROUP_REFS & set(fp_map))
                   | frozen_refs)

    active = [
        m[0] for g in ranked_groups for m in g['members']
        if m[0] in movable_set and m[0] not in placed_refs
    ]
    print(f"  Face groups: {len(ranked_groups)}, active members to place: {len(active)}")
    # Debug: trace why L1 is not placed
    for _dbg in _DBG_REFS:
        _in_locked = _dbg in locked_set
        _in_noforce = _dbg in {r for r in fp_map if is_no_force(r)}
        _in_anchor_only = _dbg in (anchor_only_refs & set(fp_map))
        _in_noface = _dbg in (NO_FACE_GROUP_REFS & set(fp_map))
        _in_frozen = _dbg in frozen_refs
        _in_movable = _dbg in movable_set
        _groups = [(g['anchor'], g['face_side']) for g in ranked_groups if any(m[0] == _dbg for m in g['members'])]
        print(f"  [DBG placed_refs check] {_dbg}: locked={_in_locked} noforce={_in_noforce} "
              f"anchor_only={_in_anchor_only} noface={_in_noface} frozen={_in_frozen} movable={_in_movable} "
              f"in_placed_refs={_dbg in placed_refs} face_groups={_groups}")

    pending       = collections.deque(ranked_groups)
    max_deferrals = len(pending) * 3
    deferrals     = 0

    while pending:
        group  = pending.popleft()
        anchor = group['anchor']

        if anchor in movable_set and anchor not in placed_refs:
            if deferrals < max_deferrals:
                pending.append(group)
                deferrals += 1
                continue
            print(f"  WARNING: anchor {anchor} never placed, skipping group")
            continue

        members_str = ', '.join(
            m[0] for m in group['members']
            if m[0] in movable_set and m[0] not in placed_refs
        )
        if members_str:
            print(f"  Placing {anchor}/{group['face_side']}: [{members_str}]")

        place_face_group(group, positions, world_pads, local_pads, fp_rotations, movable_set, placed_refs,
                         geom_db=geom_db, locked_set=locked_set, board_bbox=board_bbox)
        deferrals = 0

    # Exclude anchor_only_refs from the overlap sweep. These are the IC anchors whose
    # satellites must stay near them (U1, U2, U3, IC1, IC2, etc.). If they drift during
    # resolve_overlaps, their satellites end up far from them. NO_FACE_GROUP_REFS (U5/U6/U7/U8)
    # are intentionally left in effective_movable: they and their own satellites can drift
    # together, maintaining relative proximity even if pushed as a cluster.
    _intermediate_stable = {'L1'} & set(fp_map)
    anchor_stable     = (anchor_only_refs & set(fp_map)) | _intermediate_stable
    effective_movable = movable_set - frozen_refs - anchor_stable

    # Intra-group stacking separation: resolve_overlaps can nudge satellites in the
    # stacking direction, leaving adjacent face-group members fractionally overlapping.
    # Walk each group sorted by stacking coordinate and push pairs apart just enough.
    for group in ranked_groups:
        face_dir_g     = norm2d(*group['face_dir'])
        stacking_dir_g = perp2d(*face_dir_g)
        grp_refs = [m[0] for m in group['members'] if m[0] in positions]
        if len(grp_refs) < 2:
            continue
        mdata = []
        for ref in grp_refs:
            cx_m, cy_m = positions[ref]
            ref_w = db_polygon_world(ref, positions, fp_rotations, geom_db or {})
            stk_coords = [v[0]*stacking_dir_g[0] + v[1]*stacking_dir_g[1] for v in ref_w]
            stk_m      = cx_m*stacking_dir_g[0] + cy_m*stacking_dir_g[1]
            stk_half_m = (max(stk_coords) - min(stk_coords)) / 2.0
            mdata.append({'ref': ref, 'stk': stk_m, 'stk_half': stk_half_m,
                          'movable': ref in effective_movable})
        mdata.sort(key=lambda m: m['stk'])
        for i in range(1, len(mdata)):
            prev = mdata[i-1]
            curr = mdata[i]
            required = prev['stk'] + prev['stk_half'] + COURTYARD_GAP_MM + curr['stk_half']
            if curr['stk'] < required:
                delta = required - curr['stk']
                if prev['movable'] and curr['movable']:
                    # Split displacement: move each by half to minimise excursion in either direction
                    half = delta / 2.0
                    prev['stk'] -= half
                    sx, sy = positions[prev['ref']]
                    positions[prev['ref']] = (sx - half * stacking_dir_g[0],
                                              sy - half * stacking_dir_g[1])
                    curr['stk'] += half
                    sx, sy = positions[curr['ref']]
                    positions[curr['ref']] = (sx + half * stacking_dir_g[0],
                                              sy + half * stacking_dir_g[1])
                elif curr['movable']:
                    curr['stk'] += delta
                    sx, sy = positions[curr['ref']]
                    positions[curr['ref']] = (sx + delta * stacking_dir_g[0],
                                              sy + delta * stacking_dir_g[1])
                elif prev['movable']:
                    prev['stk'] -= delta
                    sx, sy = positions[prev['ref']]
                    positions[prev['ref']] = (sx - delta * stacking_dir_g[0],
                                              sy - delta * stacking_dir_g[1])

    # Anchor clearance pass: push satellites out of locked/anchor-stable courtyards
    # after the stacking separation, so any stacking push into an anchor is corrected.
    immovable = (locked_set | anchor_stable) & set(positions)
    for ia in sorted(immovable):
        if ia not in positions:
            continue
        ax0, ay0, ax1, ay1 = db_aabb(ia, positions, fp_rotations, geom_db or {})
        ia_poly = db_polygon_world(ia, positions, fp_rotations, geom_db or {})
        for sa in effective_movable:
            if sa not in positions:
                continue
            sx0, sy0, sx1, sy1 = db_aabb(sa, positions, fp_rotations, geom_db or {})
            ox = min(ax1, sx1) - max(ax0, sx0) + COURTYARD_GAP_MM
            oy = min(ay1, sy1) - max(ay0, sy0) + COURTYARD_GAP_MM
            if ox <= 0 or oy <= 0:
                continue
            if not sat_overlap_check(ia_poly,
                                     db_polygon_world(sa, positions, fp_rotations, geom_db or {}),
                                     COURTYARD_GAP_MM):
                continue
            sx, sy = positions[sa]
            acx = (ax0 + ax1) / 2.0
            acy = (ay0 + ay1) / 2.0
            if ox < oy:
                direction = 1.0 if sx >= acx else -1.0
                positions[sa] = (sx + direction * ox, sy)
            else:
                direction = 1.0 if sy >= acy else -1.0
                positions[sa] = (sx, sy + direction * oy)
            if sa in _DBG_REFS or ia in _DBG_REFS:
                print(f"  [DBG anchor-clear] pushed {sa} away from {ia}: "
                      f"ox={ox:.3f} oy={oy:.3f} → ({positions[sa][0]:.3f},{positions[sa][1]:.3f})")

    for _dbr in _DBG_REFS:
        if _dbr in positions:
            print(f"  [DBG post-anchor-clear] {_dbr}: pos={positions[_dbr]}")

    # Column/row normalization: snap all face-group members to a uniform face-direction
    # coordinate so they form a clean column (left/right groups) or row (up/down groups).
    # face_comp_col = anchor_edge + gap + max(sat_near_half) across all group members.
    # This puts every member's near courtyard edge at the same distance from the anchor,
    # matching the designer's intent for tidy bypass-cap columns.
    print("  Normalizing face groups to uniform columns/rows...")
    for group in ranked_groups:
        anchor_n   = group['anchor']
        face_dir_n = norm2d(*group['face_dir'])
        if anchor_n not in positions:
            continue

        # Anchor far-edge projection in face_dir (same formula as place_face_group)
        anchor_cx_n, anchor_cy_n = positions[anchor_n]
        anchor_edge_n = anchor_cx_n * face_dir_n[0] + anchor_cy_n * face_dir_n[1]
        if geom_db and anchor_n in geom_db and geom_db[anchor_n].get("courtyard"):
            a_cyd_n = geom_db[anchor_n]["courtyard"]
            a_local_n = a_cyd_n.get("polygon") or _bbox_corners(a_cyd_n["local_bbox"])
            a_rot_n = fp_rotations.get(anchor_n, 0.0)
            anchor_edge_n += max(
                rot2d(lx, ly, a_rot_n)[0] * face_dir_n[0] + rot2d(lx, ly, a_rot_n)[1] * face_dir_n[1]
                for lx, ly in a_local_n
            )

        # Refs excluded from normalization: manual list + auto (any ref_b in RULES).
        # Excluded refs are skipped in both the near_halves computation and the snap,
        # so they don't inflate the column position and don't get forcibly snapped.
        no_norm = NO_NORMALIZE_REFS | NO_NORMALIZE_AUTO

        # sat_near_half for each normalizable group member at its current rotation
        near_halves = []
        for ref_m, *_ in group['members']:
            if ref_m not in positions or ref_m in no_norm:
                continue
            rot_m = fp_rotations.get(ref_m, 0.0)
            if geom_db and ref_m in geom_db and geom_db[ref_m].get("courtyard"):
                s_cyd_n = geom_db[ref_m]["courtyard"]
                s_local_n = s_cyd_n.get("polygon") or _bbox_corners(s_cyd_n["local_bbox"])
                face_projs = [rot2d(lx2, ly2, rot_m)[0] * face_dir_n[0] +
                              rot2d(lx2, ly2, rot_m)[1] * face_dir_n[1]
                              for lx2, ly2 in s_local_n]
                near_halves.append(-min(face_projs))
            else:
                near_halves.append(1.0)

        if not near_halves:
            continue

        face_comp_col = anchor_edge_n + COURTYARD_GAP_MM + max(near_halves)

        # Snap each normalizable movable member to the uniform column/row face coordinate
        for ref_m, *_ in group['members']:
            if ref_m not in positions or ref_m not in effective_movable or ref_m in no_norm:
                continue
            cx_m, cy_m   = positions[ref_m]
            current_fc   = cx_m * face_dir_n[0] + cy_m * face_dir_n[1]
            delta_fc     = face_comp_col - current_fc
            positions[ref_m] = (cx_m + delta_fc * face_dir_n[0],
                                cy_m + delta_fc * face_dir_n[1])
            if ref_m in _DBG_REFS:
                print(f"  [DBG col-norm] {ref_m}: face_comp_col={face_comp_col:.3f} "
                      f"delta={delta_fc:.3f} → ({positions[ref_m][0]:.3f},{positions[ref_m][1]:.3f})")

    for _dbr in _DBG_REFS:
        if _dbr in positions:
            print(f"  [DBG post-col-norm] {_dbr}: pos={positions[_dbr]}")

    # Post-normalization stacking separation: bringing members to a shared column/row
    # collapses any face-direction separation between them, so the y-gap that existed
    # when members had different face_comp values may now be a 2D overlap.
    # Walk each group again and push adjacent members apart in the stacking direction.
    for group in ranked_groups:
        face_dir_g     = norm2d(*group['face_dir'])
        stacking_dir_g = perp2d(*face_dir_g)
        grp_refs = [m[0] for m in group['members'] if m[0] in positions]
        if len(grp_refs) < 2:
            continue
        mdata = []
        for ref in grp_refs:
            cx_m, cy_m = positions[ref]
            ref_w = db_polygon_world(ref, positions, fp_rotations, geom_db or {})
            stk_coords = [v[0]*stacking_dir_g[0] + v[1]*stacking_dir_g[1] for v in ref_w]
            stk_m      = cx_m*stacking_dir_g[0] + cy_m*stacking_dir_g[1]
            stk_half_m = (max(stk_coords) - min(stk_coords)) / 2.0
            mdata.append({'ref': ref, 'stk': stk_m, 'stk_half': stk_half_m,
                          'movable': ref in effective_movable})
        mdata.sort(key=lambda m: m['stk'])
        for i in range(1, len(mdata)):
            prev = mdata[i-1]
            curr = mdata[i]
            required = prev['stk'] + prev['stk_half'] + COURTYARD_GAP_MM + curr['stk_half']
            if curr['stk'] < required:
                delta = required - curr['stk']
                if prev['movable'] and curr['movable']:
                    half = delta / 2.0
                    prev['stk'] -= half
                    sx, sy = positions[prev['ref']]
                    positions[prev['ref']] = (sx - half * stacking_dir_g[0],
                                              sy - half * stacking_dir_g[1])
                    curr['stk'] += half
                    sx, sy = positions[curr['ref']]
                    positions[curr['ref']] = (sx + half * stacking_dir_g[0],
                                              sy + half * stacking_dir_g[1])
                elif curr['movable']:
                    curr['stk'] += delta
                    sx, sy = positions[curr['ref']]
                    positions[curr['ref']] = (sx + delta * stacking_dir_g[0],
                                              sy + delta * stacking_dir_g[1])
                elif prev['movable']:
                    prev['stk'] -= delta
                    sx, sy = positions[prev['ref']]
                    positions[prev['ref']] = (sx - delta * stacking_dir_g[0],
                                              sy - delta * stacking_dir_g[1])

    # SAT cross-check within each group: the 1D stacking push above uses 1D half-extents
    # and can miss cases where 2D polygon overlaps remain (e.g. asymmetric courtyards or
    # col-norm collapsing face-direction separation). Check every same-group pair with SAT
    # and push apart in the stacking direction if they still overlap.
    for group in ranked_groups:
        stacking_dir_g = perp2d(*norm2d(*group['face_dir']))
        grp_refs = [m[0] for m in group['members'] if m[0] in positions]
        for _gi in range(len(grp_refs)):
            for _gj in range(_gi + 1, len(grp_refs)):
                _ra, _rb = grp_refs[_gi], grp_refs[_gj]
                _pa = db_polygon_world(_ra, positions, fp_rotations, geom_db or {})
                _pb = db_polygon_world(_rb, positions, fp_rotations, geom_db or {})
                if not sat_overlap_check(_pa, _pb, COURTYARD_GAP_MM):
                    continue
                _stk_a  = positions[_ra][0]*stacking_dir_g[0] + positions[_ra][1]*stacking_dir_g[1]
                _stk_b  = positions[_rb][0]*stacking_dir_g[0] + positions[_rb][1]*stacking_dir_g[1]
                _half_a = (max(v[0]*stacking_dir_g[0]+v[1]*stacking_dir_g[1] for v in _pa)
                          - min(v[0]*stacking_dir_g[0]+v[1]*stacking_dir_g[1] for v in _pa)) / 2.0
                _half_b = (max(v[0]*stacking_dir_g[0]+v[1]*stacking_dir_g[1] for v in _pb)
                          - min(v[0]*stacking_dir_g[0]+v[1]*stacking_dir_g[1] for v in _pb)) / 2.0
                # Ensure _ra has smaller stk (push _rb in positive stk direction)
                if _stk_a > _stk_b:
                    _ra, _rb = _rb, _ra
                    _stk_a, _stk_b = _stk_b, _stk_a
                    _half_a, _half_b = _half_b, _half_a
                _required = _stk_a + _half_a + COURTYARD_GAP_MM + _half_b
                _delta    = (_required - _stk_b)
                if _delta <= 0:
                    continue
                _mov_a = _ra in effective_movable
                _mov_b = _rb in effective_movable
                if _mov_a and _mov_b:
                    _h = _delta / 2.0
                    sx, sy = positions[_ra]; positions[_ra] = (sx - _h*stacking_dir_g[0], sy - _h*stacking_dir_g[1])
                    sx, sy = positions[_rb]; positions[_rb] = (sx + _h*stacking_dir_g[0], sy + _h*stacking_dir_g[1])
                elif _mov_b:
                    sx, sy = positions[_rb]; positions[_rb] = (sx + _delta*stacking_dir_g[0], sy + _delta*stacking_dir_g[1])
                elif _mov_a:
                    sx, sy = positions[_ra]; positions[_ra] = (sx - _delta*stacking_dir_g[0], sy - _delta*stacking_dir_g[1])

    _n_retried = _retry_violated_rules(RULES, positions, local_pads, fp_rotations, geom_db, effective_movable)
    # Broad movable set for the global pass: includes anchor_stable refs (root IC anchors)
    # so they can be pushed clear of KiCad-locked components (priority 0) during the global
    # overlap pass. In global_no_overlap_pass, anchor_stable refs are priority 1 (above
    # satellites at priority 2) so they yield to locked obstacles but never to satellites.
    broad_movable = movable_set
    print("  Global overlap elimination...")
    global_no_overlap_pass(positions, fp_rotations, broad_movable, geom_db or {}, board_bbox=board_bbox)
    _resolve_cross_group_overlaps(RULES, ranked_groups, positions, local_pads,
                                   fp_rotations, geom_db, effective_movable,
                                   board_bbox=board_bbox)

    for _dbr in _DBG_REFS:
        if _dbr in positions:
            print(f"  [DBG post-global-overlap] {_dbr}: pos={positions[_dbr]}")
            if "J_PWR_IN1" in positions:
                pa = db_polygon_world(_dbr, positions, fp_rotations, geom_db or {})
                pb = db_polygon_world("J_PWR_IN1", positions, fp_rotations, geom_db or {})
                still_overlaps = sat_overlap_check(pa, pb, 0.0)
                print(f"  [DBG] {_dbr}<->J_PWR_IN1 gap=0 overlap: {still_overlaps}")

    # Post-global anchor-overlap correction.
    # The global pass skips rule pairs (intentional proximity). But a third-party push can
    # slide a satellite sideways into its anchor's polygon corner — the pair is then skipped
    # forever. Fix: for every face-group satellite that now overlaps its anchor (when it
    # shouldn't), push it further in the face direction until clear.
    for group in ranked_groups:
        anchor   = group['anchor']
        face_dir = group['face_dir']   # cardinal unit vector, e.g. (0,1) for "down"
        fd_x, fd_y = face_dir
        for ref_a, _net, _max, _rtype in group['members']:
            if ref_a not in positions or anchor not in positions:
                continue
            if ref_a in locked_set or ref_a not in movable_set:
                continue
            if ref_a in frozen_refs:
                continue
            poly_sat = db_polygon_world(ref_a,  positions, fp_rotations, geom_db or {})
            poly_anc = db_polygon_world(anchor, positions, fp_rotations, geom_db or {})
            if not sat_overlap_check(poly_sat, poly_anc, 0.0):
                continue
            # Compute overlap in the face direction axis and push satellite further out.
            ax0, ay0, ax1, ay1 = db_aabb(anchor, positions, fp_rotations, geom_db or {})
            sx0, sy0, sx1, sy1 = db_aabb(ref_a,  positions, fp_rotations, geom_db or {})
            ox = min(ax1, sx1) - max(ax0, sx0) + COURTYARD_GAP_MM
            oy = min(ay1, sy1) - max(ay0, sy0) + COURTYARD_GAP_MM
            push = abs(ox * fd_x) + abs(oy * fd_y)
            if push <= 0:
                push = COURTYARD_GAP_MM  # minimum nudge
            cx, cy = positions[ref_a]
            positions[ref_a] = (cx + fd_x * push, cy + fd_y * push)
            _clamp_to_board(ref_a, positions, fp_rotations, geom_db or {}, board_bbox)
            if ref_a in _DBG_REFS or anchor in _DBG_REFS:
                print(f"  [anchor-overlap-fix] {ref_a} pushed {push:.3f}mm {group['face_side']} "
                      f"to clear {anchor} → {positions[ref_a]}")

    after = {}
    for ref_a, ref_b, max_dist, _rtype, reason, net_hint in RULES:
        if ref_a in fp_map and ref_b in fp_map:
            after[(ref_a, ref_b)] = measure_dist(ref_a, ref_b, net_hint, positions, local_pads, fp_rotations, geom_db)

    n_fail = sum(1 for ref_a, ref_b, max_dist, _, _, _ in RULES
                 if (ref_a, ref_b) in after and after[(ref_a, ref_b)][0] > max_dist)
    print(f"  Pass {pass_num} result: {n_fail} rule(s) still failing")

    return after, frozen_refs, new_frozen, n_fail


def main():
    parser = argparse.ArgumentParser(description="Sequential greedy face-group placement by proximity rules.")
    parser.add_argument("--pcb",    metavar="PATH", help="Override PCB_FILE path.")
    parser.add_argument("--report", metavar="PATH", help="Override REPORT_FILE path.")
    parser.add_argument("--output", metavar="PATH",
                        help="Save placed PCB to this path instead of overwriting the input. "
                             "Use when the input is a scatter file you want to preserve.")
    parser.add_argument("--live",   action="store_true", help="Apply positions/rotations and save PCB (default: dry-run).")
    parser.add_argument("--passes", type=int, default=5,  help="Maximum placement passes (default: 5).")
    parser.add_argument("--refs",   type=str, default="",
                        help="Comma-separated list of refs to place. All others treated as fixed for this run.")
    args = parser.parse_args()

    global PCB_FILE, REPORT_FILE, BACKUPS_DIR
    if args.pcb:    PCB_FILE    = args.pcb
    if args.report: REPORT_FILE = args.report

    geom_db = load_geometry_db(GEOM_DB_FILE)

    global NO_ROTATE_IN_OVERLAP
    NO_ROTATE_IN_OVERLAP = {
        ref for ref, entry in (geom_db or {}).items()
        if entry.get("rotation_symmetry") == "none"
    }

    print(f"Loading PCB: {PCB_FILE}")
    board, fp_map, positions, world_pads, local_pads, fp_rotations, locked_set = load_board(PCB_FILE)
    board_bbox = get_board_bbox(board)
    if board_bbox:
        print(f"  Board bounds: x=[{board_bbox[0]:.1f}, {board_bbox[2]:.1f}] y=[{board_bbox[1]:.1f}, {board_bbox[3]:.1f}] mm")

    movable_set    = build_movable_set(fp_map, locked_set)
    locked_count   = len(locked_set)
    no_force_count = sum(1 for ref in fp_map if is_no_force(ref) and ref not in locked_set)
    print(f"  Footprints: {len(fp_map)} total, {len(movable_set)} movable, {no_force_count} no-force, {locked_count} locked")

    if args.refs:
        target_refs = {r.strip() for r in args.refs.split(",") if r.strip()}
        missing = target_refs - set(fp_map)
        if missing:
            print(f"  WARNING: --refs contains refs not in PCB: {sorted(missing)}")
        movable_set = movable_set & target_refs
        print(f"  Targeted mode: placing only {sorted(movable_set)}")

    satellite_refs   = {rule[0] for rule in RULES}
    anchor_only_refs = {rule[1] for rule in RULES if rule[1] not in satellite_refs}

    # In targeted mode: single pass, skip root-anchor repositioning (anchors are fixed by design)
    targeted = bool(args.refs)
    max_passes = 1 if targeted else args.passes

    # Original state snapshot — fixed for reporting regardless of pass count
    before = {}
    for ref_a, ref_b, max_dist, _rtype, reason, net_hint in RULES:
        if ref_a in fp_map and ref_b in fp_map:
            before[(ref_a, ref_b)] = measure_dist(ref_a, ref_b, net_hint, positions, local_pads, fp_rotations, geom_db)

    cumulative_frozen = set()
    after  = before.copy()
    n_fail = len(RULES)

    for pass_num in range(1, max_passes + 1):
        after, frozen_this_pass, new_frozen, n_fail = run_placement_pass(
            pass_num, positions, world_pads, local_pads, fp_rotations,
            fp_map, movable_set, locked_set,
            set() if targeted else anchor_only_refs,
            cumulative_frozen,
            geom_db=geom_db,
            targeted=targeted,
            board_bbox=board_bbox,
        )
        cumulative_frozen = frozen_this_pass

        if n_fail == 0:
            print(f"\nAll rules satisfied after pass {pass_num} — stopping.")
            break

        if pass_num > 1 and not new_frozen:
            print(f"\nNo new components frozen in pass {pass_num} — converged, stopping.")
            break

    print(f"\n{'='*50}")
    print(f"Completed {pass_num} pass(es). Building final report...")
    report_text, n_fail = build_report(before, after, fp_map, args.live, cumulative_frozen,
                                        positions=positions, fp_rotations=fp_rotations, geom_db=geom_db)

    pathlib.Path(REPORT_FILE).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(REPORT_FILE).write_text(report_text + "\n", encoding="utf-8")
    print(report_text)
    print(f"\nReport written to: {REPORT_FILE}")

    if args.live:
        print("\nApplying positions, rotations, and saving PCB...")

        for ref in movable_set:
            if ref not in fp_map:
                continue
            fp   = fp_map[ref]
            x, y = positions[ref]
            rot  = fp_rotations[ref]
            fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
            fp.SetOrientation(pcbnew.EDA_ANGLE(float(rot), pcbnew.DEGREES_T))

        def _rm_companion_files(pcb_path: str) -> None:
            stem = str(pathlib.Path(pcb_path).with_suffix(""))
            for ext in (".kicad_pro", ".kicad_prl", ".lck"):
                candidate = stem + ext
                if pathlib.Path(candidate).exists():
                    try:
                        pathlib.Path(candidate).unlink()
                    except OSError:
                        pass
            # Also clean root-dir companions that pcbnew may drop using the stem only
            pcb_dir = pathlib.Path(PCB_FILE).parent
            bare_stem = pathlib.Path(pcb_path).stem
            for ext in (".kicad_pro", ".kicad_prl"):
                candidate = pcb_dir / (bare_stem + ext)
                if candidate.exists():
                    try:
                        candidate.unlink()
                    except OSError:
                        pass

        out_path = args.output if (args.output and args.output != PCB_FILE) else None
        if out_path:
            # --output specified: write to a separate file, leave the input scatter untouched.
            pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            board.Save(out_path)
            _rm_companion_files(out_path)
            print(f"  PCB saved to: {out_path}  (input scatter preserved at {PCB_FILE})")
        else:
            pathlib.Path(BACKUPS_DIR).mkdir(parents=True, exist_ok=True)
            ts          = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            pcb_stem    = pathlib.Path(PCB_FILE).stem
            backup_path = str(pathlib.Path(BACKUPS_DIR) / f"{pcb_stem}_backup_{ts}.kicad_pcb")
            shutil.copy2(PCB_FILE, backup_path)
            print(f"  Backup saved to: {backup_path}")
            board.Save(PCB_FILE)
            print(f"  PCB saved to: {PCB_FILE}")
    else:
        print("\nDRY-RUN: no changes written. Pass --live to apply and save.")

    if n_fail > 0:
        print(f"\nFAILURES REMAIN: {n_fail} rule(s) still violated after placement.")
        sys.exit(1)

    print("\nAll proximity rules satisfied.")
    sys.exit(0)


if __name__ == "__main__":
    main()
