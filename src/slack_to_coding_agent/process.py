from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import BackendConfig, CONFIG_DIR

LOGGER = logging.getLogger(__name__)


class ManagedBackendProcess:
    def __init__(self, process: subprocess.Popen[bytes], log_file: Path):
        self.process = process
        self.log_file = log_file
        atexit.register(self.stop)

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        LOGGER.info("Stopping managed backend process pid=%s", self.process.pid)
        _send_signal(self.process, signal.SIGTERM)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            LOGGER.warning("Backend process did not stop gracefully; killing pid=%s", self.process.pid)
            _send_signal(self.process, signal.SIGKILL)


def ensure_backend_started(config: BackendConfig) -> ManagedBackendProcess | None:
    """Start the configured backend process if needed.

    Returns a process handle only when this invocation started the process. Existing healthy
    backends are left alone and return None.
    """

    if not config.start_command:
        return None

    if _is_healthy(config):
        LOGGER.info("Backend %s is already healthy", config.name)
        return None

    log_file = _log_file(config)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_file.open("ab")

    env = os.environ.copy()
    env.update(config.startup_env)
    cwd = Path(config.startup_cwd).expanduser() if config.startup_cwd else None

    LOGGER.info("Starting backend %s: %s", config.name, config.start_command)
    popen_kwargs: dict[str, object] = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(
        config.start_command,
        cwd=str(cwd) if cwd else None,
        env=env,
        shell=True,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        **popen_kwargs,
    )
    log_handle.close()

    deadline = time.monotonic() + config.startup_timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Backend {config.name!r} exited during startup with code {process.returncode}. "
                f"See log: {log_file}"
            )
        if _is_healthy(config):
            LOGGER.info("Backend %s is healthy (pid=%s, log=%s)", config.name, process.pid, log_file)
            return ManagedBackendProcess(process, log_file)
        time.sleep(0.5)

    process.terminate()
    raise TimeoutError(
        f"Backend {config.name!r} did not become healthy within "
        f"{config.startup_timeout_seconds:g}s. See log: {log_file}"
    )


def _is_healthy(config: BackendConfig) -> bool:
    if config.health_url:
        try:
            response = httpx.get(config.health_url, timeout=2)
            return 200 <= response.status_code < 300
        except Exception:
            return False

    parsed = urlparse(config.base_url)
    if parsed.scheme in {"http", "https"}:
        try:
            response = httpx.get(config.base_url, timeout=2)
            return response.status_code < 500
        except Exception:
            return False
    return False


def _log_file(config: BackendConfig) -> Path:
    if config.startup_log_file:
        return Path(config.startup_log_file).expanduser()
    return CONFIG_DIR / f"{config.name}-backend.log"


def _send_signal(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, sig)
            return
        except ProcessLookupError:
            return
    if sig == signal.SIGTERM:
        process.terminate()
    else:
        process.kill()
