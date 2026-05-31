import asyncio
import json
from datetime import datetime

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

    ds_type = ds.type.value if hasattr(ds.type, "value") else ds.type
    datasource_config = {"type": ds_type, "config": ds.config}

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

    bad_data = get_bad_data(t.id, t.name)

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

    bad_data = get_bad_data(t.id, t.name)
    content = json.dumps(bad_data, ensure_ascii=False, indent=2)

    return StreamingResponse(
        iter([content]),
        media_type="application/json",
        headers={
            "Content-Disposition": f"attachment; filename=bad_data_task_{t.id}.json"
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
