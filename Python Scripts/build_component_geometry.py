"""
build_component_geometry.py
───────────────────────────
Generates (or regenerates) component_geometry.json from a KiCad PCB file.

Extracted per footprint:
  • courtyard.local_bbox   — axis-aligned bounding box in local (0°) coords
  • courtyard.polygon      — actual courtyard outline vertices in local (0°) coords
  • courtyard.width_mm / height_mm
  • pads                   — local_xy, net, num, shape, size_mm, type, drill_mm, face_at_0deg
  • position_mm, rotation_deg, layer, locked, footprint
  • preferred_rotation     — current PCB rotation (edit manually for new footprints if needed)
  • rotation_symmetry      — 'none', '180', '4fold' (computed from pad layout)
  • net_dominant_face      — {net: face} from pad positions

Usage:
  "C:/Program Files/KiCad/10.0/bin/python.exe" build_component_geometry.py
  "C:/Program Files/KiCad/10.0/bin/python.exe" build_component_geometry.py --update   # patch polygon only
"""

# ── PROJECT CONFIG ────────────────────────────────────────────────────────────
PCB_FILE        = r"E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb"
OUTPUT_FILE     = r"E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\component_geometry.json"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"
# ─────────────────────────────────────────────────────────────────────────────

import sys, os, math, json, argparse, collections

sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew


# ── COORDINATE HELPERS ────────────────────────────────────────────────────────

def to_mm(v):
    return pcbnew.ToMM(v)

def rot2d(x, y, deg):
    r = math.radians(deg)
    return (x * math.cos(r) - y * math.sin(r),
            x * math.sin(r) + y * math.cos(r))

def unrot2d(wx, wy, deg):
    """Convert world offset (relative to fp center) → local (0°) coords."""
    r = math.radians(deg)
    lx =  wx * math.cos(r) + wy * math.sin(r)
    ly = -wx * math.sin(r) + wy * math.cos(r)
    return lx, ly


# ── PAD HELPERS ──────────────────────────────────────────────────────────────

def face_for_local_xy(lx, ly):
    """Determine which face a pad is on from its local position."""
    if abs(lx) >= abs(ly):
        return 'right' if lx >= 0 else 'left'
    return 'down' if ly >= 0 else 'up'

def pad_local_xy(pad, fp_rot_deg):
    """Return pad centroid in footprint local (0°) coordinates."""
    fp_pos = pad.GetParentFootprint().GetPosition()
    pad_pos = pad.GetCenter()
    wx = to_mm(pad_pos.x) - to_mm(fp_pos.x)
    wy = to_mm(pad_pos.y) - to_mm(fp_pos.y)
    return unrot2d(wx, wy, fp_rot_deg)

def pad_shape_name(pad):
    shape_map = {
        pcbnew.PAD_SHAPE_CIRCLE:    'circle',
        pcbnew.PAD_SHAPE_OVAL:      'oval',
        pcbnew.PAD_SHAPE_RECT:      'rect',
        pcbnew.PAD_SHAPE_ROUNDRECT: 'roundrect',
        pcbnew.PAD_SHAPE_TRAPEZOID: 'trapezoid',
        pcbnew.PAD_SHAPE_CHAMFERED_RECT: 'chamfered_rect',
    }
    return shape_map.get(pad.GetShape(), 'unknown')

def pad_type_name(pad):
    type_map = {
        pcbnew.PAD_ATTRIB_PTH:  'PTH',
        pcbnew.PAD_ATTRIB_SMD:  'SMT',
        pcbnew.PAD_ATTRIB_CONN: 'CONN',
        pcbnew.PAD_ATTRIB_NPTH: 'NPTH',
    }
    return type_map.get(pad.GetAttribute(), 'SMT')


# ── COURTYARD EXTRACTION ──────────────────────────────────────────────────────

def get_courtyard_polygon_local(fp, fp_rot_deg):
    """
    Extract the courtyard outline as a list of [x, y] vertices in local (0°) coords.
    Returns None if no courtyard is defined.
    """
    fp_pos = fp.GetPosition()
    fp_x_mm = to_mm(fp_pos.x)
    fp_y_mm = to_mm(fp_pos.y)

    for layer in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
        try:
            cyd = fp.GetCourtyard(layer)
            if cyd.OutlineCount() == 0:
                continue
            outline = cyd.COutline(0)
            n = outline.PointCount()
            if n < 3:
                continue
            vertices = []
            for i in range(n):
                pt = outline.CPoint(i)
                wx = to_mm(pt.x) - fp_x_mm
                wy = to_mm(pt.y) - fp_y_mm
                lx, ly = unrot2d(wx, wy, fp_rot_deg)
                vertices.append([round(lx, 4), round(ly, 4)])
            return vertices
        except Exception:
            continue
    return None

def courtyard_local_bbox(polygon):
    """Compute local AABB from polygon vertices."""
    if not polygon:
        return None
    xs = [v[0] for v in polygon]
    ys = [v[1] for v in polygon]
    return {
        'x0': round(min(xs), 4),
        'y0': round(min(ys), 4),
        'x1': round(max(xs), 4),
        'y1': round(max(ys), 4),
    }

def courtyard_from_pads(fp, fp_rot_deg, margin=0.15):
    """Fallback courtyard from pad bounding box + margin."""
    pts = []
    for pad in fp.Pads():
        lx, ly = pad_local_xy(pad, fp_rot_deg)
        sx, sy = to_mm(pad.GetSizeX()) / 2, to_mm(pad.GetSizeY()) / 2
        pts += [(lx - sx, ly - sy), (lx + sx, ly + sy)]
    if not pts:
        return None, None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = min(xs) - margin, max(xs) + margin
    y0, y1 = min(ys) - margin, max(ys) + margin
    poly = [[round(x0,4),round(y0,4)],[round(x1,4),round(y0,4)],
            [round(x1,4),round(y1,4)],[round(x0,4),round(y1,4)]]
    bbox = {'x0': round(x0,4), 'y0': round(y0,4), 'x1': round(x1,4), 'y1': round(y1,4)}
    return poly, bbox


# ── ROTATION SYMMETRY ─────────────────────────────────────────────────────────

def detect_rotation_symmetry(pad_local_xys, tol=0.05):
    """
    Detect rotational symmetry from pad positions (ignoring net assignment).
    Returns '4fold', '180', or 'none'.
    """
    pts = [(round(x, 2), round(y, 2)) for x, y in pad_local_xys]
    if not pts:
        return 'none'

    def rotated_set(pts, deg):
        r = math.radians(deg)
        return {(round(x * math.cos(r) - y * math.sin(r), 2),
                 round(x * math.sin(r) + y * math.cos(r), 2))
                for x, y in pts}

    orig = set(pts)
    r180 = rotated_set(pts, 180)
    r90  = rotated_set(pts, 90)

    def sets_match(a, b):
        if len(a) != len(b):
            return False
        for pa in a:
            if not any(abs(pa[0]-pb[0]) < tol and abs(pa[1]-pb[1]) < tol for pb in b):
                return False
        return True

    if sets_match(orig, r90) and sets_match(orig, r180):
        return '4fold'
    if sets_match(orig, r180):
        return '180'
    return 'none'


# ── NET DOMINANT FACE ─────────────────────────────────────────────────────────

def compute_net_dominant_face(pad_entries):
    """Return {net: dominant_face} from list of pad dicts."""
    net_faces = collections.defaultdict(list)
    for p in pad_entries:
        if p['net']:
            net_faces[p['net']].append(p['face_at_0deg'])
    result = {}
    for net, faces in net_faces.items():
        counter = collections.Counter(faces)
        result[net] = counter.most_common(1)[0][0]
    return result


# ── FOOTPRINT ENTRY BUILDER ───────────────────────────────────────────────────

def build_entry(fp):
    ref        = fp.GetReference()
    fp_rot_deg = fp.GetOrientationDegrees()
    fp_pos     = fp.GetPosition()
    layer      = 'F.Cu' if fp.GetLayer() == pcbnew.F_Cu else 'B.Cu'
    locked     = fp.IsLocked()
    footprint  = fp.GetFPIDAsString().split(':')[-1] if ':' in fp.GetFPIDAsString() else fp.GetFPIDAsString()

    # ── Courtyard
    polygon = get_courtyard_polygon_local(fp, fp_rot_deg)
    if polygon:
        bbox = courtyard_local_bbox(polygon)
    else:
        polygon, bbox = courtyard_from_pads(fp, fp_rot_deg)

    if bbox:
        w = round(bbox['x1'] - bbox['x0'], 4)
        h = round(bbox['y1'] - bbox['y0'], 4)
        courtyard = {
            'local_bbox':  bbox,
            'polygon':     polygon,
            'width_mm':    w,
            'height_mm':   h,
        }
    else:
        courtyard = None

    # ── Pads
    pad_entries = []
    for pad in fp.Pads():
        lx, ly = pad_local_xy(pad, fp_rot_deg)
        face   = face_for_local_xy(lx, ly)
        sx, sy = to_mm(pad.GetSizeX()), to_mm(pad.GetSizeY())
        try:
            drill_x = to_mm(pad.GetDrillSizeX())
            drill_y = to_mm(pad.GetDrillSizeY())
        except Exception:
            drill_x = drill_y = 0.0
        pad_entries.append({
            'num':         pad.GetNumber(),
            'net':         pad.GetNetname(),
            'local_xy':    [round(lx, 4), round(ly, 4)],
            'face_at_0deg': face,
            'shape':       pad_shape_name(pad),
            'size_mm':     [round(sx, 4), round(sy, 4)],
            'type':        pad_type_name(pad),
            'drill_mm':    [round(drill_x, 4), round(drill_y, 4)],
        })

    pad_local_xys = [(p['local_xy'][0], p['local_xy'][1]) for p in pad_entries]

    return {
        'footprint':          footprint,
        'layer':              layer,
        'locked':             locked,
        'position_mm':        [round(to_mm(fp_pos.x), 4), round(to_mm(fp_pos.y), 4)],
        'rotation_deg':       fp_rot_deg,
        'preferred_rotation': fp_rot_deg,
        'rotation_symmetry':  detect_rotation_symmetry(pad_local_xys),
        'courtyard':          courtyard,
        'pads':               pad_entries,
        'net_dominant_face':  compute_net_dominant_face(pad_entries),
    }


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build component_geometry.json from a KiCad PCB.")
    parser.add_argument('--update', action='store_true',
                        help='Patch polygon into existing DB instead of full regeneration.')
    args = parser.parse_args()

    print(f"Loading PCB: {PCB_FILE}")
    board = pcbnew.LoadBoard(PCB_FILE)
    fps   = {fp.GetReference(): fp for fp in board.GetFootprints()}
    print(f"  {len(fps)} footprints found")

    if args.update and os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            db = json.load(f)
        print(f"  Patching polygon into existing DB ({len(db)} entries)...")
        updated = 0
        for ref, fp in fps.items():
            if ref not in db:
                continue
            fp_rot_deg = fp.GetOrientationDegrees()
            polygon    = get_courtyard_polygon_local(fp, fp_rot_deg)
            if polygon is None:
                polygon, _ = courtyard_from_pads(fp, fp_rot_deg)
            if polygon and db[ref].get('courtyard'):
                db[ref]['courtyard']['polygon'] = polygon
                # Refresh bbox from polygon (more accurate than stored value)
                bbox = courtyard_local_bbox(polygon)
                db[ref]['courtyard']['local_bbox'] = bbox
                db[ref]['courtyard']['width_mm']   = round(bbox['x1'] - bbox['x0'], 4)
                db[ref]['courtyard']['height_mm']  = round(bbox['y1'] - bbox['y0'], 4)
                updated += 1
        print(f"  Updated {updated} entries with polygon data")
    else:
        print("  Building full geometry DB...")
        db = {}
        for ref, fp in fps.items():
            try:
                db[ref] = build_entry(fp)
            except Exception as e:
                print(f"  WARNING: {ref} failed — {e}")
        print(f"  Built {len(db)} entries")

    with open(OUTPUT_FILE, 'w') as f:
        json.dump(db, f, indent=2)
    print(f"  Saved to {OUTPUT_FILE}")


if __name__ == '__main__':
    main()
