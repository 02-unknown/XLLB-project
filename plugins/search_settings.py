# plugins/search_settings.py —— 官方插件：联网搜索设置。
# 在 WebUI 中配置联网搜索方式：Tavily（推荐）、无需 Key 的 Bing（不推荐）、自定义搜索 API。
NAME = "联网搜索设置"
VERSION = "1.0.0"
DESCRIPTION = "配置联网搜索：Tavily（推荐）/ Bing（无需Key，不推荐）/ 自定义搜索 API"
AUTHOR = "官方"
OFFICIAL = True
HOT_SWAP = True

SETTINGS = {
    "provider": "tavily",
    "tavily_api_key": "",
    "custom_url": "",
    "custom_key": "",
}


def settings_schema():
    return [
        {"key": "provider", "label": "搜索方式", "type": "select",
         "options": [
             {"value": "tavily", "label": "Tavily（推荐）"},
             {"value": "bing", "label": "Bing（不推荐，无需Key）"},
             {"value": "custom", "label": "自定义搜索API"},
         ]},
        {"key": "tavily_api_key", "label": "Tavily API Key", "type": "password",
         "placeholder": "仅 Tavily 需要"},
        {"key": "custom_url", "label": "自定义搜索 URL", "type": "text",
         "placeholder": "如 https://api.example.com/search"},
        {"key": "custom_key", "label": "自定义 API Key", "type": "password",
         "placeholder": "可选"},
    ]


def _apply(settings, c):
    c.SEARCH_PROVIDER = settings.get("provider", "tavily")
    if settings.get("tavily_api_key"):
        c.TAVILY_API_KEY = settings["tavily_api_key"].strip()
    c.SEARCH_CUSTOM_URL = (settings.get("custom_url") or "").strip()
    c.SEARCH_CUSTOM_KEY = (settings.get("custom_key") or "").strip()


def on_load(settings, ctx):
    _apply(settings, ctx.config)


def on_settings_changed(settings, ctx):
    _apply(settings, ctx.config)


def commands():
    return [{"name": "/search", "desc": "查看/切换搜索方式", "args": "[tavily|bing|custom]"}]


def on_command(command, args, ctx):
    if command != "/search":
        return None
    c = ctx.config
    if args and args[0] in ("tavily", "bing", "custom"):
        ctx.manager.save_settings(NAME, {"provider": args[0]})
        return {"reply": f"搜索方式已切换为 {args[0]}。", "speak": True}
    return {"reply": f"当前搜索方式：{c.SEARCH_PROVIDER}"
                     f"（tavily=推荐；bing=无需Key，不推荐；custom=自定义API）", "speak": False}
