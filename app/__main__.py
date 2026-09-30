"""命令行入口：python -m app [--host H] [--port P] [--db PATH] [--reseed] [--scenario S]"""
from __future__ import annotations

import argparse

from .server import make_server


def main() -> None:
    ap = argparse.ArgumentParser(description="社区地下车库防汛挡板部署与归位系统")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/garrison.db")
    ap.add_argument("--reseed", action="store_true", help="重置并载入演示数据（部署/缺件/提前解除样例）")
    ap.add_argument("--scenario", default="yellow",
                    choices=["storm", "yellow", "recede"], help="模拟天气脚本")
    ap.add_argument("--interval", type=float, default=15.0, help="后台复查间隔秒")
    args = ap.parse_args()

    httpd = make_server(args.host, args.port, args.db, reseed=args.reseed,
                        scenario=args.scenario, monitor_interval=args.interval)
    print("=" * 60, flush=True)
    print("  社区地下车库防汛挡板部署与归位系统", flush=True)
    print(f"  访问地址: http://localhost:{args.port}/", flush=True)
    print(f"  数据库:   {args.db}    天气脚本: {args.scenario}（{args.interval}s 复查）", flush=True)
    print("=" * 60, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.app.monitor.stop()
        print("\n已停止")


if __name__ == "__main__":
    main()
