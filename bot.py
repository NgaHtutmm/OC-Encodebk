import os
import time
import math
import asyncio
import re
import glob
import subprocess
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

import whisper
from scenedetect import detect, ContentDetector
from openai import AsyncOpenAI

try:
    from rabbit import Rabbit
except ImportError:
    print("⚠️ rabbit.py မတွေ့ပါ၊ မူလအတိုင်းသာ သုံးပါမည်။")
    class Rabbit:
        @staticmethod
        def uni2zg(text): return text

# ==========================================
# ⚠️ Configurations & API Setup
# ==========================================
API_ID = 7978114
API_HASH = "5f7839feeba133497f24acfd005ef2ec"
BOT_TOKEN = "8698443493:AAERfS3QsJb8dkl-6NlsDn5JvwT5viLVIZY" # ⚠️ အစ်ကို့ Bot Token ကို ပြန်ထည့်ပါ

# 🚀 DeepSeek API Setup
DEEPSEEK_API_KEY = "sk-53d47c2226a444f38c2a029bc78cec8b" # ⚠️ အစ်ကို့ DeepSeek Key ကို ပြန်ထည့်ပါ
ai_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
AI_MODEL_NAME = "deepseek-chat"

PREROLL_FILE = "preroll.mp4"
OUTRO_FILE = "outro.mp4"
BANNER_FILE = "banner.mp4"
FONT_BOLD_FILE = "font_bold.ttf"
LOGO_FILE = "logo.png"
THUMB_FILE = "thumb.jpg"

URL_REGEX = r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"

app = Client("ultimate_encode_bot_v2", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, sleep_threshold=120)

user_data = {}
cancel_flags = {}
active_processes = {}

# ==========================================
# 🌟 LEECH BOT UI (GLOBAL STATUS TRACKER)
# ==========================================
ACTIVE_JOBS = {}       
STATUS_MESSAGES = {}   
LAST_UPDATE_TIME = {}  

def humanbytes(size):
    if not size: return "0 B"
    power = 2**10
    n = 0
    Dic_powerN = {0: ' ', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power:
        size /= power
        n += 1
    return str(round(size, 2)) + " " + Dic_powerN[n] + 'B'

def TimeFormatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    tmp = ((str(days) + "d ") if days else "") + \
        ((str(hours) + "h ") if hours else "") + \
        ((str(minutes) + "m ") if minutes else "") + \
        ((str(seconds) + "s") if seconds else "")
    return tmp.strip() if tmp else "0s"

def generate_progress_bar(percentage):
    filled = int(percentage / 10)
    return '⬢' * filled + '⬡' * (10 - filled)

async def update_central_status(user_id, client):
    now = time.time()
    if now - LAST_UPDATE_TIME.get(user_id, 0) < 15.0:
        return

    if user_id not in ACTIVE_JOBS or not ACTIVE_JOBS[user_id]:
        if user_id in STATUS_MESSAGES:
            try: await STATUS_MESSAGES[user_id].delete()
            except: pass
            del STATUS_MESSAGES[user_id]
        return

    text = ""
    buttons = []

    for i, (j_id, job) in enumerate(ACTIVE_JOBS[user_id].items(), 1):
        elapsed = TimeFormatter((now - job['start_time']) * 1000)

        text += f"**{i}. {job['filename']}**\n"
        text += f"┌ **{job['status']}...**\n"
        text += f"├ {generate_progress_bar(job['progress'])} {round(job['progress'], 2)}%\n"

        if job['total'] > 0:
            text += f"├ **Processed:** {humanbytes(job['current'])} of {humanbytes(job['total'])}\n"
        else:
            text += f"├ **Processed:** {job['current']} / {job['total']} chunks\n"

        if job['speed'] > 0:
            text += f"├ **Speed:** {humanbytes(job['speed'])}/s\n"

        text += f"├ **ETA:** {job['eta']}\n"
        text += f"├ **Elapsed:** {elapsed}\n"
        text += f"└ **Action:** /cancel_{j_id}\n\n"

        buttons.append([InlineKeyboardButton(f"❌ Cancel Task {i} ({j_id[-4:]})", callback_data=f"cancel_job_{j_id}")])

    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
    LAST_UPDATE_TIME[user_id] = now

    try:
        if user_id in STATUS_MESSAGES:
            await STATUS_MESSAGES[user_id].edit_text(text, reply_markup=reply_markup)
        else:
            msg = await client.send_message(user_id, text, reply_markup=reply_markup)
            STATUS_MESSAGES[user_id] = msg
    except Exception as e:
        if "Message is not modified" not in str(e):
            if user_id in STATUS_MESSAGES: del STATUS_MESSAGES[user_id]

def init_job(user_id, job_id, filename):
    if user_id not in ACTIVE_JOBS: ACTIVE_JOBS[user_id] = {}
    ACTIVE_JOBS[user_id][job_id] = {
        'filename': filename[:35] + "..." if len(filename) > 35 else filename,
        'status': 'Initializing',
        'progress': 0.0,
        'current': 0,
        'total': 0,
        'speed': 0,
        'eta': 'Calculating...',
        'start_time': time.time()
    }

def remove_job(user_id, job_id):
    if user_id in ACTIVE_JOBS and job_id in ACTIVE_JOBS[user_id]:
        del ACTIVE_JOBS[user_id][job_id]

async def force_new_status_message(user_id):
    if user_id in STATUS_MESSAGES:
        try: await STATUS_MESSAGES[user_id].delete()
        except: pass
        del STATUS_MESSAGES[user_id]
    LAST_UPDATE_TIME[user_id] = 0

# ==========================================
# 🛠 CONCURRENT QUEUE SYSTEM (တန်းစီစနစ်)
# ==========================================
encode_lock = None
srt_semaphore = None

def get_encode_lock():
    global encode_lock
    if encode_lock is None:
        encode_lock = asyncio.Lock()
    return encode_lock

def get_srt_semaphore():
    global srt_semaphore
    if srt_semaphore is None:
        srt_semaphore = asyncio.Semaphore(3)
    return srt_semaphore

async def run_queued_task(client, user_id, ud, job_id, task_type="video"):
    if job_id in ACTIVE_JOBS.get(user_id, {}):
        ACTIVE_JOBS[user_id][job_id]['status'] = 'Waiting in Queue ⏳'
        asyncio.create_task(update_central_status(user_id, client))
    
    if task_type == "video":
        asyncio.create_task(process_everything(client, user_id, ud, job_id))
    elif task_type == "srt":
        asyncio.create_task(process_standalone_srt_wrapper(client, user_id, ud, job_id))
    elif task_type == "extract_srt":
        asyncio.create_task(extract_original_srt_wrapper(client, user_id, ud, job_id))

async def process_standalone_srt_wrapper(client, user_id, ud, job_id):
    async with get_srt_semaphore():
        if cancel_flags.get(job_id):
            remove_job(user_id, job_id)
            return
        await process_standalone_srt(client, user_id, ud, job_id)

async def extract_original_srt_wrapper(client, user_id, ud, job_id):
    async with get_srt_semaphore():
        if cancel_flags.get(job_id):
            remove_job(user_id, job_id)
            return
        await extract_original_srt(client, user_id, ud, job_id)

# ==========================================
# 🛠 PROGRESS HANDLERS & HELPERS
# ==========================================
async def global_progress(current, total, client, user_id, job_id, status_text, start_time):
    if cancel_flags.get(job_id): raise asyncio.CancelledError("User Cancelled")
    now = time.time()
    diff = now - start_time
    speed = current / diff if diff > 0 else 0
    time_to_completion = round((total - current) / speed) * 1000 if speed > 0 else 0
    percentage = (current * 100 / total) if total > 0 else 0

    if user_id in ACTIVE_JOBS and job_id in ACTIVE_JOBS[user_id]:
        ACTIVE_JOBS[user_id][job_id].update({
            'status': status_text,
            'progress': percentage,
            'current': current,
            'total': total,
            'speed': speed,
            'eta': TimeFormatter(time_to_completion)
        })
    asyncio.create_task(update_central_status(user_id, client))

async def get_video_duration(file_path):
    cmd = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{file_path}"'
    process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await process.communicate()
    try: return float(stdout.decode().strip())
    except: return 0.0

async def has_audio(file_path):
    cmd = f'ffprobe -v error -select_streams a -show_entries stream=codec_type -of default=noprint_wrappers=1:nokey=1 "{file_path}"'
    process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, _ = await process.communicate()
    return "audio" in stdout.decode().strip()

def get_seconds_from_time(time_str):
    try:
        h, m, s = time_str.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except: return 0.0

def get_smart_trim_time(video_path):
    try:
        scene_list = detect(video_path, ContentDetector())
        if not scene_list: return 0
        return scene_list[0][1].get_seconds() if len(scene_list) > 1 else 0
    except: return 0

def format_timestamp(seconds: float):
    hours = math.floor(seconds / 3600)
    seconds %= 3600
    minutes = math.floor(seconds / 60)
    seconds %= 60
    milliseconds = round((seconds - math.floor(seconds)) * 1000)
    seconds = math.floor(seconds)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

def fix_srt_timecode(tc_line):
    parts = tc_line.split('-->')
    if len(parts) != 2: return tc_line
    def clean_time(t_str):
        t_str = t_str.strip().replace('.', ',')
        if ',' not in t_str and t_str.count(':') > 0:
            p = t_str.rsplit(':', 1)
            t_str = f"{p[0]},{p[1]}"
        if ',' not in t_str: t_str += ",000"
        t_part, ms_part = t_str.split(',', 1)
        hms = t_part.split(':')
        if len(hms) == 1: hms = ['00', '00', hms[0]]
        elif len(hms) == 2: hms = ['00', hms[0], hms[1]]
        hms = [str(x).zfill(2) for x in hms]
        ms_part = str(ms_part).zfill(3)[:3]
        return f"{':'.join(hms)},{ms_part}"
    try:
        return f"{clean_time(parts[0])} --> {clean_time(parts[1])}"
    except:
        return tc_line

PROMPT_FILE = "translation_prompt.txt"

def get_translation_prompt():
    import os
    if os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return """You are a highly experienced 18+ adult movie subtitle translator native to Myanmar."""

async def generate_mm_subtitle(video_path, output_srt, client, user_id, job_id, trim_start=0, actual_duration=0):
    try:
        if user_id in ACTIVE_JOBS and job_id in ACTIVE_JOBS[user_id]:
            ACTIVE_JOBS[user_id][job_id]['status'] = 'Extracting Audio'
            asyncio.create_task(update_central_status(user_id, client))

        trim_cmd = f"-ss {trim_start} " if trim_start > 0 else ""
        dur_cmd = f"-t {actual_duration} " if actual_duration > 0 else ""
        os.system(f'ffmpeg -y {trim_cmd} {dur_cmd} -i "{video_path}" -vn -af "aresample=async=1" -acodec pcm_s16le -ar 16000 -ac 1 "temp_audio_{job_id}.wav"')

        model = whisper.load_model("base")

        if user_id in ACTIVE_JOBS and job_id in ACTIVE_JOBS[user_id]:
            ACTIVE_JOBS[user_id][job_id]['status'] = 'Transcribing Audio'
            asyncio.create_task(update_central_status(user_id, client))

        result = await asyncio.to_thread(model.transcribe, f"temp_audio_{job_id}.wav", task="translate")

        if not result['segments']:
            return True, ""

        translated_dict = {}
        segments = result['segments']
        chunk_size = 50
        total_segments = len(segments)
        total_chunks = math.ceil(total_segments / chunk_size)

        for i in range(0, total_segments, chunk_size):
            if cancel_flags.get(job_id): raise asyncio.CancelledError()

            chunk_index = (i // chunk_size) + 1
            percentage = (chunk_index / total_chunks) * 100

            if user_id in ACTIVE_JOBS and job_id in ACTIVE_JOBS[user_id]:
                ACTIVE_JOBS[user_id][job_id].update({
                    'status': 'DeepSeek Translating',
                    'progress': percentage,
                    'current': chunk_index,
                    'total': total_chunks,
                    'eta': 'Working...',
                    'speed': 0
                })
                asyncio.create_task(update_central_status(user_id, client))

            chunk = segments[i:i+chunk_size]
            text_for_ai = ""
            current_ids = []

            for j, segment in enumerate(chunk):
                idx = i + j + 1
                clean_text = segment['text'].strip().replace('\n', ' ')
                text_for_ai += f"[{idx}] {clean_text}\n"
                current_ids.append(idx)

            max_retries = 3
            for attempt in range(max_retries):
                if cancel_flags.get(job_id): raise asyncio.CancelledError()
                try:
                    response = await ai_client.chat.completions.create(
                        model=AI_MODEL_NAME,
                        messages=[
                            {"role": "system", "content": get_translation_prompt()},
                            {"role": "user", "content": f"IMPORTANT: Only translate IDs from {current_ids[0]} to {current_ids[-1]}.\nTexts:\n{text_for_ai}"}
                        ],
                        temperature=0.8
                    )
                    response_text = response.choices[0].message.content

                    success_count = 0
                    for line in response_text.split('\n'):
                        match = re.search(r'\[(\d+)\]\s*(.*)', line)
                        if match:
                            t_idx = int(match.group(1))
                            if t_idx in current_ids:
                                translated_dict[t_idx] = match.group(2).strip()
                                success_count += 1

                    if success_count > 0: break
                    else: await asyncio.sleep(1)
                except Exception as e:
                    print(f"DeepSeek Translation Error: {e}", flush=True)
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2)

        srt_content = ""
        for i, segment in enumerate(result['segments']):
            start = format_timestamp(segment['start'])
            end = format_timestamp(segment['end'])
            idx = i + 1
            trans_text = translated_dict.get(idx, segment['text'].strip())
            try: trans_text_zg = Rabbit.uni2zg(trans_text)
            except: trans_text_zg = trans_text
            srt_content += f"{idx}\n{start} --> {end}\n{trans_text_zg}\n\n"

        with open(output_srt, "w", encoding="utf-8") as f:
            f.write(srt_content)
        return True, ""
    except Exception as e:
        return False, str(e)
    finally:
        try: os.remove(f"temp_audio_{job_id}.wav")
        except: pass

async def translate_existing_srt(input_srt, output_srt, client, user_id, job_id):
    try:
        with open(input_srt, "r", encoding="utf-8") as f:
            content = f.read()

        blocks = re.split(r'\n\s*\n', content.strip())
        parsed_blocks = []
        for block in blocks:
            lines = block.split('\n')
            if len(lines) >= 3:
                idx_str = lines[0].strip()
                timing = fix_srt_timecode(lines[1].strip())
                text = " ".join(lines[2:]).strip()
                match = re.search(r'\d+', idx_str)
                idx = int(match.group()) if match else len(parsed_blocks) + 1
                parsed_blocks.append({"idx": idx, "orig_idx_str": idx_str, "timing": timing, "text": text})

        translated_dict = {}
        chunk_size = 50
        total_blocks = len(parsed_blocks)
        total_chunks = math.ceil(total_blocks / chunk_size)

        for i in range(0, total_blocks, chunk_size):
            if cancel_flags.get(job_id): raise asyncio.CancelledError()

            chunk_index = (i // chunk_size) + 1
            percentage = (chunk_index / total_chunks) * 100

            if user_id in ACTIVE_JOBS and job_id in ACTIVE_JOBS[user_id]:
                ACTIVE_JOBS[user_id][job_id].update({
                    'status': 'DeepSeek SRT Translating',
                    'progress': percentage,
                    'current': chunk_index,
                    'total': total_chunks,
                    'eta': 'Working...',
                    'speed': 0
                })
                asyncio.create_task(update_central_status(user_id, client))

            chunk = parsed_blocks[i:i+chunk_size]
            text_for_ai = ""
            current_ids = []
            for pb in chunk:
                text_for_ai += f"[{pb['idx']}] {pb['text']}\n"
                current_ids.append(pb['idx'])

            max_retries = 3
            for attempt in range(max_retries):
                if cancel_flags.get(job_id): raise asyncio.CancelledError()
                try:
                    response = await ai_client.chat.completions.create(
                        model=AI_MODEL_NAME,
                        messages=[
                            {"role": "system", "content": get_translation_prompt()},
                            {"role": "user", "content": f"IMPORTANT: Only translate IDs from {current_ids[0]} to {current_ids[-1]}.\nTexts:\n{text_for_ai}"}
                        ],
                        temperature=0.9
                    )
                    response_text = response.choices[0].message.content

                    success_count = 0
                    for line in response_text.split('\n'):
                        match = re.search(r'\[(\d+)\]\s*(.*)', line)
                        if match:
                            t_idx = int(match.group(1))
                            if t_idx in current_ids:
                                translated_dict[t_idx] = match.group(2).strip()
                                success_count += 1
                    if success_count > 0: break
                    else: await asyncio.sleep(1)
                except Exception as e:
                    print(f"DeepSeek Translation Error: {e}", flush=True)
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2)

        srt_content = ""
        for pb in parsed_blocks:
            idx = pb["idx"]
            trans_text = translated_dict.get(idx, pb["text"])
            try: trans_text_zg = Rabbit.uni2zg(trans_text)
            except: trans_text_zg = trans_text
            srt_content += f"{pb['orig_idx_str']}\n{pb['timing']}\n{trans_text_zg}\n\n"

        with open(output_srt, "w", encoding="utf-8") as f:
            f.write(srt_content)
        return True, ""
    except Exception as e:
        return False, str(e)

async def ask_banner(message_or_callback, user_id):
    user_data[user_id]["state"] = "ASK_BANNER"
    btns = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ မူလ Banner အသေသုံးမည်", callback_data="banner_default")],
        [InlineKeyboardButton("📥 ဖိုင်အသစ် ကိုယ်တိုင်ပို့မည်", callback_data="banner_custom")],
        [InlineKeyboardButton("❌ မထည့်ပါ", callback_data="banner_no")],
        [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
    ])
    text = "🚀 **ဗီဒီယိုအောက်ခြေတွင် ကြော်ငြာ Banner ထည့်မလား? ဘယ်ဖိုင်ကို သုံးမလဲ?**"
    if isinstance(message_or_callback, CallbackQuery): await message_or_callback.message.edit_text(text, reply_markup=btns)
    else: await message_or_callback.reply_text(text, reply_markup=btns)

async def ask_thumb(message_or_callback, user_id):
    user_data[user_id]["state"] = "ASK_THUMB"
    btns = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼️ ပုံအသစ်ပြောင်းမည်", callback_data="thumb_custom")],
        [InlineKeyboardButton("✅ မူလပုံပဲသုံးမည်", callback_data="thumb_default")],
        [InlineKeyboardButton("❌ လုံးဝမထည့်ပါ", callback_data="thumb_none")],
        [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
    ])
    text = "🖼️ **ဗီဒီယိုရဲ့ မျက်နှာဖုံးပုံ (Thumbnail) ကို ဘယ်လိုလုပ်မလဲ?**"
    if isinstance(message_or_callback, CallbackQuery): await message_or_callback.message.edit_text(text, reply_markup=btns)
    else: await message_or_callback.reply_text(text, reply_markup=btns)

# ==========================================
# ⚙️ COMMANDS & SETUP HANDLERS
# ==========================================
@app.on_message(filters.command("status"))
async def status_command(client, message):
    user_id = message.from_user.id
    await force_new_status_message(user_id)
    await update_central_status(user_id, client)
    if user_id not in STATUS_MESSAGES:
        await message.reply_text("💤 **လက်ရှိ Run နေသော Task မရှိပါ။**")

@app.on_message(filters.command("start"))
async def start_command(client, message):
    user_id = message.from_user.id
    if user_id in user_data: del user_data[user_id]
    await message.reply_text("👋 **Video ဖိုင် (သို့မဟုတ်) Download Link ကို ပို့ပေးပါ။**\n\n📁 *သီးသန့် Subtitle (.srt / .ass) ဘာသာပြန်လိုပါက ဖိုင်ကို တိုက်ရိုက် ပို့ပေးပါ (သို့) /translate ကို နှိပ်ပါ။*\n📊 *လက်ရှိအခြေအနေကြည့်ရန် /status ကိုနှိပ်ပါ*")

@app.on_message(filters.command("translate"))
async def translate_command(client, message):
    user_id = message.from_user.id
    if user_id in user_data: del user_data[user_id]
    await message.reply_text("📁 **ကျေးဇူးပြု၍ ဘာသာပြန်လိုသော Subtitle (.srt သို့မဟုတ် .ass) ဖိုင်ကို တိုက်ရိုက် ပို့ပေးပါ။**")

@app.on_message(filters.command("getprompt"))
async def get_prompt_cmd(client, message):
    prompt_text = get_translation_prompt()
    with open("current_prompt.txt", "w", encoding="utf-8") as f:
        f.write(prompt_text)
    await message.reply_document(
        "current_prompt.txt",
        caption="📝 **ဒီမှာ လက်ရှိသုံးနေတဲ့ Prompt ပါ။**\n\nပြင်ချင်ရင် ဒီဖိုင်ကို ဒေါင်းလုဒ်ဆွဲ၊ ဖုန်း (သို့) ကွန်ပျူတာမှာ ပြင်ပြီး `.txt` ဖိုင်အနေနဲ့ Bot ဆီ ပြန်ပို့ပါ။\nပြီးရင် ကိုယ်ပို့လိုက်တဲ့ ဖိုင်ကို Reply ပြန်ပြီး `/setprompt` လို့ ရိုက်လိုက်ပါ။"
    )
    import os
    os.remove("current_prompt.txt")

@app.on_message(filters.command("setprompt"))
async def set_prompt_cmd(client, message):
    if message.reply_to_message and message.reply_to_message.document:
        if not message.reply_to_message.document.file_name.endswith(".txt"):
            await message.reply_text("⚠️ `.txt` ဖိုင်ကိုသာ လက်ခံပါသည်။")
            return
        file_path = await message.reply_to_message.download()
        with open(file_path, "r", encoding="utf-8") as f:
            new_prompt = f.read()
        with open(PROMPT_FILE, "w", encoding="utf-8") as f:
            f.write(new_prompt)
        import os
        os.remove(file_path)
        await message.reply_text("✅ **Prompt အသစ်ကို အောင်မြင်စွာ Update လုပ်လိုက်ပါပြီ!**\nနောက်ဘာသာပြန်မယ့် ကားတွေကစပြီး ဒီ Prompt အသစ်နဲ့ အလုပ်လုပ်ပါမယ်။")
    else:
        await message.reply_text("⚠️ ကျေးဇူးပြု၍ သင်ပြင်ဆင်ထားသော `.txt` ဖိုင်ကို Reply ပြန်ပြီး `/setprompt` လို့ရိုက်ပါ။")

@app.on_message(filters.video | filters.document | filters.photo | filters.animation)
async def handle_media(client, message):
    user_id = message.from_user.id
    state = user_data.get(user_id, {}).get("state")

    if state == "WAITING_SUBTITLE":
        if message.document and message.document.file_name and message.document.file_name.endswith(('.srt', '.ass')):
            user_data[user_id]["sub_msg"] = message
            user_data[user_id]["state"] = "ASK_TRANSLATE_SRT"
            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("🤖 မြန်မာလို AI ဖြင့် ဘာသာပြန်မည်", callback_data="translate_srt_yes")],
                [InlineKeyboardButton("❌ မပြန်ပါ (မူလအတိုင်းသုံးမည်)", callback_data="translate_srt_no")],
                [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
            ])
            await message.reply_text("🤖 **ဒီ SRT ဖိုင်ကို DeepSeek သုံးပြီး မြန်မာလို အကြမ်းစား ဘာသာပြန်ခိုင်းမလား?**", reply_markup=btns)
        else:
            await message.reply_text("⚠️ ကျေးဇူးပြု၍ .srt သို့မဟုတ် .ass ဖိုင်ကိုသာ ပို့ပါ။")
        return

    if state == "WAITING_LOGO_FILE":
        if message.photo or (message.document and message.document.mime_type and message.document.mime_type.startswith('image/')):
            user_data[user_id]["logo_msg"] = message
            user_data[user_id]["state"] = "ASK_SUB"
            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("🤖 AI ဖြင့် Auto မြန်မာစာတန်းထိုးမည်", callback_data="sub_auto")],
                [InlineKeyboardButton("📂 Subtitle ဖိုင် (.srt) ကိုယ်တိုင်ပေးမည်", callback_data="sub_upload")],
                [InlineKeyboardButton("❌ မထည့်ပါ", callback_data="sub_no")],
                [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
            ])
            await message.reply_text("📝 **မြန်မာစာတန်းထိုး (.srt) ထည့်မလား?**", reply_markup=btns)
        else:
            await message.reply_text("⚠️ ကျေးဇူးပြု၍ Logo အတွက် ပုံ (Photo သို့မဟုတ် PNG) ကိုသာ ပို့ပေးပါ။")
        return

    if state not in ["WAITING_FRONT_PREROLL", "WAITING_BANNER_FILE", "WAITING_THUMB"]:
        if message.document and message.document.file_name and message.document.file_name.endswith(('.srt', '.ass')):
            user_data[user_id] = {"srt_msg": message, "state": "ASK_STANDALONE_TRANSLATE"}
            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("🤖 AI ဖြင့် မြန်မာလို ဘာသာပြန်မည်", callback_data="standalone_trans_yes")],
                [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
            ])
            await message.reply_text("📁 **Subtitle ဖိုင်ကို လက်ခံရရှိပါပြီ။ သီးသန့် ဘာသာပြန်ခိုင်းမလား?**", reply_markup=btns)
            return

    if state == "WAITING_FRONT_PREROLL":
        if message.video or (message.document and message.document.mime_type and message.document.mime_type.startswith('video/')):
            user_data[user_id]["front_msg"] = message
            user_data[user_id]["use_custom_front"] = True
            user_data[user_id]["use_default_front"] = True
            user_data[user_id]["custom_front_path"] = f"custom_front_{user_id}.mp4"

            user_data[user_id]["state"] = "ASK_OUTRO"
            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ အသေထည့်မည် (outro.mp4)", callback_data="outro_yes")],
                [InlineKeyboardButton("❌ မထည့်ပါ", callback_data="outro_no")],
                [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
            ])
            await message.reply_text("🎬 **နောက်ဆုံးပိုင်း (Outro) ကို ဆက်ပြီး ထည့်မလား?**", reply_markup=btns)
        else:
            await message.reply_text("⚠️ ကျေးဇူးပြု၍ Preroll အသစ်အတွက် Video ဖိုင်ကိုသာ ပို့ပေးပါ။")
        return

    if state == "WAITING_BANNER_FILE":
        if message.video or message.animation or (message.document and message.document.mime_type and (message.document.mime_type.startswith('video/') or message.document.mime_type == 'image/gif')):
            user_data[user_id]["banner_msg"] = message
            user_data[user_id]["state"] = "ASK_EXISTING_BANNER"
            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ အဟောင်းပါသည် (ဖုံးမည်)", callback_data="banner_exist_yes")],
                [InlineKeyboardButton("❌ အဟောင်းမပါပါ", callback_data="banner_exist_no")],
                [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
            ])
            await message.reply_text("🤔 **ဗီဒီယိုအဟောင်းထဲတွင် Banner ပါပြီးသားဖြစ်နေပါသလား?**", reply_markup=btns)
        else:
            await message.reply_text("⚠️ ကျေးဇူးပြု၍ Banner အတွက် Video သို့မဟုတ် GIF ဖိုင်ကိုသာ ပို့ပေးပါ။")
        return

    if state == "WAITING_THUMB":
        if message.photo or (message.document and message.document.mime_type and message.document.mime_type.startswith('image/')):
            job_id = str(int(time.time() * 1000))
            cancel_flags[job_id] = False

            thumb_dl_path = f"custom_thumb_{job_id}.jpg"
            downloaded_thumb = await message.download(file_name=thumb_dl_path)

            ud = user_data.pop(user_id, {})
            ud["use_thumb"] = True
            ud["thumb_path"] = downloaded_thumb

            await force_new_status_message(user_id)
            init_job(user_id, job_id, ud["video_msg"].video.file_name if getattr(ud["video_msg"], "video", None) and ud["video_msg"].video.file_name else f"Video_{job_id}")
            asyncio.create_task(update_central_status(user_id, client))
            asyncio.create_task(run_queued_task(client, user_id, ud, job_id, "video"))

            await message.reply_text(f"✅ **Task အသစ် ထည့်သွင်းပြီးပါပြီ။** \n(Queue တွင်စောင့်နေပါသည်၊ နောက်ကားများ ဆက်ပို့နိုင်ပါပြီ)")
        else:
            await message.reply_text("⚠️ ကျေးဇူးပြု၍ Thumbnail ပုံကိုသာ ပို့ပေးပါ။")
        return

    is_video = False
    if message.video: is_video = True
    elif message.document and message.document.mime_type and message.document.mime_type.startswith('video/'): is_video = True

    if is_video:
        user_data[user_id] = {"video_msg": message, "state": "ASK_VIDEO_ACTION"}
        btns = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎥 Encode လုပ်မယ်", callback_data="action_encode")],
            [InlineKeyboardButton("📝 SRT ထုတ်မယ်", callback_data="action_extract_srt")],
            [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
        ])
        await message.reply_text("🎬 **ဒီ Video ကို ဘာလုပ်ချင်လဲ ရွေးပေးပါ-**", reply_markup=btns)
    elif state not in ["WAITING_SUBTITLE", "WAITING_FRONT_PREROLL", "WAITING_THUMB", "WAITING_BANNER_FILE", "WAITING_LOGO_FILE", "ASK_STANDALONE_TRANSLATE"]:
        await message.reply_text("⚠️ ကျေးဇူးပြု၍ Video ဖိုင်ကိုသာ ပို့ပေးပါ။")

@app.on_callback_query()
async def callback_handler(client, callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data

    try:
        if not data.startswith("cancel_"):
            await callback.answer()
    except:
        pass

    if data.startswith("cancel_"):
        if data.startswith("cancel_job_"):
            job_id = data.replace("cancel_job_", "")
            cancel_flags[job_id] = True
            if job_id in active_processes:
                try: active_processes[job_id].terminate()
                except: pass
            await callback.answer(f"❌ Task {job_id[-4:]} ကို ရပ်တန့်လိုက်ပါပြီ။", show_alert=True)
            remove_job(user_id, job_id)
            await force_new_status_message(user_id)
            asyncio.create_task(update_central_status(user_id, client))
        else:
            if user_id in user_data: del user_data[user_id]
            await callback.answer("❌ လုပ်ငန်းစဉ်ကို ရပ်တန့်လိုက်ပါပြီ။", show_alert=True)
            await callback.message.edit_text("❌ **Cancelled Setup.**\n(စတင်ရန် Video ဖိုင်/Link အသစ်ပြန်ပို့ပါ)")
        return

    if user_id not in user_data:
        await callback.answer("⚠️ Session Expired. ဗီဒီယို/ဖိုင် အသစ်ပြန်ပို့ပါ။", show_alert=True)
        return

    if data == "standalone_trans_yes":
        ud = user_data.pop(user_id, {})
        job_id = str(int(time.time() * 1000))
        cancel_flags[job_id] = False

        await force_new_status_message(user_id)
        init_job(user_id, job_id, ud["srt_msg"].document.file_name)
        asyncio.create_task(update_central_status(user_id, client))
        asyncio.create_task(run_queued_task(client, user_id, ud, job_id, "srt"))

        await callback.message.edit_text(f"✅ **Task အသစ် ထည့်သွင်းပြီးပါပြီ။** \n(Queue တွင်စောင့်နေပါသည်၊ နောက်ကားများ ဆက်ပို့နိုင်ပါပြီ)")

    elif data == "action_encode":
        user_data[user_id]["state"] = "ASK_CUT_START"
        btns = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ ဖြတ်မည်", callback_data="cut_start_yes")],
            [InlineKeyboardButton("❌ မဖြတ်ပါ", callback_data="cut_start_no")],
            [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
        ])
        await callback.message.edit_text("✂️ **ဗီဒီယို ရှေ့ပိုင်း (Preroll/Intro) ကို ဖြတ်ထုတ်မလား?**", reply_markup=btns)

    elif data == "action_extract_srt":
        ud = user_data.pop(user_id, {})
        job_id = str(int(time.time() * 1000))
        cancel_flags[job_id] = False

        await force_new_status_message(user_id)
        init_job(user_id, job_id, ud["video_msg"].video.file_name if getattr(ud["video_msg"], "video", None) and ud["video_msg"].video.file_name else f"Video_{job_id}")
        asyncio.create_task(update_central_status(user_id, client))
        asyncio.create_task(run_queued_task(client, user_id, ud, job_id, "extract_srt"))

        await callback.message.edit_text(f"✅ **SRT ထုတ်မည့် Task အသစ် ထည့်သွင်းပြီးပါပြီ။** \n(Queue တွင်စောင့်နေပါသည်)")

    elif data in ["cut_start_yes", "cut_start_no"]:
        if data == "cut_start_yes":
            user_data[user_id]["state"] = "ASK_CUT_METHOD"
            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("🤖 AI ဖြင့် Auto ရှာဖြတ်မည်", callback_data="cut_method_ai")],
                [InlineKeyboardButton("⏱ စက္ကန့် ကိုယ်တိုင်ရေးထည့်မည်", callback_data="cut_method_manual")],
                [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
            ])
            await callback.message.edit_text("✂️ **ဘယ်လို ဖြတ်ထုတ်မလဲ?**", reply_markup=btns)
        else:
            user_data[user_id]["trim_start"] = -1
            user_data[user_id]["state"] = "ASK_TRIM_END"
            await callback.message.edit_text("✂️ **ဗီဒီယို နောက်ဆုံး (End) ကနေ ဘယ်နှစ်စက္ကန့် ဖြတ်ထုတ်မလဲ?**\n(ဘာမှမဖြတ်လိုပါက `0` ဟုသာ ပို့ပါ)")

    elif data in ["cut_method_ai", "cut_method_manual"]:
        if data == "cut_method_ai":
            user_data[user_id]["trim_start"] = 0
            user_data[user_id]["state"] = "ASK_TRIM_END"
            await callback.message.edit_text("✂️ **ဗီဒီယို နောက်ဆုံး (End) ကနေ ဘယ်နှစ်စက္ကန့် ဖြတ်ထုတ်မလဲ?**\n(ဘာမှမဖြတ်လိုပါက `0` ဟုသာ ပို့ပါ)")
        else:
            user_data[user_id]["state"] = "ASK_TRIM_START_MANUAL"
            await callback.message.edit_text("⏱ **ဗီဒီယို ရှေ့ဆုံးကနေ ဖြတ်ထုတ်မည့် 'စက္ကန့်' အရေအတွက်ကို ဂဏန်းဖြင့် ရိုက်ထည့်ပါ။**\n(ဥပမာ - 15, 20.5)")

    elif data in ["crop_yes", "crop_no"]:
        user_data[user_id]["use_crop"] = (data == "crop_yes")
        user_data[user_id]["state"] = "ASK_LOGO"
        btns = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ မူလ Logo အုပ်မည်", callback_data="logo_default")],
            [InlineKeyboardButton("🖼️ Logo အသစ်ပို့မည်", callback_data="logo_custom")],
            [InlineKeyboardButton("❌ မအုပ်ပါ", callback_data="logo_no")],
            [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
        ])
        await callback.message.edit_text("🖼 **သူများ Logo နေရာမှာ ကိုယ့် Logo ကို ဖုံးအုပ်မလား?**", reply_markup=btns)

    elif data in ["logo_default", "logo_custom", "logo_no"]:
        user_data[user_id]["use_logo"] = (data != "logo_no")
        user_data[user_id]["custom_logo"] = (data == "logo_custom")

        if data == "logo_custom":
            user_data[user_id]["state"] = "WAITING_LOGO_FILE"
            await callback.message.edit_text("📥 **Logo အတွက် ပုံ (Photo သို့မဟုတ် PNG) ကို အခု ပို့ပေးပါ...**")
        else:
            user_data[user_id]["state"] = "ASK_SUB"
            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("🤖 AI ဖြင့် Auto မြန်မာစာတန်းထိုးမည်", callback_data="sub_auto")],
                [InlineKeyboardButton("📂 Subtitle ဖိုင် (.srt) ကိုယ်တိုင်ပေးမည်", callback_data="sub_upload")],
                [InlineKeyboardButton("❌ မထည့်ပါ", callback_data="sub_no")],
                [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
            ])
            await callback.message.edit_text("📝 **မြန်မာစာတန်းထိုး (.srt) ထည့်မလား?**", reply_markup=btns)

    elif data in ["sub_auto", "sub_upload", "sub_no"]:
        user_data[user_id]["use_sub"] = (data != "sub_no")
        user_data[user_id]["auto_sub"] = (data == "sub_auto")

        if data == "sub_upload":
            user_data[user_id]["state"] = "WAITING_SUBTITLE"
            btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]])
            await callback.message.edit_text("📥 **Subtitle ဖိုင် (.srt သို့ .ass) ကို အခုပေးပို့လိုက်ပါ...**", reply_markup=btn)
        else:
            user_data[user_id]["state"] = "ASK_FRONT_PREROLL"
            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 မူလ Preroll သာသုံးမည်", callback_data="front_default")],
                [InlineKeyboardButton("🆕 အသစ် + မူလ Preroll တွဲသုံးမည်", callback_data="front_custom")],
                [InlineKeyboardButton("❌ ရှေ့မှာဘာမှမထည့်ပါ", callback_data="front_none")],
                [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
            ])
            await callback.message.edit_text("🎬 **ရှေ့ပိုင်း (Intro) ကို ဘယ်လိုလုပ်မလဲ?**", reply_markup=btns)

    elif data in ["translate_srt_yes", "translate_srt_no"]:
        user_data[user_id]["translate_uploaded_sub"] = (data == "translate_srt_yes")
        user_data[user_id]["state"] = "ASK_FRONT_PREROLL"
        btns = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 မူလ Preroll သာသုံးမည်", callback_data="front_default")],
            [InlineKeyboardButton("🆕 အသစ် + မူလ Preroll တွဲသုံးမည်", callback_data="front_custom")],
            [InlineKeyboardButton("❌ ရှေ့မှာဘာမှမထည့်ပါ", callback_data="front_none")],
            [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
        ])
        await callback.message.edit_text("🎬 **ရှေ့ပိုင်း (Intro) ကို ဘယ်လိုလုပ်မလဲ?**", reply_markup=btns)

    elif data in ["front_default", "front_custom", "front_none"]:
        user_data[user_id]["use_custom_front"] = (data == "front_custom")
        user_data[user_id]["use_default_front"] = (data in ["front_default", "front_custom"])

        if data == "front_custom":
            user_data[user_id]["state"] = "WAITING_FRONT_PREROLL"
            await callback.message.edit_text("📥 **ရှေ့ဆုံးမှာထည့်မည့် Video အတိုလေး (Intro အသစ်) ကို အခု ပို့ပေးပါ...**")
        else:
            user_data[user_id]["state"] = "ASK_OUTRO"
            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ အသေထည့်မည် (outro.mp4)", callback_data="outro_yes")],
                [InlineKeyboardButton("❌ မထည့်ပါ", callback_data="outro_no")],
                [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
            ])
            await callback.message.edit_text("🎬 **နောက်ဆုံးပိုင်း (Outro) ကို ဆက်ပြီး ထည့်မလား?**", reply_markup=btns)

    elif data in ["outro_yes", "outro_no"]:
        user_data[user_id]["use_outro"] = (data == "outro_yes")
        await ask_banner(callback, user_id)

    elif data in ["banner_default", "banner_custom", "banner_no"]:
        user_data[user_id]["use_banner"] = (data != "banner_no")
        user_data[user_id]["custom_banner"] = (data == "banner_custom")

        if data == "banner_default":
            user_data[user_id]["state"] = "ASK_EXISTING_BANNER"
            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ အဟောင်းပါသည် (ဖုံးမည်)", callback_data="banner_exist_yes")],
                [InlineKeyboardButton("❌ အဟောင်းမပါပါ", callback_data="banner_exist_no")],
                [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
            ])
            await callback.message.edit_text("🤔 **ဗီဒီယိုအဟောင်းထဲတွင် Banner ပါပြီးသားဖြစ်နေပါသလား?**", reply_markup=btns)
        elif data == "banner_custom":
            user_data[user_id]["state"] = "WAITING_BANNER_FILE"
            await callback.message.edit_text("📥 **ကြော်ငြာ Banner အတွက် Video သို့မဟုတ် GIF ဖိုင်ကို အခု ပို့ပေးပါ...**")
        else:
            await ask_thumb(callback, user_id)

    elif data in ["banner_exist_yes", "banner_exist_no"]:
        user_data[user_id]["has_existing_banner"] = (data == "banner_exist_yes")
        if data == "banner_exist_yes":
            user_data[user_id]["state"] = "WAITING_BANNER_MINUTES"
            await callback.message.edit_text("⏱️ **Banner အဟောင်းပါသော မိနစ်များကို ကော်မာ (,) ခြား၍ ရိုက်ထည့်ပါ။**\n(ဥပမာ - 15, 45, 60)")
        else:
            await ask_thumb(callback, user_id)

    elif data in ["thumb_default", "thumb_none"]:
        ud = user_data.pop(user_id, {})
        ud["use_thumb"] = (data == "thumb_default")
        if ud["use_thumb"]: ud["thumb_path"] = THUMB_FILE
        else: ud["thumb_path"] = None

        job_id = str(int(time.time() * 1000))
        cancel_flags[job_id] = False

        await force_new_status_message(user_id)
        init_job(user_id, job_id, ud["video_msg"].video.file_name if getattr(ud["video_msg"], "video", None) and ud["video_msg"].video.file_name else f"Video_{job_id}")
        asyncio.create_task(update_central_status(user_id, client))
        asyncio.create_task(run_queued_task(client, user_id, ud, job_id, "video"))

        await callback.message.edit_text(f"✅ **Task အသစ် ထည့်သွင်းပြီးပါပြီ။** \n(Queue တွင်စောင့်နေပါသည်၊ နောက်ကားများ ဆက်ပို့နိုင်ပါပြီ)")

    elif data == "thumb_custom":
        user_data[user_id]["state"] = "WAITING_THUMB"
        await callback.message.edit_text("🖼️ **Thumbnail အတွက် ပုံ (Photo) ကို အခု ပို့ပေးပါ...**")

# ==========================================
# ⚙️ MAIN PROCESSING ENGINE (Core Tasks)
# ==========================================

async def extract_original_srt(client, user_id, ud, job_id):
    v_msg = ud["video_msg"]
    v_path = f"video_srt_{job_id}.mp4"
    audio_path = f"temp_audio_srt_{job_id}.wav"
    srt_path = f"original_{job_id}.srt"

    try:
        ACTIVE_JOBS[user_id][job_id]['status'] = 'Downloading Video'
        if ud.get("is_leeched"):
            v_path = ud.get("leeched_file_path")
        else:
            v_path = await v_msg.download(
                file_name=v_path,
                progress=global_progress,
                progress_args=(client, user_id, job_id, "Downloading Video", time.time())
            )
        if cancel_flags.get(job_id): raise asyncio.CancelledError()

        ACTIVE_JOBS[user_id][job_id].update({'status': 'Extracting Audio (FFmpeg)', 'progress': 40})
        asyncio.create_task(update_central_status(user_id, client))

        os.system(f'ffmpeg -y -i "{v_path}" -vn -af "aresample=async=1" -acodec pcm_s16le -ar 16000 -ac 1 "{audio_path}"')
        
        if not os.path.exists(audio_path):
            raise Exception("Audio မပါဝင်ပါ သို့မဟုတ် Audio ခွဲထုတ်၍မရပါ။")
        if cancel_flags.get(job_id): raise asyncio.CancelledError()

        ACTIVE_JOBS[user_id][job_id].update({'status': 'Generating SRT (Local Whisper)', 'progress': 80})
        asyncio.create_task(update_central_status(user_id, client))

        model = whisper.load_model("small")
        result = await asyncio.to_thread(
            model.transcribe, 
            audio_path,
            fp16=False,
            condition_on_previous_text=False
        )

        srt_content = ""
        valid_idx = 1
        for segment in result['segments']:
            text = segment['text'].strip()
            
            # ==========================================
            # 🚀 HALLUCINATION FILTER (စောက်တလွဲ စာကြောင်းများကို ဖြတ်ထုတ်ခြင်း)
            # ==========================================
            if not text:
                continue
            
            # ၁။ စာလုံးတစ်လုံးတည်း ၅ ခါထက်ပိုထပ်နေလျှင် (ဥပမာ - ლლლლ, සිවිවිවි) ကျော်မည်
            if re.search(r'(.)\1{4,}', text):
                continue
            
            # ၂။ စကားလုံးတစ်ခုတည်း ၃ ခါထက်ပိုထပ်နေလျှင် (ဥပမာ - oh oh oh oh, hh hh hh) ကျော်မည်
            if re.search(r'\b(\w+)(?:[\s,]+\1\b){3,}', text, re.IGNORECASE):
                continue
            
            # ၃။ အသံထွက်သက်သက် ညည်းသံများကို ကျော်မည်
            lower_text = text.lower()
            if lower_text in ["oh", "ah", "uh", "mhm", "hh", "hmm", "wow"]:
                continue
            
            # ၄။ အက္ခရာမဟုတ်သော သင်္ကေတများချည်းဆိုလျှင် ကျော်မည် (ဥပမာ - ʕʔʔʔ)
            if len(re.findall(r'[a-zA-Z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', text)) < 2:
                continue
            # ==========================================

            start = format_timestamp(segment['start'])
            end = format_timestamp(segment['end'])
            srt_content += f"{valid_idx}\n{start} --> {end}\n{text}\n\n"
            valid_idx += 1

        # မူရင်းစာကြောင်းများ လုံးဝမကျန်တော့လျှင် (18+ ညည်းသံချည်းပဲဆိုလျှင်) Error ပြရန်
        if valid_idx == 1:
            raise Exception("စကားပြောသံ မတွေ့ရပါ။ (ညည်းသံ/Music သီးသန့် ဖြစ်နေနိုင်ပါသည်)")

        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)

        ACTIVE_JOBS[user_id][job_id].update({'status': 'Uploading SRT', 'progress': 100})
        asyncio.create_task(update_central_status(user_id, client))

        original_name = getattr(v_msg.video, "file_name", None) or getattr(v_msg.document, "file_name", f"video_{job_id}")
        base_name = os.path.splitext(original_name)[0] if original_name else f"video_{job_id}"
        srt_name = f"{base_name}.srt"

        await client.send_document(
            chat_id=user_id,
            document=srt_path,
            file_name=srt_name,
            caption="✅ **မူရင်းဘာသာစကားဖြင့် SRT ဖိုင် ရပါပြီဗျာ (Filtered)။**",
            reply_to_message_id=v_msg.id
        )
        await client.send_message(user_id, f"✅ Task {job_id[-4:]} အောင်မြင်စွာ ပြီးဆုံးပါပြီ။")

    except asyncio.CancelledError: pass
    except Exception as e: await client.send_message(user_id, f"❌ Task {job_id[-4:]} Error: {e}")
    finally:
        if not ud.get("is_leeched") and os.path.exists(v_path): os.remove(v_path)
        if ud.get("is_leeched") and ud.get("leeched_file_path") and os.path.exists(ud.get("leeched_file_path")): os.remove(ud.get("leeched_file_path"))
        if os.path.exists(audio_path): os.remove(audio_path)
        if os.path.exists(srt_path): os.remove(srt_path)

        remove_job(user_id, job_id)
        if job_id in cancel_flags: del cancel_flags[job_id]
        await force_new_status_message(user_id)
        asyncio.create_task(update_central_status(user_id, client))

async def process_standalone_srt(client, user_id, ud, job_id):
    msg = ud["srt_msg"]
    input_srt_name = f"standalone_in_{job_id}.srt"
    output_srt = f"standalone_out_{job_id}.srt"
    actual_input_path = None
    try:
        actual_input_path = await msg.download(
            file_name=input_srt_name,
            progress=global_progress,
            progress_args=(client, user_id, job_id, "Downloading SRT", time.time())
        )

        success, err = await translate_existing_srt(actual_input_path, output_srt, client, user_id, job_id)
        if cancel_flags.get(job_id): raise asyncio.CancelledError()

        if success and os.path.exists(output_srt):
            ACTIVE_JOBS[user_id][job_id]['status'] = 'Uploading Output'
            asyncio.create_task(update_central_status(user_id, client))

            original_name = msg.document.file_name if msg.document else "Translated.srt"
            if original_name.lower().endswith(".srt"):
                new_file_name = original_name[:-4] + "-mmsub.srt"
            else:
                new_file_name = "Translated-mmsub.srt"

            await msg.reply_document(
                document=output_srt,
                file_name=new_file_name,
                caption="✅ **AI ဖြင့် ဘာသာပြန်ထားသော Subtitle**"
            )
            await client.send_message(user_id, f"✅ Task {job_id[-4:]} ပြီးဆုံးပါပြီ။")
        else:
            await client.send_message(user_id, f"❌ Task {job_id[-4:]} Error:\n{err}")
    except asyncio.CancelledError: pass
    except Exception as e: await client.send_message(user_id, f"❌ Task {job_id[-4:]} Error: {e}")
    finally:
        if actual_input_path and os.path.exists(actual_input_path): os.remove(actual_input_path)
        if os.path.exists(output_srt): os.remove(output_srt)
        remove_job(user_id, job_id)
        await force_new_status_message(user_id)
        asyncio.create_task(update_central_status(user_id, client))

async def process_everything(client, user_id, ud, job_id):
    v_msg = ud["video_msg"]
    trim_end = ud.get("trim_end", 0)
    use_crop = ud.get("use_crop", False)
    use_logo = ud.get("use_logo", False)
    custom_logo = ud.get("custom_logo", False)
    use_sub = ud.get("use_sub", False)
    auto_sub = ud.get("auto_sub", False)
    use_banner = ud.get("use_banner", False)
    custom_banner = ud.get("custom_banner", False)
    has_existing_banner = ud.get("has_existing_banner", False)
    existing_banner_times = ud.get("existing_banner_times", [])
    use_custom_front = ud.get("use_custom_front", False)
    use_default_front = ud.get("use_default_front", False)
    use_outro = ud.get("use_outro", False)
    use_thumb = ud.get("use_thumb", False)

    v_path = f"video_{job_id}.mp4"
    s_path = f"sub_{job_id}.srt"
    out_path = f"encoded_{job_id}.mp4"
    banner_path = BANNER_FILE
    logo_path = LOGO_FILE

    try:
        async with get_encode_lock():
            if cancel_flags.get(job_id): raise asyncio.CancelledError()
            
            ACTIVE_JOBS[user_id][job_id]['status'] = 'Preparing & Downloading'
            asyncio.create_task(update_central_status(user_id, client))

            if use_logo and custom_logo and "logo_msg" in ud:
                custom_logo_path = f"custom_logo_{job_id}.png"
                logo_path = await ud["logo_msg"].download(file_name=custom_logo_path)
                ud["downloaded_logo"] = logo_path

            if use_logo and not custom_logo and not os.path.exists(LOGO_FILE): raise Exception("`logo.png` မတွေ့ပါ။")
            if use_default_front and not os.path.exists(PREROLL_FILE): raise Exception("`preroll.mp4` မတွေ့ပါ။")
            if use_outro and not os.path.exists(OUTRO_FILE): raise Exception("`outro.mp4` မတွေ့ပါ။")

            if use_banner and custom_banner and "banner_msg" in ud:
                ext = ".mp4" if getattr(ud["banner_msg"], "video", None) else ".gif"
                custom_banner_path = f"custom_banner_{job_id}{ext}"
                banner_path = await ud["banner_msg"].download(file_name=custom_banner_path)
                ud["downloaded_banner"] = banner_path

            if use_banner and not custom_banner and not os.path.exists(BANNER_FILE): raise Exception("`banner.mp4/gif` မတွေ့ပါ။")

            if use_banner and banner_path.lower().endswith('.mp4'):
                silent_banner = f"silent_banner_{job_id}.mp4"
                os.system(f'ffmpeg -y -i "{banner_path}" -c:v copy -an "{silent_banner}" -loglevel quiet')
                if os.path.exists(silent_banner):
                    banner_path = silent_banner
                    ud["silent_banner"] = silent_banner

            if use_custom_front and "front_msg" in ud:
                ud["custom_front_path"] = await ud["front_msg"].download(file_name=ud["custom_front_path"])

            if ud.get("is_leeched"):
                v_path = ud.get("leeched_file_path")
            else:
                v_path = await v_msg.download(
                    file_name=v_path,
                    progress=global_progress,
                    progress_args=(client, user_id, job_id, "Downloading Video", time.time())
                )

            if cancel_flags.get(job_id): raise asyncio.CancelledError()
            ACTIVE_JOBS[user_id][job_id]['status'] = 'Analyzing Video'
            asyncio.create_task(update_central_status(user_id, client))

            file_size_mb = os.path.getsize(v_path) / (1024 * 1024)
            original_duration = await get_video_duration(v_path)
            if original_duration <= 0: original_duration = 3600

            trim_start_val = ud.get("trim_start", -1)
            if trim_start_val == 0: trim_start = get_smart_trim_time(v_path)
            elif trim_start_val == -1: trim_start = 0
            else: trim_start = trim_start_val

            actual_duration = original_duration - trim_start - trim_end
            if actual_duration <= 0: actual_duration = original_duration

            if use_sub:
                trans_success = True
                trans_err = ""
                if not auto_sub and "sub_msg" in ud:
                    s_path = await ud["sub_msg"].download(file_name=s_path)
                    if ud.get("translate_uploaded_sub", False):
                        trans_success, trans_err = await translate_existing_srt(s_path, s_path, client, user_id, job_id)
                elif auto_sub:
                    trans_success, trans_err = await generate_mm_subtitle(v_path, s_path, client, user_id, job_id, trim_start, actual_duration)

                if not trans_success:
                    await client.send_message(user_id, f"⚠️ **Task {job_id[-4:]} Subtitle Error:**\n{trans_err[:200]}\n*(မူလအတိုင်း ဆက်လုပ်နေပါသည်)*")
                    await asyncio.sleep(3)

            target_mb = file_size_mb * 0.90
            if target_mb > 1450: 
                target_mb = 1450

            video_bitrate_k = int((target_mb * 8192) / actual_duration) - 192
            if video_bitrate_k < 800: video_bitrate_k = 800
            
            b_v = f"-b:v {video_bitrate_k}k -maxrate {int(video_bitrate_k*1.5)}k -bufsize {int(video_bitrate_k*2)}k -profile:v high"

            if cancel_flags.get(job_id): raise asyncio.CancelledError()
            ACTIVE_JOBS[user_id][job_id]['status'] = 'Starting FFmpeg'
            ACTIVE_JOBS[user_id][job_id]['total'] = actual_duration
            asyncio.create_task(update_central_status(user_id, client))

            main_has_audio = await has_audio(v_path)
            
            dur_cf = 0; cf_has_audio = False
            if use_custom_front:
                dur_cf = await get_video_duration(ud.get("custom_front_path"))
                cf_has_audio = await has_audio(ud.get("custom_front_path"))
                if dur_cf <= 0: dur_cf = 0.1
                
            dur_df = 0; df_has_audio = False
            if use_default_front:
                dur_df = await get_video_duration(PREROLL_FILE)
                df_has_audio = await has_audio(PREROLL_FILE)
                if dur_df <= 0: dur_df = 0.1
                
            dur_out = 0; out_has_audio = False
            if use_outro:
                dur_out = await get_video_duration(OUTRO_FILE)
                out_has_audio = await has_audio(OUTRO_FILE)
                if dur_out <= 0: dur_out = 0.1
                
            total_enc_dur = actual_duration + dur_cf + dur_df + dur_out

            cmd = f'ffmpeg -y '
            input_idx = 0
            filters_list = []

            idx_cf = -1
            if use_custom_front:
                cmd += f'-i "{ud.get("custom_front_path")}" '
                idx_cf = input_idx; input_idx += 1

            idx_df = -1
            if use_default_front:
                cmd += f'-i "{PREROLL_FILE}" '
                idx_df = input_idx; input_idx += 1

            trim_str = f"-ss {trim_start} " if trim_start > 0 else ""
            dur_str = f"-t {actual_duration} " if (trim_end > 0 and actual_duration > 0) else ""
            cmd += f'{trim_str}{dur_str}-i "{v_path}" '
            idx_main = input_idx; input_idx += 1

            idx_logo = -1
            if use_logo:
                cmd += f'-i "{logo_path}" '; idx_logo = input_idx; input_idx += 1

            idx_outro = -1
            if use_outro:
                cmd += f'-i "{OUTRO_FILE}" '; idx_outro = input_idx; input_idx += 1

            idx_banner = -1
            if use_banner:
                cmd += f'-stream_loop -1 -i "{banner_path}" '
                idx_banner = input_idx; input_idx += 1

            if use_custom_front:
                filters_list.append(f"[{idx_cf}:v:0]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[cf_v]")
                if cf_has_audio: filters_list.append(f"[{idx_cf}:a:0]aresample=48000[cf_a]")
                else: filters_list.append(f"anullsrc=r=48000:cl=stereo:d={dur_cf:.3f}[cf_a]")

            if use_default_front:
                filters_list.append(f"[{idx_df}:v:0]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[df_v]")
                if df_has_audio: filters_list.append(f"[{idx_df}:a:0]aresample=48000[df_a]")
                else: filters_list.append(f"anullsrc=r=48000:cl=stereo:d={dur_df:.3f}[df_a]")

            crop_filter = "crop=iw:ih-110:0:110," if use_crop else ""
            filters_list.append(f"[{idx_main}:v:0]{crop_filter}scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[main_scaled]")
            current_v = "main_scaled"

            if use_logo:
                filters_list.append(f"[{idx_logo}:v:0]scale=100:-1[logo_scaled]")
                filters_list.append(f"[{current_v}][logo_scaled]overlay=W-w-10:80[{current_v}_logo]")
                current_v = f"{current_v}_logo"

            if use_banner:
                banner_dur = await get_video_duration(banner_path)
                if banner_dur <= 0: banner_dur = 15.0

                enable_str = ""
                if has_existing_banner and existing_banner_times:
                    enable_str = "+".join([f"between(t,{t*60},{t*60+banner_dur})" for t in existing_banner_times])
                else:
                    if actual_duration >= 3600: intervals = [1800, 3600, 5400]
                    else: intervals = [600, 1200, 1800]
                    intervals = [i for i in intervals if i < actual_duration]
                    if intervals: enable_str = "+".join([f"between(t,{i},{i+banner_dur})" for i in intervals])

                if enable_str:
                    # 🚀 Banner ကို Main Video ရဲ့ အကျယ် (1920) အပြည့်ဖြစ်အောင် ဆွဲဆန့်မည် (-2 က အချိုးအစားမပျက်အောင် ထိန်းပေးသည်)
                    filters_list.append(f"[{idx_banner}:v:0]scale=1920:-2[banner_scaled]")
                    # 🚀 အောက်ခြေမှာ ဘယ်ညာအပြည့်ကပ်ပြီး ပေါ်စေရန် overlay=0:H-h ကို သုံးထားသည်
                    filters_list.append(f"[{current_v}][banner_scaled]overlay=0:H-h:enable='{enable_str}':shortest=1[{current_v}_banner]")
                    current_v = f"{current_v}_banner"

            if use_sub and os.path.exists(s_path):
                sub_style = ":force_style='Fontname=Zawgyi-One,PrimaryColour=&H0000FFFF,FontSize=22,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Bold=1'"
                filters_list.append(f"[{current_v}]subtitles={s_path}{sub_style}[{current_v}_sub]")
                current_v = f"{current_v}_sub"

            filters_list.append(f"[{current_v}]drawtext=fontfile={FONT_BOLD_FILE}:text='t.me/ocadults':x=30:y=30:fontsize=36:fontcolor=yellow:borderw=2:bordercolor=black,drawtext=fontfile={FONT_BOLD_FILE}:text='ocadults.net':x=w-tw-30:y=30:fontsize=36:fontcolor=yellow:borderw=2:bordercolor=black[main_v_final]")
            
            if main_has_audio: filters_list.append(f"[{idx_main}:a:0]aresample=48000:async=1[main_a_final]")
            else: filters_list.append(f"anullsrc=r=48000:cl=stereo:d={actual_duration:.3f}[main_a_final]")

            if use_outro:
                filters_list.append(f"[{idx_outro}:v:0]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[out_v]")
                if out_has_audio: filters_list.append(f"[{idx_outro}:a:0]aresample=48000[out_a]")
                else: filters_list.append(f"anullsrc=r=48000:cl=stereo:d={dur_out:.3f}[out_a]")

            concat_inputs = ""
            concat_count = 0
            if use_custom_front: concat_inputs += "[cf_v][cf_a]"; concat_count += 1
            if use_default_front: concat_inputs += "[df_v][df_a]"; concat_count += 1
            concat_inputs += "[main_v_final][main_a_final]"; concat_count += 1
            if use_outro: concat_inputs += "[out_v][out_a]"; concat_count += 1

            if concat_count > 1:
                filters_list.append(f"{concat_inputs}concat=n={concat_count}:v=1:a=1[outv][outa]")
                map_cmd = '-map "[outv]" -map "[outa]"'
            else: map_cmd = '-map "[main_v_final]" -map "[main_a_final]"'

            filter_complex = ";".join(filters_list)
            
            cmd += f'-filter_complex "{filter_complex}" {map_cmd} -c:v libx264 {b_v} -preset veryfast -c:a aac -b:a 192k "{out_path}"'

            process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            active_processes[job_id] = process

            last_update = 0
            enc_start = time.time()
            last_ffmpeg_output = ""

            while True:
                if cancel_flags.get(job_id):
                    process.terminate()
                    raise asyncio.CancelledError()

                chunk = await process.stderr.read(4096)
                if not chunk: break

                chunk_str = chunk.decode('utf-8', errors='replace')
                last_ffmpeg_output += chunk_str
                if len(last_ffmpeg_output) > 2000: last_ffmpeg_output = last_ffmpeg_output[-2000:]

                matches = re.findall(r"time=\s*(\d+:\d{2}:\d{2}[\.\d]*)", chunk_str)
                if matches and total_enc_dur > 0:
                    current_sec = get_seconds_from_time(matches[-1])
                    percentage = min((current_sec / total_enc_dur) * 100, 100)
                    now = time.time()

                    elapsed = now - enc_start
                    speed = current_sec / elapsed if elapsed > 0 else 0
                    eta_sec = (total_enc_dur - current_sec) / speed if speed > 0 else 0

                    ACTIVE_JOBS[user_id][job_id].update({
                        'status': 'Encoding Video',
                        'progress': percentage,
                        'current': current_sec,
                        'total': total_enc_dur,
                        'speed': speed * 1024 * 1024,
                        'eta': TimeFormatter(eta_sec * 1000)
                    })

                    if now - last_update >= 15:
                        last_update = now
                        asyncio.create_task(update_central_status(user_id, client))

            await process.wait()
            if cancel_flags.get(job_id): raise asyncio.CancelledError()

            if process.returncode != 0 or not os.path.exists(out_path):
                raise Exception(f"Encoding Error:\n\n`{last_ffmpeg_output[-800:]}`")

        if cancel_flags.get(job_id): raise asyncio.CancelledError()

        ACTIVE_JOBS[user_id][job_id]['status'] = 'Uploading to Telegram'
        asyncio.create_task(update_central_status(user_id, client))

        if ud.get("is_leeched"):
            original_name = os.path.basename(ud.get("leeched_file_path", "Leeched_Video"))
        else:
            if getattr(v_msg.video, "file_name", None): original_name = v_msg.video.file_name
            elif getattr(v_msg.document, "file_name", None): original_name = v_msg.document.file_name
            else: original_name = f"Video_{job_id}.mp4"

        caption = f"✅ **{original_name}**\n\n⚙️ Encoded by OC Admin\n📁 Original Size: {round(file_size_mb, 2)} MB\n🎯 Target Size: ~{round(target_mb, 2)} MB"
        thumb_to_use = ud.get("thumb_path") if use_thumb and os.path.exists(ud.get("thumb_path", "")) else None

        await client.send_video(
            chat_id=user_id,
            video=out_path,
            caption=caption,
            thumb=thumb_to_use,
            duration=int(total_enc_dur),
            width=1920,
            height=1080,
            reply_to_message_id=v_msg.id,
            progress=global_progress,
            progress_args=(client, user_id, job_id, "Uploading", time.time())
        )
        await client.send_message(user_id, f"✅ Task {job_id[-4:]} ({original_name}) အောင်မြင်စွာ ပြီးဆုံးပါပြီ။", reply_to_message_id=v_msg.id)

    except asyncio.CancelledError: pass
    except Exception as e: await client.send_message(user_id, f"❌ Task {job_id[-4:]} System Error: {e}")
    finally:
        files_to_delete = [out_path]
        if s_path and os.path.exists(s_path): files_to_delete.append(s_path)
        if not ud.get("is_leeched") and v_path and os.path.exists(v_path): files_to_delete.append(v_path)
        if ud.get("is_leeched") and ud.get("leeched_file_path") and os.path.exists(ud.get("leeched_file_path")): files_to_delete.append(ud.get("leeched_file_path"))
        if use_custom_front and "front_msg" in ud and os.path.exists(ud.get("custom_front_path", "")): files_to_delete.append(ud["custom_front_path"])
        if use_thumb and "custom_thumb_" in str(ud.get("thumb_path", "")) and os.path.exists(ud.get("thumb_path", "")): files_to_delete.append(ud["thumb_path"])
        if custom_banner and "downloaded_banner" in ud and os.path.exists(ud["downloaded_banner"]): files_to_delete.append(ud["downloaded_banner"])
        if "silent_banner" in ud and os.path.exists(ud["silent_banner"]): files_to_delete.append(ud["silent_banner"])
        if custom_logo and "downloaded_logo" in ud and os.path.exists(ud["downloaded_logo"]): files_to_delete.append(ud["downloaded_logo"])

        for f in files_to_delete:
            if os.path.exists(f):
                try: os.remove(f)
                except: pass

        remove_job(user_id, job_id)
        if job_id in cancel_flags: del cancel_flags[job_id]
        if job_id in active_processes: del active_processes[job_id]
        
        await force_new_status_message(user_id)
        asyncio.create_task(update_central_status(user_id, client))

# ==========================================
# ⚙️ FINAL HANDLER (TEXT URL LEECH)
# ==========================================
@app.on_message(filters.text)
async def handle_text(client, message):
    user_id = message.from_user.id
    text = message.text.strip()

    if text.startswith("/cancel_"):
        job_id = text.replace("/cancel_", "")
        if job_id in cancel_flags:
            cancel_flags[job_id] = True
            if job_id in active_processes:
                try: active_processes[job_id].terminate()
                except: pass
            await message.reply_text(f"❌ **Task {job_id[-4:]} ကို ရပ်တန့်လိုက်ပါပြီ။**")
            remove_job(user_id, job_id)
            await force_new_status_message(user_id)
            asyncio.create_task(update_central_status(user_id, client))
        else:
            await message.reply_text("⚠️ အဆိုပါ Task ရှာမတွေ့ပါ။ ပြီးသွားတာ ဒါမှမဟုတ် ပယ်ဖျက်ပြီးသား ဖြစ်နိုင်ပါတယ်။")
        return

    state = user_data.get(user_id, {}).get("state")

    if not state and re.match(URL_REGEX, text):
        if user_id in user_data: del user_data[user_id]

        user_data[user_id] = {"video_msg": message, "is_leeched": True}
        job_id = str(int(time.time() * 1000))

        await force_new_status_message(user_id)
        init_job(user_id, job_id, text)
        ACTIVE_JOBS[user_id][job_id]['status'] = 'Downloading Link'
        asyncio.create_task(update_central_status(user_id, client))

        dl_path_template = f"leech_{job_id}.%(ext)s"

        USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64 AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36)"
        cmd = f'yt-dlp --no-warnings --user-agent "{USER_AGENT}" -f "best" -o "{dl_path_template}" "{text}"'
        process = await asyncio.create_subprocess_shell(cmd)
        await process.wait()

        downloaded_files = glob.glob(f"leech_{job_id}.*")

        if not downloaded_files or process.returncode != 0:
            ACTIVE_JOBS[user_id][job_id]['status'] = 'Direct Link Mode'
            asyncio.create_task(update_central_status(user_id, client))

            ext = "mp4"
            if text.lower().endswith(".mkv"): ext = "mkv"
            elif text.lower().endswith(".ts"): ext = "ts"

            fallback_path = f"leech_{job_id}.{ext}"
            wget_cmd = f'wget -q --user-agent="{USER_AGENT}" -O "{fallback_path}" "{text}"'
            wget_process = await asyncio.create_subprocess_shell(wget_cmd)
            await wget_process.wait()

            downloaded_files = glob.glob(f"leech_{job_id}.*")

        if downloaded_files:
            user_data[user_id]["leeched_file_path"] = downloaded_files[0]
            user_data[user_id]["state"] = "ASK_VIDEO_ACTION"
            remove_job(user_id, job_id)
            await force_new_status_message(user_id)
            asyncio.create_task(update_central_status(user_id, client))

            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎥 Encode လုပ်မယ်", callback_data="action_encode")],
                [InlineKeyboardButton("📝 SRT ထုတ်မယ်", callback_data="action_extract_srt")],
                [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
            ])
            await message.reply_text("✅ **Server သို့ အောင်မြင်စွာ Download ဆွဲပြီးပါပြီ!**\n\n🎬 **ဒီ Video ကို ဘာလုပ်ချင်လဲ ရွေးပေးပါ-**", reply_markup=btns)
        else:
            remove_job(user_id, job_id)
            await force_new_status_message(user_id)
            asyncio.create_task(update_central_status(user_id, client))
            await message.reply_text("❌ **Link မှ Download ဆွဲ၍ မရပါ။ (Private Link သို့မဟုတ် Server ပိတ်ထားခြင်း ဖြစ်နိုင်ပါသည်)**")
            del user_data[user_id]
        return

    if user_id not in user_data: return

    if state == "ASK_TRIM_START_MANUAL":
        try:
            trim_start = float(text)
            user_data[user_id]["trim_start"] = trim_start
            user_data[user_id]["state"] = "ASK_TRIM_END"
            await message.reply_text("✂️ **ဗီဒီယို နောက်ဆုံး (End) ကနေ ဘယ်နှစ်စက္ကန့် ဖြတ်ထုတ်မလဲ?**\n(ဘာမှမဖြတ်လိုပါက `0` ဟုသာ ပို့ပါ)")
        except: await message.reply_text("⚠️ ဂဏန်းသာ ရိုက်ထည့်ပါ (ဥပမာ - 15, 20.5)")

    elif state == "ASK_TRIM_END":
        try:
            trim_end = float(text)
            user_data[user_id]["trim_end"] = trim_end
            user_data[user_id]["state"] = "ASK_CROP"
            btns = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ ဖြတ်ထုတ်မည် (Crop)", callback_data="crop_yes")],
                [InlineKeyboardButton("❌ မဖြတ်ပါ", callback_data="crop_no")],
                [InlineKeyboardButton("❌ Cancel Setup", callback_data=f"cancel_setup_{user_id}")]
            ])
            await message.reply_text("🔪 **အပေါ်ဘယ်/ညာက သူများစာတွေကို Crop နဲ့ လှီးထုတ်မလား?**", reply_markup=btns)
        except: await message.reply_text("⚠️ ဂဏန်းသာ ရိုက်ထည့်ပါ (ဥပမာ - 15, 0)")

    elif state == "WAITING_BANNER_MINUTES":
        try:
            mins = [int(x.strip()) for x in text.split(",") if x.strip().isdigit()]
            user_data[user_id]["existing_banner_times"] = mins
            await ask_thumb(message, user_id)
        except:
            await message.reply_text("⚠️ ကျေးဇူးပြု၍ မိနစ်များကို ဂဏန်းသက်သက်သာ ရေးပါ (ဥပမာ - 15, 45, 60)")

print("Ultimate Editor & Leech Bot Started...")
app.run()
