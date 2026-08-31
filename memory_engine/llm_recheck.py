# memory_engine/llm_recheck.py
# 置信度仲裁的「情况B」：模糊冲突时用轻量级 LLM 做二选一（只输出候选 ID，Token 极短）。
# 以及「注入复核」：检索/联网内容注入上下文前，判断其与当前对话是否相关。
# 可插拔：默认调用应用内判断模型（core.llm.generate）；LLM 不可用或调用失败时返回
# 安全默认值（二选一复核返回 None 由检索层回退 Top1；注入复核默认允许注入），保证不阻塞。
from __future__ import annotations

import memory_engine.logging as me_log


def _app_llm_available() -> bool:
    try:
        from core import llm  # noqa: F401
        return True
    except Exception:
        return False


def value_judge(user_input: str, reply: str) -> bool:
    """归档价值判断：这段对话是否包含值得长期记住的信息。

    数字协议：1=值得记住（具体事件/约定/偏好/经历/重要事实）；0=问候寒暄/语气应答/无信息量闲聊。
    判断模型不可用 / 输出无法解析 / 调用失败时默认保留（True，不丢数据）。
    """
    if not _app_llm_available():
        return True
    prompt = (
        f"用户说：{user_input}\n"
        f"助手回答：{reply}\n"
        "请判断这段对话是否包含值得长期记住的信息"
        "（具体事件、约定计划、偏好、经历、重要事实等）。\n"
        "如果只是问候、寒暄、语气应答、无信息量闲聊（如“你好”“哈哈”“好的”“谢谢”“再见”），"
        "请输出数字 0。\n"
        "包含值得记住的信息请输出数字 1。只输出 1 或 0，不要任何解释。"
    )
    try:
        from core import llm
        out = (llm.generate(prompt, num_predict=4, temperature=0.0,
                            purpose="archive_value", user_text=user_input) or "").strip()
        me_log.debug(f"[归档价值] 用户问题={(user_input or '')[:20]!r} 判断输出={out!r}")
        return "1" in out and "0" not in out
    except Exception:
        return True


def recheck_binary(user_input: str, cand_a: dict, cand_b: dict, role_hint: str = "") -> str:
    """二选一复核：返回选中候选的 id；无法判断 / LLM 不可用时返回 None。"""
    if not _app_llm_available():
        return None
    role_line = f"当前对话角色：{role_hint}。请优先选择与这些角色相关的事件。\n" if role_hint else ""
    prompt = (
        "你是记忆检索复核器。下面有两个历史记忆片段摘要和用户的查询。\n"
        + role_line
        + f"用户查询：{user_input}\n"
        f"候选A（id={cand_a['id']}）：{cand_a['summary']}\n"
        f"候选B（id={cand_b['id']}）：{cand_b['summary']}\n"
        "请判断哪一个更贴合用户查询。只输出候选 ID 本身（如 "
        f"{cand_a['id']} 或 {cand_b['id']}），不要任何其他内容。"
    )
    try:
        from core import llm
        reply = llm.generate(prompt, num_predict=24, temperature=0.0, purpose="memory_recheck",
                             user_text=user_input)
        reply = (reply or "").strip()
        for cand in (cand_a, cand_b):
            if cand["id"] in reply:
                return cand["id"]
        # 尝试解析 1/2
        if "1" in reply[:4]:
            return cand_a["id"]
        if "2" in reply[:4]:
            return cand_b["id"]
    except Exception:
        pass
    return None


def review_injection(user_input: str, content: str, context: str = "") -> bool:
    """注入复核：检索/联网内容与当前对话无关时阻止注入。

    数字协议（简化判断）：200=相关可注入；404=完全无关阻止注入。
    判断模型调用失败（异常）时默认允许注入（不丢信息）；但成功返回却未明确
    输出 200（含混 / 空输出 / 404 等）时按阻止处理，避免无关内容被放行。
    开关由「上下文记忆库」插件设置（cfg.INJECT_REVIEW）在引擎层控制。
    context：最近对话上下文（判断模型据此判断“用户正在聊什么”），可空。
    """
    if not content or not str(content).strip():
        return True
    if not _app_llm_available():
        return True
    parts = []
    if context:
        parts.append(f"最近的对话：\n{str(context)[:400]}\n\n")
    parts += [
        f"用户问题：{user_input}\n",
        f"检索到的内容：{str(content)[:300]}\n",
        "请判断检索到的内容是否与当前对话（含用户问题）相关，是否有助于回答用户。\n",
        "注意：即使检索内容与用户问题有个别字词重合，只要整体与当前对话无关"
        "（例如用户正在聊音乐，检索内容却是生日聚会），也要输出 404。\n",
        "另外：如果用户问题只是日常寒暄/打招呼（如“你好”“嘿嘿”），与任何检索内容"
        "都不构成实质相关，请输出 404。\n",
        "相关请只输出数字 200；完全无关请只输出数字 404。不要输出任何其他内容。",
    ]
    prompt = "".join(parts)
    try:
        from core import llm
        reply = (llm.generate(prompt, num_predict=8, temperature=0.0,
                              purpose="inject_review", user_text=user_input) or "").strip()
        me_log.debug(f"[注入复核] 用户问题={(user_input or '')[:30]!r} 带上下文={bool(context)} "
                     f"判断模型输出={reply!r}")
        if "404" in reply and "200" not in reply:
            return False
        if "200" in reply and "404" not in reply:
            return True
        # 成功返回但未明确输出 200（含混 / 空输出）→ 默认阻止，避免无关内容放行
        me_log.debug(f"[注入复核] 输出无法确认相关性（{reply!r}）→ 阻止注入")
        return False
    except Exception:
        return True
