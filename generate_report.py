from pathlib import Path
import re


def write_markdown_review(src_txt: Path, out_md: Path, min_len: int = 25):
    text = src_txt.read_text(encoding="utf-8")

    keywords = ("táblázat", "ábra", "forrás", "http", "www")
    kw_re = re.compile(r"(" + "|".join(re.escape(k) for k in keywords) + r")", re.IGNORECASE)

    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        marked = line

        # highlight keywords
        marked = kw_re.sub(
            lambda m: f"<span style='color:red;font-weight:bold'>{m.group(1)}</span>",
            marked,
        )

        # highlight short lines
        if stripped and len(stripped) < min_len:
            marked = f"\n<span style='color:red'>⚠ SHORT: {marked}</span>"

        out_lines.append(marked)

    out_md.write_text("\n".join(out_lines), encoding="utf-8")
