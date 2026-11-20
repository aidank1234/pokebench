"""Infrastructure-only agent. Never use it for claims about Pokemon competence."""
import asyncio
import json
from harbor.agents.base import BaseAgent
from .battle_client import request


class FixtureAgent(BaseAgent):
    @staticmethod
    def name():
        return 'cynthia-infrastructure-fixture'

    def version(self):
        return '1'

    async def setup(self, environment):
        pass

    async def run(self, instruction, environment, context):
        environment.block_ai_clients = True
        shell = await environment.exec('python3 -c "print(6 * 7)"', timeout_sec=20)
        if shell.return_code != 0 or shell.stdout.strip() != '42':
            raise RuntimeError('Isolated shell regression: ' + str(shell))
        session = json.loads((environment.workspace / 'battle-session.json').read_text())
        result = await asyncio.to_thread(request, session, '/v1/action', {'buttons': ['A'], 'frames': 1})
        context.metadata = {'mock_infrastructure_test_only': True, 'result': result}
