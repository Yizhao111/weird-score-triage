#!/usr/bin/env python3
import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render an interactive HTML report from Step 3 TSV outputs."
    )
    parser.add_argument("--benchmark", required=True, help="Benchmark name, e.g. sldbench.")
    parser.add_argument(
        "--tables-dir",
        type=Path,
        required=True,
        help="Directory containing ok_runs.tsv, error_categories.tsv, error_types.tsv, and missing_extracted_files.tsv.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output HTML path.",
    )
    return parser.parse_args()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def read_optional_tsv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return read_tsv(path)


def canonical_output_path(benchmark: str, output: Path, date_str: str) -> Path:
    return output.parent / f"{benchmark}-step3-report-{date_str}.html"


def build_summary(ok_rows, error_category_rows, error_type_rows, missing_rows):
    error_categories = Counter(row["error_category"] for row in error_category_rows)
    error_types = Counter(row["error_name"] for row in error_type_rows)
    missing_totals = Counter()
    for row in missing_rows:
        for key in (
            "missing_agent_trajectory_json",
            "missing_verifier_test_stdout_txt",
        ):
            missing_totals[key] += int(row[key] or 0)
    return {
        "ok_rows": len(ok_rows),
        "error_category_rows": len(error_category_rows),
        "error_type_rows": len(error_type_rows),
        "missing_file_rows": len(missing_rows),
        "error_categories": error_categories.most_common(),
        "error_types": error_types.most_common(20),
        "missing_totals": dict(missing_totals),
    }


def build_combined_rows(ok_rows, error_category_rows, missing_rows, reasoning_rows):
    ok_index = {
        (row["task"], row["agent"], row["model"]): row
        for row in ok_rows
    }
    missing_index = {
        (row["task"], row["agent"], row["model"]): row
        for row in missing_rows
    }
    reasoning_index = {
        (row["task"], row["agent"], row["model"]): row
        for row in reasoning_rows
    }
    category_index = {}
    for row in error_category_rows:
        key = (row["task"], row["agent"], row["model"])
        category_index.setdefault(key, []).append(row)

    combined_rows = []
    all_keys = sorted(set(ok_index) | set(category_index) | set(missing_index) | set(reasoning_index))
    for key in all_keys:
        ok_row = ok_index.get(key, {})
        missing_row = missing_index.get(key, {})
        reasoning_row = reasoning_index.get(key, {})
        categories = category_index.get(key, [])
        base = {
            "task": key[0],
            "agent": key[1],
            "model": key[2],
            "n_trials": ok_row.get("n_trials") or (categories[0]["n_trials"] if categories else ""),
            "ok_runs": ok_row.get("ok_runs", "0"),
            "exception_summary": ok_row.get("exception_summary", ""),
            "reward_mean": ok_row.get("reward_mean") or missing_row.get("reward_mean", ""),
            "reward_std": ok_row.get("reward_std") or missing_row.get("reward_std", ""),
            "reward_std_large_flag": (
                ok_row.get("reward_std_large_flag")
                or missing_row.get("reward_std_large_flag")
                or "no"
            ),
            "missing_agent_trajectory_json": missing_row.get("missing_agent_trajectory_json", "0"),
            "missing_verifier_test_stdout_txt": missing_row.get("missing_verifier_test_stdout_txt", "0"),
            "trajectory_json_path": missing_row.get("trajectory_json_path", ""),
            "verifier_test_stdout_path": missing_row.get("verifier_test_stdout_path", ""),
            "trajectory_last_step": missing_row.get("trajectory_last_step", ""),
            "reasoning": reasoning_row.get("reasoning", ""),
        }
        error_categories = " | ".join(
            row["error_category"] for row in categories if row.get("error_category")
        )
        matched_patterns = " || ".join(
            (
                f"{row['error_category']}: {row['matched_patterns']}"
                if row.get("matched_patterns")
                else row["error_category"]
            )
            for row in categories
            if row.get("error_category")
        )
        combined_rows.append(
            {
                **base,
                "error_category": error_categories,
                "matched_patterns": matched_patterns,
            }
        )
    return combined_rows


def render_html(benchmark: str, data: dict) -> str:
    payload = json.dumps(data)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__BENCHMARK__ Step 3 Report</title>
<style>
:root {{
  --bg: #f6f7f9;
  --paper: #ffffff;
  --ink: #17202a;
  --muted: #667085;
  --line: #d0d5dd;
  --blue: #175cd3;
  --red: #b42318;
  --orange: #b54708;
  --green: #067647;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  color: var(--ink);
  background: var(--bg);
  font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
header {{
  padding: 28px clamp(18px, 4vw, 48px) 18px;
  border-bottom: 1px solid var(--line);
}}
h1 {{
  margin: 0 0 8px;
  font-size: clamp(28px, 4vw, 46px);
  line-height: 1;
}}
.meta {{ color: var(--muted); max-width: 900px; }}
.wrap {{ padding: 20px clamp(18px, 4vw, 48px) 40px; }}
.stats {{
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}}
.stat {{
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--paper);
}}
.stat b {{ display: block; font-size: 28px; }}
.stat span {{ color: var(--muted); font-size: 12px; text-transform: uppercase; }}
.summary-grid {{
  display: grid;
  grid-template-columns: 1.3fr 1fr 1fr;
  gap: 12px;
  margin-bottom: 18px;
}}
.panel {{
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--paper);
}}
.panel h2 {{
  margin: 0 0 10px;
  font-size: 15px;
}}
.panel ul {{
  margin: 0;
  padding-left: 18px;
}}
.chart-panel {{
  margin: 0 0 16px;
}
.chart-panel.hidden {{
  display: none;
}
.chart-frame {{
  width: 100%;
  overflow-x: auto;
  overflow-y: hidden;
}
.chart-caption {{
  margin: 0 0 10px;
  color: var(--muted);
  font-size: 13px;
}
.tabs {{
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 16px 0 10px;
}}
.tab {{
  border: 1px solid var(--line);
  background: var(--paper);
  color: var(--ink);
  padding: 9px 12px;
  border-radius: 8px;
  cursor: pointer;
}}
.tab.active {{
  border-color: var(--blue);
  color: var(--blue);
}}
.controls {{
  display: grid;
  grid-template-columns: 2fr repeat(3, minmax(150px, 1fr)) minmax(180px, 220px);
  gap: 10px;
  margin-bottom: 12px;
}}
input, select {{
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--paper);
  color: var(--ink);
  font: inherit;
}}
.toggle-filter {{
  display: flex;
  align-items: center;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--paper);
  white-space: nowrap;
  color: var(--ink);
  cursor: pointer;
}}
.toggle-filter.active {{
  border-color: var(--orange);
  background: #fff3e0;
  color: var(--orange);
}}
.table-wrap {{
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow-x: auto;
  overflow-y: hidden;
  -webkit-overflow-scrolling: touch;
  background: var(--paper);
}}
table {{
  width: 100%;
  min-width: 100%;
  border-collapse: collapse;
}}
.table-wrap[data-tab="rerun"] table,
.table-wrap[data-tab="high-difficulty"] table {{
  min-width: 2200px;
}}
.table-wrap[data-tab="high-difficulty"] table {{
  min-width: 1100px;
}
thead {{
  background: #f8fafc;
}}
th, td {{
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}}
th {{
  font-size: 12px;
  color: var(--muted);
  text-transform: uppercase;
  cursor: pointer;
}}
tr:last-child td {{ border-bottom: 0; }}
.mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
.count-bad {{ color: var(--red); font-weight: 700; }}
.count-warn {{ color: var(--orange); font-weight: 700; }}
.count-good {{ color: var(--green); font-weight: 700; }}
.row-missing td {{ background: #fff7ed; }}
.row-all-ok td {{ background: #fff3e0; }}
.task-group td {{
  background: #eef4ff;
  font-weight: 700;
  border-top: 1px solid var(--line);
}
.task-child td:first-child {{
  padding-left: 24px;
}
.task-summary td {{
  background: #f8fafc;
  font-weight: 700;
  border-top: 1px dashed var(--line);
}
.task-score {{
  color: var(--blue);
  font-weight: 700;
}
.std-outlier {{ color: var(--orange); font-weight: 700; }}
.hidden {{ display: none; }}
.sort-indicator {{ margin-left: 6px; color: var(--blue); }}
.path-link {{
  color: var(--blue);
  text-decoration: none;
  border-bottom: 1px dotted var(--blue);
}}
.path-link:hover {{ text-decoration: underline; }}
.tooltip-wrap {{
  position: relative;
}}
.tooltip-panel {{
  display: none;
  position: absolute;
  left: 0;
  top: calc(100% - 6px);
  z-index: 20;
  min-width: 420px;
  max-width: min(760px, 80vw);
  max-height: 360px;
  overflow: auto;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
  box-shadow: 0 10px 30px rgba(16, 24, 40, 0.18);
  color: var(--ink);
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}}
.inline-help {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  margin-left: 6px;
  border: 1px solid var(--blue);
  border-radius: 999px;
  background: #eff8ff;
  color: var(--blue);
  font-size: 11px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
  vertical-align: middle;
}}
.inline-help .tooltip-panel {{
  left: auto;
  right: 0;
}
.tooltip-wrap:hover .tooltip-panel {{
  display: block;
}}
@media (max-width: 960px) {{
  .stats, .summary-grid, .controls {{ grid-template-columns: 1fr; }}
  .tooltip-panel {{
    min-width: 280px;
    max-width: 90vw;
  }}
}}
</style>
</head>
<body>
<header>
  <h1>__BENCHMARK__ Step 3 Report</h1>
  <div class="meta">Generated __GENERATED__. Interactive report over the extracted-trial audit tables: merged run summary and missing extracted files.</div>
</header>
<div class="wrap">
  <div class="stats">
    <div class="stat"><b id="stat-ok"></b><span>OK Rows</span></div>
    <div class="stat"><b id="stat-cat"></b><span>Error Category Rows</span></div>
    <div class="stat"><b id="stat-type"></b><span>Error Type Rows</span></div>
    <div class="stat"><b id="stat-missing"></b><span>Missing File Rows</span></div>
  </div>

  <div class="summary-grid">
    <div class="panel">
      <h2>Top Error Categories</h2>
      <ul id="top-categories"></ul>
    </div>
    <div class="panel">
      <h2>Top Error Types</h2>
      <ul id="top-types"></ul>
    </div>
    <div class="panel">
      <h2>Missing Extracted Files</h2>
      <ul id="missing-totals"></ul>
    </div>
  </div>

  <div class="tabs">
    <button class="tab active" data-tab="rerun">Re-run analysis</button>
    <button class="tab" data-tab="high-difficulty">High Difficulty</button>
  </div>

  <div class="controls">
    <input id="search" type="search" placeholder="Filter by task, agent, model, or pattern">
    <select id="task-filter"><option value="">All tasks</option></select>
    <select id="agent-filter"><option value="">All agents</option></select>
    <select id="model-filter"><option value="">All models</option></select>
    <button id="orange-only" class="toggle-filter" type="button" aria-pressed="false">Only orange rows</button>
    <select id="difficulty-band" class="hidden">
      <option value="">All tasks</option>
      <option value="high">High difficulty (top 25%)</option>
      <option value="above-median">Above median</option>
      <option value="below-median">Below median</option>
      <option value="low">Low difficulty (bottom 25%)</option>
    </select>
  </div>

  <div id="difficulty-chart-panel" class="panel chart-panel hidden">
    <h2>High Difficulty Chart</h2>
    <p class="chart-caption">Ranked horizontal bar chart of task-level difficulty scores. `difficulty_score = 0.40 * low_reward_signal + 0.20 * low_ok_signal + 0.15 * failure_mix_signal + 0.15 * frontier_struggle_signal + 0.10 * variance_signal`.</p>
    <div class="chart-frame">
      <svg id="difficulty-chart" role="img" aria-label="High difficulty ranked bar chart"></svg>
    </div>
  </div>

  <div class="table-wrap">
    <table>
      <thead><tr id="head-row"></tr></thead>
      <tbody id="body-rows"></tbody>
    </table>
  </div>
</div>

<script id="report-data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById("report-data").textContent);

const tabDefs = {{
  rerun: {{
    rows: DATA.combined_rows,
    columns: [
      "task",
      "agent",
      "model",
      "n_trials",
      "exception_summary",
      "reward_mean",
      "reward_std",
      "reasoning",
      "trajectory_json_path",
      "verifier_test_stdout_path",
      "error_category",
      "matched_patterns"
    ],
  }},
  "high-difficulty": {{
    rows: DATA.combined_rows,
    columns: [
      "agent",
      "model",
      "n_trials",
      "exception_summary",
      "reward_mean",
      "reward_std"
    ],
  }},
}};

let currentTab = "rerun";
let sortState = {{ key: "", dir: "asc" }};

function uniq(values) {{
  return [...new Set(values.filter(Boolean))].sort();
}}

function fillSummary() {{
  document.getElementById("stat-ok").textContent = DATA.summary.ok_rows;
  document.getElementById("stat-cat").textContent = DATA.summary.error_category_rows;
  document.getElementById("stat-type").textContent = DATA.summary.error_type_rows;
  document.getElementById("stat-missing").textContent = DATA.summary.missing_file_rows;

  const topCat = document.getElementById("top-categories");
  topCat.innerHTML = DATA.summary.error_categories.map(([name, count]) => `<li><span class="mono">${{name}}</span>: ${{count}}</li>`).join("");
  const topType = document.getElementById("top-types");
  topType.innerHTML = DATA.summary.error_types.map(([name, count]) => `<li><span class="mono">${{name}}</span>: ${{count}}</li>`).join("");
  const missing = document.getElementById("missing-totals");
  missing.innerHTML = Object.entries(DATA.summary.missing_totals).map(([name, count]) => `<li><span class="mono">${{name}}</span>: ${{count}}</li>`).join("");
}}

function fillFilters() {{
  const rows = Object.values(tabDefs).flatMap(def => def.rows);
  for (const [id, key] of [["task-filter","task"], ["agent-filter","agent"], ["model-filter","model"]]) {{
    const el = document.getElementById(id);
    el.innerHTML = `<option value="">All ${{key}}s</option>` + uniq(rows.map(row => row[key]))
      .map(value => `<option value="${{value}}">${{value}}</option>`).join("");
  }}
}}

function rowMatches(row) {{
  const search = document.getElementById("search").value.trim().toLowerCase();
  const task = document.getElementById("task-filter").value;
  const agent = document.getElementById("agent-filter").value;
  const model = document.getElementById("model-filter").value;
  const orangeOnly = document.getElementById("orange-only").dataset.active === "true";
  if (task && row.task !== task) return false;
  if (agent && row.agent !== agent) return false;
  if (model && row.model !== model) return false;
  if (orangeOnly && !isOrangeRow(row)) return false;
  if (!search) return true;
  return Object.values(row).join(" ").toLowerCase().includes(search);
}}

function applyTabControlVisibility() {{
  const orangeOnly = document.getElementById("orange-only");
  const difficultyBand = document.getElementById("difficulty-band");
  const chartPanel = document.getElementById("difficulty-chart-panel");
  const isHighDifficulty = currentTab === "high-difficulty";
  orangeOnly.classList.toggle("hidden", isHighDifficulty);
  difficultyBand.classList.toggle("hidden", !isHighDifficulty);
  chartPanel.classList.toggle("hidden", !isHighDifficulty);
}

function isMissingRow(row) {{
  return (
    Number(row.missing_agent_trajectory_json || 0) > 0 ||
    Number(row.missing_verifier_test_stdout_txt || 0) > 0 ||
    rowIsStdOutlier(row)
  );
}}

function isOrangeRow(row) {{
  return !(row.exception_summary === "OK:5" || Number(row.ok_runs || 0) === 5);
}}

function cellClass(key, value, row) {{
  if (["count", "ok_runs", "missing_agent_trajectory_json", "missing_verifier_test_stdout_txt"].includes(key)) {{
    const n = Number(value || 0);
    if (n >= 5) return "count-bad mono";
    if (n > 0) return "count-warn mono";
    return "count-good mono";
  }}
  if (key === "reward_std" && String(value || "") && rowIsStdOutlier(row)) return "mono std-outlier";
  if (key === "trajectory_json_path" || key === "verifier_test_stdout_path" || key === "trajectory_last_step") return "mono";
  if (key === "reward_mean" || key === "reward_std") return "mono";
  return "";
}}

function rowIsStdOutlier(row) {{
  return row && row.reward_std_large_flag === "yes";
}}

function escapeHtml(value) {{
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}}

function safeCell(value) {{
  return value == null ? "" : String(value);
}}

function prettyLastStep(raw) {{
  var text = safeCell(raw);
  if (!text) return "";
  var splitIdx = text.indexOf(": ");
  if (splitIdx === -1) return text;
  var trialId = text.slice(0, splitIdx);
  var payload = text.slice(splitIdx + 2);
  try {{
    return trialId + "\\n" + JSON.stringify(JSON.parse(payload), null, 2);
  }} catch (err) {{
    return text;
  }}
}}

function renderCell(key, value, row) {{
  var safeValue = safeCell(value);
  if (key === "trajectory_json_path" && safeValue) {{
    var lastSteps = safeCell(row.trajectory_last_step).split(" || ");
    return safeValue.split(" | ").map(function (path, idx) {{
      var tooltip = lastSteps[idx]
        ? '<div class="tooltip-panel"><pre style="margin:0; font: inherit;">' + escapeHtml(prettyLastStep(lastSteps[idx])) + '</pre></div>'
        : "";
      return '<div class="tooltip-wrap"><a class="path-link" href="file://' + encodeURI(path) + '">' + escapeHtml(path) + '</a>' + tooltip + '</div>';
    }}).join("");
  }}
  if (key === "verifier_test_stdout_path" && safeValue) {{
    var contents = safeCell(row.verifier_test_stdout_content).split(" || ");
    return safeValue.split(" | ").map(function (path, idx) {{
      var content = contents[idx] || "";
      var tooltip = content
        ? '<div class="tooltip-panel"><pre style="margin:0; font: inherit;">' + escapeHtml(content) + '</pre></div>'
        : "";
      return '<div class="tooltip-wrap"><a class="path-link" href="file://' + encodeURI(path) + '">' + escapeHtml(path) + '</a>' + tooltip + '</div>';
    }}).join("");
  }}
  return escapeHtml(safeValue);
}}

function compareValues(a, b, key) {{
  const numericKeys = ["n_trials", "ok_runs", "count", "reward_mean", "reward_std", "missing_agent_trajectory_json", "missing_verifier_test_stdout_txt"];
  if (numericKeys.includes(key)) {{
    return Number(a[key] || 0) - Number(b[key] || 0);
  }}
  return String(a[key] || "").localeCompare(String(b[key] || ""));
}}

function parseExceptionSummary(summary) {{
  const counts = new Map();
  safeCell(summary).split(" | ").forEach(function (part) {{
    if (!part) return;
    const idx = part.lastIndexOf(":");
    if (idx === -1) return;
    const name = part.slice(0, idx).trim();
    const count = Number(part.slice(idx + 1).trim());
    if (!name || !Number.isFinite(count)) return;
    counts.set(name, (counts.get(name) || 0) + count);
  }});
  return counts;
}}

function summarizeTaskRows(rows) {{
  const exceptionCounts = new Map();
  let totalTrials = 0;
  let weightedMeanSum = 0;
  let weightedSqSum = 0;
  let rewardTrials = 0;
  rows.forEach(function (row) {{
    const n = Number(row.n_trials || 0);
    totalTrials += n;
    parseExceptionSummary(row.exception_summary).forEach(function (count, name) {{
      exceptionCounts.set(name, (exceptionCounts.get(name) || 0) + count);
    }});
    const mean = Number(row.reward_mean);
    if (Number.isFinite(mean) && n > 0) {{
      weightedMeanSum += mean * n;
      rewardTrials += n;
      const std = Number(row.reward_std);
      if (Number.isFinite(std)) {{
        weightedSqSum += Math.max(0, n - 1) * std * std + n * mean * mean;
      }} else {{
        weightedSqSum += n * mean * mean;
      }}
    }}
  }});
  const rewardMean = rewardTrials ? weightedMeanSum / rewardTrials : null;
  let rewardStd = null;
  if (rewardTrials >= 2 && rewardMean !== null) {{
    const variance = Math.max(0, (weightedSqSum - rewardTrials * rewardMean * rewardMean) / (rewardTrials - 1));
    rewardStd = Math.sqrt(variance);
  }}
  const exceptionSummary = Array.from(exceptionCounts.entries())
    .sort(function (a, b) {{ return b[1] - a[1] || a[0].localeCompare(b[0]); }})
    .map(function ([name, count]) {{ return `${name}:${count}`; }})
    .join(" | ");
  return {{
    n_trials: totalTrials,
    ok_runs: rows.reduce(function (acc, row) {{ return acc + Number(row.ok_runs || 0); }}, 0),
    exception_summary: exceptionSummary,
    reward_mean: rewardMean,
    reward_std: rewardStd,
  }};
}}

function clamp01(value) {{
  return Math.max(0, Math.min(1, value));
}}

function quantile(values, q) {{
  const clean = values.filter(function (value) {{ return Number.isFinite(value); }}).slice().sort(function (a, b) {{ return a - b; }});
  if (!clean.length) return null;
  if (clean.length === 1) return clean[0];
  const pos = (clean.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  if (lo === hi) return clean[lo];
  const frac = pos - lo;
  return clean[lo] * (1 - frac) + clean[hi] * frac;
}}

function usesBoundedScores(rows) {{
  const rewards = rows
    .map(function (row) {{ return Number(row.reward_mean); }})
    .filter(function (value) {{ return Number.isFinite(value); }});
  return rewards.length > 0 && rewards.every(function (value) {{ return value >= 0 && value <= 1; }});
}}

function lowRewardSignal(rewardMean, bounded, rewardMeans) {{
  if (!Number.isFinite(rewardMean)) return 1;
  if (bounded) return clamp01((0.5 - rewardMean) / 0.5);
  const q25 = quantile(rewardMeans, 0.25);
  const q75 = quantile(rewardMeans, 0.75);
  if (!Number.isFinite(q25) || !Number.isFinite(q75) || q75 <= q25) return 0;
  return clamp01((q75 - rewardMean) / (q75 - q25));
}}

function lowOkSignal(okRate) {{
  if (!Number.isFinite(okRate)) return 1;
  return clamp01((0.6 - okRate) / 0.6);
}}

function failureMixSignal(okRate) {{
  if (!Number.isFinite(okRate)) return 1;
  const failureRate = 1 - okRate;
  return clamp01(failureRate / 0.25);
}}

function rowDifficultySignal(row, bounded) {{
  const n = Number(row.n_trials || 0);
  const okRuns = Number(row.ok_runs || 0);
  const okRate = n > 0 ? okRuns / n : 0;
  const rewardMean = Number(row.reward_mean);
  const okSignal = lowOkSignal(okRate);
  const rewardSignal = lowRewardSignal(rewardMean, bounded, []);
  return Math.max(okSignal, rewardSignal);
}}

function frontierStruggleSignal(rows, bounded) {{
  const targets = [
    ["gpt-5.4", "codex"],
    ["claude-opus-4-6", "claude-code"],
    ["gemini-3.1-pro-preview", "gemini-cli"],
  ];
  const keyed = new Map(rows.map(function (row) {{ return [`${row.model}@@${row.agent}`, row]; }}));
  let seen = 0;
  let total = 0;
  targets.forEach(function ([model, agent]) {{
    const row = keyed.get(`${model}@@${agent}`);
    if (!row) return;
    seen += 1;
    total += rowDifficultySignal(row, bounded);
  }});
  return seen ? total / seen : 0;
}}

function varianceSignal(taskStd, stdP75, stdP90) {{
  if (!Number.isFinite(taskStd) || !Number.isFinite(stdP75) || !Number.isFinite(stdP90)) return 0;
  if (taskStd <= stdP75) return 0;
  if (stdP90 <= stdP75) return 1;
  return clamp01((taskStd - stdP75) / (stdP90 - stdP75));
}}

function computeDifficultyScores(grouped) {{
  const tasks = Array.from(grouped.keys());
  const bounded = usesBoundedScores(DATA.combined_rows);
  const rawSummaries = tasks.map(function (task) {{
    const rows = grouped.get(task);
    const summary = summarizeTaskRows(rows);
    const rewardMean = Number(summary.reward_mean);
    const okRate = summary.n_trials > 0 ? summary.ok_runs / summary.n_trials : 0;
    return {{
      task: task,
      rows: rows,
      summary: summary,
      reward_mean: rewardMean,
      ok_rate: okRate,
    }};
  }});
  const rewardMeans = rawSummaries.map(function (item) {{ return item.reward_mean; }}).filter(Number.isFinite);
  const taskStds = rawSummaries.map(function (item) {{ return Number(item.summary.reward_std); }}).filter(Number.isFinite);
  const stdP75 = quantile(taskStds, 0.75);
  const stdP90 = quantile(taskStds, 0.90);
  rawSummaries.forEach(function (item) {{
    const lowReward = lowRewardSignal(item.reward_mean, bounded, rewardMeans);
    const lowOk = lowOkSignal(item.ok_rate);
    const failureMix = failureMixSignal(item.ok_rate);
    const frontier = frontierStruggleSignal(item.rows, bounded);
    const variance = varianceSignal(Number(item.summary.reward_std), stdP75, stdP90);
    // Higher scores mean "harder-looking" tasks: poor reward outcomes dominate,
    // then low completion, exception burden, frontier-pair struggle, and variance as weaker support signals.
    item.summary.difficulty_score = clamp01(
      0.40 * lowReward +
      0.20 * lowOk +
      0.15 * failureMix +
      0.15 * frontier +
      0.10 * variance
    );
  }});
  rawSummaries.sort(function (a, b) {{
    const scoreDiff = (b.summary.difficulty_score || 0) - (a.summary.difficulty_score || 0);
    if (scoreDiff !== 0) return scoreDiff;
    return (a.reward_mean || Infinity) - (b.reward_mean || Infinity);
  }});
  const difficultyScores = rawSummaries
    .map(function (item) {{ return Number(item.summary.difficulty_score); }})
    .filter(Number.isFinite);
  return {
    items: rawSummaries,
    thresholds: {
      p25: quantile(difficultyScores, 0.25),
      p50: quantile(difficultyScores, 0.50),
      p75: quantile(difficultyScores, 0.75),
    },
  };
}}

function buildHeadHtml(columns) {{
  return columns.map(function (col) {{
    const indicator = sortState.key === col ? `<span class="sort-indicator">${sortState.dir === "asc" ? "↑" : "↓"}</span>` : "";
    return `<th data-col="${col}">${col.replaceAll("_", " ")}${indicator}</th>`;
  }}).join("");
}}

function difficultyHeaderHtml() {{
  return `difficulty score`
    + `<div class="tooltip-wrap inline-help" aria-label="Difficulty score help">?`
    + `<div class="tooltip-panel"><pre style="margin:0; font: inherit; white-space: pre-wrap;">difficulty_score = 0.40 * low_reward_signal
+ 0.20 * low_ok_signal
+ 0.15 * failure_mix_signal
+ 0.15 * frontier_struggle_signal
+ 0.10 * variance_signal

failure_mix_signal is a low-gravity signal for non-OK trials, so a task with some exceptions can score low-but-nonzero instead of 0.000.</pre></div>`
    + `</div>`;
}

function buildHighDifficultyHeadHtml() {{
  const columns = [
    ["agent", "agent"],
    ["model", "model"],
    ["n_trials", "n trials"],
    ["exception_summary", "exception summary"],
    ["reward_mean", "reward mean"],
    ["reward_std", "reward std"],
    ["difficulty_score", difficultyHeaderHtml()],
  ];
  return columns.map(function ([key, label]) {{
    const active = sortState.key === key;
    const indicator = active ? `<span class="sort-indicator">${sortState.dir === "asc" ? "↑" : "↓"}</span>` : "";
    return `<th data-col="${key}">${label}${indicator}</th>`;
  }}).join("");
}

function compareTaskSummaries(a, b, key) {{
  const aSummary = a.summary || {};
  const bSummary = b.summary || {};
  if (key === "difficulty_score") return Number(aSummary.difficulty_score || 0) - Number(bSummary.difficulty_score || 0);
  if (key === "n_trials") return Number(aSummary.n_trials || 0) - Number(bSummary.n_trials || 0);
  if (key === "reward_mean") return Number(aSummary.reward_mean || 0) - Number(bSummary.reward_mean || 0);
  if (key === "reward_std") return Number(aSummary.reward_std || 0) - Number(bSummary.reward_std || 0);
  if (key === "exception_summary") return String(aSummary.exception_summary || "").localeCompare(String(bSummary.exception_summary || ""));
  if (key === "agent") {{
    const aAgent = a.rows.map(function (row) {{ return String(row.agent || ""); }}).sort()[0] || "";
    const bAgent = b.rows.map(function (row) {{ return String(row.agent || ""); }}).sort()[0] || "";
    return aAgent.localeCompare(bAgent);
  }}
  if (key === "model") {{
    const aModel = a.rows.map(function (row) {{ return String(row.model || ""); }}).sort()[0] || "";
    const bModel = b.rows.map(function (row) {{ return String(row.model || ""); }}).sort()[0] || "";
    return aModel.localeCompare(bModel);
  }}
  return String(a.task || "").localeCompare(String(b.task || ""));
}

function escapeAttr(value) {{
  return escapeHtml(value).replaceAll("'", "&#39;");
}

function difficultyBandColor(score, thresholds) {{
  if (!Number.isFinite(score)) return "#98a2b3";
  if (Number.isFinite(thresholds.p75) && score >= thresholds.p75) return "#175cd3";
  if (Number.isFinite(thresholds.p50) && score >= thresholds.p50) return "#36b37e";
  if (Number.isFinite(thresholds.p25) && score < thresholds.p25) return "#98a2b3";
  return "#f79009";
}

function renderDifficultyChart(items, thresholds) {{
  const svg = document.getElementById("difficulty-chart");
  if (!items.length) {{
    svg.setAttribute("width", "960");
    svg.setAttribute("height", "80");
    svg.innerHTML = `<text x="24" y="40" fill="#667085" font-size="14">No tasks match the current difficulty filter.</text>`;
    return;
  }}
  const leftPad = 260;
  const rightPad = 40;
  const topPad = 24;
  const bottomPad = 30;
  const rowHeight = 28;
  const barHeight = 18;
  const chartWidth = 960;
  const barAreaWidth = chartWidth - leftPad - rightPad;
  const height = topPad + bottomPad + items.length * rowHeight;
  svg.setAttribute("width", String(chartWidth));
  svg.setAttribute("height", String(height));
  const gridLines = [0, 0.25, 0.5, 0.75, 1].map(function (tick) {{
    const x = leftPad + tick * barAreaWidth;
    return `<g><line x1="${x}" y1="${topPad - 8}" x2="${x}" y2="${height - bottomPad + 4}" stroke="#d0d5dd" stroke-dasharray="3 3"></line><text x="${x}" y="${height - 8}" text-anchor="middle" fill="#667085" font-size="11">${tick.toFixed(2)}</text></g>`;
  }}).join("");
  const bars = items.map(function (item, index) {{
    const score = Number(item.summary.difficulty_score || 0);
    const y = topPad + index * rowHeight;
    const width = Math.max(0, score) * barAreaWidth;
    const color = difficultyBandColor(score, thresholds);
    return `<g>`
      + `<text x="${leftPad - 12}" y="${y + 13}" text-anchor="end" fill="#17202a" font-size="12">${escapeHtml(item.task)}</text>`
      + `<rect x="${leftPad}" y="${y}" width="${width}" height="${barHeight}" rx="4" fill="${color}"><title>${escapeAttr(item.task)}: ${score.toFixed(3)}</title></rect>`
      + `<text x="${leftPad + width + 8}" y="${y + 13}" fill="#17202a" font-size="12">${score.toFixed(3)}</text>`
      + `</g>`;
  }}).join("");
  svg.innerHTML = gridLines + bars;
}

function bindHeadClicks() {{
  document.querySelectorAll("#head-row th[data-col]").forEach(function (th) {{
    th.addEventListener("click", function () {{
      const key = th.dataset.col;
      if (sortState.key === key) {{
        sortState.dir = sortState.dir === "asc" ? "desc" : "asc";
      }} else {{
        sortState.key = key;
        sortState.dir = "asc";
      }}
      renderTable();
    }});
  }});
}}

function renderTable() {{
  const def = tabDefs[currentTab];
  const head = document.getElementById("head-row");
  const body = document.getElementById("body-rows");
  const wrap = document.querySelector(".table-wrap");
  wrap.dataset.tab = currentTab;
  applyTabControlVisibility();
  if (currentTab === "high-difficulty") {{
    if (!sortState.key) {{
      sortState.key = "difficulty_score";
      sortState.dir = "desc";
    }}
    head.innerHTML = buildHighDifficultyHeadHtml();
    bindHeadClicks();
    const grouped = new Map();
    def.rows.filter(rowMatches).forEach(function (row) {{
      if (!grouped.has(row.task)) grouped.set(row.task, []);
      grouped.get(row.task).push(row);
    }});
    const scored = computeDifficultyScores(grouped);
    const thresholds = scored.thresholds || {};
    const difficultyBand = document.getElementById("difficulty-band").value;
    let taskSummaries = scored.items;
    if (difficultyBand === "high" && Number.isFinite(thresholds.p75)) {{
      taskSummaries = taskSummaries.filter(function (item) {{ return Number(item.summary.difficulty_score) >= thresholds.p75; }});
    }} else if (difficultyBand === "above-median" && Number.isFinite(thresholds.p50)) {{
      taskSummaries = taskSummaries.filter(function (item) {{ return Number(item.summary.difficulty_score) >= thresholds.p50; }});
    }} else if (difficultyBand === "below-median" && Number.isFinite(thresholds.p50)) {{
      taskSummaries = taskSummaries.filter(function (item) {{ return Number(item.summary.difficulty_score) < thresholds.p50; }});
    }} else if (difficultyBand === "low" && Number.isFinite(thresholds.p25)) {{
      taskSummaries = taskSummaries.filter(function (item) {{ return Number(item.summary.difficulty_score) < thresholds.p25; }});
    }}
    taskSummaries.sort(function (a, b) {{
      const cmp = compareTaskSummaries(a, b, sortState.key || "difficulty_score");
      return sortState.dir === "asc" ? cmp : -cmp;
    }});
    renderDifficultyChart(taskSummaries, thresholds);
    body.innerHTML = taskSummaries.map(function (item) {{
      const task = item.task;
      const rows = item.rows.slice().sort(function (a, b) {{
        const agentCmp = String(a.agent || "").localeCompare(String(b.agent || ""));
        if (agentCmp !== 0) return agentCmp;
        return String(a.model || "").localeCompare(String(b.model || ""));
      }});
      const summary = item.summary;
      const groupHeader = `<tr class="task-group"><td colspan="7">${escapeHtml(task)}</td></tr>`;
      const children = rows.map(function (row) {{
        const highlightMissing = isMissingRow(row);
        const highlightAllOk = isOrangeRow(row);
        const rowClass = highlightMissing ? "task-child row-missing" : highlightAllOk ? "task-child row-all-ok" : "task-child";
        return `<tr class="${rowClass}">`
          + `<td class="${cellClass("agent", row.agent, row)}">${renderCell("agent", row.agent, row)}</td>`
          + `<td class="${cellClass("model", row.model, row)}">${renderCell("model", row.model, row)}</td>`
          + `<td class="${cellClass("n_trials", row.n_trials, row)}">${renderCell("n_trials", row.n_trials, row)}</td>`
          + `<td class="${cellClass("exception_summary", row.exception_summary, row)}">${renderCell("exception_summary", row.exception_summary, row)}</td>`
          + `<td class="${cellClass("reward_mean", row.reward_mean, row)}">${renderCell("reward_mean", row.reward_mean, row)}</td>`
          + `<td class="${cellClass("reward_std", row.reward_std, row)}">${renderCell("reward_std", row.reward_std, row)}</td>`
          + `<td></td>`
          + `</tr>`;
      }}).join("");
      const summaryRow = `<tr class="task-summary">`
        + `<td>Task total</td>`
        + `<td></td>`
        + `<td class="mono">${summary.n_trials || ""}</td>`
        + `<td>${escapeHtml(summary.exception_summary || "")}</td>`
        + `<td class="mono">${summary.reward_mean == null ? "" : summary.reward_mean.toFixed(6)}</td>`
        + `<td class="mono">${summary.reward_std == null ? "" : summary.reward_std.toFixed(6)}</td>`
        + `<td class="mono task-score">${summary.difficulty_score == null ? "" : summary.difficulty_score.toFixed(3)}</td>`
        + `</tr>`;
      return groupHeader + children + summaryRow;
    }}).join("");
    return;
  }}
  head.innerHTML = buildHeadHtml(def.columns);
  bindHeadClicks();
  const rows = def.rows.filter(rowMatches).slice().sort(function (a, b) {{
    if (!sortState.key) return 0;
    const cmp = compareValues(a, b, sortState.key);
    return sortState.dir === "asc" ? cmp : -cmp;
  }});
  body.innerHTML = rows.map(row => {{
    const highlightMissing = isMissingRow(row);
    const highlightAllOk = isOrangeRow(row);
    const rowClass = highlightMissing ? ' class="row-missing"' : highlightAllOk ? ' class="row-all-ok"' : "";
    return `<tr${rowClass}>${def.columns.map(col => `<td class="${cellClass(col, row[col], row)}">${renderCell(col, row[col], row)}</td>`).join("")}</tr>`;
  }}).join("");
}}

for (const tab of document.querySelectorAll(".tab")) {{
  tab.addEventListener("click", () => {{
    currentTab = tab.dataset.tab;
    sortState = currentTab === "high-difficulty"
      ? { key: "difficulty_score", dir: "desc" }
      : { key: "", dir: "asc" };
    document.querySelectorAll(".tab").forEach(el => el.classList.toggle("active", el === tab));
    renderTable();
  }});
}}

for (const id of ["search", "task-filter", "agent-filter", "model-filter", "orange-only", "difficulty-band"]) {{
  document.getElementById(id).addEventListener("input", renderTable);
  document.getElementById(id).addEventListener("change", renderTable);
}}

document.getElementById("orange-only").addEventListener("click", function () {{
  const next = this.dataset.active === "true" ? "false" : "true";
  this.dataset.active = next;
  this.setAttribute("aria-pressed", next);
  this.classList.toggle("active", next === "true");
  renderTable();
}});

fillSummary();
fillFilters();
renderTable();
</script>
</body>
</html>"""
    template = template.replace("{{", "{").replace("}}", "}")
    return (
        template.replace("__BENCHMARK__", benchmark)
        .replace("__GENERATED__", generated_at)
        .replace("__DATA__", payload)
    )


def main() -> None:
    args = parse_args()
    ok_rows = read_tsv(args.tables_dir / "ok_runs.tsv")
    error_category_rows = read_tsv(args.tables_dir / "error_categories.tsv")
    error_type_rows = read_tsv(args.tables_dir / "error_types.tsv")
    missing_rows = read_tsv(args.tables_dir / "missing_extracted_files.tsv")
    reasoning_rows = read_optional_tsv(args.tables_dir / "reasoning.tsv")
    combined_rows = build_combined_rows(ok_rows, error_category_rows, missing_rows, reasoning_rows)

    data = {
        "ok_rows": ok_rows,
        "error_category_rows": error_category_rows,
        "error_type_rows": error_type_rows,
        "missing_rows": missing_rows,
        "reasoning_rows": reasoning_rows,
        "combined_rows": combined_rows,
        "summary": build_summary(ok_rows, error_category_rows, error_type_rows, missing_rows),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    html = render_html(args.benchmark, data)
    args.output.write_text(html, encoding="utf-8")

    date_str = datetime.now().strftime("%Y-%m-%d")
    stable_output = canonical_output_path(args.benchmark, args.output, date_str)
    if stable_output != args.output:
        stable_output.write_text(html, encoding="utf-8")

    print(args.output)
    if stable_output != args.output:
        print(stable_output)


if __name__ == "__main__":
    main()
