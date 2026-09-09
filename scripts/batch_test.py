#!/usr/bin/env python
"""批量测试：读问题清单 JSONL，逐条发请求，输出结果表 + 汇总。

用法：
  python scripts/batch_test.py --file testdata/batch_questions.jsonl
  python scripts/batch_test.py --file testdata/batch_questions.jsonl --stream

问题行格式：{"id": "q1", "text": "狗拉稀怎么办", "expect_mode": "provisional",
             "image": "testdata/autodl/xxx.png", "conversation_id": "batch-1"}
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx


def _post_one(client, q, api, stream):
    url = f"{api}/api/v1/consult/stream" if stream else f"{api}/api/v1/consult"
    data = {"conversation_id": q.get("conversation_id", "batch")}
    if q.get("text"):
        data["text"] = q["text"]
    files = None
    if q.get("image") and Path(q["image"]).is_file():
        files = {"images": (Path(q["image"]).name, Path(q["image"]).read_bytes(), "image/png")}
    headers = {"X-User-Id": "batch-tester", "X-Tenant-Id": "batch-tenant"}
    t0 = time.perf_counter()
    try:
        if stream:
            tokens = 0
            body = {}
            with client.stream("POST", url, data=data, files=files, headers=headers, timeout=90) as resp:
                for line in resp.iter_lines():
                    if line.startswith("data: {"):
                        payload = json.loads(line[len("data: "):])
                        if payload.get("stage") == "token":
                            tokens += 1
                        elif payload.get("stage") == "final":
                            body = payload.get("response", {})
        else:
            r = client.post(url, data=data, files=files, headers=headers, timeout=90)
            body = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"id": q["id"], "error": str(exc)[:100], "ms": round((time.perf_counter() - t0) * 1000)}
    result = {
        "id": q["id"],
        "ms": round((time.perf_counter() - t0) * 1000),
        "status": body.get("status"),
        "mode": body.get("answer_mode"),
        "followups": len(body.get("follow_up_questions") or []),
        "risk": body.get("risk_level"),
        "ans_len": len(body.get("answer") or ""),
    }
    if stream:
        result["tokens"] = tokens
    expect = q.get("expect_mode", "")
    result["expect"] = expect
    result["ok"] = (not expect) or result["mode"] == expect or (expect == "fast" and result["ms"] < 1000)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="testdata/batch_questions.jsonl")
    parser.add_argument("--api", default="http://127.0.0.1:18100")
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()

    questions = []
    for line in Path(args.file).read_text(encoding="utf-8").splitlines():
        if line.strip():
            questions.append(json.loads(line))
    if not questions:
        print("问题清单为空")
        return 1

    results = []
    with httpx.Client() as client:
        for q in questions:
            r = _post_one(client, q, args.api, args.stream)
            results.append(r)
            mark = "PASS" if r.get("ok") else "FAIL"
            if r.get("error"):
                print(f"[{mark}] {r['id']} | {r['ms']}ms | ERROR: {r['error']}")
            else:
                extra = f" tokens={r['tokens']}" if args.stream else ""
                print(f"[{mark}] {r['id']} | {r['ms']}ms | {r['status']} mode={r['mode']} followups={r['followups']} risk={r['risk']} ans={r['ans_len']}字 expect={r['expect']}{extra}")
            time.sleep(0.3)

    total = len(results)
    passed = sum(1 for r in results if r.get("ok"))
    errored = sum(1 for r in results if r.get("error"))
    modes = {}
    for r in results:
        if r.get("mode"):
            modes[r["mode"]] = modes.get(r["mode"], 0) + 1
    print("")
    print(f"=== 汇总：{total} 条 | 符合预期 {passed} | 错误 {errored} | 模式分布 {modes} ===")
    return 0 if errored == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
