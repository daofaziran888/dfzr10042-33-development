"""地区交易低样本发布门槛与外部报告披露控制。

业务规则
--------
1. 地区交易先按订单号去重，再按“有效订单”口径聚合到省和上级区域。
2. 省级发布门槛与上级区域发布门槛分别核定：

   - 省级有效订单数达到 ``province_min_orders`` 才能在外部报告里单独成项；
   - 上级区域有效订单数达到 ``region_min_orders`` 才具备区级汇总资格。

3. 外部报告只按“已允许的地区集合”实算，每个区域最多选择一种粒度：

   - 省明细：只列达到省级门槛的省；
   - 区级汇总：负责人显式批准后，用达到区级门槛的区合计替代该区全部省明细。

4. 低样本抑制与反推保护：

   - 未达标省份不进入外部报告；
   - 区级汇总与省明细不会同时出现在同一区域；
   - 报告内所有合计只由已披露行重新计算，不引用未授权数据；
   - 未披露项只保留名称和原因，不携带订单量、金额等指标。

5. 发布幂等：同一 ``scope_id`` 并发发布只落一条记录；重复订单重送不改变聚合口径。

“有效订单”采用白名单：状态为空或命中 ``VALID_ORDER_STATUSES`` 才计入；
取消/退款/无效等命中黑名单，未知状态一律按无效处理，宁可少算也不越门槛。
金额缺失或为负同样不计入有效订单。

模块只依赖标准库。除发布登记外，所有函数都是确定性纯函数，可直接用于测试。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence


# ============================================================
# 常量
# ============================================================

VALID_ORDER_STATUSES = frozenset({
    "valid", "paid", "completed", "success", "settled",
    "有效", "已支付", "已完成", "成功", "已结算",
})

INVALID_ORDER_STATUSES = frozenset({
    "cancelled", "canceled", "refunded", "invalid", "void", "failed",
    "已取消", "已退款", "无效", "失败", "作废",
})

DEFAULT_REGION_LABEL = "未分区"
DEFAULT_PROVINCE_LABEL = "未分省"


class PublicationConflict(RuntimeError):
    """同一发布范围出现不同数据版本时拒绝覆盖。"""


# ============================================================
# 字段读取与订单归一化
# ============================================================

def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _pick(row: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    """按优先级读取第一个非空字段，兼容中英文字段名。"""
    for key in keys:
        if key in row:
            value = row[key]
            if value is not None and value != "":
                return value
    return default


def _parse_amount(value: Any) -> float | None:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount) or amount < 0:
        return None
    return amount


def _is_valid_status(value: Any) -> bool:
    status = _text(value).casefold()
    if not status:
        return True
    if status in INVALID_ORDER_STATUSES:
        return False
    return status in VALID_ORDER_STATUSES


@dataclass(frozen=True)
class NormalizedOrder:
    """去重后的订单；金额与有效性已经标准化。"""

    order_id: str
    region: str
    province: str
    amount: float
    status: str
    is_valid: bool
    source_index: int


def normalize_order(row: Mapping[str, Any], index: int = 0) -> NormalizedOrder:
    """把一条原始订单转换为标准结构。缺失订单号时使用行号，避免误去重。"""
    order_id = _text(_pick(
        row, "order_id", "订单号", "订单编号", "订单ID", default=f"__row_{index}",
    )) or f"__row_{index}"
    region = _text(_pick(
        row, "region", "区域", "上级区域", "大区", default=DEFAULT_REGION_LABEL,
    )) or DEFAULT_REGION_LABEL
    province = _text(_pick(
        row, "province", "省份", "省", "省区", default=f"{region}·{DEFAULT_PROVINCE_LABEL}",
    ))
    status = _text(_pick(row, "status", "订单状态", "状态", default="valid"))
    amount = _parse_amount(_pick(row, "amount", "金额", "订单金额", "GMV", default=0))
    is_valid = amount is not None and _is_valid_status(status)
    return NormalizedOrder(
        order_id=order_id,
        region=region,
        province=province,
        amount=0.0 if amount is None else amount,
        status=status or "valid",
        is_valid=is_valid,
        source_index=index,
    )


def normalize_orders(orders: Iterable[Mapping[str, Any]]) -> list[NormalizedOrder]:
    """按订单号去重，保留首次出现的记录。

    重复订单（包括重送）不会增加任何省或区域的有效订单数。
    """
    seen: set[str] = set()
    normalized: list[NormalizedOrder] = []
    for index, row in enumerate(orders):
        order = normalize_order(row, index)
        if order.order_id in seen:
            continue
        seen.add(order.order_id)
        normalized.append(order)
    return normalized


# ============================================================
# 聚合结果
# ============================================================

@dataclass(frozen=True)
class ProvinceAggregate:
    region: str
    province: str
    order_count: int
    amount: float

    def to_dict(self, *, include_metrics: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "region": self.region,
            "province": self.province,
        }
        if include_metrics:
            data.update({"order_count": self.order_count, "amount": round(self.amount, 2)})
        return data


@dataclass(frozen=True)
class RegionAggregate:
    region: str
    order_count: int
    amount: float
    province_count: int

    def to_dict(self, *, include_metrics: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {"region": self.region}
        if include_metrics:
            data.update({
                "order_count": self.order_count,
                "amount": round(self.amount, 2),
                "province_count": self.province_count,
            })
        return data


@dataclass(frozen=True)
class AggregateResult:
    provinces: tuple[ProvinceAggregate, ...]
    regions: tuple[RegionAggregate, ...]
    raw_rows: int
    unique_orders: int
    valid_orders: int
    duplicate_order_ids: tuple[str, ...]
    invalid_order_ids: tuple[str, ...]
    total_amount: float

    def province_map(self) -> dict[tuple[str, str], ProvinceAggregate]:
        return {(row.region, row.province): row for row in self.provinces}

    def region_map(self) -> dict[str, RegionAggregate]:
        return {row.region: row for row in self.regions}


def aggregate_orders(orders: Iterable[Mapping[str, Any]]) -> AggregateResult:
    """先去重，再按有效订单聚合到省和上级区域。"""
    raw_orders = list(orders)
    normalized = normalize_orders(raw_orders)
    provinces: dict[tuple[str, str], list[NormalizedOrder]] = {}
    regions: dict[str, list[NormalizedOrder]] = {}
    invalid: list[str] = []
    valid_count = 0
    total_amount = 0.0

    for order in normalized:
        if not order.is_valid:
            invalid.append(order.order_id)
            continue
        valid_count += 1
        total_amount += order.amount
        provinces.setdefault((order.region, order.province), []).append(order)
        regions.setdefault(order.region, []).append(order)

    occurrence: dict[str, int] = {}
    for index, row in enumerate(raw_orders):
        order_id = normalize_order(row, index).order_id
        occurrence[order_id] = occurrence.get(order_id, 0) + 1
    duplicates = sorted(
        order_id for order_id, count in occurrence.items() if count > 1
    )

    province_rows = tuple(
        sorted(
            (
                ProvinceAggregate(
                    region=region,
                    province=province,
                    order_count=len(bucket),
                    amount=round(sum(o.amount for o in bucket), 2),
                )
                for (region, province), bucket in provinces.items()
            ),
            key=lambda row: (row.region, row.province),
        )
    )
    region_rows = tuple(
        sorted(
            (
                RegionAggregate(
                    region=region,
                    order_count=len(bucket),
                    amount=round(sum(o.amount for o in bucket), 2),
                    province_count=len({o.province for o in bucket}),
                )
                for region, bucket in regions.items()
            ),
            key=lambda row: row.region,
        )
    )
    return AggregateResult(
        provinces=province_rows,
        regions=region_rows,
        raw_rows=len(raw_orders),
        unique_orders=len(normalized),
        valid_orders=valid_count,
        duplicate_order_ids=tuple(duplicates),
        invalid_order_ids=tuple(invalid),
        total_amount=round(total_amount, 2),
    )


# ============================================================
# 发布策略与决策
# ============================================================

@dataclass(frozen=True)
class RegionPublicationPolicy:
    """省级与上级区域分别核定的发布门槛。"""

    province_min_orders: int = 5
    region_min_orders: int = 5

    def __post_init__(self) -> None:
        if self.province_min_orders < 1:
            raise ValueError("province_min_orders 必须大于 0")
        if self.region_min_orders < 1:
            raise ValueError("region_min_orders 必须大于 0")

    def to_dict(self) -> dict[str, int]:
        return {
            "province_min_orders": self.province_min_orders,
            "region_min_orders": self.region_min_orders,
        }


@dataclass(frozen=True)
class ProvinceDecision:
    region: str
    province: str
    order_count: int
    amount: float
    eligible: bool
    allowed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": "province",
            "region": self.region,
            "province": self.province,
            "order_count": self.order_count,
            "amount": round(self.amount, 2),
            "eligible": self.eligible,
            "allowed": self.allowed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RegionDecision:
    region: str
    order_count: int
    amount: float
    province_count: int
    eligible: bool
    has_under_threshold_provinces: bool
    conflict: bool
    approved: bool
    rollup_allowed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": "region",
            "region": self.region,
            "order_count": self.order_count,
            "amount": round(self.amount, 2),
            "province_count": self.province_count,
            "eligible": self.eligible,
            "has_under_threshold_provinces": self.has_under_threshold_provinces,
            "conflict": self.conflict,
            "approved": self.approved,
            "rollup_allowed": self.rollup_allowed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PublicationDecision:
    """省级与区级发布资格的核定结果（内部使用，可含低样本数值）。"""

    scope_id: str
    policy: RegionPublicationPolicy
    provinces: tuple[ProvinceDecision, ...]
    regions: tuple[RegionDecision, ...]
    allowed_provinces: tuple[tuple[str, str], ...]
    allowed_regions: tuple[str, ...]
    pending_confirmations: tuple[str, ...]
    aggregate: AggregateResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "policy": self.policy.to_dict(),
            "provinces": [row.to_dict() for row in self.provinces],
            "regions": [row.to_dict() for row in self.regions],
            "allowed_provinces": [
                {"region": region, "province": province}
                for region, province in self.allowed_provinces
            ],
            "allowed_regions": list(self.allowed_regions),
            "pending_confirmations": list(self.pending_confirmations),
            "audit": {
                "raw_rows": self.aggregate.raw_rows,
                "unique_orders": self.aggregate.unique_orders,
                "valid_orders": self.aggregate.valid_orders,
                "duplicate_order_ids": list(self.aggregate.duplicate_order_ids),
                "invalid_order_ids": list(self.aggregate.invalid_order_ids),
            },
        }

    @property
    def included_regions(self) -> tuple[str, ...]:
        covered = {region for region, _ in self.allowed_provinces}
        return tuple(sorted(covered | set(self.allowed_regions)))


def evaluate_publication(
    orders: Sequence[Mapping[str, Any]],
    *,
    scope_id: str = "default",
    policy: RegionPublicationPolicy | None = None,
    approved_regions: Iterable[str] = (),
    approved_provinces: Iterable[tuple[str, str]] | Iterable[str] = (),
) -> PublicationDecision:
    """核定每个省与每个上级区域是否具备发布资格。

    ``approved_regions`` 是负责人显式批准的区级汇总范围；未批准的冲突区域
    会进入 ``pending_confirmations``，本轮不披露。
    """
    policy = policy or RegionPublicationPolicy()
    aggregate = aggregate_orders(orders)

    region_approvals = {_text(item) for item in approved_regions if _text(item)}
    province_approvals: set[tuple[str, str]] | None
    raw_province_approvals = list(approved_provinces)
    if raw_province_approvals:
        province_approvals = set()
        for item in raw_province_approvals:
            if isinstance(item, str):
                province_approvals.add(("", _text(item)))
            else:
                region, province = item  # type: ignore[misc]
                province_approvals.add((_text(region), _text(province)))
    else:
        province_approvals = None

    province_decisions: list[ProvinceDecision] = []
    for row in aggregate.provinces:
        eligible = row.order_count >= policy.province_min_orders
        if not eligible:
            allowed = False
            reason = "below_province_threshold"
        elif province_approvals is not None:
            allowed = (
                (row.region, row.province) in province_approvals
                or ("", row.province) in province_approvals
            )
            reason = "allowed" if allowed else "province_not_approved"
        else:
            allowed = True
            reason = "allowed"
        province_decisions.append(ProvinceDecision(
            region=row.region,
            province=row.province,
            order_count=row.order_count,
            amount=row.amount,
            eligible=eligible,
            allowed=allowed,
            reason=reason,
        ))

    province_by_region: dict[str, list[ProvinceDecision]] = {}
    for decision in province_decisions:
        province_by_region.setdefault(decision.region, []).append(decision)

    region_decisions: list[RegionDecision] = []
    allowed_regions: list[str] = []
    pending: list[str] = []
    for row in aggregate.regions:
        children = province_by_region.get(row.region, [])
        under = [child for child in children if not child.eligible]
        has_under = bool(under)
        eligible = row.order_count >= policy.region_min_orders
        approved = row.region in region_approvals
        # 只有存在低样本省份、且区级合计达到门槛时，区级汇总才有意义；
        # 若所有省都已达标，应优先披露省明细，避免粗粒度掩盖明细。
        conflict = eligible and has_under
        rollup_allowed = conflict and approved
        if not eligible:
            reason = "below_region_threshold"
        elif not has_under:
            reason = "province_detail_preferred"
        elif not approved:
            reason = "owner_confirmation_pending"
            pending.append(row.region)
        else:
            reason = "region_rollup_approved"
        if rollup_allowed:
            allowed_regions.append(row.region)
        region_decisions.append(RegionDecision(
            region=row.region,
            order_count=row.order_count,
            amount=row.amount,
            province_count=row.province_count,
            eligible=eligible,
            has_under_threshold_provinces=has_under,
            conflict=conflict,
            approved=approved,
            rollup_allowed=rollup_allowed,
            reason=reason,
        ))

    rollup_regions = set(allowed_regions)
    allowed_province_keys = tuple(
        (decision.region, decision.province)
        for decision in province_decisions
        if decision.allowed
        and decision.region not in rollup_regions
    )

    return PublicationDecision(
        scope_id=scope_id,
        policy=policy,
        provinces=tuple(province_decisions),
        regions=tuple(region_decisions),
        allowed_provinces=allowed_province_keys,
        allowed_regions=tuple(allowed_regions),
        pending_confirmations=tuple(pending),
        aggregate=aggregate,
    )


# ============================================================
# 过滤规则：按有效订单数保留达标省份/区域
# ============================================================

def province_valid_order_filter(
    orders: Sequence[Mapping[str, Any]],
    *,
    min_orders: int = 5,
) -> tuple[list[ProvinceAggregate], list[ProvinceAggregate]]:
    """新增的地区交易过滤规则：按各省有效订单数保留达标省份。

    返回 ``(passed, dropped)``；输入先按订单号去重，再只统计有效订单。
    """
    if min_orders < 1:
        raise ValueError("min_orders 必须大于 0")
    aggregate = aggregate_orders(orders)
    passed = [row for row in aggregate.provinces if row.order_count >= min_orders]
    dropped = [row for row in aggregate.provinces if row.order_count < min_orders]
    return passed, dropped


def region_valid_order_filter(
    orders: Sequence[Mapping[str, Any]],
    *,
    min_orders: int = 5,
) -> tuple[list[RegionAggregate], list[RegionAggregate]]:
    """按上级区域的有效订单数保留达标区域（与省级门槛独立核定）。"""
    if min_orders < 1:
        raise ValueError("min_orders 必须大于 0")
    aggregate = aggregate_orders(orders)
    passed = [row for row in aggregate.regions if row.order_count >= min_orders]
    dropped = [row for row in aggregate.regions if row.order_count < min_orders]
    return passed, dropped


def public_decision_view(
    decision: PublicationDecision,
    *,
    include_metrics: bool = False,
    include_audit: bool = False,
) -> dict[str, Any]:
    """发布决策的对外视图：未授权省份/区域不带任何指标数值。

    内部复核才使用 ``decision.to_dict()``；接口默认返回本视图。
    ``audit`` 里的有效订单总数同样会通过“总数 - 已披露”反推出隐藏区域，
    因此默认不返回，只有 ``include_metrics`` / ``include_audit`` 才附带。
    """
    allowed_province_keys = set(decision.allowed_provinces)
    allowed_region_keys = set(decision.allowed_regions)

    provinces = []
    for row in decision.provinces:
        allowed = (row.region, row.province) in allowed_province_keys
        item = {
            "region": row.region,
            "province": row.province,
            "eligible": row.eligible,
            "allowed": row.allowed,
            "reason": row.reason,
        }
        if include_metrics or allowed:
            item.update({"order_count": row.order_count, "amount": round(row.amount, 2)})
        provinces.append(item)

    regions = []
    for row in decision.regions:
        allowed = row.region in allowed_region_keys
        item = {
            "region": row.region,
            "eligible": row.eligible,
            "has_under_threshold_provinces": row.has_under_threshold_provinces,
            "conflict": row.conflict,
            "approved": row.approved,
            "rollup_allowed": row.rollup_allowed,
            "reason": row.reason,
        }
        if include_metrics or allowed:
            item.update({
                "order_count": row.order_count,
                "amount": round(row.amount, 2),
                "province_count": row.province_count,
            })
        regions.append(item)

    view = {
        "scope_id": decision.scope_id,
        "policy": decision.policy.to_dict(),
        "provinces": provinces,
        "regions": regions,
        "allowed_provinces": [
            {"region": region, "province": province}
            for region, province in decision.allowed_provinces
        ],
        "allowed_regions": list(decision.allowed_regions),
        "pending_confirmations": list(decision.pending_confirmations),
    }
    if include_metrics or include_audit:
        view["audit"] = {
            "raw_rows": decision.aggregate.raw_rows,
            "unique_orders": decision.aggregate.unique_orders,
            "valid_orders": decision.aggregate.valid_orders,
            "duplicate_order_ids": list(decision.aggregate.duplicate_order_ids),
            "invalid_order_ids": list(decision.aggregate.invalid_order_ids),
        }
    return view


# ============================================================
# 外部报告：只按已允许集合实算
# ============================================================

@dataclass(frozen=True)
class ReportRow:
    level: str
    region: str
    label: str
    order_count: int
    amount: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "region": self.region,
            "label": self.label,
            "order_count": self.order_count,
            "amount": round(self.amount, 2),
        }


def build_external_report(
    orders: Sequence[Mapping[str, Any]],
    decision: PublicationDecision,
) -> dict[str, Any]:
    """按发布决策里的已允许集合，从原始订单重新聚合生成外部报告。

    未获授权的省份不会出现在 ``rows``，也不会以数值形式出现在任何合计中。
    重新聚合的结果必须与核定时的聚合完全一致，否则拒绝出报告——避免用
    另一批订单生成“已批准”的省明细，绕过门槛。
    """
    recomputed = aggregate_orders(orders)
    if (
        recomputed.provinces != decision.aggregate.provinces
        or recomputed.regions != decision.aggregate.regions
    ):
        raise ValueError("发布决策与当前订单数据不一致，拒绝生成外部报告")
    aggregate = recomputed
    province_map = aggregate.province_map()
    region_map = aggregate.region_map()

    rows: list[ReportRow] = []
    for region in decision.allowed_regions:
        region_row = region_map.get(region)
        if region_row is None:
            continue
        rows.append(ReportRow(
            level="region",
            region=region,
            label=region,
            order_count=region_row.order_count,
            amount=region_row.amount,
        ))

    for region, province in decision.allowed_provinces:
        province_row = province_map.get((region, province))
        if province_row is None:
            continue
        rows.append(ReportRow(
            level="province",
            region=region,
            label=province,
            order_count=province_row.order_count,
            amount=province_row.amount,
        ))

    rows.sort(key=lambda row: (row.region, row.level, row.label))

    # 反推保护：同一区域不允许同时出现区级合计与省明细。
    _validate_no_region_detail_overlap(rows)

    total_orders = sum(row.order_count for row in rows)
    total_amount = round(sum(row.amount for row in rows), 2)
    # 各类合计只由已披露行重算；键带区域，避免同名省份跨区碰撞。
    category_totals = [
        {
            "level": row.level,
            "region": row.region,
            "label": row.label,
            "order_count": row.order_count,
            "amount": round(row.amount, 2),
        }
        for row in rows
    ]
    region_totals = {
        region: {
            "order_count": sum(row.order_count for row in rows if row.region == region),
            "amount": round(sum(row.amount for row in rows if row.region == region), 2),
        }
        for region in sorted({row.region for row in rows})
    }

    suppressed: list[dict[str, Any]] = []
    disclosed_targets = set(decision.allowed_provinces)
    for province in decision.provinces:
        key = (province.region, province.province)
        if key in disclosed_targets:
            continue
        if province.region in decision.allowed_regions:
            reason = "rolled_up_into_region"
        elif not province.eligible:
            reason = "below_province_threshold"
        else:
            reason = "province_not_approved"
        # 只保留名称和原因，绝不携带订单量或金额，避免反推。
        suppressed.append({
            "level": "province",
            "region": province.region,
            "label": province.province,
            "reason": reason,
        })

    disclosed_regions = {region for region, _ in decision.allowed_provinces}
    disclosed_regions.update(decision.allowed_regions)
    for region in decision.regions:
        if region.region in disclosed_regions:
            continue
        suppressed.append({
            "level": "region",
            "region": region.region,
            "label": region.region,
            "reason": region.reason,
        })

    return {
        "scope_id": decision.scope_id,
        "policy": decision.policy.to_dict(),
        "rows": [row.to_dict() for row in rows],
        "totals": {
            "order_count": total_orders,
            "amount": total_amount,
        },
        "category_totals": category_totals,
        "region_totals": region_totals,
        "included_regions": list(decision.included_regions),
        "included_provinces": [
            province for _, province in decision.allowed_provinces
        ],
        "pending_confirmations": list(decision.pending_confirmations),
        "suppressed": suppressed,
        "disclosure_guard": {
            "region_and_province_exclusive": True,
            "totals_recomputed_from_disclosed_rows": True,
            "suppressed_metrics_included": False,
        },
    }


def _validate_no_region_detail_overlap(rows: Sequence[ReportRow]) -> None:
    regions_with_rollup = {row.region for row in rows if row.level == "region"}
    regions_with_detail = {row.region for row in rows if row.level == "province"}
    overlap = regions_with_rollup & regions_with_detail
    if overlap:
        raise ValueError(
            "同一区域不能同时披露区级合计与省明细：" + "、".join(sorted(overlap))
        )


# ============================================================
# 幂等发布登记
# ============================================================

@dataclass(frozen=True)
class PublicationRecord:
    scope_id: str
    fingerprint: str
    revision: int
    published_at: str
    decision: PublicationDecision
    report: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "fingerprint": self.fingerprint,
            "revision": self.revision,
            "published_at": self.published_at,
            "report": self.report,
        }


def _fingerprint(
    orders: Sequence[Mapping[str, Any]],
    policy: RegionPublicationPolicy,
    approved_regions: Iterable[str],
    approved_provinces: Iterable[Any],
) -> str:
    """按“有效且去重后”的订单计算指纹。

    重送已有订单、补送已取消订单都不会改变指纹，因此同一范围仍是同一次发布。
    """
    normalized = [
        {
            "order_id": order.order_id,
            "region": order.region,
            "province": order.province,
            "amount": round(order.amount, 2),
        }
        for order in normalize_orders(orders)
        if order.is_valid
    ]
    payload = {
        "orders": sorted(normalized, key=lambda row: row["order_id"]),
        "policy": policy.to_dict(),
        "approved_regions": sorted({_text(item) for item in approved_regions}),
        "approved_provinces": sorted(
            _text(item) if isinstance(item, str) else f"{item[0]}/{item[1]}"
            for item in approved_provinces
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PublicationRegistry:
    """进程内发布登记：同一 scope 只落一次，重复请求返回原记录。

    真实项目会换成带唯一约束的数据库；这里用锁保证并发下只有一条记录。
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, PublicationRecord] = {}

    def publish(
        self,
        scope_id: str,
        orders: Sequence[Mapping[str, Any]],
        *,
        policy: RegionPublicationPolicy | None = None,
        approved_regions: Iterable[str] = (),
        approved_provinces: Iterable[Any] = (),
    ) -> tuple[PublicationRecord, bool]:
        """返回 ``(记录, 是否本次新建)``。"""
        policy = policy or RegionPublicationPolicy()
        approved_regions = tuple(approved_regions)
        approved_provinces = tuple(approved_provinces)
        fingerprint = _fingerprint(
            orders, policy, approved_regions, approved_provinces
        )
        with self._lock:
            existing = self._records.get(scope_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise PublicationConflict(
                        f"发布范围 {scope_id} 已存在不同数据版本，拒绝覆盖"
                    )
                return existing, False
            decision = evaluate_publication(
                orders,
                scope_id=scope_id,
                policy=policy,
                approved_regions=approved_regions,
                approved_provinces=approved_provinces,
            )
            report = build_external_report(orders, decision)
            record = PublicationRecord(
                scope_id=scope_id,
                fingerprint=fingerprint,
                revision=1,
                published_at=datetime.now(timezone.utc).isoformat(),
                decision=decision,
                report=report,
            )
            self._records[scope_id] = record
            return record, True

    def get(self, scope_id: str) -> PublicationRecord | None:
        with self._lock:
            return self._records.get(scope_id)

    def all_records(self) -> tuple[PublicationRecord, ...]:
        with self._lock:
            return tuple(self._records.values())

    def reset(self) -> None:
        with self._lock:
            self._records.clear()


registry = PublicationRegistry()
