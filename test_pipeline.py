"""
FFmpeg অংশগুলো যাচাই করার টেস্ট (API key ছাড়াই চলে)।
চালান:  python3 test_pipeline.py
"""
import subprocess
import tempfile
import wave
from pathlib import Path

import dubber

work = Path(tempfile.mkdtemp(prefix="dubtest_"))
ok = fail = 0


def check(name, condition, detail=""):
    global ok, fail
    if condition:
        ok += 1
        print(f"  ✅ {name} {detail}")
    else:
        fail += 1
        print(f"  ❌ {name} {detail}")


print("\n=== টেস্ট শুরু ===\n")

# ---- একটা নকল ভিডিও বানাই (১২ সেকেন্ড, টোন সহ) ----
video = work / "sample.mp4"
subprocess.run([
    "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=12",
    "-f", "lavfi", "-i", "sine=frequency=300:duration=12",
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-shortest", str(video),
], capture_output=True, check=True)
check("নকল ভিডিও তৈরি", video.exists(),
      f"({video.stat().st_size // 1024} KB)")

# ---- ধাপ ১: অডিও এক্সট্র্যাকশন ----
wav = dubber.extract_audio(video, work / "audio.wav")
with wave.open(str(wav)) as wf:
    rate, chans = wf.getframerate(), wf.getnchannels()
check("অডিও এক্সট্র্যাক্ট (16kHz mono)", rate == 16000 and chans == 1,
      f"— {rate}Hz, {chans}ch")

dur = dubber.media_duration(video)
check("ভিডিওর দৈর্ঘ্য মাপা", 11.5 < dur < 12.5, f"— {dur:.2f}s")


# ---- ধাপ ৪: টাইম-ফিটিং ----
def make_tone(path, seconds):
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={seconds}",
        "-ar", str(dubber.TTS_SAMPLE_RATE), "-ac", "1", str(path),
    ], capture_output=True, check=True)
    return path


# ক) অডিও স্লটের চেয়ে লম্বা -> ছোট হওয়া উচিত
long_a = make_tone(work / "long.wav", 4.0)
fitted = dubber.fit_to_slot(long_a, work / "long_fit.wav", 3.0)
check("লম্বা অডিও স্লটে ফিট", 2.85 < fitted < 3.15,
      f"— 4.0s → {fitted:.2f}s (স্লট 3.0s)")

# খ) অডিও স্লটের চেয়ে ছোট -> অপরিবর্তিত থাকা উচিত
short_a = make_tone(work / "short.wav", 2.0)
kept = dubber.fit_to_slot(short_a, work / "short_fit.wav", 5.0)
check("ছোট অডিও অপরিবর্তিত", 1.9 < kept < 2.1,
      f"— 2.0s → {kept:.2f}s (স্লট 5.0s)")

# গ) খুব লম্বা -> ১.৬x এর বেশি দ্রুত হবে না
huge = make_tone(work / "huge.wav", 10.0)
capped = dubber.fit_to_slot(huge, work / "huge_fit.wav", 2.0)
check("সর্বোচ্চ স্পিড সীমা মানা", capped > 5.5,
      f"— 10.0s → {capped:.2f}s (1.6x সীমা কাজ করছে)")

# ---- ধাপ ৫: টাইমলাইন সাজানো ----
pieces = [
    {"path": make_tone(work / "p1.wav", 2.0), "start": 1.0},
    {"path": make_tone(work / "p2.wav", 2.0), "start": 5.0},
    {"path": make_tone(work / "p3.wav", 1.5), "start": 9.0},
]
timeline = dubber.build_timeline(pieces, dur, work / "timeline.wav")
tl_dur = dubber.media_duration(timeline)
check("টাইমলাইন তৈরি", 11.5 < tl_dur < 12.5, f"— {tl_dur:.2f}s")

# ---- ধাপ ৬: ভিডিওর সাথে জোড়া (ব্যাকগ্রাউন্ড সহ) ----
out1 = dubber.mux_final(video, timeline, work / "out_bg.mp4",
                        keep_background=True)
check("ভিডিও জোড়া (ব্যাকগ্রাউন্ড সহ)",
      Path(out1).exists() and Path(out1).stat().st_size > 5000,
      f"({Path(out1).stat().st_size // 1024} KB)")

# ---- জোড়া (ব্যাকগ্রাউন্ড ছাড়া) ----
out2 = dubber.mux_final(video, timeline, work / "out_nobg.mp4",
                        keep_background=False)
check("ভিডিও জোড়া (ব্যাকগ্রাউন্ড ছাড়া)", Path(out2).exists())

# আউটপুটে অডিও+ভিডিও দুইটাই আছে কিনা
probe = subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
     "-of", "csv=p=0", str(out1)], capture_output=True, text=True)
streams = probe.stdout.split()
check("আউটপুটে ভিডিও+অডিও দুইটাই", "video" in probe.stdout and "audio" in probe.stdout,
      f"— {', '.join(streams)}")

# ---- সাবটাইটেল ----
segs = [{"start": 1.0, "end": 3.0, "text": "প্রথম লাইন", "original": "first"},
        {"start": 5.0, "end": 7.5, "text": "দ্বিতীয় লাইন", "original": "second"}]
srt = dubber.write_srt(segs, work / "sub.srt")
content = Path(srt).read_text(encoding="utf-8")
check("সাবটাইটেল তৈরি",
      "00:00:01,000 --> 00:00:03,000" in content and "প্রথম লাইন" in content)

# ---- JSON ফেন্স পরিষ্কার করা ----
check("JSON fence পরিষ্কার",
      dubber._strip_fence('```json\n{"a":1}\n```') == '{"a":1}')

# ---- ওয়েব অ্যাপ লোড হয় কিনা ----
import app as webapp
client = webapp.app.test_client()
resp = client.get("/")
check("ওয়েব পেজ লোড", resp.status_code == 200 and b"Auto Dub" in resp.data,
      f"— HTTP {resp.status_code}")

resp = client.get("/status/nope")
check("অজানা জবে 404", resp.status_code == 404)

print(f"\n=== ফলাফল: {ok} পাস, {fail} ফেল ===\n")
print(f"টেস্ট ফাইল: {work}")
raise SystemExit(1 if fail else 0)
