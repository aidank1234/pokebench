"""Command-level delegation flags for human review; not a proof of nondelegation."""
import json
from pathlib import Path
import re

AI_COMMAND = re.compile(r'(?<![\w-])(?:codex|claude|gemini)(?=\s|$)|'
                        r'api\.openai\.com|api\.anthropic\.com|generativelanguage\.googleapis\.com|'
                        r'from\s+(?:openai|anthropic|google\.genai)\s+import|'
                        r'import\s+(?:openai|anthropic|google\.genai)', re.I)


def command_candidates(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('command', 'cmd') and isinstance(item, str):
                yield item
            elif key not in ('stdout', 'stderr', 'output', 'result', 'text'):
                yield from command_candidates(item)
    elif isinstance(value, list):
        for item in value:
            yield from command_candidates(item)


def audit_agent_logs(log_dir):
    log_dir = Path(log_dir)
    flags = []
    commands = 0
    for name in ('events.jsonl', 'tools.jsonl'):
        path = log_dir / name
        if not path.exists():
            continue
        for line_number, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            for command in command_candidates(event):
                commands += 1
                if AI_COMMAND.search(command):
                    flags.append({'file': name, 'line': line_number, 'command': command[:8000]})
    return {'command_count': commands, 'flags': flags, 'requires_manual_review': bool(flags),
            'scope': 'Heuristic command scan. Public networking remains available; this is not proof of no delegation.'}
