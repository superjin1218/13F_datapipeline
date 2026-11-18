"""Launch the local dashboard without accepting connections from the network."""

import argparse

import uvicorn


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Drakos 13F 데이터 파이프라인')
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    uvicorn.run('app:app', host='127.0.0.1', port=args.port, proxy_headers=False)
