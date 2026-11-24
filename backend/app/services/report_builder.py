"""报告模板领域服务：外部报告只能按已允许的地区集合生成。

发布前的地区实算委托给 ``region_publication``，未达门槛的省份不会出现在
报告行和合计里；报告模板本身仍沿用通用登记/流转骨架。
"""
from __future__ import annotations

from typing import Any

from app.region_publication import (
    RegionPublicationPolicy,
    build_external_report,
    evaluate_publication,
    public_decision_view,
)
from app.region_samples import scenario_orders
from app.services.base import BaseModuleService


class ReportBuilderService(BaseModuleService):
    MODULE = "report_builder"
    ENTITY = "报告模板"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["模板编号", "报告名称", "引用指标", "排版格式", "生成周期", "推送渠道", "最近生成", "模板状态"]
    REQUIRED_FIELDS = ["模板编号", "报告名称", "引用指标"]
    STATUS_ORDER = ["草稿", "已发布", "格式异常", "已停用"]
    ACTION_RULES = {"发布模板": "已发布", "标记异常": "格式异常", "停用模板": "已停用"}
    NEGATIVE_ACTIONS = ["停用模板"]

    def external_region_report(
        self,
        orders: list[dict[str, Any]] | None = None,
        *,
        scope_id: str = "report_builder",
        policy: RegionPublicationPolicy | None = None,
        approved_regions: list[str] | None = None,
    ) -> dict[str, Any]:
        """按已允许的地区集合实算外部报告，绝不回传未授权省份指标。"""
        rows = scenario_orders() if orders is None else orders
        decision = evaluate_publication(
            rows,
            scope_id=scope_id,
            policy=policy or RegionPublicationPolicy(),
            approved_regions=approved_regions or [],
        )
        return {
            "decision": public_decision_view(decision),
            "report": build_external_report(rows, decision),
        }
