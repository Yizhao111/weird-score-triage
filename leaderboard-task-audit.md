---
name: leaderboard-task-audit
description: Fetch the Harbor leaderboard from Supabase and surface anomalous scores — inversions of expected model/agent capability rankings, near-zero or negative outliers, and systematic harness failures. Run any time new eval results are published.
---

# /leaderboard-task-audit — Harbor Benchmark Anomaly Detection

For a particular benchmark, fetch the latest leaderboard data, download running trials tar.gz, and identify scores that violate known capability rankings or show systematic failures. Produce a structured report for the qual team.

This skill only examines the three tracked model families: OpenAI (`gpt-5.4 > gpt-5-mini > gpt-5-nano`), Anthropic (`claude-opus-4-6 > claude-sonnet-4-6 > claude-haiku-4-5-20251001`), and Google (`gemini-3.1-pro-preview > gemini-3-flash-preview`). All other models in the leaderboard are filtered out.

Must pass arguments: `$ARGUMENTS`
- Pass a benchmark name (e.g. `usaco`) to focus the report on one benchmark.

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

This repository has two separate audit generators:

- `generate_leaderboard_audit.py` — the original cross-benchmark overview audit. Do not modify this for task-level triage.
- `generate_leaderboard_task_audit.py` — the focused benchmark/task audit. Use this for single-benchmark reports, score-aggregation diagnostics, and downloaded trial archive analysis.

For the focused task audit, prepare both leaderboard RPC files and then run the task generator with the benchmark name:

```bash
python3 generate_leaderboard_task_audit.py <benchmark>
```

The task generator writes artifacts named `leaderboard-task-audit[-<benchmark>]-<timestamp>.json`, `.md`, and `.html` under `~/harbor-audits/`. When a benchmark argument is provided, the HTML is rendered as a single-benchmark report and the benchmark card is expanded by default.

The current focused report includes:
- official benchmark scores from `get_leaderboard`
- task-level diagnostics from `get_leaderboard_task`
- top 8 and bottom 8 model/agent scores
- highlighted model/agent pairs that appear in anomaly records
- parity links to `https://github.com/harbor-framework/harbor/blob/main/adapters/<adapter>/parity_experiment.json` when available
- downloaded trial archive analysis from `/tmp/harbor-cell-trials/<benchmark>/**/*.tar.gz`

The task generator filters out benchmark families that should not be audited in this workflow before processing:
- `deveval`
- `ds-1000`
- `featbench`
- `multi-swe-bench`

---


## Step 1 — Fetch task-level leaderboard data first

Fetch task-level aggregate cells from `get_leaderboard_task`, not raw trial-table rows. These rows are used for task diagnostics, suspicious-cell selection, exact-zero task clusters, and downloaded trial inspection.

Also fetch benchmark-level aggregate cells from `get_leaderboard`. These are the official leaderboard scores and must be used for model inversion, agent inversion, top/bottom score tables, parity comparison, history comparison, and root-cause classification. Do not recompute benchmark scores as a simple mean of task rows. Some benchmarks, especially AlgoTune, use non-linear aggregation: for example `get_leaderboard` can report `1.568884` while the arithmetic task mean is `442.4343`.

This step intentionally handles misspelled or stale benchmark names. For example, if a user asks for `inemath`, the live key is likely `ineqmath`; use the script's close-match output rather than guessing.

Before running the task generator, save both RPC outputs:

```bash
curl 'https://hnkceovsiaczvcwhdlkb.supabase.co/rest/v1/rpc/get_leaderboard' \
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
> /tmp/leaderboard_aggregate.json
```

Then fetch the task rows:

```bash
python3 - "$ARGUMENTS" << 'PYEOF'
import csv
import json
import re
import sys
import urllib.request
from collections import Counter
from difflib import get_close_matches
from pathlib import Path

SUPABASE_URL = "https://hnkceovsiaczvcwhdlkb.supabase.co"
SUPABASE_KEY = "sb_publishable_kpc09uUk5qcIzVex3NWGAg_y5W7jr6t"
TRACKED_MODELS = {
    "gpt-5.4", "gpt-5-mini", "gpt-5-nano",
    "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
    "gemini-3.1-pro-preview", "gemini-3-flash-preview",
}
FOCUS = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""

def norm(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())

def rpc(name, body):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/rpc/{name}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Referer": "https://harborsubabase.vercel.app/",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)

def fetch_task_rows(min_trials=3):
    rows = rpc("get_leaderboard_task", {"p_min_trials": min_trials, "p_window": 3})
    return [row for row in rows if row.get("model") in TRACKED_MODELS]

rows_all = fetch_task_rows(min_trials=3)
Path("/tmp/leaderboard.json").write_text(json.dumps(rows_all, indent=2))
benchmarks = sorted({row["benchmark"] for row in rows_all})

if not FOCUS or FOCUS == "all":
    resolved = "all"
    rows = rows_all
else:
    exact = [b for b in benchmarks if b.lower() == FOCUS or norm(b) == norm(FOCUS)]
    if not exact:
        contains = [b for b in benchmarks if norm(FOCUS) in norm(b) or norm(b) in norm(FOCUS)]
        close = get_close_matches(FOCUS, benchmarks, n=10, cutoff=0.3)
        candidates = []
        for b in contains + close:
            if b not in candidates:
                candidates.append(b)
        print(f"No exact live benchmark key for {FOCUS!r}. Close matches: {candidates}")
        if not candidates:
            # Retry with a 1-trial minimum before failing; active benchmarks can be sparse.
            rows_min1 = fetch_task_rows(min_trials=1)
            min1_benchmarks = sorted({row["benchmark"] for row in rows_min1})
            candidates = get_close_matches(FOCUS, min1_benchmarks, n=10, cutoff=0.3)
            print(f"Close matches with p_min_trials=1: {candidates}")
        if not candidates:
            raise SystemExit("No benchmark match found; verify the adapter name.")
        resolved = candidates[0]
        print(f"Using closest benchmark key: {resolved}")
    else:
        resolved = exact[0]
    rows = [row for row in rows_all if row["benchmark"] == resolved]

if FOCUS and FOCUS != "all" and not rows:
    print(f"No p_min_trials=3 rows for {resolved}; retrying p_min_trials=1.")
    rows_all = fetch_task_rows(min_trials=1)
    rows = [row for row in rows_all if row["benchmark"] == resolved]

if not rows:
    raise SystemExit(f"No task-level rows found for benchmark={resolved!r}")

safe_name = norm(resolved) or "all"
json_path = Path(f"/tmp/{safe_name}_leaderboard_task.json")
csv_path = Path(f"/tmp/{safe_name}_task_level_scores_min3.csv")
Path("/tmp/leaderboard_task_focus.json").write_text(json.dumps(rows, indent=2))
json_path.write_text(json.dumps(rows, indent=2))

fieldnames = ["benchmark", "task_name", "model", "agent", "score", "score_std", "n_trials"]
with csv_path.open("w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field) for field in fieldnames})

print(f"Resolved benchmark: {resolved}")
print(f"Rows: {len(rows)}")
print(f"Tasks: {len({row['task_name'] for row in rows})}")
print(f"Models: {sorted({row['model'] for row in rows})}")
print(f"Agents: {sorted({row['agent'] for row in rows})}")
print(f"n_trials counts: {dict(Counter(row.get('n_trials') for row in rows))}")
print(f"Saved JSON: {json_path}")
print(f"Saved CSV: {csv_path}")
PYEOF
```

### Important: Match the website's displayed trial set

The leaderboard UI is the source of truth for which trials contributed to a visible score. Use `get_leaderboard_task` for score detection, then use `get_cell_trials` only for trajectory inspection. **Do not query the `trial` table directly and treat those rows as evidence**; direct table queries include rows that the website excludes.

Website trial semantics, as documented in `/leaderboard` and implemented by `/trial_view`:

- `get_leaderboard` and `get_leaderboard_task` provide the displayed aggregate cells.
- `get_cell_trials` returns the exact trial IDs shown when a score cell is clicked.
- A displayed/valid trial is either a clean run with `reward IS NOT NULL` and `exception_info IS NULL`, or a tolerated terminal exception such as `RewardFileNotFoundError`, `AgentTimeoutError`, or `VerifierTimeoutError`.
- Excluded infra failures such as `NonZeroAgentExitCodeError`, rate limits, billing errors, and cancellation errors must not be used to explain a displayed score unless they also appear in `get_cell_trials`.
- Tolerated failures are scored at the benchmark-specific floor used by the leaderboard RPC. Do not hard-code floor semantics unless the UI or RPC output confirms them for the current benchmark.
- In the default `= 3 trials` mode, the visible score is the average over the latest 3 displayed trials for the cell; `/trial_view` may show up to 5.

---

## Step 1b — Compare cross-benchmark consistency in the same category

Before treating a suspicious score as benchmark-specific, check sibling benchmarks in the same category. If the same model/agent combo, model-order inversion, or agent-order inversion recurs across multiple sibling benchmarks, the issue may be broader than the benchmark under audit.

This script uses `/tmp/leaderboard.json` from Step 1, resolves category display names to live leaderboard keys, computes benchmark-level means from task rows, and writes `/tmp/category_consistency.json`.

```bash
python3 << 'PYEOF'
import json
import re
from collections import defaultdict
from difflib import get_close_matches
from pathlib import Path

MODEL_TIERS = {
    "gpt-5.4": 3, "gpt-5-mini": 2, "gpt-5-nano": 1,
    "claude-opus-4-6": 3, "claude-sonnet-4-6": 2, "claude-haiku-4-5-20251001": 1,
    "gemini-3.1-pro-preview": 2, "gemini-3-flash-preview": 1,
}
AGENT_TIERS = {"codex": 3, "claude-code": 3, "gemini-cli": 2, "terminus-2": 1}
CATEGORIES = {
    "Software Engineering": [
        "Compile Bench", "DevOps Gym", "SWE-Bench Pro", "SWE Verified", "SWE Lancer",
        "Feature Bench", "LiveCode Bench", "SWE-Multilingual", "SWT Bench", "BigCode Bench",
        "Aider Polyglot", "HumanEvalFix", "QuixBugs", "CRUST Bench", "USACO", "GSO", "AlgoTune",
    ],
    "Mathematics & Reasoning": ["IneqMath", "Omni Math", "Reasoning Gym", "AIME", "ARC AGI-2", "KUMO"],
    "Knowledge & Long Context": ["AA-LCR", "GPQA Diamond", "HLE", "MMMLU", "SimpleQA"],
    "Agents, Tools & Systems": [
        "GAIA", "GAIA2", "Skills Bench", "BFCL", "Terminal Bench 2.0",
        "DeepSynth", "Wide Search", "Seal-0",
    ],
    "Safety & Security": ["Cyber Gym", "Strong Reject"],
    "Professional Domains": ["CRM Arena", "Finance Agent", "Law Bench", "Spreadsheet Bench", "MedAgent Bench", "PIXTU"],
    "Data & Analytics": ["DA Code", "Spider 2", "SLD Bench", "LAB Bench", "BIX Bench", "QCircuit Bench"],
    "Scientific Research": ["Replication Bench", "SciCode", "MLGym Bench", "CodePDE", "Research Code Bench"],
    "Multimodal": ["MMAU"],
}
ALIASES = {
    "compilebench": "compilebench", "devopsgym": "devopsgym", "swebenchpro": "swebenchpro",
    "sweverified": "sweverified", "swelancer": "swelancer", "featurebench": "featurebench",
    "livecodebench": "livecodebench", "swemultilingual": "swemultilingual", "swtbench": "swtbench",
    "bigcodebench": "bigcodebench", "aiderpolyglot": "aiderpolyglot", "humanevalfix": "humanevalfix",
    "quixbugs": "quixbugs", "crustbench": "crustbench", "usaco": "usaco", "gso": "gso",
    "algotune": "algotune", "ineqmath": "ineqmath", "omnimath": "omnimath",
    "reasoninggym": "reasoninggym", "aime": "aime", "arcagi2": "arcagi2", "kumo": "kumo",
    "aalcr": "aalcr", "gpqadiamond": "gpqadiamond", "hle": "hle", "mmmlu": "mmmlu",
    "simpleqa": "simpleqa", "gaia": "gaia", "gaia2": "gaia2", "skillsbench": "skillsbench",
    "bfcl": "bfcl", "terminalbench20": "terminalbench", "deepsynth": "deepsynth",
    "widesearch": "widesearch", "seal0": "seal0", "cybergym": "cybergym",
    "strongreject": "strongreject", "crmarena": "crmarena", "financeagent": "financeagent",
    "lawbench": "lawbench", "spreadsheetbench": "spreadsheetbench", "medagentbench": "medagentbench",
    "pixtu": "pixtu", "dacode": "dacode", "spider2": "spider2", "sldbench": "sldbench",
    "labbench": "labbench", "bixbench": "bixbench", "qcircuitbench": "qcircuitbench",
    "replicationbench": "replicationbench", "scicode": "scicode", "mlgymbench": "mlgymbench",
    "codepde": "codepde", "researchcodebench": "researchcodebench", "mmau": "mmau",
}

def norm(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())

def canon(value):
    return ALIASES.get(norm(value), norm(value))

def model_family(model):
    if model.startswith("gpt"):
        return "openai"
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("gemini"):
        return "google"
    return None

def mean(values):
    return sum(values) / len(values) if values else 0.0

def resolve_benchmark(name, live_benchmarks):
    wanted = canon(name)
    by_canon = defaultdict(list)
    for bench in live_benchmarks:
        by_canon[canon(bench)].append(bench)
    if wanted in by_canon:
        return by_canon[wanted][0]
    close = get_close_matches(wanted, list(by_canon), n=1, cutoff=0.72)
    return by_canon[close[0]][0] if close else None

def aggregate(rows):
    grouped = defaultdict(list)
    stds = defaultdict(list)
    for row in rows:
        key = (row["benchmark"], row["model"], row["agent"])
        grouped[key].append(float(row["score"]))
        stds[key].append(float(row.get("score_std") or 0))
    return {
        key: {
            "mean": mean(scores),
            "task_count": len(scores),
            "negative_rate": sum(score < 0 for score in scores) / len(scores),
            "near_zero_rate": sum(score <= 0.05 for score in scores) / len(scores),
            "zero_rate": sum(score == 0 for score in scores) / len(scores),
            "mean_std": mean(stds[key]),
        }
        for key, scores in grouped.items()
    }

def model_inversions_for_benchmark(bench, bench_stats):
    out = []
    models = sorted({model for model, _ in bench_stats})
    agents = sorted({agent for _, agent in bench_stats})
    for agent in agents:
        for stronger in models:
            for weaker in models:
                if stronger == weaker or model_family(stronger) != model_family(weaker):
                    continue
                if MODEL_TIERS.get(stronger, 0) <= MODEL_TIERS.get(weaker, 0):
                    continue
                s1 = bench_stats.get((stronger, agent), {}).get("mean")
                s2 = bench_stats.get((weaker, agent), {}).get("mean")
                if s1 is None or s2 is None:
                    continue
                gap = s2 - s1
                if gap > 0.03:
                    out.append({
                        "benchmark": bench, "agent": agent,
                        "stronger_model": stronger, "stronger_score": round(s1, 4),
                        "weaker_model": weaker, "weaker_score": round(s2, 4),
                        "gap": round(gap, 4),
                    })
    return out

def agent_inversions_for_benchmark(bench, bench_stats):
    out = []
    models = sorted({model for model, _ in bench_stats})
    agents = sorted({agent for _, agent in bench_stats})
    for model in models:
        for stronger in agents:
            for weaker in agents:
                if stronger == weaker or AGENT_TIERS.get(stronger, 0) <= AGENT_TIERS.get(weaker, 0):
                    continue
                s1 = bench_stats.get((model, stronger), {}).get("mean")
                s2 = bench_stats.get((model, weaker), {}).get("mean")
                if s1 is None or s2 is None:
                    continue
                gap = s2 - s1
                if gap > 0.03:
                    out.append({
                        "benchmark": bench, "model": model,
                        "stronger_agent": stronger, "stronger_score": round(s1, 4),
                        "weaker_agent": weaker, "weaker_score": round(s2, 4),
                        "gap": round(gap, 4),
                    })
    return out

rows = json.load(open("/tmp/leaderboard.json"))
focus_rows = json.load(open("/tmp/leaderboard_task_focus.json"))
focus_benchmark = focus_rows[0]["benchmark"] if focus_rows else None
if not focus_benchmark:
    raise SystemExit("No focused benchmark rows found; run Step 1 first.")

live_benchmarks = sorted({row["benchmark"] for row in rows})
category_name = None
category_live = []
for name, display_benchmarks in CATEGORIES.items():
    resolved = [resolve_benchmark(display_name, live_benchmarks) for display_name in display_benchmarks]
    resolved = [bench for bench in resolved if bench]
    if focus_benchmark in resolved:
        category_name = name
        category_live = resolved
        break

if not category_name:
    print(f"No category mapping found for {focus_benchmark}; skipping category consistency.")
    Path("/tmp/category_consistency.json").write_text(json.dumps({
        "focus_benchmark": focus_benchmark,
        "category": None,
        "message": "No category mapping found.",
    }, indent=2))
    raise SystemExit(0)

stats = aggregate([row for row in rows if row["benchmark"] in category_live])
by_benchmark = defaultdict(dict)
for (bench, model, agent), values in stats.items():
    by_benchmark[bench][(model, agent)] = values

focus_stats = by_benchmark[focus_benchmark]
focus_low_combos = []
for (model, agent), values in focus_stats.items():
    if values["negative_rate"] >= 0.05 or values["near_zero_rate"] >= 0.15 or values["mean_std"] >= 0.5:
        focus_low_combos.append({
            "model": model, "agent": agent, "mean": round(values["mean"], 4),
            "negative_rate": round(values["negative_rate"], 4),
            "near_zero_rate": round(values["near_zero_rate"], 4),
            "mean_std": round(values["mean_std"], 4),
        })

focus_model_inversions = model_inversions_for_benchmark(focus_benchmark, focus_stats)
focus_agent_inversions = agent_inversions_for_benchmark(focus_benchmark, focus_stats)

combo_recurrence = []
for combo in focus_low_combos:
    model = combo["model"]
    agent = combo["agent"]
    siblings = []
    for bench in category_live:
        if bench == focus_benchmark:
            continue
        values = by_benchmark[bench].get((model, agent))
        if not values:
            continue
        if values["negative_rate"] >= 0.05 or values["near_zero_rate"] >= 0.15 or values["mean_std"] >= 0.5:
            siblings.append({
                "benchmark": bench,
                "mean": round(values["mean"], 4),
                "negative_rate": round(values["negative_rate"], 4),
                "near_zero_rate": round(values["near_zero_rate"], 4),
                "mean_std": round(values["mean_std"], 4),
            })
    combo_recurrence.append({**combo, "sibling_matches": siblings, "n_sibling_matches": len(siblings)})

all_model_inversions = []
all_agent_inversions = []
for bench in category_live:
    all_model_inversions.extend(model_inversions_for_benchmark(bench, by_benchmark[bench]))
    all_agent_inversions.extend(agent_inversions_for_benchmark(bench, by_benchmark[bench]))

def model_inv_key(item):
    return (item["agent"], item["stronger_model"], item["weaker_model"])

def agent_inv_key(item):
    return (item["model"], item["stronger_agent"], item["weaker_agent"])

model_inv_by_key = defaultdict(list)
for item in all_model_inversions:
    model_inv_by_key[model_inv_key(item)].append(item)
agent_inv_by_key = defaultdict(list)
for item in all_agent_inversions:
    agent_inv_by_key[agent_inv_key(item)].append(item)

model_inversion_recurrence = [
    {**item, "sibling_matches": [x for x in model_inv_by_key[model_inv_key(item)] if x["benchmark"] != focus_benchmark]}
    for item in focus_model_inversions
]
agent_inversion_recurrence = [
    {**item, "sibling_matches": [x for x in agent_inv_by_key[agent_inv_key(item)] if x["benchmark"] != focus_benchmark]}
    for item in focus_agent_inversions
]

report = {
    "focus_benchmark": focus_benchmark,
    "category": category_name,
    "category_benchmarks_found": category_live,
    "category_benchmarks_missing_from_live_slice": [
        display_name for display_name in CATEGORIES[category_name]
        if not resolve_benchmark(display_name, live_benchmarks)
    ],
    "focus_low_or_unstable_combos": combo_recurrence,
    "focus_model_inversions": model_inversion_recurrence,
    "focus_agent_inversions": agent_inversion_recurrence,
}
Path("/tmp/category_consistency.json").write_text(json.dumps(report, indent=2))

print(f"Focus benchmark: {focus_benchmark}")
print(f"Category: {category_name}")
print(f"Category live benchmarks: {category_live}")
print()
print("Recurring low/unstable model-agent combos:")
for item in sorted(combo_recurrence, key=lambda x: (-x["n_sibling_matches"], x["agent"], x["model"]))[:20]:
    print(
        f"  {item['model']}/{item['agent']} focus_mean={item['mean']:.4f} "
        f"sibling_matches={item['n_sibling_matches']}"
    )
    for match in item["sibling_matches"][:5]:
        print(
            f"    {match['benchmark']}: mean={match['mean']:.4f} "
            f"neg_rate={match['negative_rate']:.2f} near_zero_rate={match['near_zero_rate']:.2f} "
            f"mean_std={match['mean_std']:.3f}"
        )
print()
print("Recurring model inversions:")
for item in sorted(model_inversion_recurrence, key=lambda x: -len(x["sibling_matches"]))[:20]:
    print(
        f"  {item['agent']} {item['weaker_model']} > {item['stronger_model']} "
        f"focus_gap={item['gap']:.4f} sibling_matches={len(item['sibling_matches'])}"
    )
print()
print("Recurring agent inversions:")
for item in sorted(agent_inversion_recurrence, key=lambda x: -len(x["sibling_matches"]))[:20]:
    print(
        f"  {item['model']} {item['weaker_agent']} > {item['stronger_agent']} "
        f"focus_gap={item['gap']:.4f} sibling_matches={len(item['sibling_matches'])}"
    )
print()
print("Saved: /tmp/category_consistency.json")
PYEOF
```

Interpretation rules:

- A focus-only anomaly with no sibling matches is more likely benchmark-specific.
- A recurring combo anomaly across siblings points to model/agent compatibility, harness behavior, or provider/runtime issues.
- A recurring model inversion across siblings weakens the case that one benchmark is broken; it may reflect benchmark-category fit or a family-wide model regression.
- A recurring agent inversion across siblings points toward harness behavior in that category.

## Step 2 — Analyze the task-level score slice

Analyze the focused task rows before downloading any tarballs. This produces benchmark-level model/agent means, task difficulty summaries, score-shape warnings, ranking inversions, score-profile clusters, and a focused suspect-cell list for `get_cell_trials`.

```bash
python3 << 'PYEOF'
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

MODEL_TIERS = {
    "gpt-5.4": 3, "gpt-5-mini": 2, "gpt-5-nano": 1,
    "claude-opus-4-6": 3, "claude-sonnet-4-6": 2, "claude-haiku-4-5-20251001": 1,
    "gemini-3.1-pro-preview": 2, "gemini-3-flash-preview": 1,
}
AGENT_TIERS = {"codex": 3, "claude-code": 3, "gemini-cli": 2, "terminus-2": 1}

def model_family(model):
    if model.startswith("gpt"):
        return "openai"
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("gemini"):
        return "google"
    return None

def task_sort_key(task_name):
    return int(task_name) if str(task_name).isdigit() else str(task_name)

def mean(values):
    return sum(values) / len(values) if values else 0.0

def score_row(row, kind, **extra):
    out = {
        "kind": kind,
        "benchmark": row["benchmark"],
        "task_name": row["task_name"],
        "model": row["model"],
        "agent": row["agent"],
        "score": float(row["score"]),
        "score_std": float(row.get("score_std") or 0),
        "n_trials": row.get("n_trials"),
    }
    out.update(extra)
    return out

def profile_features(scores):
    return [
        mean(scores),
        statistics.pstdev(scores) if len(scores) > 1 else 0.0,
        sum(score < 0 for score in scores) / len(scores),
        sum(score <= 0.05 for score in scores) / len(scores),
        sum(score == 1 for score in scores) / len(scores),
    ]

def standardize(points):
    if not points:
        return []
    width = len(points[0])
    cols = [[point[i] for point in points] for i in range(width)]
    means_ = [mean(col) for col in cols]
    stds_ = [statistics.pstdev(col) or 1.0 for col in cols]
    return [[(point[i] - means_[i]) / stds_[i] for i in range(width)] for point in points]

def euclidean(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5

def kmeans_profiles(records, feature_key="features", k=4, iterations=50):
    if not records:
        return []
    k = max(1, min(k, len(records)))
    points = standardize([record[feature_key] for record in records])
    order = sorted(range(len(records)), key=lambda idx: (records[idx].get("mean", 0), records[idx].get("stdev", 0)))
    if k == 1:
        centroid_indices = [order[len(order) // 2]]
    else:
        centroid_indices = [order[round(i * (len(order) - 1) / (k - 1))] for i in range(k)]
    centroids = [points[idx][:] for idx in centroid_indices]
    assignments = [0] * len(points)
    for _ in range(iterations):
        changed = False
        for idx, point in enumerate(points):
            cluster = min(range(k), key=lambda cid: euclidean(point, centroids[cid]))
            if assignments[idx] != cluster:
                assignments[idx] = cluster
                changed = True
        if not changed:
            break
        for cid in range(k):
            members = [points[idx] for idx, cluster in enumerate(assignments) if cluster == cid]
            if members:
                centroids[cid] = [mean([member[i] for member in members]) for i in range(len(points[0]))]

    clusters = []
    for cid in range(k):
        members = [records[idx] for idx, cluster in enumerate(assignments) if cluster == cid]
        if not members:
            continue
        cluster_mean = mean([member["mean"] for member in members])
        cluster_stdev = mean([member.get("stdev", 0.0) for member in members])
        cluster_negative_rate = mean([member.get("negative_rate", 0.0) for member in members])
        cluster_near_zero_rate = mean([member.get("near_zero_rate", 0.0) for member in members])
        cluster_one_rate = mean([member.get("one_rate", 0.0) for member in members])
        if cluster_negative_rate >= 0.2:
            label = "negative/failure-heavy"
        elif cluster_mean <= 0.25 or cluster_near_zero_rate >= 0.5:
            label = "near-floor"
        elif cluster_stdev >= 0.35:
            label = "mixed/unstable"
        elif cluster_mean >= 0.9 and cluster_one_rate >= 0.7:
            label = "mostly-perfect"
        else:
            label = "middle-band"
        clusters.append({
            "cluster_id": cid,
            "label": label,
            "size": len(members),
            "mean": round(cluster_mean, 4),
            "stdev": round(cluster_stdev, 4),
            "negative_rate": round(cluster_negative_rate, 4),
            "near_zero_rate": round(cluster_near_zero_rate, 4),
            "one_rate": round(cluster_one_rate, 4),
            "members": sorted(members, key=lambda item: (item.get("mean", 0), item.get("name", "")))[:25],
        })
    return sorted(clusters, key=lambda item: (item["mean"], -item["stdev"], item["label"]))

def build_highlights(rows, by_combo, by_task, means):
    highlights = {
        "negative_rows": [],
        "high_trial_variance_rows": [],
        "near_zero_outlier_rows": [],
        "zero_outlier_rows": [],
        "model_inversions": [],
        "agent_inversions": [],
        "hard_tasks": [],
        "variable_tasks": [],
        "all_zero_tasks": [],
        "all_one_tasks": [],
        "score_clusters": {
            "task_clusters": [],
            "combo_clusters": [],
        },
    }

    for row in rows:
        score = float(row["score"])
        std = float(row.get("score_std") or 0)
        combo_mean = means[(row["model"], row["agent"])]
        if score < 0:
            highlights["negative_rows"].append(score_row(row, "negative-row", combo_mean=round(combo_mean, 4)))
        if std >= 1.0:
            highlights["high_trial_variance_rows"].append(score_row(row, "high-trial-variance", combo_mean=round(combo_mean, 4)))
        if score <= 0.05 and combo_mean > 0.8:
            kind = "zero-outlier" if score == 0 else "near-zero-outlier"
            record = score_row(row, kind, combo_mean=round(combo_mean, 4))
            highlights["near_zero_outlier_rows"].append(record)
            if score == 0:
                highlights["zero_outlier_rows"].append(record)

    for agent in sorted({agent for _, agent in means}):
        models = [model for model, combo_agent in means if combo_agent == agent]
        for stronger in models:
            for weaker in models:
                if stronger == weaker:
                    continue
                if model_family(stronger) != model_family(weaker):
                    continue
                if MODEL_TIERS.get(stronger, 0) <= MODEL_TIERS.get(weaker, 0):
                    continue
                gap = means[(weaker, agent)] - means[(stronger, agent)]
                if gap > 0.03:
                    highlights["model_inversions"].append({
                        "kind": "model-inversion",
                        "agent": agent,
                        "stronger_model": stronger,
                        "stronger_score": round(means[(stronger, agent)], 4),
                        "weaker_model": weaker,
                        "weaker_score": round(means[(weaker, agent)], 4),
                        "gap": round(gap, 4),
                    })

    for model in sorted({model for model, _ in means}):
        agents = [agent for combo_model, agent in means if combo_model == model]
        for stronger in agents:
            for weaker in agents:
                if stronger == weaker:
                    continue
                if AGENT_TIERS.get(stronger, 0) <= AGENT_TIERS.get(weaker, 0):
                    continue
                gap = means[(model, weaker)] - means[(model, stronger)]
                if gap > 0.03:
                    highlights["agent_inversions"].append({
                        "kind": "agent-inversion",
                        "model": model,
                        "stronger_agent": stronger,
                        "stronger_score": round(means[(model, stronger)], 4),
                        "weaker_agent": weaker,
                        "weaker_score": round(means[(model, weaker)], 4),
                        "gap": round(gap, 4),
                    })

    for task, scores in by_task.items():
        task_mean = mean(scores)
        task_std = statistics.pstdev(scores) if len(scores) > 1 else 0.0
        task_record = {
            "task_name": task,
            "mean": round(task_mean, 4),
            "stdev": round(task_std, 4),
            "min": round(min(scores), 4),
            "max": round(max(scores), 4),
            "zeros": sum(score == 0 for score in scores),
            "ones": sum(score == 1 for score in scores),
            "n_cells": len(scores),
        }
        if all(score == 0 for score in scores):
            highlights["all_zero_tasks"].append(task_record)
        if all(score == 1 for score in scores):
            highlights["all_one_tasks"].append(task_record)

    highlights["hard_tasks"] = sorted(
        [
            {
                "task_name": task,
                "mean": round(mean(scores), 4),
                "min": round(min(scores), 4),
                "max": round(max(scores), 4),
                "zeros": sum(score == 0 for score in scores),
                "ones": sum(score == 1 for score in scores),
                "n_cells": len(scores),
            }
            for task, scores in by_task.items()
        ],
        key=lambda item: (item["mean"], task_sort_key(item["task_name"])),
    )[:15]
    highlights["variable_tasks"] = sorted(
        [
            {
                "task_name": task,
                "mean": round(mean(scores), 4),
                "stdev": round(statistics.pstdev(scores), 4) if len(scores) > 1 else 0.0,
                "min": round(min(scores), 4),
                "max": round(max(scores), 4),
                "n_cells": len(scores),
            }
            for task, scores in by_task.items()
        ],
        key=lambda item: (-item["stdev"], task_sort_key(item["task_name"])),
    )[:15]

    task_profiles = []
    for task, scores in by_task.items():
        task_profiles.append({
            "name": str(task),
            "task_name": task,
            "mean": mean(scores),
            "stdev": statistics.pstdev(scores) if len(scores) > 1 else 0.0,
            "negative_rate": sum(score < 0 for score in scores) / len(scores),
            "near_zero_rate": sum(score <= 0.05 for score in scores) / len(scores),
            "one_rate": sum(score == 1 for score in scores) / len(scores),
            "n_cells": len(scores),
            "features": profile_features(scores),
        })
    combo_profiles = []
    for (model, agent), values in by_combo.items():
        scores = [score for _, score, _ in values]
        combo_profiles.append({
            "name": f"{model}/{agent}",
            "model": model,
            "agent": agent,
            "mean": mean(scores),
            "stdev": statistics.pstdev(scores) if len(scores) > 1 else 0.0,
            "negative_rate": sum(score < 0 for score in scores) / len(scores),
            "near_zero_rate": sum(score <= 0.05 for score in scores) / len(scores),
            "one_rate": sum(score == 1 for score in scores) / len(scores),
            "n_tasks": len(scores),
            "features": profile_features(scores),
        })
    highlights["score_clusters"]["task_clusters"] = kmeans_profiles(task_profiles, k=4)
    highlights["score_clusters"]["combo_clusters"] = kmeans_profiles(combo_profiles, k=4)
    return highlights

rows = json.load(open("/tmp/leaderboard_task_focus.json"))
if not rows:
    raise SystemExit("No rows in /tmp/leaderboard_task_focus.json; run Step 1 first.")

benchmark = rows[0]["benchmark"] if len({row["benchmark"] for row in rows}) == 1 else "all"
by_combo = defaultdict(list)
stds = defaultdict(list)
by_task = defaultdict(list)
for row in rows:
    score = float(row["score"])
    std = float(row.get("score_std") or 0)
    combo = (row["model"], row["agent"])
    by_combo[combo].append((row["task_name"], score, std))
    stds[combo].append(std)
    by_task[row["task_name"]].append(score)

agg = {}
agg_std = {}
for combo, values in by_combo.items():
    scores = [score for _, score, _ in values]
    agg[(benchmark, combo[0], combo[1])] = round(sum(scores) / len(scores), 4)
    agg_std[(benchmark, combo[0], combo[1])] = round(sum(stds[combo]) / len(stds[combo]), 4)

print(f"Benchmark: {benchmark}")
print(f"Task rows: {len(rows)}")
print(f"Tasks: {len(by_task)}")
print()
print("Benchmark mean by model/agent:")
for (model, agent), values in sorted(
    by_combo.items(),
    key=lambda item: (item[0][1], -sum(v[1] for v in item[1]) / len(item[1]), item[0][0]),
):
    scores = [score for _, score, _ in values]
    print(
        f"  {agent:12s} {model:38s} "
        f"mean={sum(scores)/len(scores):.4f} median={statistics.median(scores):.4f} "
        f"min={min(scores):.1f} max={max(scores):.1f} "
        f"neg={sum(score < 0 for score in scores):2d} zero={sum(score == 0 for score in scores):2d} "
        f"ones={sum(score == 1 for score in scores):2d} "
        f"mean_std={sum(stds[(model, agent)])/len(stds[(model, agent)]):.4f}"
    )

all_zero = [task for task, scores in by_task.items() if all(score == 0 for score in scores)]
all_one = [task for task, scores in by_task.items() if all(score == 1 for score in scores)]
print()
print("Task summary:")
print(f"  all_zero={len(all_zero)} {sorted(all_zero, key=task_sort_key)[:20]}")
print(f"  all_one={len(all_one)} {sorted(all_one, key=task_sort_key)[:20]}")
print("  hardest by task mean:")
for task, scores in sorted(by_task.items(), key=lambda item: (sum(item[1]) / len(item[1]), task_sort_key(item[0])))[:15]:
    print(
        f"    task={str(task):>4s} mean={sum(scores)/len(scores):.4f} "
        f"zeros={sum(score == 0 for score in scores):2d}/{len(scores)} "
        f"ones={sum(score == 1 for score in scores):2d}/{len(scores)} "
        f"range={min(scores):.1f}-{max(scores):.1f}"
    )
print("  most variable by displayed score:")
for task, scores in sorted(by_task.items(), key=lambda item: statistics.pstdev(item[1]), reverse=True)[:15]:
    print(
        f"    task={str(task):>4s} mean={sum(scores)/len(scores):.4f} "
        f"stdev={statistics.pstdev(scores):.4f} "
        f"range={min(scores):.1f}-{max(scores):.1f}"
    )

print()
print("Negative task-score rows by model/agent:")
for (model, agent), values in sorted(by_combo.items(), key=lambda item: -sum(score < 0 for _, score, _ in item[1])):
    negative = [(task, score, std) for task, score, std in values if score < 0]
    if not negative:
        continue
    dist = Counter(score for _, score, _ in negative)
    tasks = ",".join(str(task) for task, _, _ in sorted(negative, key=lambda item: (item[1], task_sort_key(item[0])))[:30])
    print(f"  {agent:12s} {model:38s} neg={len(negative):2d} dist={dict(sorted(dist.items()))} tasks={tasks}")

print()
print("Aggregate model order inversions >3pp:")
means = {(model, agent): sum(score for _, score, _ in values) / len(values) for (model, agent), values in by_combo.items()}
highlights = build_highlights(rows, by_combo, by_task, means)
for item in highlights["model_inversions"]:
    print(
        f"  {item['agent']:12s} {item['weaker_model']} {item['weaker_score']:.4f} > "
        f"{item['stronger_model']} {item['stronger_score']:.4f} by {item['gap']:.4f}"
    )

print()
print("Aggregate agent order inversions >3pp:")
for item in highlights["agent_inversions"]:
    print(
        f"  {item['model']:38s} {item['weaker_agent']} {item['weaker_score']:.4f} > "
        f"{item['stronger_agent']} {item['stronger_score']:.4f} by {item['gap']:.4f}"
    )

print()
print("Score clusters:")
for scope, clusters in highlights["score_clusters"].items():
    print(f"  {scope}:")
    for cluster in clusters:
        print(
            f"    {cluster['label']:22s} size={cluster['size']:3d} "
            f"mean={cluster['mean']:.4f} stdev={cluster['stdev']:.4f} "
            f"neg_rate={cluster['negative_rate']:.2f} near_zero_rate={cluster['near_zero_rate']:.2f}"
        )

suspect = []
for key in ("negative_rows", "high_trial_variance_rows", "near_zero_outlier_rows"):
    suspect.extend(highlights[key])
seen_cells = set()
deduped_suspect = []
for row in suspect:
    key = (row["benchmark"], row["task_name"], row["model"], row["agent"])
    if key in seen_cells:
        continue
    seen_cells.add(key)
    deduped_suspect.append(row)

print()
print("Highlight counts:")
for key in (
    "negative_rows",
    "high_trial_variance_rows",
    "near_zero_outlier_rows",
    "zero_outlier_rows",
    "model_inversions",
    "agent_inversions",
    "hard_tasks",
    "variable_tasks",
    "all_zero_tasks",
    "all_one_tasks",
):
    print(f"  {key}: {len(highlights[key])}")

Path("/tmp/task_score_highlights.json").write_text(json.dumps(highlights, indent=2))
Path("/tmp/suspect_cells.json").write_text(json.dumps(deduped_suspect, indent=2))
Path("/tmp/agg.json").write_text(json.dumps({str(key): value for key, value in agg.items()}, indent=2))
Path("/tmp/agg_std.json").write_text(json.dumps({str(key): value for key, value in agg_std.items()}, indent=2))
print()
print("Saved highlights: /tmp/task_score_highlights.json")
print(f"Saved suspect cells: /tmp/suspect_cells.json ({len(deduped_suspect)} cells)")
print("Saved aggregate means: /tmp/agg.json")
PYEOF
```

Interpretation rules:

- Treat task-level scores as detection evidence, not root-cause evidence.
- If a benchmark README says the metric is accuracy but task scores include `-1.0`, inspect the verifier code and displayed trials. In Harbor adapters, `-1.0` often means evaluator exception, missing response artifact, or verifier crash, while ordinary wrong answers may be `0.0`.
- Do not infer that a benchmark is unbounded merely because displayed rows include negative values. First verify whether negative rewards are legitimate metric values or failure sentinels.
- `near_zero_outlier_rows` includes exact zero and near-zero scores (`<= 0.05`) when that model/agent's task mean is otherwise high (`> 0.8`). `zero_outlier_rows` is retained as the exact-zero subset for compatibility.
- `score_clusters` uses dependency-free k-means over score profiles: mean, stdev, negative-rate, near-zero-rate, and one-rate. Use clusters to separate benchmark-wide task difficulty from agent/model-specific failure patterns.
- Start `get_cell_trials` inspection with the exact task rows that drive the aggregate anomaly: negative scores, high `score_std`, near-zero rows in otherwise high-scoring combos, rows inside negative/failure-heavy clusters, and rows involved in model/agent inversions.

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

Run this only for cells you are about to diagnose. It uses the exact `(benchmark, task_name, model, agent)` rows emitted by Step 2 and downloads the same trials the website shows when a user clicks a score cell.

First fetch displayed-trial metadata for all suspect cells. This is fast and tells you whether a bad score is made of clean runs, tolerated timeouts, or obvious infra noise. For the anomalies you are actively diagnosing, this script can also download the displayed trial tarballs in parallel; by default it downloads only ranks `1..3`, matching the displayed score window.

```bash
python3 << 'PYEOF'
import concurrent.futures
import json
import os
import re
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

SUPABASE_URL = "https://hnkceovsiaczvcwhdlkb.supabase.co"
SUPABASE_KEY = "sb_publishable_kpc09uUk5qcIzVex3NWGAg_y5W7jr6t"
DOWNLOAD_TARBALLS = os.environ.get("DOWNLOAD_TARBALLS", "1") != "0"
DOWNLOAD_MAX_RANK = int(os.environ.get("DOWNLOAD_MAX_RANK", "3"))
TRIAL_OUT_ROOT = Path(os.environ.get("TRIAL_OUT_ROOT", "/tmp/harbor-cell-trials"))

cells = json.load(open("/tmp/suspect_cells.json"))
unique = []
seen = set()
for cell in cells:
    key = (cell["benchmark"], cell["task_name"], cell["model"], cell["agent"])
    if key not in seen:
        seen.add(key)
        unique.append(cell)

def rpc(name, body):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/rpc/{name}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Referer": "https://harborsubabase.vercel.app/",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def fetch_cell(cell):
    try:
        trials = rpc("get_cell_trials", {
            "p_benchmark": cell["benchmark"],
            "p_task_name": cell["task_name"],
            "p_model": cell["model"],
            "p_agent": cell["agent"],
        })
        return {"cell": cell, "trials": trials}
    except Exception as e:
        return {"cell": cell, "trials": [], "error": repr(e)}

def safe_part(value):
    return re.sub(r"[^A-Za-z0-9._=-]+", "_", str(value))

def trial_path(cell, trial):
    return (
        TRIAL_OUT_ROOT
        / safe_part(cell["benchmark"])
        / safe_part(cell["task_name"])
        / safe_part(cell["model"])
        / safe_part(cell["agent"])
        / f"{trial['trial_id']}.tar.gz"
    )

def download_trial(cell, trial):
    path = trial_path(cell, trial)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return {"trial_id": trial["trial_id"], "path": str(path), "status": "exists"}
    urllib.request.urlretrieve(trial["trial_uri"], path)
    return {"trial_id": trial["trial_id"], "path": str(path), "status": "downloaded"}

out = []
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
    futures = [executor.submit(fetch_cell, cell) for cell in unique]
    for idx, future in enumerate(concurrent.futures.as_completed(futures), 1):
        out.append(future.result())
        if idx % 25 == 0:
            print(f"fetched {idx}/{len(unique)}")

Path("/tmp/suspect_cell_trials.json").write_text(json.dumps(out, indent=2))

downloads = []
download_errors = []
if DOWNLOAD_TARBALLS:
    jobs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        for item in out:
            if item.get("error"):
                continue
            cell = item["cell"]
            for trial in item.get("trials", []):
                if (trial.get("trial_rank") or 99) <= DOWNLOAD_MAX_RANK:
                    jobs.append(executor.submit(download_trial, cell, trial))
        for idx, future in enumerate(concurrent.futures.as_completed(jobs), 1):
            try:
                downloads.append(future.result())
            except Exception as e:
                download_errors.append(repr(e))
            if idx % 50 == 0:
                print(f"downloaded/checked {idx}/{len(jobs)} trial tarballs")
    Path("/tmp/suspect_cell_trial_downloads.json").write_text(json.dumps({
        "download_root": str(TRIAL_OUT_ROOT),
        "download_max_rank": DOWNLOAD_MAX_RANK,
        "downloads": downloads,
        "errors": download_errors,
    }, indent=2))

for label, rank_filter in [
    ("latest_3_displayed", lambda trial: (trial.get("trial_rank") or 99) <= 3),
    ("all_returned_by_trial_view", lambda trial: True),
]:
    rewards = Counter()
    exceptions = Counter()
    by_combo = defaultdict(Counter)
    total = 0
    errors = 0
    for item in out:
        if item.get("error"):
            errors += 1
        cell = item["cell"]
        combo = (cell["model"], cell["agent"])
        for trial in item.get("trials", []):
            if not rank_filter(trial):
                continue
            total += 1
            rewards[str(trial.get("reward"))] += 1
            exception_info = trial.get("exception_info")
            if isinstance(exception_info, dict):
                exception_type = exception_info.get("exception_type") or "OK"
            else:
                exception_type = "OK" if not exception_info else str(exception_info)[:80]
            exceptions[exception_type] += 1
            by_combo[combo][str(trial.get("reward"))] += 1
    print(f"\n{label}: total_trials={total} errors={errors}")
    print(f"  rewards={dict(rewards)}")
    print(f"  exceptions={dict(exceptions)}")
    for combo, counts in sorted(by_combo.items(), key=lambda item: -item[1].get("-1", 0))[:20]:
        print(f"  {combo}: {dict(counts)}")

print("\nSaved: /tmp/suspect_cell_trials.json")
if DOWNLOAD_TARBALLS:
    print(f"Downloaded/checked trial tarballs: {len(downloads)}")
    print(f"Download errors: {len(download_errors)}")
    print("Saved: /tmp/suspect_cell_trial_downloads.json")
PYEOF
```

To fetch metadata only without tarballs, run `DOWNLOAD_TARBALLS=0 python3 ...`. To download all trials returned by `/trial_view` instead of only the latest score window, run `DOWNLOAD_MAX_RANK=5 python3 ...`.

Then inspect tarballs for representative cells. Use this single-cell script when the batch summary shows a pattern worth explaining, such as many `reward=-1` rows with `exception=OK`.

```bash
BENCHMARK='<benchmark>' \
TASK_NAME='<task_name>' \
MODEL='<model>' \
AGENT='<agent>' \
python3 - << 'PYEOF'
import json
import os
import re
import tarfile
import urllib.request
from pathlib import Path

SUPABASE_URL = "https://hnkceovsiaczvcwhdlkb.supabase.co"
SUPABASE_KEY = "sb_publishable_kpc09uUk5qcIzVex3NWGAg_y5W7jr6t"

required = ["BENCHMARK", "TASK_NAME", "MODEL", "AGENT"]
missing = [name for name in required if not os.environ.get(name)]
if missing:
    raise SystemExit(f"Missing required env var(s): {', '.join(missing)}")

BENCHMARK = os.environ["BENCHMARK"]
TASK_NAME = os.environ["TASK_NAME"]
MODEL = os.environ["MODEL"]
AGENT = os.environ["AGENT"]
OUT = Path("/tmp/harbor-cell-trials") / BENCHMARK / TASK_NAME / MODEL / AGENT
OUT.mkdir(parents=True, exist_ok=True)

def rpc(name, body):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/rpc/{name}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Referer": "https://harborsubabase.vercel.app/",
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
            return f.read().decode("utf-8", "replace") if f else ""
    return ""

def clip(text, limit=500):
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "").strip()
    return text[-limit:] if len(text) > limit else text

trials = rpc("get_cell_trials", {
    "p_benchmark": BENCHMARK,
    "p_task_name": TASK_NAME,
    "p_model": MODEL,
    "p_agent": AGENT,
})
(OUT / "displayed_trials.json").write_text(json.dumps(trials, indent=2))
print(f"Displayed trials: {len(trials)}")

for trial in trials:
    trial_id = trial["trial_id"]
    tgz_path = OUT / f"{trial_id}.tar.gz"
    if not tgz_path.exists():
        urllib.request.urlretrieve(trial["trial_uri"], tgz_path)

    exception_info = trial.get("exception_info")
    if isinstance(exception_info, dict):
        exception_type = exception_info.get("exception_type") or "OK"
    else:
        exception_type = "OK" if not exception_info else str(exception_info)[:80]

    cause = []
    steps = "missing"
    verifier_tail = ""
    agent_tail = ""
    try:
        with tarfile.open(tgz_path, "r:gz") as tf:
            trajectory = read_member(tf, "/agent/trajectory.json")
            agent_log = read_member(tf, [
                "/agent/claude-code.txt",
                "/agent/codex.txt",
                "/agent/gemini-cli.txt",
            ])
            app_answer = read_member(tf, ["/app/answer.txt", "/app/response.txt"])
            verifier = read_member(tf, [
                "/verifier/test-stdout.txt",
                "/verifier/test-stderr.txt",
                "/verifier/reward.txt",
            ])
            exception_txt = read_member(tf, "/exception.txt")
            combined_agent_text = "\n".join([agent_log, trajectory, app_answer])

            if "Response file not found" in verifier:
                cause.append("missing_submission_artifact")
            if "no valid response in Codex output" in verifier:
                cause.append("verifier_codex_only_fallback")
            if "The answer is" in combined_agent_text or "final answer" in combined_agent_text.lower():
                cause.append("plausible_answer_in_agent_log")
            if "Credit balance is too low" in combined_agent_text:
                cause.append("credit_balance_low")
            if exception_type == "AgentTimeoutError" or "Agent execution timed out" in exception_txt:
                cause.append("agent_timeout")
            if "Evaluation failed with error:" in verifier:
                m = re.search(r"Evaluation failed with error: (.*)", verifier)
                cause.append("verifier_exception:" + (m.group(1)[:120] if m else "unknown"))
            if "No such file or directory" in verifier:
                cause.append("missing_required_output")
            if "Metric Error:" in verifier:
                m = re.search(r"Metric Error: (.*)", verifier)
                cause.append("metric_error:" + (m.group(1)[:120] if m else "unknown"))
            if trajectory:
                try:
                    steps = len(json.loads(trajectory).get("steps", []))
                except Exception:
                    steps = "parse_error"
            verifier_tail = clip(verifier, 500)
            agent_tail = clip(combined_agent_text, 500)
    except Exception as e:
        cause.append("tar_read_error:" + str(e))
        steps = "unknown"

    if not cause:
        cause.append("valid_run" if exception_type == "OK" else "unclassified_failure")
    print(
        f"\nrank={trial.get('trial_rank')} id={trial_id} reward={trial.get('reward')} "
        f"exception={exception_type} steps={steps} cause={';'.join(cause)}"
    )
    if verifier_tail:
        print(f"verifier_tail: {verifier_tail}")
    if agent_tail and ("missing_submission_artifact" in cause or "verifier_codex_only_fallback" in cause):
        print(f"agent_tail: {agent_tail}")
PYEOF
```

## Step 2d — Summarize downloaded anomaly trajectories as a table

After Step 2c downloads the displayed trial tarballs, read the files and produce a simple table grouped by anomaly category. The goal is to turn trajectory/verifier details into a plain-language cause and a rerun recommendation label.

The automated task generator now performs this scan directly for any archives already present under `/tmp/harbor-cell-trials/<benchmark>/`. The HTML renders the result as `Trial Archive Categories` and `Trial Archive Samples`.

Use these category names consistently:
- `missing_submission_artifact` — verifier or exception logs show a missing expected file such as `answer.txt`, `/app/law.py`, or another required output.
- `timeout_or_budget_issue` — displayed trial is dominated by `AgentTimeoutError`, `VerifierTimeoutError`, or timeout text.
- `rate_limit_noise` — displayed trial logs contain quota, billing, `RESOURCE_EXHAUSTED`, rate-limit, or low-credit evidence.
- `verifier_or_metric_error` — verifier emitted a metric error, crash, or traceback.
- `true_wrong_answer` — the run completed normally but received non-positive reward.
- `valid_run` — no failure pattern was found in the downloaded archive.
- `unclassified_failure` — an exception exists but did not match a known category.
- `tar_read_error` — the local archive could not be read.

Important: only use downloaded archives from `get_cell_trials` paths as evidence for the visible score. Do not mix in raw trial-table artifacts.

Outputs:
- `/tmp/anomaly_trial_causes.json` — structured trial-level causes
- `/tmp/anomaly_trial_causes.md` — markdown table for reports

```bash
python3 << 'PYEOF'
import json
import re
import tarfile
from collections import defaultdict
from pathlib import Path

TRIAL_OUT_ROOT = Path("/tmp/harbor-cell-trials")
MAX_EVIDENCE_CHARS = 180

cell_trials = json.load(open("/tmp/suspect_cell_trials.json"))
highlights = json.load(open("/tmp/task_score_highlights.json"))

def safe_part(value):
    return re.sub(r"[^A-Za-z0-9._=-]+", "_", str(value))

def cell_key(cell):
    return (
        cell["benchmark"],
        str(cell["task_name"]),
        cell["model"],
        cell["agent"],
    )

def trial_path(cell, trial):
    return (
        TRIAL_OUT_ROOT
        / safe_part(cell["benchmark"])
        / safe_part(cell["task_name"])
        / safe_part(cell["model"])
        / safe_part(cell["agent"])
        / f"{trial['trial_id']}.tar.gz"
    )

def read_member(tf, suffixes):
    if isinstance(suffixes, str):
        suffixes = [suffixes]
    for member in tf.getmembers():
        if any(member.name.endswith(suffix) for suffix in suffixes):
            f = tf.extractfile(member)
            return f.read().decode("utf-8", "replace") if f else ""
    return ""

def clean(text):
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text

def clip(text, limit=MAX_EVIDENCE_CHARS):
    text = clean(text)
    return text[:limit] + ("..." if len(text) > limit else "")

def exception_type(trial):
    exception_info = trial.get("exception_info")
    if isinstance(exception_info, dict):
        return exception_info.get("exception_type") or "OK"
    return "OK" if not exception_info else str(exception_info)[:80]

def anomaly_categories_by_cell(highlights):
    categories = defaultdict(set)
    for bucket in ("negative_rows", "high_trial_variance_rows", "near_zero_outlier_rows", "zero_outlier_rows"):
        for row in highlights.get(bucket, []):
            categories[cell_key(row)].add(bucket)
    return categories

def classify_trial(cell, trial, tarball_path):
    etype = exception_type(trial)
    reward = trial.get("reward")
    cause_code = "unclassified"
    cause = "The trial needs manual inspection."
    evidence = ""
    rerun_label = "inspect-manually"

    verifier = ""
    agent_log = ""
    trajectory = ""
    app_answer = ""
    exception_txt = ""
    if tarball_path.exists():
        try:
            with tarfile.open(tarball_path, "r:gz") as tf:
                verifier = read_member(tf, [
                    "/verifier/test-stdout.txt",
                    "/verifier/test-stderr.txt",
                    "/verifier/reward.txt",
                ])
                agent_log = read_member(tf, [
                    "/agent/claude-code.txt",
                    "/agent/codex.txt",
                    "/agent/gemini-cli.txt",
                ])
                trajectory = read_member(tf, "/agent/trajectory.json")
                app_answer = read_member(tf, ["/app/answer.txt", "/app/response.txt"])
                exception_txt = read_member(tf, "/exception.txt")
        except Exception as e:
            cause_code = "tar_read_error"
            cause = "The downloaded tarball could not be read."
            evidence = str(e)
            rerun_label = "inspect-manually"
            return cause_code, cause, evidence, rerun_label
    else:
        cause_code = "tarball_missing"
        cause = "The displayed-trial metadata exists, but the tarball was not downloaded."
        evidence = str(tarball_path)
        rerun_label = "download-and-inspect"
        return cause_code, cause, evidence, rerun_label

    combined_agent_text = "\n".join([agent_log, trajectory, app_answer])
    has_plausible_answer = "The answer is" in combined_agent_text or "final answer" in combined_agent_text.lower()

    if "Credit balance is too low" in combined_agent_text or "RESOURCE_EXHAUSTED" in combined_agent_text:
        cause_code = "rate_limit_or_quota"
        cause = "The run appears to be affected by quota, billing, or provider resource noise."
        evidence = clip(combined_agent_text)
        rerun_label = "rerun-recommended"
    elif etype == "AgentTimeoutError" or "Agent execution timed out" in exception_txt:
        cause_code = "timeout_or_budget_issue"
        cause = "The agent timed out before producing a completed result."
        evidence = clip(exception_txt or verifier)
        rerun_label = "rerun-with-more-time"
    elif etype == "VerifierTimeoutError":
        cause_code = "timeout_or_budget_issue"
        cause = "The verifier timed out while scoring the run."
        evidence = clip(exception_txt or verifier)
        rerun_label = "rerun-after-verifier-timeout-check"
    elif "Response file not found" in verifier and "no valid response in Codex output" in verifier and has_plausible_answer:
        cause_code = "model_agent_output_mismatch"
        cause = "The agent log contains a plausible answer, but the verifier only looked for the required answer file or Codex fallback."
        evidence = clip(verifier)
        rerun_label = "rerun-after-harness-fix"
    elif "Response file not found" in verifier:
        cause_code = "missing_submission_artifact"
        cause = "The verifier could not find the required answer file."
        evidence = clip(verifier)
        rerun_label = "rerun-after-agent-output-fix"
    elif "Evaluation failed with error:" in verifier:
        cause_code = "verifier_exception"
        cause = "The evaluator crashed and assigned a failure reward."
        evidence = clip(verifier)
        rerun_label = "rerun-after-verifier-fix"
    elif reward == 0 or str(reward) == "0":
        cause_code = "true_wrong_answer"
        cause = "The run completed and was scored as wrong."
        evidence = clip(verifier or app_answer or agent_log)
        rerun_label = "do-not-rerun"
    elif reward == 1 or str(reward) == "1":
        cause_code = "successful_trial_in_flagged_cell"
        cause = "This individual trial succeeded; the anomaly is from other trials in the same cell or score variance."
        evidence = clip(verifier or app_answer or agent_log)
        rerun_label = "do-not-rerun-this-trial"
    elif str(reward) == "-1":
        cause_code = "unclassified_failure_reward"
        cause = "The leaderboard counted a failure reward, but the tarball evidence did not match a known bucket."
        evidence = clip(verifier or exception_txt or agent_log)
        rerun_label = "inspect-before-rerun"
    else:
        evidence = clip(verifier or exception_txt or agent_log)

    return cause_code, cause, evidence, rerun_label

def md_escape(value):
    value = str(value if value is not None else "")
    return value.replace("|", "\\|").replace("\n", " ")

categories = anomaly_categories_by_cell(highlights)
records = []
for item in cell_trials:
    if item.get("error"):
        continue
    cell = item["cell"]
    cat = sorted(categories.get(cell_key(cell), {"suspect_cell"}))
    for trial in item.get("trials", []):
        if (trial.get("trial_rank") or 99) > 3:
            continue
        tarball_path = trial_path(cell, trial)
        cause_code, cause, evidence, rerun_label = classify_trial(cell, trial, tarball_path)
        records.append({
            "anomaly_categories": cat,
            "benchmark": cell["benchmark"],
            "task_name": cell["task_name"],
            "model": cell["model"],
            "agent": cell["agent"],
            "cell_score": cell.get("score"),
            "cell_score_std": cell.get("score_std"),
            "trial_rank": trial.get("trial_rank"),
            "trial_id": trial.get("trial_id"),
            "trial_uri": trial.get("trial_uri"),
            "local_tarball": str(tarball_path),
            "reward": trial.get("reward"),
            "exception_type": exception_type(trial),
            "cause_code": cause_code,
            "cause": cause,
            "evidence": evidence,
            "rerun_label": rerun_label,
        })

records.sort(key=lambda row: (
    ",".join(row["anomaly_categories"]),
    row["benchmark"],
    str(row["task_name"]),
    row["model"],
    row["agent"],
    row["trial_rank"] or 99,
))

Path("/tmp/anomaly_trial_causes.json").write_text(json.dumps(records, indent=2))

lines = [
    "| Anomaly category | Cell | Rank | Reward | Trial | Cause | Evidence | Rerun label |",
    "|---|---:|---:|---:|---|---|---|---|",
]
for row in records:
    trial_link = f"[{row['trial_id']}]({row['trial_uri']})" if row.get("trial_uri") else row.get("trial_id")
    cell = f"{row['benchmark']} / task {row['task_name']} / {row['model']} / {row['agent']}"
    lines.append(
        "| "
        + " | ".join([
            md_escape(", ".join(row["anomaly_categories"])),
            md_escape(cell),
            md_escape(row["trial_rank"]),
            md_escape(row["reward"]),
            md_escape(trial_link),
            md_escape(row["cause"]),
            md_escape(row["evidence"]),
            md_escape(row["rerun_label"]),
        ])
        + " |"
    )

Path("/tmp/anomaly_trial_causes.md").write_text("\n".join(lines) + "\n")

by_cause = defaultdict(int)
by_rerun = defaultdict(int)
for row in records:
    by_cause[row["cause_code"]] += 1
    by_rerun[row["rerun_label"]] += 1

print(f"Analyzed displayed trials: {len(records)}")
print(f"Cause counts: {dict(sorted(by_cause.items()))}")
print(f"Rerun labels: {dict(sorted(by_rerun.items()))}")
print("Saved: /tmp/anomaly_trial_causes.json")
print("Saved: /tmp/anomaly_trial_causes.md")
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

Use `experiment-track.csv` to attach experiment ownership metadata to each flagged benchmark. The CSV is keyed by `Adapter Name`; use the `People` column to show who runs the benchmark experiment.

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

Run this script to perform Steps 3a-3d for the focused benchmark:

```bash
python3 << 'PYEOF'
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path("/Users/han/Workplace/weird-score-triage")
ADAPTERS = Path("/tmp/harbor/adapters")
OWNERS_CSV = ROOT / "experiment-track.csv"

ALIASES = {
    "bfcl": "berkeleyfunctioncallingleaderboardbfcl",
    "berkeleyfunctioncallingleaderboardbfcl": "bfcl",
    "swtbench": "swtbench",
    "swt_bench": "swtbench",
    "researchcodebench": "researchcodebench",
    "reaserchcodebench": "researchcodebench",
    "research-code-bench": "researchcodebench",
    "spreadsheetbenchverified": "spreadsheetbench",
    "spreadsheetbench": "spreadsheetbench",
    "spreadsheetbench-verified": "spreadsheetbench",
    "mlgymbench": "mlgymbench",
    "terminalbench20": "terminalbench",
}

def norm(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())

def canon(value):
    n = norm(value)
    return ALIASES.get(n, n)

def parse_number(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if match:
            num = float(match.group(0))
            return num / 100.0 if "%" in value and num > 1 else num
    return None

def model_family(model):
    if model.startswith("gpt") or "openai" in model:
        return "openai"
    if model.startswith("claude") or "anthropic" in model:
        return "anthropic"
    if model.startswith("gemini") or "google" in model:
        return "google"
    return None

def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default

def find_adapter(benchmark):
    if not ADAPTERS.exists():
        return None
    adapters = [path for path in ADAPTERS.iterdir() if path.is_dir()]
    by_canon = defaultdict(list)
    for path in adapters:
        by_canon[canon(path.name)].append(path)
    key = canon(benchmark)
    if key in by_canon:
        return by_canon[key][0]
    partial = [path for path in adapters if key in canon(path.name) or canon(path.name) in key]
    return sorted(partial, key=lambda path: len(path.name))[0] if partial else None

def readme_insight(adapter_path):
    if not adapter_path:
        return "Adapter folder was not found, so README validation is unavailable."
    readme = adapter_path / "README.md"
    if not readme.exists():
        return "Adapter README is absent."
    text = readme.read_text(errors="replace")
    candidates = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line.strip(" -*#\t"))
        if not line:
            continue
        lower = line.lower()
        if any(word in lower for word in ["metric", "score", "accuracy", "negative", "timeout", "limitation", "known", "evaluation"]):
            candidates.append(line)
    if candidates:
        return candidates[0][:260]
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line.strip(" -*#\t"))
        if line:
            return line[:260]
    return "README exists but no concise insight was extracted."

def load_owners():
    owners = {}
    if not OWNERS_CSV.exists():
        return owners
    with OWNERS_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            adapter_name = row.get("Adapter Name") or row.get("adapter_name") or ""
            people = row.get("People") or row.get("people") or ""
            owners[canon(adapter_name)] = {"adapter_name": adapter_name, "people": people}
    return owners

def find_owner(benchmark, adapter_path, owners):
    keys = [canon(benchmark)]
    if adapter_path:
        keys.append(canon(adapter_path.name))
    for key in keys:
        if key in owners:
            return owners[key]
    for key in keys:
        for owner_key, owner in owners.items():
            if key in owner_key or owner_key in key:
                return owner
    return {"adapter_name": "", "people": ""}

def live_scores(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["model"], row["agent"])].append(float(row["score"]))
    return {
        key: round(sum(scores) / len(scores), 4)
        for key, scores in grouped.items()
    }

def anomalous_combos(highlights, live):
    combos = set()
    for bucket in ("negative_rows", "high_trial_variance_rows", "near_zero_outlier_rows", "zero_outlier_rows"):
        for row in highlights.get(bucket, []):
            combos.add((row["model"], row["agent"]))
    for row in highlights.get("model_inversions", []):
        agent = row["agent"]
        combos.add((row["stronger_model"], agent))
        combos.add((row["weaker_model"], agent))
    for row in highlights.get("agent_inversions", []):
        model = row["model"]
        combos.add((model, row["stronger_agent"]))
        combos.add((model, row["weaker_agent"]))
    return sorted(combos) if combos else sorted(live)

def extract_parity_records(adapter_path):
    if not adapter_path:
        return []
    parity_path = adapter_path / "parity_experiment.json"
    if not parity_path.exists():
        return []
    data = load_json(parity_path, [])
    if isinstance(data, dict):
        data = [data]
    records = []
    for entry in data:
        agent = entry.get("agent") or entry.get("agent_name") or ""
        model = entry.get("model") or entry.get("model_name") or ""
        for metric in entry.get("metrics", []) or []:
            score = (
                parse_number(metric.get("harbor"))
                or parse_number(metric.get("score"))
                or parse_number(metric.get("value"))
            )
            if score is not None:
                records.append({
                    "agent": agent,
                    "model": model,
                    "metric": metric.get("metric") or metric.get("benchmark_name") or "score",
                    "score": score,
                    "source": "metrics",
                })
        for score_item in entry.get("scores", []) or []:
            score = parse_number(score_item.get("value")) or parse_number(score_item.get("score"))
            if score is not None:
                records.append({
                    "agent": agent,
                    "model": model,
                    "metric": score_item.get("metric") or "score",
                    "score": score,
                    "source": "scores",
                })
    return records

def comparable_parity(model, agent, parity_records):
    exact = []
    family = []
    for record in parity_records:
        record_model = record.get("model") or ""
        record_agent = record.get("agent") or ""
        model_exact = norm(model) and norm(model) in norm(record_model)
        agent_exact = norm(agent) and norm(agent) in norm(record_agent)
        if model_exact and agent_exact:
            exact.append({**record, "comparability": "exact-model-agent"})
        elif model_family(model) and model_family(model) == model_family(record_model) and agent_exact:
            family.append({**record, "comparability": "same-family-agent"})
    return exact[0] if exact else (family[0] if family else None)

def verdict(live_score, parity_record, readme_note):
    if parity_record is None:
        return "UNVERIFIED", None
    delta = live_score - parity_record["score"]
    if delta < -0.10:
        return "REGRESSION", round(delta, 4)
    if delta > 0.10:
        return "POSSIBLE INFLATION", round(delta, 4)
    if abs(delta) <= 0.05:
        return "EXPECTED BEHAVIOR", round(delta, 4)
    return "CONFIRMED ANOMALY", round(delta, 4)

focus_rows = load_json("/tmp/leaderboard_task_focus.json", [])
if not focus_rows:
    raise SystemExit("No focused task rows found; run Step 1 first.")
benchmark = focus_rows[0]["benchmark"]
highlights = load_json("/tmp/task_score_highlights.json", {})
live = live_scores(focus_rows)
adapter_path = find_adapter(benchmark)
readme_note = readme_insight(adapter_path)
owners = load_owners()
owner = find_owner(benchmark, adapter_path, owners)
parity_records = extract_parity_records(adapter_path)
combos = anomalous_combos(highlights, live)

checks = []
for model, agent in combos:
    live_score = live.get((model, agent))
    parity_record = comparable_parity(model, agent, parity_records)
    v, delta = verdict(live_score, parity_record, readme_note)
    checks.append({
        "benchmark": benchmark,
        "model": model,
        "agent": agent,
        "live_leaderboard_score": live_score,
        "parity_score": parity_record["score"] if parity_record else None,
        "parity_model": parity_record.get("model") if parity_record else "",
        "parity_agent": parity_record.get("agent") if parity_record else "",
        "parity_metric": parity_record.get("metric") if parity_record else "",
        "comparability": parity_record.get("comparability") if parity_record else "not-comparable-or-missing",
        "delta_live_minus_parity": delta,
        "readme_insight": readme_note,
        "verdict": v,
    })

report = {
    "benchmark": benchmark,
    "adapter_path": str(adapter_path) if adapter_path else "",
    "parity_path": str(adapter_path / "parity_experiment.json") if adapter_path and (adapter_path / "parity_experiment.json").exists() else "",
    "readme_path": str(adapter_path / "README.md") if adapter_path and (adapter_path / "README.md").exists() else "",
    "readme_insight": readme_note,
    "experiment_owner": owner,
    "parity_records_found": len(parity_records),
    "checks": checks,
}
Path("/tmp/parity_confirmation.json").write_text(json.dumps(report, indent=2))

lines = [f"#### {benchmark} — Parity Check", ""]
lines.append(f"- Adapter: `{report['adapter_path'] or 'not found'}`")
lines.append(f"- Experiment owner: {owner.get('people') or 'unknown'} ({owner.get('adapter_name') or 'no CSV match'})")
lines.append(f"- README insight: {readme_note}")
lines.append("")
lines.append("| Model | Agent | Parity score | Live leaderboard score | Delta | Comparability | Verdict |")
lines.append("|---|---|---:|---:|---:|---|---|")
for row in checks:
    parity_score = "" if row["parity_score"] is None else f"{row['parity_score']:.4f}"
    live_score = "" if row["live_leaderboard_score"] is None else f"{row['live_leaderboard_score']:.4f}"
    delta = "" if row["delta_live_minus_parity"] is None else f"{row['delta_live_minus_parity']:+.4f}"
    lines.append(
        f"| {row['model']} | {row['agent']} | {parity_score} | {live_score} | "
        f"{delta} | {row['comparability']} | {row['verdict']} |"
    )
Path("/tmp/parity_confirmation.md").write_text("\n".join(lines) + "\n")

print(f"Benchmark: {benchmark}")
print(f"Adapter: {report['adapter_path'] or 'not found'}")
print(f"Owner: {owner.get('people') or 'unknown'}")
print(f"Parity records found: {len(parity_records)}")
for row in checks:
    print(
        f"{row['model']}/{row['agent']} live={row['live_leaderboard_score']} "
        f"parity={row['parity_score']} delta={row['delta_live_minus_parity']} "
        f"verdict={row['verdict']} comparability={row['comparability']}"
    )
print("Saved: /tmp/parity_confirmation.json")
print("Saved: /tmp/parity_confirmation.md")
PYEOF
```

---

## Step 4 — Build the final structured report

This workflow now produces a focused anomaly report for the benchmark under audit. Build `report_data` from the machine-readable artifacts created by earlier steps; do not make markdown the source of truth.

Inputs consumed:
- `/tmp/leaderboard_task_focus.json` from Step 1
- `/tmp/category_consistency.json` from Step 1b
- `/tmp/task_score_highlights.json` from Step 2
- `/tmp/anomaly_trial_causes.json` from Step 2d
- `/tmp/parity_confirmation.json` from Step 3

Required output shape:

```json
{
  "meta": {
    "date": "<YYYY-MM-DD>",
    "scope": "<focused task-level leaderboard slice>",
    "benchmarks_seen": 1,
    "benchmarks_flagged": 1,
    "clean_benchmarks": []
  },
  "summary": {
    "headline_findings": ["<short evidence-backed sentence>"],
    "analysis_notes": ["<coverage/confidence note>"]
  },
  "findings": [
    {
      "benchmark": "<exact live benchmark key>",
      "priority": 1,
      "root_cause": "Scoring or Verifier Issue",
      "parity_verdict": "UNVERIFIED",
      "historical_trend": "MIXED",
      "experiment_owner": {"adapter_name": "", "people": ""},
      "anomaly": "<one sentence>",
      "parity": "<one sentence>",
      "historical": "<one sentence>",
      "recommended_action": "<one sentence>",
      "tags": ["near-zero", "verifier"],
      "evidence": {
        "highlight_counts": {},
        "category_consistency": {},
        "trial_cause_counts": {},
        "rerun_label_counts": {},
        "parity_checks": [],
        "trajectory_table": []
      }
    }
  ],
  "action_queue": {
    "Scoring or Verifier Issue": [],
    "Task Or Environment Issue": [],
    "Agent Execution Issue": [],
    "Model-Agent Compatibility Issue": [],
    "Model Behavior Issue": [],
    "Needs More Investigation": []
  }
}
```

Root cause labels must be exactly one of:
- `Model Behavior Issue`
- `Agent Execution Issue`
- `Model-Agent Compatibility Issue`
- `Scoring or Verifier Issue`
- `Task Or Environment Issue`
- `Needs More Investigation`

Priority rules:
- `1`: verifier/scoring failure, task/environment failure, broad model-agent compatibility failure, or rerun-blocking infra issue
- `2`: repeated suspicious behavior with partial evidence or mixed categories
- `3`: likely expected behavior, isolated model behavior, or mostly unverified signal

Verdict rules:
- `parity_verdict` must be one of `CONFIRMED ANOMALY`, `EXPECTED BEHAVIOR`, `REGRESSION`, `UNVERIFIED`, `NEEDS_INVESTIGATION`
- Use `REGRESSION` if any comparable parity check regressed by more than 10pp
- Use `EXPECTED BEHAVIOR` only when comparable parity is within 5pp and trajectory evidence does not show harness/scoring failure
- Use `UNVERIFIED` when parity is missing or not comparable
- Use `NEEDS_INVESTIGATION` when parity evidence conflicts with trajectory evidence

Historical trend in this focused workflow is category-consistency evidence, not a full time-series regression unless Step 2b was also run. Use:
- `MIXED` when the same issue appears in sibling benchmarks
- `STABLE` when sibling benchmarks do not reproduce the issue
- `UNVERIFIED` when no same-category comparison is available

## Step 5 — Write JSON, markdown, and interactive HTML

Preferred current procedure: run the repository generator instead of the inline script below.

```bash
python3 /Users/han/Workplace/weird-score-triage/generate_leaderboard_task_audit.py <benchmark>
```

The generator reads:
- `/tmp/leaderboard_aggregate.json` from `get_leaderboard` for official benchmark scores
- `/tmp/leaderboard.json` from `get_leaderboard_task` for task diagnostics
- `/tmp/harbor/adapters/` for README/parity context
- `/tmp/harbor-mix/benchmark_info_jobs/` for history context
- `/tmp/harbor-cell-trials/<benchmark>/**/*.tar.gz` for downloaded displayed-trial archive categories and samples

The generator renders:
- `Top Scores` and `Bottom Scores` side by side with 8 rows each
- highlighted model/agent rows when they appear in anomaly records
- `Experiment Owner` and `Score Aggregation` side by side
- conditional score aggregation text: `displayed vs task_mean` only when the task mean differs from the official displayed score
- `Trial Archive Categories` and `Trial Archive Samples`
- parity labels as clickable links when `parity_experiment.json` exists

The older inline script below is retained only as a fallback/reference for manual reconstruction.

```bash
python3 << 'PYEOF'
import html
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path("/Users/han/Workplace/weird-score-triage")
OUT_DIR = Path.home() / "harbor-audits"
TEMPLATE = ROOT / "templates/leaderboard-task-audit-interactive.html"
ROOT_CAUSES = [
    "Scoring or Verifier Issue",
    "Task Or Environment Issue",
    "Agent Execution Issue",
    "Model-Agent Compatibility Issue",
    "Model Behavior Issue",
    "Needs More Investigation",
]

def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default

def count_by(rows, key):
    return dict(sorted(Counter(row.get(key) for row in rows if row.get(key)).items()))

def highlight_counts(highlights):
    out = {}
    for key, value in highlights.items():
        if isinstance(value, list):
            out[key] = len(value)
    return out

def choose_root_cause(cause_counts, highlights):
    if cause_counts.get("verifier_exception") or highlights.get("negative_rows"):
        return "Scoring or Verifier Issue"
    if cause_counts.get("model_agent_output_mismatch"):
        return "Model-Agent Compatibility Issue"
    if cause_counts.get("missing_submission_artifact") or cause_counts.get("timeout_or_budget_issue") or cause_counts.get("rate_limit_or_quota"):
        return "Agent Execution Issue"
    if highlights.get("all_zero_tasks"):
        return "Task Or Environment Issue"
    if cause_counts.get("true_wrong_answer"):
        return "Model Behavior Issue"
    return "Needs More Investigation"

def priority_for(root_cause, rerun_counts):
    if root_cause in {"Scoring or Verifier Issue", "Task Or Environment Issue"}:
        return 1
    if root_cause in {"Agent Execution Issue", "Model-Agent Compatibility Issue"}:
        return 1 if any(label.startswith("rerun") for label in rerun_counts) else 2
    if root_cause == "Model Behavior Issue":
        return 3
    return 3

def parity_verdict(parity):
    checks = parity.get("checks") or []
    verdicts = [row.get("verdict") for row in checks]
    if "REGRESSION" in verdicts:
        return "REGRESSION"
    if "CONFIRMED ANOMALY" in verdicts:
        return "CONFIRMED ANOMALY"
    if verdicts and all(v == "EXPECTED BEHAVIOR" for v in verdicts):
        return "EXPECTED BEHAVIOR"
    if not checks or all(v == "UNVERIFIED" for v in verdicts):
        return "UNVERIFIED"
    return "NEEDS_INVESTIGATION"

def historical_label(category):
    if not category or not category.get("category"):
        return "UNVERIFIED"
    recurring = 0
    recurring += sum(1 for row in category.get("focus_low_or_unstable_combos", []) if row.get("n_sibling_matches", 0) > 0)
    recurring += sum(1 for row in category.get("focus_model_inversions", []) if row.get("sibling_matches"))
    recurring += sum(1 for row in category.get("focus_agent_inversions", []) if row.get("sibling_matches"))
    return "MIXED" if recurring else "STABLE"

def sentence_from_counts(benchmark, counts, cause_counts):
    parts = []
    if counts.get("negative_rows"):
        parts.append(f"{counts['negative_rows']} negative task-score rows")
    if counts.get("near_zero_outlier_rows"):
        parts.append(f"{counts['near_zero_outlier_rows']} near-zero outlier rows")
    if counts.get("high_trial_variance_rows"):
        parts.append(f"{counts['high_trial_variance_rows']} high-variance rows")
    if counts.get("model_inversions"):
        parts.append(f"{counts['model_inversions']} model-order inversions")
    if counts.get("agent_inversions"):
        parts.append(f"{counts['agent_inversions']} agent-order inversions")
    base = ", ".join(parts) if parts else "no structured task-score anomalies"
    if cause_counts:
        top_cause, top_count = max(cause_counts.items(), key=lambda item: item[1])
        return f"{benchmark} has {base}; downloaded trajectories most often classify as {top_cause} ({top_count} trials)."
    return f"{benchmark} has {base}; trajectory cause extraction was not available."

def action_for(root_cause, benchmark, cause_counts):
    if root_cause == "Scoring or Verifier Issue":
        return f"Audit {benchmark}'s verifier/scoring path before rerunning; then rerun affected displayed cells."
    if root_cause == "Model-Agent Compatibility Issue":
        return f"Fix the model-agent output contract for {benchmark}, then rerun the affected model/agent cells."
    if root_cause == "Agent Execution Issue":
        return f"Rerun affected {benchmark} cells after addressing missing artifacts, timeouts, or provider noise."
    if root_cause == "Task Or Environment Issue":
        return f"Inspect task setup for {benchmark} task clusters before rerunning."
    if root_cause == "Model Behavior Issue":
        return f"Review representative {benchmark} wrong-answer trajectories; rerun only if prompt or grading changed."
    return f"Collect more trajectory evidence for {benchmark} before deciding whether to rerun."

def tags_for(root_cause, counts, cause_counts, parity_label, hist_label):
    tags = {root_cause.lower().replace(" ", "-")}
    for key in ("negative_rows", "near_zero_outlier_rows", "high_trial_variance_rows", "model_inversions", "agent_inversions"):
        if counts.get(key):
            tags.add(key.replace("_", "-"))
    for key in cause_counts:
        tags.add(str(key).replace("_", "-"))
    tags.add(parity_label.lower().replace("_", "-"))
    tags.add(hist_label.lower())
    return sorted(tags)

def md_escape(value):
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

def build_markdown(report_data):
    lines = [
        f"## Leaderboard Anomaly Report - {report_data['meta']['date']}",
        "",
        "### Trend Summary",
    ]
    for item in report_data["summary"]["headline_findings"]:
        lines.append(f"- {item}")
    for item in report_data["summary"]["analysis_notes"]:
        lines.append(f"- Confidence caveat: {item}")
    lines.append("")
    lines.append(f"### Flagged Benchmarks ({len(report_data['findings'])})")
    lines.append("")
    for finding in report_data["findings"]:
        owner = finding["experiment_owner"]
        lines.extend([
            f"#### {finding['benchmark']}",
            f"- **Experiment owner**: {owner.get('people') or 'unknown'} - {owner.get('adapter_name') or 'no CSV match'}",
            f"- **Anomaly**: {finding['anomaly']}",
            f"- **Parity verdict**: {finding['parity_verdict']} - {finding['parity']}",
            f"- **Historical trend**: {finding['historical_trend']} - {finding['historical']}",
            f"- **Root cause hypothesis**: {finding['root_cause']} - {finding.get('root_cause_reason', '')}",
            f"- **Recommended action**: {finding['recommended_action']}",
            "",
        ])
    lines.append("### Action Priority Queue")
    lines.append("")
    for cause in ROOT_CAUSES:
        items = report_data["action_queue"].get(cause, [])
        lines.append(f"**{cause}**")
        if items:
            for item in items:
                lines.append(f"- {item}")
        else:
            lines.append("- none")
        lines.append("")
    finding = report_data["findings"][0] if report_data["findings"] else {}
    evidence = finding.get("evidence", {})
    near_zero = evidence.get("near_zero_outliers", [])
    exact_zero = evidence.get("exact_zero_tasks", [])
    lines.append("### Near-Zero Outliers")
    if near_zero:
        for row in near_zero[:20]:
            lines.append(f"- {finding['benchmark']}: task {row.get('task_name')} {row.get('model')}/{row.get('agent')} score={row.get('score')}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("### Exact-Zero Task Clusters")
    if exact_zero:
        lines.append(f"- {finding.get('benchmark')}: {len(exact_zero)} task(s): {', '.join(str(row.get('task_name')) for row in exact_zero[:30])}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("### Downloaded Trajectory Cause Table")
    table = evidence.get("trajectory_table", [])
    if table:
        lines.append("| Anomaly category | Cell | Rank | Reward | Trial | Cause | Rerun label |")
        lines.append("|---|---|---:|---:|---|---|---|")
        for row in table[:80]:
            trial_link = f"[{row.get('trial_id')}]({row.get('trial_uri')})" if row.get("trial_uri") else row.get("trial_id")
            cell = f"{row.get('benchmark')} / task {row.get('task_name')} / {row.get('model')} / {row.get('agent')}"
            lines.append(
                "| "
                + " | ".join([
                    md_escape(", ".join(row.get("anomaly_categories", []))),
                    md_escape(cell),
                    md_escape(row.get("trial_rank")),
                    md_escape(row.get("reward")),
                    md_escape(trial_link),
                    md_escape(row.get("cause")),
                    md_escape(row.get("rerun_label")),
                ])
                + " |"
            )
    else:
        lines.append("- no downloaded trajectory cause table available")
    lines.append("")
    lines.append("### Clean Benchmarks")
    clean = report_data["meta"].get("clean_benchmarks") or []
    lines.append(", ".join(clean) if clean else "none in focused benchmark mode")
    lines.append("")
    return "\n".join(lines)

focus_rows = load_json("/tmp/leaderboard_task_focus.json", [])
if not focus_rows:
    raise SystemExit("Missing /tmp/leaderboard_task_focus.json; run Step 1 first.")
benchmark = focus_rows[0]["benchmark"]
highlights = load_json("/tmp/task_score_highlights.json", {})
category = load_json("/tmp/category_consistency.json", {})
trial_causes = load_json("/tmp/anomaly_trial_causes.json", [])
parity = load_json("/tmp/parity_confirmation.json", {})

counts = highlight_counts(highlights)
cause_counts = count_by(trial_causes, "cause_code")
rerun_counts = count_by(trial_causes, "rerun_label")
root_cause = choose_root_cause(cause_counts, highlights)
priority = priority_for(root_cause, rerun_counts)
pv = parity_verdict(parity)
hist = historical_label(category)
owner = parity.get("experiment_owner") or {"adapter_name": "", "people": ""}
anomaly = sentence_from_counts(benchmark, counts, cause_counts)
parity_text = (
    f"{len(parity.get('checks') or [])} parity comparison(s); README insight: {parity.get('readme_insight') or 'not available'}"
    if parity else "Parity confirmation was not run."
)
historical_text = (
    f"Category comparison used {len(category.get('category_benchmarks_found') or [])} live benchmark(s) in {category.get('category') or 'unknown category'}."
    if category else "Category consistency was not run."
)
recommended_action = action_for(root_cause, benchmark, cause_counts)

root_reason = {
    "Scoring or Verifier Issue": "Verifier exceptions, negative failure rewards, or scoring sentinels dominate the evidence.",
    "Model-Agent Compatibility Issue": "The trajectory evidence points to a specific model/agent output contract mismatch.",
    "Agent Execution Issue": "The downloaded trajectories show missing artifacts, timeouts, or provider/runtime noise.",
    "Task Or Environment Issue": "Task-level clusters suggest setup or environment failures.",
    "Model Behavior Issue": "Runs completed and were scored wrong rather than failing in harness or verifier code.",
    "Needs More Investigation": "The current artifacts do not isolate a single cause.",
}[root_cause]

finding = {
    "benchmark": benchmark,
    "priority": priority,
    "root_cause": root_cause,
    "root_cause_reason": root_reason,
    "parity_verdict": pv,
    "historical_trend": hist,
    "experiment_owner": owner,
    "anomaly": anomaly,
    "parity": parity_text,
    "historical": historical_text,
    "recommended_action": recommended_action,
    "tags": tags_for(root_cause, counts, cause_counts, pv, hist),
    "evidence": {
        "highlight_counts": counts,
        "category_consistency": category,
        "trial_cause_counts": cause_counts,
        "rerun_label_counts": rerun_counts,
        "parity_checks": parity.get("checks") or [],
        "near_zero_outliers": highlights.get("near_zero_outlier_rows") or [],
        "exact_zero_tasks": highlights.get("all_zero_tasks") or [],
        "score_clusters": highlights.get("score_clusters") or {},
        "trajectory_table": trial_causes,
    },
}

action_queue = {cause: [] for cause in ROOT_CAUSES}
action_queue[root_cause].append(f"{benchmark}: {recommended_action}")
root_mix = ", ".join(f"{cause}={1 if cause == root_cause else 0}" for cause in ROOT_CAUSES)
headline = [
    anomaly,
    f"Root-cause mix: {root_mix}.",
]
if cause_counts:
    top_cause, top_count = max(cause_counts.items(), key=lambda item: item[1])
    headline.append(f"Downloaded trajectories most often show {top_cause} ({top_count} trial(s)).")
if category and category.get("category"):
    headline.append(f"Same-category check: {hist} across {category.get('category')} sibling benchmarks.")

report_data = {
    "meta": {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "scope": f"{benchmark} task-level leaderboard slice using displayed task cells and downloaded suspect trajectories.",
        "benchmarks_seen": 1,
        "benchmarks_flagged": 1,
        "clean_benchmarks": [],
    },
    "summary": {
        "headline_findings": headline,
        "analysis_notes": [
            "Report is built from displayed leaderboard RPCs and get_cell_trials, not raw trial-table rows.",
            "Parity is used only when model/agent comparability is exact or same-family; otherwise it is marked unverified.",
            "Historical trend is category consistency unless a separate results_over_time check was run.",
        ],
    },
    "findings": [finding],
    "action_queue": action_queue,
}

OUT_DIR.mkdir(parents=True, exist_ok=True)
timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
json_path = OUT_DIR / f"leaderboard-audit-{timestamp}.json"
md_path = OUT_DIR / f"leaderboard-audit-{timestamp}.md"
html_path = OUT_DIR / f"leaderboard-audit-{timestamp}.html"

markdown = build_markdown(report_data)
json_path.write_text(json.dumps(report_data, indent=2))
md_path.write_text(markdown)
if TEMPLATE.exists():
    template = TEMPLATE.read_text()
    html_text = template.replace("%%REPORT_DATA_JSON%%", json.dumps(report_data))
else:
    html_text = "<!doctype html><meta charset='utf-8'><title>Leaderboard Audit</title><pre>" + html.escape(markdown) + "</pre>"
html_path.write_text(html_text)

print(json.dumps({"json": str(json_path), "markdown": str(md_path), "html": str(html_path)}, indent=2))
PYEOF
```
