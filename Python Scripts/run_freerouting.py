"""
run_freerouting.py — Headless FreeRouting autorouter wrapper

Copies the DSN to a short temp path (avoids Java path-with-spaces issues),
launches FreeRouting with the configured parameters, polls the SES output
file for stability, and validates the result before copying back.

Classifies failures with actionable messages and exits with code 1 on any error.

Usage:
    python run_freerouting.py <input.dsn> <output.ses> [max_passes]

    <input.dsn>   — Specctra DSN exported from KiCad (pcbnew.ExportSpecctraDSN)
    <output.ses>  — Destination path for the Specctra SES result
    [max_passes]  — Override MAX_PASSES (default: value in CONFIG block)

Fill in the PROJECT CONFIG block before running.
"""

import routing_config as _cfg

FREEROUTING_JAR  = _cfg.FREEROUTING_JAR
TEMP_DIR         = _cfg.FREEROUTING_TEMP_DIR
PCB_FILE         = _cfg.PCB_FILE
KICAD_SITE_PKGS  = _cfg.KICAD_SITE_PKGS

# Autorouter parameters
MAX_PASSES       = 100      # routing passes (-mp); increase for complex boards
THREADS          = 4        # parallel routing threads (-mt)
MAX_RUNTIME_SECS = 1200     # hard timeout in seconds (20 min)
STABLE_SECS      = 12       # stop when SES file size stable for this many seconds
POLL_INTERVAL    = 1        # seconds between SES-size polls

# Minimum SES file size that counts as non-truncated
MIN_SES_BYTES    = 512

# Java heap size for FreeRouting
JAVA_HEAP        = "-Xmx2g"  # increase to -Xmx4g if you get OutOfMemoryError

import subprocess
import time
import os
import sys
import shutil
import glob

CRASH_PATTERNS = [
    "OutOfMemoryError",
    "NullPointerException",
    "StackOverflowError",
    "ArrayIndexOutOfBoundsException",
    "ClassNotFoundException",
    "NoClassDefFoundError",
    "Exception in thread",
    "FATAL ERROR",
    "A fatal error has been detected",
]


def _check_java_crash_log(temp_dir):
    logs = glob.glob(os.path.join(temp_dir, "hs_err_pid*.log"))
    logs += glob.glob(os.path.join(os.getcwd(), "hs_err_pid*.log"))
    return logs


def _classify_failure(exit_code, stdout, stderr, ses_path, timed_out):
    reasons = []
    if timed_out:
        reasons.append(
            "TIMEOUT: exceeded MAX_RUNTIME_SECS. "
            "Increase MAX_RUNTIME_SECS or reduce board complexity."
        )
    if exit_code not in (0, None, -15):
        reasons.append(f"BAD_EXIT_CODE: java exited with code {exit_code}.")
    for pattern in CRASH_PATTERNS:
        if pattern in stderr or pattern in stdout:
            if "OutOfMemoryError" in pattern:
                reasons.append(f"OUT_OF_MEMORY: change {JAVA_HEAP} to -Xmx4g and retry.")
            elif "NullPointerException" in pattern or "ArrayIndexOutOfBounds" in pattern:
                reasons.append(f"JAVA_EXCEPTION ({pattern}): try a fresh DSN export.")
            else:
                reasons.append(f"JAVA_ERROR: '{pattern}' in output.")
    crash_logs = _check_java_crash_log(TEMP_DIR)
    if crash_logs:
        reasons.append(f"JVM_CRASH_LOG: {crash_logs}")
    if not os.path.exists(ses_path):
        reasons.append("NO_SES_FILE: FreeRouting crashed before writing any routing.")
    elif os.path.getsize(ses_path) == 0:
        reasons.append("EMPTY_SES_FILE: 0 bytes.")
    elif os.path.getsize(ses_path) < MIN_SES_BYTES:
        reasons.append(f"TRUNCATED_SES_FILE: {os.path.getsize(ses_path)} bytes.")
    return reasons


def _validate_ses(ses_path):
    try:
        with open(ses_path, "r", errors="replace") as f:
            content = f.read(4096)
    except OSError as e:
        return False, f"Cannot read SES file: {e}"
    if not content.lstrip().startswith("(session"):
        return False, "SES does not start with '(session'."
    try:
        with open(ses_path, "r", errors="replace") as f:
            full = f.read()
    except OSError as e:
        return False, f"Cannot read SES: {e}"
    if "(routes" not in full:
        return False, "SES has no '(routes' block — FreeRouting crashed before routing."
    return True, "OK"


def run(dsn_path, ses_path, max_passes=MAX_PASSES):
    os.makedirs(TEMP_DIR, exist_ok=True)
    temp_dsn = os.path.join(TEMP_DIR, "autoroute.dsn")
    temp_ses = os.path.join(TEMP_DIR, "autoroute.ses")
    if os.path.abspath(dsn_path) != os.path.abspath(temp_dsn):
        shutil.copy(dsn_path, temp_dsn)
    if os.path.exists(temp_ses):
        os.remove(temp_ses)

    cmd = [
        "java",
        "-Djava.awt.headless=true",
        JAVA_HEAP,
        "-jar", FREEROUTING_JAR,
        "-de", temp_dsn,
        "-do", temp_ses,
        "-mp", str(max_passes),
        "-mt", str(THREADS),
    ]
    print("Starting FreeRouting:", " ".join(cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )

    last_size  = -1
    stable_secs = 0
    elapsed    = 0
    timed_out  = False

    while proc.poll() is None:
        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        if os.path.exists(temp_ses):
            size = os.path.getsize(temp_ses)
            if size > 0 and size == last_size:
                stable_secs += POLL_INTERVAL
                if stable_secs >= STABLE_SECS:
                    print(f"SES stable for {STABLE_SECS}s — routing done. Terminating.")
                    proc.terminate()
                    break
            else:
                stable_secs = 0
            last_size = size
        if elapsed >= MAX_RUNTIME_SECS:
            print(f"Hard timeout {MAX_RUNTIME_SECS}s reached. Terminating.")
            proc.terminate()
            timed_out = True
            break

    stdout, stderr = proc.communicate(timeout=30)
    exit_code = proc.returncode

    failures = _classify_failure(exit_code, stdout, stderr, temp_ses, timed_out)
    if failures:
        print("\n=== FREEROUTING FAILURE REPORT ===")
        for f in failures:
            print(f"  * {f}")
        print("\nFull stderr (last 3000 chars):", stderr[-3000:] if stderr else "(empty)")
        sys.exit(1)

    valid, reason = _validate_ses(temp_ses)
    if not valid:
        print(f"\n=== SES VALIDATION FAILED ===\n  {reason}")
        sys.exit(1)

    shutil.copy(temp_ses, ses_path)
    print(f"\nSES written to: {ses_path}")
    for line in stdout.splitlines():
        if "unrouted" in line.lower():
            print(line)
    return ses_path


if __name__ == "__main__":
    os.makedirs(TEMP_DIR, exist_ok=True)
    default_dsn = os.path.join(TEMP_DIR, "autoroute.dsn")
    default_ses = os.path.join(TEMP_DIR, "autoroute.ses")

    if len(sys.argv) == 1:
        # Auto mode: export DSN from PCB, then route
        sys.path.insert(0, KICAD_SITE_PKGS)
        import pcbnew
        print(f"Exporting DSN from {PCB_FILE} ...")
        board = pcbnew.LoadBoard(PCB_FILE)
        if not pcbnew.ExportSpecctraDSN(board, default_dsn):
            print(f"ERROR: DSN export failed — check PCB file and path.")
            sys.exit(1)
        print(f"DSN exported to {default_dsn}")
        run(default_dsn, default_ses)
        print(f"\nNext: open KiCad PCB Editor → File → Import → Specctra Session")
        print(f"      and select: {default_ses}")
    elif len(sys.argv) < 3:
        print("Usage:")
        print("  python run_freerouting.py                    # auto-export DSN + route")
        print("  python run_freerouting.py <dsn> <ses> [mp]  # explicit paths")
        sys.exit(1)
    else:
        dsn = sys.argv[1]
        ses = sys.argv[2]
        mp  = int(sys.argv[3]) if len(sys.argv) > 3 else MAX_PASSES
        run(dsn, ses, mp)
