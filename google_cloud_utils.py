from __future__ import annotations
import unicodedata
import re

import time
import json
from pathlib import Path
from typing import Iterable

from google.cloud import texttospeech

from config import ROOT, MAX_BYTES_PER_REQUEST
from utils import debug_string, utf8_len

from google.api_core import exceptions as gexc


def _write_error_marker(path: Path, msg: str) -> None:
    err_path = path.with_suffix(path.suffix + ".error.txt")
    err_path.write_text(msg, encoding="utf-8", newline="\n")


CFG_PATH = ROOT / "config.json"


# Remove: musical symbols block + common invisible/control chars that appear in PDFs
_MUSICAL_BLOCK = re.compile(r"[\U0001D100-\U0001D1FF]")
_INVISIBLES = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2060-\u206F\ufeff]")  # ZW*, bidi, BOM, etc.


def sanitize_for_tts(text: str) -> str:
    # Normalize (often helps with odd ligatures / compatibility chars)
    text = unicodedata.normalize("NFKC", text)

    # Drop musical symbols (your 𝄐𝄐) + invisibles
    text = _MUSICAL_BLOCK.sub("", text)
    text = _INVISIBLES.sub("", text)

    # Drop other control chars except \n and \t
    text = "".join(ch for ch in text if (ch in "\n\t") or (unicodedata.category(ch)[0] != "C"))

    # Collapse excessive whitespace
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def load_gcloud_config(path: Path = CFG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _iter_chunks_by_paragraphs(text: str, max_bytes: int) -> Iterable[str]:
    """
    Yield chunks that are <= max_bytes (UTF-8), preferring to cut on paragraph boundaries.

    Strategy (best effort):
      1) Pack paragraphs (split by blank line) into a chunk.
      2) If a single paragraph is too large, fall back to sentence-ish splitting.
      3) Final fallback: hard cut by bytes.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    def hard_cut_by_bytes(s: str) -> list[str]:
        out: list[str] = []
        buf = ""
        for ch in s:
            candidate = buf + ch
            if utf8_len(candidate) > max_bytes and buf:
                out.append(buf.strip())
                buf = ch
            else:
                buf = candidate
        if buf.strip():
            out.append(buf.strip())
        return out

    sent_end_re = r"(?<=[\.!\?…])\s+"
    for p in paragraphs:
        if utf8_len(p) <= max_bytes:
            yield p
            continue

        # Paragraph too large -> split into sentences (rough)
        sentences = [x.strip() for x in __import__("re").split(sent_end_re, p) if x.strip()]
        cur = ""
        for s in sentences:
            if not cur:
                cur = s
                continue

            cand = cur + " " + s
            if utf8_len(cand) <= max_bytes:
                cur = cand
            else:
                # If even a single sentence is too big, hard-cut it.
                if utf8_len(cur) > max_bytes:
                    for piece in hard_cut_by_bytes(cur):
                        yield piece
                    cur = ""
                else:
                    yield cur.strip()
                    cur = s

        if cur:
            if utf8_len(cur) <= max_bytes:
                yield cur.strip()
            else:
                for piece in hard_cut_by_bytes(cur):
                    yield piece


def synthesize_text(
    *,
    text: str,
    output_stem: str,
    config: dict,
    out_dir: Path,
    max_bytes: int = MAX_BYTES_PER_REQUEST,
    chunk_offset: int = 0,
    sleep_s: float = 0.0,
    dry_run: bool = False,
    skip_existing: bool = False,
) -> list[Path]:

    client = texttospeech.TextToSpeechClient()

    voice = texttospeech.VoiceSelectionParams(
        language_code=config["voice"]["languageCode"],
        name=config["voice"]["name"],
    )

    audio_config = texttospeech.AudioConfig(
        audio_encoding=getattr(texttospeech.AudioEncoding, config["audioConfig"]["audioEncoding"]),
        sample_rate_hertz=config["audioConfig"].get("sampleRateHertz"),
        speaking_rate=config["audioConfig"].get("speakingRate"),
    )

    out_dir.mkdir(parents=True, exist_ok=True)

    text = sanitize_for_tts(text)
    parts = list(_iter_chunks_by_paragraphs(text, max_bytes=max_bytes))
    written: list[Path] = []

    if dry_run:
        total = len(parts)
        would = []
        for idx in range(1, total + 1):
            if idx < chunk_offset:
                continue
            part_path = out_dir / f"{output_stem}_part{idx}.wav"
            if skip_existing and part_path.exists():
                continue
            would.append(idx)
        print(f"[dry-run] {output_stem}: {len(text)} chars, {total} chunk(s)")
        if chunk_offset:
            print(f"[dry-run] offset={chunk_offset} -> starts at chunk {chunk_offset}")
        if skip_existing:
            print("[dry-run] skip_existing=True (will not resynthesize existing wav chunks)")
        print(f"[dry-run] would synthesize {len(would)} chunk(s): {would[:20]}" + (" ..." if len(would) > 20 else ""))
        return []

    for idx, _segment in enumerate(parts, start=1):
        if idx < chunk_offset:
            print(f"[skip] chunk {idx}/{len(parts)} (offset={chunk_offset})")
            continue

        part_path = out_dir / f"{output_stem}_part{idx}.wav"
        if skip_existing and part_path.exists():
            print(f"[skip] exists: {part_path.name}")
            continue

        start = time.time()
        try:
            # Prepare the input for this segment.
            segment = sanitize_for_tts(_segment)
            synthesis_input = texttospeech.SynthesisInput(text=segment)
            print(f"[tts] chunk {idx}/{len(parts)} | head={segment[:40]!r} | bytes={utf8_len(segment)}")

            # Perform the synthesis.
            resp = client.synthesize_speech(
                request={
                    "input": synthesis_input,
                    "voice": voice,
                    "audio_config": audio_config
                }
            )

            part_path.write_bytes(resp.audio_content)
            written.append(part_path)

            dt = time.time() - start
            print(f"[ok ] {part_path.name} ({dt:.2f}s)")

            if sleep_s > 0:
                time.sleep(sleep_s)

        except gexc.InvalidArgument as e:
            # Known failure class: sentence too long (or other invalid input)
            msg = f"[chunk {idx}] InvalidArgument: {e}\n\n--- chunk head ---\n{repr(segment)}\n"
            print(f"[SKIP] ch_{idx}_part{idx}.wav -> {e}")
            _write_error_marker(part_path, msg)
            continue

        except Exception as e:
            # Any other error: also skip, but record it
            msg = f"[chunk {idx}] Exception: {type(e).__name__}: {e}\n\n--- chunk head ---\n{repr(segment)}\n"
            print(f"[SKIP] ch_{idx}_part{idx}.wav -> {type(e).__name__}: {e}")
            _write_error_marker(part_path, msg)
            continue

    print(f"[done] {output_stem}: {len(text)} chars, {len(parts)} chunk(s), wrote {len(written)} file(s)")
    return written
