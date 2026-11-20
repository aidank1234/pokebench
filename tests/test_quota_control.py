import asyncio
import json
from types import SimpleNamespace
import pytest

from cynthiabench.quota_control import run_with_quota_halt, ProviderQuotaHalt
from cynthiabench.harbor_env import CynthiaEnvironment


def test_quota_latch_cancels_running_job_and_preserves_cleanup(tmp_path):
    halt, record = tmp_path / 'halt.json', tmp_path / 'stop.json'
    state = {'cleaned': False, 'started': False}

    async def job_run():
        state['started'] = True
        try:
            await asyncio.sleep(100)
        finally:
            state['cleaned'] = True

    async def run():
        async def publish():
            await asyncio.sleep(.02)
            halt.write_text('{"quota_blocked":true}')
        writer = asyncio.create_task(publish())
        with pytest.raises(ProviderQuotaHalt):
            await run_with_quota_halt(SimpleNamespace(run=job_run), halt, record, .005)
        await writer

    asyncio.run(run())
    assert state == {'started': True, 'cleaned': True}
    assert json.loads(record.read_text())['reason'] == 'provider_quota_halt'


def test_existing_halt_does_not_start_job_or_environment(tmp_path):
    halt = tmp_path / 'halt.json'; halt.write_text('{}')

    async def job_run():
        raise AssertionError('No scheduling allowed')

    async def run():
        with pytest.raises(ProviderQuotaHalt):
            await run_with_quota_halt(SimpleNamespace(run=job_run), halt, tmp_path / 'stop.json')
        environment = object.__new__(CynthiaEnvironment)
        environment.quota_halt_path = halt
        with pytest.raises(RuntimeError, match='quota halt'):
            await environment.start()
    asyncio.run(run())


def test_normal_job_result_returns_and_external_cancel_is_not_quota(tmp_path):
    async def normal():
        return {'complete': True}

    async def cancelled():
        raise asyncio.CancelledError()

    async def run():
        assert await run_with_quota_halt(SimpleNamespace(run=normal), tmp_path / 'halt', tmp_path / 'stop') == {'complete': True}
        with pytest.raises(asyncio.CancelledError):
            await run_with_quota_halt(SimpleNamespace(run=cancelled), tmp_path / 'halt', tmp_path / 'stop')
    asyncio.run(run())
