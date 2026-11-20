from decimal import Decimal
import pytest

from cynthiabench.scoring import episode_score, summarize_system


def episode(case="case-0", outcome="loss", hp="80", **changes):
    return {"model": "test-model", "case": case, "status": "accepted",
        "verified_terminal": True, "termination_category": "game_terminal",
        "outcome": outcome, "win": int(outcome == "win"),
        "opponent_hp_depletion": hp, **changes}


def test_victory_bonus_and_partial_loss_credit():
    assert episode_score(episode(outcome="win", hp="100")) == 100
    assert episode_score(episode(hp="80")) == 40
    assert episode_score(episode(hp="0")) == 0


@pytest.mark.parametrize("change", [
    {"verified_terminal": False}, {"termination_category": "time_limit"},
    {"termination_category": "token_limit"}, {"termination_category": "tool_failure"},
    {"outcome": "timeout"}, {"outcome": "draw"}, {"status": "unreviewed"},
    {"status": "rejected"}, {"status": "invalid"}, {"status": None},
])
def test_non_game_or_unverified_stop_never_becomes_a_zero_loss(change):
    assert episode_score(episode(hp="0", **change)) is None


@pytest.mark.parametrize("row", [episode(hp="NaN"), episode(hp="101"), episode(hp="-1"),
    episode(outcome="win", hp="99"), episode(win=1), episode(win_score=100),
    episode(pokebench_score="0")])
def test_invalid_terminal_measurements_are_rejected(row):
    with pytest.raises(ValueError):
        episode_score(row)


def test_composite_is_withheld_until_all_thirty_cells_are_complete():
    expected = [f"case-{i}" for i in range(30)]
    games = [episode(case=case, outcome="win", hp="100") for case in expected]
    games[-1] = episode(case=expected[-1], hp="0", termination_category="time_limit")
    pending = summarize_system("test-model", games, expected)
    assert not pending["completed"] and pending["trials"] == 29
    assert pending["losses"] == 0 and pending["pending_cases"] == [expected[-1]]
    assert pending["pokebench_score"] is None and pending["win_rate_percent"] is None
    games[-1] = episode(case=expected[-1], hp="80")
    complete = summarize_system("test-model", games, expected)
    assert complete["completed"] and complete["trials"] == 30
    assert complete["wins"] == 29 and complete["losses"] == 1
    assert abs(complete["pokebench_score"] - Decimal(2940) / 30) < Decimal("1e-25")
    assert complete["pokebench_score"] == complete["win_credit"] + complete["loss_hp_credit"]


def test_missing_cells_and_duplicate_replacements_cannot_complete_a_cohort():
    expected = [f"case-{i}" for i in range(30)]
    assert summarize_system("test-model", [], expected)["pokebench_score"] is None
    genuine_loss = episode()
    later_win = episode(outcome="win", hp="100")
    with pytest.raises(ValueError, match="Duplicate"):
        summarize_system("test-model", [genuine_loss, later_win], expected)
    with pytest.raises(ValueError, match="30 distinct"):
        summarize_system("test-model", [], expected[:-1])


def test_zero_win_cohort_keeps_loss_progress_without_fake_wins():
    expected = [f"case-{i}" for i in range(30)]
    result = summarize_system("test-model", [episode(case=case, hp="80") for case in expected], expected)
    assert result["completed"] and result["wins"] == 0
    assert result["win_rate_percent"] == 0 and result["pokebench_score"] == 40


def test_missing_acceptance_status_is_not_accepted():
    row = episode()
    del row["status"]
    assert episode_score(row) is None
