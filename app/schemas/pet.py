"""宠物档案（v5 §7.2；2026-08-19 升级：支持名称 + 多宠物列表 + 年龄单位）"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PetInfo(BaseModel):
    """单只宠物档案。

    兼容说明（2026-08-19）：
    - name：宠物名称，多宠场景用于区分与追问引用；
    - age_value + age_unit：新年龄格式（2 岁 / 8 个月），后端自动换算为 age_months；
      旧字段 age_months 保留兼容（二者同时给时以 age_value/age_unit 为准）。
    """

    name: str | None = None
    species: str | None = None
    breed: str | None = None
    age_months: int | None = Field(default=None, ge=0, le=600)
    age_value: int | None = Field(default=None, ge=0, le=200)
    age_unit: Literal["month", "year"] | None = None
    weight_kg: float | None = Field(default=None, gt=0, le=200)
    sex: str | None = None
    neutered: bool | None = None
    chronic_conditions: list[str] = Field(default_factory=list)     # 慢性疾病
    current_medications: list[str] = Field(default_factory=list)     # 当前用药

    @model_validator(mode="after")
    def _normalize_age(self) -> "PetInfo":
        """age_value/age_unit → age_months（新格式优先，兼容旧字段）。年月综合转为月数"""
        if self.age_value is not None and self.age_unit is not None:
            months = self.age_value * 12 if self.age_unit == "year" else self.age_value
            self.age_months = min(months, 600)
        return self

    @property
    def display_name(self) -> str:
        """对话/追问中引用宠物时使用的名称。"""
        if self.name:
            return self.name
        if self.species:
            return {"dog": "狗狗", "cat": "猫咪"}.get(self.species.lower(), self.species)        #
        return "宠物"
