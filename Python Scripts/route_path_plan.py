#!/usr/bin/env python3
"""
route_path_plan.py -- Phase 10 Script 1.6

Pure analysis: reads the board, detects HS differential pair crossing topology,
and writes two output files to cfg.REPORTS_DIR:
  routing_plan.json        -- machine-readable hints for route_highspeed.py
  routing_plan_report.txt  -- human-readable findings and corrective actions

No component moves. No board writes. No board.Save().

Usage:
    python route_path_plan.py          # full analysis + write outputs
    python route_path_plan.py --quiet  # suppress console output
"""

import sys
import os
import math
import json
import datetime
import pathlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import routing_config as cfg

KICAD_BIN = str(pathlib.Path(cfg.KICAD_SITE_PKGS).parent.parent)
os.environ["PATH"] = KICAD_BIN + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, cfg.KICAD_SITE_PKGS)
import pcbnew

QUIET = "--quiet" in sys.argv

# Allow overriding the output directory for testing
_reports_dir_override = None
for _arg in sys.argv:
    if _arg.startswith("--reports-dir="):
        _reports_dir_override = _arg.split("=", 1)[1]

# Match route_highspeed.py constants exactly
CLEARANCE_MM         = 0.15
CROSS_Y_THRESHOLD    = 15.0   # mm: max y-centroid spread between peers for crossing detection

ca        = cfg.CLEARANCE_AUDIT
VIA_PAD   = ca["hs_via_drill_mm"] + 2 * ca["hs_via_annular_ring_mm"]
CORRECTION_PITCH_THRESHOLD = VIA_PAD + 2 * CLEARANCE_MM  # ~0.85mm


# ---------------------------------------------------------------------------
# Board load
# ---------------------------------------------------------------------------

board = pcbnew.LoadBoard(cfg.PCB_FILE)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def tomm(iu):
    return pcbnew.ToMM(iu)

def dist(x1, y1, x2, y2):
    return math.hypot(x2 - x1, y2 - y1)

def unit_vec(dx, dy):
    mag = math.hypot(dx, dy)
    if mag < 1e-9:
        return 0.0, 0.0
    return dx / mag, dy / mag


# ---------------------------------------------------------------------------
# Pad collection (identical to route_highspeed.py)
# ---------------------------------------------------------------------------

def get_pads_on_net(net_name, skip_inline=True):
    """Return all endpoint pads on net_name.

    skip_inline=True: exclude footprints with more than one pad on this net
    (inline ESD arrays, ferrite beads, etc.) -- same filter as route_highspeed.py.
    """
    result = []
    for fp in board.GetFootprints():
        fp_net_pads = [p for p in fp.Pads() if p.GetNetname() == net_name]
        if skip_inline and len(fp_net_pads) > 1:
            continue
        for pad in fp_net_pads:
            pos = pad.GetPosition()
            result.append({
                "ref":       fp.GetReference(),
                "x":         tomm(pos.x),
                "y":         tomm(pos.y),
                "layer":     pad.GetLayer(),
                "locked":    fp.IsLocked(),
                "orient_deg": fp.GetOrientationDegrees(),
            })
    return result


# ---------------------------------------------------------------------------
# Pair matching and chaining (identical to route_highspeed.py)
# ---------------------------------------------------------------------------

def pair_pads(pads_p, pads_n):
    """Match each P pad with its closest N pad (greedy nearest-neighbor)."""
    remaining_n = list(pads_n)
    pairs = []
    for p in pads_p:
        if not remaining_n:
            break
        closest = min(remaining_n, key=lambda n: dist(p["x"], p["y"], n["x"], n["y"]))
        pairs.append((p, closest))
        remaining_n.remove(closest)
    return pairs


def chain_pairs(pairs):
    """Order (P, N) pairs by nearest-neighbor on centroid, topmost first."""
    if len(pairs) <= 1:
        return list(pairs)

    def centroid(pair):
        p, n = pair
        return ((p["x"] + n["x"]) / 2, (p["y"] + n["y"]) / 2)

    remaining = list(pairs)
    chain = [min(remaining, key=lambda pr: centroid(pr)[1])]
    remaining.remove(chain[0])
    while remaining:
        cx, cy = centroid(chain[-1])
        nearest = min(remaining, key=lambda pr: dist(cx, cy, *centroid(pr)))
        chain.append(nearest)
        remaining.remove(nearest)
    return chain


# ---------------------------------------------------------------------------
# Pair centroid index -- identical build loop to route_highspeed.py
# Maps pair_name -> (src_cx, dst_cx, src_cy, dst_cy)
# ---------------------------------------------------------------------------

pair_centroid_x = {}
for _pname, (_pnet_p, _pnet_n, _player, _) in cfg.HS_PAIRS.items():
    if _pname not in cfg.HS_ROUTE_WIDTHS:
        continue
    _pp = get_pads_on_net(_pnet_p)
    _pn = get_pads_on_net(_pnet_n)
    _ch = chain_pairs(pair_pads(_pp, _pn))
    if len(_ch) < 2:
        continue
    _sp, _sn = _ch[0]
    _dp, _dn = _ch[-1]
    pair_centroid_x[_pname] = (
        (_sp["x"] + _sn["x"]) / 2,   # src_cx
        (_dp["x"] + _dn["x"]) / 2,   # dst_cx
        (_sp["y"] + _sn["y"]) / 2,   # src_cy
        (_dp["y"] + _dn["y"]) / 2,   # dst_cy
    )


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def perp_pitch(p_pad, n_pad, perp_ux, perp_uy):
    """Perpendicular separation between P and N pad centers."""
    p_proj = p_pad["x"] * perp_ux + p_pad["y"] * perp_uy
    n_proj = n_pad["x"] * perp_ux + n_pad["y"] * perp_uy
    return abs(p_proj - n_proj)


def p_sign(p_pad, n_pad, perp_ux, perp_uy):
    """Sign of P's position relative to pair centroid along the perpendicular axis.
    +1 = P is in the positive perp direction relative to centroid.
    -1 = P is in the negative perp direction.
    """
    cx = (p_pad["x"] + n_pad["x"]) / 2
    cy = (p_pad["y"] + n_pad["y"]) / 2
    proj = (p_pad["x"] - cx) * perp_ux + (p_pad["y"] - cy) * perp_uy
    return 1.0 if proj >= 0 else -1.0


# ---------------------------------------------------------------------------
# Per-pair analysis
# ---------------------------------------------------------------------------

def analyze_pair(pair_name):
    """Analyze one HS pair. Returns (result_dict, error_string).
    error_string is None on success.
    """
    net_p, net_n, layer_name, _ = cfg.HS_PAIRS[pair_name]

    pads_p = get_pads_on_net(net_p)
    pads_n = get_pads_on_net(net_n)

    if not pads_p:
        return None, f"no pads found for {net_p}"
    if not pads_n:
        return None, f"no pads found for {net_n}"

    pairs = chain_pairs(pair_pads(pads_p, pads_n))
    if len(pairs) < 2:
        return None, "fewer than 2 pad pairs -- may be single-component or unconnected"

    src_p_pad, src_n_pad = pairs[0]
    dst_p_pad, dst_n_pad = pairs[-1]

    # Axis: source centroid -> destination centroid
    src_cx = (src_p_pad["x"] + src_n_pad["x"]) / 2
    src_cy = (src_p_pad["y"] + src_n_pad["y"]) / 2
    dst_cx = (dst_p_pad["x"] + dst_n_pad["x"]) / 2
    dst_cy = (dst_p_pad["y"] + dst_n_pad["y"]) / 2
    axis_ux, axis_uy = unit_vec(dst_cx - src_cx, dst_cy - src_cy)
    perp_ux, perp_uy = -axis_uy, axis_ux

    src_p_sign = p_sign(src_p_pad, src_n_pad, perp_ux, perp_uy)
    dst_p_sign = p_sign(dst_p_pad, dst_n_pad, perp_ux, perp_uy)
    src_pitch  = perp_pitch(src_p_pad, src_n_pad, perp_ux, perp_uy)
    dst_pitch  = perp_pitch(dst_p_pad, dst_n_pad, perp_ux, perp_uy)

    # Rank-based crossing detection -- identical to route_highspeed.py
    cur_sy = pair_centroid_x.get(pair_name, (0, 0, 0, 0))[2]
    cur_dy = pair_centroid_x.get(pair_name, (0, 0, 0, 0))[3]
    layer_peers = [
        (n, s, d)
        for n, (s, d, sy, dy) in pair_centroid_x.items()
        if cfg.HS_PAIRS.get(n, (None, None, None, None))[2] == layer_name
        and abs(sy - cur_sy) < CROSS_Y_THRESHOLD
        and abs(dy - cur_dy) < CROSS_Y_THRESHOLD
    ]

    is_crossing = False
    rank_change = 0
    n_peers = len(layer_peers)
    if n_peers >= 2 and pair_name in pair_centroid_x:
        src_sorted = sorted(layer_peers, key=lambda x: x[1])
        dst_sorted = sorted(layer_peers, key=lambda x: x[2])
        src_rank   = {n: i for i, (n, _, _) in enumerate(src_sorted)}
        dst_rank   = {n: i for i, (n, _, _) in enumerate(dst_sorted)}
        rank_change = dst_rank.get(pair_name, 0) - src_rank.get(pair_name, 0)
        is_crossing = rank_change < 0

    # Correction endpoint: prefer src; use dst if src pitch is too tight
    correction_endpoint = None
    correction_signal   = None
    if is_crossing:
        correction_endpoint = "dst" if src_pitch < CORRECTION_PITCH_THRESHOLD else "src"
        correction_signal   = "P"

    # Rotation suggestion
    suggested_action = None
    action_notes     = []
    if is_crossing:
        ep = correction_endpoint
        ref_p   = (dst_p_pad if ep == "dst" else src_p_pad)["ref"]
        ref_n   = (dst_n_pad if ep == "dst" else src_n_pad)["ref"]
        locked  = (dst_p_pad if ep == "dst" else src_p_pad)["locked"]
        orient  = (dst_p_pad if ep == "dst" else src_p_pad)["orient_deg"]

        if ref_p == ref_n:
            sym = cfg.ROTATION_SYMMETRY.get(ref_p, "unknown")
            suggested_action = f"Rotate {ref_p} by 180deg"
            if locked:
                action_notes.append(
                    f"{ref_p} is currently LOCKED in KiCad -- unlock it before rotating"
                )
            if sym == "none":
                action_notes.append(
                    f"{ref_p} has asymmetric pads (ROTATION_SYMMETRY='none') -- "
                    f"verify electrical connections carefully after rotation"
                )
            elif sym == "unknown":
                action_notes.append(
                    f"{ref_p} is not listed in ROTATION_SYMMETRY -- "
                    f"verify pad symmetry before rotating"
                )
        else:
            # P and N are on different components at this endpoint
            refs = sorted({ref_p, ref_n})
            suggested_action = (
                f"Swap positions of {refs[0]} and {refs[1]}, or reposition them "
                f"so the P-net component is on the same side as it is at the opposite endpoint"
            )
            action_notes.append(
                "P and N pads are on separate components -- a 180deg rotation of one "
                "component alone will not resolve the crossing; repositioning is needed"
            )
            for ref in refs:
                if cfg.ROTATION_SYMMETRY.get(ref) == "none":
                    action_notes.append(
                        f"{ref} has asymmetric pads -- do not rotate it"
                    )

    # Warnings
    warnings = []
    if src_pitch < CORRECTION_PITCH_THRESHOLD:
        warnings.append(
            f"src pad pitch {src_pitch:.3f}mm < {CORRECTION_PITCH_THRESHOLD:.3f}mm -- "
            f"too tight for a via crossing escape at the source endpoint"
        )
    if dst_pitch < CORRECTION_PITCH_THRESHOLD:
        warnings.append(
            f"dst pad pitch {dst_pitch:.3f}mm < {CORRECTION_PITCH_THRESHOLD:.3f}mm -- "
            f"too tight for a via crossing escape at the destination endpoint"
        )

    status = "CROSSING_DETECTED" if is_crossing else "OK"

    def ep_dict(p_pad, n_pad):
        return {
            "ref_p":    p_pad["ref"],
            "x_p":      round(p_pad["x"], 3),
            "y_p":      round(p_pad["y"], 3),
            "orient_p": p_pad["orient_deg"],
            "locked_p": p_pad["locked"],
            "ref_n":    n_pad["ref"],
            "x_n":      round(n_pad["x"], 3),
            "y_n":      round(n_pad["y"], 3),
            "orient_n": n_pad["orient_deg"],
            "locked_n": n_pad["locked"],
        }

    return {
        "net_p":               net_p,
        "net_n":               net_n,
        "layer":               layer_name,
        "crossing":            is_crossing,
        "rank_change":         rank_change,
        "n_peers":             n_peers,
        "src_endpoint":        ep_dict(src_p_pad, src_n_pad),
        "dst_endpoint":        ep_dict(dst_p_pad, dst_n_pad),
        "axis_ux":             round(axis_ux, 4),
        "axis_uy":             round(axis_uy, 4),
        "perp_ux":             round(perp_ux, 4),
        "perp_uy":             round(perp_uy, 4),
        "src_p_sign":          src_p_sign,
        "dst_p_sign":          dst_p_sign,
        "src_perp_pitch_mm":   round(src_pitch, 3),
        "dst_perp_pitch_mm":   round(dst_pitch, 3),
        "correction_endpoint": correction_endpoint,
        "correction_signal":   correction_signal,
        "suggested_action":    suggested_action,
        "action_notes":        action_notes,
        "warnings":            warnings,
        "status":              status,
    }, None


# ---------------------------------------------------------------------------
# Human-readable report
# ---------------------------------------------------------------------------

def _side_label(sign):
    return "RIGHT" if sign > 0 else "LEFT"

def _locked_tag(locked):
    return "  [LOCKED]" if locked else ""

def _pitch_tag(pitch):
    if pitch < CORRECTION_PITCH_THRESHOLD:
        return f"  <- TIGHT (< {CORRECTION_PITCH_THRESHOLD:.3f}mm threshold -- via escape risky here)"
    return "  <- OK"

def format_report(results, skipped):
    lines = []
    W = 72

    def rule(ch="-"):
        return ch * W

    lines += [
        rule("="),
        "route_path_plan.py -- HS Pair Routing Analysis Report",
        f"Generated : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Board     : {cfg.PCB_FILE}",
        rule("="),
        "",
    ]

    crossings = [(n, r) for n, r in results.items() if r["crossing"]]
    ok_pairs  = [(n, r) for n, r in results.items() if not r["crossing"]]

    lines.append(
        f"SUMMARY: {len(results)} pairs analyzed  |  "
        f"{len(crossings)} crossing(s) detected  |  "
        f"{len(ok_pairs)} OK  |  "
        f"{len(skipped)} skipped"
    )
    lines.append("")

    # ── Crossing pairs ────────────────────────────────────────────────────────
    if crossings:
        lines += [rule(), "CROSSING PAIRS -- ACTION REQUIRED", rule(), ""]

    for pname, r in crossings:
        se = r["src_endpoint"]
        de = r["dst_endpoint"]

        lines += [
            f"PAIR: {pname}",
            f"  Nets  : {r['net_p']} / {r['net_n']}",
            f"  Layer : {r['layer']}",
            f"  Status: CROSSING DETECTED",
            "",
            "  Source endpoint (topmost / first in chain):",
            f"    P pad: {se['ref_p']:12s}  at ({se['x_p']:8.3f}, {se['y_p']:8.3f})  "
            f"orientation: {se['orient_p']:6.1f}deg{_locked_tag(se['locked_p'])}",
            f"    N pad: {se['ref_n']:12s}  at ({se['x_n']:8.3f}, {se['y_n']:8.3f})  "
            f"orientation: {se['orient_n']:6.1f}deg{_locked_tag(se['locked_n'])}",
            f"    Perpendicular pad pitch: {r['src_perp_pitch_mm']:.3f}mm"
            + _pitch_tag(r["src_perp_pitch_mm"]),
            "",
            "  Destination endpoint (bottommost / last in chain):",
            f"    P pad: {de['ref_p']:12s}  at ({de['x_p']:8.3f}, {de['y_p']:8.3f})  "
            f"orientation: {de['orient_p']:6.1f}deg{_locked_tag(de['locked_p'])}",
            f"    N pad: {de['ref_n']:12s}  at ({de['x_n']:8.3f}, {de['y_n']:8.3f})  "
            f"orientation: {de['orient_n']:6.1f}deg{_locked_tag(de['locked_n'])}",
            f"    Perpendicular pad pitch: {r['dst_perp_pitch_mm']:.3f}mm"
            + _pitch_tag(r["dst_perp_pitch_mm"]),
            "",
        ]

        src_side = _side_label(r["src_p_sign"])
        dst_side = _side_label(r["dst_p_sign"])
        lines += [
            f"  What is wrong:",
            f"    Looking along the routing direction (source -> destination),",
            f"    {r['net_p']} (P) exits the source on the {src_side} but arrives at",
            f"    the destination on the {dst_side}. P and N swap sides mid-route.",
            f"    Rank among {r['n_peers']} layer peers changes by {r['rank_change']} "
            f"(negative = crossing required).",
            "",
            f"  Correction endpoint: {r['correction_endpoint'].upper()} -- routing script "
            + ("will use dst (src pitch too tight for via escape)"
               if r["correction_endpoint"] == "dst"
               else "will use src (preferred; sufficient pitch)"),
            "",
        ]

        if r["suggested_action"]:
            lines.append(f"  SUGGESTED ACTION: {r['suggested_action']}")
            for note in r["action_notes"]:
                lines.append(f"    -> {note}")
            lines.append(
                f"    -> After rotating, re-run this script to confirm the crossing is resolved."
            )

        for w in r["warnings"]:
            lines.append(f"  WARNING: {w}")

        lines.append("")

    # ── OK pairs ─────────────────────────────────────────────────────────────
    if ok_pairs:
        lines += [rule(), "OK PAIRS -- No crossing detected", rule(), ""]
        for pname, r in ok_pairs:
            se = r["src_endpoint"]
            de = r["dst_endpoint"]
            lines.append(
                f"  {pname:15s}  {r['net_p']:22s} / {r['net_n']:22s}  {r['layer']:6s}"
            )
            lines.append(
                f"    src: {se['ref_p']}/{se['ref_n']}  ->  dst: {de['ref_p']}/{de['ref_n']}  "
                f"pitch src={r['src_perp_pitch_mm']:.3f}mm dst={r['dst_perp_pitch_mm']:.3f}mm"
            )
            for w in r["warnings"]:
                lines.append(f"    WARNING: {w}")
        lines.append("")

    # ── Skipped pairs ─────────────────────────────────────────────────────────
    if skipped:
        lines += [rule(), "SKIPPED PAIRS", rule(), ""]
        for pname, reason in skipped.items():
            lines.append(f"  {pname}: {reason}")
        lines.append("")

    lines += [rule("="), "End of report", rule("=")]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON output (for route_highspeed.py)
# ---------------------------------------------------------------------------

def build_json(results, skipped):
    n_crossing = sum(1 for r in results.values() if r["crossing"])
    pairs_json = {}
    for pname, r in results.items():
        pairs_json[pname] = {
            "net_p":               r["net_p"],
            "net_n":               r["net_n"],
            "layer":               r["layer"],
            "crossing":            r["crossing"],
            "src_endpoint":        {"ref_p": r["src_endpoint"]["ref_p"],
                                    "ref_n": r["src_endpoint"]["ref_n"]},
            "dst_endpoint":        {"ref_p": r["dst_endpoint"]["ref_p"],
                                    "ref_n": r["dst_endpoint"]["ref_n"]},
            "axis_ux":             r["axis_ux"],
            "axis_uy":             r["axis_uy"],
            "perp_ux":             r["perp_ux"],
            "perp_uy":             r["perp_uy"],
            "src_p_sign":          r["src_p_sign"],
            "dst_p_sign":          r["dst_p_sign"],
            "src_perp_pitch_mm":   r["src_perp_pitch_mm"],
            "dst_perp_pitch_mm":   r["dst_perp_pitch_mm"],
            "correction_endpoint": r["correction_endpoint"],
            "correction_signal":   r["correction_signal"],
            "warnings":            r["warnings"],
            "status":              r["status"],
        }
    return {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d"),
        "script":    "route_path_plan.py",
        "pairs":     pairs_json,
        "summary": {
            "total_pairs":           len(results),
            "in_scope":              len(results),
            "crossings_detected":    n_crossing,
            "crossings_correctable": sum(1 for r in results.values()
                                         if r["crossing"] and r["correction_endpoint"]),
            "warnings":              sum(len(r["warnings"]) for r in results.values()),
            "skipped":               len(skipped),
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not QUIET:
        print(f"route_path_plan.py -- analyzing {len(cfg.HS_ROUTE_WIDTHS)} pairs")
        print(f"Board: {cfg.PCB_FILE}")
        print()

    results = {}
    skipped = {}

    for pair_name in cfg.HS_ROUTE_WIDTHS:
        if pair_name not in cfg.HS_PAIRS:
            skipped[pair_name] = "listed in HS_ROUTE_WIDTHS but not in HS_PAIRS"
            continue

        result, error = analyze_pair(pair_name)
        if error:
            skipped[pair_name] = error
            if not QUIET:
                print(f"  SKIP  {pair_name}: {error}")
            continue

        results[pair_name] = result
        if not QUIET:
            tag = "CROSSING" if result["crossing"] else "OK"
            print(f"  {tag:8s} {pair_name}")
            if result["crossing"]:
                print(f"           correction at {result['correction_endpoint']} endpoint")
                if result["suggested_action"]:
                    print(f"           action: {result['suggested_action']}")

    # Write outputs
    out_dir = _reports_dir_override if _reports_dir_override else cfg.REPORTS_DIR
    os.makedirs(out_dir, exist_ok=True)

    json_path = os.path.join(out_dir, "routing_plan.json")
    with open(json_path, "w") as f:
        json.dump(build_json(results, skipped), f, indent=2)

    report_path = os.path.join(out_dir, "routing_plan_report.txt")
    with open(report_path, "w") as f:
        f.write(format_report(results, skipped))

    n_crossing = sum(1 for r in results.values() if r["crossing"])
    if not QUIET:
        print()
        print(f"routing_plan.json        -> {json_path}")
        print(f"routing_plan_report.txt  -> {report_path}")
        print()
        print(f"Result: {len(results)} pairs, {n_crossing} crossing(s), {len(skipped)} skipped")


if __name__ == "__main__":
    main()
