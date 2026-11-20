#!/usr/bin/env python3
"""Recompute the published tables from the included episode CSV.

No emulator, model credentials, network access, or Harbor installation is needed.
"""
from __future__ import annotations

import argparse
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from cynthiabench.scoring import METRIC_ID, average, decimal, episode_score, summarize_system, true

SYSTEM_FIELDS = ["model", "label", "harness", "cli_version", "cli_sha256", "effort",
    "max_output_tokens", "trials", "wins", "losses", "win_rate_percent",
    "mean_opponent_hp_depletion", "pokebench_score", "win_credit", "loss_hp_credit",
    "median_actions", "median_wall_seconds", "episodes_using_reviewed_helper"]
TEAM_FIELDS = ["model", "team_id", "trials", "wins", "losses", "win_rate_percent",
               "mean_opponent_hp_depletion", "mean_battle_score"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_csv(path):
    with Path(path).open(newline="") as stream:
        return list(csv.DictReader(stream))


def jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    return value


def csv_text(rows, fields):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def cost_summary(rows):
    summary = []
    for model in dict.fromkeys(row["model"] for row in rows):
        group = [row for row in rows if row["model"] == model]
        known = [decimal(row["known_usage_priced_subtotal_usd"]) for row in group
                 if row["known_usage_priced_subtotal_usd"]]
        estimates = [decimal(row["native_list_price_estimate_usd"]) for row in group
                     if row["native_list_price_estimate_usd"]]
        summary.append({"model": model, "recorded_battles": len(group),
            "known_usage_priced_subtotal_usd": sum(known) if known else None,
            "mean_known_usage_priced_subtotal_usd": average(known) if known else None,
            "native_list_price_estimate_usd": sum(estimates) if estimates else None,
            "mean_native_list_price_estimate_usd": average(estimates) if estimates else None,
            "fully_metered_battles": sum(true(row["usage_complete"]) for row in group) if known else None,
            "invoiced_cost_usd": None})
    return summary


def calculate(root=ROOT):
    root = Path(root)
    directory = root / "data/results"
    study = json.loads((directory / "study.json").read_text())
    definition = json.loads((directory / "score-definition.json").read_text())
    if definition["metric_id"] != METRIC_ID:
        raise ValueError("Unsupported scoring definition")
    if decimal(definition["win_weight"]) != Decimal("0.5") or decimal(definition["terminal_hp_weight"]) != Decimal("0.5"):
        raise ValueError("The v1 weights must remain 0.5 / 0.5")
    if definition["source_sha256"] != sha(directory / "episodes.csv"):
        raise ValueError("Score definition does not match the included episode table")
    fixtures = json.loads((root / "data/teams/fixtures.json").read_text())["fixtures"]
    expected = {fixture["name"]: fixture for fixture in fixtures}
    if len(fixtures) != 30 or len(expected) != 30:
        raise ValueError("Expected 30 distinct fixture records")
    if len({fixture["team_id"] for fixture in fixtures}) != 10:
        raise ValueError("Expected ten teams")
    for team in {f["team_id"] for f in fixtures}:
        if len([f for f in fixtures if f["team_id"] == team]) != 3:
            raise ValueError("Each team needs three starting states")
    rows = read_csv(directory / "episodes.csv")
    profiles = {profile["model"]: profile for profile in study["systems"]}
    if len(profiles) != study["system_count"]:
        raise ValueError("Duplicate or missing system profiles")
    seen_trials = set()
    for row in rows:
        if row["model"] not in profiles or row["case"] not in expected:
            raise ValueError("Episode is outside the declared study")
        profile = profiles[row["model"]]
        for key in ("harness", "cli_version", "cli_sha256", "effort", "max_output_tokens"):
            wanted = "" if profile[key] is None else str(profile[key])
            if row[key] != wanted:
                raise ValueError("Episode settings disagree with the system profile")
        fixture = expected[row["case"]]
        for key, expected_value in [("team_id", fixture["team_id"]),
                                    ("replicate", fixture["replicate"]),
                                    ("initial_rng_seed", fixture["seed"])]:
            if str(row[key]) != str(expected_value):
                raise ValueError("Fixture identity mismatch: " + row["case"])
        for key in ("state_sha256", "save_sha256", "rom_sha256", "core_sha256"):
            if row[key] != fixture[key]:
                raise ValueError("Fixture hash mismatch: " + row["case"])
        if row["trial_id"] in seen_trials:
            raise ValueError("Duplicate trial ID")
        seen_trials.add(row["trial_id"])
        for key in ("terminal_evidence_sha256", "original_recording_sha256"):
            value = row[key]
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("Invalid evidence commitment")
        for key in ("actions", "emulated_frames", "wall_seconds"):
            if decimal(row[key]) < 0:
                raise ValueError("Negative runtime measurement")
        episode_score(row)
    models = []
    by_team = []
    for model, profile in profiles.items():
        games = [row for row in rows if row["model"] == model]
        result = {**profile, **summarize_system(model, games, list(expected))}
        result["median_actions"] = statistics.median([int(row["actions"]) for row in games]) if games else None
        result["median_wall_seconds"] = statistics.median([float(row["wall_seconds"]) for row in games]) if games else None
        result["episodes_using_reviewed_helper"] = sum(true(row["reviewed_native_helper_used"]) for row in games)
        models.append(result)
        for team in sorted({fixture["team_id"] for fixture in fixtures}):
            cells = [row for row in games if row["team_id"] == team]
            valid = [(row, episode_score(row)) for row in cells]
            valid = [(row, points) for row, points in valid if points is not None]
            complete = len(valid) == 3 and len({row["case"] for row, _ in valid}) == 3
            wins = sum(row["outcome"] == "win" for row, _ in valid)
            by_team.append({"model": model, "team_id": team, "trials": len(valid),
                "wins": wins, "losses": len(valid) - wins,
                "win_rate_percent": average(100 * int(row["outcome"] == "win") for row, _ in valid) if complete else None,
                "mean_opponent_hp_depletion": average(row["opponent_hp_depletion"] for row, _ in valid) if complete else None,
                "mean_battle_score": average(points for _, points in valid) if complete else None})
    complete = all(model["completed"] for model in models)
    if complete:
        models.sort(key=lambda model: (-model["pokebench_score"], model["model"]))
    costs = read_csv(directory / "costs.csv")
    if len({(row["model"], row["case"]) for row in costs}) != len(costs):
        raise ValueError("Duplicate cost row")
    known_cells = {(row["model"], row["case"]) for row in rows}
    if any((row["model"], row["case"]) not in known_cells for row in costs):
        raise ValueError("Cost row is outside the selected cohort")
    summary = {"schema_version": 2, "benchmark": "PokéBench", "study_version": study["study_version"],
        "metric_id": METRIC_ID, "completed": complete, "team_count": 10,
        "rng_fixtures_per_team": 3, "system_count": len(models),
        "planned_episodes": len(profiles) * 30, "accepted_episodes": sum(m["trials"] for m in models),
        "total_victories": sum(m["wins"] for m in models),
        "models": models, "cost_coverage": cost_summary(costs),
        "trace_coverage": study["trace_coverage"], "protocol_notes": study["protocol_notes"],
        "review_scope": study["review_scope"], "source_episodes_sha256": sha(directory / "episodes.csv")}
    return summary, by_team


def check(root=ROOT):
    root = Path(root)
    directory = root / "data/results"
    summary, teams = calculate(root)
    if not summary["completed"]:
        raise ValueError("The published cohort is incomplete; no final score is available")
    expected_json = jsonable(summary)
    if json.loads((directory / "summary.json").read_text()) != expected_json:
        raise ValueError("summary.json does not match the episode data")
    for name, data, fields in [("systems.csv", summary["models"], SYSTEM_FIELDS),
                               ("by-team.csv", teams, TEAM_FIELDS)]:
        if (directory / name).read_text() != csv_text(data, fields):
            raise ValueError(name + " does not match the episode data")
    provenance = json.loads((directory / "provenance.json").read_text())
    for relative, expected_hash in provenance["published_artifact_sha256"].items():
        if sha(root / relative) != expected_hash:
            raise ValueError("Published artifact changed: " + relative)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true", help="Validate published tables without writing files")
    parser.add_argument("--write", action="store_true", help="Regenerate aggregate CSV/JSON files")
    args = parser.parse_args()
    if args.check and args.write:
        parser.error("Choose --check or --write")
    if args.check:
        summary = check(args.root)
        print(f"Verified {summary['accepted_episodes']} games, {summary['system_count']} systems, {summary['total_victories']} wins.")
        return
    summary, teams = calculate(args.root)
    if args.write:
        directory = args.root / "data/results"
        (directory / "systems.csv").write_text(csv_text(summary["models"], SYSTEM_FIELDS))
        (directory / "by-team.csv").write_text(csv_text(teams, TEAM_FIELDS))
        (directory / "summary.json").write_text(json.dumps(jsonable(summary), indent=2, ensure_ascii=False) + "\n")
        print(f"Wrote aggregate tables; completed={summary['completed']}.")
    else:
        print(json.dumps(jsonable(summary), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
