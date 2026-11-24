"""下钻方案领域服务：地区下钻的发布资格核定。

除通用登记/流转外，地区下钻方案在发布前必须先通过低样本门槛：
省级与上级区域门槛分别核定，报告只能按已允许的地区集合实算。
"""
from __future__ import annotations

from typing import Any

from app.region_publication import (
    RegionPublicationPolicy,
    build_external_report,
    evaluate_publication,
    registry,
)
from app.region_samples import scenario_orders
from app.services.base import BaseModuleService


class DrillPathService(BaseModuleService):
    MODULE = "drill_path"
    ENTITY = "下钻方案"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["方案编号", "父级维度", "子级维度", "过渡层数", "数据粒度", "预计算模式", "最近下钻", "方案状态"]
    REQUIRED_FIELDS = ["方案编号", "父级维度", "子级维度"]
    STATUS_ORDER = ["草稿", "生效中", "粒度冲突", "已停用"]
    ACTION_RULES = {"启效方案": "生效中", "标记冲突": "粒度冲突", "停用方案": "已停用"}
    NEGATIVE_ACTIONS = ["停用方案"]

    def region_publication(
        self,
        orders: list[dict[str, Any]] | None = None,
        *,
        scope_id: str = "drill_path",
        policy: RegionPublicationPolicy | None = None,
        approved_regions: list[str] | None = None,
        publish: bool = False,
    ) -> dict[str, Any]:
        """核定地区下钻的发布资格，可选同时登记发布。

        默认使用内置北一区/北二区样例，方便起服务后直接核对规则。
        """
        rows = scenario_orders() if orders is None else orders
        policy = policy or RegionPublicationPolicy()
        approved_regions = approved_regions or []
        decision = evaluate_publication(
            rows,
            scope_id=scope_id,
            policy=policy,
            approved_regions=approved_regions,
        )
        result = {
            "decision": decision.to_dict(),
            "report": build_external_report(rows, decision),
            "published": False,
        }
        if publish:
            record, created = registry.publish(
                scope_id,
                rows,
                policy=policy,
                approved_regions=approved_regions,
            )
            result["published"] = created
            result["published_at"] = record.published_at
        return result
