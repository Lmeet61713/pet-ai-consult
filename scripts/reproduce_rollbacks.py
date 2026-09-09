# -*- coding: utf-8 -*-
"""复现固定模板回落题目，抓取 medical_review violations 明细。"""
import json
import time
from pathlib import Path

import httpx

QUESTIONS = [
    ("MVP-NUT-002", "我家狗是不是太胖了？怎么判断？"),
    ("MVP-NUT-009", "怎么记录喝水"),
    ("MVP-ENDO-001", "老猫很能吃却掉体重"),
    ("MVP-ENDO-008", "糖尿病宠物吐了没精神"),
    ("MVP-INF-008", "看起来健康会有猫白血病吗"),
    ("MVP-INF-010", "狗钩体会传人吗"),
    ("MVP-EYE-008", "眼睛好点能停药吗"),
    ("MVP-EAR-005", "狗狗耳朵肿了是耳血肿吗，能自己消吗"),
    ("MVP-EAR-007", "猫耳朵边缘有痂"),
    ("MVP-TOX-015", "猫舔到浓缩消毒剂"),
    ("MVP-DX-001", "宠物血检有箭头就是生病吗"),
    ("V17-DOG-GI-001", "犬便秘怎么办"),
    ("V17-DOG-END-001", "犬甲状腺功能减退"),
    ("V17-DOG-PAR-001", "心丝虫怎么查出来"),
    ("V17-DOG-HEP-001", "犬肝病日常观察怎么办"),
    ("V17-DOG-ORAL-002", "怎么给狗刷牙才正确"),
    ("V17-CAT-RES-001", "猫哮喘怎么办"),
    ("V17-CAT-CAR-001", "猫肥厚性心肌病怎么办"),
    ("V17-CAT-DERM-001", "猫跳蚤过敏性皮炎怎么办"),
    ("V17-CAT-REP-002", "猫乳腺肿块怎么办"),
    ("V17-CAT-VAX-001", "猫打完疫苗有点蔫正常吗"),
    ("V17-CAT-PED-001", "小猫疫苗要打几针？"),
    ("V17-CAT-ORAL-002", "猫牙吸收与口腔疼痛怎么办"),
    ("V17-CAT-END-001", "猫咪最近变瘦了正常吗"),
]

LOG = Path("/root/autodl-tmp/projects/pet-consult-v17/runtime/api.log")

def tail_log():
    return LOG.read_text(encoding="utf-8", errors="replace") if LOG.is_file() else ""

def extract_review(rid, log_text):
    """从日志提取该 request_id 的 medical_review 与 generate 行。"""
    lines = []
    for line in log_text.splitlines():
        if rid in line and ("medical_review" in line or "safety_rewrite" in line or "generate" in line):
            try:
                obj = json.loads(line)
                lines.append(obj)
            except Exception:
                continue
    return lines

def main():
    out = []
    def p(s=""):
        out.append(str(s))

    with httpx.Client(timeout=90) as client:
        for cid, text in QUESTIONS:
            try:
                r = client.post(
                    "http://127.0.0.1:18100/api/v1/consult",
                    data={"conversation_id": f"rollback-{cid}", "text": text},
                    headers={"X-User-Id": "rollback", "X-Tenant-Id": "rollback"},
                )
                body = r.json()
                rid = body.get("request_id", "")
                answer = body.get("answer") or ""
                is_template = "规则评估未发现" in answer and len(answer) < 250
                p(f"### {cid} | {text}")
                p(f"status={body.get('status')} mode={body.get('answer_mode')} ans={len(answer)}字 模板回落={is_template}")
                if not is_template:
                    p(f"回答预览: {answer[:120]}")
                # 从日志提取
                events = extract_review(rid, tail_log())
                for ev in events:
                    st = ev.get("stage", "?")
                    if st == "medical_review":
                        p(f"  [medical_review] passed={ev.get('passed')} violations={ev.get('violations')} ms={ev.get('ms')}")
                    elif st == "safety_rewrite":
                        p(f"  [safety_rewrite] error={ev.get('error')}")
                    elif st == "generate":
                        p(f"  [generate] mode={ev.get('mode')} ms={ev.get('ms')}")
                p("")
            except Exception as exc:
                p(f"### {cid} | ERROR: {exc}")
                p("")
            time.sleep(0.2)

    result = "\n".join(out)
    Path("/tmp/rollback_analysis.txt").write_text(result, encoding="utf-8")
    print(f"done, {len(QUESTIONS)} questions analyzed")

if __name__ == "__main__":
    main()
