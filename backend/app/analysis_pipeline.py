"""洞察工坊 · 跨模块分析管道

实现 10 条关键跨模块数据流：
1. metric_def -> olap_query -> query_cache -> dashboard_def
2. dimension_def -> drill_path -> olap_query
3. event_track -> segment_builder -> user_segment
4. ab_experiment -> experiment_result -> metric_compare
5. attribution_model -> event_track -> funnel_def
6. segment_builder -> cohort_analysis
7. anomaly_detect -> metric_def -> scheduled_analysis
8. olap_query -> data_export -> data_snapshot
9. dataset_join -> olap_query -> report_builder
10. ranking_board -> metric_def

另有流 11：dimension_def -> drill_path -> report_builder 的地区低样本发布门槛，
在传入 ``region_orders`` 时启用。

所有管道函数都是确定性纯函数，可直接用于测试。
"""
from __future__ import annotations

from typing import Any

from app.models import (
    ABExperiment,
    AggregationMethod,
    AnomalyAlgorithm,
    AnomalyDetect,
    AttributionAlgorithm,
    AttributionModel,
    CohortAnalysis,
    DashboardDef,
    DataExport,
    DataSnapshot,
    DatasetJoin,
    DimensionDef,
    DrillPath,
    EventTrack,
    ExperimentResult,
    FunnelDef,
    JoinType,
    MetricDef,
    MetricPeriod,
    OlapQuery,
    QueryCache,
    RankingBoard,
    ReportTemplate,
    Segment,
    UserSegmentCompare,
)
from app.region_publication import (
    RegionPublicationPolicy,
    build_external_report,
    evaluate_publication,
)


# ============================================================
# 流 1: metric_def -> olap_query -> query_cache -> dashboard_def
# 指标定义 -> 多维查询 -> 查询缓存 -> 看板定义
# ============================================================

def pipeline_metric_to_dashboard(
    metrics: list[MetricDef],
    dimensions: list[DimensionDef],
    sample_data: list[dict[str, Any]],
) -> dict[str, Any]:
    """完整的指标到看板分析管道。

    1. 从指标定义构建 OLAP 查询
    2. 执行查询并缓存结果
    3. 组装看板数据
    """
    cached_results = {}
    dashboards = []

    for metric in metrics:
        metric_val = metric.compute(sample_data)

        # 构建 OLAP 查询
        query = OlapQuery(
            query_id=f"Q_{metric.metric_id}",
            query_name=f"查询_{metric.metric_name}",
            metrics=[metric],
            dimensions=dimensions,
            filters={"status": "已发布"},
            order_by=f"{metric.field_name} DESC",
            time_range=metric.period.value,
        )

        # 生成 SQL
        sql = query.build_sql()

        # 缓存查询结果
        cache = QueryCache(
            cache_id=f"CACHE_{metric.metric_id}",
            query_hash=f"hash:{sql[:50]}",
            olap_query_ref=query.query_id,
            result_rows=[{"_metric_value": metric_val}],
            ttl_seconds=3600,
            hit_count=1,
        )

        cached_results[query.query_id] = cache

        # 构建看板定义
        dashboard = DashboardDef(
            dashboard_id=f"DASH_{metric.metric_id}",
            dashboard_name=f"看板_{metric.metric_name}",
            bound_metrics=[metric],
            bound_queries=[query],
            subscribers=["analyst@example.com"],
            refresh_strategy="hourly",
            cache_ttl=3600,
        )

        dashboard_data = dashboard.collect_cached_results(list(cached_results.values()))
        dashboards.append({
            "dashboard_id": dashboard.dashboard_id,
            "dashboard_name": dashboard.dashboard_name,
            "metric_name": metric.metric_name,
            "computed_value": metric_val,
            "sql": sql,
            "cache_hit": cache.hit_count,
            "chart_count": len(dashboard_data),
        })

    return {
        "pipeline": "metric_def -> olap_query -> query_cache -> dashboard_def",
        "dashboards": dashboards,
        "total_cached_queries": len(cached_results),
    }


# ============================================================
# 流 2: dimension_def -> drill_path -> olap_query
# 维度定义 -> 下钻路径 -> 多维查询
# ============================================================

def pipeline_dimension_drill(
    dimensions: list[DimensionDef],
    metrics: list[MetricDef],
) -> dict[str, Any]:
    """维度下钻分析管道。

    1. 从维度定义构建下钻路径
    2. 将下钻路径映射到 OLAP 查询
    3. 为每对父子维度生成查询方案
    """
    drill_results = []

    for i, parent_dim in enumerate(dimensions):
        for j, child_dim in enumerate(dimensions):
            if i == j:
                continue

            # 构建下钻路径
            drill = DrillPath(
                path_id=f"DR_{parent_dim.dimension_id}_{child_dim.dimension_id}",
                parent_dimension=parent_dim,
                child_dimension=child_dim,
                transition_layers=1,
                data_granularity="day",
                precompute_mode="on_demand" if j % 2 == 0 else "precomputed",
            )

            # 映射到 OLAP 查询
            query = OlapQuery(
                query_id=f"Q_DRILL_{drill.path_id}",
                query_name=f"下钻_{parent_dim.dimension_name}->{child_dim.dimension_name}",
                metrics=metrics[:1] if metrics else [],
                dimensions=[parent_dim, child_dim],
                order_by=f"{metrics[0].field_name} DESC" if metrics else "",
            )

            drill_results.append({
                "drill_path_id": drill.path_id,
                "parent_dimension": parent_dim.dimension_name,
                "child_dimension": child_dim.dimension_name,
                "parent_hierarchy": parent_dim.hierarchy_levels,
                "child_hierarchy": child_dim.hierarchy_levels,
                "sql": query.build_sql(),
                "precompute_mode": drill.precompute_mode,
            })

    return {
        "pipeline": "dimension_def -> drill_path -> olap_query",
        "drill_paths": drill_results,
        "total_drill_paths": len(drill_results),
    }


# ============================================================
# 流 3: event_track -> segment_builder -> user_segment
# 事件追踪 -> 群体圈选 -> 分群比较
# ============================================================

def pipeline_event_to_segment(
    events: list[EventTrack],
    users: list[dict[str, Any]],
) -> dict[str, Any]:
    """事件追踪到分群比较的完整管道。

    1. 从事件定义构建群体圈选条件
    2. 圈选目标用户群体
    3. 执行跨群体比较
    """
    segments = []
    comparisons = []

    for event in events:
        # 构建群体圈选
        segment = Segment(
            segment_id=f"SEG_{event.event_id}",
            segment_name=f"群体_{event.event_name}",
            conditions=event.event_properties,
            bound_events=[event],
        )

        selected_users = segment.select_users(users)
        segments.append({
            "segment_id": segment.segment_id,
            "segment_name": segment.segment_name,
            "member_count": segment.member_count,
            "conditions_count": len(segment.conditions),
            "sample_users": selected_users[:3],
        })

    # 执行跨群体比较（两两比较）
    for i, seg_a in enumerate(segments):
        for j, seg_b in enumerate(segments):
            if i >= j:
                continue
            metric = MetricDef(
                metric_id=f"METR_COMPARE_{i}_{j}",
                metric_name="转化率",
                calculation_formula="COUNT(conversions)/COUNT(users)",
                aggregation=AggregationMethod.AVG,
                data_source="clickhouse",
                period=MetricPeriod.DAILY,
                owner="analyst",
                field_name="conversion_rate",
                table_name="user_events",
            )
            comparison = {
                "segment_a": seg_a["segment_name"],
                "segment_b": seg_b["segment_name"],
                "count_a": seg_a["member_count"],
                "count_b": seg_b["member_count"],
                "metric": metric.metric_name,
            }
            comparisons.append(comparison)

    return {
        "pipeline": "event_track -> segment_builder -> user_segment",
        "segments": segments,
        "comparisons": comparisons,
        "total_segments": len(segments),
        "total_comparisons": len(comparisons),
    }


# ============================================================
# 流 4: ab_experiment -> experiment_result -> metric_compare
# A/B实验 -> 实验分析 -> 指标对比
# ============================================================

def pipeline_experiment_to_compare(
    experiments: list[ABExperiment],
    metrics: list[MetricDef],
    control_data: dict[str, list[float]],
    treatment_data: dict[str, list[float]],
) -> dict[str, Any]:
    """A/B 实验到指标对比的完整管道。

    1. 对每个实验和指标执行统计检验
    2. 生成实验分析报告
    3. 汇总指标对比结论
    """
    results = []
    comparison_summary = {}

    for exp in experiments:
        for metric in metrics:
            exp_id = exp.experiment_id
            control = control_data.get(exp_id, [])
            treatment = treatment_data.get(exp_id, [])

            exp_result = ExperimentResult(
                report_id=f"RPT_{exp.experiment_id}_{metric.metric_id}",
                experiment=exp,
                metric=metric,
                control_data=control,
                treatment_data=treatment,
            )
            analysis = exp_result.analyze()

            key = f"{exp.experiment_name}__{metric.metric_name}"
            comparison_summary[key] = {
                "experiment_name": exp.experiment_name,
                "metric_name": metric.metric_name,
                "control_mean": analysis.control_mean,
                "treatment_mean": analysis.treatment_mean,
                "lift": analysis.lift,
                "p_value": analysis.p_value,
                "significant": analysis.significant,
                "lift_pct": f"{analysis.lift * 100:.2f}%",
            }

            results.append(analysis)

    return {
        "pipeline": "ab_experiment -> experiment_result -> metric_compare",
        "comparisons": comparison_summary,
        "total_reports": len(results),
        "significant_count": sum(1 for r in results if r.significant),
    }


# ============================================================
# 流 5: attribution_model -> event_track -> funnel_def
# 归因分析 -> 事件追踪 -> 漏斗分析
# ============================================================

def pipeline_attribution_to_funnel(
    models: list[AttributionModel],
    touchpoints: list[dict[str, Any]],
    conversions: list[dict[str, Any]],
    user_events: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """归因分析到漏斗分析的完整管道。

    1. 执行归因算法分配权重
    2. 基于归因结果关联事件追踪
    3. 构建漏斗分析
    """
    attribution_results = []
    funnel_results = []

    for model in models:
        # 执行归因
        weights = model.attribute(touchpoints, conversions)

        # 构建基于归因事件的漏斗
        if model.denominator_event and model.numerator_event:
            funnel = FunnelDef(
                funnel_id=f"FUN_{model.model_id}",
                funnel_name=f"漏斗_{model.model_name}",
                steps=[model.denominator_event, model.numerator_event],
                conversion_window_hours=model.attribution_window_days * 24,
            )
            funnel = funnel.compute_funnel(user_events)

            funnel_results.append({
                "funnel_id": funnel.funnel_id,
                "funnel_name": funnel.funnel_name,
                "step_counts": funnel.step_counts,
                "step_rates": funnel.step_rates,
                "total_conversion_rate": funnel.total_conversion_rate,
            })

        attribution_results.append({
            "model_id": model.model_id,
            "model_name": model.model_name,
            "algorithm": model.algorithm.value,
            "window_days": model.attribution_window_days,
            "channel_weights": weights,
            "total_weight": sum(weights.values()),
        })

    return {
        "pipeline": "attribution_model -> event_track -> funnel_def",
        "attributions": attribution_results,
        "funnels": funnel_results,
    }


# ============================================================
# 流 6: segment_builder -> cohort_analysis
# 群体圈选 -> 同期群分析
# ============================================================

def pipeline_segment_to_cohort(
    segments: list[Segment],
    user_events: dict[str, list[str]],
) -> dict[str, Any]:
    """群体圈选到同期群分析的完整管道。

    1. 从群体中提取用户ID
    2. 计算各观察窗口的留存率
    3. 生成留存矩阵
    """
    cohort_results = []

    for segment in segments:
        cohort = CohortAnalysis(
            cohort_id=f"COH_{segment.segment_id}",
            cohort_name=f"群组_{segment.segment_name}",
            partition_period="weekly",
            observation_windows=[1, 3, 7, 14, 30],
        )

        users = [f"user_{i}" for i in range(segment.member_count)]
        retentions = cohort.compute_retention(users, user_events)

        cohort_results.append({
            "cohort_name": cohort.cohort_name,
            "member_base": cohort.member_base_count,
            "window_d1_retention": retentions[0] if len(retentions) > 0 else 0,
            "window_d3_retention": retentions[1] if len(retentions) > 1 else 0,
            "window_d7_retention": retentions[2] if len(retentions) > 2 else 0,
            "window_d14_retention": retentions[3] if len(retentions) > 3 else 0,
            "window_d30_retention": retentions[4] if len(retentions) > 4 else 0,
            "retention_curve": retentions,
        })

    return {
        "pipeline": "segment_builder -> cohort_analysis",
        "cohorts": cohort_results,
        "total_cohorts": len(cohort_results),
    }


# ============================================================
# 流 7: anomaly_detect -> metric_def -> scheduled_analysis
# 异常检测 -> 指标定义 -> 定时分析
# ============================================================

def pipeline_anomaly_to_scheduled(
    anomaly_rules: list[AnomalyDetect],
    metric_time_series: dict[str, list[float]],
) -> dict[str, Any]:
    """异常检测到定时分析的完整管道。

    1. 对每个规则的指标时间序列执行异常检测
    2. 汇总异常点
    3. 生成定时分析调度建议
    """
    schedule_results = []

    for rule in anomaly_rules:
        target_id = rule.target_metric.metric_id
        series = metric_time_series.get(target_id, [])

        anomalous_indices = rule.detect(series)

        # 基于异常检测结果生成调度建议
        schedule = {
            "rule_id": rule.rule_id,
            "metric_name": rule.target_metric.metric_name,
            "algorithm": rule.algorithm.value,
            "baseline_period": rule.baseline_period_days,
            "threshold": rule.threshold_multiplier,
            "total_points": len(series),
            "anomalous_points": len(anomalous_indices),
            "anomalous_indices": anomalous_indices,
            "anomaly_rate": round(len(anomalous_indices) / len(series), 4) if series else 0,
            "suggested_cron": _suggest_cron_from_anomaly_rate(
                len(anomalous_indices) / len(series) if series else 0
            ),
        }
        schedule_results.append(schedule)

    return {
        "pipeline": "anomaly_detect -> metric_def -> scheduled_analysis",
        "schedule_results": schedule_results,
        "total_rules": len(anomaly_rules),
    }


def _suggest_cron_from_anomaly_rate(rate: float) -> str:
    """根据异常率建议 Cron 表达式。"""
    if rate > 0.1:
        return "0 */2 * * *"  # 每2小时
    elif rate > 0.05:
        return "0 */6 * * *"  # 每6小时
    elif rate > 0.01:
        return "0 0 * * *"  # 每天
    else:
        return "0 0 * * 1"  # 每周一


# ============================================================
# 流 8: olap_query -> data_export -> data_snapshot
# 多维查询 -> 数据导出 -> 分析快照
# ============================================================

def pipeline_query_to_snapshot(
    queries: list[OlapQuery],
    mock_data: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """查询到快照的完整管道。

    1. 从 OLAP 查询构建数据导出任务
    2. 执行字段筛选和排序
    3. 创建分析快照版本
    """
    snapshots = []

    for query in queries:
        source_rows = mock_data.get(query.query_id, [])

        # 构建导出任务
        export = DataExport(
            task_id=f"EXP_{query.query_id}",
            source_query=query,
            export_format="csv",
            field_selection=[m.field_name for m in query.metrics] if query.metrics else [],
            row_limit=1000,
            order_by=f"-{query.metrics[0].field_name}" if query.metrics else "",
            file_path=f"/exports/{query.query_id}.csv",
        )

        export_rows = export.build_row_subset(source_rows)

        # 生成 Schema 摘要
        schema_columns = [m.field_name for m in query.metrics]
        if query.dimensions:
            for d in query.dimensions:
                schema_columns.extend([d.field_source])

        # 创建快照
        snapshot = DataSnapshot(
            snapshot_id=f"SS_{query.query_id}",
            analysis_plan_id=query.query_id,
            data_rows=export_rows,
            schema_summary=", ".join(set(schema_columns)),
            reason=f"查询{query.query_name}的结果快照",
            retention_days=90,
            dependency_version="v1.0.0",
            created_at="2026-11-18T12:00:00",
        )

        snapshots.append({
            "snapshot_id": snapshot.snapshot_id,
            "query_name": query.query_name,
            "export_rows": len(export_rows),
            "export_file": export.file_path,
            "schema_summary": snapshot.schema_summary,
            "retention_days": snapshot.retention_days,
        })

    return {
        "pipeline": "olap_query -> data_export -> data_snapshot",
        "snapshots": snapshots,
        "total_snapshots": len(snapshots),
    }


# ============================================================
# 流 9: dataset_join -> olap_query -> report_builder
# 数据集关联 -> 多维查询 -> 报告生成
# ============================================================

def pipeline_join_to_report(
    joins: list[DatasetJoin],
    datasets: dict[str, list[dict[str, Any]]],
    report_templates: list[ReportTemplate],
) -> dict[str, Any]:
    """数据集关联到报告生成的完整管道。

    1. 执行数据集关联（JOIN）
    2. 构建关联后的多维查询
    3. 生成分析报告
    """
    join_results = []
    report_results = []

    for join in joins:
        primary_rows = datasets.get(join.primary_dataset, [])
        join_rows = datasets.get(join.join_dataset, [])

        joined_rows = join.execute_join(primary_rows, join_rows)

        join_results.append({
            "join_id": join.join_id,
            "primary_dataset": join.primary_dataset,
            "join_dataset": join.join_dataset,
            "join_type": join.join_type.value,
            "field_mapping": join.field_mapping,
            "result_rows": len(joined_rows),
        })

    # 为每个报告模板生成报告
    for template in report_templates:
        query_results = {}
        for metric in template.referenced_metrics:
            # 从关联结果中提取数据
            for jr in join_results:
                joined_data = datasets.get(jr["primary_dataset"], [])
                if joined_data:
                    query_results[metric.metric_id] = joined_data

        report_data = template.generate_report_data(query_results)
        report_results.append({
            "template_id": report_data["template_id"],
            "report_name": report_data["report_name"],
            "metric_count": len(report_data.get("metrics", {})),
            "metrics_summary": {
                mid: info["value"]
                for mid, info in report_data.get("metrics", {}).items()
            },
        })

    return {
        "pipeline": "dataset_join -> olap_query -> report_builder",
        "joins": join_results,
        "reports": report_results,
    }


# ============================================================
# 流 10: ranking_board -> metric_def
# 排行榜 -> 指标定义
# ============================================================

def pipeline_ranking_to_metric(
    ranking_boards: list[RankingBoard],
    data_rows: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """排行榜到指标定义的完整管道。

    1. 从排行榜读取排行指标定义
    2. 执行指标计算和排序
    3. 返回 TOP N 排行结果
    """
    rankings = []

    for rb in ranking_boards:
        metric = rb.rank_metric
        rows = data_rows.get(rb.rank_id, [])

        top_items = rb.compute_ranking(rows)

        rankings.append({
            "rank_id": rb.rank_id,
            "metric_name": metric.metric_name,
            "metric_field": metric.field_name,
            "aggregation": metric.aggregation.value,
            "dimensions": [d.dimension_name for d in rb.rank_dimensions],
            "top_n": rb.top_n,
            "result_count": len(top_items),
            "top_3": [
                {
                    "dimension": str(item.get(
                        rb.rank_dimensions[0].field_source, ""
                    )) if rb.rank_dimensions else "",
                    "value": float(item.get(metric.field_name, 0)),
                }
                for item in top_items[:3]
            ],
        })

    return {
        "pipeline": "ranking_board -> metric_def",
        "rankings": rankings,
        "total_ranking_boards": len(rankings),
    }


# ============================================================
# 流 11: dimension_def -> drill_path -> report_builder（地区低样本门槛）
# 地区下钻 -> 省级/区级发布核定 -> 外部报告
# ============================================================

def pipeline_region_publication(
    orders: list[dict[str, Any]],
    dimensions: list[DimensionDef],
    report_templates: list[ReportTemplate],
    *,
    scope_id: str = "region-publication",
    policy: RegionPublicationPolicy | None = None,
    approved_regions: list[str] | None = None,
) -> dict[str, Any]:
    """地区下钻到外部报告的低样本发布管道。

    1. 取地区维度（优先 ``region`` 字段）与报告模板
    2. 按有效订单去重聚合到省和上级区域
    3. 分别核定省级与区级发布资格
    4. 只按已允许的地区集合实算报告行与合计
    """
    region_dimension = next(
        (dim for dim in dimensions if dim.field_source == "region"),
        dimensions[0] if dimensions else None,
    )
    decision = evaluate_publication(
        orders,
        scope_id=scope_id,
        policy=policy or RegionPublicationPolicy(),
        approved_regions=approved_regions or [],
    )
    report = build_external_report(orders, decision)

    return {
        "pipeline": "dimension_def -> drill_path -> report_builder (region gate)",
        "region_dimension": region_dimension.dimension_name if region_dimension else None,
        "report_template": report_templates[0].report_name if report_templates else None,
        "decision": decision.to_dict(),
        "report": report,
    }


# ============================================================
# 全量跨模块集成管道
# ============================================================

def run_full_analysis_pipeline(
    metrics: list[MetricDef],
    dimensions: list[DimensionDef],
    events: list[EventTrack],
    experiments: list[ABExperiment],
    attribution_models: list[AttributionModel],
    anomaly_rules: list[AnomalyDetect],
    segments: list[Segment],
    ranking_boards: list[RankingBoard],
    joins: list[DatasetJoin],
    report_templates: list[ReportTemplate],
    sample_data: dict[str, Any],
) -> dict[str, Any]:
    """运行全量跨模块分析管道。

    整合 10 条数据流，返回统一的分析结果映射。
    """
    all_results = {}

    # 流 1
    if metrics and dimensions:
        all_results["flow1_dashboard"] = pipeline_metric_to_dashboard(
            metrics, dimensions, sample_data.get("metric_rows", [])
        )

    # 流 2
    if dimensions and metrics:
        all_results["flow2_drill"] = pipeline_dimension_drill(dimensions, metrics)

    # 流 3
    if events:
        all_results["flow3_segment"] = pipeline_event_to_segment(
            events, sample_data.get("users", [])
        )

    # 流 4
    if experiments and metrics:
        all_results["flow4_experiment"] = pipeline_experiment_to_compare(
            experiments, metrics,
            sample_data.get("control_data", {}),
            sample_data.get("treatment_data", {}),
        )

    # 流 5
    if attribution_models:
        all_results["flow5_attribution"] = pipeline_attribution_to_funnel(
            attribution_models,
            sample_data.get("touchpoints", []),
            sample_data.get("conversions", []),
            sample_data.get("user_events_funnel", {}),
        )

    # 流 6
    if segments:
        all_results["flow6_cohort"] = pipeline_segment_to_cohort(
            segments, sample_data.get("user_events_cohort", {})
        )

    # 流 7
    if anomaly_rules:
        all_results["flow7_anomaly"] = pipeline_anomaly_to_scheduled(
            anomaly_rules, sample_data.get("time_series", {})
        )

    # 流 8
    if sample_data.get("query_data"):
        all_results["flow8_snapshot"] = pipeline_query_to_snapshot(
            [], sample_data.get("query_data", {})
        )

    # 流 9
    if joins and report_templates:
        all_results["flow9_report"] = pipeline_join_to_report(
            joins, sample_data.get("datasets", {}), report_templates
        )

    # 流 10
    if ranking_boards:
        all_results["flow10_ranking"] = pipeline_ranking_to_metric(
            ranking_boards, sample_data.get("ranking_data", {})
        )

    # 流 11：地区低样本发布门槛（示例数据里没有地区订单时跳过）
    region_orders = sample_data.get("region_orders")
    if region_orders:
        all_results["flow11_region_publication"] = pipeline_region_publication(
            region_orders,
            dimensions,
            report_templates,
            scope_id="full-pipeline",
        )

    return all_results