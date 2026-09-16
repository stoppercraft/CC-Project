"""
Verify that the HS routing layer is properly sandwiched by solid GND reference planes.

A buried stripline requires unbroken GND on both copper layers immediately adjacent
to the HS layer. Any non-GND zone, track, or via anti-pad on a reference plane
beneath a HS trace breaks the return path, invalidates impedance, and creates
reflections.

Run after Phase 9 zone fill and again after Phase 10 HS routing. Any FAIL is
blocking before Phase 10.5 autorouting.
"""

import sys, os, pathlib

import routing_config as cfg

sys.path.insert(0, cfg.KICAD_SITE_PKGS)
import pcbnew

if len(sys.argv) < 2:
    print("Usage: python verify_hs_sandwich.py <HS_LAYER>")
    print("  e.g. python verify_hs_sandwich.py F.Cu")
    print("  e.g. python verify_hs_sandwich.py B.Cu")
    sys.exit(1)

PCB_FILE     = cfg.PCB_FILE
REPORT_FILE  = str(pathlib.Path(cfg.REPORTS_DIR) / f"hs_sandwich_{sys.argv[1].replace('.','_')}.txt")
HS_LAYER     = sys.argv[1]
GND_NET_NAME = "GND"

board  = pcbnew.LoadBoard(PCB_FILE)
hs_id  = board.GetLayerID(HS_LAYER)
issues = []

os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)

# ── Gate: skip entirely if no HS tracks on the designated layer ───────────────
hs_tracks = [
    t for t in board.GetTracks()
    if not isinstance(t, pcbnew.PCB_VIA) and t.GetLayer() == hs_id
]
if not hs_tracks:
    msg = f"HS SANDWICH CHECK: SKIP — no tracks found on {HS_LAYER}\n"
    with open(REPORT_FILE, "w") as f:
        f.write(msg)
    print(msg.strip())
    sys.exit(0)

# ── Determine reference planes from the actual layer stack ────────────────────
copper_layers = [board.GetLayerName(i) for i in board.GetEnabledLayers().CuStack()]
hs_idx        = copper_layers.index(HS_LAYER)
ref_names     = []
if hs_idx > 0:
    ref_names.append(copper_layers[hs_idx - 1])
if hs_idx < len(copper_layers) - 1:
    ref_names.append(copper_layers[hs_idx + 1])
ref_ids = [board.GetLayerID(n) for n in ref_names]

print(f"HS layer   : {HS_LAYER}")
print(f"Ref planes : {ref_names}")

# ── HS channel bounding box (1 mm margin each side) ──────────────────────────
xs = ([pcbnew.ToMM(t.GetX()) for t in hs_tracks] +
      [pcbnew.ToMM(t.GetEndX()) for t in hs_tracks])
ys = ([pcbnew.ToMM(t.GetY()) for t in hs_tracks] +
      [pcbnew.ToMM(t.GetEndY()) for t in hs_tracks])
bbox = (min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1)

# ── Rule 1: All zones on reference planes must be GND ────────────────────────
for zone in board.Zones():
    if zone.GetLayer() in ref_ids and zone.GetNetname() != GND_NET_NAME:
        issues.append(
            f"NON-GND ZONE on {board.GetLayerName(zone.GetLayer())}: "
            f"net={zone.GetNetname()} — reference plane must be solid GND"
        )

# ── Rule 2: No non-GND tracks on reference planes ────────────────────────────
for track in board.GetTracks():
    if (isinstance(track, pcbnew.PCB_TRACK)
            and not isinstance(track, pcbnew.PCB_VIA)
            and track.GetLayer() in ref_ids
            and track.GetNetname() != GND_NET_NAME):
        issues.append(
            f"NON-GND TRACK on {board.GetLayerName(track.GetLayer())}: "
            f"net={track.GetNetname()} at "
            f"({pcbnew.ToMM(track.GetX()):.2f}, {pcbnew.ToMM(track.GetY()):.2f})"
        )

# ── Rule 3: Non-GND, non-HS signal vias in HS channel piercing reference planes ─
# HS signal transition vias (F.Cu→B.Cu at component pads) are expected and have
# GND return vias placed adjacent — exempt them. Only non-HS, non-GND vias matter.
hs_nets_set = set(cfg.HS_NETS)
for track in board.GetTracks():
    net_name = track.GetNetname()
    if (not isinstance(track, pcbnew.PCB_VIA)
            or net_name == GND_NET_NAME
            or net_name in hs_nets_set):
        continue
    lset    = track.GetLayerSet()
    pierced = [l for l in ref_ids if lset.Contains(l)]
    if not pierced:
        continue
    vx, vy = pcbnew.ToMM(track.GetX()), pcbnew.ToMM(track.GetY())
    if bbox[0] <= vx <= bbox[2] and bbox[1] <= vy <= bbox[3]:
        issues.append(
            f"NON-GND VIA in HS channel piercing "
            f"{[board.GetLayerName(l) for l in pierced]}: "
            f"net={track.GetNetname()} at ({vx:.2f}, {vy:.2f}) "
            f"— anti-pad creates gap in return path"
        )

# ── Rule 4: Zone boundaries on reference planes must not cross HS channel ─────
for zone in board.Zones():
    if zone.GetLayer() not in ref_ids:
        continue
    poly = zone.Outline()
    outline = poly.Outline(0) if poly.OutlineCount() > 0 else None
    if outline is None:
        continue
    for seg_idx in range(outline.SegmentCount()):
        seg = outline.Segment(seg_idx)
        sx, sy = pcbnew.ToMM(seg.A.x), pcbnew.ToMM(seg.A.y)
        if bbox[0] <= sx <= bbox[2] and bbox[1] <= sy <= bbox[3]:
            issues.append(
                f"ZONE BOUNDARY on {board.GetLayerName(zone.GetLayer())} "
                f"crosses HS channel at ({sx:.2f}, {sy:.2f}) "
                f"— split reference plane breaks return path"
            )

# ── Write report ──────────────────────────────────────────────────────────────
with open(REPORT_FILE, "w") as f:
    if issues:
        f.write(f"HS SANDWICH CHECK: {len(issues)} ISSUE(S)\n\n")
        for i in issues:
            f.write(f"  FAIL: {i}\n")
        f.write("\nResolution guide:\n")
        f.write("  Non-GND zone on ref plane       -> move net to F.Cu/B.Cu or add solid GND zone\n")
        f.write("  Non-GND track on ref plane       -> reroute on F.Cu or B.Cu\n")
        f.write("  Non-GND via in HS channel        -> move via outside HS bbox, or add GND via within 0.5 mm\n")
        f.write("  Zone boundary crossing HS channel -> merge split zones or shift HS routing\n")
    else:
        f.write("HS SANDWICH CHECK: PASS — reference planes solid under HS channel\n")

if issues:
    print(f"\nFAIL — {len(issues)} issue(s). See {REPORT_FILE}")
    for i in issues:
        print(f"  {i}")
else:
    print(f"\nPASS — reference planes solid under HS channel. Report: {REPORT_FILE}")
