# core/judge.py
# 轻量判断：当前问题是否需要联网搜索实时信息（使用“判断模型”）。
import core.config as config
from core.llm import generate


def judge_need_online(user_text):
    if not config.internet_enabled:
        return False

    recent_user_msgs = [m["content"] for m in config.conversation_history[-4:] if m["role"] == "user"]
    context = "；".join(recent_user_msgs) if recent_user_msgs else user_text

    prompt = (
        f"对话历史：{context}\n\n"
        f"当前问题：{user_text}\n"
        "请判断当前问题是否需要联网搜索实时信息。\n"
        "判断原则：\n"
        "1. 如果是在索取现实世界的最新/外部信息（新闻、天气、实时事件、行情、价格、地址、营业时间等），请判断为需要联网；\n"
        "2. 如果当前问题是在延续之前的实时查询（如继续追问新闻详情、继续询问其他城市天气），也请判断为需要联网；\n"
        "3. 如果问题只是在延续对话、询问之前聊到的记忆/安排/偏好（如“那周六呢”“那个奶茶店叫什么”“然后呢”），请判断为无需联网；\n"
        "4. 其余日常闲聊、角色扮演、询问角色自身的事，无需联网。\n"
        "如果需要，回复“需要联网”；如果不需要，回复“无需联网”。\n"
    )

    result = generate(prompt, num_predict=-1, purpose="judge", user_text=user_text)

    if config.DEBUG_MODE:
        print(f"* (judge) 上下文：{context}")
        print(f"* (judge) 当前问题：{user_text}")
        print(f"* (judge) 判断结果：{result}")

    # 否定形式优先判断，避免“不需要联网”被“需要联网”误命中
    if "无需联网" in result or "不需要联网" in result or "不用联网" in result:
        return False
    return "需要联网" in result
