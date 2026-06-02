# Dingo — Agent Instructions

## Project Overview

Dingo is a comprehensive AI data quality evaluation tool for ML practitioners, data engineers, and AI researchers. It systematically assesses training data, fine-tuning datasets, and production AI systems using rule-based, LLM-based, and agent-based evaluation methods.

**Repository**: https://github.com/MigoXLab/dingo
**PyPI**: `pip install dingo-python`
**License**: Apache 2.0
**Python**: >=3.10

## Tech Stack

| Layer | Technology |
|-------|------------|
| Language | Python 3.10+ |
| Data Models | Pydantic (BaseModel, extra="allow") |
| LLM Integration | OpenAI SDK (supports any compatible API) |
| MCP Server | FastMCP + SSE transport |
| Distributed | PySpark (optional) |

## Build / Lint / Test Commands

```bash
# Install in dev mode
pip install -e .

# Run all unit tests
pytest test/scripts --ignore=test/scripts/data

# Run a single test file
pytest test/scripts/model/rule/test_rule_common.py -v

# Run a single test class or method
pytest test/scripts/model/rule/test_rule_common.py::TestRulePIIDetection -v
pytest test/scripts/model/rule/test_rule_common.py::TestRulePIIDetection::test_no_pii_content -v

# Run tests matching a keyword
pytest test/scripts -k "test_faithfulness" -v

# Skip slow/external tests
pytest test/scripts -m "not slow and not external" --ignore=test/scripts/data

# Lint: pre-commit hooks (isort + flake8 + trailing-whitespace + check-yaml)
pre-commit run --all-files

# Lint: check syntax and imports for all Python files
python .github/scripts/check_imports.py

# Integration tests via CLI
dingo eval --input .github/env/local_plaintext.json
dingo eval --input .github/env/local_plaintext_save.json
dingo eval --input .github/env/local_json.json --json
```

## CLI Reference

```bash
dingo eval --input config.json      # Run evaluation
dingo eval --input config.json --json  # JSON output for automation
dingo info                           # List all evaluators, groups
dingo info --rules --json            # Rule evaluators as JSON
dingo serve                          # Start MCP server (SSE)
dingo serve --transport stdio        # MCP server (stdio for local agent)
python -m dingo.run.cli --input config.json  # Same as above
```

## Directory Structure

```
dingo/
├── setup.py                  ← version defined here (currently 2.2.2)
├── requirements/
│   ├── runtime.txt           ← core deps (openai, pydantic, numpy, etc.)
│   ├── datasource.txt        ← optional datasource deps
│   ├── optional.txt          ← heavy optional deps (torch, pyspark, etc.)
│   └── agent.txt             ← agent eval deps (langchain, tavily)
├── .pre-commit-config.yaml   ← isort (line_length=200, multi_line_output=0) + flake8
├── setup.cfg                 ← flake8 (max-line-length=120), isort, pytest markers
├── dingo/
│   ├── config/input_args.py  ← Pydantic config models
│   ├── io/input/data.py      ← Data model (extra="allow")
│   ├── io/output/            ← EvalDetail, ResultInfo, SummaryModel
│   ├── data/                 ← datasources, datasets, converters
│   ├── model/
│   │   ├── model.py          ← Model registry (rule_register, llm_register)
│   │   ├── rule/             ← 80+ rule evaluators
│   │   └── llm/              ← LLM/agent evaluators (text_quality, rag, hhh, etc.)
│   ├── exec/                 ← LocalExecutor, SparkExecutor
│   └── run/cli.py            ← CLI entry point
├── test/scripts/             ← pytest tests mirroring dingo/ structure
│   ├── model/rule/           ← rule evaluator tests
│   ├── model/llm/            ← LLM evaluator tests
│   └── exec/                 ← CLI and executor tests
└── .github/env/              ← integration test configs
```

## Code Style Guidelines

### General

- **PEP 8** enforced by pre-commit (isort + flake8). isort config: `line_length=200, multi_line_output=0, known_first_party=dingo`. flake8: `max-line-length=120, ignore=E251`.
- **Type hints** required on all function signatures. Use `from typing import List, Dict, Optional, etc.`
- **Naming**: `PascalCase` classes, `snake_case` functions/methods/variables, `UPPER_CASE` constants, `_leading_underscore` for private/internal methods.
- **Comments**: English or Chinese (both acceptable). Docstrings in English preferred.
- **Module-level `__init__.py`**: re-export with `# noqa E402` if needed.

### Imports

- **Core deps** (numpy, pydantic, requests, openai, etc.): top-level imports OK.
- **Optional/heavy deps** (torch, transformers, pyarrow, boto3, sqlalchemy, cv2, fasttext, langchain): **must** use lazy imports inside methods with clear `ImportError` messages.
- Import ordering: standard library → third-party → first-party (`dingo.*`). isort handles this automatically.

```python
# Correct — lazy import with helpful error
def load_data(self):
    try:
        import pyarrow.parquet as pq
    except ImportError:
        raise ImportError("pyarrow is required for Parquet support. Install: pip install dingo-python[parquet]")

# Wrong — top-level import of optional dep
import pyarrow.parquet as pq
```

### Data Model

`Data` uses `extra = "allow"` — access optional fields with `getattr(data, 'field', default)` instead of direct attribute access.

```python
raw_data = getattr(input_data, 'raw_data', {})
context = getattr(input_data, 'context', None)
```

Common fields: `data_id`, `prompt`, `content`, `image`, `context`, `raw_data`, `reference`, `user_input`, `response`, `retrieved_contexts`.

### Evaluator Contract

Every evaluator class must:
1. Use decorator registration: `@Model.rule_register(metric_type, groups)` or `@Model.llm_register('Name')`
2. Return `EvalDetail(metric=cls.__name__, status=bool, label=List[str], reason=List[str])`
3. Use `@classmethod eval(cls, input_data: Data) -> EvalDetail`
4. Set `_required_fields = [RequiredField.CONTENT]` (list of RequiredField enum members)

`EvalDetail.status` semantics: `True` = issue found (bad), `False` = no issue (good).

### Error Handling

- Evaluators must **never raise exceptions** for bad input data; return `EvalDetail` with error label instead.
- Use `from dingo.utils import log` for logging (`log.info()`, `log.warning()`, `log.error()`).
- External API calls: always handle timeouts, connection errors, and JSON parse errors with retry logic (see `BaseOpenAI.eval()` — 3 retry attempts).
- Custom exceptions in `dingo/utils/exception.py`: `ExceedMaxTokens`, `ConvertJsonError`, `ConvertError`.
- CLI exit codes: `0` success, `1` config error, `2` eval error, `3` IO error.

### Configuration

- Pydantic models for all config args in `dingo/config/input_args.py`.
- Never hardcode API keys; always use config parameters (via `EvaluatorRuleArgs` / `EvaluatorLLMArgs`) or environment variables.
- LLM config keys: `key`, `api_url`, `model`, `embedding_config`, `model_extra` (for temperature, max_tokens, etc.).

### Dependency Management

| Type | Location | Lazy import? |
|------|----------|-------------|
| Core (openai, numpy, pydantic, etc.) | `requirements/runtime.txt` | No |
| Datasource (pyarrow, boto3, etc.) | `requirements/runtime.txt` (bundled) | Yes |
| Heavy optional (torch, pyspark) | `setup.py` extras | Yes |
| Agent (langchain, tavily) | `requirements/agent.txt` → `extras['agent']` | Yes |

### Testing Conventions

- Test files in `test/scripts/` mirroring `dingo/` structure.
- Use pytest classes (`class TestSomething:`) with method-level test functions.
- Use `unittest.mock.patch` for mocking LLM/API calls.
- Test data in `test/data/`, test configs in `test/env/`.
- Pytest markers: `slow`, `external`, `integration` (defined in `setup.cfg`).

## Registration System

```python
# Rule evaluator
@Model.rule_register('QUALITY_BAD_COMPLETENESS', ['default', 'pretrain'])
class MyRule(BaseRule):
    _required_fields = [RequiredField.CONTENT]

    @classmethod
    def eval(cls, input_data: Data) -> EvalDetail:
        res = EvalDetail(metric=cls.__name__)
        if problem_found:
            res.status = True
            res.label = [f"{cls.metric_type}.{cls.__name__}"]
            res.reason = ["Description"]
        else:
            res.label = [QualityLabel.QUALITY_GOOD]
        return res

# LLM evaluator
@Model.llm_register('MyLLMEvaluator')
class MyLLMEvaluator(BaseOpenAI):
    prompt = "..."
    _required_fields = [RequiredField.CONTENT]

    @classmethod
    def build_messages(cls, input_data: Data) -> List:
        return [{'role': 'user', 'content': cls.prompt + input_data.content}]

# Agent evaluator
@Model.llm_register('MyAgent')
class MyAgent(BaseAgent):
    available_tools = ["tavily_search"]

    @classmethod
    def eval(cls, input_data: Data) -> EvalDetail: ...
```

## Config Maintenance Rules

| Event | Update |
|-------|--------|
| New evaluator added | Ensure registration decorator is correct; update `docs/metrics.md` |
| New datasource added | Update `requirements/runtime.txt`, `setup.py` extras if heavy |
| New dependency added | Core → `runtime.txt`, optional → `setup.py` extras; use lazy import |
| Version bump | Update `setup.py` version field |

After completing a feature, check if any of the above need updating.
