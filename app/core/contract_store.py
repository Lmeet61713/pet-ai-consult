"""DeepSeek 官方 API 契约验证标记。

`scripts/verify_deepseek_contract.py --confirm` 通过后写入
`.deepseek_contract.verified`（带配置摘要 JSON），生产启动时读取它。

- config_hash：参与摘要的 DeepSeek 配置（不含密钥），配置变更 → 标记失效
- 有效期：7 天，过期视为未验证
- 验证脚本失败时调用 invalidate() 删除旧标记
"""
from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings

CONTRACT_FILE = Path(__file__).resolve().parents[2] / ".deepseek_contract.verified"
CONTRACT_SCHEMA_VERSION = "deepseek-official-v1"
CONTRACT_MAX_AGE_DAYS = 7

# 参与 config_hash 的 DeepSeek 非敏感配置。
_HASH_FIELDS = (
    "knowledge_provider",
    "knowledge_api_base_url",
    "knowledge_model",
    "knowledge_thinking_mode",
)


def config_hash(settings: Settings) -> str:
    # API Key 只以 SHA-256 指纹参与最终摘要，不写入契约文件。密钥轮换后旧标记失效。
    key_fingerprint = hashlib.sha256(settings.knowledge_api_key.encode()).hexdigest()
    parts = "|".join(str(getattr(settings, f)) for f in _HASH_FIELDS) + "|" + key_fingerprint
    return hashlib.sha256(parts.encode()).hexdigest()


@dataclass
class ContractRecord:
    provider: str
    base_url_hash: str
    schema_version: str
    verified_at: str
    config_hash: str

    def matches(self, settings: Settings) -> bool:
        return (
            self.schema_version == CONTRACT_SCHEMA_VERSION
            and self.config_hash == config_hash(settings)
        )

    def is_fresh(self) -> bool:
        try:
            verified_at = datetime.datetime.fromisoformat(self.verified_at)
        except ValueError:
            return False
        age = datetime.datetime.now(verified_at.tzinfo) - verified_at
        return age.total_seconds() <= CONTRACT_MAX_AGE_DAYS * 86400


class ContractStore:
    """契约标记的读写；损坏/不匹配/过期一律视为未验证。"""

    def __init__(self, settings: Settings, path: Path | None = None):
        self.s = settings
        configured_path = (
            Path(settings.deepseek_contract_file).expanduser()
            if settings.deepseek_contract_file
            else None
        )
        self._path = path or configured_path or CONTRACT_FILE

    def load_verified_contract(self) -> ContractRecord | None:
        if not self._path.exists():
            return None
        try:
            rec = ContractRecord(**json.loads(self._path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - 损坏标记视为未验证
            return None
        if not rec.matches(self.s) or not rec.is_fresh():
            return None
        return rec

    def write_verified(self) -> None:
        """verify_deepseek_contract.py --confirm 通过后调用。"""
        record = {
            "provider": self.s.knowledge_provider,
            "base_url_hash": hashlib.sha256(
                self.s.knowledge_api_base_url.encode("utf-8")
            ).hexdigest()[:16],
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "verified_at": datetime.datetime.now().astimezone().isoformat(),
            "config_hash": config_hash(self.s),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def invalidate(self) -> None:
        """验证失败或配置变更时删除旧标记。"""
        if self._path.exists():
            self._path.unlink()
