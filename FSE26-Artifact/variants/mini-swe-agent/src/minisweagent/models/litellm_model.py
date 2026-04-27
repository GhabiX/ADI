import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from openai import OpenAI
import tiktoken
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from minisweagent.models import GLOBAL_MODEL_STATS

logger = logging.getLogger("litellm_model")


@dataclass
class LitellmModelConfig:
    model_name: str
    model_kwargs: dict[str, Any] = field(default_factory=dict)
    # 默认API配置，可以被配置文件覆盖
    api_key: str = ""
    base_url: str = ""
    # Claude 3.5 Sonnet定价 (每1K tokens，基于$3/MTok input, $15/MTok output)
    input_cost_per_1k: float = 3.0 / 1000  # $3/MTok = $0.003 per 1K input tokens  
    output_cost_per_1k: float = 15.0 / 1000  # $15/MTok = $0.015 per 1K output tokens


class LitellmModel:
    def __init__(self, **kwargs):
        self.config = LitellmModelConfig(**kwargs)
        self.cost = 0.0
        self.n_calls = 0
        
        # 获取API配置的优先级：model_kwargs > 环境变量 > 默认值
        api_key = (
            self.config.model_kwargs.get("api_key") or 
            os.getenv("OPENAI_API_KEY") or 
            self.config.api_key
        )
        base_url = (
            self.config.model_kwargs.get("api_base") or 
            os.getenv("OPENAI_API_BASE") or 
            self.config.base_url
        )
        
        # 初始化OpenAI客户端
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url
        )
        
        # 初始化tiktoken编码器
        try:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")
        except Exception:
            # 如果获取编码器失败，使用简单的字符计数
            self.tokenizer = None
            logger.warning("Failed to load tiktoken encoder, using character-based estimation")

    @retry(
        stop=stop_after_attempt(10),
        wait=wait_exponential(multiplier=1, min=4, max=60),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        retry=retry_if_not_exception_type(
            (
                KeyboardInterrupt,
            )
        ),
    )
    def _query(self, messages: list[dict[str, str]], **kwargs):
        """使用原生OpenAI客户端发送请求"""
        try:
            # 合并配置参数
            all_params = {
                "model": self.config.model_name,
                "messages": messages,
                "temperature": 0.0,  # 默认值
                **self.config.model_kwargs,
                **kwargs
            }
            
            # 过滤掉OpenAI不支持的参数
            openai_unsupported = {
                'drop_params', 'custom_llm_provider', 'api_base', 'api_key',
                'litellm_provider', 'input_cost_per_token', 'output_cost_per_token'
            }
            params = {k: v for k, v in all_params.items() if k not in openai_unsupported}
            
            response = self.client.chat.completions.create(**params)
            return response
            
        except Exception as e:
            logger.error(f"OpenAI API call failed: {e}")
            raise e

    def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
        response = self._query(messages, **kwargs)        
        
        # 计算成本
        cost = self._calculate_cost(messages, response)

        self.n_calls += 1
        self.cost += cost
        GLOBAL_MODEL_STATS.add(cost)
        return {
            "content": response.choices[0].message.content or "",
        }

    def _calculate_cost(self, messages: list[dict[str, str]], response) -> float:
        """使用tiktoken计算成本"""
        try:
            # 计算输入tokens，安全处理content字段
            input_parts = []
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, list):
                    # 处理content是列表的情况
                    content = " ".join(str(item) for item in content)
                elif not isinstance(content, str):
                    # 处理其他类型
                    content = str(content)
                input_parts.append(content)
            input_text = "\n".join(input_parts)
            if self.tokenizer:
                input_tokens = len(self.tokenizer.encode(input_text))
                output_tokens = response.usage.completion_tokens if hasattr(response, 'usage') else 0
            else:
                # 简单估算：每4个字符约等于1个token
                input_tokens = len(input_text) // 4
                output_text = response.choices[0].message.content or ""
                output_tokens = len(output_text) // 4
            
            # 计算成本
            input_cost = (input_tokens / 1000) * self.config.input_cost_per_1k
            output_cost = (output_tokens / 1000) * self.config.output_cost_per_1k
            total_cost = input_cost + output_cost
            
            logger.debug(f"Cost calculation: {input_tokens} input tokens, {output_tokens} output tokens, ${total_cost:.6f}")
            return total_cost
            
        except Exception as e:
            logger.warning(f"Cost calculation failed: {e}, using default estimate")
            return 0.01  # 默认每次调用$0.01

    def get_template_vars(self) -> dict[str, Any]:
        return asdict(self.config) | {"n_model_calls": self.n_calls, "model_cost": self.cost}
