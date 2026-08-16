"""
Tests for the FFmpeg half of the pipeline. Runs offline, no API key needed.

Run:  python3 test_pipeline.py
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


print("\n=== running tests ===\n")

# ---- build a synthetic 12-second video with a tone ----
video = work / "sample.mp4"
subprocess.run([
    "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=12",
    "-f", "lavfi", "-i", "sine=frequency=300:duration=12",
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-shortest", str(video),
], capture_output=True, check=True)
check("sample video created", video.exists(),
      f"({video.stat().st_size // 1024} KB)")

# ---- step 1: audio extraction ----
wav = dubber.extract_audio(video, work / "audio.wav")
with wave.open(str(wav)) as wf:
    rate, chans = wf.getframerate(), wf.getnchannels()
check("audio extracted (16 kHz mono)", rate == 16000 and chans == 1,
      f"— {rate}Hz, {chans}ch")

dur = dubber.media_duration(video)
check("duration probed", 11.5 < dur < 12.5, f"— {dur:.2f}s")


# ---- step 4: time fitting ----
def make_tone(path, seconds):
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={seconds}",
        "-ar", str(dubber.TTS_SAMPLE_RATE), "-ac", "1", str(path),
    ], capture_output=True, check=True)
    return path


# a) audio longer than its slot -> should be compressed
long_a = make_tone(work / "long.wav", 4.0)
fitted = dubber.fit_to_slot(long_a, work / "long_fit.wav", 3.0)
check("long audio fitted to slot", 2.85 < fitted < 3.15,
      f"— 4.0s -> {fitted:.2f}s (slot 3.0s)")

# b) audio shorter than its slot -> should be left alone
short_a = make_tone(work / "short.wav", 2.0)
kept = dubber.fit_to_slot(short_a, work / "short_fit.wav", 5.0)
check("short audio left untouched", 1.9 < kept < 2.1,
      f"— 2.0s -> {kept:.2f}s (slot 5.0s)")

# c) far too long -> must not exceed the 1.6x speedup ceiling
huge = make_tone(work / "huge.wav", 10.0)
capped = dubber.fit_to_slot(huge, work / "huge_fit.wav", 2.0)
check("speedup ceiling respected", capped > 5.5,
      f"— 10.0s -> {capped:.2f}s (1.6x cap holding)")

# ---- step 5: timeline assembly ----
pieces = [
    {"path": make_tone(work / "p1.wav", 2.0), "start": 1.0},
    {"path": make_tone(work / "p2.wav", 2.0), "start": 5.0},
    {"path": make_tone(work / "p3.wav", 1.5), "start": 9.0},
]
timeline = dubber.build_timeline(pieces, dur, work / "timeline.wav")
tl_dur = dubber.media_duration(timeline)
check("timeline assembled", 11.5 < tl_dur < 12.5, f"— {tl_dur:.2f}s")

# ---- step 6: muxing ----
out1 = dubber.mux_final(video, timeline, work / "out_bg.mp4",
                        keep_background=True)
check("muxed with background",
      Path(out1).exists() and Path(out1).stat().st_size > 5000,
      f"({Path(out1).stat().st_size // 1024} KB)")

# ---- muxing without the original bed ----
out2 = dubber.mux_final(video, timeline, work / "out_nobg.mp4",
                        keep_background=False)
check("muxed without background", Path(out2).exists())

# does the output carry both streams?
probe = subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
     "-of", "csv=p=0", str(out1)], capture_output=True, text=True)
streams = probe.stdout.split()
check("output has both streams", "video" in probe.stdout and "audio" in probe.stdout,
      f"— {', '.join(streams)}")

# ---- subtitles ----
segs = [{"start": 1.0, "end": 3.0, "text": "first line", "original": "first"},
        {"start": 5.0, "end": 7.5, "text": "second line", "original": "second"}]
srt = dubber.write_srt(segs, work / "sub.srt")
content = Path(srt).read_text(encoding="utf-8")
check("subtitles written",
      "00:00:01,000 --> 00:00:03,000" in content and "first line" in content)

# ---- fence stripping ----
check("JSON fence stripped",
      dubber._strip_fence('```json\n{"a":1}\n```') == '{"a":1}')

# ---- does the web app come up? ----
import app as webapp
client = webapp.app.test_client()
resp = client.get("/")
check("web page loads", resp.status_code == 200 and b"Auto Dub" in resp.data,
      f"— HTTP {resp.status_code}")

resp = client.get("/status/nope")
check("unknown job returns 404", resp.status_code == 404)

print(f"\n=== result: {ok} passed, {fail} failed ===\n")
print(f"test artifacts: {work}")
raise SystemExit(1 if fail else 0)
