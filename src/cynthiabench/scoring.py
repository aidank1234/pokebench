"""PokéBench Score v1, derived only from verified terminal game outcomes."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Iterable, Mapping, Sequence

METRIC_ID = "pokebench-score-v1"
EXPECTED_BATTLES = 30


def decimal(value: Any) -> Decimal:
    """Parse a finite measurement without treating booleans as numbers."""
    if isinstance(value, bool) or value is None:
        raise ValueError("Expected a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Expected a finite number") from exc
    if not result.is_finite():
        raise ValueError("Expected a finite number")
    return result


def true(value: Any) -> bool:
    return value is True or value == "true" or value == "True" or value == "1"


def episode_score(episode: Mapping[str, Any]) -> Decimal | None:
    """Return points for an accepted game; a non-game stop remains pending.

    ``verified_terminal`` records the evidence review decision. A timeout,
    cancellation, missing verification, or non-game termination cannot supply
    a zero-point loss. This function does not itself verify emulator evidence.
    """
    if not true(episode.get("verified_terminal")):
        return None
    if episode.get("status") != "accepted":
        return None
    if episode.get("termination_category") != "game_terminal":
        return None
    outcome = episode.get("outcome")
    if outcome not in ("win", "loss"):
        return None
    hp = decimal(episode.get("opponent_hp_depletion"))
    if not 0 <= hp <= 100:
        raise ValueError("HP depletion must be between 0 and 100")
    expected_win = int(outcome == "win")
    if "win" in episode and decimal(episode["win"]) != expected_win:
        raise ValueError("Outcome and win flag disagree")
    if "win_score" in episode and decimal(episode["win_score"]) != 100 * expected_win:
        raise ValueError("Outcome and win score disagree")
    if outcome == "win" and hp != 100:
        raise ValueError("A verified win must have 100% opponent HP depletion")
    with localcontext() as context:
        context.prec = 40
        points = Decimal(100) if expected_win else hp / 2
    supplied = episode.get("pokebench_score")
    if supplied not in (None, "") and abs(decimal(supplied) - points) > Decimal("1e-12"):
        raise ValueError("Published battle score disagrees with v1 formula")
    return points


def average(values: Iterable[Any]) -> Decimal:
    values = [decimal(v) for v in values]
    if not values:
        raise ValueError("Cannot average an empty sequence")
    with localcontext() as context:
        context.prec = 40
        return sum(values, Decimal(0)) / len(values)


def summarize_system(model: str, episodes: Sequence[Mapping[str, Any]],
                     expected_cases: Sequence[str]) -> dict[str, Any]:
    """Publish a headline score only after all 30 declared cells are complete.

    Duplicate cells are an error, including duplicate genuine losses and wins.
    Selection and preservation of a first genuine result happen upstream.
    """
    expected = set(expected_cases)
    if len(expected_cases) != EXPECTED_BATTLES or len(expected) != EXPECTED_BATTLES:
        raise ValueError("Declare exactly 30 distinct expected fixtures")
    seen: set[str] = set()
    completed: list[tuple[Mapping[str, Any], Decimal]] = []
    for row in episodes:
        if row.get("model") != model:
            raise ValueError("Mixed model IDs in system summary")
        case = str(row.get("case", ""))
        if case not in expected:
            raise ValueError("Episode is outside the declared fixture plan")
        if case in seen:
            raise ValueError("Duplicate model/fixture cell")
        seen.add(case)
        score = episode_score(row)
        if score is not None:
            completed.append((row, score))
    complete_cases = {str(row["case"]) for row, _ in completed}
    wins = sum(row["outcome"] == "win" for row, _ in completed)
    result: dict[str, Any] = {
        "model": model, "trials": len(completed), "expected_trials": EXPECTED_BATTLES,
        "wins": wins, "losses": len(completed) - wins,
        "completed": complete_cases == expected,
        "pending_cases": sorted(expected - complete_cases),
        "win_rate_percent": None, "mean_opponent_hp_depletion": None,
        "pokebench_score": None, "win_credit": None, "loss_hp_credit": None,
    }
    if result["completed"]:
        with localcontext() as context:
            context.prec = 40
            win_rate = Decimal(100) * wins / EXPECTED_BATTLES
            hp = average(row["opponent_hp_depletion"] for row, _ in completed)
            score = average(points for _, points in completed)
            result.update(win_rate_percent=win_rate, mean_opponent_hp_depletion=hp,
                          pokebench_score=score, win_credit=win_rate,
                          loss_hp_credit=score - win_rate)
    return result
