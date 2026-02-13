"""
Usage examples:
  python pdf_to_raw_text.py --src Alapvizsga_Module_3.pdf --out raw_ch_3.txt
  python pdf_to_raw_text.py --src-dir source_documents --out-dir raw_text_documents --pattern "*.pdf"
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import pymupdf


@dataclass(frozen=True)
class PdfToTextConfig:
    src: Path
    out: Path
    join_with: str = ""          # "" keeps original page text concatenation tight; "\n\n" adds spacing
    newline: str = "\n"          # output newline normalization
    encoding: str = "utf-8"
    overwrite: bool = False
    create_parents: bool = True

    # Cropping: fractions of page height to ignore from top/bottom (0.0 disables)
    top_pct: float = 0.0
    bottom_pct: float = 0.0

    # Optional debug page markers like "=== PAGE i ==="
    add_page_markers: bool = False


def write_text(path: Path, text: str, *, encoding: str = "utf-8", newline: str = "\n") -> None:
    path.write_text(text, encoding=encoding, newline=newline)


def _validate_crop(top_pct: float, bottom_pct: float) -> None:
    if top_pct == 0.0 and bottom_pct == 0.0:
        return
    if not (0.0 <= top_pct < 0.5 and 0.0 <= bottom_pct < 0.5 and (top_pct + bottom_pct) < 0.9):
        raise ValueError(
            "top_pct and bottom_pct must be in [0, 0.5) and not remove almost the whole page "
            "(also require top_pct + bottom_pct < 0.9)."
        )


def extract_text_from_pdf(
    pdf_path: Path,
    *,
    page_sep: str = "",
    top_pct: float = 0.0,
    bottom_pct: float = 0.0,
    add_page_markers: bool = False,
) -> str:
    """
    Extract text from PDF. Optionally crop the work area by ignoring top/bottom
    fractions of each page height (useful for headers/footers).
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Not a PDF: {pdf_path}")

    _validate_crop(top_pct, bottom_pct)

    parts: list[str] = []
    doc = pymupdf.open(pdf_path)
    try:
        for i, page in enumerate(doc, start=1):
            if top_pct != 0.0 or bottom_pct != 0.0:
                rect = page.rect
                h = rect.height
                clip = pymupdf.Rect(
                    rect.x0,
                    rect.y0 + h * top_pct,
                    rect.x1,
                    rect.y1 - h * bottom_pct,
                )
                page_text = page.get_text("text", clip=clip)
            else:
                page_text = page.get_text("text")

            if add_page_markers:
                parts.append(f"\n\n=== PAGE {i} ===\n")

            parts.append(page_text)
    finally:
        doc.close()

    return page_sep.join(parts)


def pdf_to_raw_text(cfg: PdfToTextConfig) -> Path:
    if cfg.out.exists() and not cfg.overwrite:
        raise FileExistsError(f"Target exists (use --overwrite): {cfg.out}")

    if cfg.create_parents:
        cfg.out.parent.mkdir(parents=True, exist_ok=True)

    text = extract_text_from_pdf(
        cfg.src,
        page_sep=cfg.join_with,
        top_pct=cfg.top_pct,
        bottom_pct=cfg.bottom_pct,
        add_page_markers=cfg.add_page_markers,
    )
    write_text(cfg.out, text, encoding=cfg.encoding, newline=cfg.newline)
    return cfg.out


def iter_pdfs(src_dir: Path, pattern: str) -> Iterable[Path]:
    # Non-recursive by default; easy to change to rglob if you want.
    yield from sorted(src_dir.glob(pattern))


def make_out_path(pdf_path: Path, out_dir: Path, suffix: str) -> Path:
    return out_dir / f"{pdf_path.stem}{suffix}"


def run_cli(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Extract raw text from PDF(s) into .txt files (optional crop).")
    mode = p.add_mutually_exclusive_group(required=True)

    # Single-file mode
    mode.add_argument("--src", type=Path, help="Path to a single PDF file.")

    # Batch mode
    mode.add_argument("--src-dir", type=Path, help="Directory containing PDFs to process.")

    p.add_argument("--out", type=Path, help="Output text file (single-file mode).")
    p.add_argument("--out-dir", type=Path, help="Output directory (batch mode).")
    p.add_argument("--pattern", default="*.pdf", help='Batch mode glob pattern (default: "*.pdf").')
    p.add_argument("--suffix", default=".txt", help='Batch mode output suffix (default: ".txt").')

    p.add_argument(
        "--join-with",
        default="",
        help='String inserted between pages (default: ""). Try "\\n\\n" for readability.',
    )
    p.add_argument("--newline", default="\n", help='Output newline normalization (default: "\\n").')
    p.add_argument("--encoding", default="utf-8", help="Output encoding (default: utf-8).")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs.")
    p.add_argument("--no-create-parents", action="store_true", help="Do not create output directories.")

    # Cropping options
    p.add_argument(
        "--top",
        type=float,
        default=0.1,
        help="Top margin to ignore, as fraction of page height (default: 0.0; e.g. 0.10 = 10%).",
    )
    p.add_argument(
        "--bottom",
        type=float,
        default=0.1,
        help="Bottom margin to ignore, as fraction of page height (default: 0.0; e.g. 0.10 = 10%).",
    )

    # Optional debug markers
    p.add_argument(
        "--page-markers",
        action="store_true",
        help='Add page markers like "=== PAGE i ===" to the output (off by default).',
    )

    args = p.parse_args(argv)

    create_parents = not args.no_create_parents

    # Single-file mode
    if args.src is not None:
        if args.out is None:
            p.error("--out is required when using --src")

        cfg = PdfToTextConfig(
            src=args.src,
            out=args.out,
            join_with=args.join_with,
            newline=args.newline,
            encoding=args.encoding,
            overwrite=args.overwrite,
            create_parents=create_parents,
            top_pct=args.top,
            bottom_pct=args.bottom,
            add_page_markers=args.page_markers,
        )
        out_path = pdf_to_raw_text(cfg)
        print(f"Wrote: {out_path}")
        return 0

    # Batch mode
    if args.src_dir is not None:
        if args.out_dir is None:
            p.error("--out-dir is required when using --src-dir")

        args.out_dir.mkdir(parents=True, exist_ok=True) if create_parents else None

        pdfs = list(iter_pdfs(args.src_dir, args.pattern))
        if not pdfs:
            print(f"No PDFs found in {args.src_dir} with pattern {args.pattern}", file=sys.stderr)
            return 2

        ok = 0
        for pdf in pdfs:
            out_path = make_out_path(pdf, args.out_dir, args.suffix)
            cfg = PdfToTextConfig(
                src=pdf,
                out=out_path,
                join_with=args.join_with,
                newline=args.newline,
                encoding=args.encoding,
                overwrite=args.overwrite,
                create_parents=create_parents,
                top_pct=args.top,
                bottom_pct=args.bottom,
                add_page_markers=args.page_markers,
            )
            try:
                pdf_to_raw_text(cfg)
                print(f"Wrote: {out_path}")
                ok += 1
            except Exception as e:
                print(f"FAILED: {pdf} -> {out_path} ({e})", file=sys.stderr)

        return 0 if ok == len(pdfs) else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(run_cli())
