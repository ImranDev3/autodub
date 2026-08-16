# Auto Dub

Self-hosted automatic video dubbing. Upload a video — get it back dubbed in Bengali (or 9 other languages), with subtitles.

Like YouTube's auto-dubbing, but running on your own machine, with your own API key.

```
Upload  →  Transcribe  →  Translate  →  Synthesize  →  Time-fit  →  Dubbed MP4
```

---

## Features

- **Drop a file, walk away.** No editing, no manual timing, no cutting.
- **10 languages** — Bengali, English, Hindi, Urdu, Arabic, Spanish, French, Indonesian, Portuguese, Japanese.
- **Lip-sync aware.** Translations are constrained to fit the original timing, then micro-adjusted with tempo shifting.
- **Keeps the music.** Original audio is ducked underneath instead of discarded, so background score and sound effects survive.
- **Subtitles included.** A matching `.srt` is generated on every run.
- **One API key.** Transcription, translation and speech all run on Gemini — nothing else to sign up for.
- **Web UI and CLI.** Use whichever fits your workflow.

---

## Quick start

### 1. Get an API key (free)

Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey), sign in with a Google account, and click **Create API key**.

### 2. Install FFmpeg

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y ffmpeg

# macOS
brew install ffmpeg

# Windows — download from ffmpeg.org/download.html and add it to PATH
```

### 3. Run it

```bash
git clone https://github.com/ImranDev3/autodub.git
cd autodub

cp .env.example .env
nano .env               # paste your key into GEMINI_API_KEY

chmod +x run.sh
./run.sh
```

Open **http://localhost:8000**.

<details>
<summary>Windows (PowerShell)</summary>

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
$env:GEMINI_API_KEY = "your_key_here"
venv\Scripts\python app.py
```
</details>

---

## Command line

```bash
export GEMINI_API_KEY=your_key

python3 dubber.py lecture.mp4 -o dubbed.mp4        # dub to Bengali
python3 dubber.py lecture.mp4 -l hi -v Kore        # Hindi, female voice
python3 dubber.py lecture.mp4 --no-bg              # drop the original audio bed
```

| Flag | Meaning | Default |
|---|---|---|
| `-o, --out` | Output file path | `dubbed.mp4` |
| `-l, --lang` | Target language code | `bn` |
| `-v, --voice` | Gemini voice name | `Charon` |
| `--no-bg` | Discard original background audio | off |

---

## How it works

| # | Stage | Tool |
|---|---|---|
| 1 | Extract audio from video (16 kHz mono) | FFmpeg |
| 2 | Transcribe with timestamps **and** translate, in a single call | Gemini 2.5 Flash |
| 3 | Synthesize speech for each segment | Gemini TTS |
| 4 | Fit each segment into its original time slot | FFmpeg `atempo` |
| 5 | Assemble segments onto a timeline at their exact offsets | FFmpeg |
| 6 | Mux dubbed track over the ducked original, copy video stream | FFmpeg |

### Two design decisions worth knowing

**Timing is solved in two layers.** Spoken Bengali typically runs longer than the same sentence in English, which is what usually wrecks dubbing sync. The model is instructed to write translations that are *sayable within the original slot* — trimming filler while preserving meaning and tone. Whatever overflow remains is absorbed by tempo shifting, capped at 1.6× so speech never turns robotic.

**Transcription and translation share one API call.** Asking the model to listen and translate in a single pass — instead of transcribing, then sending the text back for translation — roughly halves both cost and latency.

The video stream is copied rather than re-encoded (`-c:v copy`), so output is fast and visually lossless.

---

## Cost

Roughly **$0.15–$0.30 per 10 minutes** of video on Gemini's paid tier.

Google AI Studio also has a **free tier** with a daily request allowance — enough to test with and often enough for light personal use, at no cost.

---

## Configuration

**`.env`**

```bash
GEMINI_API_KEY=...     # required
PORT=8000              # server port
MAX_UPLOAD_MB=500      # upload size limit
CONCURRENCY=2          # videos processed simultaneously
```

**`dubber.py`** — constants near the top:

| Setting | Purpose |
|---|---|
| `LANGUAGES` | Add or remove target languages |
| `VOICES` | Voice presets (Gemini offers 30 prebuilt voices) |
| `max_speedup=1.6` | Ceiling on tempo compression |
| `background_volume=0.12` | How loud the original audio sits under the dub |
| `TRANSCRIBE_PROMPT` | Translation instructions — edit this to tune terminology, register, or domain |

---

## Project layout

```
autodub/
├── dubber.py            core pipeline — also runnable as a CLI
├── app.py               Flask server, upload handling, job queue
├── templates/
│   ├── index.html       upload page with live progress
│   └── watch.html       playback page
├── test_pipeline.py     14 tests, no API key required
├── requirements.txt
├── run.sh               setup + launch in one command
└── .env.example
```

---

## Tests

The FFmpeg half of the pipeline is covered by tests that run offline, against a synthetic video:

```bash
python3 test_pipeline.py
```

```
✅ sample video created
✅ audio extracted (16 kHz mono)
✅ duration probed
✅ long audio fitted to slot
✅ short audio left untouched
✅ speedup ceiling respected
✅ timeline assembled
✅ muxed with background
✅ muxed without background
✅ output has both streams
✅ subtitles written
✅ JSON fence stripped
✅ web page loads
✅ unknown job returns 404
```

The Gemini-dependent stages need a real API key and are not covered.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `GEMINI_API_KEY is not set` | Check that `.env` exists and contains your key |
| `FFmpeg failed` | Run `ffmpeg -version` to confirm it's installed and on PATH |
| `HTTP 429` | Free-tier quota exhausted — wait, or enable billing |
| Dub drifts out of sync | Common with fast, densely-spoken source audio. Lower `max_speedup` and let segments overrun slightly, or split the video |
| Terminology mistranslated | Add domain instructions to `TRANSCRIBE_PROMPT` (e.g. "keep technical terms in English") |
| Jobs vanish after restart | Expected — jobs live in memory. See below |

---

## Roadmap

- **Voice cloning** — match the original speaker's voice via ElevenLabs (5–10× the cost)
- **Per-speaker voices** — the script already carries a `speaker` field; wire distinct voices to each
- **Proper source separation** — Demucs to fully isolate vocals from music before dubbing
- **Persistence** — jobs are currently in-memory and lost on restart; production use wants PostgreSQL + Celery/Redis

---

## A note on copyright

Dubbing and republishing someone else's video can infringe copyright. Use this on your own content, material you have permission for, licensed content, or for personal and educational purposes.

---

## License

MIT
