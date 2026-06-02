from typing import List

from dingo.config.input_args import EvaluatorRuleArgs
from dingo.io import Data
from dingo.io.output.eval_detail import EvalDetail


class BaseRule:
    metric_type: str = ''  # This will be set by the decorator
    group: List[str] = []  # This will be set by the decorator
    dynamic_config: EvaluatorRuleArgs = EvaluatorRuleArgs()  # Default config, can be overridden by subclasses

    @classmethod
    def eval(cls, input_data: Data) -> EvalDetail:
        # 确保 input_data.content 总是字符串类型
        if hasattr(input_data, 'content'):
            content = input_data.content
            if content is None:
                input_data.content = ""
            elif not isinstance(content, str):
                input_data.content = str(content)
        raise NotImplementedError()
