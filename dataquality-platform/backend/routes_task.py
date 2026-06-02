import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_session
from backend.models import Datasource, Task, TaskStatus, Rule
from backend.schemas import TaskCreate, TaskResponse
from backend.engine import run_evaluation_task, get_task_progress, get_bad_data, get_builtin_rules

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_running_tasks: dict[int, asyncio.Task] = {}

DEBUG_DIR = Path(__file__).parent / "debug_requests"
DEBUG_DIR.mkdir(exist_ok=True)


def _log_request(task_id: int, label: str, data: dict):
    """保存请求调试信息到文件"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = DEBUG_DIR / f"task_{task_id}_{label}_{timestamp}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[DEBUG] {label} 日志已保存: {filename}")


def _to_response(t: Task) -> TaskResponse:
    progress_data = get_task_progress(t.id)
    status = t.status
    progress = t.progress
    total = t.total
    processed = t.processed
    score = t.score
    num_good = t.num_good
    num_bad = t.num_bad
    summary = t.summary
    error_message = t.error_message

    if progress_data and status == "running":
        status = progress_data.get("status", status)
        progress = progress_data.get("progress", progress)
        total = progress_data.get("total", total)
        processed = progress_data.get("processed", processed)
        if "score" in progress_data:
            score = progress_data["score"]
        if "num_good" in progress_data:
            num_good = progress_data["num_good"]
        if "num_bad" in progress_data:
            num_bad = progress_data["num_bad"]
        if "summary" in progress_data:
            summary = progress_data["summary"]
        if "error_message" in progress_data:
            error_message = progress_data["error_message"]

    return TaskResponse(
        id=t.id,
        name=t.name,
        datasource_id=t.datasource_id,
        rule_ids=t.rule_ids,
        field_mapping=t.field_mapping,
        rule_configs=t.rule_configs,
        table_name=t.table_name,
        sampling=t.sampling,
        status=status,
        progress=progress,
        total=total,
        processed=processed,
        score=score,
        num_good=num_good,
        num_bad=num_bad,
        summary=summary,
        error_message=error_message,
        created_at=t.created_at,
        updated_at=t.updated_at,
        started_at=t.started_at,
        finished_at=t.finished_at,
    )


@router.get("", response_model=list[TaskResponse])
async def list_tasks(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Task).order_by(Task.id.desc()))
    return [_to_response(t) for t in result.scalars().all()]


@router.post("", response_model=TaskResponse)
async def create_task(
    body: TaskCreate,
    session: AsyncSession = Depends(get_session),
):
    ds = await session.get(Datasource, body.datasource_id)
    if not ds:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Datasource not found")

    rule_ids_str = json.dumps(body.rule_ids)

    t = Task(
        name=body.name,
        datasource_id=body.datasource_id,
        rule_ids=rule_ids_str,
        field_mapping=body.field_mapping or {},
        rule_configs=body.rule_configs,
        table_name=body.table_name,
        sampling=body.sampling.model_dump() if body.sampling else None,
        status=TaskStatus.PENDING,
    )
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return _to_response(t)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: int, session: AsyncSession = Depends(get_session)):
    t = await session.get(Task, task_id)
    if not t:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Task not found")
    return _to_response(t)


@router.post("/{task_id}/start", response_model=TaskResponse)
async def start_task(task_id: int, session: AsyncSession = Depends(get_session)):
    t = await session.get(Task, task_id)
    if not t:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Task not found")

    if t.status in (TaskStatus.RUNNING,):
        raise HTTPException(status_code=400, detail="Task is already running")

    ds = await session.get(Datasource, t.datasource_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Datasource not found")

    rule_ids = json.loads(t.rule_ids)

    all_rules = get_builtin_rules()

    resolved_rules = []
    for rid in rule_ids:
        if rid > 0:
            r = await session.get(Rule, rid)
            if r:
                resolved_rules.append({
                    "name": r.name,
                    "config": r.config,
                    "rule_type": r.rule_type,
                })
        else:
            idx = abs(rid) - 1
            if 0 <= idx < len(all_rules):
                br = all_rules[idx]
                resolved_rules.append({"name": br["name"], "config": {}})

    if t.rule_configs:
        rc_map = {}
        for rc in t.rule_configs:
            rc_map[rc.get("rule_id")] = rc
        for i, rr in enumerate(resolved_rules):
            matched_rc = None
            for rc in t.rule_configs:
                if rc.get("name") == rr["name"]:
                    matched_rc = rc
                    break
            if matched_rc:
                if matched_rc.get("config"):
                    merged_config = {**rr["config"], **matched_rc["config"]}
                    rr["config"] = merged_config
                if matched_rc.get("target_field"):
                    rr["target_field"] = matched_rc["target_field"]

    ds_type = ds.type.value if hasattr(ds.type, "value") else ds.type
    datasource_config = {"type": ds_type, "config": ds.config}
    if t.table_name:
        datasource_config["table"] = t.table_name

    debug_request_data = {
        "task_id": t.id,
        "task_name": t.name,
        "datasource_config": datasource_config,
        "rule_configs": resolved_rules,
        "field_mapping": t.field_mapping,
    }
    _log_request(t.id, "start_request", debug_request_data)

    t.status = TaskStatus.RUNNING
    t.started_at = datetime.utcnow()
    await session.commit()
    await session.refresh(t)

    atask = asyncio.create_task(
        run_evaluation_task(
            task_id=t.id,
            datasource_config=datasource_config,
            rule_configs=resolved_rules,
            task_name=t.name,
            field_mapping=t.field_mapping,
            sampling=t.sampling,
        )
    )
    _running_tasks[t.id] = atask

    atask.add_done_callback(lambda _: _finalize_task(t.id))

    return _to_response(t)


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(task_id: int, session: AsyncSession = Depends(get_session)):
    t = await session.get(Task, task_id)
    if not t:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Task not found")

    if t.status != TaskStatus.RUNNING:
        raise HTTPException(status_code=400, detail="Task is not running")

    atask = _running_tasks.get(task_id)
    if atask and not atask.done():
        atask.cancel()

    t.status = TaskStatus.CANCELLED
    t.finished_at = datetime.utcnow()
    await session.commit()
    await session.refresh(t)
    return _to_response(t)


@router.delete("/{task_id}")
async def delete_task(task_id: int, session: AsyncSession = Depends(get_session)):
    t = await session.get(Task, task_id)
    if not t:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Task not found")

    if t.status == TaskStatus.RUNNING:
        atask = _running_tasks.get(task_id)
        if atask and not atask.done():
            atask.cancel()

    await session.delete(t)
    await session.commit()
    return {"ok": True}


@router.get("/{task_id}/report")
async def get_report(task_id: int, session: AsyncSession = Depends(get_session)):
    t = await session.get(Task, task_id)
    if not t:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Task not found")

    progress_data = get_task_progress(t.id)
    summary = None
    if progress_data and "summary" in progress_data:
        summary = progress_data["summary"]
    elif t.summary:
        summary = t.summary

    bad_data = get_bad_data(t.id, t.name, summary)

    return {
        "task_id": t.id,
        "task_name": t.name,
        "status": t.status.value if hasattr(t.status, "value") else t.status,
        "score": progress_data.get("score") if progress_data else t.score,
        "num_good": progress_data.get("num_good", t.num_good) if progress_data else t.num_good,
        "num_bad": progress_data.get("num_bad", t.num_bad) if progress_data else t.num_bad,
        "total": progress_data.get("total", t.total) if progress_data else t.total,
        "type_ratio": summary.get("type_ratio") if summary else None,
        "metrics_score": summary.get("metrics_score") if summary else None,
        "bad_data": bad_data[:100],
    }


@router.get("/{task_id}/download")
async def download_bad_data(task_id: int, session: AsyncSession = Depends(get_session)):
    t = await session.get(Task, task_id)
    if not t:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Task not found")

    bad_data = get_bad_data(t.id, t.name, t.summary)
    content = json.dumps(bad_data, ensure_ascii=False, indent=2)

    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={
            "Content-Disposition": f"attachment; filename=bad_data_task_{t.id}.json"
        },
    )


@router.get("/{task_id}/download-csv")
async def download_bad_data_csv(task_id: int, session: AsyncSession = Depends(get_session)):
    import csv
    import io

    t = await session.get(Task, task_id)
    if not t:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Task not found")

    bad_data = get_bad_data(t.id, t.name, t.summary)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["序号", "质量维度", "规则名称", "问题标签", "问题原因", "检测字段", "数据内容", "数据提示"])

    row_idx = 0
    for item in bad_data:
        dingo_result = item.get("dingo_result", {})
        eval_details = dingo_result.get("eval_details", {})
        prompt = item.get("prompt", "") or ""
        raw_content = item.get("content", "") or ""
        content_str = str(raw_content)[:500] if raw_content else ""

        for field_name, details in eval_details.items():
            if not isinstance(details, list):
                continue
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                if detail.get("status") is not True:
                    continue

                labels = detail.get("label") or []
                reasons = detail.get("reason") or []
                metric = detail.get("metric", "")

                full_label = labels[0] if labels else ""
                reason_str = "; ".join(str(r) for r in reasons) if reasons else ""

                if full_label and "." in full_label:
                    dimension = full_label.rsplit(".", 1)[0]
                else:
                    dimension = ""

                row_idx += 1
                writer.writerow([
                    row_idx,
                    dimension,
                    metric,
                    full_label,
                    reason_str,
                    field_name,
                    content_str,
                    str(prompt)[:500] if prompt else "",
                ])

    csv_content = output.getvalue()
    output.close()

    bom = "\ufeff"
    return StreamingResponse(
        iter([bom + csv_content]),
        media_type="text/csv; charset=utf-8-sig",
        headers={
            "Content-Disposition": f"attachment; filename=bad_data_task_{t.id}.csv"
        },
    )


def _finalize_task(task_id: int):
    progress_data = get_task_progress(task_id)
    if not progress_data:
        return

    import asyncio

    async def _update():
        from backend.database import async_session
        async with async_session() as session:
            t = await session.get(Task, task_id)
            if not t:
                return
            status = progress_data.get("status", "completed")
            t.status = TaskStatus(status)
            t.progress = progress_data.get("progress", 100.0)
            t.total = progress_data.get("total", 0)
            t.processed = progress_data.get("processed", 0)
            t.score = progress_data.get("score")
            t.num_good = progress_data.get("num_good", 0)
            t.num_bad = progress_data.get("num_bad", 0)
            t.summary = progress_data.get("summary")
            t.error_message = progress_data.get("error_message")
            t.finished_at = datetime.utcnow()
            await session.commit()

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_update())
        else:
            loop.run_until_complete(_update())
    except RuntimeError:
        pass
