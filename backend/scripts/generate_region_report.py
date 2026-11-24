"""生成北一区/北二区场景的省级结果、发布核定与外部报告产物。

用法（在 backend 目录下）：
    .venv/bin/python scripts/generate_region_report.py [--out DIR] [--with-resend]

输出：
    region_publication.json  机器可读的完整结果
    region_publication.md    供人工核对的发布报告
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从 backend 目录直接运行：把项目根加入 sys.path，再导入应用包。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.region_publication import (  # noqa: E402
    PublicationRegistry,
    RegionPublicationPolicy,
    build_external_report,
    evaluate_publication,
)
from app.region_samples import scenario_orders  # noqa: E402


def build_document(*, with_resend: bool, province_min: int, region_min: int) -> dict:
    orders = scenario_orders(with_resend=with_resend)
    policy = RegionPublicationPolicy(
        province_min_orders=province_min,
        region_min_orders=region_min,
    )
    default = evaluate_publication(
        orders,
        scope_id="north-region-2026-11",
        policy=policy,
    )
    approved = evaluate_publication(
        orders,
        scope_id="north-region-2026-11",
        policy=policy,
        approved_regions=["北一区"],
    )

    # 幂等核对：先发布一次，再带上重送/无效订单重发同一范围。
    registry = PublicationRegistry()
    first, created_first = registry.publish("north-region-2026-11", scenario_orders(), policy=policy)
    resent, created_second = registry.publish(
        "north-region-2026-11", scenario_orders(with_resend=True), policy=policy
    )

    return {
        "input": {
            "with_resend": with_resend,
            "row_count": len(orders),
            "policy": policy.to_dict(),
        },
        "province_results": [
            {
                "region": row.region,
                "province": row.province,
                "order_count": row.order_count,
                "amount": row.amount,
            }
            for row in default.aggregate.provinces
        ],
        "region_results": [
            {
                "region": row.region,
                "order_count": row.order_count,
                "amount": row.amount,
                "province_count": row.province_count,
            }
            for row in default.aggregate.regions
        ],
        "audit": default.to_dict()["audit"],
        "default_decision": default.to_dict(),
        "default_report": build_external_report(orders, default),
        "approved_north1_decision": approved.to_dict(),
        "approved_north1_report": build_external_report(orders, approved),
        "idempotency_check": {
            "first_publish_created": created_first,
            "resent_publish_created": created_second,
            "same_fingerprint": first.fingerprint == resent.fingerprint,
            "record_count": len(registry.all_records()),
            "totals_before": first.report["totals"],
            "totals_after_resend": resent.report["totals"],
        },
    }


def _fmt_rows(rows: list[dict]) -> str:
    if not rows:
        return "（无）"
    lines = ["| 层级 | 区域 | 条目 | 有效订单数 | 金额 |", "| --- | --- | --- | ---: | ---: |"]
    for row in rows:
        lines.append(
            f"| {'区级汇总' if row['level'] == 'region' else '省级'} "
            f"| {row['region']} | {row['label']} "
            f"| {row['order_count']} | {row['amount']:.2f} |"
        )
    return "\n".join(lines)


def render_markdown(document: dict) -> str:
    default = document["default_report"]
    approved = document["approved_north1_report"]
    audit = document["audit"]
    policy = document["input"]["policy"]

    lines = [
        "# 地区交易低样本发布报告",
        "",
        f"- 发布范围：`{default['scope_id']}`",
        f"- 省级门槛：有效订单数 ≥ {policy['province_min_orders']}",
        f"- 区级门槛：有效订单数 ≥ {policy['region_min_orders']}",
        f"- 输入行数：{document['input']['row_count']}"
        f"（去重后 {audit['unique_orders']} 笔，有效 {audit['valid_orders']} 笔）",
        f"- 重复订单号：{', '.join(audit['duplicate_order_ids']) or '无'}",
        f"- 无效订单号：{', '.join(audit['invalid_order_ids']) or '无'}",
        "",
        "## 省级实际结果（全量，仅内部核定用）",
        "",
        "| 区域 | 省份 | 有效订单数 | 金额 |",
        "| --- | --- | ---: | ---: |",
    ]
    for row in document["province_results"]:
        lines.append(
            f"| {row['region']} | {row['province']} | {row['order_count']} | {row['amount']:.2f} |"
        )

    lines += [
        "",
        "## 区级实际结果（全量，仅内部核定用）",
        "",
        "| 区域 | 有效订单数 | 金额 | 省份数 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in document["region_results"]:
        lines.append(
            f"| {row['region']} | {row['order_count']} | {row['amount']:.2f} | {row['province_count']} |"
        )

    lines += [
        "",
        "## 发布报告（默认：北一区待负责人确认，不披露）",
        "",
        _fmt_rows(default["rows"]),
        "",
        f"合计：{default['totals']['order_count']} 笔 / {default['totals']['amount']:.2f}",
        "",
        "入算地区："
        + ("、".join(default["included_regions"]) or "（无）")
        + "；入算省份："
        + ("、".join(default["included_provinces"]) or "（无）"),
        "",
        "未披露项（仅名称与原因）：",
        "",
    ]
    for item in default["suppressed"]:
        lines.append(f"- {item['level']} `{item['label']}`（{item['region']}）：{item['reason']}")

    lines += [
        "",
        "## 发布报告（备选：负责人批准北一区按区级汇总）",
        "",
        _fmt_rows(approved["rows"]),
        "",
        f"合计：{approved['totals']['order_count']} 笔 / {approved['totals']['amount']:.2f}",
        "",
        "> 区级汇总与省明细互斥；北一区若批准，只披露 6 笔的区合计，"
        "甲、乙仍不单独成项。",
        "",
        "## 核对结论",
        "",
        f"- 入算地区数据：默认报告只含 "
        f"{'、'.join(default['included_regions'])}，"
        f"北一区因两省均未达省级门槛且区级汇总未获批准，进入待确认。",
        f"- 丙的金额：{next((r['amount'] for r in default['rows'] if r['label'] == '丙'), None)}",
        "- 北一区能否单独成为一项：不能。区合计 6 笔虽达区级门槛，"
        "但甲 4 笔、乙 2 笔均低于省级门槛 5 笔；报告不允许用区合计倒推省明细。",
        "- 重送已有订单：去重后有效订单数与金额不变，发布记录不新增。",
        "",
        "## 幂等核对",
        "",
        f"- 首次发布新建记录：{document['idempotency_check']['first_publish_created']}",
        f"- 重送订单后再次发布新建记录：{document['idempotency_check']['resent_publish_created']}",
        f"- 两次指纹一致：{document['idempotency_check']['same_fingerprint']}",
        f"- 登记记录数：{document['idempotency_check']['record_count']}",
        f"- 重送前合计：{document['idempotency_check']['totals_before']}",
        f"- 重送后合计：{document['idempotency_check']['totals_after_resend']}",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "reports"), help="输出目录")
    parser.add_argument("--with-resend", action="store_true", help="附带重送订单验证幂等")
    parser.add_argument("--province-min", type=int, default=5)
    parser.add_argument("--region-min", type=int, default=5)
    args = parser.parse_args()

    output_dir = Path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)

    document = build_document(
        with_resend=args.with_resend,
        province_min=args.province_min,
        region_min=args.region_min,
    )
    json_path = output_dir / "region_publication.json"
    md_path = output_dir / "region_publication.md"
    json_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_markdown(document), encoding="utf-8")
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
