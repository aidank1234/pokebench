"""Persistent host budget/provider halt gate; the actor cannot read or clear it."""
from pathlib import Path

def check_quota_halt(path):
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise RuntimeError('Provider quota halt is active: ' + str(path))
