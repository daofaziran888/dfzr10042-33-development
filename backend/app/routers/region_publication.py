"""地区发布接口：核定省级/区级发布资格、生成外部报告并幂等登记发布。

前端和外部报告都只读取这里返回的已披露集合；未达门槛的省不会出现在
报告行和合计中，只以名称 + 原因的形式出现在 ``suppressed`` 里。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.region_publication import (
    PublicationConflict,
    RegionPublicationPolicy,
    build_external_report,
    evaluate_publication,
    public_decision_view,
    registry,
)
from app.region_samples import scenario_orders
from app.schemas import (
    RegionPublicationPayload,
    RegionPolicyPayload,
    RegionScenarioPayload,
)


router = APIRouter(prefix="/api/region-publication", tags=["地区发布门槛"])


def _policy(payload: RegionPolicyPayload) -> RegionPublicationPolicy:
    return RegionPublicationPolicy(
        province_min_orders=payload.province_min_orders,
        region_min_orders=payload.region_min_orders,
    )


def _approved_provinces(payload: RegionPublicationPayload) -> list[tuple[str, str]]:
    result = []
    for item in payload.approved_provinces:
        region = str(item.get("region", "")).strip()
        province = str(item.get("province", "")).strip()
        if province:
            result.append((region, province))
    return result


@router.get("/policy")
def get_policy() -> dict[str, Any]:
    """返回默认门槛与说明，供前端展示。"""
    default = RegionPublicationPolicy()
    return {
        "policy": default.to_dict(),
        "notes": [
            "省级与上级区域门槛分别核定，互不替代",
            "各省有效订单数先去重再计数",
            "同区域不并列区合计与省明细，避免反推",
            "同一 scope 重复发布只落一条记录",
        ],
    }


@router.get("/scenario")
def get_scenario(
    with_resend: bool = False,
    approved_region: list[str] | None = Query(default=None),
    include_metrics: bool = False,
    province_min_orders: int = Query(default=5, ge=1),
    region_min_orders: int = Query(default=5, ge=1),
) -> dict[str, Any]:
    """用内置北一区/北二区样例生成核定结果与外部报告（只读，不登记）。

    ``include_metrics`` 仅用于内部核对全量省级结果；默认关闭，未授权项不带指标。
    两级门槛分别生效，便于核对省与区资格冲突的场景。
    """
    orders = scenario_orders(with_resend=with_resend)
    approved = list(approved_region or [])
    decision = evaluate_publication(
        orders,
        scope_id="north-region-2026-11",
        policy=RegionPublicationPolicy(
            province_min_orders=province_min_orders,
            region_min_orders=region_min_orders,
        ),
        approved_regions=approved,
    )
    result: dict[str, Any] = {
        "with_resend": with_resend,
        "approved_regions": approved,
        "policy": decision.policy.to_dict(),
        "decision": public_decision_view(
            decision,
            include_metrics=include_metrics,
            include_audit=include_metrics,
        ),
        "report": build_external_report(orders, decision),
    }
    # 输入行数也能通过“总行数 - 已披露订单”反推隐藏区域，默认不回传。
    if include_metrics:
        result["input"] = {
            "row_count": len(orders),
            "with_resend": with_resend,
            "approved_regions": approved,
        }
    return result


@router.post("/evaluate")
def evaluate(payload: RegionPublicationPayload) -> dict[str, Any]:
    """对调用方上传的订单核定发布资格，不写登记表。"""
    if not payload.orders:
        raise HTTPException(status_code=400, detail="orders 不能为空")
    decision = evaluate_publication(
        payload.orders,
        scope_id=payload.scope_id,
        policy=_policy(payload.policy),
        approved_regions=payload.approved_regions,
        approved_provinces=_approved_provinces(payload),
    )
    return {
        "decision": public_decision_view(decision),
        "report": build_external_report(payload.orders, decision),
    }


@router.post("/publish")
def publish(payload: RegionPublicationPayload) -> dict[str, Any]:
    """幂等发布：同一 scope 并发提交只落一次，返回已有记录。"""
    if not payload.orders:
        raise HTTPException(status_code=400, detail="orders 不能为空")
    try:
        record, created = registry.publish(
            payload.scope_id,
            payload.orders,
            policy=_policy(payload.policy),
            approved_regions=payload.approved_regions,
            approved_provinces=_approved_provinces(payload),
        )
    except PublicationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "created": created,
        "record": record.to_dict(),
    }


@router.post("/scenario/publish")
def publish_scenario(payload: RegionScenarioPayload) -> dict[str, Any]:
    """用内置样例一键发布，便于核对发布报告产物。"""
    orders = scenario_orders(with_resend=payload.with_resend)
    policy = RegionPublicationPolicy(
        province_min_orders=payload.province_min_orders,
        region_min_orders=payload.region_min_orders,
    )
    try:
        record, created = registry.publish(
            payload.scope_id,
            orders,
            policy=policy,
            approved_regions=payload.approved_regions,
        )
    except PublicationConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "created": created,
        "record": record.to_dict(),
    }


@router.get("/records")
def list_records() -> dict[str, Any]:
    """列出已登记的发布记录（不含未授权明细）。"""
    records = registry.all_records()
    return {
        "total": len(records),
        "items": [
            {
                "scope_id": record.scope_id,
                "revision": record.revision,
                "published_at": record.published_at,
                "report": record.report,
            }
            for record in records
        ],
    }


@router.get("/records/{scope_id}")
def get_record(scope_id: str) -> dict[str, Any]:
    record = registry.get(scope_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"发布范围 {scope_id} 不存在")
    return record.to_dict()
