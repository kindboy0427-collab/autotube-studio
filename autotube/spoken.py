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
            return f"{int_ko(int(a or 0))} 점, {DIG[int(frac[0])]}가 {cnt} 개 이어지는"
        return f"{int_ko(int(a or 0))} 점 " + " ".join(DIG[int(c)] for c in frac)
    return int_ko(int(s))


UNIT_WORDS = {"%": "퍼센트", "mm": "밀리미터", "cm": "센티미터", "km": "킬로미터", "m": "미터", "nm": "나노미터",
              "kg": "킬로그램", "g": "그램", "°C": "도", "℃": "도", "km/h": "킬로미터"}


def to_spoken_ko(text: str) -> str:
    if not text:
        return text
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
