"""
debug_fanout.py
Single debug harness for route_fanout_vias.py. Runs the full pipeline on
the real board, then emits only the target component(s) so via positions
and stub geometry can be inspected in KiCad without running --apply.

Configure TARGET_REFS below, then run this script directly.
Via positions are guaranteed identical to a full --apply run.
"""
import sys, math
sys.path.insert(0, 'C:/Program Files/KiCad/10.0/bin/Lib/site-packages')
sys.path.insert(0, r'E:\Claude Projects\CC Project Folder\Python Scripts')

import pcbnew
import route_fanout_vias as rfv
import routing_config as cfg

BOARD = cfg.PCB_FILE

# ── Configure target(s) ───────────────────────────────────────────────────────
# List one or more footprint references to inspect.
# All faces and pad types are emitted automatically.
TARGET_REFS = ['SOM1']

# ── Clear board ───────────────────────────────────────────────────────────────
import subprocess as _sp
_sp.run([sys.executable, "-c",
    "import sys; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages');"
    "import pcbnew;"
    f"board=pcbnew.LoadBoard(r'{BOARD}');"
    "[board.Remove(t) for t in list(board.GetTracks()) if not t.IsLocked()];"
    "board.Save(board.GetFileName())"],
    check=False)
print("cleared")

board = pcbnew.LoadBoard(BOARD)

# ── Run full pipeline, return computed positions ──────────────────────────────
result = rfv._run(board, apply=False, _debug_return_computed=True)
pending, pad_obs, fp_by_ref, clearance, skip_nets, bus_stubs_by_ref, all_skip_pvs, keepout_data = result
print(f"full pipeline done: {len(pending)} pad(s) in pending")

layer_fcu = board.GetLayerID("F.Cu")
layer_bcu = board.GetLayerID("B.Cu")

n_vias = n_tracks = 0

def _add_track(x1, y1, x2, y2, nw, net):
    global n_tracks
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
    t.SetEnd  (pcbnew.VECTOR2I(pcbnew.FromMM(x2), pcbnew.FromMM(y2)))
    t.SetWidth(pcbnew.FromMM(nw))
    t.SetLayer(layer_fcu)
    if net: t.SetNet(net)
    board.Add(t)
    n_tracks += 1

def _emit_via(pv, vx, vy):
    global n_vias
    net = board.FindNet(pv.net_name)
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(vx), pcbnew.FromMM(vy)))
    via.SetDrill(pcbnew.FromMM(pv.via_drill_mm))
    via.SetWidth(pcbnew.FromMM(pv.via_drill_mm + 2 * pv.via_annular_mm))
    via.SetLayerPair(layer_fcu, layer_bcu)
    if net: via.SetNet(net)
    board.Add(via)
    n_vias += 1
    return net

# ── Emit each target ──────────────────────────────────────────────────────────
for ref in TARGET_REFS:
    target_pads = [pv for pv in pending if pv.ref == ref]
    print(f"\n{ref}: {len(target_pads)} pad(s) in pending")

    emitted = skipped = 0

    # Regular pads (each has its own via)
    for pv in target_pads:
        if pv.implicit_keepout:
            skipped += 1
            continue
        if not pv.net_name or pv.net_name.startswith('unconnected-'):
            skipped += 1
            continue
        net = _emit_via(pv, pv.via_x, pv.via_y)
        emitted += 1

        if pv.cluster_real_pads:
            # Bus topology: axial stub from each pad to via depth, lateral bus connects all.
            edx, edy = pv.escape_dx, pv.escape_dy
            ldx, ldy = -edy, edx
            via_along = pv.via_x * edx + pv.via_y * edy
            lat_coords = ([px * ldx + py * ldy for px, py, _ in pv.cluster_real_pads]
                          + [pv.via_x * ldx + pv.via_y * ldy])
            lat_min, lat_max = min(lat_coords), max(lat_coords)
            bx1 = lat_min * ldx + via_along * edx
            by1 = lat_min * ldy + via_along * edy
            bx2 = lat_max * ldx + via_along * edx
            by2 = lat_max * ldy + via_along * edy
            if math.hypot(bx2 - bx1, by2 - by1) >= 1e-6:
                _add_track(bx1, by1, bx2, by2, pv.neckdown_w_mm, net)
            for px, py, pad_nw in pv.cluster_real_pads:
                pad_lat = px * ldx + py * ldy
                sx2 = pad_lat * ldx + via_along * edx
                sy2 = pad_lat * ldy + via_along * edy
                if math.hypot(sx2 - px, sy2 - py) >= 1e-6:
                    _add_track(px, py, sx2, sy2, pad_nw, net)
        else:
            segs = rfv._route_45deg_stub(pv.pad_x, pv.pad_y, pv.via_x, pv.via_y,
                                         pv.escape_dx, pv.escape_dy, axial_first=True)
            if not segs:
                segs = [(pv.pad_x, pv.pad_y, pv.via_x, pv.via_y)]
            for x1, y1, x2, y2 in segs:
                if math.hypot(x2-x1, y2-y1) < 1e-6: continue
                _add_track(x1, y1, x2, y2, pv.neckdown_w_mm, net)

    print(f"  regular pads: {emitted} emitted, {skipped} skipped")

    # Face-fanout-assigned stub-only pads (in pending, no via placed)
    stub_only = [pv for pv in target_pads
                 if pv.face_fanout_assigned
                 and hasattr(pv, 'stub_only_vx') and hasattr(pv, 'stub_only_vy')]
    for pv in stub_only:
        net = board.FindNet(pv.net_name)
        segs = rfv._route_45deg_stub(pv.pad_x, pv.pad_y, pv.stub_only_vx, pv.stub_only_vy,
                                     pv.escape_dx, pv.escape_dy, axial_first=True)
        if not segs:
            segs = [(pv.pad_x, pv.pad_y, pv.stub_only_vx, pv.stub_only_vy)]
        for x1, y1, x2, y2 in segs:
            if math.hypot(x2-x1, y2-y1) < 1e-6: continue
            _add_track(x1, y1, x2, y2, pv.neckdown_w_mm, net)
        if hasattr(pv, 'stub_ext_vx'):
            ex2, ey2 = pv.stub_ext_vx, pv.stub_ext_vy
            if math.hypot(ex2 - pv.stub_only_vx, ey2 - pv.stub_only_vy) >= 1e-6:
                _add_track(pv.stub_only_vx, pv.stub_only_vy, ex2, ey2, pv.neckdown_w_mm, net)

    # Skip-net pvs (e.g. GND pads added as face fanout boundary anchors)
    target_skip = [pv for pv in all_skip_pvs
                   if pv.ref == ref and hasattr(pv, 'stub_only_vx')]
    for pv in target_skip:
        net = board.FindNet(pv.net_name)
        segs = rfv._route_45deg_stub(pv.pad_x, pv.pad_y, pv.stub_only_vx, pv.stub_only_vy,
                                     pv.escape_dx, pv.escape_dy, axial_first=True)
        if not segs:
            segs = [(pv.pad_x, pv.pad_y, pv.stub_only_vx, pv.stub_only_vy)]
        for x1, y1, x2, y2 in segs:
            if math.hypot(x2-x1, y2-y1) < 1e-6: continue
            _add_track(x1, y1, x2, y2, pv.neckdown_w_mm, net)
        if hasattr(pv, 'stub_ext_vx'):
            ex2, ey2 = pv.stub_ext_vx, pv.stub_ext_vy
            if math.hypot(ex2 - pv.stub_only_vx, ey2 - pv.stub_only_vy) >= 1e-6:
                _add_track(pv.stub_only_vx, pv.stub_only_vy, ex2, ey2, pv.neckdown_w_mm, net)

    # Bus stubs (GND/skip-net track segments from face fanout for this ref)
    ref_bus = bus_stubs_by_ref.get(ref, [])
    for x1, y1, x2, y2, nw, bnet in ref_bus:
        if math.hypot(x2-x1, y2-y1) < 1e-6: continue
        _add_track(x1, y1, x2, y2, nw, board.FindNet(bnet))
    if ref_bus:
        print(f"  bus stubs: {len(ref_bus)} segment(s)")

board.Save(board.GetFileName())
print(f"\nWritten: {n_vias} vias, {n_tracks} tracks")
print(f"Saved → {board.GetFileName()}")
print("Reload in KiCad to inspect.")
