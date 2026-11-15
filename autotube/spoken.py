# -*- coding: utf-8 -*-
"""Write numbers the way a Korean narrator says them, before text-to-speech.

TTS models often stumble on "99.9999%", "300mm", "1/1000", "2,400억": they read digits one by one,
skip the decimal point or switch to English. The narration text is converted to spoken Korean
(Sino-Korean numerals); the planner is also told to write long digit strings in spoken form.
"""
from __future__ import annotations
import re

DIG = "영일이삼사오육칠팔구"
UNITS = [(10 ** 16, "경"), (10 ** 12, "조"), (10 ** 8, "억"), (10 ** 4, "만")]


def _under10000(n):
    out = ""
    for val, name in ((1000, "천"), (100, "백"), (10, "십")):
        q, n = divmod(n, val)
        if q:
            out += ("" if q == 1 else DIG[q]) + name
    if n:
        out += DIG[n]
    return out


def int_ko(n: int) -> str:
    if n == 0:
        return "영"
    out = ""
    for val, name in UNITS:
        q, n = divmod(n, val)
        if q:
            out += ("" if (q == 1 and name == "만") else _under10000(q)) + name + " "
    if n:
        out += _under10000(n)
    return out.strip()


NATIVE = ["", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉", "열", "열한", "열두", "열세", "열네", "열다섯"]


def num_ko(s: str) -> str:
    s = s.replace(",", "")
    if "." in s:
        a, b = s.split(".", 1)
        frac = b
        if len(frac) >= 4 and len(set(frac)) == 1:  # 99.99999 -> 구십구 점 구가 다섯 개
            cnt = NATIVE[len(frac)] if len(frac) < len(NATIVE) else int_ko(len(frac))
            return f"{int_ko(int(a or 0))}쩜, {DIG[int(frac[0])]}가 {cnt} 개 이어지는"
        return f"{int_ko(int(a or 0))}쩜" + "".join(DIG[int(c)] for c in frac)
    return int_ko(int(s))


UNIT_WORDS = {"%": "퍼센트", "mm": "밀리미터", "cm": "센티미터", "km": "킬로미터", "m": "미터", "nm": "나노미터",
              "kg": "킬로그램", "g": "그램", "°C": "도", "℃": "도", "km/h": "킬로미터"}


def to_spoken_ko(text: str) -> str:
    if not text:
        return text
    text = hangul_phrases(apply_pronunciation(text))
    # ranges 98~99% -> 98에서 99%
    text = re.sub(r"(\d)\s*[~∼]\s*(\d)", lambda m: m.group(1) + "에서 " + m.group(2), text)
    # fractions 1/1000 -> 천분의 일
    text = re.sub(r"(?<![\d.])(\d+)\s*/\s*(\d+)(?![\d.])",
                  lambda m: f"{int_ko(int(m.group(2)))}분의 {int_ko(int(m.group(1)))}", text)
    # number + unit
    unit_rx = "|".join(re.escape(u) for u in sorted(UNIT_WORDS, key=len, reverse=True))
    text = re.sub(r"(\d[\d,]*(?:\.\d+)?)\s*(" + unit_rx + r")(?![A-Za-z])",
                  lambda m: f"{num_ko(m.group(1))} {UNIT_WORDS[m.group(2)]}", text)
    # remaining plain numbers (keep years/ordinals natural: 2024년 -> 이천이십사 년)
    text = re.sub(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?", lambda m: num_ko(m.group(0)), text)
    return re.sub(r"\s{2,}", " ", text)


# ---------------------------------------------------------------------------------------------
# Pronunciation dictionary: foreign names and words the voice reads wrongly are written the way a Korean
# narrator says them. Only the text sent to the voice is changed - subtitles keep the original spelling.
# The owner can add entries in data/pronunciation.json  ({"원문": "읽는 방식"}).
# ---------------------------------------------------------------------------------------------
DEFAULT_PRON = {
    # English words and names are NOT turned into Hangul: they stay in Latin letters so the voice says them in native
    # English ("Claude 3 Opus" -> "Claude three Opus"). Only Korean words the voice gets wrong are listed here.
    "곳이": "자리가", "곳에": "자리에", "곳을": "자리를", "곳은": "자리는", "등골": "등꼴",
}


ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def en_int(n: int) -> str:
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        return ONES[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + en_int(n % 100))
    for val, name in ((10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand")):
        if n >= val:
            return en_int(n // val) + " " + name + ("" if n % val == 0 else " " + en_int(n % val))
    return str(n)


def en_num(tok: str) -> str:
    tok = tok.replace(",", "")
    if "." in tok:
        a, b = tok.split(".", 1)
        return en_int(int(a or 0)) + " point " + " ".join(ONES[int(c)] for c in b)
    return en_int(int(tok))


def english_phrases(text: str) -> str:
    """Numbers inside English names/phrases are spelled in English words: 'Claude 3.5 Sonnet' -> 'Claude three point
    five Sonnet', 'o1' -> 'o one', 'Llama 3.1 405B' -> 'Llama three point one four hundred five B'."""
    pat = r"(?<![가-힣A-Za-z0-9])[A-Za-z][A-Za-z0-9.-]*(?:[ ][A-Za-z0-9][A-Za-z0-9.,-]*)*"

    def fix_token(tok):
        tail = ""
        while tok and tok[-1] in ".,-":
            tail = tok[-1] + tail
            tok = tok[:-1]
        m = re.fullmatch(r"([A-Za-z]+)([0-9]+)", tok)
        if m:
            return m.group(1) + " " + en_num(m.group(2)) + tail
        m = re.fullmatch(r"([0-9][0-9,]*(?:[.][0-9]+)?)([A-Za-z]*)", tok)
        if m:
            return en_num(m.group(1)) + ((" " + m.group(2)) if m.group(2) else "") + tail
        return tok + tail

    def fix(m):
        return " ".join(fix_token(t) for t in m.group(0).split(" "))
    return re.sub(pat, fix, text)


def _load_pron():
    table = dict(DEFAULT_PRON)
    try:
        import json
        from . import config
        f = config.DATA / "pronunciation.json"
        if f.exists():
            table.update({str(k): str(v) for k, v in json.loads(f.read_text(encoding="utf-8")).items()})
    except Exception:
        pass
    return table


def apply_pronunciation(text: str) -> str:
    if not text:
        return text
    for src, dst in sorted(_load_pron().items(), key=lambda kv: -len(kv[0])):
        if re.search(r"[A-Za-z0-9]", src):
            text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(src) + r"(?![A-Za-z0-9])", dst, text)
        else:
            text = text.replace(src, dst)
    return text


# ---------------------------------------------------------------------------------------------
# One consistent way to say English names inside Korean narration: written in Hangul the way a Korean narrator says
# them, English digits as "쓰리", "포인트 파이브". (Mixing real English audio into a Korean voice sounded broken.)
# ---------------------------------------------------------------------------------------------
NAME_HANGUL = {
    "claude": "클로드", "anthropic": "앤트로픽", "openai": "오픈에이아이", "opus": "오퍼스", "sonnet": "소넷", "haiku": "하이쿠",
    "gemini": "제미나이", "llama": "라마", "gpt": "지피티", "chatgpt": "챗지피티", "pro": "프로", "apollo": "아폴로",
    "redwood": "레드우드", "research": "리서치", "eu": "이유", "ai": "에이아이", "act": "액트", "google": "구글",
    "deepmind": "딥마인드", "meta": "메타", "sandbagging": "샌드배깅", "o": "오", "mistral": "미스트랄", "grok": "그록",
    "microsoft": "마이크로소프트", "nvidia": "엔비디아", "tesla": "테슬라", "nasa": "나사", "mit": "엠아이티",
}
DIGIT_EN = ["제로", "원", "투", "쓰리", "포", "파이브", "식스", "세븐", "에이트", "나인"]


SINO_DIGIT = ["공", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구"]


def _num_en_ko(tok: str, unit: str = "") -> str:
    """How the owner says them (checked against a recording of the owner): 'Claude 3.5' -> 삼 점 오, 'Claude 3 Opus' ->
    쓰리 오퍼스, 'o1' -> 오원, 'Gemini 1.5' -> 일 점 오, '405B' -> 사공오 비."""
    tok = tok.replace(",", "")
    if "." in tok:  # said as one word: 일쩜오, 삼쩜오, 삼쩜일
        a, b = tok.split(".", 1)
        return "".join(SINO_DIGIT[int(c)] for c in a) + "쩜" + "".join(SINO_DIGIT[int(c)] for c in b)
    n = int(tok)
    if len(tok) >= 3 and unit:
        return "".join(SINO_DIGIT[int(c)] for c in tok)
    return DIGIT_EN[n] if n < 10 else int_ko(n)


def hangul_phrases(text: str) -> str:
    pat = r"(?<![가-힣A-Za-z0-9])[A-Za-z][A-Za-z0-9.-]*(?:[ ][A-Za-z0-9][A-Za-z0-9.,-]*)*"

    def word(w):
        return NAME_HANGUL.get(w.lower(), w)

    def fix_token(tok):
        tail = ""
        while tok and tok[-1] in ".,-":
            tail = tok[-1] + tail
            tok = tok[:-1]
        m = re.fullmatch(r"([A-Za-z]+)([0-9]+)", tok)
        if m:
            return word(m.group(1)) + _num_en_ko(m.group(2)) + tail
        m = re.fullmatch(r"([0-9][0-9,]*(?:[.][0-9]+)?)([A-Za-z]*)", tok)
        if m:
            unit = {"b": "비", "m": "엠", "k": "케이"}.get(m.group(2).lower(), word(m.group(2))) if m.group(2) else ""
            return _num_en_ko(m.group(1), m.group(2)) + ((" " + unit) if unit else "") + tail
        return word(tok) + tail

    def fix(m):
        return " ".join(fix_token(t) for t in m.group(0).replace("-", " ").split(" "))
    return re.sub(pat, fix, text)
