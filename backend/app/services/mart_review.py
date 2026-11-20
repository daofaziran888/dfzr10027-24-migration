"""实验复盘报表：按提升量给出可复算的复盘先后顺序。

报表本身保存在独立命名空间，回滚读端不会删除报表；每条报表同时冻结当时使用的
统计记录快照并记录来源引用，因此回滚后仍能找到这次复盘所用的记录，
并可重新打开关联的实验统计，核对样本日期与旧主题编号。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services import mart_storage
from app.services.experiment_stats import STAT_ROW_TYPE
from app import store as store_module

REVIEW_TABLE = "_mart_review_reports"
RANKING_METRIC = "相对提升"
RANKING_RULE = "按相对提升降序排序，提升量相同时按实验编号升序"


def store():
    """动态获取当前全局 store，保证 reset_store() 后跟随新实例。"""
    return store_module.store


def _legacy_topic_code(experiment_code: str) -> str:
    """旧列表里统计行使用的主题编号（无数字 id，按实验+业务日期追溯）。"""
    return f"DATA-STAT-{experiment_code}"


def _next_report_no() -> str:
    return f"REVIEW-{len(store().rows(REVIEW_TABLE)) + 1:03d}"


def _collect_rows(business_date: str | None, experiment_codes: list[str] | None) -> list[dict[str, Any]]:
    """合并新结构统计记录与尚未迁移的旧列表统计行。

    新结构已有的实验以统计记录表为准；尚未迁移的实验仍从旧混合列表补入，
    保证「乙尚未迁移」时复盘也能覆盖两项实验。
    """
    new_rows = mart_storage.list_statistics()
    new_experiments = {row.get("实验编号") for row in new_rows}

    legacy_rows: list[dict[str, Any]] = []
    for row in store().rows(mart_storage.TOPIC_TABLE):
        if mart_storage.is_stat_row(row) and row.get("实验编号") not in new_experiments:
            legacy_rows.append(mart_storage._normalize_stat_row(dict(row)))

    merged: list[dict[str, Any]] = []
    for row in new_rows:
        row = dict(row)
        row["_source"] = "new_structure"
        merged.append(row)
    for row in legacy_rows:
        row["_source"] = "legacy_list"
        merged.append(row)

    if business_date:
        merged = [row for row in merged
                  if str(row.get("业务日期") or row.get("日期")) == business_date]
    if experiment_codes:
        wanted = set(experiment_codes)
        merged = [row for row in merged if row.get("实验编号") in wanted]

    # 同一实验同一业务日只保留一行（新结构优先），避免重复实验干扰排序
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in merged:
        key = (str(row.get("实验编号")), str(row.get("业务日期")))
        deduped.setdefault(key, row)
    return list(deduped.values())


def _rank_key(row: dict[str, Any]) -> tuple[float, str]:
    lift = row.get("相对提升")
    lift_value = float(lift) if lift is not None else float("-inf")
    return -lift_value, str(row.get("实验编号"))


def build_review_report(
    business_date: str | None = None,
    experiment_codes: list[str] | None = None,
    remark: str | None = None,
) -> dict[str, Any]:
    """生成一期复盘报表：可复算的排序、覆盖实验与各自样本日期。"""
    rows = _collect_rows(business_date, experiment_codes)
    if not rows:
        raise ValueError("当前读模式下没有可复盘的实验统计记录")

    if business_date is None:
        business_date = max(str(row.get("业务日期")) for row in rows)
        rows = [row for row in rows if str(row.get("业务日期")) == business_date]

    ranked = sorted(rows, key=_rank_key)

    items: list[dict[str, Any]] = []
    for index, row in enumerate(ranked, start=1):
        experiment_code = str(row.get("实验编号"))
        is_new = row.get("_source") == "new_structure"
        stat_ref: dict[str, Any]
        if is_new:
            stat_ref = {
                "table": mart_storage.STAT_TABLE,
                "stat_id": row.get("stat_id"),
                "business_key": {
                    "实验编号": experiment_code,
                    "业务日期": row.get("业务日期"),
                    "region": row.get("region", "all"),
                    "channel": row.get("channel", "all"),
                },
            }
        else:
            stat_ref = {
                "table": mart_storage.TOPIC_TABLE,
                "business_key": {
                    "实验编号": experiment_code,
                    "业务日期": row.get("业务日期"),
                    "region": row.get("region", "all"),
                    "channel": row.get("channel", "all"),
                },
            }
        items.append({
            "rank": index,
            "实验编号": experiment_code,
            "相对提升": row.get("相对提升"),
            "绝对提升": row.get("绝对提升"),
            "控制组转化率": row.get("控制组转化率"),
            "实验组转化率": row.get("实验组转化率"),
            "z_score": row.get("z_score"),
            "显著性": row.get("显著性"),
            "业务日期": row.get("业务日期"),
            "样本日期": row.get("样本日期"),
            "row_type": STAT_ROW_TYPE,
            "source": row.get("_source"),
            "旧主题编号": _legacy_topic_code(experiment_code),
            "stat_ref": stat_ref,
            "frozen_stat": mart_storage.clone_row(
                {k: v for k, v in row.items() if not k.startswith("_")}),
        })

    report = {
        "report_no": _next_report_no(),
        "created_at": datetime.now().isoformat(),
        "business_date": business_date,
        "read_mode_at_creation": mart_storage.get_read_mode(),
        "ranking_metric": RANKING_METRIC,
        "ranking_rule": RANKING_RULE,
        "remark": remark or "",
        "order": [item["实验编号"] for item in items],
        "coverage": {
            "experiments": [item["实验编号"] for item in items],
            "sample_dates": {item["实验编号"]: item["样本日期"] for item in items},
            "business_dates": sorted({str(item["业务日期"]) for item in items}),
            "sources": sorted({str(item["source"]) for item in items}),
        },
        "provenance": mart_storage.storage_provenance(),
        "items": items,
    }
    store().rows(REVIEW_TABLE).append(report)
    return dict(report)


def list_review_reports() -> list[dict[str, Any]]:
    """复盘报表列表（不含冻结明细，保持列表轻量）。"""
    summaries = []
    for row in store().rows(REVIEW_TABLE):
        summaries.append({
            key: value for key, value in row.items() if key != "items"
        })
    return summaries


def get_review_report(report_no: str) -> dict[str, Any] | None:
    for row in store().rows(REVIEW_TABLE):
        if row.get("report_no") == report_no:
            return dict(row)
    return None


def resolve_report_statistics(report_no: str) -> list[dict[str, Any]]:
    """打开报表关联的实验统计：冻结快照 + 当前读模式下能否重新读到同一记录。"""
    report = get_review_report(report_no)
    if report is None:
        raise KeyError(f"复盘报表 {report_no} 不存在")

    current_rows = mart_storage.list_statistics()
    resolved = []
    for item in report["items"]:
        key = item["stat_ref"]["business_key"]
        current = next((
            row for row in current_rows
            if str(row.get("实验编号")) == key["实验编号"]
            and str(row.get("业务日期")) == key["业务日期"]
            and str(row.get("region", "all")) == key["region"]
            and str(row.get("channel", "all")) == key["channel"]
        ), None)
        resolved.append({
            "rank": item["rank"],
            "实验编号": item["实验编号"],
            "旧主题编号": item["旧主题编号"],
            "样本日期": item["样本日期"],
            "业务日期": item["业务日期"],
            "source_at_creation": item["source"],
            "stat_ref": item["stat_ref"],
            "frozen_stat": item["frozen_stat"],
            "current_visible": current is not None,
            "current_stat": current,
        })
    return resolved
