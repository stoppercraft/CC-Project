import sys
sys.path.insert(0, 'C:/Program Files/KiCad/10.0/bin/Lib/site-packages')
import pcbnew, math

board = pcbnew.LoadBoard(r'E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb')

target_nets = {'DP_RX2_N', 'LT_TX1_P', 'LT_TX1_N'}
print('=== Tracks/Vias for short nets in Y range 115-130 ===')
for t in board.GetTracks():
    net = t.GetNetname()
    if net not in target_nets:
        continue
    cy = pcbnew.ToMM(t.GetY())
    if not (115 < cy < 130):
        continue
    if isinstance(t, pcbnew.PCB_VIA):
        cx = pcbnew.ToMM(t.GetX())
        drill = pcbnew.ToMM(t.GetDrillValue())
        copper_w = pcbnew.ToMM(t.GetWidth(pcbnew.F_Cu))
        print(f'VIA  {net:<20} pos=({cx:.3f},{cy:.3f}) drill={drill:.3f} copper_w={copper_w:.3f}')
    else:
        sx = pcbnew.ToMM(t.GetStart().x); sy = pcbnew.ToMM(t.GetStart().y)
        ex = pcbnew.ToMM(t.GetEnd().x); ey = pcbnew.ToMM(t.GetEnd().y)
        w = pcbnew.ToMM(t.GetWidth())
        L = math.hypot(ex-sx, ey-sy)
        print(f'TRK  {net:<20} ({sx:.3f},{sy:.3f})->({ex:.3f},{ey:.3f}) L={L:.4f} w={w:.3f}')
