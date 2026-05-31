import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_session
from backend.models import Rule
from backend.schemas import RuleCreate, RuleUpdate, RuleResponse
from backend.engine import get_builtin_rules, get_builtin_rules_grouped

router = APIRouter(prefix="/api/rules", tags=["rules"])

VALID_RULE_TYPES = {"pattern", "keyword", "length", "regex"}
VALID_METRIC_TYPES = {
    "QUALITY_BAD_EFFECTIVENESS",
    "QUALITY_BAD_FLUENCY",
    "QUALITY_BAD_UNDERSTANDABILITY",
    "QUALITY_BAD_COMPLETENESS",
    "QUALITY_BAD_SIMILARITY",
    "QUALITY_BAD_SECURITY",
    "QUALITY_BAD_RELEVANCE",
}


def _to_response(r: Rule) -> RuleResponse:
    return RuleResponse(
        id=r.id,
        name=r.name,
        metric_type=r.metric_type,
        group=r.group,
        rule_type=r.rule_type,
        is_builtin=r.is_builtin,
        description=r.description,
        config=r.config,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _validate_rule_config(rule_type: str, config: dict):
    if rule_type == "pattern":
        patterns = config.get("patterns", [])
        if not patterns or not isinstance(patterns, list):
            raise HTTPException(status_code=422, detail="pattern 类型规则必须提供 patterns 列表")
        for p in patterns:
            if not isinstance(p, str) or not p.strip():
                raise HTTPException(status_code=422, detail="patterns 中每项必须为非空字符串")
            try:
                re.compile(p)
            except re.error as e:
                raise HTTPException(status_code=422, detail=f"正则表达式无效: {p}, 错误: {e}")
    elif rule_type == "keyword":
        keywords = config.get("keywords", [])
        if not keywords or not isinstance(keywords, list):
            raise HTTPException(status_code=422, detail="keyword 类型规则必须提供 keywords 列表")
        for kw in keywords:
            if not isinstance(kw, str) or not kw.strip():
                raise HTTPException(status_code=422, detail="keywords 中每项必须为非空字符串")
    elif rule_type == "length":
        if "min_length" not in config and "max_length" not in config:
            raise HTTPException(status_code=422, detail="length 类型规则必须提供 min_length 或 max_length")
        if "min_length" in config and not isinstance(config["min_length"], int):
            raise HTTPException(status_code=422, detail="min_length 必须为整数")
        if "max_length" in config and not isinstance(config["max_length"], int):
            raise HTTPException(status_code=422, detail="max_length 必须为整数")
    elif rule_type == "regex":
        pattern = config.get("pattern", "")
        if not pattern or not isinstance(pattern, str):
            raise HTTPException(status_code=422, detail="regex 类型规则必须提供 pattern 字符串")
        try:
            re.compile(pattern)
        except re.error as e:
            raise HTTPException(status_code=422, detail=f"正则表达式无效: {e}")


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


@router.get("/rule-types")
async def list_rule_types():
    return [
        {"value": "pattern", "label": "正则模式匹配", "description": "使用正则表达式列表匹配数据中的问题模式"},
        {"value": "keyword", "label": "关键词检测", "description": "检测数据中是否包含指定关键词"},
        {"value": "length", "label": "长度校验", "description": "校验文本长度是否在指定范围内"},
        {"value": "regex", "label": "单正则匹配", "description": "使用单个正则表达式检测数据问题"},
    ]


@router.get("", response_model=list[RuleResponse])
async def list_rules(
    search: str = Query(None),
    dimension: str = Query(None),
    rule_type: str = Query(None),
    is_builtin: bool = Query(None),
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Rule).order_by(Rule.metric_type, Rule.name)
    if search:
        stmt = stmt.where(Rule.name.contains(search))
    if dimension:
        stmt = stmt.where(Rule.metric_type == dimension)
    if rule_type:
        stmt = stmt.where(Rule.rule_type == rule_type)
    if is_builtin is not None:
        stmt = stmt.where(Rule.is_builtin == is_builtin)
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
        if is_builtin is False:
            continue
        if br["name"] not in seen:
            seen.add(br["name"])
            builtin_responses.append(RuleResponse(
                id=-(idx + 1),
                name=br["name"],
                metric_type=br["metric_type"],
                group=br["groups"][0] if br["groups"] else "default",
                rule_type="builtin",
                is_builtin=True,
                description=br.get("description"),
                config={},
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ))

    if is_builtin is True:
        return builtin_responses
    if is_builtin is False:
        return custom_rules
    return builtin_responses + custom_rules


@router.get("/{rule_id}", response_model=RuleResponse)
async def get_rule(rule_id: int, session: AsyncSession = Depends(get_session)):
    if rule_id < 0:
        builtin = get_builtin_rules()
        idx = abs(rule_id) - 1
        if 0 <= idx < len(builtin):
            br = builtin[idx]
            return RuleResponse(
                id=rule_id,
                name=br["name"],
                metric_type=br["metric_type"],
                group=br["groups"][0] if br["groups"] else "default",
                rule_type="builtin",
                is_builtin=True,
                description=br.get("description"),
                config={},
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        raise HTTPException(status_code=404, detail="Rule not found")

    r = await session.get(Rule, rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="Rule not found")
    return _to_response(r)


@router.post("", response_model=RuleResponse)
async def create_rule(
    body: RuleCreate,
    session: AsyncSession = Depends(get_session),
):
    if not body.name or not body.name.strip():
        raise HTTPException(status_code=422, detail="规则名称不能为空")

    existing = await session.execute(
        select(Rule).where(Rule.name == body.name.strip())
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"规则名称 '{body.name}' 已存在")

    builtin_names = {r["name"] for r in get_builtin_rules()}
    if body.name in builtin_names:
        raise HTTPException(status_code=409, detail=f"规则名称 '{body.name}' 与内置规则冲突")

    if body.metric_type not in VALID_METRIC_TYPES:
        raise HTTPException(status_code=422, detail=f"无效的质量维度，可选值: {', '.join(sorted(VALID_METRIC_TYPES))}")

    rt = body.rule_type or "pattern"
    if rt not in VALID_RULE_TYPES:
        raise HTTPException(status_code=422, detail=f"无效的规则类型，可选值: {', '.join(sorted(VALID_RULE_TYPES))}")

    _validate_rule_config(rt, body.config)

    r = Rule(
        name=body.name.strip(),
        metric_type=body.metric_type,
        group=body.group or "custom",
        rule_type=rt,
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
        raise HTTPException(status_code=404, detail="规则不存在")
    if r.is_builtin:
        raise HTTPException(status_code=400, detail="内置规则不可修改")

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="规则名称不能为空")
        existing = await session.execute(
            select(Rule).where(Rule.name == name, Rule.id != rule_id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"规则名称 '{name}' 已被其他规则使用")
        builtin_names = {r_["name"] for r_ in get_builtin_rules()}
        if name in builtin_names:
            raise HTTPException(status_code=409, detail=f"规则名称 '{name}' 与内置规则冲突")
        r.name = name

    if body.metric_type is not None:
        if body.metric_type not in VALID_METRIC_TYPES:
            raise HTTPException(status_code=422, detail=f"无效的质量维度，可选值: {', '.join(sorted(VALID_METRIC_TYPES))}")
        r.metric_type = body.metric_type

    if body.rule_type is not None:
        if body.rule_type not in VALID_RULE_TYPES:
            raise HTTPException(status_code=422, detail=f"无效的规则类型，可选值: {', '.join(sorted(VALID_RULE_TYPES))}")
        r.rule_type = body.rule_type

    if body.group is not None:
        r.group = body.group

    if body.description is not None:
        r.description = body.description

    if body.config is not None:
        rt = body.rule_type or r.rule_type
        _validate_rule_config(rt, body.config)
        r.config = body.config

    await session.commit()
    await session.refresh(r)
    return _to_response(r)


@router.delete("/{rule_id}")
async def delete_rule(rule_id: int, session: AsyncSession = Depends(get_session)):
    if rule_id < 0:
        raise HTTPException(status_code=400, detail="内置规则不可删除")

    r = await session.get(Rule, rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="规则不存在")
    if r.is_builtin:
        raise HTTPException(status_code=400, detail="内置规则不可删除")

    from backend.models import Task
    tasks = await session.execute(select(Task))
    for task in tasks.scalars().all():
        import json
        try:
            rids = json.loads(task.rule_ids)
            if rule_id in rids:
                raise HTTPException(
                    status_code=409,
                    detail=f"规则正在被任务 '{task.name}'(ID={task.id}) 使用，无法删除",
                )
        except (json.JSONDecodeError, TypeError):
            pass

    await session.delete(r)
    await session.commit()
    return {"ok": True, "message": f"规则 '{r.name}' 已删除"}
