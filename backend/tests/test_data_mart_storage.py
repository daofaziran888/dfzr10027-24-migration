"""集市存储上线端到端场景测试。

覆盖故事线：
旧混合列表（甲旧值 + 乙）→ 甲迁移并更正转化人数 → 乙尚未迁移时生成复盘 R1
（顺序：乙 > 甲旧）→ 回滚读端 → 再次提交迁移（乙迁入、甲更正值受保护）
→ 生成复盘 R2（顺序：甲新 > 乙）→ 从两版报表打开关联统计，核对样本日期与旧主题编号。
"""
from __future__ import annotations

import pytest

from app.services import mart_review, mart_storage
from app.services.data_mart import DataMartService
from app.services.experiment_stats import STAT_ROW_TYPE, TOPIC_ROW_TYPE
from app.services.pipeline_orchestrator import run_experiment_stat_pipeline
from app import store as store_module


@pytest.fixture(autouse=True)
def _fresh_store():
    from app.store import reset_store
    reset_store()
    yield


def store():
    return store_module.store

BUSINESS_DATE = "2026-08-20"
EXPE_A = "EXPE-001"  # 甲
EXPE_B = "EXPE-002"  # 乙


def _legacy_stat(experiment: str, *, control: tuple[int, int], treatment: tuple[int, int],
                 sample_date: str) -> dict:
    """构造旧列表里没有数字编号的实验统计行（按实验+业务日期追溯）。"""
    return {
        "row_type": STAT_ROW_TYPE,
        "主题编号": f"DATA-STAT-{experiment}",
        "实验编号": experiment,
        "业务日期": BUSINESS_DATE,
        "样本日期": sample_date,
        "region": "all",
        "channel": "all",
        "控制组样本量": control[0],
        "控制组转化人数": control[1],
        "实验组样本量": treatment[0],
        "实验组转化人数": treatment[1],
        "控制组转化率": round(control[1] / control[0], 4),
        "实验组转化率": round(treatment[1] / treatment[0], 4),
        "绝对提升": round(treatment[1] / treatment[0] - control[1] / control[0], 4),
        "相对提升": round(
            (treatment[1] / treatment[0] - control[1] / control[0])
            / (control[1] / control[0]), 4),
        "显著性": False,
    }


@pytest.fixture()
def legacy_world():
    """准备旧结构混合列表：同日的甲（旧值）、乙各一份统计。"""
    assert mart_storage.get_read_mode() == mart_storage.READ_MODE_LEGACY
    # 甲旧值：treatment 375/5000，相对提升约 0.0714，低于乙
    a_old = _legacy_stat(EXPE_A, control=(5000, 350), treatment=(5000, 375),
                         sample_date="2026-08-15")
    # 乙：treatment 528/8000，相对提升 0.1
    b_row = _legacy_stat(EXPE_B, control=(8000, 480), treatment=(8000, 528),
                         sample_date="2026-08-20")
    mart_storage.ingest_legacy_statistic(a_old)
    mart_storage.ingest_legacy_statistic(b_row)


class TestLegacyMixedList:
    """旧结构：主题配置行与无数字编号统计行同处一张列表。"""

    def test_legacy_rows_have_distinct_types(self, legacy_world):
        rows = store().rows("data_mart")
        topics = [r for r in rows if mart_storage.is_topic_row(r)]
        stats = [r for r in rows if mart_storage.is_stat_row(r)]
        assert len(topics) == 3
        assert {r["id"] for r in topics} == {1, 2, 3}
        assert len(stats) == 2
        assert all(r.get("id") is None for r in stats)
        assert all(r["row_type"] == STAT_ROW_TYPE for r in stats)

    def test_topic_catalog_excludes_statistics(self, legacy_world):
        # 主题目录只返回配置行，配置行不参与实验显著性查询
        topics = mart_storage.list_topics()
        assert len(topics) == 3
        assert all(r["row_type"] == TOPIC_ROW_TYPE for r in topics)
        stats = mart_storage.list_statistics()
        assert {r["实验编号"] for r in stats} == {EXPE_A, EXPE_B}
        assert all(r.get("id") is None for r in stats)

    def test_topic_id_and_metadata_preserved(self, legacy_world):
        topic = mart_storage.find_topic(3)
        assert topic["主题编号"] == "DATA-EXPERIMENT"
        assert topic["主题名称"] == "实验主题"
        assert topic["负责人"] == "王五"

    def test_legacy_ingest_is_idempotent(self, legacy_world):
        before = len(store().rows("data_mart"))
        duplicate = _legacy_stat(EXPE_A, control=(5000, 350), treatment=(5000, 375),
                                 sample_date="2026-08-15")
        _, created = mart_storage.ingest_legacy_statistic(duplicate)
        assert created is False
        assert len(store().rows("data_mart")) == before
        stats = mart_storage.list_statistics(EXPE_A)
        assert len(stats) == 1

    def test_old_a_lift_below_b(self, legacy_world):
        lifts = {r["实验编号"]: r["相对提升"] for r in mart_storage.list_statistics()}
        assert lifts[EXPE_A] < lifts[EXPE_B]


class TestMigrationAndCorrection:
    """迁移批次、主题快照、转化人数更正。"""

    def test_migrate_a_only_with_snapshot(self, legacy_world):
        batch = mart_storage.run_migration(experiment_codes=[EXPE_A])
        assert batch["experiment_scope"] == [EXPE_A]
        snapshot = batch["snapshot"]
        # 主题目录整体迁移，数字身份与元数据保留
        assert snapshot["topic_count"] == 3
        assert {row["id"] for row in snapshot["topics"]} == {1, 2, 3}
        assert snapshot["topics"][2]["主题编号"] == "DATA-EXPERIMENT"
        # 只迁了甲的统计行
        assert snapshot["stat_count"] == 1
        assert snapshot["statistics"][0]["实验编号"] == EXPE_A
        assert len(batch["checksum"]) == 64  # sha256
        # 读端切到新结构
        assert mart_storage.get_read_mode() == mart_storage.READ_MODE_NEW
        # 乙尚未迁移：新结构统计记录里只有甲，乙仍只在旧列表
        new_stats = mart_storage.list_statistics()
        assert {r["实验编号"] for r in new_stats} == {EXPE_A}
        legacy_stats = [r for r in store().rows("data_mart") if mart_storage.is_stat_row(r)]
        assert {r["实验编号"] for r in legacy_stats} == {EXPE_A, EXPE_B}

    def test_topic_catalog_readable_after_migration(self, legacy_world):
        mart_storage.run_migration(experiment_codes=[EXPE_A])
        topics = mart_storage.list_topics()
        assert len(topics) == 3
        # 明细请求按数字 id 仍然可读
        detail = mart_storage.find_topic(1)
        assert detail["主题编号"] == "DATA-ORDER"
        # DataMartService 行为与存储层一致
        service = DataMartService()
        items, total = service.list_entries(filters={}, page=1, size=20)
        assert total == 3
        assert service.get_entry(2)["主题编号"] == "DATA-USER"

    def test_correction_flips_ranking(self, legacy_world):
        mart_storage.run_migration(experiment_codes=[EXPE_A])
        # 新结构收到一次转化人数更正：甲 treatment 375 -> 412
        corrected = mart_storage.correct_statistic(
            EXPE_A, BUSINESS_DATE, {"实验组转化人数": 412})
        assert corrected["实验组转化人数"] == 412
        assert corrected["相对提升"] == pytest.approx(0.1771, abs=0.0001)
        assert len(corrected["corrections"]) == 1
        # 乙未迁移，新结构统计记录里查不到乙；乙仍只在旧列表里
        assert mart_storage.list_statistics(EXPE_B) == []
        b_legacy = next(r for r in store().rows("data_mart")
                        if mart_storage.is_stat_row(r) and r["实验编号"] == EXPE_B)
        # 更正后甲的相对提升 0.1771 超过乙的 0.1
        assert corrected["相对提升"] > b_legacy["相对提升"]

    def test_correction_rejected_in_legacy_mode(self, legacy_world):
        with pytest.raises(ValueError):
            mart_storage.correct_statistic(EXPE_A, BUSINESS_DATE, {"实验组转化人数": 412})

    def test_pipeline_writes_statistics_table_in_new_mode(self, legacy_world):
        mart_storage.run_migration(experiment_codes=[EXPE_A])
        result = run_experiment_stat_pipeline(EXPE_A)
        assert result["write_mode"] == mart_storage.READ_MODE_NEW
        assert result["stat_row_type"] == STAT_ROW_TYPE
        # 管道按业务键幂等 upsert，不新增重复行
        pipeline_rows = [row for row in mart_storage.list_statistics(EXPE_A)
                         if row["业务日期"] == result["business_date"]]
        assert len(pipeline_rows) == 1
        again = run_experiment_stat_pipeline(EXPE_A)
        assert again["created"] is False
        pipeline_rows = [row for row in mart_storage.list_statistics(EXPE_A)
                         if row["业务日期"] == result["business_date"]]
        assert len(pipeline_rows) == 1
        # 主题目录行数不受管道写入影响
        assert len(mart_storage.list_topics()) == 3


class TestReviewReports:
    """两版复盘报表：覆盖实验、样本日期、可复算排序、回滚追溯。"""

    def test_full_review_lifecycle(self, legacy_world):
        # 1) 迁移甲并更正
        mart_storage.run_migration(experiment_codes=[EXPE_A], batch_no="BATCH-001")
        mart_storage.correct_statistic(EXPE_A, BUSINESS_DATE, {"实验组转化人数": 412})

        # 验证报表结构：覆盖两项实验、各自样本日期、行类型与统计引用
        report = mart_review.build_review_report(business_date=BUSINESS_DATE)
        assert report["coverage"]["experiments"] == [EXPE_A, EXPE_B]
        assert report["coverage"]["sample_dates"] == {
            EXPE_A: "2026-08-15", EXPE_B: "2026-08-20"}
        assert report["ranking_metric"] == "相对提升"
        assert len(report["items"]) == 2
        # 每个条目都能追溯到来源行类型与统计引用
        for item in report["items"]:
            assert item["row_type"] == STAT_ROW_TYPE
            assert item["旧主题编号"] == f"DATA-STAT-{item['实验编号']}"
            assert "stat_ref" in item and "frozen_stat" in item

    def test_review_order_reverses(self, legacy_world):
        # 故事严格顺序：迁移甲（旧值）→ 乙未迁移时生成 R1 → 更正甲
        mart_storage.run_migration(experiment_codes=[EXPE_A], batch_no="BATCH-001")
        r1 = mart_review.build_review_report(
            business_date=BUSINESS_DATE, remark="本期复盘-旧甲")
        # 旧甲提升 0.0714 < 乙 0.1，顺序为乙、甲
        assert r1["order"] == [EXPE_B, EXPE_A]
        assert r1["items"][0]["rank"] == 1 and r1["items"][0]["实验编号"] == EXPE_B
        # R1 里甲来自新结构（未更正），乙来自旧列表
        sources = {item["实验编号"]: item["source"] for item in r1["items"]}
        assert sources == {EXPE_A: "new_structure", EXPE_B: "legacy_list"}

        # 新结构收到转化人数更正
        mart_storage.correct_statistic(EXPE_A, BUSINESS_DATE, {"实验组转化人数": 412})

        # 回滚读端：R1 仍可读取，乙仍只在旧列表
        rollback = mart_storage.rollback_read_side()
        assert rollback["current_mode"] == mart_storage.READ_MODE_LEGACY
        assert rollback["statistics_preserved"] == 1
        r1_again = mart_review.get_review_report(r1["report_no"])
        assert r1_again["order"] == [EXPE_B, EXPE_A]

        # 回滚后主题目录读旧列表，仍只含 3 条配置行（统计行不混入）
        assert len(mart_storage.list_topics()) == 3

        # 再次提交迁移：乙迁入；甲因已有更正记录被保护，不被旧值覆盖
        batch2 = mart_storage.run_migration(batch_no="BATCH-002")
        assert mart_storage.get_read_mode() == mart_storage.READ_MODE_NEW
        assert batch2["experiment_scope"] == [EXPE_A, EXPE_B]
        a_row = mart_storage.list_statistics(EXPE_A)[0]
        assert a_row["实验组转化人数"] == 412
        assert len(mart_storage.list_statistics()) == 2

        # 迁移幂等：再提交一次不产生重复统计行
        mart_storage.run_migration(batch_no="BATCH-003")
        assert len(mart_storage.list_statistics()) == 2
        assert len(mart_storage.list_migration_batches()) == 3

        # 生成 R2：更正后的甲超过乙，顺序反转
        r2 = mart_review.build_review_report(
            business_date=BUSINESS_DATE, remark="本期复盘-新甲")
        assert r2["order"] == [EXPE_A, EXPE_B]
        assert r2["items"][0]["实验编号"] == EXPE_A
        assert r2["coverage"]["experiments"] == [EXPE_A, EXPE_B]

    def test_open_statistics_from_both_reports(self, legacy_world):
        mart_storage.run_migration(experiment_codes=[EXPE_A], batch_no="BATCH-001")
        r1 = mart_review.build_review_report(business_date=BUSINESS_DATE)
        mart_storage.correct_statistic(EXPE_A, BUSINESS_DATE, {"实验组转化人数": 412})
        mart_storage.rollback_read_side()
        mart_storage.run_migration(batch_no="BATCH-002")
        r2 = mart_review.build_review_report(business_date=BUSINESS_DATE)

        # 回滚后再次迁移完成，两版报表都能打开关联实验统计
        for report_no, expected_order in ((r1["report_no"], [EXPE_B, EXPE_A]),
                                          (r2["report_no"], [EXPE_A, EXPE_B])):
            linked = mart_review.resolve_report_statistics(report_no)
            assert [item["实验编号"] for item in linked] == expected_order
            for item in linked:
                # 核对样本日期与旧主题编号
                assert item["旧主题编号"] == f"DATA-STAT-{item['实验编号']}"
                assert item["样本日期"] == item["frozen_stat"]["样本日期"]
                assert item["frozen_stat"]["row_type"] == STAT_ROW_TYPE
                # 新结构下两个实验当前都可见
                assert item["current_visible"] is True
                assert item["current_stat"]["业务日期"] == BUSINESS_DATE

        # R1 冻结的甲仍是旧值 375，R2 冻结的甲是更正后 412
        r1_stats = {item["实验编号"]: item for item in
                    mart_review.resolve_report_statistics(r1["report_no"])}
        r2_stats = {item["实验编号"]: item for item in
                    mart_review.resolve_report_statistics(r2["report_no"])}
        assert r1_stats[EXPE_A]["frozen_stat"]["实验组转化人数"] == 375
        assert r2_stats[EXPE_A]["frozen_stat"]["实验组转化人数"] == 412

    def test_snapshot_checksum_verifiable(self, legacy_world):
        import hashlib
        import json
        batch = mart_storage.run_migration(experiment_codes=[EXPE_A])
        snapshot = batch["snapshot"]
        body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
        assert batch["checksum"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
        # 快照里可核对每条主题的数字身份
        snapshot_ids = sorted(row["id"] for row in snapshot["topics"])
        assert snapshot_ids == [1, 2, 3]


class TestProvenance:
    """报表应能复查主题列表和显著性数据由哪些实验和日期构成。"""

    def test_provenance_contents(self, legacy_world):
        mart_storage.run_migration(experiment_codes=[EXPE_A])
        provenance = mart_storage.storage_provenance()
        assert provenance["read_mode"] == mart_storage.READ_MODE_NEW
        assert provenance["topic_row_type"] == TOPIC_ROW_TYPE
        assert provenance["statistic_row_type"] == STAT_ROW_TYPE
        assert provenance["topic_count"] == 3
        assert provenance["statistic_count"] == 1
        assert provenance["experiments"] == [EXPE_A]
        assert provenance["business_dates"] == [BUSINESS_DATE]
        assert provenance["sample_dates"] == ["2026-08-15"]

    def test_report_embeds_provenance(self, legacy_world):
        mart_storage.run_migration(experiment_codes=[EXPE_A])
        report = mart_review.build_review_report(business_date=BUSINESS_DATE)
        provenance = report["provenance"]
        assert provenance["statistic_row_type"] == STAT_ROW_TYPE
        # 乙尚未迁移，来源构成里能同时看到新结构与旧列表
        assert set(report["coverage"]["sources"]) == {"new_structure", "legacy_list"}
