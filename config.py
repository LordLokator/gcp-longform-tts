from pathlib import Path

ROOT = Path(__file__).parent
DATAFOLDER = ROOT / "data"

SOURCE_PDF_DIR = DATAFOLDER / "source_documents"
RAW_TEXT_DIR = DATAFOLDER / "raw_text"
PROCESSED_TEXT_DIR = DATAFOLDER / "processed_text"
AUDIO_CHUNKS_DIR = DATAFOLDER / "audio_chunks"
OUTPUT_AUDIO_DIR = DATAFOLDER / "output_audio"

MAX_BYTES_PER_REQUEST = 3000
