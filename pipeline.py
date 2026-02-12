"""Legacy entry point (kept for convenience).

Prefer: python main.py tts --chapter intro --src data/processed_text/text_ch_intro.txt
"""

from __future__ import annotations

from config import DATAFOLDER, AUDIO_CHUNKS_DIR
from utils import read_raw_text
from google_cloud_utils import load_gcloud_config, synthesize_text


def main():

    input_file = DATAFOLDER / "input" / "text_ch_intro.txt"
    raw_text = read_raw_text(input_file)

    config = load_gcloud_config()
    output_stem = "ch_intro"

    synthesize_text(
        text=raw_text,
        config=config,
        output_stem=output_stem,
        out_dir=AUDIO_CHUNKS_DIR,
        offset=6
    )


if __name__ == "__main__":
    main()
