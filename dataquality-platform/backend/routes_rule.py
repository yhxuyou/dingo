import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_session
from backend.models import Rule
from backend.schemas import RuleCreate, RuleUpdate, RuleResponse
from backend.engine import get_builtin_rules, get_builtin_rules_grouped, get_rule_default_config

router = APIRouter(prefix="/api/rules", tags=["rules"])

VALID_RULE_TYPES = {"pattern", "keyword", "length", "regex", "threshold", "python", "sql"}
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
    elif rule_type == "threshold":
        valid_metrics = {"char_ratio", "word_ratio", "line_ratio", "count", "regex_count", "regex_ratio", "repetition", "uniqueness"}
        metric = config.get("metric", "")
        if not metric or metric not in valid_metrics:
            raise HTTPException(status_code=422, detail=f"无效的检测指标，可选值: {', '.join(sorted(valid_metrics))}")
        operator = config.get("operator", "gt")
        valid_operators = {"gt", "lt", "gte", "lte", "eq", "between"}
        if operator not in valid_operators:
            raise HTTPException(status_code=422, detail=f"无效的比较运算符，可选值: {', '.join(sorted(valid_operators))}")
        if "threshold" not in config:
            raise HTTPException(status_code=422, detail="threshold 类型规则必须提供 threshold 阈值")
        if metric in ("regex_count", "regex_ratio"):
            metric_params = config.get("metric_params", {})
            if not metric_params.get("pattern"):
                raise HTTPException(status_code=422, detail="正则匹配类指标必须在 metric_params 中提供 pattern")
        if metric == "word_ratio":
            metric_params = config.get("metric_params", {})
            if not metric_params.get("key_list"):
                raise HTTPException(status_code=422, detail="词占比指标必须在 metric_params 中提供 key_list")
    elif rule_type == "python":
        code = config.get("code", "")
        if not code or not code.strip():
            raise HTTPException(status_code=422, detail="python 类型规则必须提供 code 代码")
        try:
            import ast
            ast.parse(code)
        except SyntaxError as e:
            raise HTTPException(status_code=422, detail=f"Python 代码语法错误: {e}")
        from backend.python_sandbox import validate_python_code
        is_safe, msg = validate_python_code(code)
        if not is_safe:
            raise HTTPException(status_code=422, detail=f"Python 代码安全检查未通过: {msg}")
    elif rule_type == "sql":
        sql = config.get("sql", "")
        if not sql or not sql.strip():
            raise HTTPException(status_code=422, detail="sql 类型规则必须提供 sql 查询语句")
        from backend.sql_safety import validate_sql_safety
        is_safe, msg = validate_sql_safety(sql)
        if not is_safe:
            raise HTTPException(status_code=422, detail=f"SQL 安全检查未通过: {msg}")
        operator = config.get("operator", "gt")
        valid_operators = {"gt", "lt", "gte", "lte", "eq", "between"}
        if operator not in valid_operators:
            raise HTTPException(status_code=422, detail=f"无效的比较运算符，可选值: {', '.join(sorted(valid_operators))}")


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
        {"value": "pattern", "label": "正则模式匹配", "group": "basic", "description": "使用正则表达式列表匹配数据中的问题模式"},
        {"value": "keyword", "label": "关键词检测", "group": "basic", "description": "检测数据中是否包含指定关键词"},
        {"value": "length", "label": "长度校验", "group": "basic", "description": "校验文本长度是否在指定范围内"},
        {"value": "regex", "label": "单正则匹配", "group": "basic", "description": "使用单个正则表达式检测数据问题"},
        {"value": "threshold", "label": "阈值规则", "group": "advanced", "description": "组合检测指标与阈值判断，支持字符占比、词占比、正则匹配计数等"},
        {"value": "python", "label": "Python 脚本", "group": "expert", "description": "自定义 Python 检测逻辑，编写 evaluate 函数实现灵活检测"},
        {"value": "sql", "label": "SQL 查询", "group": "expert", "description": "数据库 SQL 质量评估，执行 SELECT 查询搭配阈值判断"},
    ]


@router.post("/test-python")
async def test_python_code(body: dict):
    code = body.get("code", "")
    test_content = body.get("test_content", "测试文本")
    timeout = body.get("timeout", 10)
    if not code.strip():
        raise HTTPException(status_code=422, detail="代码不能为空")
    try:
        import ast
        ast.parse(code)
    except SyntaxError as e:
        return {"success": False, "error": f"语法错误: {e}"}
    from backend.python_sandbox import validate_python_code, execute_in_sandbox
    is_safe, msg = validate_python_code(code)
    if not is_safe:
        return {"success": False, "error": f"安全检查未通过: {msg}"}
    try:
        result = execute_in_sandbox(code, test_content, timeout)
        return {"success": True, "result": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/test-sql")
async def test_sql_query(body: dict):
    sql = body.get("sql", "")
    datasource_id = body.get("datasource_id")
    table_name = body.get("table_name", "")
    if not sql.strip():
        raise HTTPException(status_code=422, detail="SQL 不能为空")
    from backend.sql_safety import validate_sql_safety
    is_safe, msg = validate_sql_safety(sql)
    if not is_safe:
        return {"success": False, "error": f"安全检查未通过: {msg}"}
    if not datasource_id:
        return {"success": False, "error": "请选择数据源"}
    from backend.database import get_session
    async for session in get_session():
        from backend.models import Datasource
        ds = await session.get(Datasource, datasource_id)
        if not ds:
            return {"success": False, "error": "数据源不存在"}
        ds_type = ds.type.value if hasattr(ds.type, 'value') else ds.type
        config = ds.config
        if ds_type == "local_file":
            return {"success": False, "error": "本地文件数据源不支持 SQL 查询"}
        try:
            from sqlalchemy import create_engine, text
            dialect_map = {"mysql": "mysql+pymysql", "postgresql": "postgresql+psycopg2", "oracle": "oracle+cx_oracle", "sqlserver": "mssql+pyodbc", "sqlite": "sqlite"}
            dialect = dialect_map.get(ds_type, ds_type)
            password_part = f":{config.get('password', '')}" if config.get("password") else ""
            port_part = f":{config.get('port', '')}" if config.get("port") else ""
            if ds_type == "sqlite":
                url = f"sqlite:///{config.get('database', '')}"
            else:
                url = f"{dialect}://{config.get('username', '')}{password_part}@{config.get('host', '')}{port_part}/{config.get('database', '')}"
            if table_name:
                quote = "`" if ds_type == "mysql" else '"'
                sql = sql.replace("{table}", f"{quote}{table_name}{quote}")
            engine = create_engine(url)
            with engine.connect() as conn:
                result = conn.execute(text(sql))
                columns = list(result.keys())
                rows = [dict(row._mapping) for row in result.fetchmany(10)]
            engine.dispose()
            for row in rows:
                for k, v in row.items():
                    if not isinstance(v, (str, int, float, bool, type(None))):
                        row[k] = str(v)
            return {"success": True, "columns": columns, "rows": rows}
        except Exception as e:
            return {"success": False, "error": str(e)}


@router.get("/sql-templates")
async def list_sql_templates():
    return [
        {"name": "空值检测", "sql": "SELECT COUNT(*) as cnt FROM {table} WHERE {column} IS NULL OR {column} = ''", "description": "检测空值或空字符串的数量", "operator": "gt", "threshold": 0},
        {"name": "重复检测", "sql": "SELECT COUNT(*) - COUNT(DISTINCT {column}) as cnt FROM {table}", "description": "检测重复值的数量", "operator": "gt", "threshold": 0},
        {"name": "格式检测", "sql": "SELECT COUNT(*) as cnt FROM {table} WHERE {column} != '' AND {column} NOT REGEXP '{pattern}'", "description": "检测不符合指定格式的记录数", "operator": "gt", "threshold": 0},
        {"name": "范围检测", "sql": "SELECT COUNT(*) as cnt FROM {table} WHERE {column} < {min} OR {column} > {max}", "description": "检测超出指定范围的记录数", "operator": "gt", "threshold": 0},
        {"name": "唯一性检测", "sql": "SELECT COUNT(*) - COUNT(DISTINCT {column}) as cnt FROM {table}", "description": "检测非唯一值的数量", "operator": "gt", "threshold": 0},
        {"name": "参照完整性", "sql": "SELECT COUNT(*) as cnt FROM {table} t1 LEFT JOIN {ref_table} t2 ON t1.{column} = t2.{ref_column} WHERE t2.{ref_column} IS NULL", "description": "检测外键引用不存在的记录数", "operator": "gt", "threshold": 0},
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


@router.get("/{rule_id}/default-config")
async def get_rule_config(rule_id: int, session: AsyncSession = Depends(get_session)):
    if rule_id < 0:
        builtin = get_builtin_rules()
        idx = abs(rule_id) - 1
        if 0 <= idx < len(builtin):
            br = builtin[idx]
            default_cfg = get_rule_default_config(br["name"])
            return {
                "rule_id": rule_id,
                "name": br["name"],
                "is_builtin": True,
                "rule_type": "builtin",
                "default_config": default_cfg,
                "configurable_fields": _get_configurable_fields(default_cfg),
            }
        raise HTTPException(status_code=404, detail="Rule not found")

    r = await session.get(Rule, rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="Rule not found")

    if r.is_builtin:
        default_cfg = get_rule_default_config(r.name)
        return {
            "rule_id": r.id,
            "name": r.name,
            "is_builtin": True,
            "rule_type": r.rule_type,
            "default_config": default_cfg,
            "configurable_fields": _get_configurable_fields(default_cfg),
        }

    return {
        "rule_id": r.id,
        "name": r.name,
        "is_builtin": False,
        "rule_type": r.rule_type,
        "default_config": r.config,
        "configurable_fields": _get_configurable_fields_for_custom(r.rule_type, r.config),
    }


def _get_configurable_fields(default_config: dict) -> list[dict]:
    fields = []
    if default_config.get("threshold") is not None:
        fields.append({"key": "threshold", "label": "阈值", "type": "number", "default": default_config["threshold"]})
    if default_config.get("pattern") is not None:
        fields.append({"key": "pattern", "label": "正则表达式", "type": "text", "default": default_config["pattern"]})
    if default_config.get("key_list") is not None:
        fields.append({"key": "key_list", "label": "关键词列表", "type": "list", "default": default_config["key_list"]})
    if default_config.get("refer_path") is not None:
        fields.append({"key": "refer_path", "label": "引用路径", "type": "list", "default": default_config["refer_path"]})
    if default_config.get("parameters") is not None:
        fields.append({"key": "parameters", "label": "额外参数", "type": "json", "default": default_config["parameters"]})
    return fields


def _get_configurable_fields_for_custom(rule_type: str, config: dict) -> list[dict]:
    fields = []
    if rule_type == "pattern":
        fields.append({"key": "patterns", "label": "正则模式列表", "type": "list", "default": config.get("patterns", [])})
    elif rule_type == "keyword":
        fields.append({"key": "keywords", "label": "关键词列表", "type": "list", "default": config.get("keywords", [])})
    elif rule_type == "length":
        fields.append({"key": "min_length", "label": "最小长度", "type": "number", "default": config.get("min_length", 0)})
        fields.append({"key": "max_length", "label": "最大长度", "type": "number", "default": config.get("max_length", 999999)})
    elif rule_type == "regex":
        fields.append({"key": "pattern", "label": "正则表达式", "type": "text", "default": config.get("pattern", "")})
    elif rule_type == "threshold":
        fields.append({"key": "metric", "label": "检测指标", "type": "text", "default": config.get("metric", "char_ratio")})
        fields.append({"key": "operator", "label": "比较运算符", "type": "text", "default": config.get("operator", "gt")})
        fields.append({"key": "threshold", "label": "阈值", "type": "number", "default": config.get("threshold", 0)})
    elif rule_type == "python":
        fields.append({"key": "code", "label": "Python 代码", "type": "text", "default": config.get("code", "")})
        fields.append({"key": "timeout", "label": "超时(秒)", "type": "number", "default": config.get("timeout", 10)})
    elif rule_type == "sql":
        fields.append({"key": "sql", "label": "SQL 查询", "type": "text", "default": config.get("sql", "")})
        fields.append({"key": "operator", "label": "比较运算符", "type": "text", "default": config.get("operator", "gt")})
        fields.append({"key": "threshold", "label": "阈值", "type": "number", "default": config.get("threshold", 0)})
    return fields
