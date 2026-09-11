import os
import uuid
import subprocess
import json
import urllib.parse
import urllib.request
import time
import asyncio
import shutil
import threading
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

WHISPER_BIN = os.path.expanduser("~/whisper.cpp/build/bin/whisper-cli")
WHISPER_MODEL = os.path.expanduser("~/whisper.cpp/models/ggml-tiny.bin")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ==============================
# Render Port Check အတွက် Web Server ငယ်
# ==============================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")
    def log_message(self, format, *args):
        pass # Log တွေ ရှုပ်မနေအောင် ပိတ်ထားသည်

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# ==============================
# ၁။ မှတ်ဉာဏ်စနစ် (Series Memory)
# ==============================
def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"characters": {}, "story_context": ""}

def save_memory(data):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def create_job_folder():
    job_id = uuid.uuid4().hex[:8]
    job_dir = os.path.join(DOWNLOAD_DIR, f"job_{job_id}")
    os.makedirs(job_dir, exist_ok=True)
    return job_dir

# ==============================
# ၂။ Edge TTS (Myanmar Voice)
# ==============================
async def create_myanmar_voice(text, output_path):
    command = [
        "edge-tts",
        "--voice", "my-MM-ThihaNeural",
        "--rate", "-5%",
        "--pitch", "-2Hz",
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
# ၃။ Gemini AI (Recap + Hook + Memory)
# ==============================
def create_recap_data(transcript, memory_data):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise Exception("GEMINI_API_KEY မတွေ့ပါ။")

    prompt = f"""You are a professional Myanmar movie recap script writer.
The user will provide a transcript with timestamps and past memory.

Task Requirements:
1. Understand the story perfectly.
2. Write a VERY LONG, detailed recap narration in spoken Myanmar. Make sure it is longer than the video by explaining actions thoroughly. Include all important dialogues as narration.
3. Identify the most exciting 3-second scene (The Hook) and return its start time in HH:MM:SS format.
4. Extract character names and update them for memory.

Respond ONLY with a raw JSON object (no markdown, no extra text).
Format:
{{
    "hook_time": "00:00:15",
    "characters": {{"hero": "ထန်ထျန်ဂျီ"}},
    "myanmar_script": "ဇာတ်လမ်းက ဒီလိုစထားပါတယ်..."
}}

Previous Memory: {json.dumps(memory_data, ensure_ascii=False)}
Transcript:
{transcript}
"""

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
    data = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json"
        }
    }

    for attempt in range(3):
        try:
            request = urllib.request.Request(
                url, data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                method="POST"
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))
            
            raw_response = result["candidates"][0]["content"]["parts"][0]["text"].strip()
            response_data = json.loads(raw_response)
            return response_data

        except Exception as e:
            if attempt < 2:
                time.sleep(5)
                continue
            raise Exception("Gemini Recap Error:\n" + str(e))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 Pro Movie Recap Bot အဆင်သင့်ဖြစ်ပါပြီ!\n"
        "Video ပို့လိုက်တာနဲ့ (Hook ထုတ်ခြင်း, SRT ပို့ခြင်း, Copyright ရှောင်ခြင်း အားလုံး) အလိုအလျောက် လုပ်ပေးပါမယ်။"
    )

async def video_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    try:
        job_dir = create_job_folder()
        status_msg = await message.reply_text("📥 Video ကို Download လုပ်နေပါတယ်...")

        video = message.video
        file = await context.bot.get_file(video.file_id)
        video_path = os.path.join(job_dir, f"video_{message.message_id}.mp4")
        await file.download_to_drive(video_path)

        audio_path = os.path.join(job_dir, f"audio_{message.message_id}.wav")
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", audio_path], stdout=subprocess.DEVNULL)

        await status_msg.edit_text("🤖 Whisper ဖြင့် စာသားနှင့် အချိန်အမှတ် (SRT) ဖမ်းနေပါတယ်...")

        text_base = os.path.join(job_dir, f"transcript_{message.message_id}")
        whisper_command = [
            WHISPER_BIN, "-m", WHISPER_MODEL, "-f", audio_path,
            "-l", "zh", "-t", "4", "-osrt", "-of", text_base
        ]
        await asyncio.to_thread(subprocess.run, whisper_command, stdout=subprocess.DEVNULL)
        
        srt_path = text_base + ".srt"
        if not os.path.exists(srt_path):
            raise Exception("Whisper SRT ဖိုင် မထွက်လာပါ။")

        with open(srt_path, "rb") as srt_file:
            await message.reply_document(document=srt_file, caption="📄 ထုတ်ယူရရှိသော SRT (အချိန်အမှတ်) ဖိုင်")

        with open(srt_path, "r", encoding="utf-8") as f:
            transcript = f.read().strip()

        await status_msg.edit_text("🧠 Gemini မှ ဇာတ်လမ်း၊ Hook နှင့် ဇာတ်ကောင်များကို စဉ်းစားတွက်ချက်နေပါတယ်...")

        memory_data = load_memory()
        recap_data = create_recap_data(transcript, memory_data)
        
        hook_time = recap_data.get("hook_time", "00:00:10")
        raw_script = recap_data.get("myanmar_script", "ဇာတ်လမ်းအကျဉ်း...")
        
        if "characters" in recap_data:
            memory_data["characters"].update(recap_data["characters"])
            save_memory(memory_data)

        clean_script = raw_script.replace(" ", "")

        await status_msg.edit_text(f"🎙️ AI Voice ဖန်တီးနေပါတယ်... (Hook Time: {hook_time})")

        voice_path = os.path.join(job_dir, f"voice_{message.message_id}.mp3")
        await create_myanmar_voice(clean_script, voice_path)

        await status_msg.edit_text("🛡️ Video ကို Copyright ရှောင်ရန်၊ တရုတ်စာတန်းဝါးရန်နှင့် ညှိနှိုင်းရန် ပြင်ဆင်နေပါတယ်...")

        main_synced_path = os.path.join(job_dir, "main_synced.mp4")
        complex_filter = (
            "[0:v]hflip,"
            "drawbox=y=ih-120:color=black@1.0:width=iw:height=120:t=fill,"
            "eq=contrast=1.05:saturation=1.1,"
            "hue=s='1+0.2*sin(t/3*PI)',"
            "tpad=stop_mode=clone:stop_duration=999[v_out]"
        )

        main_process_cmd = [
            "ffmpeg", "-y", "-i", video_path, "-i", voice_path,
            "-filter_complex", complex_filter,
            "-map", "[v_out]", "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            "-shortest",
            main_synced_path
        ]
        await asyncio.to_thread(subprocess.run, main_process_cmd, stdout=subprocess.DEVNULL)

        await status_msg.edit_text("🔥 ၃ စက္ကန့်စာ Hook ဖြတ်ထုတ်နေပါတယ်...")

        hook_path = os.path.join(job_dir, "hook.mp4")
        hook_filter = (
            "[0:v]hflip,"
            "drawbox=y=ih-120:color=black@1.0:width=iw:height=120:t=fill,"
            "eq=contrast=1.05:saturation=1.1,"
            "hue=s='1+0.2*sin(t/3*PI)'[v_out]"
        )
        hook_cmd = [
            "ffmpeg", "-y", "-ss", hook_time, "-i", video_path, "-t", "3",
            "-filter_complex", hook_filter,
            "-map", "[v_out]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            hook_path
        ]
        await asyncio.to_thread(subprocess.run, hook_cmd, stdout=subprocess.DEVNULL)

        await status_msg.edit_text("🔗 Hook နှင့် မူရင်းရုပ်ရှင် ပေါင်းစပ်နေပါတယ်...")

        concat_txt = os.path.join(job_dir, "concat.txt")
        with open(concat_txt, "w") as f:
            f.write(f"file '{hook_path}'\n")
            f.write(f"file '{main_synced_path}'\n")

        final_path = os.path.join(job_dir, f"Final_Recap_{message.message_id}.mp4")
        concat_cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
            "-c", "copy", final_path
        ]
        await asyncio.to_thread(subprocess.run, concat_cmd, stdout=subprocess.DEVNULL)

        await status_msg.edit_text("🎬 လုပ်ငန်းစဉ်အားလုံး ပြီးစီးပါပြီ! ဗီဒီယို ပို့နေပါတယ်...")
        with open(final_path, "rb") as video_file:
            await message.reply_document(
                document=video_file, filename=os.path.basename(final_path),
                caption="🎬 <b>Pro Myanmar Movie Recap</b>\n✨ Auto Hook\n🛡️ Copyright Bypassed\n🧠 Series Memory Updated",
                parse_mode="HTML", read_timeout=900, write_timeout=900
            )

        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
            print(f"🧹 Auto Clean: {job_dir} deleted")

    except Exception as e:
        print("BOT ERROR:", repr(e))
        await message.reply_text("❌ Error ဖြစ်သွားပါတယ်!\n\n" + str(e))

def main():
    if not TOKEN:
        print("❌ BOT_TOKEN မတွေ့ပါ။")
        return

    # Render Portအတွက် Web Server ကို Background Thread ဖြင့် စတင်ခြင်း
    threading.Thread(target=run_health_server, daemon=True).start()
    print("🌐 Render Health Check Server started on port 10000...")

    request = HTTPXRequest(connect_timeout=120, read_timeout=900, write_timeout=900, pool_timeout=120, http_version="1.1")
    app = (
        Application.builder()
        .token(TOKEN)
        .request(request)
        .get_updates_request(request)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video_received))

    print("🤖 Pro Movie Recap Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()

