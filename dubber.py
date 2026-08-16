"""
Auto Dubbing Engine
-------------------
এক লাইনে: ভিডিও দিন -> বাংলা (বা অন্য ভাষায়) ডাব করা ভিডিও পান।

পাইপলাইন:
  1. FFmpeg দিয়ে ভিডিও থেকে অডিও বের করা
  2. Gemini দিয়ে টাইমস্ট্যাম্প সহ ট্রান্সক্রাইব + অনুবাদ (এক কলেই)
  3. Gemini TTS দিয়ে প্রতিটি সেগমেন্টের ভয়েস তৈরি
  4. প্রতিটি সেগমেন্ট তার নিজের সময়ের ভেতর ফিট করা (atempo)
  5. টাইমলাইন বানিয়ে আসল অডিওর উপর বসানো + ভিডিওর সাথে জোড়া

শুধু একটাই API key লাগে: GEMINI_API_KEY
"""

import base64
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from pathlib import Path

import requests

# ---------------------------------------------------------------- config

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
MODEL_UNDERSTAND = "gemini-2.5-flash"          # শোনা + অনুবাদ (সস্তা, দ্রুত)
MODEL_TTS = "gemini-2.5-flash-preview-tts"     # ভয়েস তৈরি

TTS_SAMPLE_RATE = 24000

# ভাষার তালিকা — চাইলে বাড়াতে পারেন
LANGUAGES = {
    "bn": "Bengali (Bangla)",
    "en": "English",
    "hi": "Hindi",
    "ur": "Urdu",
    "ar": "Arabic",
    "es": "Spanish",
    "fr": "French",
    "id": "Indonesian",
    "pt": "Portuguese",
    "ja": "Japanese",
}

# Gemini prebuilt ভয়েস
VOICES = {
    "male_warm": "Charon",
    "male_clear": "Orus",
    "female_warm": "Kore",
    "female_bright": "Leda",
    "neutral": "Puck",
}
DEFAULT_VOICE = "Charon"


class DubError(Exception):
    pass


def _api_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise DubError("GEMINI_API_KEY সেট করা নেই। .env ফাইলে যোগ করুন।")
    return key


def _run(cmd):
    """FFmpeg কমান্ড চালায়, সমস্যা হলে পরিষ্কার এরর দেয়।"""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-800:]
        raise DubError(f"FFmpeg ব্যর্থ: {' '.join(cmd[:4])}...\n{tail}")
    return proc


# ---------------------------------------------------------------- step 1

def extract_audio(video_path, out_wav):
    """ভিডিও থেকে 16kHz mono WAV — Gemini এর জন্য আদর্শ ফরম্যাট।"""
    _run([
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(out_wav),
    ])
    return out_wav


def media_duration(path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- step 2

TRANSCRIBE_PROMPT = """You are a professional dubbing script writer.

Listen to the attached audio and produce a dubbing script in {target_name}.

Rules:
- Split the speech into natural segments, each 2 to 12 seconds long.
- Give precise start and end times in seconds (decimals allowed).
- `original` = what was actually said, verbatim, in the source language.
- `text` = a natural, idiomatic {target_name} rendering meant to be SPOKEN aloud.
- Critically: `text` must be sayable comfortably within (end - start) seconds.
  Spoken {target_name} is often longer than the source, so tighten the phrasing —
  cut filler, keep meaning and tone. Never pad it out.
- Write numbers, dates and units as words, the way a narrator would say them.
- Keep proper nouns and brand names in their normal form.
- Skip pure music, silence, applause and background noise — no segment for those.
- If a speaker changes, start a new segment and set `speaker` to a stable label
  like "S1", "S2".

Return ONLY valid JSON, no markdown fence, in exactly this shape:
{{"segments":[{{"start":0.0,"end":4.2,"speaker":"S1","original":"...","text":"..."}}]}}
"""


def transcribe_and_translate(wav_path, target_lang="bn", log=print):
    """এক কলেই: শোনা -> টাইমস্ট্যাম্প -> অনুবাদ। খরচ কম, কারণ দুইটা ধাপ একসাথে।"""
    target_name = LANGUAGES.get(target_lang, target_lang)
    audio_b64 = base64.b64encode(Path(wav_path).read_bytes()).decode()

    log(f"অডিও পাঠানো হচ্ছে ({len(audio_b64) // 1400} KB)...")

    body = {
        "contents": [{
            "parts": [
                {"text": TRANSCRIBE_PROMPT.format(target_name=target_name)},
                {"inline_data": {"mime_type": "audio/wav", "data": audio_b64}},
            ]
        }],
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json",
        },
    }

    data = _post(f"{API_BASE}/models/{MODEL_UNDERSTAND}:generateContent", body,
                 timeout=600)
    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(_strip_fence(raw))
    segments = parsed.get("segments", [])

    # পরিষ্কার করা: খালি সেগমেন্ট বাদ, সময় অনুযায়ী সাজানো, ওভারল্যাপ ঠিক করা
    clean = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start = max(0.0, float(seg.get("start", 0)))
        end = float(seg.get("end", start + 2))
        if end <= start:
            end = start + 2.0
        clean.append({
            "start": start, "end": end, "text": text,
            "original": (seg.get("original") or "").strip(),
            "speaker": seg.get("speaker") or "S1",
        })

    clean.sort(key=lambda s: s["start"])
    for i in range(len(clean) - 1):
        if clean[i]["end"] > clean[i + 1]["start"]:
            clean[i]["end"] = clean[i + 1]["start"]

    if not clean:
        raise DubError("অডিওতে কোনো কথা পাওয়া যায়নি।")

    log(f"{len(clean)} টি সেগমেন্ট পাওয়া গেছে।")
    return clean


def _strip_fence(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()


def _post(url, body, timeout=180, retries=4):
    """রেট লিমিট আর সাময়িক এররে নিজে থেকে আবার চেষ্টা করে।"""
    headers = {"x-goog-api-key": _api_key(), "Content-Type": "application/json"}
    last = None
    for attempt in range(retries):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt * 3
                time.sleep(wait)
                last = f"HTTP {resp.status_code}: {resp.text[:300]}"
                continue
            raise DubError(f"API এরর {resp.status_code}: {resp.text[:400]}")
        except requests.RequestException as exc:
            last = str(exc)
            time.sleep(2 ** attempt * 2)
    raise DubError(f"API বারবার ব্যর্থ হয়েছে: {last}")


# ---------------------------------------------------------------- step 3

def synthesize_segment(text, voice, out_wav):
    """Gemini TTS -> raw PCM -> WAV ফাইল।"""
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice}
                }
            },
        },
    }
    data = _post(f"{API_BASE}/models/{MODEL_TTS}:generateContent", body, timeout=180)

    try:
        part = data["candidates"][0]["content"]["parts"][0]
        pcm = base64.b64decode(part["inlineData"]["data"])
    except (KeyError, IndexError) as exc:
        raise DubError(f"TTS থেকে অডিও আসেনি: {exc}")

    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(TTS_SAMPLE_RATE)
        wf.writeframes(pcm)
    return out_wav


# ---------------------------------------------------------------- step 4

def fit_to_slot(in_wav, out_wav, slot_seconds, max_speedup=1.6):
    """
    সেগমেন্টের অডিওকে তার নির্ধারিত সময়ের ভেতর ফিট করা।
    বেশি লম্বা হলে একটু দ্রুত করা হয় — কিন্তু ১.৬x এর বেশি নয়,
    নইলে শুনতে রোবটের মতো লাগে।
    """
    actual = media_duration(in_wav)
    if actual <= 0 or slot_seconds <= 0.3:
        shutil.copy(in_wav, out_wav)
        return media_duration(out_wav)

    ratio = actual / slot_seconds

    if ratio <= 1.02:
        # সময়ের ভেতরেই আছে — কিছু করার দরকার নেই
        shutil.copy(in_wav, out_wav)
        return actual

    tempo = min(ratio, max_speedup)

    # FFmpeg এর atempo একবারে 0.5-2.0 পর্যন্ত পারে, তাই দরকার হলে চেইন করি
    filters, remaining = [], tempo
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    filters.append(f"atempo={remaining:.4f}")

    _run(["ffmpeg", "-y", "-i", str(in_wav), "-filter:a", ",".join(filters),
          "-ar", str(TTS_SAMPLE_RATE), "-ac", "1", str(out_wav)])
    return media_duration(out_wav)


# ---------------------------------------------------------------- step 5

def build_timeline(pieces, total_seconds, out_wav):
    """
    প্রতিটি ডাব করা সেগমেন্টকে তার সঠিক সময়ে বসিয়ে একটা পূর্ণ ট্র্যাক বানায়।
    ফাঁকা জায়গাগুলো নীরবতা দিয়ে ভরাট হয়।
    """
    if not pieces:
        raise DubError("বসানোর মতো কোনো অডিও নেই।")

    inputs, filters, labels = [], [], []
    for idx, piece in enumerate(pieces):
        inputs += ["-i", str(piece["path"])]
        delay_ms = int(round(piece["start"] * 1000))
        filters.append(f"[{idx}:a]adelay={delay_ms}|{delay_ms},"
                       f"aresample={TTS_SAMPLE_RATE}[d{idx}]")
        labels.append(f"[d{idx}]")

    graph = (";".join(filters) + ";" + "".join(labels)
             + f"amix=inputs={len(pieces)}:normalize=0:dropout_transition=0[mixed];"
             + "[mixed]alimiter=limit=0.95,apad[out]")

    # দৈর্ঘ্য `-t` দিয়ে কাটা হয় (ফিল্টারের ভেতর atrim দিলে ffmpeg আটকে যেতে পারে)
    _run(["ffmpeg", "-y", *inputs, "-filter_complex", graph,
          "-map", "[out]", "-t", f"{max(total_seconds, 0.5):.3f}",
          "-ar", str(TTS_SAMPLE_RATE), "-ac", "2", str(out_wav)])
    return out_wav


def mux_final(video_path, dub_wav, out_video, keep_background=True,
              background_volume=0.12):
    """
    ডাব ট্র্যাক ভিডিওর সাথে জোড়া।
    keep_background: আসল অডিও খুব নিচু ভলিউমে রাখে যাতে মিউজিক/সাউন্ড হারিয়ে না যায়।
    ভিডিও রি-এনকোড হয় না (-c:v copy) — তাই দ্রুত আর কোয়ালিটি অক্ষত।
    """
    if keep_background:
        graph = (f"[0:a]volume={background_volume}[bg];"
                 "[1:a]volume=1.0[dub];"
                 "[bg][dub]amix=inputs=2:duration=first:normalize=0[out]")
        cmd = ["ffmpeg", "-y", "-i", str(video_path), "-i", str(dub_wav),
               "-filter_complex", graph, "-map", "0:v", "-map", "[out]"]
    else:
        cmd = ["ffmpeg", "-y", "-i", str(video_path), "-i", str(dub_wav),
               "-map", "0:v", "-map", "1:a"]

    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(out_video)]

    try:
        _run(cmd)
    except DubError:
        # কিছু ফরম্যাটে stream copy কাজ করে না — তখন রি-এনকোড
        cmd[cmd.index("copy")] = "libx264"
        cmd.insert(-1, "-preset")
        cmd.insert(-1, "veryfast")
        _run(cmd)
    return out_video


def write_srt(segments, path):
    """বোনাস: ডাব করা টেক্সটের সাবটাইটেল ফাইল।"""
    def stamp(sec):
        ms = int(round(sec * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(f"{i}\n{stamp(seg['start'])} --> {stamp(seg['end'])}\n"
                     f"{seg['text']}\n")
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------- main

def dub_video(video_path, out_video, target_lang="bn", voice=DEFAULT_VOICE,
              keep_background=True, progress=None):
    """
    পুরো পাইপলাইন। progress(percent, message) কলব্যাক দিয়ে অগ্রগতি জানায়।
    """
    def report(pct, msg):
        if progress:
            progress(pct, msg)

    video_path = Path(video_path)
    work = Path(tempfile.mkdtemp(prefix="dub_"))

    try:
        report(5, "ভিডিও থেকে অডিও আলাদা করা হচ্ছে...")
        source_wav = extract_audio(video_path, work / "source.wav")
        total = media_duration(video_path)

        report(15, "কথা শোনা ও অনুবাদ করা হচ্ছে...")
        segments = transcribe_and_translate(source_wav, target_lang,
                                            log=lambda m: report(20, m))

        report(30, f"{len(segments)} টি সেগমেন্টের ভয়েস তৈরি হচ্ছে...")
        pieces = []
        for idx, seg in enumerate(segments):
            raw = work / f"raw_{idx:04d}.wav"
            fitted = work / f"fit_{idx:04d}.wav"

            try:
                synthesize_segment(seg["text"], voice, raw)
            except DubError:
                continue  # একটা সেগমেন্ট ব্যর্থ হলে পুরো কাজ থামে না

            slot = seg["end"] - seg["start"]
            fit_to_slot(raw, fitted, slot)
            pieces.append({"path": fitted, "start": seg["start"]})

            pct = 30 + int(50 * (idx + 1) / len(segments))
            report(pct, f"ভয়েস তৈরি: {idx + 1}/{len(segments)}")

        if not pieces:
            raise DubError("কোনো ভয়েস তৈরি করা যায়নি।")

        report(85, "অডিও ট্র্যাক সাজানো হচ্ছে...")
        dub_wav = build_timeline(pieces, total, work / "dub.wav")

        report(92, "ভিডিওর সাথে জোড়া হচ্ছে...")
        mux_final(video_path, dub_wav, out_video, keep_background)

        srt_path = Path(out_video).with_suffix(".srt")
        write_srt(segments, srt_path)

        report(100, "সম্পন্ন!")
        return {"video": str(out_video), "srt": str(srt_path),
                "segments": segments, "duration": total}

    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="ভিডিও অটো-ডাবিং")
    ap.add_argument("video", help="ইনপুট ভিডিও ফাইল")
    ap.add_argument("-o", "--out", default="dubbed.mp4", help="আউটপুট ফাইল")
    ap.add_argument("-l", "--lang", default="bn", help="টার্গেট ভাষা (bn/en/hi...)")
    ap.add_argument("-v", "--voice", default=DEFAULT_VOICE, help="ভয়েসের নাম")
    ap.add_argument("--no-bg", action="store_true",
                    help="আসল ব্যাকগ্রাউন্ড অডিও বাদ দিন")
    args = ap.parse_args()

    result = dub_video(args.video, args.out, args.lang, args.voice,
                       keep_background=not args.no_bg,
                       progress=lambda p, m: print(f"[{p:3d}%] {m}", flush=True))
    print(f"\n✅ তৈরি: {result['video']}")
    print(f"📝 সাবটাইটেল: {result['srt']}")
