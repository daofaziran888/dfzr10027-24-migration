"""实验统计计算口径：实验管道写入与集市复盘读取共用同一份实现。

z 检验与 Cohen's h 的算法与历史管道保持一致，仅把计算过程从编排器里抽出，
保证「实验统计写入」和「集市读取/复盘排序」对同一份分组明细算出完全相同的结果。
"""
from __future__ import annotations

import math
from typing import Any

STAT_ROW_TYPE = "experiment_stat"
TOPIC_ROW_TYPE = "topic"


def _to_int(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def compute_experiment_stat(
    control: dict[str, Any],
    treatment: dict[str, Any],
    *,
    experiment_code: str,
    business_date: str,
    sample_date: str | None = None,
    region: str = "all",
    channel: str = "all",
) -> dict[str, Any]:
    """根据对照组/实验组分组明细计算双比例 z 检验与效应量。

    business_date 为统计行归属的业务日期，sample_date 为实验样本本身的观测日期；
    两者可能不同（同一业务日复盘多个实验时，样本日期仍保留各实验自己的日期）。
    """
    n_c = _to_int(control.get("样本量"))
    n_t = _to_int(treatment.get("样本量"))
    conv_c = _to_int(control.get("转化人数"))
    conv_t = _to_int(treatment.get("转化人数"))

    p_c = conv_c / n_c if n_c else 0.0
    p_t = conv_t / n_t if n_t else 0.0
    p_pool = (conv_c + conv_t) / (n_c + n_t) if (n_c + n_t) else 0.0
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n_c + 1 / n_t)) if (n_c + n_t) else 0.0
    z = (p_t - p_c) / se if se else 0.0
    cohen_h = 2 * (math.asin(math.sqrt(p_t)) - math.asin(math.sqrt(p_c)))

    return {
        "实验编号": experiment_code,
        "业务日期": business_date,
        "样本日期": sample_date or business_date,
        "region": region,
        "channel": channel,
        "控制组样本量": n_c,
        "实验组样本量": n_t,
        "控制组转化人数": conv_c,
        "实验组转化人数": conv_t,
        "控制组转化率": round(p_c, 4),
        "实验组转化率": round(p_t, 4),
        "绝对提升": round(p_t - p_c, 4),
        "相对提升": round((p_t - p_c) / p_c, 4) if p_c else None,
        "z_score": round(z, 4),
        "cohen_h": round(cohen_h, 4),
        "显著性": abs(z) > 1.96,
    }
