import asyncio
import json
import os
import sys
import uuid
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dingo.config import InputArgs
from dingo.config.input_args import (
    DatasetArgs,
    DatasetSqlArgs,
    DatasetCsvArgs,
    DatasetExcelArgs,
    DatasetParquetArgs,
    ExecutorArgs,
    ExecutorResultSaveArgs,
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
        input_path = ds_config.get("query", "SELECT 1")
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
        if rule_name in Model.rule_name_map:
            eval_config = None
            if rule_config:
                eval_config = EvaluatorRuleArgs(**rule_config)
            evals.append(EvalPiplineConfig(name=rule_name, config=eval_config))

    evaluator = [EvalPipline(fields=field_mapping or {}, evals=evals)]

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
        ),
        evaluator=evaluator,
    )


async def run_evaluation_task(
    task_id: int,
    datasource_config: dict,
    rule_configs: list[dict],
    task_name: str,
    field_mapping: Optional[dict] = None,
):
    _task_store[task_id] = {
        "status": "running",
        "progress": 0.0,
        "total": 0,
        "processed": 0,
    }

    try:
        input_args = build_input_args(
            datasource_config, rule_configs, task_name, field_mapping
        )

        loop = asyncio.get_event_loop()

        def _execute():
            executor = Executor.exec_map["local"](input_args)
            return executor.execute()

        result = await loop.run_in_executor(None, _execute)

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
        _task_store[task_id] = {
            "status": "failed",
            "progress": _task_store[task_id].get("progress", 0),
            "total": _task_store[task_id].get("total", 0),
            "processed": _task_store[task_id].get("processed", 0),
            "error_message": str(e),
        }


def get_task_progress(task_id: int) -> Optional[dict]:
    return _task_store.get(task_id)


def get_bad_data(task_id: int, task_name: str) -> list[dict]:
    task_dir = None
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
