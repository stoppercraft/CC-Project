"""
insert_meanders_tight.py — Intra-pair skew correction via serpentine meanders
                           (tight clearance variant: binary-search safe amplitude
                            with full geometric DRC simulation on every segment)

Iterates over all differential pair groups, finds the shorter net, and inserts
single-sided serpentine meanders to match the longer net's routed length within
the intra-pair skew tolerance.

Usage:
    python insert_meanders_tight.py

Fill in the PROJECT CONFIG block before running.
"""

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Differential pair groups: group_name -> [net_P, net_N]
# The shorter net in each pair will receive meanders.
GROUPS = {
    "PAIR_0": ["/NET0P", "/NET0N"],
    "PAIR_1": ["/NET1P", "/NET1N"],
    # add more pairs as needed
}

# For each net, its companion (used to meander away from the other wire)
# Auto-generated from GROUPS below — override manually if needed.
COMPANIONS = {}  # filled automatically from GROUPS

# Trace geometry
TRACE_WIDTH   = 0.127   # mm — nominal diff-pair trace width
TOLERANCE     = 0.010   # mm — acceptable intra-pair skew (stop when delta <= this)
MIN_CLEARANCE = 0.200   # mm — board DRC clearance rule
A_GLOBAL_MIN  = 1.5 * TRACE_WIDTH            # minimum meander amplitude
A_GLOBAL_MAX  = max(3.0 * TRACE_WIDTH, 0.5)  # maximum meander amplitude
MIN_PITCH     = 3.0 * TRACE_WIDTH            # minimum meander pitch
# ─────────────────────────────────────────────────────────────────────────────

import sys
import math
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

# Build COMPANIONS map automatically
for _grp, (_pa, _pb) in GROUPS.items():
    COMPANIONS[_pa] = _pb
    COMPANIONS[_pb] = _pa


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def net_routed_length(board, net_name):
    total = 0.0
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            continue
        if t.GetNetname() != net_name:
            continue
        s, e = t.GetStart(), t.GetEnd()
        dx = pcbnew.ToMM(e.x - s.x)
        dy = pcbnew.ToMM(e.y - s.y)
        total += math.sqrt(dx * dx + dy * dy)
    return total


def get_segments_sorted(board, net_name):
    segs = []
    for i, t in enumerate(board.GetTracks()):
        if t.GetClass() == "PCB_VIA":
            continue
        if t.GetNetname() != net_name:
            continue
        s, e = t.GetStart(), t.GetEnd()
        dx = pcbnew.ToMM(e.x - s.x)
        dy = pcbnew.ToMM(e.y - s.y)
        L = math.sqrt(dx * dx + dy * dy)
        segs.append((L, i, t))
    segs.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [(L, t) for L, i, t in segs]


def seg_to_seg_min_dist(ax, ay, bx, by, cx, cy, dx2, dy):
    """Exact minimum centre-to-centre distance between two line segments."""
    d1x, d1y = bx - ax, by - ay
    d2x, d2y = dx2 - cx, dy - cy
    rx, ry   = ax - cx, ay - cy

    a = d1x * d1x + d1y * d1y
    e = d2x * d2x + d2y * d2y
    f = d2x * rx + d2y * ry

    if a < 1e-12 and e < 1e-12:
        return math.sqrt(rx * rx + ry * ry)

    if a < 1e-12:
        s = 0.0
        t = max(0.0, min(1.0, f / e))
    else:
        c_val = d1x * rx + d1y * ry
        if e < 1e-12:
            t = 0.0
            s = max(0.0, min(1.0, -c_val / a))
        else:
            b_val = d1x * d2x + d1y * d2y
            denom = a * e - b_val * b_val
            if abs(denom) > 1e-12:
                s = max(0.0, min(1.0, (b_val * f - c_val * e) / denom))
            else:
                s = 0.0
            t = (b_val * s + f) / e
            if t < 0.0:
                t = 0.0
                s = max(0.0, min(1.0, -c_val / a))
            elif t > 1.0:
                t = 1.0
                s = max(0.0, min(1.0, (b_val - c_val) / a))

    p1x = ax + s * d1x
    p1y = ay + s * d1y
    p2x = cx + t * d2x
    p2y = cy + t * d2y
    return math.sqrt((p1x - p2x) ** 2 + (p1y - p2y) ** 2)


def build_meander_pts(sx, sy, ux, uy, px, py, seg_len, N, A):
    """Return list of (x, y) waypoints for N-period single-sided meander."""
    pitch = seg_len / (N + 1)
    pts = [(sx, sy)]
    for i in range(N):
        c0x = sx + ux * (i * pitch + 0.25 * pitch)
        c0y = sy + uy * (i * pitch + 0.25 * pitch)
        m0x = sx + ux * (i * pitch + 0.50 * pitch) + px * A
        m0y = sy + uy * (i * pitch + 0.50 * pitch) + py * A
        c1x = sx + ux * (i * pitch + 0.75 * pitch)
        c1y = sy + uy * (i * pitch + 0.75 * pitch)
        pts.extend([(c0x, c0y), (m0x, m0y), (c1x, c1y)])
    pts.append((sx + ux * seg_len, sy + uy * seg_len))
    return pts


def check_meander_clearance(nearby_segs, sx, sy, ux, uy, px, py, seg_len, N, A, own_hw=None):
    """Return minimum edge-to-edge clearance between the meander path and nearby segments."""
    pts = build_meander_pts(sx, sy, ux, uy, px, py, seg_len, N, A)
    min_d = float('inf')
    if own_hw is None:
        own_hw = TRACE_WIDTH / 2.0
    for i in range(len(pts) - 1):
        ax, ay = pts[i]
        bx, by = pts[i + 1]
        for (cx, cy, dx2, dy, other_hw) in nearby_segs:
            d = seg_to_seg_min_dist(ax, ay, bx, by, cx, cy, dx2, dy)
            d -= (own_hw + other_hw)  # edge-to-edge
            if d < min_d:
                min_d = d
    return min_d


def safe_amplitude(board, net_name, track, px_unit, py_unit, default_A):
    """
    Binary-search for the largest meander amplitude that keeps edge-to-edge clearance
    >= MIN_CLEARANCE against all non-self copper on the same layer (including pads and vias).
    """
    start = track.GetStart()
    end   = track.GetEnd()
    sx, sy = pcbnew.ToMM(start.x), pcbnew.ToMM(start.y)
    ex, ey = pcbnew.ToMM(end.x),   pcbnew.ToMM(end.y)
    seg_dx, seg_dy = ex - sx, ey - sy
    seg_len = math.sqrt(seg_dx ** 2 + seg_dy ** 2)
    if seg_len < 1e-6:
        return default_A

    ux, uy = seg_dx / seg_len, seg_dy / seg_len
    layer  = track.GetLayer()
    margin = default_A + 1.5
    own_hw = pcbnew.ToMM(track.GetWidth()) / 2.0

    bxmin = min(sx, ex) - margin
    bxmax = max(sx, ex) + margin
    bymin = min(sy, ey) - margin
    bymax = max(sy, ey) + margin

    VIA_PAD_RADIUS = 0.35  # conservative via pad radius in mm

    nearby = []

    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            ts = t.GetStart()
            tsx, tsy = pcbnew.ToMM(ts.x), pcbnew.ToMM(ts.y)
            if tsx < bxmin or tsx > bxmax or tsy < bymin or tsy > bymax:
                continue
            if t.GetNetname() == net_name:
                continue
            nearby.append((tsx, tsy, tsx, tsy, VIA_PAD_RADIUS))
            continue

        if t.GetLayer() != layer:
            continue
        if t.GetNetname() == net_name:
            continue
        ts, te = t.GetStart(), t.GetEnd()
        tsx, tsy = pcbnew.ToMM(ts.x), pcbnew.ToMM(ts.y)
        tex, tey = pcbnew.ToMM(te.x), pcbnew.ToMM(te.y)
        if (max(tsx, tex) < bxmin or min(tsx, tex) > bxmax or
                max(tsy, tey) < bymin or min(tsy, tey) > bymax):
            continue
        t_hw = pcbnew.ToMM(t.GetWidth()) / 2.0
        nearby.append((tsx, tsy, tex, tey, t_hw))

    for fp in board.GetFootprints():
        for pad in fp.Pads():
            pc = pad.GetCenter()
            pcx, pcy = pcbnew.ToMM(pc.x), pcbnew.ToMM(pc.y)
            if pcx < bxmin or pcx > bxmax or pcy < bymin or pcy > bymax:
                continue
            if pad.GetNetname() == net_name:
                continue
            pad_sz = pad.GetSize()
            pad_r = max(pcbnew.ToMM(pad_sz.x), pcbnew.ToMM(pad_sz.y)) / 2.0
            nearby.append((pcx, pcy, pcx, pcy, pad_r))

    if not nearby:
        return default_A

    SAFE_MARGIN = 0.010
    TARGET_CLEARANCE = MIN_CLEARANCE + SAFE_MARGIN

    def clearance_at_multi_N(A):
        mn = float('inf')
        max_N = max(1, int(seg_len / MIN_PITCH) - 1)
        candidates = [2, 3, 4, 5, 7, 10, 15, 20, max_N]
        test_Ns = sorted(set(min(n, max_N) for n in candidates if n >= 1))
        for N_t in test_Ns:
            c = check_meander_clearance(nearby, sx, sy, ux, uy, px_unit, py_unit, seg_len, N_t, A, own_hw)
            if c < mn:
                mn = c
        return mn

    if clearance_at_multi_N(0.0) < MIN_CLEARANCE - 0.01:
        return 0.0

    if clearance_at_multi_N(default_A) >= TARGET_CLEARANCE:
        return default_A

    lo, hi = 0.0, default_A
    for _ in range(25):
        mid = (lo + hi) / 2.0
        if clearance_at_multi_N(mid) >= TARGET_CLEARANCE:
            lo = mid
        else:
            hi = mid

    return lo


def companion_direction(board, net_name, track):
    """Return +1 or -1 for the perpendicular direction that points away from the companion trace."""
    companion = COMPANIONS.get(net_name)
    if not companion:
        return 1

    start = track.GetStart()
    end   = track.GetEnd()
    sx, sy = pcbnew.ToMM(start.x), pcbnew.ToMM(start.y)
    ex, ey = pcbnew.ToMM(end.x),   pcbnew.ToMM(end.y)
    seg_dx, seg_dy = ex - sx, ey - sy
    seg_len = math.sqrt(seg_dx ** 2 + seg_dy ** 2)
    if seg_len < 1e-6:
        return 1

    ux, uy = seg_dx / seg_len, seg_dy / seg_len
    px, py = -uy, ux
    mx, my = (sx + ex) / 2.0, (sy + ey) / 2.0
    layer_name = board.GetLayerName(track.GetLayer())

    comp_x_sum, comp_y_sum, comp_count = 0.0, 0.0, 0
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA" or t.GetNetname() != companion:
            continue
        if board.GetLayerName(t.GetLayer()) != layer_name:
            continue
        ts, te = t.GetStart(), t.GetEnd()
        tcx = (pcbnew.ToMM(ts.x) + pcbnew.ToMM(te.x)) / 2.0
        tcy = (pcbnew.ToMM(ts.y) + pcbnew.ToMM(te.y)) / 2.0
        if math.sqrt((tcx - mx) ** 2 + (tcy - my) ** 2) > 30.0:
            continue
        comp_x_sum += tcx
        comp_y_sum += tcy
        comp_count += 1

    if comp_count == 0:
        for t in board.GetTracks():
            if t.GetClass() == "PCB_VIA" or t.GetNetname() != companion:
                continue
            ts, te = t.GetStart(), t.GetEnd()
            tcx = (pcbnew.ToMM(ts.x) + pcbnew.ToMM(te.x)) / 2.0
            tcy = (pcbnew.ToMM(ts.y) + pcbnew.ToMM(te.y)) / 2.0
            if math.sqrt((tcx - mx) ** 2 + (tcy - my) ** 2) <= 30.0:
                comp_x_sum += tcx
                comp_y_sum += tcy
                comp_count += 1

    if comp_count == 0:
        return 1

    dvx = comp_x_sum / comp_count - mx
    dvy = comp_y_sum / comp_count - my
    proj = dvx * px + dvy * py
    if abs(proj) < 1e-6:
        return 1
    return -1 if proj > 0 else +1


def extra_length(seg, N, A):
    """Extra routed length added by N-period single-sided meander on a segment of length seg."""
    pitch = seg / (N + 1)
    return N * (2.0 * math.sqrt((0.25 * pitch) ** 2 + A ** 2) - 0.5 * pitch)


def solve_exact_NA(seg, deficit, A_max):
    """
    Find N and A so extra_length(seg, N, A) ≈ deficit.
    Returns (N, A, actual_extra) or (None, None, None).
    """
    for N in range(1, 2000):
        pitch = seg / (N + 1)
        if pitch < MIN_PITCH:
            break
        A_upper = min(A_max, pitch / 2.0 - 1e-6)
        if A_upper < A_GLOBAL_MIN:
            break
        rhs_per = deficit / N + 0.5 * pitch
        inner = (rhs_per / 2.0) ** 2 - (0.25 * pitch) ** 2
        if inner <= 0:
            continue
        A = math.sqrt(inner)
        if A_GLOBAL_MIN <= A <= A_upper:
            return N, A, extra_length(seg, N, A)

    for N in range(1, 2000):
        pitch = seg / (N + 1)
        if pitch < MIN_PITCH:
            break
        A_upper = min(A_max, pitch / 2.0 - 1e-6)
        if A_upper < A_GLOBAL_MIN:
            break
        cap = extra_length(seg, N, A_upper)
        if cap >= deficit:
            rhs_per = deficit / N + 0.5 * pitch
            inner = (rhs_per / 2.0) ** 2 - (0.25 * pitch) ** 2
            if inner > 0:
                A = math.sqrt(inner)
                if A_GLOBAL_MIN <= A <= A_upper:
                    return N, A, extra_length(seg, N, A)
            return N, A_upper, cap

    return None, None, None


def max_capacity(seg, A_max):
    best_N, best_A, best_e = 0, 0.0, 0.0
    for N in range(1, 2000):
        pitch = seg / (N + 1)
        if pitch < MIN_PITCH:
            break
        A_cap = min(A_max, pitch / 2.0 - 1e-6)
        if A_cap < A_GLOBAL_MIN:
            break
        e = extra_length(seg, N, A_cap)
        if e > best_e:
            best_e = e
            best_N = N
            best_A = A_cap
    return best_N, best_A, best_e


def insert_meander(board, track, N, A, direction_sign):
    """Replace track with N-period single-sided serpentine. Returns extra length added."""
    start = track.GetStart()
    end   = track.GetEnd()
    sx, sy = pcbnew.ToMM(start.x), pcbnew.ToMM(start.y)
    ex, ey = pcbnew.ToMM(end.x),   pcbnew.ToMM(end.y)
    seg_dx, seg_dy = ex - sx, ey - sy
    seg_len = math.sqrt(seg_dx ** 2 + seg_dy ** 2)
    if seg_len < 0.1:
        return 0.0

    ux, uy = seg_dx / seg_len, seg_dy / seg_len
    px = -uy * direction_sign
    py =  ux * direction_sign

    pts = build_meander_pts(sx, sy, ux, uy, px, py, seg_len, N, A)

    deduped = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - deduped[-1][0]) > 1e-4 or abs(p[1] - deduped[-1][1]) > 1e-4:
            deduped.append(p)

    layer     = track.GetLayer()
    net       = track.GetNet()
    width_iu  = track.GetWidth()
    board.Remove(track)

    for i in range(len(deduped) - 1):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(deduped[i][0]),
                                   pcbnew.FromMM(deduped[i][1])))
        t.SetEnd  (pcbnew.VECTOR2I(pcbnew.FromMM(deduped[i + 1][0]),
                                   pcbnew.FromMM(deduped[i + 1][1])))
        t.SetWidth(width_iu)
        t.SetLayer(layer)
        t.SetNet(net)
        board.Add(t)

    pitch = seg_len / (N + 1)
    return N * (2.0 * math.sqrt((0.25 * pitch) ** 2 + A ** 2) - 0.5 * pitch)


def meander_net(board, net_name, deficit_mm):
    """Insert meanders on net_name to cover deficit_mm. Returns (total_added, plan_log)."""
    if deficit_mm <= 0.005:
        return 0.0, []

    segs   = get_segments_sorted(board, net_name)
    usable = [(L, t) for (L, t) in segs if L >= 0.5]
    if not usable:
        return 0.0, []

    plan_log  = []
    remaining = deficit_mm

    for (seg_len, track) in usable:
        if remaining <= TOLERANCE / 2.0:
            break

        dir_sign = companion_direction(board, net_name, track)

        start = track.GetStart()
        end   = track.GetEnd()
        sx, sy = pcbnew.ToMM(start.x), pcbnew.ToMM(start.y)
        ex, ey = pcbnew.ToMM(end.x),   pcbnew.ToMM(end.y)
        sl = math.sqrt((ex - sx) ** 2 + (ey - sy) ** 2)
        if sl < 1e-6:
            continue
        ux, uy = (ex - sx) / sl, (ey - sy) / sl
        px_eff = -uy * dir_sign
        py_eff =  ux * dir_sign

        A_max = safe_amplitude(board, net_name, track, px_eff, py_eff, A_GLOBAL_MAX)

        if A_max < A_GLOBAL_MIN:
            print(f"    skip  seg={seg_len:.3f}mm dir={dir_sign:+d}: safe A={A_max:.4f} < min={A_GLOBAL_MIN:.4f}")
            dir_sign2 = -dir_sign
            px_eff2 = -uy * dir_sign2
            py_eff2 =  ux * dir_sign2
            A_max2 = safe_amplitude(board, net_name, track, px_eff2, py_eff2, A_GLOBAL_MAX)
            if A_max2 >= A_GLOBAL_MIN:
                print(f"    retry other dir={dir_sign2:+d}: safe A={A_max2:.4f}")
                dir_sign  = dir_sign2
                px_eff, py_eff = px_eff2, py_eff2
                A_max = A_max2
            else:
                print(f"    both dirs blocked — skip segment")
                continue

        N, A, actual = solve_exact_NA(seg_len, remaining, A_max)
        if N is not None:
            added = insert_meander(board, track, N, A, dir_sign)
            plan_log.append((seg_len, N, A, dir_sign, added))
            remaining -= added
            break
        else:
            N_cap, A_cap, e_cap = max_capacity(seg_len, A_max)
            if N_cap > 0 and e_cap > 0.005:
                added = insert_meander(board, track, N_cap, A_cap, dir_sign)
                plan_log.append((seg_len, N_cap, A_cap, dir_sign, added))
                remaining -= added
                segs   = get_segments_sorted(board, net_name)
                usable = [(L, t) for (L, t) in segs if L >= 0.5]

    return sum(x[4] for x in plan_log), plan_log


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
print("Loading board …")
board = pcbnew.LoadBoard(PCB_FILE)
print(f"A_GLOBAL_MAX = {A_GLOBAL_MAX:.4f} mm  A_GLOBAL_MIN = {A_GLOBAL_MIN:.4f} mm")
print(f"MIN_PITCH    = {MIN_PITCH:.4f} mm  MIN_CLEARANCE = {MIN_CLEARANCE:.3f} mm")
print()

results = []

for group_name, (net_a, net_b) in sorted(GROUPS.items()):
    len_a = net_routed_length(board, net_a)
    len_b = net_routed_length(board, net_b)

    if len_a == 0 and len_b == 0:
        results.append({"group": group_name, "status": "SKIP"})
        print(f"SKIP    {group_name}")
        continue

    if len_a == 0 or len_b == 0:
        results.append({"group": group_name, "status": "PARTIAL",
                        "reason": f"{net_a}={len_a:.3f}  {net_b}={len_b:.3f}"})
        print(f"PARTIAL {group_name}")
        continue

    skew = abs(len_a - len_b)
    if skew <= TOLERANCE:
        results.append({"group": group_name, "status": "PASS", "skew_before": skew, "skew_after": skew})
        print(f"PASS    {group_name}: skew={skew:.3f} mm")
        continue

    shorter_net = net_a if len_a < len_b else net_b
    longer_len  = len_b if len_a < len_b else len_a
    deficit     = longer_len - net_routed_length(board, shorter_net)

    print(f"\nMEANDER {group_name}: shorter={shorter_net}  deficit={deficit:.3f} mm")

    added_total, plan_log = meander_net(board, shorter_net, deficit)
    for (seg_len, N, A, dsign, extra) in plan_log:
        print(f"  seg={seg_len:.3f}mm N={N} A={A:.4f}mm dir={dsign:+d} added={extra:.4f}mm")

    new_shorter = net_routed_length(board, shorter_net)
    new_skew    = abs(longer_len - new_shorter)
    verdict     = "PASS" if new_skew <= TOLERANCE else "CLOSE"

    results.append({
        "group": group_name, "status": "MEANDER",
        "shorter_net": shorter_net, "deficit_mm": deficit,
        "added_mm": added_total, "skew_before": skew,
        "skew_after": new_skew, "n_segs": len(plan_log), "verdict": verdict,
    })
    print(f"  => new_skew={new_skew:.4f} mm  {verdict}")

board.Save(PCB_FILE)
print("\nBoard saved.")

print("\n" + "=" * 72)
print("MEANDER INSERTION SUMMARY")
print("=" * 72)
for r in results:
    g, s = r["group"], r["status"]
    if s in ("SKIP", "PARTIAL"):
        print(f"  {s:<8} {g}")
    elif s == "PASS":
        print(f"  PASS     {g}: skew={r['skew_before']:.3f} mm")
    else:
        print(
            f"  MEANDER  {g}: deficit={r['deficit_mm']:.3f}  "
            f"added={r['added_mm']:.4f}  "
            f"before={r['skew_before']:.3f}  after={r['skew_after']:.4f}  "
            f"{r['verdict']}  [{r['n_segs']} seg(s)]"
        )

n_mp = sum(1 for r in results if r["status"] == "MEANDER" and r["verdict"] == "PASS")
n_mc = sum(1 for r in results if r["status"] == "MEANDER" and r["verdict"] == "CLOSE")
n_mt = sum(1 for r in results if r["status"] == "MEANDER")
print(f"\nMEANDER groups PASS  : {n_mp} / {n_mt}")
print(f"MEANDER groups CLOSE : {n_mc} / {n_mt}")
print("\nDone.")
