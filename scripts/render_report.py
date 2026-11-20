#!/usr/bin/env python3
"""Render the public results page from the included numeric data."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from summarize_results import calculate


def number(value, places=2):
    return "pending" if value is None else f"{float(value):.{places}f}"


def percent(value):
    return "pending" if value is None else number(value) + "%"


def render(root=ROOT, output=None):
    root = Path(root)
    summary, teams = calculate(root)
    models = summary["models"]
    lines = ["# Results", "", "Each system uses the same 30 starting fixtures: ten teams and three initial RNG states per team.", "",
        "| System | Wins | Win rate | Mean opponent HP depleted | PokéBench Score |",
        "| --- | ---: | ---: | ---: | ---: |"]
    for model in models:
        lines.append(f"| {model['label']} / {model['harness']} | {model['wins']}/{model['trials']} | "
            f"{percent(model['win_rate_percent'])} | {percent(model['mean_opponent_hp_depletion'])} | "
            f"{number(model['pokebench_score'])} |")
    if not summary["completed"]:
        lines += ["", "This cohort is incomplete. Unfinished cells are pending; a non-game stop does not become a loss.",
                  "A system’s final composite and component averages remain unavailable until all 30 cells are complete."]
    else:
        lines += ["", f"The selected cohort contains {summary['accepted_episodes']} genuine game endings: "
            f"{summary['total_victories']} wins and {summary['accepted_episodes']-summary['total_victories']} losses.", "",
            "PokéBench Score is the mean of the win percentage and mean terminal opponent HP-depletion percentage. "
            "Equivalently, each win earns 100 points and each loss earns half its HP-depletion percentage. "
            "The score was defined after these runs. [Methodology](METHODOLOGY.md) gives the formula, weighting choice and limitations."]
    lines += ["", "![PokéBench Score](figures/pokebench-score.svg)", "",
        "![Model performance](figures/pokebench-results.svg)", "", "## HP depletion", "",
        "Each original opponent contributes equally: `100 × average(1 − current HP / maximum HP)`. "
        "Measurements are taken at the terminal game state. Healing can reduce this value; it is not cumulative damage. "
        "The system mean includes winning battles at 100%.", "", "![Opponent HP depleted](figures/pokebench-progress.svg)", "",
        "## Results by team", "",
        "Each cell is wins out of the three starting states. Team specifications are in the [catalogue](TEAMS.md).", "",
        "| Team | " + " | ".join(model["label"] for model in models) + " |",
        "| --- | " + " | ".join("---:" for _ in models) + " |"]
    index = {(row["model"], row["team_id"]): row for row in teams}
    for team in sorted({row["team_id"] for row in teams}):
        lines.append("| " + team + " | " + " | ".join(
            f"{index[model['model'],team]['wins']}/{index[model['model'],team]['trials']}" for model in models) + " |")
    lines += ["", "![Battle results](figures/pokebench-fixtures.svg)", "",
        "## Cost records", "", "These are usage-priced estimates, not invoiced subscription charges.", "",
        "| System | Mean recorded estimate per battle | Coverage |", "| --- | ---: | --- |"]
    costs = {item["model"]: item for item in summary["cost_coverage"]}
    for model in models:
        cost = costs.get(model["model"])
        if cost and cost["mean_known_usage_priced_subtotal_usd"] is not None:
            lines.append(f"| {model['label']} | ${number(cost['mean_known_usage_priced_subtotal_usd'],3)} known subtotal | "
                         "7 complete usage records; 23 partial because failed requests returned no usage |")
        elif cost and cost["mean_native_list_price_estimate_usd"] is not None:
            lines.append(f"| {model['label']} | ${number(cost['mean_native_list_price_estimate_usd'])} native list-price estimate | "
                         "30 records, including recorded Haiku helper usage |")
        else:
            lines.append(f"| {model['label']} | Unknown | No comparable per-battle dollar estimate |")
    lines += ["", "Cost bases differ, so no cost-efficiency ranking is reported. "
        "[Per-battle cost records](../data/results/costs.csv) distinguish known subtotals from complete estimates; blank values are unknown.", "",
        "## Retained traces", "", "| System | Native records retained outside this repository |", "| --- | --- |"]
    traces = {row["model"]: row for row in summary["trace_coverage"]}
    for model in models:
        trace = traces[model["model"]]
        lines.append(f"| {model['label']} | {trace['exposed_reasoning']}; {trace['decisions'].lower()} |")
    lines += ["", "These are provider-exposed records, not a guarantee of complete internal reasoning. "
        "Host action/state logs retain wall time and emulator frames. Gemini thought chunks preserve order but lack timestamps for every chunk. "
        "Game recordings omit model-thinking pauses. Raw native sessions and recordings are retained separately; this release publishes numeric results and evidence commitments.", "",
        "## Data and verification", "", "- [180 episode rows](../data/results/episodes.csv)",
        "- [System summaries](../data/results/systems.csv) and [team aggregates](../data/results/by-team.csv)",
        "- [Structured results](../data/results/summary.json) and [score definition](../data/results/score-definition.json)",
        "- [Source and evidence commitments](../data/results/provenance.json) and [field definitions](../data/results/README.md)", "",
        "The comparison uses curated fixtures and different native harnesses and completion controls. "
        "Six unfinished Carbon games were replayed in fresh sessions; genuine losses were retained. "
        "Eight Flash results needed missing framework reward metadata reconstructed from existing terminal evidence, without changing the game outcomes. "
        "Source games passed AI protocol/evidence review, not independent human validation. See [methodology](METHODOLOGY.md) for these qualifications.", ""]
    text = "\n".join(lines)
    if output is not None:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return {"completed": summary["completed"], "summary": summary, "markdown": text}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    target = args.output or args.root / "docs/RESULTS.md"
    result = render(args.root, target)
    print(f"Wrote {target.name}; completed={result['completed']}.")


if __name__ == "__main__":
    main()
