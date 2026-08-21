# core/search.py
# 联网搜索：支持 Tavily（推荐）、无需 Key 的 Bing（不推荐）、自定义搜索 API；
# 本地额度计数（仅 Tavily 计费）。
import json
import random
import re
import time
from urllib.parse import quote

import requests

import core.config as config

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def load_usage():
    try:
        with open(config.USAGE_FILE, 'r', encoding='utf-8') as f:
            return int(json.load(f).get("used", 0))
    except Exception:
        return 0


def save_usage(count):
    try:
        with open(config.USAGE_FILE, 'w', encoding='utf-8') as f:
            json.dump({"used": int(count)}, f)
    except Exception as e:
        print(f"保存使用计数失败: {e}")


def get_usage_info():
    """返回联网额度概览，供 Web 界面展示。"""
    used = config.tavily_used_count or load_usage()
    config.tavily_used_count = used
    return {
        "used": used,
        "limit": config.TAVILY_MONTHLY_LIMIT,
        "remaining": config.TAVILY_MONTHLY_LIMIT - used,
    }


def reset_usage(new_used):
    """手动设置已用次数（Web 控制台使用）。"""
    new_used = int(new_used)
    if 0 <= new_used <= config.TAVILY_MONTHLY_LIMIT:
        config.tavily_used_count = new_used
        save_usage(new_used)
        return get_usage_info()
    raise ValueError("已用次数需在 0 与月度上限之间")


# ==================== Tavily ====================
def search_tavily(query, max_retries=2):
    """执行 Tavily 搜索，成功后自动更新全局计数并保存到文件。"""
    if config.tavily_used_count == 0:
        config.tavily_used_count = load_usage()

    payload = {
        "api_key": config.TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "max_results": 5,
        "include_answer": True,
        "include_raw_content": False,
        "include_images": False,
    }

    for attempt in range(max_retries + 1):
        try:
            resp = requests.post("https://api.tavily.com/search", json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            config.tavily_used_count += 1
            save_usage(config.tavily_used_count)
            remaining = config.TAVILY_MONTHLY_LIMIT - config.tavily_used_count
            print(f"Tavily API 使用：{config.tavily_used_count}/{config.TAVILY_MONTHLY_LIMIT}，剩余 {remaining} 次")
            return data
        except Exception as e:
            print(f"Tavily 请求失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}")
            if attempt < max_retries:
                time.sleep(1)
    return None


# ==================== Bing（无需 Key，不推荐） ====================
def _clean_html(text):
    text = re.sub(r'<[^>]+>', '', text or "")
    return re.sub(r'\s+', ' ', text).strip()


def search_bing(query, max_results=5):
    """无需 API Key 的 Bing 搜索（HTML 解析；不推荐：结果质量与稳定性有限）。"""
    headers = {"User-Agent": _USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
    url = f"https://www.bing.com/search?q={quote(query)}&count={max_results}&setlang=zh-hans"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"Bing 搜索失败: {e}")
        return []
    html = resp.text
    results = []
    blocks = re.findall(r'<li class="b_algo".*?</li>', html, re.S)
    for block in blocks[:max_results]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url = m.group(1)
        title = _clean_html(m.group(2))
        mp = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        snippet = _clean_html(mp.group(1)) if mp else ""
        results.append({"title": title, "url": url,
                        "content": (title + "：" + snippet).strip("：")})
    return results


# ==================== 自定义搜索 API ====================
def search_custom(query, max_results=5):
    """调用自定义搜索 API。GET {url}?q=...&key=...，期望 JSON：
    {"results": [{"title": ..., "url": ..., "content": ...}, ...]}（也兼容 {"data": [...]}）。"""
    url = (config.SEARCH_CUSTOM_URL or "").strip()
    if not url:
        return []
    params = {"q": query}
    if config.SEARCH_CUSTOM_KEY:
        params["key"] = config.SEARCH_CUSTOM_KEY
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"自定义搜索失败: {e}")
        return []
    items = data.get("results") or data.get("data") or []
    results = []
    for it in items[:max_results]:
        results.append({
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "content": (it.get("content") or it.get("snippet") or it.get("description") or "").strip(),
        })
    return [r for r in results if r["content"]]


# ==================== 统一入口 ====================
def _tavily_to_results(data, max_results):
    if not data or isinstance(data, str):
        return []
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "content": r.get("content", "").strip()}
            for r in (data.get("results") or [])[:max_results] if r.get("content")]


def search_web(query, max_results=5):
    """按配置的搜索提供商搜索，返回统一结果列表 [{title, url, content}]。"""
    provider = config.SEARCH_PROVIDER
    if provider == "bing":
        return search_bing(query, max_results)
    if provider == "custom":
        return search_custom(query, max_results)
    return _tavily_to_results(search_tavily(query), max_results)


def get_internet_info(user_text):
    provider = config.SEARCH_PROVIDER
    if provider not in ("bing", "custom"):
        # Tavily：优先使用其 answer 摘要
        data = search_tavily(user_text)
        if data is None:
            return "网络搜索失败，请稍后重试。"
        if isinstance(data, str):
            return f"Tavily 总结：{data}\n请用中文简洁回答，保持当前角色语气。"
        answer = data.get("answer", "").strip()
        if answer:
            return f"Tavily 总结：{answer}\n请用中文简洁回答，保持当前角色语气。"
        results = _tavily_to_results(data, 5)
    else:
        results = search_web(user_text, 5)

    if not results:
        return "网络搜索没有返回有效内容。"
    info = "以下是从网络上查到的相关信息：\n"
    for i, r in enumerate(results, 1):
        content = (r.get("content") or "").strip()
        if content:
            info += f"{i}. {content}\n"
    info += "请用中文简洁回答，保持当前角色语气。"
    return info
