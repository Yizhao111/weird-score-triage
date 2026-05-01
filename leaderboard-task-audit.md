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

This repository has one audit generators:

- `generate_leaderboard_task_audit.py` — the focused benchmark/task audit. Use this for single-benchmark reports, score-aggregation diagnostics, and downloaded trial archive analysis. For a focused benchmark with extracted trials under `/tmp/<benchmark>/`, its `.html` output should be the Step 3 interactive report style, not the benchmark-card audit template.

For the focused task audit, prepare both leaderboard RPC files and then run the task generator with the benchmark name:

```bash
python3 generate_leaderboard_task_audit.py <benchmark>
```

The task generator writes artifacts named `leaderboard-task-audit[-<benchmark>]-<timestamp>.json`, `.md`, and `.html` under `~/harbor-audits/`.

For focused benchmark runs:
- `.json` and `.md` remain the leaderboard task-audit summary artifacts.
- `.html` is expected to use the Step 3 interactive layout when extracted trials are available under `/tmp/<benchmark>/`.
- Do not treat the benchmark-card audit HTML as the final deliverable for single-benchmark triage when the Step 3 inputs exist.

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

## Step 2 - Fetch all the task trial tar.gz for the focused benchmark

Then fetch all the task rows for the benchmark of interest. You will need to iterate through task_name, model, agent combinations and call `get_cell_trials` for each cell (this returns the exact trial IDs and download URIs the leaderboard uses), then download the tar.gz for each trial. This is necessary to confirm the displayed score, inspect trial-level data for root-cause analysis, and classify failures correctly.

Do **not** query the `trial` table directly — that includes rows the website excludes (infra failures, cancellations). Use `get_cell_trials` only.

> **Note on trial count vs scoring window:** `get_cell_trials` returns **all eligible trials** for a cell, which may be more than the `p_window` used for scoring. For example, with `p_window=3` a cell can have 5 trials on disk but only the 3 most recent count toward the displayed score. Expect the downloaded archive count to exceed the `n_trials` value shown in `get_leaderboard_task` rows.

**Fetch strategy:**
1. Call `get_cell_trials` for all cells in parallel (20 workers).
2. From the returned trials, sample **5 archives** to measure download size/time, then report the estimate to the user and ask whether to proceed.
3. After confirmation, download all remaining archives in parallel (20 workers), skipping any already on disk.

### Step 2a — Fetch task rows and resolve benchmark name

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

### Step 2b — Fetch `get_cell_trials` for all cells in parallel, sample 5 archives, then download all

```python
# Run as: python3 - <benchmark> (reads /tmp/<benchmark>_leaderboard_task.json written by Step 2a)
import json, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SUPABASE_URL = "https://hnkceovsiaczvcwhdlkb.supabase.co"
SUPABASE_KEY = "sb_publishable_kpc09uUk5qcIzVex3NWGAg_y5W7jr6t"
BENCHMARK = sys.argv[1].strip()
safe_name = re.sub(r"[^a-z0-9]", "", BENCHMARK.lower())
rows = json.loads(Path(f"/tmp/{safe_name}_leaderboard_task.json").read_text())
cells = list({(r["benchmark"], r["task_name"], r["model"], r["agent"]) for r in rows})

def rpc(name, body):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/rpc/{name}",
        data=json.dumps(body).encode(), method="POST",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                 "Content-Type": "application/json", "Referer": "https://harborsubabase.vercel.app/"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def fetch_cell(cell):
    bm, task, model, agent = cell
    trials = rpc("get_cell_trials", {"p_benchmark": bm, "p_task_name": task, "p_model": model, "p_agent": agent})
    return f"{task}/{model}/{agent}", trials

print(f"Fetching get_cell_trials for {len(cells)} cells (20 workers)...")
all_trials = {}
with ThreadPoolExecutor(max_workers=20) as ex:
    for key, trials in [f.result() for f in as_completed({ex.submit(fetch_cell, c): c for c in cells})]:
        all_trials[key] = trials
Path(f"/tmp/{safe_name}_all_cell_trials.json").write_text(json.dumps(all_trials, indent=2))
print(f"Total archives available: {sum(len(v) for v in all_trials.values())}")

# Build the full download list
ARCHIVE_ROOT = Path("/tmp/harbor-cell-trials") / BENCHMARK
to_download = []
for key, trials in all_trials.items():
    task, model, agent = key.split("/")
    out_dir = ARCHIVE_ROOT / task / model / agent
    out_dir.mkdir(parents=True, exist_ok=True)
    for t in trials:
        if t.get("trial_uri") and t.get("trial_id"):
            dest = out_dir / f"{t['trial_id']}.tar.gz"
            if not dest.exists():
                to_download.append((t["trial_uri"], dest))

# --- Sample 5 to estimate size/time, then ask user ---
import random, time
sample = random.sample(to_download, min(5, len(to_download)))
t0, total_sample_bytes = time.time(), 0
for uri, dest in sample:
    urllib.request.urlretrieve(uri, dest)
    total_sample_bytes += dest.stat().st_size
elapsed = time.time() - t0
avg_kb = total_sample_bytes / len(sample) / 1024
est_total_mb = len(to_download) * avg_kb / 1024
est_time_min = len(to_download) * (elapsed / len(sample)) / 20 / 60  # 20 workers
print(f"\nSample: {len(sample)} archives, avg {avg_kb:.0f} KB each, {elapsed:.1f}s")
print(f"Estimated full download: ~{est_total_mb:.0f} MB, ~{est_time_min:.1f} min (20 workers)")
print("Proceed? (yes/no)")
# --- After user confirms yes ---
```

After the user confirms, download the rest:

```python
remaining = [(uri, dest) for uri, dest in to_download if not dest.exists()]
print(f"Downloading {len(remaining)} archives (20 workers)...")
def dl(item):
    uri, dest = item
    try:
        urllib.request.urlretrieve(uri, dest)
        return dest.stat().st_size, None
    except Exception as e:
        return 0, str(e)

total_bytes, errors = 0, []
with ThreadPoolExecutor(max_workers=20) as ex:
    for size, err in [f.result() for f in as_completed({ex.submit(dl, i): i for i in remaining})]:
        total_bytes += size
        if err: errors.append(err)
print(f"Done: {total_bytes/1024/1024:.1f} MB downloaded. Errors: {len(errors)}")
if errors: print("First 5 errors:", errors[:5])
```

### Step 2c — Intentionally unzip the downloaded trial tar.gz files into `/tmp/<benchmark>/`

Do not inspect the archives in place only. After the download finishes, intentionally unpack every fetched trial archive under a benchmark-specific root so the extracted file tree is available for manual inspection, regex checks, and follow-on scripts.

Use a destination like:

- archives: `/tmp/harbor-cell-trials/<benchmark>/<task_name>/<model>/<agent>/<trial_id>.tar.gz`
- extracted tree: `/tmp/<benchmark>/<task_name>/<model>/<agent>/<trial_id>/`

Example:

```bash
BENCHMARK="$ARGUMENTS" \
python3 - << 'PYEOF'
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

benchmark = os.environ["BENCHMARK"].strip()
archive_root = Path("/tmp/harbor-cell-trials") / benchmark
extract_root = Path("/tmp") / benchmark
extract_root.mkdir(parents=True, exist_ok=True)

archives = sorted(archive_root.glob("*/*/*/*.tar.gz"))
if not archives:
    raise SystemExit(f"No archives found under {archive_root}")

def extract_one(tgz_path):
    rel = tgz_path.relative_to(archive_root)
    task_name, model, agent, filename = rel.parts
    trial_id = filename.removesuffix(".tar.gz")
    out_dir = extract_root / task_name / model / agent / trial_id
    if out_dir.exists():
        return "skipped"
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "-xzf", str(tgz_path), "-C", str(out_dir)], check=True)
    return "extracted"

errors = []
counts = {"extracted": 0, "skipped": 0}
with ThreadPoolExecutor(max_workers=(os.cpu_count() or 1) * 2) as ex:
    futures = {ex.submit(extract_one, p): p for p in archives}
    for f in as_completed(futures):
        try:
            counts[f.result()] += 1
        except Exception as e:
            errors.append(f"{futures[f]}: {e}")

print(f"Benchmark: {benchmark}")
print(f"Archives: {len(archives)}")
print(f"Extracted: {counts['extracted']}  Skipped (already done): {counts['skipped']}")
print(f"Errors: {len(errors)}")
if errors:
    print("\n".join(errors[:5]))
print(f"Extracted root: {extract_root}")
PYEOF
```

### Step 3 - Filter out the cases that we will need to re-run, then inspect the extracted trial contents for anomalous patterns in the task-level trial data. Look for:

**Quick sanity check first** — before running the stats script, verify the extraction looks healthy:

```bash
BENCHMARK="<benchmark>"
echo "Archives:    $(find /tmp/harbor-cell-trials/$BENCHMARK -name '*.tar.gz' | wc -l)"
echo "Extracted:   $(find /tmp/$BENCHMARK -mindepth 4 -maxdepth 4 -type d | wc -l)"
echo "trajectory:  $(find /tmp/$BENCHMARK -name 'trajectory.json' | wc -l)"
echo "test-stdout: $(find /tmp/$BENCHMARK -name 'test-stdout.txt' | wc -l)"
echo "exception:   $(find /tmp/$BENCHMARK -name 'exception.txt' | wc -l)"
```

A healthy benchmark has `test-stdout.txt` count ≈ archive count. Significantly fewer `trajectory.json` files (compared to test-stdout count) indicates trials that were killed before the agent started — common with early `AgentTimeoutError` or `EnvironmentStartTimeoutError`.

**Run the stats script against the archive tree:**

```bash
python3 scripts/generate_jobs_task_stats_long.py \
  --archive-dir /tmp/harbor-cell-trials/<benchmark> \
  --output /tmp/<benchmark>_task_stats_long.csv
```

This produces a CSV with one row per (task, model, agent) cell, showing `reward_mean`, `errortype` counts, and `log_patterns`. Use it to surface which cells have systematic exceptions before looking at individual trials.

- Check if all folders having the agent/trajectory.json file and if the verifier/test-stdout.txt and verifier/test-output.txt exist. Not having these files can be a sign of a failed harness run, which could explain anomalous scores.
- Use and adapt scripts/generate_jobs_task_stats_long.py to parse trial-level data and get the OKs and exceptions counts per task/model/agent.
- When judging whether `reward_std` is large, use two defaults:
  - Percentile within the benchmark to surface suspicious high-variance cells.
  - Mean-gap versus pooled std to decide whether an apparent model/agent inversion is likely meaningful or just noise.
  - Rationale: raw std alone is hard to interpret across different tasks and score scales; percentile gives a stable benchmark-local ranking of noisy cells, while pooled-std comparison answers the more important question of whether the observed score gap is large enough to support a real ordering claim.

- Look for the error list of Trial orchestration errors:
Trial orchestration / execution lifecycle:
EnvironmentStartTimeoutError, 
AgentSetupTimeoutError, 
AgentTimeoutError, 
VerifierTimeoutError, 
CancelledError

Agent/runtime errors:
NonZeroAgentExitCodeError, 
RuntimeError, 
ValueError
OSError, 
NotFoundError
ContextLengthExceededError
OutputLengthExceededError

Verifier errors:
DownloadVerifierDirError
AddTestsDirError, 
RewardFileNotFoundError
RewardFileEmptyError
FileNotFoundError
VerifierOutputParseError

Infrastructure / platform / API errors:
DaytonaError, 
DaytonaNotFoundError, 
DaytonaRateLimitError, 
RateLimitError, 
BadRequestError

But this is not an exhaustive list, so also look for any other unexpected error patterns in the logs, such as repeated timeouts, missing files, or common exception types that could indicate systematic issues with the harness or specific model/agent combinations.

### Step 3b — Generate the four TSV input tables

Run this script against the extracted trial tree to produce the four TSV files that `generate_step3_html_report.py` expects. It uses the corrected classifier (no HF-hub false positives — see Known classifier false positives below).

```bash
python3 - <benchmark> << 'PYEOF'
import csv, json, statistics, sys
from collections import defaultdict, Counter
from pathlib import Path

BENCHMARK  = sys.argv[1].strip()
BENCH_ROOT = Path(f"/tmp/{BENCHMARK}")
OUT_DIR    = Path(f"/tmp/{BENCHMARK}_step3_tables")
OUT_DIR.mkdir(exist_ok=True)

trials = []
for trial_dir in sorted(BENCH_ROOT.glob("*/*/*/*")):
    if not trial_dir.is_dir():
        continue
    parts = trial_dir.relative_to(BENCH_ROOT).parts
    if len(parts) != 4:
        continue
    task, model, agent, trial_id = parts
    run_dirs = [d for d in trial_dir.iterdir() if d.is_dir()]
    if not run_dirs:
        continue
    run_dir = run_dirs[0]

    result_path     = run_dir / "result.json"
    verifier_stdout = run_dir / "verifier" / "test-stdout.txt"
    exception_txt   = run_dir / "exception.txt"
    trajectory_path = run_dir / "agent" / "trajectory.json"

    reward, exception_type = None, "OK"
    if result_path.exists():
        try:
            r = json.loads(result_path.read_text())
            reward = (r.get("verifier_result") or {}).get("rewards", {}).get("reward")
            exception_type = ((r.get("exception_info") or {}).get("exception_type") or "OK")
        except Exception:
            pass

    verifier_text = verifier_stdout.read_text() if verifier_stdout.exists() else ""
    exc_text      = exception_txt.read_text()   if exception_txt.exists()    else ""
    low_exc       = exc_text.lower()

    # Proper classification — does NOT match the HF Hub "rate limits" advisory
    real_rl = any(t in exc_text.lower() + verifier_text.lower()
                  for t in ["credit balance is too low", "resource_exhausted",
                             "quota exceeded", "billing"])
    if real_rl:
        cat = "real_rate_limit"
    elif exception_type in ("AgentTimeoutError", "VerifierTimeoutError") or "timed out" in low_exc:
        cat = "timeout"
    elif exception_type == "RewardFileNotFoundError" or "rewardfilenotfounderror" in low_exc:
        cat = "reward_file_missing"
    elif exception_type not in ("OK", "", None):
        cat = f"other_exception:{exception_type}"
    elif reward is not None and reward <= -0.99:
        cat = "floor_score"
    elif reward is None:
        cat = "no_reward_ok"
    else:
        cat = "valid_run"

    low_all = (exc_text + verifier_text).lower()
    patterns = [p for p in [
        "agenttimeouterror","verifiertimeouterror","rewardfilenotfounderror",
        "nonzeroagentexitcodeerror","contextlengthexceedederror","outputlengthexceedederror",
        "ratelimiterror","daytonaerror","filenotfounderror","valueerror","runtimeerror",
        "importerror","syntaxerror","typeerror","keyerror","nameerror","attributeerror","traceback",
    ] if p in low_all]

    trials.append({
        "task": task, "model": model, "agent": agent, "trial_id": trial_id,
        "reward": reward, "exception_type": exception_type, "category": cat,
        "patterns": patterns,
        "has_trajectory": trajectory_path.exists(),
        "has_verifier_stdout": verifier_stdout.exists(),
        "trajectory_path": str(trajectory_path) if trajectory_path.exists() else "",
        "verifier_stdout_path": str(verifier_stdout) if verifier_stdout.exists() else "",
    })

cells = defaultdict(list)
for t in trials:
    cells[(t["task"], t["model"], t["agent"])].append(t)

all_stds = []
for ct in cells.values():
    rewards = [t["reward"] for t in ct if t["reward"] is not None]
    if len(rewards) >= 2:
        all_stds.append(statistics.stdev(rewards))
p75_std = sorted(all_stds)[int(len(all_stds) * 0.75)] if all_stds else 999

# ok_runs.tsv
ok_rows = []
for (task, model, agent), ct in sorted(cells.items()):
    rewards = [t["reward"] for t in ct if t["reward"] is not None]
    rm = round(statistics.mean(rewards), 6) if rewards else ""
    rs = round(statistics.stdev(rewards), 6) if len(rewards) >= 2 else ""
    exc_counts = Counter(t["exception_type"] for t in ct)
    exc_summary = " | ".join(f"{k}:{v}" for k, v in exc_counts.most_common())
    ok_rows.append({"task": task, "agent": agent, "model": model,
        "n_trials": len(ct),
        "ok_runs": sum(1 for t in ct if t["exception_type"] == "OK"),
        "exception_summary": exc_summary,
        "reward_mean": rm, "reward_std": rs,
        "reward_std_large_flag": "yes" if (rs != "" and rs > p75_std) else "no"})
with (OUT_DIR / "ok_runs.tsv").open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(ok_rows[0].keys()), delimiter="\t")
    w.writeheader(); w.writerows(ok_rows)

# error_categories.tsv
ec_rows = []
for (task, model, agent), ct in sorted(cells.items()):
    by_cat = defaultdict(list)
    for t in ct:
        if t["category"] != "valid_run":
            by_cat[t["category"]].append(t)
    for cat, cat_trials in sorted(by_cat.items()):
        pat_counts = Counter(p for t in cat_trials for p in t["patterns"])
        ec_rows.append({"task": task, "agent": agent, "model": model,
            "n_trials": len(ct), "error_category": cat,
            "matched_patterns": " | ".join(f"{p}:{n}" for p, n in pat_counts.most_common(8))})
if ec_rows:
    with (OUT_DIR / "error_categories.tsv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(ec_rows[0].keys()), delimiter="\t")
        w.writeheader(); w.writerows(ec_rows)

# error_types.tsv — one row per trial so build_summary Counter works correctly
et_rows = [{"error_name": t["exception_type"]} for t in trials]
with (OUT_DIR / "error_types.tsv").open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["error_name"], delimiter="\t")
    w.writeheader(); w.writerows(et_rows)

# missing_extracted_files.tsv
mf_rows = []
for (task, model, agent), ct in sorted(cells.items()):
    rewards = [t["reward"] for t in ct if t["reward"] is not None]
    rm = round(statistics.mean(rewards), 6) if rewards else ""
    rs = round(statistics.stdev(rewards), 6) if len(rewards) >= 2 else ""
    last_steps = []
    for t in ct:
        if t["trajectory_path"]:
            try:
                tj = json.loads(Path(t["trajectory_path"]).read_text())
                steps = tj.get("steps", [])
                last_steps.append(f"{t['trial_id']}: {json.dumps(steps[-1])}" if steps else "—")
            except Exception:
                last_steps.append("—")
        else:
            last_steps.append("—")
    mf_rows.append({"task": task, "agent": agent, "model": model,
        "reward_mean": rm, "reward_std": rs,
        "reward_std_large_flag": "yes" if (rs != "" and rs > p75_std) else "no",
        "missing_agent_trajectory_json":    sum(1 for t in ct if not t["has_trajectory"]),
        "missing_verifier_test_stdout_txt": sum(1 for t in ct if not t["has_verifier_stdout"]),
        "trajectory_json_path":    " | ".join(t["trajectory_path"] or "—" for t in ct),
        "verifier_test_stdout_path": " | ".join(t["verifier_stdout_path"] or "—" for t in ct),
        "trajectory_last_step": " || ".join(last_steps)})
with (OUT_DIR / "missing_extracted_files.tsv").open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(mf_rows[0].keys()), delimiter="\t")
    w.writeheader(); w.writerows(mf_rows)

print(f"ok_runs.tsv:                  {len(ok_rows)} rows")
print(f"error_categories.tsv:         {len(ec_rows)} rows")
print(f"error_types.tsv:              {len(et_rows)} rows (one per trial)")
print(f"missing_extracted_files.tsv:  {len(mf_rows)} rows")
print(f"Tables written to: {OUT_DIR}")
PYEOF
```

### Step 3c — Use a subagent to explain each orange row

After generating the Step 3 tables, use subagents to inspect the extracted files for each orange-highlighted merged-table row and write a compact explanation artifact for the HTML.

**When a row is orange**
- A row is orange when it is not an all-OK cell, meaning it does not have an all-success exception summary such as `OK:5`.
- These are the rows where reviewers benefit from a short explanation before opening raw logs.

**What the subagent should inspect**
- `exception.txt` when present
- `agent/trajectory.json` when present, or the fact that it is missing
- `verifier/test-stdout.txt` when present
- `verifier/reward.txt` when present
- `result.json` when present — check `agent_execution.start_time` and `end_time`. If the exception is `AgentTimeoutError` and `end_time − start_time` equals the configured agent timeout, the timeout is genuine (the agent hit the wall clock limit, not a transient cancellation)
- `trial.log` when present

**Output contract**
- Write `/tmp/<benchmark>_step3_tables/reasoning.tsv`
- Columns: `task`, `agent`, `model`, `reasoning`, `rerun_recommendation`, `rerun_justification`
- `reasoning` must be evidence-based and no more than 4 sentences
- Mention the dominant exception or missing-file pattern, whether the agent appears to have started, and whether the verifier appears to have run
- `rerun_recommendation` must be one of `yes`, `maybe`, or `no`
- `rerun_justification` must be concise and explain the rerun call using the benchmark policy below

**Rerun policy for subagents**
- Valid reruns: rate-limit failures, Daytona/platform failures, `CancelledError`, and selected `NonZeroAgentExitCodeError` cases without API-policy or refusal signals
- Usually not reruns: `RewardFileNotFoundError`, `AgentTimeoutError`, `VerifierTimeoutError`, stable wrong-answer behavior, and clear policy/refusal blocks
- The final HTML rerun summary is built from a merged pass: prefer subagent `rerun_recommendation` and `rerun_justification` when present, then fall back to the local heuristic pass for uncovered rows

**Subagent fan-out requirement**
- Split the orange rows across **8 subagents**.
- If the orchestrator is **Codex**, use **`gpt-5.4-mini`** for the 8 subagents.
- If the orchestrator is **Claude Code**, use **Claude 4.5 Haiku** for the 8 subagents.
- Assign each subagent a disjoint slice of orange rows so ownership is clear and the outputs can be merged without collisions.
- Merge the 8 partial outputs into a single `/tmp/<benchmark>_step3_tables/reasoning.tsv` keyed by `task`, `agent`, and `model`.

**Rationale**
- Orange rows are triage rows, not final diagnoses. The report needs one compact, local explanation per flagged cell so a reviewer can decide whether to drill into raw files.
- Subagents are a good fit because each `(task, agent, model)` cell is independent, the evidence lives in a small file set, and the work parallelizes well across flagged rows.
- Eight workers is a practical default for this step: it gives meaningful concurrency across orange rows without making the merge process or prompt coordination fragile.
- The 4-sentence limit forces the output to stay diagnostic and evidence-based rather than drifting into long summaries or speculation.
- A keyed TSV keeps the HTML deterministic: the renderer simply joins `reasoning.tsv` by `task/agent/model` and displays the result as a new column.

**Suggested Codex prompt**

```text
Analyze the assigned orange-highlighted Step 3 rows for <benchmark>. For each assigned (task, agent, model) cell that is not all-OK, inspect exception.txt, agent/trajectory.json, verifier/test-stdout.txt, and other local trial artifacts under /tmp/<benchmark>/ when useful. Write a partial TSV with columns task, agent, model, reasoning, rerun_recommendation, rerun_justification. Keep reasoning evidence-based and under 4 sentences, explain why the row is flagged, and classify reruns conservatively: yes for transient infra/quota/cancellation patterns, maybe for ambiguous NonZeroAgentExitCodeError cases without policy signals, and no for RewardFileNotFoundError, AgentTimeoutError, verifier timeout, policy/refusal, or stable behavior failures.
```

### Step 3d — Render the HTML report

```bash
BENCHMARK="<benchmark>"
DATETIME=$(date +%Y-%m-%d_%H%M)
python3 scripts/generate_step3_html_report.py \
  --benchmark "$BENCHMARK" \
  --tables-dir /tmp/${BENCHMARK}_step3_tables \
  --output ~/harbor-audits/${BENCHMARK}-step3-${DATETIME}.html
```

Open the report:

```
file:///Users/han/harbor-audits/<benchmark>-step3-<datetime>.html
```

This Step 3-style HTML is the intended final deliverable for focused benchmark triage. If `generate_leaderboard_task_audit.py <benchmark>` is used after extraction, its emitted `leaderboard-task-audit-<benchmark>-<timestamp>.html` should match this Step 3 layout rather than the benchmark-card audit layout.

The report now renders a single merged table with:
- per-cell `task`, `agent`, `model`, `n_trials`, exception summary, `reward_mean`, and `reward_std`
- clickable `trajectory_json_path` and `verifier_test_stdout_path`
- merged `error_category` and `matched_patterns`
- optional subagent-produced `reasoning`

The top summary panels show **Top Error Categories**, **Top Error Types**, and **Missing Extracted Files** totals. The table supports free-text search and dropdown filters for task/agent/model. The **Only orange rows** toggle is visible only on the Re-run Analysis tab. If `/tmp/<benchmark>_step3_tables/reasoning.tsv` exists, the HTML joins it automatically and shows a `reasoning` column.

**Orange row criteria:** a row is highlighted orange when `ok_runs < 3` (fewer than 3 successful trials, matching the `p_window=3` scoring window). A deeper orange (`row-missing`) is applied when trajectory or verifier stdout files are missing, or when `reward_std` is above the benchmark's 75th-percentile std.

The **Re-run Summary** panel (visible on the Re-run analysis tab) shows four aggregate metric boxes (cells reviewed, yes, maybe, no) followed by a **bullet list** of every reviewed orange cell. Each bullet is formatted as:

```
<YES|MAYBE|NO> — <task> / <agent> / <model> — <rerun_justification>
```

Bullets are colour-coded (green = yes, orange = maybe, grey = no) and sorted yes → maybe → no. The `generate_step3_html_report.py` script populates `#rerun-bullets` from `combined_rows` (which has subagent reasoning merged in) inside `fillRerunSummary()`.

The **Accuracy & Insight** tab contains:

1. **Agent × Model Score chart** — horizontal grouped bar chart (models on Y axis sorted by best score desc, agents as bar series with pastel colours, score labels at bar ends, legend on the right). Data source: `get_leaderboard` scores read from `/tmp/leaderboard_aggregate.json`. The script's `read_leaderboard_scores(benchmark)` function filters this file for the current benchmark and passes the result as `DATA.leaderboard_scores`.

2. **Six insight subsections** (2-column grid below the chart):
   - **Model Inversions (across this benchmark)** — task-level extracted trial data; flags stronger models scoring >5pp below weaker family peers on the same agent.
   - **Agent Inversions (across this benchmark)** — task-level extracted trial data; flags stronger agents scoring >5pp below weaker agents on the same model.
   - **Native Agent Underperformance** — task-level data; flags models scoring significantly lower on their native agent than on others.
   - **Cross-Family Surprises** — task-level data; flags weaker models outperforming frontier models from other families.
   - **Model Laggards (across leaderboard)** — `get_leaderboard` data; computes per-model cross-agent mean and flags models whose mean inverts expected family ranking (>3pp gap) or is negative.
   - **Harness Laggards (across leaderboard)** — `get_leaderboard` data; flags (model, agent) pairs where the agent score is ≥15pp below the model's best-agent score.

**Trajectory and stdout path cells** render as short labels (`trajectory.json1`, `trajectory.json2`, `test-stdout.txt1`, …). The full path is shown on native hover (title attribute) and the link opens the file directly. Hovering a `test-stdout.txt` label also shows a preview of the first 3 000 characters of that file in a tooltip panel.

If `/tmp/<benchmark>_inversion_analysis.json` exists, each non-empty subsection bullet that matches an entry's `match_key` expands to show a collapsible **"Root-cause analysis ▸"** block with the subagent-produced root cause and per-task notes (see Step 3e).

> **Note on `error_types.tsv`:** The script writes one row per trial (not one row per distinct type) so that `build_summary`'s `Counter` produces correct counts. Do not collapse this file to one row per type.

### Step 3e — Insight analysis with subagents

After Step 3d, open the **Accuracy & Insight** tab and check all six subsections. For any section that contains flagged items, run subagents to examine the extracted trajectories and produce a root-cause explanation that the HTML will render inline under each bullet as a collapsible **"Root-cause analysis ▸"** block.

This applies to all six sections:
- **Model Inversions** and **Agent Inversions** — computed from `ok_runs.tsv` `reward_mean`
- **Native Agent Underperformance** — same source
- **Cross-Family Surprises** — same source
- **Model Laggards** and **Harness Laggards** — computed from `get_leaderboard` aggregate scores

**When to run this step**
- Any bullet appears in any Accuracy & Insight subsection.
- Run after Step 3d so the report is already rendered and the finding list is known.

**Step 1 — Compute findings for all sections**

The HTML computes per-(model, agent) mean of `reward_mean` across all tasks from `ok_runs.tsv` for the task-level sections, and reads `/tmp/leaderboard_aggregate.json` for the leaderboard sections. Replicate both in Python to enumerate all flagged items and identify the worst tasks per finding:

```python
import csv, json
from collections import defaultdict
from pathlib import Path

rows = list(csv.DictReader(open(f"/tmp/<benchmark>_step3_tables/ok_runs.tsv"), delimiter="\t"))

MODEL_TIERS = {
    "gpt-5.4": 3, "gpt-5-mini": 2, "gpt-5-nano": 1,
    "claude-opus-4-6": 3, "claude-sonnet-4-6": 2, "claude-haiku-4-5-20251001": 1,
    "gemini-3.1-pro-preview": 2, "gemini-3-flash-preview": 1,
}
AGENT_TIERS = {"codex": 4, "claude-code": 4, "gemini-cli": 3, "terminus-2": 2}
NATIVE = {"gpt": "codex", "claude": "claude-code", "gemini": "gemini-cli"}

def family(m):
    if m.startswith("gpt"): return "openai"
    if m.startswith("claude"): return "anthropic"
    if m.startswith("gemini"): return "google"
    return ""

def native_agent(m):
    for prefix, agent in NATIVE.items():
        if m.startswith(prefix): return agent

grouped = defaultdict(list)
for r in rows:
    try: grouped[(r["model"], r["agent"])].append(float(r["reward_mean"]))
    except: pass
cell_mean = {k: sum(v)/len(v) for k, v in grouped.items()}

# --- Task-level sections (ok_runs.tsv) ---
# Model inversions: stronger model >5pp below weaker peer on same agent
for agent in sorted({a for _, a in cell_mean}):
    pairs = [(m, cell_mean[(m, agent)]) for m in MODEL_TIERS if (m, agent) in cell_mean]
    for m_s, s_s in pairs:
        for m_w, s_w in pairs:
            if family(m_s) != family(m_w) or MODEL_TIERS[m_s] <= MODEL_TIERS[m_w]: continue
            if s_w - s_s > 0.05:
                print(f"MODEL INVERSION agent={agent}: {m_s}={s_s:.3f} < {m_w}={s_w:.3f}")

# Agent inversions: stronger agent >5pp below weaker agent on same model
for model in sorted({m for m, _ in cell_mean}):
    pairs = [(a, cell_mean[(model, a)]) for a in AGENT_TIERS if (model, a) in cell_mean]
    for a_s, s_s in pairs:
        for a_w, s_w in pairs:
            if AGENT_TIERS.get(a_s, 0) <= AGENT_TIERS.get(a_w, 0): continue
            if s_w - s_s > 0.05:
                print(f"AGENT INVERSION model={model}: {a_s}={s_s:.3f} < {a_w}={s_w:.3f}")

# Native agent underperformance: native agent >10pp below another agent on same model
for model in MODEL_TIERS:
    nat = native_agent(model)
    if not nat: continue
    nat_score = cell_mean.get((model, nat))
    if nat_score is None: continue
    for agent in AGENT_TIERS:
        if agent == nat: continue
        other = cell_mean.get((model, agent))
        if other and other > nat_score + 0.10:
            print(f"NATIVE UNDERPERF {model}/{nat}={nat_score:.3f} < {model}/{agent}={other:.3f}")

# Cross-family surprises: weaker-family model beats frontier of another family by >5pp
frontier = {"openai": "gpt-5.4", "anthropic": "claude-opus-4-6", "google": "gemini-3.1-pro-preview"}
for agent in AGENT_TIERS:
    for model_w in MODEL_TIERS:
        sw = cell_mean.get((model_w, agent))
        if sw is None: continue
        for fam_s, model_s in frontier.items():
            if fam_s == family(model_w) or MODEL_TIERS[model_w] >= MODEL_TIERS[model_s]: continue
            ss = cell_mean.get((model_s, agent))
            if ss and sw > ss + 0.05:
                print(f"CROSS-FAMILY {model_w}/{agent}={sw:.3f} > {model_s}/{agent}={ss:.3f}")

# --- Leaderboard sections (leaderboard_aggregate.json) ---
lb = json.loads(Path("/tmp/leaderboard_aggregate.json").read_text())
lb_bm = [r for r in lb if r["benchmark"] == "<benchmark>"]
lb_cell = {(r["model"], r["agent"]): r["score"] for r in lb_bm}
lb_model_mean = defaultdict(list)
for r in lb_bm: lb_model_mean[r["model"]].append(r["score"])
lb_model_mean = {m: sum(v)/len(v) for m, v in lb_model_mean.items()}

# Model laggards: stronger model cross-agent mean >3pp below weaker peer, or negative
for m_s in MODEL_TIERS:
    for m_w in MODEL_TIERS:
        if family(m_s) != family(m_w) or MODEL_TIERS[m_s] <= MODEL_TIERS[m_w]: continue
        s, w = lb_model_mean.get(m_s), lb_model_mean.get(m_w)
        if s is not None and w is not None and w - s > 0.03:
            print(f"MODEL LAGGARD {m_s} avg={s:.3f} < {m_w} avg={w:.3f}")
    if lb_model_mean.get(m_s, 0) < 0:
        print(f"MODEL LAGGARD {m_s} negative mean={lb_model_mean[m_s]:.3f}")

# Harness laggards: agent >=15pp below that model's best agent score
for model in MODEL_TIERS:
    best = max((lb_cell.get((model, a), -999) for a in AGENT_TIERS), default=None)
    if best is None: continue
    for agent in AGENT_TIERS:
        score = lb_cell.get((model, agent))
        if score is not None and best - score >= 0.15:
            print(f"HARNESS LAGGARD {model}/{agent}={score:.3f} (best={best:.3f}, gap={best-score:.3f})")
```

For each flagged pair, find the worst tasks (largest per-task score gap) to focus subagent inspection:

```python
# Example for one finding pair
s_a = {r["task"]: float(r["reward_mean"]) for r in rows
       if r["agent"] == "<agent>" and r["model"] == "<model_a>" and r["reward_mean"]}
s_b = {r["task"]: float(r["reward_mean"]) for r in rows
       if r["agent"] == "<agent>" and r["model"] == "<model_b>" and r["reward_mean"]}
diffs = [(t, s_b[t] - s_a[t]) for t in set(s_a) & set(s_b)]
diffs.sort(key=lambda x: -x[1])
for t, d in diffs[:10]:
    print(f"  {t}: a={s_a[t]:.3f}  b={s_b[t]:.3f}  gap={d:.3f}")
```

**Step 2 — Fan out subagents to examine trajectories**

Spawn one subagent per finding in parallel. If orchestrator is **Claude Code**, use **Claude 4.5 Haiku**. If orchestrator is **Codex**, use **`gpt-5.4-mini`**.

Each subagent receives:
- The finding (models/agents involved, scores, section name)
- The top 5 worst-gap tasks
- The path template: `/tmp/<benchmark>/{task}/{model}/{agent}/{trial_id}/{run_dir}/`

The subagent should:
1. List trial IDs for the relevant models and agent on each task
2. Read `agent/trajectory.json` (or `agent/<agent>.txt`) — compare step count, answer mechanism, output format
3. Read `result.json` and `verifier/test-stdout.txt` for reward confirmation
4. Identify the consistent behavioural difference that explains the finding

**Output contract**

Each subagent returns a JSON object. Merge all into `/tmp/<benchmark>_inversion_analysis.json` (a JSON array). Each entry must include `match_type` and `match_key` fields so the HTML knows how to attach it to the correct bullet:

```json
[
  {
    "type": "model",
    "agent": "<agent>",
    "stronger_model": "<model>",
    "stronger_score": 0.180,
    "weaker_model": "<model>",
    "weaker_score": 0.236,
    "gap": 0.056,
    "root_cause": "<2-3 sentence explanation>",
    "task_notes": [
      {"task": "<task>", "note": "<1-2 sentence comparison>"},
      ...
    ]
  },
  {
    "type": "harness_laggard",
    "section": "Harness Laggards (across leaderboard)",
    "match_type": "prefix",
    "match_key": "<model>/<agent> (",
    "primary_model": "<model>",
    "agent": "<agent>",
    "score": 0.000,
    "root_cause": "<2-3 sentence explanation>",
    "task_notes": [...]
  },
  {
    "type": "cross_family",
    "section": "Cross-Family Surprises",
    "match_type": "contains",
    "match_key": "<frontier_model> best=",
    "primary_model": "<frontier_model_underperforming>",
    "agent": "<agent>",
    "root_cause": "<2-3 sentence explanation>",
    "task_notes": [...]
  }
]
```

**`match_type` and `match_key` rules — critical for HTML rendering**

The HTML matches each entry to the correct insight bullet using these fields. The bullet text format differs per section:

| Section | Bullet format | `match_type` | `match_key` example |
|---|---|---|---|
| Model Inversions | `stronger/agent=0.180 is below weaker/agent=0.236.` | `prefix` | `gpt-5.4/terminus-2=` |
| Agent Inversions | `model/stronger=0.500 is below model/weaker=0.600.` | `prefix` | `claude-opus-4-6/claude-code=` |
| Cross-Family Surprises | `weaker/agent=0.236 exceeds frontier best=0.180.` | `contains` | `claude-opus-4-6 best=` |
| Harness Laggards | `model/agent (0.0) is 77.8pp below model/best (77.8).` | `prefix` | `claude-opus-4-6/terminus-2 (` |
| Model Laggards | `model avg 45.2 is 12.3pp below model avg 57.5 — …` | `prefix` | `claude-opus-4-6 avg ` |
| Native Underperf. | `model/native=0.300 is below model/other=0.500.` | `prefix` | `claude-opus-4-6/claude-code=` |

For **Model/Agent Inversion** entries without explicit `match_type`/`match_key`, the HTML defaults to prefix matching on `stronger_model/agent=` (or `model/stronger_agent=`).

**Step 3 — Regenerate the HTML**

Re-run Step 3d. The script reads `/tmp/<benchmark>_inversion_analysis.json` automatically and renders a collapsible **"Root-cause analysis ▸"** block under each matching bullet across all six Accuracy & Insight subsections.

No flag is needed — if the file does not exist the tab renders normally without the detail blocks.

---

## Known classifier false positives

The `generate_leaderboard_task_audit.py` classifier and `generate_jobs_task_stats_long.py` log-pattern scanner both match keywords across the combined text of `exception.txt`, `verifier/test-stdout.txt`, and agent logs. Several patterns produce systematic false positives that must be manually verified before reporting.

### `rate_limit_noise` / `rate_limit_text` — HuggingFace Hub advisory

**Trigger:** `verifier/test-stdout.txt` for benchmarks that load datasets from HuggingFace Hub contains the unauthenticated-access warning:
> `Warning: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN to enable higher rate limits and faster downloads.`

The phrase "**rate limits**" in that advisory matches the classifier's `"rate limit"` keyword check, so **every trial for that benchmark** is tagged `rate_limit_noise` — even trials that completed successfully with a valid reward.

**How to verify:** Check whether real API rate-limit errors (credit exhaustion, `RESOURCE_EXHAUSTED`, `credit balance is too low`, `quota exceeded`) are present:
```bash
grep -rl "credit balance\|quota exceeded\|resource_exhausted\|billing" \
  /tmp/<benchmark> --include="test-stdout.txt" | wc -l
grep -rl "credit balance\|quota exceeded\|resource_exhausted\|billing" \
  /tmp/<benchmark> --include="claude-code.txt" --include="codex.txt" --include="gemini-cli.txt" | wc -l
```
If both counts are 0, the `rate_limit_noise` classification is entirely a false positive. Ignore it and re-classify using the proper category breakdown from the stats script.

**Affected benchmarks:** any benchmark that loads datasets via `datasets.load_from_disk()` or `datasets.load_dataset()` without an `HF_TOKEN` set in the docker environment (includes sldbench, and likely other science/research adapters).

### `RewardFileNotFoundError` on codex + certain models — silent file-write failure

**Trigger:** A `codex` agent running a model that uses `apply_patch` as its primary file-creation mechanism (observed with `gpt-5-nano` on codex v0.115.0) will call `apply_patch` through bash sessions that return empty output and exit code 0, meaning the command appears to succeed but no file is actually written. The verifier then reports `RewardFileNotFoundError` for the required output (e.g., `/app/law.py`).

**How to distinguish from a genuine missing-output failure:**
1. Check the agent trajectory (`agent/codex.txt` or `trajectory.json`): if the agent's reasoning shows it designed a correct solution and issued file-write commands but the verifier still can't find the output file, suspect the write mechanism.
2. Check whether the same model on a different agent (e.g., `terminus-2`) successfully produces the file.
3. Check whether stronger models on the same agent (e.g., `gpt-5-mini/codex`) do NOT show the same pattern — a model-specific rather than agent-wide failure points to the model's coding strategy, not a general harness bug.

**Classification:** `Model-Agent Compatibility Issue`, not `Model Behavior Issue`. The model reasoned correctly but its file-writing strategy is incompatible with the execution environment.

### `AgentTimeoutError` on stronger models — over-thorough reasoning, not capability failure

When a stronger model (e.g., `claude-opus-4-6`) times out on tasks where weaker models complete, the instinct is to flag a model inversion. Before doing so:

1. Check the trajectory step count — if the agent has 40–80+ steps and was mid-execution at timeout, it was working, not stuck.
2. Check the token count — if `n_input_tokens` is `null` (agent killed), compare with completed trials of the same model on other tasks to see if context growth is the issue.
3. Compute the **adjusted aggregate** by excluding timed-out cells. If the adjusted mean restores expected model ordering, classify as `Agent Execution Issue` (timeout budget) and recommend rerunning with a higher `timeout_multiplier`, not re-ranking the model.
