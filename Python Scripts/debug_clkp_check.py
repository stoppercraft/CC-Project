import sys, math
sys.path.insert(0, 'C:/Program Files/KiCad/10.0/bin/Lib/site-packages')
sys.path.insert(0, r'E:\Claude Projects\CC Project Folder\Python Scripts')
import pcbnew, route_fanout_vias as rfv, routing_config as cfg
from route_fanout_vias import PendingVia, _route_45deg_stub, _dist_to_segment, _seg_to_seg_dist, _dist_point_to_bbox, _seg_bbox_dist

IU = 1_000_000
BOARD = cfg.PCB_FILE
ca = cfg.CLEARANCE_AUDIT
CLEARANCE = ca["via_clearance_mm"]
HS_NET_SET = set(cfg.HS_NETS)

board = pcbnew.LoadBoard(BOARD)
_all_fps = list(board.GetFootprints())
u3_fp = next((fp for fp in _all_fps if fp.GetReference() == 'U3'), None)
layer_fcu = board.GetLayerID("F.Cu")
layer_bcu = board.GetLayerID("B.Cu")
edx, edy = 0.0, -1.0
ldx, ldy = -edy, edx
_TEST_PADS = {'1','2','3','4','5','6','7','8','9','10','11','12','13','14','15'}
north_pads = [p for p in u3_fp.Pads()
              if abs(rfv._radial_escape_direction(u3_fp, p)[0] - edx) < 0.01
              and abs(rfv._radial_escape_direction(u3_fp, p)[1] - edy) < 0.01
              and p.GetNumber() in _TEST_PADS]
pad_obs = []
for fp in _all_fps:
    for p in fp.Pads():
        pad_obs.append(rfv._pad_obstacle(p, CLEARANCE, fp.GetReference()))
face_pads = []
for pad in north_pads:
    net = pad.GetNetname()
    priority = rfv.PRIORITY_HS if (net in HS_NET_SET) else rfv.PRIORITY_OTHER
    drill, ann = rfv.via_params(priority)
    floor, nl, mx = rfv.neckdown_params(priority, net)
    sz = pad.GetSize(); bb = pad.GetBoundingBox()
    pw = sz.x / IU; ph = sz.y / IU
    nw = rfv.neckdown_stub_width(floor, pw, ph, net, priority)
    face_pads.append(PendingVia(
        ref='U3', pad_num=pad.GetNumber(), net_name=net,
        pad_x=pad.GetX()/IU, pad_y=pad.GetY()/IU,
        pad_layer_id=pad.GetLayer(), target_layer_id=layer_bcu,
        escape_dx=edx, escape_dy=edy,
        priority=priority, via_drill_mm=drill, via_annular_mm=ann,
        neckdown_w_mm=nw, neckdown_len_mm=nl, max_search_mm=mx,
        pad_w_mm=pw, pad_h_mm=ph,
        pad_bbox=(bb.GetLeft()/IU, bb.GetTop()/IU, bb.GetRight()/IU, bb.GetBottom()/IU),
    ))
face_center = sum(v.pad_x * ldx + v.pad_y * ldy for v in face_pads) / len(face_pads)
face_pads.sort(key=lambda v: -abs(v.pad_x * ldx + v.pad_y * ldy - face_center))
pending_set = {('U3', pv.pad_num) for pv in face_pads if pv.net_name}
assignments = rfv._face_fanout(face_pads, pad_obs, pending_set, CLEARANCE, face_pads[0].via_annular_mm, edx, edy)

clk_n_pv, clk_n_vx, clk_n_vy = next((pv,vx,vy) for pv,vx,vy in assignments if pv.pad_num=='3')
clk_p_pv, clk_p_vx, clk_p_vy = next((pv,vx,vy) for pv,vx,vy in assignments if pv.pad_num=='4')
print(f"CLK_N pad:   ({clk_n_pv.pad_x:.3f}, {clk_n_pv.pad_y:.3f}), bbox={clk_n_pv.pad_bbox}")
print(f"CLK_N via:   ({clk_n_vx:.3f}, {clk_n_vy:.3f})")
print(f"CLK_P pad:   ({clk_p_pv.pad_x:.3f}, {clk_p_pv.pad_y:.3f})")
print(f"CLK_P via:   ({clk_p_vx:.3f}, {clk_p_vy:.3f})")
print(f"CLK_P bbox:  {clk_p_pv.pad_bbox}")
print(f"neckdown_len_mm: {clk_p_pv.neckdown_len_mm}")
# Find CLK_N pad obstacle
clk_n_obs = next((o for o in pad_obs if o.ref=='U3' and o.net_name==clk_n_pv.net_name), None)
print(f"CLK_N obs:   cx={clk_n_obs.cx:.3f}, cy={clk_n_obs.cy:.3f}, bbox={clk_n_obs.bbox}, r={clk_n_obs.r:.4f}")

other_placed_test = []
for pv, vx, vy in assignments:
    if pv.pad_num == '4':
        continue
    pcrj = pv.via_drill_mm / 2.0 + CLEARANCE
    segsj = _route_45deg_stub(pv.pad_x, pv.pad_y, vx, vy, edx, edy, axial_first=True)
    if not segsj:
        segsj = [(pv.pad_x, pv.pad_y, vx, vy)]
    other_placed_test.append({'vx': vx, 'vy': vy, 'r': pcrj,
        'stub_hw': pv.neckdown_w_mm / 2.0, 'segs': segsj,
        'pad_i': 0, 'net': pv.net_name, 'pad_num': pv.pad_num})
print(f"\nother_placed: {[pc['pad_num'] for pc in other_placed_test]}")

v = clk_p_pv
via_copper_r = v.via_drill_mm / 2.0 + CLEARANCE
stub_hw = v.neckdown_w_mm / 2.0
chk_r = via_copper_r + CLEARANCE
print(f"via_copper_r={via_copper_r}, stub_hw={stub_hw}, chk_r={chk_r}")

def check_pos(lat_test, depth_test, ls=-1):
    # CLK_P has ls=-1 (left of face center), ldx=1: via goes left (x decreases)
    vx = v.pad_x + ls * lat_test * ldx
    vy = v.pad_y + ls * lat_test * ldy + edy * depth_test
    cost = math.hypot(lat_test, depth_test)
    fail = None
    for obs in pad_obs:
        if obs.ref == v.ref and obs.net_name == v.net_name:
            continue
        if v.net_name and obs.net_name == v.net_name:
            continue
        if obs.bbox is not None:
            d = _dist_point_to_bbox(vx, vy, obs.bbox)
            thr = chk_r
        else:
            d = math.hypot(vx - obs.cx, vy - obs.cy)
            thr = via_copper_r + obs.r
        if d < thr:
            fail = f"1-via-vs-pad:{obs.ref}/{obs.net_name}(d={d:.4f}<{thr:.4f})"
            break
    if not fail:
        for j, pc in enumerate(other_placed_test):
            d = math.hypot(vx - pc['vx'], vy - pc['vy'])
            thr = via_copper_r + pc['r'] + CLEARANCE
            if d < thr:
                fail = f"2-via-vs-via[{pc['pad_num']}](d={d:.4f}<{thr:.4f})"
                break
    if not fail:
        for j, pc in enumerate(other_placed_test):
            if pc['net'] == v.net_name:
                continue
            for x1s, y1s, x2s, y2s in pc['segs']:
                d = _dist_to_segment(vx, vy, x1s, y1s, x2s, y2s)
                thr = via_copper_r + pc['stub_hw'] + CLEARANCE
                if d < thr:
                    fail = f"3-via-vs-stub[{pc['pad_num']}](d={d:.4f}<{thr:.4f})"
                    break
            if fail:
                break
    if not fail:
        segs = _route_45deg_stub(v.pad_x, v.pad_y, vx, vy, edx, edy, axial_first=True)
        if not segs:
            segs = [(v.pad_x, v.pad_y, vx, vy)]
        for x1, y1, x2, y2 in segs:
            if math.hypot(x2-x1, y2-y1) < 1e-6:
                continue
            for obs in pad_obs:
                if obs.ref == v.ref and abs(obs.cx-v.pad_x) < 0.05 and abs(obs.cy-v.pad_y) < 0.05:
                    continue
                if v.net_name and obs.net_name == v.net_name:
                    continue
                if obs.bbox is not None:
                    d = _seg_bbox_dist(x1, y1, x2, y2, obs.bbox)
                    thr = stub_hw + CLEARANCE
                else:
                    d = _dist_to_segment(obs.cx, obs.cy, x1, y1, x2, y2)
                    thr = stub_hw + obs.r
                if d < thr:
                    fail = f"5a-stub-vs-pad:{obs.ref}/{obs.net_name}(d={d:.4f}<{thr:.4f})"
                    break
            if fail:
                break
            for j, pc in enumerate(other_placed_test):
                d = _dist_to_segment(pc['vx'], pc['vy'], x1, y1, x2, y2)
                thr = stub_hw + pc['r'] + CLEARANCE
                if d < thr:
                    fail = f"5b-stub-vs-via[{pc['pad_num']}](d={d:.4f}<{thr:.4f})"
                    break
            if fail:
                break
            for j, pc in enumerate(other_placed_test):
                if pc['net'] == v.net_name:
                    continue
                for x1s, y1s, x2s, y2s in pc['segs']:
                    d = _seg_to_seg_dist(x1, y1, x2, y2, x1s, y1s, x2s, y2s)
                    thr = stub_hw + pc['stub_hw'] + CLEARANCE
                    if d < thr:
                        fail = f"5c-stub-vs-stub[{pc['pad_num']}](d={d:.4f}<{thr:.4f})"
                        break
                if fail:
                    break
            if fail:
                break
    return cost, fail

print(f"\nActual CLK_P via at: ({clk_p_vx:.3f}, {clk_p_vy:.3f})")
print(f"Expected via with ls=-1, lat=0.150, depth=1.822: ({v.pad_x+(-1)*0.150*ldx:.3f}, {v.pad_y+(-1)*0.150*ldy+edy*1.822:.3f})")

print("\nCLK_P check results (correct sign: ls=-1, via goes LEFT):")
print(f"{'lat':>6} {'depth':>8} {'vx':>8} {'vy':>8}  result")
for lat_test in [0.000, 0.050, 0.100, 0.150, 0.200]:
    # sweep from own-pad floor upward in DEPTH_STEP=0.025 increments until first PASS, then stop
    floor = max(v.neckdown_len_mm, 0.0)
    depth_test = floor
    first_pass = None
    sweep_results = []
    while depth_test <= 2.0:
        cost, fail = check_pos(lat_test, depth_test)
        vx_t = v.pad_x + (-1)*lat_test*ldx
        vy_t = v.pad_y + (-1)*lat_test*ldy + edy*depth_test
        status = "PASS" if not fail else f"FAIL:{fail}"
        sweep_results.append((lat_test, depth_test, vx_t, vy_t, status))
        if not fail and first_pass is None:
            first_pass = depth_test
            break
        depth_test = round(depth_test + 0.025, 6)
    # print last few failures + first pass
    show = sweep_results[-4:] if len(sweep_results) > 4 else sweep_results
    for lt, dt, vx_t, vy_t, st in show:
        print(f"  {lt:.3f}   {dt:.4f}   {vx_t:.4f}   {vy_t:.4f}  {st}")
    print()
