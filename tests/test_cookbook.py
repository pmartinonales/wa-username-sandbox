"""Spec v2 §6.17: every README cookbook use case runs green, driven from its
own curl commands. We extract each `<!-- usecase:N -->` bash block from
README.md and execute it verbatim (bash -e) against a live server — if the
docs rot, these tests fail."""
import asyncio
import re
import socket
from pathlib import Path

import pytest
import uvicorn

from app import webhooks
from app.main import app

README = Path(__file__).resolve().parent.parent / "README.md"
BLOCK_RE = re.compile(r"<!-- usecase:(\d+) -->\n```bash\n(.*?)```", re.DOTALL)


def cookbook_blocks() -> dict[int, str]:
    blocks = {int(n): body for n, body in BLOCK_RE.findall(README.read_text())}
    assert set(blocks) == set(range(1, 15)), f"README must document use cases 1-14, found {sorted(blocks)}"
    return blocks


BLOCKS = cookbook_blocks()


@pytest.fixture
async def live_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    await webhooks.wait_for_pending()
    server.should_exit = True
    await task


@pytest.mark.parametrize("n", sorted(BLOCKS))
async def test_cookbook_usecase(n, live_server, fresh_db):
    proc = await asyncio.create_subprocess_exec(
        "bash", "-e", "-c", BLOCKS[n],
        env={"BASE": live_server, "PATH": "/usr/bin:/bin:/usr/local/bin",
             "WEBHOOK_URL": ""},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
    assert proc.returncode == 0, (
        f"README use case {n} failed (exit {proc.returncode})\n"
        f"--- stdout ---\n{out.decode()}\n--- stderr ---\n{err.decode()}")
    # every block prints at least one OK assertion line
    assert "OK" in out.decode(), out.decode()
