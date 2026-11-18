"""Launch the local dashboard without accepting connections from the network."""

import argparse
import asyncio
from contextlib import suppress

import uvicorn


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Drakos 13F 데이터 파이프라인')
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    config = uvicorn.Config('app:app', host='127.0.0.1', port=args.port, proxy_headers=False)
    # asyncio's Windows default supports the pipes used by the collector.
    with suppress(KeyboardInterrupt):
        asyncio.run(uvicorn.Server(config).serve())
