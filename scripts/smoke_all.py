#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一服务器端到端冒烟入口（在容器内运行，命中真实 PG/服务/出网）。"""
from __future__ import annotations

import argparse
import importlib


CHECKS = [
    ("ofd_synthetic", "scripts.smoke_ofd_live", "run", {}),
    ("cloud", "scripts.smoke_cloud_live", "run", {}),
    ("editor", "scripts.smoke_editor_e2e", "run", {}),
    ("pg_rls", "scripts.smoke_pg_rls", "run", {}),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8123")
    ap.add_argument("--env", default="/data/bidvolt/.env")
    ap.add_argument("--skip", default="")
    args = ap.parse_args()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    results: list[tuple[str, bool, str]] = []
    for name, module, func, extra in CHECKS:
        if name in skip:
            continue
        kwargs = {"base": args.base}
        if name == "pg_rls":
            kwargs["env_path"] = args.env
        try:
            getattr(importlib.import_module(module), func)(**kwargs)
            results.append((name, True, ""))
            print(f"[PASS] {name}")
        except Exception as exc:  # noqa: BLE001
            results.append((name, False, str(exc)))
            print(f"[FAIL] {name}: {exc}")

    print("== SUMMARY ==")
    for name, ok, err in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({err})" if err else ""))
    if any(not ok for _, ok, _ in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
