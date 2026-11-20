"""集市存储层：主题目录与实验统计记录分离后的统一读写来源。

设计要点（对应集市存储上线约束）：

1. 行类型可区分：主题配置行 row_type=topic（带数字 id 与主题元数据），
   实验统计行 row_type=experiment_stat（无数字主题 id，按 实验编号+业务日期 追溯）。
2. 读模式可回滚：
   - legacy  读端仍看旧 data_mart 混合列表（主题行 + 无数字编号统计行）；
   - new     读端看新结构 _mart_topics 主题目录与 _mart_statistics 统计记录。
3. 写入按当前读模式路由，并对 (实验编号, 业务日期, region, channel) 做幂等 upsert，
   重复回填不会多出一份数据。
4. 迁移以批次为单位，批次内保存可核对的主题快照（含 sha256 校验和），
   回滚只切换读模式，不销毁新结构数据，可再次提交迁移。
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from typing import Any

from app.services.experiment_stats import (
    STAT_ROW_TYPE,
    TOPIC_ROW_TYPE,
    compute_experiment_stat,
)
from app import store as store_module

TOPIC_TABLE = "data_mart"
NEW_TOPIC_TABLE = "_mart_topics"
STAT_TABLE = "_mart_statistics"
MIGRATION_TABLE = "_mart_migrations"

READ_MODE_LEGACY = "legacy"
READ_MODE_NEW = "new"


def store():
    """动态获取当前全局 store，保证 reset_store() 后服务层跟随新实例。"""
    return store_module.store


# ---------------------------------------------------------------------------
# 行类型判定
# ---------------------------------------------------------------------------

def is_topic_row(row: dict[str, Any]) -> bool:
    """主题配置行：带数字 id，或显式标记为 topic。"""
    if row.get("row_type") == TOPIC_ROW_TYPE:
        return True
    if row.get("row_type") == STAT_ROW_TYPE:
        return False
    return row.get("id") is not None


def is_stat_row(row: dict[str, Any]) -> bool:
    """统计行：显式标记，或旧列表里没有数字 id 的实验统计行。"""
    if row.get("row_type") == STAT_ROW_TYPE:
        return True
    if row.get("row_type") == TOPIC_ROW_TYPE:
        return False
    return row.get("id") is None and bool(row.get("实验编号"))


def get_read_mode() -> str:
    """当前集市读端模式；未迁移过时默认旧结构。"""
    state = store().rows("_mart_state")
    if state:
        return str(state[0].get("read_mode", READ_MODE_LEGACY))
    return READ_MODE_LEGACY


def set_read_mode(mode: str) -> None:
    table = store().rows("_mart_state")
    if not table:
        table.append({"read_mode": mode, "updated_at": datetime.now().isoformat()})
    else:
        table[0]["read_mode"] = mode
        table[0]["updated_at"] = datetime.now().isoformat()


# ---------------------------------------------------------------------------
# 主题目录读取（兼容已有的主题编号和明细请求）
# ---------------------------------------------------------------------------

def list_topics() -> list[dict[str, Any]]:
    """按当前读模式返回主题配置行；统计行永远不会混进主题目录。"""
    if get_read_mode() == READ_MODE_NEW:
        return [dict(row) for row in store().rows(NEW_TOPIC_TABLE)]
    return [
        _ensure_topic_type(dict(row))
        for row in store().rows(TOPIC_TABLE) if is_topic_row(row)
    ]


def _ensure_topic_type(row: dict[str, Any]) -> dict[str, Any]:
    row.setdefault("row_type", TOPIC_ROW_TYPE)
    return row


def find_topic(entry_id: int) -> dict[str, Any] | None:
    """按数字 id 读取主题明细，新旧结构下 id 与元数据保持不变。"""
    if get_read_mode() == READ_MODE_NEW:
        for row in store().rows(NEW_TOPIC_TABLE):
            if int(row.get("id", 0)) == entry_id:
                return dict(row)
        return None
    for row in store().rows(TOPIC_TABLE):
        if is_topic_row(row) and int(row.get("id", 0)) == entry_id:
            return dict(row)
    return None


def find_topic_by_code(主题编号: str) -> dict[str, Any] | None:
    for row in list_topics():
        if row.get("主题编号") == 主题编号:
            return row
    return None


def append_topic(entry: dict[str, Any]) -> dict[str, Any]:
    """登记新主题；按读模式写入对应主题目录，并打上 topic 行类型。"""
    entry = dict(entry)
    entry["row_type"] = TOPIC_ROW_TYPE
    if get_read_mode() == READ_MODE_NEW:
        store().rows(NEW_TOPIC_TABLE).append(entry)
    else:
        store().rows(TOPIC_TABLE).append(entry)
    return entry


# ---------------------------------------------------------------------------
# 统计记录读取（无数字编号，按实验与业务日期追溯）
# ---------------------------------------------------------------------------

def _stat_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("实验编号", "")),
        str(row.get("业务日期") or row.get("日期") or ""),
        str(row.get("region", "all")),
        str(row.get("channel", "all")),
    )


def list_statistics(experiment_code: str | None = None) -> list[dict[str, Any]]:
    """按当前读模式返回实验统计行；主题配置行不参与显著性查询。"""
    if get_read_mode() == READ_MODE_NEW:
        rows = [dict(row) for row in store().rows(STAT_TABLE)]
    else:
        rows = [dict(row) for row in store().rows(TOPIC_TABLE) if is_stat_row(row)]
    if experiment_code:
        rows = [row for row in rows if row.get("实验编号") == experiment_code]
    return rows


def _next_stat_id() -> int:
    table = store().rows(STAT_TABLE)
    return max((int(row.get("stat_id", 0)) for row in table), default=0) + 1


def _normalize_stat_row(row: dict[str, Any]) -> dict[str, Any]:
    """统一统计行字段，兼容旧列表里以「日期」承载业务日期的写法。"""
    normalized = dict(row)
    normalized["row_type"] = STAT_ROW_TYPE
    normalized.setdefault("业务日期", normalized.get("日期"))
    normalized.setdefault("样本日期", normalized.get("业务日期"))
    normalized.setdefault("region", "all")
    normalized.setdefault("channel", "all")
    return normalized


def upsert_statistic(row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """幂等写入一条实验统计记录。

    返回 (写入后的行, 是否为新建)。同一 (实验编号, 业务日期, region, channel)
    重复回填时原地覆盖，数据不会多一份。
    """
    payload = _normalize_stat_row(row)
    key = _stat_key(payload)

    if get_read_mode() == READ_MODE_NEW:
        table = store().rows(STAT_TABLE)
        for existing in table:
            if _stat_key(existing) == key:
                stat_id = existing.get("stat_id")
                created_at = existing.get("created_at")
                existing.clear()
                existing.update(payload)
                existing["stat_id"] = stat_id
                existing["created_at"] = created_at
                existing["updated_at"] = datetime.now().isoformat()
                return dict(existing), False
        payload["stat_id"] = _next_stat_id()
        payload["created_at"] = datetime.now().isoformat()
        table.append(payload)
        return dict(payload), True

    # 旧结构：统计行追加在 data_mart 混合列表里，同样按 key 幂等覆盖
    table = store().rows(TOPIC_TABLE)
    for existing in table:
        if is_stat_row(existing) and _stat_key(existing) == key:
            existing.clear()
            existing.update(payload)
            return dict(existing), False
    table.append(payload)
    return dict(payload), True


def correct_statistic(experiment_code: str, business_date: str, fields: dict[str, Any]) -> dict[str, Any]:
    """对新结构里已迁移的统计记录做更正（如转化人数更正）。

    更正后重算转化率、提升量、z 检验与显著性，并保留更正轨迹。
    旧结构下不允许走更正入口，必须先迁移。
    """
    if get_read_mode() != READ_MODE_NEW:
        raise ValueError("当前读端仍指向旧结构列表，请先提交迁移后再更正统计记录")

    target = None
    for row in store().rows(STAT_TABLE):
        if row.get("实验编号") == experiment_code and str(row.get("业务日期")) == business_date:
            target = row
            break
    if target is None:
        raise KeyError(f"统计记录不存在：实验 {experiment_code} 业务日期 {business_date}")

    target.setdefault("corrections", [])
    target["corrections"].append({
        "fields": dict(fields),
        "corrected_at": datetime.now().isoformat(),
    })
    target.update(fields)

    # 基于更正后的分组人数重算全部派生指标
    recalculated = compute_experiment_stat(
        {"样本量": target.get("控制组样本量"), "转化人数": target.get("控制组转化人数")},
        {"样本量": target.get("实验组样本量"), "转化人数": target.get("实验组转化人数")},
        experiment_code=experiment_code,
        business_date=business_date,
        sample_date=str(target.get("样本日期") or business_date),
        region=str(target.get("region", "all")),
        channel=str(target.get("channel", "all")),
    )
    # 保留追溯字段，刷新计算字段
    stat_id = target.get("stat_id")
    created_at = target.get("created_at")
    corrections = target.get("corrections")
    target.clear()
    target.update(recalculated)
    target["row_type"] = STAT_ROW_TYPE
    target["stat_id"] = stat_id
    target["created_at"] = created_at
    target["corrections"] = corrections
    target["updated_at"] = datetime.now().isoformat()
    return dict(target)


def ingest_legacy_statistic(row: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """旧结构时期实验管道写入混合列表的入口（无数字编号，按实验+业务日期追溯）。"""
    if get_read_mode() != READ_MODE_LEGACY:
        raise ValueError("新结构已上线，实验统计请直接写入统计记录表")
    return upsert_statistic(row)


# ---------------------------------------------------------------------------
# 迁移批次与主题数据快照
# ---------------------------------------------------------------------------

def _snapshot_checksum(snapshot: dict[str, Any]) -> str:
    body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _next_batch_no() -> str:
    table = store().rows(MIGRATION_TABLE)
    return f"BATCH-{len(table) + 1:03d}"


def run_migration(experiment_codes: list[str] | None = None,
                  batch_no: str | None = None) -> dict[str, Any]:
    """提交一次集市迁移，产出可核对批次的主题数据快照。

    - 主题配置行整体复制进主题目录，数字 id 与全部元数据原样保留；
    - 实验统计行按实验范围迁移进统计记录表，未迁移的实验仍只留在旧列表；
    - 重复提交是幂等的：已迁移的统计行按业务键覆盖，不会多出一份；
    - 迁移成功后读端切到新结构。
    """
    legacy_topics = [dict(r) for r in store().rows(TOPIC_TABLE) if is_topic_row(r)]
    legacy_stats = [dict(r) for r in store().rows(TOPIC_TABLE) if is_stat_row(r)]
    if experiment_codes is not None:
        wanted = set(experiment_codes)
        selected_stats = [r for r in legacy_stats if r.get("实验编号") in wanted]
    else:
        selected_stats = legacy_stats

    # 主题目录复制不依赖读模式，直接写入新表，数字 id 与元数据原样保留
    topic_table = store().rows(NEW_TOPIC_TABLE)
    existing_topic_ids = {int(r.get("id", 0)) for r in topic_table}
    migrated_topics: list[dict[str, Any]] = []
    for row in legacy_topics:
        topic = dict(row)
        topic["row_type"] = TOPIC_ROW_TYPE
        if int(topic.get("id", 0)) not in existing_topic_ids:
            topic_table.append(topic)
            existing_topic_ids.add(int(topic.get("id", 0)))
        migrated_topics.append(dict(topic))

    # 统计行直接写入统计记录表（绕过读模式路由），保证迁移目标始终是新结构
    stat_table = store().rows(STAT_TABLE)
    existing_keys = {_stat_key(r) for r in stat_table}
    migrated_stats: list[dict[str, Any]] = []
    for row in selected_stats:
        normalized = _normalize_stat_row(dict(row))
        key = _stat_key(normalized)
        if key in existing_keys:
            # 回滚后再次提交：已更正记录受保护，不被旧列表历史值覆盖；其余幂等覆盖
            current = next(r for r in stat_table if _stat_key(r) == key)
            if current.get("corrections"):
                migrated_stats.append(dict(current))
                continue
            stat_id = current.get("stat_id")
            created_at = current.get("created_at")
            current.clear()
            current.update(normalized)
            current["stat_id"] = stat_id
            current["created_at"] = created_at
            current["updated_at"] = datetime.now().isoformat()
            migrated_stats.append(dict(current))
        else:
            normalized["stat_id"] = _next_stat_id()
            normalized["created_at"] = datetime.now().isoformat()
            stat_table.append(normalized)
            existing_keys.add(key)
            migrated_stats.append(dict(normalized))

    snapshot = {
        "topics": migrated_topics,
        "statistics": migrated_stats,
        "topic_count": len(migrated_topics),
        "stat_count": len(migrated_stats),
    }
    batch = {
        "batch_no": batch_no or _next_batch_no(),
        "migrated_at": datetime.now().isoformat(),
        "experiment_scope": sorted({str(r.get("实验编号")) for r in selected_stats}),
        "snapshot": snapshot,
        "checksum": _snapshot_checksum(snapshot),
    }
    store().rows(MIGRATION_TABLE).append(batch)
    set_read_mode(READ_MODE_NEW)
    return dict(batch)


def rollback_read_side() -> dict[str, Any]:
    """回滚读端到旧结构混合列表；新结构数据保留，可再次提交迁移。"""
    previous = get_read_mode()
    set_read_mode(READ_MODE_LEGACY)
    return {
        "previous_mode": previous,
        "current_mode": READ_MODE_LEGACY,
        "topics_preserved": len(store().rows(NEW_TOPIC_TABLE)),
        "statistics_preserved": len(store().rows(STAT_TABLE)),
        "rolled_back_at": datetime.now().isoformat(),
    }


def list_migration_batches() -> list[dict[str, Any]]:
    return [dict(row) for row in store().rows(MIGRATION_TABLE)]


def get_migration_batch(batch_no: str) -> dict[str, Any] | None:
    for row in store().rows(MIGRATION_TABLE):
        if row.get("batch_no") == batch_no:
            return dict(row)
    return None


def storage_provenance() -> dict[str, Any]:
    """复查主题列表与显著性数据分别由哪些行类型、实验和日期构成。"""
    stat_rows = list_statistics()
    return {
        "read_mode": get_read_mode(),
        "topic_source": NEW_TOPIC_TABLE if get_read_mode() == READ_MODE_NEW else TOPIC_TABLE,
        "topic_row_type": TOPIC_ROW_TYPE,
        "topic_count": len(list_topics()),
        "statistic_source": STAT_TABLE if get_read_mode() == READ_MODE_NEW else TOPIC_TABLE,
        "statistic_row_type": STAT_ROW_TYPE,
        "statistic_count": len(stat_rows),
        "experiments": sorted({str(r.get("实验编号")) for r in stat_rows}),
        "business_dates": sorted({str(r.get("业务日期") or r.get("日期")) for r in stat_rows}),
        "sample_dates": sorted({str(r.get("样本日期")) for r in stat_rows if r.get("样本日期")}),
    }


def clone_row(row: dict[str, Any]) -> dict[str, Any]:
    """深拷贝一行，供报表冻结快照使用。"""
    return copy.deepcopy(row)
