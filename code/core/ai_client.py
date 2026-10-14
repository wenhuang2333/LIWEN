import json
import time
import requests
from dataclasses import dataclass

from utils.logger import info, error, warn


@dataclass
class AIProvider:
    name: str
    base_url: str
    default_model: str


PRESET_PROVIDERS = {
    "OpenAI": AIProvider("OpenAI", "https://api.openai.com/v1/chat/completions", "gpt-4o"),
    "DeepSeek": AIProvider("DeepSeek", "https://api.deepseek.com/v1/chat/completions", "deepseek-v4-pro"),
    "Kimi (月之暗面)": AIProvider("Kimi (月之暗面)", "https://api.moonshot.cn/v1/chat/completions", "kimi-k2-0711-preview"),
    "MiniMax (稀宇)": AIProvider("MiniMax (稀宇)", "https://api.minimax.io/v1/chat/completions", "MiniMax-M2.7"),
    "通义千问 (阿里)": AIProvider("通义千问 (阿里)", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "qwen-plus"),
    "讯飞星火": AIProvider("讯飞星火", "https://spark-api-open.xf-yun.com/v1/chat/completions", "generalv3"),
    "Ollama": AIProvider("Ollama", "http://localhost:11434/v1/chat/completions", "llama3"),
    "Groq": AIProvider("Groq", "https://api.groq.com/openai/v1/chat/completions", "llama-3.1-70b-versatile"),
    "Together AI": AIProvider("Together AI", "https://api.together.xyz/v1/chat/completions", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "自定义": AIProvider("自定义", "", ""),
}

# Old base URLs (without /chat/completions) → new full endpoint URLs
OLD_BASE_URLS = {
    "https://api.openai.com/v1": "https://api.openai.com/v1/chat/completions",
    "https://api.deepseek.com/v1": "https://api.deepseek.com/v1/chat/completions",
    "https://api.moonshot.cn/v1": "https://api.moonshot.cn/v1/chat/completions",
    "https://api.minimaxi.com/v1": "https://api.minimax.io/v1/chat/completions",
    "https://api.minimaxi.com/v1/chat/completions": "https://api.minimax.io/v1/chat/completions",
    "https://dashscope.aliyuncs.com/compatible-mode/v1": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "https://spark-api-open.xf-yun.com/v1": "https://spark-api-open.xf-yun.com/v1/chat/completions",
    "http://localhost:11434/v1": "http://localhost:11434/v1/chat/completions",
    "https://api.groq.com/openai/v1": "https://api.groq.com/openai/v1/chat/completions",
    "https://api.together.xyz/v1": "https://api.together.xyz/v1/chat/completions",
}


def migrate_api_url(url):
    """Migrate old base URLs (without /chat/completions, or old domains) to full correct URLs."""
    if not url:
        return url
    clean = url.rstrip("/")
    if clean in OLD_BASE_URLS:
        return OLD_BASE_URLS[clean]
    return url


def get_provider_default_config(provider_name):
    """Return a fresh config dict for the named preset provider."""
    prov = PRESET_PROVIDERS.get(provider_name)
    return {
        "api_url": prov.base_url if prov else "",
        "api_key": "",
        "model": prov.default_model if prov else "",
        "temperature": 1.0,
        "max_tokens": 4096,
        "system_prompt": "",
    }


class AiClient:

    def __init__(self, base_url, api_key, model, temperature=1.0, max_tokens=4096):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def _is_anthropic_url(self):
        """Guess whether the endpoint expects Anthropic-format payloads and SSE."""
        url_lower = self.base_url.lower()
        return "/anthropic" in url_lower or "/messages" in url_lower

    def _build_openai_payload(self, messages):
        return {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

    def _build_anthropic_payload(self, messages):
        system_prompts = [m["content"] for m in messages if m["role"] == "system"]
        chat_messages = [m for m in messages if m["role"] != "system"]
        payload = {
            "model": self.model,
            "messages": chat_messages,
            "stream": True,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if system_prompts:
            payload["system"] = system_prompts[0] if len(system_prompts) == 1 else system_prompts
        # Anthropic requires specific headers
        return payload

    def chat_stream(self, messages):
        url = self.base_url
        anthropic_mode = self._is_anthropic_url()

        if anthropic_mode:
            payload = self._build_anthropic_payload(messages)
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            }
        else:
            payload = self._build_openai_payload(messages)
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }

        print(f"[AI DEBUG] POST {url}")
        print(f"[AI DEBUG] 模型: {self.model}, temperature: {self.temperature}, max_tokens: {self.max_tokens}")
        print(f"[AI DEBUG] 消息数: {len(messages)}, 模式: {'Anthropic' if anthropic_mode else 'OpenAI'}")
        info(f"AI 流式请求: url={url}, model={self.model}, messages={len(messages)}, mode={'anthropic' if anthropic_mode else 'openai'}")

        t0 = time.time()
        response = requests.post(url, headers=headers, json=payload,
                                 stream=True, timeout=(30, 120))
        t1 = time.time()
        print(f"[AI DEBUG] HTTP 响应 {response.status_code}, 连接耗时 {t1 - t0:.2f}s")
        info(f"AI HTTP 响应: status={response.status_code}, connect_time={t1 - t0:.2f}s")

        if response.status_code != 200:
            try:
                err = response.json()
                if isinstance(err, dict):
                    msg = err.get("error", {})
                    if isinstance(msg, dict):
                        msg = msg.get("message", str(err))
                    else:
                        msg = str(err)
                else:
                    msg = str(err)
            except Exception:
                msg = response.text or f"HTTP {response.status_code}"
            print(f"[AI DEBUG] API 错误: {msg}")
            error(f"AI API 错误 ({response.status_code}): {msg}")
            raise Exception(f"API 错误 ({response.status_code}): {msg}\n请求地址: {url}")

        # ── Parse SSE stream ──
        token_count = 0
        line_count = 0
        current_event = None       # Anthropic: "message_start", "content_block_delta", …
        detected_format = None     # "openai" | "anthropic"

        for line in response.iter_lines(decode_unicode=True):
            line_count += 1

            # Anthropic SSE: "event: <type>" line before "data: <json>" line
            if line.startswith("event: "):
                current_event = line[7:].strip()
                continue

            if not line.startswith("data: "):
                continue

            data_str = line[6:]

            # OpenAI done signal
            if data_str.strip() == "[DONE]":
                print(f"[AI DEBUG] 收到 [DONE], 共 {token_count} tokens, {line_count} 行, 格式: {detected_format or '?'}")
                break

            # Anthropic message_stop event
            if current_event == "message_stop":
                print(f"[AI DEBUG] 收到 message_stop, 共 {token_count} tokens, {line_count} 行, 格式: anthropic")
                break

            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            # Auto-detect format from JSON structure
            if detected_format is None:
                if "type" in data:
                    detected_format = "anthropic"
                    print(f"[AI DEBUG] 自动检测格式: Anthropic")
                elif "choices" in data:
                    detected_format = "openai"
                    print(f"[AI DEBUG] 自动检测格式: OpenAI")

            content = ""

            if detected_format == "anthropic" or current_event == "content_block_delta":
                detected_format = "anthropic"
                # Anthropic: content_block_delta → delta.text
                if data.get("type") == "content_block_delta":
                    delta = data.get("delta", {})
                    content = delta.get("text", "")
                # Also handle content_block_start for text blocks
                elif data.get("type") == "content_block_start":
                    cb = data.get("content_block", {})
                    content = cb.get("text", "")
            else:
                # OpenAI: choices[0].delta.content
                delta = data.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content", "")

            if content:
                if token_count == 0:
                    first_token_time = time.time()
                    ttf = first_token_time - t1
                    print(f"[AI DEBUG] 首个 token 到达, TTF: {ttf:.2f}s")
                    info(f"AI 首个 token: ttf={ttf:.2f}s")
                token_count += 1
                yield content

            # Reset event tracker
            current_event = None

        total_time = time.time() - t0
        print(f"[AI DEBUG] 流式完成: {token_count} tokens, {line_count} 行, 总耗时 {total_time:.2f}s, 格式: {detected_format or '?'}")
        info(f"AI 流式完成: tokens={token_count}, lines={line_count}, total_time={total_time:.2f}s, format={detected_format or '?'}")

        if token_count == 0 and line_count > 10:
            hint = (
                "服务器返回了数据但未提取到任何 token。可能是 API 格式不匹配。\n"
                "请检查 API 地址是否为 OpenAI 兼容格式（/v1/chat/completions）。"
            )
            print(f"[AI DEBUG] {hint}")
            warn(hint)

    def chat(self, messages):
        url = self.base_url
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        response = requests.post(url, headers=headers, json=payload, timeout=120)

        if response.status_code != 200:
            try:
                err = response.json()
                msg = err.get("error", {}).get("message", response.text)
            except Exception:
                msg = response.text or f"HTTP {response.status_code}"
            raise Exception(f"API 错误 ({response.status_code}): {msg}\n请求地址: {url}")

        data = response.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")


def build_messages(system_prompt, history):
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    return messages
