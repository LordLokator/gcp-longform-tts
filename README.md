# Google Cloud TTS – Hungarian textbook → long-form audio

A small project for creating an audiobook version of pdf files, using Python3 and the Google Cloud Text-to-Speech Long-form API.

Voice in use: `hu-HU-Chirp3-HD-Algenib`
Audio encoding: `LINEAR16` (WAV chunks), merged to MP3 with `ffmpeg`.

This repo is a small pipeline:

**PDF → raw text → cleaned text → Google Cloud TTS (chunked) → MP3 per chapter**

## Folder layout

- `data/source_documents/` – your input PDFs (intro + chapters)
- `data/raw_text/` – extracted raw text (`raw_<chapter>.txt`)
- `data/processed_text/` – cleaned text for TTS (`text_<chapter>.txt`)
- `data/audio_chunks/` – WAV chunks (`ch_<chapter>_partN.wav`)
- `data/output_audio/` – final MP3s (`ch_<chapter>.mp3`)

## Setup

### 1) Python deps

```bash
pip install -r requirements.txt
```

### 2) Authentication

This project uses Google Cloud Text-to-Speech Long-form API.

Authentication was tested using the Google Cloud CLI:

    gcloud auth application-default login

The pipeline relies on Application Default Credentials (ADC).


### 3) Config

Edit `config.json` to change voice / speaking rate / sample rate.

## Run

### End-to-end for one PDF

```bash
python main.py run --pdf data/source_documents/intro.pdf --chapter intro
python main.py merge --chapter intro
```

If a long run fails mid-way, restart from a later chunk:

```bash
python main.py run --pdf data/source_documents/ch_3.pdf --chapter 3 --offset 12
```

(Offset is **1-indexed**, i.e. `--offset 12` starts at chunk #12.)

### Safety flags (recommended)

- **Dry run**: show how the text will be chunked (count + indices), without calling Google TTS:

```bash
python main.py run --pdf data/source_documents/ch_3.pdf --chapter 3 --dry-run
# or
python main.py tts --src data/processed_text/text_3.txt --chapter 3 --dry-run
```

- **Skip existing chunks**: if `data/audio_chunks/ch_<chapter>_partN.wav` already exists, do not re-synthesize it:

```bash
python main.py run --pdf data/source_documents/ch_3.pdf --chapter 3 --skip-existing
python main.py tts --src data/processed_text/text_3.txt --chapter 3 --skip-existing
```

### Step-by-step (debugging friendly)

```bash
python main.py extract --src data/source_documents/ch_1.pdf --out data/raw_text/raw_1.txt
python main.py clean   --src data/raw_text/raw_1.txt         --out data/processed_text/text_1.txt
python main.py tts     --src data/processed_text/text_1.txt  --chapter 1
python main.py merge   --chapter 1
```

## What’s done vs. not done (current state)

### Done
- PDF → raw text extraction (`pdf_extractor.py`)
- Raw text → TTS-friendly cleanup (`text_clean.py`)
  - removes obvious page-number lines
  - merges soft line-wrapped newlines
  - inserts paragraph breaks between sentences (without breaking on section numbers like `2.1.`)
- Chunked Google Cloud TTS calls (`google_cloud_utils.py`)
  - chunks by **UTF-8 bytes** (safer than Python char counts)
  - supports `--offset` resume
- WAV chunk → MP3 merge (`sound_postprocess.py`, requires `ffmpeg`)
- A single CLI entry point (`main.py`) for the whole pipeline

### Not done yet (likely next)
- “Pretty headings” pass:
  - convert FULL-CAPS headings to Proper Case
  - convert section numbers to spoken Hungarian (e.g. `2.2.1` → `kettő pont kettő pont egy`)
- Smarter removal of tables / multi-column artifacts (book-specific heuristics)
- Logging / run manifests (so you can reproduce exactly what was synthesized)

## Notes

- `config.py` uses `MAX_BYTES_PER_REQUEST = 4500` to stay under the API input limit with Hungarian UTF-8 characters.
- `ffmpeg` must be available in PATH for MP3 merging.
- Tested on Windows 11. Should work on any OS with Python 3.10+.
