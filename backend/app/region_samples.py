"""地区发布场景样例：北一区（甲 4 笔、乙 2 笔）与北二区（丙 8 笔）。

订单字段与 ``app.region_publication`` 的归一化逻辑一致，可直接用于测试、
接口演示和发布报告产物生成。样例金额只用于核对聚合口径，不代表真实业务数据。
"""
from __future__ import annotations

from typing import Any

# 订单号前缀固定，便于在报告里核对。
NORTH_REGION_ORDERS: list[dict[str, Any]] = [
    # 北一区 · 甲：4 笔有效订单，合计 400
    {"order_id": "N1-JIA-0001", "region": "北一区", "province": "甲", "amount": 100.0, "status": "valid"},
    {"order_id": "N1-JIA-0002", "region": "北一区", "province": "甲", "amount": 120.0, "status": "valid"},
    {"order_id": "N1-JIA-0003", "region": "北一区", "province": "甲", "amount": 80.0, "status": "valid"},
    {"order_id": "N1-JIA-0004", "region": "北一区", "province": "甲", "amount": 100.0, "status": "valid"},
    # 北一区 · 乙：2 笔有效订单，合计 100
    {"order_id": "N1-YI-0001", "region": "北一区", "province": "乙", "amount": 60.0, "status": "paid"},
    {"order_id": "N1-YI-0002", "region": "北一区", "province": "乙", "amount": 40.0, "status": "paid"},
    # 北二区 · 丙：8 笔有效订单，合计 1000
    {"order_id": "N2-BING-0001", "region": "北二区", "province": "丙", "amount": 150.0, "status": "completed"},
    {"order_id": "N2-BING-0002", "region": "北二区", "province": "丙", "amount": 130.0, "status": "completed"},
    {"order_id": "N2-BING-0003", "region": "北二区", "province": "丙", "amount": 120.0, "status": "completed"},
    {"order_id": "N2-BING-0004", "region": "北二区", "province": "丙", "amount": 110.0, "status": "completed"},
    {"order_id": "N2-BING-0005", "region": "北二区", "province": "丙", "amount": 140.0, "status": "completed"},
    {"order_id": "N2-BING-0006", "region": "北二区", "province": "丙", "amount": 160.0, "status": "completed"},
    {"order_id": "N2-BING-0007", "region": "北二区", "province": "丙", "amount": 100.0, "status": "completed"},
    {"order_id": "N2-BING-0008", "region": "北二区", "province": "丙", "amount": 90.0, "status": "completed"},
]

# 重送已有订单：重复 2 笔乙的订单，再夹带 1 笔已取消的大额订单。
# 两者都不应改变任何省或区域的有效订单数与金额。
NORTH_REGION_RESEND_ROWS: list[dict[str, Any]] = [
    {"order_id": "N1-YI-0001", "region": "北一区", "province": "乙", "amount": 60.0, "status": "paid"},
    {"order_id": "N1-YI-0002", "region": "北一区", "province": "乙", "amount": 40.0, "status": "paid"},
    {"order_id": "N1-YI-0003", "region": "北一区", "province": "乙", "amount": 9999.0, "status": "cancelled"},
]

NORTH_REGION_WITH_RESEND: list[dict[str, Any]] = [
    *NORTH_REGION_ORDERS,
    *NORTH_REGION_RESEND_ROWS,
]


def scenario_orders(*, with_resend: bool = False) -> list[dict[str, Any]]:
    """返回场景订单的副本，避免调用方修改模块级样例。"""
    source = NORTH_REGION_WITH_RESEND if with_resend else NORTH_REGION_ORDERS
    return [dict(row) for row in source]
