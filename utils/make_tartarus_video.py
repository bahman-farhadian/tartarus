# -*- coding: utf-8 -*-
"""
make_tartarus_video.py - generate a vocabulary-drill video from a Tartarus word
list, using macOS 'say' for audio and ffmpeg for the video.

For each word in the list, the output video shows the word and its meaning on
a dark background, with audio spoken several times in a row.

Requires ffmpeg/ffprobe (e.g. `brew install ffmpeg-full`) and macOS 'say'.
Standard library only - no pip install / virtualenv needed.

Run through the project Makefile, e.g.:
    make video opts="--user bahman --lang german_home --audio-lang german --number 20"
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

# Import the sibling CLI module when this utility runs from the project root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tartarus as ll

FONT_FILE = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
VIDEO_SIZE = "1280x720"
BACKGROUND_COLOR = "0x303030"
REPEAT_GAP_SECONDS = 1.0
WORD_GAP_SECONDS = 2.0


def escape_drawtext(text):
    """Escapes a string for safe use inside an ffmpeg drawtext filter."""
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "’")
    text = text.replace("%", "\\%")
    return text


def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result


def atempo_filter(speed):
    """Builds an ffmpeg atempo filter chain for an arbitrary speed factor.

    A single atempo filter only accepts 0.5-2.0, so factors outside that
    range are split across multiple chained atempo filters."""
    if speed <= 0:
        raise ValueError("speed must be > 0")
    factors = []
    remaining = speed
    while remaining < 0.5 or remaining > 2.0:
        step = 0.5 if remaining < 0.5 else 2.0
        factors.append(step)
        remaining /= step
    factors.append(remaining)
    return ",".join(f"atempo={f}" for f in factors)


def meaning_for(entry):
    definition = entry.get("definition")
    if isinstance(definition, list):
        return definition[0] if definition else ""
    if isinstance(definition, str):
        return definition
    return ""


def make_word_clip(entry, voice, repeats, speed, tmpdir, index):
    word = entry["word"]
    meaning = meaning_for(entry)

    raw_aiff = os.path.join(tmpdir, f"{index}_raw.aiff")
    say_cmd = ["say", "-o", raw_aiff]
    if voice:
        say_cmd += ["-v", voice]
    say_cmd.append(word)
    run(say_cmd)

    raw_wav = os.path.join(tmpdir, f"{index}_raw.wav")
    run([
        "ffmpeg", "-y", "-i", raw_aiff,
        "-filter:a", atempo_filter(speed),
        "-ar", "44100", "-ac", "1", raw_wav,
    ])

    silence_wav = os.path.join(tmpdir, f"{index}_silence.wav")
    run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100",
        "-t", str(REPEAT_GAP_SECONDS), "-c:a", "pcm_s16le", silence_wav,
    ])

    # Repeat the word's audio `repeats` times, with a 1-second hold between.
    concat_list = os.path.join(tmpdir, f"{index}_concat.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for i in range(repeats):
            f.write(f"file '{raw_wav}'\n")
            if i != repeats - 1:
                f.write(f"file '{silence_wav}'\n")

    repeated_wav = os.path.join(tmpdir, f"{index}_repeated.wav")
    run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", concat_list,
        "-c:a", "pcm_s16le", repeated_wav,
    ])

    duration = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", repeated_wav,
    ]).stdout.strip()

    word_text = escape_drawtext(word)
    meaning_text = escape_drawtext(meaning)
    drawtext = (
        f"drawtext=fontfile={FONT_FILE}:text='{word_text}':fontcolor=white:"
        f"fontsize=72:x=(w-text_w)/2:y=(h/2)-60"
    )
    if meaning_text:
        drawtext += (
            f",drawtext=fontfile={FONT_FILE}:text='{meaning_text}':fontcolor=white:"
            f"fontsize=40:x=(w-text_w)/2:y=(h/2)+40"
        )

    clip_path = os.path.join(tmpdir, f"{index}_clip.mp4")
    run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={BACKGROUND_COLOR}:s={VIDEO_SIZE}:d={duration}",
        "-i", repeated_wav,
        "-vf", drawtext,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest", clip_path,
    ])
    return clip_path


def make_blank_clip(duration, tmpdir, index):
    """A clip showing only the background (no text), used between words."""
    clip_path = os.path.join(tmpdir, f"{index}_blank.mp4")
    run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={BACKGROUND_COLOR}:s={VIDEO_SIZE}:d={duration}",
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=mono:sample_rate=44100",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-t", str(duration), clip_path,
    ])
    return clip_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, help="Username (word list owner).")
    parser.add_argument("--lang", required=True, help="Word list name (e.g. german_home).")
    parser.add_argument("--audio-lang",
        help="Override the language used for voice selection (e.g. 'german' when --lang is 'german_home').")
    parser.add_argument("--word-list", help="Path to the word list JSON (default: data/word_lists/<user>_<lang>.json).")
    parser.add_argument("--output", default=None, help="Output video path (default: videos/<user>_<lang>.mp4).")
    parser.add_argument("--number", type=int, help="Only include the first N words.")
    parser.add_argument("--repeats", type=int, default=4, help="Times to say each word (default: 4).")
    parser.add_argument("--speed", type=float, default=1.0, help="Audio speed, e.g. 0.8 for slower (default: 1.0).")
    args = parser.parse_args()

    user = ll.sanitize_name(args.user, "user")
    lang = ll.sanitize_name(args.lang, "lang")
    audio_lang = ll.sanitize_name(args.audio_lang, "audio_lang") if args.audio_lang else None
    word_list_path = args.word_list or ll.word_list_path(user, lang)

    videos_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videos")
    os.makedirs(videos_dir, exist_ok=True)
    output_path = args.output or os.path.join(videos_dir, f"{user}_{lang}.mp4")

    if not os.path.exists(word_list_path):
        print(f"Error: word list not found: {word_list_path}", file=sys.stderr)
        sys.exit(1)

    with open(word_list_path, "r", encoding="utf-8") as f:
        entries = json.load(f)

    if args.number:
        entries = entries[:args.number]

    if not entries:
        print("Error: word list is empty.", file=sys.stderr)
        sys.exit(1)

    voice = ll.voice_for_language(audio_lang or lang)
    print(f"Voice: {voice or '(system default)'}")
    print(f"Words: {len(entries)}")

    with tempfile.TemporaryDirectory() as tmpdir:
        clip_paths = []
        for i, entry in enumerate(entries):
            print(f"  [{i + 1}/{len(entries)}] {entry['word']}")
            if i != 0:
                clip_paths.append(make_blank_clip(WORD_GAP_SECONDS, tmpdir, f"{i}_gap"))
            clip_paths.append(make_word_clip(entry, voice, args.repeats, args.speed, tmpdir, i))

        concat_list = os.path.join(tmpdir, "concat.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            for clip_path in clip_paths:
                f.write(f"file '{clip_path}'\n")

        run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c", "copy", output_path,
        ])

    print(f"Done: {output_path}")


if __name__ == "__main__":
    main()
