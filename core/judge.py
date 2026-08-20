# core/judge.py
# 轻量判断：当前问题是否需要联网搜索实时信息（使用“判断模型”）。
import core.config as config
from core.llm import generate


def judge_need_online(user_text):
    if not config.internet_enabled:
        return False

    recent_user_msgs = [m["content"] for m in config.conversation_history[-3:] if m["role"] == "user"]
    context = "；".join(recent_user_msgs) if recent_user_msgs else user_text

    prompt = (
        f"对话历史：{context}\n\n"
        f"当前问题：{user_text}\n"
        f"请判断当前问题是否需要联网搜索实时信息（如天气、时间、新闻等）。"
        f"如果当前问题是在延续之前的实时查询（如继续询问其他城市天气），请判断为需要联网。\n"
        f"如果需要，回复“需要联网”；如果不需要，回复“无需联网”。\n"
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
