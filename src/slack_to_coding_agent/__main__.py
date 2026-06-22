from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .config import CONFIG_FILE, ensure_config_file, load_config
from .slack_app import run


def main() -> None:
    parser = argparse.ArgumentParser(description="Slack bridge to a local coding-agent backend")
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_FILE,
        help=f"Config file path (default: {CONFIG_FILE})",
    )
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="Create the default config file and exit",
    )
    parser.add_argument("--log-level", default="INFO", help="Python logging level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.init_config:
        ensure_config_file(args.config)
        print(f"Config file ready: {args.config}")
        return

    config = load_config(args.config)
    run(config)


if __name__ == "__main__":
    main()
