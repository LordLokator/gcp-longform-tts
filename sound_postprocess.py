from __future__ import annotations

import re
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile

from config import AUDIO_CHUNKS_DIR, OUTPUT_AUDIO_DIR

# ch_intro_part1.wav, ch_1_part12.wav, ...
FILENAME_RE = re.compile(r"^ch_(?P<chapter>[^_]+)_part(?P<part>\d+)\.wav$", re.IGNORECASE)


def chapter_sort_key(chapter: str) -> tuple[int, int | str]:
    """
    Sort chapters with:
      intro first,
      then numeric chapters ascending,
      then anything else (lexicographically).
    """
    c = chapter.strip().lower()
    if c == "intro":
        return (0, 0)
    if c.isdigit():
        return (1, int(c))
    return (2, c)


def collect_by_chapter(input_dir: Path) -> dict[str, list[Path]]:
    if not input_dir.exists() or not input_dir.is_dir():
        raise FileNotFoundError(f"Input folder does not exist or is not a directory: {input_dir}")

    buckets: dict[str, list[tuple[int, str, Path]]] = {}

    for p in input_dir.iterdir():
        if not p.is_file():
            continue
        m = FILENAME_RE.match(p.name)
        if not m:
            continue

        chapter = m.group("chapter").strip()
        part = int(m.group("part"))
        buckets.setdefault(chapter, []).append((part, p.name.lower(), p))

    out: dict[str, list[Path]] = {}
    for chapter, items in buckets.items():
        items.sort(key=lambda x: (x[0], x[1]))
        out[chapter] = [p for _, __, p in items]

    return out


def _ffmpeg_concat_to_mp3(
    wavs: list[Path],
    out_mp3: Path,
    *,
    title: str | None = None,
    album: str | None = None,
    artist: str | None = None,
    track: str | int | None = None,
    genre: str = "Audiobook",
) -> None:
    """
    Concatenate WAV files in order into an MP3 using ffmpeg concat demuxer.
    Does not modify the input WAVs.
    """
    if not wavs:
        raise ValueError("No input wavs to concatenate.")

    out_mp3.parent.mkdir(parents=True, exist_ok=True)

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
        "-loglevel", "error",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_path),
        "-c:a", "libmp3lame",
        "-q:a", "2",
        str(out_mp3),
        "-metadata", f"genre={genre}",
    ]

    # Inject metadata if provided
    if title:
        cmd += ["-metadata", f"title={title}"]
    if album:
        cmd += ["-metadata", f"album={album}"]
    if artist:
        cmd += ["-metadata", f"artist={artist}"]
    if track is not None:
        cmd += ["-metadata", f"track={track}"]
        "-metadata", f"genre={genre}",

    try:
        subprocess.run(cmd, check=True)
    finally:
        try:
            list_path.unlink(missing_ok=True)
        except Exception:
            pass


def tag_mp3_ffmpeg(
    src: Path,
    dst: Path,
    *,
    title: str,
    album: str,
    artist: str,
    track: int | str,
    genre: str = "Audiobook",
) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(src),
        "-metadata", f"title={title}",
        "-metadata", f"album={album}",
        "-metadata", f"artist={artist}",
        "-metadata", f"track={track}",
        "-metadata", f"genre={genre}",
        "-codec", "copy",
        str(dst),
    ]
    subprocess.run(cmd, check=True)


def main() -> None:
    chapters = collect_by_chapter(AUDIO_CHUNKS_DIR)

    if not chapters:
        print(f"No matching WAV chunks found in: {AUDIO_CHUNKS_DIR}")
        return

    for chapter in sorted(chapters.keys(), key=chapter_sort_key):
        wavs = chapters[chapter]
        out_mp3 = OUTPUT_AUDIO_DIR / f"Chapter_{chapter}.mp3"
        print(f"[{chapter}] {len(wavs)} parts -> {out_mp3}")
        _ffmpeg_concat_to_mp3(wavs, out_mp3)
        tag_mp3_ffmpeg()

    print("Done.")


if __name__ == "__main__":
    main()
