# -*- coding: utf-8 -*-
"""Script/plan/review writer.

Order with providers.script == "auto":
  1. Claude Pro  via Claude Code  (subscription, $0 extra)
  2. ChatGPT Plus via Codex       (subscription, $0 extra)
  3. OpenAI API                   (pay per token)
  4. Gemini API                   (pay per token)
A provider whose plan window is full is paused for a while; the next one takes over.
"""
from __future__ import annotations
from . import config, http, budget, gemini, subscription

ALL = ("claude_code", "codex", "openai", "gemini")


class LLMError(RuntimeError):
    pass


def _available(name):
    if name in ("claude_code", "codex"):
        return subscription.available(name)
    if name == "openai":
        return bool(config.get_secret("OPENAI_API_KEY"))
    if name == "gemini":
        return bool(config.get_secret("GEMINI_API_KEY"))
    return False


def order():
    choice = config.load_settings()["providers"]["script"]
    seq = list(ALL) if choice in ("auto", "", None) else [choice] + [p for p in ALL if p != choice]
    return [p for p in seq if _available(p)]


def provider():
    seq = order()
    if not seq:
        raise LLMError("대본용 AI가 없습니다 (Claude Code·Codex 로그인 또는 OPENAI/GEMINI 키)")
    return seq[0]


def _openai(prompt, system, production_id, max_tokens, json_mode):
    s = config.load_settings()
    model = s["providers"]["openai_text_model"]
    budget.check(0.05, production_id)
    body = {"model": model, "instructions": system, "input": prompt, "max_output_tokens": max_tokens, "store": False}
    if json_mode:
        body["text"] = {"format": {"type": "json_object"}}
    headers = {"Authorization": "Bearer " + config.get_secret("OPENAI_API_KEY")}
    try:
        res = http.post_json("https://api.openai.com/v1/responses", body, headers=headers, timeout=600)
    except http.HttpError as ex:
        if ex.status == 400 and json_mode:  # model without json mode: retry once plain
            body.pop("text", None)
            res = http.post_json("https://api.openai.com/v1/responses", body, headers=headers, timeout=600)
        else:
            raise
    text = "".join(c.get("text", "") for item in res.get("output", []) for c in item.get("content", [])
                   if c.get("type") == "output_text")
    u = res.get("usage", {})
    tin, tout = u.get("input_tokens", len(prompt) // 2), u.get("output_tokens", len(text) // 2)
    p = s["prices"]
    budget.record("openai_text", tin + tout, tin / 1e6 * p["openai_per_mtok_in"] + tout / 1e6 * p["openai_per_mtok_out"],
                  model, production_id)
    if not text.strip():
        raise LLMError("OpenAI 빈 응답")
    return text


def _call(name, prompt, system, production_id, max_tokens, json_mode):
    tail = "\n\n반드시 JSON 객체 하나만 출력하라. 설명·코드블록 없이." if json_mode else ""
    if name == "claude_code":
        return subscription.run_claude_code(prompt + tail, system, production_id)
    if name == "codex":
        return subscription.run_codex(prompt + tail, system, production_id)
    if name == "openai":
        return _openai(prompt, system, production_id, max_tokens, json_mode)
    return gemini.generate_text(prompt + tail, system=system, production_id=production_id)


def generate(prompt: str, system: str = "", production_id=None, max_tokens=24000, json_mode=True) -> str:
    errors = []
    for name in order():
        try:
            text = _call(name, prompt, system, production_id, max_tokens, json_mode)
            if json_mode:
                http.parse_json_loose(text)  # broken JSON -> try the next provider
            return text
        except budget.BudgetExceeded:
            raise
        except Exception as ex:
            errors.append(f"{name}: {str(ex)[:150]}")
    raise LLMError("모든 대본 AI 실패 → " + " | ".join(errors) if errors else
                   "대본용 AI가 없습니다 (Claude Code·Codex 로그인 또는 OPENAI/GEMINI 키)")


def generate_json(prompt: str, system: str = "", production_id=None, max_tokens=24000):
    return http.parse_json_loose(generate(prompt, system, production_id, max_tokens, True))
