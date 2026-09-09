#!/usr/bin/env python
"""shadow vs grounded 对比报告（生成 markdown）。"""
import argparse
import json
from pathlib import Path


def _load(path):
    return {r["id"]: r for r in json.loads(Path(path).read_text(encoding="utf-8"))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--a", required=True, help="shadow 结果")
    parser.add_argument("--b", required=True, help="grounded 结果")
    parser.add_argument("--out", default="suite_compare_report.md")
    args = parser.parse_args()

    a = _load(args.a)
    b = _load(args.b)
    lines = []
    lines.append("# shadow vs grounded 对比报告")
    lines.append("")
    lines.append(f"- 对比：{len(a)} 条（shadow） vs {len(b)} 条（grounded）")
    lines.append("")
    for qid in a:
        ra, rb = a[qid], b.get(qid, {})
        lines.append(f"## {qid}（{ra.get('conversation_id')}）")
        lines.append("")
        lines.append("| | shadow | grounded |")
        lines.append("|---|---|---|")
        lines.append(f"| 耗时 | {ra.get('ms')}ms | {rb.get('ms')}ms |")
        lines.append(f"| 状态/模式 | {ra.get('status')}/{ra.get('mode')} | {rb.get('status')}/{rb.get('mode')} |")
        lines.append(f"| 风险 | {ra.get('risk')} | {rb.get('risk')} |")
        lines.append(f"| 追问 | {'；'.join(ra.get('followups') or [])[:120] or '无'} | {'；'.join(rb.get('followups') or [])[:120] or '无'} |")
        lines.append("")
        lines.append("**shadow 回答：**")
        lines.append("")
        lines.append(f"> {ra.get('answer') or ra.get('error') or '(空)'}")
        lines.append("")
        lines.append("**grounded 回答：**")
        lines.append("")
        lines.append(f"> {rb.get('answer') or rb.get('error') or '(空)'}")
        lines.append("")
        lines.append("---")
        lines.append("")
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已生成：{args.out}")


if __name__ == "__main__":
    raise SystemExit(main())
