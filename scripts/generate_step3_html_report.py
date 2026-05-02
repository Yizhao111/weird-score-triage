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


def read_optional_json(path: Path):
    if not path.exists():
        return {}
    with path.open() as f:
        return json.load(f)


def read_leaderboard_scores(benchmark: str) -> list[dict]:
    path = Path("/tmp/leaderboard_aggregate.json")
    if not path.exists():
        return []
    with path.open() as f:
        rows = json.load(f)
    return [r for r in rows if r.get("benchmark") == benchmark]


def read_experiment_owner(benchmark: str) -> str:
    csv_path = Path(__file__).parent.parent / "experiment-track.csv"
    if not csv_path.exists():
        return ""
    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            if row.get("Adapter Name", "").strip().lower() == benchmark.strip().lower():
                return row.get("People", "").strip()
    return ""


def read_inversion_analysis(benchmark: str) -> list[dict]:
    path = Path(f"/tmp/{benchmark}_inversion_analysis.json")
    if not path.exists():
        return []
    with path.open() as f:
        return json.load(f)


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


_STDOUT_PREVIEW_CHARS = 3000


def _read_reward_files(stdout_paths_str: str) -> str:
    """Read verifier/reward.txt for each trial, using the stdout path to locate the run dir."""
    if not stdout_paths_str:
        return ""
    rewards = []
    for path in stdout_paths_str.split(" | "):
        path = path.strip()
        if not path or path == "—":
            rewards.append("—")
            continue
        reward_path = Path(path).parent / "reward.txt"
        try:
            rewards.append(reward_path.read_text().strip())
        except OSError:
            rewards.append("—")
    return " | ".join(rewards)


def _read_stdout_previews(paths_str: str) -> str:
    """Read up to _STDOUT_PREVIEW_CHARS chars from each test-stdout.txt path, joined by ' || '."""
    if not paths_str:
        return ""
    previews = []
    for path in paths_str.split(" | "):
        path = path.strip()
        if not path or path == "—":
            previews.append("")
            continue
        try:
            text = Path(path).read_text(errors="replace")
            if len(text) > _STDOUT_PREVIEW_CHARS:
                text = text[:_STDOUT_PREVIEW_CHARS] + "\n...[truncated]"
            previews.append(text)
        except OSError:
            previews.append("[file not found]")
    return " || ".join(previews)


def build_combined_rows(ok_rows, error_category_rows, missing_rows, reasoning_rows, rerun_rows):
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
    rerun_index = {
        (row["task"], row["agent"], row["model"]): row
        for row in rerun_rows
    }
    category_index = {}
    for row in error_category_rows:
        key = (row["task"], row["agent"], row["model"])
        category_index.setdefault(key, []).append(row)

    combined_rows = []
    all_keys = sorted(set(ok_index) | set(category_index) | set(missing_index) | set(reasoning_index) | set(rerun_index))
    for key in all_keys:
        ok_row = ok_index.get(key, {})
        missing_row = missing_index.get(key, {})
        reasoning_row = reasoning_index.get(key, {})
        rerun_row = rerun_index.get(key, {})
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
            "verifier_test_stdout_content": _read_stdout_previews(missing_row.get("verifier_test_stdout_path", "")),
            "verifier_reward": _read_reward_files(missing_row.get("verifier_test_stdout_path", "")),
            "trajectory_last_step": missing_row.get("trajectory_last_step", ""),
            "reasoning": reasoning_row.get("reasoning", ""),
            "rerun_recommendation": reasoning_row.get("rerun_recommendation") or rerun_row.get("rerun_recommendation", ""),
            "rerun_reason": reasoning_row.get("rerun_justification") or rerun_row.get("rerun_reason", ""),
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
    payload = json.dumps(data).replace("</", "<\\/")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    owner = read_experiment_owner(benchmark)
    owner_line = (
        f'<div class="meta" style="margin-top:4px">Experiment Owner: {owner}</div>'
        if owner else ""
    )
    template = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__BENCHMARK__ Audit Report</title>
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
.rerun-panel.hidden {{
  display: none;
}}
.rerun-summary {{
  margin: 0 0 16px;
}}
.rerun-metrics {{
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-top: 12px;
}}
.rerun-metric {{
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fcfcfd;
}}
.rerun-metric b {{
  display: block;
  font-size: 24px;
}}
.rerun-metric span {{
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
}}
.rerun-panel.hidden {{
  display: none;
}}
.rerun-summary {{
  margin: 0 0 16px;
}}
.rerun-metrics {{
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-top: 12px;
}}
.rerun-metric {{
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fcfcfd;
}}
.rerun-metric b {{
  display: block;
  font-size: 24px;
}}
.rerun-metric span {{
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
}}
.insight-sections {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.insight-section {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--paper);
  overflow: hidden;
}
.insight-section h3 {
  margin: 0;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  background: #f8fafc;
  font-size: 15px;
}
.insight-empty {
  padding: 14px;
  color: var(--muted);
}
.insight-body {
  padding: 14px;
}
.insight-metric {
  display: block;
  margin-bottom: 10px;
  color: var(--blue);
  font-size: 28px;
  font-weight: 700;
}
.insight-list {
  margin: 0;
  padding-left: 18px;
}
.insight-list li {
  margin-bottom: 8px;
}
.inversion-detail {{
  margin-top: 6px;
  padding: 8px 10px;
  border-left: 3px solid var(--line);
  font-size: 12px;
  color: var(--muted);
}}
.inversion-detail summary {{
  cursor: pointer;
  font-weight: 500;
  color: var(--blue);
  font-size: 12px;
  user-select: none;
}}
.inversion-detail p {{
  margin: 6px 0 8px;
  color: var(--ink);
  line-height: 1.5;
}}
.inversion-detail table {{
  border-collapse: collapse;
  width: 100%;
  margin-top: 4px;
}}
.inversion-detail td {{
  padding: 3px 6px;
  border: 1px solid var(--line);
  vertical-align: top;
  line-height: 1.4;
}}
.inversion-detail td:first-child {{
  white-space: nowrap;
  font-family: ui-monospace, monospace;
  font-size: 11px;
  color: var(--muted);
  width: 180px;
}}
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
.table-wrap[data-tab="rerun"] table {{
  min-width: 2200px;
}}
.col-stdout-wide {{
  min-width: 220px;
  width: 220px;
}}
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
.instructions-btn {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--paper);
  color: var(--muted);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  margin-bottom: 10px;
}}
.instructions-btn:hover {{
  border-color: var(--blue);
  color: var(--blue);
}}
.modal-overlay {{
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(16,24,40,0.45);
  z-index: 100;
  align-items: flex-start;
  justify-content: center;
  padding: 40px 16px;
  overflow-y: auto;
}}
.modal-overlay.open {{ display: flex; }}
.modal {{
  background: var(--paper);
  border-radius: 12px;
  padding: 28px 32px 32px;
  max-width: 720px;
  width: 100%;
  position: relative;
  box-shadow: 0 20px 60px rgba(16,24,40,0.22);
  line-height: 1.6;
}}
.modal h2 {{ margin: 0 0 4px; font-size: 18px; }}
.modal h3 {{ margin: 20px 0 6px; font-size: 14px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }}
.modal h4 {{ margin: 14px 0 4px; font-size: 14px; }}
.modal p {{ margin: 0 0 10px; }}
.modal ul, .modal ol {{ margin: 0 0 10px; padding-left: 22px; }}
.modal li {{ margin-bottom: 4px; }}
.modal hr {{ border: none; border-top: 1px solid var(--line); margin: 18px 0; }}
.modal .close-btn {{
  position: absolute;
  top: 16px; right: 20px;
  background: none; border: none;
  font-size: 22px; color: var(--muted);
  cursor: pointer; line-height: 1;
}}
.modal .close-btn:hover {{ color: var(--ink); }}
.modal code {{
  background: #f1f3f5;
  border-radius: 4px;
  padding: 1px 5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}}
</style>
</head>
<body>
<header>
  <h1>__BENCHMARK__ Audit Report</h1>
  <div class="meta">Generated __GENERATED__. Interactive report over the extracted-trial audit tables: merged run summary and missing extracted files.</div>
  __OWNER_LINE__
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
    <button class="tab" data-tab="accuracy-insight">Accuracy & Insight</button>
  </div>

  <button id="rerun-instructions-btn" class="instructions-btn" style="display:none" type="button">📋 Instructions</button>
  <button id="insight-instructions-btn" class="instructions-btn" style="display:none" type="button">📋 Instructions</button>

  <div class="controls">
    <input id="search" type="search" placeholder="Filter by task, agent, model, or pattern">
    <select id="task-filter"><option value="">All tasks</option></select>
    <select id="agent-filter"><option value="">All agents</option></select>
    <select id="model-filter"><option value="">All models</option></select>
    <select id="error-type-filter"><option value="">All error types</option></select>
    <button id="orange-only" class="toggle-filter" type="button" aria-pressed="false">Only orange rows</button>
  </div>

  <div id="rerun-summary-panel" class="panel rerun-panel hidden">
    <h2>Re-run Summary</h2>
    <p id="rerun-summary-text" class="chart-caption"></p>
    <div class="rerun-metrics">
      <div class="rerun-metric"><b id="rerun-reviewed"></b><span>Orange Cells Reviewed</span></div>
      <div class="rerun-metric"><b id="rerun-yes"></b><span>Re-run Yes</span></div>
      <div class="rerun-metric"><b id="rerun-maybe"></b><span>Re-run Maybe</span></div>
      <div class="rerun-metric"><b id="rerun-no"></b><span>Re-run No</span></div>
    </div>
    <ul id="rerun-bullets" style="margin: 14px 0 0; padding-left: 20px; line-height: 1.7;"></ul>
  </div>

  <div id="accuracy-insight-panel" class="insight-sections hidden"></div>

  <div class="table-wrap">
    <table>
      <thead><tr id="head-row"></tr></thead>
      <tbody id="body-rows"></tbody>
    </table>
  </div>
</div>

<div id="rerun-instructions-modal" class="modal-overlay" role="dialog" aria-modal="true">
  <div class="modal">
    <button class="close-btn" id="modal-close" aria-label="Close">×</button>
    <h2>Re-run Analysis — Triage Guide</h2>
    <p style="color:var(--muted);font-size:13px;margin:0 0 16px">How to read orange rows and decide whether to rerun a cell.</p>
    <hr>
    <h3>Why a row is highlighted <span style="color:#b54708">orange</span></h3>
    <p>A row is highlighted <span style="color:#b54708">orange</span> if it matches one or more of the following conditions:</p>
    <ol>
      <li><strong>OK count &lt; 3</strong> — fewer than 3 successful OK trials, below the <code>p_window=3</code> scoring requirement.</li>
      <li><strong>Missing artifacts</strong> — one or more trials are missing <code>agent/trajectory.json</code> or <code>verifier/test-stdout.txt</code>.</li>
      <li><strong>High variance</strong> — <code>reward_std</code> is above the benchmark's 75th-percentile std, indicating unstable results across trials.</li>
    </ol>
    <hr>
    <h3>Validity rule</h3>
    <p>A cell is acceptable if it has <strong>at least 3 confirmed valid OK trials</strong>.</p>
    <p>If some trials are missing <code>trajectory.json</code> or <code>test-stdout.txt</code>, do not immediately mark the cell for rerun. Instead, check the other available artifacts:</p>
    <ul>
      <li><code>result.json</code></li>
      <li><code>exception.txt</code></li>
      <li><code>verifier/reward.txt</code></li>
      <li>other verifier or agent logs</li>
    </ul>
    <p>If you can confirm ≥3 trials are valid OK runs, no rerun is needed — even if other trials have missing files.</p>
    <hr>
    <h3>How to handle AgentTimeoutError</h3>
    <p><code>AgentTimeoutError</code> is common on hard tasks and should not automatically trigger a rerun.</p>
    <h4>Usually valid timeout</h4>
    <p>Treat the timeout as valid model behaviour if:</p>
    <ul>
      <li>the task has a normal timeout window (e.g. 10 minutes);</li>
      <li><code>agent/trajectory.json</code> shows multiple steps with meaningful progress;</li>
      <li>the last trajectory step looks like the agent was still working when the time limit was reached.</li>
    </ul>
    <p>In this case the timeout reflects the agent failing to finish within the allowed time — this is a capability signal, not an infra failure, and usually does not need a rerun.</p>
    <h4>Suspicious timeout — consider rerun</h4>
    <ul>
      <li><code>agent/trajectory.json</code> is missing or has zero/very few steps;</li>
      <li>the agent appears to have been blocked before real execution began;</li>
      <li>the timeout was caused by an external issue such as rate limiting, API interruption, or sandbox failure.</li>
    </ul>
    <hr>
    <h3>When to rerun</h3>
    <p>Flag a cell for rerun when <strong>both</strong> conditions are true:</p>
    <ol>
      <li>The cell has fewer than 3 valid OK trials.</li>
      <li>The non-OK trials appear to be caused by transient infrastructure or execution issues, not stable model behaviour.</li>
    </ol>
    <h4>1. Too few OK trials + infra failures</h4>
    <p>Rerun if the cell has &lt;3 valid OKs and remaining trials failed due to:</p>
    <ul>
      <li><code>CancelledError</code>, <code>DaytonaError</code>, <code>DaytonaNotFoundError</code>, <code>DownloadVerifierDirError</code></li>
      <li>Real API rate-limit errors: <code>credit balance is too low</code>, <code>quota exceeded</code>, <code>RESOURCE_EXHAUSTED</code></li>
      <li>Sandbox setup or artifact download failures</li>
    </ul>
    <p>These are platform-side issues and should not count as the model's real performance.</p>
    <h4>2. Agent interrupted before completing</h4>
    <p>Rerun if the agent was interrupted mid-run by an external issue — such as API rate limiting, API timeout, sandbox interruption, or verifier download failure — and the cell still has fewer than 3 valid OK trials.</p>
    <hr>
    <p>Once you've triaged the orange rows, fill in your findings here:</p>
    <a href="https://docs.google.com/document/d/19v5UlRBecPm_hNDsj1oE4X-SPiI9mUpTqLK-t6lqdbY/edit?tab=t.0#heading=h.56j7yxv0uyk6"
       target="_blank" rel="noopener noreferrer"
       style="display:inline-flex;align-items:center;gap:6px;margin-top:4px;padding:8px 14px;background:var(--blue);color:#fff;border-radius:6px;text-decoration:none;font-weight:500;font-size:13px;">
      Fill in re-run findings ↗
    </a>
  </div>
</div>

<div id="insight-instructions-modal" class="modal-overlay" role="dialog" aria-modal="true">
  <div class="modal">
    <button class="close-btn" id="insight-modal-close" aria-label="Close">×</button>
    <h2>Accuracy &amp; Insight — Guide</h2>
    <p style="color:var(--muted);font-size:13px;margin:0 0 16px">How to interpret the score chart and insight subsections.</p>
    <hr>
    <p>Once you validate the abnormal behavior, fill in:</p>
    <a href="https://docs.google.com/document/d/19v5UlRBecPm_hNDsj1oE4X-SPiI9mUpTqLK-t6lqdbY/edit?tab=t.0#heading=h.mdovut8re9iz"
       target="_blank" rel="noopener noreferrer"
       style="display:inline-flex;align-items:center;gap:6px;margin-top:4px;padding:8px 14px;background:var(--blue);color:#fff;border-radius:6px;text-decoration:none;font-weight:500;font-size:13px;">
      Open instructions doc ↗
    </a>
  </div>
</div>

<script id="report-data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById("report-data").textContent);
const MODEL_TIERS = {
  "gpt-5.4": 3,
  "gpt-5-mini": 2,
  "gpt-5-nano": 1,
  "claude-opus-4-6": 3,
  "claude-sonnet-4-6": 2,
  "claude-haiku-4-5-20251001": 1,
  "gemini-3.1-pro-preview": 2,
  "gemini-3-flash-preview": 1,
};
const AGENT_TIERS = {
  "codex": 3,
  "claude-code": 3,
  "gemini-cli": 2,
  "terminus-2": 1,
};
const FRONTIER_MODELS = {
  "openai": "gpt-5.4",
  "anthropic": "claude-opus-4-6",
  "google": "gemini-3.1-pro-preview",
};
const NATIVE_AGENT = { "gpt": "codex", "claude": "claude-code", "gemini": "gemini-cli" };

function modelFamily(model) {
  const value = String(model || "");
  if (value.startsWith("gpt")) return "openai";
  if (value.startsWith("claude")) return "anthropic";
  if (value.startsWith("gemini")) return "google";
  return "";
}

function strongerModel(a, b) {
  return modelFamily(a) && modelFamily(a) === modelFamily(b) && Number(MODEL_TIERS[a] || 0) > Number(MODEL_TIERS[b] || 0);
}

function strongerAgent(a, b) {
  return Number(AGENT_TIERS[a] || 0) > Number(AGENT_TIERS[b] || 0);
}

function nativeAgent(model) {
  const value = String(model || "");
  if (value.startsWith("gpt")) return NATIVE_AGENT.gpt;
  if (value.startsWith("claude")) return NATIVE_AGENT.claude;
  if (value.startsWith("gemini")) return NATIVE_AGENT.gemini;
  return "";
}

function buildAccuracyInsightSummary(rows) {
  const grouped = new Map();
  rows.forEach(function (row) {
    const score = Number(row.reward_mean);
    if (!Number.isFinite(score)) return;
    const key = `${row.model}@@${row.agent}`;
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(score);
  });
  const benchData = new Map();
  grouped.forEach(function (scores, key) {
    const mean = scores.reduce(function (acc, value) { return acc + value; }, 0) / scores.length;
    benchData.set(key, mean);
  });
  const models = Array.from(new Set(rows.map(function (row) { return row.model; }))).sort();
  const agents = Array.from(new Set(rows.map(function (row) { return row.agent; }))).sort();
  const sections = {
    "Model Inversions": [],
    "Agent Inversions": [],
    "Native Agent Underperformance": [],
    "Cross-Family Surprises": [],
  };

  agents.forEach(function (agent) {
    models.forEach(function (stronger) {
      models.forEach(function (weaker) {
        if (!strongerModel(stronger, weaker)) return;
        const strongScore = benchData.get(`${stronger}@@${agent}`);
        const weakScore = benchData.get(`${weaker}@@${agent}`);
        if (!Number.isFinite(strongScore) || !Number.isFinite(weakScore) || strongScore >= weakScore - 0.05) return;
        sections["Model Inversions"].push(`${stronger}/${agent}=${strongScore.toFixed(3)} is below ${weaker}/${agent}=${weakScore.toFixed(3)}.`);
      });
    });
  });

  models.forEach(function (model) {
    agents.forEach(function (stronger) {
      agents.forEach(function (weaker) {
        if (!strongerAgent(stronger, weaker)) return;
        const strongScore = benchData.get(`${model}@@${stronger}`);
        const weakScore = benchData.get(`${model}@@${weaker}`);
        if (!Number.isFinite(strongScore) || !Number.isFinite(weakScore) || strongScore >= weakScore - 0.05) return;
        sections["Agent Inversions"].push(`${model}/${stronger}=${strongScore.toFixed(3)} is below ${model}/${weaker}=${weakScore.toFixed(3)}.`);
      });
    });
  });

  models.forEach(function (model) {
    const native = nativeAgent(model);
    if (!native) return;
    const nativeScore = benchData.get(`${model}@@${native}`);
    if (!Number.isFinite(nativeScore)) return;
    agents.forEach(function (otherAgent) {
      if (otherAgent === native) return;
      const otherScore = benchData.get(`${model}@@${otherAgent}`);
      if (!Number.isFinite(otherScore) || otherScore <= nativeScore + 0.10) return;
      sections["Native Agent Underperformance"].push(`${model}/${native}=${nativeScore.toFixed(3)} is below ${model}/${otherAgent}=${otherScore.toFixed(3)}.`);
    });
  });

  const frontierBest = {};
  Object.entries(FRONTIER_MODELS).forEach(function ([family, model]) {
    let best = -Infinity;
    agents.forEach(function (agent) {
      const score = benchData.get(`${model}@@${agent}`);
      if (Number.isFinite(score)) best = Math.max(best, score);
    });
    if (best > -Infinity) frontierBest[family] = { model: model, score: best };
  });
  models.forEach(function (model) {
    const family = modelFamily(model);
    if (!family || Number(MODEL_TIERS[model] || 99) > 1) return;
    agents.forEach(function (agent) {
      const score = benchData.get(`${model}@@${agent}`);
      if (!Number.isFinite(score)) return;
      Object.entries(frontierBest).forEach(function ([frontierFamily, info]) {
        if (frontierFamily === family || score <= info.score + 0.15) return;
        sections["Cross-Family Surprises"].push(`${model}/${agent}=${score.toFixed(3)} exceeds ${info.model} best=${info.score.toFixed(3)}.`);
      });
    });
  });

  return sections;
}
const ACCURACY_INSIGHT_SUMMARY = buildAccuracyInsightSummary(DATA.combined_rows);

function buildLeaderboardInsights(lbScores) {
  if (!lbScores || !lbScores.length) return {"Model Laggards": [], "Harness Laggards": []};
  const byModel = {};
  const byAgent = {};
  lbScores.forEach(function(r) {
    if (!byModel[r.model]) byModel[r.model] = {};
    byModel[r.model][r.agent] = r.score;
    if (!byAgent[r.agent]) byAgent[r.agent] = {};
    byAgent[r.agent][r.model] = r.score;
  });
  const models = Object.keys(byModel);
  const agents = Object.keys(byAgent);
  function agentMean(agentScores) {
    const vals = Object.values(agentScores).filter(Number.isFinite);
    return vals.length ? vals.reduce(function(a,b){return a+b;},0)/vals.length : null;
  }
  const modelMeans = {};
  models.forEach(function(m) { modelMeans[m] = agentMean(byModel[m]); });

  // Model Laggards — stronger family member scores below weaker peer (>3pp gap), or negative mean
  const modelLaggards = [];
  const seen = new Set();
  models.forEach(function(stronger) {
    models.forEach(function(weaker) {
      if (!strongerModel(stronger, weaker)) return;
      const sm = modelMeans[stronger], wm = modelMeans[weaker];
      if (!Number.isFinite(sm) || !Number.isFinite(wm) || sm >= wm - 0.03) return;
      const key = stronger + ">" + weaker;
      if (seen.has(key)) return;
      seen.add(key);
      const gap = ((wm - sm) * 100).toFixed(1);
      modelLaggards.push(stronger + " avg " + (sm*100).toFixed(1) + " is " + gap + "pp below " + weaker + " avg " + (wm*100).toFixed(1) + " — expected ordering inverted.");
    });
  });
  models.forEach(function(m) {
    const mean = modelMeans[m];
    if (Number.isFinite(mean) && mean < 0) {
      modelLaggards.push(m + " avg " + (mean*100).toFixed(1) + " — negative mean score across all agents.");
    }
  });
  modelLaggards.sort(function(a,b) {
    const na = parseFloat((a.match(/(-?\d+\.\d+)pp/) || ["","0"])[1]);
    const nb = parseFloat((b.match(/(-?\d+\.\d+)pp/) || ["","0"])[1]);
    return nb - na;
  });

  // Harness Laggards — for each model, agents >15pp below that model's best-agent score
  const harnessLaggards = [];
  models.forEach(function(model) {
    const agentScores = byModel[model];
    const vals = Object.entries(agentScores).filter(function(e){ return Number.isFinite(e[1]); });
    if (!vals.length) return;
    vals.sort(function(a,b){return b[1]-a[1];});
    const bestAgent = vals[0][0], bestScore = vals[0][1];
    vals.forEach(function(entry) {
      const agent = entry[0], score = entry[1];
      if (agent === bestAgent) return;
      const gap = (bestScore - score) * 100;
      if (gap < 15) return;
      harnessLaggards.push(model + "/" + agent + " (" + (score*100).toFixed(1) + ") is " + gap.toFixed(1) + "pp below " + model + "/" + bestAgent + " (" + (bestScore*100).toFixed(1) + ").");
    });
  });
  harnessLaggards.sort(function(a,b) {
    const ga = parseFloat((a.match(/(\d+\.\d+)pp/) || ["","0"])[1]);
    const gb = parseFloat((b.match(/(\d+\.\d+)pp/) || ["","0"])[1]);
    return gb - ga;
  });

  return {"Model Laggards": modelLaggards, "Harness Laggards": harnessLaggards};
}
const LEADERBOARD_INSIGHTS = buildLeaderboardInsights(DATA.leaderboard_scores);

// Each entry in inversion_analysis may have:
//   match_type: "prefix" (bullet starts with match_key) or "contains" (bullet includes match_key)
//   match_key:  the key to test against the bullet text
// Older model-inversion entries without these fields default to prefix matching on stronger_model/agent.
function normalizeInsightSectionName(name) {
  const raw = String(name || "").trim();
  if (!raw) return "";
  return raw
    .replace(/\s*\(across this benchmark\)\s*$/i, "")
    .replace(/\s*\(across leaderboard\)\s*$/i, "")
    .trim();
}

const INVERSION_ENTRIES = (DATA.inversion_analysis || []).map(function(entry) {
  const matchType = entry.match_type || "prefix";
  const matchKey  = entry.match_key  || (entry.stronger_model + "/" + entry.agent + "=");
  return {
    entry: entry,
    matchType: matchType,
    matchKey: matchKey,
    sections: entry.section
      ? [normalizeInsightSectionName(entry.section), String(entry.section).trim()].filter(Boolean)
      : null
  };
});

const tabDefs = {{
  rerun: {{
    rows: DATA.combined_rows,
    columns: [
      "task",
      "agent",
      "model",
      "n_trials",
      "exception_summary",
      "rerun_recommendation",
      "rerun_reason",
      "reward_mean",
      "reward_std",
      "reasoning",
      "trajectory_json_path",
      "verifier_test_stdout_path",
      "verifier_reward",
      "error_category",
      "matched_patterns"
    ],
  }},
  "accuracy-insight": {{
    rows: DATA.combined_rows,
    columns: [],
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

function fillRerunSummary() {{
  const rerunRows = Array.isArray(DATA.rerun_rows) ? DATA.rerun_rows : [];
  const summary = DATA.rerun_summary && typeof DATA.rerun_summary === "object" ? DATA.rerun_summary : {{}};

  // Build bullet list from combined_rows (has subagent reasoning merged in)
  const bulletSource = DATA.combined_rows.filter(function (row) {{
    return safeCell(row.rerun_recommendation) !== "";
  }});

  const fallback = {{
    cells_reviewed: bulletSource.length,
    rerun_yes: bulletSource.filter(function (row) {{ return row.rerun_recommendation === "yes"; }}).length,
    rerun_maybe: bulletSource.filter(function (row) {{ return row.rerun_recommendation === "maybe"; }}).length,
    rerun_no: bulletSource.filter(function (row) {{ return row.rerun_recommendation === "no"; }}).length,
  }};
  const merged = Object.assign({{}}, fallback, summary);
  const summaryText = merged.summary
    || `Reviewed ${{merged.cells_reviewed || 0}} orange cells. Final rerun labels prefer subagent judgments when present and otherwise fall back to the heuristic pass.`;
  document.getElementById("rerun-summary-text").textContent = summaryText;
  document.getElementById("rerun-reviewed").textContent = merged.cells_reviewed || 0;
  document.getElementById("rerun-yes").textContent = merged.rerun_yes || 0;
  document.getElementById("rerun-maybe").textContent = merged.rerun_maybe || 0;
  document.getElementById("rerun-no").textContent = merged.rerun_no || 0;

  const COLOR = {{ yes: "#067647", maybe: "#b54708", no: "#667085" }};
  const ORDER = {{ yes: 0, maybe: 1, no: 2 }};
  const sorted = bulletSource.slice().sort(function (a, b) {{
    const oa = ORDER[a.rerun_recommendation] ?? 9;
    const ob = ORDER[b.rerun_recommendation] ?? 9;
    if (oa !== ob) return oa - ob;
    return (a.task + a.agent + a.model).localeCompare(b.task + b.agent + b.model);
  }});
  const ul = document.getElementById("rerun-bullets");
  ul.innerHTML = sorted.map(function (row) {{
    const rec = safeCell(row.rerun_recommendation);
    const color = COLOR[rec] || "#667085";
    const label = `<strong style="color:${{color}}">${{escapeHtml(rec.toUpperCase())}}</strong>`;
    const cell = `<span class="mono">${{escapeHtml(row.task)}} / ${{escapeHtml(row.agent)}} / ${{escapeHtml(row.model)}}</span>`;
    const reason = safeCell(row.rerun_reason);
    return `<li>${{label}} — ${{cell}}${{reason ? ` — ${{escapeHtml(reason)}}` : ""}}</li>`;
  }}).join("");
}}

function fillFilters() {{
  const rows = Object.values(tabDefs).flatMap(def => def.rows);
  for (const [id, key] of [["task-filter","task"], ["agent-filter","agent"], ["model-filter","model"]]) {{
    const el = document.getElementById(id);
    el.innerHTML = `<option value="">All ${{key}}s</option>` + uniq(rows.map(row => row[key]))
      .map(value => `<option value="${{value}}">${{value}}</option>`).join("");
  }}
  // Error type filter: extract distinct exception type names from exception_summary
  const errorTypeSet = new Set();
  rows.forEach(function(row) {{
    safeCell(row.exception_summary).split(" | ").forEach(function(part) {{
      const name = part.split(":")[0].trim();
      if (name) errorTypeSet.add(name);
    }});
  }});
  const errorTypeEl = document.getElementById("error-type-filter");
  errorTypeEl.innerHTML = '<option value="">All error types</option>'
    + Array.from(errorTypeSet).sort().map(name => `<option value="${{name}}">${{name}}</option>`).join("");
}}

function rowMatches(row) {{
  const search = document.getElementById("search").value.trim().toLowerCase();
  const task = document.getElementById("task-filter").value;
  const agent = document.getElementById("agent-filter").value;
  const model = document.getElementById("model-filter").value;
  const errorType = document.getElementById("error-type-filter").value;
  const orangeOnly = document.getElementById("orange-only").dataset.active === "true";
  if (task && row.task !== task) return false;
  if (agent && row.agent !== agent) return false;
  if (model && row.model !== model) return false;
  if (errorType) {{
    const names = safeCell(row.exception_summary).split(" | ").map(p => p.split(":")[0].trim());
    if (!names.includes(errorType)) return false;
  }}
  if (orangeOnly && !isOrangeRow(row)) return false;
  if (!search) return true;
  return Object.values(row).join(" ").toLowerCase().includes(search);
}}

function applyTabControlVisibility() {{
  const orangeOnly = document.getElementById("orange-only");
  const insightPanel = document.getElementById("accuracy-insight-panel");
  const rerunPanel = document.getElementById("rerun-summary-panel");
  const isAccuracyInsight = currentTab === "accuracy-insight";
  const isRerun = currentTab === "rerun";
  orangeOnly.classList.toggle("hidden", !isRerun);
  document.getElementById("error-type-filter").classList.toggle("hidden", !isRerun);
  document.getElementById("rerun-instructions-btn").style.display = isRerun ? "inline-flex" : "none";
  document.getElementById("insight-instructions-btn").style.display = isAccuracyInsight ? "inline-flex" : "none";
  insightPanel.classList.toggle("hidden", !isAccuracyInsight);
  rerunPanel.classList.toggle("hidden", !isRerun);
}

function isMissingRow(row) {{
  return (
    Number(row.missing_agent_trajectory_json || 0) > 0 ||
    Number(row.missing_verifier_test_stdout_txt || 0) > 0 ||
    rowIsStdOutlier(row)
  );
}}

function isOrangeRow(row) {{
  return Number(row.ok_runs || 0) < 3;
}}

function cellClass(key, value, row) {{
  if (["count", "ok_runs", "missing_agent_trajectory_json", "missing_verifier_test_stdout_txt"].includes(key)) {{
    const n = Number(value || 0);
    if (n >= 5) return "count-bad mono";
    if (n > 0) return "count-warn mono";
    return "count-good mono";
  }}
  if (key === "reward_std" && String(value || "") && rowIsStdOutlier(row)) return "mono std-outlier";
  if (key === "verifier_test_stdout_path") return "mono col-stdout-wide";
  if (key === "trajectory_json_path" || key === "trajectory_last_step") return "mono";
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
      var label = "trajectory.json" + (idx + 1);
      if (path === "—") return '<span style="color:var(--muted)">' + label + '</span>';
      return '<div class="tooltip-wrap"><a class="path-link" href="file://' + encodeURI(path) + '" title="' + escapeHtml(path) + '">' + label + '</a>' + tooltip + '</div>';
    }}).join("");
  }}
  if (key === "verifier_test_stdout_path" && safeValue) {{
    var contents = safeCell(row.verifier_test_stdout_content).split(" || ");
    return safeValue.split(" | ").map(function (path, idx) {{
      var content = contents[idx] || "";
      var tooltip = content
        ? '<div class="tooltip-panel"><pre style="margin:0; font: inherit;">' + escapeHtml(content) + '</pre></div>'
        : "";
      var label = "test-stdout.txt" + (idx + 1);
      if (path === "—") return '<span style="color:var(--muted)">' + label + '</span>';
      return '<div class="tooltip-wrap"><a class="path-link" href="file://' + encodeURI(path) + '" title="' + escapeHtml(path) + '">' + label + '</a>' + tooltip + '</div>';
    }}).join("");
  }}
  if (key === "verifier_reward" && safeValue) {{
    return safeValue.split(" | ").map(function (val, idx) {{
      const v = val.trim();
      if (!v || v === "—") return '<div style="color:var(--muted);font-family:ui-monospace,monospace;font-size:12px">—</div>';
      return '<div style="font-family:ui-monospace,monospace;font-size:12px">' + escapeHtml(v) + '</div>';
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

function buildHeadHtml(columns) {{
  return columns.map(function (col) {{
    const indicator = sortState.key === col ? `<span class="sort-indicator">${sortState.dir === "asc" ? "↑" : "↓"}</span>` : "";
    return `<th data-col="${col}">${col.replaceAll("_", " ")}${indicator}</th>`;
  }}).join("");
}}

function escapeAttr(value) {{
  return escapeHtml(value).replaceAll("'", "&#39;");
}

function renderLeaderboardChart(scores) {{
  if (!scores || !scores.length) {{
    return '<p style="color:var(--muted);padding:4px 0">No get_leaderboard data found at /tmp/leaderboard_aggregate.json for this benchmark.</p>';
  }}
  const MODEL_LABELS = {{
    "gpt-5.4": "GPT 5.4",
    "gpt-5-mini": "GPT 5 Mini",
    "gpt-5-nano": "GPT 5 Nano",
    "claude-opus-4-6": "Claude Opus 4.6",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
    "gemini-3.1-pro-preview": "Gemini 3.1 Pro",
    "gemini-3-flash-preview": "Gemini 3 Flash",
  }};
  const AGENT_COLORS = {{
    "codex":      "#b5d4b0",
    "terminus-2": "#c8b8e8",
    "claude-code":"#f5cdb4",
    "gemini-cli": "#aacce8",
  }};
  // build model -> agent -> score lookup
  const byModel = {{}};
  scores.forEach(function(r) {{
    if (!byModel[r.model]) byModel[r.model] = {{}};
    byModel[r.model][r.agent] = r.score;
  }});
  const agents = [...new Set(scores.map(function(r) {{ return r.agent; }}))].sort();
  // sort models by best score desc
  const models = Object.keys(byModel).sort(function(a, b) {{
    const bA = Math.max.apply(null, Object.values(byModel[a]));
    const bB = Math.max.apply(null, Object.values(byModel[b]));
    return bB - bA;
  }});
  // dimensions
  const barH = 17, barGap = 3, groupGap = 12;
  const padL = 148, padR = 170, padT = 20, padB = 48;
  const W = 860;
  const totalBars = models.reduce(function(s, m) {{ return s + agents.filter(function(a) {{ return byModel[m][a] !== undefined; }}).length; }}, 0);
  const H = padT + padB + totalBars * (barH + barGap) + models.length * groupGap;
  const chartW = W - padL - padR;
  // x range (multiply by 100 for % display)
  const allScores = scores.map(function(r) {{ return r.score; }});
  const rawMin = Math.min.apply(null, allScores);
  const rawMax = Math.max.apply(null, allScores);
  const xMinVal = Math.min(rawMin, 0);
  const xMaxVal = rawMax + (rawMax - rawMin) * 0.08;
  const xRange = xMaxVal - xMinVal;
  function xPx(v) {{ return padL + (v - xMinVal) / xRange * chartW; }}
  const x0 = xPx(0);
  // ticks at nice multiples of 0.1 (displayed as %)
  const tickStep = xRange <= 0.5 ? 0.1 : xRange <= 1.2 ? 0.2 : 0.5;
  const tickStart = Math.ceil(xMinVal / tickStep) * tickStep;
  const ticks = [];
  for (let t = tickStart; t <= xMaxVal + 1e-9; t += tickStep) ticks.push(Math.round(t * 1e6) / 1e6);
  let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:' + W + 'px;display:block">';
  // vertical grid lines + x axis tick labels
  ticks.forEach(function(tick) {{
    const x = xPx(tick);
    svg += '<line x1="' + x.toFixed(1) + '" y1="' + padT + '" x2="' + x.toFixed(1) + '" y2="' + (H - padB) + '" stroke="#ebebeb" stroke-width="1"/>';
    svg += '<text x="' + x.toFixed(1) + '" y="' + (H - padB + 16) + '" text-anchor="middle" fill="#667085" font-size="11">' + (tick * 100).toFixed(0) + '</text>';
  }});
  // zero line when negatives exist
  if (rawMin < 0) {{
    svg += '<line x1="' + x0.toFixed(1) + '" y1="' + padT + '" x2="' + x0.toFixed(1) + '" y2="' + (H - padB) + '" stroke="#adb5bd" stroke-width="1.5" stroke-dasharray="4 3"/>';
  }}
  // bars
  let curY = padT;
  models.forEach(function(model) {{
    const modelAgents = agents.filter(function(a) {{ return byModel[model][a] !== undefined; }});
    const groupH = modelAgents.length * (barH + barGap) - barGap;
    const labelY = (curY + groupH / 2 + 4.5).toFixed(1);
    const label = MODEL_LABELS[model] || model;
    svg += '<text x="' + (padL - 10) + '" y="' + labelY + '" text-anchor="end" fill="#17202a" font-size="13">' + escapeHtml(label) + '</text>';
    modelAgents.forEach(function(agent) {{
      const score = byModel[model][agent];
      const color = AGENT_COLORS[agent] || "#d0d5dd";
      const xLeft  = xPx(Math.min(score, 0));
      const xRight = xPx(Math.max(score, 0));
      const bw = Math.max(xRight - xLeft, 1);
      svg += '<rect x="' + xLeft.toFixed(1) + '" y="' + curY.toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + barH + '" fill="' + color + '" rx="3"><title>' + escapeHtml(agent) + ': ' + (score * 100).toFixed(1) + '</title></rect>';
      // score label just after bar
      const lx = (xPx(Math.max(score, 0)) + 5).toFixed(1);
      const ly = (curY + barH - 4).toFixed(1);
      svg += '<text x="' + lx + '" y="' + ly + '" fill="#374151" font-size="11">' + (score * 100).toFixed(1) + '</text>';
      curY += barH + barGap;
    }});
    curY += groupGap;
  }});
  // x axis baseline
  svg += '<line x1="' + padL + '" y1="' + (H - padB) + '" x2="' + (W - padR) + '" y2="' + (H - padB) + '" stroke="#d0d5dd" stroke-width="1"/>';
  // x axis label
  svg += '<text x="' + (padL + chartW / 2).toFixed(1) + '" y="' + (H - padB + 34) + '" text-anchor="middle" fill="#374151" font-size="12">Benchmark score</text>';
  // legend (top-right)
  const lx0 = W - padR + 20;
  let ly = padT + 4;
  svg += '<text x="' + lx0 + '" y="' + ly + '" fill="#374151" font-size="12" font-weight="600">Agent</text>';
  ly += 20;
  agents.forEach(function(agent) {{
    const color = AGENT_COLORS[agent] || "#d0d5dd";
    svg += '<rect x="' + lx0 + '" y="' + (ly - 11) + '" width="14" height="14" fill="' + color + '" rx="3"/>';
    svg += '<text x="' + (lx0 + 20) + '" y="' + ly + '" fill="#374151" font-size="12">' + escapeHtml(agent) + '</text>';
    ly += 22;
  }});
  svg += '</svg>';
  return svg;
}}

function renderAccuracyInsightSections() {
  const panel = document.getElementById("accuracy-insight-panel");
  const chartHtml = '<div style="grid-column:1/-1;border:1px solid var(--line);border-radius:8px;background:var(--paper);padding:14px;margin-bottom:4px">'
    + '<h2 style="margin:0 0 4px;font-size:15px;">Agent × Model Score</h2>'
    + '<p style="margin:0 0 10px;color:var(--muted);font-size:13px;">Weighted average scores from <code>get_leaderboard</code> (official benchmark-level aggregates, p_window=3). Hover a bar for the exact score.</p>'
    + '<div style="overflow-x:auto">' + renderLeaderboardChart(DATA.leaderboard_scores) + '</div>'
    + '</div>';
  const inversionSections = new Set([
    "Model Inversions (across this benchmark)",
    "Agent Inversions (across this benchmark)",
    "Native Agent Underperformance",
    "Cross-Family Surprises",
    "Model Laggards (across leaderboard)",
    "Harness Laggards (across leaderboard)",
  ]);
  const sections = [
    ["Model Inversions (across this benchmark)", ACCURACY_INSIGHT_SUMMARY["Model Inversions"] || [], "Per (model, agent) mean across tasks from extracted trial data. Flags stronger models scoring >5pp below weaker family peers on the same agent."],
    ["Agent Inversions (across this benchmark)", ACCURACY_INSIGHT_SUMMARY["Agent Inversions"] || [], "Per (model, agent) mean across tasks from extracted trial data. Flags stronger agents scoring >5pp below weaker agents on the same model."],
    ["Native Agent Underperformance", ACCURACY_INSIGHT_SUMMARY["Native Agent Underperformance"] || []],
    ["Cross-Family Surprises", ACCURACY_INSIGHT_SUMMARY["Cross-Family Surprises"] || []],
    ["Model Laggards (across leaderboard)", LEADERBOARD_INSIGHTS["Model Laggards"] || [], "Per-model mean across all agents from get_leaderboard. Flags models whose cross-agent average inverts expected family ranking or is negative."],
    ["Harness Laggards (across leaderboard)", LEADERBOARD_INSIGHTS["Harness Laggards"] || [], "Per (model, agent) score from get_leaderboard. Flags agents ≥15pp below the same model’s best-agent score."],
  ];
  panel.innerHTML = chartHtml + sections.map(function (entry) {
    const title = entry[0], subset = entry[1], subtitle = entry[2] || "";
    const subtitleHtml = subtitle ? `<p style="margin:0 0 8px;color:var(--muted);font-size:12px">${escapeHtml(subtitle)}</p>` : "";
    if (!subset.length) {
      return `<section class="insight-section"><h3>${escapeHtml(title)}</h3><div class="insight-body">${subtitleHtml}<span class="insight-metric">0</span><div class="insight-empty">No examples found.</div></div></section>`;
    }
    const withInversions = inversionSections.has(title);
    const items = subset.slice(0, 8).map(function (text) {
      var detail = "";
      if (withInversions) {
        const normalizedTitle = normalizeInsightSectionName(title);
        INVERSION_ENTRIES.forEach(function(item) {
          if (
            item.sections &&
            !item.sections.includes(title) &&
            !item.sections.includes(normalizedTitle)
          ) return;
          const matched = item.matchType === "contains"
            ? text.includes(item.matchKey)
            : text.startsWith(item.matchKey);
          if (!matched) return;
          const entry = item.entry;
          var noteRows = (entry.task_notes || []).map(function(n) {
            const taskCell = escapeHtml(n.task);
            const modelCell = n.model ? `<td>${escapeHtml(n.model)}</td>` : "";
            return `<tr><td>${taskCell}</td>${modelCell}<td>${escapeHtml(n.note)}</td></tr>`;
          }).join("");
          detail = `<details class="inversion-detail">`
            + `<summary>Root-cause analysis ▸</summary>`
            + `<p>${escapeHtml(entry.root_cause)}</p>`
            + (noteRows ? `<table>${noteRows}</table>` : "")
            + `</details>`;
        });
      }
      return `<li>${escapeHtml(text)}${detail}</li>`;
    }).join("");
    return `<section class="insight-section">`
      + `<h3>${escapeHtml(title)}</h3>`
      + `<div class="insight-body">`
      + subtitleHtml
      + `<span class="insight-metric">${subset.length}</span>`
      + `<ul class="insight-list">${items}</ul>`
      + `</div>`
      + `</section>`;
  }).join("");
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
  if (currentTab === "accuracy-insight") {
    wrap.classList.add("hidden");
    renderAccuracyInsightSections();
    return;
  }
  wrap.classList.remove("hidden");
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
    sortState = {{ key: "", dir: "asc" }};
    document.querySelectorAll(".tab").forEach(el => el.classList.toggle("active", el === tab));
    renderTable();
  }});
}}

for (const id of ["search", "task-filter", "agent-filter", "model-filter", "error-type-filter", "orange-only"]) {{
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

document.getElementById("rerun-instructions-btn").addEventListener("click", function() {{
  document.getElementById("rerun-instructions-modal").classList.add("open");
}});
document.getElementById("modal-close").addEventListener("click", function() {{
  document.getElementById("rerun-instructions-modal").classList.remove("open");
}});
document.getElementById("rerun-instructions-modal").addEventListener("click", function(e) {{
  if (e.target === this) this.classList.remove("open");
}});

document.getElementById("insight-instructions-btn").addEventListener("click", function() {{
  document.getElementById("insight-instructions-modal").classList.add("open");
}});
document.getElementById("insight-modal-close").addEventListener("click", function() {{
  document.getElementById("insight-instructions-modal").classList.remove("open");
}});
document.getElementById("insight-instructions-modal").addEventListener("click", function(e) {{
  if (e.target === this) this.classList.remove("open");
}});

document.addEventListener("keydown", function(e) {{
  if (e.key === "Escape") {{
    document.getElementById("rerun-instructions-modal").classList.remove("open");
    document.getElementById("insight-instructions-modal").classList.remove("open");
  }}
}});

fillSummary();
fillRerunSummary();
fillFilters();
renderTable();
</script>
</body>
</html>"""
    template = template.replace("{{", "{").replace("}}", "}")
    return (
        template.replace("__BENCHMARK__", benchmark)
        .replace("__GENERATED__", generated_at)
        .replace("__OWNER_LINE__", owner_line)
        .replace("__DATA__", payload)
    )


def main() -> None:
    args = parse_args()
    ok_rows = read_tsv(args.tables_dir / "ok_runs.tsv")
    error_category_rows = read_tsv(args.tables_dir / "error_categories.tsv")
    error_type_rows = read_tsv(args.tables_dir / "error_types.tsv")
    missing_rows = read_tsv(args.tables_dir / "missing_extracted_files.tsv")
    reasoning_rows = read_optional_tsv(args.tables_dir / "reasoning.tsv")
    rerun_rows = read_optional_tsv(args.tables_dir / "rerun_summary.tsv")
    rerun_summary = read_optional_json(args.tables_dir / "rerun_summary.json")
    combined_rows = build_combined_rows(ok_rows, error_category_rows, missing_rows, reasoning_rows, rerun_rows)

    data = {
        "ok_rows": ok_rows,
        "error_category_rows": error_category_rows,
        "error_type_rows": error_type_rows,
        "missing_rows": missing_rows,
        "reasoning_rows": reasoning_rows,
        "rerun_rows": rerun_rows,
        "rerun_summary": rerun_summary,
        "combined_rows": combined_rows,
        "summary": build_summary(ok_rows, error_category_rows, error_type_rows, missing_rows),
        "leaderboard_scores": read_leaderboard_scores(args.benchmark),
        "inversion_analysis": read_inversion_analysis(args.benchmark),
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
