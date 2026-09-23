import argparse
import os
import sys

import uvicorn

from .runtime import StartupError, data_directory, instance_lock, listening_socket


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run CourseDeck on this device")
    parser.add_argument("--port", type=int, default=48321)
    parser.add_argument(
        "--data-dir", help="Local data folder (defaults to the project's data folder)"
    )
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    directory = data_directory(args.data_dir)
    try:
        with instance_lock(directory), listening_socket(args.port) as sock:
            os.environ["COURSEDECK_DATA_DIR"] = str(directory)
            config = uvicorn.Config(
                "coursedeck.app:create_app",
                factory=True,
                host="127.0.0.1",
                port=args.port,
                access_log=False,
            )
            uvicorn.Server(config).run(sockets=[sock])
    except (StartupError, OSError) as exc:
        print(f"Cannot start CourseDeck: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
