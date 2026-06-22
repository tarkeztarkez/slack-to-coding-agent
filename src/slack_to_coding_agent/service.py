from __future__ import annotations

import os
import platform
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path


SERVICE_NAME = "slack-to-coding-agent"
LAUNCHD_LABEL = "com.slack-to-coding-agent"


def _module_command(config_path: Path, log_level: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "slack_to_coding_agent",
        "--config",
        str(config_path.expanduser()),
        "--log-level",
        log_level,
    ]


def install_service(config_path: Path, log_level: str) -> Path:
    system = platform.system().lower()
    if system == "linux":
        return _install_systemd_user_service(config_path, log_level)
    if system == "darwin":
        return _install_launchd_service(config_path, log_level)
    raise RuntimeError(
        f"Unsupported platform for --install-service: {platform.system()}. "
        "Linux systemd user services and macOS LaunchAgents are supported."
    )


def _install_systemd_user_service(config_path: Path, log_level: str) -> Path:
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_path = service_dir / f"{SERVICE_NAME}.service"
    log_dir = Path.home() / ".slack-to-coding-agent"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "service.log"
    command = " ".join(shlex.quote(part) for part in _module_command(config_path, log_level))

    service_path.write_text(
        "\n".join(
            [
                "[Unit]",
                "Description=Slack to Coding Agent",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                f"WorkingDirectory={Path.cwd()}",
                f"ExecStart={command}",
                "Restart=always",
                "RestartSec=5",
                f"StandardOutput=append:{log_path}",
                f"StandardError=append:{log_path}",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        ),
        encoding="utf-8",
    )

    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", service_path.name])
    return service_path


def _install_launchd_service(config_path: Path, log_level: str) -> Path:
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plist_path = agents_dir / f"{LAUNCHD_LABEL}.plist"
    log_dir = Path.home() / ".slack-to-coding-agent"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / "service.out.log"
    stderr_path = log_dir / "service.err.log"

    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": _module_command(config_path, log_level),
        "WorkingDirectory": str(Path.cwd()),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "")},
    }
    with plist_path.open("wb") as fh:
        plistlib.dump(plist, fh)

    domain = f"gui/{os.getuid()}"
    # Ignore bootout failures: the service may not be loaded yet.
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False)
    _run(["launchctl", "bootstrap", domain, str(plist_path)])
    _run(["launchctl", "enable", f"{domain}/{LAUNCHD_LABEL}"])
    _run(["launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}"])
    return plist_path


def _run(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        rendered = " ".join(shlex.quote(part) for part in command)
        raise RuntimeError(f"Command failed: {rendered}") from exc
