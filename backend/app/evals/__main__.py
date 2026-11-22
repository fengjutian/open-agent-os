"""命令行入口。

    python -m app.evals --suite evals/baseline.json
    python -m app.evals --suite evals/baseline.json --suite evals/tools.json --json report.json

退出码：全部通过 0，有失败 1。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.evals", description="运行评测套件")
    parser.add_argument(
        "--suite",
        required=True,
        action="append",
        metavar="PATH",
        help="套件 JSON 路径，可重复传入多个",
    )
    parser.add_argument("--json", dest="json_path", help="把报告写成 JSON 文件")
    parser.add_argument("--quiet", action="store_true", help="只输出结论")
    args = parser.parse_args(argv)

    from fastapi.testclient import TestClient

    from app.db.session import SessionLocal
    from app.evals.runner import load_suite, run_suite
    from app.main import app
    from app.services.api_keys import ApiKeyService

    suites = [load_suite(path) for path in args.suite]

    reports = []
    with TestClient(app) as client:
        # 评测需要 admin 权限：临时发一把，跑完不删也无所谓（名字带随机后缀）
        with SessionLocal() as db:
            _, raw = ApiKeyService(db).create(name=f"eval-{uuid4().hex[:8]}", role="admin")
        client.headers["Authorization"] = f"Bearer {raw}"

        for suite in suites:
            report = run_suite(client, suite)
            reports.append(report)
            if args.quiet:
                continue
            print(
                f"\n[{report.suite}] {report.passed}/{len(report.results)} 通过，"
                f"耗时 {report.duration_ms:.0f}ms"
            )
            for result in report.results:
                print(f"  {'PASS' if result.passed else 'FAIL'}  {result.name}  ({result.duration_ms:.0f}ms)")
                for failure in result.failures:
                    print(f"        - {failure}")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps([report.to_dict() for report in reports], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if not args.quiet:
            print(f"\n报告已写入 {args.json_path}")

    failed = sum(report.failed for report in reports)
    if args.quiet:
        total = sum(len(report.results) for report in reports)
        print(f"{total - failed}/{total} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
