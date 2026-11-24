"""地区低样本发布门槛测试。

覆盖：
- 按省份有效订单数聚合的过滤规则
- 重复订单/无效订单不推进门槛
- 省级与区级门槛分别核定
- 北一区（甲 4、乙 2）与北二区（丙 8）的发布冲突
- 外部报告反推保护、未授权数值不出现
- 并发/重复发布幂等
"""
from __future__ import annotations

import threading

import pytest

from app.analysis_pipeline import pipeline_region_publication
from app.models import DimensionDef, ReportTemplate, MetricDef, AggregationMethod, MetricDataSource, MetricPeriod
from app.region_publication import (
    PublicationConflict,
    PublicationRegistry,
    RegionPublicationPolicy,
    aggregate_orders,
    build_external_report,
    evaluate_publication,
    normalize_orders,
    province_valid_order_filter,
    region_valid_order_filter,
)
from app.region_samples import scenario_orders


def _order(order_id, province, amount=10.0, region="北一区", status="valid"):
    return {
        "order_id": order_id,
        "region": region,
        "province": province,
        "amount": amount,
        "status": status,
    }


@pytest.fixture
def scenario():
    return scenario_orders()


@pytest.fixture
def scenario_with_resend():
    return scenario_orders(with_resend=True)


# ============================================================
# 过滤规则
# ============================================================

class TestValidOrderFilter:
    def test_scenario_province_counts(self, scenario):
        aggregate = aggregate_orders(scenario)
        counts = {row.province: row.order_count for row in aggregate.provinces}
        assert counts == {"甲": 4, "乙": 2, "丙": 8}

    def test_province_filter_keeps_only_qualified(self, scenario):
        passed, dropped = province_valid_order_filter(scenario, min_orders=5)
        assert [row.province for row in passed] == ["丙"]
        assert {row.province for row in dropped} == {"甲", "乙"}

    def test_region_filter_is_independent(self, scenario):
        # 北一区合计 6 笔达到区级门槛，但两个省都不达省级门槛。
        passed, dropped = region_valid_order_filter(scenario, min_orders=5)
        assert [row.region for row in passed] == ["北一区", "北二区"]
        assert dropped == []

    def test_region_threshold_can_differ(self, scenario):
        passed, dropped = region_valid_order_filter(scenario, min_orders=7)
        assert [row.region for row in passed] == ["北二区"]
        assert [row.region for row in dropped] == ["北一区"]

    def test_duplicate_orders_do_not_push_threshold(self):
        orders = [_order(f"B{i}", "乙", 10.0) for i in range(2)]
        # 重复送同一批订单，数量应保持 2，而不是 4。
        orders += [dict(row) for row in orders]
        passed, dropped = province_valid_order_filter(orders, min_orders=5)
        assert passed == []
        assert dropped[0].order_count == 2

    def test_invalid_orders_do_not_count(self):
        orders = [_order(f"V{i}", "乙", 10.0) for i in range(4)]
        orders.append(_order("X1", "乙", 9999.0, status="cancelled"))
        passed, dropped = province_valid_order_filter(orders, min_orders=5)
        assert passed == []
        assert dropped[0].order_count == 4

    def test_filter_requires_positive_threshold(self, scenario):
        with pytest.raises(ValueError):
            province_valid_order_filter(scenario, min_orders=0)


# ============================================================
# 归一化与去重审计
# ============================================================

class TestNormalization:
    def test_duplicate_ids_collapse(self):
        orders = [_order("A", "甲"), _order("A", "甲"), _order("B", "甲")]
        normalized = normalize_orders(orders)
        assert [row.order_id for row in normalized] == ["A", "B"]

    def test_missing_order_id_uses_row_index(self):
        normalized = normalize_orders([
            {"region": "北一区", "province": "甲", "amount": 1},
            {"region": "北一区", "province": "甲", "amount": 1},
        ])
        assert len(normalized) == 2
        assert normalized[0].order_id != normalized[1].order_id

    def test_scenario_audit(self, scenario_with_resend):
        aggregate = aggregate_orders(scenario_with_resend)
        # 14 原始 + 3 重送 = 17 行；去重后 15 笔（14 有效 + 1 取消）
        assert aggregate.raw_rows == 17
        assert aggregate.unique_orders == 15
        assert aggregate.valid_orders == 14
        assert set(aggregate.duplicate_order_ids) == {"N1-YI-0001", "N1-YI-0002"}
        assert aggregate.invalid_order_ids == ("N1-YI-0003",)

    def test_resend_does_not_change_totals(self, scenario, scenario_with_resend):
        base = aggregate_orders(scenario)
        resent = aggregate_orders(scenario_with_resend)
        assert resent.provinces == base.provinces
        assert resent.regions == base.regions
        assert resent.total_amount == base.total_amount


# ============================================================
# 发布资格核定
# ============================================================

class TestPublicationDecision:
    def test_province_and_region_evaluated_separately(self, scenario):
        decision = evaluate_publication(scenario)
        provinces = {row.province: row for row in decision.provinces}
        regions = {row.region: row for row in decision.regions}

        assert provinces["丙"].eligible is True
        assert provinces["甲"].eligible is False
        assert provinces["乙"].eligible is False

        # 北一区合计 6 达标，但因存在低样本省份而冲突。
        assert regions["北一区"].eligible is True
        assert regions["北一区"].conflict is True
        assert regions["北一区"].rollup_allowed is False
        assert regions["北二区"].conflict is False

    def test_north_region_conflict_is_pending_not_published(self, scenario):
        decision = evaluate_publication(scenario)
        assert decision.pending_confirmations == ("北一区",)
        assert decision.allowed_regions == ()
        assert decision.allowed_provinces == (("北二区", "丙"),)

    def test_approval_allows_region_rollup(self, scenario):
        decision = evaluate_publication(scenario, approved_regions=["北一区"])
        assert decision.allowed_regions == ("北一区",)
        # 区级汇总后不再单独披露该区省份。
        assert decision.allowed_provinces == (("北二区", "丙"),)
        assert decision.pending_confirmations == ()

    def test_approval_of_ineligible_region_does_not_grant_access(self):
        orders = [
            _order(f"A{i}", "甲", 10.0, region="北三区") for i in range(2)
        ]
        decision = evaluate_publication(orders, approved_regions=["北三区"])
        assert decision.allowed_regions == ()
        region = decision.regions[0]
        assert region.eligible is False
        assert region.rollup_allowed is False

    def test_no_conflict_when_all_provinces_pass(self):
        orders = [
            _order(f"A{i}", "甲", 10.0) for i in range(5)
        ] + [
            _order(f"B{i}", "乙", 10.0) for i in range(5)
        ]
        decision = evaluate_publication(orders)
        region = decision.regions[0]
        assert region.conflict is False
        assert region.reason == "province_detail_preferred"
        assert decision.allowed_regions == ()

    def test_custom_region_threshold_blocks_rollup(self, scenario):
        policy = RegionPublicationPolicy(province_min_orders=5, region_min_orders=7)
        decision = evaluate_publication(scenario, policy=policy, approved_regions=["北一区"])
        region = next(row for row in decision.regions if row.region == "北一区")
        assert region.eligible is False
        assert region.rollup_allowed is False
        assert decision.allowed_regions == ()


# ============================================================
# 外部报告披露
# ============================================================

class TestExternalReport:
    def test_default_report_only_contains_bing(self, scenario):
        decision = evaluate_publication(scenario)
        report = build_external_report(scenario, decision)

        assert report["included_regions"] == ["北二区"]
        assert report["included_provinces"] == ["丙"]
        assert [row["label"] for row in report["rows"]] == ["丙"]
        assert report["totals"] == {"order_count": 8, "amount": 1000.0}
        assert report["pending_confirmations"] == ["北一区"]

    def test_bing_amount_is_1000(self, scenario):
        decision = evaluate_publication(scenario)
        report = build_external_report(scenario, decision)
        bing = next(row for row in report["rows"] if row["label"] == "丙")
        assert bing["amount"] == 1000.0
        assert bing["order_count"] == 8
        assert report["category_totals"][0]["amount"] == 1000.0

    def test_suppressed_entries_carry_no_metrics(self, scenario):
        decision = evaluate_publication(scenario)
        report = build_external_report(scenario, decision)
        assert report["suppressed"]
        for item in report["suppressed"]:
            assert set(item) == {"level", "region", "label", "reason"}

    def test_under_threshold_values_absent_everywhere(self, scenario):
        decision = evaluate_publication(scenario)
        report = build_external_report(scenario, decision)
        numbers = set()

        def collect(value):
            if isinstance(value, bool):
                return
            if isinstance(value, (int, float)):
                numbers.add(float(value))
            elif isinstance(value, dict):
                for nested in value.values():
                    collect(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested)

        collect(report)
        # 甲 400、乙 100、北一区 500、6 笔、2 笔、4 笔均不得出现。
        forbidden = {400.0, 100.0, 500.0, 6.0, 2.0, 4.0}
        assert numbers.isdisjoint(forbidden)
        assert 1000.0 in numbers

    def test_tampered_orders_cannot_ride_on_old_decision(self, scenario):
        decision = evaluate_publication(scenario, approved_regions=["北一区"])
        tampered = [dict(row) for row in scenario]
        tampered[0]["amount"] = 999.0
        with pytest.raises(ValueError):
            build_external_report(tampered, decision)

    def test_raw_orders_not_echoed(self, scenario):
        decision = evaluate_publication(scenario)
        report = build_external_report(scenario, decision)
        assert "orders" not in report
        assert "input" not in report

    def test_approved_report_uses_region_total_only(self, scenario):
        decision = evaluate_publication(scenario, approved_regions=["北一区"])
        report = build_external_report(scenario, decision)

        region_rows = [row for row in report["rows"] if row["region"] == "北一区"]
        assert len(region_rows) == 1
        assert region_rows[0]["level"] == "region"
        assert region_rows[0]["order_count"] == 6
        assert region_rows[0]["amount"] == 500.0
        # 北一区没有省明细行，无法与区合计相减反推。
        assert not any(
            row["region"] == "北一区" and row["level"] == "province"
            for row in report["rows"]
        )
        assert report["totals"] == {"order_count": 14, "amount": 1500.0}

    def test_region_totals_recomputed_from_disclosed_rows(self, scenario):
        decision = evaluate_publication(scenario)
        report = build_external_report(scenario, decision)
        for region, total in report["region_totals"].items():
            rows = [row for row in report["rows"] if row["region"] == region]
            assert total["order_count"] == sum(row["order_count"] for row in rows)
            assert abs(total["amount"] - sum(row["amount"] for row in rows)) < 1e-9

    def test_report_is_deterministic(self, scenario):
        decision = evaluate_publication(scenario)
        first = build_external_report(scenario, decision)
        second = build_external_report(scenario, decision)
        assert first == second


# ============================================================
# 幂等发布
# ============================================================

class TestPublicationRegistry:
    def test_same_scope_publishes_once(self, scenario):
        registry = PublicationRegistry()
        record, created = registry.publish("scope-a", scenario)
        assert created is True
        again, created_again = registry.publish("scope-a", scenario)
        assert created_again is False
        assert again.fingerprint == record.fingerprint
        assert len(registry.all_records()) == 1

    def test_resend_same_orders_is_idempotent(self, scenario, scenario_with_resend):
        registry = PublicationRegistry()
        registry.publish("scope-b", scenario)
        # 重送已有订单 + 补送已取消订单，内容等价，仍是同一条记录。
        record, created = registry.publish("scope-b", scenario_with_resend)
        assert created is False
        assert record.report["totals"] == {"order_count": 8, "amount": 1000.0}

    def test_conflicting_version_is_rejected(self, scenario):
        registry = PublicationRegistry()
        registry.publish("scope-c", scenario)
        changed = [dict(row) for row in scenario]
        changed[0]["amount"] = 999.0
        with pytest.raises(PublicationConflict):
            registry.publish("scope-c", changed)

    def test_concurrent_publish_only_one_record(self, scenario):
        registry = PublicationRegistry()
        results: list[bool] = []
        errors: list[BaseException] = []
        barrier = threading.Barrier(12)

        def worker():
            try:
                barrier.wait()
                _, created = registry.publish("scope-d", scenario)
                results.append(created)
            except BaseException as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert results.count(True) == 1
        assert results.count(False) == 11
        assert len(registry.all_records()) == 1

    def test_approval_change_is_a_new_version(self, scenario):
        registry = PublicationRegistry()
        registry.publish("scope-e", scenario)
        with pytest.raises(PublicationConflict):
            registry.publish("scope-e", scenario, approved_regions=["北一区"])


# ============================================================
# 跨模块管道
# ============================================================

class TestRegionPipeline:
    def test_pipeline_matches_direct_result(self, scenario):
        dimensions = [
            DimensionDef(
                dimension_id="D_REGION",
                dimension_name="地区",
                field_source="region",
                hierarchy_levels=["region", "province"],
            )
        ]
        metric = MetricDef(
            metric_id="M_GMV",
            metric_name="地区交易额",
            calculation_formula="SUM(amount)",
            aggregation=AggregationMethod.SUM,
            data_source=MetricDataSource.DORIS,
            period=MetricPeriod.DAILY,
            owner="analyst",
            field_name="amount",
            table_name="orders",
        )
        templates = [ReportTemplate(template_id="RPT", report_name="地区周报", referenced_metrics=[metric])]

        result = pipeline_region_publication(scenario, dimensions, templates)
        assert result["region_dimension"] == "地区"
        assert result["report_template"] == "地区周报"
        assert result["report"]["totals"] == {"order_count": 8, "amount": 1000.0}
        assert result["decision"]["pending_confirmations"] == ["北一区"]
