# KiCad PCB Automation Scripts

Generic, project-agnostic KiCad scripting toolkit. Each script has a
`PROJECT CONFIG` block at the top — fill that in for your project, then run
with KiCad's bundled Python or a compatible interpreter.

**Quick start:**
1. Open the script you need.
2. Edit the `# ── PROJECT CONFIG ──` block at the top.
3. Run: `"C:/Program Files/KiCad/10.0/bin/python.exe" scripts/<script>.py`

---

## Script Reference

| Script | Guide Phase | CONFIG items to fill in | What it does |
|--------|-------------|--------------------------|--------------|
| `assign_3d_models.py` | Phase 9 (pre-layout) | `PCB_FILE`, `KICAD_SITE_PKGS`, `MODEL_MAP` (ref → .step path) | Assigns 3-D STEP models to footprints for mechanical/render review |
| `measure_loops.py` | Phase 10 (layout) | `PCB_FILE`, `KICAD_SITE_PKGS`, `LOOP_NETS` (net pairs defining switching loops) | Measures the area of power-switching current loops and flags oversized loops |
| `verify_dfm.py` | Phase 11.5 (DFM review) | `PCB_FILE`, `REPORT_FILE`, `KICAD_SITE_PKGS`, `MIN_ANNULAR_RING_MM`, `MIN_DRILL_MM`, `MIN_CLEARANCE_MM` | Checks annular ring, drill size, trace-to-copper clearance, and silkscreen-over-pad violations |
| `validate_footprint_dims.py` | Phase 9 (footprint audit) | `PCB_FILE`, `REPORT_FILE`, `KICAD_SITE_PKGS`, `EXPECTED_DIMS` (ref → expected pad pitch / body size) | Validates that footprint pad dimensions match datasheet values |
| `trace_length_check.py` | Phase 10.7 (length matching) | `PCB_FILE`, `KICAD_SITE_PKGS`, `LENGTH_GROUPS` (group → net list), `TOLERANCE_MM` | Reports routed length for each net and intra-group length mismatch |
| `insert_meanders.py` | Phase 10.7 (length matching) | `PCB_FILE`, `KICAD_SITE_PKGS`, `GROUPS`, `COMPANIONS`, `TRACE_WIDTH`, `TOLERANCE`, `MIN_CLEARANCE` | Inserts single-sided serpentine meanders on the shorter net of each diff pair to equalise lengths |
| `insert_meanders_tight.py` | Phase 10.7 (length matching) | `PCB_FILE`, `KICAD_SITE_PKGS`, `GROUPS`, `COMPANIONS`, `TRACE_WIDTH`, `TOLERANCE`, `MIN_CLEARANCE` | Same as `insert_meanders.py` with full per-segment geometric DRC simulation (binary-search safe amplitude) for congested areas |
| `find_close_net_refs.py` | Phase 10 (routing debug) | `PCB_FILE`, `KICAD_SITE_PKGS`, `PAIRS` (group → (net_A, net_B)) | Lists footprint references and pad numbers connected to each net — useful for tracing diff-pair endpoints |
| `measure_all_diff_pairs.py` | Phase 10.7 Step 1 (length matching) | `PCB_FILE`, `KICAD_SITE_PKGS`, `GROUPS` (label, P_net, N_net per pair), `INTERLANE_SETS` (prefix, label, limit_mm per lane group), `INTRA_LIMIT_MM` | Reports P/N lengths and intra-pair skew per diff pair; inter-lane spread per lane group; skew adjustment chart with pad locators for all failing pairs |
| `verify_highspeed.py` | Phase 11 Step 3.5 (HS verification) | `PCB_FILE`, `REPORT_FILE`, `KICAD_SITE_PKGS`, `HS_LAYER_NAME`, `SKEW_FAIL_MM`, `HS_PAIRS`, `LANE_GROUPS` | Verifies HS diff pairs: correct layer, no mid-trace vias, intra-pair skew, and inter-lane spread |
| `add_return_vias.py` | Phase 11 Step 2 (return path) | `PCB_FILE`, `REPORT_FILE`, `KICAD_SITE_PKGS`, `SEARCH_RADIUS_MM`, `ADD_VIAS`, `GND_NET_NAME` | Reports signal vias lacking a nearby GND via; optionally inserts GND stitching vias (set `ADD_VIAS=True` only when board lacks solid GND planes) |
| `check_lowspeed_bus.py` | Phase 11 Step 3 (signal integrity) | `PCB_FILE`, `REPORT_FILE`, `KICAD_SITE_PKGS`, `BUS_NETS`, `LENGTH_WARN_MM`, `GUARD_DIST_MM` | Checks low-speed bus nets (I2C/SPI/UART) for excessive length and missing GND guard traces |
| `add_silkscreen.py` | Phase 11 (documentation) | `PCB_FILE`, `KICAD_SITE_PKGS`, `CONNECTOR_REF_PREFIX`, `CONNECTOR_LABELS`, `BOARD_NAME`, `COMPLIANCE_MARKS` | Hides non-connector ref/value text, adds human-readable connector labels on F.Silkscreen, adds board name and compliance marks on F.Fab |
| `check_copper_balance.py` | Phase 11.5 Check 1 (DFM) | `PCB_FILE`, `REPORT_FILE`, `KICAD_SITE_PKGS`, `TARGET_LAYERS`, `BALANCE_THRESHOLD` | Computes per-layer copper coverage and checks F.Cu/B.Cu area ratio against warping-risk threshold |
| `check_polarity_markers.py` | Phase 11.5 Check 2 (DFM) | `PCB_FILE`, `REPORT_FILE`, `KICAD_SITE_PKGS`, `SEARCH_RADIUS_MM`, `POLARIZED_FOOTPRINT_KEYWORDS` | Confirms every IC, diode, and polarized capacitor has a silkscreen or fab-layer polarity marker nearby |
| `add_coating_notes.py` | Phase 11.5 (DFM documentation) | `PCB_FILE`, `KICAD_SITE_PKGS`, `NO_COAT_REFS`, `COAT_SPEC` | Places "NO COAT" annotations near listed connectors and a conformal coating specification on F.Fab |
| `width_audit.py` | Phase 11.5 Loop D (power integrity — preferred) | `PCB_FILE`, `KICAD_SITE_PKGS`, `POWER_NETS` | Audits power net trace widths; auto-excludes intentional neck-downs near pad edges (prevents false positives on via fanouts and connector lands) |
| `verify_trace_widths.py` | Phase 11.5 Loop D (power integrity — fallback) | `PCB_FILE`, `KICAD_SITE_PKGS`, `POWER_NETS` (net → max_A, req_trunk_mm, req_branch_mm) | Audits power net trace widths against IPC-2221B; no neck-down exclusion — use if width_audit.py misclassifies segments |
| `compute_impedance.py` | Phase 5 Step 4b (compliance rules) | `H_MM`, `T_MM`, `ER` (stackup), `TARGETS` (label → Z_diff ohm), `ROUTING_LAYER_TYPE` (`"inner"` or `"outer"`) | Computes differential trace W and S in mm from IPC-2141A formulas; prints exact .kicad_dru constraint line for each impedance target |
| `lock_highspeed_nets.py` | Phase 10 (post HS routing pre-flight) | `PCB_FILE`, `KICAD_SITE_PKGS`, `HS_NETS` (all P and N net names from HS_PAIRS) | Sets KiCad LOCKED flag on every HS segment and via so FreeRouting cannot reroute manual pre-routes; reports 0-segment nets as warnings |
| `verify_hs_sandwich.py` | Phase 10 Part B + Phase 11.5 (HS verification) | `PCB_FILE`, `KICAD_SITE_PKGS`, `REPORT_FILE`, `HS_LAYER`, `GND_NET_NAME` | Verifies HS layer is sandwiched by solid GND reference planes; checks 4 rules: non-GND zones, non-GND tracks, non-GND via anti-pads, zone boundaries crossing HS channel |
| `verify_thermal.py` | Phase 9+ Loop E (thermal adequacy) | `REPORT_FILE`, `T_AMB_C`, `THERMAL_TABLE` (ref, desc, P_diss_W, T_j_max_C, n_vias, drill_mm per IC > 0.3W) | Computes required vs. achieved θ_ja per IC; reports PASS/FAIL and minimum via count needed to fix failures |
| `run_freerouting.py` | Phase 10.5 Step 3 (autorouting) | `FREEROUTING_JAR`, `TEMP_DIR`, `MAX_PASSES`, `THREADS`, `MAX_RUNTIME_SECS`, `JAVA_HEAP` | Headless FreeRouting wrapper: copies DSN to temp dir, runs java, monitors SES stability, validates result, classifies failures with actionable messages |
| `score_autoroute.py` | Phase 10.5 Step 5 (autoroute QA) | `PCB_FILE`, `KICAD_SITE_PKGS`, `VIA_DENSITY_THRESHOLD`, `LONG_ROUTE_MM`, `LAYER_IMBALANCE_PCT` | Scores autoroute quality on three metrics (via density, long routes, layer imbalance); PASS/FAIL verdict determines whether to proceed or trigger placement optimisation |
| `scatter_components.py` | Phase 8 (test scatter) | `--pcb`, `--seed`, `--margin`, `--dry-run` | Randomizes all non-locked component positions to create a fresh scatter state for testing the two-script placement pipeline. Creates a timestamped backup in `Backups/` before saving. Run with `--dry-run` to preview the scatter without writing. |
| `proximity_rules_config.py` | Phase 8 (shared config) | Imported by both placement scripts — not run directly. | Shared configuration for the two-script placement pipeline. Contains `RULES` (6-tuple proximity rules), `RULE_TYPE_PRIORITY`, `POWER_NET_EXACT`, and `is_power_net()`. Both `place_components_organized.py` and `place_by_proximity_rules.py` import from here. Edit this file to update rules; do not copy rules into either script directly. |
| `place_components_organized.py` | Phase 8 (component placement) | All parameters via CLI — no config block editing required at run time. See `Phase8_NetAwarePlacement_Insert.md` for full procedure. | Net-aware cascade placement. Treats locked footprints as fixed attractors. Uses sliding-scale courtyard gap: passives 0.025 mm, ICs 0.15 mm (override with `--courtyard-gap`), connectors 0.50 mm. Pipeline: seed → overlap → IC rotation → passive face placement → passive rotation → re-snap → overlap. Detects diff pairs and enforces min edge-to-edge separation for meander budget. **Cluster-guided seeding:** before the first force pass, run `--dump-nets -` to get the component net list, Claude assigns functional clusters, writes `clusters.json`, then force pass runs with `--clusters clusters.json` to seed each group in its correct board region. Two pass types: **force pass** (cluster seed, full cascade) and **refine pass** (`--use-current-positions --refine-only`). **clusters.json control fields:** `non_passive_overrides` (list of D-prefix IC refs to treat as non-passives, e.g. redriver ICs named D_TX1/D_AUX); `passive_side_overrides` (dict of ref→side to force a passive to a specific face of its parent in global PCB coordinates, bypassing geometry computation); `forced_rotations` (dict of ref→degrees to lock an IC rotation). Passive rotation uses parent-priority pull: the passive pulls toward its assigned parent IC only, ignoring other components on the same net that would bias the direction. RESULT line reports resolvable vs locked-locked violations separately. |
| `check_component_proximity.py` | Phase 8 | `PCB_FILE`, `REPORT_FILE`, `RULES` (from PROXIMITY_RULES_TABLE in PROJECT_PARAMS) | Schematic-aware proximity check for all rule types. Measures pad-to-pad distance on the specific `net_hint` net; falls back to min shared net (`net_hint=None`) or centroid-to-centroid when no shared net exists. Report tags: `[net:X]`, `[hint-miss:X]`, `[pad:X]`, `[ctr]`. |
| `place_by_proximity_rules.py` | Phase 8 | `PCB_FILE`, `REPORT_FILE`, `BACKUPS_DIR`, `RULES` (6-tuple: ref_a, ref_b, max_dist_mm, priority, rotation_hint, net_hint), `OVERLAP_ITERS`, `COURTYARD_GAP_MM`, `NO_FORCE_PREFIXES`, `NO_FORCE_REFS` | Sequential greedy face-group placement driven by PROXIMITY_RULES_TABLE. Locked footprints are fixed anchors. No-force refs (TP_*, MH_*) block overlap resolution but never move. Pipeline: (1) build_face_groups — satellites grouped by anchor and which face (right/left/up/down) they belong on, keyed by net_hint pad position; rules with net_hint=None skipped here (refs become NO_FACE_GROUP_REFS); (2) compute_frozen_refs — satellites where ALL rules already pass are frozen; (3) place_face_group — staircase placement: each satellite at its own face_comp (clamped to clear anchor courtyard), stacked with courtyard-based stk_half; occupied zone conflict check with face-direction depth filter (far-field components beyond group face extent excluded); group shift only allowed if shift ≤ group_extent / 2; (4) resolve_overlaps — OVERLAP_ITERS sweeps nudge remaining overlapping pairs; anchor_only_refs excluded (anchor_stable) to prevent satellite drift; NO_FACE_GROUP_REFS stay in effective_movable so they drift with their satellites as a cluster; (5) multi-pass convergence — repeat until frozen set stabilizes; (6) report — before/after distances with same method tags as check script. Default: dry-run. Pass `--live` to apply positions and save PCB (auto-backup created). |
| `check_pad_orientation.py` | Phase 10.5 Pre-flight A | `PCB_FILE`, `KICAD_SITE_PKGS`, `MIN_PADS`, `ASPECT_THRESHOLD` | Flags elongated pads whose orientation matches the footprint rotation — the most common silent FreeRouting killer |
| `check_keepouts.py` | Phase 10.5 Pre-flight B | `PCB_FILE`, `KICAD_SITE_PKGS`, `COVERAGE_THRESHOLD` | Warns if any keepout/rule-area zone covers > threshold % of the board, which would block FreeRouting from routing most connections |

---

## CONFIG block pattern

Every script follows this pattern at the top:

```python
# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
SCHEMATIC_FILE  = r"C:\path\to\project\ProjectName.kicad_sch"   # where used
REPORTS_DIR     = r"C:\path\to\project\Reports"
BACKUPS_DIR     = r"C:\path\to\project\Backups"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"
# ─────────────────────────────────────────────────────────────────────────────
```

Scripts that modify the board back it up first:
```python
import shutil, datetime
tag = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
shutil.copy(PCB_FILE, os.path.join(BACKUPS_DIR, f"backup.{tag}.bak"))
```

Scripts using pcbnew start with:
```python
import sys
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew
```

---

## Pre-flight order for autorouting

Run in this order before every FreeRouting DSN export:

1. `check_pad_orientation.py` — fix elongated pad orientation issues
2. `check_keepouts.py` — confirm no keepout covers > 70% of board
3. Run DRC in KiCad GUI — resolve all `shorting_item` violations
4. Export DSN via `pcbnew.ExportSpecctraDSN(board, dsn_path)`
5. `run_freerouting.py <input.dsn> <output.ses>`
6. Import SES via `pcbnew.ImportSpecctraSES(board, ses_path)` + save
7. `score_autoroute.py` — quality gate before proceeding
