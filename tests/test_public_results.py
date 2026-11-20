import importlib.util
import json
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("public_result_summary", ROOT / "scripts/summarize_results.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def copied_data(tmp_path):
    shutil.copytree(ROOT / "data", tmp_path / "data")
    return tmp_path


def rewrite_episodes(root, rows):
    directory = root / "data/results"
    (directory / "episodes.csv").write_text(MODULE.csv_text(rows, list(rows[0])))
    definition = json.loads((directory / "score-definition.json").read_text())
    definition["source_sha256"] = MODULE.sha(directory / "episodes.csv")
    (directory / "score-definition.json").write_text(json.dumps(definition))


def test_public_tables_reproduce_all_180_games_and_six_scores():
    summary = MODULE.check(ROOT)
    assert summary["completed"] and summary["accepted_episodes"] == 180
    assert summary["total_victories"] == 86
    observed = {row["model"]: float(row["pokebench_score"]) for row in summary["models"]}
    expected = {"claude-opus-5-5": 78.12483900440991, "gemini-4-carbon-high": 70.5662387004531,
        "gpt-6-astra": 65.9006902113575, "gemini-4-neon-xhigh": 65.74259092996983,
        "gpt-6.1-sol": 64.2132442720678, "deepseek-flash": 43.76235086544508}
    assert observed == pytest.approx(expected, abs=1e-12)
    _, teams = MODULE.calculate(ROOT)
    assert len(teams) == 60 and sum(row["wins"] for row in teams) == 86


def test_check_does_not_change_public_files():
    before = {str(p.relative_to(ROOT)): MODULE.sha(p) for p in (ROOT / "data").rglob("*") if p.is_file()}
    MODULE.check(ROOT)
    assert before == {str(p.relative_to(ROOT)): MODULE.sha(p) for p in (ROOT / "data").rglob("*") if p.is_file()}


def test_duplicate_cell_is_not_treated_as_a_better_result(tmp_path):
    root = copied_data(tmp_path)
    rows = MODULE.read_csv(root / "data/results/episodes.csv")
    rewrite_episodes(root, rows + [{**rows[0], "trial_id": "another-trial"}])
    with pytest.raises(ValueError, match="Duplicate model/fixture"):
        MODULE.calculate(root)


def test_non_game_stop_with_zero_reward_leaves_headline_pending(tmp_path):
    root = copied_data(tmp_path)
    rows = MODULE.read_csv(root / "data/results/episodes.csv")
    changed = next(row for row in rows if row["model"] == "deepseek-flash" and row["outcome"] == "loss")
    changed.update(termination_category="time_limit", verified_terminal="false", pokebench_score="0")
    rewrite_episodes(root, rows)
    summary, _ = MODULE.calculate(root)
    flash = next(row for row in summary["models"] if row["model"] == "deepseek-flash")
    assert not summary["completed"] and flash["pokebench_score"] is None
    assert flash["losses"] == 24 and flash["trials"] == 29
    assert changed["case"] in flash["pending_cases"]


def test_fixture_identity_and_weights_cannot_silently_change(tmp_path):
    root = copied_data(tmp_path)
    rows = MODULE.read_csv(root / "data/results/episodes.csv")
    rows[0]["initial_rng_seed"] = "0"
    rewrite_episodes(root, rows)
    with pytest.raises(ValueError, match="Fixture identity"):
        MODULE.calculate(root)
    definition = json.loads((root / "data/results/score-definition.json").read_text())
    definition["win_weight"] = "0.75"
    (root / "data/results/score-definition.json").write_text(json.dumps(definition))
    with pytest.raises(ValueError, match="v1 weights"):
        MODULE.calculate(root)


def test_cost_unknowns_are_not_zeros():
    summary, _ = MODULE.calculate(ROOT)
    costs = {row["model"]: row for row in summary["cost_coverage"]}
    assert set(costs) == {"deepseek-flash", "claude-opus-5-5"}
    assert costs["deepseek-flash"]["fully_metered_battles"] == 7
    assert all(row["invoiced_cost_usd"] is None for row in costs.values())
