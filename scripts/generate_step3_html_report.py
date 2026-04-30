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


def build_combined_rows(ok_rows, error_category_rows):
    ok_index = {
        (row["task"], row["agent"], row["model"]): row
        for row in ok_rows
    }
    category_index = {}
    for row in error_category_rows:
        key = (row["task"], row["agent"], row["model"])
        category_index.setdefault(key, []).append(row)

    combined_rows = []
    all_keys = sorted(set(ok_index) | set(category_index))
    for key in all_keys:
        ok_row = ok_index.get(key, {})
        categories = category_index.get(key, [])
        base = {
            "task": key[0],
            "agent": key[1],
            "model": key[2],
            "n_trials": ok_row.get("n_trials") or (categories[0]["n_trials"] if categories else ""),
            "ok_runs": ok_row.get("ok_runs", "0"),
            "exception_summary": ok_row.get("exception_summary", ""),
            "reward_mean": ok_row.get("reward_mean", ""),
            "reward_std": ok_row.get("reward_std", ""),
            "reward_std_large_flag": ok_row.get("reward_std_large_flag", "no"),
        }
        if not categories:
            combined_rows.append(
                {
                    **base,
                    "error_category": "",
                    "matched_patterns": "",
                }
            )
            continue
        for category_row in categories:
            combined_rows.append(
                {
                    **base,
                    "error_category": category_row["error_category"],
                    "matched_patterns": category_row["matched_patterns"],
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
  grid-template-columns: 2fr repeat(3, minmax(150px, 1fr));
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
.table-wrap {{
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  background: var(--paper);
}}
table {{
  width: 100%;
  border-collapse: collapse;
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
    <button class="tab active" data-tab="missing">Missing Extracted Files</button>
    <button class="tab" data-tab="combined">Run Summary</button>
  </div>

  <div class="controls">
    <input id="search" type="search" placeholder="Filter by task, agent, model, or pattern">
    <select id="task-filter"><option value="">All tasks</option></select>
    <select id="agent-filter"><option value="">All agents</option></select>
    <select id="model-filter"><option value="">All models</option></select>
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
  missing: {{
    rows: DATA.missing_rows,
    columns: ["task", "agent", "model", "reward_mean", "reward_std", "missing_agent_trajectory_json", "missing_verifier_test_stdout_txt", "trajectory_json_path", "verifier_test_stdout_path"],
  }},
  combined: {{
    rows: DATA.combined_rows,
    columns: ["task", "agent", "model", "n_trials", "exception_summary", "reward_mean", "reward_std", "error_category", "matched_patterns"],
  }},
}};

let currentTab = "missing";
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
  if (task && row.task !== task) return false;
  if (agent && row.agent !== agent) return false;
  if (model && row.model !== model) return false;
  if (!search) return true;
  return Object.values(row).join(" ").toLowerCase().includes(search);
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

function buildHeadHtml(columns) {{
  return columns.map(function (col) {{
    const indicator = sortState.key === col ? `<span class="sort-indicator">${sortState.dir === "asc" ? "↑" : "↓"}</span>` : "";
    return `<th data-col="${col}">${col.replaceAll("_", " ")}${indicator}</th>`;
  }}).join("");
}}

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
  head.innerHTML = buildHeadHtml(def.columns);
  bindHeadClicks();
  const rows = def.rows.filter(rowMatches).slice().sort(function (a, b) {{
    if (!sortState.key) return 0;
    const cmp = compareValues(a, b, sortState.key);
    return sortState.dir === "asc" ? cmp : -cmp;
  }});
  body.innerHTML = rows.map(row => {{
    const highlightMissing =
      currentTab === "missing" &&
      (Number(row.missing_agent_trajectory_json || 0) > 0 ||
       Number(row.missing_verifier_test_stdout_txt || 0) > 0 ||
       rowIsStdOutlier(row));
    const highlightAllOk =
      currentTab === "combined" &&
      !(row.exception_summary === "OK:5" || Number(row.ok_runs || 0) === 5);
    const rowClass = highlightMissing ? ' class="row-missing"' : highlightAllOk ? ' class="row-all-ok"' : "";
    return `<tr${rowClass}>${def.columns.map(col => `<td class="${cellClass(col, row[col], row)}">${renderCell(col, row[col], row)}</td>`).join("")}</tr>`;
  }}).join("");
}}

for (const tab of document.querySelectorAll(".tab")) {{
  tab.addEventListener("click", () => {{
    currentTab = tab.dataset.tab;
    document.querySelectorAll(".tab").forEach(el => el.classList.toggle("active", el === tab));
    renderTable();
  }});
}}

for (const id of ["search", "task-filter", "agent-filter", "model-filter"]) {{
  document.getElementById(id).addEventListener("input", renderTable);
  document.getElementById(id).addEventListener("change", renderTable);
}}

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
    combined_rows = build_combined_rows(ok_rows, error_category_rows)

    data = {
        "ok_rows": ok_rows,
        "error_category_rows": error_category_rows,
        "error_type_rows": error_type_rows,
        "missing_rows": missing_rows,
        "combined_rows": combined_rows,
        "summary": build_summary(ok_rows, error_category_rows, error_type_rows, missing_rows),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_html(args.benchmark, data), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
