from __future__ import annotations

import asyncio
import logging
import sys

from src.cli import build_parser, run_cli


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Logging setup temprano para 'run', lazy para el resto
    level = "INFO"
    if args.command == "run":
        import os
        from dotenv import load_dotenv
        load_dotenv()
        level = os.environ.get("POLLER_LOG_LEVEL", "INFO")

    setup_logging(level)
    asyncio.run(run_cli(args))


if __name__ == "__main__":
    main()
