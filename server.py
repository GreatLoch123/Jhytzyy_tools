from __future__ import annotations

import argparse
import logging
import sys

from app.collector import DiscoveryScheduler
from app.config import COLLECTION_LOG_PATH, LOG_DIR
from app.db import init_db, sync_auto_port_filters, sync_switches_from_workbook
from app.http_server import create_server


def main() -> int:
    parser = argparse.ArgumentParser(description="医院网络综合管理台后端服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=COLLECTION_LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )
    init_db()
    sync_switches_from_workbook()
    sync_auto_port_filters()

    scheduler = DiscoveryScheduler()
    scheduler.start()

    server = create_server(args.host, args.port)
    print(f"医院网络综合管理台已启动: http://{args.host}:{args.port}")
    print("自动发现：默认关闭，可在采集设置中启用并设置间隔")
    print("功能页面: 拓扑 / 网址导航 / IP定位 / 远程检测 / 采集设置")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        scheduler.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
