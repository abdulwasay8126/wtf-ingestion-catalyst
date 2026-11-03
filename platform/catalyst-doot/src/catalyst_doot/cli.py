"""Local entry point; publication reads do not start collection."""

import argparse

import uvicorn

from catalyst_doot.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Catalyst Doot earnings dashboard")
    parser.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
