"""Asset-free tests never invoke model providers; private fixtures skip explicitly."""
from pathlib import Path
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1]

def pytest_sessionstart(session):
    (ROOT/'work').mkdir(exist_ok=True)
    (ROOT/'reports').mkdir(exist_ok=True)

def pytest_collection_modifyitems(items):
    if sys.platform != 'darwin':
        marker=pytest.mark.skip(reason='Native isolation requires macOS Seatbelt')
        for item in items:
            if item.path.name == 'test_isolation.py' or item.name in {'test_agent_cannot_read_or_write_private_benchmark','test_zsh_heredoc_works_without_opening_private_sibling','test_direct_python_json_pipeline_is_quiet_and_stays_isolated'}:
                item.add_marker(marker)
