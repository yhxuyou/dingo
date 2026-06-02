import asyncio
import json
import os
import re
import sys
import traceback
import uuid
from typing import Optional

# 在导入 dingo 模块之前设置环境变量，强制使用线程模式
os.environ["LOCAL_DEPLOYMENT_MODE"] = "true"

# 将项目内的 dingo 目录添加到 sys.path 最前面，确保优先使用项目内的 dingo
project_dingo_dir = os.path.join(os.path.dirname(__file__), "dingo")
sys.path.insert(0, project_dingo_dir)

# 将项目根目录添加到 sys.path
sys.path.insert(0, os.path.dirname(__file__))

from dingo.config import InputArgs
from dingo.config.input_args import (
    DatasetArgs,
    DatasetSqlArgs,
    DatasetCsvArgs,
    DatasetExcelArgs,
    DatasetParquetArgs,
    ExecutorArgs,
    ExecutorResultSaveArgs,
    SamplingArgs,
    EvalPipline,
    EvalPiplineConfig,
    EvaluatorRuleArgs,
)
from dingo.exec import Executor
from dingo.model import Model

Model.load_model()

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "outputs")
os.makedirs(OUTPUT_DIR, exist_ok=True)

_task_store: dict[int, dict] = {}


def get_builtin_rules() -> list[dict]:
    rules = []
    for name, cls in sorted(Model.rule_name_map.items()):
        metric_type = getattr(cls, "metric_type", "")
        groups = getattr(cls, "group", [])
        required = [f.value for f in getattr(cls, "_required_fields", [])]
        info = getattr(cls, "_metric_info", {})
        rules.append({
            "name": name,
            "metric_type": metric_type,
            "groups": groups,
            "required_fields": required,
            "description": info.get("description", ""),
            "quality_dimension": info.get("quality_dimension", ""),
        })
    return rules


def get_rule_default_config(rule_name: str) -> dict:
    cls = Model.rule_name_map.get(rule_name)
    if not cls:
        return {}
    dc = getattr(cls, "dynamic_config", None)
    if dc is None:
        return {}
    return dc.model_dump()


def get_builtin_rules_grouped() -> dict[str, list[dict]]:
    rules = get_builtin_rules()
    grouped: dict[str, list[dict]] = {}
    for rule in rules:
        mt = rule["metric_type"]
        if mt not in grouped:
            grouped[mt] = []
        grouped[mt].append(rule)
    return grouped


def get_llm_evaluators() -> list[dict]:
    evaluators = []
    for name, cls in sorted(Model.llm_name_map.items()):
        required = [f.value for f in getattr(cls, "_required_fields", [])]
        evaluators.append({
            "name": name,
            "required_fields": required,
        })
    return evaluators


def build_input_args(
    datasource_config: dict,
    rule_configs: list[dict],
    task_name: str = "dataquality_task",
    field_mapping: Optional[dict] = None,
    sampling: Optional[dict] = None,
) -> InputArgs:
    ds_type = datasource_config.get("type", "local_file")
    ds_config = datasource_config.get("config", {})

    dataset_args = DatasetArgs()
    input_path = ""

    if ds_type == "local_file":
        input_path = ds_config.get("file_path", "")
        fmt = ds_config.get("format", "")
        if not fmt:
            ext = os.path.splitext(input_path)[1].lower()
            fmt_map = {
                ".json": "json", ".jsonl": "jsonl", ".txt": "plaintext",
                ".csv": "csv", ".xlsx": "excel", ".xls": "excel",
                ".parquet": "parquet",
            }
            fmt = fmt_map.get(ext, "jsonl")
        dataset_args.source = "local"
        dataset_args.format = fmt
        if fmt == "csv":
            dataset_args.csv_config = DatasetCsvArgs(
                encoding=ds_config.get("encoding", "utf-8"),
                delimiter=ds_config.get("delimiter"),
            )
        elif fmt in ("excel",):
            dataset_args.excel_config = DatasetExcelArgs(
                sheet_name=ds_config.get("sheet_name", 0),
            )
        elif fmt == "parquet":
            dataset_args.parquet_config = DatasetParquetArgs()
    elif ds_type in ("mysql", "postgresql", "oracle", "sqlserver", "sqlite"):
        dialect_map = {
            "mysql": "mysql",
            "postgresql": "postgresql",
            "oracle": "oracle",
            "sqlserver": "mssql",
            "sqlite": "sqlite",
        }
        driver_map = {
            "mysql": "pymysql",
            "postgresql": "psycopg2",
            "oracle": "cx_oracle",
            "sqlserver": "pyodbc",
            "sqlite": "",
        }
        table_name = datasource_config.get("table", "")
        query = ds_config.get("query", "")
        if table_name and (not query or query.strip() in ("SELECT 1", "")):
            quote = "`" if ds_type == "mysql" else '"'
            query = f"SELECT * FROM {quote}{table_name}{quote}"
        input_path = query
        dataset_args.source = "sql"
        dataset_args.format = "jsonl"
        dataset_args.sql_config = DatasetSqlArgs(
            dialect=dialect_map.get(ds_type, ds_type),
            driver=ds_config.get("driver", driver_map.get(ds_type, "")),
            username=ds_config.get("username", ""),
            password=ds_config.get("password", ""),
            host=ds_config.get("host", ""),
            port=str(ds_config.get("port", "")),
            database=ds_config.get("database", ""),
            connect_args=ds_config.get("connect_args", ""),
        )

    evals = []
    for rc in rule_configs:
        rule_name = rc.get("name", "")
        rule_config = rc.get("config", {})
        rule_type = rc.get("rule_type", "")
        target_field = rc.get("target_field", "content")

        if rule_name in Model.rule_name_map:
            # 内置规则：直接传入完整 config（含 threshold, pattern, key_list, parameters 等）
            eval_config = None
            if rule_config:
                # 分离 EvaluatorRuleArgs 支持的字段和 parameters
                known_fields = {}
                params = rule_config.get("parameters", {})
                for k, v in rule_config.items():
                    if k in ("threshold", "pattern", "key_list", "refer_path"):
                        known_fields[k] = v
                if params:
                    known_fields["parameters"] = params
                if known_fields:
                    eval_config = EvaluatorRuleArgs(**known_fields)
            evals.append(EvalPiplineConfig(name=rule_name, config=eval_config))
        elif rule_type in ("pattern", "regex"):
            pattern = rule_config.get("pattern", "")
            if rule_type == "pattern":
                patterns = rule_config.get("patterns", [])
                if patterns:
                    pattern = "|".join(f"({p})" for p in patterns)
            if pattern:
                eval_config = EvaluatorRuleArgs(pattern=pattern)
                evals.append(EvalPiplineConfig(name="RulePatternSearch", config=eval_config))
        elif rule_type == "keyword":
            keywords = rule_config.get("keywords", [])
            if keywords:
                pattern = "|".join(f"({re.escape(kw)})" for kw in keywords)
                eval_config = EvaluatorRuleArgs(pattern=pattern)
                evals.append(EvalPiplineConfig(name="RulePatternSearch", config=eval_config))
        elif rule_type == "length":
            min_len = rule_config.get("min_length", 0)
            max_len = rule_config.get("max_length", 999999)
            key_list = [str(min_len), str(max_len)]
            eval_config = EvaluatorRuleArgs(key_list=key_list)
            evals.append(EvalPiplineConfig(name="RuleWordNumber", config=eval_config))
        elif rule_type == "threshold":
            metric_params = rule_config.get("metric_params", {})
            eval_config = EvaluatorRuleArgs(
                threshold=rule_config.get("threshold", 0),
                pattern=metric_params.get("pattern"),
                key_list=metric_params.get("key_list"),
                parameters={
                    "metric": rule_config.get("metric", "char_ratio"),
                    "operator": rule_config.get("operator", "gt"),
                    "metric_params": metric_params,
                    "threshold_max": rule_config.get("threshold_max"),
                }
            )
            evals.append(EvalPiplineConfig(name="RuleThresholdCheck", config=eval_config))
        elif rule_type == "python":
            eval_config = EvaluatorRuleArgs(
                parameters={
                    "code": rule_config.get("code", ""),
                    "timeout": rule_config.get("timeout", 10),
                }
            )
            evals.append(EvalPiplineConfig(name="RuleCustomPython", config=eval_config))

    if field_mapping:
        evaluator = [EvalPipline(fields=field_mapping, evals=evals)]
    else:
        field_groups = {}
        for i, e_c in enumerate(evals):
            tf = rule_configs[i].get("target_field", "content") if i < len(rule_configs) else "content"
            field_groups.setdefault(tf, []).append(e_c)
        evaluator = []
        for tf, eg in field_groups.items():
            fields_list = [f.strip() for f in tf.split(",") if f.strip()]
            if len(fields_list) > 1:
                for single_field in fields_list:
                    evaluator.append(EvalPipline(fields={"content": single_field}, evals=eg))
            elif fields_list:
                evaluator.append(EvalPipline(fields={"content": fields_list[0]}, evals=eg))
            else:
                evaluator.append(EvalPipline(fields={}, evals=eg))
        if not evaluator:
            evaluator = [EvalPipline(fields={}, evals=evals)]

    sampling_args = SamplingArgs()
    if sampling:
        sampling_args = SamplingArgs(
            mode=sampling.get("mode", "full"),
            size=sampling.get("size"),
            seed=sampling.get("seed"),
        )

    return InputArgs(
        task_name=task_name,
        input_path=input_path,
        output_path=OUTPUT_DIR,
        log_level="WARNING",
        dataset=dataset_args,
        executor=ExecutorArgs(
            max_workers=ds_config.get("max_workers", 1),
            batch_size=ds_config.get("batch_size", 10),
            result_save=ExecutorResultSaveArgs(
                bad=True, good=False, all_labels=True, raw=True, merge=True,
            ),
            sampling=sampling_args,
        ),
        evaluator=evaluator,
    )


async def run_evaluation_task(
    task_id: int,
    datasource_config: dict,
    rule_configs: list[dict],
    task_name: str,
    field_mapping: Optional[dict] = None,
    sampling: Optional[dict] = None,
):
    _task_store[task_id] = {
        "status": "running",
        "progress": 0.0,
        "total": 0,
        "processed": 0,
    }

    DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_requests")
    os.makedirs(DEBUG_DIR, exist_ok=True)

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    engine_debug_file = os.path.join(DEBUG_DIR, f"task_{task_id}_engine_input_{timestamp}.json")
    engine_debug_data = {
        "task_id": task_id,
        "task_name": task_name,
        "datasource_config": datasource_config,
        "rule_configs": rule_configs,
        "field_mapping": field_mapping,
    }
    with open(engine_debug_file, "w", encoding="utf-8") as f:
        json.dump(engine_debug_data, f, ensure_ascii=False, indent=2)
    print(f"[ENGINE DEBUG] 任务 {task_id} 引擎输入已保存: {engine_debug_file}")

    try:
        # 设置环境变量，强制使用线程模式而不是多进程，确保类型转换正确生效
        os.environ["LOCAL_DEPLOYMENT_MODE"] = "true"
        
        sql_rules = [rc for rc in rule_configs if rc.get("rule_type") == "sql"]
        text_rule_configs = [rc for rc in rule_configs if rc.get("rule_type") != "sql"]

        input_args = build_input_args(
            datasource_config, text_rule_configs, task_name, field_mapping, sampling
        )

        input_args_file = os.path.join(DEBUG_DIR, f"task_{task_id}_input_args_{timestamp}.json")
        input_args_debug = {
            "task_name": input_args.task_name,
            "input_path": input_args.input_path,
            "input_args": input_args.model_dump(),
            "dataset": {
                "source": input_args.dataset.source,
                "format": input_args.dataset.format,
                "sql_config": input_args.dataset.sql_config.model_dump() if input_args.dataset.sql_config else None,
            },
            "evaluator": [
                {
                    "fields": ep.fields,
                    "evals": [
                        {"name": e.name, "config": e.config.model_dump() if e.config else None}
                        for e in ep.evals
                    ]
                }
                for ep in input_args.evaluator
            ],
        }
        with open(input_args_file, "w", encoding="utf-8") as f:
            json.dump(input_args_debug, f, ensure_ascii=False, indent=2)
        print(f"[ENGINE DEBUG] 任务 {task_id} InputArgs 已保存: {input_args_file}")

        loop = asyncio.get_event_loop()

        def _execute():
            try:
                executor = Executor.exec_map["local"](input_args)
                return executor.execute()
            finally:
                pass

        result = await loop.run_in_executor(None, _execute)

        sql_results = []
        if sql_rules:
            try:
                sql_results = await _execute_sql_rules(task_id, datasource_config, sql_rules)
            except Exception as e:
                print(f"[ENGINE ERROR] SQL 规则执行失败: {e}")

        _task_store[task_id] = {
            "status": "completed",
            "progress": 100.0,
            "total": result.total,
            "processed": result.total,
            "score": result.score,
            "num_good": result.num_good,
            "num_bad": result.num_bad,
            "summary": result.to_dict(),
        }

    except asyncio.CancelledError:
        _task_store[task_id] = {
            "status": "cancelled",
            "progress": _task_store[task_id].get("progress", 0),
            "total": _task_store[task_id].get("total", 0),
            "processed": _task_store[task_id].get("processed", 0),
            "error_message": "Task was cancelled",
        }
    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"[ENGINE ERROR] 任务 {task_id} 执行失败，完整堆栈:")
        print(error_detail)
        _task_store[task_id] = {
            "status": "failed",
            "progress": _task_store[task_id].get("progress", 0),
            "total": _task_store[task_id].get("total", 0),
            "processed": _task_store[task_id].get("processed", 0),
            "error_message": str(e),
        }


def get_task_progress(task_id: int) -> Optional[dict]:
    return _task_store.get(task_id)


def get_bad_data(task_id: int, task_name: str, summary: dict = None) -> list[dict]:
    task_dir = None

    if summary and summary.get("output_path"):
        candidate = summary["output_path"]
        if os.path.isdir(candidate):
            task_dir = candidate

    if not task_dir:
        for d in os.listdir(OUTPUT_DIR):
            full_path = os.path.join(OUTPUT_DIR, d)
            if os.path.isdir(full_path) and task_name in d:
                task_dir = full_path
                break

    if not task_dir:
        return []

    all_results_file = os.path.join(task_dir, "all_results.jsonl")
    if not os.path.exists(all_results_file):
        return []

    bad_data = []
    with open(all_results_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                dingo_result = data.get("dingo_result", {})
                if dingo_result.get("eval_status", False):
                    bad_data.append(data)
            except json.JSONDecodeError:
                continue

    return bad_data


async def _execute_sql_rules(task_id: int, datasource_config: dict, sql_rules: list) -> list:
    results = []
    ds_type = datasource_config.get("type", "local_file")
    ds_config = datasource_config.get("config", {})
    table_name = datasource_config.get("table", "")

    if ds_type == "local_file":
        return [{"rule_name": rc.get("name", ""), "passed": True, "reason": "本地文件数据源不支持 SQL 规则", "value": None} for rc in sql_rules]

    try:
        from sqlalchemy import create_engine, text as sql_text
        dialect_map = {"mysql": "mysql+pymysql", "postgresql": "postgresql+psycopg2", "oracle": "oracle+cx_oracle", "sqlserver": "mssql+pyodbc", "sqlite": "sqlite"}
        driver_map = {"mysql": "pymysql", "postgresql": "psycopg2", "oracle": "cx_oracle", "sqlserver": "pyodbc", "sqlite": ""}
        dialect = dialect_map.get(ds_type, ds_type)
        password_part = f":{ds_config.get('password', '')}" if ds_config.get("password") else ""
        port_part = f":{ds_config.get('port', '')}" if ds_config.get("port") else ""

        if ds_type == "sqlite":
            url = f"sqlite:///{ds_config.get('database', '')}"
        else:
            url = f"{dialect}://{ds_config.get('username', '')}{password_part}@{ds_config.get('host', '')}{port_part}/{ds_config.get('database', '')}"
            if ds_config.get("connect_args"):
                args = ds_config["connect_args"]
                if not args.startswith("?"):
                    args = f"?{args}"
                url = f"{url}{args}"

        engine = create_engine(url)
        quote = "`" if ds_type == "mysql" else '"'

        for rc in sql_rules:
            rule_config = rc.get("config", {})
            sql = rule_config.get("sql", "")
            if table_name:
                sql = sql.replace("{table}", f"{quote}{table_name}{quote}")
            operator = rule_config.get("operator", "gt")
            threshold = rule_config.get("threshold", 0)
            value_column = rule_config.get("value_column", "cnt")
            is_percentage = rule_config.get("is_percentage", False)

            try:
                with engine.connect() as conn:
                    result = conn.execute(sql_text(sql))
                    row = result.fetchone()
                    if row is None:
                        results.append({"rule_name": rc.get("name", ""), "passed": True, "reason": "SQL 查询无结果", "value": None})
                        continue

                    row_dict = dict(row._mapping)
                    value = row_dict.get(value_column, row_dict.get(list(row_dict.keys())[0], 0))
                    if value is None:
                        value = 0

                    if is_percentage:
                        total_result = conn.execute(sql_text(f"SELECT COUNT(*) FROM {quote}{table_name}{quote}"))
                        total_row = total_result.fetchone()
                        total = total_row[0] if total_row else 1
                        value = float(value) / total if total > 0 else 0.0

                    ops = {"gt": lambda v, t: v > t, "lt": lambda v, t: v < t, "gte": lambda v, t: v >= t, "lte": lambda v, t: v <= t, "eq": lambda v, t: v == t}
                    is_bad = ops.get(operator, ops["gt"])(value, threshold)

                    results.append({
                        "rule_name": rc.get("name", ""),
                        "passed": not is_bad,
                        "reason": f"SQL查询值={value}, {operator} {threshold}" if is_bad else "通过",
                        "value": value,
                        "sql": sql,
                    })
            except Exception as e:
                results.append({"rule_name": rc.get("name", ""), "passed": True, "reason": f"SQL执行错误: {str(e)}", "value": None})

        engine.dispose()
    except Exception as e:
        for rc in sql_rules:
            results.append({"rule_name": rc.get("name", ""), "passed": True, "reason": f"数据库连接错误: {str(e)}", "value": None})

    return results
