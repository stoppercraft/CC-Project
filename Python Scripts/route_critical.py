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

APPLY        = "--apply" in sys.argv
CLEARANCE_MM = 0.15
MAZE_RES_MM  = 0.1
MAZE_MARGIN  = 8.0
BEND_COST    = 4

board     = pcbnew.LoadBoard(cfg.PCB_FILE)
F_CU      = board.GetLayerID("F.Cu")
B_CU      = board.GetLayerID("B.Cu")
ALT_LAYER = board.GetLayerID(cfg.LAYER_SCHEME.get("power_alt", "F.Cu"))
log       = []
_conflict_count = 0

# Virtual copper table: (x1, y1, x2, y2, half_width, layer_id, net_name)
# Point obstacles (vias): x1==x2, y1==y2, half_width==via_radius.
_run_obs: list = []


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

def build_obstacles(layer_id, net_excl_pads, net_excl_tracks=None):
    """
    Return forbidden rectangles on layer_id (clearance-expanded).

    net_excl_pads   : set of nets whose pads are NOT obstacles (endpoints).
    net_excl_tracks : set of nets whose board tracks/vias/_run_obs entries
                      are NOT obstacles.  Defaults to net_excl_pads.
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
                  via_pad, via_drill, stub_w):
    """
    Place a short stub on pad_layer from (px,py) to a nearby clear point, and
    a through via at that point.  Returns via (vx, vy) as the new routing anchor.
    """
    via_r = via_pad / 2.0
    c     = CLEARANCE_MM
    obs_pad   = build_obstacles(pad_layer,      {net_name})
    obs_route = build_obstacles(route_layer_id, {net_name})

    for dist in (0.5, 0.7, 1.0, 1.2, 1.5, 2.0, 2.5):
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0),
                        (-1, -1), (-1, 1), (1, -1), (1, 1)):
            norm = math.hypot(dx, dy)
            vx = px + dx / norm * dist
            vy = py + dy / norm * dist
            via_rect  = (vx - via_r - c, vy - via_r - c,
                         vx + via_r + c, vy + via_r + c)
            stub_rect = seg_rect(px, py, vx, vy, stub_w + 2 * c)
            if (not any(rect_overlap(via_rect,  o) for o in obs_pad)   and
                    not any(rect_overlap(via_rect,  o) for o in obs_route) and
                    not any(rect_overlap(stub_rect, o) for o in obs_pad)):
                add_track(px, py, vx, vy, stub_w, pad_layer, net_name)
                add_via(vx, vy, net_name, via_pad, via_drill)
                log.append(f"    pad-via {ref}: stub→({vx:.3f},{vy:.3f})")
                return vx, vy

    log.append(f"    WARNING: no offset via near {ref} — via at pad center")
    add_via(px, py, net_name, via_pad, via_drill)
    return px, py


# ── Neckdown: shorten wide trace to fit pad exit ─────────────────────────────

def find_neckdown_exit(px, py, dest_x, dest_y, net_name,
                        full_w, neck_w, neck_max, layer_id):
    hw = full_w / 2.0
    dx_raw = dest_x - px;  dy_raw = dest_y - py
    if abs(dx_raw) >= abs(dy_raw):
        ux, uy = (1.0 if dx_raw >= 0 else -1.0), 0.0
    else:
        ux, uy = 0.0, (1.0 if dy_raw >= 0 else -1.0)

    ic_obs = build_obstacles(layer_id, {net_name})

    t = 0.0
    while t <= neck_max + 1e-9:
        sx, sy = px + ux * t, py + uy * t
        pt = (sx - hw, sy - hw, sx + hw, sy + hw)
        if not any(rect_overlap(pt, o) for o in ic_obs):
            return sx, sy
        t += MAZE_RES_MM
    return px + ux * neck_max, py + uy * neck_max


# ── Duplicate track guard ─────────────────────────────────────────────────────

def net_already_routed(net_name, layer_id):
    for track in board.GetTracks():
        if track.GetClass() == "PCB_VIA":
            continue
        if track.GetNetname() == net_name and track.GetLayer() == layer_id:
            return True
    return False


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
        x1, y1 = place_pad_via(ref_a, x1, y1, layer_a, net_name, layer_id,
                                via_pad, via_drill, stub_w)
        pad_w_a = via_pad

    if not pad_on_layer(ls_b, layer_id) and via_pad is not None:
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
            add_via(ex1, ey1, net_name, via_pad, via_drill)
            waypoints = alt_wp
            used_layer = ALT_LAYER
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
        log.append(f"  SKIP loop — already routed on {loop['layer']}: {', '.join(routed_nets)}")
        continue

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
