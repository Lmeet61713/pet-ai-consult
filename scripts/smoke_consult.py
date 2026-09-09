"""问诊链路冒烟（v6.3 §24.3 / V1.1 P0-5：v6.3 四态 + 三回答模式）

用例：
1. 图+描述 → success + normal/provisional，取决于图片是否足以支持用户描述
2. 急症（呼吸困难）→ success + urgent_guidance，就医建议紧急程度 emergency
3. 追问（有图无文字）→ success + provisional，带追问

用法：python scripts/smoke_consult.py [--base http://127.0.0.1:18100] [--image pet.jpg] [--token xxx]
需要：API 已启动（mock 模式或全真实环境均可）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

import httpx


async def post(base: str, text: str, image: bytes | None, token: str | None) -> dict:
    # 每个场景使用独立会话，避免上一场景历史改变完整度与风险判断。
    form = {"conversation_id": f"smoke_{uuid.uuid4().hex[:12]}", "text": text}
    files = [("images", ("pet.jpg", image, "image/jpeg"))] if image else None
    headers = {"X-User-Id": "smoke_test", "X-Tenant-Id": "smoke_tenant"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # trust_env=False：本地直连不走系统代理（Windows 开发机代理会把 127.0.0.1 请求转给代理）
    async with httpx.AsyncClient(base_url=base, timeout=60, trust_env=False) as c:
        r = await c.post("/api/v1/consult", data=form, files=files, headers=headers)
        r.raise_for_status()
        return r.json()


def response_problems(data: dict, expected_modes: tuple[str, ...]) -> list[str]:
    problems: list[str] = []
    actual_mode = data.get("answer_mode")
    if data.get("status") != "success":
        problems.append(f"status={data.get('status')} 期望 success")
    if actual_mode not in expected_modes:
        problems.append(f"answer_mode={actual_mode} 期望 {'/'.join(expected_modes)}")
    if data.get("status") != "success":
        return problems
    if not data.get("answer"):
        problems.append("answer 为空")
    if not data.get("disclaimer"):
        problems.append("disclaimer 为空")
    if data.get("risk_level") is None:
        problems.append("risk_level 为空")
    if actual_mode == "urgent_guidance":
        vet = data.get("vet_recommendation") or {}
        if not (vet.get("recommended") and vet.get("urgency") in ("urgent", "emergency")):
            problems.append(f"急症就医建议缺失: {vet}")
    if actual_mode == "provisional" and not data.get("follow_up_questions"):
        problems.append("provisional 缺少追问")
    return problems


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18100")
    parser.add_argument("--image", default=None)
    parser.add_argument("--token", default=None, help="生产环境 JWT（可选）")
    args = parser.parse_args()

    image = open(args.image, "rb").read() if args.image else None

    cases = [
        (
            "正常: 图+描述",
            {"text": "眼睛分泌物多，三天了", "image": image},
            ("normal", "provisional"),
        ),
        (
            "急症: 呼吸困难",
            {"text": "它呼吸困难喘不过气", "image": None},
            ("urgent_guidance",),
        ),
        (
            "追问: 只有图没文字",
            {"text": "", "image": image},
            ("provisional",),
        ),
    ]
    failed = 0
    for name, kw, expected_modes in cases:
        if name.startswith("追问") and not image:
            print(f"SKIP {name}（无图片参数）")
            continue
        data = await post(args.base, kw["text"], kw["image"], args.token)
        problems = response_problems(data, expected_modes)
        ok = not problems
        print(f"{'PASS' if ok else 'FAIL'} {name}: status={data.get('status')} mode={data.get('answer_mode')}")
        for p in problems:
            print(f"    - {p}")
        if not ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
