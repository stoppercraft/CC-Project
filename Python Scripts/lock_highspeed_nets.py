"""
Lock all HS differential pair segments and vias so FreeRouting cannot reroute them.

Run after Phase 10 manual HS pre-routing and before Phase 10.5 DSN export.
Any HS net with 0 segments locked means the Phase 10 routing script did not
complete for that net — fix before proceeding, or FreeRouting will overwrite
the manual pre-route.
"""

import sys

import routing_config as cfg

sys.path.insert(0, cfg.KICAD_SITE_PKGS)
import pcbnew

PCB_FILE = cfg.PCB_FILE
HS_NETS  = cfg.HS_NETS

board = pcbnew.LoadBoard(PCB_FILE)

print("=" * 60)
print("LOCKING HS NETS FOR FREEROUTING EXCLUSION")
print("=" * 60)

total_locked = 0
for net_name in HS_NETS:
    count = 0
    for track in board.GetTracks():
        if track.GetNetname() == net_name:
            track.SetLocked(True)
            count += 1
    status = "OK" if count > 0 else "WARN — 0 segments found (net not routed?)"
    print(f"  {net_name:<20} {count:>4} segments locked   [{status}]")
    total_locked += count

board.Save(PCB_FILE)

print()
print(f"Total segments locked: {total_locked}")
print(f"Saved: {PCB_FILE}")
print()
if any(True for n in HS_NETS
       if sum(1 for t in board.GetTracks() if t.GetNetname() == n) == 0):
    print("WARNING: One or more HS nets have 0 segments — re-run Phase 10 HS routing")
    print("before exporting DSN. FreeRouting will autoroute unlocked HS nets.")
else:
    print("All HS nets locked. Safe to export DSN for Phase 10.5 autorouting.")
print("=" * 60)
