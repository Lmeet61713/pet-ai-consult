"""
多轮追问使用的结构化病例事实。
机器人追问用户
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CaseFacts(BaseModel):
    """
    只保存用户陈述和图片观察中能够确认的事实。
    :param: domain: 领域
    :param: duration: 持续时间
    :param: duration_hours: 持续时间（小时）
    :param: discharge_color: ？？？
    :param: discharge_character: ？？？
    :param: eye_signs: 眼部征
    :param: energy_status: 体能状态
    :param: appetite_status: 饮食状态
    :param: eye_closeup_available: 眼部可否闭合
    :param: answered_slots: 已回答槽位，避免重复问一个问题
    :param: missing_slots: 缺失槽位，判断用户还没问哪些问题
    :param: asked_questions: 已问问题，即可去重（系统的回答）
    :param: confirmed_facts: 确认的事实列表
    """

    domain: str = "unknown"
    duration: str = ""
    duration_hours: float | None = None
    discharge_color: str = ""
    discharge_character: str = ""
    eye_signs: list[str] = Field(default_factory=list)
    energy_status: str = ""
    appetite_status: str = ""
    eye_closeup_available: bool = False
    answered_slots: list[str] = Field(default_factory=list)
    missing_slots: list[str] = Field(default_factory=list)
    asked_questions: list[str] = Field(default_factory=list)
    confirmed_facts: list[str] = Field(default_factory=list)

