from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LOCK_OWNER_PREFIX = "agent-team-lock:v1:"


@dataclass(frozen=True)
class LockOwnerInfo:
    hostname: str
    runner: str
    pid: int
    process_start: str | None
    nonce: str


def make_lock_owner(runner: str) -> str:
    payload = {
        "hostname": socket.gethostname(),
        "runner": runner,
        "pid": os.getpid(),
        "process_start": _process_start_token(os.getpid()),
        "nonce": str(uuid.uuid4()),
    }
    return LOCK_OWNER_PREFIX + json.dumps(payload, sort_keys=True, separators=(",", ":"))


def parse_lock_owner(owner: str | None) -> LockOwnerInfo | None:
    if not owner or not owner.startswith(LOCK_OWNER_PREFIX):
        return None
    try:
        payload = json.loads(owner[len(LOCK_OWNER_PREFIX) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return _lock_owner_from_payload(payload)


def is_definitely_dead_same_host_owner(owner: str | None) -> bool:
    info = parse_lock_owner(owner)
    if info is None or info.hostname != socket.gethostname():
        return False
    current_start = _process_start_token(info.pid)
    if current_start is not None and info.process_start is not None:
        return current_start != info.process_start
    return not _pid_exists(info.pid)


def is_live_same_host_owner(owner: str | None) -> bool:
    info = parse_lock_owner(owner)
    if info is None or info.hostname != socket.gethostname():
        return False
    current_start = _process_start_token(info.pid)
    if current_start is not None and info.process_start is not None:
        return current_start == info.process_start
    if info.process_start is None:
        return _pid_exists(info.pid)
    return False


def _lock_owner_from_payload(payload: dict[str, Any]) -> LockOwnerInfo | None:
    hostname = payload.get("hostname")
    runner = payload.get("runner")
    pid = payload.get("pid")
    process_start = payload.get("process_start")
    nonce = payload.get("nonce")
    if not isinstance(hostname, str) or not hostname:
        return None
    if not isinstance(runner, str) or not runner:
        return None
    if not isinstance(pid, int) or pid <= 0:
        return None
    if process_start is not None and not isinstance(process_start, str):
        return None
    if not isinstance(nonce, str) or not nonce:
        return None
    return LockOwnerInfo(hostname, runner, pid, process_start, nonce)


def _process_start_token(pid: int) -> str | None:
    try:
        return str(Path(f"/proc/{pid}").stat().st_ctime_ns)
    except OSError:
        return None


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
