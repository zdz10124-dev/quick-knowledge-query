from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Callable, Optional

import httpx


DeltaCallback = Callable[[str], None]
ErrorCallback = Callable[[Exception], None]


@dataclass(frozen=True)
class TokenUsage:
    """单次 DeepSeek 响应的 token 用量。"""

    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_tokens: int

    @classmethod
    def from_response(cls, response: dict) -> "TokenUsage":
        usage = response.get("usage") or {}
        details = usage.get("input_tokens_details") or {}
        return cls(
            input_tokens=int(usage.get("input_tokens") or 0),
            cached_input_tokens=int(details.get("cached_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        )


DoneCallback = Callable[[TokenUsage], None]


@dataclass(frozen=True)
class ApiConfig:
    """DeepSeek API 运行配置。"""

    api_base: str
    api_key_env: str
    model: str
    system_prompt: str
    screenshot_prompt: str
    max_output_tokens: int


class DeepSeekClient:
    """无工具、无历史串联的 DeepSeek Responses API 客户端。"""

    def __init__(self, config: ApiConfig) -> None:
        self.config = config
        # None 表示尚未在应用内覆盖，此时读取环境变量；空字符串表示用户明确清空。
        self._api_key_override: str | None = None

    def get_environment_api_key(self) -> str:
        """读取配置指定的环境变量，仅供应用内预填 API Key 输入框。"""
        return os.getenv(self.config.api_key_env, "").strip()

    def get_api_key(self) -> str:
        """返回当前实际使用的 API Key：应用内设置优先，否则使用环境变量。"""
        if self._api_key_override is not None:
            return self._api_key_override
        return self.get_environment_api_key()

    def set_api_key(self, api_key: str) -> None:
        """设置仅本次进程使用的 API Key；留空时恢复使用环境变量，不写入磁盘。"""
        value = api_key.strip()
        self._api_key_override = value if value else None

    def has_api_key(self) -> bool:
        """检查当前是否有可用 API Key，但不暴露密钥内容。"""
        return bool(self.get_api_key())

    def stream_answer(
        self,
        *,
        context: str,
        question: Optional[str] = None,
        image_png: Optional[bytes] = None,
        on_delta: DeltaCallback,
        on_done: DoneCallback,
        on_error: ErrorCallback,
    ) -> None:
        """同步执行一次独立请求，并通过回调流式返回最终回答文本。

        每次请求都重新构造：系统提示词 + 固定知识上下文 + 当前问题。
        之前的用户问题、模型回答均不会被带入下一次请求。
        """
        try:
            api_key = self.get_api_key()
            if not api_key:
                raise RuntimeError(
                    f"尚未填写 API Key。请在应用内填写，或设置环境变量 {self.config.api_key_env}。"
                )

            input_items = []
            if context.strip():
                input_items.append(
                    {
                        "role": "user",
                        "content": f"知识补充：{context.strip()}",
                    }
                )

            if image_png is not None:
                prompt = (question or self.config.screenshot_prompt).strip()
                image_url = "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")
                input_items.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": image_url},
                        ],
                    }
                )
            else:
                if not question or not question.strip():
                    raise ValueError("问题不能为空。")
                input_items.append({"role": "user", "content": question.strip()})

            payload = {
                "model": self.config.model,
                "instructions": self.config.system_prompt,
                "input": input_items,
                "stream": True,
                "max_output_tokens": int(self.config.max_output_tokens),
            }

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            url = self.config.api_base.rstrip("/") + "/responses"

            timeout = httpx.Timeout(connect=15.0, read=120.0, write=60.0, pool=15.0)
            # DeepSeek 属于国内模型，这里明确不继承系统代理/VPN环境。
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code != 200:
                        body = response.read().decode("utf-8", "replace")
                        raise RuntimeError(
                            f"DeepSeek API 返回 {response.status_code}：{body[:700]}"
                        )

                    incomplete_reason = None
                    final_usage: TokenUsage | None = None
                    for line in response.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:]
                        if raw == "[DONE]":
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        event_type = event.get("type")
                        if event_type == "response.output_text.delta":
                            delta = event.get("delta", "")
                            if delta:
                                on_delta(delta)
                        elif event_type == "response.failed":
                            err = event.get("response", {}).get("error") or event.get("error")
                            raise RuntimeError(f"DeepSeek 响应失败：{err}")
                        elif event_type == "response.completed":
                            final_usage = TokenUsage.from_response(event.get("response") or {})
                        elif event_type == "response.incomplete":
                            response_data = event.get("response") or {}
                            incomplete_reason = (
                                response_data.get("incomplete_details")
                                or event.get("incomplete_details")
                            )
                            final_usage = TokenUsage.from_response(response_data)

                    if incomplete_reason:
                        on_delta(f"\n[回答可能被截断：{incomplete_reason}]")
                    if final_usage is None:
                        raise RuntimeError("DeepSeek 流式响应结束时未返回 token usage。")
                    on_done(final_usage)
        except Exception as exc:  # noqa: BLE001 - 统一交给 UI 显示明确错误
            on_error(exc)
