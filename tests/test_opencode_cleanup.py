"""Cancellation races against a fake CLI/proxy; no model or game calls."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from cynthiabench import opencode_agent as oc
from test_opencode_agent import registration, status, transcript


def harness(tmp_path, monkeypatch, *, hold_export=False, hold_close=False,
            close_fails=False, hold_registration=False):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    state = SimpleNamespace(export_started=None, export_release=None,
        close_started=threading.Event(), close_release=threading.Event(),
        registration_started=threading.Event(), registration_release=threading.Event(),
        order=[], processes=[], export_wait_cancelled=False)

    async def spawn(*args, **kwargs):
        primary = not state.processes
        async def drain(): pass
        def write(raw): state.prompt = raw.decode()
        proc = SimpleNamespace(pid=9999999-len(state.processes), returncode=None,
            stdin=SimpleNamespace(write=write, drain=drain, close=lambda: None),
            stopped=asyncio.Event())
        state.processes.append(proc)
        async def wait():
            if primary:
                kwargs['stdout'].write((json.dumps({'type': 'step_finish',
                    'sessionID': 'ses_test', 'part': {'reason': 'stop'}})+'\n').encode())
            else:
                state.export_started.set()
                if hold_export:
                    try:
                        await state.export_release.wait()
                    except asyncio.CancelledError:
                        state.export_wait_cancelled = True
                        raise
                if proc.returncode is not None:
                    return proc.returncode
                kwargs['stdout'].write(json.dumps(transcript(workspace, state.prompt)).encode())
            proc.returncode = 0
            return 0
        proc.wait = wait
        return proc

    def proxy(_config, path, body=None):
        if path == '/admin/trials':
            state.registration_started.set()
            if hold_registration:
                assert state.registration_release.wait(3)
            return registration()
        if path.endswith('/cancel'):
            state.order.append('cancel')
            return {'trial_id': 'trial-test', 'cancellation_requested': True}
        if path.endswith('/close'):
            state.close_started.set()
            if hold_close:
                assert state.close_release.wait(3)
            state.order.append('close')
            if close_fails:
                raise OSError('synthetic revocation failure')
            return {'trial_id': 'trial-test', 'closed': True}
        state.order.append('status')
        current = status()
        if 'cancel' in state.order:
            current.update(cancellation_requested=True,
                pre_cancellation_provider_failure=False,
                pre_cancellation_infrastructure_failure=False)
        return current

    monkeypatch.setattr(oc.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(oc, 'proxy_request', proxy)
    monkeypatch.setattr(oc, 'strict_sandbox_profile', lambda *a, **k: '(deny default)')
    monkeypatch.setattr(oc, 'validate_cli_pin', lambda x: x)
    monkeypatch.setattr(oc, 'calculation_python_identity', lambda x: {'executable': str(x)})
    agent = object.__new__(oc.OpenCodeFlashAgent)
    agent.cli_pin = {'provider': 'opencode', 'executable': '/fake/opencode',
                     'version': '1.19.6', 'sha256': 'a'*64}
    agent.logs_dir = tmp_path / 'logs'
    agent.quota_halt_path = tmp_path / 'halt.json'
    agent.proxy_admin_config_path = tmp_path / 'admin.json'
    environment = SimpleNamespace(workspace=workspace, session_id='trial-test',
        admin=SimpleNamespace(url='http://127.0.0.1:12345'),
        battle_session={'session_id': 'game'})
    return agent, environment, SimpleNamespace(metadata={}), state


@pytest.mark.parametrize('cancel_count', [1, 3])
def test_late_cancel_during_export_preserves_full_evidence_and_revokes(tmp_path, monkeypatch, cancel_count):
    agent, environment, context, state = harness(tmp_path, monkeypatch, hold_export=True)
    async def race():
        state.export_started, state.export_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(agent.run('original task', environment, context))
        await state.export_started.wait()
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        assert not state.export_wait_cancelled
        state.export_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(race())
    audit = json.loads((agent.logs_dir / 'runtime-audit.json').read_text())
    assert audit['terminal_event'] and not audit['native_evidence_error']
    assert audit['assistant_message_count'] == 1 and audit['real_user_prompt_count'] == 1
    assert audit['cancellation_received_during'] == 'native_finalization'
    assert audit['primary_process_finished_before_cancellation'] is True
    assert audit['framework_cancellation_count'] == cancel_count
    assert audit['proxy_close_receipt']['closed'] and audit['proxy_cancellation_marker']
    assert context.n_input_tokens == 19 and context.n_output_tokens == 8
    assert state.order == ['cancel', 'status', 'close', 'status']
    assert audit['cancellation_transport_only']
    assert not agent.quota_halt_path.exists()


@pytest.mark.parametrize('close_fails', [False, True])
def test_repeated_cancel_during_revoke_retains_native_audit_and_halts_if_unrevoked(tmp_path, monkeypatch, close_fails):
    agent, environment, context, state = harness(tmp_path, monkeypatch,
        hold_close=True, close_fails=close_fails)
    async def race():
        state.export_started, state.export_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(agent.run('original task', environment, context))
        assert await asyncio.to_thread(state.close_started.wait, 2)
        task.cancel(); await asyncio.sleep(0)
        task.cancel(); await asyncio.sleep(0)
        assert not task.done()
        state.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(race())
    audit = json.loads((agent.logs_dir / 'runtime-audit.json').read_text())
    assert audit['terminal_event'] and audit['actor_models'] == [oc.MODEL]
    assert audit['cancellation_received_during'] == 'credential_revocation'
    assert audit['framework_cancellation_count'] == 2
    assert bool(audit.get('proxy_revocation_error')) is close_fails
    assert agent.quota_halt_path.exists() is close_fails
    if close_fails:
        assert not audit['cancellation_transport_only']
        assert environment.finalize_reason == 'runtime_error'
    else:
        assert audit['proxy_close_receipt']['closed']
        assert audit['cancellation_transport_only']
        assert audit['proxy_cancellation_status']['cancellation_requested']


def test_cancelled_registration_is_reaped_and_revoked_without_launching_actor(tmp_path, monkeypatch):
    agent, environment, context, state = harness(tmp_path, monkeypatch, hold_registration=True)
    async def race():
        task = asyncio.create_task(agent.run('original task', environment, context))
        assert await asyncio.to_thread(state.registration_started.wait, 2)
        task.cancel(); await asyncio.sleep(0)
        state.registration_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(race())
    assert state.processes == []
    assert state.order == ['cancel', 'close']
    audit = json.loads((agent.logs_dir / 'runtime-audit.json').read_text())
    assert audit['cancellation_received_during'] == 'proxy_registration'
    assert audit['proxy_close_receipt']['closed']
    assert not agent.quota_halt_path.exists()


def test_export_timeout_after_late_cancel_stops_exporter_and_still_audits(tmp_path, monkeypatch):
    agent, environment, context, state = harness(tmp_path, monkeypatch, hold_export=True)
    original_wait_for = asyncio.wait_for
    async def short_export_timeout(awaitable, timeout):
        return await original_wait_for(awaitable, .03 if timeout == 30 else timeout)
    monkeypatch.setattr(oc.asyncio, 'wait_for', short_export_timeout)
    def killpg(pid, sig):
        proc = next(p for p in state.processes if p.pid == pid)
        proc.returncode = -int(sig)
        state.export_release.set()
    monkeypatch.setattr(oc.os, 'killpg', killpg)
    async def race():
        state.export_started, state.export_release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(agent.run('original task', environment, context))
        await state.export_started.wait()
        task.cancel(); await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await original_wait_for(task, 2)
    asyncio.run(race())
    assert len(state.processes) == 2 and state.processes[1].returncode < 0
    audit = json.loads((agent.logs_dir / 'runtime-audit.json').read_text())
    assert audit['native_evidence_error'] and audit['proxy_status']['trial_id'] == 'trial-test'
    assert audit['proxy_close_receipt']['closed']
    assert not audit['cancellation_transport_only']
