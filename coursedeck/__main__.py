import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Run CourseDeck on this device")
    parser.add_argument("--port", type=int, default=48321)
    args = parser.parse_args()
    uvicorn.run(
        "coursedeck.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
