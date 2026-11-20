"""存储实例绑定回归测试。

历史上服务层用 ``from app.store import store`` 静态绑定全局实例，一旦在导入后调用
``reset_store()``（测试隔离场景），服务层仍指向旧实例，写入会「消失」。
集市存储相关服务统一通过 ``app.store.store`` 动态访问，本测试锁定该行为。
"""
from __future__ import annotations

from app import store as store_module
from app.services import mart_review, mart_storage
from app.services.data_mart import DataMartService
from app.services.pipeline_orchestrator import run_experiment_stat_pipeline


def test_services_follow_reset_store():
    # 在 reset 之前就拿到服务层引用，模拟测试模块先于 conftest 导入的顺序
    before = store_module.store
    from app.store import reset_store
    reset_store()
    after = store_module.store
    assert before is not after

    # 服务层写入必须落到 reset 之后的新实例
    row, created = mart_storage.upsert_statistic(
        {"实验编号": "EXPE-001", "业务日期": "2026-08-15"})
    assert created is True
    assert any(r.get("实验编号") == "EXPE-001"
               for r in after.rows("data_mart")
               if mart_storage.is_stat_row(r))

    # 管道编排器同样跟随新实例
    result = run_experiment_stat_pipeline("EXPE-001")
    assert result["experiment_code"] == "EXPE-001"
    assert mart_storage.list_statistics("EXPE-001")

    # 主题服务读取的也是新实例
    service = DataMartService()
    items, total = service.list_entries(page=1, size=20)
    assert total == 3
    assert service.get_entry(1)["主题编号"] == "DATA-ORDER"

    # 复盘服务引用的 store 与当前全局一致
    assert mart_review.store() is after
