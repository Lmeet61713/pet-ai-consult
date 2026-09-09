#!/usr/bin/env python
"""全功能跑测脚本：shadow/grounded 两轮对比 + 全功能覆盖。

用法：
  python scripts/run_full_suite.py --mode shadow --out /tmp/suite_shadow.jsonl
  python scripts/run_full_suite.py --mode grounded --out /tmp/suite_grounded.jsonl
  python scripts/compare_suite.py --a /tmp/suite_shadow.jsonl --b /tmp/suite_grounded.jsonl
"""
import argparse
import json
import time
from pathlib import Path

import httpx


def run_question(client, q, api):
    url = f"{api}/api/v1/consult"
    data = {"conversation_id": q["conversation_id"]}
    if q.get("text"):
        data["text"] = q["text"]
    files = None
    if q.get("image") and Path(q["image"]).is_file():
        files = {"images": (Path(q["image"]).name, Path(q["image"]).read_bytes(), "image/png")}
    headers = {"X-User-Id": "suite-tester", "X-Tenant-Id": "suite-tenant"}
    t0 = time.perf_counter()
    try:
        r = client.post(url, data=data, files=files, headers=headers, timeout=90)
        body = r.json()
        return {
            "id": q["id"],
            "conversation_id": q["conversation_id"],
            "ms": round((time.perf_counter() - t0) * 1000),
            "status": body.get("status"),
            "mode": body.get("answer_mode"),
            "risk": body.get("risk_level"),
            "followups": body.get("follow_up_questions") or [],
            "answer": body.get("answer") or "",
            "error": body.get("error"),
        }
    except Exception as exc:
        return {"id": q["id"], "error": str(exc)[:120], "ms": round((time.perf_counter() - t0) * 1000)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="shadow")
    parser.add_argument("--file", default="testdata/full_suite_questions.jsonl")
    parser.add_argument("--out", default="/tmp/suite_results.jsonl")
    parser.add_argument("--api", default="http://127.0.0.1:18100")
    args = parser.parse_args()

    questions = [
        json.loads(line)
        for line in Path(args.file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results = []
    with httpx.Client() as client:
        for i, q in enumerate(questions):
            r = run_question(client, q, args.api)
            r["mode_tag"] = args.mode
            results.append(r)
            status = r.get("status", "?")
            mode = r.get("mode", "-")
            print(f"[{i+1}/{len(questions)}] {r['id']} | {r.get('ms')}ms | {status} {mode} | ans={len(r.get('answer') or '')}字" + (f" | ERR {r.get('error')}" if r.get("error") else ""))
            time.sleep(0.3)
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成：{len(results)} 条 → {args.out}")


if __name__ == "__main__":
    raise SystemExit(main())
