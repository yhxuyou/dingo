import re
from typing import List

from dingo.config.input_args import EvaluatorRuleArgs
from dingo.io.input import Data, RequiredField
from dingo.io.output.eval_detail import EvalDetail, QualityLabel
from dingo.model.model import Model
from dingo.model.rule.base import BaseRule


@Model.rule_register("QUALITY_BAD_EFFECTIVENESS", ["custom"])
class RuleThresholdCheck(BaseRule):
    dynamic_config = EvaluatorRuleArgs(
        threshold=0.5,
        parameters={"metric": "char_ratio", "operator": "gt", "metric_params": {}, "threshold_max": None}
    )

    _required_fields = [RequiredField.CONTENT]

    @classmethod
    def eval(cls, input_data: Data) -> EvalDetail:
        res = EvalDetail(metric=cls.__name__)
        content = input_data.content
        if content is None:
            content = ""
        elif not isinstance(content, str):
            content = str(content)

        params = cls.dynamic_config.parameters or {}
        metric = params.get("metric", "char_ratio")
        operator = params.get("operator", "gt")
        metric_params = params.get("metric_params", {})
        threshold_max = params.get("threshold_max")

        value = cls._compute_metric(metric, content, metric_params)
        is_bad = cls._check_threshold(value, operator, cls.dynamic_config.threshold, threshold_max)

        if is_bad:
            res.status = True
            res.label = [f"{cls.metric_type}.{cls.__name__}"]
            res.reason = [cls._build_reason(metric, value, operator, cls.dynamic_config.threshold, metric_params)]
        else:
            res.label = [QualityLabel.QUALITY_GOOD]
        return res

    @classmethod
    def _compute_metric(cls, metric: str, content: str, params: dict) -> float:
        if metric == "char_ratio":
            return cls._metric_char_ratio(content, params)
        elif metric == "word_ratio":
            return cls._metric_word_ratio(content, params)
        elif metric == "line_ratio":
            return cls._metric_line_ratio(content, params)
        elif metric == "count":
            return cls._metric_count(content, params)
        elif metric == "regex_count":
            return cls._metric_regex_count(content, params)
        elif metric == "regex_ratio":
            return cls._metric_regex_ratio(content, params)
        elif metric == "repetition":
            return cls._metric_repetition(content)
        elif metric == "uniqueness":
            return cls._metric_uniqueness(content)
        return 0.0

    @classmethod
    def _metric_char_ratio(cls, content: str, params: dict) -> float:
        if len(content) == 0:
            return 0.0
        target = params.get("target", "newline")
        if target == "newline":
            count = content.count("\n")
        elif target == "uppercase":
            count = sum(1 for c in content if c.isupper())
        elif target == "lowercase":
            count = sum(1 for c in content if c.islower())
        elif target == "digit":
            count = sum(1 for c in content if c.isdigit())
        elif target == "space":
            count = sum(1 for c in content if c.isspace())
        elif target == "special":
            count = sum(1 for c in content if not c.isalnum() and not c.isspace())
        elif target == "custom":
            custom_chars = params.get("custom_chars", "")
            count = sum(1 for c in content if c in custom_chars)
        else:
            count = 0
        return count / len(content)

    @classmethod
    def _metric_word_ratio(cls, content: str, params: dict) -> float:
        words = content.split()
        if len(words) == 0:
            return 0.0
        key_list = params.get("key_list", [])
        if not key_list:
            return 0.0
        matched = sum(1 for w in words if w in key_list)
        return matched / len(words)

    @classmethod
    def _metric_line_ratio(cls, content: str, params: dict) -> float:
        lines = content.split("\n")
        lines = [l for l in lines if l.strip()]
        if len(lines) == 0:
            return 0.0
        target = params.get("target", "ends_with_punct")
        if target == "ends_with_punct":
            punct_marks = params.get("punct_marks", [".", "!", "?", "。", "！", "？"])
            matched = sum(1 for l in lines if l.rstrip() and l.rstrip()[-1] in punct_marks)
        elif target == "starts_with_bullet":
            bullet_chars = params.get("bullet_chars", ["•", "-", "*", "·", "►"])
            matched = sum(1 for l in lines if l.lstrip() and l.lstrip()[0] in bullet_chars)
        elif target == "empty":
            all_lines = content.split("\n")
            matched = sum(1 for l in all_lines if not l.strip())
            return matched / len(all_lines) if len(all_lines) > 0 else 0.0
        else:
            matched = 0
        return matched / len(lines)

    @classmethod
    def _metric_count(cls, content: str, params: dict) -> float:
        target = params.get("target", "char")
        if target == "char":
            return float(len(content))
        elif target == "word":
            return float(len(content.split()))
        elif target == "sentence":
            sentences = re.split(r'[.!?。！？]', content)
            return float(len([s for s in sentences if s.strip()]))
        elif target == "line":
            return float(len(content.split("\n")))
        elif target == "byte":
            return float(len(content.encode("utf-8")))
        return 0.0

    @classmethod
    def _metric_regex_count(cls, content: str, params: dict) -> float:
        pattern = params.get("pattern", "")
        if not pattern:
            return 0.0
        try:
            matches = re.findall(pattern, content)
            return float(len(matches))
        except re.error:
            return 0.0

    @classmethod
    def _metric_regex_ratio(cls, content: str, params: dict) -> float:
        if len(content) == 0:
            return 0.0
        count = cls._metric_regex_count(content, params)
        return count / len(content)

    @classmethod
    def _metric_repetition(cls, content: str) -> float:
        if len(content) < 6:
            return 0.0
        try:
            from dingo.model.rule.utils.util import base_rps_frac_chars_in_dupe_ngrams
            return base_rps_frac_chars_in_dupe_ngrams(6, content)
        except ImportError:
            words = content.split()
            if len(words) == 0:
                return 0.0
            unique = len(set(words))
            total = len(words)
            return (total - unique) / total if total > 0 else 0.0

    @classmethod
    def _metric_uniqueness(cls, content: str) -> float:
        words = content.split()
        if len(words) == 0:
            return 0.0
        return len(set(words)) / len(words)

    @classmethod
    def _check_threshold(cls, value: float, operator: str, threshold: float, threshold_max=None) -> bool:
        ops = {
            "gt": lambda v, t: v > t,
            "lt": lambda v, t: v < t,
            "gte": lambda v, t: v >= t,
            "lte": lambda v, t: v <= t,
            "eq": lambda v, t: v == t,
            "between": lambda v, t: t <= v <= (threshold_max if threshold_max is not None else t),
        }
        check_fn = ops.get(operator, ops["gt"])
        return check_fn(value, threshold)

    @classmethod
    def _build_reason(cls, metric: str, value: float, operator: str, threshold: float, params: dict) -> str:
        op_labels = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<=", "eq": "=", "between": "介于"}
        op_str = op_labels.get(operator, ">")
        metric_labels = {
            "char_ratio": "字符占比", "word_ratio": "词占比", "line_ratio": "行占比",
            "count": "计数", "regex_count": "正则匹配计数", "regex_ratio": "正则匹配占比",
            "repetition": "重复度", "uniqueness": "唯一性",
        }
        metric_str = metric_labels.get(metric, metric)
        val_str = f"{value:.4f}" if isinstance(value, float) and value < 100 else str(int(value))
        return f"{metric_str}={val_str} {op_str} {threshold}"


@Model.rule_register("QUALITY_BAD_EFFECTIVENESS", ["custom"])
class RuleCustomPython(BaseRule):
    dynamic_config = EvaluatorRuleArgs(
        parameters={"code": "", "timeout": 10}
    )

    _required_fields = [RequiredField.CONTENT]

    @classmethod
    def eval(cls, input_data: Data) -> EvalDetail:
        res = EvalDetail(metric=cls.__name__)
        content = input_data.content
        if content is None:
            content = ""
        elif not isinstance(content, str):
            content = str(content)

        params = cls.dynamic_config.parameters or {}
        code = params.get("code", "")
        timeout = params.get("timeout", 10)

        if not code.strip():
            res.label = [QualityLabel.QUALITY_GOOD]
            return res

        try:
            from backend.python_sandbox import execute_in_sandbox
            result = execute_in_sandbox(code, content, timeout)
        except Exception as e:
            res.status = True
            res.label = [f"{cls.metric_type}.{cls.__name__}"]
            res.reason = [f"执行错误: {str(e)}"]
            return res

        if isinstance(result, dict) and result.get("passed") is False:
            res.status = True
            res.label = [f"{cls.metric_type}.{cls.__name__}"]
            res.reason = [result.get("reason", "未通过检测")]
        else:
            res.label = [QualityLabel.QUALITY_GOOD]
        return res
