#!/usr/bin/env bash
# Auto Dub চালু করার স্ক্রিপ্ট
set -e

cd "$(dirname "$0")"

# .env থেকে API key লোড
if [ -f .env ]; then
  set -a; source .env; set +a
fi

if [ -z "$GEMINI_API_KEY" ]; then
  echo "⚠️  GEMINI_API_KEY নেই। .env ফাইলে যোগ করুন:"
  echo "    GEMINI_API_KEY=your_key_here"
  echo "    কী নিন: https://aistudio.google.com/apikey"
  exit 1
fi

command -v ffmpeg >/dev/null || { echo "❌ ffmpeg ইনস্টল করুন"; exit 1; }

if [ ! -d venv ]; then
  echo "📦 প্রথমবার সেটআপ হচ্ছে..."
  python3 -m venv venv
  ./venv/bin/pip install -q --upgrade pip
  ./venv/bin/pip install -q -r requirements.txt
fi

exec ./venv/bin/python app.py
