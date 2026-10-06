"""Safe asynchronous subprocess execution shared by model adapters."""
from __future__ import annotations

import asyncio
import os
import signal


async def run_process(command: list[str], cwd, timeout: int | float, env=None):
    """Run a process without a shell and always reap its process group."""
    options = {"start_new_session": True} if os.name != "nt" else {}
    process = await asyncio.create_subprocess_exec(
        *command, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **options,
    )

    async def terminate() -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM) if os.name != "nt" else process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL) if os.name != "nt" else process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        await terminate()
        raise
    return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
