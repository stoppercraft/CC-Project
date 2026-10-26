"""
debug_highspeed.py
Algorithm verification harness for route_highspeed.py.

Two modes — set FULL_SETUP below:

  FULL_SETUP = True  (first run of a debug session):
    1. Clear ALL unlocked tracks/vias
    2. Run route_fanout_vias.py --apply
    3. Run route_critical.py --apply
    4. Lock all current tracks/vias (fanout stubs, power trunks, VIPPO vias)
    5. Run route_highspeed.py --apply --pairs TARGET_PAIRS

  FULL_SETUP = False  (fast iteration — upstream work is already locked):
    1. Clear UNLOCKED tracks/vias only (HS routes from previous run removed;
       locked fanout/critical tracks survive)
    2. Run route_highspeed.py --apply --pairs TARGET_PAIRS

Workflow:
  1. Set FULL_SETUP = True, run once. Inspect in KiCad.
  2. Change algorithm in route_highspeed.py.
  3. Set FULL_SETUP = False, run again. Only HS routes are replaced.
  4. Repeat step 2–3 until satisfied.
  5. Run Route and Verify for full pipeline validation before committing.

DO NOT run DRC or board inspection after this script — the board shows partial
HS routing only (TARGET_PAIRS routed, all other HS pairs absent).
"""
import sys, os, subprocess

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON      = sys.executable

sys.path.insert(0, SCRIPTS_DIR)
import routing_config as cfg

PCB = cfg.PCB_FILE

# ── Configure here ────────────────────────────────────────────────────────────

# Pair names from routing_config.HS_PAIRS to route and inspect.
TARGET_PAIRS = []   # empty = route all pairs (same as route_highspeed.py default)

# True  → rebuild from scratch (clear all → fanout → critical → lock → hs)
# False → fast iteration (clear unlocked only → hs)
FULL_SETUP = True

# ─────────────────────────────────────────────────────────────────────────────

pairs_arg = ",".join(TARGET_PAIRS)

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

    # ── Lock all current tracks and vias ──────────────────────────────────────
    # Everything placed by fanout + critical is now locked.  The next clear
    # (in a subsequent FULL_SETUP=False run) will only remove HS routes.
    print("\n=== [setup] locking fanout + critical tracks/vias ===")
    r = subprocess.run(
        [PYTHON, "-c",
         "import sys; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages');"
         "import pcbnew;"
         f"board=pcbnew.LoadBoard(r'{PCB}');"
         "[t.SetLocked(True) for t in board.GetTracks()];"
         "n=sum(1 for t in board.GetTracks() if t.IsLocked());"
         "board.Save(board.GetFileName());"
         "print(f'Locked {n} tracks/vias')"],
        capture_output=True, text=True)
    locked = [l for l in r.stdout.splitlines() if l.startswith("Locked")]
    print(locked[0] if locked else r.stdout.strip())

else:
    # ── Fast iteration: clear unlocked HS routes only ─────────────────────────
    print("=== [fast] clear unlocked HS routes ===")
    r = subprocess.run(
        [PYTHON, "-c",
         "import sys; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages');"
         "import pcbnew;"
         f"board=pcbnew.LoadBoard(r'{PCB}');"
         "to_remove=[t for t in board.GetTracks() if not t.IsLocked()];"
         "[board.Remove(t) for t in to_remove];"
         "board.Save(board.GetFileName());"
         "print(f'Cleared {len(to_remove)} unlocked tracks/vias')"],
        capture_output=True, text=True)
    cleared = [l for l in r.stdout.splitlines() if l.startswith("Cleared")]
    print(cleared[0] if cleared else "(cleared)")

# ── Route target pairs ────────────────────────────────────────────────────────
print(f"\n=== route_highspeed.py --apply --pairs {pairs_arg} ===")
r = subprocess.run(
    [PYTHON, os.path.join(SCRIPTS_DIR, "route_highspeed.py"),
     "--apply", "--pairs", pairs_arg],
    capture_output=True, text=True)
print(r.stdout)
if r.returncode != 0:
    print(f"[exit code {r.returncode} — conflicts present]")

print(f"\nBoard written: {TARGET_PAIRS} routed.")
if FULL_SETUP:
    print("Upstream tracks/vias are now LOCKED. Set FULL_SETUP = False for fast iteration.")
print("Reload in KiCad to inspect. DO NOT run DRC or Route and Verify — board is partial.")
