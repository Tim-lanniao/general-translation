"""Glossary format and matching: ``source=target`` lines with optional ``[scene]`` groups.

Indexed once, then each request selects only the terms its sentence contains. The gateway
matches written text; the audio app adds Cologne phonetics for recognised speech.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import chain


def _words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower()  # Maße and Masse are different entries
    return re.findall(r"[^\W_]+", text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue"))


def _sound(word: str) -> str:
    """Cologne phonetics for German names. Short words and codes stay literal."""
    word = word.replace("ß", "ss")
    if len(word) < 5 or not (word.isascii() and word.isalpha()):
        return word
    codes = []
    for i, ch in enumerate(word):
        before, after = word[i - 1:i], word[i + 1:i + 2]
        if ch in "aeijouy":
            code = "0"
        elif ch == "h":
            continue
        elif ch == "b" or (ch == "p" and after != "h"):
            code = "1"
        elif ch in "dt" and after not in ("c", "s", "z"):
            code = "2"
        elif ch in "fvpw":
            code = "3"
        elif ch in "gkq":
            code = "4"
        elif ch == "c":
            hard = "ahkloqrux" if i == 0 else "ahkoqux"
            code = "4" if before not in ("s", "z") and after and after in hard else "8"
        elif ch == "x" and before not in ("c", "k", "q"):
            code = "48"
        elif ch == "l":
            code = "5"
        elif ch in "mn":
            code = "6"
        elif ch == "r":
            code = "7"
        else:
            code = "8"
        for digit in code:
            if not codes or digit != codes[-1]:
                codes.append(digit)
    return "#" + "".join(d for i, d in enumerate(codes) if i == 0 or d != "0")


@dataclass(frozen=True)
class Term:
    source: str
    target: str
    scene: str

    def hint(self):
        return f"{self.source}={self.target}" if self.target else self.source


class Glossary:
    def __init__(self, raw: str, *, phonetic=True):
        self.phonetic = phonetic
        self.raw = raw
        self.entries = []
        self.by_scene = defaultdict(list)
        self.exact = defaultdict(list)
        self.sounds = {}
        scene = ""
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                scene = line[1:-1].strip().casefold()
                continue
            for item in re.split(r"[;；]", line):
                source, sep, target = item.replace("＝", "=").partition("=")
                # The original single-line setting accepted comma-separated names.
                names = [source] if sep else re.split(r"[,，、]", source)
                for name in names:
                    name = name.strip()
                    words = tuple(_words(name))
                    if not words or (sep and not target.strip()):
                        continue
                    term = Term(name, target.strip(), scene)
                    self.entries.append(term)
                    self.by_scene[scene].append(term)
                    self.exact[words].append(term)
                    code = re.fullmatch(r"([a-z]+)[ -]?(\d+[a-z]*)", name.lower())
                    if code:
                        # GX-120, GX 120 and GX120 designate the same model.
                        for alias in (code.groups(), ("".join(code.groups()),)):
                            if alias != words:
                                self.exact[alias].append(term)
                    if phonetic and all(w.isalpha() for w in words):
                        sound = tuple(map(_sound, words))
                        if self.sounds.setdefault(sound, words) != words:
                            self.sounds[sound] = None  # homophones require exact spelling
        self.lengths = sorted({len(key) for key in self.exact}, reverse=True)

    def _lookup(self, words, sounds):
        entries = self.exact.get(words)
        if entries or not all(w.isalpha() for w in words):
            return entries, False
        for suffix in ("s", "es", "n", "en", "e"):
            stem = words[-1].removesuffix(suffix)
            if stem != words[-1] and len(stem) >= 5:
                entries = self.exact.get((*words[:-1], stem))
                if entries:
                    return entries, False
        near = self.sounds.get(sounds)
        if near and all(a == b or SequenceMatcher(None, a, b).ratio() >= 0.7 for a, b in zip(near, words)):
            return self.exact[near], True
        return None, False

    def matches(self, source: str, topic: str, *, near_only=False) -> list[Term]:
        words = _words(source)
        sounds = list(map(_sound, words)) if self.phonetic else []
        scene = topic.casefold()
        result = []
        i = 0
        while i < len(words):
            for size in self.lengths:
                if i + size > len(words):
                    continue
                entries, near = self._lookup(tuple(words[i:i + size]), tuple(sounds[i:i + size]))
                if not entries:
                    continue
                if len(entries) == 1:
                    term = entries[0]
                else:
                    preferred = ([t for t in entries if t.scene and t.scene in scene]
                                 or [t for t in entries if not t.scene] or entries)
                    if len({t.target for t in preferred}) > 1:
                        continue
                    term = preferred[0]
                if (near or not near_only) and term not in result:
                    result.append(term)
                i += size
                break
            else:
                i += 1
        return result

    def translation_hints(self, source: str, topic: str, aliases=()) -> str:
        if not self.entries:
            return ""
        hints = []
        length = 0
        names = (self.matches(name, topic) for name in aliases)
        for term in chain(self.matches(source, topic), chain.from_iterable(names)):
            hint = term.hint()
            if hint in hints:
                continue
            if length + len(hint) + 1 <= 512:
                hints.append(hint)
                length += len(hint) + 1
                if len(hints) == 12:
                    break
        return "\n".join(hints)
