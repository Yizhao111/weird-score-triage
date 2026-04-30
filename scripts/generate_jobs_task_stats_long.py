import argparse
import csv
import json
import re
import tarfile
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate trial-level results under a jobs directory into a long-form CSV "
            "grouped by dataset, task, agent, and model."
        )
    )
    parser.add_argument(
        "--jobs-dir",
        type=Path,
        default=Path("jobs"),
        help="Path to the jobs directory. Defaults to ./jobs.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("jobs_task_stats_long.csv"),
        help="Output CSV path. Defaults to ./jobs_task_stats_long.csv.",
    )
    parser.add_argument(
        "--archive-dir",
        type=Path,
        default=None,
        help=(
            "Path to a benchmark archive tree laid out like "
            "<benchmark>/<task>/<model>/<agent>/*.tar.gz. "
            "When set, the script parses trial archives instead of jobs/*/*/result.json."
        ),
    )
    parser.add_argument(
        "--error-log",
        type=Path,
        default=None,
        help=(
            "Optional sidecar log for skipped unreadable JSON files and archive read "
            "failures. Defaults to <output>.errors.log when omitted."
        ),
    )
    return parser.parse_args()


def safe_mean(values: list[float | int | None]) -> float | None:
    present_values = [value for value in values if value is not None]
    return mean(present_values) if present_values else None


def infer_dataset(result_data: dict, result_path: Path) -> str:
    task_path = ((result_data.get("task_id") or {}).get("path")) or ""
    parts = task_path.split("/")
    if len(parts) >= 3 and parts[0] == "datasets":
        return parts[1]
    return result_path.parent.parent.name.split("__")[0]


def infer_error_type(exception_info: object) -> str:
    if isinstance(exception_info, dict):
        return exception_info.get("exception_type") or "UNKNOWN"
    if exception_info is None:
        return "OK"
    return type(exception_info).__name__


def format_float(value: float | None, digits: int) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


KNOWN_ERROR_PATTERNS = [
    "EnvironmentStartTimeoutError",
    "AgentSetupTimeoutError",
    "AgentTimeoutError",
    "VerifierTimeoutError",
    "CancelledError",
    "NonZeroAgentExitCodeError",
    "RuntimeError",
    "ValueError",
    "OSError",
    "NotFoundError",
    "ContextLengthExceededError",
    "OutputLengthExceededError",
    "DownloadVerifierDirError",
    "AddTestsDirError",
    "RewardFileNotFoundError",
    "RewardFileEmptyError",
    "FileNotFoundError",
    "VerifierOutputParseError",
    "DaytonaError",
    "DaytonaNotFoundError",
    "DaytonaRateLimitError",
    "RateLimitError",
    "BadRequestError",
]


def read_tar_member(tf: tarfile.TarFile, suffixes, max_chars: int = 50000) -> str:
    if isinstance(suffixes, str):
        suffixes = [suffixes]
    for member in tf.getmembers():
        if any(member.name.endswith(suffix) for suffix in suffixes):
            extracted = tf.extractfile(member)
            if not extracted:
                return ""
            return extracted.read(max_chars).decode("utf-8", "replace")
    return ""


def collect_log_patterns(text: str) -> list[str]:
    if not text:
        return []
    found = []
    for pattern in KNOWN_ERROR_PATTERNS:
        if pattern in text:
            found.append(pattern)

    # Also capture any other exception/error-like class names that appear in logs.
    # This keeps the parser open-ended instead of limiting it to the seeded list.
    generic_tokens = set(
        re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception))\b", text)
    )
    generic_tokens.discard("Error")
    generic_tokens.discard("Exception")
    found.extend(sorted(generic_tokens))

    lower = text.lower()
    if "timed out" in lower or "timeout" in lower:
        found.append("timeout_text")
    if "/app/law.py" in lower and "no such file" in lower:
        found.append("missing_law_py")
    if "no such file or directory" in lower:
        found.append("missing_file_text")
    if "missing required" in lower:
        found.append("missing_required_text")
    if "rate limit" in lower:
        found.append("rate_limit_text")
    if "resource_exhausted" in lower:
        found.append("resource_exhausted_text")
    if "billing" in lower:
        found.append("billing_text")
    if "credit balance is too low" in lower:
        found.append("credit_balance_low")
    if "traceback" in lower:
        found.append("traceback_text")

    return sorted(set(found))


def load_trial_groups(
    jobs_dir: Path,
    error_events: list[str] | None = None,
) -> dict[tuple[str, str, str, str], list[dict[str, object]]]:
    trial_groups: dict[tuple[str, str, str, str], list[dict[str, object]]] = (
        defaultdict(list)
    )

    for result_path in sorted(jobs_dir.glob("*/*/result.json")):
        try:
            result_data = json.loads(result_path.read_text())
        except Exception as exc:
            message = f"Skipping unreadable JSON: {result_path} ({exc})"
            print(message)
            if error_events is not None:
                error_events.append(message)
            continue

        config = result_data.get("config") or {}
        agent_config = config.get("agent") or {}
        task_name = result_data.get("task_name")
        agent_name = agent_config.get("name")
        model_name = agent_config.get("model_name")
        if not (task_name and agent_name and model_name):
            continue

        dataset = infer_dataset(result_data, result_path)
        agent_result = result_data.get("agent_result") or {}
        verifier_result = result_data.get("verifier_result") or {}
        rewards = verifier_result.get("rewards") or {}

        key = (dataset, task_name, agent_name, model_name)
        trial_groups[key].append(
            {
                "reward": rewards.get("reward"),
                "errortype": infer_error_type(result_data.get("exception_info")),
                "log_patterns": "",
            }
        )

    return trial_groups


def load_archive_trial_groups(
    archive_dir: Path,
    error_events: list[str] | None = None,
) -> dict[tuple[str, str, str, str], list[dict[str, object]]]:
    trial_groups: dict[tuple[str, str, str, str], list[dict[str, object]]] = (
        defaultdict(list)
    )
    dataset = archive_dir.name

    for archive_path in sorted(archive_dir.glob("*/*/*/*.tar.gz")):
        task_name, model_name, agent_name, _ = archive_path.relative_to(archive_dir).parts

        try:
            with tarfile.open(archive_path, "r:gz") as tf:
                result_text = read_tar_member(tf, "result.json")
                exception_text = read_tar_member(tf, "exception.txt", 20000)
                verifier_text = read_tar_member(tf, "verifier/test-stdout.txt", 20000)
                agent_text = read_tar_member(
                    tf,
                    ["agent/claude-code.txt", "agent/codex.txt", "agent/gemini-cli.txt"],
                    20000,
                )
        except Exception as exc:
            if error_events is not None:
                error_events.append(f"Archive read failure: {archive_path} ({exc})")
            key = (dataset, task_name, agent_name, model_name)
            trial_groups[key].append(
                {
                    "reward": None,
                    "errortype": "TarReadError",
                    "log_patterns": f"TarReadError | {type(exc).__name__}",
                }
            )
            continue

        result_data = {}
        if result_text:
            try:
                result_data = json.loads(result_text)
            except Exception:
                result_data = {}

        agent_result = result_data.get("agent_result") or {}
        verifier_result = result_data.get("verifier_result") or result_data.get("verifier") or {}
        reward = result_data.get("reward")
        if reward is None and isinstance(verifier_result, dict):
            rewards = verifier_result.get("rewards") or {}
            reward = rewards.get("reward", verifier_result.get("reward"))

        exception_info = result_data.get("exception_info")
        exception_type = infer_error_type(exception_info)
        log_patterns = collect_log_patterns(
            "\n".join([result_text, exception_text, verifier_text, agent_text])
        )

        key = (dataset, task_name, agent_name, model_name)
        trial_groups[key].append(
            {
                "reward": reward,
                "errortype": exception_type,
                "log_patterns": " | ".join(log_patterns),
            }
        )

    return trial_groups


def write_long_csv(
    output_path: Path,
    trial_groups: dict[tuple[str, str, str, str], list[dict[str, object]]],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "dataset",
                "task",
                "agent",
                "model",
                "n_trials",
                "reward_mean",
                "errortype",
                "log_patterns",
            ]
        )

        for (dataset, task_name, agent_name, model_name), matches in sorted(
            trial_groups.items()
        ):
            err_counts = Counter(match["errortype"] for match in matches)
            err_summary = " | ".join(
                f"{err}:{count}"
                for err, count in sorted(
                    err_counts.items(), key=lambda item: (item[0] != "OK", item[0])
                )
            )
            log_counts = Counter()
            for match in matches:
                for pattern in str(match.get("log_patterns") or "").split(" | "):
                    if pattern:
                        log_counts[pattern] += 1
            log_summary = " | ".join(
                f"{pattern}:{count}" for pattern, count in sorted(log_counts.items())
            )

            writer.writerow(
                [
                    dataset,
                    task_name,
                    agent_name,
                    model_name,
                    len(matches),
                    format_float(
                        safe_mean([match["reward"] for match in matches]), digits=6
                    ),
                    err_summary,
                    log_summary,
                ]
            )


def main() -> None:
    args = parse_args()
    error_events: list[str] = []
    if args.archive_dir:
        trial_groups = load_archive_trial_groups(args.archive_dir, error_events)
    else:
        trial_groups = load_trial_groups(args.jobs_dir, error_events)
    write_long_csv(args.output, trial_groups)
    error_log = args.error_log or args.output.with_suffix(args.output.suffix + ".errors.log")
    error_log.parent.mkdir(parents=True, exist_ok=True)
    error_log.write_text(
        "\n".join(error_events) + ("\n" if error_events else ""),
        encoding="utf-8",
    )
    n_trials = sum(len(matches) for matches in trial_groups.values())
    print(
        f"Wrote {args.output} with {len(trial_groups)} aggregate rows "
        f"from {n_trials} trials."
    )
    print(f"Wrote {error_log} with {len(error_events)} logged error event(s).")


if __name__ == "__main__":
    main()
