"""Recognition hotwords: the gateway's glossary format plus a Whisper prompt within its token limit."""
from __future__ import annotations

from itertools import chain

from translation_gateway.terms import Glossary


class Hotwords(Glossary):
    def __init__(self, raw: str):
        super().__init__(raw, phonetic=True)
        self.topic = None
        self.topic_terms = []

    def recognition_prompt(self, topic: str, recent: str, tokenizer, budget: int = 223) -> str | None:
        if not self.entries:
            return None
        # Only the German side enters Whisper. Count the leading space just as
        # faster-whisper 1.2.1 does before retaining the last 223 prompt tokens.
        scene = topic.casefold()
        if self.topic != topic:
            mentioned = [t for t in self.entries if len(t.target) >= 2 and t.target.casefold() in scene]
            self.topic_terms = self.matches(topic, topic) + mentioned
            self.topic = topic
        priority = self.topic_terms + self.matches(recent, topic)
        groups = (entries for tag, entries in self.by_scene.items() if tag and tag in scene)
        names = []
        seen = set()
        prompt = None
        for term in chain(priority, chain.from_iterable(groups), self.by_scene.get("", ())):
            if term.source in seen:
                continue
            if len(seen) >= 64:  # bounded tokenizer work even with hundreds of oversized names
                break
            seen.add(term.source)
            candidate = "Namen und Begriffe: " + ", ".join([*names, term.source]) + "."
            if len(tokenizer.encode(" " + candidate, add_special_tokens=False).ids) > budget:
                continue
            names.append(term.source)
            prompt = candidate
            if len(names) == 32:
                break
        return prompt
