"""Stop a Harbor job on a persistent provider halt without touching its queue."""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
from pathlib import Path


class ProviderQuotaHalt(RuntimeError):
    pass


async def run_with_quota_halt(job, halt_path, record_path, poll_seconds=0.1):
    halt_path, record_path = Path(halt_path), Path(record_path)
    if halt_path.exists() or halt_path.is_symlink():
        raise ProviderQuotaHalt('Provider halt exists before scheduling')
    owner = asyncio.current_task()

    async def watch():
        while not (halt_path.exists() or halt_path.is_symlink()):
            await asyncio.sleep(poll_seconds)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps({
            'reason': 'provider_quota_halt', 'noticed_at': datetime.now(timezone.utc).isoformat(),
            'halt_path': str(halt_path),
            'action': 'Cancelled the owning public asyncio task; native/Harbor cleanup retains terminal games.',
        }, indent=2) + '\n')
        owner.cancel('provider_quota_halt')

    watcher = asyncio.create_task(watch())
    try:
        return await job.run()
    except asyncio.CancelledError:
        if halt_path.exists() or halt_path.is_symlink():
            raise ProviderQuotaHalt('Provider quota halted new scheduling; preserve all attempt evidence') from None
        raise
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
