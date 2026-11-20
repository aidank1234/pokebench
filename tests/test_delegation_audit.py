import json

from cynthiabench.delegation_audit import audit_agent_logs


def test_flags_tool_commands_without_treating_model_discussion_as_execution(tmp_path):
    events = [
        {'item': {'type': 'command_execution', 'command': 'python3 battle.py observe'}},
        {'message': {'content': [{'type': 'text', 'text': 'I must not invoke codex or claude.'}]}},
        {'message': {'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'claude -p "help"'}}]}},
    ]
    (tmp_path / 'events.jsonl').write_text('\n'.join(map(json.dumps, events)))
    (tmp_path / 'tools.jsonl').write_text(json.dumps({'tool': {'name': 'shell', 'args': {'command': 'curl https://api.openai.com/v1/responses'}}}) + '\n')
    result = audit_agent_logs(tmp_path)
    assert result['command_count'] == 3
    assert len(result['flags']) == 2
    assert result['requires_manual_review']
