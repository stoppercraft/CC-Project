"""
debug_highspeed.py
Algorithm verification harness for route_highspeed.py.

Two modes — set FULL_SETUP below:

  FULL_SETUP = True  (first run of a debug session):
    1. Clear ALL unlocked tracks/vias
    2. Run route_fanout_vias.py --apply
    3. Run route_critical.py --apply
    4. Snapshot all track fingerprints to hs_debug_baseline.json
    5. Run route_highspeed.py --apply --pairs TARGET_PAIRS

  FULL_SETUP = False  (fast iteration):
    1. Load snapshot, remove all tracks not in it (removes HS routes;
       fanout/critical tracks survive — unlocked, same as Route and Verify)
    2. Run route_highspeed.py --apply --pairs TARGET_PAIRS

No locking is used. Route_highspeed.py always sees the same unlocked
board state it would see in a real Route and Verify run.

Workflow:
  1. Set FULL_SETUP = True, run once. Inspect in KiCad.
  2. Change algorithm in route_highspeed.py.
  3. Set FULL_SETUP = False, run again. Only HS routes are replaced.
  4. Repeat step 2-3 until satisfied.
  5. Run Route and Verify for full pipeline validation before committing.

DO NOT run DRC or board inspection after this script — the board shows partial
HS routing only (TARGET_PAIRS routed, all other HS pairs absent).
"""
import sys, os, subprocess, json

SCRIPTS_DIR   = os.path.dirname(os.path.abspath(__file__))
PYTHON        = sys.executable
SNAPSHOT_FILE = os.path.join(SCRIPTS_DIR, "hs_debug_baseline.json")

sys.path.insert(0, SCRIPTS_DIR)
import routing_config as cfg

PCB = cfg.PCB_FILE

# ── Configure here ────────────────────────────────────────────────────────────

# Pair names from routing_config.HS_PAIRS to route and inspect.
TARGET_PAIRS = ["USB2"]   # empty = route all pairs (same as route_highspeed.py default)

# Segment indices (0-based) to route within each pair.
TARGET_SEGS  = [2]        # empty = route all segments

# Component refs — route all pairs that have a pad on any of these refs.
TARGET_REFS  = []         # empty = no ref filter

# Net names — route only pairs whose P or N net matches.
TARGET_NETS  = []         # empty = no net filter

# True  → rebuild from scratch (clear all → fanout → critical → snapshot → hs)
# False → fast iteration (restore snapshot → hs)
FULL_SETUP = False

# ─────────────────────────────────────────────────────────────────────────────

pairs_arg = ",".join(TARGET_PAIRS)
segs_arg  = ",".join(str(s) for s in TARGET_SEGS)
refs_arg  = ",".join(TARGET_REFS)
nets_arg  = ",".join(TARGET_NETS)

if FULL_SETUP:
    # ── Clear ALL unlocked tracks and vias ────────────────────────────────────
    print("=== [setup] clear all unlocked ===")
    r = subprocess.run(
        [PYTHON, "-c",
         "import sys; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages');"
         "import pcbnew;"
         f"board=pcbnew.LoadBoard(r'{PCB}');"
         "to_remove=[t for t in board.GetTracks() if not t.IsLocked()];"
         "[board.Remove(t) for t in to_remove];"
         "board.Save(board.GetFileName());"
         "print(f'Cleared {len(to_remove)} tracks/vias')"],
        capture_output=True, text=True)
    cleared = [l for l in r.stdout.splitlines() if l.startswith("Cleared")]
    print(cleared[0] if cleared else "(cleared)")

    # ── Run route_fanout_vias.py --apply ──────────────────────────────────────
    print("\n=== [setup] route_fanout_vias.py --apply ===")
    r = subprocess.run(
        [PYTHON, os.path.join(SCRIPTS_DIR, "route_fanout_vias.py"), "--apply"],
        capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if any(k in line for k in ("via(s) placed", "Saved →", "keepout", "pocket-push")):
            print(line)

    # ── Run route_critical.py --apply ─────────────────────────────────────────
    print("\n=== [setup] route_critical.py --apply ===")
    r = subprocess.run(
        [PYTHON, os.path.join(SCRIPTS_DIR, "route_critical.py"), "--apply"],
        capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if any(k in line for k in ("CONFLICT", "anchor-reloc", "Board saved", "full-net")):
            print(line)

    # ── Snapshot fanout + critical track fingerprints ─────────────────────────
    print("\n=== [setup] snapshotting fanout + critical tracks ===")
    r = subprocess.run(
        [PYTHON, "-c",
         "import sys, json; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages');"
         "import pcbnew;"
         f"board=pcbnew.LoadBoard(r'{PCB}');"
         "fps=[[t.GetNetname(),t.GetStart().x,t.GetStart().y,t.GetEnd().x,t.GetEnd().y]"
         " for t in board.GetTracks()];"
         f"json.dump(fps,open(r'{SNAPSHOT_FILE}','w'));"
         "print(f'Snapshot: {len(fps)} tracks/vias')"],
        capture_output=True, text=True)
    snapped = [l for l in r.stdout.splitlines() if l.startswith("Snapshot")]
    print(snapped[0] if snapped else r.stdout.strip())

else:
    # ── Fast iteration: restore fanout + critical baseline ────────────────────
    if not os.path.exists(SNAPSHOT_FILE):
        print("ERROR: hs_debug_baseline.json not found — run with FULL_SETUP = True first.")
        sys.exit(1)
    print("=== [fast] restoring fanout + critical baseline ===")
    r = subprocess.run(
        [PYTHON, "-c",
         "import sys, json; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages');"
         "import pcbnew;"
         f"snap=set(tuple(x) for x in json.load(open(r'{SNAPSHOT_FILE}')));"
         f"board=pcbnew.LoadBoard(r'{PCB}');"
         "fp=lambda t:(t.GetNetname(),t.GetStart().x,t.GetStart().y,t.GetEnd().x,t.GetEnd().y);"
         "to_remove=[t for t in board.GetTracks() if fp(t) not in snap];"
         "[board.Remove(t) for t in to_remove];"
         "board.Save(board.GetFileName());"
         "kept=sum(1 for t in board.GetTracks());"
         "print(f'Removed {len(to_remove)} HS tracks, kept {kept} baseline tracks')"],
        capture_output=True, text=True)
    result = [l for l in r.stdout.splitlines() if l.startswith("Removed")]
    print(result[0] if result else r.stdout.strip())
    if r.returncode != 0:
        print(r.stderr.strip())

# ── Route target pairs ────────────────────────────────────────────────────────
segs_label = f" segs '{segs_arg}'" if segs_arg else ""
refs_label = f" refs '{refs_arg}'" if refs_arg else ""
nets_label = f" nets '{nets_arg}'" if nets_arg else ""
print(f"\n=== route_highspeed.py --apply --pairs '{pairs_arg}'{segs_label}{refs_label}{nets_label} ===")
cmd = [PYTHON, os.path.join(SCRIPTS_DIR, "route_highspeed.py"),
       "--apply", "--pairs", pairs_arg]
if segs_arg:
    cmd += ["--segs", segs_arg]
if refs_arg:
    cmd += ["--refs", refs_arg]
if nets_arg:
    cmd += ["--nets", nets_arg]
r = subprocess.run(cmd, capture_output=True, text=True)
print(r.stdout)
if r.returncode != 0:
    print(f"[exit code {r.returncode} — conflicts present]")

segs_note = f" seg(s) {TARGET_SEGS}" if TARGET_SEGS else ""
refs_note = f" refs {TARGET_REFS}" if TARGET_REFS else ""
nets_note = f" nets {TARGET_NETS}" if TARGET_NETS else ""
print(f"\nBoard written: {TARGET_PAIRS if TARGET_PAIRS else 'all pairs'} routed{segs_note}{refs_note}{nets_note}.")
if FULL_SETUP:
    print("Baseline snapshot saved. Set FULL_SETUP = False for fast iteration.")
print("Reload in KiCad to inspect. DO NOT run DRC or Route and Verify — board is partial.")
