"""
debug_som1.py
Runs the full route_fanout_vias.py decision pipeline (all preprocessing, face dispatch,
stagger, tighten) then emits ONLY SOM1 pads from the fully-computed positions.

Uses _debug_return_computed=True which returns after all via position computation
but before the board emission (section 6). Positions are therefore identical to
running --apply on the full board.
"""
import sys, math
sys.path.insert(0, 'C:/Program Files/KiCad/10.0/bin/Lib/site-packages')
sys.path.insert(0, r'E:\Claude Projects\CC Project Folder\Python Scripts')

import pcbnew
import route_fanout_vias as rfv
import routing_config as cfg

BOARD = cfg.PCB_FILE

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

# ── Run full pipeline, get computed positions ─────────────────────────────────
result = rfv._run(board, apply=False, _debug_return_computed=True)
pending, pad_obs, fp_by_ref, clearance, skip_nets, bus_stubs_by_ref, all_skip_pvs, keepout_data = result
print(f"full pipeline done: {len(pending)} pad(s) in pending")

layer_fcu = board.GetLayerID("F.Cu")
layer_bcu = board.GetLayerID("B.Cu")

# ── Emit helpers ──────────────────────────────────────────────────────────────
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

# ── Emit SOM1 regular pads ────────────────────────────────────────────────────
som1_pads = [pv for pv in pending if pv.ref == 'SOM1']
print(f"SOM1 pads in pending: {len(som1_pads)}")

emitted = skipped = 0
for pv in som1_pads:
    if pv.implicit_keepout:
        skipped += 1
        continue
    if not pv.net_name or pv.net_name.startswith('unconnected-'):
        skipped += 1
        continue
    net = _emit_via(pv, pv.via_x, pv.via_y)
    emitted += 1

    if pv.cluster_real_pads:
        # Herringbone: individual pads route to shared via
        for px, py, nw in pv.cluster_real_pads:
            segs = rfv._route_45deg_stub(px, py, pv.via_x, pv.via_y,
                                         pv.escape_dx, pv.escape_dy, axial_first=False)
            if not segs:
                segs = [(px, py, pv.via_x, pv.via_y)]
            for x1, y1, x2, y2 in segs:
                if math.hypot(x2-x1, y2-y1) < 1e-6: continue
                _add_track(x1, y1, x2, y2, nw, net)
    else:
        segs = rfv._route_45deg_stub(pv.pad_x, pv.pad_y, pv.via_x, pv.via_y,
                                     pv.escape_dx, pv.escape_dy, axial_first=True)
        if not segs:
            segs = [(pv.pad_x, pv.pad_y, pv.via_x, pv.via_y)]
        for x1, y1, x2, y2 in segs:
            if math.hypot(x2-x1, y2-y1) < 1e-6: continue
            _add_track(x1, y1, x2, y2, pv.neckdown_w_mm, net)

print(f"SOM1 regular pads: {emitted} emitted, {skipped} skipped")

# ── Emit SOM1 face_fanout_assigned stub-only pads (from pending) ──────────────
som1_stub_only = [pv for pv in som1_pads
                  if pv.face_fanout_assigned
                  and hasattr(pv, 'stub_only_vx') and hasattr(pv, 'stub_only_vy')]
for pv in som1_stub_only:
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

# ── Emit SOM1 skip-net pvs (GND bus stubs, co-opt helper stubs) ───────────────
som1_skip = [pv for pv in all_skip_pvs if pv.ref == 'SOM1']
for pv in som1_skip:
    if not hasattr(pv, 'stub_only_vx'): continue
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
if som1_skip:
    print(f"SOM1 skip-net stubs: {len(som1_skip)} emitted")

# ── Emit SOM1 bus stubs (GND/skip-net track segments from face fanout) ─────────
som1_bus = bus_stubs_by_ref.get('SOM1', [])
for x1, y1, x2, y2, nw, bnet in som1_bus:
    if math.hypot(x2-x1, y2-y1) < 1e-6: continue
    _add_track(x1, y1, x2, y2, nw, board.FindNet(bnet))
if som1_bus:
    print(f"SOM1 bus stubs: {len(som1_bus)} segment(s) emitted")

board.Save(board.GetFileName())
print(f"\nWritten: {n_vias} vias, {n_tracks} tracks")
print(f"Saved → {board.GetFileName()}")
print("Reload in KiCad to inspect.")
