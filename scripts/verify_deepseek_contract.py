"""DeepSeek 官方 API 契约验证。

检查鉴权、结构化问诊输出、医疗安全字段和 thinking 配置。
`--confirm` 在全部通过后写入生产启动所需的契约标记。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.schemas.consult import KnowledgeConsultRequest

# Direct execution sets sys.path[0] to scripts/, so make the documented
# `python scripts/verify_deepseek_contract.py` command resolve the app package.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _request(question: str) -> KnowledgeConsultRequest:
    from app.schemas.consult import KnowledgeConsultRequest

    return KnowledgeConsultRequest(
        user_question=question,
        pet_info={"species": "猫", "age_months": 24},
        image_summary={"observations": [], "red_flags": [], "limitations": []},
        conversation_summary="",
        recent_turns=[],
        risk_context={"risk_level": "low", "matched_rules": []},
        missing_information=[],
        answer_mode="normal",
    )


async def run_checks(settings: Settings, question: str) -> int:
    from app.clients.knowledge_consult_client import build_knowledge_consult_adapter
    from app.core.deadline import Deadline

    checks: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" - {detail}" if detail else ""))

    configured = bool(
        settings.knowledge_provider == "deepseek_official"
        and settings.knowledge_api_base_url
        and settings.knowledge_api_key
        and settings.knowledge_model
    )
    record("1. DeepSeek 官方 API 配置", configured)
    if not configured:
        return _summary(checks)

    adapter = build_knowledge_consult_adapter(settings)
    try:
        result = await adapter.generate_consultation(
            request=_request(question),
            deadline=Deadline.after_seconds(settings.knowledge_timeout),
            request_id="deepseek-contract-check",
        )
        record("2. 真实鉴权和调用成功", True)
        structured = bool(result.summary and result.disclaimer and result.vet_recommendation)
        record("3. 结构化 JSON 可解析", structured)
        safe_shape = bool(result.what_to_do_now is not None and result.avoid_actions is not None)
        record("4. 医疗安全字段完整", safe_shape)
    except Exception as exc:  # noqa: BLE001
        record("2. 真实鉴权和调用成功", False, str(exc))
        record("3. 结构化 JSON 可解析", False, "未执行")
        record("4. 医疗安全字段完整", False, "未执行")
    finally:
        await adapter.close()

    record(
        "5. thinking 模式固定",
        settings.knowledge_thinking_mode in ("disabled", "enabled"),
        f"KNOWLEDGE_THINKING_MODE={settings.knowledge_thinking_mode}",
    )
    return _summary(checks)


def _summary(checks: list[tuple[str, bool, str]]) -> int:
    failed = [check for check in checks if not check[1]]
    print(f"\n结果：{len(checks) - len(failed)}/{len(checks)} 通过")
    return 1 if failed else 0


async def main() -> int:
    from app.core.config import Settings
    from app.core.contract_store import ContractStore

    parser = argparse.ArgumentParser()
    parser.add_argument("--question", default="猫眼睛分泌物增多两天，需要注意什么？")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="通过后写入生产启动所需的 DeepSeek 契约标记",
    )
    args = parser.parse_args()
    settings = Settings(app_env="test", mock_mode=False, mock_knowledge_consult=False)
    rc = await run_checks(settings, args.question)
    store = ContractStore(settings)
    if rc == 0 and args.confirm:
        store.write_verified()
        print("已写入 DeepSeek API 契约标记")
    elif rc != 0:
        store.invalidate()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
