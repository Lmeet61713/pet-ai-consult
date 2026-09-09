"""
绝对 deadline 预算管理（V1.1 P0-3）

请求入口创建唯一 `expires_at`；所有阶段（Vision/生成/重写/Guard）与所有重试
读取同一个剩余时间，任何重试都不得重新获得完整预算。

机制说明：
- `child(cap)`：阶段子预算（与全局共享 expires_at，再叠加阶段上限）
- `require(minimum)`：实时领取剩余预算；不足抛 RequestDeadlineExceeded
- 外层 `asyncio.wait_for` 做硬兜底，超时也抛 RequestDeadlineExceeded

设计意图：
- 防止单次请求因重试或慢阶段无限挂起
- 确保急症场景下快速降级到固定模板
"""
from __future__ import annotations

import time

from app.core.exceptions import RequestDeadlineExceeded


class Deadline:
    """绝对截止时间管理器。

    使用 time.monotonic() 单调时钟，不受系统时间跳变影响。
    expires_at 创建后固定不变，remaining 实时计算。

    用法：
        deadline = Deadline.after_seconds(45.0)
        budget = deadline.require(cap=15.0)  # 领取阶段预算
        child = deadline.child(cap=10.0)      # 创建子预算
    """

    def __init__(self, *, expires_at: float) -> None:
        self.expires_at = expires_at  # 单调时钟上的绝对过期时间

    @classmethod
    def after_seconds(cls, total_seconds: float) -> "Deadline":
        """从当前时间开始，创建一个 total_seconds 后过期的 Deadline。"""
        return cls(expires_at=time.monotonic() + total_seconds)

    @property
    def remaining(self) -> float:
        """当前剩余时间（秒），已过期则返回 0.0。"""
        return max(0.0, self.expires_at - time.monotonic())

    def require(self, *, cap: float | None = None, minimum: float = 0.1) -> float:
        """领取本阶段预算：实时剩余（可选 cap 上限），不足 minimum 抛超时异常。

        Args:
            cap: 预算上限（防止单个阶段吞噬全部剩余时间）
            minimum: 最小所需预算，默认 0.1 秒

        Returns:
            可用的预算时间（秒）

        Raises:
            RequestDeadlineExceeded: 剩余不足 minimum
        """
        remaining = self.remaining
        if remaining < minimum:
            raise RequestDeadlineExceeded("请求已超过处理时限")
        return min(remaining, cap) if cap is not None else remaining

    def has_remaining(self, seconds: float) -> bool:
        """检查是否有至少 seconds 秒的剩余时间。"""
        return self.remaining >= seconds

    def child(self, *, cap: float) -> "Deadline":
        """创建阶段子预算：min(全局 expires_at, 现在+cap)。

        子预算既受父级全局约束，也加了自己的阶段上限。
        用于 Vision、生成等可能超时的独立阶段。
        """
        return Deadline(expires_at=min(self.expires_at, time.monotonic() + cap))


class DeadlineFactory:
    """Deadline 工厂，根据配置的总超时创建 Deadline 实例。"""

    def __init__(self, total_seconds: float):
        self.total_seconds = total_seconds  # 默认总超时（秒）

    def after_seconds(self, total_seconds: float | None = None) -> Deadline:
        """创建 Deadline 实例。

        Args:
            total_seconds: 可覆盖默认超时（测试场景用）
        """
        return Deadline.after_seconds(total_seconds or self.total_seconds)