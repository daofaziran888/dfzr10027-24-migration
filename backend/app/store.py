"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

集市存储上线后，以下划线开头的命名空间属于内部表（主题目录、统计记录、迁移批次、
复盘报表），不进运营概览与质量扫描，避免内部结构污染业务模块计数。
"""
from __future__ import annotations

from typing import Any

from app.seed import SEED_ROWS

# 集市存储上线后新增的内部命名空间
MART_INTERNAL_TABLES = [
    "_mart_state",         # 集市读端模式（legacy 旧混合列表 / new 新结构）
    "_mart_topics",        # 主题目录：带数字编号的分析主题配置行
    "_mart_statistics",    # 统计记录：实验管道写入的实验统计行
    "_mart_migrations",    # 迁移批次：可核对的主题数据快照
    "_mart_review_reports",  # 复盘报表：可复算的实验提升排序
]


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._tables.setdefault("_pipeline_results", [])
        for name in MART_INTERNAL_TABLES:
            self._tables.setdefault(name, [])
        # 旧混合列表里的种子主题行打上 topic 行类型，与无数字编号的实验统计行区分
        for row in self._tables["data_mart"]:
            row.setdefault("row_type", "topic")

    def module_names(self) -> list[str]:
        """返回业务模块名；下划线开头的内部命名空间不算业务模块。"""
        return sorted(name for name in self._tables if not name.startswith("_"))

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            # 集市迁移后旧列表里可能残留无数字编号的实验统计行，
            # 它们属于统计记录而非主题配置，不计入业务模块看板
            if name == "data_mart":
                rows = [row for row in rows if row.get("row_type") != "experiment_stat"
                        and row.get("id") is not None]
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()


def reset_store() -> None:
    """重新初始化全局 store，用于测试隔离。"""
    global store
    store = Store()
