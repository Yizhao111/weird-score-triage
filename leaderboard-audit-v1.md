---
name: leaderboard-audit
description: Fetch the Harbor leaderboard from Supabase and surface anomalous scores — inversions of expected model/agent capability rankings, near-zero or negative outliers, and systematic harness failures. Run any time new eval results are published.
---

# /leaderboard-audit — Harbor Benchmark Anomaly Detection

Fetch the latest leaderboard data and identify scores that violate known capability rankings or show systematic failures. Produce a structured report for the qual team.

This skill only examines the three tracked model families: OpenAI (`gpt-5.4 > gpt-5-mini > gpt-5-nano`), Anthropic (`claude-opus-4-6 > claude-sonnet-4-6 > claude-haiku-4-5-20251001`), and Google (`gemini-3.1-pro-preview > gemini-3-flash-preview`). All other models in the leaderboard are filtered out.

Arguments (optional): `$ARGUMENTS`
- Pass a benchmark name (e.g. `usaco`) to focus the report on one benchmark.
- Pass `all` or leave empty for the full cross-benchmark report.

---

## Capability priors

Use these as ground truth for "expected" ordering. A score that inverts these rankings is an anomaly candidate. 

**Model tiers (strongest → weakest within family):**
- OpenAI: gpt-5.4 > gpt-5-mini > gpt-5-nano
- Anthropic: claude-opus-4-6 > claude-sonnet-4-6 > claude-haiku-4-5-20251001
- Google: gemini-3.1-pro-preview > gemini-3-flash-preview

**Agent harness tiers (strongest → weakest):**
- codex ≈ claude-code > gemini-cli > terminus-2

A weaker model outscoring a stronger model on the same agent, or a weaker agent outscoring a stronger agent on the same model, is a flag — unless the gap is small (< 3pp) and within noise.

---

## Prerequisite — Clone the Harbor repos

Clone (or pull) **both** repos before running any analysis. They provide different reference data:

**1. Harbor framework** — adapter READMEs and parity experiment results:
```bash
if [ -d /tmp/harbor ]; then
  git -C /tmp/harbor pull --quiet
else
  git clone --depth 1 https://github.com/harbor-framework/harbor /tmp/harbor
fi
echo "Harbor repo ready at /tmp/harbor"
ls /tmp/harbor/adapters/
```

**2. Harbor mix analyzer** — historical benchmark results over time per model/agent:
```bash
if [ -d /tmp/harbor-mix ]; then
  git -C /tmp/harbor-mix pull --quiet
else
  git clone --depth 1 --branch pipeline \
    https://github.com/XiangningLin/habor-mix-analyzer /tmp/harbor-mix
fi
echo "Mix analyzer ready at /tmp/harbor-mix"
ls /tmp/harbor-mix/benchmark_info_jobs/
```

If either clone fails, report it and note which steps will be skipped — the analysis can still run without them, but confirmation and historical checks will be unavailable.

---

## Automation shortcut

This repository includes `generate_leaderboard_audit.py`, which implements the report-building portions of this workflow. After fetching `/tmp/leaderboard.json` and preparing `/tmp/harbor` plus `/tmp/harbor-mix`, run:

```bash
python3 generate_leaderboard_audit.py
```

The script writes the required `.json`, `.md`, and interactive `.html` artifacts under `~/harbor-audits/`.

---


## Step 1 — Fetch leaderboard data

Fetch the current leaderboard slice used for this audit. By default this command uses a 3-trial minimum so it can analyze a broad set of active model/agent combinations.

```bash
curl 'https://hnkceovsiaczvcwhdlkb.supabase.co/rest/v1/rpc/get_leaderboard_task' \
  --compressed -s -X POST \
  -H 'apikey: sb_publishable_kpc09uUk5qcIzVex3NWGAg_y5W7jr6t' \
  -H 'Authorization: Bearer sb_publishable_kpc09uUk5qcIzVex3NWGAg_y5W7jr6t' \
  -H 'Content-Type: application/json' \
  -H 'Referer: https://harborsubabase.vercel.app/' \
  --data-raw '{"p_min_trials":3,"p_window":3}' \
| jq '[.[] | select(.model | IN(
    "gpt-5.4","gpt-5-mini","gpt-5-nano",
    "claude-opus-4-6","claude-sonnet-4-6","claude-haiku-4-5-20251001",
    "gemini-3.1-pro-preview","gemini-3-flash-preview"
  ))]' \
> /tmp/leaderboard.json
echo "Rows: $(python3 -c "import json; d=json.load(open('/tmp/leaderboard.json')); print(len(d))")"
```

### Important: Match the website's displayed trial set

The leaderboard UI is the source of truth for which trials contributed to a visible score. Use the aggregate RPCs for score detection, then use `get_cell_trials` for trajectory inspection. **Do not query the `trial` table directly and treat those rows as evidence**; direct table queries include rows that the website excludes.

Website trial semantics, as documented in `/leaderboard` and implemented by `/trial_view`:

- `get_leaderboard` and `get_leaderboard_task` provide the displayed aggregate cells.
- `get_cell_trials` returns the exact trial IDs shown when a score cell is clicked.
- A displayed/valid trial is either a clean run with `reward IS NOT NULL` and `exception_info IS NULL`, or a tolerated terminal exception such as `RewardFileNotFoundError`, `AgentTimeoutError`, or `VerifierTimeoutError`.
- Excluded infra failures such as `NonZeroAgentExitCodeError`, rate limits, billing errors, and cancellation errors must not be used to explain a displayed score unless they also appear in `get_cell_trials`.
- Tolerated failures are scored at the benchmark-specific floor used by the leaderboard RPC. Do not hard-code floor semantics unless the UI or RPC output confirms them for the current benchmark.
- In the default `= 3 trials` mode, the visible score is the average over the latest 3 displayed trials for the cell; `/trial_view` may show up to 5.


---

## Step 2 — Run the analysis

Execute the following Python script via Bash. It runs the anomaly detection on the fetched leaderboard dataset and saves the aggregated scores for later steps.

```bash
python3 - "$ARGUMENTS" << 'PYEOF'
import json, sys
from collections import defaultdict

FOCUS = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""

def analyze(path):
    with open(path) as f:
        data = json.load(f)

    if FOCUS and FOCUS != "all":
        data = [d for d in data if d['benchmark'].lower() == FOCUS]

    combo_scores = defaultdict(list)
    combo_stds   = defaultdict(list)
    task_combos  = defaultdict(list)  # (bench, task_name) -> [(model, agent, score)]

    for d in data:
        key = (d['benchmark'], d['model'], d['agent'])
        combo_scores[key].append(d['score'])
        combo_stds[key].append(d.get('score_std', 0))
        task_combos[(d['benchmark'], d['task_name'])].append(
            (d['model'], d['agent'], d['score'])
        )

    agg, agg_std = {}, {}
    for key, scores in combo_scores.items():
        agg[key] = round(sum(scores) / len(scores), 4)
        s = combo_stds[key]
        agg_std[key] = round(sum(s) / len(s), 4)

    return agg, agg_std, task_combos

MODEL_TIERS = {
    'gpt-5.4': 3, 'gpt-5-mini': 2, 'gpt-5-nano': 1,
    'claude-opus-4-6': 3, 'claude-sonnet-4-6': 2, 'claude-haiku-4-5-20251001': 1,
    'gemini-3.1-pro-preview': 2, 'gemini-3-flash-preview': 1,
}
FRONTIER_MODELS = {
    'openai': 'gpt-5.4',
    'anthropic': 'claude-opus-4-6',
    'google': 'gemini-3.1-pro-preview',
}
AGENT_TIERS = {
    'codex': 3, 'claude-code': 3, 'gemini-cli': 2, 'terminus-2': 1,
}
NATIVE_AGENT = {'gpt': 'codex', 'claude': 'claude-code', 'gemini': 'gemini-cli'}

# These benchmarks use non-[0,1] metrics, so absolute normalized thresholds
# such as near-zero, floor, saturation, and negative-score checks do not apply.
# Keep this list explicit so new score scales are reviewed deliberately.
UNBOUNDED_SCORE_BENCHMARKS = {
    'algotune',  # optimization ratios/objective values can exceed 1 by orders of magnitude
    'mlgym',    # cumulative rewards can be negative or far above 1
    'sldbench', # clipped R² ranges below 0 and up to 1
}

def uses_bounded_0_1_scores(bench, scores):
    if bench.lower() in UNBOUNDED_SCORE_BENCHMARKS:
        return False
    return bool(scores) and all(0.0 <= s <= 1.0 for s in scores)

def model_family(m):
    if m.startswith('gpt'): return 'openai'
    if m.startswith('claude'): return 'anthropic'
    if m.startswith('gemini'): return 'google'
    return None

def native_agent(m):
    for prefix, agent in NATIVE_AGENT.items():
        if m.startswith(prefix): return agent
    return None

def stronger_model(m1, m2):
    if model_family(m1) != model_family(m2): return None
    t1, t2 = MODEL_TIERS.get(m1), MODEL_TIERS.get(m2)
    if t1 is None or t2 is None: return None
    return t1 > t2

def stronger_agent(a1, a2):
    t1, t2 = AGENT_TIERS.get(a1), AGENT_TIERS.get(a2)
    if t1 is None or t2 is None: return None
    return t1 > t2

def stronger_weaker_model_pairs(models):
    for stronger in models:
        for weaker in models:
            if stronger == weaker:
                continue
            if stronger_model(stronger, weaker):
                yield stronger, weaker

def stronger_weaker_agent_pairs(agents):
    for stronger in agents:
        for weaker in agents:
            if stronger == weaker:
                continue
            if stronger_agent(stronger, weaker):
                yield stronger, weaker

def flag_anomalies(agg, agg_std, task_combos):
    anomalies  = defaultdict(list)
    near_zeros = defaultdict(list)   # bench -> [(model, agent, score, agent_median)]
    zero_tasks = defaultdict(list)   # bench -> [task_name, ...]
    stats = {}
    benchmarks = sorted(set(k[0] for k in agg))

    for bench in benchmarks:
        bench_data = {(m, a): s for (b, m, a), s in agg.items() if b == bench}
        bench_std  = {(m, a): s for (b, m, a), s in agg_std.items() if b == bench}
        stats[bench] = bench_data
        models = sorted(set(m for (m, a) in bench_data))
        agents = sorted(set(a for (m, a) in bench_data))
        all_scores = list(bench_data.values())
        bounded_0_1 = uses_bounded_0_1_scores(bench, all_scores)

        # 1. Negative scores. Only anomalous for normalized [0,1] metrics.
        if bounded_0_1:
            for (m, a), s in bench_data.items():
                if s < -0.05:
                    anomalies[bench].append(f"NEGATIVE SCORE  {m}/{a} = {s:.3f}")

        # 2. Near-zero when others on same agent are not — collected separately.
        # This threshold is only meaningful for normalized [0,1] metrics.
        if bounded_0_1:
            for agent in agents:
                agent_scores = {m: bench_data[(m, a)] for (m, a) in bench_data if a == agent}
                if not agent_scores: continue
                median = sorted(agent_scores.values())[len(agent_scores)//2]
                for m, s in agent_scores.items():
                    if s < 0.05 and median > 0.3:
                        near_zeros[bench].append((m, agent, s, median))

        # 3. Within-family model inversions >5pp
        for agent in agents:
            for stronger, weaker in stronger_weaker_model_pairs(models):
                s1, s2 = bench_data.get((stronger, agent)), bench_data.get((weaker, agent))
                if s1 is None or s2 is None: continue
                if s1 < s2 - 0.05:
                    anomalies[bench].append(
                        f"MODEL INVERSION {stronger}/{agent} = {s1:.3f}  <  {weaker}/{agent} = {s2:.3f}")

        # 4. Agent inversions >5pp
        for model in models:
            for stronger, weaker in stronger_weaker_agent_pairs(agents):
                s1, s2 = bench_data.get((model, stronger)), bench_data.get((model, weaker))
                if s1 is None or s2 is None: continue
                if s1 < s2 - 0.05:
                    anomalies[bench].append(
                        f"AGENT INVERSION {model}/{stronger} = {s1:.3f}  <  {model}/{weaker} = {s2:.3f}")

        # 5. Systematic terminus-2 collapse. Uses normalized near-zero thresholds.
        if bounded_0_1:
            t2_scores = {m: bench_data.get((m, 'terminus-2')) for m in models}
            other_scores = {}
            for m in models:
                others = [bench_data.get((m, a)) for a in agents if a != 'terminus-2' and bench_data.get((m, a)) is not None]
                if others: other_scores[m] = max(others)
            collapsed = [m for m in models if t2_scores.get(m) is not None and other_scores.get(m) is not None
                         and t2_scores[m] < 0.05 and other_scores[m] > 0.30]
            if len(collapsed) >= 3:
                anomalies[bench].append(f"TERMINUS-2 COLLAPSE  {len(collapsed)} models: {collapsed}")

        # 6. High score variance using score_std
        for (m, a), std in bench_std.items():
            score = bench_data.get((m, a), 0)
            if score > 0.1 and std / score > 0.5:
                anomalies[bench].append(
                    f"HIGH VARIANCE   {m}/{a}  mean={score:.3f}  std={std:.3f}  (std/mean={std/score:.2f})")
            if std > score > 0.05:
                anomalies[bench].append(
                    f"STD > MEAN      {m}/{a}  mean={score:.3f}  std={std:.3f}  (unreliable)")

        # 7. Exact-zero task clusters — collected separately
        bench_tasks = {tk: combos for (b, tk), combos in task_combos.items() if b == bench}
        broken = [tk for tk, combos in bench_tasks.items()
                  if combos and all(sc == 0.0 for _, _, sc in combos)]
        if broken:
            zero_tasks[bench].extend(broken)

        # 8. Benchmark saturation / floor / score compression.
        # Saturation/floor thresholds assume normalized [0,1] metrics.
        if bounded_0_1:
            max_s, min_s = max(all_scores), min(all_scores)
            if min_s > 0.95:
                anomalies[bench].append(
                    f"SATURATED       all combos >{min_s:.2f} — benchmark not discriminative")
            if max_s < 0.15:
                anomalies[bench].append(
                    f"FLOOR           all combos <{max_s:.2f} — benchmark possibly broken or too hard")
            if max_s - min_s < 0.03 and len(all_scores) >= 4:
                anomalies[bench].append(
                    f"COMPRESSED      score range only {(max_s-min_s)*100:.1f}pp "
                    f"({min_s:.3f}–{max_s:.3f}) — no discriminative signal")

        # 9. Native agent underperformance (>10pp below a non-native agent)
        for model in models:
            nat = native_agent(model)
            if nat is None or nat not in agents: continue
            nat_score = bench_data.get((model, nat))
            if nat_score is None: continue
            for agent in agents:
                if agent == nat: continue
                other = bench_data.get((model, agent))
                if other is not None and other > nat_score + 0.10:
                    anomalies[bench].append(
                        f"NATIVE UNDERPERF {model}/{nat} = {nat_score:.3f}  <<  "
                        f"{model}/{agent} = {other:.3f}")

        # 10. Cross-family inversions (lower-tier model beats another family's frontier model by >15pp)
        frontier_best = {}
        for family, frontier_model in FRONTIER_MODELS.items():
            best = max((bench_data.get((frontier_model, a), -1) for a in agents), default=-1)
            if best > 0:
                frontier_best[family] = (frontier_model, best)
        for (m, a), s in bench_data.items():
            fam = model_family(m)
            if fam is None or MODEL_TIERS.get(m, 99) > 1:
                continue
            for frontier_family, (frontier_model, frontier_score) in frontier_best.items():
                if frontier_family == fam:
                    continue
                if s > frontier_score + 0.15:
                    anomalies[bench].append(
                        f"CROSS-FAMILY INV {m}/{a} = {s:.3f}  >>  "
                        f"{frontier_model} best={frontier_score:.3f} "
                        f"(+{(s-frontier_score)*100:.0f}pp)")

    return anomalies, near_zeros, zero_tasks, stats, benchmarks

def cross_benchmark_checks(agg):
    print(f"\n{'='*70}")
    print(f"  CROSS-BENCHMARK MODEL RANKING STABILITY")
    print(f"{'='*70}")
    benchmarks = sorted(set(k[0] for k in agg))
    models = sorted(set(k[1] for k in agg))
    agents = sorted(set(k[2] for k in agg))
    inversion_counts = defaultdict(int)
    bench_counts     = defaultdict(int)
    for bench in benchmarks:
        bench_data = {(m, a): s for (b, m, a), s in agg.items() if b == bench}
        for agent in agents:
            for stronger, weaker in stronger_weaker_model_pairs(models):
                s1, s2 = bench_data.get((stronger, agent)), bench_data.get((weaker, agent))
                if s1 is None or s2 is None: continue
                bench_counts[(stronger, weaker, agent)] += 1
                if s1 < s2 - 0.05:
                    inversion_counts[(stronger, weaker, agent)] += 1
    unstable = [(pair, inv, bench_counts[pair])
                for pair, inv in inversion_counts.items()
                if bench_counts[pair] >= 3 and inv / bench_counts[pair] > 0.30]
    unstable.sort(key=lambda x: -x[1] / x[2])
    if unstable:
        print(f"\n  Pairs inverted on >30% of shared benchmarks (min 3):")
        for (m1, m2, agent), inv, total in unstable:
            print(f"  ⚠  {m1} < {m2} on {agent}: {inv}/{total} benchmarks ({inv/total*100:.0f}%)")
    else:
        print(f"\n  No unstable model rankings detected across benchmarks.")

def print_report(label, anomalies, near_zeros, zero_tasks, stats, benchmarks):
    total_flags = (sum(len(v) for v in anomalies.values()) +
                   sum(len(v) for v in near_zeros.values()) +
                   sum(len(v) for v in zero_tasks.values()))
    print(f"\n{'='*70}")
    print(f"  {label}  ({len(benchmarks)} benchmarks, {total_flags} flags)")
    print(f"{'='*70}")

    # ── Section A: Exact-zero task clusters ──────────────────────────────────
    if zero_tasks:
        print(f"\n{'█'*60}")
        print(f"  ⛔  EXACT-ZERO TASK CLUSTERS  (environment / setup bugs)")
        print(f"{'█'*60}")
        print(f"  Tasks where EVERY model+agent scored 0.0 — likely broken env,")
        print(f"  missing file, or inaccessible resource. Fix before re-running.\n")
        for bench in sorted(zero_tasks):
            tasks = zero_tasks[bench]
            print(f"  [{bench}]  {len(tasks)} broken task(s):")
            for t in sorted(tasks):
                print(f"    ✗  {t}")
        print()

    # ── Section B: Near-zero outliers ────────────────────────────────────────
    if near_zeros:
        print(f"\n{'█'*60}")
        print(f"  🟡  NEAR-ZERO OUTLIERS  (score < 5% while agent median > 30%)")
        print(f"{'█'*60}")
        print(f"  These combos are near-zero while peers on the same agent are not.")
        print(f"  Likely harness/compat failures, not capability gaps.\n")
        for bench in sorted(near_zeros):
            entries = near_zeros[bench]
            print(f"  [{bench}]")
            for (m, agent, s, median) in sorted(entries, key=lambda x: x[2]):
                print(f"    ✗  {m:38s} / {agent:15s}  score={s:.3f}  (agent median={median:.3f})")
        print()

    # ── Section C: All other anomalies per benchmark ─────────────────────────
    flagged = [b for b in benchmarks if anomalies[b]]
    clean   = [b for b in benchmarks if not anomalies[b] and b not in near_zeros and b not in zero_tasks]
    if flagged:
        print(f"{'─'*60}")
        print(f"  OTHER ANOMALIES BY BENCHMARK")
        print(f"{'─'*60}")
    for bench in flagged:
        bench_data = stats[bench]
        print(f"\n  [{bench}]  {len(anomalies[bench])} flag(s)")
        for f in sorted(anomalies[bench]):
            print(f"    ⚠  {f}")
        ranked = sorted(bench_data.items(), key=lambda x: -x[1])
        print(f"    Top 5:")
        for (m, a), s in ranked[:5]:
            print(f"      {m:38s} {a:15s}  {s:.3f}")
        if len(ranked) > 8:
            print(f"    Bottom 3:")
            for (m, a), s in ranked[-3:]:
                print(f"      {m:38s} {a:15s}  {s:.3f}")

    print(f"\n  Clean benchmarks: {', '.join(clean) or 'none'}\n")
    return flagged

# ── Run on fetched dataset ────────────────────────────────────────────────────
agg, std, tasks = analyze('/tmp/leaderboard.json')

an, nz, zt, st, bm = flag_anomalies(agg, std, tasks)

flagged = print_report("LEADERBOARD ANALYSIS", an, nz, zt, st, bm)

cross_benchmark_checks(agg)

# ── Save for later steps ──────────────────────────────────────────────────────
import json as _json
_json.dump({str(k): v for k, v in agg.items()}, open('/tmp/agg.json', 'w'))
PYEOF
```

---

## Step 2b — Historical trend check (harbor-mix-analyzer)

For each flagged benchmark, compare the current aggregated scores against the historical `results_over_time` data from the mix analyzer repo. This catches regressions that only became visible over multiple runs.

Important: `results_over_time` is not guaranteed to have a single schema. In current mix-analyzer files it may be:
- a dict keyed by `"model/agent"` or similar, where each value is a numeric series or scalar
- a list of dated snapshots like `{"date": ..., "results": [...]}`, where each snapshot contains per-model score records

Normalize the history first; do not assume `.get("model/agent")` will work on every file.

```bash
python3 << 'PYEOF'
import json, os
from collections import defaultdict

agg = {eval(k): v for k, v in json.load(open('/tmp/agg.json')).items()}
benchmarks = sorted(set(k[0] for k in agg))
jobs_dir = '/tmp/harbor-mix/benchmark_info_jobs'

if not os.path.isdir(jobs_dir):
    print("harbor-mix-analyzer not cloned — skipping historical check")
    exit()

print(f"\n{'='*70}")
print(f"  HISTORICAL TREND CHECK (results_over_time)")
print(f"{'='*70}")

def norm(s):
    return s.lower().replace('-','').replace('_','').replace(' ','')

all_json_files = [f for f in os.listdir(jobs_dir) if f.endswith('.json')]
all_stems = {f: norm(f[:-5]) for f in all_json_files}  # filename -> normalized stem

def find_best_match(bench):
    bench_n = norm(bench)
    # 1. exact normalized match
    for f, stem in all_stems.items():
        if stem == bench_n:
            return f, 1.0
    # 2. substring either direction
    substr_hits = []
    for f, stem in all_stems.items():
        if bench_n in stem or stem in bench_n:
            # score = overlap length / max length (longer overlap = better)
            overlap = len(set(bench_n) & set(stem))
            score = len(min(bench_n, stem, key=len)) / max(len(bench_n), len(stem))
            substr_hits.append((score, f))
    if substr_hits:
        return sorted(substr_hits, reverse=True)[0][1], sorted(substr_hits, reverse=True)[0][0]
    # 3. difflib sequence similarity as last resort
    from difflib import SequenceMatcher
    scored = [(SequenceMatcher(None, bench_n, stem).ratio(), f)
              for f, stem in all_stems.items()]
    best_score, best_file = max(scored)
    if best_score >= 0.6:
        return best_file, best_score
    return None, 0.0

for bench in benchmarks:
    best_file, confidence = find_best_match(bench)
    if best_file is None:
        print(f"\n  {bench}: no historical file found in benchmark_info_jobs/")
        continue
    if confidence < 1.0:
        print(f"\n  {bench}: fuzzy-matched to {best_file} (confidence={confidence:.2f}) — verify this is correct")

    hist_path = os.path.join(jobs_dir, best_file)
    try:
        hist = json.load(open(hist_path))
    except Exception as e:
        print(f"\n  {bench}: failed to parse {best_file} — {e}")
        continue

    results_over_time = hist.get('results_over_time', {})
    if not results_over_time:
        print(f"\n  {bench}: no results_over_time key in {best_file}")
        continue

    def score_from_result_entry(result_entry):
        """Extract a comparable scalar score from one historical result entry."""
        scores = result_entry.get('scores') or []
        if not scores:
            return None

        preferred_metrics = [
            'accuracy_overall',
            'accuracy',
            'score',
            'resolved_rate',
            'pass_rate',
            'success_rate',
        ]
        for metric in preferred_metrics:
            for item in scores:
                if item.get('metric') == metric and isinstance(item.get('value'), (int, float)):
                    return item['value']

        numeric_scores = [item.get('value') for item in scores if isinstance(item.get('value'), (int, float))]
        return numeric_scores[0] if numeric_scores else None

    def normalize_model_name(name):
        return (name or '').strip().lower()

    def normalize_agent_name(name):
        return (name or '').strip().lower()

    def collect_history_series(results_over_time):
        """
        Return {(model, agent): [historical_score, ...]} across supported schemas.
        Supports:
        1. dict keyed by 'model/agent' -> list|scalar
        2. list of dated snapshots with `results: [...]`
        """
        series = defaultdict(list)

        if isinstance(results_over_time, dict):
            for key, value in results_over_time.items():
                if '/' not in key:
                    continue
                model, agent = key.split('/', 1)
                if isinstance(value, list):
                    vals = [x for x in value if isinstance(x, (int, float))]
                    if vals:
                        series[(model, agent)].extend(vals)
                elif isinstance(value, (int, float)):
                    series[(model, agent)].append(value)
            return series

        if isinstance(results_over_time, list):
            dated_rows = sorted(
                [row for row in results_over_time if isinstance(row, dict)],
                key=lambda row: row.get('date', '')
            )
            for row in dated_rows:
                for result in row.get('results', []):
                    model = result.get('model')
                    agent = (
                        result.get('agent')
                        or result.get('system_description')
                        or result.get('system')
                    )
                    score = score_from_result_entry(result)
                    if not model or not agent or score is None:
                        continue
                    series[(model, agent)].append(score)
            return series

        return series

    hist_series_map = collect_history_series(results_over_time)

    print(f"\n{'─'*60}")
    print(f"  {bench}  (history from {best_file})")
    print(f"{'─'*60}")

    # Compare each (model, agent) combo in the live data against normalized history
    bench_live = {(m, a): s for (b, m, a), s in agg.items() if b == bench}
    matched_any = False
    for (model, agent), live_score in sorted(bench_live.items(), key=lambda x: -x[1]):
        hist_series = None
        for (hist_model, hist_agent), values in hist_series_map.items():
            if (
                normalize_model_name(hist_model) == normalize_model_name(model)
                and normalize_agent_name(hist_agent) == normalize_agent_name(agent)
            ):
                hist_series = values
                break
        if not hist_series:
            continue
        matched_any = True
        hist_latest = hist_series[-1]
        hist_mean   = sum(hist_series) / len(hist_series)
        delta = live_score - hist_latest
        flag = ""
        if delta < -0.10:
            flag = "  ← REGRESSION"
        elif delta > 0.10:
            flag = "  ← INFLATION"
        print(f"  {model:38s} {agent:15s}  live={live_score:.3f}  hist={hist_latest:.3f}  Δ={delta:+.3f}{flag}")
    if not matched_any:
        print("  No directly comparable model/agent history found after schema normalization.")
PYEOF
```

**How to read the output:**
- `REGRESSION` (Δ < −10pp) — live score dropped significantly from historical baseline; likely a harness change or env regression
- `INFLATION` (Δ > +10pp) — live score jumped; could be task leakage, scoring change, or genuine improvement
- Small Δ (±5pp) — consistent with historical trend, anomaly from Step 2 is probably benchmark-fit not a bug


---

## Step 2c — Inspect displayed trial trajectories for flagged cells

Run this only for cells you are about to diagnose. It downloads the same trials the website shows when a user clicks a task score cell. This is the correct path for root-cause evidence.

Use the exact `(benchmark, task_name, model, agent)` from Step 2. For a benchmark-level anomaly, start with the task rows that actually drive the aggregate: low scores, high variance, or model/agent inversions.

```bash
BENCHMARK='<benchmark>' \
TASK_NAME='<task_name>' \
MODEL='<model>' \
AGENT='<agent>' \
python3 - << 'PYEOF'
import json, os, re, tarfile, urllib.request
from pathlib import Path

SUPABASE_URL = 'https://hnkceovsiaczvcwhdlkb.supabase.co'
SUPABASE_KEY = 'sb_publishable_kpc09uUk5qcIzVex3NWGAg_y5W7jr6t'

required = ['BENCHMARK', 'TASK_NAME', 'MODEL', 'AGENT']
missing = [name for name in required if not os.environ.get(name)]
if missing:
    raise SystemExit(f"Missing required env var(s): {', '.join(missing)}")

BENCHMARK = os.environ['BENCHMARK']
TASK_NAME = os.environ['TASK_NAME']
MODEL = os.environ['MODEL']
AGENT = os.environ['AGENT']
OUT = Path('/tmp/harbor-cell-trials') / BENCHMARK / TASK_NAME / MODEL / AGENT
OUT.mkdir(parents=True, exist_ok=True)

def rpc(name, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f'{SUPABASE_URL}/rest/v1/rpc/{name}',
        data=data,
        method='POST',
        headers={
            'apikey': SUPABASE_KEY,
            'Authorization': f'Bearer {SUPABASE_KEY}',
            'Content-Type': 'application/json',
            'Referer': 'https://harborsubabase.vercel.app/',
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def read_member(tf, suffixes):
    if isinstance(suffixes, str):
        suffixes = [suffixes]
    for member in tf.getmembers():
        if any(member.name.endswith(suffix) for suffix in suffixes):
            f = tf.extractfile(member)
            return f.read().decode('utf-8', 'replace') if f else ''
    return ''

trials = rpc('get_cell_trials', {
    'p_benchmark': BENCHMARK,
    'p_task_name': TASK_NAME,
    'p_model': MODEL,
    'p_agent': AGENT,
})
(OUT / 'displayed_trials.json').write_text(json.dumps(trials, indent=2))
print(f'Displayed trials: {len(trials)}')

for trial in trials:
    trial_id = trial['trial_id']
    tgz_path = OUT / f'{trial_id}.tar.gz'
    if not tgz_path.exists():
        urllib.request.urlretrieve(trial['trial_uri'], tgz_path)

    exception_type = (trial.get('exception_info') or {}).get('exception_type') or 'OK'
    cause = []
    try:
        with tarfile.open(tgz_path, 'r:gz') as tf:
            trajectory = read_member(tf, '/agent/trajectory.json')
            agent_log = read_member(tf, [
                '/agent/claude-code.txt',
                '/agent/codex.txt',
                '/agent/gemini-cli.txt',
            ])
            verifier = read_member(tf, '/verifier/test-stdout.txt')
            exception_txt = read_member(tf, '/exception.txt')

            if 'Credit balance is too low' in agent_log or 'Credit balance is too low' in trajectory:
                cause.append('credit_balance_low')
            if exception_type == 'AgentTimeoutError' or 'Agent execution timed out' in exception_txt:
                cause.append('agent_timeout')
            if 'No such file or directory' in verifier:
                cause.append('missing_required_output')
            if 'Evaluation crashed:' in verifier:
                m = re.search(r'Evaluation crashed: (.*)', verifier)
                cause.append('verifier_crash:' + (m.group(1)[:120] if m else 'unknown'))
            if 'Metric Error:' in verifier:
                m = re.search(r'Metric Error: (.*)', verifier)
                cause.append('metric_error:' + (m.group(1)[:120] if m else 'unknown'))
            if trajectory:
                try:
                    steps = len(json.loads(trajectory).get('steps', []))
                except Exception:
                    steps = 'parse_error'
            else:
                steps = 'missing'
    except Exception as e:
        cause.append('tar_read_error:' + str(e))
        steps = 'unknown'

    if not cause:
        cause.append('valid_run' if exception_type == 'OK' else 'unclassified_failure')
    print(
        f"rank={trial['trial_rank']} id={trial_id} reward={trial.get('reward')} "
        f"exception={exception_type} steps={steps} cause={';'.join(cause)}"
    )
PYEOF
```

Interpretation rules:

- If `get_cell_trials` does not return a trial, do not use it as evidence for the visible score.
- If the verifier reports a missing required submission artifact such as `/app/answer.txt`, `answer.txt`, or another benchmark-required output file, classify the trial as `missing_submission_artifact` first.
- If `agent_log` contains a plausible final answer or clear task work product, but the verifier reports the required output file is missing, treat the failure as an `Agent Execution Issue` or `Model-Agent Compatibility Issue`, not a `Model Behavior Issue`.
- If the displayed trials are dominated by `AgentTimeoutError` or `VerifierTimeoutError`, classify the cell as `timeout_or_budget_issue` first. Do not treat it as a wrong-answer failure unless the completed non-timeout trials also show incorrect outputs.
- Tolerated timeout rows can legitimately contribute the benchmark floor to the displayed score. Inspect the trajectory tail before calling them model failures; common patterns include long-running optimization, repeated retries, or failure to write required output before timeout.
- If the logs show `RESOURCE_EXHAUSTED`, quota, billing, or rate-limit messages, record `rate_limit_noise` separately. Treat rate-limit evidence as explanatory only when it appears in the displayed trials for the cell; do not use raw trial-table noise as evidence.
- Billing, rate-limit, cancellation, and nonzero-agent-exit rows found via direct table queries are excluded from the website score unless `get_cell_trials` returns them.
- Only classify a cell as a `Model Behavior Issue` when the run completes normally, the required submission artifact is present or not needed, and the produced content is actually wrong or judged incorrect.
- If a metric permits negative values, do not treat negative reward alone as a scoring bug. Pair the score with verifier output, agent log, adapter README, and parity evidence before assigning root cause.
- Keep these buckets separate in notes and final reporting: `missing_submission_artifact`, `timeout_or_budget_issue`, `rate_limit_noise`, `true_wrong_answer`.

---

## Step 3 — Confirm with current numbers (in-depth per flagged benchmark)

For each benchmark flagged in Step 2, cross-reference the live leaderboard scores against the Harbor adapter's ground-truth parity data. This step catches cases where a score looks anomalous but is actually expected given the benchmark's design, or conversely where parity data confirms a real regression.

### 3a — Locate the adapter

The Harbor repo organizes benchmarks under `adapters/`. Find the folder whose name matches or closely matches the benchmark name (e.g. `usaco`, `labbench`, `mmmlu`):

```bash
# List all adapter folders — use this to find the right match
ls /tmp/harbor/adapters/

# For a specific benchmark (replace <benchmark> with the actual name)
ls /tmp/harbor/adapters/<benchmark>/
```

If no exact match exists, look for partial matches (e.g. `research-code-bench` might be `research_code_bench` or `researchcodebench`). If no adapter folder exists for a flagged benchmark, note it and skip to Step 4.

### 3b — Read the adapter README

Read the benchmark's `README.md` for:
- what the benchmark actually measures
- the score range and whether negative scores are valid
- any known limitations or caveats about specific model/agent combinations
- expected baseline performance ranges

```bash
cat /tmp/harbor/adapters/<benchmark>/README.md
```

Use the README to validate or invalidate anomaly hypotheses from Step 2. For example:
- if the README says the scoring formula is `correct − 0.25 × wrong`, negative scores are expected for random guessing — **not a bug**
- if the README documents a known timeout issue with verbose models or agents, use that to contextualize timeout-heavy cells
- if the README lists required environment files, cross-check against the near-zero task clusters

### 3c — Read parity_experiment.json

Read the parity results file for ground-truth reference scores:

```bash
cat /tmp/harbor/adapters/<benchmark>/parity_experiment.json
```

Before comparing parity to live leaderboard results, verify that the parity run is actually comparable. Only treat parity as a direct baseline when it is reasonably aligned on:
- the same benchmark or benchmark slice
- the same agent, or a clearly equivalent agent mode
- the same model, or at least the same model family and intended comparison target
- the same task variant, dataset split, or evaluation setting when that distinction matters

If parity only covers a different agent, a different model family, a different slice, or a different evaluation mode, use it only as context — not as a direct pass/fail baseline.

Compare the parity scores against the live leaderboard aggregates only for reasonably comparable `(model, agent)` setups. Flag any of the following:

| Situation | What it means |
|---|---|
| Live score matches parity ± 5pp | Score is consistent — anomaly may be benchmark-fit, not a bug |
| Live score is much lower than parity | Regression since parity was established — likely harness or env change |
| Live score is much higher than parity | Possible scoring inflation or task leakage |
| Model/agent combo missing from parity | No baseline to compare — flag as unverified |
| Parity file is absent | Adapter has no reference run — note this explicitly |

For each flagged benchmark, produce a confirmation block:

```
#### <benchmark> — Parity Check

- Parity score (model/agent): <value from parity_experiment.json>
- Live leaderboard score: <value from Step 2>
- Delta: <live − parity>
- README insight: <one sentence from README that is relevant>
- Verdict: CONFIRMED ANOMALY / EXPECTED BEHAVIOR / REGRESSION / UNVERIFIED
```

### 3d — Map experiment ownership

Use `/Users/han/Workplace/weird-score-triage/experiment-track.csv` to attach experiment ownership metadata to each flagged benchmark. The CSV is keyed by `Adapter Name`; use the `People` column to show who runs the benchmark experiment.

Normalize names before matching:
- lowercase
- remove spaces, hyphens, underscores, and punctuation
- allow aliases such as `bfcl` ↔ `Berkeley Function Calling Leaderboard (BFCL)`, `swtbench` ↔ `SWT Bench`, `research-code-bench` ↔ `reaserchcodebench`, and `spreadsheetbench-verified` ↔ `SpreadsheetBench`

For each finding, add an `experiment_owner` object:

```json
{
  "adapter_name": "<Adapter Name from CSV>",
  "people": "<People from CSV>"
}
```

If no CSV row matches a benchmark, still include `experiment_owner` with empty strings so the HTML can show that ownership is unknown rather than silently omitting the field.

---

## Step 4 — Interpret and summarize

After Steps 2 and 3, synthesize all findings into a structured report.

### Step 4a — Build structured findings first

Before writing any prose, create a JSON-serializable object called `report_data`. Treat this as the source of truth for all later artifacts.

Required shape:

```json
{
  "meta": {
    "date": "<YYYY-MM-DD>",
    "scope": "<one-sentence description of the analyzed leaderboard slice>",
    "benchmarks_seen": 0,
    "benchmarks_flagged": 0,
    "clean_benchmarks": []
  },
  "summary": {
    "headline_findings": [
      "<short sentence>",
      "<short sentence>"
    ],
      "analysis_notes": [
        "<short note about data quality, coverage, or confidence>"
      ]
  },
  "findings": [
    {
      "benchmark": "<name>",
      "priority": 1,
      "root_cause": "Scoring or Verifier Issue",
      "parity_verdict": "CONFIRMED ANOMALY",
      "historical_trend": "REGRESSION",
      "experiment_owner": {
        "adapter_name": "<Adapter Name from experiment-track.csv>",
        "people": "<person or owner from experiment-track.csv>"
      },
      "anomaly": "<one sentence>",
      "parity": "<one sentence>",
      "historical": "<one sentence>",
      "recommended_action": "<one sentence>",
      "tags": ["<short-tag>", "<short-tag>"]
    }
  ],
  "action_queue": {
    "Scoring or Verifier Issue": ["<item>", "<item>"],
    "Task Or Environment Issue": ["<item>", "<item>"],
    "Agent Execution Issue": ["<item>", "<item>"],
    "Model-Agent Compatibility Issue": ["<item>", "<item>"],
    "Model Behavior Issue": ["<item>", "<item>"],
    "Needs More Investigation": ["<item>", "<item>"]
  }
}
```

Rules for `report_data`:
- Every flagged benchmark must become exactly one `findings[]` entry.
- `priority` must be a small integer (`1`, `2`, or `3`) so downstream HTML can sort and filter cleanly.
- `root_cause` must use exactly one of the allowed categories below.
- `parity_verdict` must be one of: `CONFIRMED ANOMALY`, `EXPECTED BEHAVIOR`, `REGRESSION`, `UNVERIFIED`, `NEEDS_INVESTIGATION`.
- `historical_trend` should be a short label such as `REGRESSION`, `INFLATION`, `STABLE`, or `MIXED`.
- `experiment_owner` should be populated from `experiment-track.csv`; use empty strings when no row matches.
- `tags` should be short machine-friendly strings, not prose sentences.
- If parity or history is missing or ambiguous, say so explicitly in the relevant string field rather than omitting it.

### Step 4b — Interpret each flagged benchmark

For each flagged benchmark in `report_data.findings`:

1. **State the anomaly** in one sentence.
2. **Hypothesize the root cause** using these categories:
   - `Model Behavior Issue` — the model behaved unexpectedly (e.g. perfectionist loop, wrong MC answers)
   - `Agent Execution Issue` — harness/agent is broken for this benchmark (e.g. terminus-2 env setup missing)
   - `Model-Agent Compatibility Issue` — specific model+agent combination fails (e.g. gpt-5-nano API format incompatible with codex)
   - `Scoring or Verifier Issue` — negative or nonsensical scores suggest verifier or scoring logic issues
   - `Task Or Environment Issue` — environment setup issue for specific task IDs
   - `Needs More Investigation` — insufficient evidence to classify
3. **Recommend action**: re-run trajectories, fix env setup, audit scoring script, etc.

Group findings by root cause category at the end for a prioritized action list. Use `report_data.action_queue` to hold those grouped items.

---

## Output format

Build the report in memory from `report_data`, then write files at the end.

Artifacts:
- `leaderboard-audit-<timestamp>.json` — the structured `report_data` object
- `leaderboard-audit-<timestamp>.md` — the human-readable markdown report
- `leaderboard-audit-<timestamp>.html` — interactive HTML artifact

The report structure:

```
## Leaderboard Anomaly Report — <datetime>
### Trend Summary (use very intuitive language with evidence)
- <agent-harness trend>; examples include <benchmark A> (`<agent1 score> < <agent2 score>`), <benchmark B> (`...`), and <benchmark C> (`...`).
- <model-order trend>; examples include <benchmark A> (`<weaker model score> > <stronger model score>`), <benchmark B> (`...`), and <benchmark C> (`...`).
- <scoring/verifier trend>; examples include <benchmark A> (`<score>`), <benchmark B> (`...`), and <benchmark C> (`...`).
- <task-level trend>; examples include <benchmark A> (`<N exact-zero tasks>`), <benchmark B> (`...`), and <benchmark C> (`...`).
- `Root-cause mix`: <CATEGORY1>=<count>, <CATEGORY2>=<count>, <CATEGORY3>=<count>, <CATEGORY4>=<count>, <CATEGORY5>=<count>.
- `Confidence caveat`: <short note about parity/history limitations>.

Example:
- `gemini-cli` systematically underperforms `terminus-2` for `gemini-3-flash-preview`; examples include `gpqa-diamond` (`0.704 < 0.896`), `lawbench` (`0.542 < 0.648`), and `widesearch` (`0.536 < 0.641`).
- `gemini-3-flash-preview` outperforms `gemini-3.1-pro-preview` on some benchmarks; examples include `codepde (0.200 > 0.133)`, `mmmlu (0.151 > 0.069)`, and `swtbench (0.560 > 0.487)`.
- `xxx` benchmark show lots of rate limit issue for `yyy` model


### Flagged Benchmarks (<N>)

#### <benchmark>
- **Experiment owner**: <People from experiment-track.csv> — <Adapter Name>
- **Anomaly**: <one sentence>
- **Parity verdict**: CONFIRMED ANOMALY / EXPECTED BEHAVIOR / REGRESSION / UNVERIFIED — <delta vs parity_experiment.json>
- **Historical trend**: REGRESSION / INFLATION / STABLE — <delta vs results_over_time>
- **Root cause hypothesis**: <category> — <reasoning>
- **Recommended action**: <what to do>

...

### Action Priority Queue

**Agent Execution Issue / Model-Agent Compatibility Issue** (harness fixes, highest leverage):
- ...

**Task Or Environment Issue** (benchmark/task setup fixes):
- ...

**Model Behavior Issue** (needs trajectory review):
- ...

**Scoring or Verifier Issue** (needs verifier or scoring audit):
- ...

**Needs More Investigation**:
- ...

### Near-Zero Outliers
- <benchmark>: <model/agent/task examples> — likely agent execution or compatibility issue

### Exact-Zero Task Clusters
- <benchmark>: <task names / count> — likely task or environment issue


### Clean Benchmarks
<list>
```

Markdown generation rules:
- Write markdown from `report_data`; do not treat the markdown as the source of truth.
- Keep the markdown concise and decision-oriented.
- Preserve the exact benchmark names used in `report_data.findings`.

HTML generation modes:
- `markdown_only` — write only `.json` and `.md`
- `simple_html` — write `.json`, `.md`, and a lightweight static `.html` wrapper around the report content
- `interactive_html` — write `.json`, `.md`, and a standalone interactive `.html` file rendered from embedded `report_data`

If HTML is requested, choose the mode that best matches the user request:
- If the user just says “HTML”, default to `simple_html`
- If the user asks for an interactive artifact, dashboard, filters, or richer UI, use `interactive_html`

Requirements for `interactive_html`:
- single self-contained file with embedded CSS and JS
- no network dependencies
- render from `report_data`, not by reparsing markdown prose
- include benchmark cards or rows as the primary unit of display
- include search
- include filters for root cause, verdict, and priority if those fields are populated
- include sorting by priority or benchmark name
- include collapsible or expandable detail sections
- include a separate action-queue section
- remain readable on desktop and mobile

Presentation guidance for `interactive_html`:
- favor clarity over decoration
- use the structured fields directly rather than inventing new labels
- color/severity mapping should be deterministic:
  - `Scoring or Verifier Issue` => highest severity
  - `Task Or Environment Issue` / `Agent Execution Issue` => high severity
  - `Model-Agent Compatibility Issue` => medium severity
  - `Model Behavior Issue` => medium severity
  - `Needs More Investigation` => lower severity
  - `EXPECTED BEHAVIOR` => informational
- avoid building a framework app unless the user explicitly asks for one

---

## Step 5 — Write output files

After composing `report_data` and the markdown report in Step 4, write the artifacts to disk.

Default behavior:
- always write `.json`
- always write `.md`
- always write `.html`, use an interactive HTML (use `interactive_html`)

HTML template:
- Use `templates/leaderboard-audit-interactive.html` as the reference renderer.
- The template lives at `/Users/han/Workplace/weird-score-triage/templates/leaderboard-audit-interactive.html`.
- Replace the `%%REPORT_DATA_JSON%%` placeholder with serialized `report_data` JSON. Do not reparse markdown prose to build the HTML.

```bash
mkdir -p ~/harbor-audits
timestamp="$(date +%Y-%m-%d_%H%M)"
json_path=~/harbor-audits/leaderboard-audit-"$timestamp".json
md_path=~/harbor-audits/leaderboard-audit-"$timestamp".md
html_path=~/harbor-audits/leaderboard-audit-"$timestamp".html
html_template=/Users/han/Workplace/weird-score-triage/templates/leaderboard-audit-interactive.html

# Write `report_data` to "$json_path".
# Write the final markdown report to "$md_path".
# Write the interactive HTML report to "$html_path" by loading "$html_template"
# and replacing %%REPORT_DATA_JSON%% with json.dumps(report_data).
```
