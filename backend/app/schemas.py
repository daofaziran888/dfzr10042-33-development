"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class RegionPolicyPayload(BaseModel):
    """省级与上级区域分别核定的发布门槛。"""

    province_min_orders: int = Field(default=5, ge=1)
    region_min_orders: int = Field(default=5, ge=1)


class RegionPublicationPayload(BaseModel):
    scope_id: str = Field(default="default", min_length=1, max_length=120)
    orders: list[dict[str, Any]] = Field(default_factory=list)
    policy: RegionPolicyPayload = Field(default_factory=RegionPolicyPayload)
    approved_regions: list[str] = Field(default_factory=list)
    approved_provinces: list[dict[str, str]] = Field(default_factory=list)


class RegionScenarioPayload(BaseModel):
    scope_id: str = Field(default="north-region-2026-11", min_length=1, max_length=120)
    with_resend: bool = False
    province_min_orders: int = Field(default=5, ge=1)
    region_min_orders: int = Field(default=5, ge=1)
    approved_regions: list[str] = Field(default_factory=list)
