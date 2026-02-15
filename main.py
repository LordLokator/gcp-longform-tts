from __future__ import annotations

import argparse
from pathlib import Path

from config import (
    SOURCE_PDF_DIR,
    RAW_TEXT_DIR,
    PROCESSED_TEXT_DIR,
    AUDIO_CHUNKS_DIR,
    OUTPUT_AUDIO_DIR,
)
from generate_report import write_markdown_review
from pdf_extractor import PdfToTextConfig, pdf_to_raw_text
from text_clean import CleanConfig, clean_file
from google_cloud_utils import load_gcloud_config, synthesize_text
from sound_postprocess import collect_by_chapter, chapter_sort_key


def cmd_extract(args: argparse.Namespace) -> None:
    src = Path(args.src)
    out = Path(args.out)

    cfg = PdfToTextConfig(
        src=src,
        out=out,
        join_with=args.join_with,
        overwrite=args.overwrite,
        create_parents=True,
        add_page_markers=False,
        bottom_pct=args.bottom,
        top_pct=args.top,
    )
    pdf_to_raw_text(cfg)
    print(f"[ok] raw text -> {out}")


def cmd_clean(args: argparse.Namespace) -> None:
    src = Path(args.src)
    out = Path(args.out)

    cfg = CleanConfig()
    clean_file(src, out, cfg=cfg)
    print(f"[ok] cleaned text -> {out}")


def cmd_tts(args: argparse.Namespace) -> None:
    src = Path(args.src)
    chapter = args.chapter
    out_stem = f"ch_{chapter}"

    text = src.read_text(encoding="utf-8").strip()
    config = load_gcloud_config()

    synthesize_text(
        text=text,
        config=config,
        output_stem=out_stem,
        out_dir=AUDIO_CHUNKS_DIR,
        chunk_offset=args.offset,
        sleep_s=args.sleep,
        dry_run=args.dry_run,
        skip_existing=args.skip_existing,
    )


def cmd_merge(args: argparse.Namespace) -> None:
    # Reuse the existing logic in sound_postprocess.py (mp3 per chapter)
    chapters = collect_by_chapter(AUDIO_CHUNKS_DIR)
    if not chapters:
        print(f"[warn] no wav chunks found in {AUDIO_CHUNKS_DIR}")
        return

    wanted = args.chapter
    for chapter in sorted(chapters.keys(), key=chapter_sort_key):
        if wanted and chapter != wanted:
            continue

        wavs = chapters[chapter]
        out_mp3 = OUTPUT_AUDIO_DIR / f"ch_{chapter}.mp3"
        from sound_postprocess import _ffmpeg_concat_to_mp3
        print(f"[merge] {chapter}: {len(wavs)} parts -> {out_mp3}")
        _ffmpeg_concat_to_mp3(wavs, out_mp3)

    print("[ok] merge done")


def cmd_run(args: argparse.Namespace) -> None:
    chapter = args.chapter
    pdf = Path(args.pdf)

    raw_out = RAW_TEXT_DIR / f"raw_{chapter}.txt"
    clean_out = PROCESSED_TEXT_DIR / f"text_{chapter}.txt"

    # 1) PDF -> raw text
    pdf_to_raw_text(
        PdfToTextConfig(
            src=pdf,
            out=raw_out,
            create_parents=True,
            add_page_markers=False,
            bottom_pct=args.bottom,
            top_pct=args.top,
            join_with=args.join_with,
            overwrite=args.overwrite,
        )
    )

    # 2) raw -> cleaned
    clean_file(raw_out, clean_out, cfg=CleanConfig())
    print(f"[ok] cleaned text -> {clean_out}")

    # 2.5) manual pause (optional) BEFORE any remote calls
    if args.pause_before_tts:
        while True:
            review_md = clean_out.with_suffix(".review.md")
            write_markdown_review(clean_out, review_md)
            print(f"[review] Markdown review created: {review_md}")

            print("\n[manual] Review/edit the cleaned text now.")
            print(f"         File [pdf]: {pdf}")
            print(f"         File [txt]: {clean_out}")

            print("         R -> REDO | Q -> QUIT | Else -> Continue")
            resp = input("> ").strip().lower()
            if resp in {"q", "quit", "exit"}:
                print("[abort] stopped before TTS")
                return
            elif resp in {"r", "redo"}:
                continue
            else:
                break

    # 3) cleaned -> wav chunks
    config = load_gcloud_config()
    synthesize_text(
        text=clean_out.read_text(encoding="utf-8"),
        config=config,
        output_stem=f"ch_{chapter}",
        out_dir=AUDIO_CHUNKS_DIR,
        chunk_offset=args.offset,
        sleep_s=args.sleep,
        dry_run=args.dry_run,
        skip_existing=args.skip_existing,
    )

    print("[done] run finished (use `python main.py merge --chapter ...` to create MP3)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="PDF -> cleaned text -> Google Cloud TTS -> merged audio")

    # Cropping options (available for ALL subcommands)
    p.add_argument(
        "--top",
        type=float,
        default=0.10,
        help="Top margin to ignore as fraction of page height (e.g. 0.10 = 10%).",
    )
    p.add_argument(
        "--bottom",
        type=float,
        default=0.10,
        help="Bottom margin to ignore as fraction of page height (e.g. 0.10 = 10%).",
    )

    sub = p.add_subparsers(required=True)

    # extract
    pe = sub.add_parser("extract", help="Extract raw text from a PDF")
    pe.add_argument("--src", required=True, help="Path to PDF")
    pe.add_argument("--out", required=True, help="Path to output .txt")
    pe.add_argument("--join-with", default="", help="String inserted between pages (default: '')")
    pe.add_argument("--overwrite", action="store_true")
    pe.set_defaults(func=cmd_extract)

    # clean
    pc = sub.add_parser("clean", help="Clean raw text into TTS-friendly text")
    pc.add_argument("--src", required=True, help="Path to raw .txt")
    pc.add_argument("--out", required=True, help="Path to cleaned .txt")
    pc.set_defaults(func=cmd_clean)

    # tts
    pt = sub.add_parser("tts", help="Call Google Cloud TTS on a cleaned text file -> wav chunks")
    pt.add_argument("--src", required=True, help="Path to cleaned .txt")
    pt.add_argument("--chapter", required=True, help="Chapter id, e.g. intro, 1, 2, 3")
    pt.add_argument("--out-dir", default=str(AUDIO_CHUNKS_DIR), help="Output directory for wav chunks")
    pt.add_argument("--offset", type=int, default=0, help="Skip first N chunks (1-indexed)")
    pt.add_argument("--sleep", type=float, default=0.0, help="Optional sleep between requests (seconds)")
    pt.add_argument("--dry-run", action="store_true", help="Only show chunking plan (no TTS requests)")
    pt.add_argument("--skip-existing", action="store_true", help="Skip chunks whose WAV already exists")
    pt.set_defaults(func=cmd_tts)

    # merge
    pm = sub.add_parser("merge", help="Merge wav chunks into MP3 (requires ffmpeg)")
    pm.add_argument("--chapter", default="", help="Only merge this chapter id (default: all)")
    pm.add_argument("--chunks-dir", default=str(AUDIO_CHUNKS_DIR), help="Where wav chunks are stored")
    pm.add_argument("--out-dir", default=str(OUTPUT_AUDIO_DIR), help="Where final MP3s go")
    pm.set_defaults(func=cmd_merge)

    # run
    pr = sub.add_parser("run", help="End-to-end: pdf -> raw -> clean -> tts chunks")
    pr.add_argument("--pdf", required=True, help="Path to input PDF")
    pr.add_argument("--chapter", required=True, help="Chapter id (used for filenames)")
    pr.add_argument("--join-with", default="", help="String inserted between pages (default: '')")
    pr.add_argument("--overwrite", action="store_true")
    pr.add_argument("--offset", type=int, default=0, help="Skip first N chunks (1-indexed)")
    pr.add_argument("--sleep", type=float, default=0.0, help="Optional sleep between requests (seconds)")
    pr.add_argument("--dry-run", action="store_true", help="Only show chunking plan (no TTS requests)")
    pr.add_argument("--skip-existing", action="store_true", help="Skip chunks whose WAV already exists")
    pr.add_argument(
        "--pause-before-tts",
        action="store_true",
        help="Pause after auto-cleanup so you can manually edit the cleaned text before TTS",
    )

    # outputs (override defaults)
    pr.add_argument("--raw-dir", default=str(RAW_TEXT_DIR), help="Directory for raw text outputs")
    pr.add_argument("--clean-dir", default=str(PROCESSED_TEXT_DIR), help="Directory for cleaned text outputs")
    pr.add_argument("--chunks-dir", default=str(AUDIO_CHUNKS_DIR), help="Directory for wav chunk outputs")
    pr.add_argument("--raw-out", default="", help="Explicit raw output path (overrides --raw-dir)")
    pr.add_argument("--clean-out", default="", help="Explicit cleaned output path (overrides --clean-dir)")

    pr.set_defaults(func=cmd_run)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
