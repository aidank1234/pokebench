"""The public report is numeric and never reads or exports replay binaries."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("public_report", ROOT / "scripts/render_report.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_public_report_matches_completed_dataset_without_video_outputs(tmp_path):
    target = tmp_path / "RESULTS.md"
    result = MODULE.render(ROOT, target)
    assert result["completed"] and result["summary"]["accepted_episodes"] == 180
    text = target.read_text()
    assert "78.12" in text and "43.76" in text and "180 genuine game endings" in text
    assert ".mp4" not in text and "media/" not in text
    assert set(tmp_path.iterdir()) == {target}
    assert text.index("figures/pokebench-score.svg") < text.index("figures/pokebench-results.svg")
    assert text.index("figures/pokebench-results.svg") < text.index("figures/pokebench-progress.svg")
    assert text.index("figures/pokebench-progress.svg") < text.index("figures/pokebench-fixtures.svg")


def test_rendering_without_output_is_read_only():
    result = MODULE.render(ROOT)
    assert result["completed"] and "# Results" in result["markdown"]
