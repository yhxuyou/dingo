from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_session
from backend.models import Rule
from backend.schemas import RuleCreate, RuleUpdate, RuleResponse
from backend.engine import get_builtin_rules, get_builtin_rules_grouped

router = APIRouter(prefix="/api/rules", tags=["rules"])


def _to_response(r: Rule) -> RuleResponse:
    return RuleResponse(
        id=r.id,
        name=r.name,
        metric_type=r.metric_type,
        group=r.group,
        is_builtin=r.is_builtin,
        description=r.description,
        config=r.config,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


@router.get("/builtin")
async def list_builtin_rules():
    return get_builtin_rules()


@router.get("/builtin/grouped")
async def list_builtin_rules_grouped():
    return get_builtin_rules_grouped()


@router.get("/dimensions")
async def list_dimensions():
    grouped = get_builtin_rules_grouped()
    dimensions = []
    for metric_type, rules in sorted(grouped.items()):
        dimensions.append({
            "name": metric_type,
            "label": metric_type.replace("QUALITY_BAD_", "").replace("_", " ").title(),
            "count": len(rules),
            "rules": rules,
        })
    return dimensions


@router.get("", response_model=list[RuleResponse])
async def list_rules(
    search: str = Query(None),
    dimension: str = Query(None),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Rule).order_by(Rule.metric_type, Rule.name)
    if search:
        stmt = stmt.where(Rule.name.contains(search))
    if dimension:
        stmt = stmt.where(Rule.metric_type == dimension)
    result = await session.execute(stmt)
    custom_rules = [_to_response(r) for r in result.scalars().all()]

    builtin = get_builtin_rules()
    builtin_responses = []
    seen = set()
    for idx, br in enumerate(builtin):
        if search and search.lower() not in br["name"].lower():
            continue
        if dimension and br["metric_type"] != dimension:
            continue
        if br["name"] not in seen:
            seen.add(br["name"])
            builtin_responses.append(RuleResponse(
                id=-(idx + 1),
                name=br["name"],
                metric_type=br["metric_type"],
                group=br["groups"][0] if br["groups"] else "default",
                is_builtin=True,
                description=br.get("description"),
                config={},
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ))

    return builtin_responses + custom_rules


@router.post("", response_model=RuleResponse)
async def create_rule(
    body: RuleCreate,
    session: AsyncSession = Depends(get_session),
):
    r = Rule(
        name=body.name,
        metric_type=body.metric_type,
        group=body.group or "custom",
        is_builtin=False,
        description=body.description,
        config=body.config,
    )
    session.add(r)
    await session.commit()
    await session.refresh(r)
    return _to_response(r)


@router.put("/{rule_id}", response_model=RuleResponse)
async def update_rule(
    rule_id: int,
    body: RuleUpdate,
    session: AsyncSession = Depends(get_session),
):
    r = await session.get(Rule, rule_id)
    if not r:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Rule not found")
    if r.is_builtin:
        raise HTTPException(status_code=400, detail="Cannot modify builtin rules")
    if body.name is not None:
        r.name = body.name
    if body.metric_type is not None:
        r.metric_type = body.metric_type
    if body.group is not None:
        r.group = body.group
    if body.description is not None:
        r.description = body.description
    if body.config is not None:
        r.config = body.config
    await session.commit()
    await session.refresh(r)
    return _to_response(r)


@router.delete("/{rule_id}")
async def delete_rule(rule_id: int, session: AsyncSession = Depends(get_session)):
    r = await session.get(Rule, rule_id)
    if not r:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Rule not found")
    if r.is_builtin:
        raise HTTPException(status_code=400, detail="Cannot delete builtin rules")
    await session.delete(r)
    await session.commit()
    return {"ok": True}
