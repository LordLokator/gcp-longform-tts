from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

UPPER_HU = "A-ZÁÉÍÓÖŐÚÜŰ"
LOWER_HU = "a-záéíóöőúüű"

DOT_PLACEHOLDER = "∯"  # rare symbol to temporarily replace abbreviation dots

HU_CARDINAL = {
    1: "egy",
    2: "kettő",
    3: "három",
    4: "négy",
    5: "öt",
    6: "hat",
    7: "hét",
    8: "nyolc",
    9: "kilenc",
    10: "tíz",
}

HU_ORDINAL = {
    1: "Első",
    2: "Második",
    3: "Harmadik",
    4: "Negyedik",
    5: "Ötödik",
    6: "Hatodik",
    7: "Hetedik",
    8: "Nyolcadik",
    9: "Kilencedik",
    10: "Tizedik",
}


@dataclass
class CleanConfig:
    """Text cleanup config (tweak freely per book)."""

    # Lines to drop (full-line matches)
    drop_line_patterns: tuple[str, ...] = (
        r"^\s*\d+\s*$",                           # page number line: "12"
        r"^\s*[–—-]?\s*\d+\s*[–—-]?\s*$",         # "— 12 —"
    )

    # Extra "contains" filters (if you know header/footer text fragments)
    drop_line_if_contains: tuple[str, ...] = (
        # Example: "Tartalomjegyzék",
        # Example: "Kiadó:",
    )

    # Common abbreviations to protect from sentence splitting (extend freely)
    abbreviations: tuple[str, ...] = (
        "Dr.", "Prof.", "pl.", "Pl.", "stb.", "Stb.", "kb.", "Kb.",
        "u.", "U.", "vö.", "Vö.", "ld.", "Ld.", "sz.", "Sz.",
        "év.", "Év.", "old.", "Old.", "fej.", "Fej.",
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def normalize_headings(text: str) -> str:
    """
    1) Lowercase lines that contain only uppercase HU letters, digits and simple punctuation.
    2) Insert two blank lines before section-numbered headings like:
       1.
       2.3.
       4.5.6.
       7.8.9.10
    """

    # ----------------------------
    # 1) Lowercase full-caps lines
    # ----------------------------
    fullcaps_pattern = re.compile(
        rf"^[{UPPER_HU}0-9.:\-–—()\s]+$",
        re.MULTILINE
    )

    text = fullcaps_pattern.sub(
        lambda m: m.group(0).lower(),
        text
    )

    # ----------------------------
    # 2) Add spacing before numbered headings
    # ----------------------------
    # Matches:
    #   1.
    #   2.3.
    #   4.5.6.
    #   7.8.9.10
    numbering_pattern = re.compile(
        r"(?m)^(?=\d+(?:\.\d+)*\.?\s)"
    )

    text = numbering_pattern.sub("\n\n", text)

    return text


def replace_numbered_headings(text: str) -> str:
    def replacer(match):
        full = match.group(1)
        parts = [int(p) for p in full.split(".")]

        if len(parts) == 1:
            # 1.
            return HU_ORDINAL.get(parts[0], str(parts[0])) + " "
        else:
            # 1.1.1
            words = [HU_CARDINAL.get(p, str(p)) + " pont" for p in parts[:-1]]
            words.append(HU_CARDINAL.get(parts[-1], str(parts[-1])))
            return (" ".join(words) + " ").capitalize()

    pattern = re.compile(r"(?m)^(\d+(?:\.\d+)+|\d+)\.?\s+")
    return pattern.sub(replacer, text)

# def remove_digit_only_lines(text: str) -> str:
#     """
#     Remove lines that contain only digits (e.g. page numbers like '12').
#     """

#     pattern = re.compile(r"(?m)^[0-9]+\s*$")
#     return pattern.sub("", text)


def remove_headings(text: str) -> str:
    """
    Remove lines that contain only digits (e.g. page numbers like '12').
    """

    pattern = re.compile(r"(?m)^[\ufeff\u200b\u200c\u200d\u00a0]+.*Modul.*$")
    return pattern.sub("", text)


def fix_dangling_words(text: str) -> str:
    """
    Dirty fix for weird problem when e.g. "Az\nAlaptörvény" is improperly handled.
    """
    # Join short standalone words (1–10 letters) with next line
    return re.sub(
        rf"(?m)^([{LOWER_HU}{UPPER_HU}]{{1,10}})\s*\n+",
        r"\1 ",
        text,
    )

def fix_parentheses_linebreaks(text: str) -> str:
    result: list[str] = []
    depth = 0
    for ch in text:
        if ch == '(':
            depth += 1
            result.append(ch)
        elif ch == ')':
            if depth > 0:
                depth -= 1
            result.append(ch)
        elif ch == '\n' and depth > 0:
            result.append(' ')
        else:
            result.append(ch)
    return ''.join(result)



def normalize_unicode(text: str) -> str:
    # Normalize to a consistent form (helps with “lookalike” characters)
    text = unicodedata.normalize("NFKC", text)

    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove soft hyphen (discretionary hyphen) artifacts from PDF extraction
    # If it occurs at line end (word split), join without space.
    text = re.sub(r"\u00AD\s*\n\s*", "", text)
    # If it occurs elsewhere, just drop it.
    text = text.replace("\u00AD", "")

    # Convert NBSP and friends to normal spaces
    text = text.replace("\u00A0", " ").replace("\u2007", " ").replace("\u202F", " ")

    # Normalize quotes/dashes (optional but helps TTS consistency)
    text = text.replace("“", "\"").replace("”", "\"").replace("„", "\"")
    text = text.replace("\t", " ")
    text = text.replace("’", "'").replace("–", "-").replace("—", "-")

    return text


def remove_control_chars(text: str) -> str:
    # Remove ASCII control chars except newline/tab
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)
    return text


def drop_junk_lines(text: str, cfg: CleanConfig) -> str:
    lines = text.split("\n")
    kept: list[str] = []
    for line in lines:
        raw = line

        # Drop if matches any full-line pattern
        if any(re.match(pat, raw) for pat in cfg.drop_line_patterns):
            continue

        # Drop if contains known header/footer fragments
        if cfg.drop_line_if_contains and any(fragment in raw for fragment in cfg.drop_line_if_contains):
            continue

        kept.append(raw)
    return "\n".join(kept)


def fix_hyphenation(text: str) -> str:
    # 1) Hungarian year + suffix split across lines: "1996-\nban" -> "1996-ban"
    text = re.sub(
        r"(\b\d{4})-\n(?=(ban|ben|ból|ből|nál|nél|ra|re|ig|tól|től|val|vel|ként|kor)\b)",
        r"\1-",
        text,
    )
    return text


def collapse_whitespace(text: str) -> str:
    # Trim trailing spaces on each line
    text = re.sub(r"[ \t]+\n", "\n", text)
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Collapse internal multiple spaces (but keep newlines for now)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def merge_soft_linebreaks(text: str) -> str:
    """
    Heuristic merge:
    - Keep blank lines as paragraph separators.
    - Merge single newlines that look like wrapped lines into spaces.
    """

    text = re.sub(r"\n{3,}", "\n\n", text)

    lines = text.split("\n")
    out: list[str] = []
    i = 0

    bullet_re = re.compile(r"^\s*([•\-–—]|(\d+[\.\)]))\s+")
    heading_re = re.compile(r"^\s*[A-ZÁÉÍÓÖŐÚÜŰ0-9][A-ZÁÉÍÓÖŐÚÜŰ0-9 \-]{4,}\s*$")


    # If a whole line is just a short function word, merge it with the next line
    join_if_alone = {
        "a", "az", "és", "vagy", "de", "hogy", "mint", "mert", "hanem",
        "illetve", "valamint", "amely", "amelyek", "aki", "akik",
        "ez", "ezt", "azt", "annak", "ennek", "arra", "erre",
        "alapján", "szerint", "miatt", "között", "közül", "alatt", "fölött",
    }

    # If a normal line ENDS with a glue word, it's very likely wrapped text, not a new sentence
    join_if_endswith = join_if_alone | {
        "az", "a", "és", "vagy", "de", "valamint", "illetve", "mint", "mert",
        "hogy", "amely", "amelyek", "aki", "akik",
    }

    join_end_re = re.compile(rf"(?:^|\s)\b({'|'.join(map(re.escape, sorted(join_if_endswith, key=len, reverse=True)))})\s*$", re.IGNORECASE)


    while i < len(lines):
        line = lines[i].strip()
        if line == "":
            out.append("")  # blank line preserves paragraph boundary
            i += 1
            continue
        if bullet_re.match(line) or heading_re.match(line):
            out.append(line)  # keep bullets/headings separate
            i += 1
            continue

        buf = line
        j = i + 1
        while j < len(lines):
            nxt = lines[j].strip()
            if nxt == "":
                break
            if bullet_re.match(nxt) or heading_re.match(nxt):
                break

            # If current buffer ends a sentence strongly, don't merge further
            if re.search(r"[.!?…]([\"'\)\]]*)\s*$", buf):
                break

            # If next line starts like continuation: lowercase, digit, quote, bracket
            if re.match(rf"^[{LOWER_HU}0-9\"'\(\[]", nxt):
                buf += " " + nxt
                j += 1
                continue

            # If next starts uppercase, it might be a new sentence — BUT
            # allow merging if the current line is clearly a wrapped fragment ("Az", "A", etc.)
            if re.match(rf"^[{UPPER_HU}]", nxt):
                # NEW: merge if the previous line ends with a glue word (e.g. "... bemutatjuk az")
                if join_end_re.search(buf) or buf.lower() in join_if_alone or len(buf) <= 3:
                    buf += " " + nxt
                    j += 1
                    continue
                break

            buf += " " + nxt
            j += 1

        out.append(buf)
        i = j

    return "\n".join(out)


def protect_abbreviations(text: str, cfg: CleanConfig) -> str:
    for abbr in cfg.abbreviations:
        text = text.replace(abbr, abbr.replace(".", DOT_PLACEHOLDER))

    # Protect initials like "A." "B." in names
    text = re.sub(rf"\b([{UPPER_HU}])\.", rf"\1{DOT_PLACEHOLDER}", text)

    # Protect multi-initial sequences: "A. B." already handled, but this keeps it stable
    return text


def restore_abbreviations(text: str) -> str:
    return text.replace(DOT_PLACEHOLDER, ".")


def final_polish(text: str) -> str:
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def clean_text(text: str, cfg: CleanConfig) -> str:
    text = normalize_unicode(text)
    text = remove_control_chars(text)
    text = normalize_headings(text)
    text = drop_junk_lines(text, cfg)

    text = fix_hyphenation(text)
    text = collapse_whitespace(text)

    # text = remove_digit_only_lines(text)
    text = remove_headings(text)
    text = merge_soft_linebreaks(text)

    text = protect_abbreviations(text, cfg)
    text = insert_sentence_breaks(text)
    text = restore_abbreviations(text)

    text = fix_dangling_words(text)
    text = replace_numbered_headings(text)

    text = final_polish(text)
    return text


def clean_file(src: Path, out: Path, cfg: CleanConfig | None = None) -> Path:
    cfg = cfg or CleanConfig()
    raw = read_text(src)
    cleaned = clean_text(raw, cfg)
    write_text(out, cleaned)
    return out


def main():
    from config import RAW_TEXT_DIR, PROCESSED_TEXT_DIR

    src = RAW_TEXT_DIR / "raw_intro.txt"
    out = PROCESSED_TEXT_DIR / "text_intro.txt"

    clean_file(src, out)
    print(f"[WROTE] {out}")


if __name__ == "__main__":
    main()
