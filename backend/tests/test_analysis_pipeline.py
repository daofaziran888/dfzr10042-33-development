"""洞察工坊 · 端到端分析链路测试

测试全量 10 条跨模块数据流的确定性和正确性。
"""
from __future__ import annotations

import pytest

from app.models import (
    ABExperiment,
    AggregationMethod,
    AnomalyAlgorithm,
    AnomalyDetect,
    AttributionAlgorithm,
    AttributionModel,
    DatasetJoin,
    DimensionDef,
    EventTrack,
    ExperimentStatus,
    FunnelDef,
    JoinType,
    MetricDataSource,
    MetricDef,
    MetricPeriod,
    OlapQuery,
    RankingBoard,
    ReportTemplate,
    Segment,
)
from app.analysis_pipeline import (
    pipeline_anomaly_to_scheduled,
    pipeline_attribution_to_funnel,
    pipeline_dimension_drill,
    pipeline_event_to_segment,
    pipeline_experiment_to_compare,
    pipeline_join_to_report,
    pipeline_metric_to_dashboard,
    pipeline_query_to_snapshot,
    pipeline_ranking_to_metric,
    pipeline_segment_to_cohort,
    run_full_analysis_pipeline,
)


# ============================================================
# 共享测试数据
# ============================================================

@pytest.fixture
def sample_metrics():
    return [
        MetricDef(
            metric_id="M_DAU",
            metric_name="日活跃用户",
            calculation_formula="COUNT(DISTINCT user_id) WHERE date=today",
            aggregation=AggregationMethod.COUNT_DISTINCT,
            data_source=MetricDataSource.CLICKHOUSE,
            period=MetricPeriod.DAILY,
            owner="analyst1",
            field_name="user_id",
            table_name="user_events",
            dimension_keys=["platform", "channel"],
        ),
        MetricDef(
            metric_id="M_GMV",
            metric_name="总交易额",
            calculation_formula="SUM(order_amount)",
            aggregation=AggregationMethod.SUM,
            data_source=MetricDataSource.DORIS,
            period=MetricPeriod.DAILY,
            owner="analyst2",
            field_name="order_amount",
            table_name="orders",
            dimension_keys=["category", "region"],
        ),
        MetricDef(
            metric_id="M_CONV",
            metric_name="转化率",
            calculation_formula="COUNT(conversions)/COUNT(impressions)",
            aggregation=AggregationMethod.AVG,
            data_source=MetricDataSource.CLICKHOUSE,
            period=MetricPeriod.WEEKLY,
            owner="analyst3",
            field_name="conversion_rate",
            table_name="event_analytics",
            dimension_keys=["funnel_step"],
        ),
    ]


@pytest.fixture
def sample_dimensions():
    return [
        DimensionDef(
            dimension_id="D_PLATFORM",
            dimension_name="平台",
            field_source="platform",
            hierarchy_levels=["platform", "app_version", "device_type"],
            enum_cardinality=5,
            linked_metric_count=3,
        ),
        DimensionDef(
            dimension_id="D_REGION",
            dimension_name="地区",
            field_source="region",
            hierarchy_levels=["region", "city", "district"],
            enum_cardinality=50,
            linked_metric_count=4,
        ),
        DimensionDef(
            dimension_id="D_CATEGORY",
            dimension_name="品类",
            field_source="category",
            hierarchy_levels=["category", "subcategory", "sku"],
            enum_cardinality=200,
            linked_metric_count=2,
        ),
    ]


@pytest.fixture
def sample_events():
    return [
        EventTrack(
            event_id="E_REGISTER",
            event_name="用户注册",
            event_properties={"source": "organic", "channel": "wechat"},
            trigger_condition="event='register'",
            attribution_key="user_id",
        ),
        EventTrack(
            event_id="E_PURCHASE",
            event_name="支付成功",
            event_properties={"source": "paid", "channel": "app"},
            trigger_condition="event='purchase' AND amount>0",
            attribution_key="order_id",
        ),
        EventTrack(
            event_id="E_RETENTION",
            event_name="7日回访",
            event_properties={"source": "retained", "channel": "push"},
            trigger_condition="event='open_app' AND days_since_register<=7",
            attribution_key="user_id",
        ),
    ]


@pytest.fixture
def sample_experiments():
    return [
        ABExperiment(
            experiment_id="EXP_UI",
            experiment_name="首页UI优化实验",
            groups={"control": 0.5, "treatment": 0.5},
            split_ratio={"control": 0.5, "treatment": 0.5},
            evaluation_metrics=[],
            min_sample_size=1000,
            status=ExperimentStatus.RUNNING,
        ),
        ABExperiment(
            experiment_id="EXP_ALGO",
            experiment_name="推荐算法升级实验",
            groups={"control": 0.3, "treatment_a": 0.35, "treatment_b": 0.35},
            split_ratio={"control": 0.3, "treatment_a": 0.35, "treatment_b": 0.35},
            evaluation_metrics=[],
            min_sample_size=2000,
            status=ExperimentStatus.RUNNING,
        ),
    ]


@pytest.fixture
def sample_attribution_models(sample_events):
    return [
        AttributionModel(
            model_id="ATTR_FIRST",
            model_name="首次触点归因",
            algorithm=AttributionAlgorithm.FIRST_TOUCH,
            attribution_window_days=30,
            denominator_event=sample_events[0],
            numerator_event=sample_events[1],
        ),
        AttributionModel(
            model_id="ATTR_LAST",
            model_name="末次触点归因",
            algorithm=AttributionAlgorithm.LAST_TOUCH,
            attribution_window_days=14,
            denominator_event=sample_events[0],
            numerator_event=sample_events[2],
        ),
        AttributionModel(
            model_id="ATTR_LINEAR",
            model_name="线性归因",
            algorithm=AttributionAlgorithm.LINEAR,
            attribution_window_days=30,
            denominator_event=sample_events[0],
            numerator_event=sample_events[1],
        ),
    ]


@pytest.fixture
def sample_anomaly_rules(sample_metrics):
    return [
        AnomalyDetect(
            rule_id="ANOM_DAU",
            target_metric=sample_metrics[0],
            algorithm=AnomalyAlgorithm.IQR,
            baseline_period_days=30,
            threshold_multiplier=3.0,
            sensitivity=0.05,
        ),
        AnomalyDetect(
            rule_id="ANOM_GMV",
            target_metric=sample_metrics[1],
            algorithm=AnomalyAlgorithm.ZSCORE,
            baseline_period_days=14,
            threshold_multiplier=2.5,
            sensitivity=0.05,
        ),
        AnomalyDetect(
            rule_id="ANOM_CONV",
            target_metric=sample_metrics[2],
            algorithm=AnomalyAlgorithm.MOVING_AVERAGE,
            baseline_period_days=7,
            threshold_multiplier=3.0,
            sensitivity=0.05,
        ),
    ]


@pytest.fixture
def sample_segments(sample_events):
    return [
        Segment(
            segment_id="SEG_ORGANIC",
            segment_name="自然用户群体",
            conditions={"source": "organic"},
            member_count=5000,
            bound_events=[sample_events[0]],
        ),
        Segment(
            segment_id="SEG_PAID",
            segment_name="付费用户群体",
            conditions={"source": "paid"},
            member_count=3000,
            bound_events=[sample_events[1]],
        ),
    ]


@pytest.fixture
def sample_ranking_boards(sample_metrics, sample_dimensions):
    return [
        RankingBoard(
            rank_id="RANK_GMV",
            rank_metric=sample_metrics[1],
            rank_dimensions=[sample_dimensions[2]],
            time_range="2026-11-01",
            top_n=10,
            update_frequency="daily",
        ),
    ]


@pytest.fixture
def sample_joins():
    return [
        DatasetJoin(
            join_id="JOIN_ORDER_USER",
            primary_dataset="orders",
            join_dataset="users",
            join_keys=["user_id"],
            join_type=JoinType.INNER,
            field_mapping={"user_name": "order_user_name", "user_region": "order_region"},
        ),
        DatasetJoin(
            join_id="JOIN_EVENT_PRODUCT",
            primary_dataset="events",
            join_dataset="products",
            join_keys=["product_id"],
            join_type=JoinType.LEFT,
            field_mapping={"product_name": "event_product_name", "category": "event_category"},
        ),
    ]


@pytest.fixture
def sample_report_templates(sample_metrics):
    return [
        ReportTemplate(
            template_id="RPT_DAILY",
            report_name="日报模板",
            referenced_metrics=[sample_metrics[0], sample_metrics[1]],
            layout_format="pdf",
            generation_period="daily",
            push_channel="email",
        ),
    ]


@pytest.fixture
def sample_data():
    return {
        "metric_rows": [
            {"user_id": "u1", "platform": "ios", "channel": "organic", "order_amount": 100.0, "conversion_rate": 0.12},
            {"user_id": "u2", "platform": "android", "channel": "paid", "order_amount": 200.0, "conversion_rate": 0.08},
            {"user_id": "u3", "platform": "ios", "channel": "organic", "order_amount": 150.0, "conversion_rate": 0.15},
            {"user_id": "u4", "platform": "android", "channel": "paid", "order_amount": 300.0, "conversion_rate": 0.10},
            {"user_id": "u5", "platform": "ios", "channel": "wechat", "order_amount": 250.0, "conversion_rate": 0.14},
        ],
        "users": [
            {"user_id": "u1", "source": "organic", "channel": "wechat", "age_group": "18-25"},
            {"user_id": "u2", "source": "paid", "channel": "app", "age_group": "26-35"},
            {"user_id": "u3", "source": "organic", "channel": "wechat", "age_group": "18-25"},
            {"user_id": "u4", "source": "paid", "channel": "app", "age_group": "36-45"},
            {"user_id": "u5", "source": "organic", "channel": "wechat", "age_group": "18-25"},
        ],
        "control_data": {
            "EXP_UI": [0.12, 0.08, 0.15, 0.10, 0.14, 0.11, 0.13, 0.09],
            "EXP_ALGO": [200.0, 180.0, 220.0, 190.0, 210.0, 195.0, 205.0, 215.0],
        },
        "treatment_data": {
            "EXP_UI": [0.18, 0.14, 0.19, 0.16, 0.17, 0.15, 0.20, 0.18],
            "EXP_ALGO": [250.0, 230.0, 270.0, 240.0, 260.0, 245.0, 255.0, 265.0],
        },
        "touchpoints": [
            {"user_id": "u1", "channel": "organic", "timestamp": "2026-11-01T10:00:00Z"},
            {"user_id": "u1", "channel": "paid", "timestamp": "2026-11-01T14:00:00Z"},
            {"user_id": "u2", "channel": "paid", "timestamp": "2026-11-01T09:00:00Z"},
            {"user_id": "u2", "channel": "organic", "timestamp": "2026-11-01T11:00:00Z"},
            {"user_id": "u3", "channel": "wechat", "timestamp": "2026-11-01T08:00:00Z"},
            {"user_id": "u3", "channel": "paid", "timestamp": "2026-11-01T15:00:00Z"},
        ],
        "conversions": [
            {"user_id": "u1", "conversion_time": "2026-11-01T15:00:00Z"},
            {"user_id": "u2", "conversion_time": "2026-11-01T12:00:00Z"},
            {"user_id": "u3", "conversion_time": "2026-11-01T16:00:00Z"},
        ],
        "user_events_funnel": {
            "u1": [
                {"event_name": "用户注册", "timestamp": "2026-11-01T10:00:00"},
                {"event_name": "支付成功", "timestamp": "2026-11-01T14:00:00"},
            ],
            "u2": [
                {"event_name": "用户注册", "timestamp": "2026-11-01T09:00:00"},
                {"event_name": "支付成功", "timestamp": "2026-11-01T15:00:00"},
            ],
            "u3": [
                {"event_name": "用户注册", "timestamp": "2026-11-01T08:00:00"},
            ],
            "u4": [
                {"event_name": "用户注册", "timestamp": "2026-11-01T10:30:00"},
                {"event_name": "支付成功", "timestamp": "2026-11-01T16:00:00"},
                {"event_name": "7日回访", "timestamp": "2026-11-07T10:00:00"},
            ],
            "u5": [
                {"event_name": "用户注册", "timestamp": "2026-11-01T11:00:00"},
                {"event_name": "支付成功", "timestamp": "2026-11-01T18:00:00"},
            ],
        },
        "user_events_cohort": {
            "user_1": ["1", "3", "7", "14", "30"],
            "user_2": ["1", "7"],
            "user_3": ["1", "3", "7", "14", "30"],
            "user_4": ["1", "3", "7"],
            "user_5": ["1", "3", "7", "14"],
        },
        "time_series": {
            "M_DAU": [1000.0, 1050.0, 980.0, 1020.0, 1100.0, 1080.0, 950.0,
                      1030.0, 1070.0, 1090.0, 5000.0, 1010.0, 1020.0, 1040.0],
            "M_GMV": [10000.0, 10500.0, 9800.0, 10200.0, 11000.0, 10800.0,
                      9500.0, 10300.0, 10700.0, 10900.0, 10100.0, 10200.0, 10400.0, 30000.0],
            "M_CONV": [0.05, 0.06, 0.04, 0.05, 0.07, 0.06, 0.04, 0.05, 0.06, 0.06,
                       0.05, 0.05, 0.06, 0.20],
        },
        "datasets": {
            "orders": [
                {"order_id": "o1", "user_id": "u1", "amount": 100.0},
                {"order_id": "o2", "user_id": "u2", "amount": 200.0},
                {"order_id": "o3", "user_id": "u3", "amount": 150.0},
            ],
            "users": [
                {"user_id": "u1", "user_name": "Alice", "user_region": "beijing"},
                {"user_id": "u2", "user_name": "Bob", "user_region": "shanghai"},
                {"user_id": "u3", "user_name": "Charlie", "user_region": "beijing"},
            ],
            "events": [
                {"event_id": "e1", "product_id": "p1", "event_type": "view"},
                {"event_id": "e2", "product_id": "p2", "event_type": "click"},
            ],
            "products": [
                {"product_id": "p1", "product_name": "商品A", "category": "服装"},
                {"product_id": "p2", "product_name": "商品B", "category": "食品"},
            ],
        },
        "ranking_data": {
            "RANK_GMV": [
                {"category": "服装", "subcategory": "女装", "order_amount": 15000.0},
                {"category": "食品", "subcategory": "零食", "order_amount": 12000.0},
                {"category": "数码", "subcategory": "手机", "order_amount": 25000.0},
                {"category": "服装", "subcategory": "男装", "order_amount": 9000.0},
                {"category": "图书", "subcategory": "科技", "order_amount": 5000.0},
                {"category": "家电", "subcategory": "空调", "order_amount": 18000.0},
                {"category": "食品", "subcategory": "饮品", "order_amount": 8000.0},
                {"category": "数码", "subcategory": "配件", "order_amount": 11000.0},
                {"category": "服装", "subcategory": "童装", "order_amount": 7000.0},
                {"category": "图书", "subcategory": "文学", "order_amount": 6000.0},
                {"category": "家电", "subcategory": "冰箱", "order_amount": 14000.0},
                {"category": "食品", "subcategory": "乳制品", "order_amount": 10000.0},
            ],
        },
    }


# ============================================================
# 流 1: metric_def -> olap_query -> query_cache -> dashboard_def
# ============================================================

class TestFlow1MetricToDashboard:
    def test_builds_dashboard_from_metrics(self, sample_metrics, sample_dimensions, sample_data):
        result = pipeline_metric_to_dashboard(
            sample_metrics, sample_dimensions, sample_data["metric_rows"]
        )
        assert result["pipeline"] == "metric_def -> olap_query -> query_cache -> dashboard_def"
        assert len(result["dashboards"]) == 3
        assert result["total_cached_queries"] == 3

    def test_dashboard_has_chart_data(self, sample_metrics, sample_dimensions, sample_data):
        result = pipeline_metric_to_dashboard(
            sample_metrics, sample_dimensions, sample_data["metric_rows"]
        )
        dau_dash = result["dashboards"][0]
        assert dau_dash["metric_name"] == "日活跃用户"
        assert dau_dash["computed_value"] == 5.0  # COUNT_DISTINCT of 5 user_ids in 5 rows
        assert "SELECT" in dau_dash["sql"]

    def test_gmv_sum_computation(self, sample_metrics, sample_dimensions, sample_data):
        result = pipeline_metric_to_dashboard(
            sample_metrics, sample_dimensions, sample_data["metric_rows"]
        )
        gmv_dash = result["dashboards"][1]
        assert gmv_dash["metric_name"] == "总交易额"
        assert gmv_dash["computed_value"] == 1000.0  # 100+200+150+300+250

    def test_sql_is_deterministic(self, sample_metrics, sample_dimensions, sample_data):
        r1 = pipeline_metric_to_dashboard(sample_metrics, sample_dimensions, sample_data["metric_rows"])
        r2 = pipeline_metric_to_dashboard(sample_metrics, sample_dimensions, sample_data["metric_rows"])
        assert r1["dashboards"][0]["sql"] == r2["dashboards"][0]["sql"]


# ============================================================
# 流 2: dimension_def -> drill_path -> olap_query
# ============================================================

class TestFlow2DimensionDrill:
    def test_generates_drill_paths(self, sample_dimensions, sample_metrics):
        result = pipeline_dimension_drill(sample_dimensions, sample_metrics)
        assert result["pipeline"] == "dimension_def -> drill_path -> olap_query"
        assert result["total_drill_paths"] == 6  # 3*2 (i!=j)

    def test_drill_has_hierarchy_info(self, sample_dimensions, sample_metrics):
        result = pipeline_dimension_drill(sample_dimensions, sample_metrics)
        first_drill = result["drill_paths"][0]
        assert first_drill["parent_dimension"] is not None
        assert first_drill["child_dimension"] is not None
        assert len(first_drill["parent_hierarchy"]) > 0
        assert "precompute_mode" in first_drill

    def test_drill_paths_are_distinct(self, sample_dimensions, sample_metrics):
        result = pipeline_dimension_drill(sample_dimensions, sample_metrics)
        path_ids = [d["drill_path_id"] for d in result["drill_paths"]]
        assert len(path_ids) == len(set(path_ids))


# ============================================================
# 流 3: event_track -> segment_builder -> user_segment
# ============================================================

class TestFlow3EventToSegment:
    def test_segments_users_by_event_properties(self, sample_events, sample_data):
        result = pipeline_event_to_segment(sample_events, sample_data["users"])
        assert result["pipeline"] == "event_track -> segment_builder -> user_segment"
        assert result["total_segments"] == 3
        assert result["total_comparisons"] == 3  # C(3,2)=3

    def test_organic_segment_has_correct_count(self, sample_events, sample_data):
        result = pipeline_event_to_segment(sample_events, sample_data["users"])
        organic_seg = result["segments"][0]
        assert organic_seg["segment_name"] == "群体_用户注册"
        # conditions = {"source": "organic", "channel": "wechat"}
        assert organic_seg["member_count"] == 3  # u1, u3, u5
        assert organic_seg["conditions_count"] == 2


# ============================================================
# 流 4: ab_experiment -> experiment_result -> metric_compare
# ============================================================

class TestFlow4ExperimentCompare:
    def test_generates_experiment_reports(self, sample_experiments, sample_metrics, sample_data):
        result = pipeline_experiment_to_compare(
            sample_experiments, sample_metrics,
            sample_data["control_data"], sample_data["treatment_data"]
        )
        assert result["pipeline"] == "ab_experiment -> experiment_result -> metric_compare"
        assert result["total_reports"] == 6  # 2 experiments * 3 metrics

    def test_ui_experiment_shows_lift(self, sample_experiments, sample_metrics, sample_data):
        result = pipeline_experiment_to_compare(
            sample_experiments, sample_metrics,
            sample_data["control_data"], sample_data["treatment_data"]
        )
        ui_dau = result["comparisons"]["首页UI优化实验__日活跃用户"]
        # treatment data is higher than control
        assert ui_dau["lift"] > 0
        assert "lift_pct" in ui_dau

    def test_experiment_result_is_deterministic(self, sample_experiments, sample_metrics, sample_data):
        r1 = pipeline_experiment_to_compare(
            sample_experiments, sample_metrics,
            sample_data["control_data"], sample_data["treatment_data"]
        )
        r2 = pipeline_experiment_to_compare(
            sample_experiments, sample_metrics,
            sample_data["control_data"], sample_data["treatment_data"]
        )
        assert r1["comparisons"] == r2["comparisons"]


# ============================================================
# 流 5: attribution_model -> event_track -> funnel_def
# ============================================================

class TestFlow5AttributionFunnel:
    def test_attribution_has_channel_weights(self, sample_attribution_models, sample_data):
        result = pipeline_attribution_to_funnel(
            sample_attribution_models,
            sample_data["touchpoints"],
            sample_data["conversions"],
            sample_data["user_events_funnel"],
        )
        assert result["pipeline"] == "attribution_model -> event_track -> funnel_def"
        assert len(result["attributions"]) == 3

    def test_attribution_weights_sum_to_1(self, sample_attribution_models, sample_data):
        result = pipeline_attribution_to_funnel(
            sample_attribution_models,
            sample_data["touchpoints"],
            sample_data["conversions"],
            sample_data["user_events_funnel"],
        )
        for attr in result["attributions"]:
            total = list(attr["channel_weights"].values())
            if total:
                assert abs(sum(total) - 1.0) < 0.01

    def test_funnel_is_computed(self, sample_attribution_models, sample_data):
        result = pipeline_attribution_to_funnel(
            sample_attribution_models,
            sample_data["touchpoints"],
            sample_data["conversions"],
            sample_data["user_events_funnel"],
        )
        assert len(result["funnels"]) > 0
        funnel = result["funnels"][0]
        assert "step_counts" in funnel
        assert "total_conversion_rate" in funnel


# ============================================================
# 流 6: segment_builder -> cohort_analysis
# ============================================================

class TestFlow6SegmentCohort:
    def test_computes_retention_curve(self, sample_segments, sample_data):
        result = pipeline_segment_to_cohort(
            sample_segments, sample_data["user_events_cohort"]
        )
        assert result["pipeline"] == "segment_builder -> cohort_analysis"
        assert result["total_cohorts"] == 2

    def test_retention_decreases_over_time(self, sample_segments, sample_data):
        result = pipeline_segment_to_cohort(
            sample_segments, sample_data["user_events_cohort"]
        )
        for cohort in result["cohorts"]:
            curve = cohort["retention_curve"]
            for i in range(len(curve) - 1):
                assert curve[i + 1] <= curve[i] + 0.01  # may equal

    def test_retention_values_in_range(self, sample_segments, sample_data):
        result = pipeline_segment_to_cohort(
            sample_segments, sample_data["user_events_cohort"]
        )
        for cohort in result["cohorts"]:
            for val in cohort["retention_curve"]:
                assert 0.0 <= val <= 1.0


# ============================================================
# 流 7: anomaly_detect -> metric_def -> scheduled_analysis
# ============================================================

class TestFlow7AnomalyScheduled:
    def test_detects_anomalies(self, sample_anomaly_rules, sample_data):
        result = pipeline_anomaly_to_scheduled(
            sample_anomaly_rules, sample_data["time_series"]
        )
        assert result["pipeline"] == "anomaly_detect -> metric_def -> scheduled_analysis"
        assert result["total_rules"] == 3

    def test_dau_anomaly_detected(self, sample_anomaly_rules, sample_data):
        result = pipeline_anomaly_to_scheduled(
            sample_anomaly_rules, sample_data["time_series"]
        )
        dau_result = result["schedule_results"][0]
        assert dau_result["metric_name"] == "日活跃用户"
        assert dau_result["anomalous_points"] > 0  # 5000 is anomalous

    def test_suggests_cron_based_on_anomaly_rate(self, sample_anomaly_rules, sample_data):
        result = pipeline_anomaly_to_scheduled(
            sample_anomaly_rules, sample_data["time_series"]
        )
        for sched in result["schedule_results"]:
            assert "suggested_cron" in sched
            assert len(sched["suggested_cron"]) > 5


# ============================================================
# 流 8: olap_query -> data_export -> data_snapshot
# ============================================================

class TestFlow8QuerySnapshot:
    def test_creates_snapshots_from_queries(self, sample_metrics, sample_dimensions, sample_data):
        queries = [
            OlapQuery(
                query_id="Q1",
                query_name="DAU查询",
                metrics=[sample_metrics[0]],
                dimensions=sample_dimensions[:1],
            )
        ]
        query_data = {"Q1": sample_data["metric_rows"]}
        result = pipeline_query_to_snapshot(queries, query_data)
        assert result["pipeline"] == "olap_query -> data_export -> data_snapshot"
        assert len(result["snapshots"]) == 1
        assert result["snapshots"][0]["export_rows"] == 5


# ============================================================
# 流 9: dataset_join -> olap_query -> report_builder
# ============================================================

class TestFlow9JoinReport:
    def test_joins_datasets(self, sample_joins, sample_data, sample_report_templates):
        result = pipeline_join_to_report(
            sample_joins, sample_data["datasets"], sample_report_templates
        )
        assert result["pipeline"] == "dataset_join -> olap_query -> report_builder"
        assert len(result["joins"]) == 2

    def test_join_order_user_has_results(self, sample_joins, sample_data, sample_report_templates):
        result = pipeline_join_to_report(
            sample_joins, sample_data["datasets"], sample_report_templates
        )
        order_join = result["joins"][0]
        assert order_join["primary_dataset"] == "orders"
        assert order_join["result_rows"] == 3  # 3 orders matched to 3 users

    def test_generates_reports(self, sample_joins, sample_data, sample_report_templates):
        result = pipeline_join_to_report(
            sample_joins, sample_data["datasets"], sample_report_templates
        )
        assert len(result["reports"]) == 1
        assert result["reports"][0]["metric_count"] > 0


# ============================================================
# 流 10: ranking_board -> metric_def
# ============================================================

class TestFlow10RankingMetric:
    def test_computes_top_n_ranking(self, sample_ranking_boards, sample_data):
        result = pipeline_ranking_to_metric(
            sample_ranking_boards, sample_data["ranking_data"]
        )
        assert result["pipeline"] == "ranking_board -> metric_def"
        assert result["total_ranking_boards"] == 1

    def test_ranking_returns_top_3(self, sample_ranking_boards, sample_data):
        result = pipeline_ranking_to_metric(
            sample_ranking_boards, sample_data["ranking_data"]
        )
        ranking = result["rankings"][0]
        assert len(ranking["top_3"]) == 3
        # Top should be 数码/手机 = 25000
        assert ranking["top_3"][0]["value"] == 25000.0

    def test_ranking_is_descending(self, sample_ranking_boards, sample_data):
        result = pipeline_ranking_to_metric(
            sample_ranking_boards, sample_data["ranking_data"]
        )
        ranking = result["rankings"][0]
        top3_vals = [item["value"] for item in ranking["top_3"]]
        assert top3_vals == sorted(top3_vals, reverse=True)


# ============================================================
# 全量管道测试
# ============================================================

class TestFullPipeline:
    def test_runs_all_flows(self, sample_metrics, sample_dimensions, sample_events,
                            sample_experiments, sample_attribution_models,
                            sample_anomaly_rules, sample_segments, sample_ranking_boards,
                            sample_joins, sample_report_templates, sample_data):
        result = run_full_analysis_pipeline(
            metrics=sample_metrics,
            dimensions=sample_dimensions,
            events=sample_events,
            experiments=sample_experiments,
            attribution_models=sample_attribution_models,
            anomaly_rules=sample_anomaly_rules,
            segments=sample_segments,
            ranking_boards=sample_ranking_boards,
            joins=sample_joins,
            report_templates=sample_report_templates,
            sample_data=sample_data,
        )
        assert "flow1_dashboard" in result
        assert "flow2_drill" in result
        assert "flow3_segment" in result
        assert "flow4_experiment" in result
        assert "flow5_attribution" in result
        assert "flow6_cohort" in result
        assert "flow7_anomaly" in result
        assert "flow9_report" in result
        assert "flow10_ranking" in result

    def test_region_flow_runs_when_region_orders_present(
        self, sample_metrics, sample_dimensions, sample_events,
        sample_experiments, sample_attribution_models,
        sample_anomaly_rules, sample_segments, sample_ranking_boards,
        sample_joins, sample_report_templates, sample_data,
    ):
        from app.region_samples import scenario_orders
        enriched = dict(sample_data)
        enriched["region_orders"] = scenario_orders()
        result = run_full_analysis_pipeline(
            metrics=sample_metrics, dimensions=sample_dimensions, events=sample_events,
            experiments=sample_experiments, attribution_models=sample_attribution_models,
            anomaly_rules=sample_anomaly_rules, segments=sample_segments,
            ranking_boards=sample_ranking_boards, joins=sample_joins,
            report_templates=sample_report_templates, sample_data=enriched,
        )
        flow = result["flow11_region_publication"]
        assert flow["report"]["totals"] == {"order_count": 8, "amount": 1000.0}
        assert flow["decision"]["pending_confirmations"] == ["北一区"]

    def test_full_pipeline_is_deterministic(self, sample_metrics, sample_dimensions, sample_events,
                                            sample_experiments, sample_attribution_models,
                                            sample_anomaly_rules, sample_segments, sample_ranking_boards,
                                            sample_joins, sample_report_templates, sample_data):
        r1 = run_full_analysis_pipeline(
            metrics=sample_metrics, dimensions=sample_dimensions, events=sample_events,
            experiments=sample_experiments, attribution_models=sample_attribution_models,
            anomaly_rules=sample_anomaly_rules, segments=sample_segments,
            ranking_boards=sample_ranking_boards, joins=sample_joins,
            report_templates=sample_report_templates, sample_data=sample_data,
        )
        r2 = run_full_analysis_pipeline(
            metrics=sample_metrics, dimensions=sample_dimensions, events=sample_events,
            experiments=sample_experiments, attribution_models=sample_attribution_models,
            anomaly_rules=sample_anomaly_rules, segments=sample_segments,
            ranking_boards=sample_ranking_boards, joins=sample_joins,
            report_templates=sample_report_templates, sample_data=sample_data,
        )
        assert r1 == r2