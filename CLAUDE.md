# YouTube Playlist Transcriber

A tool for bulk-downloading transcripts from YouTube videos and playlists. Three separate entry points exist, each with different capabilities and dependency requirements.

## Entry Points

### `app.py` — Flask Web App (primary)
- Run: `python app.py` → opens browser at `http://127.0.0.1:5000`
- Uses `yt-dlp` to fetch YouTube's built-in VTT captions (no audio download, very fast)
- Handles single videos and playlists
- Browser cookies option (Chrome/Firefox) for age-restricted videos
- Real-time progress via Server-Sent Events (`/stream` endpoint)
- Retry logic: up to 3 retries, delays of 15/30/60s; back-off doubles after 3 consecutive rate limits
- Random 2–5s delay between videos to avoid rate limiting
- Skips videos whose `.txt` output already exists
- Cancel button stops the background thread mid-job
- `/transcripts` lists saved files; `/download/<filename>` serves them

### `transcribe.py` — CLI Script
- Run: `python transcribe.py` → prompts for URL
- Uses `youtube_transcript_api` library (not `yt-dlp`) — faster but less control
- Simple one-retry on rate limit (5s wait), random 2–4s delay between videos
- Lighter dependencies

### `bulk_transcribe_youtube_videos_from_playlist.py` — Legacy Heavy Script
- **Not used by the web app.** Standalone script for when YouTube captions aren't available.
- Downloads actual audio via `pytubefix`, then transcribes with local Whisper (`faster_whisper`) or OpenAI Whisper API
- GPU support via CUDA; auto-detects and falls back to CPU
- Configuration is hardcoded at the top of the file (URLs, flags, API key)
- Heavy deps: `pytubefix`, `faster_whisper`, `numba`, `pydub`, `tqdm`, `openai`, `psutil`, optional `spacy`
- These deps are **not** in `requirements.txt`

## Output

All three scripts write to the same two directories (created automatically):

| Directory | Contents |
|---|---|
| `generated_transcript_combined_texts/` | `<filename>.txt` — header + plain combined transcript text |
| `generated_transcript_metadata_tables/` | `<filename>.csv` and `<filename>.json` — per-segment timing data |

**Text file format:**
```
Title: <video title>
URL: <video url>
Source: auto-generated
============================================================

<full transcript as one block of text>
```

**Segment metadata fields:** `text`, `start` (seconds), `duration` (seconds)

**Filename convention:** `clean_filename()` strips non-word chars, lowercases, replaces spaces/hyphens with `_`. Falls back to `video_id` if title cleans to empty.

## Dependencies

Install for `app.py` and `transcribe.py`:
```
pip install -r requirements.txt  # flask, pandas, yt-dlp
```

`app.py` looks for `yt-dlp.exe` in the project root first, then falls back to system `yt-dlp`.

## Standalone Tools

### `transcript_reader.html`
A standalone browser tool (open directly, not served by Flask) for cleaning and reading raw transcripts. Paste or upload a `.txt` file, uses `compromise.js` to split into sentences and format into paragraphs. Has a full-screen reader mode with adjustable font, size, and width.

## Build

`build.bat` + `YouTube Transcriber.spec` — PyInstaller config for packaging the web app as a Windows `.exe`.
