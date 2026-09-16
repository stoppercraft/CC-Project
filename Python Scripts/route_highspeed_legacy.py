"""
route_highspeed.py — Phase 10 Part B
Routes all high-speed differential pairs defined in routing_config.HS_PAIRS
before FreeRouting. Routing widths and gaps come from routing_config.HS_ROUTE_WIDTHS.

Routing method — centerline with perpendicular offsets:
  Each segment routes along the centerline between matched P/N pad pairs.
  P and N are offset symmetrically to opposite sides of the centerline by cc/2,
  where cc = width + gap. Short diagonal stubs connect offset points back to
  actual pad positions. P and N cannot cross because they are always on opposite
  sides of the centerline.

Via convention:
  Endpoint vias (pad layer → target_layer) at component pads only — never mid-trace.
  GND return via placed near every signal via; position found by cardinal search to
  avoid board obstacles. Return vias rely on zone fill for their GND connection if no
  connecting GND track can be emitted from an adjacent pad.
  Via sizes come from routing_config.CLEARANCE_AUDIT (hs_via_* keys).

Obstacle model:
  Each routed segment is checked against board pads/tracks and previously placed
  segments in this run (_run_obs). Overlaps within a diff pair (P vs N) are exempt
  since their intra-pair gap is set by impedance rules, not DRC clearance.
  CONFLICT count increments for any external-obstacle overlap; script exits non-zero.

IMPORTANT — re-running this script:
  board.Remove() causes SIGSEGV in standalone KiCad 10 Python. Before re-running,
  delete all HS net tracks manually in KiCad PCB Editor:
    click any HS track → right-click → Select → All Tracks in Net → Delete → repeat per net → Save

After running, run lock_highspeed_nets.py, then verify_hs_sandwich.py.

Usage:
    python route_highspeed.py          # dry run — no PCB changes
    python route_highspeed.py --apply  # apply tracks/vias and save
"""

import sys, os, math, pathlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import routing_config as cfg

KICAD_BIN = str(pathlib.Path(cfg.KICAD_SITE_PKGS).parent.parent)
os.environ["PATH"] = KICAD_BIN + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, cfg.KICAD_SITE_PKGS)
import pcbnew

APPLY        = "--apply" in sys.argv
CLEARANCE_MM = 0.15

board   = pcbnew.LoadBoard(cfg.PCB_FILE)
F_CU    = board.GetLayerID("F.Cu")
B_CU    = board.GetLayerID("B.Cu")
GND_NET = cfg.LAYER_SCHEME.get("gnd_ref", "GND")

ca        = cfg.CLEARANCE_AUDIT
VIA_PAD   = ca["hs_via_drill_mm"] + 2 * ca["hs_via_annular_ring_mm"]
VIA_DRILL = ca["hs_via_drill_mm"]

log             = []
_conflict_count = 0
# Virtual track/via table for dry-run obstacle visibility.
# Each entry: (x1, y1, x2, y2, half_width, layer_id, net_name)
# Point obstacles (vias): x1==x2, y1==y2.
_run_obs: list = []


# ── Geometry helpers ──────────────────────────────────────────────────────────

def mm(x):    return pcbnew.FromMM(x)
def tomm(x):  return pcbnew.ToMM(x)
def dist(ax, ay, bx, by): return math.sqrt((bx - ax) ** 2 + (by - ay) ** 2)

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

def seg_to_seg_dist(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2):
    """Minimum edge-to-edge distance between two line segments (or point obstacles)."""
    return min(
        point_to_seg_dist(ax1, ay1, bx1, by1, bx2, by2),
        point_to_seg_dist(ax2, ay2, bx1, by1, bx2, by2),
        point_to_seg_dist(bx1, by1, ax1, ay1, ax2, ay2),
        point_to_seg_dist(bx2, by2, ax1, ay1, ax2, ay2),
    )

def pad_obs_rect(pad):
    """Pad copper AABB expanded by CLEARANCE_MM."""
    pos = pad.GetPosition()
    px, py = tomm(pos.x), tomm(pos.y)
    size = pad.GetSize()
    sx, sy = tomm(size.x) / 2, tomm(size.y) / 2
    try:
        ang = math.radians(pad.GetOrientationDegrees())
    except AttributeError:
        ang = 0.0
    ca2, sa2 = abs(math.cos(ang)), abs(math.sin(ang))
    hx = sx * ca2 + sy * sa2 + CLEARANCE_MM
    hy = sx * sa2 + sy * ca2 + CLEARANCE_MM
    return (px - hx, py - hy, px + hx, py + hy)


# ── Obstacle model ────────────────────────────────────────────────────────────

def build_obstacles(layer_id, exclude_nets):
    """
    Return obstacle rectangles on layer_id, excluding any pad/track in exclude_nets.
    exclude_nets: set of net names to skip (e.g. both nets of the diff pair being routed).
    """
    obs = []
    c   = CLEARANCE_MM

    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname() in exclude_nets:
                continue
            if not pad.GetLayerSet().Contains(layer_id):
                continue
            obs.append(pad_obs_rect(pad))

    for track in board.GetTracks():
        if track.GetNetname() in exclude_nets:
            continue
        if track.GetClass() == "PCB_VIA":
            try:
                vr = tomm(track.GetWidth(F_CU)) / 2   # KiCad 10: layer arg required
            except TypeError:
                vr = tomm(track.GetWidth()) / 2
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
        if rnet in exclude_nets or rlayer != layer_id:
            continue
        obs.append((min(rx1, rx2) - rhw - c, min(ry1, ry2) - rhw - c,
                    max(rx1, rx2) + rhw + c, max(ry1, ry2) + rhw + c))

    return obs


# ── PCB write helpers ─────────────────────────────────────────────────────────

def add_track(x1, y1, x2, y2, width_mm, layer_id, net_name):
    """Accumulate in _run_obs; add to board only in APPLY mode. Returns length mm."""
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
        log.append(f"    WARNING: net {net_name!r} not found in board")
        return 0.0
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I(mm(x1), mm(y1)))
    track.SetEnd(pcbnew.VECTOR2I(mm(x2), mm(y2)))
    track.SetWidth(mm(width_mm))
    track.SetLayer(layer_id)
    track.SetNet(net)
    board.Add(track)
    return length


def add_via(x, y, net_name, via_pad_mm, via_drill_mm, layer_a, layer_b):
    """Accumulate in _run_obs on all through-hole layers; add to board only in APPLY mode.
    Through-hole vias always span F.Cu→B.Cu on the board, so _run_obs covers all relevant
    layers ({layer_a, layer_b, F_CU, B_CU}) for accurate dry-run obstacle visibility."""
    via_r = via_pad_mm / 2
    for layer in {layer_a, layer_b, F_CU, B_CU}:
        _run_obs.append((x, y, x, y, via_r, layer, net_name))
    log.append(f"    VIA {net_name} at ({x:.3f},{y:.3f})"
               f"  pad={via_pad_mm}mm  drill={via_drill_mm}mm")
    if not APPLY:
        return
    net = board.FindNet(net_name)
    if net is None:
        log.append(f"    WARNING: net {net_name!r} not found in board")
        return
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
    v.SetWidth(mm(via_pad_mm))
    v.SetDrill(mm(via_drill_mm))
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetNet(net)
    v.SetLayerPair(F_CU, B_CU)   # through-hole always spans full stackup
    board.Add(v)


# ── GND return via ────────────────────────────────────────────────────────────

def place_gnd_return_via(sig_x, sig_y, layer_id):
    """
    Find a clear cardinal position for a GND return via near (sig_x, sig_y).
    The via is isolated (no connecting track); GND connection comes from zone fill.
    Checks obstacles on both F.Cu and B.Cu — through-hole vias span the full stackup.
    """
    via_r  = VIA_PAD / 2.0
    c      = CLEARANCE_MM
    obs_f  = build_obstacles(F_CU, {GND_NET})
    obs_b  = build_obstacles(B_CU, {GND_NET})

    for step in (0.8, 1.0, 1.2, 1.5, 2.0):
        for dx, dy in ((1, 0), (-1, 0), (0, -1), (0, 1)):
            vx, vy  = sig_x + dx * step, sig_y + dy * step
            vr_box  = (vx - via_r - c, vy - via_r - c,
                       vx + via_r + c, vy + via_r + c)
            if (not any(rect_overlap(vr_box, o) for o in obs_f) and
                    not any(rect_overlap(vr_box, o) for o in obs_b)):
                add_via(vx, vy, GND_NET, VIA_PAD, VIA_DRILL, F_CU, B_CU)
                return True

    log.append(f"    WARNING: no clear GND return via near ({sig_x:.2f},{sig_y:.2f})")
    return False


# ── Pad lookup ────────────────────────────────────────────────────────────────

def get_pads_on_net(net_name):
    result = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetname() == net_name:
                pos = pad.GetPosition()
                result.append({
                    "ref":   fp.GetReference(),
                    "x":     tomm(pos.x),
                    "y":     tomm(pos.y),
                    "layer": pad.GetLayer(),
                })
    return result


# ── Pair matching and chaining ────────────────────────────────────────────────

def pair_pads(pads_p, pads_n):
    """Match each P pad with its closest N pad (greedy nearest-neighbor)."""
    remaining_n = list(pads_n)
    pairs = []
    for p in pads_p:
        if not remaining_n:
            log.append(f"  WARNING: more P pads than N — unmatched P at"
                       f" ({p['x']:.2f},{p['y']:.2f})")
            break
        closest = min(remaining_n, key=lambda n: dist(p["x"], p["y"], n["x"], n["y"]))
        pairs.append((p, closest))
        remaining_n.remove(closest)
    if remaining_n:
        log.append(f"  WARNING: {len(remaining_n)} unmatched N pad(s)")
    return pairs


def chain_pairs(pairs):
    """Order (P,N) pairs by nearest-neighbor on centroid, topmost first."""
    if len(pairs) <= 1:
        return list(pairs)

    def centroid(pair):
        p, n = pair
        return ((p["x"] + n["x"]) / 2, (p["y"] + n["y"]) / 2)

    remaining = list(pairs)
    chain = [min(remaining, key=lambda pr: centroid(pr)[1])]
    remaining.remove(chain[0])
    while remaining:
        cx, cy = centroid(chain[-1])
        nearest = min(remaining, key=lambda pr: dist(cx, cy, *centroid(pr)))
        chain.append(nearest)
        remaining.remove(nearest)
    return chain


# ── Via placement at pads ─────────────────────────────────────────────────────

def place_vias_if_needed(pad, net_name, target_layer_id):
    """Place signal via + GND return via if pad layer ≠ target_layer_id."""
    if pad["layer"] != target_layer_id:
        add_via(pad["x"], pad["y"], net_name, VIA_PAD, VIA_DRILL,
                pad["layer"], target_layer_id)
        place_gnd_return_via(pad["x"], pad["y"], target_layer_id)


# ── Centerline segment router ─────────────────────────────────────────────────

def route_segment_centerline(p_a, n_a, p_b, n_b, width_mm, cc, layer_id, net_p, net_n):
    """Route one diff-pair segment via centerline + perpendicular offsets.

    Geometry:
      centerline A = midpoint(p_a, n_a); centerline B = midpoint(p_b, n_b)
      unit direction u = (center_A → center_B)
      perpendicular p = u rotated 90° CCW
      P_sign = +1 if p_a is CCW of u, else -1
      P_offset = center ± P_sign * p * cc/2
      N_offset = center ∓ P_sign * p * cc/2
    Three segments per net: pad → offset_A → offset_B → pad.

    Returns (len_p, len_n) in mm.
    """
    cx1 = (p_a["x"] + n_a["x"]) / 2;  cy1 = (p_a["y"] + n_a["y"]) / 2
    cx2 = (p_b["x"] + n_b["x"]) / 2;  cy2 = (p_b["y"] + n_b["y"]) / 2
    seg_len = dist(cx1, cy1, cx2, cy2)
    if seg_len < 1e-4:
        return 0.0, 0.0

    ux, uy = (cx2 - cx1) / seg_len, (cy2 - cy1) / seg_len
    px, py = -uy, ux  # perpendicular CCW

    rel_x  = p_a["x"] - cx1;  rel_y = p_a["y"] - cy1
    p_sign = 1.0 if (ux * rel_y - uy * rel_x) >= 0 else -1.0
    half   = cc / 2.0

    op_ax = cx1 + p_sign * px * half;  op_ay = cy1 + p_sign * py * half
    on_ax = cx1 - p_sign * px * half;  on_ay = cy1 - p_sign * py * half
    op_bx = cx2 + p_sign * px * half;  op_by = cy2 + p_sign * py * half
    on_bx = cx2 - p_sign * px * half;  on_by = cy2 - p_sign * py * half

    len_p = (add_track(p_a["x"], p_a["y"], op_ax, op_ay, width_mm, layer_id, net_p) +
             add_track(op_ax, op_ay, op_bx, op_by, width_mm, layer_id, net_p) +
             add_track(op_bx, op_by, p_b["x"], p_b["y"], width_mm, layer_id, net_p))

    len_n = (add_track(n_a["x"], n_a["y"], on_ax, on_ay, width_mm, layer_id, net_n) +
             add_track(on_ax, on_ay, on_bx, on_by, width_mm, layer_id, net_n) +
             add_track(on_bx, on_by, n_b["x"], n_b["y"], width_mm, layer_id, net_n))

    return len_p, len_n


# ── Blocker identification ────────────────────────────────────────────────────

def identify_blockers(region, layer_id, exclude_nets):
    """Return human-readable labels for pads/tracks in region that are not in exclude_nets."""
    labels = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for pad in fp.Pads():
            if pad.GetNetname() in exclude_nets:
                continue
            if not pad.GetLayerSet().Contains(layer_id):
                continue
            if rect_overlap(pad_obs_rect(pad), region):
                labels.append(f"{ref}.{pad.GetNumber()}({pad.GetNetname()})")
    for rx1, ry1, rx2, ry2, rhw, rlayer, rnet in _run_obs:
        if rnet in exclude_nets or rlayer != layer_id:
            continue
        obs = (min(rx1,rx2)-rhw-CLEARANCE_MM, min(ry1,ry2)-rhw-CLEARANCE_MM,
               max(rx1,rx2)+rhw+CLEARANCE_MM, max(ry1,ry2)+rhw+CLEARANCE_MM)
        if rect_overlap(obs, region):
            labels.append(f"[routed:{rnet}]")
    return labels


# ── Post-route conflict check ─────────────────────────────────────────────────

def check_pair_conflicts(net_p, net_n, layer_id):
    """
    Check all _run_obs entries for net_p/net_n on layer_id against external obstacles.
    Intra-pair overlaps (P vs N) are exempt — their gap is set by impedance rules.
    Uses actual segment geometry for _run_obs to avoid AABB false positives from
    diagonal tracks. Board copper (pads, existing PCB tracks) uses AABB.
    """
    global _conflict_count
    pair_nets = {net_p, net_n}
    ext_run = [(rx1, ry1, rx2, ry2, rhw, rnet)
               for rx1, ry1, rx2, ry2, rhw, rlayer, rnet in _run_obs
               if rnet not in pair_nets and rlayer == layer_id]

    for rx1, ry1, rx2, ry2, rhw, rlayer, rnet in _run_obs:
        if rnet not in pair_nets or rlayer != layer_id:
            continue
        blockers = []
        for ox1, oy1, ox2, oy2, ohw, onet in ext_run:
            if seg_to_seg_dist(rx1, ry1, rx2, ry2, ox1, oy1, ox2, oy2) < rhw + ohw + CLEARANCE_MM:
                blockers.append(f"[routed:{onet}]")
        # Board copper check (AABB — board tracks are rarely diagonal)
        sr = (min(rx1, rx2) - rhw, min(ry1, ry2) - rhw,
              max(rx1, rx2) + rhw, max(ry1, ry2) + rhw)
        board_hits = [b for b in identify_blockers(sr, layer_id, pair_nets)
                      if not b.startswith("[routed:")]
        blockers.extend(board_hits)
        if blockers:
            seen: set = set()
            unique = [b for b in blockers if not (b in seen or seen.add(b))]
            log.append(f"    CONFLICT: {rnet} ({rx1:.2f},{ry1:.2f})→({rx2:.2f},{ry2:.2f})"
                       f"  {len(unique)} blocker(s): {', '.join(unique)}")
            _conflict_count += 1


# ── Pair router ───────────────────────────────────────────────────────────────

def route_pair(pair_name, net_p, net_n, layer_name, width_mm, gap_mm):
    layer_id = board.GetLayerID(layer_name)
    cc       = width_mm + gap_mm

    log.append(f"\n  [{pair_name}]  {net_p}/{net_n}  layer={layer_name}  "
               f"w={width_mm}mm  gap={gap_mm}mm")

    pads_p = get_pads_on_net(net_p)
    pads_n = get_pads_on_net(net_n)
    if not pads_p:
        log.append(f"  SKIP: no pads found for {net_p}");  return
    if not pads_n:
        log.append(f"  SKIP: no pads found for {net_n}");  return

    pairs     = chain_pairs(pair_pads(pads_p, pads_n))
    total_p   = total_n = 0.0
    seen_vias = set()

    def via_key(pad, net):
        return (round(pad["x"], 3), round(pad["y"], 3), net)

    for i, (p_a, n_a) in enumerate(pairs):
        for pad, net in ((p_a, net_p), (n_a, net_n)):
            k = via_key(pad, net)
            if k not in seen_vias:
                place_vias_if_needed(pad, net, layer_id)
                seen_vias.add(k)

        if i + 1 < len(pairs):
            p_b, n_b = pairs[i + 1]
            for pad, net in ((p_b, net_p), (n_b, net_n)):
                k = via_key(pad, net)
                if k not in seen_vias:
                    place_vias_if_needed(pad, net, layer_id)
                    seen_vias.add(k)

            lp, ln = route_segment_centerline(
                p_a, n_a, p_b, n_b, width_mm, cc, layer_id, net_p, net_n)
            total_p += lp;  total_n += ln

            cx1 = (p_a["x"] + n_a["x"]) / 2;  cy1 = (p_a["y"] + n_a["y"]) / 2
            cx2 = (p_b["x"] + n_b["x"]) / 2;  cy2 = (p_b["y"] + n_b["y"]) / 2
            log.append(f"    seg {i}: ({cx1:.2f},{cy1:.2f})→({cx2:.2f},{cy2:.2f})"
                       f"  Lp={lp:.3f}mm  Ln={ln:.3f}mm")

    check_pair_conflicts(net_p, net_n, layer_id)

    delta      = abs(total_p - total_n)
    skew_limit = cfg.HS_PAIRS[pair_name][3]
    status     = "PASS" if delta <= skew_limit else "REVIEW — add trombone meander"
    log.append(f"    P={total_p:.3f}mm  N={total_n:.3f}mm  Δ={delta:.3f}mm"
               f"  (limit {skew_limit}mm)  [{status}]")


# ── Main ──────────────────────────────────────────────────────────────────────

mode = "APPLY" if APPLY else "DRY RUN"
log.append(f"route_highspeed.py — {mode}")
log.append("=" * 70)
log.append(f"Pairs to route: {len(cfg.HS_PAIRS)}")
log.append(f"Via: pad={VIA_PAD}mm  drill={VIA_DRILL}mm  GND_NET={GND_NET!r}")
log.append("NOTE: run on a clean board — delete existing HS tracks before running.")

for pair_name, (net_p, net_n, layer_name, _skew) in cfg.HS_PAIRS.items():
    if pair_name not in cfg.HS_ROUTE_WIDTHS:
        log.append(f"\nSKIP {pair_name}: not in HS_ROUTE_WIDTHS")
        continue
    width_mm, gap_mm = cfg.HS_ROUTE_WIDTHS[pair_name]
    route_pair(pair_name, net_p, net_n, layer_name, width_mm, gap_mm)

if APPLY:
    board.Save(cfg.PCB_FILE)
    log.append("\nBoard saved.")

log.append("Next: run lock_highspeed_nets.py, then verify_hs_sandwich.py.")
if _conflict_count:
    log.append(f"\n{_conflict_count} CONFLICT(s) — review before applying.")

report_path = pathlib.Path(cfg.REPORTS_DIR) / "fix_log_phase10.txt"
report_path.parent.mkdir(parents=True, exist_ok=True)
existing = ""
try:
    existing = report_path.read_text(encoding="utf-8")
except FileNotFoundError:
    pass
report_path.write_text(existing + "\n" + "\n".join(log) + "\n", encoding="utf-8")
print("\n".join(log))

sys.exit(1 if _conflict_count else 0)
