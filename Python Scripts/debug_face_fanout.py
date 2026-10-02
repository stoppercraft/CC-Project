"""
debug_face_fanout.py
Runs face fanout on one or more component faces for visual inspection.
Mirrors the full script's decision logic exactly by using _run()'s preprocessed
pending list (after clustering and via-sharing suppression).

Edit FACES below to select which faces to process.
  (ref, escape_dx, escape_dy, pad_filter)
  pad_filter: set of pad number strings to include, or None for all pads on that face.

Board is cleared internally before writing.
"""
import sys, math
sys.path.insert(0, 'C:/Program Files/KiCad/10.0/bin/Lib/site-packages')
sys.path.insert(0, r'E:\Claude Projects\CC Project Folder\Python Scripts')

import pcbnew
import route_fanout_vias as rfv
import routing_config as cfg
from route_fanout_vias import PendingVia

BOARD      = cfg.PCB_FILE
ca         = cfg.CLEARANCE_AUDIT
HS_NET_SET = set(cfg.HS_NETS)

# ── Face definitions ──────────────────────────────────────────────────────────
FACES = [
    ('U3',    0.0, -1.0, {str(n) for n in range(1, 17)}),   # U3 north face pads 1-16
    ('J_DSI1', 0.0, -1.0, None),                             # J_DSI1 all pads
]

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

# ── Get preprocessed pending list from _run() ─────────────────────────────────
# This runs all of _run()'s pending construction including diff-pair alignment,
# clustering, and via-sharing suppression — same state as the full script.
pending, pad_obs, fp_by_ref, clearance, skip_nets = rfv._run(
    board, apply=False, _debug_return_pending=True
)
print(f"preprocessed pending: {len(pending)} pad(s)")

layer_fcu = board.GetLayerID("F.Cu")
layer_bcu = board.GetLayerID("B.Cu")

# ── Emit helpers ──────────────────────────────────────────────────────────────
n_vias = n_tracks = 0

def _find_net(name):
    return board.FindNet(name)

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

def _emit_assignments(assignments, face_pads, bus_stubs, edx, edy):
    global n_vias
    for pv, vx, vy in assignments:
        net = _find_net(pv.net_name)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(vx), pcbnew.FromMM(vy)))
        via.SetDrill(pcbnew.FromMM(pv.via_drill_mm))
        via.SetWidth(pcbnew.FromMM(pv.via_drill_mm + 2 * pv.via_annular_mm))
        via.SetLayerPair(layer_fcu, layer_bcu)
        if net: via.SetNet(net)
        board.Add(via)
        n_vias += 1
        segs = rfv._route_45deg_stub(pv.pad_x, pv.pad_y, vx, vy, edx, edy, axial_first=True)
        if not segs:
            segs = [(pv.pad_x, pv.pad_y, vx, vy)]
        for x1, y1, x2, y2 in segs:
            if math.hypot(x2-x1, y2-y1) < 1e-6: continue
            _add_track(x1, y1, x2, y2, pv.neckdown_w_mm, net)

    for pv in face_pads:
        if not hasattr(pv, 'stub_only_vx'): continue
        vx, vy = pv.stub_only_vx, pv.stub_only_vy
        net = _find_net(pv.net_name)
        segs = rfv._route_45deg_stub(pv.pad_x, pv.pad_y, vx, vy, edx, edy, axial_first=True)
        if not segs:
            segs = [(pv.pad_x, pv.pad_y, vx, vy)]
        for x1, y1, x2, y2 in segs:
            if math.hypot(x2-x1, y2-y1) < 1e-6: continue
            _add_track(x1, y1, x2, y2, pv.neckdown_w_mm, net)
        if hasattr(pv, 'stub_ext_vx'):
            ex2, ey2 = pv.stub_ext_vx, pv.stub_ext_vy
            if math.hypot(ex2-vx, ey2-vy) >= 1e-6:
                _add_track(vx, vy, ex2, ey2, pv.neckdown_w_mm, net)

    for x1, y1, x2, y2, nw, bnet in bus_stubs:
        if math.hypot(x2-x1, y2-y1) < 1e-6: continue
        _add_track(x1, y1, x2, y2, nw, _find_net(bnet))

def _emit_one_stagger(pv, edx, edy):
    global n_vias
    if pv.face_fanout_assigned: return
    if not pv.net_name or pv.net_name.startswith('unconnected-'): return
    net = _find_net(pv.net_name)
    vx, vy = pv.via_x, pv.via_y
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(vx), pcbnew.FromMM(vy)))
    via.SetDrill(pcbnew.FromMM(pv.via_drill_mm))
    via.SetWidth(pcbnew.FromMM(pv.via_drill_mm + 2 * pv.via_annular_mm))
    via.SetLayerPair(layer_fcu, layer_bcu)
    if net: via.SetNet(net)
    board.Add(via)
    n_vias += 1
    if pv.cluster_real_pads:
        for px, py, nw in pv.cluster_real_pads:
            segs = rfv._route_45deg_stub(px, py, vx, vy, edx, edy, axial_first=False)
            if not segs:
                segs = [(px, py, vx, vy)]
            for x1, y1, x2, y2 in segs:
                if math.hypot(x2-x1, y2-y1) < 1e-6: continue
                _add_track(x1, y1, x2, y2, nw, net)
    else:
        segs = rfv._route_45deg_stub(pv.pad_x, pv.pad_y, vx, vy, edx, edy, axial_first=True)
        if not segs:
            segs = [(pv.pad_x, pv.pad_y, vx, vy)]
        for x1, y1, x2, y2 in segs:
            if math.hypot(x2-x1, y2-y1) < 1e-6: continue
            _add_track(x1, y1, x2, y2, pv.neckdown_w_mm, net)

# Stagger queue: filled during face loop, tightened then emitted after.
_stagger_queue: list = []  # [(face_pads, edx, edy), ...]

# ── Process each face ─────────────────────────────────────────────────────────
pending_set = {(pv.ref, pv.pad_num) for pv in pending}

for ref, edx, edy, pad_filter in FACES:
    fp = fp_by_ref.get(ref)
    assert fp, f"{ref} not found on board"
    ldx, ldy = -edy, edx

    # Pull only this face's pads from the preprocessed pending list
    def _matches(pv):
        if pv.ref != ref: return False
        if abs(pv.escape_dx - edx) > 0.01 or abs(pv.escape_dy - edy) > 0.01: return False
        if pad_filter is not None and pv.pad_num not in pad_filter: return False
        return True

    face_pads = [pv for pv in pending if _matches(pv)]
    print(f"\n{ref} face ({edx:.0f},{edy:.0f}): {len(face_pads)} pad(s) after preprocessing")

    if not face_pads:
        print("  (no pads)")
        continue

    if rfv._is_radial_fanout_fp(fp) and rfv._face_needs_coopt(face_pads, clearance):
        # Radial-fanout component with tight whole face — augment with skip-net pads
        skip_pvs = rfv._build_skip_net_pvs(fp, face_pads, edx, edy, skip_nets)
        face_grp_aug = face_pads + skip_pvs
        pending_set.update((pv.ref, pv.pad_num) for pv in skip_pvs)
        fc = sum(v.pad_x * ldx + v.pad_y * ldy for v in face_grp_aug) / len(face_grp_aug)
        face_grp_aug.sort(key=lambda v: -abs(v.pad_x * ldx + v.pad_y * ldy - fc))
        assignments = rfv._face_fanout(
            face_grp_aug, pad_obs, pending_set,
            clearance, face_grp_aug[0].via_annular_mm, edx, edy,
        )
        bus_stubs = getattr(rfv._face_fanout, '_last_bus_stubs',   [])
        keepouts  = getattr(rfv._face_fanout, '_last_keepout_set', set())
        print(f"  [whole-face fanout] {len(face_pads)} signal + {len(skip_pvs)} skip-net → "
              f"{len(assignments)} placed, {len(keepouts)} keepout(s), "
              f"{len(bus_stubs)} bus-stub segment(s)")
        _emit_assignments(assignments, face_grp_aug, bus_stubs, edx, edy)

    else:
        subgroups = rfv._find_tight_subgroups(face_pads, clearance)
        if subgroups:
            sg_pad_ids = set()
            for sg in subgroups:
                fc = sum(v.pad_x * ldx + v.pad_y * ldy for v in sg) / len(sg)
                sg.sort(key=lambda v: -abs(v.pad_x * ldx + v.pad_y * ldy - fc))
                assignments = rfv._face_fanout(
                    sg, pad_obs, pending_set,
                    clearance, sg[0].via_annular_mm, edx, edy,
                )
                bus_stubs = getattr(rfv._face_fanout, '_last_bus_stubs',   [])
                keepouts  = getattr(rfv._face_fanout, '_last_keepout_set', set())
                print(f"  [sub-group fanout] {len(sg)} pads → "
                      f"{len(assignments)} placed, {len(keepouts)} keepout(s), "
                      f"{len(bus_stubs)} bus-stub segment(s)")
                _emit_assignments(assignments, sg, bus_stubs, edx, edy)
                for pv, _, _ in assignments:
                    pv.face_fanout_assigned = True
                    sg_pad_ids.add(id(pv))
                for pv in sg:
                    if pv.implicit_keepout:
                        pv.face_fanout_assigned = True
                        sg_pad_ids.add(id(pv))

            remainder = [pv for pv in face_pads if id(pv) not in sg_pad_ids]
            if remainder:
                print(f"  [standard stagger] {len(remainder)} pad(s)")
                rfv._stagger_vias(remainder, ca, clearance)
                _stagger_queue.append((remainder, edx, edy))
        else:
            print(f"  [standard stagger] {len(face_pads)} pad(s)")
            rfv._stagger_vias(face_pads, ca, clearance)
            _stagger_queue.append((face_pads, edx, edy))

# ── Tighten stagger vias then emit ────────────────────────────────────────────
_all_stagger = [pv for grp, _, _ in _stagger_queue
                for pv in grp
                if not pv.face_fanout_assigned
                and pv.net_name and not pv.net_name.startswith('unconnected-')]
if _all_stagger:
    n_tight = rfv._tighten_vias(_all_stagger, pad_obs, [], clearance)
    if n_tight:
        print(f"\n[tighten] {n_tight} stagger via(s) compacted")
for grp, edx, edy in _stagger_queue:
    for pv in grp:
        _emit_one_stagger(pv, edx, edy)

board.Save(board.GetFileName())
print(f"\nWritten: {n_vias} vias, {n_tracks} tracks")
print(f"Saved → {board.GetFileName()}")
print("Reload in KiCad to inspect.")
