import os
import uuid
import subprocess
import json
import re
import urllib.request
import time
import asyncio
import shutil
import threading
import httpx
from urllib.parse import quote
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DOWNLOAD_DIR = "downloads"
MEMORY_FILE = "series_memory.json"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ==============================
# Render Port Check + Keep Alive
# ==============================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# ==============================
# Memory System
# ==============================
def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "characters": {},
        "story_context": "",
        "timeline": [],
        "style_notes": ""
    }

def save_memory(data):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def create_job_folder():
    job_id = uuid.uuid4().hex[:8]
    job_dir = os.path.join(DOWNLOAD_DIR, f"job_{job_id}")
    os.makedirs(job_dir, exist_ok=True)
    return job_dir

# ==============================
# Myanmar Text Cleaner – ASCII/အထူးသင်္ကေတအမှားအားလုံးဖယ်
# ==============================
def clean_myanmar_text(text):
    # မြန်မာစာလုံးချင်းကြား နေရာလွတ်ဖယ်
    text = re.sub(r'([က-အဤဧဩဪ၎၏ဥူဧံ])\s+([က-အဤဧဩဪ၎၏ဥူဧံ])', r'\1\2', text)
    # နေရာလွတ်အများကြီးကို တစ်ခုထဲပြုပြင်
    text = re.sub(r'\s+', ' ', text)
    # ⚠️ အမှားပေးတဲ့ အထူးဇာတ်ကောင်အားလုံးဖယ် – u2011 ပါ အားလုံးပါဝင်တယ်
    text = re.sub(r'[\u200b-\u200f\u00ad\u2011-\u201f\u2028-\u202f\ufeff]', '', text)
    return text.strip()

# ==============================
# Edge TTS
# ==============================
async def create_myanmar_voice(text, output_path):
    command = [
        "edge-tts",
        "--voice", "my-MM-ThihaNeural",
        "--rate", "+10%",
        "--pitch", "0Hz",
        "--text", text,
        "--write-media", output_path,
    ]
    result = await asyncio.to_thread(
        subprocess.run, command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if result.returncode != 0:
        raise Exception("Edge TTS Error:\n" + result.stderr[-2000:])
    if not os.path.exists(output_path):
        raise Exception("Voice MP3 မထွက်လာပါ။")

# ==============================
# Gemini API – Direct Call
# ==============================
def create_recap_data(memory_data):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise Exception("GEMINI_API_KEY မတွေ့ပါ။")

    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-2.0-flash:generateContent?key={api_key}"

    prompt = f"""You are a professional Myanmar movie recap script writer.

Task Requirements:
1. Write a VERY LONG, detailed and exciting movie recap narration in natural spoken Myanmar.
2. Start with a strong hook within the first 3–5 seconds to grab attention.
3. Structure: Hook → Start → Conflict → Turn → Climax → End → Short thought/question.
4. Return the most exciting 3‑second scene start time in HH:MM:SS format.
5. Extract character names and update memory.

⚠️ STRICT TEXT RULES:
- NEVER put spaces inside a Myanmar word: "နတ်ဆိုး" NOT "နတ် ဆိုး"
- Use ONE space ONLY between separate words.
- Write short sentences, lively and fast‑paced style.

Respond ONLY with raw JSON:
{{
    "hook_time": "00:00:10",
    "characters": {{"hero": "မင်းသား"}},
    "myanmar_script": "ဇာတ်လမ်းက ဒီလိုစထားပါတယ်..."
}}

Previous Memory: {json.dumps(memory_data, ensure_ascii=False)}
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }

    for attempt in range(3):
        try:
            resp = httpx.post(url, json=payload, timeout=120)
            resp.raise_for_status()
            raw_response = resp.json()
            text_result = raw_response["candidates"][0]["content"]["parts"][0]["text"].strip()
            return json.loads(text_result)
        except Exception as e:
            if attempt < 2:
                time.sleep(5)
                continue
            raise Exception("Gemini Recap Error:\n" + str(e))

# ==============================
# Start Command
# ==============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 Pro Movie Recap Bot အဆင်သင့်ဖြစ်ပါပြီ!\n"
        "⚠️ ပထမဆုံးအကြိမ် အသုံးပြုခြင်းအတွက် တစ်မိနစ်ခန့်စောင့်ပေးပါ – ဆာဗာအသစ်နိုးနေပါတယ် 😊\n"
        "ဗီဒီယိုဖိုင် ပို့လိုက်ရုံနဲ့ အားလုံးအလုပ်လုပ်ပါလိမ့်မယ်။\n"
        "⚠️ ဖိုင်အရွယ်အစား 20MB အောက်သာ ပို့ပေးပါ။"
    )

# ==============================
# Main Video Processing – URL/ASCII အမှားအားလုံးပြင်ပြီးသား
# ==============================
async def video_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    job_dir = None
    try:
        job_dir = create_job_folder()
        status_msg = await message.reply_text("📥 ဗီဒီယိုဖိုင် အချက်အလက်များကို ရယူနေပါတယ်...")

        video = message.video or message.document
        if not video:
            await message.reply_text("❌ ကျေးဇူးပြု၍ ဗီဒီယိုဖိုင် ပို့ပေးပါ။")
            return

        # ✅ ဖိုင်အရွယ်အစား ကန့်သတ်ချက် စစ်ဆေး – 20MB အောက်သာ
        file_size = video.file_size or 0
        if file_size > 20 * 1024 * 1024:
            await status_msg.edit_text("❌ ဖိုင်အရွယ်အစား ကြီးလွန်းနေပါတယ်!\n⚠️ 20MB အောက်သာ ပို့ပေးပါ။ နောက်မှ 2GB အထိ ဖွင့်ပေးနိုင်ပါတယ်။")
            return

        file_id = video.file_id
        file_info = await context.bot.get_file(file_id)
        file_url = file_info.file_path
        video_path = os.path.join(job_dir, f"video_{message.message_id}.mp4")

        await status_msg.edit_text("📥 ဗီဒီယိုဖိုင် ဒေါင်းလုဒ်ဆွဲနေပါသည်... ခဏစောင့်ပါ ⏳")

        # ✅ URL ကို လုံခြုံအောင် ပြုပြင် – ASCII/အထူးသင်္ကေတအမှားမရှိတော့ဘူး
        def download_file():
            safe_url = quote(file_url, safe='/:?=&%')
            req = urllib.request.Request(safe_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=600) as resp:
                with open(video_path, "wb") as f:
                    while chunk := resp.read(1024 * 1024):
                        f.write(chunk)

        await asyncio.to_thread(download_file)

        await status_msg.edit_text("🧠 ဇာတ်လမ်းအကျဉ်းနှင့် Hook ကို ဖန်တီးနေပါတယ်...")

        memory_data = load_memory()
        recap_data = create_recap_data(memory_data)

        hook_time = recap_data.get("hook_time", "00:00:10")
        raw_script = recap_data.get("myanmar_script", "ဇာတ်လမ်းအကျဉ်း...")

        if "characters" in recap_data:
            memory_data["characters"].update(recap_data["characters"])
            save_memory(memory_data)

        clean_script_text = clean_myanmar_text(raw_script)

        await status_msg.edit_text(f"🎙️ အသံဖန်တီးနေပါတယ်... (Hook အချိန်: {hook_time})")
        voice_path = os.path.join(job_dir, f"voice_{message.message_id}.mp3")
        await create_myanmar_voice(clean_script_text, voice_path)

        await status_msg.edit_text("🛡️ ဗီဒီယိုပုံစံ ပြုပြင်နေပါတယ်...")

        main_synced_path = os.path.join(job_dir, "main_synced.mp4")
        complex_filter = (
            "[0:v]hflip,"
            "scale=iw*0.985:ih*0.985,"
            "pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
            "eq=contrast=1.07:saturation=1.12:brightness=0.01,"
            "gblur=sigma=0.15,"
            "noise=alls=2:allt=t,"
            "drawbox=y=ih-120:color=black@1.0:width=iw:height=120:t=fill,"
            "tpad=stop_mode=clone:stop_duration=999[v_out]"
        )

        main_process_cmd = [
            "ffmpeg", "-y", "-i", video_path, "-i", voice_path,
            "-filter_complex", complex_filter,
            "-map", "[v_out]", "-map", "1:a",
            "-map_metadata", "-1", "-bitexact",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            "-af", "loudnorm=I=-14:LRA=7:TP=-2,atempo=1.05",
            "-shortest",
            main_synced_path
        ]
        await asyncio.to_thread(subprocess.run, main_process_cmd, stdout=subprocess.DEVNULL)

        await status_msg.edit_text("🔥 ၃ စက္ကန့် Hook ဖြတ်ထုတ်နေပါတယ်...")

        hook_path = os.path.join(job_dir, "hook.mp4")
        hook_filter = "[0:v]hflip,scale=iw*0.985:ih*0.985,pad=1280:720:(ow-iw)/2:(oh-ih)/2,eq=contrast=1.07:saturation=1.12:brightness=0.01,gblur=sigma=0.15,noise=alls=2:allt=t,drawbox=y=ih-120:color=black@1.0:width=iw:height=120:t=fill[v_out]"
        hook_cmd = [
            "ffmpeg", "-y", "-ss", hook_time, "-i", video_path, "-t", "3",
            "-filter_complex", hook_filter,
            "-map", "[v_out]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            hook_path
        ]
        await asyncio.to_thread(subprocess.run, hook_cmd, stdout=subprocess.DEVNULL)

        await status_msg.edit_text("🔗 Hook + ဇာတ်လမ်း ပေါင်းစပ်နေပါတယ်...")

        final_path = os.path.join(job_dir, f"final_recap_{message.message_id}.mp4")
        merge_cmd = [
            "ffmpeg", "-y",
            "-i", hook_path,
            "-i", main_synced_path,
            "-filter_complex",
            "[0:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30[v0];"
            "[0:a]aresample=44100,aformat=channel_layouts=stereo[a0];"
            "[1:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30[v1];"
            "[1:a]aresample=44100,aformat=channel_layouts=stereo[a1];"
            "[v0][a0][v1][a1]concat=n=2:v=1:a=1[outv][outa]",
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac",
            final_path
        ]

        result = await asyncio.to_thread(subprocess.run, merge_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"FFmpeg Error:\n{result.stderr[-500:]}")
        if not os.path.exists(final_path):
            raise Exception("ဖိုင်ဖန်တီးမှု မအောင်မြင်ပါ။")

        await status_msg.edit_text("🎬 ပြီးပါပြီ! ဗီဒီယို ပို့နေပါသည်...")
        with open(final_path, "rb") as video_file:
            await message.reply_document(
                document=video_file,
                filename=os.path.basename(final_path),
                caption="🎬 <b>Pro Myanmar Movie Recap</b>\n✨ Auto Hook\n🛡️ Copyright‑Safe Processing",
                parse_mode="HTML",
                read_timeout=1200,
                write_timeout=1200
            )

    except Exception as e:
        print("BOT ERROR:", repr(e))
        await message.reply_text("❌ အမှားဖြစ်သွားပါတယ်!\n\n" + str(e))
    finally:
        if job_dir and os.path.exists(job_dir):
            shutil.rmtree(job_dir)

# ==============================
# Main – Event Loop + ASCII + အချိန်ပြဿနာအားလုံးပြင်ပြီးသား
# ==============================
def main():
    if not TOKEN:
        print("❌ BOT_TOKEN မတွေ့ပါ။")
        return

    threading.Thread(target=run_health_server, daemon=True).start()
    print("🌐 Health Check Server Running...")

    api_base_url = "https://api.telegram.org/bot"

    request = HTTPXRequest(
        connect_timeout=300,
        read_timeout=1800,
        write_timeout=1800,
        pool_timeout=300,
        http_version="1.1"
    )

    app = (
        Application.builder()
        .token(TOKEN)
        .base_url(api_base_url)
        .request(request)
        .get_updates_request(request)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.Document.VIDEO | filters.Document.ALL,
        video_received
    ))

    print("🤖 Bot အဆင်သင့်ဖြစ်ပြီ – မက်ဆေ့ချ်စောင့်နေပါတယ်...")

    try:
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "document", "video"],
            close_loop=False
        )
    except Exception as e:
        print(f"⚠️ Polling ရပ်သွားပြီ: {e}")

if __name__ == "__main__":
    main()
