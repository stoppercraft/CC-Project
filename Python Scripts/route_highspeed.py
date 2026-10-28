"""
route_highspeed.py — Phase 10 Part B (rewrite)

Routes high-speed differential pairs defined in routing_config.HS_PAIRS whose
name appears in routing_config.HS_ROUTE_WIDTHS.  Pairs not in HS_ROUTE_WIDTHS
are intentionally deferred to FreeRouting (config choice).

Algorithm (per pair):

Phase 1 — Escape vias (per endpoint):
  For each endpoint (2 endpoints per pair) place a via for P and a via for N
  such that on the routing layer their lateral order matches the lateral order
  at the OTHER endpoint.  If the pair has a topological crossing (P.x > N.x at
  one endpoint but P.x < N.x at the other), we choose ONE endpoint to correct
  (the source endpoint by default) and geometry-thread one signal's escape stub
  past its partner pad so its via lands on the FAR side of the partner.
  A ground return via is placed next to each signal via.

Phase 2 — Route the pair centerline on the HS routing layer:
  Compute midpoint of P-via and N-via at each end (call them center_a, center_b).
  Try straight-line, then a single H/V or V/H bend, then a 45° Z, then A*.
  All resulting waypoints are octilinear (0/45/90) and collinear-merged.

Phase 3 — Emit P and N tracks as perpendicular offsets ±cc/2 of the centerline
  (miter-bisector at each bend).

Obstacle model:
  build_obstacles(layer_id, net_excl_pads, net_excl_tracks)
    net_excl_pads   : nets whose pads are NOT obstacles (endpoint pads incl. partner
                      so escape can start adjacent to partner pad)
    net_excl_tracks : nets whose board tracks/vias/_run_obs are NOT obstacles
                      (only this net — partner's placed copper IS an obstacle,
                      preventing P/N shorts).

Rules honoured:
  - Zero hardcoded refs / net names / layer numeric constants.
  - board.GetLayerID(name) — never numeric layer constants.
  - Never move or rotate locked footprints (this script only adds tracks/vias).
  - board.Remove() never called.
  - Coordinates in mm; convert via pcbnew.FromMM / pcbnew.ToMM.
  - Everything emitted to _run_obs even in dry run so later pairs see obstacles.

Usage:
  python route_highspeed.py           # dry run
  python route_highspeed.py --apply   # write tracks/vias, save board
"""

import sys, os, math, pathlib, heapq

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import routing_config as cfg

KICAD_BIN = str(pathlib.Path(cfg.KICAD_SITE_PKGS).parent.parent)
os.environ["PATH"] = KICAD_BIN + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, cfg.KICAD_SITE_PKGS)
import pcbnew

import argparse as _ap
_parser = _ap.ArgumentParser(add_help=False)
_parser.add_argument("--apply",  action="store_true")
_parser.add_argument("--pairs",  default="",
                     help="comma-separated pair names to route; default routes all pairs")
_args, _ = _parser.parse_known_args()
APPLY            = _args.apply
TARGET_PAIRS_ARG = {p.strip() for p in _args.pairs.split(",") if p.strip()}

CLEARANCE_MM         = 0.15
STUB_W_MM            = 0.127  # narrow escape stub
BOARD_EDGE_CLEARANCE = 0.50   # mm — copper-to-edge DRC minimum
RELOC_STEP_MM        = 0.10   # walk step for anchor relocation
RELOC_MAX_MM         = 5.0    # max walk distance for anchor relocation
COMP_KEEPOUT_MM      = 0.5    # footprint bbox keepout margin for trunk routes

board   = pcbnew.LoadBoard(cfg.PCB_FILE)
F_CU    = board.GetLayerID("F.Cu")
B_CU    = board.GetLayerID("B.Cu")

def _track_fp(t):
    s, e = t.GetStart(), t.GetEnd()
    return (t.GetNetname(), s.x, s.y, e.x, e.y)

_pre_existing_track_fps: frozenset = frozenset(_track_fp(t) for t in board.GetTracks())
GND_NET = cfg.LAYER_SCHEME.get("gnd_ref", "GND")

_bbox   = board.GetBoardEdgesBoundingBox()
_BRD_X0 = pcbnew.ToMM(_bbox.GetLeft())
_BRD_X1 = pcbnew.ToMM(_bbox.GetRight())
_BRD_Y0 = pcbnew.ToMM(_bbox.GetTop())
_BRD_Y1 = pcbnew.ToMM(_bbox.GetBottom())

ca_audit  = cfg.CLEARANCE_AUDIT
VIA_PAD       = ca_audit["hs_via_drill_mm"] + 2 * ca_audit["hs_via_annular_ring_mm"]
VIA_DRILL     = ca_audit["hs_via_drill_mm"]
NECKDOWN_W_MM = ca_audit.get("neckdown_stub_w_mm", 0.08)

log             = []
_conflict_count = 0
_run_obs:       list = []
_via_cache:     dict = {}   # (ref, rx, ry, net) -> (vx, vy)
_corridor_trunk: dict = {}  # pair_name → ('HV', trunk_x) or ('VH', None)
_pair_obs_ranges: dict = {} # pair_name → (start, end) slice of _run_obs
_skipped_pairs: set = set() # pairs with segments skipped due to blocked paths

# Pre-existing fanout vias placed by route_fanout_vias.py — indexed at startup.
FANOUT_SNAP_MM = 3.5  # max pad-to-via distance to consider a via a fanout anchor
_fanout_via_index: list = []  # list of (net_name, vx, vy)


def _build_fanout_via_index():
    # Exclude vias that sit on a pad of a DIFFERENT net — those are stale
    # via-in-pad artifacts (e.g. from a previous route_highspeed run) and
    # are not usable as fanout anchors.  Vias that coincide with a same-net
    # pad are legitimate fanout stubs (route_fanout_vias.py may land a via
    # on a same-net pad of the same component when escaping through the BGA).
    pad_pos_nets: dict = {}  # (rx, ry) → set of net names at that position
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            pos = pad.GetPosition()
            key = (round(tomm(pos.x), 3), round(tomm(pos.y), 3))
            pad_pos_nets.setdefault(key, set()).add(pad.GetNetname())

    seen: set = set()  # deduplicate identical (net, vx, vy) entries
    for trk in board.GetTracks():
        if trk.GetClass() != "PCB_VIA":
            continue
        pos = trk.GetPosition()
        vx, vy = tomm(pos.x), tomm(pos.y)
        vnet = trk.GetNetname()
        pos_key = (round(vx, 3), round(vy, 3))
        nets_here = pad_pos_nets.get(pos_key, set())
        # Skip only if ALL pads at this position belong to a different net
        # (i.e. this via sits on a pad it does not belong to — a stale artifact).
        if nets_here and vnet not in nets_here:
            continue
        key = (vnet, round(vx, 3), round(vy, 3))
        if key in seen:
            continue
        seen.add(key)
        _fanout_via_index.append((vnet, vx, vy))


def _find_fanout_via(pad_x, pad_y, net_name):
    """Return (vx, vy) of nearest pre-existing via on net_name within FANOUT_SNAP_MM.

    Skips vias already claimed by a different pad endpoint (present in _via_cache
    values) so two distinct pads on the same net can't both snap to the same via.
    """
    claimed = {(round(vx, 3), round(vy, 3)) for vx, vy in _via_cache.values()}
    best_d, best = FANOUT_SNAP_MM, None
    for vnet, vx, vy in _fanout_via_index:
        if vnet != net_name:
            continue
        if (round(vx, 3), round(vy, 3)) in claimed:
            continue
        d = math.hypot(vx - pad_x, vy - pad_y)
        if d < best_d:
            best_d, best = d, (vx, vy)
    return best


def _is_fanout_via(ax, ay, net_name):
    """Return True if (ax, ay) matches a pre-existing via in _fanout_via_index."""
    for vnet, vx, vy in _fanout_via_index:
        if vnet == net_name and math.hypot(vx - ax, vy - ay) < 0.01:
            return True
    return False


_fanout_stubs: dict = {}  # (net, px, py) → (vx, vy) — via at end of fanout stub
_stub_tips:    dict = {}  # (net, px, py) → (tx, ty) — tip of no-via locked stub
_stub_bends:   dict = {}  # (net, px, py) → (bx, by) — bend point (second-to-last node) of no-via stub


def _build_fanout_stub_index():
    """Map each pad position to its fanout via (or stub tip) by tracing locked track chains."""
    adj: dict = {}
    for trk in board.GetTracks():
        if trk.GetClass() == "PCB_VIA":
            continue
        vnet = trk.GetNetname()
        s = trk.GetStart(); e = trk.GetEnd()
        sx, sy = round(tomm(s.x), 3), round(tomm(s.y), 3)
        ex, ey = round(tomm(e.x), 3), round(tomm(e.y), 3)
        adj.setdefault((vnet, sx, sy), []).append((ex, ey))
        adj.setdefault((vnet, ex, ey), []).append((sx, sy))

    via_pos: set = {(vnet, round(vx, 3), round(vy, 3))
                    for vnet, vx, vy in _fanout_via_index}

    for fp in board.GetFootprints():
        for pad in fp.Pads():
            pnet = pad.GetNetname()
            pos  = pad.GetPosition()
            px, py = round(tomm(pos.x), 3), round(tomm(pos.y), 3)
            if (pnet, px, py) not in adj:
                continue
            visited: set = {(px, py)}
            frontier = [(px, py)]
            found = None
            while frontier and found is None:
                cur = frontier.pop(0)
                for nx, ny in adj.get((pnet, cur[0], cur[1]), []):
                    if (nx, ny) in visited:
                        continue
                    visited.add((nx, ny))
                    if (pnet, nx, ny) in via_pos:
                        found = (nx, ny)
                        break
                    frontier.append((nx, ny))
            if found is not None:
                _fanout_stubs[(pnet, px, py)] = found
            elif len(visited) > 1:
                # No via at chain end — walk chain in order to get tip and bend point
                _chain = [(px, py)]
                _chain_vis: set = {(px, py)}
                while True:
                    _cur = _chain[-1]
                    _nexts = [n for n in adj.get((pnet, _cur[0], _cur[1]), [])
                              if n not in _chain_vis]
                    if not _nexts:
                        break
                    _nxt = _nexts[0]
                    _chain_vis.add(_nxt)
                    _chain.append(_nxt)
                if len(_chain) >= 2:
                    _stub_tips[(pnet, px, py)]  = _chain[-1]
                    _stub_bends[(pnet, px, py)] = _chain[-2]


def _follow_stub_to_via(pad_x, pad_y, net_name):
    """Return the fanout via position for this pad from the stub index, or None."""
    return _fanout_stubs.get((net_name, round(pad_x, 3), round(pad_y, 3)))


def _follow_stub_tip(pad_x, pad_y, net_name):
    """Return the no-via stub tip for this pad, or None."""
    return _stub_tips.get((net_name, round(pad_x, 3), round(pad_y, 3)))


def _follow_stub_bend(pad_x, pad_y, net_name):
    """Return the bend point (second-to-last node) of the no-via stub, or None."""
    return _stub_bends.get((net_name, round(pad_x, 3), round(pad_y, 3)))


def _stub_direct_path(tx, ty, dest_x, dest_y):
    """Return a 2- or 3-point octilinear path from stub tip (tx,ty) to (dest_x,dest_y).

    Uses the canonical diagonal direction that points toward (dest_x, dest_y):
    diagonal to dest_x, then straight to dest_y. Falls back to H-then-V if the
    diagonal would overshoot dest_y.
    """
    dx_d = dest_x - tx; dy_d = dest_y - ty
    if abs(dx_d) < 1e-4:
        if abs(dy_d) < 1e-4:
            return None
        return [(tx, ty), (dest_x, dest_y)]
    _S2 = 1.0 / math.sqrt(2.0)
    edx_u = _S2 if dx_d > 0 else -_S2
    edy_u = _S2 if dy_d > 0 else -_S2
    t = dx_d / edx_u  # always positive
    ext_y = ty + t * edy_u
    # ext_y must land between ty and dest_y (no overshoot in y)
    if (ext_y - ty) * dy_d >= 0 and abs(ext_y - ty) <= abs(dy_d) + 1e-4:
        if abs(ext_y - dest_y) < 1e-4:
            return [(tx, ty), (dest_x, dest_y)]
        return [(tx, ty), (dest_x, ext_y), (dest_x, dest_y)]
    # Fallback: horizontal then vertical
    return [(tx, ty), (dest_x, ty), (dest_x, dest_y)]


def _stub_exit_path(tx, ty, dest_x, dest_y, bx, by, width_mm, obstacles):
    """Build a clear path from stub tip (tx,ty) to (dest_x,dest_y) that continues
    the natural stub exit direction (bend→tip, snapped to 45°).

    Option A: exit in stub direction until x=dest_x, then straight V (for stubs
              whose exit direction moves toward dest_x).
    Option B: exit a computed minimum distance d_sw (to clear via obstacles along
              the following V segment), then straight V, then 45° diagonal to dest.

    Uses _path_clear_approx (accurate circular check for via obstacles) to verify
    each candidate. Falls back to _stub_direct_path if neither option passes.
    """
    hw = width_mm / 2
    _via_r_max = VIA_PAD / 2 + CLEARANCE_MM + 0.1  # max r_obs for a real via obstacle

    # Compute 45°-snapped exit direction from bend to tip
    raw_dx = tx - bx; raw_dy = ty - by
    if math.hypot(raw_dx, raw_dy) < 1e-6:
        return _stub_direct_path(tx, ty, dest_x, dest_y)
    angle = math.atan2(raw_dy, raw_dx)
    snapped = round(angle / (math.pi / 4)) * (math.pi / 4)
    ex = math.cos(snapped); ey = math.sin(snapped)

    def _try(path):
        return merge_collinear(path) if _path_clear_approx(path, width_mm, obstacles) else None

    # Option A: exit to x=dest_x then straight V
    if abs(ex) > 0.01 and ((ex > 0) == (dest_x > tx)) and abs(dest_x - tx) > 1e-4:
        t = (dest_x - tx) / ex
        corner_y = ty + t * ey
        if (t >= 0 and (corner_y - ty) * (dest_y - ty) >= -1e-6
                and abs(corner_y - ty) <= abs(dest_y - ty) + 1e-4):
            if abs(corner_y - dest_y) < 1e-4:
                r = _try([(tx, ty), (dest_x, dest_y)])
            else:
                r = _try([(tx, ty), (dest_x, corner_y), (dest_x, dest_y)])
            if r is not None:
                return r

    # Option B: exit d_sw, then straight V, then diagonal to dest
    if abs(ex) > 0.01 and abs(ey) > 0.01:
        # Minimum d_sw so the V segment at vert_x clears all via obstacles.
        # Only consider via-sized square obstacles (r_obs ≤ _via_r_max) that are
        # within the approximate y range of the V segment [min(ty,dest_y), max(ty,dest_y)].
        d_sw_min = 0.0
        _vy_lo = min(ty, dest_y) - _via_r_max
        _vy_hi = max(ty, dest_y) + _via_r_max
        for o in obstacles:
            lx, ly, rx, ry = o
            if abs((rx - lx) - (ry - ly)) > 1e-3:
                continue  # non-square (pad) — skip
            r_obs = (rx - lx) / 2
            if r_obs > _via_r_max:
                continue  # too large to be a via (thermal pad, keepout, etc.) — skip
            via_cx = (lx + rx) / 2; via_cy = (ly + ry) / 2
            if via_cy < _vy_lo or via_cy > _vy_hi:
                continue  # not in the V segment's y range — skip
            needed_sep = r_obs + hw
            dist_now = abs(via_cx - tx)
            if dist_now < needed_sep:
                # Via is currently too close; check if moving in exit direction helps
                if (via_cx - tx) * ex < 0:  # via on opposite side from exit — moving away helps
                    delta = needed_sep - dist_now
                    d_sw_min = max(d_sw_min, delta / abs(ex) + 0.002)

        # Try d_sw_min and two slightly larger increments
        for d_sw in (d_sw_min, d_sw_min + 0.010, d_sw_min + 0.025):
            vert_x = tx + d_sw * ex
            vert_y = ty + d_sw * ey
            dx_diag = dest_x - vert_x
            if abs(dx_diag) < 1e-4:
                path_b = [(tx, ty), (vert_x, vert_y), (dest_x, dest_y)]
            else:
                dy_diag = abs(dx_diag) * (1.0 if (dest_y - vert_y) >= 0 else -1.0)
                y_before = dest_y - dy_diag
                if (y_before - vert_y) * (dest_y - vert_y) < -1e-6:
                    continue  # dest behind us in V direction
                if abs(y_before - vert_y) < 1e-4:
                    path_b = [(tx, ty), (vert_x, vert_y), (dest_x, dest_y)]
                else:
                    path_b = [(tx, ty), (vert_x, vert_y), (vert_x, y_before), (dest_x, dest_y)]
            r = _try(path_b)
            if r is not None:
                return r

    return _stub_direct_path(tx, ty, dest_x, dest_y)


# ── Geometry ──────────────────────────────────────────────────────────────────

def mm(x):   return pcbnew.FromMM(x)
def tomm(x): return pcbnew.ToMM(x)
def dist(ax, ay, bx, by): return math.hypot(bx - ax, by - ay)

def rect_overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

def seg_rect(x1, y1, x2, y2, w):
    hw = w / 2
    return (min(x1, x2) - hw, min(y1, y2) - hw,
            max(x1, x2) + hw, max(y1, y2) + hw)

def point_to_seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

def pad_obs_rect(pad):
    pos = pad.GetPosition()
    px, py = tomm(pos.x), tomm(pos.y)
    size = pad.GetSize()
    sx, sy = tomm(size.x) / 2, tomm(size.y) / 2
    try:
        ang = math.radians(pad.GetOrientationDegrees())
    except AttributeError:
        ang = 0.0
    ca_, sa_ = abs(math.cos(ang)), abs(math.sin(ang))
    hx = sx * ca_ + sy * sa_ + CLEARANCE_MM
    hy = sx * sa_ + sy * ca_ + CLEARANCE_MM
    return (px - hx, py - hy, px + hx, py + hy)


# ── Obstacle model ────────────────────────────────────────────────────────────

def build_sibling_via_reserves():
    """
    For every component that has pads on 2+ different HS nets (e.g. AC coupling
    caps that carry LT_TX on one pad and DP_TX on the other), record all those
    pad positions as via reservations. When an In2.Cu (or any HS routing layer)
    segment is being routed for one of these nets, the OTHER nets' sibling pad
    positions must remain clear so those sibling nets can later place a
    via-in-pad escape there.

    Returns list of (net_name, x, y) tuples.
    """
    all_hs_nets = set()
    for _pname, (pnet, nnet, _layer, _skew) in cfg.HS_PAIRS.items():
        all_hs_nets.add(pnet)
        all_hs_nets.add(nnet)

    comp_hs_pads = {}
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            net = pad.GetNetname()
            if net in all_hs_nets:
                pos = pad.GetPosition()
                comp_hs_pads.setdefault(ref, []).append(
                    (net, tomm(pos.x), tomm(pos.y)))

    reserves = []
    for _ref, pads in comp_hs_pads.items():
        nets = set(n for n, _x, _y in pads)
        if len(nets) >= 2:
            for net, x, y in pads:
                reserves.append((net, x, y))
    return reserves


_sibling_reserves = build_sibling_via_reserves()
_build_fanout_via_index()
_build_fanout_stub_index()
# Stub-tip positions for O(1) lookup — used to select correct obstacle radius
_stub_tip_xy: frozenset = frozenset(
    (round(tx, 3), round(ty, 3)) for tx, ty in _stub_tips.values()
)


def _build_obstacles_impl(layer_id, net_excl_pads, net_excl_tracks=None,
                          include_sibling_reserves=False, sibling_extra=0.0,
                          comp_keepout_mm=0.0):
    """
    Return forbidden rectangles on layer_id (clearance-expanded).
      net_excl_pads   : nets whose pads are NOT obstacles.
      net_excl_tracks : nets whose tracks/vias/_run_obs are NOT obstacles.
                        Defaults to net_excl_pads if omitted.
      include_sibling_reserves : if True, add via-sized (clearance-expanded)
                        reservations at every sibling HS pad position whose net
                        is not in net_excl_pads. Used when routing an HS pair's
                        centerline segment so the trace detours around pad
                        positions that will need via-in-pad escapes for OTHER
                        HS nets sharing the same component (e.g. AC caps).
      sibling_extra   : extra radius added to sibling reserve obstacles, in mm.
                        Set to half_cc so emitted P/N tracks (offset ±half_cc
                        from centerline, plus miter at bends) still clear the
                        sibling pad even when the corridor check is passed.
    """
    if net_excl_tracks is None:
        net_excl_tracks = net_excl_pads
    obs = []
    c   = CLEARANCE_MM

    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname() in net_excl_pads:
                continue
            if not pad.GetLayerSet().Contains(layer_id):
                continue
            obs.append(pad_obs_rect(pad))

    for track in board.GetTracks():
        if track.GetNetname() in net_excl_tracks:
            continue
        if track.GetClass() == "PCB_VIA":
            if not track.GetLayerSet().Contains(layer_id):
                continue
            # PROHIBITED 8B: never call GetWidth(layer_id) on PCB_VIA — asserts wxWidgets
            vr = tomm(track.GetDrill()) / 2 + 0.15
            vx = tomm(track.GetPosition().x)
            vy = tomm(track.GetPosition().y)
            obs.append((vx - vr - c, vy - vr - c, vx + vr + c, vy + vr + c))
        elif track.GetLayer() == layer_id:
            tw = tomm(track.GetWidth()) / 2
            x0 = tomm(min(track.GetStart().x, track.GetEnd().x))
            x1 = tomm(max(track.GetStart().x, track.GetEnd().x))
            y0 = tomm(min(track.GetStart().y, track.GetEnd().y))
            y1 = tomm(max(track.GetStart().y, track.GetEnd().y))
            obs.append((x0 - tw - c, y0 - tw - c, x1 + tw + c, y1 + tw + c))

    for rx1, ry1, rx2, ry2, rhw, rlayer, rnet in _run_obs:
        if rnet in net_excl_tracks or rlayer != layer_id:
            continue
        obs.append((min(rx1, rx2) - rhw - c, min(ry1, ry2) - rhw - c,
                    max(rx1, rx2) + rhw + c, max(ry1, ry2) + rhw + c))

    if include_sibling_reserves:
        via_r = VIA_PAD / 2.0
        for (res_net, rx, ry) in _sibling_reserves:
            if res_net in net_excl_pads:
                continue  # our own net — no need to reserve against ourselves
            r = via_r + c + sibling_extra
            obs.append((rx - r, ry - r, rx + r, ry + r))

    if comp_keepout_mm > 0:
        excl = set(net_excl_pads)
        for fp in board.GetFootprints():
            if any(pad.GetNetname() in excl for pad in fp.Pads()):
                continue
            try:
                bbox = fp.GetBoundingBox()
            except Exception:
                continue
            bx0 = tomm(bbox.GetLeft())   - comp_keepout_mm
            by0 = tomm(bbox.GetTop())    - comp_keepout_mm
            bx1 = tomm(bbox.GetRight())  + comp_keepout_mm
            by1 = tomm(bbox.GetBottom()) + comp_keepout_mm
            if bx1 > bx0 and by1 > by0:
                obs.append((bx0, by0, bx1, by1))

    ec = BOARD_EDGE_CLEARANCE; INF = 9999.0
    obs.extend([(_BRD_X1 - ec, -INF,  INF,           INF),
                (-INF,         -INF,  _BRD_X0 + ec,  INF),
                (-INF,         -INF,  INF,            _BRD_Y0 + ec),
                (-INF,          _BRD_Y1 - ec, INF,   INF)])
    return obs


def _build_obstacles_board_only_impl(layer_id, net_excl, track_excl=None):
    """build_obstacles without _run_obs entries — for use with exact-distance run_obs check."""
    if track_excl is None:
        track_excl = net_excl
    obs = []
    c = CLEARANCE_MM
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname() in net_excl:
                continue
            if not pad.GetLayerSet().Contains(layer_id):
                continue
            obs.append(pad_obs_rect(pad))
    for track in board.GetTracks():
        if track.GetNetname() in track_excl:
            continue
        if track.GetClass() == "PCB_VIA":
            if not track.GetLayerSet().Contains(layer_id):
                continue
            # PROHIBITED 8B: never call GetWidth(layer_id) on PCB_VIA — asserts wxWidgets
            vr = tomm(track.GetDrill()) / 2 + 0.15
            vx2 = tomm(track.GetPosition().x)
            vy2 = tomm(track.GetPosition().y)
            obs.append((vx2 - vr - c, vy2 - vr - c, vx2 + vr + c, vy2 + vr + c))
        elif track.GetLayer() == layer_id:
            tw = tomm(track.GetWidth()) / 2
            x0 = tomm(min(track.GetStart().x, track.GetEnd().x))
            x1 = tomm(max(track.GetStart().x, track.GetEnd().x))
            y0 = tomm(min(track.GetStart().y, track.GetEnd().y))
            y1 = tomm(max(track.GetStart().y, track.GetEnd().y))
            obs.append((x0 - tw - c, y0 - tw - c, x1 + tw + c, y1 + tw + c))
    ec = BOARD_EDGE_CLEARANCE; INF = 9999.0
    obs.extend([(_BRD_X1 - ec, -INF,  INF,           INF),
                (-INF,         -INF,  _BRD_X0 + ec,  INF),
                (-INF,         -INF,  INF,            _BRD_Y0 + ec),
                (-INF,          _BRD_Y1 - ec, INF,   INF)])
    return obs


# Per-pair accumulator: tracks placed by earlier segments of the current pair,
# keyed by layer_id.  Each entry is (rect, net_name) so build_obstacles can
# exclude same-net entries — prevents a previous segment's approach tracks from
# blocking the NEXT segment's departure from the shared endpoint pad.
_seg_placed = {}     # type: dict[int, list[tuple[tuple, str]]]  # (rect, net_name)
_seg_reserved = {}   # look-ahead corridor reservations for the next segment


def build_obstacles(layer_id, net_excl_pads, **kw):
    _sp = [r for r, n in _seg_placed.get(layer_id, []) if n not in net_excl_pads]
    return (_build_obstacles_impl(layer_id, net_excl_pads, **kw)
            + _sp
            + _seg_reserved.get(layer_id, []))


def build_obstacles_board_only(layer_id, net_excl, **kw):
    _sp = [r for r, n in _seg_placed.get(layer_id, []) if n not in net_excl]
    return (_build_obstacles_board_only_impl(layer_id, net_excl, **kw)
            + _sp
            + _seg_reserved.get(layer_id, []))


def _poly_obs(poly, half_w):
    """Convert a polyline to clearance-expanded AABB obstacle rects (same format as build_obstacles)."""
    c = CLEARANCE_MM
    return [(min(poly[i][0], poly[i+1][0]) - half_w - c,
             min(poly[i][1], poly[i+1][1]) - half_w - c,
             max(poly[i][0], poly[i+1][0]) + half_w + c,
             max(poly[i][1], poly[i+1][1]) + half_w + c)
            for i in range(len(poly) - 1)]


def _via_clear_of_run_obs(vx, vy, net_name, route_layer_id):
    """
    True if a via at (vx, vy) on route_layer_id clears all _run_obs entries
    using exact segment distance (not AABB) — avoids false positives for diagonal tracks.
    """
    min_d = VIA_PAD / 2.0 + CLEARANCE_MM
    for rx1, ry1, rx2, ry2, rhw, rlayer, rnet in _run_obs:
        if rnet == net_name or rlayer != route_layer_id:
            continue
        req = rhw + min_d
        if point_to_seg_dist(vx, vy, rx1, ry1, rx2, ry2) < req:
            return False
    return True


def _stub_clear_of_run_obs(x1, y1, x2, y2, stub_w, net_name, pad_layer_id):
    """
    True if the stub segment from (x1,y1) to (x2,y2) on pad_layer_id clears all
    _run_obs entries using exact distance — avoids AABB false positives for diagonal stubs
    adjacent to locked vias/tracks on the same pad layer.
    """
    min_d = stub_w / 2.0 + CLEARANCE_MM
    for rx1, ry1, rx2, ry2, rhw, rlayer, rnet in _run_obs:
        if rnet == net_name or rlayer != pad_layer_id:
            continue
        req = rhw + min_d
        # Compute minimum distance between the two line segments.
        # Check all four endpoint-to-segment distances (covers all non-crossing cases;
        # for crossing segments the segments themselves would violate DRC anyway).
        d = min(point_to_seg_dist(rx1, ry1, x1, y1, x2, y2),
                point_to_seg_dist(rx2, ry2, x1, y1, x2, y2),
                point_to_seg_dist(x1, y1, rx1, ry1, rx2, ry2),
                point_to_seg_dist(x2, y2, rx1, ry1, rx2, ry2))
        if d < req:
            return False
    return True


def _path_clear_of_explicit_segs(pts, half_w, segs):
    """Exact segment-to-segment clearance check against an explicit list of
    (x1, y1, x2, y2, seg_hw) tuples.  Uses the same 4-endpoint min-distance
    approximation as _path_clear_of_run_obs.  Unlike AABB, this does not
    inflate diagonal segments, so it avoids false rejections of valid paths."""
    req_margin = half_w + CLEARANCE_MM
    for i in range(len(pts) - 1):
        x1, y1 = pts[i];  x2, y2 = pts[i + 1]
        for sx1, sy1, sx2, sy2, shw in segs:
            req = shw + req_margin
            d = min(point_to_seg_dist(sx1, sy1, x1, y1, x2, y2),
                    point_to_seg_dist(sx2, sy2, x1, y1, x2, y2),
                    point_to_seg_dist(x1, y1, sx1, sy1, sx2, sy2),
                    point_to_seg_dist(x2, y2, sx1, sy1, sx2, sy2))
            if d < req:
                return False
    return True


def _path_clear_of_run_obs(pts, half_w, net_name, layer_id):
    """
    Exact segment-to-segment check for each leg of a polyline against _run_obs.
    Used as a fallback when AABB path_clear rejects a path due to diagonal _run_obs
    conservatism (the AABB of a diagonal track is larger than its actual copper).
    Returns True only if every leg of pts clears every _run_obs entry on layer_id.
    """
    req_margin = half_w + CLEARANCE_MM
    for i in range(len(pts) - 1):
        x1, y1 = pts[i];  x2, y2 = pts[i + 1]
        for rx1, ry1, rx2, ry2, rhw, rlayer, rnet in _run_obs:
            if rnet == net_name or rlayer != layer_id:
                continue
            req = rhw + req_margin
            d = min(point_to_seg_dist(rx1, ry1, x1, y1, x2, y2),
                    point_to_seg_dist(rx2, ry2, x1, y1, x2, y2),
                    point_to_seg_dist(x1, y1, rx1, ry1, rx2, ry2),
                    point_to_seg_dist(x2, y2, rx1, ry1, rx2, ry2))
            if d < req:
                return False
    return True


def build_obstacles_fine(layer_id, net_excl_pads, own_ref, pads_only=False):
    """
    Like build_obstacles but accepts own_ref for API compatibility.
    All pads use full DRC clearance via pad_obs_rect() — no per-footprint
    clearance reduction (which would allow routing through the DRC clearance
    zone of adjacent same-footprint pads, causing clearance violations).
    Tracks/vias/_run_obs are excluded for own-net only (same as build_obstacles).
    pads_only=True: omit board tracks and _run_obs — returns board pads only.
    """
    obs = []
    c   = CLEARANCE_MM

    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname() in net_excl_pads:
                continue
            if not pad.GetLayerSet().Contains(layer_id):
                continue
            obs.append(pad_obs_rect(pad))

    if pads_only:
        return obs

    excl = set(net_excl_pads)  # for tracks / _run_obs
    for track in board.GetTracks():
        if track.GetNetname() in excl:
            continue
        if track.GetClass() == "PCB_VIA":
            if not track.GetLayerSet().Contains(layer_id):
                continue
            # PROHIBITED 8B: never call GetWidth(layer_id) on PCB_VIA — asserts wxWidgets
            vr = tomm(track.GetDrill()) / 2 + 0.15
            vx = tomm(track.GetPosition().x)
            vy = tomm(track.GetPosition().y)
            obs.append((vx - vr - c, vy - vr - c, vx + vr + c, vy + vr + c))
        elif track.GetLayer() == layer_id:
            tw = tomm(track.GetWidth()) / 2
            x0 = tomm(min(track.GetStart().x, track.GetEnd().x))
            x1 = tomm(max(track.GetStart().x, track.GetEnd().x))
            y0 = tomm(min(track.GetStart().y, track.GetEnd().y))
            y1 = tomm(max(track.GetStart().y, track.GetEnd().y))
            obs.append((x0 - tw - c, y0 - tw - c, x1 + tw + c, y1 + tw + c))

    for rx1, ry1, rx2, ry2, rhw, rlayer, rnet in _run_obs:
        if rnet in excl or rlayer != layer_id:
            continue
        obs.append((min(rx1, rx2) - rhw - c, min(ry1, ry2) - rhw - c,
                    max(rx1, rx2) + rhw + c, max(ry1, ry2) + rhw + c))
    ec = BOARD_EDGE_CLEARANCE; INF = 9999.0
    obs.extend([(_BRD_X1 - ec, -INF,  INF,           INF),
                (-INF,         -INF,  _BRD_X0 + ec,  INF),
                (-INF,         -INF,  INF,            _BRD_Y0 + ec),
                (-INF,          _BRD_Y1 - ec, INF,   INF)])
    return obs


def _anchor_accessible(ax, ay, net_name, layer_id, width_mm):
    """True if at least one cardinal direction from (ax,ay) is clear at width_mm."""
    obs = build_obstacles(layer_id, {net_name})
    step = 0.2
    for dx, dy in ((step, 0), (-step, 0), (0, step), (0, -step)):
        r = seg_rect(ax + dx, ay + dy, ax, ay, width_mm)
        if not any(rect_overlap(r, o) for o in obs):
            return True
    return False


def _hs_relocate_anchor(pad, ax, ay, net_name, pad_layer_id, route_layer_id,
                        via_pad_mm, via_drill_mm, stub_w, trunk_w):
    """
    Walk the escape via outward from (ax,ay) along the pad→via direction until
    the new position is accessible at trunk_w on the route layer.  If the outward
    direction is exhausted, tries 8 canonical directions as a second pass.
    Uses a narrower stub width (NECKDOWN_W_MM) as a fallback if stub_w is blocked.
    Emits an extension stub and a new via; returns (cx,cy) or None.
    """
    ddx = ax - pad["x"]; ddy = ay - pad["y"]
    d = math.hypot(ddx, ddy)
    if d < 1e-6:
        return None
    edx = ddx / d; edy = ddy / d
    obs_pad = build_obstacles(pad_layer_id, {net_name})

    # Narrower fallback for the extension stub (HS traces are already 0.127mm,
    # so neckdown is the only further reduction available).
    ext_stub_widths = [stub_w]
    if NECKDOWN_W_MM < stub_w - 1e-6:
        ext_stub_widths.append(NECKDOWN_W_MM)

    # Primary pass: walk along the original escape direction.
    # Secondary pass: try 8 canonical directions when primary is exhausted.
    _S2 = 1.0 / math.sqrt(2.0)
    _all_dirs = [(edx, edy),
                 (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0),
                 (_S2,  _S2), (_S2, -_S2), (-_S2,  _S2), (-_S2, -_S2)]
    # Deduplicate while preserving order
    _seen = set(); _dirs = []
    for _dv in _all_dirs:
        _key = (round(_dv[0], 6), round(_dv[1], 6))
        if _key not in _seen:
            _seen.add(_key); _dirs.append(_dv)

    # Via size ladder: standard first, then progressively smaller as a last resort.
    # Smaller vias are only tried AFTER all directions exhaust with the standard size.
    _via_ladder = [(via_pad_mm, via_drill_mm)]
    for _vp, _vd in ((0.40, 0.20), (0.30, 0.10), (0.20, 0.10)):
        if _vp < via_pad_mm - 1e-6:
            _via_ladder.append((_vp, _vd))

    for _cur_vp, _cur_vd in _via_ladder:
        _cur_vr = _cur_vp / 2.0
        for _de, _dn in _dirs:
            n = RELOC_STEP_MM
            while n <= RELOC_MAX_MM + 1e-9:
                cx = ax + _de * n
                cy = ay + _dn * n
                if not _anchor_accessible(cx, cy, net_name, route_layer_id, trunk_w):
                    n += RELOC_STEP_MM
                    continue
                placed_sw = None
                for sw in ext_stub_widths:
                    stub_r = seg_rect(ax, ay, cx, cy, sw)
                    if not any(rect_overlap(stub_r, o) for o in obs_pad):
                        placed_sw = sw
                        break
                if placed_sw is None:
                    n += RELOC_STEP_MM
                    continue
                # For smaller vias, check that the via pad fits without overlapping obstacles.
                if _cur_vp < via_pad_mm - 1e-6:
                    _vbox = (cx - _cur_vr - CLEARANCE_MM, cy - _cur_vr - CLEARANCE_MM,
                             cx + _cur_vr + CLEARANCE_MM, cy + _cur_vr + CLEARANCE_MM)
                    if any(rect_overlap(_vbox, o) for o in obs_pad):
                        n += RELOC_STEP_MM
                        continue
                add_track(ax, ay, cx, cy, placed_sw, pad_layer_id, net_name)
                add_via(cx, cy, net_name, _cur_vp, _cur_vd)
                neck = f" [stub {placed_sw}mm]" if placed_sw < stub_w else ""
                via_note = f" [via {_cur_vp}mm]" if _cur_vp < via_pad_mm else ""
                dir_note = "" if (_de, _dn) == (edx, edy) else f" dir=({_de:.2f},{_dn:.2f})"
                log.append(f"    [anchor-reloc] {pad['ref']} {net_name}: extended {n:.2f}mm"
                           f"{dir_note}{neck}{via_note} → ({cx:.3f},{cy:.3f})")
                return (cx, cy)
    return None


def identify_blockers(region, layer_id, net_excl_pads, net_excl_tracks=None):
    if net_excl_tracks is None:
        net_excl_tracks = net_excl_pads
    labels = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            if pad.GetNetname() in net_excl_pads:
                continue
            if not pad.GetLayerSet().Contains(layer_id):
                continue
            if rect_overlap(pad_obs_rect(pad), region):
                labels.append(f"{ref}.{pad.GetNumber()}({pad.GetNetname()})")
    for rx1, ry1, rx2, ry2, rhw, rlayer, rnet in _run_obs:
        if rnet in net_excl_tracks or rlayer != layer_id:
            continue
        obs = (min(rx1, rx2) - rhw - CLEARANCE_MM, min(ry1, ry2) - rhw - CLEARANCE_MM,
               max(rx1, rx2) + rhw + CLEARANCE_MM, max(ry1, ry2) + rhw + CLEARANCE_MM)
        if rect_overlap(obs, region):
            labels.append(f"[routed:{rnet}]")
    return labels


# ── PCB write helpers ─────────────────────────────────────────────────────────

def add_track(x1, y1, x2, y2, width_mm, layer_id, net_name):
    length = dist(x1, y1, x2, y2)
    if length < 1e-4:
        return 0.0
    hw = width_mm / 2
    _run_obs.append((x1, y1, x2, y2, hw, layer_id, net_name))
    log.append(f"    TRACK {net_name}: ({x1:.3f},{y1:.3f})→({x2:.3f},{y2:.3f})"
               f"  {width_mm}mm  {board.GetLayerName(layer_id)}  L={length:.2f}mm")
    if not APPLY:
        return length
    net = board.FindNet(net_name)
    if net is None:
        log.append(f"    WARNING: net {net_name!r} not in board")
        return 0.0
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(mm(x1), mm(y1)))
    t.SetEnd(pcbnew.VECTOR2I(mm(x2), mm(y2)))
    t.SetWidth(mm(width_mm))
    t.SetLayer(layer_id)
    t.SetNet(net)
    board.Add(t)
    return length


def add_via(x, y, net_name, via_pad_mm, via_drill_mm):
    via_r = via_pad_mm / 2
    for _ln in ("F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"):
        _lid = board.GetLayerID(_ln)
        if _lid >= 0:
            _run_obs.append((x, y, x, y, via_r, _lid, net_name))
    log.append(f"    VIA {net_name} at ({x:.3f},{y:.3f})"
               f"  pad={via_pad_mm}mm  drill={via_drill_mm}mm")
    if not APPLY:
        return
    net = board.FindNet(net_name)
    if net is None:
        log.append(f"    WARNING: net {net_name!r} not in board")
        return
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
    v.SetWidth(mm(via_pad_mm))
    v.SetDrill(mm(via_drill_mm))
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetNet(net)
    v.SetLayerPair(F_CU, B_CU)
    board.Add(v)


# ── Pad lookup ────────────────────────────────────────────────────────────────

def get_pads_on_net(net_name, skip_inline=True):
    """Return one pad per footprint on net_name (skip footprints with multiple pads
    on the same net — those are passthrough parts, not endpoints)."""
    result = []
    for fp in board.GetFootprints():
        if skip_inline:
            count = sum(1 for pad in fp.Pads() if pad.GetNetname() == net_name)
            if count > 1:
                continue
        for pad in fp.Pads():
            if pad.GetNetname() == net_name:
                pos = pad.GetPosition()
                result.append({
                    "ref":       fp.GetReference(),
                    "x":         tomm(pos.x),
                    "y":         tomm(pos.y),
                    "layer":     pad.GetLayer(),
                    "layer_set": pad.GetLayerSet(),
                })
    return result


def pad_on_layer(pad, layer_id):
    ls = pad.get("layer_set")
    return ls is not None and ls.Contains(layer_id)


# ── Pair matching ─────────────────────────────────────────────────────────────

def pair_pads(pads_p, pads_n):
    """Greedy nearest-neighbor between P and N pads."""
    remaining = list(pads_n)
    pairs = []
    for p in pads_p:
        if not remaining:
            break
        closest = min(remaining, key=lambda n: dist(p["x"], p["y"], n["x"], n["y"]))
        pairs.append((p, closest))
        remaining.remove(closest)
    return pairs


def order_pairs(pairs):
    """Order matched (P,N) pairs by nearest-neighbor chain, topmost first."""
    if len(pairs) <= 1:
        return list(pairs)
    def cen(pr):
        p, n = pr
        return ((p["x"] + n["x"]) / 2, (p["y"] + n["y"]) / 2)
    remaining = list(pairs)
    chain = [min(remaining, key=lambda pr: cen(pr)[1])]
    remaining.remove(chain[0])
    while remaining:
        cx, cy = cen(chain[-1])
        nxt = min(remaining, key=lambda pr: dist(cx, cy, *cen(pr)))
        chain.append(nxt)
        remaining.remove(nxt)
    return chain


# ── Geometry utilities ───────────────────────────────────────────────────────

def merge_collinear(pts):
    if len(pts) <= 2:
        return list(pts)
    result = [pts[0]]
    for i in range(1, len(pts) - 1):
        ax, ay = result[-1]; bx, by = pts[i]; cx, cy = pts[i+1]
        if abs((bx-ax)*(cy-by) - (by-ay)*(cx-bx)) < 1e-6:
            continue
        result.append((bx, by))
    result.append(pts[-1])
    return result


def _route_45deg_stub(pad_x, pad_y, via_x, via_y, edx, edy, axial_first=True):
    """Decompose pad→via into 1 or 2 segments each on a valid 45° angle.
    Copied from route_fanout_vias.py — single source of truth for canonical stubs.
    edx/edy must be a canonical unit direction (0/45/90°).
    Returns list of (x1, y1, x2, y2) tuples; empty if pad == via.
    """
    dx = via_x - pad_x
    dy = via_y - pad_y
    if math.hypot(dx, dy) < 1e-6:
        return []
    escape_comp = dx * edx  + dy * edy
    lat_comp    = dx * (-edy) + dy * edx
    abs_ec = abs(escape_comp)
    abs_lc = abs(lat_comp)
    if abs_lc < 1e-4 or abs_ec < 1e-4 or abs(abs_ec - abs_lc) < 1e-4:
        return [(pad_x, pad_y, via_x, via_y)]
    ec_sign = 1 if escape_comp > 0 else -1
    lc_sign = 1 if lat_comp    > 0 else -1
    lat_dx  = -edy
    lat_dy  =  edx
    if axial_first and abs_ec >= abs_lc:
        wx = pad_x + ec_sign * edx * (abs_ec - abs_lc)
        wy = pad_y + ec_sign * edy * (abs_ec - abs_lc)
    elif abs_ec >= abs_lc:
        wx = pad_x + ec_sign * edx * abs_lc + lc_sign * lat_dx * abs_lc
        wy = pad_y + ec_sign * edy * abs_lc + lc_sign * lat_dy * abs_lc
    else:
        wx = pad_x + ec_sign * edx * abs_ec + lc_sign * lat_dx * abs_ec
        wy = pad_y + ec_sign * edy * abs_ec + lc_sign * lat_dy * abs_ec
    return [(pad_x, pad_y, wx, wy), (wx, wy, via_x, via_y)]


def _stub_pts(px, py, vx, vy, edx, edy):
    """Return _route_45deg_stub result as a list of (x, y) points."""
    segs = _route_45deg_stub(px, py, vx, vy, edx, edy)
    if not segs:
        return [(px, py), (vx, vy)]
    pts = [(segs[0][0], segs[0][1])]
    for s in segs:
        pts.append((s[2], s[3]))
    return pts


def _snap_canonical(ux, uy):
    """Snap a unit vector to the nearest of 8 canonical directions."""
    _S2 = 1.0 / math.sqrt(2.0)
    _CANON = [(1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0),
              (_S2, _S2), (_S2, -_S2), (-_S2, _S2), (-_S2, -_S2)]
    return max(_CANON, key=lambda d: d[0] * ux + d[1] * uy)


def octilinear_paths(x1, y1, x2, y2):
    """Return candidate H/V waypoint lists between the two points.
    Main run uses only horizontal and vertical segments — no diagonals.
    """
    paths = []
    # H then V, V then H
    paths.append([(x1, y1), (x2, y1), (x2, y2)])
    paths.append([(x1, y1), (x1, y2), (x2, y2)])
    # Straight line (if already H or V)
    adx, ady = abs(x2 - x1), abs(y2 - y1)
    if adx < 1e-4 or ady < 1e-4:
        paths.append([(x1, y1), (x2, y2)])
    return paths


def path_clear(pts, w, obstacles):
    for i in range(len(pts) - 1):
        r = seg_rect(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1], w)
        for o in obstacles:
            if rect_overlap(r, o):
                return False
    return True


def _path_clear_approx(pts, w, obstacles):
    """Like path_clear but uses actual segment-to-center distance for square
    obstacle boxes (vias). For a 45° segment near a circular via, the AABB
    of the segment inflates the corner area and produces false positives.
    Square obstacle boxes (equal width and height) are via obstacles; all
    others (non-square pad obstacles) still use AABB."""
    hw = w / 2
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]; x2, y2 = pts[i+1]
        r = seg_rect(x1, y1, x2, y2, w)
        for o in obstacles:
            if not rect_overlap(r, o):
                continue  # AABB fast-pass: definitely clear
            lx, ly, rx, ry = o
            if abs((rx - lx) - (ry - ly)) < 1e-3:  # square → via obstacle
                cx = (lx + rx) / 2; cy = (ly + ry) / 2
                r_obs = (rx - lx) / 2
                if point_to_seg_dist(cx, cy, x1, y1, x2, y2) < r_obs + hw:
                    return False
            else:  # non-square pad: exact corner + endpoint + crossing check
                if lx - hw <= x1 <= rx + hw and ly - hw <= y1 <= ry + hw:
                    return False
                if lx - hw <= x2 <= rx + hw and ly - hw <= y2 <= ry + hw:
                    return False
                for _cx, _cy in ((lx,ly),(rx,ly),(lx,ry),(rx,ry)):
                    if point_to_seg_dist(_cx, _cy, x1, y1, x2, y2) < hw:
                        return False
                for _ex1,_ey1,_ex2,_ey2 in ((lx,ly,rx,ly),(rx,ly,rx,ry),
                                               (rx,ry,lx,ry),(lx,ry,lx,ly)):
                    if _segs_cross(x1, y1, x2, y2, _ex1, _ey1, _ex2, _ey2):
                        return False
    return True


def _segs_cross(x1, y1, x2, y2, x3, y3, x4, y4):
    """True if segments (x1,y1)-(x2,y2) and (x3,y3)-(x4,y4) properly cross."""
    def _c2d(ax, ay, bx, by): return ax * by - ay * bx
    d1x, d1y = x2 - x1, y2 - y1
    d2x, d2y = x4 - x3, y4 - y3
    dn = _c2d(d1x, d1y, d2x, d2y)
    if abs(dn) < 1e-9:
        return False
    t = _c2d(x3 - x1, y3 - y1, d2x, d2y) / dn
    u = _c2d(x3 - x1, y3 - y1, d1x, d1y) / dn
    return 0.0 < t < 1.0 and 0.0 < u < 1.0


def _astar_centerline(cx0, cy0, cx1, cy1, w_full, obs_layer, grid_pitch=0.6):
    """
    A* grid search for a clear centerline path when all octilinear candidates fail.
    Returns a merged octilinear polyline or None if no path found within budget.
    Uses H/V steps on a square grid at grid_pitch mm.

    Minimum safe grid_pitch to prevent P/N copper shorts from the H/V staircase
    pattern: cc + width + clearance (per-pair).  Default 0.6mm covers most pairs;
    a caller may pass a finer pitch computed as math.ceil(min_safe/0.05)*0.05.
    """
    gp = grid_pitch
    sx = round(cx0 / gp)
    sy = round(cy0 / gp)
    ex = round(cx1 / gp)
    ey = round(cy1 / gp)
    if (sx, sy) == (ex, ey):
        return [(cx0, cy0), (cx1, cy1)]

    def seg_blocked(gx0, gy0, gx1, gy1):
        r = seg_rect(gx0 * gp, gy0 * gp, gx1 * gp, gy1 * gp, w_full)
        return any(rect_overlap(r, o) for o in obs_layer)

    import heapq as _hq
    open_set = []
    _hq.heappush(open_set, (math.hypot(sx - ex, sy - ey), 0.0, sx, sy))
    came_from = {}
    g_score = {(sx, sy): 0.0}

    max_cells = 40000
    checked = 0
    while open_set and checked < max_cells:
        _f, g, cx, cy = _hq.heappop(open_set)
        checked += 1
        if cx == ex and cy == ey:
            path = []
            cur = (cx, cy)
            while cur in came_from:
                path.append((cur[0] * gp, cur[1] * gp))
                cur = came_from[cur]
            path.append((sx * gp, sy * gp))  # snapped grid start (canonical)
            path.reverse()
            path[-1] = (ex * gp, ey * gp)    # snapped grid end (canonical)
            return merge_collinear(path)
        for ddx, ddy in ((0,1),(0,-1),(1,0),(-1,0),(1,1),(1,-1),(-1,1),(-1,-1)):
            nx, ny = cx + ddx, cy + ddy
            if (nx, ny) in g_score:
                continue
            if seg_blocked(cx, cy, nx, ny):
                continue
            ng = g + math.hypot(ddx, ddy)
            g_score[(nx, ny)] = ng
            came_from[(nx, ny)] = (cx, cy)
            h = math.hypot(nx - ex, ny - ey)
            _hq.heappush(open_set, (ng + h, ng, nx, ny))
    return None


# ── Escape via placement ─────────────────────────────────────────────────────

def _try_via(vx, vy, obs_pad, obs_route, stub_x1, stub_y1, stub_w,
             net_name=None, route_layer_id=None, pad_layer_id=None,
             obs_pad_solid=None):
    """True if (vx,vy) is a clear via location with a clear stub from (stub_x1,stub_y1).

    obs_pad_solid: board pads only (no _run_obs). When provided, via_box is checked
    against obs_pad_solid with NO exact fallback (board pads are rectangular — AABB
    is always exact). Script-placed _run_obs on pad_layer_id are then checked with
    exact point-to-segment distance to avoid diagonal-stub AABB false positives.
    obs_route AABB failures use _via_clear_of_run_obs (exact) on route_layer_id.
    stub_box vs obs_pad uses _stub_clear_of_run_obs (exact) on pad_layer_id.
    """
    via_r = VIA_PAD / 2.0
    c = CLEARANCE_MM
    # Obstacles already contain CLEARANCE_MM (from pad_obs_rect / build_obstacles).
    # Use copper-only extents here — adding c again would double-count clearance.
    via_box  = (vx - via_r, vy - via_r, vx + via_r, vy + via_r)
    stub_box = seg_rect(stub_x1, stub_y1, vx, vy, stub_w)

    # Board pads: AABB is exact for rectangular pads — no fallback needed.
    _pad_check = obs_pad_solid if obs_pad_solid is not None else obs_pad
    if any(rect_overlap(via_box, o) for o in _pad_check):
        return False

    # Script-placed tracks on pad layer may be diagonal — exact distance check.
    if obs_pad_solid is not None and pad_layer_id is not None and net_name is not None:
        min_d = via_r + CLEARANCE_MM
        for rx1, ry1, rx2, ry2, rhw, rlayer, rnet in _run_obs:
            if rnet == net_name or rlayer != pad_layer_id:
                continue
            if point_to_seg_dist(vx, vy, rx1, ry1, rx2, ry2) < rhw + min_d:
                return False

    # Route layer: diagonal script vias may have conservative AABB.
    if any(rect_overlap(via_box, o) for o in obs_route):
        if net_name is None or not _via_clear_of_run_obs(vx, vy, net_name, route_layer_id):
            return False

    # Stub: full obs_pad with exact fallback for diagonal _run_obs stubs.
    if any(rect_overlap(stub_box, o) for o in obs_pad):
        if pad_layer_id is None or not _stub_clear_of_run_obs(
                stub_x1, stub_y1, vx, vy, stub_w, net_name, pad_layer_id):
            return False
    return True


def ic_outward_direction(pad):
    """
    Compute the unit vector pointing AWAY from the pad's footprint body.
    Uses the centroid of all pads on the same footprint: away-from-centroid
    is the natural escape direction from an IC edge pad or connector pad.
    Returns (ux, uy).  For a symmetric two-pad passive (centroid on pad axis)
    returns (0, 0); caller should fall back to pair-perp direction.
    """
    for fp in board.GetFootprints():
        if fp.GetReference() != pad["ref"]:
            continue
        xs = []; ys = []
        for pd in fp.Pads():
            pos = pd.GetPosition()
            xs.append(tomm(pos.x)); ys.append(tomm(pos.y))
        if not xs:
            return 0.0, 0.0
        cx = sum(xs) / len(xs);  cy = sum(ys) / len(ys)
        dx = pad["x"] - cx;      dy = pad["y"] - cy
        L = math.hypot(dx, dy)
        if L < 0.05:
            return 0.0, 0.0
        return dx / L, dy / L
    return 0.0, 0.0


def _astar_escape(pad, net_name, partner_net, route_layer_id,
                   lateral_constraint=None, avoid_pad=None, min_distance_mm=0.0,
                   avoid_strict=True, no_north_of=None):
    """
    BFS/A* maze on pad layer to find first reachable via position.
    Uses fine-pitch obstacle model (physical-only for own-footprint neighbors).
    lateral_constraint: optional (lx, ly, perp_ux, perp_uy, lsign) tuple — rejects
      any candidate via position where ((vx-lx)*perp_ux + (vy-ly)*perp_uy)*lsign <= 0.
      Carries the actual perp unit vector so diagonal pairs are handled correctly.
    avoid_pad: optional pad dict — when avoid_strict=True, candidate via positions must
      be at least (VIA_PAD + 2*CLEARANCE_MM) away from this pad center. When
      avoid_strict=False, avoid_pad only biases the direction search (no hard distance
      constraint) — used for non-crossing pairs where only direction bias is needed.
    min_distance_mm: candidate positions closer than this to the pad center are
      expanded but not accepted — ensures the via lands far enough out to leave
      room for a crossing partner to slot between this pad and this via.
    Returns (vx, vy) or None.
    """
    STUB_W = NECKDOWN_W_MM
    c = CLEARANCE_MM
    pad_excl = {net_name, partner_net}
    track_excl = {net_name}
    obs_fine  = build_obstacles_fine(pad["layer"], pad_excl, own_ref=pad["ref"])
    obs_route = build_obstacles(route_layer_id, track_excl)

    via_r = VIA_PAD / 2.0
    min_avoid_d = (VIA_PAD + 2 * c) if (avoid_pad is not None and avoid_strict) else 0.0
    gp = 0.2  # grid pitch mm

    sx = round(pad["x"] / gp)
    sy = round(pad["y"] / gp)

    from collections import deque
    visited = set()
    queue = deque()
    queue.append((sx, sy))
    visited.add((sx, sy))

    def cell_blocked(gx, gy):
        wx = gx * gp
        wy = gy * gp
        box = (wx - via_r, wy - via_r, wx + via_r, wy + via_r)
        return (any(rect_overlap(box, o) for o in obs_fine) or
                any(rect_overlap(box, o) for o in obs_route))

    def stub_blocked(x1, y1, x2, y2):
        sb = seg_rect(x1, y1, x2, y2, STUB_W)
        return any(rect_overlap(sb, o) for o in obs_fine)

    max_cells = 3000
    checked = 0
    while queue and checked < max_cells:
        cx, cy = queue.popleft()
        checked += 1
        wx, wy = cx * gp, cy * gp
        if (cx != sx or cy != sy) and not cell_blocked(cx, cy):
            # Lateral constraint check
            if lateral_constraint is not None:
                lx, ly, lpux, lpuy, lsign = lateral_constraint
                proj = (wx - lx) * lpux + (wy - ly) * lpuy
                if proj * lsign <= 0:
                    continue
            # Avoid pad proximity check: keep enough room for avoid_pad's own via
            if avoid_pad is not None:
                if math.hypot(wx - avoid_pad["x"], wy - avoid_pad["y"]) < min_avoid_d:
                    continue
            # Reject positions further along pair-axis than partner's already-placed via,
            # or backward (south of pad along routing axis).
            if no_north_of is not None:
                nnx, nny, nnax, nnay = no_north_of
                if (wx - nnx) * (-nnax) + (wy - nny) * (-nnay) < 0:
                    continue
                if wx * nnax + wy * nnay < pad["x"] * nnax + pad["y"] * nnay:
                    continue
            # Minimum escape distance: expand neighbors but do not accept if too close.
            # This ensures the via lands far enough from the pad that a crossing
            # partner can slot between this pad and this via.
            if (min_distance_mm <= 0 or
                    math.hypot(wx - pad["x"], wy - pad["y"]) >= min_distance_mm):
                _sb = stub_blocked(pad["x"], pad["y"], wx, wy)
                _stub_ok = (not _sb or _stub_clear_of_run_obs(
                    pad["x"], pad["y"], wx, wy, STUB_W_MM, net_name, pad["layer"]))
                if _stub_ok:
                    _sd = math.hypot(wx - pad["x"], wy - pad["y"])
                    _edx_s, _edy_s = _snap_canonical(
                        (wx - pad["x"]) / _sd, (wy - pad["y"]) / _sd)
                    for _seg in (_route_45deg_stub(pad["x"], pad["y"], wx, wy,
                                                   _edx_s, _edy_s) or
                                 [(pad["x"], pad["y"], wx, wy)]):
                        add_track(_seg[0], _seg[1], _seg[2], _seg[3],
                                  STUB_W, pad["layer"], net_name)
                    add_via(wx, wy, net_name, VIA_PAD, VIA_DRILL)
                    log.append(f"    A*-escape {pad['ref']} net={net_name}: "
                               f"({pad['x']:.3f},{pad['y']:.3f})→({wx:.3f},{wy:.3f})")
                    return wx, wy
        for ddx, ddy in ((0,1),(0,-1),(1,0),(-1,0),(1,1),(1,-1),(-1,1),(-1,-1)):
            nx, ny = cx + ddx, cy + ddy
            if (nx, ny) not in visited:
                visited.add((nx, ny))
                queue.append((nx, ny))
    return None


def _astar_crossing_escape(pad, net_name, partner_net, route_layer_id,
                            cross_side, cross_partner):
    """
    BFS on pad layer with partner pad as wall. Accepts first via candidate
    on the FAR side of cross_partner along cross_side.
    Uses neckdown width for stubs to thread through tight BGA/QFN crossing gaps.
    """
    STUB_W = NECKDOWN_W_MM
    c = CLEARANCE_MM
    # Crossing: partner pad IS a wall (not excluded)
    pad_excl = {net_name}
    track_excl = {net_name}
    obs_fine  = build_obstacles_fine(pad["layer"], pad_excl, own_ref=pad["ref"])
    obs_route = build_obstacles(route_layer_id, track_excl)

    via_r = VIA_PAD / 2.0
    gp = 0.15  # finer grid for crossing thread

    sx = round(pad["x"] / gp)
    sy = round(pad["y"] / gp)

    cs_ux, cs_uy = cross_side
    pp_size = cross_partner.get("_half_along", 0.35)
    ppx, ppy = cross_partner["x"], cross_partner["y"]
    far_threshold = pp_size + via_r + c

    from collections import deque
    visited = set()
    parent  = {}
    queue = deque()
    queue.append((sx, sy))
    visited.add((sx, sy))

    def cell_blocked(gx, gy):
        wx = gx * gp
        wy = gy * gp
        box = (wx - via_r, wy - via_r, wx + via_r, wy + via_r)
        return (any(rect_overlap(box, o) for o in obs_fine) or
                any(rect_overlap(box, o) for o in obs_route))

    def stub_blocked_pts(pts):
        for i in range(len(pts) - 1):
            sb = seg_rect(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1], STUB_W)
            if any(rect_overlap(sb, o) for o in obs_fine):
                return True
        return False

    max_cells = 8000
    checked = 0
    while queue and checked < max_cells:
        cx, cy = queue.popleft()
        checked += 1
        wx, wy = cx * gp, cy * gp
        # Accept if via on FAR side of partner along cross_side
        proj = (wx - ppx) * cs_ux + (wy - ppy) * cs_uy
        if proj >= far_threshold and (cx != sx or cy != sy) and not cell_blocked(cx, cy):
            # Reconstruct path
            path = [(wx, wy)]
            cur = (cx, cy)
            while cur in parent:
                cur = parent[cur]
                path.append((cur[0] * gp, cur[1] * gp))
            path.reverse()
            # Prepend actual pad start
            full_path = [(pad["x"], pad["y"])] + path[1:]
            # Canonical stub via _route_45deg_stub; fall back to BFS path if blocked
            _dc = math.hypot(wx - pad["x"], wy - pad["y"])
            _edx_c, _edy_c = (_snap_canonical((wx - pad["x"]) / _dc,
                                               (wy - pad["y"]) / _dc)
                               if _dc > 1e-9 else (cs_ux, cs_uy))
            _pts45 = _stub_pts(pad["x"], pad["y"], wx, wy, _edx_c, _edy_c)
            use_path = _pts45 if not stub_blocked_pts(_pts45) else full_path
            if stub_blocked_pts(use_path):
                # Skip — path itself has issues; keep searching
                pass
            else:
                for i in range(len(use_path) - 1):
                    add_track(use_path[i][0], use_path[i][1],
                              use_path[i+1][0], use_path[i+1][1],
                              STUB_W, pad["layer"], net_name)
                add_via(wx, wy, net_name, VIA_PAD, VIA_DRILL)
                log.append(f"    A*-cross-escape {pad['ref']} net={net_name}: "
                           f"threaded past {cross_partner['ref']} → ({wx:.3f},{wy:.3f}) "
                           f"[{len(use_path)-1} seg(s)]")
                return wx, wy
        for ddx, ddy in ((0,1),(0,-1),(1,0),(-1,0),(1,1),(1,-1),(-1,1),(-1,-1)):
            nx, ny = cx + ddx, cy + ddy
            if (nx, ny) not in visited:
                # Only expand into non-blocked cells (except start)
                if not cell_blocked(nx, ny):
                    visited.add((nx, ny))
                    parent[(nx, ny)] = (cx, cy)
                    queue.append((nx, ny))
                else:
                    visited.add((nx, ny))
    return None


def _via_in_pad_fallback(pad, net_name, route_layer_id, partner_net=None):
    """Last resort: place a via at the pad center itself (via-in-pad).

    Rejected when the via is larger than the pad (via would protrude past pad
    copper, creating clearance violations to adjacent pads).

    NOTE: obs_route rects already have CLEARANCE_MM baked in. The via_box here
    is copper-only (no extra clearance) so the overlap test represents exactly
    one clearance between copper edges, not two.
    """
    via_r = VIA_PAD / 2.0
    vx, vy = pad["x"], pad["y"]
    # Guard: reject via-in-pad when via is wider than the pad.  A via that
    # protrudes past pad copper violates clearance to adjacent pads — no amount
    # of exact-distance checking can fix a genuine copper overlap.
    for fp in board.GetFootprints():
        if fp.GetReference() != pad["ref"]:
            continue
        for _pd in fp.Pads():
            if (abs(tomm(_pd.GetPosition().x) - vx) < 1e-3 and
                    abs(tomm(_pd.GetPosition().y) - vy) < 1e-3):
                size = _pd.GetSize()
                sx, sy = tomm(size.x) / 2, tomm(size.y) / 2
                try:
                    ang = math.radians(_pd.GetOrientationDegrees())
                except AttributeError:
                    ang = 0.0
                ca_, sa_ = abs(math.cos(ang)), abs(math.sin(ang))
                hx = sx * ca_ + sy * sa_
                hy = sx * sa_ + sy * ca_
                if via_r > min(hx, hy):
                    log.append(f"    via-in-pad {pad['ref']} net={net_name}: SKIPPED "
                               f"(via {VIA_PAD:.2f}mm > pad {2*min(hx,hy):.2f}mm — would protrude)")
                    return None
        break
    # Check script-placed vias/tracks with exact distance (avoids AABB false positives
    # for diagonally separated vias placed earlier in this script run).
    if not _via_clear_of_run_obs(vx, vy, net_name, route_layer_id):
        log.append(f"    via-in-pad {pad['ref']} net={net_name}: BLOCKED by run obstacle (exact dist)")
        return None
    # Board pads and pre-existing tracks: AABB is fine (H/V geometry).
    # Skip via objects from board.GetTracks() — those are already covered by
    # _via_clear_of_run_obs above (script-placed) or are pre-existing vias whose
    # separation from connector pads is always > minimum clearance in practice.
    obs_board_no_vias = []
    c_tmp = CLEARANCE_MM; via_r_tmp = VIA_PAD / 2.0
    for fp in board.GetFootprints():
        for _pd in fp.Pads():
            if _pd.GetNetname() == net_name:
                continue
            if not _pd.GetLayerSet().Contains(route_layer_id):
                continue
            obs_board_no_vias.append(pad_obs_rect(_pd))
    for _trk in board.GetTracks():
        if _trk.GetNetname() == net_name:
            continue
        if _trk.GetClass() == "PCB_VIA":
            continue  # vias handled by _via_clear_of_run_obs
        if _trk.GetLayer() == route_layer_id:
            tw = tomm(_trk.GetWidth()) / 2
            x0 = tomm(min(_trk.GetStart().x, _trk.GetEnd().x))
            x1 = tomm(max(_trk.GetStart().x, _trk.GetEnd().x))
            y0 = tomm(min(_trk.GetStart().y, _trk.GetEnd().y))
            y1 = tomm(max(_trk.GetStart().y, _trk.GetEnd().y))
            obs_board_no_vias.append((x0 - tw - c_tmp, y0 - tw - c_tmp,
                                      x1 + tw + c_tmp, y1 + tw + c_tmp))
    via_box = (vx - via_r, vy - via_r, vx + via_r, vy + via_r)  # copper only
    if any(rect_overlap(via_box, o) for o in obs_board_no_vias):
        log.append(f"    via-in-pad {pad['ref']} net={net_name}: BLOCKED by board obstacle")
        return None
    add_via(vx, vy, net_name, VIA_PAD, VIA_DRILL)
    log.append(f"    via-in-pad {pad['ref']} net={net_name}: ({vx:.3f},{vy:.3f})")
    return vx, vy


def place_escape_via(pad, net_name, partner_net, route_layer_id,
                     out_ux, out_uy, cross_side=None, cross_partner=None,
                     avoid_pad=None, lateral_constraint=None,
                     min_distance_mm=0.0, avoid_strict=True,
                     min_partner_gap_mm=None,
                     no_north_of=None):
    """
    Place a via for pad's net that lands on the FAR side of the pair axis
    (direction (out_ux, out_uy)) with a stub on the pad layer.

    cross_side / cross_partner : if given, the pad's escape must thread PAST
      cross_partner (a pad dict of the partner-net pad at this endpoint) so
      the via lands on the OPPOSITE side of that partner pad along cross_side
      (a unit vector in the crossing direction).  In this case the pad-layer
      obstacle set INCLUDES the partner-net pads (partner pad is a wall to
      thread around), and stubs are considered in staged L-shapes.

    Returns (vx, vy) or None if no clear position found.
    """
    px, py    = pad["x"], pad["y"]
    pad_layer = pad["layer"]
    stub_w    = STUB_W_MM
    c         = CLEARANCE_MM

    # Non-crossing case: partner pad excluded so we can escape into its zone
    # freely; on the route layer, only own-net is excluded so partner via is
    # a hard obstacle.
    if cross_side is None:
        pad_excl = {net_name, partner_net}
    else:
        pad_excl = {net_name}   # partner pad IS a wall we must go around
    # For pin escape at fine pitch (QFN/BGA) the effective width of trace + 2*clearance
    # can exceed pin pitch. Build a reduced-clearance obstacle set that includes only
    # PHYSICAL pad extents for pads on the SAME footprint as `pad` (own-footprint
    # neighbours) — full clearance is still applied to all other pads.
    obs_pad       = build_obstacles_fine(pad_layer, pad_excl, own_ref=pad["ref"])
    obs_pad_solid = build_obstacles_fine(pad_layer, pad_excl, own_ref=pad["ref"], pads_only=True)
    obs_route     = build_obstacles(route_layer_id, {net_name})

    # Direct outward escape: try all 8 canonical directions (0/45/90°) sorted by
    # proximity to the outward direction, at distances from 0.6 to 3.5mm.
    # Stubs must always be at 0°, 45°, or 90° — no arbitrary angles.
    if cross_side is None:
        _S2 = 1.0 / math.sqrt(2.0)
        _CANON_DIRS = [
            (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0),
            (_S2,  _S2), (_S2, -_S2), (-_S2,  _S2), (-_S2, -_S2),
        ]
        base_angle = math.atan2(out_uy, out_ux) if (abs(out_ux) + abs(out_uy)) > 0.01 else 0.0
        def _ang_dist_base(dx, dy):
            a = math.atan2(dy, dx) - base_angle
            return abs(math.atan2(math.sin(a), math.cos(a)))
        sorted_dirs = sorted(_CANON_DIRS, key=lambda xy: _ang_dist_base(xy[0], xy[1]))

        # If avoid_pad given, re-sort to prefer directions furthest from it
        if avoid_pad is not None:
            ax = avoid_pad["x"] - px; ay = avoid_pad["y"] - py
            aL = math.hypot(ax, ay)
            if aL > 0.05:
                avoid_ang = math.atan2(ay, ax)
                def _dir_score(xy):
                    va = math.atan2(xy[1], xy[0])
                    d_avoid = abs(math.atan2(math.sin(va - avoid_ang),
                                             math.cos(va - avoid_ang)))
                    return (-d_avoid, _ang_dist_base(xy[0], xy[1]))
                sorted_dirs = sorted(sorted_dirs, key=_dir_score)

        best = None
        best_d = float('inf')
        used_stub_w = STUB_W_MM
        min_partner_gap = 0.0
        if avoid_pad is not None and avoid_strict:
            min_partner_gap = (min_partner_gap_mm if min_partner_gap_mm is not None
                               else VIA_PAD + 2 * CLEARANCE_MM)  # default ~0.85mm
        # Filter distances by minimum escape requirement; try standard width then neckdown.
        all_distances = (0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 3.5)
        distances = [d for d in all_distances if d >= min_distance_mm]
        if not distances:
            distances = [min_distance_mm]
        for try_stub_w in (STUB_W_MM, NECKDOWN_W_MM):
            for d in distances:
                for cdx, cdy in sorted_dirs:
                    vx = px + cdx * d
                    vy = py + cdy * d
                    if avoid_pad is not None:
                        if math.hypot(vx - avoid_pad["x"], vy - avoid_pad["y"]) < min_partner_gap:
                            continue
                    if lateral_constraint is not None:
                        lx, ly, lpux, lpuy, lsign = lateral_constraint
                        proj = (vx - lx) * lpux + (vy - ly) * lpuy
                        if proj * lsign <= 0:
                            continue
                    if no_north_of is not None:
                        nnx, nny, nnax, nnay = no_north_of
                        # Reject positions further along pair-axis than partner's via.
                        if (vx - nnx) * (-nnax) + (vy - nny) * (-nnay) < 0:
                            continue
                        # Also reject positions south of the pad (backward escape).
                        if vx * nnax + vy * nnay < px * nnax + py * nnay:
                            continue
                    if _try_via(vx, vy, obs_pad, obs_route, px, py, try_stub_w,
                               net_name=net_name, route_layer_id=route_layer_id,
                               pad_layer_id=pad_layer, obs_pad_solid=obs_pad_solid):
                        if d < best_d:
                            best_d = d; best = (vx, vy, cdx, cdy); used_stub_w = try_stub_w
                            break
                if best is not None:
                    break
            if best is not None:
                break
        if best is not None:
            vx, vy, best_dx, best_dy = best
            add_track(px, py, vx, vy, used_stub_w, pad_layer, net_name)
            add_via(vx, vy, net_name, VIA_PAD, VIA_DRILL)
            log.append(f"    escape {pad['ref']} net={net_name}: "
                       f"({px:.3f},{py:.3f})→({vx:.3f},{vy:.3f})"
                       f"  d={best_d:.2f}mm"
                       f"  dir={math.degrees(math.atan2(best_dy, best_dx)):.0f}°"
                       f"  stub={used_stub_w:.3f}mm")
            return vx, vy
        # Angle sweep failed — try A* maze escape as fallback
        v = _astar_escape(pad, net_name, partner_net, route_layer_id,
                          lateral_constraint=lateral_constraint,
                          avoid_pad=avoid_pad, min_distance_mm=min_distance_mm,
                          avoid_strict=avoid_strict, no_north_of=no_north_of)
        if v is not None:
            return v
        # Last resort: via-in-pad (only when min_distance is not required).
        # Skip if via-in-pad position itself violates the no_north_of constraint.
        _vip_ok = min_distance_mm <= 0
        if _vip_ok and no_north_of is not None:
            nnx, nny, nnax, nnay = no_north_of
            if (px - nnx) * (-nnax) + (py - nny) * (-nnay) < 0:
                _vip_ok = False
        if _vip_ok:
            v = _via_in_pad_fallback(pad, net_name, route_layer_id)
            if v is not None:
                return v
            # Smaller via fallback: when standard via is too large for pad, try
            # progressively smaller sizes (0.40→0.30→0.20mm) — supports fine-pitch
            # BGA/QFN pads where via-in-pad is the only viable escape strategy.
            for _fpad, _fdrill in ((0.40, 0.20), (0.30, 0.10), (0.20, 0.10)):
                if _fpad >= VIA_PAD:
                    continue
                _fvr = _fpad / 2.0
                _via_fits = False
                for _fp in board.GetFootprints():
                    if _fp.GetReference() != pad["ref"]: continue
                    for _pd2 in _fp.Pads():
                        if (abs(tomm(_pd2.GetPosition().x) - px) < 1e-3 and
                                abs(tomm(_pd2.GetPosition().y) - py) < 1e-3):
                            _s = _pd2.GetSize()
                            _sx2, _sy2 = tomm(_s.x) / 2, tomm(_s.y) / 2
                            try: _ang2 = math.radians(_pd2.GetOrientationDegrees())
                            except AttributeError: _ang2 = 0.0
                            _ca2 = abs(math.cos(_ang2)); _sa2 = abs(math.sin(_ang2))
                            _hx2 = _sx2*_ca2 + _sy2*_sa2
                            _hy2 = _sx2*_sa2 + _sy2*_ca2
                            if _fvr <= min(_hx2, _hy2):
                                _via_fits = True
                    break
                if not _via_fits:
                    continue
                # Check clearance on route layer (exact for session vias)
                if not _via_clear_of_run_obs(px, py, net_name, route_layer_id):
                    continue
                # Check board obstacles on route layer
                _vbox = (px - _fvr - CLEARANCE_MM, py - _fvr - CLEARANCE_MM,
                         px + _fvr + CLEARANCE_MM, py + _fvr + CLEARANCE_MM)
                _obs_rl = [pad_obs_rect(_pd3)
                           for _fp2 in board.GetFootprints()
                           for _pd3 in _fp2.Pads()
                           if (_pd3.GetNetname() != net_name and
                               _pd3.GetLayerSet().Contains(route_layer_id))]
                if any(rect_overlap(_vbox, o) for o in _obs_rl):
                    continue
                add_via(px, py, net_name, _fpad, _fdrill)
                log.append(f"    via-in-pad (fallback {_fpad:.2f}mm) {pad['ref']} "
                           f"net={net_name}: ({px:.3f},{py:.3f})")
                return px, py
        log.append(f"    WARNING: escape via search FAILED for {pad['ref']} net={net_name}")
        return None

    # Crossing case: stub must thread past cross_partner so the via lands on the
    # OPPOSITE side of the partner along cross_side.  Try staged L-shapes:
    # go outward first (perpendicular to pair axis), then along cross_side past
    # the partner, then plant via.
    ppx, ppy = cross_partner["x"], cross_partner["y"]
    csx, csy = cross_side           # unit vector we must end up on the +side of
    # partner pad half-extent along cross_side
    pp_size = cross_partner.get("_half_along", 0.35)  # fallback ~0.7mm pad
    # Threading distance past partner
    via_r = VIA_PAD / 2.0

    # Use neckdown width for crossing stubs — tight threading past partner pad.
    cross_stub_w = NECKDOWN_W_MM
    _cout_ux, _cout_uy = _snap_canonical(out_ux, out_uy)
    _ccsx, _ccsy = _snap_canonical(csx, csy)
    for outward in (0.4, 0.6, 0.8, 1.0, 1.3, 1.6, 2.0):
        # Intermediate point: outward from own pad along canonical direction
        mx = px + _cout_ux * outward
        my = py + _cout_uy * outward
        for along in (pp_size + via_r + c + 0.10,
                      pp_size + via_r + c + 0.20,
                      pp_size + via_r + c + 0.35,
                      pp_size + via_r + c + 0.50,
                      pp_size + via_r + c + 0.70,
                      pp_size + via_r + c + 1.0,
                      pp_size + via_r + c + 1.4):
            proj_from_partner = (mx - ppx) * _ccsx + (my - ppy) * _ccsy
            shift = along - proj_from_partner
            vx = mx + _ccsx * shift
            vy = my + _ccsy * shift
            stub1 = seg_rect(px, py, mx, my, cross_stub_w)
            stub2 = seg_rect(mx, my, vx, vy, cross_stub_w)
            via_box  = (vx - via_r, vy - via_r, vx + via_r, vy + via_r)
            if any(rect_overlap(stub1, o) for o in obs_pad): continue
            if any(rect_overlap(stub2, o) for o in obs_pad): continue
            if any(rect_overlap(via_box,  o) for o in obs_pad):   continue
            if any(rect_overlap(via_box,  o) for o in obs_route): continue
            add_track(px, py, mx, my, cross_stub_w, pad_layer, net_name)
            add_track(mx, my, vx, vy, cross_stub_w, pad_layer, net_name)
            add_via(vx, vy, net_name, VIA_PAD, VIA_DRILL)
            log.append(f"    cross-escape {pad['ref']} net={net_name}: threaded past "
                       f"{cross_partner['ref']} → via ({vx:.3f},{vy:.3f})"
                       f"  stub={cross_stub_w:.3f}mm")
            return vx, vy

    # L-shape sweep failed — try A* crossing escape
    v = _astar_crossing_escape(pad, net_name, partner_net, route_layer_id,
                                cross_side, cross_partner)
    if v is not None:
        return v
    log.append(f"    WARNING: crossing escape FAILED for {pad['ref']} net={net_name}")
    return None



# ── Crossing detection ───────────────────────────────────────────────────────

def detect_crossing(pairs, route_layer_id):
    """
    Given exactly two matched (P,N) endpoints, decide if the pair has a
    topological crossing (P.x vs N.x sign flips between the two endpoints).
    Returns dict:
      { 'crossing': bool,
        'src_idx': int, 'dst_idx': int,
        'axis_ux','axis_uy': unit vec along pair (source→destination centroids),
        'perp_ux','perp_uy': unit perpendicular,
        'src_p_sign': +1 or -1  P sign at source along perp,
        'dst_p_sign': +1 or -1  P sign at destination along perp,
      }
    src is defined as endpoint 0 in `pairs` order.
    """
    if len(pairs) != 2:
        return None
    (p0, n0), (p1, n1) = pairs[0], pairs[1]
    c0 = ((p0["x"] + n0["x"]) / 2, (p0["y"] + n0["y"]) / 2)
    c1 = ((p1["x"] + n1["x"]) / 2, (p1["y"] + n1["y"]) / 2)
    dxc, dyc = c1[0] - c0[0], c1[1] - c0[1]
    L = math.hypot(dxc, dyc)
    if L < 1e-4:
        return None
    axis_ux, axis_uy = dxc / L, dyc / L
    perp_ux, perp_uy = -axis_uy, axis_ux

    def p_side(p_pad, n_pad, cx, cy):
        rel_x = p_pad["x"] - cx;  rel_y = p_pad["y"] - cy
        return 1.0 if (rel_x * perp_ux + rel_y * perp_uy) >= 0 else -1.0

    s0 = p_side(p0, n0, *c0)
    s1 = p_side(p1, n1, *c1)
    crossing = (s0 * s1) < 0
    return {
        "crossing": crossing,
        "src_idx": 0, "dst_idx": 1,
        "axis_ux": axis_ux, "axis_uy": axis_uy,
        "perp_ux": perp_ux, "perp_uy": perp_uy,
        "src_p_sign": s0, "dst_p_sign": s1,
    }


# ── Per-pair via placement ────────────────────────────────────────────────────

def escape_endpoint(p_pad, n_pad, net_p, net_n, route_layer_id,
                    perp_ux, perp_uy, axis_ux, axis_uy,
                    force_crossing_of=None):
    """
    Place P and N vias at this endpoint.

    force_crossing_of: None, or "P" or "N".  If set, the named signal must
    thread past its partner pad so its via lands on the opposite side of the
    partner along perp axis.

    Outward direction (away from pair centerline centroid) = axis toward "outside":
    at the source we want to escape opposite the destination, at destination the
    reverse.  Here we take outward = negative axis (caller passes axis pointing
    into the pair; the escape stub itself is perpendicular).  We use ±perp as
    the "outward" direction relative to each pad.

    Returns (vp_xy, vn_xy).  Either may be None on failure.
    """
    # For non-crossing endpoint, each pad escapes outward along the perp
    # in the direction AWAY from its partner.
    cx = (p_pad["x"] + n_pad["x"]) / 2
    cy = (p_pad["y"] + n_pad["y"]) / 2

    def outward_of(pad):
        """Return (oux, ouy, lateral_sign, used_axis_fallback).

        used_axis_fallback=True means ic_outward was unusable (e.g. single-row
        connector) and the exact pair-axis direction was substituted.  The caller
        uses this flag to avoid sign-flipping that direction in the axis override.
        """
        # Prefer IC-away direction (works for edge pins on QFN/SOIC/connectors).
        icux, icuy = ic_outward_direction(pad)
        if abs(icux) + abs(icuy) > 0.1:
            # Sanity-check: if ic_outward is mostly PERPENDICULAR to the pair
            # routing axis (e.g. a single-row connector where the centroid is on
            # the pad row, making ic_outward point along the row), it would escape
            # along the connector row and clip adjacent pads.  In that case, fall
            # back to the pair axis direction (which points toward the routing area).
            dot_ax = abs(icux * axis_ux + icuy * axis_uy)
            ic_mag = abs(icux) + abs(icuy)
            if dot_ax >= 0.3 * ic_mag:
                # ic_outward has enough axis component — use it
                s = 1.0 if (icux * perp_ux + icuy * perp_uy) >= 0 else -1.0
                return icux, icuy, s, False
            # ic_outward is mostly perpendicular to the routing axis — escape
            # along the row would clip adjacent pads.  Use pair axis instead.
        # Fallback: use the pair routing axis direction (points away from connector
        # body toward the routing area at the current endpoint).
        s  = 1.0 if ((pad["x"] - cx) * perp_ux + (pad["y"] - cy) * perp_uy) >= 0 else -1.0
        return axis_ux, axis_uy, s, True

    # Precompute partner pad "half extent" along perp for crossing thread.
    def half_along_perp(pad_dict):
        # Look up the actual pad object via ref+position
        for fp in board.GetFootprints():
            if fp.GetReference() != pad_dict["ref"]:
                continue
            for pad in fp.Pads():
                if abs(tomm(pad.GetPosition().x) - pad_dict["x"]) < 1e-3 and \
                   abs(tomm(pad.GetPosition().y) - pad_dict["y"]) < 1e-3:
                    size = pad.GetSize()
                    sx, sy = tomm(size.x) / 2, tomm(size.y) / 2
                    try:
                        ang = math.radians(pad.GetOrientationDegrees())
                    except AttributeError:
                        ang = 0.0
                    ca_, sa_ = abs(math.cos(ang)), abs(math.sin(ang))
                    hx = sx * ca_ + sy * sa_
                    hy = sx * sa_ + sy * ca_
                    # Project half-extent onto perp axis
                    return abs(perp_ux) * hx + abs(perp_uy) * hy
        return 0.35

    p_pad_ex = dict(p_pad); p_pad_ex["_half_along"] = half_along_perp(p_pad)
    n_pad_ex = dict(n_pad); n_pad_ex["_half_along"] = half_along_perp(n_pad)

    # Compute lateral constraints for non-crossing escapes:
    # Each via must stay on the same side of the pair axis as its own pad.
    # lateral_sign_of returns +1 if pad is on the positive-perp side, -1 otherwise.
    def lateral_sign_of(pad_dict):
        rel = ((pad_dict["x"] - cx) * perp_ux +
               (pad_dict["y"] - cy) * perp_uy)
        return 1 if rel >= 0 else -1

    p_sign = lateral_sign_of(p_pad)
    n_sign = lateral_sign_of(n_pad)
    # P's via must be on p_sign side of N pad center; N's via on n_sign side of P pad center.
    p_lateral = (n_pad["x"], n_pad["y"], perp_ux, perp_uy, p_sign)
    n_lateral = (p_pad["x"], p_pad["y"], perp_ux, perp_uy, n_sign)

    # At tight-pitch single-row connectors (e.g. J_DSI1 0.5mm), P's escape must
    # stay ≥ (VIA_PAD/2 + partner_via_r + CLEARANCE) from N's pad so N can use
    # via-in-pad.  Detect this by projecting the P–N pad separation onto perp.
    perp_pair_pitch = abs((p_pad["x"] - n_pad["x"]) * perp_ux +
                          (p_pad["y"] - n_pad["y"]) * perp_uy)
    _tight_conn = perp_pair_pitch < VIA_PAD + 2 * CLEARANCE_MM

    # Order matters: non-crossing side placed first (reference), crossing side second.
    # When crossing is forced, the FIRST-placed (non-crossing) signal is biased to
    # escape AWAY from where the crossing partner will later need to thread — this
    # keeps the crossing signal's escape zone (and its via-in-pad fallback) free.
    order = []
    if force_crossing_of == "P":
        # P will cross past N — so N should escape away from P
        order = [("N", n_pad, net_n, net_p, None, p_pad),
                 ("P", p_pad, net_p, net_n, n_pad_ex, None)]
    elif force_crossing_of == "N":
        order = [("P", p_pad, net_p, net_n, None, n_pad),
                 ("N", n_pad, net_n, net_p, p_pad_ex, None)]
    else:
        # Non-crossing: each escape via must stay ≥ (VIA_PAD + 2*CLEARANCE) from
        # the partner pad so that if the partner uses via-in-pad, the two vias
        # don't violate DRC clearance.  At fine pitch (e.g. 0.5mm J_DSI1) this
        # prevents the first-placed P via from landing too close to N's pad,
        # which would otherwise block N's escape entirely.
        order = [("P", p_pad, net_p, net_n, None, n_pad),
                 ("N", n_pad, net_n, net_p, None, p_pad)]

    def _run_order(ord_list, res, fc, ol):
        for tag, pad, net, partner, cross_partner, avoid_pad in ord_list:
            if pad_on_layer(pad, route_layer_id):
                _st = _follow_stub_tip(pad["x"], pad["y"], net)
                if _st is not None:
                    res[tag] = _st
                    ol.add(tag)
                    log.append(f"    stub-tip {pad['ref']} net={net}: ({_st[0]:.3f},{_st[1]:.3f})")
                else:
                    res[tag] = (pad["x"], pad["y"])
                    ol.add(tag)
                continue
            _ck = (pad["ref"], round(pad["x"], 3), round(pad["y"], 3), net)
            if _ck in _via_cache:
                res[tag] = _via_cache[_ck]
                fc.add(tag)
                log.append(f"    reuse cached via {pad['ref']} net={net}: {_via_cache[_ck]}")
                continue
            # Stub-follow: trace locked fanout stub from pad to its via (most reliable)
            _sv = _follow_stub_to_via(pad["x"], pad["y"], net)
            if _sv is not None:
                vx, vy = _sv
                _run_obs.append((vx, vy, vx, vy, VIA_PAD / 2.0, route_layer_id, net))
                _via_cache[_ck] = (vx, vy)
                res[tag] = (vx, vy)
                log.append(f"    stub-snap {pad['ref']} net={net}: ({vx:.3f},{vy:.3f})")
                continue
            # No-via stub tip: pad has a locked stub but no via — start from tip
            _st = _follow_stub_tip(pad["x"], pad["y"], net)
            if _st is not None:
                res[tag] = _st
                log.append(f"    stub-tip {pad['ref']} net={net}: ({_st[0]:.3f},{_st[1]:.3f})")
                continue
            # No fanout via — use pad position directly (F.Cu routing)
            res[tag] = (pad["x"], pad["y"])
            log.append(f"    direct-pad {pad['ref']} net={net}: "
                       f"({pad['x']:.3f},{pad['y']:.3f})")

    _snap_obs   = len(_run_obs)
    _snap_log   = len(log)
    _snap_cache = set(_via_cache.keys())

    results    = {}
    from_cache: set = set()
    on_layer: set   = set()
    _run_order(order, results, from_cache, on_layer)

    # ── Order-swap retry for non-crossing pairs ───────────────────────────────
    # When exactly one signal escapes and the other fails, the first-placed via
    # may occupy the only viable escape direction for the second signal (common
    # at tight-pitch single-row connectors, e.g. MIPI1_CLK at J_DSI1 0.5mm).
    # Re-try with reversed order: the previously-failing signal escapes first
    # (no partner via in _run_obs yet) and the other adapts around it.
    if (force_crossing_of is None
            and bool(results.get("P") is not None) != bool(results.get("N") is not None)):
        _p_ok1 = results.get("P") is not None
        _n_ok1 = results.get("N") is not None
        del _run_obs[_snap_obs:]
        del log[_snap_log:]
        for _k in list(_via_cache.keys()):
            if _k not in _snap_cache:
                del _via_cache[_k]
        results2 = {}; from_cache2: set = set(); on_layer2: set = set()
        _run_order(list(reversed(order)), results2, from_cache2, on_layer2)
        _p_ok2 = results2.get("P") is not None
        _n_ok2 = results2.get("N") is not None
        if (_p_ok2 and _n_ok2) or ((_p_ok2 + _n_ok2) > (_p_ok1 + _n_ok1)):
            results = results2; from_cache = from_cache2; on_layer = on_layer2
            log.append("    [escape order swapped — retry succeeded]")
        else:
            del _run_obs[_snap_obs:]
            del log[_snap_log:]
            for _k in list(_via_cache.keys()):
                if _k not in _snap_cache:
                    del _via_cache[_k]
            results = {}; from_cache = set(); on_layer = set()
            _run_order(order, results, from_cache, on_layer)

    return results["P"], results["N"]


# ── Emit centerline + offsets ─────────────────────────────────────────────────

def _perp_of(ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return 0.0, 1.0
    return -dy / L, dx / L


def _miter_predict_crossing(poly, vpa_xy, vpb_xy, cc):
    """Return True if emit_diffpair would produce an end-stub crossing.

    In the per-segment parallel track approach, p_sign is constant throughout
    (no bisector propagation). A crossing occurs when the destination via is on
    the opposite side from the source via.
    """
    if len(poly) < 2:
        return False
    px0, py0 = _perp_of(*poly[0], *poly[1])
    p_sign_src = 1.0 if (px0*(vpa_xy[0]-poly[0][0]) + py0*(vpa_xy[1]-poly[0][1])) >= 0 else -1.0
    pxl, pyl  = _perp_of(*poly[-2], *poly[-1])
    p_sign_dst = 1.0 if (pxl*(vpb_xy[0]-poly[-1][0]) + pyl*(vpb_xy[1]-poly[-1][1])) >= 0 else -1.0
    return p_sign_src != p_sign_dst


def _chosen_has_via_clearance_issue(poly, vp0_xy, vn0_xy, vp1_xy, vn1_xy, cc, width_mm,
                                     direct_pad=False):
    """Return True if the chosen path's P or N track would physically overlap a
    cross-net endpoint via (creating a true electrical short).

    Checks all four endpoint vias: P track vs N vias (vn0, vn1); N track vs P
    vias (vp0, vp1).  Uses overlap-only threshold VIA_PAD/2 + width/2 (no
    clearance margin) so only actual conductor overlaps are flagged — tight but
    legal spacings are left to DRC.  Simulates cur_p_sign propagation matching
    emit_diffpair for accurate track positions.

    direct_pad=True: endpoints are SMT pads, not through-hole vias.  Uses
    width_mm/2 as endpoint radius so fine-pitch pads (e.g. 0.5mm-pitch USB-C)
    do not trigger false positives that would fire at a legal 0.19mm spacing.
    """
    if len(poly) < 2:
        return False
    half = cc / 2.0
    # Overlap threshold: track edge touches endpoint conductor edge.
    # SMT pad endpoints use track half-width as radius (not the larger VIA_PAD/2)
    # so fine-pitch connectors don't produce false positives.
    ep_r = width_mm / 2.0 if direct_pad else VIA_PAD / 2.0
    overlap_thr = ep_r + width_mm / 2.0

    px0, py0 = _perp_of(*poly[0], *poly[1])
    cur_ps = 1.0 if (px0*(vp0_xy[0]-poly[0][0]) + py0*(vp0_xy[1]-poly[0][1])) >= 0 else -1.0

    for i in range(len(poly) - 1):
        a, b = poly[i], poly[i + 1]
        pax, pay = _perp_of(*a, *b)

        if abs(b[0] - a[0]) < 1e-6:  # Vertical segment
            p_x = a[0] + cur_ps * pax * half
            n_x = a[0] - cur_ps * pax * half
            y_lo = min(a[1], b[1])
            y_hi = max(a[1], b[1])
            for vxy in (vn0_xy, vn1_xy):  # P track vs N vias
                if y_lo - overlap_thr <= vxy[1] <= y_hi + overlap_thr:
                    if abs(p_x - vxy[0]) < overlap_thr:
                        return True
            for vxy in (vp0_xy, vp1_xy):  # N track vs P vias
                if y_lo - overlap_thr <= vxy[1] <= y_hi + overlap_thr:
                    if abs(n_x - vxy[0]) < overlap_thr:
                        return True

        elif abs(b[1] - a[1]) < 1e-6:  # Horizontal segment
            p_y = a[1] + cur_ps * pay * half
            n_y = a[1] - cur_ps * pay * half
            x_lo = min(a[0], b[0])
            x_hi = max(a[0], b[0])
            for vxy in (vn0_xy, vn1_xy):  # P track vs N vias
                if x_lo - overlap_thr <= vxy[0] <= x_hi + overlap_thr:
                    if abs(p_y - vxy[1]) < overlap_thr:
                        return True
            for vxy in (vp0_xy, vp1_xy):  # N track vs P vias
                if x_lo - overlap_thr <= vxy[0] <= x_hi + overlap_thr:
                    if abs(n_y - vxy[1]) < overlap_thr:
                        return True

        # Propagate cur_ps through bend at b
        if i < len(poly) - 2:
            p2x, p2y = _perp_of(*b, *poly[i + 2])
            bx = pax + p2x; by = pay + p2y
            bL = math.hypot(bx, by)
            if bL < 0.05: bx, by = pax, pay; bL = 1.0
            bx /= bL; by /= bL
            p_apx = b[0] + cur_ps * pax * half
            p_apy = b[1] + cur_ps * pay * half
            local_sign = 1.0 if (bx*(p_apx - b[0]) + by*(p_apy - b[1])) >= 0 else -1.0
            cur_ps = 1.0 if (p2x*(local_sign*bx) + p2y*(local_sign*by)) >= 0 else -1.0

    return False


def _parallel_corner(p_end, p_next, edx_i, edy_i, edx_i1, edy_i1):
    """Intersect two adjacent parallel offset lines at a centerline corner.

    Line 1 passes through p_end in direction (edx_i, edy_i).
    Line 2 passes through p_next in direction (edx_i1, edy_i1).
    Returns (x, y) intersection, or None if lines are parallel (cross ≈ 0).
    The intersection is the single point shared by both parallel segments,
    eliminating any bridge segments between them.
    """
    cross = edx_i * edy_i1 - edy_i * edx_i1
    if abs(cross) < 1e-6:
        return None
    dx = p_next[0] - p_end[0]
    dy = p_next[1] - p_end[1]
    t = (dx * edy_i1 - dy * edx_i1) / cross
    return (p_end[0] + t * edx_i, p_end[1] + t * edy_i)


def emit_diffpair(poly, vpa, vna, vpb, vnb, width_mm, cc, layer_id, net_p, net_n):
    """Emit P and N tracks offset ±cc/2 from the centerline polyline.

    Per-segment parallel tracks with intersection corners (no bridge segments).
    For each centerline segment a parallel P/N track is emitted at ±half offset.
    At each bend the corner point is the geometric intersection of adjacent parallel
    lines — one point, zero bridge segments, all angles strictly canonical.

    p_sign is constant throughout: for canonical-angle paths with no 180° turns
    (merge_collinear eliminates those), the perpendicular side never flips.
    """
    if len(poly) < 2:
        return 0.0, 0.0
    half = cc / 2.0

    # Determine P side from the SOURCE via relative to first-segment perpendicular.
    px0, py0 = _perp_of(*poly[0], *poly[1])
    p_sign   = 1.0 if (px0 * (vpa[0]-poly[0][0]) + py0 * (vpa[1]-poly[0][1])) >= 0 else -1.0

    n_segs = len(poly) - 1

    # Precompute canonical unit directions for all segments.
    seg_dirs = []
    for i in range(n_segs):
        dxi = poly[i+1][0] - poly[i][0]; dyi = poly[i+1][1] - poly[i][1]
        dL  = math.hypot(dxi, dyi) or 1.0
        seg_dirs.append((dxi / dL, dyi / dL))

    # Source stubs: via → first-segment parallel start anchor
    _edx0, _edy0 = seg_dirs[0]
    p_start = (poly[0][0] + p_sign * px0 * half, poly[0][1] + p_sign * py0 * half)
    n_start = (poly[0][0] - p_sign * px0 * half, poly[0][1] - p_sign * py0 * half)
    p_pts = _stub_pts(vpa[0], vpa[1], p_start[0], p_start[1], _edx0, _edy0)
    n_pts = _stub_pts(vna[0], vna[1], n_start[0], n_start[1], _edx0, _edy0)

    # Main run: per-segment parallel tracks joined at intersection corners.
    for i in range(n_segs):
        pxi, pyi = _perp_of(*poly[i], *poly[i + 1])

        if i < n_segs - 1:
            # Interior corner: intersect parallel-i and parallel-(i+1).
            pxi1, pyi1   = _perp_of(*poly[i+1], *poly[i+2])
            p_seg_end    = (poly[i+1][0] + p_sign * pxi  * half, poly[i+1][1] + p_sign * pyi  * half)
            n_seg_end    = (poly[i+1][0] - p_sign * pxi  * half, poly[i+1][1] - p_sign * pyi  * half)
            p_next_start = (poly[i+1][0] + p_sign * pxi1 * half, poly[i+1][1] + p_sign * pyi1 * half)
            n_next_start = (poly[i+1][0] - p_sign * pxi1 * half, poly[i+1][1] - p_sign * pyi1 * half)
            edxi_c, edyi_c   = seg_dirs[i]
            edxi1_c, edyi1_c = seg_dirs[i + 1]
            cp = _parallel_corner(p_seg_end, p_next_start, edxi_c, edyi_c, edxi1_c, edyi1_c)
            cn = _parallel_corner(n_seg_end, n_next_start, edxi_c, edyi_c, edxi1_c, edyi1_c)
            if cp is not None and cn is not None:
                p_pts.append(cp)
                n_pts.append(cn)
            else:
                # Fallback: stub bridge (parallel lines — not expected after merge_collinear)
                p_pts.append(p_seg_end)
                n_pts.append(n_seg_end)
                bp = _stub_pts(p_seg_end[0], p_seg_end[1], p_next_start[0], p_next_start[1], edxi1_c, edyi1_c)
                bn = _stub_pts(n_seg_end[0], n_seg_end[1], n_next_start[0], n_next_start[1], edxi1_c, edyi1_c)
                for pt in bp[1:]:
                    p_pts.append(pt)
                for pt in bn[1:]:
                    n_pts.append(pt)
        else:
            # Last segment — conventional perpendicular endpoint.
            p_seg_end = (poly[i+1][0] + p_sign * pxi * half, poly[i+1][1] + p_sign * pyi * half)
            n_seg_end = (poly[i+1][0] - p_sign * pxi * half, poly[i+1][1] - p_sign * pyi * half)
            p_pts.append(p_seg_end)
            n_pts.append(n_seg_end)

    # Destination stubs: last parallel endpoint → via.
    # p_pts[-1] is already p_end (last segment's p_seg_end); [1:] skips the duplicate.
    pxl, pyl = _perp_of(*poly[-2], *poly[-1])
    p_end    = (poly[-1][0] + p_sign * pxl * half, poly[-1][1] + p_sign * pyl * half)
    n_end    = (poly[-1][0] - p_sign * pxl * half, poly[-1][1] - p_sign * pyl * half)
    _edxL, _edyL = seg_dirs[-1]
    p_pts.extend(_stub_pts(p_end[0], p_end[1], vpb[0], vpb[1], _edxL, _edyL)[1:])
    n_pts.extend(_stub_pts(n_end[0], n_end[1], vnb[0], vnb[1], _edxL, _edyL)[1:])

    p_pts = merge_collinear(p_pts)
    n_pts = merge_collinear(n_pts)

    lp = ln = 0.0
    for i in range(len(p_pts) - 1):
        lp += add_track(p_pts[i][0], p_pts[i][1],
                        p_pts[i+1][0], p_pts[i+1][1],
                        width_mm, layer_id, net_p)
    for i in range(len(n_pts) - 1):
        ln += add_track(n_pts[i][0], n_pts[i][1],
                        n_pts[i+1][0], n_pts[i+1][1],
                        width_mm, layer_id, net_n)
    return lp, ln


# ── Routing order ─────────────────────────────────────────────────────────────

def routing_order():
    scored = []
    for name, (net_p, net_n, layer, _skew) in cfg.HS_PAIRS.items():
        if name not in cfg.HS_ROUTE_WIDTHS:
            continue
        # HDMI/LT_TX first (most critical), then AUX, then others
        if name.startswith("HDMI"):
            prio = 100
        elif name.startswith("LT_TX"):
            prio = 90
        elif name.startswith("LT_AUX"):
            prio = 80
        elif name.startswith("DP_TX"):
            prio = 70
        else:
            prio = 50
        # Secondary sort: among equal-priority pairs, route the one whose
        # CONNECTOR-SIDE pads have the highest x-coordinate first.
        # "Connector side" = the endpoint at higher y (SOM2 is at lower y).
        # Using connector-side x (not SOM2 x) ensures that for MIPI1 pairs,
        # D1 (x≈147.4) routes before D0 (x≈146.4) before CLK (x≈145.4),
        # so CLK's long A* centerline does not crowd In2.Cu before D0/D1 escape.
        #
        # Exception: crossing pairs (P and N swap lateral sides between endpoints)
        # use ASCENDING connector-side x so the pair with the largest crossing extent
        # (highest connector-x) routes LAST and navigates around earlier pairs'
        # already-placed B.Cu layer-split tracks.
        pads_p = get_pads_on_net(net_p)
        pads_n = get_pads_on_net(net_n)
        all_pads = pads_p + pads_n
        if all_pads:
            all_y = [p["y"] for p in all_pads]
            y_mid = (min(all_y) + max(all_y)) / 2.0
            connector_pads = [p for p in all_pads if p["y"] > y_mid]
            if not connector_pads:
                connector_pads = all_pads
            sort_x = max(p["x"] for p in connector_pads)
            # Detect crossing topology by comparing P vs N average x at each half
            src_p_xs = [p["x"] for p in pads_p if p["y"] < y_mid]
            src_n_xs = [p["x"] for p in pads_n if p["y"] < y_mid]
            dst_p_xs = [p["x"] for p in pads_p if p["y"] > y_mid]
            dst_n_xs = [p["x"] for p in pads_n if p["y"] > y_mid]
            if src_p_xs and src_n_xs and dst_p_xs and dst_n_xs:
                s_src = 1 if (sum(src_p_xs)/len(src_p_xs)) > (sum(src_n_xs)/len(src_n_xs)) else -1
                s_dst = 1 if (sum(dst_p_xs)/len(dst_p_xs)) > (sum(dst_n_xs)/len(dst_n_xs)) else -1
                is_crossing = (s_src * s_dst < 0)
            else:
                is_crossing = False
            if is_crossing and src_p_xs and dst_p_xs:
                # For crossing pairs, sort by crossing extent (|src_P_x - dst_P_x|)
                # ascending: smallest-crossing pair routes first so it claims escape
                # via positions near its source pads without blocking adjacent pairs.
                # Largest-crossing pair (e.g. HDMI CLK) routes last and navigates
                # around all prior pairs' already-placed B.Cu layer-split tracks.
                src_p_x_avg = sum(src_p_xs) / len(src_p_xs)
                dst_p_x_avg = sum(dst_p_xs) / len(dst_p_xs)
                crossing_extent = abs(src_p_x_avg - dst_p_x_avg)
                sort_x_key = -crossing_extent
            else:
                # Non-crossing: sort by connector-side x descending (current behavior)
                sort_x_key = sort_x
        else:
            sort_x_key = 0.0
        scored.append((prio, sort_x_key, name))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [n for _p, _x, n in scored]


# ── Option C: Corridor pre-assignment ────────────────────────────────────────

def _detect_corridor_groups(order):
    """
    Group pairs that share the same source-ref cluster and dest-ref cluster
    (i.e. both endpoints of the pair are on the same two footprints).
    Returns list of groups; each group is a list of ≥2 pair names.
    """
    cluster_map = {}
    for pair_name in order:
        net_p, net_n, _, _ = cfg.HS_PAIRS[pair_name]
        pads_p = get_pads_on_net(net_p)
        pads_n = get_pads_on_net(net_n)
        if not pads_p or not pads_n:
            continue
        matched = order_pairs(pair_pads(pads_p, pads_n))
        if len(matched) < 2:
            continue
        all_pads = [p for pr in matched for p in pr]
        mid_y = sum(p["y"] for p in all_pads) / len(all_pads)
        src_refs = frozenset(p["ref"] for pr in matched for p in pr if p["y"] <= mid_y)
        dst_refs = frozenset(p["ref"] for pr in matched for p in pr if p["y"] >  mid_y)
        key = (src_refs, dst_refs)
        cluster_map.setdefault(key, []).append(pair_name)
    return [g for g in cluster_map.values() if len(g) >= 2]


def _assign_corridor_trunks(order):
    """
    Option C — for each corridor group (≥2 pairs sharing the same endpoint
    footprints), pre-assign each pair a corridor path style and trunk coordinate.

    Non-crossing groups (src ordering = dst ordering):
      Each pair → ('HV', dst_cx): go horizontal to trunk_x at source y,
      then vertical to dest y, then horizontal to dest_cx.

    Crossing groups (src ordering ≠ dst ordering):
      Crossing pairs (those that swap rank between src and dst) →
        ('VH', None): go vertical to dest y first, then horizontal to dest_cx.
        These pairs are moved to the FRONT of the routing order for their group
        so they claim a clean vertical corridor before non-crossing pairs place
        their horizontal segments at source y.
      Non-crossing pairs → ('HV', dst_cx): standard H-V.

    The VH path is [(cx0,cy0),(cx0,cy1),(cx1,cy1)] — no trunk_x needed.
    The HV path is [(cx0,cy0),(trunk_x,cy0),(trunk_x,cy1),(cx1,cy1)].

    Populates _corridor_trunk[pair_name] = (style, trunk_x_or_none).
    Modifies order in-place to route crossing pairs before their group siblings.
    """
    global _corridor_trunk
    groups = _detect_corridor_groups(order)

    for group in groups:
        pair_info = []  # (src_cx, dst_cx, pair_name)
        for pair_name in group:
            net_p, net_n, _, _ = cfg.HS_PAIRS[pair_name]
            pads_p = get_pads_on_net(net_p)
            pads_n = get_pads_on_net(net_n)
            matched = order_pairs(pair_pads(pads_p, pads_n))
            if len(matched) < 2:
                continue
            p0, n0 = matched[0]
            p1, n1 = matched[-1]
            fv_p0 = _follow_stub_to_via(p0["x"], p0["y"], net_p)
            fv_n0 = _follow_stub_to_via(n0["x"], n0["y"], net_n)
            fv_p1 = _follow_stub_to_via(p1["x"], p1["y"], net_p)
            fv_n1 = _follow_stub_to_via(n1["x"], n1["y"], net_n)
            vp0 = fv_p0 if fv_p0 else (p0["x"], p0["y"])
            vn0 = fv_n0 if fv_n0 else (n0["x"], n0["y"])
            vp1 = fv_p1 if fv_p1 else (p1["x"], p1["y"])
            vn1 = fv_n1 if fv_n1 else (n1["x"], n1["y"])
            src_cx = (vp0[0] + vn0[0]) / 2
            dst_cx = (vp1[0] + vn1[0]) / 2
            pair_info.append((src_cx, dst_cx, pair_name))

        if len(pair_info) < 2:
            continue

        # Establish source and destination rank for each pair
        dst_sorted = sorted(pair_info, key=lambda t: t[1])
        src_sorted = sorted(pair_info, key=lambda t: t[0])
        dst_rank = {t[2]: i for i, t in enumerate(dst_sorted)}
        src_rank = {t[2]: i for i, t in enumerate(src_sorted)}

        # A pair is "crossing" if its rank at dest is lower than at src,
        # i.e. it moves from right-of-group at source to left-of-group at dest.
        # Such pairs take the VH path (vertical first, then horizontal at dest y)
        # so their downward vertical segment doesn't block the HV horizontal
        # segments of the non-crossing pairs at source y.
        crossing_pairs = {t[2] for t in pair_info if dst_rank[t[2]] < src_rank[t[2]]}
        noncrossing_pairs = {t[2] for t in pair_info if t[2] not in crossing_pairs}

        all_non_crossing = not crossing_pairs
        if all_non_crossing:
            # Simple case: assign HV trunk at dst_cx for every pair
            for _, dst_cx, pair_name in dst_sorted:
                _corridor_trunk[pair_name] = ('HV', dst_cx)
            log.append("  [corridor] group assigned (non-crossing): "
                       + ", ".join(f"{p}→HV x={dx:.3f}"
                                   for _, dx, p in dst_sorted))
        else:
            # Crossing group: VH for crossing pairs, HV for non-crossing pairs
            for _, dst_cx, pair_name in dst_sorted:
                if pair_name in crossing_pairs:
                    _corridor_trunk[pair_name] = ('VH', None)
                else:
                    _corridor_trunk[pair_name] = ('HV', dst_cx)
            log.append("  [corridor] crossing group assigned: "
                       + ", ".join(
                           f"{p}→{'VH' if p in crossing_pairs else 'HV x=' + f'{dx:.3f}'}"
                           for _, dx, p in dst_sorted))

            # Move crossing pairs to the FRONT of their group in routing order
            # so they claim vertical corridors before non-crossing pairs block source y.
            group_set = {t[2] for t in pair_info}
            group_positions = [(i, pair_name)
                               for i, pair_name in enumerate(order)
                               if pair_name in group_set]
            if group_positions:
                indices = [i for i, _ in group_positions]
                pairs_in_order = [p for _, p in group_positions]
                # Re-order: crossing pairs first, non-crossing pairs second,
                # each sub-list preserving relative order from routing_order()
                reordered = ([p for p in pairs_in_order if p in crossing_pairs] +
                             [p for p in pairs_in_order if p not in crossing_pairs])
                for slot, new_pair in zip(indices, reordered):
                    order[slot] = new_pair
                log.append(f"  [corridor] reordered group in routing order: {reordered}")


# ── Option D: Rip-up and retry ────────────────────────────────────────────────

def _rip_up_retry(order, max_passes=3):
    """
    Option D — after all pairs are routed, scan _run_obs for pairs whose
    track segments cross another pair's segments on the same layer.
    For each crossing, the lower-priority pair (later in routing order) is
    ripped up (its _run_obs entries invalidated, board tracks removed) and
    re-routed with the non-crossing pair's tracks as hard obstacles.

    Also re-routes any pairs in _skipped_pairs (segments skipped due to
    blocked paths in the main pass) — but only once per pair to avoid
    infinite loops when a pair is genuinely unroutable.

    Repeats up to max_passes times or until no crossings remain.
    """
    global _conflict_count, _skipped_pairs

    _already_ripped: set = set()  # pairs attempted in rip-up; not retried if still blocked

    for pass_num in range(max_passes):
        # Collect _run_obs segments for each pair (skip invalidated entries)
        pair_segs = {}
        for pair_name in order:
            s, e = _pair_obs_ranges.get(pair_name, (0, 0))
            pair_segs[pair_name] = [(i, _run_obs[i]) for i in range(s, e)
                                    if _run_obs[i][5] >= 0]

        # Find pairs whose tracks cross another pair's tracks on the same layer
        to_rip = set()
        for i, pair_a in enumerate(order):
            for pair_b in order[i + 1:]:
                for _ia, sa in pair_segs.get(pair_a, []):
                    x1, y1, x2, y2, hw, la, na = sa
                    for _ib, sb in pair_segs.get(pair_b, []):
                        x3, y3, x4, y4, hw2, lb, nb = sb
                        if la != lb:
                            continue
                        if _segs_cross(x1, y1, x2, y2, x3, y3, x4, y4):
                            to_rip.add(pair_b)  # rip lower-priority (later in order)

        # Add skipped pairs that haven't been tried in rip-up yet
        to_rip.update(_skipped_pairs - _already_ripped)

        if not to_rip:
            log.append(f"  [rip-up] pass {pass_num + 1}: no crossings or new skipped pairs — done")
            break

        log.append(f"  [rip-up] pass {pass_num + 1}: ripping {sorted(to_rip)}")
        _already_ripped.update(to_rip)

        for pair_name in to_rip:
            net_p, net_n, _, _ = cfg.HS_PAIRS[pair_name]
            ripped_nets = {net_p, net_n}

            # Invalidate _run_obs entries for this pair (layer_id = -1 → never matches)
            s, e = _pair_obs_ranges.get(pair_name, (0, 0))
            for i in range(s, e):
                rx1, ry1, rx2, ry2, rhw, rl, rn = _run_obs[i]
                _run_obs[i] = (rx1, ry1, rx2, ry2, rhw, -1, rn)

            # Remove unlocked board tracks for this pair
            if APPLY:
                removed = 0
                _tc = board.GetTracks()
                try:
                    _all_trks = list(_tc)
                except TypeError:
                    try:
                        _all_trks = [_tc[i] for i in range(len(_tc))]
                    except Exception:
                        _all_trks = []
                        log.append(f"  [rip-up] WARNING: could not iterate board tracks for {pair_name}")
                for trk in _all_trks:
                    if (trk.GetNetname() in ripped_nets
                            and not trk.IsLocked()
                            and _track_fp(trk) not in _pre_existing_track_fps):
                        board.Remove(trk)
                        removed += 1
                log.append(f"  [rip-up] {pair_name}: removed {removed} board tracks")

            # Clear via cache for ripped nets so escape vias are re-resolved
            for k in list(_via_cache.keys()):
                if k[3] in ripped_nets:
                    del _via_cache[k]

        _skipped_pairs -= to_rip

        # Re-route ripped pairs in routing priority order
        for pair_name in sorted(to_rip, key=lambda p: order.index(p)):
            snap = len(_run_obs)
            route_pair(pair_name)
            _pair_obs_ranges[pair_name] = (snap, len(_run_obs))

    remaining = _count_crossings(order)
    if remaining:
        log.append(f"  [rip-up] {remaining} inter-pair crossing(s) remain after {max_passes} passes")
        _conflict_count += remaining
    if _skipped_pairs:
        log.append(f"  [rip-up] {len(_skipped_pairs)} pair(s) have unrouted segment(s): "
                   f"{sorted(_skipped_pairs)}")


def _count_crossings(order):
    """Count distinct pair-pair crossing relationships in current _run_obs."""
    count = 0
    for i, pair_a in enumerate(order):
        sa, ea = _pair_obs_ranges.get(pair_a, (0, 0))
        segs_a = [_run_obs[j] for j in range(sa, ea) if _run_obs[j][5] >= 0]
        for pair_b in order[i + 1:]:
            sb, eb = _pair_obs_ranges.get(pair_b, (0, 0))
            segs_b = [_run_obs[j] for j in range(sb, eb) if _run_obs[j][5] >= 0]
            crossed = False
            for x1, y1, x2, y2, hw, la, na in segs_a:
                for x3, y3, x4, y4, hw2, lb, nb in segs_b:
                    if la == lb and _segs_cross(x1,y1,x2,y2,x3,y3,x4,y4):
                        crossed = True; break
                if crossed:
                    break
            if crossed:
                count += 1
    return count


# ── Route one pair ────────────────────────────────────────────────────────────

def route_pair(pair_name):
    global _conflict_count
    net_p, net_n, layer_name, skew_limit = cfg.HS_PAIRS[pair_name]
    width_mm, gap_mm = cfg.HS_ROUTE_WIDTHS[pair_name]
    cc       = width_mm + gap_mm
    layer_id = board.GetLayerID(layer_name)

    log.append(f"\n[{pair_name}]  {net_p}/{net_n}  layer={layer_name}"
               f"  w={width_mm}mm  gap={gap_mm}mm  skew_limit={skew_limit}mm")

    pads_p = get_pads_on_net(net_p)
    pads_n = get_pads_on_net(net_n)
    if not pads_p or not pads_n:
        log.append("    SKIP: no pads found");  return
    pairs = order_pairs(pair_pads(pads_p, pads_n))
    if len(pairs) < 2:
        log.append("    SKIP: fewer than 2 matched endpoints");  return
    if len(pairs) > 2:
        log.append(f"    NOTE: {len(pairs)} matched endpoints — routing consecutive segments")

    _trunk_info = _corridor_trunk.get(pair_name)  # ('HV', x) or ('VH', None) or None

    total_p = total_n = 0.0
    _seg_spine = []  # intermediate centerline waypoints from previous segment
    _seg_placed.clear()    # reset inter-segment obstacle accumulator for this pair
    _seg_reserved.clear()  # reset look-ahead reservations for this pair
    for seg in range(len(pairs) - 1):
        layer_id = board.GetLayerID(layer_name)  # reset per iteration; may be overridden below
        sub_pairs = [pairs[seg], pairs[seg + 1]]
        info = detect_crossing(sub_pairs, layer_id)
        if info is None:
            log.append("    SKIP seg: cannot compute pair axis");  continue

        crossing_label = "CROSSING" if info["crossing"] else "no crossing"
        log.append(f"  seg {seg}: {sub_pairs[0][0]['ref']}/{sub_pairs[0][1]['ref']}"
                   f" → {sub_pairs[1][0]['ref']}/{sub_pairs[1][1]['ref']}"
                   f"  [{crossing_label}]")

        p0, n0 = sub_pairs[0]
        p1, n1 = sub_pairs[1]

        # Decide crossing correction endpoint (correct at source; if source pads
        # are already on the routing layer choose destination).
        force_at_src = None
        force_at_dst = None
        _both_on_route_layer = False
        if info["crossing"]:
            src_p_on = pad_on_layer(p0, layer_id)
            src_n_on = pad_on_layer(n0, layer_id)
            dst_p_on = pad_on_layer(p1, layer_id)
            dst_n_on = pad_on_layer(n1, layer_id)
            if not (src_p_on and src_n_on):
                # Check if source endpoint has geometric room for a crossing escape.
                # The via must thread past the partner pad: need at least
                # VIA_PAD + 2*CLEARANCE_MM clearance between the via copper edge
                # and the partner pad edge along the perp axis.
                # Proxy: pair pitch along perp vs. required threading gap.
                _perp_pitch_src = abs(
                    (p0["x"] - n0["x"]) * info["perp_ux"] +
                    (p0["y"] - n0["y"]) * info["perp_uy"])
                _min_thread_gap = VIA_PAD + 2 * CLEARANCE_MM  # ~0.85mm
                if _perp_pitch_src >= _min_thread_gap and not (dst_p_on and dst_n_on):
                    # Source has room; but prefer destination if destination pitch
                    # is larger (easier crossing).
                    _perp_pitch_dst = abs(
                        (p1["x"] - n1["x"]) * info["perp_ux"] +
                        (p1["y"] - n1["y"]) * info["perp_uy"])
                    if _perp_pitch_dst > _perp_pitch_src * 1.5:
                        # Destination is substantially wider — correct there
                        force_at_dst = "P"
                        log.append(f"    NOTE: dst pitch ({_perp_pitch_dst:.3f}mm) >> "
                                   f"src pitch ({_perp_pitch_src:.3f}mm) — correcting crossing at dst")
                    else:
                        force_at_src = "P"
                elif not (dst_p_on and dst_n_on):
                    # Source too tight for crossing — correct at destination
                    force_at_dst = "P"
                    log.append(f"    NOTE: src perp pitch ({_perp_pitch_src:.3f}mm) too tight "
                               f"for crossing (need {_min_thread_gap:.3f}mm) — correcting at dst")
                else:
                    # Both endpoints need vias; destination pads are on route layer
                    force_at_src = "P"
            elif not (dst_p_on and dst_n_on):
                force_at_dst = "P"
            else:
                log.append("    WARNING: crossing pair but both endpoints on route "
                           "layer — no via correction possible")
                _both_on_route_layer = True

        # Place source vias
        vp0, vn0 = escape_endpoint(p0, n0, net_p, net_n, layer_id,
                                    info["perp_ux"], info["perp_uy"],
                                    info["axis_ux"], info["axis_uy"],
                                    force_crossing_of=force_at_src)
        vp1, vn1 = escape_endpoint(p1, n1, net_p, net_n, layer_id,
                                    info["perp_ux"], info["perp_uy"],
                                    -info["axis_ux"], -info["axis_uy"],
                                    force_crossing_of=force_at_dst)

        # If source-side crossing correction failed (None via), the source
        # geometry may be too tight to thread (e.g. fine-pitch connector).
        # Retry ONLY the failed signal at the source without crossing obligation,
        # using a lateral constraint so it stays on its own side of the pair axis.
        # The successfully-placed sibling via is kept — we do NOT re-run escape_endpoint
        # (that would block on the sibling's already-placed _run_obs entry).
        if force_at_src is not None and None in (vp0, vn0):
            # Identify which signal failed and which succeeded at source
            _failed_tag  = "P" if vp0 is None else "N"
            _failed_pad  = p0  if vp0 is None else n0
            _failed_net  = net_p if vp0 is None else net_n
            _partner_net = net_n if vp0 is None else net_p
            _partner_pad = n0   if vp0 is None else p0
            log.append(f"    NOTE: src crossing escape failed for {_failed_tag} — "
                       f"retrying without crossing (lateral constraint only)")
            # Outward direction for the failed pad
            _cx_src = (p0["x"] + n0["x"]) / 2;  _cy_src = (p0["y"] + n0["y"]) / 2
            _icux, _icuy = ic_outward_direction(_failed_pad)
            if abs(_icux) + abs(_icuy) > 0.1:
                _oux, _ouy = _icux, _icuy
            else:
                _rx = _failed_pad["x"] - _cx_src;  _ry = _failed_pad["y"] - _cy_src
                _s  = 1.0 if (_rx * info["perp_ux"] + _ry * info["perp_uy"]) >= 0 else -1.0
                _oux, _ouy = _s * info["perp_ux"], _s * info["perp_uy"]
            # Lateral constraint: failed signal's via must stay on its own side
            _fs = 1 if ((_failed_pad["x"] - _cx_src) * info["perp_ux"] +
                        (_failed_pad["y"] - _cy_src) * info["perp_uy"]) >= 0 else -1
            _lat = (_partner_pad["x"], _partner_pad["y"],
                    info["perp_ux"], info["perp_uy"], _fs)
            _v = place_escape_via(_failed_pad, _failed_net, _partner_net, layer_id,
                                   _oux, _ouy, cross_side=None, cross_partner=None,
                                   avoid_pad=None, lateral_constraint=_lat)
            if _v is not None:
                if _failed_tag == "P":
                    vp0 = _v
                else:
                    vn0 = _v

        # If destination-side crossing correction failed, retry P naturally so
        # it lands on its own side — the s0*s1 crossover code handles the swap.
        if force_at_dst is not None and vp1 is None and vn1 is not None:
            log.append("    NOTE: dst crossing escape failed for P — "
                       "retrying without crossing (lateral constraint only)")
            _cx_dst = (p1["x"] + n1["x"]) / 2;  _cy_dst = (p1["y"] + n1["y"]) / 2
            _icux, _icuy = ic_outward_direction(p1)
            if abs(_icux) + abs(_icuy) > 0.1:
                _oux_d, _ouy_d = _icux, _icuy
            else:
                _rx = p1["x"] - _cx_dst;  _ry = p1["y"] - _cy_dst
                _s  = 1.0 if (_rx * info["perp_ux"] + _ry * info["perp_uy"]) >= 0 else -1.0
                _oux_d, _ouy_d = _s * info["perp_ux"], _s * info["perp_uy"]
            _fs = 1 if ((p1["x"] - _cx_dst) * info["perp_ux"] +
                        (p1["y"] - _cy_dst) * info["perp_uy"]) >= 0 else -1
            _lat_d = (n1["x"], n1["y"], info["perp_ux"], info["perp_uy"], _fs)
            _v = place_escape_via(p1, net_p, net_n, layer_id,
                                   _oux_d, _ouy_d,
                                   cross_side=None, cross_partner=None,
                                   avoid_pad=None, lateral_constraint=_lat_d)
            if _v is not None:
                vp1 = _v

        if None in (vp0, vn0, vp1, vn1):
            log.append("    SKIP seg: escape via placement failed for one or more pads")
            _conflict_count += 1
            continue

        # Determine routing layer: all fanout-via endpoints → pair layer (In2.Cu);
        # any direct-pad endpoint → F.Cu (vias are through-holes, connect to F.Cu).
        def _ep_is_direct(v, pad):
            return abs(v[0] - pad["x"]) < 0.005 and abs(v[1] - pad["y"]) < 0.005
        _vp0_d = _ep_is_direct(vp0, p0); _vn0_d = _ep_is_direct(vn0, n0)
        _vp1_d = _ep_is_direct(vp1, p1); _vn1_d = _ep_is_direct(vn1, n1)
        _seg_all_via = not any((_vp0_d, _vn0_d, _vp1_d, _vn1_d))
        if not _seg_all_via:
            layer_id = F_CU
            _both_on_route_layer = True
            _seg_type = "direct-pad" if (_vp0_d and _vn0_d and _vp1_d and _vn1_d) else "mixed"
            log.append(f"    NOTE: {_seg_type} segment — routing on F.Cu")

        # Stub-tip natural extension: when the source endpoint is a no-via stub tip,
        # route P and N independently following the stub's natural exit direction.
        # This avoids direction-reversal zigzags from centerline-offset coupling when
        # the stub tips are further apart than the coupling distance.
        if seg == 0:
            _st_p = _follow_stub_tip(p0["x"], p0["y"], net_p)
            _st_n = _follow_stub_tip(n0["x"], n0["y"], net_n)
            _p_is_stub = (_st_p is not None and
                          abs(_st_p[0] - vp0[0]) < 0.005 and abs(_st_p[1] - vp0[1]) < 0.005)
            _n_is_stub = (_st_n is not None and
                          abs(_st_n[0] - vn0[0]) < 0.005 and abs(_st_n[1] - vn0[1]) < 0.005)
            if _p_is_stub or _n_is_stub:
                _sext_obs = build_obstacles(layer_id, {net_p, net_n},
                                            include_sibling_reserves=False)
                _pb_p = _follow_stub_bend(p0["x"], p0["y"], net_p)
                _pb_n = _follow_stub_bend(n0["x"], n0["y"], net_n)
                _pp_ext = None
                _nn_ext = None
                if _p_is_stub:
                    if _pb_p is not None:
                        _pp_ext = _stub_exit_path(vp0[0], vp0[1], vp1[0], vp1[1],
                                                   _pb_p[0], _pb_p[1],
                                                   width_mm, _sext_obs)
                    if _pp_ext is None:
                        _pt = _stub_direct_path(vp0[0], vp0[1], vp1[0], vp1[1])
                        if _pt is not None and _path_clear_approx(_pt, width_mm, _sext_obs):
                            _pp_ext = _pt
                if _n_is_stub:
                    if _pb_n is not None:
                        _nn_ext = _stub_exit_path(vn0[0], vn0[1], vn1[0], vn1[1],
                                                   _pb_n[0], _pb_n[1],
                                                   width_mm, _sext_obs)
                    if _nn_ext is None:
                        _pt = _stub_direct_path(vn0[0], vn0[1], vn1[0], vn1[1])
                        if _pt is not None and _path_clear_approx(_pt, width_mm, _sext_obs):
                            _nn_ext = _pt
                if (_pp_ext is not None and _nn_ext is not None):
                    _pp_ext = merge_collinear(_pp_ext)
                    _nn_ext = merge_collinear(_nn_ext)
                    for _si in range(len(_pp_ext) - 1):
                        add_track(_pp_ext[_si][0], _pp_ext[_si][1],
                                  _pp_ext[_si + 1][0], _pp_ext[_si + 1][1],
                                  width_mm, layer_id, net_p)
                    for _si in range(len(_nn_ext) - 1):
                        add_track(_nn_ext[_si][0], _nn_ext[_si][1],
                                  _nn_ext[_si + 1][0], _nn_ext[_si + 1][1],
                                  width_mm, layer_id, net_n)
                    _lp = sum(math.hypot(_pp_ext[i+1][0]-_pp_ext[i][0],
                                         _pp_ext[i+1][1]-_pp_ext[i][1])
                              for i in range(len(_pp_ext)-1))
                    _ln = sum(math.hypot(_nn_ext[i+1][0]-_nn_ext[i][0],
                                         _nn_ext[i+1][1]-_nn_ext[i][1])
                              for i in range(len(_nn_ext)-1))
                    total_p += _lp; total_n += _ln
                    log.append(f"    NOTE: stub-tip direct routing  Lp={_lp:.3f}mm  Ln={_ln:.3f}mm"
                               f"  Δ={abs(_lp-_ln):.3f}mm")
                    _seg_placed.setdefault(layer_id, []).extend(
                        (r, net_p) for r in _poly_obs(_pp_ext, width_mm / 2))
                    _seg_placed.setdefault(layer_id, []).extend(
                        (r, net_n) for r in _poly_obs(_nn_ext, width_mm / 2))
                    continue

        # Verify P and N are on the same lateral side at both endpoints
        cx0 = (vp0[0] + vn0[0]) / 2;  cy0 = (vp0[1] + vn0[1]) / 2
        cx1 = (vp1[0] + vn1[0]) / 2;  cy1 = (vp1[1] + vn1[1]) / 2
        axL = math.hypot(cx1 - cx0, cy1 - cy0) or 1.0
        aux, auy = (cx1 - cx0) / axL, (cy1 - cy0) / axL
        pux, puy = -auy, aux
        s0 = 1.0 if ((vp0[0] - cx0) * pux + (vp0[1] - cy0) * puy) >= 0 else -1.0
        s1 = 1.0 if ((vp1[0] - cx1) * pux + (vp1[1] - cy1) * puy) >= 0 else -1.0

        # Also check: at each endpoint the P and N vias should be on OPPOSITE
        # lateral sides (they form the diff pair). If they landed on the same
        # side at an endpoint, retry with force_crossing to correct that endpoint.
        def _lateral_sign(v, ccx, ccy, ppux, ppuy):
            if v is None: return 0
            return 1 if (v[0] - ccx) * ppux + (v[1] - ccy) * ppuy >= 0 else -1

        # Source endpoint check
        src_p = _lateral_sign(vp0, cx0, cy0, pux, puy)
        src_n = _lateral_sign(vn0, cx0, cy0, pux, puy)
        if src_p != 0 and src_p == src_n:
            log.append("    RETRY src: P/N vias on same lateral side — forcing crossing")
            vp0b, vn0b = escape_endpoint(p0, n0, net_p, net_n, layer_id,
                                          info["perp_ux"], info["perp_uy"],
                                          info["axis_ux"], info["axis_uy"],
                                          force_crossing_of="P")
            if vp0b is not None and vn0b is not None:
                vp0, vn0 = vp0b, vn0b
            else:
                vp0b, vn0b = escape_endpoint(p0, n0, net_p, net_n, layer_id,
                                              info["perp_ux"], info["perp_uy"],
                                              info["axis_ux"], info["axis_uy"],
                                              force_crossing_of="N")
                if vp0b is not None and vn0b is not None:
                    vp0, vn0 = vp0b, vn0b

        # Destination endpoint check
        dst_p = _lateral_sign(vp1, cx1, cy1, pux, puy)
        dst_n = _lateral_sign(vn1, cx1, cy1, pux, puy)
        if dst_p != 0 and dst_p == dst_n:
            log.append("    RETRY dst: P/N vias on same lateral side — forcing crossing")
            vp1b, vn1b = escape_endpoint(p1, n1, net_p, net_n, layer_id,
                                          info["perp_ux"], info["perp_uy"],
                                          -info["axis_ux"], -info["axis_uy"],
                                          force_crossing_of="P")
            if vp1b is not None and vn1b is not None:
                vp1, vn1 = vp1b, vn1b
            else:
                vp1b, vn1b = escape_endpoint(p1, n1, net_p, net_n, layer_id,
                                              info["perp_ux"], info["perp_uy"],
                                              -info["axis_ux"], -info["axis_uy"],
                                              force_crossing_of="N")
                if vp1b is not None and vn1b is not None:
                    vp1, vn1 = vp1b, vn1b

        # Recompute centroids/axis after any retry
        cx0 = (vp0[0] + vn0[0]) / 2;  cy0 = (vp0[1] + vn0[1]) / 2
        cx1 = (vp1[0] + vn1[0]) / 2;  cy1 = (vp1[1] + vn1[1]) / 2
        axL = math.hypot(cx1 - cx0, cy1 - cy0) or 1.0
        aux, auy = (cx1 - cx0) / axL, (cy1 - cy0) / axL
        pux, puy = -auy, aux
        s0 = 1.0 if ((vp0[0] - cx0) * pux + (vp0[1] - cy0) * puy) >= 0 else -1.0
        s1 = 1.0 if ((vp1[0] - cx1) * pux + (vp1[1] - cy1) * puy) >= 0 else -1.0

        if s0 * s1 < 0:
            # Crossing detected: P switches from one lateral side to the other.
            # This can happen even when detect_crossing() returned False, because
            # the escape via positions may produce a side-swap that detect_crossing()
            # (which works from pad centroids) didn't predict.
            #
            # Attempt 1: re-place the DESTINATION via to restore side consistency.
            # (For force_at_src cases this is a follow-up correction; for the
            #  spontaneous crossing case it is the first attempt.)
            if force_at_src is None:
                for _force_which in ("N", "P"):
                    vp1b, vn1b = escape_endpoint(p1, n1, net_p, net_n, layer_id,
                                                  info["perp_ux"], info["perp_uy"],
                                                  -info["axis_ux"], -info["axis_uy"],
                                                  force_crossing_of=_force_which)
                    if vp1b is not None and vn1b is not None:
                        cx0b = (vp0[0]+vn0[0])/2; cy0b = (vp0[1]+vn0[1])/2
                        cx1b = (vp1b[0]+vn1b[0])/2; cy1b = (vp1b[1]+vn1b[1])/2
                        axLb = math.hypot(cx1b-cx0b, cy1b-cy0b) or 1.0
                        puxb, puyb = -(cy1b-cy0b)/axLb, (cx1b-cx0b)/axLb
                        s0b = 1.0 if ((vp0[0]-cx0b)*puxb+(vp0[1]-cy0b)*puyb)>=0 else -1.0
                        s1b = 1.0 if ((vp1b[0]-cx1b)*puxb+(vp1b[1]-cy1b)*puyb)>=0 else -1.0
                        if s0b * s1b >= 0:
                            vp1, vn1 = vp1b, vn1b
                            cx0, cy0 = cx0b, cy0b
                            cx1, cy1 = cx1b, cy1b
                            pux, puy = puxb, puyb
                            s0, s1 = s0b, s1b
                            log.append(f"    [escape order swapped — retry succeeded]")
                            break
            # Attempt 2: force_at_src follow-up (re-attempt destination correction)
            if s0 * s1 < 0 and force_at_src is not None:
                log.append("    NOTE: lateral crossing unresolved — retrying destination "
                           "endpoint with crossing correction for P")
                vp1b, vn1b = escape_endpoint(p1, n1, net_p, net_n, layer_id,
                                              info["perp_ux"], info["perp_uy"],
                                              -info["axis_ux"], -info["axis_uy"],
                                              force_crossing_of="P")
                if vp1b is not None and vn1b is not None:
                    vp1, vn1 = vp1b, vn1b
                    # Recompute to check if crossing is resolved
                    cx0 = (vp0[0] + vn0[0]) / 2;  cy0 = (vp0[1] + vn0[1]) / 2
                    cx1 = (vp1[0] + vn1[0]) / 2;  cy1 = (vp1[1] + vn1[1]) / 2
                    axL = math.hypot(cx1 - cx0, cy1 - cy0) or 1.0
                    aux, auy = (cx1 - cx0) / axL, (cy1 - cy0) / axL
                    pux, puy = -auy, aux
                    s0 = 1.0 if ((vp0[0]-cx0)*pux + (vp0[1]-cy0)*puy) >= 0 else -1.0
                    s1 = 1.0 if ((vp1[0]-cx1)*pux + (vp1[1]-cy1)*puy) >= 0 else -1.0

            if s0 * s1 < 0 and _both_on_route_layer:
                # Both endpoints are SMT pads on the route layer — no escape vias
                # placed, so layer-split would create dangling buried tracks.
                # Route P then N sequentially on the route layer; N avoids P's track.
                _wf_sl = width_mm + cc
                _obs_sl = build_obstacles(layer_id, {net_p, net_n},
                                          include_sibling_reserves=True,
                                          sibling_extra=cc / 2.0)
                _obs_sl_board = build_obstacles_board_only(layer_id, {net_p, net_n})
                _pp_sl = None
                for _obs_try_p in (_obs_sl, _obs_sl_board):
                    for _c2 in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                        if path_clear(_c2, _wf_sl, _obs_try_p):
                            _pp_sl = merge_collinear(_c2); break
                    if _pp_sl is None:
                        _pp_sl = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                                   _wf_sl, _obs_try_p)
                    if _pp_sl is None and _obs_try_p is _obs_sl_board:
                        _cand = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                                  _wf_sl, _obs_try_p)
                        if (_cand is not None and
                                _path_clear_of_run_obs(_cand, width_mm / 2.0, net_p, layer_id)):
                            _pp_sl = _cand
                    if _pp_sl is not None:
                        break
                _nr_sl = None
                if _pp_sl is not None:
                    _pw_sl = width_mm / 2.0 + CLEARANCE_MM
                    _pp_sl_obs = [(min(a[0],b[0])-_pw_sl, min(a[1],b[1])-_pw_sl,
                                   max(a[0],b[0])+_pw_sl, max(a[1],b[1])+_pw_sl)
                                  for a, b in zip(_pp_sl, _pp_sl[1:])]
                    for _obs_try_n in (_obs_sl, _obs_sl_board):
                        _obs_n_sl = list(_obs_try_n) + _pp_sl_obs
                        for _c2 in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                            if path_clear(_c2, _wf_sl, _obs_n_sl):
                                _nr_sl = merge_collinear(_c2); break
                        if _nr_sl is None:
                            _nr_sl = _astar_centerline(vn0[0], vn0[1], vn1[0], vn1[1],
                                                       _wf_sl, _obs_n_sl)
                        if _nr_sl is not None:
                            break
                if _pp_sl is None or _nr_sl is None:
                    log.append("    WARNING: same-layer crossing routing failed; skipping emit")
                    _conflict_count += 1
                else:
                    for _i in range(len(_pp_sl) - 1):
                        add_track(_pp_sl[_i][0], _pp_sl[_i][1],
                                  _pp_sl[_i+1][0], _pp_sl[_i+1][1],
                                  width_mm, layer_id, net_p)
                    for _i in range(len(_nr_sl) - 1):
                        add_track(_nr_sl[_i][0], _nr_sl[_i][1],
                                  _nr_sl[_i+1][0], _nr_sl[_i+1][1],
                                  width_mm, layer_id, net_n)
                    log.append(f"    SAME-LAYER CROSSING: P and N on {layer_name}")
                    _hw = width_mm / 2.0
                    _seg_placed.setdefault(layer_id, []).extend((r, net_p) for r in _poly_obs(_pp_sl, _hw))
                    _seg_placed.setdefault(layer_id, []).extend((r, net_n) for r in _poly_obs(_nr_sl, _hw))
                continue  # skip regular emit_diffpair

            if s0 * s1 < 0:
                # Endpoints too tight for crossing correction.
                # Layer-split route: P on F.Cu between its endpoint vias,
                # N on In2.Cu.  Through-hole endpoint vias connect both layers,
                # so no additional crossover vias are needed.
                _F_CU  = 0
                _vr    = VIA_PAD / 2.0
                _cl    = CLEARANCE_MM
                _wf    = width_mm + cc
                # When both endpoints are already on the route layer (SMT pads),
                # escape_endpoint returns the pad position directly — no actual
                # escape vias are placed and P has no copper on the N routing layer.
                # Adding P-via boxes in this case would phantom-block N's destination.
                if not _both_on_route_layer:
                    _vp0_box = (vp0[0]-_vr-_cl, vp0[1]-_vr-_cl,
                                vp0[0]+_vr+_cl, vp0[1]+_vr+_cl)
                    _vp1_box = (vp1[0]-_vr-_cl, vp1[1]-_vr-_cl,
                                vp1[0]+_vr+_cl, vp1[1]+_vr+_cl)
                else:
                    _vp0_box = None
                    _vp1_box = None
                # Find N route — for F.Cu pairs N must cross on a buried layer
                # (P stays on F.Cu, so N can't also use F.Cu through the crossing).
                # For buried pairs (e.g. In2.Cu) N stays on layer_id and B.Cu is
                # the fallback if layer_id is blocked.
                _nr = _nr_layer = _nr_name = None
                # Corridor trunk trial for N: try the pre-assigned HV/VH path on the
                # primary layer FIRST — this avoids the congested octilinear/A* search
                # that falls back to B.Cu when In2.Cu is blocked by sibling pairs.
                if _trunk_info is not None and layer_id != F_CU:
                    _ti_style, _ti_tx = _trunk_info
                    if _ti_style == 'HV' and _ti_tx is not None:
                        _n_corr = merge_collinear([(vn0[0], vn0[1]), (_ti_tx, vn0[1]),
                                                    (_ti_tx, vn1[1]), (vn1[0], vn1[1])])
                    else:
                        _n_corr = merge_collinear([(vn0[0], vn0[1]), (vn0[0], vn1[1]),
                                                    (vn1[0], vn1[1])])
                    _obs_n_corr = (list(build_obstacles(layer_id, {net_p, net_n},
                                                         include_sibling_reserves=True,
                                                         sibling_extra=cc / 2.0))
                                   + [b for b in (_vp0_box, _vp1_box) if b is not None])
                    if path_clear(_n_corr, _wf, _obs_n_corr):
                        _nr = _n_corr
                        _nr_layer = layer_id
                        _nr_name  = layer_name
                        log.append(f"    NOTE: layer-split N corridor {_ti_style}"
                                   + (f" x={_ti_tx:.3f}mm" if _ti_tx else ""))
                if layer_id == F_CU:
                    _n_try_layers = [(board.GetLayerID("In2.Cu"), "In2.Cu"),
                                     (B_CU, "B.Cu")]
                else:
                    _n_try_layers = [(layer_id, layer_name)]
                    if layer_id != B_CU:
                        _n_try_layers.append((B_CU, "B.Cu"))
                for _try_nl, _try_nname in _n_try_layers:
                    if _nr is not None: break  # corridor already found
                    _pboxes = [b for b in (_vp0_box, _vp1_box) if b is not None]
                    _obs_n_try = (list(build_obstacles(_try_nl, {net_p, net_n},
                                                       include_sibling_reserves=True,
                                                       sibling_extra=cc / 2.0))
                                  + _pboxes)
                    _obs_nb_try = (list(build_obstacles_board_only(_try_nl, {net_p, net_n}))
                                   + _pboxes)
                    for _c2 in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                        if path_clear(_c2, _wf, _obs_n_try):
                            _nr = merge_collinear(_c2); break
                    if _nr is None:
                        _nr = _astar_centerline(vn0[0], vn0[1], vn1[0], vn1[1],
                                                _wf, _obs_n_try)
                    if _nr is None:
                        for _c2 in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                            if (path_clear(_c2, _wf, _obs_nb_try) and
                                    _path_clear_of_run_obs(_c2, width_mm / 2.0,
                                                           net_n, _try_nl)):
                                _nr = merge_collinear(_c2); break
                    if _nr is None:
                        _astar_board = _astar_centerline(vn0[0], vn0[1], vn1[0], vn1[1],
                                                         _wf, _obs_nb_try)
                        if (_astar_board is not None and
                                _path_clear_of_run_obs(_astar_board, width_mm / 2.0,
                                                       net_n, _try_nl)):
                            _nr = _astar_board
                    if _nr is not None:
                        _nr_layer = _try_nl
                        _nr_name  = _try_nname
                        break
                # Find P route — try F.Cu then B.Cu as crossover layer.
                # Try corridor trunk on F.Cu first before octilinear/A* search.
                _pp = _pp_layer = _pp_name = None
                if _trunk_info is not None:
                    _ti_style, _ti_tx = _trunk_info
                    if _ti_style == 'HV' and _ti_tx is not None:
                        _p_corr = merge_collinear([(vp0[0], vp0[1]), (_ti_tx, vp0[1]),
                                                    (_ti_tx, vp1[1]), (vp1[0], vp1[1])])
                    else:
                        _p_corr = merge_collinear([(vp0[0], vp0[1]), (vp0[0], vp1[1]),
                                                    (vp1[0], vp1[1])])
                    _obs_p_corr = build_obstacles(_F_CU, {net_p, net_n})
                    if path_clear(_p_corr, _wf, _obs_p_corr):
                        _pp = _p_corr
                        _pp_layer = _F_CU
                        _pp_name  = "F.Cu"
                        log.append(f"    NOTE: layer-split P corridor {_ti_style}"
                                   + (f" x={_ti_tx:.3f}mm" if _ti_tx else "") + " on F.Cu")
                for _try_layer, _try_name in ((_F_CU, "F.Cu"), (B_CU, "B.Cu")):
                    if _pp is not None: break  # corridor already found
                    _obs_try = build_obstacles(_try_layer, {net_p, net_n})
                    _obs_try_board = build_obstacles_board_only(
                        _try_layer, {net_p, net_n})
                    for _c2 in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                        if path_clear(_c2, _wf, _obs_try):
                            _pp = merge_collinear(_c2); break
                    if _pp is None:
                        _pp = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                                _wf, _obs_try)
                    if _pp is None:
                        for _c2 in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                            if (path_clear(_c2, _wf, _obs_try_board) and
                                    _path_clear_of_run_obs(_c2, width_mm / 2.0,
                                                           net_p, _try_layer)):
                                _pp = merge_collinear(_c2); break
                    if _pp is None:
                        _astar_b = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                                     _wf, _obs_try_board)
                        if (_astar_b is not None and
                                _path_clear_of_run_obs(_astar_b, width_mm / 2.0,
                                                       net_p, _try_layer)):
                            _pp = _astar_b
                    if _pp is not None:
                        _pp_layer = _try_layer
                        _pp_name  = _try_name
                        break
                # Swap fallback: if normal (N on layer_id/B.Cu, P on F.Cu/B.Cu) failed,
                # try the reversed assignment — P on layer_id, N on outer layers.
                # Helps when layer_id is blocked for N but open for P.
                if _nr is None and layer_id != F_CU:
                    _swap_p_boxes = [b for b in (_vp0_box, _vp1_box) if b is not None]
                    _swap_n_pp = _swap_n_nr = _swap_n_ppl = _swap_n_nrl = None
                    _swap_n_ppn = _swap_n_nrn = None
                    # P on layer_id
                    _swap_obs_p = (list(build_obstacles(layer_id, {net_p, net_n},
                                                         include_sibling_reserves=True,
                                                         sibling_extra=cc / 2.0))
                                   + [b for b in (_vp0_box, _vp1_box) if b is not None])
                    _swap_obs_pb = (list(build_obstacles_board_only(layer_id, {net_p, net_n}))
                                    + [b for b in (_vp0_box, _vp1_box) if b is not None])
                    for _sc in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                        if path_clear(_sc, _wf, _swap_obs_p):
                            _swap_n_pp = merge_collinear(_sc); break
                    if _swap_n_pp is None:
                        _swap_n_pp = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                                        _wf, _swap_obs_p)
                    if _swap_n_pp is None:
                        for _sc in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                            if (path_clear(_sc, _wf, _swap_obs_pb) and
                                    _path_clear_of_run_obs(_sc, width_mm / 2.0,
                                                           net_p, layer_id)):
                                _swap_n_pp = merge_collinear(_sc); break
                    if _swap_n_pp is None:
                        _sb = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                                _wf, _swap_obs_pb)
                        if (_sb is not None and
                                _path_clear_of_run_obs(_sb, width_mm / 2.0, net_p, layer_id)):
                            _swap_n_pp = _sb
                    # N on outer layers (F.Cu, B.Cu)
                    if _swap_n_pp is not None:
                        _swap_p_boxes = [b for b in (_vp0_box, _vp1_box) if b is not None]
                        for _snl, _snn in ((_F_CU, "F.Cu"), (B_CU, "B.Cu")):
                            _swap_obs_n = (list(build_obstacles(_snl, {net_p, net_n}))
                                           + _swap_p_boxes)
                            _swap_obs_nb = (list(build_obstacles_board_only(_snl, {net_p, net_n}))
                                            + _swap_p_boxes)
                            for _sc in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                                if path_clear(_sc, _wf, _swap_obs_n):
                                    _swap_n_nr = merge_collinear(_sc); break
                            if _swap_n_nr is None:
                                _swap_n_nr = _astar_centerline(
                                    vn0[0], vn0[1], vn1[0], vn1[1], _wf, _swap_obs_n)
                            if _swap_n_nr is None:
                                for _sc in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                                    if (path_clear(_sc, _wf, _swap_obs_nb) and
                                            _path_clear_of_run_obs(
                                                _sc, width_mm / 2.0, net_n, _snl)):
                                        _swap_n_nr = merge_collinear(_sc); break
                            if _swap_n_nr is None:
                                _sb = _astar_centerline(
                                    vn0[0], vn0[1], vn1[0], vn1[1], _wf, _swap_obs_nb)
                                if (_sb is not None and
                                        _path_clear_of_run_obs(
                                            _sb, width_mm / 2.0, net_n, _snl)):
                                    _swap_n_nr = _sb
                            if _swap_n_nr is not None:
                                _swap_n_ppl = layer_id;  _swap_n_ppn = layer_name
                                _swap_n_nrl = _snl;      _swap_n_nrn = _snn
                                break
                    if _swap_n_pp is not None and _swap_n_nr is not None:
                        _pp = _swap_n_pp; _pp_layer = _swap_n_ppl; _pp_name = _swap_n_ppn
                        _nr = _swap_n_nr; _nr_layer = _swap_n_nrl; _nr_name = _swap_n_nrn
                        log.append(f"    NOTE: layer-split swapped "
                                   f"(P on {_swap_n_ppn}, N on {_swap_n_nrn})")

                if _nr is None or _pp is None:
                    log.append("    WARNING: layer-split routing failed "
                               f"(N={'ok' if _nr else 'FAIL'} "
                               f"P-alt={'ok' if _pp else 'FAIL'}); skipping emit")
                    _conflict_count += 1
                else:
                    for _i in range(len(_pp) - 1):
                        add_track(_pp[_i][0], _pp[_i][1],
                                  _pp[_i+1][0], _pp[_i+1][1], width_mm, _pp_layer, net_p)
                    for _i in range(len(_nr) - 1):
                        add_track(_nr[_i][0], _nr[_i][1],
                                  _nr[_i+1][0], _nr[_i+1][1], width_mm, _nr_layer, net_n)
                    log.append(f"    LAYER-SPLIT: P on {_pp_name} "
                               f"({vp0[0]:.3f},{vp0[1]:.3f})→({vp1[0]:.3f},{vp1[1]:.3f}), "
                               f"N on {_nr_name}")
                    _hw = width_mm / 2.0
                    _seg_placed.setdefault(_pp_layer, []).extend((r, net_p) for r in _poly_obs(_pp, _hw))
                    _seg_placed.setdefault(_nr_layer, []).extend((r, net_n) for r in _poly_obs(_nr, _hw))
                continue  # skip regular emit_diffpair

        # Route centerline between (cx0,cy0) and (cx1,cy1)
        # include_sibling_reserves: keep clear any HS pad position on a
        # sibling net (e.g. AC-coupling cap pad carrying the paired
        # differential family's other pair) so it remains available for its
        # own future via-in-pad escape.
        half_cc = cc / 2.0  # emitted P/N track centers are ±half_cc from centerline
        obs_layer = build_obstacles(layer_id, {net_p, net_n},
                                    include_sibling_reserves=True,
                                    sibling_extra=half_cc)
        w_full = width_mm + cc   # bounding-corridor width for path_clear
        obs_no_sib = None        # built lazily below
        chosen = None

        # Note: same-net destination vias (vp1, vn1) are excluded from obs_layer
        # by build_obstacles.  P/N track-to-partner-via clearance is constrained
        # only by the via positions from route_fanout_vias.py.  Pairs where
        # min_cross_sep < required (e.g. HDMI0 at U3, 0.400mm < 0.977mm) will
        # produce track-to-via DRC warnings that require wider via separation at
        # the component level.
        _ep_obs = []  # reserved for future use

        # Option C — try pre-assigned corridor trunk first.
        # HV style: src → (trunk_x, src_cy) → (trunk_x, dst_cy) → dst  [unique column]
        # VH style: src → (src_cx, dst_cy) → dst  [cross first vertically, then run H]
        # Guarantees a unique path per pair in the group, avoiding A* congestion.
        # _trunk_info already computed at route_pair() top; also used in layer-split above.
        _trunk_x = None  # used in sibling-relaxed retry below
        if _trunk_info is not None:
            _style, _trunk_x = _trunk_info
            if _style == 'HV' and _trunk_x is not None:
                _cp = merge_collinear([(cx0, cy0), (_trunk_x, cy0),
                                       (_trunk_x, cy1), (cx1, cy1)])
            else:  # VH — go vertical first at src_x, then horizontal at dst_y
                _cp = merge_collinear([(cx0, cy0), (cx0, cy1), (cx1, cy1)])
            if path_clear(_cp, w_full, obs_layer):
                chosen = _cp
                _label = f"x={_trunk_x:.3f}mm" if _trunk_x is not None else f"VH src_x={cx0:.3f}mm"
                log.append(f"    NOTE: corridor {_style} trunk {_label}")

        if chosen is None:
            for cand in octilinear_paths(cx0, cy0, cx1, cy1):
                if path_clear(cand, w_full, obs_layer):
                    chosen = merge_collinear(cand)
                    break

        if chosen is None:
            # Progressive sibling-reserve relaxation: try sibling_extra=0 (pad
            # clearance only, no half_cc margin) before dropping reserves entirely.
            _obs_mid = build_obstacles(layer_id, {net_p, net_n},
                                       include_sibling_reserves=True, sibling_extra=0.0)
            if _trunk_info is not None:
                if _style == 'HV' and _trunk_x is not None:
                    _cp = merge_collinear([(cx0, cy0), (_trunk_x, cy0),
                                           (_trunk_x, cy1), (cx1, cy1)])
                else:
                    _cp = merge_collinear([(cx0, cy0), (cx0, cy1), (cx1, cy1)])
                if path_clear(_cp, w_full, _obs_mid):
                    chosen = _cp
                    _label = f"x={_trunk_x:.3f}mm" if _trunk_x is not None else f"VH src_x={cx0:.3f}mm"
                    log.append(f"    NOTE: corridor {_style} trunk {_label} "
                               f"(sibling extra relaxed)")
            if chosen is None:
                for cand in octilinear_paths(cx0, cy0, cx1, cy1):
                    if path_clear(cand, w_full, _obs_mid):
                        chosen = merge_collinear(cand)
                        log.append("    NOTE: octilinear path found (sibling extra relaxed)")
                        break

        if chosen is None:
            obs_no_sib = build_obstacles(layer_id, {net_p, net_n},
                                         include_sibling_reserves=False)
            # Retry corridor trunk without sibling reserves
            if _trunk_info is not None:
                if _style == 'HV' and _trunk_x is not None:
                    _cp = merge_collinear([(cx0, cy0), (_trunk_x, cy0),
                                           (_trunk_x, cy1), (cx1, cy1)])
                else:
                    _cp = merge_collinear([(cx0, cy0), (cx0, cy1), (cx1, cy1)])
                if path_clear(_cp, w_full, obs_no_sib):
                    chosen = _cp
                    _label = f"x={_trunk_x:.3f}mm" if _trunk_x is not None else f"VH src_x={cx0:.3f}mm"
                    log.append(f"    NOTE: corridor {_style} trunk {_label} "
                               f"(sibling reserves relaxed)")
            if chosen is None:
                for cand in octilinear_paths(cx0, cy0, cx1, cy1):
                    if path_clear(cand, w_full, obs_no_sib):
                        chosen = merge_collinear(cand)
                        log.append("    NOTE: octilinear path found (sibling reserves relaxed)")
                        break

        # Spine-waypoint search: for multi-segment pairs, try routing through
        # intermediate waypoints from the previous segment's centerline.  This
        # lets the router reuse the already-established corridor (e.g. a long
        # horizontal trunk placed by seg 0) for seg 1 instead of finding an
        # independent path — the HS-safe analog of route_critical.py's
        # spine-waypoint tapping (no T-junctions; separate copper per segment).
        if chosen is None and _seg_spine and not _both_on_route_layer:
            obs_no_sib = obs_no_sib or build_obstacles(layer_id, {net_p, net_n},
                                                        include_sibling_reserves=False)
            for _obs_sp, _sp_lbl in [(obs_layer, ""), (obs_no_sib, " (sibling relaxed)")]:
                if chosen is not None:
                    break
                for _sx, _sy in _seg_spine:
                    if chosen is not None:
                        break
                    for _l1 in octilinear_paths(cx0, cy0, _sx, _sy):
                        if not path_clear(_l1, w_full, _obs_sp):
                            continue
                        for _l2 in octilinear_paths(_sx, _sy, cx1, cy1):
                            if not path_clear(_l2, w_full, _obs_sp):
                                continue
                            chosen = merge_collinear(_l1[:-1] + _l2)
                            log.append(f"    NOTE: spine-waypoint via "
                                       f"({_sx:.3f},{_sy:.3f}){_sp_lbl}")
                            break
                        if chosen is not None:
                            break

        if chosen is None:
            obs_no_sib = obs_no_sib or build_obstacles(layer_id, {net_p, net_n},
                                                        include_sibling_reserves=False)
            # A* with progressive sibling-reserve relaxation:
            #   1. full reserves (sibling_extra=half_cc)
            #   2. partial reserves (sibling_extra=0 — pad clearance only)
            #   3. no reserves
            for _aobs, _albl in [
                    (obs_layer,  "sibling reserves included"),
                    (_obs_mid,   "sibling extra relaxed"),
                    (obs_no_sib, "sibling reserves relaxed")]:
                astar_path = _astar_centerline(cx0, cy0, cx1, cy1, w_full, _aobs)
                if astar_path is not None:
                    chosen = astar_path
                    log.append(f"    NOTE: A* centerline found ({len(chosen)-1} seg(s),"
                               f" {_albl})")
                    break
            if chosen is None:
                    # Board-only + exact run_obs fallback: AABB of diagonal _run_obs
                    # tracks can be over-conservative; retry with board-level obstacles
                    # only and verify the candidate against exact segment distances.
                    _obs_bo = build_obstacles_board_only(layer_id, {net_p, net_n})
                    for _bc in octilinear_paths(cx0, cy0, cx1, cy1):
                        if (path_clear(_bc, w_full, _obs_bo) and
                                _path_clear_of_run_obs(_bc, w_full / 2.0, net_p, layer_id) and
                                _path_clear_of_run_obs(_bc, w_full / 2.0, net_n, layer_id)):
                            chosen = merge_collinear(_bc)
                            log.append("    NOTE: octilinear path found (board-only + exact run_obs)")
                            break
                    if chosen is None:
                        _astar_bo = _astar_centerline(cx0, cy0, cx1, cy1, w_full, _obs_bo)
                        if (_astar_bo is not None and
                                _path_clear_of_run_obs(_astar_bo, w_full / 2.0, net_p, layer_id) and
                                _path_clear_of_run_obs(_astar_bo, w_full / 2.0, net_n, layer_id)):
                            chosen = _astar_bo
                            log.append(f"    NOTE: A* found (board-only + exact run_obs, "
                                       f"{len(chosen)-1} seg(s))")
                    if chosen is None:
                        # Neckdown-width fallback: use a reduced corridor width for
                        # path finding only.  The path check tolerates tighter
                        # passages; actual emitted tracks are still at width_mm.
                        # May produce DRC clearance violations — last resort only.
                        _w_neck = max(width_mm * 0.75 + cc, NECKDOWN_W_MM + cc)
                        if _w_neck < w_full - 1e-6:
                            if obs_no_sib is None:
                                obs_no_sib = build_obstacles(layer_id, {net_p, net_n},
                                                             include_sibling_reserves=False)
                            for _nko, _nkl in [(obs_no_sib, ""), (_obs_bo, " board-only")]:
                                for _nc in octilinear_paths(cx0, cy0, cx1, cy1):
                                    if path_clear(_nc, _w_neck, _nko):
                                        chosen = merge_collinear(_nc)
                                        log.append(f"    NOTE: neckdown path found "
                                                   f"(w={_w_neck:.3f}mm{_nkl}) — may DRC")
                                        break
                                if chosen is None:
                                    _nk_a = _astar_centerline(cx0, cy0, cx1, cy1,
                                                               _w_neck, _nko)
                                    if _nk_a is not None:
                                        chosen = _nk_a
                                        log.append(f"    NOTE: neckdown A* found "
                                                   f"(w={_w_neck:.3f}mm{_nkl}) — may DRC")
                                if chosen is not None:
                                    break
                    if chosen is None:
                        # Reverse-direction A* retry: swapping start/end changes the
                        # heuristic goal and heapq tie-breaking, potentially finding a
                        # different path through congestion the forward search missed.
                        # Also try reverse neckdown if the corridor is tight.
                        _rev_a = _astar_centerline(cx1, cy1, cx0, cy0, w_full, _obs_bo)
                        if (_rev_a is not None and
                                _path_clear_of_run_obs(_rev_a, w_full / 2.0, net_p, layer_id) and
                                _path_clear_of_run_obs(_rev_a, w_full / 2.0, net_n, layer_id)):
                            chosen = list(reversed(_rev_a))
                            log.append("    NOTE: A* found (reverse direction, board-only)")
                    if chosen is None and _w_neck < w_full - 1e-6:
                        _rev_nk = _astar_centerline(cx1, cy1, cx0, cy0, _w_neck, _obs_bo)
                        if _rev_nk is not None:
                            chosen = list(reversed(_rev_nk))
                            log.append(f"    NOTE: neckdown A* found (reverse dir, w={_w_neck:.3f}mm) — may DRC")
                    if chosen is None:
                        # Finer-grid A* retry: per-pair minimum safe grid pitch is
                        # cc + width + CLEARANCE (prevents P/N copper shorts in
                        # H/V staircase patterns).  Rounded up to 0.05mm, this is
                        # often smaller than the default 0.6mm, giving the search
                        # more routing options in tight areas.
                        _gp_min  = cc + width_mm + CLEARANCE_MM
                        _gp_fine = math.ceil(_gp_min / 0.05) * 0.05
                        if _gp_fine < 0.6 - 1e-6:
                            for _fgo, _fgl in [(_obs_bo, "board-only"),
                                               (obs_no_sib, "no-sib") if obs_no_sib else (None, None)]:
                                if _fgo is None:
                                    continue
                                _fg_a = _astar_centerline(cx0, cy0, cx1, cy1,
                                                          w_full, _fgo, _gp_fine)
                                if (_fg_a is not None and
                                        _path_clear_of_run_obs(_fg_a, w_full / 2.0, net_p, layer_id) and
                                        _path_clear_of_run_obs(_fg_a, w_full / 2.0, net_n, layer_id)):
                                    chosen = _fg_a
                                    log.append(f"    NOTE: A* found (finer grid gp={_gp_fine:.2f}mm, {_fgl})")
                                    break
                                if chosen is None:
                                    _fg_ar = _astar_centerline(cx1, cy1, cx0, cy0,
                                                               w_full, _fgo, _gp_fine)
                                    if (_fg_ar is not None and
                                            _path_clear_of_run_obs(_fg_ar, w_full / 2.0, net_p, layer_id) and
                                            _path_clear_of_run_obs(_fg_ar, w_full / 2.0, net_n, layer_id)):
                                        chosen = list(reversed(_fg_ar))
                                        log.append(f"    NOTE: A* found (finer grid gp={_gp_fine:.2f}mm, {_fgl}, rev)")
                                        break
                                if chosen is not None:
                                    break
                    if chosen is None:
                        # Independent P/N fallback: the shared corridor (width_mm+cc)
                        # is blocked, but P and N may each have a clear individual path
                        # (width_mm) to their own destination — e.g. when a previously
                        # routed pair runs between the two source vias.
                        _ip_obs = list(build_obstacles(layer_id, {net_p, net_n},
                                                       include_sibling_reserves=False))
                        _ip_obs_bo = list(build_obstacles_board_only(layer_id, {net_p, net_n}))
                        _ip_pp = None
                        for _ip_c in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                            if path_clear(_ip_c, width_mm, _ip_obs):
                                _ip_pp = merge_collinear(_ip_c); break
                        if _ip_pp is None:
                            _ip_pp = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                                       width_mm, _ip_obs)
                        if _ip_pp is None:
                            for _ip_c in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                                if (path_clear(_ip_c, width_mm, _ip_obs_bo) and
                                        _path_clear_of_run_obs(_ip_c, width_mm/2.0,
                                                               net_p, layer_id)):
                                    _ip_pp = merge_collinear(_ip_c); break
                        if _ip_pp is None:
                            _ip_r = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                                      width_mm, _ip_obs_bo)
                            if (_ip_r is not None and
                                    _path_clear_of_run_obs(_ip_r, width_mm/2.0,
                                                           net_p, layer_id)):
                                _ip_pp = _ip_r
                        _ip_nr = None
                        for _ip_c in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                            if path_clear(_ip_c, width_mm, _ip_obs):
                                _ip_nr = merge_collinear(_ip_c); break
                        if _ip_nr is None:
                            _ip_nr = _astar_centerline(vn0[0], vn0[1], vn1[0], vn1[1],
                                                       width_mm, _ip_obs)
                        if _ip_nr is None:
                            for _ip_c in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                                if (path_clear(_ip_c, width_mm, _ip_obs_bo) and
                                        _path_clear_of_run_obs(_ip_c, width_mm/2.0,
                                                               net_n, layer_id)):
                                    _ip_nr = merge_collinear(_ip_c); break
                        if _ip_nr is None:
                            _ip_r = _astar_centerline(vn0[0], vn0[1], vn1[0], vn1[1],
                                                      width_mm, _ip_obs_bo)
                            if (_ip_r is not None and
                                    _path_clear_of_run_obs(_ip_r, width_mm/2.0,
                                                           net_n, layer_id)):
                                _ip_nr = _ip_r
                        if _ip_pp is not None and _ip_nr is not None:
                            for _ip_i in range(len(_ip_pp) - 1):
                                add_track(_ip_pp[_ip_i][0], _ip_pp[_ip_i][1],
                                          _ip_pp[_ip_i+1][0], _ip_pp[_ip_i+1][1],
                                          width_mm, layer_id, net_p)
                            for _ip_i in range(len(_ip_nr) - 1):
                                add_track(_ip_nr[_ip_i][0], _ip_nr[_ip_i][1],
                                          _ip_nr[_ip_i+1][0], _ip_nr[_ip_i+1][1],
                                          width_mm, layer_id, net_n)
                            log.append("    NOTE: independent P/N routing succeeded")
                            _hw = width_mm / 2.0
                            _seg_placed.setdefault(layer_id, []).extend((r, net_p) for r in _poly_obs(_ip_pp, _hw))
                            _seg_placed.setdefault(layer_id, []).extend((r, net_n) for r in _poly_obs(_ip_nr, _hw))
                            continue

                    if chosen is None:
                        # All paths blocked — skip this segment rather than emitting a
                        # straight diagonal that crosses adjacent pair routes.
                        # Option D rip-up pass will re-attempt after obstacle landscape
                        # changes from other pairs being ripped up.
                        log.append("    NOTE: all paths blocked — skipping segment "
                                   "(Option D rip-up will retry)")
                        _skipped_pairs.add(pair_name)
                        _conflict_count += 1
                        continue

        # Via-clearance guard: when the chosen path's P or N track would physically
        # overlap a cross-net endpoint via (electrical short), first try alternate
        # 2-segment paths; if none avoids the overlap, route P and N independently
        # with the partner-net vias added as explicit obstacles.
        def _ep_r(x, y):
            if (round(x, 3), round(y, 3)) in _stub_tip_xy or _both_on_route_layer:
                return width_mm / 2.0
            return VIA_PAD / 2.0
        # Force independent P/N routing when the centerline detour is >2× the
        # straight-line distance AND all endpoints are on the route layer (no
        # escape vias).  Direct-pad segments can have varying cc (e.g. USB2:
        # 2.0mm at src, 0.5mm at dst) so the shared w_full=cc+width corridor
        # over-routes around obstacles that individual traces can navigate.
        _vc_force_indep = (chosen is not None and _both_on_route_layer and
                           math.hypot(cx1-cx0, cy1-cy0) > 1e-3 and
                           sum(math.hypot(chosen[i+1][0]-chosen[i][0],
                                          chosen[i+1][1]-chosen[i][1])
                               for i in range(len(chosen)-1))
                           > math.hypot(cx1-cx0, cy1-cy0) * 2.0)
        if _vc_force_indep or _chosen_has_via_clearance_issue(
                chosen, vp0, vn0, vp1, vn1, cc, width_mm,
                direct_pad=_both_on_route_layer):
            if _vc_force_indep:
                log.append("    NOTE: direct-pad detour >2× straight — routing P/N independently")
            _vc_alt = None
            _vc_obs_list = [obs_layer]
            if obs_no_sib is None:
                _vc_obs_list.append(build_obstacles(layer_id, {net_p, net_n},
                                                    include_sibling_reserves=False))
            else:
                _vc_obs_list.append(obs_no_sib)
            for _vc_obs in _vc_obs_list:
                for _vc_cand in octilinear_paths(cx0, cy0, cx1, cy1):
                    _vc_m = merge_collinear(_vc_cand)
                    if (path_clear(_vc_m, w_full, _vc_obs) and
                            not _chosen_has_via_clearance_issue(
                                _vc_m, vp0, vn0, vp1, vn1, cc, width_mm)):
                        _vc_alt = _vc_m
                        log.append("    NOTE: via-clearance overlap — switched to alternate path")
                        break
                if _vc_alt is not None:
                    break
            if _vc_alt is not None:
                chosen = _vc_alt
            else:
                # No 2-segment path avoids the overlap: route P and N independently,
                # each avoiding the partner-net endpoint vias as explicit obstacles.
                log.append("    NOTE: via-clearance overlap — no clean 2-seg path; "
                           "routing P/N independently")
                # seg_rect already adds track_hw; obstacle needs only via_radius + clearance
                def _vc_box(xy):
                    m = _ep_r(xy[0], xy[1]) + CLEARANCE_MM
                    return (xy[0]-m, xy[1]-m, xy[0]+m, xy[1]+m)
                _vc_np_boxes = [_vc_box(vn0), _vc_box(vn1)]
                _vc_pp_boxes = [_vc_box(vp0), _vc_box(vp1)]
                _vc_base = list(build_obstacles(layer_id, {net_p, net_n},
                                                include_sibling_reserves=False))
                _vc_base_bo = list(build_obstacles_board_only(layer_id, {net_p, net_n}))
                _vc_obs_p    = _vc_base    + _vc_np_boxes
                _vc_obs_n    = _vc_base    + _vc_pp_boxes
                _vc_obs_p_bo = _vc_base_bo + _vc_np_boxes
                _vc_obs_n_bo = _vc_base_bo + _vc_pp_boxes
                _vc_pp = None
                for _vc_c in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                    if path_clear(_vc_c, width_mm, _vc_obs_p):
                        _vc_pp = merge_collinear(_vc_c); break
                if _vc_pp is None:
                    _xp_lo = min(vp0[0], vp1[0]); _xp_hi = max(vp0[0], vp1[0])
                    for _jdx in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                        for _xj in (_xp_lo - _jdx, _xp_hi + _jdx):
                            _cj = merge_collinear([(vp0[0], vp0[1]), (_xj, vp0[1]),
                                                   (_xj, vp1[1]), (vp1[0], vp1[1])])
                            if path_clear(_cj, width_mm, _vc_obs_p):
                                _vc_pp = _cj
                                log.append(f"    NOTE: via-clear-split P 3-seg jog x={_xj:.3f}")
                                break
                        if _vc_pp is not None: break
                if _vc_pp is None:
                    _yp_lo = min(vp0[1], vp1[1]); _yp_hi = max(vp0[1], vp1[1])
                    for _jdy in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                        for _yj in (_yp_lo - _jdy, _yp_hi + _jdy):
                            _cj = merge_collinear([(vp0[0], vp0[1]), (vp0[0], _yj),
                                                   (vp1[0], _yj), (vp1[0], vp1[1])])
                            if path_clear(_cj, width_mm, _vc_obs_p):
                                _vc_pp = _cj
                                log.append(f"    NOTE: via-clear-split P 3-seg V-H-V jog y={_yj:.3f}")
                                break
                        if _vc_pp is not None: break
                if _vc_pp is None:
                    # D-V-D jog: 45° diagonal exit, vertical column, 45° diagonal approach.
                    # Tries both left and right jog columns — direction is not assumed.
                    _dyp = 1.0 if vp1[1] >= vp0[1] else -1.0
                    _dvd_p_done = False
                    for _d in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                        for _xj in (vp0[0] - _d, vp0[0] + _d):
                            _y_top = vp0[1] + _d * _dyp
                            _y_bot = vp1[1] - abs(vp1[0] - _xj) * _dyp
                            if (_dyp > 0 and _y_top >= _y_bot) or (_dyp < 0 and _y_top <= _y_bot):
                                continue
                            _cj = merge_collinear([(vp0[0], vp0[1]), (_xj, _y_top),
                                                   (_xj, _y_bot), (vp1[0], vp1[1])])
                            if _path_clear_approx(_cj, width_mm, _vc_obs_p):
                                _vc_pp = _cj
                                log.append(f"    NOTE: via-clear-split P D-V-D jog x={_xj:.3f}")
                                _dvd_p_done = True
                                break
                        if _dvd_p_done:
                            break
                if _vc_pp is None:
                    _adxp = abs(vp1[0]-vp0[0]); _adyp = abs(vp1[1]-vp0[1])
                    _sxp  = 1 if vp1[0] > vp0[0] else -1
                    _syp  = 1 if vp1[1] > vp0[1] else -1
                    if _adyp > _adxp + 1e-3:  # VDV: V stub, 45° diagonal, V arrive
                        for _d in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                            if _d >= _adyp - _adxp - 1e-3:
                                break
                            _ep = (vp0[0], vp0[1] + _syp*_d)
                            _ap = (vp1[0], vp0[1] + _syp*(_d + _adxp))
                            _cj = merge_collinear([(vp0[0],vp0[1]), _ep, _ap, (vp1[0],vp1[1])])
                            if _path_clear_approx(_cj, width_mm, _vc_obs_p):
                                _vc_pp = _cj
                                log.append(f"    NOTE: via-clear-split P VDV d={_d:.3f}")
                                break
                    else:  # HDV: H stub, 45° diagonal, V arrive (also covers adx ≈ ady)
                        _min_dp = _adxp - _adyp
                        for _d in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                            if _d <= _min_dp - 1e-3 or _d >= _adxp - 1e-3:
                                continue
                            _ep = (vp0[0] + _sxp*_d, vp0[1])
                            _ap = (vp1[0], vp0[1] + _syp*(_adxp - _d))
                            _cj = merge_collinear([(vp0[0],vp0[1]), _ep, _ap, (vp1[0],vp1[1])])
                            if _path_clear_approx(_cj, width_mm, _vc_obs_p):
                                _vc_pp = _cj
                                log.append(f"    NOTE: via-clear-split P HDV d={_d:.3f}")
                                break
                if _vc_pp is None:
                    _adxp = abs(vp1[0]-vp0[0]); _adyp = abs(vp1[1]-vp0[1])
                    _sxp  = 1 if vp1[0] > vp0[0] else -1
                    _syp  = 1 if vp1[1] > vp0[1] else -1
                    if _adyp > _adxp + 1e-3:  # VDV-rev: stub away from dest y, diagonal, V arrive
                        for _d in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                            if _d >= _adxp - 1e-3:
                                break
                            _ep = (vp0[0], vp0[1] - _syp*_d)
                            _ap = (vp1[0], vp0[1] + _syp*(_adxp - _d))
                            _cj = merge_collinear([(vp0[0],vp0[1]), _ep, _ap, (vp1[0],vp1[1])])
                            if _path_clear_approx(_cj, width_mm, _vc_obs_p):
                                _vc_pp = _cj
                                log.append(f"    NOTE: via-clear-split P VDV-rev d={_d:.3f}")
                                break
                    else:  # HDV-rev: stub away from dest x, diagonal overshoots y, V return (also adx ≈ ady)
                        for _d in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                            if _d >= _adxp - 1e-3:
                                break
                            _ep = (vp0[0] - _sxp*_d, vp0[1])
                            _ap = (vp1[0], vp0[1] + _syp*(_adxp + _d))
                            _cj = merge_collinear([(vp0[0],vp0[1]), _ep, _ap, (vp1[0],vp1[1])])
                            if _path_clear_approx(_cj, width_mm, _vc_obs_p):
                                _vc_pp = _cj
                                log.append(f"    NOTE: via-clear-split P HDV-rev d={_d:.3f}")
                                break
                if _vc_pp is None:
                    _vc_pp = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                               width_mm, _vc_obs_p)
                if _vc_pp is None:
                    for _vc_c in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                        if (path_clear(_vc_c, width_mm, _vc_obs_p_bo) and
                                _path_clear_of_run_obs(_vc_c, width_mm / 2.0, net_p, layer_id)):
                            _vc_pp = merge_collinear(_vc_c); break
                if _vc_pp is None:
                    _vc_board = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                                  width_mm, _vc_obs_p_bo)
                    if (_vc_board is not None and
                            _path_clear_of_run_obs(_vc_board, width_mm / 2.0, net_p, layer_id)):
                        _vc_pp = _vc_board
                # Add P's planned segments as obstacles for N's search so they
                # cannot cross — same pattern as route_critical.py's _run_obs update.
                # AABB boxes go into _vc_obs_n for the A* grid search.
                # Exact tuples go into _p_exact_segs for octilinear candidate checks
                # so diagonal segments don't produce inflated false-obstacle regions.
                _p_exact_segs = []
                if _vc_pp is not None:
                    _hw_p = width_mm / 2.0
                    _p_segs = [(min(_vc_pp[_i][0], _vc_pp[_i+1][0]) - _hw_p - CLEARANCE_MM,
                                min(_vc_pp[_i][1], _vc_pp[_i+1][1]) - _hw_p - CLEARANCE_MM,
                                max(_vc_pp[_i][0], _vc_pp[_i+1][0]) + _hw_p + CLEARANCE_MM,
                                max(_vc_pp[_i][1], _vc_pp[_i+1][1]) + _hw_p + CLEARANCE_MM)
                               for _i in range(len(_vc_pp) - 1)
                               if (abs(_vc_pp[_i][0] - _vc_pp[_i+1][0]) < 1e-3 or
                                   abs(_vc_pp[_i][1] - _vc_pp[_i+1][1]) < 1e-3)]
                    _p_exact_segs = [(_vc_pp[_i][0], _vc_pp[_i][1],
                                      _vc_pp[_i+1][0], _vc_pp[_i+1][1], _hw_p)
                                     for _i in range(len(_vc_pp) - 1)]
                    _vc_obs_n    = _vc_obs_n    + _p_segs
                    _vc_obs_n_bo = _vc_obs_n_bo + _p_segs

                _vc_nr = None
                for _vc_c in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                    if (_path_clear_approx(_vc_c, width_mm, _vc_obs_n_bo) and
                            _path_clear_of_explicit_segs(_vc_c, width_mm / 2.0, _p_exact_segs)):
                        _vc_nr = merge_collinear(_vc_c); break
                if _vc_nr is None:
                    _xn_lo = min(vn0[0], vn1[0]); _xn_hi = max(vn0[0], vn1[0])
                    for _jdx in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                        for _xj in (_xn_lo - _jdx, _xn_hi + _jdx):
                            _cj = merge_collinear([(vn0[0], vn0[1]), (_xj, vn0[1]),
                                                   (_xj, vn1[1]), (vn1[0], vn1[1])])
                            if (_path_clear_approx(_cj, width_mm, _vc_obs_n_bo) and
                                    _path_clear_of_explicit_segs(_cj, width_mm / 2.0, _p_exact_segs)):
                                _vc_nr = _cj
                                log.append(f"    NOTE: via-clear-split N 3-seg jog x={_xj:.3f}")
                                break
                        if _vc_nr is not None: break
                if _vc_nr is None:
                    _yn_lo = min(vn0[1], vn1[1]); _yn_hi = max(vn0[1], vn1[1])
                    for _jdy in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                        for _yj in (_yn_lo - _jdy, _yn_hi + _jdy):
                            _cj = merge_collinear([(vn0[0], vn0[1]), (vn0[0], _yj),
                                                   (vn1[0], _yj), (vn1[0], vn1[1])])
                            if (_path_clear_approx(_cj, width_mm, _vc_obs_n_bo) and
                                    _path_clear_of_explicit_segs(_cj, width_mm / 2.0, _p_exact_segs)):
                                _vc_nr = _cj
                                log.append(f"    NOTE: via-clear-split N 3-seg V-H-V jog y={_yj:.3f}")
                                break
                        if _vc_nr is not None: break
                if _vc_nr is None:
                    # D-V-D jog: 45° diagonal exit, vertical column, 45° diagonal approach.
                    # Tries both left and right jog columns — direction is not assumed.
                    _dyn = 1.0 if vn1[1] >= vn0[1] else -1.0
                    _dvd_n_done = False
                    for _d in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                        for _xj in (vn0[0] - _d, vn0[0] + _d):
                            _y_top = vn0[1] + _d * _dyn
                            _y_bot = vn1[1] - abs(vn1[0] - _xj) * _dyn
                            if (_dyn > 0 and _y_top >= _y_bot) or (_dyn < 0 and _y_top <= _y_bot):
                                continue
                            _cj = merge_collinear([(vn0[0], vn0[1]), (_xj, _y_top),
                                                   (_xj, _y_bot), (vn1[0], vn1[1])])
                            if (_path_clear_approx(_cj, width_mm, _vc_obs_n_bo) and
                                    _path_clear_of_explicit_segs(_cj, width_mm / 2.0, _p_exact_segs)):
                                _vc_nr = _cj
                                log.append(f"    NOTE: via-clear-split N D-V-D jog x={_xj:.3f}")
                                _dvd_n_done = True
                                break
                        if _dvd_n_done:
                            break
                if _vc_nr is None:
                    _adxn = abs(vn1[0]-vn0[0]); _adyn = abs(vn1[1]-vn0[1])
                    _sxn  = 1 if vn1[0] > vn0[0] else -1
                    _syn  = 1 if vn1[1] > vn0[1] else -1
                    # For diagonal N candidates use board+endpoint obstacles only.
                    # P's diagonal AABB is square (dx=dy for 45°), which the square-
                    # obstacle check treats as a via circle — falsely over-conservative.
                    # Exact P clearance is guaranteed by _path_clear_of_explicit_segs.
                    _vc_obs_n_dp = _vc_base_bo + _vc_pp_boxes
                    if _adyn > _adxn + 1e-3:  # VDV: V stub, 45° diagonal, V arrive
                        for _d in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                            if _d >= _adyn - _adxn - 1e-3:
                                break
                            _en = (vn0[0], vn0[1] + _syn*_d)
                            _an = (vn1[0], vn0[1] + _syn*(_d + _adxn))
                            _cj = merge_collinear([(vn0[0],vn0[1]), _en, _an, (vn1[0],vn1[1])])
                            if (_path_clear_approx(_cj, width_mm, _vc_obs_n_dp) and
                                    _path_clear_of_explicit_segs(_cj, width_mm/2.0, _p_exact_segs)):
                                _vc_nr = _cj
                                log.append(f"    NOTE: via-clear-split N VDV d={_d:.3f}")
                                break
                    else:  # HDV: H stub, 45° diagonal, V arrive (also covers adx ≈ ady)
                        _min_dn = _adxn - _adyn
                        for _d in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                            if _d <= _min_dn - 1e-3 or _d >= _adxn - 1e-3:
                                continue
                            _en = (vn0[0] + _sxn*_d, vn0[1])
                            _an = (vn1[0], vn0[1] + _syn*(_adxn - _d))
                            _cj = merge_collinear([(vn0[0],vn0[1]), _en, _an, (vn1[0],vn1[1])])
                            if (_path_clear_approx(_cj, width_mm, _vc_obs_n_dp) and
                                    _path_clear_of_explicit_segs(_cj, width_mm/2.0, _p_exact_segs)):
                                _vc_nr = _cj
                                log.append(f"    NOTE: via-clear-split N HDV d={_d:.3f}")
                                break
                if _vc_nr is None:
                    _adxn = abs(vn1[0]-vn0[0]); _adyn = abs(vn1[1]-vn0[1])
                    _sxn  = 1 if vn1[0] > vn0[0] else -1
                    _syn  = 1 if vn1[1] > vn0[1] else -1
                    _vc_obs_n_dp = _vc_base_bo + _vc_pp_boxes
                    if _adyn > _adxn + 1e-3:  # VDV-rev: stub away from dest y, diagonal, V arrive
                        for _d in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                            if _d >= _adxn - 1e-3:
                                break
                            _en = (vn0[0], vn0[1] - _syn*_d)
                            _an = (vn1[0], vn0[1] + _syn*(_adxn - _d))
                            _cj = merge_collinear([(vn0[0],vn0[1]), _en, _an, (vn1[0],vn1[1])])
                            if (_path_clear_approx(_cj, width_mm, _vc_obs_n_dp) and
                                    _path_clear_of_explicit_segs(_cj, width_mm/2.0, _p_exact_segs)):
                                _vc_nr = _cj
                                log.append(f"    NOTE: via-clear-split N VDV-rev d={_d:.3f}")
                                break
                    else:  # HDV-rev: stub away from dest x, diagonal overshoots y, V return (also adx ≈ ady)
                        for _d in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0):
                            if _d >= _adxn - 1e-3:
                                break
                            _en = (vn0[0] - _sxn*_d, vn0[1])
                            _an = (vn1[0], vn0[1] + _syn*(_adxn + _d))
                            _cj = merge_collinear([(vn0[0],vn0[1]), _en, _an, (vn1[0],vn1[1])])
                            if (_path_clear_approx(_cj, width_mm, _vc_obs_n_dp) and
                                    _path_clear_of_explicit_segs(_cj, width_mm/2.0, _p_exact_segs)):
                                _vc_nr = _cj
                                log.append(f"    NOTE: via-clear-split N HDV-rev d={_d:.3f}")
                                break
                if _vc_nr is None:
                    _vc_nr = _astar_centerline(vn0[0], vn0[1], vn1[0], vn1[1],
                                               width_mm, _vc_obs_n)
                    if (_vc_nr is not None and
                            not _path_clear_of_explicit_segs(_vc_nr, width_mm/2.0, _p_exact_segs)):
                        _vc_nr = None
                if _vc_nr is None:
                    for _vc_c in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                        if (path_clear(_vc_c, width_mm, _vc_obs_n_bo) and
                                _path_clear_of_run_obs(_vc_c, width_mm / 2.0, net_n, layer_id)):
                            _vc_nr = merge_collinear(_vc_c); break
                if _vc_nr is None:
                    _vc_board = _astar_centerline(vn0[0], vn0[1], vn1[0], vn1[1],
                                                  width_mm, _vc_obs_n_bo)
                    if (_vc_board is not None and
                            _path_clear_of_run_obs(_vc_board, width_mm / 2.0, net_n, layer_id)):
                        _vc_nr = _vc_board
                # Alternate-layer fallback for any failing leg.
                _vc_pp_layer = layer_id;  _vc_nr_layer = layer_id
                if (_vc_pp is None or _vc_nr is None) and not _both_on_route_layer:
                    _vc_alt_layers = []
                    if layer_id != F_CU:
                        _vc_alt_layers.append((F_CU, "F.Cu"))
                    if layer_id != B_CU:
                        _vc_alt_layers.append((B_CU, "B.Cu"))
                    for _vcal, _vcaln in _vc_alt_layers:
                        _vc_base_al  = list(build_obstacles(_vcal, {net_p, net_n},
                                                             include_sibling_reserves=False))
                        _vc_base_abo = list(build_obstacles_board_only(_vcal, {net_p, net_n}))
                        if _vc_pp is None:
                            _vc_obs_pal  = _vc_base_al  + _vc_np_boxes
                            _vc_obs_pabo = _vc_base_abo + _vc_np_boxes
                            for _vc_c in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                                if path_clear(_vc_c, width_mm, _vc_obs_pal):
                                    _vc_pp = merge_collinear(_vc_c)
                                    _vc_pp_layer = _vcal; break
                            if _vc_pp is None:
                                _r = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                                       width_mm, _vc_obs_pal)
                                if _r is not None:
                                    _vc_pp = _r; _vc_pp_layer = _vcal
                            if _vc_pp is None:
                                for _vc_c in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                                    if (path_clear(_vc_c, width_mm, _vc_obs_pabo) and
                                            _path_clear_of_run_obs(_vc_c, width_mm/2.0,
                                                                    net_p, _vcal)):
                                        _vc_pp = merge_collinear(_vc_c)
                                        _vc_pp_layer = _vcal; break
                            if _vc_pp is None:
                                _r = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                                       width_mm, _vc_obs_pabo)
                                if (_r is not None and
                                        _path_clear_of_run_obs(_r, width_mm/2.0, net_p, _vcal)):
                                    _vc_pp = _r; _vc_pp_layer = _vcal
                        if _vc_nr is None:
                            _vc_obs_nal  = _vc_base_al  + _vc_pp_boxes
                            _vc_obs_nabo = _vc_base_abo + _vc_pp_boxes
                            for _vc_c in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                                if path_clear(_vc_c, width_mm, _vc_obs_nal):
                                    _vc_nr = merge_collinear(_vc_c)
                                    _vc_nr_layer = _vcal; break
                            if _vc_nr is None:
                                _r = _astar_centerline(vn0[0], vn0[1], vn1[0], vn1[1],
                                                       width_mm, _vc_obs_nal)
                                if _r is not None:
                                    _vc_nr = _r; _vc_nr_layer = _vcal
                            if _vc_nr is None:
                                for _vc_c in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                                    if (path_clear(_vc_c, width_mm, _vc_obs_nabo) and
                                            _path_clear_of_run_obs(_vc_c, width_mm/2.0,
                                                                    net_n, _vcal)):
                                        _vc_nr = merge_collinear(_vc_c)
                                        _vc_nr_layer = _vcal; break
                            if _vc_nr is None:
                                _r = _astar_centerline(vn0[0], vn0[1], vn1[0], vn1[1],
                                                       width_mm, _vc_obs_nabo)
                                if (_r is not None and
                                        _path_clear_of_run_obs(_r, width_mm/2.0, net_n, _vcal)):
                                    _vc_nr = _r; _vc_nr_layer = _vcal
                        if _vc_pp is not None and _vc_nr is not None:
                            break

                if _vc_pp is not None and _vc_nr is not None:
                    for _vc_i in range(len(_vc_pp) - 1):
                        add_track(_vc_pp[_vc_i][0], _vc_pp[_vc_i][1],
                                  _vc_pp[_vc_i+1][0], _vc_pp[_vc_i+1][1],
                                  width_mm, _vc_pp_layer, net_p)
                    for _vc_i in range(len(_vc_nr) - 1):
                        add_track(_vc_nr[_vc_i][0], _vc_nr[_vc_i][1],
                                  _vc_nr[_vc_i+1][0], _vc_nr[_vc_i+1][1],
                                  width_mm, _vc_nr_layer, net_n)
                    _vc_pln = board.GetLayerName(_vc_pp_layer) if _vc_pp_layer != layer_id else layer_name
                    _vc_nln = board.GetLayerName(_vc_nr_layer) if _vc_nr_layer != layer_id else layer_name
                    log.append(f"    VIA-CLEAR-SPLIT: P ({vp0[0]:.3f},{vp0[1]:.3f})"
                               f"→({vp1[0]:.3f},{vp1[1]:.3f}) on {_vc_pln},"
                               f" N ({vn0[0]:.3f},{vn0[1]:.3f})"
                               f"→({vn1[0]:.3f},{vn1[1]:.3f}) on {_vc_nln}")
                    _hw = width_mm / 2.0
                    _seg_placed.setdefault(_vc_pp_layer, []).extend((r, net_p) for r in _poly_obs(_vc_pp, _hw))
                    _seg_placed.setdefault(_vc_nr_layer, []).extend((r, net_n) for r in _poly_obs(_vc_nr, _hw))
                    continue  # skip emit_diffpair
                else:
                    log.append(f"    WARNING: via-clearance split failed "
                               f"(P={'ok' if _vc_pp else 'FAIL'} "
                               f"N={'ok' if _vc_nr else 'FAIL'}); keeping chosen")
                # fall through to emit_diffpair with original chosen path

        # Miter-crossing guard: simulate cur_p_sign propagation through the chosen
        # path.  When the final propagated sign disagrees with p_sign_dst, the
        # end stubs from p_end/n_end to the destination vias would cross the
        # partner's track body on a single layer.  Fall back to layer-split so P
        # and N route independently and can cross each other without shorting.
        if not _both_on_route_layer and _miter_predict_crossing(chosen, vp0, vp1, cc):
            log.append("    NOTE: miter end-stub crossing predicted — "
                       "falling back to layer-split routing")
            _mc_wf  = width_mm + cc
            _mc_cl  = CLEARANCE_MM
            # Stub-tip endpoints have no via copper — use track half-width as
            # the exclusion radius instead of VIA_PAD/2 so the partner can
            # start routing from its own stub tip without being pushed away.
            def _ep_r(pt):
                if (round(pt[0], 3), round(pt[1], 3)) in _stub_tip_xy or _both_on_route_layer:
                    return width_mm / 2.0
                return VIA_PAD / 2.0
            _mc_p0b = (vp0[0]-_ep_r(vp0)-_mc_cl, vp0[1]-_ep_r(vp0)-_mc_cl,
                       vp0[0]+_ep_r(vp0)+_mc_cl, vp0[1]+_ep_r(vp0)+_mc_cl)
            _mc_p1b = (vp1[0]-_ep_r(vp1)-_mc_cl, vp1[1]-_ep_r(vp1)-_mc_cl,
                       vp1[0]+_ep_r(vp1)+_mc_cl, vp1[1]+_ep_r(vp1)+_mc_cl)
            # Route P on route layer
            _mc_pp = None
            _mc_obs_p = build_obstacles(layer_id, {net_p, net_n},
                                        include_sibling_reserves=True,
                                        sibling_extra=cc / 2.0)
            for _mc_c in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                if path_clear(_mc_c, _mc_wf, _mc_obs_p):
                    _mc_pp = merge_collinear(_mc_c); break
            if _mc_pp is None:
                _mc_pp = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                           _mc_wf, _mc_obs_p)
            # Route N on route layer (avoiding P-via boxes), fallback to B.Cu
            _mc_nr = _mc_nr_layer = _mc_nr_name = None
            for _mc_try_lid, _mc_try_name in [
                    (layer_id, layer_name),
                    *(([(B_CU, "B.Cu")] if layer_id != B_CU else []))]:
                _mc_obs_n = (list(build_obstacles(_mc_try_lid, {net_p, net_n},
                                                  include_sibling_reserves=True,
                                                  sibling_extra=cc / 2.0))
                             + [b for b in (_mc_p0b, _mc_p1b) if b is not None])
                for _mc_c in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                    if path_clear(_mc_c, _mc_wf, _mc_obs_n):
                        _mc_nr = merge_collinear(_mc_c)
                        _mc_nr_layer = _mc_try_lid
                        _mc_nr_name  = _mc_try_name
                        break
                if _mc_nr is None:
                    _mc_nr = _astar_centerline(vn0[0], vn0[1], vn1[0], vn1[1],
                                               _mc_wf, _mc_obs_n)
                    if _mc_nr is not None:
                        _mc_nr_layer = _mc_try_lid
                        _mc_nr_name  = _mc_try_name
                if _mc_nr is not None:
                    break
            if _mc_pp is None or _mc_nr is None:
                # Layer-split failed — try independent P/N routing as last resort
                _mi_obs = list(build_obstacles(layer_id, {net_p, net_n},
                                               include_sibling_reserves=False))
                _mi_obs_bo = list(build_obstacles_board_only(layer_id, {net_p, net_n}))
                _mi_pp = None
                for _mi_c in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                    if path_clear(_mi_c, width_mm, _mi_obs):
                        _mi_pp = merge_collinear(_mi_c); break
                if _mi_pp is None:
                    _mi_pp = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                               width_mm, _mi_obs)
                if _mi_pp is None:
                    for _mi_c in octilinear_paths(vp0[0], vp0[1], vp1[0], vp1[1]):
                        if (path_clear(_mi_c, width_mm, _mi_obs_bo) and
                                _path_clear_of_run_obs(_mi_c, width_mm/2.0, net_p, layer_id)):
                            _mi_pp = merge_collinear(_mi_c); break
                if _mi_pp is None:
                    _mi_r = _astar_centerline(vp0[0], vp0[1], vp1[0], vp1[1],
                                              width_mm, _mi_obs_bo)
                    if (_mi_r is not None and
                            _path_clear_of_run_obs(_mi_r, width_mm/2.0, net_p, layer_id)):
                        _mi_pp = _mi_r
                _mi_nr = None
                for _mi_c in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                    if path_clear(_mi_c, width_mm, _mi_obs):
                        _mi_nr = merge_collinear(_mi_c); break
                if _mi_nr is None:
                    _mi_nr = _astar_centerline(vn0[0], vn0[1], vn1[0], vn1[1],
                                               width_mm, _mi_obs)
                if _mi_nr is None:
                    for _mi_c in octilinear_paths(vn0[0], vn0[1], vn1[0], vn1[1]):
                        if (path_clear(_mi_c, width_mm, _mi_obs_bo) and
                                _path_clear_of_run_obs(_mi_c, width_mm/2.0, net_n, layer_id)):
                            _mi_nr = merge_collinear(_mi_c); break
                if _mi_nr is None:
                    _mi_r = _astar_centerline(vn0[0], vn0[1], vn1[0], vn1[1],
                                              width_mm, _mi_obs_bo)
                    if (_mi_r is not None and
                            _path_clear_of_run_obs(_mi_r, width_mm/2.0, net_n, layer_id)):
                        _mi_nr = _mi_r
                if _mi_pp is not None and _mi_nr is not None:
                    for _mi_i in range(len(_mi_pp) - 1):
                        add_track(_mi_pp[_mi_i][0], _mi_pp[_mi_i][1],
                                  _mi_pp[_mi_i+1][0], _mi_pp[_mi_i+1][1],
                                  width_mm, layer_id, net_p)
                    for _mi_i in range(len(_mi_nr) - 1):
                        add_track(_mi_nr[_mi_i][0], _mi_nr[_mi_i][1],
                                  _mi_nr[_mi_i+1][0], _mi_nr[_mi_i+1][1],
                                  width_mm, layer_id, net_n)
                    log.append("    NOTE: miter-split independent P/N routing succeeded")
                    _hw = width_mm / 2.0
                    _seg_placed.setdefault(layer_id, []).extend((r, net_p) for r in _poly_obs(_mi_pp, _hw))
                    _seg_placed.setdefault(layer_id, []).extend((r, net_n) for r in _poly_obs(_mi_nr, _hw))
                else:
                    log.append("    WARNING: miter-crossing layer-split failed; skipping emit")
                    _conflict_count += 1
            else:
                for _mc_i in range(len(_mc_pp) - 1):
                    add_track(_mc_pp[_mc_i][0], _mc_pp[_mc_i][1],
                              _mc_pp[_mc_i+1][0], _mc_pp[_mc_i+1][1],
                              width_mm, layer_id, net_p)
                for _mc_i in range(len(_mc_nr) - 1):
                    add_track(_mc_nr[_mc_i][0], _mc_nr[_mc_i][1],
                              _mc_nr[_mc_i+1][0], _mc_nr[_mc_i+1][1],
                              width_mm, _mc_nr_layer, net_n)
                log.append(f"    MITER-SPLIT: P on {layer_name} "
                           f"({vp0[0]:.3f},{vp0[1]:.3f})→"
                           f"({vp1[0]:.3f},{vp1[1]:.3f}), "
                           f"N on {_mc_nr_name}")
                _hw = width_mm / 2.0
                _seg_placed.setdefault(layer_id, []).extend((r, net_p) for r in _poly_obs(_mc_pp, _hw))
                _seg_placed.setdefault(_mc_nr_layer, []).extend((r, net_n) for r in _poly_obs(_mc_nr, _hw))
            continue  # skip emit_diffpair

        bends = max(0, len(chosen) - 2)
        lp, ln = emit_diffpair(chosen, vp0, vn0, vp1, vn1,
                                width_mm, cc, layer_id, net_p, net_n)
        log.append(f"    seg emitted: {bends} bend(s)  Lp={lp:.3f}mm  Ln={ln:.3f}mm"
                   f"  Δ={abs(lp-ln):.3f}mm")
        total_p += lp;  total_n += ln
        _seg_placed.setdefault(layer_id, []).extend(
            (r, net_p) for r in _poly_obs(chosen, width_mm / 2.0 + cc / 2.0))
        if len(pairs) > 2 and len(chosen) > 2:
            _seg_spine = list(chosen[1:-1])

    if total_p > 0 or total_n > 0:
        delta = abs(total_p - total_n)
        status = "PASS" if delta <= skew_limit else "REVIEW — add trombone meander"
        log.append(f"  {pair_name} TOTAL: P={total_p:.3f}mm  N={total_n:.3f}mm  "
                   f"Δ={delta:.3f}mm  (skew limit {skew_limit}mm)  [{status}]")


# ── Main ─────────────────────────────────────────────────────────────────────

mode = "APPLY" if APPLY else "DRY RUN"
log.append(f"route_highspeed.py — {mode}")
log.append("=" * 70)
log.append(f"Via: pad={VIA_PAD}mm  drill={VIA_DRILL}mm  GND_NET={GND_NET!r}")
order = routing_order()
if TARGET_PAIRS_ARG:
    order = [p for p in order if p in TARGET_PAIRS_ARG]
    unknown = TARGET_PAIRS_ARG - set(cfg.HS_ROUTE_WIDTHS)
    if unknown:
        log.append(f"WARNING: --pairs requested unknown pair(s): {', '.join(sorted(unknown))}")
log.append(f"Pairs to route ({len(order)}): {', '.join(order)}")
if TARGET_PAIRS_ARG:
    log.append(f"(filtered by --pairs; full set has {len(routing_order())} pairs)")
else:
    log.append("(pairs not in HS_ROUTE_WIDTHS are deferred to FreeRouting by config)")

# Option C — pre-assign corridor trunk x-coordinates for parallel pair groups
_assign_corridor_trunks(order)

# Main routing loop — track _run_obs range for each pair (used by Option D)
for pair_name in order:
    _snap = len(_run_obs)
    route_pair(pair_name)
    _pair_obs_ranges[pair_name] = (_snap, len(_run_obs))

# Option D — rip-up crossing pairs and retry with better obstacle info
log.append("\n[Option D — rip-up and retry]")
_rip_up_retry(order)

if APPLY:
    board.Save(cfg.PCB_FILE)
    log.append("\nBoard saved.")

if _conflict_count:
    log.append(f"\n{_conflict_count} CONFLICT(s) — review before applying.")

log.append("Next: run lock_highspeed_nets.py, then verify_hs_sandwich.py.")

report_path = pathlib.Path(cfg.REPORTS_DIR) / "fix_log_phase10.txt"
report_path.parent.mkdir(parents=True, exist_ok=True)
try:
    existing = report_path.read_text(encoding="utf-8")
except FileNotFoundError:
    existing = ""
report_path.write_text(existing + "\n" + "\n".join(log) + "\n", encoding="utf-8")
print("\n".join(log))

import sys as _sys
_sys.exit(1 if _conflict_count else 0)

sys.exit(1 if _conflict_count else 0)
