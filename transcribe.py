import os
import re
import sys
import csv
import json
import time
import random
import subprocess
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

OUTPUT_DIR      = os.path.dirname(os.path.abspath(__file__))
TRANSCRIPTS_DIR = os.path.join(OUTPUT_DIR, 'generated_transcript_combined_texts')
METADATA_DIR    = os.path.join(OUTPUT_DIR, 'generated_transcript_metadata_tables')
YTDLP           = 'yt-dlp'

os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)
os.makedirs(METADATA_DIR,    exist_ok=True)


def clean_filename(title):
    title = re.sub(r'[^\w\s-]', '', title)
    return re.sub(r'[-\s]+', '_', title).strip().lower()


def fetch_playlist_entries(url):
    result = subprocess.run(
        [YTDLP, "--flat-playlist", "--print", "%(id)s|||%(url)s|||%(title)s", url],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("yt-dlp error:", result.stderr)
        input("\nPress Enter to exit...")
        sys.exit(1)
    entries = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("|||", 2)
        if len(parts) == 3:
            video_id, video_url, title = parts
            entries.append((video_id.strip(), video_url.strip(), title.strip()))
    return entries


def get_transcript(video_id):
    try:
        ytt = YouTubeTranscriptApi()
        data = ytt.fetch(video_id)
        normalized = [{'text': e.text, 'start': round(e.start, 2), 'duration': round(e.duration, 2)} for e in data]
        return normalized, 'auto-generated'
    except TranscriptsDisabled:
        return None, 'transcripts disabled'
    except NoTranscriptFound:
        return None, 'no transcript found'
    except Exception as e:
        msg = str(e)
        if 'private' in msg.lower():
            return None, 'private video'
        if '429' in msg or 'too many requests' in msg.lower():
            return None, f'rate limited: {msg[:200]}'
        return None, f'error: {type(e).__name__}: {msg[:200]}'


def save_transcript(data, filename, title, video_url, source):
    combined_text = " ".join(entry['text'] for entry in data)
    combined_text = re.sub(r'\s+', ' ', combined_text).strip()

    txt_path = os.path.join(TRANSCRIPTS_DIR, f'{filename}.txt')
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(f"Title: {title}\n")
        f.write(f"URL: {video_url}\n")
        f.write(f"Source: {source}\n")
        f.write("=" * 60 + "\n\n")
        f.write(combined_text)

    with open(os.path.join(METADATA_DIR, f'{filename}.csv'), 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['text', 'start', 'duration'])
        writer.writeheader()
        writer.writerows(data)

    with open(os.path.join(METADATA_DIR, f'{filename}.json'), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)


def process_url(url):
    print("\nFetching video list...")
    entries = fetch_playlist_entries(url)

    if len(entries) == 1:
        print(f"Detected: Single video — {entries[0][2]}\n")
    else:
        print(f"Detected: Playlist with {len(entries)} videos\n")

    success, skipped = 0, 0

    for i, (video_id, video_url, title) in enumerate(entries, 1):
        print(f"[{i}/{len(entries)}] {title[:70]}", end="  ", flush=True)

        filename = clean_filename(title) or video_id
        txt_path = os.path.join(TRANSCRIPTS_DIR, f'{filename}.txt')

        if os.path.exists(txt_path):
            print("✓ already exists, skipping")
            success += 1
            continue

        data, source = get_transcript(video_id)

        if data:
            save_transcript(data, filename, title, video_url, source)
            print(f"✓ saved ({source})")
            success += 1
        elif source == 'rate limited':
            print(f"✗ rate limited — waiting 5s then retrying...")
            time.sleep(5)
            data, source = get_transcript(video_id)
            if data:
                save_transcript(data, filename, title, video_url, source)
                print(f"  ↳ ✓ saved ({source})")
                success += 1
            else:
                print(f"  ↳ ✗ skipped — {source}")
                skipped += 1
        else:
            print(f"✗ skipped — {source}")
            skipped += 1

        time.sleep(random.uniform(2, 4))

    return success, skipped


def main():
    print("=" * 60)
    print("  YouTube Playlist / Video Transcriber")
    print("  (uses YouTube's built-in transcripts — very fast)")
    print("=" * 60)
    print()
    url = input("Paste your YouTube video or playlist URL here:\n> ").strip()

    if not url:
        print("No URL entered. Exiting.")
        input("\nPress Enter to exit...")
        sys.exit(1)

    if "youtube.com" not in url and "youtu.be" not in url:
        print("That doesn't look like a YouTube URL. Please try again.")
        input("\nPress Enter to exit...")
        sys.exit(1)

    print()
    start = time.time()
    success, skipped = process_url(url)
    elapsed = round(time.time() - start, 1)

    print("\n" + "=" * 60)
    print(f"Done in {elapsed}s — {success} transcribed, {skipped} skipped.")
    print(f"\nTranscripts saved to:")
    print(f"  {TRANSCRIPTS_DIR}")
    print("=" * 60)
    input("\nPress Enter to exit...")


if __name__ == '__main__':
    main()
