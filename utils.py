from pathlib import Path
import re
import unicodedata


def read_raw_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def debug_string(s: str, n: int = 120):
    head = s[:n]
    print("repr(head):", repr(head))
    print("codepoints:", " ".join(f"U+{ord(c):04X}" for c in head[:40]))
    print("names:", " | ".join(unicodedata.name(c, "<?>") for c in head[:20]))


def utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def find_long_sentences(text: str, max_sentence_bytes: int = 1200):
    SENT_END_RE = re.compile(r'(?<=[.!?])\s+')
    # crude sentence split; good enough for diagnostics
    sentences = [s.strip() for s in SENT_END_RE.split(text) if s.strip()]
    long = [(i, utf8_len(s), s[:120]) for i, s in enumerate(sentences) if utf8_len(s) > max_sentence_bytes]
    return long
