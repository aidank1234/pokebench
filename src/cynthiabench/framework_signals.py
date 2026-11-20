"""Read authoritative Harbor lifecycle outcome through its public hook API."""
from contextvars import ContextVar
import json

_trial_result = ContextVar('cynthia_harbor_trial_result', default=None)


async def bind_trial_result(event):
    # Each Harbor trial has its own asyncio task/context. The referenced result
    # is updated by Harbor when wait_for becomes AgentTimeoutError or when the
    # operator interrupts the task. No private Job/queue attributes are used.
    _trial_result.set(event.result)


def install_framework_signals(job, launch_log=None):
    async def on_start(event):
        await bind_trial_result(event)
        if launch_log is not None:
            launch_log.parent.mkdir(parents=True, exist_ok=True)
            with launch_log.open('a') as file:
                file.write(json.dumps({'trial_name': event.trial_name, 'case': event.task_name,
                    'model': event.config.agent.model_name, 'event': 'trial-start',
                    'timestamp': event.timestamp.isoformat()}) + '\n')
    job.on_trial_started(on_start)


def cancellation_classification():
    result = _trial_result.get()
    error = getattr(result, 'exception_info', None)
    kind = getattr(error, 'exception_type', None)
    return ('timeout' if kind == 'AgentTimeoutError' else 'runtime_error'), kind
