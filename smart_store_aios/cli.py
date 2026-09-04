from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .config import Settings
from .db import StoreDB
from .profit import UnitEconomics, weekly_orders_required
from .worker import Worker


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="store-aios")
    root.add_argument("--config", default="config.json")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("kind")
    enqueue.add_argument("payload", help="JSON object")
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    commands.add_parser("status")
    economics = commands.add_parser("economics")
    economics.add_argument("--price", type=int, required=True)
    economics.add_argument("--cost", type=int, required=True)
    economics.add_argument("--shipping", type=int, default=0)
    return root


def main() -> None:
    args = parser().parse_args()
    config_path = Path(args.config)
    if args.command == "init" and not config_path.exists():
        shutil.copyfile("config.example.json", config_path)
    settings = Settings.load(config_path)
    db = StoreDB(settings.database)
    db.initialize()
    if args.command == "init":
        print(json.dumps({"database": str(settings.database), "dry_run": settings.dry_run}, ensure_ascii=False))
    elif args.command == "enqueue":
        print(db.enqueue(args.kind, json.loads(args.payload)))
    elif args.command == "worker":
        worker = Worker(settings)
        if args.once:
            print(json.dumps({"processed": worker.run_once()}))
        else:
            while worker.run_once():
                pass
    elif args.command == "status":
        print(json.dumps(db.stats(), ensure_ascii=False))
    elif args.command == "economics":
        unit = UnitEconomics(args.price, args.cost, args.shipping)
        contribution = unit.contribution(settings.profit)
        print(json.dumps({
            "contribution_per_order_krw": contribution,
            "contribution_margin_rate": unit.margin_rate(settings.profit),
            "weekly_orders_required": weekly_orders_required(settings.profit, contribution),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()

