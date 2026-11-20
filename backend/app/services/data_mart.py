"""数据集市业务规则：状态流转、字段校验与筛选口径都收在这里。

集市存储上线后，主题目录与实验统计记录分离：本服务只服务带数字编号的主题配置行，
无数字编号的实验统计行由 mart_storage 的统计记录接口承载，不会混进主题列表与
显著性查询。读端在旧结构/新结构之间可切换，主题编号与明细请求保持兼容。
"""
from __future__ import annotations

from typing import Any

from app.services import mart_storage
from app.services.experiment_stats import TOPIC_ROW_TYPE
from app import store as store_module

MODULE = "data_mart"
LIST_FIELDS = ["主题编号", "主题名称", "业务域", "事实表", "维度集合", "刷新周期", "数据量", "负责人"]
REQUIRED_FIELDS = ["主题编号", "主题名称", "业务域"]
STATUS_ORDER = ["规划中", "建设中", "已上线", "已下线"]
ACTION_RULES = {"启动建设": "建设中", "上线主题": "已上线", "下线主题": "已下线"}
NEGATIVE_ACTIONS = ["下线主题"]


class DataMartService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        # 只取主题配置行：旧结构下统计行无数字 id，会被存储层排除
        rows = mart_storage.list_topics()
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("主题编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return mart_storage.find_topic(entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        # 新主题的数字 id 在当前主题目录上顺延，新旧结构都不与统计行争号
        current_topics = mart_storage.list_topics()
        entry = {"id": max((int(row.get("id", 0)) for row in current_topics), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        return mart_storage.append_topic(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = mart_storage.find_topic(entry_id)
        if entry is None:
            return None, f"分析主题 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据集市可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # find_topic 返回的是拷贝，需要把动作回写到真实存储行
        target_row = self._persist_action(entry_id, target, action)
        return target_row, f"分析主题已{action}"

    def _persist_action(self, entry_id: int, target_status: str, action: str) -> dict[str, Any]:
        table = (store_module.store.rows(mart_storage.NEW_TOPIC_TABLE)
                 if mart_storage.get_read_mode() == mart_storage.READ_MODE_NEW
                 else store_module.store.rows(MODULE))
        for row in table:
            if mart_storage.is_topic_row(row) and int(row.get("id", 0)) == entry_id:
                row["status"] = target_status
                row["pending"] = target_status != STATUS_ORDER[-1]
                row["abnormal"] = action in NEGATIVE_ACTIONS
                row["row_type"] = TOPIC_ROW_TYPE
                return dict(row)
        raise KeyError(entry_id)
