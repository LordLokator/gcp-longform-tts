from __future__ import annotations

import subprocess
from tempfile import NamedTemporaryFile, TemporaryDirectory
import argparse
import json
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from google.api_core import exceptions as gexc
from google.cloud import texttospeech

from config import ROOT, MAX_BYTES_PER_REQUEST
from utils import utf8_len


CFG_PATH = ROOT / "config.json"


_MUSICAL_BLOCK = re.compile(r"[\U0001D100-\U0001D1FF]")
_INVISIBLES = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2060-\u206F\ufeff]")


def sanitize_for_tts(text: str) -> str:
    # Normalize (often helps with odd ligatures / compatibility chars)
    text = unicodedata.normalize("NFKC", text)

    # Drop musical symbols + invisibles
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


def _write_error_marker(path: Path, *, chunk_id: int, err_type: str, err_msg: str, segment: str) -> None:
    """
    Writes a sidecar marker that also contains the FULL failed segment,
    so main() can resplit and retry without needing to reconstruct the original chunk.
    """
    err_path = path.with_suffix(path.suffix + ".error.txt")
    payload = (
        f"[chunk {chunk_id}] {err_type}: {err_msg}\n"
        f"bytes={utf8_len(segment)} chars={len(segment)}\n"
        "\n--- segment (full) ---\n"
        f"{segment}\n"
        "--- end segment ---\n"
    )
    err_path.write_text(payload, encoding="utf-8", newline="\n")


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

        sentences = [x.strip() for x in re.split(sent_end_re, p) if x.strip()]
        current = ""
        for sentence in sentences:
            if not current:
                current = sentence
                continue

            candidate = current + " " + sentence
            if utf8_len(candidate) <= max_bytes:
                current = candidate
            else:
                # If even a single sentence is too big, hard-cut it.
                if utf8_len(current) > max_bytes:
                    for piece in hard_cut_by_bytes(current):
                        yield piece
                    current = ""
                else:
                    yield current.strip()
                    current = sentence

        if current:
            if utf8_len(current) <= max_bytes:
                yield current.strip()
            else:
                for piece in hard_cut_by_bytes(current):
                    yield piece


def _split_keep_delim(text: str, delim: str) -> list[str]:
    """
    Split by a delimiter, keeping it attached to the left side.
    Example delim=";" => ["a;", " b;", " c"]
    """
    # Put a capture group around the delimiter and re-attach.
    parts = re.split(f"({re.escape(delim)})", text)
    if not parts:
        return []
    out: list[str] = []
    buf = ""
    for p in parts:
        if p == "":
            continue
        buf += p
        if p == delim:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _iter_chunks_aggressive(text: str, max_bytes: int) -> Iterator[str]:
    """
    More aggressive chunking for retry:
    try splitting by ';' then ':' then ',' (in this order), then whitespace, then hard byte-cut.
    """
    text = sanitize_for_tts(text)
    if not text:
        return

    def hard_cut_by_bytes(s: str) -> list[str]:
        out: list[str] = []
        buf = ""
        for ch in s:
            cand = buf + ch
            if utf8_len(cand) > max_bytes and buf:
                out.append(buf.strip())
                buf = ch
            else:
                buf = cand
        if buf.strip():
            out.append(buf.strip())
        return out

    if utf8_len(text) <= max_bytes:
        yield text
        return

    # Step 1: split by sub-sentence punctuation, keep delimiters on the left side
    candidates: list[str] = [text]
    for delim in [";", ":", ","]:
        next_cands: list[str] = []
        for c in candidates:
            if utf8_len(c) <= max_bytes:
                next_cands.append(c)
                continue
            next_cands.extend(_split_keep_delim(c, delim))
        candidates = [x.strip() for x in next_cands if x and x.strip()]

    # Step 2: pack candidates into <= max_bytes
    packed: list[str] = []
    cur = ""
    for piece in candidates:
        if not cur:
            cur = piece
            continue
        cand = (cur + " " + piece).strip()
        if utf8_len(cand) <= max_bytes:
            cur = cand
        else:
            packed.append(cur.strip())
            cur = piece
    if cur.strip():
        packed.append(cur.strip())

    # Step 3: if any packed piece is still too large, split by whitespace; final fallback byte-cut
    for p in packed:
        if utf8_len(p) <= max_bytes:
            yield p
            continue

        words = [w for w in re.split(r"\s+", p) if w]
        curw = ""
        for w in words:
            if not curw:
                curw = w
                continue
            candw = curw + " " + w
            if utf8_len(candw) <= max_bytes:
                curw = candw
            else:
                if utf8_len(curw) > max_bytes:
                    for piece in hard_cut_by_bytes(curw):
                        yield piece
                    curw = w
                else:
                    yield curw.strip()
                    curw = w

        if curw:
            if utf8_len(curw) <= max_bytes:
                yield curw.strip()
            else:
                for piece in hard_cut_by_bytes(curw):
                    yield piece


def _build_client_and_params(config: dict) -> tuple[texttospeech.TextToSpeechClient, texttospeech.VoiceSelectionParams, texttospeech.AudioConfig]:
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

    return client, voice, audio_config


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
    write_failed_segment_txt: bool = True,
) -> list[Path]:
    """
    Normal synthesis pass. Skips failing chunks but writes .error.txt markers (and optionally embeds the full failed segment).
    """

    client, voice, audio_config = _build_client_and_params(config)

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
        segment = sanitize_for_tts(_segment)

        try:
            synthesis_input = texttospeech.SynthesisInput(text=segment)
            print(f"[tts] chunk {idx}/{len(parts)} | head={segment[:40]!r} | bytes={utf8_len(segment)}")

            resp = client.synthesize_speech(
                request={
                    "input": synthesis_input,
                    "voice": voice,
                    "audio_config": audio_config,
                }
            )

            part_path.write_bytes(resp.audio_content)
            written.append(part_path)

            dt = time.time() - start
            print(f"[ok ] {part_path.name} ({dt:.2f}s)")

            if sleep_s > 0:
                time.sleep(sleep_s)

        except gexc.InvalidArgument as e:
            print(f"[SKIP] {part_path.name} -> InvalidArgument: {e}")
            _write_error_marker(
                part_path,
                chunk_id=idx,
                err_type="InvalidArgument",
                err_msg=str(e),
                segment=(segment if write_failed_segment_txt else segment[:200]),
            )
            continue

        except Exception as e:
            print(f"[SKIP] {part_path.name} -> {type(e).__name__}: {e}")
            _write_error_marker(
                part_path,
                chunk_id=idx,
                err_type=type(e).__name__,
                err_msg=str(e),
                segment=(segment if write_failed_segment_txt else segment[:200]),
            )
            continue

    print(f"[done] {output_stem}: {len(text)} chars, {len(parts)} chunk(s), wrote {len(written)} file(s)")
    return written


@dataclass(frozen=True)
class ErrorRecord:
    chunk_id: int
    err_type: str
    err_msg: str
    err_path: Path
    wav_path: Path


_ERR_CHUNK_RE = re.compile(r"_part(\d+)\.wav\.error\.txt$")


def _parse_error_file(path: Path) -> ErrorRecord | None:
    """
    Expected first line:
      [chunk N] ErrorType: message
    """
    m = _ERR_CHUNK_RE.search(path.name)
    if not m:
        return None
    chunk_id = int(m.group(1))

    head = path.read_text(encoding="utf-8", errors="replace")
    first_line = head.splitlines()[0] if head else ""
    m2 = re.match(r"^\[chunk\s+(\d+)\]\s+([^:]+):\s*(.*)$", first_line.strip())
    if m2:
        err_type = m2.group(2).strip()
        err_msg = m2.group(3).strip()
    else:
        err_type = "UnknownError"
        err_msg = first_line.strip()

    wav_path = path.with_suffix("")  # removes ".txt" => "...wav.error"
    wav_path = wav_path.with_suffix("")  # removes ".error" => "...wav"
    return ErrorRecord(
        chunk_id=chunk_id,
        err_type=err_type,
        err_msg=err_msg,
        err_path=path,
        wav_path=wav_path,
    )


def _extract_full_segment_from_error(err_text: str) -> str | None:
    """
    Pulls the '--- segment (full) ---' block from the marker.
    """
    start = err_text.find("--- segment (full) ---")
    if start < 0:
        return None
    start = err_text.find("\n", start)
    if start < 0:
        return None
    end = err_text.find("--- end segment ---", start)
    if end < 0:
        return None
    segment = err_text[start:end].strip("\n")
    return segment.strip() or None


def retry_failed_chunks(
    *,
    output_stem: str,
    config: dict,
    out_dir: Path,
    max_bytes: int = MAX_BYTES_PER_REQUEST // 5,
    sleep_s: float = 0.0,
    dry_run: bool = False,
    remove_error_marker_on_success: bool = False,
) -> list[Path]:
    """
    Retry ONLY chunks that have .error.txt markers.
    Re-split the *failed segment* aggressively, synthesize into TEMP mini-wavs,
    concat them into the ORIGINAL target wav name (e.g. ch_2_part394.wav),
    and delete the temp files.

    Healthy chunks are ignored.
    """
    client, voice, audio_config = _build_client_and_params(config)
    out_dir.mkdir(parents=True, exist_ok=True)

    err_files = sorted(out_dir.glob(f"{output_stem}_part*.wav.error.txt"))
    records = [r for r in (_parse_error_file(p) for p in err_files) if r is not None]

    if not records:
        print(f"[retry] no error markers found for stem={output_stem!r} in {out_dir}")
        return []

    by_type = Counter(r.err_type for r in records)
    print(f"[retry] found {len(records)} failed chunk(s) in {out_dir}")
    print("[retry] error types:")
    for k, v in by_type.most_common():
        print(f"  - {k}: {v}")

    written: list[Path] = []

    for rec in sorted(records, key=lambda r: r.chunk_id):
        # If the original wav exists, treat as healthy and ignore.
        if rec.wav_path.exists():
            print(f"[retry-skip] chunk {rec.chunk_id}: wav exists ({rec.wav_path.name})")
            continue

        err_text = rec.err_path.read_text(encoding="utf-8", errors="replace")
        segment = _extract_full_segment_from_error(err_text)
        if not segment:
            print(f"[retry-skip] chunk {rec.chunk_id}: cannot extract full segment from {rec.err_path.name}")
            continue

        sub_parts = list(_iter_chunks_aggressive(segment, max_bytes=max_bytes))
        print(f"[retry] chunk {rec.chunk_id}: resplit into {len(sub_parts)} mini-chunk(s) -> will rebuild {rec.wav_path.name}")

        if dry_run:
            continue

        # Synthesize mini-chunks to temp wavs, then concat into the original target wav name.
        with TemporaryDirectory() as td:
            tmp_dir = Path(td)
            tmp_wavs: list[Path] = []

            ok_all = True

            for i, sub in enumerate(sub_parts, start=1):
                sub = sanitize_for_tts(sub)
                tmp_wav = tmp_dir / f"tmp_sub{i:03d}.wav"

                start = time.time()
                try:
                    synthesis_input = texttospeech.SynthesisInput(text=sub)
                    print(
                        f"[tts] retry chunk {rec.chunk_id} mini {i}/{len(sub_parts)} "
                        f"| head={sub[:40]!r} | bytes={utf8_len(sub)}"
                    )

                    resp = client.synthesize_speech(
                        request={
                            "input": synthesis_input,
                            "voice": voice,
                            "audio_config": audio_config,
                        }
                    )

                    tmp_wav.write_bytes(resp.audio_content)
                    tmp_wavs.append(tmp_wav)

                    dt = time.time() - start
                    print(f"[ok ] tmp mini {i}: {tmp_wav.name} ({dt:.2f}s)")

                    if sleep_s > 0:
                        time.sleep(sleep_s)

                except Exception as e:
                    ok_all = False
                    print(f"[SKIP] retry chunk {rec.chunk_id} mini {i} -> {type(e).__name__}: {e}")
                    # Write a marker for the FINAL target path so you can retry again later.
                    _write_error_marker(
                        rec.wav_path,
                        chunk_id=rec.chunk_id,
                        err_type=f"RETRY_{type(e).__name__}",
                        err_msg=str(e),
                        segment=sub,
                    )
                    break

            if not ok_all:
                continue

            # Concat minis into the original wav filename
            try:
                _ffmpeg_concat_wavs_to_wav(tmp_wavs, rec.wav_path)
                written.append(rec.wav_path)
                print(f"[ok ] rebuilt: {rec.wav_path.name} (from {len(tmp_wavs)} mini-wav(s))")

                # After successful rebuild:
                resolved = rec.err_path.with_suffix("")          # -> ... .wav.error
                resolved = resolved.with_suffix(".resolved.txt") # -> ... .wav.error.resolved.txt
                try:
                    rec.err_path.replace(resolved)
                except Exception:
                    pass

            except subprocess.CalledProcessError as e:
                print(f"[SKIP] ffmpeg concat failed for chunk {rec.chunk_id}: {e}")
                _write_error_marker(
                    rec.wav_path,
                    chunk_id=rec.chunk_id,
                    err_type="RETRY_FFMPEG_CONCAT_FAILED",
                    err_msg=str(e),
                    segment=segment,
                )
                continue

    print(f"[retry-done] rebuilt {len(written)} wav chunk(s)")
    return written


def _ffmpeg_concat_wavs_to_wav(wavs: list[Path], out_wav: Path) -> None:
    """
    Concatenate WAV files in order into a single WAV using ffmpeg concat demuxer.
    """
    if not wavs:
        raise ValueError("No input wavs to concatenate.")

    out_wav.parent.mkdir(parents=True, exist_ok=True)

    def ffmpeg_quote(path: Path) -> str:
        s = str(path.resolve()).replace("\\", "/")
        s = s.replace("'", r"'\''")
        return f"file '{s}'"

    with NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", delete=False) as f:
        list_path = Path(f.name)
        for w in wavs:
            f.write(ffmpeg_quote(w) + "\n")

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c",
        "copy",
        str(out_wav),
    ]

    try:
        subprocess.run(cmd, check=True)
    finally:
        try:
            list_path.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Google Cloud TTS utilities (synthesize + retry failed chunks).")

    parser.add_argument("--config", type=Path, default=CFG_PATH, help="Path to config.json")
    parser.add_argument("--out-dir", type=Path, required=True, help="Directory for wav chunks and error markers")
    parser.add_argument("--output-stem", type=str, required=True, help="Base name for output chunk files")

    # Normal synthesis inputs
    parser.add_argument("--text-file", type=Path, help="Input text file for normal synthesis (UTF-8)")
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES_PER_REQUEST, help="Max UTF-8 bytes per request")
    parser.add_argument("--chunk-offset", type=int, default=0, help="Start from this chunk id (1-based)")
    parser.add_argument("--sleep-s", type=float, default=0.0, help="Sleep between requests (seconds)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen, do not call TTS")
    parser.add_argument("--skip-existing", action="store_true", help="Skip existing wav chunks in normal synthesis")

    # Retry mode
    parser.add_argument("--retry-errors", action="store_true", help="Retry only chunks that have .error.txt markers")
    args = parser.parse_args()

    config = load_gcloud_config(args.config)

    if args.retry_errors:
        retry_failed_chunks(
            output_stem=args.output_stem,
            config=config,
            out_dir=args.out_dir,
            # max_bytes=args.max_bytes,
            sleep_s=args.sleep_s,
            dry_run=args.dry_run,
        )
        return

    # Normal synthesis mode
    if not args.text_file:
        raise SystemExit("Missing --text-file (required unless --retry-errors is set).")

    text = args.text_file.read_text(encoding="utf-8", errors="strict")
    synthesize_text(
        text=text,
        output_stem=args.output_stem,
        config=config,
        out_dir=args.out_dir,
        max_bytes=args.max_bytes,
        chunk_offset=args.chunk_offset,
        sleep_s=args.sleep_s,
        dry_run=args.dry_run,
        skip_existing=args.skip_existing,
        write_failed_segment_txt=True,  # IMPORTANT for retry to work
    )


if __name__ == "__main__":
    main()