"""
LLM 接入层（``engine=llm`` 时使用），**只用标准库实现**。

与 coding_agent / codeguard 不同，这里刻意不依赖 httpx：

- 修复 Agent 的主路径是规则级修补器，LLM 是可选增强；
- 为了一个可选能力引入第三方依赖，会让"clone 下来就能跑"这个目标打折；
- ``urllib.request`` 打一个 JSON POST 已经足够，协议本来就是公开的。

代价是要自己处理超时、错误码与重试，这些都是显式代码，反而更好排查。
"""

from __future__ import annotations

import json
import logging
import random
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from fix_agent.config import FixConfig
from fix_agent.errors import FixAgentError

logger = logging.getLogger(__name__)


class LLMError(FixAgentError):
    """LLM 调用失败。``retryable`` 决定要不要退避重试。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class LLMResult:
    """一次补全的结果。"""

    text: str
    usage: dict[str, int]
    model: str = ""


class StdlibChatLLM:
    """OpenAI 兼容协议的极简客户端。"""

    def __init__(self, config: FixConfig) -> None:
        self.config = config

    @property
    def model_name(self) -> str:
        return self.config.model

    def complete(self, system: str, user: str) -> LLMResult:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = self._endpoint()

        attempts = 3
        last_error: LLMError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._post(url, body)
            except LLMError as exc:
                last_error = exc
                if not exc.retryable or attempt >= attempts:
                    raise
                delay = min(2.0**attempt, 20.0) * (0.5 + random.random() * 0.5)
                logger.warning("LLM 调用失败（%d/%d）：%s，%.1fs 后重试", attempt, attempts, exc, delay)
                time.sleep(delay)
        raise last_error or LLMError("LLM 调用失败")  # pragma: no cover

    # 内部
    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _post(self, url: str, body: bytes) -> LLMResult:
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                **self.config.extra_headers,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = _read_error(exc)
            if exc.code in (401, 403):
                raise LLMError(f"鉴权失败（HTTP {exc.code}）：{detail}。请检查 API Key。") from exc
            if exc.code in (408, 429) or exc.code >= 500:
                raise LLMError(f"服务暂不可用（HTTP {exc.code}）：{detail}", retryable=True) from exc
            raise LLMError(f"请求被拒绝（HTTP {exc.code}）：{detail}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise LLMError(f"网络错误：{exc}", retryable=True) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"响应不是合法 JSON：{raw[:200]}") from exc
        return normalize_response(data)


def normalize_response(data: object) -> LLMResult:
    """把 OpenAI 兼容响应归一化成 :class:`LLMResult`。"""
    if not isinstance(data, dict):
        raise LLMError("响应顶层不是 JSON 对象")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        error = data.get("error")
        message = error.get("message") if isinstance(error, dict) else error
        raise LLMError(f"响应缺少 choices：{message or '未知原因'}")
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not content:
        raise LLMError("模型返回空内容")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return LLMResult(
        text=str(content),
        usage={
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
        model=str(data.get("model") or ""),
    )


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or error)[:200]
        if isinstance(data, dict) and "message" in data:
            return str(data["message"])[:200]
        return raw[:200]
    except Exception:  # noqa: BLE001 - 错误信息尽力而为
        return f"HTTP {exc.code}"


__all__ = ["LLMError", "LLMResult", "StdlibChatLLM", "normalize_response"]
