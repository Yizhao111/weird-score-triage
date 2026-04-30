#!/usr/bin/env python3
import csv
import importlib.util
import json
import math
import os
import re
import statistics
import sys
import tarfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LEADERBOARD_JSON = Path("/tmp/leaderboard.json")
LEADERBOARD_AGG_JSON = Path("/tmp/leaderboard_aggregate.json")
TRIAL_ARCHIVE_ROOT = Path("/tmp/harbor-cell-trials")
HARBOR_ADAPTERS = Path("/tmp/harbor/adapters")
MIX_JOBS = Path("/tmp/harbor-mix/benchmark_info_jobs")
STEP3_RENDERER = ROOT / "scripts" / "generate_step3_html_report.py"
OWNERS = ROOT / "experiment-track.csv"
OUT_DIR = Path.home() / "harbor-audits"

MODEL_TIERS = {
    "gpt-5.4": 3,
    "gpt-5-mini": 2,
    "gpt-5-nano": 1,
    "claude-opus-4-6": 3,
    "claude-sonnet-4-6": 2,
    "claude-haiku-4-5-20251001": 1,
    "gemini-3.1-pro-preview": 2,
    "gemini-3-flash-preview": 1,
}
FRONTIER_MODELS = {
    "openai": "gpt-5.4",
    "anthropic": "claude-opus-4-6",
    "google": "gemini-3.1-pro-preview",
}
AGENT_TIERS = {
    "codex": 3,
    "claude-code": 3,
    "gemini-cli": 2,
    "terminus-2": 1,
}
NATIVE_AGENT = {"gpt": "codex", "claude": "claude-code", "gemini": "gemini-cli"}
UNBOUNDED_SCORE_BENCHMARKS = {"algotune", "mlgym", "mlgym-bench", "sldbench"}
EXCLUDED_BENCHMARKS = {"deveval", "ds-1000", "featbench", "multi-swe-bench"}
ROOT_CAUSES = [
    "Scoring or Verifier Issue",
    "Task Or Environment Issue",
    "Agent Execution Issue",
    "Model-Agent Compatibility Issue",
    "Model Behavior Issue",
    "Needs More Investigation",
]
CONFIDENCE_Z = 1.96
MIN_INVERSION_GAP = 0.05
BOUNDED_SCORE_FLOOR = 0.05
RELATIVE_SEM_THRESHOLD = 0.35


def norm(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


ALIASES = {
    "bfcl": "berkeleyfunctioncallingleaderboardbfcl",
    "berkeleyfunctioncallingleaderboardbfcl": "bfcl",
    "swtbench": "swtbench",
    "swt_bench": "swtbench",
    "researchcodebench": "researchcodebench",
    "reaserchcodebench": "researchcodebench",
    "spreadsheetbenchverified": "spreadsheetbench",
    "spreadsheetbench": "spreadsheetbench",
    "mlgymbench": "mlgymbench",
}


def canon(value):
    n = norm(value)
    return ALIASES.get(n, n)


def load_json(path):
    with open(path) as f:
        return json.load(f)


def quantile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = int(math.floor(pos))
    upper = int(math.ceil(pos))
    if lower == upper:
        return ordered[lower]
    frac = pos - lower
    return ordered[lower] * (1 - frac) + ordered[upper] * frac


def iqr(values):
    q1 = quantile(values, 0.25)
    q3 = quantile(values, 0.75)
    if q1 is None or q3 is None:
        return 0.0
    return max(0.0, q3 - q1)


def sample_stats(values):
    if not values:
        return {"mean": None, "std": None, "sem": None, "ci95_half": None, "n": 0}
    mean = statistics.mean(values)
    std = statistics.stdev(values) if len(values) >= 2 else 0.0
    sem = std / math.sqrt(len(values)) if len(values) >= 2 else 0.0
    return {
        "mean": mean,
        "std": std,
        "sem": sem,
        "ci95_half": CONFIDENCE_Z * sem,
        "n": len(values),
    }


def scale_floor(mean_value, bounded):
    if bounded:
        return BOUNDED_SCORE_FLOOR
    baseline = abs(mean_value) * 0.1 if mean_value is not None else 0.0
    return max(baseline, 0.05)


def sem_ratio(sem_value, mean_value, bounded):
    if sem_value is None:
        return 0.0
    denom = max(abs(mean_value or 0.0), scale_floor(mean_value, bounded))
    return sem_value / denom if denom else 0.0


def uncertainty_halfwidth_threshold(values, bounded):
    if bounded:
        return 0.10
    spread = iqr(values)
    abs_median = quantile([abs(v) for v in values], 0.5) or 0.0
    return max(spread * 0.25, abs_median * 0.10, 0.05)


def pooled_sem(std_a, n_a, std_b, n_b):
    left = (std_a / math.sqrt(n_a)) if std_a is not None and n_a and n_a > 1 else 0.0
    right = (std_b / math.sqrt(n_b)) if std_b is not None and n_b and n_b > 1 else 0.0
    return math.sqrt(left * left + right * right)


def read_tar_member(tf, suffixes, max_chars=20000):
    if isinstance(suffixes, str):
        suffixes = [suffixes]
    for member in tf.getmembers():
        if any(member.name.endswith(suffix) for suffix in suffixes):
            extracted = tf.extractfile(member)
            if not extracted:
                return ""
            return extracted.read(max_chars).decode("utf-8", "replace")
    return ""


def classify_trial_archive(path):
    result = {}
    exception_text = ""
    verifier_text = ""
    agent_text = ""
    try:
        with tarfile.open(path, "r:gz") as tf:
            result_text = read_tar_member(tf, "result.json", 50000)
            if result_text:
                try:
                    result = json.loads(result_text)
                except Exception:
                    result = {}
            exception_text = read_tar_member(tf, "exception.txt", 20000)
            verifier_text = read_tar_member(tf, "verifier/test-stdout.txt", 20000)
            agent_text = read_tar_member(
                tf,
                ["agent/claude-code.txt", "agent/codex.txt", "agent/gemini-cli.txt"],
                20000,
            )
    except Exception as exc:
        return {
            "category": "tar_read_error",
            "reason": str(exc)[:180],
            "exception_type": "",
            "reward": None,
        }

    exception_info = result.get("exception_info") or {}
    exception_type = exception_info.get("exception_type") or ""
    reward = result.get("reward")
    if reward is None:
        verifier_result = result.get("verifier_result") or result.get("verifier") or {}
        if isinstance(verifier_result, dict):
            reward = verifier_result.get("reward")

    combined = "\n".join([exception_type, exception_text, verifier_text, agent_text])
    low = combined.lower()

    if any(term in low for term in ["resource_exhausted", "rate limit", "quota", "credit balance", "billing"]):
        category = "rate_limit_noise"
        reason = "quota, billing, or rate-limit text appeared in displayed trial logs"
    elif exception_type in {"AgentTimeoutError", "VerifierTimeoutError"} or "timed out" in low or "timeout" in low:
        category = "timeout_or_budget_issue"
        reason = exception_type or "timeout text appeared in logs"
    elif (
        "no such file or directory" in low
        or "missing required" in low
        or "rewardfilenotfounderror" in low
        or "answer.txt" in low and "not found" in low
        or "/app/law.py" in low and "no such file" in low
    ):
        category = "missing_submission_artifact"
        reason = "verifier or exception log reports a missing expected file"
    elif "metric error:" in low or "evaluation crashed:" in low or "traceback" in verifier_text.lower():
        category = "verifier_or_metric_error"
        reason = "verifier emitted a metric error, crash, or traceback"
    elif exception_type:
        category = "unclassified_failure"
        reason = exception_type
    elif isinstance(reward, (int, float)) and reward <= 0:
        category = "true_wrong_answer"
        reason = "completed trial with non-positive reward"
    else:
        category = "valid_run"
        reason = "no failure pattern found in downloaded archive"

    return {
        "category": category,
        "reason": reason[:180],
        "exception_type": exception_type,
        "reward": reward,
    }


def trial_archive_analysis(benchmarks, max_archives_per_benchmark=500):
    analysis = {}
    if not TRIAL_ARCHIVE_ROOT.is_dir():
        return analysis
    for benchmark in benchmarks:
        root = TRIAL_ARCHIVE_ROOT / benchmark
        if not root.is_dir():
            continue
        archives = sorted(root.glob("*/*/*/*.tar.gz"))[:max_archives_per_benchmark]
        category_counts = Counter()
        model_agent_counts = Counter()
        task_counts = Counter()
        samples = []
        for path in archives:
            rel = path.relative_to(root)
            if len(rel.parts) < 4:
                continue
            task_name, model, agent = rel.parts[0], rel.parts[1], rel.parts[2]
            trial_id = path.stem.replace(".tar", "")
            item = classify_trial_archive(path)
            category = item["category"]
            category_counts[category] += 1
            model_agent_counts[(model, agent, category)] += 1
            task_counts[(task_name, category)] += 1
            if len(samples) < 24:
                samples.append(
                    {
                        "task_name": task_name,
                        "model": model,
                        "agent": agent,
                        "trial_id": trial_id,
                        "category": category,
                        "reason": item["reason"],
                        "exception_type": item["exception_type"],
                        "reward": item["reward"],
                    }
                )
        analysis[benchmark] = {
            "source_dir": str(root),
            "archives_seen": len(archives),
            "category_counts": dict(sorted(category_counts.items())),
            "model_agent_category_counts": [
                {"model": model, "agent": agent, "category": category, "count": count}
                for (model, agent, category), count in sorted(
                    model_agent_counts.items(), key=lambda item: (-item[1], item[0])
                )[:40]
            ],
            "task_category_counts": [
                {"task_name": task, "category": category, "count": count}
                for (task, category), count in sorted(
                    task_counts.items(), key=lambda item: (-item[1], item[0])
                )[:40]
            ],
            "samples": samples,
        }
    return analysis


def model_family(model):
    if model.startswith("gpt"):
        return "openai"
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("gemini"):
        return "google"
    return None


def native_agent(model):
    for prefix, agent in NATIVE_AGENT.items():
        if model.startswith(prefix):
            return agent
    return None


def stronger_model(a, b):
    if model_family(a) != model_family(b):
        return False
    return MODEL_TIERS.get(a, 0) > MODEL_TIERS.get(b, 0)


def stronger_agent(a, b):
    return AGENT_TIERS.get(a, 0) > AGENT_TIERS.get(b, 0)


def stronger_weaker_model_pairs(models):
    for stronger in models:
        for weaker in models:
            if stronger != weaker and stronger_model(stronger, weaker):
                yield stronger, weaker


def stronger_weaker_agent_pairs(agents):
    for stronger in agents:
        for weaker in agents:
            if stronger != weaker and stronger_agent(stronger, weaker):
                yield stronger, weaker


def uses_bounded_scores(benchmark, scores):
    if norm(benchmark) in {norm(x) for x in UNBOUNDED_SCORE_BENCHMARKS}:
        return False
    return bool(scores) and all(0.0 <= s <= 1.0 for s in scores)


def aggregate_task_rows(rows):
    task_combos = defaultdict(list)
    for row in rows:
        task_combos[(row["benchmark"], row["task_name"])].append(
            (row["model"], row["agent"], float(row["score"]))
        )
    return task_combos


def filter_rows(rows, focus_benchmark=""):
    filtered = [row for row in rows if row.get("benchmark") not in EXCLUDED_BENCHMARKS]
    if focus_benchmark:
        target = focus_benchmark.lower()
        filtered = [row for row in filtered if row.get("benchmark", "").lower() == target]
    return filtered


def task_score_summaries(rows):
    grouped = defaultdict(list)
    for row in rows:
        key = (row["benchmark"], row["model"], row["agent"])
        grouped[key].append(float(row["score"]))
    summaries = {}
    for key, scores in grouped.items():
        ordered = sorted(scores)
        count = len(ordered)
        mid = count // 2
        median = ordered[mid] if count % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        summaries[key] = {
            "task_rows": count,
            "task_mean": round(sum(ordered) / count, 4),
            "task_median": round(median, 4),
            "task_min": round(ordered[0], 4),
            "task_max": round(ordered[-1], 4),
        }
    return summaries


def aggregate_benchmark_rows(rows):
    agg = {}
    std = {}
    for row in rows:
        key = (row["benchmark"], row["model"], row["agent"])
        agg[key] = round(float(row["score"]), 4)
        std[key] = round(float(row.get("score_std") or 0), 4)
    return agg, std


def aggregate(rows):
    combo_scores = defaultdict(list)
    combo_stds = defaultdict(list)
    for row in rows:
        key = (row["benchmark"], row["model"], row["agent"])
        combo_scores[key].append(float(row["score"]))
        combo_stds[key].append(float(row.get("score_std") or 0))
    agg = {key: round(sum(values) / len(values), 4) for key, values in combo_scores.items()}
    std = {
        key: round(sum(combo_stds[key]) / len(combo_stds[key]), 4)
        for key in combo_scores
    }
    return agg, std


def add_record(records, benchmark, kind, text, **extra):
    record = {"kind": kind, "text": text}
    record.update(extra)
    records[benchmark].append(record)


def detect_anomalies(agg, agg_std, task_combos):
    records = defaultdict(list)
    near_zeros = defaultdict(list)
    zero_tasks = defaultdict(list)
    stats = {}
    benchmarks = sorted({key[0] for key in agg})

    for benchmark in benchmarks:
        bench_data = {(m, a): s for (b, m, a), s in agg.items() if b == benchmark}
        bench_std = {(m, a): s for (b, m, a), s in agg_std.items() if b == benchmark}
        stats[benchmark] = bench_data
        models = sorted({m for m, _ in bench_data})
        agents = sorted({a for _, a in bench_data})
        all_scores = list(bench_data.values())
        bounded = uses_bounded_scores(benchmark, all_scores)

        if bounded:
            for (model, agent), score in bench_data.items():
                if score < -0.05:
                    add_record(
                        records,
                        benchmark,
                        "negative-score",
                        f"{model}/{agent} has negative score {score:.3f}.",
                        model=model,
                        agent=agent,
                        score=score,
                    )

        if bounded:
            for agent in agents:
                agent_scores = {
                    model: bench_data[(model, a)]
                    for model, a in bench_data
                    if a == agent
                }
                if not agent_scores:
                    continue
                median = sorted(agent_scores.values())[len(agent_scores) // 2]
                for model, score in agent_scores.items():
                    if score < 0.05 and median > 0.3:
                        near_zeros[benchmark].append(
                            {
                                "model": model,
                                "agent": agent,
                                "score": score,
                                "agent_median": median,
                            }
                        )

        for agent in agents:
            for stronger, weaker in stronger_weaker_model_pairs(models):
                strong_score = bench_data.get((stronger, agent))
                weak_score = bench_data.get((weaker, agent))
                if strong_score is None or weak_score is None:
                    continue
                if strong_score < weak_score - 0.05:
                    add_record(
                        records,
                        benchmark,
                        "model-inversion",
                        f"{stronger}/{agent} = {strong_score:.3f} < {weaker}/{agent} = {weak_score:.3f}.",
                        stronger=stronger,
                        weaker=weaker,
                        agent=agent,
                        delta=round(weak_score - strong_score, 4),
                    )

        for model in models:
            for stronger, weaker in stronger_weaker_agent_pairs(agents):
                strong_score = bench_data.get((model, stronger))
                weak_score = bench_data.get((model, weaker))
                if strong_score is None or weak_score is None:
                    continue
                if strong_score < weak_score - 0.05:
                    add_record(
                        records,
                        benchmark,
                        "agent-inversion",
                        f"{model}/{stronger} = {strong_score:.3f} < {model}/{weaker} = {weak_score:.3f}.",
                        model=model,
                        stronger=stronger,
                        weaker=weaker,
                        delta=round(weak_score - strong_score, 4),
                    )

        if bounded:
            collapsed = []
            for model in models:
                terminus_score = bench_data.get((model, "terminus-2"))
                others = [
                    bench_data.get((model, agent))
                    for agent in agents
                    if agent != "terminus-2" and bench_data.get((model, agent)) is not None
                ]
                if terminus_score is not None and others and terminus_score < 0.05 and max(others) > 0.30:
                    collapsed.append(model)
            if len(collapsed) >= 3:
                add_record(
                    records,
                    benchmark,
                    "terminus-collapse",
                    f"terminus-2 is near floor for {len(collapsed)} models while other agents exceed 0.30.",
                    models=collapsed,
                )

        for (model, agent), std in bench_std.items():
            score = bench_data.get((model, agent), 0)
            if score > 0.1 and std / score > 0.5:
                add_record(
                    records,
                    benchmark,
                    "high-variance",
                    f"{model}/{agent} mean={score:.3f}, std={std:.3f}.",
                    model=model,
                    agent=agent,
                    score=score,
                    std=std,
                )
            if std > score > 0.05:
                add_record(
                    records,
                    benchmark,
                    "std-gt-mean",
                    f"{model}/{agent} std={std:.3f} exceeds mean={score:.3f}.",
                    model=model,
                    agent=agent,
                    score=score,
                    std=std,
                )

        bench_tasks = {task: combos for (b, task), combos in task_combos.items() if b == benchmark}
        broken = [
            task
            for task, combos in bench_tasks.items()
            if combos and all(score == 0.0 for _, _, score in combos)
        ]
        if broken:
            zero_tasks[benchmark].extend(sorted(broken))

        if bounded and all_scores:
            min_score, max_score = min(all_scores), max(all_scores)
            if min_score > 0.95:
                add_record(records, benchmark, "saturation", f"All combos score above {min_score:.2f}.")
            if max_score < 0.15:
                add_record(records, benchmark, "floor", f"All combos score below {max_score:.2f}.")
            if max_score - min_score < 0.03 and len(all_scores) >= 4:
                add_record(
                    records,
                    benchmark,
                    "compression",
                    f"Score range is only {(max_score - min_score) * 100:.1f}pp.",
                )

        for model in models:
            agent = native_agent(model)
            if agent is None:
                continue
            native_score = bench_data.get((model, agent))
            if native_score is None:
                continue
            for other_agent in agents:
                if other_agent == agent:
                    continue
                other = bench_data.get((model, other_agent))
                if other is not None and other > native_score + 0.10:
                    add_record(
                        records,
                        benchmark,
                        "native-underperformance",
                        f"{model}/{agent} = {native_score:.3f} trails {model}/{other_agent} = {other:.3f}.",
                        model=model,
                        native_agent=agent,
                        other_agent=other_agent,
                        delta=round(other - native_score, 4),
                    )

        frontier_best = {}
        for family, frontier_model in FRONTIER_MODELS.items():
            best = max((bench_data.get((frontier_model, agent), -1) for agent in agents), default=-1)
            if best > 0:
                frontier_best[family] = (frontier_model, best)
        for (model, agent), score in bench_data.items():
            family = model_family(model)
            if family is None or MODEL_TIERS.get(model, 99) > 1:
                continue
            for frontier_family, (frontier_model, frontier_score) in frontier_best.items():
                if frontier_family == family:
                    continue
                if score > frontier_score + 0.15:
                    add_record(
                        records,
                        benchmark,
                        "cross-family-inversion",
                        f"{model}/{agent} = {score:.3f} exceeds {frontier_model} best={frontier_score:.3f}.",
                        model=model,
                        agent=agent,
                        frontier_model=frontier_model,
                        delta=round(score - frontier_score, 4),
                    )

    return records, near_zeros, zero_tasks, stats, benchmarks


def load_owners():
    owners = []
    with open(OWNERS, newline="") as f:
        for row in csv.DictReader(f):
            owners.append(row)
    return owners


def find_owner(benchmark, owners):
    target = canon(benchmark)
    for row in owners:
        if canon(row.get("Adapter Name")) == target:
            return {"adapter_name": row.get("Adapter Name", ""), "people": row.get("People", "").strip()}
    for row in owners:
        owner_key = canon(row.get("Adapter Name"))
        if target in owner_key or owner_key in target:
            return {"adapter_name": row.get("Adapter Name", ""), "people": row.get("People", "").strip()}
    return {"adapter_name": "", "people": ""}


def build_adapter_index():
    index = {}
    if not HARBOR_ADAPTERS.is_dir():
        return index
    for path in HARBOR_ADAPTERS.iterdir():
        if path.is_dir():
            index[canon(path.name)] = path
    return index


def find_adapter(benchmark, adapter_index):
    target = canon(benchmark)
    if target in adapter_index:
        return adapter_index[target]
    for key, path in adapter_index.items():
        if target in key or key in target:
            return path
    return None


def readme_insight(adapter_path):
    if not adapter_path:
        return "No matching Harbor adapter folder was found."
    readme = adapter_path / "README.md"
    if not readme.exists():
        return "The adapter has no README.md."
    text = readme.read_text(errors="replace")
    candidates = []
    for line in text.splitlines():
        clean = re.sub(r"\s+", " ", line.strip(" -#\t"))
        if len(clean) < 35 or len(clean) > 220:
            continue
        low = clean.lower()
        if any(term in low for term in ["score", "metric", "accuracy", "pass", "reward", "timeout", "negative", "evaluation"]):
            candidates.append(clean)
    if candidates:
        return candidates[0]
    for line in text.splitlines():
        clean = re.sub(r"\s+", " ", line.strip(" -#\t"))
        if 35 <= len(clean) <= 220:
            return clean
    return "README.md was present but did not expose a concise scoring note."


def parse_score(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if not match:
            return None
        number = float(match.group())
        return number / 100.0 if number > 1.5 else number
    return None


def parity_entries(adapter_path):
    if not adapter_path:
        return []
    path = adapter_path / "parity_experiment.json"
    if not path.exists():
        return []
    try:
        raw = load_json(path)
    except Exception:
        return []
    records = raw if isinstance(raw, list) else [raw]
    entries = []
    for record in records:
        if not isinstance(record, dict):
            continue
        score = None
        for metric in record.get("metrics") or []:
            if not isinstance(metric, dict):
                continue
            for key in ["harbor", "tb_adapter", "terminal_bench", "adapter", "adapted"]:
                score = parse_score(metric.get(key))
                if score is not None:
                    break
            if score is not None:
                break
        entries.append(
            {
                "model": record.get("model", ""),
                "agent": record.get("agent", ""),
                "score": score,
                "date": record.get("date", ""),
            }
        )
    return entries


def parity_link(adapter_path):
    if not adapter_path:
        return ""
    path = adapter_path / "parity_experiment.json"
    if not path.exists():
        return ""
    return f"https://github.com/harbor-framework/harbor/blob/main/adapters/{adapter_path.name}/parity_experiment.json"


def agent_equivalent(a, b):
    left, right = norm(a), norm(b)
    if left == right:
        return True
    groups = [
        {"codex", "codexcli", "codexcloud"},
        {"claudecode", "claudecodecli"},
        {"geminicli"},
        {"terminus2", "terminus1", "terminus"},
    ]
    return any(left in group and right in group for group in groups)


def parity_summary(benchmark, bench_data, adapter_path):
    entries = parity_entries(adapter_path)
    if not adapter_path:
        return "UNVERIFIED", "No matching adapter folder was found for parity comparison."
    if not entries:
        return "UNVERIFIED", "No parity_experiment.json baseline was available for this adapter."

    comparable = []
    for (model, agent), live_score in bench_data.items():
        for entry in entries:
            parity_score = entry.get("score")
            if parity_score is None:
                continue
            same_model = norm(entry.get("model")) == norm(model)
            same_agent = agent_equivalent(entry.get("agent"), agent)
            if same_model and same_agent:
                comparable.append((model, agent, live_score, parity_score, entry.get("date")))
    if not comparable:
        refs = ", ".join(
            f"{e['model']}/{e['agent']}" for e in entries[:3] if e.get("model") or e.get("agent")
        )
        return "UNVERIFIED", f"Parity exists but does not directly match live audited model/agent cells. Reference entries: {refs or 'not named'}."

    deltas = [(live - parity, model, agent, live, parity, date) for model, agent, live, parity, date in comparable]
    worst = min(deltas, key=lambda item: item[0])
    best = max(deltas, key=lambda item: item[0])
    if worst[0] < -0.10:
        verdict = "REGRESSION"
        chosen = worst
        direction = "below"
    elif best[0] > 0.10:
        verdict = "NEEDS_INVESTIGATION"
        chosen = best
        direction = "above"
    else:
        verdict = "EXPECTED BEHAVIOR"
        chosen = max(deltas, key=lambda item: abs(item[0]))
        direction = "near"
    delta, model, agent, live, parity, date = chosen
    return (
        verdict,
        f"{model}/{agent} live={live:.3f} is {direction} parity={parity:.3f} by {delta:+.3f}"
        + (f" from {date}." if date else "."),
    )


def history_file_index():
    if not MIX_JOBS.is_dir():
        return {}
    return {canon(path.stem): path for path in MIX_JOBS.glob("*.json")}


def find_history_file(benchmark, index):
    target = canon(benchmark)
    if target in index:
        return index[target]
    for key, path in index.items():
        if target in key or key in target:
            return path
    return None


def score_from_result_entry(result):
    scores = result.get("scores") or []
    preferred = ["accuracy_overall", "accuracy", "score", "resolved_rate", "pass_rate", "success_rate"]
    for metric in preferred:
        for item in scores:
            if item.get("metric") == metric and isinstance(item.get("value"), (int, float)):
                return float(item["value"])
    numeric = [float(item["value"]) for item in scores if isinstance(item.get("value"), (int, float))]
    return numeric[0] if numeric else None


def collect_history_series(results_over_time):
    series = defaultdict(list)
    if isinstance(results_over_time, dict):
        for key, value in results_over_time.items():
            if "/" not in key:
                continue
            model, agent = key.split("/", 1)
            if isinstance(value, list):
                series[(model, agent)].extend(float(x) for x in value if isinstance(x, (int, float)))
            elif isinstance(value, (int, float)):
                series[(model, agent)].append(float(value))
        return series
    if isinstance(results_over_time, list):
        for row in sorted([x for x in results_over_time if isinstance(x, dict)], key=lambda x: x.get("date", "")):
            for result in row.get("results", []):
                model = result.get("model")
                agent = result.get("agent") or result.get("system_description") or result.get("system")
                score = score_from_result_entry(result)
                if model and agent and score is not None:
                    series[(model, agent)].append(score)
    return series


def history_summary(benchmark, bench_data, history_index):
    path = find_history_file(benchmark, history_index)
    if not path:
        return "UNVERIFIED", "No mix-analyzer history file matched this benchmark."
    try:
        payload = load_json(path)
    except Exception as exc:
        return "UNVERIFIED", f"History file {path.name} could not be parsed: {exc}."
    series = collect_history_series(payload.get("results_over_time"))
    deltas = []
    for (model, agent), live_score in bench_data.items():
        for (hist_model, hist_agent), values in series.items():
            if norm(hist_model) == norm(model) and norm(hist_agent) == norm(agent) and values:
                deltas.append((live_score - values[-1], model, agent, live_score, values[-1]))
                break
    if not deltas:
        return "UNVERIFIED", f"History file {path.name} had no directly comparable model/agent rows."
    regressions = [item for item in deltas if item[0] < -0.10]
    inflations = [item for item in deltas if item[0] > 0.10]
    if regressions and inflations:
        label = "MIXED"
        chosen = sorted(deltas, key=lambda item: abs(item[0]), reverse=True)[0]
    elif regressions:
        label = "REGRESSION"
        chosen = min(regressions, key=lambda item: item[0])
    elif inflations:
        label = "INFLATION"
        chosen = max(inflations, key=lambda item: item[0])
    else:
        label = "STABLE"
        chosen = sorted(deltas, key=lambda item: abs(item[0]), reverse=True)[0]
    delta, model, agent, live, hist = chosen
    return label, f"{model}/{agent} live={live:.3f}, latest history={hist:.3f}, delta={delta:+.3f} from {path.name}."


def choose_root_cause(records, near_zero, zero_tasks):
    kinds = Counter(record["kind"] for record in records)
    if any(kind in kinds for kind in ["negative-score", "saturation", "floor", "compression"]):
        return "Scoring or Verifier Issue", 1
    if zero_tasks:
        return "Task Or Environment Issue", 1
    if kinds.get("terminus-collapse") or len(near_zero) >= 3:
        return "Agent Execution Issue", 1
    if kinds.get("native-underperformance") or near_zero:
        return "Model-Agent Compatibility Issue", 2
    if kinds.get("model-inversion") or kinds.get("cross-family-inversion"):
        return "Model Behavior Issue", 2
    return "Needs More Investigation", 3


def action_for(root_cause, benchmark):
    actions = {
        "Scoring or Verifier Issue": f"{benchmark}: audit score scale, verifier output, and floor semantics before accepting leaderboard aggregates.",
        "Task Or Environment Issue": f"{benchmark}: inspect exact-zero task environments and rerun after fixing missing files or setup.",
        "Agent Execution Issue": f"{benchmark}: inspect displayed trials with get_cell_trials and classify timeouts, missing artifacts, and harness failures.",
        "Model-Agent Compatibility Issue": f"{benchmark}: rerun the affected model/agent cells and check adapter output format compatibility.",
        "Model Behavior Issue": f"{benchmark}: review trajectories for completed valid runs before treating the inversion as a model quality signal.",
        "Needs More Investigation": f"{benchmark}: collect displayed trial evidence and parity/history baselines before assigning ownership.",
    }
    return actions[root_cause]


def summarize_anomaly(records, near_zero, zero_tasks):
    if zero_tasks:
        return f"{len(zero_tasks)} task(s) are exact-zero across all displayed model/agent combinations."
    if near_zero:
        first = near_zero[0]
        return (
            f"{len(near_zero)} near-zero outlier cell(s); "
            f"{first['model']}/{first['agent']} scored {first['score']:.3f} while the agent median was {first['agent_median']:.3f}."
        )
    if records:
        return records[0]["text"]
    return "Benchmark was flagged by aggregate checks but needs additional classification."


def make_tags(records, near_zero, zero_tasks, history_label, parity_verdict):
    tags = sorted({record["kind"] for record in records})
    if near_zero:
        tags.append("near-zero")
    if zero_tasks:
        tags.append("exact-zero-tasks")
    if history_label:
        tags.append(history_label.lower().replace("_", "-"))
    if parity_verdict:
        tags.append(parity_verdict.lower().replace(" ", "-"))
    return tags


def score_rows(bench_data, reverse=True, limit=5):
    return [
        {"model": model, "agent": agent, "score": score}
        for (model, agent), score in sorted(bench_data.items(), key=lambda item: item[1], reverse=reverse)[:limit]
    ]


def anomaly_score_keys(records):
    keys = defaultdict(set)
    for record in records:
        kind = record.get("kind")
        if kind == "model-inversion":
            agent = record.get("agent")
            for model_key in ["stronger", "weaker"]:
                model = record.get(model_key)
                if model and agent:
                    keys[(model, agent)].add(kind)
        elif kind == "agent-inversion":
            model = record.get("model")
            for agent_key in ["stronger", "weaker"]:
                agent = record.get(agent_key)
                if model and agent:
                    keys[(model, agent)].add(kind)
        elif kind == "native-underperformance":
            model = record.get("model")
            native = record.get("native_agent")
            other = record.get("other_agent")
            if model and native:
                keys[(model, native)].add(kind)
            if model and other:
                keys[(model, other)].add(kind)
        elif record.get("model") and record.get("agent"):
            keys[(record["model"], record["agent"])].add(kind)
    return [
        {"model": model, "agent": agent, "tags": sorted(tags)}
        for (model, agent), tags in sorted(keys.items())
    ]


def score_diagnostics(benchmark, bench_data, task_summaries, limit=8):
    rows = []
    for (model, agent), displayed_score in bench_data.items():
        summary = task_summaries.get((benchmark, model, agent))
        if not summary:
            continue
        task_mean = summary["task_mean"]
        delta = round(task_mean - displayed_score, 4)
        row = {
            "model": model,
            "agent": agent,
            "displayed_score": displayed_score,
            "task_mean": task_mean,
            "task_median": summary["task_median"],
            "task_min": summary["task_min"],
            "task_max": summary["task_max"],
            "task_rows": summary["task_rows"],
            "mean_minus_displayed": delta,
        }
        rows.append(row)
    rows.sort(key=lambda row: abs(row["mean_minus_displayed"]), reverse=True)
    return rows[:limit]


def build_findings(records, near_zeros, zero_tasks, stats, benchmarks, task_summaries, archive_analysis):
    owners = load_owners()
    adapter_index = build_adapter_index()
    history_index = history_file_index()
    flagged = [
        benchmark
        for benchmark in benchmarks
        if records.get(benchmark) or near_zeros.get(benchmark) or zero_tasks.get(benchmark)
    ]
    findings = []
    action_queue = {key: [] for key in ROOT_CAUSES}

    for benchmark in flagged:
        bench_records = records.get(benchmark, [])
        nz = near_zeros.get(benchmark, [])
        zt = zero_tasks.get(benchmark, [])
        bench_data = stats[benchmark]
        adapter_path = find_adapter(benchmark, adapter_index)
        root_cause, priority = choose_root_cause(bench_records, nz, zt)
        parity_verdict, parity_text = parity_summary(benchmark, bench_data, adapter_path)
        history_label, history_text = history_summary(benchmark, bench_data, history_index)
        readme_note = readme_insight(adapter_path)
        recommendation = action_for(root_cause, benchmark)
        owner = find_owner(benchmark, owners)
        finding = {
            "benchmark": benchmark,
            "priority": priority,
            "root_cause": root_cause,
            "parity_verdict": parity_verdict,
            "parity_link": parity_link(adapter_path),
            "historical_trend": history_label,
            "experiment_owner": owner,
            "anomaly": summarize_anomaly(bench_records, nz, zt),
            "parity": f"{parity_text} README note: {readme_note}",
            "historical": history_text,
            "recommended_action": recommendation,
            "tags": make_tags(bench_records, nz, zt, history_label, parity_verdict),
            "near_zero_outliers": nz[:25],
            "exact_zero_tasks": zt[:50],
            "anomaly_records": bench_records[:75],
            "top_scores": score_rows(bench_data, True, 8),
            "bottom_scores": score_rows(bench_data, False, 8),
            "score_basis": {
                "displayed_score": "get_leaderboard benchmark aggregate",
                "task_distribution": "get_leaderboard_task rows summarized for diagnostics",
            },
            "anomaly_score_keys": anomaly_score_keys(bench_records),
            "task_score_diagnostics": score_diagnostics(benchmark, bench_data, task_summaries),
            "trial_archive_analysis": archive_analysis.get(benchmark, {}),
        }
        findings.append(finding)
        action_queue[root_cause].append(recommendation)

    findings.sort(key=lambda item: (item["priority"], item["benchmark"]))
    return findings, action_queue, flagged


def examples(findings, tag, limit=3):
    out = []
    for finding in findings:
        for record in finding.get("anomaly_records", []):
            if record["kind"] == tag:
                out.append(f"{finding['benchmark']} ({record['text']})")
                break
        if len(out) >= limit:
            break
    return out


def trend_summary(findings, clean_benchmarks):
    cause_counts = Counter(finding["root_cause"] for finding in findings)
    agent_examples = examples(findings, "agent-inversion")
    model_examples = examples(findings, "model-inversion")
    scoring_examples = examples(findings, "negative-score") or examples(findings, "floor") or examples(findings, "compression")
    zero_examples = [
        f"{finding['benchmark']} ({len(finding.get('exact_zero_tasks', []))} exact-zero tasks)"
        for finding in findings
        if finding.get("exact_zero_tasks")
    ][:3]
    lines = []
    lines.append(
        "Agent-harness anomalies appear in "
        + (", ".join(agent_examples) if agent_examples else "no strong repeated examples from the aggregate checks")
        + "."
    )
    lines.append(
        "Model-order inversions appear in "
        + (", ".join(model_examples) if model_examples else "no strong repeated examples from the aggregate checks")
        + "."
    )
    lines.append(
        "Scoring/verifier risks appear in "
        + (", ".join(scoring_examples) if scoring_examples else "no bounded-score negative/floor/compression checks")
        + "."
    )
    lines.append(
        "Task-level exact-zero clusters appear in "
        + (", ".join(zero_examples) if zero_examples else "no benchmark-wide exact-zero task clusters")
        + "."
    )
    mix = ", ".join(f"{cause}={cause_counts.get(cause, 0)}" for cause in ROOT_CAUSES)
    lines.append(f"Root-cause mix: {mix}.")
    notes = [
        "Parity and history are treated as direct evidence only when model and agent names matched after normalization.",
        f"{len(clean_benchmarks)} benchmark(s) were clean under the configured aggregate checks.",
    ]
    return lines, notes


def build_markdown(report_data):
    lines = [
        f"## Leaderboard Anomaly Report - {report_data['meta']['generated_at']}",
        "",
        "### Trend Summary",
    ]
    for item in report_data["summary"]["headline_findings"]:
        lines.append(f"- {item}")
    for item in report_data["summary"]["analysis_notes"]:
        lines.append(f"- Confidence caveat: {item}")
    lines.extend(["", f"### Flagged Benchmarks ({len(report_data['findings'])})", ""])

    for finding in report_data["findings"]:
        owner = finding["experiment_owner"]
        owner_text = owner.get("people") or "unknown"
        adapter_text = owner.get("adapter_name") or "not mapped"
        diagnostics = finding.get("task_score_diagnostics") or []
        if diagnostics:
            top_diag = diagnostics[0]
            score_note = (
                f"{top_diag['model']}/{top_diag['agent']} displayed={top_diag['displayed_score']:.3f}, "
                f"task_mean={top_diag['task_mean']:.3f}, task_median={top_diag['task_median']:.3f}, "
                f"range={top_diag['task_min']:.3f}-{top_diag['task_max']:.3f}, "
                f"delta={top_diag['mean_minus_displayed']:+.3f}."
            )
        else:
            score_note = "No task-level score rows were available for diagnostic comparison."
        archive = finding.get("trial_archive_analysis") or {}
        if archive.get("archives_seen"):
            categories = ", ".join(
                f"{category}={count}"
                for category, count in sorted(archive.get("category_counts", {}).items())
            )
            archive_note = f"{archive['archives_seen']} downloaded trial archive(s): {categories or 'no categories'}."
        else:
            archive_note = "No downloaded trial archives found under /tmp/harbor-cell-trials for this benchmark."
        lines.extend(
            [
                f"#### {finding['benchmark']}",
                f"- **Experiment owner**: {owner_text} - {adapter_text}",
                f"- **Anomaly**: {finding['anomaly']}",
                f"- **Score aggregation**: {score_note}",
                f"- **Downloaded trial archives**: {archive_note}",
                f"- **[Parity]({finding['parity_link']}) verdict**: {finding['parity_verdict']} - {finding['parity']}"
                if finding.get("parity_link")
                else f"- **Parity verdict**: {finding['parity_verdict']} - {finding['parity']}",
                f"- **Historical trend**: {finding['historical_trend']} - {finding['historical']}",
                f"- **Root cause hypothesis**: {finding['root_cause']}",
                f"- **Recommended action**: {finding['recommended_action']}",
                "",
            ]
        )

    lines.append("### Action Priority Queue")
    for category, items in report_data["action_queue"].items():
        lines.append("")
        lines.append(f"**{category}**")
        if items:
            for item in items:
                lines.append(f"- {item}")
        else:
            lines.append("- none")

    near_zero_lines = []
    zero_lines = []
    for finding in report_data["findings"]:
        if finding.get("near_zero_outliers"):
            samples = ", ".join(
                f"{x['model']}/{x['agent']}={x['score']:.3f}" for x in finding["near_zero_outliers"][:4]
            )
            near_zero_lines.append(f"- {finding['benchmark']}: {samples}")
        if finding.get("exact_zero_tasks"):
            zero_lines.append(f"- {finding['benchmark']}: {len(finding['exact_zero_tasks'])} task(s)")

    lines.extend(["", "### Near-Zero Outliers"])
    lines.extend(near_zero_lines or ["none"])
    lines.extend(["", "### Exact-Zero Task Clusters"])
    lines.extend(zero_lines or ["none"])
    lines.extend(["", "### Clean Benchmarks"])
    lines.append(", ".join(report_data["meta"]["clean_benchmarks"]) or "none")
    lines.append("")
    return "\n".join(lines)


def load_step3_renderer():
    spec = importlib.util.spec_from_file_location("generate_step3_html_report", STEP3_RENDERER)
    if not spec or not spec.loader:
        raise RuntimeError(f"Unable to load Step 3 renderer from {STEP3_RENDERER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generate_step3_tables(benchmark):
    bench_root = Path("/tmp") / benchmark
    if not bench_root.is_dir():
        raise FileNotFoundError(f"Extracted trial root not found: {bench_root}")

    out_dir = Path("/tmp") / f"{benchmark}_step3_tables"
    out_dir.mkdir(parents=True, exist_ok=True)

    trials = []
    for trial_dir in sorted(bench_root.glob("*/*/*/*")):
        if not trial_dir.is_dir():
            continue
        parts = trial_dir.relative_to(bench_root).parts
        if len(parts) != 4:
            continue
        task, model, agent, trial_id = parts
        run_dirs = [d for d in trial_dir.iterdir() if d.is_dir()]
        if not run_dirs:
            continue
        run_dir = run_dirs[0]

        result_path = run_dir / "result.json"
        verifier_stdout = run_dir / "verifier" / "test-stdout.txt"
        exception_txt = run_dir / "exception.txt"
        trajectory_path = run_dir / "agent" / "trajectory.json"

        reward = None
        exception_type = "OK"
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text())
                verifier_result = result.get("verifier_result") or {}
                rewards = verifier_result.get("rewards") or {}
                reward = rewards.get("reward")
                exception_type = ((result.get("exception_info") or {}).get("exception_type") or "OK")
            except Exception:
                pass

        verifier_text = verifier_stdout.read_text() if verifier_stdout.exists() else ""
        exc_text = exception_txt.read_text() if exception_txt.exists() else ""
        low_exc = exc_text.lower()

        real_rl = any(
            token in (low_exc + verifier_text.lower())
            for token in ["credit balance is too low", "resource_exhausted", "quota exceeded", "billing"]
        )
        if real_rl:
            category = "real_rate_limit"
        elif exception_type in ("AgentTimeoutError", "VerifierTimeoutError") or "timed out" in low_exc:
            category = "timeout"
        elif exception_type == "RewardFileNotFoundError" or "rewardfilenotfounderror" in low_exc:
            category = "reward_file_missing"
        elif exception_type not in ("OK", "", None):
            category = f"other_exception:{exception_type}"
        elif reward is not None and reward <= -0.99:
            category = "floor_score"
        elif reward is None:
            category = "no_reward_ok"
        else:
            category = "valid_run"

        low_all = (exc_text + verifier_text).lower()
        patterns = [
            pattern
            for pattern in [
                "agenttimeouterror",
                "verifiertimeouterror",
                "rewardfilenotfounderror",
                "nonzeroagentexitcodeerror",
                "contextlengthexceedederror",
                "outputlengthexceedederror",
                "ratelimiterror",
                "daytonaerror",
                "filenotfounderror",
                "valueerror",
                "runtimeerror",
                "importerror",
                "syntaxerror",
                "typeerror",
                "keyerror",
                "nameerror",
                "attributeerror",
                "traceback",
            ]
            if pattern in low_all
        ]

        trials.append(
            {
                "task": task,
                "model": model,
                "agent": agent,
                "trial_id": trial_id,
                "reward": reward,
                "exception_type": exception_type,
                "category": category,
                "patterns": patterns,
                "has_trajectory": trajectory_path.exists(),
                "has_verifier_stdout": verifier_stdout.exists(),
                "trajectory_path": str(trajectory_path) if trajectory_path.exists() else "",
                "verifier_stdout_path": str(verifier_stdout) if verifier_stdout.exists() else "",
            }
        )

    cells = defaultdict(list)
    for trial in trials:
        cells[(trial["task"], trial["model"], trial["agent"])].append(trial)

    all_stds = []
    for cell_trials in cells.values():
        rewards = [trial["reward"] for trial in cell_trials if trial["reward"] is not None]
        if len(rewards) >= 2:
            all_stds.append(statistics.stdev(rewards))
    p75_std = sorted(all_stds)[int(len(all_stds) * 0.75)] if all_stds else 999

    ok_rows = []
    ec_rows = []
    mf_rows = []

    for (task, model, agent), cell_trials in sorted(cells.items()):
        rewards = [trial["reward"] for trial in cell_trials if trial["reward"] is not None]
        reward_mean = round(statistics.mean(rewards), 6) if rewards else ""
        reward_std = round(statistics.stdev(rewards), 6) if len(rewards) >= 2 else ""
        exc_counts = Counter(trial["exception_type"] for trial in cell_trials)
        ok_rows.append(
            {
                "task": task,
                "agent": agent,
                "model": model,
                "n_trials": len(cell_trials),
                "ok_runs": sum(1 for trial in cell_trials if trial["exception_type"] == "OK"),
                "exception_summary": " | ".join(f"{key}:{value}" for key, value in exc_counts.most_common()),
                "reward_mean": reward_mean,
                "reward_std": reward_std,
                "reward_std_large_flag": "yes" if (reward_std != "" and reward_std > p75_std) else "no",
            }
        )

        by_cat = defaultdict(list)
        for trial in cell_trials:
            if trial["category"] != "valid_run":
                by_cat[trial["category"]].append(trial)
        for category, cat_trials in sorted(by_cat.items()):
            pat_counts = Counter(pattern for trial in cat_trials for pattern in trial["patterns"])
            ec_rows.append(
                {
                    "task": task,
                    "agent": agent,
                    "model": model,
                    "n_trials": len(cell_trials),
                    "error_category": category,
                    "matched_patterns": " | ".join(
                        f"{pattern}:{count}" for pattern, count in pat_counts.most_common(8)
                    ),
                }
            )

        last_steps = []
        for trial in cell_trials:
            if trial["trajectory_path"]:
                try:
                    trajectory = json.loads(Path(trial["trajectory_path"]).read_text())
                    steps = trajectory.get("steps", [])
                    if steps:
                        last_steps.append(f"{trial['trial_id']}: {json.dumps(steps[-1])}")
                except Exception:
                    pass
        mf_rows.append(
            {
                "task": task,
                "agent": agent,
                "model": model,
                "reward_mean": reward_mean,
                "reward_std": reward_std,
                "reward_std_large_flag": "yes" if (reward_std != "" and reward_std > p75_std) else "no",
                "missing_agent_trajectory_json": sum(1 for trial in cell_trials if not trial["has_trajectory"]),
                "missing_verifier_test_stdout_txt": sum(
                    1 for trial in cell_trials if not trial["has_verifier_stdout"]
                ),
                "trajectory_json_path": " | ".join(
                    trial["trajectory_path"] for trial in cell_trials if trial["trajectory_path"]
                ),
                "verifier_test_stdout_path": " | ".join(
                    trial["verifier_stdout_path"] for trial in cell_trials if trial["verifier_stdout_path"]
                ),
                "trajectory_last_step": " || ".join(last_steps),
            }
        )

    with (out_dir / "ok_runs.tsv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ok_rows[0].keys()), delimiter="\t")
        writer.writeheader()
        writer.writerows(ok_rows)

    with (out_dir / "error_categories.tsv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ec_rows[0].keys()), delimiter="\t")
        writer.writeheader()
        writer.writerows(ec_rows)

    with (out_dir / "error_types.tsv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["error_name"], delimiter="\t")
        writer.writeheader()
        writer.writerows({"error_name": trial["exception_type"]} for trial in trials)

    with (out_dir / "missing_extracted_files.tsv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(mf_rows[0].keys()), delimiter="\t")
        writer.writeheader()
        writer.writerows(mf_rows)

    return out_dir


def render_step3_html(benchmark, html_path):
    tables_dir = generate_step3_tables(benchmark)
    step3 = load_step3_renderer()
    ok_rows = step3.read_tsv(tables_dir / "ok_runs.tsv")
    error_category_rows = step3.read_tsv(tables_dir / "error_categories.tsv")
    error_type_rows = step3.read_tsv(tables_dir / "error_types.tsv")
    missing_rows = step3.read_tsv(tables_dir / "missing_extracted_files.tsv")
    reasoning_rows = step3.read_optional_tsv(tables_dir / "reasoning.tsv")
    combined_rows = step3.build_combined_rows(ok_rows, error_category_rows, missing_rows, reasoning_rows)
    data = {
        "ok_rows": ok_rows,
        "error_category_rows": error_category_rows,
        "error_type_rows": error_type_rows,
        "missing_rows": missing_rows,
        "reasoning_rows": reasoning_rows,
        "combined_rows": combined_rows,
        "summary": step3.build_summary(ok_rows, error_category_rows, error_type_rows, missing_rows),
    }
    html = step3.render_html(benchmark, data)
    html_path.write_text(html, encoding="utf-8")


def write_outputs(report_data):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    focus = report_data["meta"].get("focus_benchmark")
    suffix = f"-{focus}" if focus else ""
    json_path = OUT_DIR / f"leaderboard-task-audit{suffix}-{timestamp}.json"
    md_path = OUT_DIR / f"leaderboard-task-audit{suffix}-{timestamp}.md"
    html_path = OUT_DIR / f"leaderboard-task-audit{suffix}-{timestamp}.html"

    json_path.write_text(json.dumps(report_data, indent=2, sort_keys=True))
    md_path.write_text(build_markdown(report_data))
    if not focus:
        raise RuntimeError("Focused benchmark is required for Step 3 HTML output.")
    render_step3_html(focus, html_path)
    return json_path, md_path, html_path


def main():
    focus_benchmark = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""
    if focus_benchmark in {"all", "*"}:
        focus_benchmark = ""
    raw_rows = load_json(LEADERBOARD_JSON)
    rows = filter_rows(raw_rows, focus_benchmark)
    score_rows_source = "task RPC averaged by script"
    if LEADERBOARD_AGG_JSON.exists():
        aggregate_rows = filter_rows(load_json(LEADERBOARD_AGG_JSON), focus_benchmark)
        agg, std = aggregate_benchmark_rows(aggregate_rows)
        score_rows_source = "benchmark RPC get_leaderboard"
    else:
        aggregate_rows = []
        agg, std = aggregate(rows)
    task_combos = aggregate_task_rows(rows)
    task_summaries = task_score_summaries(rows)
    records, near_zeros, zero_tasks, stats, benchmarks = detect_anomalies(agg, std, task_combos)
    archive_analysis = trial_archive_analysis(benchmarks)
    findings, action_queue, flagged = build_findings(
        records, near_zeros, zero_tasks, stats, benchmarks, task_summaries, archive_analysis
    )
    clean = sorted(set(benchmarks) - set(flagged))
    headline, notes = trend_summary(findings, clean)

    report_data = {
        "meta": {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip(),
            "scope": (
                "Focused Harbor leaderboard audit for "
                f"{focus_benchmark}. "
                if focus_benchmark
                else "Harbor leaderboard RPCs with p_min_trials=3 and p_window=3, filtered to tracked OpenAI, Anthropic, and Google model families. "
            )
            + "Benchmark scores come from get_leaderboard when /tmp/leaderboard_aggregate.json is present; task rows come from get_leaderboard_task.",
            "focus_benchmark": focus_benchmark,
            "benchmarks_seen": len(benchmarks),
            "benchmarks_flagged": len(findings),
            "clean_benchmarks": clean,
            "rows_analyzed": len(rows),
            "raw_rows_fetched": len(raw_rows),
            "aggregate_rows_analyzed": len(aggregate_rows),
            "excluded_benchmarks": sorted(EXCLUDED_BENCHMARKS),
            "score_rows_source": score_rows_source,
            "trial_archive_root": str(TRIAL_ARCHIVE_ROOT),
        },
        "summary": {
            "headline_findings": headline,
            "analysis_notes": notes,
        },
        "findings": findings,
        "action_queue": action_queue,
    }
    paths = write_outputs(report_data)
    print(json.dumps({key: str(path) for key, path in zip(["json", "markdown", "html"], paths)}, indent=2))
    print(f"benchmarks_seen={len(benchmarks)} benchmarks_flagged={len(findings)} rows_analyzed={len(rows)}")


if __name__ == "__main__":
    main()
