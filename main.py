import os
import uuid
import subprocess
import json
import urllib.parse
import urllib.request
import time
import asyncio
import shutil

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
MEMORY_FILE = "series_memory.json" # အချက် ၉ - မှတ်ဉာဏ်ဖိုင်

WHISPER_BIN = os.path.expanduser("~/whisper.cpp/build/bin/whisper-cli")
WHISPER_MODEL = os.path.expanduser("~/whisper.cpp/models/ggml-tiny.bin")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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
            "responseMimeType": "application/json" # JSON သီးသန့်ထုတ်ပေးရန်
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
            
            # JSON Parse လုပ်ခြင်း
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

        # --- 1. DOWNLOAD VIDEO ---
        video = message.video
        file = await context.bot.get_file(video.file_id)
        video_path = os.path.join(job_dir, f"video_{message.message_id}.mp4")
        await file.download_to_drive(video_path)

        # --- 2. EXTRACT AUDIO ---
        audio_path = os.path.join(job_dir, f"audio_{message.message_id}.wav") # Whisper အတွက် wav ပြောင်း
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", audio_path], stdout=subprocess.DEVNULL)

        await status_msg.edit_text("🤖 Whisper ဖြင့် စာသားနှင့် အချိန်အမှတ် (SRT) ဖမ်းနေပါတယ်...")

        # --- 3. WHISPER (SRT ထုတ်ခြင်း - အချက် ၁) ---
        text_base = os.path.join(job_dir, f"transcript_{message.message_id}")
        whisper_command = [
            WHISPER_BIN, "-m", WHISPER_MODEL, "-f", audio_path,
            "-l", "zh", "-t", "4", "-osrt", "-of", text_base # -otxt အစား -osrt သုံးသည်
        ]
        await asyncio.to_thread(subprocess.run, whisper_command, stdout=subprocess.DEVNULL)
        
        srt_path = text_base + ".srt"
        if not os.path.exists(srt_path):
            raise Exception("Whisper SRT ဖိုင် မထွက်လာပါ။")

        # SRT ဖိုင်ကို Telegram တွင် ပြန်ပို့ပေးခြင်း (အချက် ၂)
        with open(srt_path, "rb") as srt_file:
            await message.reply_document(document=srt_file, caption="📄 ထုတ်ယူရရှိသော SRT (အချိန်အမှတ်) ဖိုင်")

        with open(srt_path, "r", encoding="utf-8") as f:
            transcript = f.read().strip()

        await status_msg.edit_text("🧠 Gemini မှ ဇာတ်လမ်း၊ Hook နှင့် ဇာတ်ကောင်များကို စဉ်းစားတွက်ချက်နေပါတယ်...")

        # --- 4. GEMINI AI (Hook + Memory + Long Story) ---
        memory_data = load_memory()
        recap_data = create_recap_data(transcript, memory_data)
        
        hook_time = recap_data.get("hook_time", "00:00:10")
        raw_script = recap_data.get("myanmar_script", "ဇာတ်လမ်းအကျဉ်း...")
        
        # မှတ်ဉာဏ် အသစ်ပြန်သိမ်းခြင်း (အချက် ၉)
        if "characters" in recap_data:
            memory_data["characters"].update(recap_data["characters"])
            save_memory(memory_data)

        # မြန်မာစာ Space ရှင်းလင်းခြင်း (အချက် ၄)
        clean_script = raw_script.replace(" ", "")

        await status_msg.edit_text(f"🎙️ AI Voice ဖန်တီးနေပါတယ်... (Hook Time: {hook_time})")

        # --- 5. CREATE VOICE ---
        voice_path = os.path.join(job_dir, f"voice_{message.message_id}.mp3")
        await create_myanmar_voice(clean_script, voice_path)

        await status_msg.edit_text("🛡️ Video ကို Copyright ရှောင်ရန်၊ တရုတ်စာတန်းဝါးရန်နှင့် ညှိနှိုင်းရန် ပြင်ဆင်နေပါတယ်...")

        # --- 6. MAIN VIDEO PROCESSING (အချက် ၆, ၇, ၈, ၁၁, ၁၂) ---
        main_synced_path = os.path.join(job_dir, "main_synced.mp4")
        
        # Filters များ: 
        # hflip = Mirror (မှန်ပြောင်း), 
        # drawbox = အောက်ခြေစာတန်းကို အမည်းရောင်ဘားဖြင့် ဖုံးခြင်း (Blur အစား အလုံခြုံဆုံးနည်းလမ်း)
        # eq = အရောင် Color Grade တင်ခြင်း
        # hue = ၃ စက္ကန့်တိုင်း Transition အလင်းအမှောင်ပြောင်းခြင်း
        # tpad = Video တိုနေပါက Voice ပြီးသည်အထိ နောက်ဆုံး Frame ကို Freeze လုပ်ပေးထားခြင်း
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
            "-shortest", # Voice ကုန်သည်နှင့် ဗီဒီယိုပါ ရပ်တန့်မည်
            main_synced_path
        ]
        await asyncio.to_thread(subprocess.run, main_process_cmd, stdout=subprocess.DEVNULL)

        await status_msg.edit_text("🔥 ၃ စက္ကန့်စာ Hook ဖြတ်ထုတ်နေပါတယ်...")

        # --- 7. EXTRACT HOOK VIDEO (အချက် ၃) ---
        hook_path = os.path.join(job_dir, "hook.mp4")
        # Hook ကိုလည်း Main Video နဲ့ Format တူအောင် Filter အတူတူထည့်ပါမည်
        hook_filter = (
            "[0:v]hflip,"
            "drawbox=y=ih-120:color=black@1.0:width=iw:height=120:t=fill,"
            "eq=contrast=1.05:saturation=1.1,"
            "hue=s='1+0.2*sin(t/3*PI)'[v_out]"
        )
        hook_cmd = [
            "ffmpeg", "-y", "-ss", hook_time, "-i", video_path, "-t", "3",
            "-filter_complex", hook_filter,
            "-map", "[v_out]", "-map", "0:a?", # မူရင်းဗီဒီယို အသံကိုယူမည်
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            hook_path
        ]
        await asyncio.to_thread(subprocess.run, hook_cmd, stdout=subprocess.DEVNULL)

        await status_msg.edit_text("🔗 Hook နှင့် မူရင်းရုပ်ရှင် ပေါင်းစပ်နေပါတယ်...")

        # --- 8. MERGE HOOK + MAIN (အချက် ၅) ---
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

        # --- 9. SEND FINAL VIDEO ---
        await status_msg.edit_text("🎬 လုပ်ငန်းစဉ်အားလုံး ပြီးစီးပါပြီ! ဗီဒီယို ပို့နေပါတယ်...")
        with open(final_path, "rb") as video_file:
            await message.reply_document(
                document=video_file, filename=os.path.basename(final_path),
                caption="🎬 <b>Pro Myanmar Movie Recap</b>\n✨ Auto Hook\n🛡️ Copyright Bypassed\n🧠 Series Memory Updated",
                parse_mode="HTML", read_timeout=900, write_timeout=900
            )

        # --- AUTO CLEANUP ---
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
            print(f"🧹 Auto Clean: {job_dir} deleted")

    except Exception as e:
        print("BOT ERROR:", repr(e))
        await message.reply_text("❌ Error ဖြစ်သွားပါတယ်!\n\n" + str(e))

def main():
    if not TOKEN:
        print("❌ BOT_TOKEN မတွေ့ပါ။ .env ဖိုင်ကို စစ်ပါ။")
        return

    request = HTTPXRequest(connect_timeout=120, read_timeout=900, write_timeout=900, pool_timeout=120, http_version="1.1")
    app = (
        Application.builder()
        .token(TOKEN)
        .base_url("http://127.0.0.1:8081/bot")
        .base_file_url("http://127.0.0.1:8081/file/bot")
        .request(request)
        .get_updates_request(request)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video_received))

    print("🤖 Pro Movie Recap Bot is running with 12 Advanced Features...")
    app.run_polling()

if __name__ == "__main__":
    main()
