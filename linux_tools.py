"""Linux-only helpers: one-shot sudo commands and runtime dependency install.

The sudo password is never stored on disk. Callers receive the plaintext
password from the encrypted (RSA) web request, pass it into these helpers,
and drop it as soon as the command returns.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .utils import IS_LINUX, LINUX_RUNTIME_DEPS


_BIN_DIRS = (
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/local/sbin",
    "/usr/sbin",
    "/sbin",
    "/snap/bin",
)

# Binaries that prove the headless display / runtime stack is present.
_DEP_BINARIES = ("Xvfb", "x11vnc", "openbox")


def _shorten(text: str, limit: int = 160) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def have_binary(name: str) -> bool:
    """Check for a binary even when the service PATH is minimal."""
    if shutil.which(name):
        return True
    for base in _BIN_DIRS:
        candidate = Path(base) / name
        try:
            if candidate.is_file() and os.access(str(candidate), os.X_OK):
                return True
        except OSError:
            continue
    return False


def sudo_available() -> bool:
    return have_binary("sudo")


def run_sudo_sync(
    plugin: Any, args: List[str], password: str, timeout: float = 180.0
) -> Tuple[int, str]:
    """Run a sudo command with the password piped to stdin. Synchronous."""
    if not IS_LINUX:
        return 1, "该操作仅支持 Linux"
    if not sudo_available():
        return 1, "系统没有 sudo"
    if not password:
        return 1, "未提供 sudo 密码"
    try:
        proc = subprocess.run(
            ["sudo", "-S", "-p", "", *args],
            input=(password + "\n").encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 1, "sudo 命令超时"
    except Exception as exc:
        return 1, f"启动 sudo 失败: {_shorten(exc)}"
    text = (proc.stdout or b"").decode("utf-8", errors="ignore").strip()
    return int(proc.returncode or 0), text or "(无输出)"


async def run_sudo(
    plugin: Any, args: List[str], password: str, timeout: float = 180.0
) -> Tuple[int, str]:
    """Asynchronous wrapper around the synchronous sudo runner."""
    return await asyncio.to_thread(run_sudo_sync, plugin, args, password, timeout)


def missing_linux_deps() -> List[str]:
    """Return missing headless runtime binaries."""
    missing: List[str] = []
    for name in _DEP_BINARIES:
        if not have_binary(name):
            missing.append(name)
    return missing


def install_linux_deps_sync(plugin: Any, password: str = "") -> Dict[str, Any]:
    """Install the full Debian/Ubuntu runtime package list. Synchronous."""
    if not IS_LINUX:
        return {"ok": False, "message": "该操作仅支持 Linux"}
    if not password:
        return {"ok": False, "need_sudo": True, "message": "该操作需要 sudo 密码"}
    apt = shutil.which("apt-get") or (
        "/usr/bin/apt-get" if have_binary("apt-get") else None
    )
    if not apt:
        return {"ok": False, "message": "未找到 apt-get（仅支持 Debian / Ubuntu）"}

    packages = list(LINUX_RUNTIME_DEPS) if not isinstance(LINUX_RUNTIME_DEPS, str) else LINUX_RUNTIME_DEPS.split()
    steps: List[Dict[str, Any]] = []

    code, output = run_sudo_sync(plugin, ["apt-get", "update"], password, timeout=300)
    steps.append({"command": "apt-get update", "ok": code == 0, "output": output})

    # apt-get update may fail on a stale mirror while install still works.
    code, output = run_sudo_sync(
        plugin,
        ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *packages],
        password,
        timeout=900,
    )
    steps.append(
        {"command": "apt-get install -y " + " ".join(packages), "ok": code == 0, "output": output}
    )

    missing = missing_linux_deps()
    if missing:
        steps.append(
            {"command": "检查依赖", "ok": False, "output": "缺少: " + ", ".join(missing)}
        )

    ok = code == 0 and not missing
    return {
        "ok": ok,
        "need_sudo": False,
        "message": "Linux 依赖安装完成" if ok else "依赖安装失败或缺组件，请查看日志",
        "steps": steps,
    }


async def install_linux_deps(plugin: Any, password: str = "") -> Dict[str, Any]:
    return await asyncio.to_thread(install_linux_deps_sync, plugin, password)
