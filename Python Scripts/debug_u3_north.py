"""
debug_u3_north.py
Test harness for _face_fanout: runs route_fanout_vias._face_fanout on U3's
north face pads only, then writes vias and stubs to the board for visual
inspection in KiCad.

Does NOT implement any routing algorithm — all logic lives in route_fanout_vias.py.
Board must be cleared externally before running.
"""
import sys, math
sys.path.insert(0, 'C:/Program Files/KiCad/10.0/bin/Lib/site-packages')
sys.path.insert(0, r'E:\Claude Projects\CC Project Folder\Python Scripts')

import pcbnew
import route_fanout_vias as rfv
import routing_config as cfg
from route_fanout_vias import PendingVia

IU = 1_000_000

# ── Config ────────────────────────────────────────────────────────────────────
BOARD      = cfg.PCB_FILE
ca         = cfg.CLEARANCE_AUDIT
CLEARANCE  = ca["via_clearance_mm"]
HS_NET_SET = set(cfg.HS_NETS)


# ── Load board ────────────────────────────────────────────────────────────────
# Board must be pre-cleared before running this script.
# board.GetFootprints() segfaults after board.Remove() in the same process
# due to KiCad Python SWIG object lifecycle issues.  Run the clear as a
# separate subprocess so this process loads a clean board.
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

_all_fps = list(board.GetFootprints())
u3_fp = next((fp for fp in _all_fps if fp.GetReference() == 'U3'), None)
assert u3_fp, "U3 not found on board"

layer_fcu = board.GetLayerID("F.Cu")
layer_bcu = board.GetLayerID("B.Cu")

# ── North face: escape direction (0, -1) ──────────────────────────────────────
edx, edy = 0.0, -1.0
ldx, ldy = -edy, edx   # lateral = (1, 0)

_TEST_PADS = {'1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12', '13', '14', '15', '16'}
north_pads = [p for p in u3_fp.Pads()
              if abs(rfv._radial_escape_direction(u3_fp, p)[0] - edx) < 0.01
              and abs(rfv._radial_escape_direction(u3_fp, p)[1] - edy) < 0.01
              and p.GetNumber() in _TEST_PADS]

print(f"U3 north-face pads: {len(north_pads)}")

# ── Pad obstacles (all board pads, matching production _pad_obs_early) ────────
pad_obs = []
for fp in _all_fps:
    fp_ref = fp.GetReference()
    for p in fp.Pads():
        pad_obs.append(rfv._pad_obstacle(p, CLEARANCE, fp_ref))

# ── Build PendingVia objects ──────────────────────────────────────────────────
face_pads = []
for pad in north_pads:
    net      = pad.GetNetname()
    priority = rfv.PRIORITY_HS if (net in HS_NET_SET) else rfv.PRIORITY_OTHER
    drill, ann         = rfv.via_params(priority)
    floor, nl, mx      = rfv.neckdown_params(priority, net)
    sz  = pad.GetSize()
    bb  = pad.GetBoundingBox()
    pw  = sz.x / IU
    ph  = sz.y / IU
    nw  = rfv.neckdown_stub_width(floor, pw, ph, net, priority)
    face_pads.append(PendingVia(
        ref             = 'U3',
        pad_num         = pad.GetNumber(),
        net_name        = net,
        pad_x           = pad.GetX() / IU,
        pad_y           = pad.GetY() / IU,
        pad_layer_id    = pad.GetLayer(),
        target_layer_id = layer_bcu,
        escape_dx       = edx,
        escape_dy       = edy,
        priority        = priority,
        via_drill_mm    = drill,
        via_annular_mm  = ann,
        neckdown_w_mm   = nw,
        neckdown_len_mm = nl,
        max_search_mm   = mx,
        pad_w_mm        = pw,
        pad_h_mm        = ph,
        pad_bbox        = (bb.GetLeft()  / IU, bb.GetTop()    / IU,
                           bb.GetRight() / IU, bb.GetBottom() / IU),
    ))

# Sort outside-in
face_center = sum(v.pad_x * ldx + v.pad_y * ldy for v in face_pads) / len(face_pads)
face_pads.sort(key=lambda v: -abs(v.pad_x * ldx + v.pad_y * ldy - face_center))

# ── Build pending_set ─────────────────────────────────────────────────────────
# All pads with nets are via-bearing — matches production _co_pending_set which
# includes every PendingVia in the pending list without sandwich exclusions.
pending_set = {('U3', pv.pad_num) for pv in face_pads
               if pv.net_name and not pv.net_name.startswith('unconnected-')}

print(f"pending_set: {len(pending_set)} via-bearing")

# ── Call _face_fanout ─────────────────────────────────────────────────────────
assignments = rfv._face_fanout(
    face_pads, pad_obs, pending_set,
    CLEARANCE, face_pads[0].via_annular_mm, edx, edy,
)

bus_stubs  = getattr(rfv._face_fanout, '_last_bus_stubs',   [])
keepouts   = getattr(rfv._face_fanout, '_last_keepout_set', set())

print(f"\n_face_fanout: {len(assignments)} vias placed, "
      f"{len(keepouts)} keepout(s), {len(bus_stubs)} bus-stub segment(s)\n")

# ── Emit to board ─────────────────────────────────────────────────────────────
n_vias = n_tracks = 0

def _find_net(name):
    return board.FindNet(name)

def _add_track(x1, y1, x2, y2, nw, net):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
    t.SetEnd  (pcbnew.VECTOR2I(pcbnew.FromMM(x2), pcbnew.FromMM(y2)))
    t.SetWidth(pcbnew.FromMM(nw))
    t.SetLayer(layer_fcu)
    if net:
        t.SetNet(net)
    board.Add(t)

# Via-bearing pads
for pv, vx, vy in assignments:
    net = _find_net(pv.net_name)
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(vx), pcbnew.FromMM(vy)))
    via.SetDrill(pcbnew.FromMM(pv.via_drill_mm))
    via.SetWidth(pcbnew.FromMM(pv.via_drill_mm + 2 * pv.via_annular_mm))
    via.SetLayerPair(layer_fcu, layer_bcu)
    if net:
        via.SetNet(net)
    board.Add(via)
    n_vias += 1

    segs = rfv._route_45deg_stub(pv.pad_x, pv.pad_y, vx, vy, edx, edy, axial_first=True)
    if not segs:
        segs = [(pv.pad_x, pv.pad_y, vx, vy)]
    for x1, y1, x2, y2 in segs:
        if math.hypot(x2 - x1, y2 - y1) < 1e-6:
            continue
        _add_track(x1, y1, x2, y2, pv.neckdown_w_mm, net)
        n_tracks += 1

# Stub-only pads
for pv in face_pads:
    if not hasattr(pv, 'stub_only_vx'):
        continue
    vx, vy = pv.stub_only_vx, pv.stub_only_vy
    net    = _find_net(pv.net_name)
    segs   = rfv._route_45deg_stub(pv.pad_x, pv.pad_y, vx, vy, edx, edy, axial_first=True)
    if not segs:
        segs = [(pv.pad_x, pv.pad_y, vx, vy)]
    for x1, y1, x2, y2 in segs:
        if math.hypot(x2 - x1, y2 - y1) < 1e-6:
            continue
        _add_track(x1, y1, x2, y2, pv.neckdown_w_mm, net)
        n_tracks += 1

# Bus stubs
for x1, y1, x2, y2, nw, bnet in bus_stubs:
    if math.hypot(x2 - x1, y2 - y1) < 1e-6:
        continue
    _add_track(x1, y1, x2, y2, nw, _find_net(bnet))
    n_tracks += 1

board.Save(board.GetFileName())
print(f"Written: {n_vias} vias, {n_tracks} tracks")
print(f"Saved → {board.GetFileName()}")
print("Reload in KiCad to inspect.")
