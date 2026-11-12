import os

from runtime_config import load_local_environment

load_local_environment()
import re
import time
from typing import Dict, List

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.3-codex")

_global_session = None
_translation_cache: Dict[str, str] = {}
TAG_LIST = [
    "AI",
    "Machine Learning",
    "Robotics",
    "Semiconductor",
    "Autonomous Driving",
    "Venture Capital",
    "Private Equity",
    "Funding",
    "IPO",
    "M&A",
    "Fintech",
    "Blockchain",
    "Cloud Computing",
    "Big Data",
    "Biotech",
    "HealthTech",
    "EV",
    "Cybersecurity",
    "Space Tech",
    "Enterprise Software",
    "Consumer Tech",
    "Media",
    "E-commerce",
]


def _uses_max_completion_tokens(model_name: str) -> bool:
    model_name = (model_name or "").strip().lower()
    return model_name.startswith(("gpt-5", "o1", "o3", "o4"))


def azure_summary_enabled() -> bool:
    return bool(
        (os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_API_KEY"))
        or (os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_BASE_URL"))
    )


def azure_translate_enabled() -> bool:
    return azure_summary_enabled()


def _get_session():
    global _global_session
    if _global_session is None:
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1.2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["POST"]),
            raise_on_status=False,
        )
        _global_session = requests.Session()
        _global_session.trust_env = True
        _global_session.mount("https://", HTTPAdapter(max_retries=retry))
        _global_session.mount("http://", HTTPAdapter(max_retries=retry))
    return _global_session


def _extract_responses_text(response_obj) -> str:
    try:
        output_text = (getattr(response_obj, "output_text", "") or "").strip()
        if output_text:
            return output_text
    except Exception:
        pass

    try:
        data = response_obj.model_dump() if hasattr(response_obj, "model_dump") else {}
    except Exception:
        data = {}

    output_items = data.get("output", []) if isinstance(data, dict) else []
    parts: List[str] = []
    for item in output_items:
        if not isinstance(item, dict):
            continue
        for block in item.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("output_text", "text"):
                text_val = block.get("text", "")
                if isinstance(text_val, dict):
                    text_val = text_val.get("value", "")
                if text_val:
                    parts.append(str(text_val))
    return "\n".join(parts).strip()


def _fallback_summary(text: str, max_length: int) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "No content to summarize"

    summary = ""
    for line in lines:
        if len(summary) + len(line) + 1 <= max_length:
            summary = f"{summary} {line}".strip()
        else:
            break
    if not summary:
        summary = lines[0]
    return summary[:max_length].strip()


def summarize_text_en(text: str, max_length: int = 200) -> str:
    text = (text or "").strip()
    if not text:
        return "No content to summarize"

    text = text[:8000]
    if not azure_summary_enabled():
        return _fallback_summary(text, max_length)

    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a senior tech journalist assistant. Summarize the text in concise English, "
                    "preserving event, actors, metrics, timeline, and impact. "
                    f"Keep the summary within {max_length} words."
                ),
            },
            {
                "role": "user",
                "content": f"Please summarize the following content in English:\n\n{text}",
            },
        ]
        result = _call_azure_openai(messages, max_tokens=600)
        return result if result else _fallback_summary(text, max_length)
    except Exception:
        return _fallback_summary(text, max_length)


def summarize_text_zh(text: str, max_length: int = 300) -> str:
    """Create a faithful Chinese summary from source text in any language."""
    text = (text or "").strip()
    if not text or not azure_summary_enabled():
        return ""
    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是严谨的财经新闻编辑。请用中文忠实概括正文，保留交易双方、金额、"
                    "阶段、日期和关键事实。忽略图片说明、作者署名、来源标记、责任编辑、"
                    "广告和导航文字，不得补充正文没有的信息。"
                    f"摘要不超过{max_length}字。"
                ),
            },
            {"role": "user", "content": f"请总结以下正文：\n\n{text[:8000]}"},
        ]
        return _call_azure_openai(messages, max_tokens=600).strip()
    except Exception:
        return ""


def translate_text_azure(text: str, target_lang: str = "zh", use_cache: bool = True) -> str:
    text = (text or "").strip()
    if not text:
        return ""

    cache_key = f"{target_lang}:{text}"
    if use_cache and cache_key in _translation_cache:
        return _translation_cache[cache_key]

    has_chinese = any("\u4e00" <= char <= "\u9fff" for char in text)
    if target_lang == "zh" and has_chinese:
        return text
    if target_lang == "en" and not has_chinese:
        return text

    enabled = azure_translate_enabled()
    print(f"[azure_ai] translate_text_azure: enabled={enabled} OPENAI_API_KEY={'SET' if os.getenv('OPENAI_API_KEY') else 'EMPTY'} OPENAI_BASE_URL={'SET' if os.getenv('OPENAI_BASE_URL') else 'EMPTY'}")
    if not enabled:
        translated = _simple_translate_to_chinese(text) if target_lang == "zh" else _simple_translate_to_english(text)
        if use_cache:
            _translation_cache[cache_key] = translated
        return translated

    try:
        if target_lang == "zh":
            sys_prompt = "你是专业翻译员。请将以下英文内容翻译为简洁准确的中文，保持原意和专业术语准确。"
            user_content = f"请翻译为中文：\n\n{text}"
        else:
            sys_prompt = "You are a professional translator. Translate the following Chinese text into concise, accurate English."
            user_content = f"Please translate into English:\n\n{text}"

        result = _call_azure_openai(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=500,
            temperature=0.1,
        )
        print(f"[azure_ai] translate_text_azure: _call_azure_openai returned {len(result) if result else 0} chars: {repr(result[:80]) if result else 'EMPTY'}")
        translated = (result or "").strip()
        if not translated:
            print("[azure_ai] translate_text_azure: result empty, falling back to simple translate")
            translated = _simple_translate_to_chinese(text) if target_lang == "zh" else _simple_translate_to_english(text)
        if use_cache:
            _translation_cache[cache_key] = translated
        return translated
    except Exception as _te:
        print(f"[azure_ai] translate_text_azure: exception {type(_te).__name__}: {_te}")
        translated = _simple_translate_to_chinese(text) if target_lang == "zh" else _simple_translate_to_english(text)
        if use_cache:
            _translation_cache[cache_key] = translated
        return translated


def _call_azure_openai(messages: List[Dict[str, str]], max_tokens: int = 320, temperature: float = 0.2) -> str:
    proxy_api_key = os.getenv("OPENAI_API_KEY", OPENAI_API_KEY)
    proxy_base_url = os.getenv("OPENAI_BASE_URL", OPENAI_BASE_URL)
    proxy_model = os.getenv("OPENAI_MODEL", OPENAI_MODEL)

    print(f"[azure_ai] _call_azure_openai: key={'SET' if proxy_api_key else 'EMPTY'} url={proxy_base_url!r} model={proxy_model!r} OpenAI={'available' if OpenAI is not None else 'NOT_INSTALLED'}")
    if proxy_api_key and proxy_base_url and OpenAI is not None:
        try:
            client = OpenAI(api_key=proxy_api_key.strip(), base_url=proxy_base_url.strip())
            token_param_name = "max_completion_tokens" if _uses_max_completion_tokens(proxy_model) else "max_tokens"
            request_kwargs = {
                "model": proxy_model.strip(),
                "messages": messages,
                token_param_name: max_tokens,
            }
            # GPT-5 and o-series models use the model's default sampling settings.
            if not _uses_max_completion_tokens(proxy_model):
                request_kwargs["temperature"] = temperature
            response = client.chat.completions.create(
                **request_kwargs,
            )
            content = (response.choices[0].message.content or "").strip()
            print(f"[azure_ai] _call_azure_openai: chat.completions OK, got {len(content)} chars")
            return content
        except Exception as _e:
            print(f"[azure_ai] _call_azure_openai failed (model={proxy_model!r}, base_url={proxy_base_url!r}): {type(_e).__name__}: {_e}")

    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", AZURE_OPENAI_ENDPOINT)
    api_key = os.getenv("AZURE_OPENAI_API_KEY", AZURE_OPENAI_API_KEY)
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", AZURE_OPENAI_DEPLOYMENT)
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", AZURE_OPENAI_API_VERSION)
    if not endpoint or not api_key:
        return ""

    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "api-key": api_key,
    }
    params = {"api-version": api_version}
    token_param_name = "max_completion_tokens" if _uses_max_completion_tokens(deployment) else "max_tokens"
    payload = {
        "messages": messages,
        token_param_name: max_tokens,
        "n": 1,
        "stream": False,
    }
    if not _uses_max_completion_tokens(deployment):
        payload.update(temperature=temperature, top_p=0.9)

    session = _get_session()
    timeout = (5, 30)
    for attempt in range(1, 4):
        try:
            response = session.post(url, params=params, headers=headers, json=payload, timeout=timeout)
            if response.status_code >= 400:
                if response.status_code not in (429, 500, 502, 503, 504):
                    return ""
                time.sleep(0.8 * attempt)
                continue

            data = response.json()
            return (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        except requests.exceptions.RequestException:
            time.sleep(0.8 * attempt)
    return ""


def _simple_translate_to_chinese(text: str) -> str:
    translation_dict = {
        "artificial intelligence": "人工智能",
        "machine learning": "机器学习",
        "startup": "创业公司",
        "acquisition": "收购",
        "merger": "合并",
        "investment": "投资",
        "funding": "融资",
        "technology": "技术",
        "innovation": "创新",
        "platform": "平台",
        "software": "软件",
        "company": "公司",
        "market": "市场",
        "product": "产品",
        "service": "服务",
        "user": "用户",
        "revenue": "收入",
        "growth": "增长",
        "deal": "交易",
        "launch": "发布",
        "report": "报告",
        "analysis": "分析",
        "research": "研究",
    }
    result = text
    for source, target in translation_dict.items():
        result = re.sub(source, target, result, flags=re.IGNORECASE)
    return result


def _simple_translate_to_english(text: str) -> str:
    translation_dict = {
        "人工智能": "Artificial Intelligence",
        "机器学习": "Machine Learning",
        "创业公司": "Startup",
        "收购": "Acquisition",
        "合并": "Merger",
        "投资": "Investment",
        "融资": "Funding",
        "技术": "Technology",
        "创新": "Innovation",
        "平台": "Platform",
        "软件": "Software",
        "公司": "Company",
        "市场": "Market",
        "产品": "Product",
        "服务": "Service",
        "用户": "User",
        "收入": "Revenue",
        "增长": "Growth",
        "交易": "Deal",
        "发布": "Launch",
        "报告": "Report",
        "分析": "Analysis",
        "研究": "Research",
    }
    result = text
    for source, target in translation_dict.items():
        result = result.replace(source, target)
    return result


def classify_tags_azure(title: str, content: str, max_tags: int = 3) -> str:
    if not azure_translate_enabled():
        return ""

    text_for_ai = f"Title: {title}\n\n{(content or '')[:600]}"
    tags_str = ", ".join(TAG_LIST)
    messages = [
        {
            "role": "system",
            "content": (
                "You are a news article classifier. Given a title and content snippet, "
                f"select the 1-{max_tags} most relevant tags from this list: [{tags_str}]. "
                "Return ONLY a comma-separated list of tag names, nothing else. If none fit, return 'Other'."
            ),
        },
        {"role": "user", "content": text_for_ai},
    ]

    try:
        result = _call_azure_openai(messages, max_tokens=40, temperature=0)
        if not result or not result.strip():
            return ""
        cleaned = re.sub(r"[^\w\s,&-]", "", result.strip())
        tag_lower = {tag.lower(): tag for tag in TAG_LIST}
        tag_lower["other"] = "Other"
        valid = []
        for part in cleaned.split(","):
            current = part.strip()
            if current.lower() in tag_lower:
                canonical = tag_lower[current.lower()]
                if canonical not in valid:
                    valid.append(canonical)
        return ", ".join(valid[:max_tags]) if valid else ""
    except Exception:
        return ""
