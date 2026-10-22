"""
route_critical.py — Phase 10 Part A (rewrite)

Routes switching converter loops defined in routing_config.py::SWITCHING_LOOPS.
Simple rectilinear power routing on the configured loop layer with pad-transition
vias when pads and routing layer differ.

Algorithm (per net):
  1. Locate two pads (source, destination).
  2. If either pad is not on the loop layer, place a short offset stub on the pad
     layer, then a through-hole via, then route the main body on the loop layer.
  3. Try Manhattan routing (H-then-V, then V-then-H) at the requested width.
  4. If both are blocked, try each with a neckdown stub near the narrower endpoint.
  5. If still blocked and via-detour is allowed, attempt the same routing on
     LAYER_SCHEME.power_alt.
  6. Otherwise log a conflict.

Obstacle model:
  build_obstacles(layer_id, net_excl_pads, net_excl_tracks) — TWO exclusion sets:
    net_excl_pads   : nets to exclude from pad obstacles (this net + partner-pad's
                      net if you want the partner pad to be a free endpoint)
    net_excl_tracks : nets to exclude from board tracks / vias / _run_obs
                      (only the current net — placed copper on other nets IS an obstacle)

Rules honoured:
  - Zero hardcoded refs / net names / layer numeric constants; everything from cfg.
  - board.GetLayerID(name) — never numeric layer constants.
  - Locked footprints are never modified (this script only adds tracks/vias).
  - board.Remove() never called.
  - Coordinates in mm via pcbnew.FromMM / pcbnew.ToMM.

Usage:
  python route_critical.py           # dry run
  python route_critical.py --apply   # write tracks/vias, save board
"""

import sys, os, math, pathlib, heapq

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import routing_config as cfg

KICAD_BIN = str(pathlib.Path(cfg.KICAD_SITE_PKGS).parent.parent)
os.environ["PATH"] = KICAD_BIN + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, cfg.KICAD_SITE_PKGS)
import pcbnew

APPLY                 = "--apply" in sys.argv
CLEARANCE_MM          = 0.15
BOARD_EDGE_CLEARANCE  = 0.50   # mm — copper-to-edge DRC minimum
MAZE_RES_MM           = 0.1
MAZE_MARGIN           = 16.0
BEND_COST             = 4
ANCHOR_CONN_TOL       = 0.05   # mm — tolerance for "already has a track endpoint here"
COMP_KEEPOUT_MM       = 0.5    # mm — margin around component bboxes for trunk routing

board     = pcbnew.LoadBoard(cfg.PCB_FILE)
F_CU      = board.GetLayerID("F.Cu")
B_CU      = board.GetLayerID("B.Cu")
ALT_LAYER = board.GetLayerID(cfg.LAYER_SCHEME.get("power_alt", "F.Cu"))
log       = []
_conflict_count = 0

# Virtual copper table: (x1, y1, x2, y2, half_width, layer_id, net_name)
# Point obstacles (vias): x1==x2, y1==y2, half_width==via_radius.
_run_obs: list = []

# Board bounding box — used to keep routes inside the board outline.
_bbox   = board.GetBoardEdgesBoundingBox()
_BRD_X0 = pcbnew.ToMM(_bbox.GetLeft())
_BRD_X1 = pcbnew.ToMM(_bbox.GetRight())
_BRD_Y0 = pcbnew.ToMM(_bbox.GetTop())
_BRD_Y1 = pcbnew.ToMM(_bbox.GetBottom())


# ── Geometry helpers ──────────────────────────────────────────────────────────

def mm(v):   return pcbnew.FromMM(v)
def tomm(v): return pcbnew.ToMM(v)

def rect_overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

def seg_rect(x1, y1, x2, y2, w):
    hw = w / 2
    return (min(x1, x2) - hw, min(y1, y2) - hw,
            max(x1, x2) + hw, max(y1, y2) + hw)

def pad_obs_rect(pad):
    pos = pad.GetPosition()
    px, py = tomm(pos.x), tomm(pos.y)
    size = pad.GetSize()
    sx, sy = tomm(size.x) / 2, tomm(size.y) / 2
    try:
        ang = math.radians(pad.GetOrientationDegrees())
    except AttributeError:
        ang = 0.0
    ca, sa = abs(math.cos(ang)), abs(math.sin(ang))
    hx = sx * ca + sy * sa + CLEARANCE_MM
    hy = sx * sa + sy * ca + CLEARANCE_MM
    return (px - hx, py - hy, px + hx, py + hy)


# ── Pad lookup ────────────────────────────────────────────────────────────────

def get_pad_info(ref, net_name):
    """Return (x_mm, y_mm, layer_id, min_pad_dim_mm, layer_set) or None."""
    for fp in board.GetFootprints():
        if fp.GetReference() != ref:
            continue
        for pad in fp.Pads():
            if pad.GetNetname() == net_name:
                pos  = pad.GetPosition()
                size = pad.GetSize()
                return (tomm(pos.x), tomm(pos.y),
                        pad.GetLayer(),
                        min(tomm(size.x), tomm(size.y)),
                        pad.GetLayerSet())
    return None


def pad_on_layer(layer_set, layer_id):
    return layer_set is not None and layer_set.Contains(layer_id)


# ── Obstacle building ─────────────────────────────────────────────────────────

def build_obstacles(layer_id, net_excl_pads, net_excl_tracks=None,
                     comp_keepout_mm=0.0):
    """
    Return forbidden rectangles on layer_id (clearance-expanded).

    net_excl_pads   : set of nets whose pads are NOT obstacles (endpoints).
    net_excl_tracks : set of nets whose board tracks/vias/_run_obs entries
                      are NOT obstacles.  Defaults to net_excl_pads.
    comp_keepout_mm : when > 0, add each footprint's bounding box (expanded by
                      this margin) as an obstacle, skipping footprints that have
                      any pad on a net in net_excl_pads.  Keeps trunk power
                      routes out of populated component areas so future vias can
                      be placed there.
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
            try:
                vr = tomm(track.GetWidth(layer_id)) / 2
            except TypeError:
                vr = tomm(track.GetWidth()) / 2
            vx = tomm(track.GetPosition().x)
            vy = tomm(track.GetPosition().y)
            obs.append((vx - vr - c, vy - vr - c, vx + vr + c, vy + vr + c))
        elif track.GetLayer() == layer_id:
            tw = tomm(track.GetWidth()) / 2
            x0 = tomm(min(track.GetStart().x, track.GetEnd().x)) - tw
            x1 = tomm(max(track.GetStart().x, track.GetEnd().x)) + tw
            y0 = tomm(min(track.GetStart().y, track.GetEnd().y)) - tw
            y1 = tomm(max(track.GetStart().y, track.GetEnd().y)) + tw
            obs.append((x0 - c, y0 - c, x1 + c, y1 + c))

    for rx1, ry1, rx2, ry2, rhw, rlayer, rnet in _run_obs:
        if rnet in net_excl_tracks or rlayer != layer_id:
            continue
        obs.append((min(rx1, rx2) - rhw - c, min(ry1, ry2) - rhw - c,
                    max(rx1, rx2) + rhw + c, max(ry1, ry2) + rhw + c))

    # Board edge keepouts — routes must stay BOARD_EDGE_CLEARANCE inside the outline.
    # Obstacle boundary placed at the edge minus the required clearance; the maze/
    # manhattan router's own half-width expansion then enforces the full rule.
    ec  = BOARD_EDGE_CLEARANCE
    INF = 9999.0
    obs.extend([
        (_BRD_X1 - ec, -INF,  INF,          INF ),   # right edge
        (-INF,         -INF,  _BRD_X0 + ec, INF ),   # left edge
        (-INF,         -INF,  INF,           _BRD_Y0 + ec),  # top edge
        (-INF,          _BRD_Y1 - ec, INF,  INF ),   # bottom edge
    ])

    # Component body keepout — prevents trunk routes from threading through
    # populated areas where future vias need to be placed.  Skips footprints
    # that have any pad on the current net (we still need to reach those).
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

    return obs


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


def path_clear(waypoints, w, obstacles):
    for i in range(len(waypoints) - 1):
        r = seg_rect(waypoints[i][0], waypoints[i][1],
                     waypoints[i+1][0], waypoints[i+1][1], w)
        for o in obstacles:
            if rect_overlap(r, o):
                return False
    return True


# ── Collinear merge (used before emit) ────────────────────────────────────────

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


# ── Manhattan router ──────────────────────────────────────────────────────────

def route_manhattan(x1, y1, x2, y2, w, obstacles):
    for path in ([(x1, y1), (x2, y1), (x2, y2)],
                 [(x1, y1), (x1, y2), (x2, y2)]):
        if path_clear(path, w, obstacles):
            return merge_collinear(path)
    return None


# ── Maze (A*) fallback — favours straight runs ────────────────────────────────

def route_maze(x1, y1, x2, y2, w, obstacles):
    hw   = w / 2
    cobs = [(o[0] - hw, o[1] - hw, o[2] + hw, o[3] + hw) for o in obstacles]

    res = MAZE_RES_MM
    gx0 = min(x1, x2) - MAZE_MARGIN
    gx1 = max(x1, x2) + MAZE_MARGIN
    gy0 = min(y1, y2) - MAZE_MARGIN
    gy1 = max(y1, y2) + MAZE_MARGIN
    cols = max(2, round((gx1 - gx0) / res)) + 1
    rows = max(2, round((gy1 - gy0) / res)) + 1

    def to_ij(x, y):
        return (max(0, min(cols - 1, round((x - gx0) / res))),
                max(0, min(rows - 1, round((y - gy0) / res))))
    def to_xy(i, j):
        return gx0 + i * res, gy0 + j * res

    blocked = [[False] * rows for _ in range(cols)]
    for ox0, oy0, ox1, oy1 in cobs:
        ci0 = max(0, int((ox0 - gx0) / res))
        ci1 = min(cols - 1, int((ox1 - gx0) / res) + 1)
        rj0 = max(0, int((oy0 - gy0) / res))
        rj1 = min(rows - 1, int((oy1 - gy0) / res) + 1)
        for ci in range(ci0, ci1 + 1):
            for rj in range(rj0, rj1 + 1):
                blocked[ci][rj] = True

    si, sj = to_ij(x1, y1)
    gi, gj = to_ij(x2, y2)
    blocked[si][sj] = False
    blocked[gi][gj] = False

    best_g    = {(si, sj): 0}
    came_from = {(si, sj): None}
    visited   = set()
    open_heap = [(abs(gi - si) + abs(gj - sj), 0, si, sj, 0, 0)]

    while open_heap:
        _, g, ci, cj, ldi, ldj = heapq.heappop(open_heap)
        if (ci, cj) in visited:
            continue
        visited.add((ci, cj))

        if ci == gi and cj == gj:
            cells = []
            cur = (gi, gj)
            while cur is not None:
                cells.append(cur)
                cur = came_from[cur]
            cells.reverse()
            pts = [to_xy(i, j) for i, j in cells]
            if pts:
                pts[0]  = (x1, y1)
                pts[-1] = (x2, y2)
            return merge_collinear(pts)

        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ni, nj = ci + di, cj + dj
            if not (0 <= ni < cols and 0 <= nj < rows):
                continue
            if blocked[ni][nj]:
                continue
            bend   = BEND_COST if (ldi != 0 or ldj != 0) and (di != ldi or dj != ldj) else 0
            new_g  = g + 1 + bend
            if new_g < best_g.get((ni, nj), 10**9):
                best_g[(ni, nj)]    = new_g
                came_from[(ni, nj)] = (ci, cj)
                h = abs(gi - ni) + abs(gj - nj)
                heapq.heappush(open_heap, (new_g + h, new_g, ni, nj, di, dj))
    return None


# ── PCB write helpers ─────────────────────────────────────────────────────────

def add_track(x1, y1, x2, y2, width_mm, layer_id, net_name):
    if abs(x2 - x1) < 1e-6 and abs(y2 - y1) < 1e-6:
        return 0.0
    hw = width_mm / 2
    _run_obs.append((x1, y1, x2, y2, hw, layer_id, net_name))
    length = math.hypot(x2 - x1, y2 - y1)
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


def add_via(x, y, net_name, pad_mm, drill_mm):
    via_r = pad_mm / 2
    for layer in (F_CU, B_CU,
                  board.GetLayerID("In1.Cu"), board.GetLayerID("In2.Cu"),
                  board.GetLayerID("In3.Cu"), board.GetLayerID("In4.Cu")):
        if layer >= 0:
            _run_obs.append((x, y, x, y, via_r, layer, net_name))
    log.append(f"    VIA {net_name} at ({x:.3f},{y:.3f})"
               f"  pad={pad_mm}mm drill={drill_mm}mm")
    if not APPLY:
        return
    net = board.FindNet(net_name)
    if net is None:
        log.append(f"    WARNING: net {net_name!r} not in board")
        return
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
    v.SetWidth(mm(pad_mm))
    v.SetDrill(mm(drill_mm))
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetNet(net)
    v.SetLayerPair(F_CU, B_CU)
    board.Add(v)


# ── Pad-to-layer transition via ───────────────────────────────────────────────

def place_pad_via(ref, px, py, pad_layer, net_name, route_layer_id,
                  via_pad, via_drill, stub_w, *, require_offset=False):
    """
    Place a short stub on pad_layer from (px,py) to a nearby clear point, and
    a through via at that point.  Returns via (vx, vy) as the new routing anchor.

    If require_offset=True and no clear offset position exists, returns None
    instead of falling back to a via at the blocked pad center.
    """
    via_r = via_pad / 2.0
    c     = CLEARANCE_MM
    obs_pad   = build_obstacles(pad_layer,      {net_name})
    obs_route = build_obstacles(route_layer_id, {net_name})

    # Try the requested stub width first, then narrower fallbacks.
    # A narrower tail-stub still connects the pad to its via; the full-width
    # trunk routes from the via center so there is no current bottleneck.
    stub_widths = [stub_w]
    if stub_w > 0.4 + 1e-6:
        stub_widths.append(0.4)
    if stub_w > 0.3 + 1e-6:
        stub_widths.append(0.3)

    for sw in stub_widths:
        for dist in (0.5, 0.7, 1.0, 1.2, 1.5, 2.0, 2.5):
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0),
                            (-1, -1), (-1, 1), (1, -1), (1, 1)):
                norm = math.hypot(dx, dy)
                vx = px + dx / norm * dist
                vy = py + dy / norm * dist
                via_rect  = (vx - via_r - c, vy - via_r - c,
                             vx + via_r + c, vy + via_r + c)
                stub_rect = seg_rect(px, py, vx, vy, sw + 2 * c)
                if (not any(rect_overlap(via_rect,  o) for o in obs_pad)   and
                        not any(rect_overlap(via_rect,  o) for o in obs_route) and
                        not any(rect_overlap(stub_rect, o) for o in obs_pad)):
                    add_track(px, py, vx, vy, sw, pad_layer, net_name)
                    add_via(vx, vy, net_name, via_pad, via_drill)
                    log.append(f"    pad-via {ref}: stub→({vx:.3f},{vy:.3f})" +
                               (f" [neckdown {sw}mm]" if sw < stub_w else ""))
                    return vx, vy

    if require_offset:
        log.append(f"    SKIP {ref}: no clear via offset — pad unreachable in full-net pass")
        return None
    log.append(f"    WARNING: no offset via near {ref} — via at pad center")
    add_via(px, py, net_name, via_pad, via_drill)
    return px, py


# ── Neckdown: shorten wide trace to fit pad exit ─────────────────────────────

def find_neckdown_exit(px, py, dest_x, dest_y, net_name,
                        full_w, neck_w, neck_max, layer_id):
    hw  = full_w / 2.0
    nhw = (neck_w or full_w) / 2.0
    dx_raw = dest_x - px;  dy_raw = dest_y - py
    if abs(dx_raw) >= abs(dy_raw):
        ux, uy = (1.0 if dx_raw >= 0 else -1.0), 0.0
    else:
        ux, uy = 0.0, (1.0 if dy_raw >= 0 else -1.0)

    ic_obs = build_obstacles(layer_id, {net_name})

    t = 0.0
    while t <= neck_max + 1e-9:
        sx, sy = px + ux * t, py + uy * t
        # Full-width trace must fit at the exit point.
        pt = (sx - hw, sy - hw, sx + hw, sy + hw)
        if not any(rect_overlap(pt, o) for o in ic_obs):
            # Stub path from (px,py) → (sx,sy) must also be clear at neck_w.
            # Without this check the stub can thread over another net's via.
            if t < 1e-9:
                return sx, sy  # zero-length stub always ok
            stub_r = seg_rect(px, py, sx, sy, neck_w or full_w)
            if not any(rect_overlap(stub_r, o) for o in ic_obs):
                return sx, sy
        t += MAZE_RES_MM
    # No valid exit found — return pad position so caller routes at full width.
    return px, py


# ── Duplicate track guard ─────────────────────────────────────────────────────

def net_already_routed(net_name, layer_id):
    for track in board.GetTracks():
        if track.GetClass() == "PCB_VIA":
            continue
        if track.GetNetname() == net_name and track.GetLayer() == layer_id:
            return True
    return False


# ── Full-net routing helpers ──────────────────────────────────────────────────

def anchor_accessible(ax, ay, net_name, layer_id, width_mm):
    """
    Return True if at least one cardinal direction from (ax,ay) is clear
    at width_mm.  An anchor that fails this check is physically surrounded
    by other-net copper and unreachable by a trace of width_mm — e.g. a
    fanout via packed between signal vias (J_DSI1 between TOUCH_* vias).
    """
    obs = build_obstacles(layer_id, {net_name})
    step = MAZE_RES_MM
    for dx, dy in ((step, 0), (-step, 0), (0, step), (0, -step)):
        r = seg_rect(ax + dx, ay + dy, ax, ay, width_mm)
        if not any(rect_overlap(r, o) for o in obs):
            return True
    return False



PAD_STUB_TOL  = 0.25   # mm — stub endpoint must be this close to pad centre
_VIA_CONN_TOL = 0.10   # mm — stub's far end must be this close to via centre

def find_pad_via(px, py, pad_layer_id, net_name):
    """
    Return (vx, vy) if pad at (px, py) already has a stub+via on net_name,
    else None.  Checks both board objects and _run_obs (vias placed this run).

    Accepted as "this pad's via":
      (a) a non-via track on pad_layer_id/net_name has one endpoint within
          PAD_STUB_TOL of (px,py) AND its other endpoint within _VIA_CONN_TOL
          of any via on net_name; OR
      (b) a via on net_name sits within _VIA_CONN_TOL of (px,py) directly.
    """
    all_vias = []
    for t in board.GetTracks():
        if t.GetClass() != "PCB_VIA" or t.GetNetname() != net_name:
            continue
        all_vias.append((tomm(t.GetPosition().x), tomm(t.GetPosition().y)))
    for rx1, ry1, rx2, ry2, _hw, _lyr, rnet in _run_obs:
        if rnet == net_name and rx1 == rx2 and ry1 == ry2:
            all_vias.append((rx1, ry1))

    for vx, vy in all_vias:
        if math.hypot(vx - px, vy - py) <= _VIA_CONN_TOL:
            return (vx, vy)

    def _check_stub(sx, sy, ex, ey):
        near_s = math.hypot(sx - px, sy - py) <= PAD_STUB_TOL
        near_e = math.hypot(ex - px, ey - py) <= PAD_STUB_TOL
        if not near_s and not near_e:
            return None
        ox, oy = (ex, ey) if near_s else (sx, sy)
        for vx, vy in all_vias:
            if math.hypot(ox - vx, oy - vy) <= _VIA_CONN_TOL:
                return (vx, vy)
        return None

    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA" or t.GetNetname() != net_name:
            continue
        if t.GetLayer() != pad_layer_id:
            continue
        result = _check_stub(tomm(t.GetStart().x), tomm(t.GetStart().y),
                              tomm(t.GetEnd().x),   tomm(t.GetEnd().y))
        if result is not None:
            return result

    for rx1, ry1, rx2, ry2, _hw, rlyr, rnet in _run_obs:
        if rnet != net_name or rlyr != pad_layer_id or (rx1 == rx2 and ry1 == ry2):
            continue
        result = _check_stub(rx1, ry1, rx2, ry2)
        if result is not None:
            return result

    return None


def via_exists_at(x, y, net_name, tol=0.10):
    """True if a via on net_name already exists within tol mm of (x, y)."""
    for t in board.GetTracks():
        if t.GetClass() != "PCB_VIA" or t.GetNetname() != net_name:
            continue
        if math.hypot(tomm(t.GetPosition().x) - x,
                      tomm(t.GetPosition().y) - y) <= tol:
            return True
    for rx1, ry1, rx2, ry2, _hw, _lyr, rnet in _run_obs:
        if rnet == net_name and rx1 == rx2 and ry1 == ry2:
            if math.hypot(rx1 - x, ry1 - y) <= tol:
                return True
    return False


def get_pad_anchor(ref, pad, net_name, route_layer_id, via_pad, via_drill, stub_w,
                   require_offset=False):
    """
    Return (x, y) routing anchor on route_layer_id, or None if unreachable.
    Priority: nearby fanout via (with confirmed stub) > pad on layer > new via.

    A nearby via is only accepted as this pad's anchor when a track endpoint
    on the pad's own layer exists within PAD_STUB_TOL of the pad centre —
    confirming that the pad has a stub connecting it to the via.  Without this
    check, a different component's via on the same net (within
    4mm radius) would be misidentified as this pad's anchor, causing
    the pad to be marked connected when it has no copper to the routed network.

    When require_offset=True, returns None instead of a blocked pad-center
    anchor (used by route_net_full to skip truly unreachable pads).
    """
    px = tomm(pad.GetPosition().x); py = tomm(pad.GetPosition().y)
    via = find_pad_via(px, py, pad.GetLayer(), net_name)
    if via is not None:
        return via
    ls = pad.GetLayerSet()
    if pad_on_layer(ls, route_layer_id):
        return (px, py)
    if via_pad is not None:
        return place_pad_via(ref, px, py, pad.GetLayer(), net_name,
                             route_layer_id, via_pad, via_drill, stub_w,
                             require_offset=require_offset)
    return (px, py)


def anchor_connected(ax, ay, net_name, layer_id):
    """
    True if (ax, ay) has a TRACK endpoint on net_name/layer_id.
    Standalone fanout vias are intentionally excluded — a via on the net
    without a connecting trace is not yet part of the routed network.
    """
    for rx1, ry1, rx2, ry2, _hw, rlyr, rnet in _run_obs:
        if rnet != net_name or rlyr != layer_id:
            continue
        if rx1 == rx2 and ry1 == ry2:
            continue  # point obstacle = via — skip
        if (math.hypot(ax - rx1, ay - ry1) <= ANCHOR_CONN_TOL or
                math.hypot(ax - rx2, ay - ry2) <= ANCHOR_CONN_TOL):
            return True
    for track in board.GetTracks():
        if track.GetNetname() != net_name or track.GetClass() == "PCB_VIA":
            continue
        if track.GetLayer() == layer_id:
            sx = tomm(track.GetStart().x); sy = tomm(track.GetStart().y)
            ex = tomm(track.GetEnd().x);   ey = tomm(track.GetEnd().y)
            if (math.hypot(ax - sx, ay - sy) <= ANCHOR_CONN_TOL or
                    math.hypot(ax - ex, ay - ey) <= ANCHOR_CONN_TOL):
                return True
    return False


def route_between(x1, y1, x2, y2, net_name, width_mm, layer_id,
                  neck_w, neck_max, via_pad, via_drill, comp_keepout_mm=0.0):
    """Route from (x1,y1) to (x2,y2) on layer_id at width_mm.
    Returns the routed waypoint list on success, None on conflict."""
    global _conflict_count
    use_neck = neck_w and neck_max and neck_w < width_mm
    if use_neck:
        ex1, ey1 = find_neckdown_exit(x1, y1, x2, y2, net_name,
                                       width_mm, neck_w, neck_max, layer_id)
        ex2, ey2 = find_neckdown_exit(x2, y2, x1, y1, net_name,
                                       width_mm, neck_w, neck_max, layer_id)
    else:
        ex1, ey1 = x1, y1
        ex2, ey2 = x2, y2

    obstacles = build_obstacles(layer_id, {net_name},
                                comp_keepout_mm=comp_keepout_mm)
    waypoints  = route_manhattan(ex1, ey1, ex2, ey2, width_mm, obstacles)
    used_layer = layer_id

    if waypoints is None:
        log.append(f"    [manhattan blocked] → maze")
        waypoints = route_maze(ex1, ey1, ex2, ey2, width_mm, obstacles)

    # If component keepout made the route impossible, retry without it.
    if waypoints is None and comp_keepout_mm > 0:
        log.append(f"    [keepout blocked] → retry without component keepout")
        fb_obs = build_obstacles(layer_id, {net_name}, comp_keepout_mm=0.0)
        waypoints = (route_manhattan(ex1, ey1, ex2, ey2, width_mm, fb_obs) or
                     route_maze(ex1, ey1, ex2, ey2, width_mm, fb_obs))
        if waypoints is not None:
            log.append(f"    [keepout fallback succeeded]")

    if waypoints is None and via_pad is not None and ALT_LAYER != layer_id:
        alt_obs = build_obstacles(ALT_LAYER, {net_name},
                                  comp_keepout_mm=comp_keepout_mm)
        alt_wp  = (route_manhattan(ex1, ey1, ex2, ey2, width_mm, alt_obs) or
                   route_maze(ex1, ey1, ex2, ey2, width_mm, alt_obs))
        if alt_wp is not None:
            log.append(f"    [via-detour] → {board.GetLayerName(ALT_LAYER)}")
            if not via_exists_at(ex1, ey1, net_name):
                add_via(ex1, ey1, net_name, via_pad, via_drill)
            waypoints = alt_wp; used_layer = ALT_LAYER
            if not via_exists_at(ex2, ey2, net_name):
                add_via(ex2, ey2, net_name, via_pad, via_drill)

    if waypoints is None:
        region = (min(ex1,ex2)-2, min(ey1,ey2)-2, max(ex1,ex2)+2, max(ey1,ey2)+2)
        bl = identify_blockers(region, layer_id, {net_name})
        log.append(f"    CONFLICT: no path {net_name} ({x1:.2f},{y1:.2f})→({x2:.2f},{y2:.2f})"
                   f"  [{', '.join(bl) or 'none'}]")
        _conflict_count += 1
        return None

    if use_neck:
        if abs(ex1-x1) > 1e-6 or abs(ey1-y1) > 1e-6:
            add_track(x1, y1, ex1, ey1, neck_w, layer_id, net_name)
        if abs(ex2-x2) > 1e-6 or abs(ey2-y2) > 1e-6:
            add_track(ex2, ey2, x2, y2, neck_w, layer_id, net_name)
    for i in range(len(waypoints) - 1):
        add_track(waypoints[i][0], waypoints[i][1],
                  waypoints[i+1][0], waypoints[i+1][1],
                  width_mm, used_layer, net_name)
    return waypoints


def route_net_full(net_name, width_mm, layer_id, via_pad, via_drill,
                   neck_w, neck_max, tap_refs=None):
    """
    Connect all pads on net_name using greedy Prim's MST on layer_id.

    tap_refs: optional list of component refs that are low-current taps (sense
    resistors, test points).  Tap nodes connect narrow off the spine; all other
    nodes (spine) connect at full width.

    Spine-waypoint extension: after routing each trunk edge, its intermediate
    waypoints are added to the spine candidate pool.  Subsequent trunk routes
    can then start from a branch point on an already-routed trace rather than
    re-traversing the same initial segment, eliminating trace overlaps when
    multiple branches leave the same hub.
    """
    via_stub_w = neck_w or 0.4

    # Collect one anchor per unique position.
    # Each anchor is (ax, ay, ref, eff_w).
    anchors = []; seen = set()
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            if pad.GetNetname() != net_name:
                continue
            px = tomm(pad.GetPosition().x); py = tomm(pad.GetPosition().y)
            anchor = get_pad_anchor(ref, pad, net_name, layer_id,
                                    via_pad, via_drill, via_stub_w,
                                    require_offset=True)
            if anchor is None:
                continue
            ax, ay = anchor
            # Compute effective connection width at this anchor before the
            # accessibility gate.  Pads reached via a transition via connect at
            # via_pad width (not the full trunk width); the trunk expands to
            # width_mm further along the route.  Using eff_w here prevents
            # fine-pitch connector / SOM pads from being rejected by an
            # anchor_accessible check that would require a 2.5mm corridor
            # directly out of a 0.6mm via.
            if find_pad_via(px, py, pad.GetLayer(), net_name) is not None:
                eff_w = min(via_pad, width_mm)
            elif pad_on_layer(pad.GetLayerSet(), layer_id):
                eff_w = min(tomm(pad.GetSize().x), tomm(pad.GetSize().y), width_mm)
            else:
                eff_w = min(via_pad, width_mm)
            if not anchor_accessible(ax, ay, net_name, layer_id, eff_w):
                log.append(f"  [full-net] SKIP {ref}: anchor ({ax:.3f},{ay:.3f})"
                           f" unreachable at {eff_w}mm width")
                continue
            key = (round(ax, 3), round(ay, 3))
            if key in seen:
                continue
            seen.add(key); anchors.append((ax, ay, ref, eff_w))

    if len(anchors) < 2:
        return

    log.append(f"  [full-net] {net_name}: {len(anchors)} anchor(s)")

    connected   = [a for a in anchors if     anchor_connected(a[0],a[1],net_name,layer_id)]
    unconnected = [a for a in anchors if not anchor_connected(a[0],a[1],net_name,layer_id)]

    if not unconnected:
        log.append(f"    all anchors already connected — skip")
        return
    if not connected:
        connected = [unconnected.pop(0)]

    log.append(f"    connected={len(connected)}  unconnected={len(unconnected)}")

    tap_set = set(tap_refs) if tap_refs else set()

    # Greedy Prim's with spine-waypoint extension.
    # spine_pts: (x, y, anchor_or_None) — routable points on the spine.
    # anchor is None for intermediate waypoints from previously routed trunk edges.
    # Non-tap nodes must connect from spine_pts so trunk traces never originate
    # from a narrow tap via.
    _conn = list(connected); _unconn = list(unconnected)
    spine_pts = [(a[0], a[1], a)
                 for a in _conn if a[2] not in tap_set]

    tap_edges = []  # deferred: (src_xy, src_anchor_or_None, dest_anchor)

    while _unconn:
        best_dist = float("inf")
        best_src_xy = None; best_src_anchor = None; best_u = None
        for u in _unconn:
            u_is_tap = u[2] in tap_set
            srcs = ([(a[0], a[1], a) for a in _conn]
                    if (u_is_tap or not spine_pts)
                    else spine_pts)
            for sx, sy, sa in srcs:
                d = math.hypot(sx - u[0], sy - u[1])
                if d < best_dist:
                    best_dist = d
                    best_src_xy = (sx, sy); best_src_anchor = sa; best_u = u

        _conn.append(best_u); _unconn.remove(best_u)

        if best_u[2] in tap_set:
            tap_edges.append((best_src_xy, best_src_anchor, best_u))
        else:
            sx, sy = best_src_xy
            ux, uy = best_u[0], best_u[1]
            src_lbl = (f"{best_src_anchor[2]}({sx:.2f},{sy:.2f})"
                       if best_src_anchor else f"wpt({sx:.2f},{sy:.2f})")
            log.append(f"    [trunk {width_mm}mm] {src_lbl}"
                       f" → {best_u[2]}({ux:.2f},{uy:.2f})"
                       f"  d={math.hypot(sx-ux, sy-uy):.1f}mm")
            waypts = route_between(sx, sy, ux, uy, net_name, width_mm, layer_id,
                                   None, None, via_pad, via_drill,
                                   comp_keepout_mm=COMP_KEEPOUT_MM)
            # Extend spine with destination and intermediate waypoints so the
            # next trunk route can branch off the already-placed trace instead
            # of re-traversing the shared initial segment.
            spine_pts.append((ux, uy, best_u))
            if waypts and len(waypts) > 2:
                for wx, wy in waypts[1:-1]:
                    spine_pts.append((wx, wy, None))

    # Route tap edges (narrow, no component keepout).
    # Re-evaluate each tap's source against both the fully-populated spine_pts
    # AND projections onto every routed trunk segment.  The segment projection
    # finds the closest T-junction point on an existing trunk, so the tap always
    # branches perpendicularly rather than re-traversing a co-directional stretch.
    def _closest_on_seg(px, py, x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return x1, y1
        t = max(0.0, min(1.0, ((px-x1)*dx + (py-y1)*dy) / (dx*dx + dy*dy)))
        return x1 + t*dx, y1 + t*dy

    for src_xy, src_anchor, best_u in tap_edges:
        route_w = min(best_u[3], width_mm)
        ux, uy = best_u[0], best_u[1]
        sx, sy, sa = src_xy[0], src_xy[1], src_anchor
        best_tap_d = float("inf")
        for wx, wy, wa in spine_pts:
            d = math.hypot(wx - ux, wy - uy)
            if d < best_tap_d:
                best_tap_d = d; sx, sy, sa = wx, wy, wa
        for rx1, ry1, rx2, ry2, rhw, rlayer, rnet in _run_obs:
            if rnet != net_name or rlayer != layer_id:
                continue
            if rx1 == rx2 and ry1 == ry2:  # skip via point obstacles
                continue
            cx, cy = _closest_on_seg(ux, uy, rx1, ry1, rx2, ry2)
            d = math.hypot(cx - ux, cy - uy)
            if d < best_tap_d:
                best_tap_d = d; sx, sy, sa = cx, cy, None
        src_lbl = (f"{sa[2]}({sx:.2f},{sy:.2f})"
                   if sa else f"wpt({sx:.2f},{sy:.2f})")
        log.append(f"    [tap {route_w:.2f}mm] {src_lbl}"
                   f" → {best_u[2]}({ux:.2f},{uy:.2f})"
                   f"  d={math.hypot(sx-ux, sy-uy):.1f}mm")
        route_between(sx, sy, ux, uy, net_name, route_w, layer_id,
                      None, None, via_pad, via_drill, comp_keepout_mm=0.0)


# ── Main routing function ─────────────────────────────────────────────────────

def route_connection(ref_a, ref_b, net_name, width_mm, layer_id,
                     neck_w=None, neck_max=None,
                     via_allowed=False, via_pad=None, via_drill=None):
    global _conflict_count

    a = get_pad_info(ref_a, net_name)
    b = get_pad_info(ref_b, net_name)
    if a is None:
        log.append(f"    SKIP {net_name}: pad not found on {ref_a}")
        return None, None
    if b is None:
        log.append(f"    SKIP {net_name}: pad not found on {ref_b}")
        return None, None

    x1, y1, layer_a, pad_w_a, ls_a = a
    x2, y2, layer_b, pad_w_b, ls_b = b

    stub_w = neck_w or 0.4

    # Layer transition if pad not on the routing layer.
    if not pad_on_layer(ls_a, layer_id) and via_pad is not None:
        existing_a = find_pad_via(x1, y1, layer_a, net_name)
        if existing_a is not None:
            x1, y1 = existing_a
        else:
            x1, y1 = place_pad_via(ref_a, x1, y1, layer_a, net_name, layer_id,
                                    via_pad, via_drill, stub_w)
        pad_w_a = via_pad

    if not pad_on_layer(ls_b, layer_id) and via_pad is not None:
        existing_b = find_pad_via(x2, y2, layer_b, net_name)
        if existing_b is not None:
            x2, y2 = existing_b
        else:
            x2, y2 = place_pad_via(ref_b, x2, y2, layer_b, net_name, layer_id,
                                    via_pad, via_drill, stub_w)
        pad_w_b = via_pad

    # Neckdown at exit points
    use_neck_a = neck_w and neck_max and pad_w_a < width_mm
    use_neck_b = neck_w and neck_max and pad_w_b < width_mm
    if use_neck_a:
        ex1, ey1 = find_neckdown_exit(x1, y1, x2, y2, net_name,
                                       width_mm, neck_w, neck_max, layer_id)
    else:
        ex1, ey1 = x1, y1
    if use_neck_b:
        ex2, ey2 = find_neckdown_exit(x2, y2, x1, y1, net_name,
                                       width_mm, neck_w, neck_max, layer_id)
    else:
        ex2, ey2 = x2, y2

    obstacles = build_obstacles(layer_id, {net_name})

    waypoints = route_manhattan(ex1, ey1, ex2, ey2, width_mm, obstacles)
    used_layer = layer_id

    if waypoints is None:
        log.append(f"    [manhattan blocked on {board.GetLayerName(layer_id)}] → maze")
        waypoints = route_maze(ex1, ey1, ex2, ey2, width_mm, obstacles)

    if waypoints is None and via_allowed and via_pad is not None and ALT_LAYER != layer_id:
        alt_obs = build_obstacles(ALT_LAYER, {net_name})
        alt_wp  = route_manhattan(ex1, ey1, ex2, ey2, width_mm, alt_obs) or \
                  route_maze(ex1, ey1, ex2, ey2, width_mm, alt_obs)
        if alt_wp is not None:
            log.append(f"    [via-detour] → {board.GetLayerName(ALT_LAYER)}")
            if not via_exists_at(ex1, ey1, net_name):
                add_via(ex1, ey1, net_name, via_pad, via_drill)
            waypoints = alt_wp
            used_layer = ALT_LAYER
            if not via_exists_at(ex2, ey2, net_name):
                add_via(ex2, ey2, net_name, via_pad, via_drill)

    if waypoints is None:
        region = (min(ex1, ex2) - 2, min(ey1, ey2) - 2,
                  max(ex1, ex2) + 2, max(ey1, ey2) + 2)
        bl = identify_blockers(region, layer_id, {net_name})
        log.append(f"    CONFLICT: no path for {net_name} {ref_a}→{ref_b}"
                   f"  blockers: {', '.join(bl) or 'none'}")
        _conflict_count += 1
        return None, None

    # Neckdown stubs
    if use_neck_a and (abs(ex1 - x1) > 1e-6 or abs(ey1 - y1) > 1e-6):
        add_track(x1, y1, ex1, ey1, neck_w, layer_id, net_name)
    if use_neck_b and (abs(ex2 - x2) > 1e-6 or abs(ey2 - y2) > 1e-6):
        add_track(ex2, ey2, x2, y2, neck_w, layer_id, net_name)

    # Main body
    for i in range(len(waypoints) - 1):
        add_track(waypoints[i][0], waypoints[i][1],
                  waypoints[i+1][0], waypoints[i+1][1],
                  width_mm, used_layer, net_name)

    return a, b


# ── Main ─────────────────────────────────────────────────────────────────────

mode = "APPLY" if APPLY else "DRY RUN"
log.append(f"route_critical.py — {mode}")
log.append("=" * 60)

for loop in cfg.SWITCHING_LOOPS:
    layer_id = board.GetLayerID(loop["layer"])
    ic_ref   = loop["ic_ref"]
    ind_ref  = loop["inductor_ref"]
    sw_net   = loop["sw_net"]
    vin_net  = loop["vin_net"]
    out_net  = loop["out_net"]
    boot_net = loop["bootstrap_net"]
    boot_cap = loop["bootstrap_cap"]
    w_sw     = loop["sw_width_mm"]
    w_out    = loop["out_width_mm"]
    w_vin    = loop["vin_width_mm"]
    w_boot   = loop["bootstrap_width_mm"]
    neck_w   = loop.get("neckdown_width_mm")
    neck_max = loop.get("neckdown_max_mm")

    ca_audit  = cfg.CLEARANCE_AUDIT
    via_drill = ca_audit.get("via_drill_mm", 0.30)
    via_pad   = via_drill + 2 * ca_audit.get("via_annular_ring_mm", 0.15)

    via_sw   = loop.get("sw_via_allowed",   False)
    via_vin  = loop.get("vin_via_allowed",  False)
    via_out  = loop.get("out_via_allowed",  False)
    via_boot = loop.get("boot_via_allowed", False)
    boot_layer_id = board.GetLayerID(loop["bootstrap_layer"]) \
                    if "bootstrap_layer" in loop else layer_id

    log.append(f"\n[loop] {loop['name']}   layer={loop['layer']}"
               f"   alt={board.GetLayerName(ALT_LAYER)}")
    log.append(f"  widths: SW={w_sw}  VIN={w_vin}  OUT={w_out}  BOOT={w_boot}"
               f"  neckdown={neck_w}mm/{neck_max}mm")

    routed_nets = [n for n in (sw_net, out_net, vin_net, boot_net)
                   if net_already_routed(n, layer_id)]
    if routed_nets:
        log.append(f"  SKIP loop connections — already routed on {loop['layer']}: {', '.join(routed_nets)}")
    else:
        for cap in loop["input_caps"]:
            log.append(f"  [VIN]  {vin_net}: {ic_ref} → {cap}")
            route_connection(ic_ref, cap, vin_net, w_vin, layer_id, neck_w, neck_max,
                             via_allowed=via_vin, via_pad=via_pad, via_drill=via_drill)

        log.append(f"  [BOOT] {boot_net}: {ic_ref} → {boot_cap}")
        route_connection(ic_ref, boot_cap, boot_net, w_boot, boot_layer_id, neck_w, neck_max,
                         via_allowed=via_boot, via_pad=via_pad, via_drill=via_drill)

        log.append(f"  [SW]   {sw_net}: {ic_ref} → {ind_ref}")
        route_connection(ic_ref, ind_ref, sw_net, w_sw, layer_id, neck_w, neck_max,
                         via_allowed=via_sw, via_pad=via_pad, via_drill=via_drill)

        for cap in loop["output_caps"]:
            log.append(f"  [OUT]  {out_net}: {ind_ref} → {cap}")
            route_connection(ind_ref, cap, out_net, w_out, layer_id, neck_w, neck_max,
                             via_allowed=via_out, via_pad=via_pad, via_drill=via_drill)

        sw_pads = [(tomm(pad.GetPosition().x), tomm(pad.GetPosition().y))
                   for fp in board.GetFootprints()
                   for pad in fp.Pads()
                   if pad.GetNetname() == sw_net]
        if sw_pads:
            xs = [p[0] for p in sw_pads]; ys = [p[1] for p in sw_pads]
            area = (max(xs) - min(xs)) * (max(ys) - min(ys))
            status = "PASS" if area <= 50 else "FAIL"
            log.append(f"  [SW-AREA] {max(xs)-min(xs):.1f}×{max(ys)-min(ys):.1f}mm"
                       f"  ≈{area:.1f}mm²  [{status} ≤50mm²]")

    # Full-net routing pass: connect all remaining pads on each net using Prim's MST.
    # Fanout vias serve as anchors for tight-pitch pads (SOM, BGA) so full-width
    # power traces route between accessible via positions, not pad centers.
    log.append(f"\n  [full-net pass — {loop['name']}]")
    tap_refs = loop.get("full_net_taps", [])
    for net_name, w, lyr in [
        (vin_net,  w_vin,  layer_id),
        (sw_net,   w_sw,   layer_id),
        (out_net,  w_out,  layer_id),
        (boot_net, w_boot, boot_layer_id),
    ]:
        route_net_full(net_name, w, lyr, via_pad, via_drill, neck_w, neck_max,
                       tap_refs=tap_refs)

if APPLY:
    board.Save(cfg.PCB_FILE)
    log.append("\nBoard saved.")

out_text = "\n".join(log)
report_path = pathlib.Path(cfg.REPORTS_DIR) / "fix_log_phase10.txt"
report_path.parent.mkdir(parents=True, exist_ok=True)
try:
    existing = report_path.read_text(encoding="utf-8")
except FileNotFoundError:
    existing = ""
report_path.write_text(existing + "\n" + out_text + "\n", encoding="utf-8")
print(out_text)

sys.exit(1 if _conflict_count else 0)
