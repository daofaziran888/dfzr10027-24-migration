"""集市存储上线接口：主题目录/统计记录分离、迁移批次、读端回滚与复盘报表。

所有路径都挂在 /api/data_mart 前缀下的多级静态路径，避免与 /{entry_id} 数字
主题明细冲突；主题配置行（带数字 id）与实验统计行（无数字 id）在此彻底分流。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult
from app.services import mart_review, mart_storage

router = APIRouter(prefix="/api/data_mart", tags=["数据集市存储"])


# ---------------------------------------------------------------------------
# 读模式与迁移批次
# ---------------------------------------------------------------------------

@router.get("/storage/state", response_model=dict)
def storage_state() -> dict:
    """查看当前读端模式、行类型来源与主题/统计记录构成。"""
    return mart_storage.storage_provenance()


@router.post("/storage/migrate", response_model=ActionResult)
def trigger_migration(
    experiments: list[str] | None = Query(
        default=None, description="本次迁移覆盖的实验编号；不传则迁移全部旧统计行"),
    batch_no: str | None = Query(default=None, description="可选的自定义批次号"),
) -> ActionResult:
    """提交迁移：主题目录整体复制，统计记录按实验范围迁移，返回可核对快照批次。"""
    batch = mart_storage.run_migration(experiment_codes=experiments, batch_no=batch_no)
    return ActionResult(
        ok=True,
        message=f"迁移批次 {batch['batch_no']} 已提交，读端已切换到新结构",
        entry=batch,
    )


@router.post("/storage/rollback", response_model=ActionResult)
def trigger_rollback() -> ActionResult:
    """回滚读端到旧混合列表；新结构数据保留，可再次提交迁移。"""
    result = mart_storage.rollback_read_side()
    return ActionResult(ok=True, message="读端已回滚到旧结构列表，新结构数据保留", entry=result)


@router.get("/storage/migrations", response_model=dict)
def list_migrations() -> dict:
    """迁移批次列表（含每批次的主题数据快照与校验和）。"""
    return {"items": mart_storage.list_migration_batches()}


@router.get("/storage/migrations/{batch_no}", response_model=dict)
def get_migration(batch_no: str) -> dict:
    batch = mart_storage.get_migration_batch(batch_no)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"迁移批次 {batch_no} 不存在")
    return batch


# ---------------------------------------------------------------------------
# 主题目录与统计记录（行类型分流）
# ---------------------------------------------------------------------------

@router.get("/topics", response_model=dict)
def list_topics() -> dict:
    """主题目录：只含带数字编号的主题配置行，配置行不参与实验显著性查询。"""
    items = mart_storage.list_topics()
    return {"items": items, "total": len(items), "row_type": "topic"}


@router.get("/statistics", response_model=dict)
def list_statistics(
    experiment: str | None = Query(default=None, description="按实验编号过滤"),
) -> dict:
    """统计记录：无数字编号，按实验编号与业务日期追溯；主题配置行不会出现在这里。"""
    items = mart_storage.list_statistics(experiment_code=experiment)
    return {
        "items": items,
        "total": len(items),
        "row_type": "experiment_stat",
        "read_mode": mart_storage.get_read_mode(),
    }


@router.post("/statistics/legacy-ingest", response_model=ActionResult)
def legacy_ingest(payload: dict) -> ActionResult:
    """旧结构时期实验管道写入混合列表的入口（模拟旧列表统计行，无数字编号）。"""
    if not payload.get("实验编号") or not payload.get("业务日期"):
        return ActionResult(ok=False, message="缺少必填字段：实验编号、业务日期")
    try:
        row, created = mart_storage.ingest_legacy_statistic(payload)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(
        ok=True,
        message="旧列表统计行已写入" if created else "相同业务键已存在，已幂等覆盖未新增",
        entry=row,
    )


@router.post("/statistics/correct", response_model=ActionResult)
def correct_statistic(
    experiment: str = Query(..., description="实验编号"),
    business_date: str = Query(..., description="业务日期"),
    payload: dict | None = None,
) -> ActionResult:
    """对新结构统计记录做更正（如转化人数更正），并自动重算提升量与显著性。"""
    payload = payload or {}
    try:
        row = mart_storage.correct_statistic(experiment, business_date, payload)
    except KeyError as exc:
        return ActionResult(ok=False, message=str(exc))
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message="统计记录已更正并重算", entry=row)


# ---------------------------------------------------------------------------
# 复盘报表
# ---------------------------------------------------------------------------

@router.post("/reviews", response_model=ActionResult)
def create_review(
    business_date: str | None = Query(default=None, description="复盘业务日期"),
    experiments: list[str] | None = Query(default=None, description="覆盖的实验编号"),
    remark: str | None = Query(default=None, description="复盘备注"),
) -> ActionResult:
    """生成复盘报表：给出可复算的提升排序、覆盖实验与各自样本日期。"""
    try:
        report = mart_review.build_review_report(
            business_date=business_date, experiment_codes=experiments, remark=remark)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"复盘报表 {report['report_no']} 已生成", entry=report)


@router.get("/reviews", response_model=dict)
def list_reviews() -> dict:
    return {"items": mart_review.list_review_reports()}


@router.get("/reviews/{report_no}", response_model=dict)
def get_review(report_no: str) -> dict:
    report = mart_review.get_review_report(report_no)
    if report is None:
        raise HTTPException(status_code=404, detail=f"复盘报表 {report_no} 不存在")
    return report


@router.get("/reviews/{report_no}/statistics", response_model=dict)
def get_review_statistics(report_no: str) -> dict:
    """打开报表关联的实验统计，核对样本日期、旧主题编号与当前可见性。"""
    try:
        items = mart_review.resolve_report_statistics(report_no)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"items": items, "total": len(items)}
