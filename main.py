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

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ==============================
# Render Port Check အတွက် Web Server
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
# Edge TTS (Myanmar Voice)
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

import google.generativeai as genai

# ==============================
# Gemini AI (Recap + Hook + Memory)
# ==============================
def create_recap_data(memory_data):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise Exception("GEMINI_API_KEY မတွေ့ပါ။")

    genai.configure(api_key=api_key)
    
    # Google ညွှန်ကြားထားသော မော်ဒယ်အသစ် (gemini-3.6-flash) သို့ ချိတ်ဆက်ခြင်း
    model = genai.GenerativeModel('gemini-3.6-flash')

    prompt = f"""You are a professional Myanmar movie recap script writer.
Task Requirements:
1. Write a VERY LONG, detailed and exciting movie recap narration in spoken Myanmar.
2. Identify the most exciting 3-second scene (The Hook) and return its start time in HH:MM:SS format (e.g., 00:00:10).
3. Extract character names and update them for memory.

Respond ONLY with a raw JSON object (no markdown, no extra text).
Format:
{{
    "hook_time": "00:00:10",
    "characters": {{"hero": "မင်းသား"}},
    "myanmar_script": "ဇာတ်လမ်းက ဒီလိုစထားပါတယ်..."
}}

Previous Memory: {json.dumps(memory_data, ensure_ascii=False)}
"""

    for attempt in range(3):
        try:
            response = model.generate_content(
                prompt,
                generation_config={"response_mime_type": "application/json"}
            )
            raw_response = response.text.strip()
            return json.loads(raw_response)

        except Exception as e:
            if attempt < 2:
                time.sleep(5)
                continue
            raise Exception("Gemini Recap Error:\n" + str(e))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 Pro Movie Recap Bot (Unlimited Size) အဆင်သင့်ဖြစ်ပါပြီ!\n"
        "ဗီဒီယို ဖိုင်အကြီးကြီးတွေကို ပို့လို့ရပါပြီ။"
    )

async def video_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    try:
        job_dir = create_job_folder()
        status_msg = await message.reply_text("📥 ဗီဒီယိုဖိုင် အချက်အလက်များကို ရယူနေပါတယ်...")

        # ဗီဒီယို (သို့မဟုတ် ဖိုင်တွဲ) အချက်အလက်ရယူခြင်း
        video = message.video or message.document
        if not video:
            await message.reply_text("❌ ကျေးဇူးပြု၍ ဗီဒီယိုဖိုင် ပို့ပေးပါ။")
            return

        file_id = video.file_id
        file_info = await context.bot.get_file(file_id)
        file_url = file_info.file_path  # Telegram ကပေးသော တိုက်ရိုက်လင့်ခ်

        video_path = os.path.join(job_dir, f"video_{message.message_id}.mp4")
        
        await status_msg.edit_text("📥 ဗီဒီယိုဖိုင် ကြီးမားသော်လည်း တိုက်ရိုက် Download ဆွဲနေပါပြီ... ခဏစောင့်ပါ ⏳")

        # 20 MB ကန့်သတ်ချက်ကျော်လွန်၍ တိုက်ရိုက် Download ဆွဲခြင်း
        def download_file():
            urllib.request.urlretrieve(file_url, video_path)

        await asyncio.to_thread(download_file)

        await status_msg.edit_text("🧠 Gemini မှ ဇာတ်လမ်းနှင့် Hook ကို စဉ်းစားနေပါတယ်...")

        memory_data = load_memory()
        recap_data = create_recap_data(memory_data)
        
        hook_time = recap_data.get("hook_time", "00:00:10")
        raw_script = recap_data.get("myanmar_script", "ဇာတ်လမ်းအကျဉ်း...")
        
        if "characters" in recap_data:
            memory_data["characters"].update(recap_data["characters"])
            save_memory(memory_data)

        clean_script = raw_script.replace(" ", "")

        await status_msg.edit_text(f"🎙️ AI Voice ဖန်တီးနေပါတယ်... (Hook Time: {hook_time})")

        voice_path = os.path.join(job_dir, f"voice_{message.message_id}.mp3")
        await create_myanmar_voice(clean_script, voice_path)

        await status_msg.edit_text("🛡️ Video ကို Copyright ရှောင်ရန် ပြင်ဆင်နေပါတယ်...")

        main_synced_path = os.path.join(job_dir, "main_synced.mp4")
        complex_filter = (
            "[0:v]hflip,"
            "drawbox=y=ih-120:color=black@1.0:width=iw:height=120:t=fill,"
            "eq=contrast=1.05:saturation=1.1,"
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
        hook_filter = "[0:v]hflip,drawbox=y=ih-120:color=black@1.0:width=iw:height=120:t=fill[v_out]"
        hook_cmd = [
            "ffmpeg", "-y", "-ss", hook_time, "-i", video_path, "-t", "3",
            "-filter_complex", hook_filter,
            "-map", "[v_out]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            hook_path
        ]
        await asyncio.to_thread(subprocess.run, hook_cmd, stdout=subprocess.DEVNULL)

        await status_msg.edit_text("🔗 Hook နှင့် ရုပ်ရှင်ကို Filter ဖြင့် ပေါင်းစပ်နေပါတယ်...")

        final_path = os.path.join(job_dir, f"Final_Recap_{message.message_id}.mp4")
        
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
        await asyncio.to_thread(subprocess.run, merge_cmd, stdout=subprocess.DEVNULL)

        if not os.path.exists(final_path):
            raise Exception("Final Video ဖိုင် ထွက်လာခြင်း မရှိပါ။ FFmpeg Filter Concat အမှားရှိနေပါသည်။")

        # Main ဗီဒီယိုကို Standardize လုပ်ခြင်း
        std_main_cmd = [
            "ffmpeg", "-y", "-i", main_synced_path,
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            standard_main_path
        ]
        await asyncio.to_thread(subprocess.run, std_main_cmd, stdout=subprocess.DEVNULL)

        concat_txt = os.path.join(job_dir, "concat.txt")
        with open(concat_txt, "w") as f:
            f.write(f"file '{standard_hook_path}'\n")
            f.write(f"file '{standard_main_path}'\n")

        final_path = os.path.join(job_dir, f"Final_Recap_{message.message_id}.mp4")
        concat_cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
            "-c:v", "libx264", "-c:a", "aac", final_path
        ]
        await asyncio.to_thread(subprocess.run, concat_cmd, stdout=subprocess.DEVNULL)

        if not os.path.exists(final_path):
            raise Exception("Final Video ဖိုင် ထွက်လာခြင်း မရှိပါ။ FFmpeg ပေါင်းစပ်မှု အမှားရှိနေပါသည်။")

        await asyncio.to_thread(subprocess.run, concat_cmd, stdout=subprocess.DEVNULL)

        await status_msg.edit_text("🎬 ပြီးပါပြီ! အပြီးသတ် ဗီဒီယို ပို့နေပါပြီ...")
        with open(final_path, "rb") as video_file:
            await message.reply_document(
                document=video_file, filename=os.path.basename(final_path),
                caption="🎬 <b>Pro Myanmar Movie Recap (Unlimited)</b>\n✨ Auto Hook\n🛡️ Copyright Bypassed",
                parse_mode="HTML", read_timeout=1200, write_timeout=1200
            )

        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)

    except Exception as e:
        print("BOT ERROR:", repr(e))
        await message.reply_text("❌ Error ဖြစ်သွားပါတယ်!\n\n" + str(e))

def main():
    if not TOKEN:
        print("❌ BOT_TOKEN မတွေ့ပါ။")
        return

    threading.Thread(target=run_health_server, daemon=True).start()
    print("🌐 Render Health Check Server started...")

    request = HTTPXRequest(connect_timeout=120, read_timeout=1200, write_timeout=1200, pool_timeout=120, http_version="1.1")
    app = (
        Application.builder()
        .token(TOKEN)
        .request(request)
        .get_updates_request(request)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO | filters.Document.ALL, video_received))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
