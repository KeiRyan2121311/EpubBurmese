"""
📚 Epub Gemini Translator — Telegram Bot
Railway.app တွင် 24/7 run လုပ်မည်
Owner-Only | Multi Gemini Key | Chapter-by-Chapter delivery
"""

import os
import re
import time
import threading
import logging
import traceback

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup
from google import genai
from google.genai import errors
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ─────────────────────────────────────────────
#  CONFIG  (Railway Environment Variables)
# ─────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID  = int(os.environ.get("OWNER_ID", "0"))

def load_gemini_keys() -> list:
    keys, i = [], 1
    while True:
        k = os.environ.get(f"GEMINI_KEY_{i}", "").strip()
        if not k:
            break
        keys.append(k)
        i += 1
    return keys

GEMINI_KEYS = load_gemini_keys()

# ─────────────────────────────────────────────
#  PATHS  (Railway persistent volume = /data)
# ─────────────────────────────────────────────
DATA_DIR   = "/data" if os.path.isdir("/data") else "/tmp"
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

EPUB_PATH     = os.path.join(UPLOAD_DIR, "novel.epub")
PROMPT_PATH   = os.path.join(UPLOAD_DIR, "prompt.txt")
GLOSSARY_PATH = os.path.join(UPLOAD_DIR, "consistent.txt")

# ─────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────
state = {
    "running":     False,
    "stop_flag":   False,
    "current_ch":  0,
    "total_ch":    0,
    "start_ch":    1,
    "end_ch":      0,
    "key_index":   0,
    "novel_title": "",
    "log":         [],
    "thread":      None,
}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  OWNER GUARD
# ─────────────────────────────────────────────
def owner_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            await update.message.reply_text("⛔ Owner Only Bot ဖြစ်သည်။")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def clean_name(name: str) -> str:
    if not name:
        return "Unknown_Novel"
    c = re.sub(r'[\\/*?:"<>|]', "", name).replace(" ", "_").strip("_")
    return c or "Unknown_Novel"


def split_chunks(text: str, max_chars: int = 4000) -> list:
    chunks, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) < max_chars:
            cur += para + "\n"
        else:
            if cur.strip():
                chunks.append(cur)
            cur = para + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def add_log(msg: str):
    state["log"].append(msg)
    if len(state["log"]) > 50:
        state["log"].pop(0)
    log.info(msg)


def translate_chunk(api_keys, text_chunk, prompt_text, glossary_text, key_idx):
    full_prompt = f"""You are a professional novel translator. Follow these instructions strictly:

---
{prompt_text}
---

Name / Glossary consistency (follow exactly):
---
{glossary_text}
---

Translate the following text into natural, fluid Burmese. Return ONLY the Burmese translation.

{text_chunk}"""

    total = len(api_keys)
    for attempt in range(total * 3):
        if state["stop_flag"]:
            raise InterruptedError("stop")

        idx = (key_idx + attempt) % total
        try:
            client = genai.Client(api_key=api_keys[idx])
            resp   = client.models.generate_content(
                model="gemini-2.5-flash", contents=full_prompt
            )
            return resp.text, idx
        except errors.APIError as e:
            wait = 15
            m = re.search(r"retry in ([\d\.]+)s", getattr(e, "message", ""))
            if m:
                wait = int(float(m.group(1))) + 5
            add_log(f"⚠️ Key #{idx+1} Limit → {wait}s wait")
            for _ in range(wait):
                if state["stop_flag"]:
                    raise InterruptedError("stop")
                time.sleep(1)
        except InterruptedError:
            raise
        except Exception as ex:
            add_log(f"❌ Key #{idx+1} error: {ex}")
            for _ in range(10):
                if state["stop_flag"]:
                    raise InterruptedError("stop")
                time.sleep(1)

    # All keys exhausted → 90s deep sleep
    add_log("🚨 All keys rate-limited. Deep sleep 90s...")
    for _ in range(90):
        if state["stop_flag"]:
            raise InterruptedError("stop")
        time.sleep(1)

    try:
        client = genai.Client(api_key=api_keys[0])
        resp   = client.models.generate_content(
            model="gemini-2.5-flash", contents=full_prompt
        )
        return resp.text, 0
    except Exception:
        return text_chunk, key_idx


# ─────────────────────────────────────────────
#  TRANSLATION THREAD
# ─────────────────────────────────────────────
def translation_worker(app, chat_id, start_ch, end_ch):
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def send_text(text):
        for i in range(0, len(text), 4000):
            await app.bot.send_message(
                chat_id=chat_id,
                text=text[i:i+4000],
                parse_mode="HTML"
            )

    async def send_epub(path, caption):
        with open(path, "rb") as f:
            await app.bot.send_document(
                chat_id=chat_id,
                document=f,
                caption=caption,
                parse_mode="HTML"
            )

    def post_text(text):
        loop.run_until_complete(send_text(text))

    def post_epub(path, caption):
        loop.run_until_complete(send_epub(path, caption))

    try:
        if not GEMINI_KEYS:
            post_text("❌ Gemini API Key မတွေ့ပါ။ Railway Variables တွင် GEMINI_KEY_1 ထည့်ပေးပါ။")
            return
        if not os.path.exists(EPUB_PATH):
            post_text("❌ Epub ဖိုင် မတွေ့ပါ။ Bot သို့ epub ဖိုင် Send ပေးပါ။")
            return
        if not os.path.exists(PROMPT_PATH):
            post_text("❌ prompt.txt မတွေ့ပါ။ Bot သို့ prompt.txt Send ပေးပါ။")
            return

        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            prompt_text = f.read()

        glossary_text = ""
        if os.path.exists(GLOSSARY_PATH):
            with open(GLOSSARY_PATH, "r", encoding="utf-8") as f:
                glossary_text = f.read()

        book     = epub.read_epub(EPUB_PATH)
        chapters = [it for it in book.get_items()
                    if it.get_type() == ebooklib.ITEM_DOCUMENT]
        try:
            title = book.get_metadata("DC", "title")[0][0]
        except Exception:
            title = "Unknown Novel"

        state["novel_title"] = title
        safe_title = clean_name(title)
        out_dir    = os.path.join(OUTPUT_DIR, safe_title)
        os.makedirs(out_dir, exist_ok=True)

        real_end = min(end_ch, len(chapters))
        selected = chapters[start_ch - 1: real_end]
        total    = len(selected)
        state["total_ch"] = total

        post_text(
            f"📚 <b>{title}</b>\n"
            f"🔢 Chapter {start_ch} → {real_end} ({total} chapters)\n"
            f"🔑 Keys: {len(GEMINI_KEYS)}\n\n"
            f"Translation စတင်ပါပြီ..."
        )

        key_idx = state["key_index"]

        for idx, chapter in enumerate(selected):
            if state["stop_flag"]:
                post_text("⛔ Translation ရပ်တန့်လိုက်ပြီ။")
                break

            ch_num = start_ch + idx
            state["current_ch"] = ch_num
            add_log(f"→ Ch {ch_num} | Key #{(key_idx % len(GEMINI_KEYS)) + 1}")

            html    = chapter.get_content().decode("utf-8")
            soup    = BeautifulSoup(html, "html.parser")
            raw_txt = soup.get_text()
            if not raw_txt.strip():
                continue

            chunks = split_chunks(raw_txt, max_chars=4000)
            translated_parts = []

            try:
                for c_idx, chunk in enumerate(chunks):
                    if state["stop_flag"]:
                        raise InterruptedError("stop")
                    add_log(
                        f"  Ch {ch_num} chunk {c_idx+1}/{len(chunks)} "
                        f"| Key #{(key_idx % len(GEMINI_KEYS)) + 1}"
                    )
                    part, key_idx = translate_chunk(
                        GEMINI_KEYS, chunk, prompt_text, glossary_text, key_idx
                    )
                    translated_parts.append(part)
                    state["key_index"] = key_idx
                    for _ in range(2):
                        if state["stop_flag"]:
                            raise InterruptedError("stop")
                        time.sleep(1)
            except InterruptedError:
                post_text("⛔ Translation ရပ်တန့်လိုက်ပြီ။")
                break

            final_text = "\n".join(translated_parts)
            html_paras = "".join(
                f"<p>{p.strip()}</p>"
                for p in final_text.split("\n") if p.strip()
            )

            ch_book  = epub.EpubBook()
            ch_book.set_title(f"{title} - Ch {ch_num} (Burmese)")
            ch_book.set_language("my")
            c_item = epub.EpubHtml(
                title=chapter.title or f"Chapter {ch_num}",
                file_name=f"ch_{ch_num}.xhtml",
                lang="my",
            )
            c_item.content = f"<html><body>{html_paras}</body></html>".encode("utf-8")
            ch_book.add_item(c_item)
            ch_book.toc   = (c_item,)
            ch_book.spine = ["nav", c_item]
            ch_book.add_item(epub.EpubNav())
            ch_book.add_item(epub.EpubNcx())

            epub_out = os.path.join(out_dir, f"Ch_{ch_num:04d}_translated.epub")
            epub.write_epub(epub_out, ch_book)
            add_log(f"✅ Ch {ch_num} saved")

            post_epub(
                epub_out,
                f"✅ <b>Chapter {ch_num}</b> ပြီးပါပြီ\n"
                f"📖 {title}\n"
                f"🔑 Key #{(key_idx % len(GEMINI_KEYS)) + 1} | "
                f"Progress: {idx+1}/{total}"
            )

            key_idx = (key_idx + 1) % len(GEMINI_KEYS)
            state["key_index"] = key_idx

        else:
            post_text(
                f"🎉 <b>Translation ပြီးဆုံးပါပြီ!</b>\n"
                f"Chapter {start_ch}–{real_end} အားလုံး ပေးပို့ပြီးဆုံးသည်။"
            )

    except Exception as ex:
        tb = traceback.format_exc()
        add_log(f"FATAL: {ex}\n{tb}")
        post_text(f"❌ Fatal error:\n<code>{ex}</code>")
    finally:
        state["running"]   = False
        state["stop_flag"] = False
        loop.close()


# ─────────────────────────────────────────────
#  COMMAND HANDLERS
# ─────────────────────────────────────────────

@owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Epub Gemini Translator Bot</b>\n\n/help ကို နှိပ်ပါ။",
        parse_mode="HTML"
    )


@owner_only
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Help</b>\n\n"
        "<b>━━ ဖိုင် Setup ━━</b>\n"
        "① epub ဖိုင် → Bot သို့ Send\n"
        "② prompt.txt → Bot သို့ Send\n"
        "③ consistent.txt → Bot သို့ Send (optional)\n\n"
        "<b>━━ Commands ━━</b>\n"
        "/translate 1 50  → Chapter 1–50 ဘာသာပြန်မည်\n"
        "/status          → လက်ရှိ အခြေအနေ\n"
        "/progress        → Progress bar\n"
        "/log             → နောက်ဆုံး Log\n"
        "/stop            → Force Stop\n"
        "/files           → ဖိုင်များ စစ်ဆေးမည်\n"
        "/keys            → API Keys အရေအတွက်",
        parse_mode="HTML"
    )


@owner_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = state
    if s["running"]:
        done = s["current_ch"] - s["start_ch"] + 1
        pct  = int(done / s["total_ch"] * 100) if s["total_ch"] > 0 else 0
        text = (
            f"🔄 <b>Translation လည်ပတ်နေသည်</b>\n\n"
            f"📖 {s['novel_title']}\n"
            f"📝 Chapter: {s['current_ch']} / {s['start_ch'] + s['total_ch'] - 1}\n"
            f"📊 Progress: {pct}%\n"
            f"🔑 Key: #{(s['key_index'] % len(GEMINI_KEYS)) + 1} / {len(GEMINI_KEYS)}"
        )
    else:
        text = (
            f"💤 <b>Bot အနားနေသည်</b>\n\n"
            f"🔑 Keys: {len(GEMINI_KEYS)}\n"
            f"📘 Epub:    {'✅' if os.path.exists(EPUB_PATH)     else '❌'}\n"
            f"📝 Prompt:  {'✅' if os.path.exists(PROMPT_PATH)   else '❌'}\n"
            f"📋 Glossary:{'✅' if os.path.exists(GLOSSARY_PATH) else '⚪ (optional)'}\n\n"
            f"/translate [start] [end] ဖြင့် စတင်ပါ"
        )
    await update.message.reply_text(text, parse_mode="HTML")


@owner_only
async def cmd_progress(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = state
    if not s["running"]:
        await update.message.reply_text("💤 Bot အနားနေသည်။")
        return
    done  = s["current_ch"] - s["start_ch"] + 1
    total = s["total_ch"]
    pct   = int(done / total * 100) if total > 0 else 0
    filled = int(pct / 5)
    bar   = "█" * filled + "░" * (20 - filled)
    await update.message.reply_text(
        f"📊 <b>Progress</b>\n"
        f"[{bar}] {pct}%\n"
        f"Chapter {s['current_ch']} / {s['start_ch'] + total - 1}",
        parse_mode="HTML"
    )


@owner_only
async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = state["log"][-20:] if state["log"] else ["(log empty)"]
    await update.message.reply_text(
        "<code>" + "\n".join(lines) + "</code>",
        parse_mode="HTML"
    )


@owner_only
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not state["running"]:
        await update.message.reply_text("💤 Translation မလည်ပတ်နေပါ။")
        return
    state["stop_flag"] = True
    await update.message.reply_text(
        "⛔ <b>Stop signal ပေးလိုက်ပြီ</b>\n"
        "လက်ရှိ Chunk ပြီးမှ ရပ်သွားပါမည်။",
        parse_mode="HTML"
    )


@owner_only
async def cmd_files(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📘 Epub:    {'✅' if os.path.exists(EPUB_PATH)     else '❌ မတွေ့'}\n"
        f"📝 Prompt:  {'✅' if os.path.exists(PROMPT_PATH)   else '❌ မတွေ့'}\n"
        f"📋 Glossary:{'✅' if os.path.exists(GLOSSARY_PATH) else '⚪ မတွေ့ (optional)'}"
    )


@owner_only
async def cmd_keys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    n = len(GEMINI_KEYS)
    if n == 0:
        await update.message.reply_text("❌ GEMINI_KEY_N Variable မတွေ့ပါ!")
        return
    masked = "\n".join(f"Key #{i+1}: {k[:10]}..." for i, k in enumerate(GEMINI_KEYS))
    await update.message.reply_text(
        f"🔑 <b>{n} key(s) loaded</b>\n{masked}",
        parse_mode="HTML"
    )


@owner_only
async def cmd_translate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if state["running"]:
        await update.message.reply_text(
            "⚠️ Translation လည်ပတ်နေဆဲ ဖြစ်သည်။\n/stop ဖြင့် ရပ်တန့်ပြီးမှ ထပ်ကြိုးစားပါ။"
        )
        return

    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("⚠️ အသုံးပြုနည်း: /translate [start] [end]\nဥပမာ: /translate 1 50")
        return

    try:
        start_ch = int(args[0])
        end_ch   = int(args[1])
        if start_ch < 1 or end_ch < start_ch:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Chapter နံပါတ် မမှန်ကန်ပါ။")
        return

    if not os.path.exists(EPUB_PATH):
        await update.message.reply_text("❌ Epub ဖိုင် မတွေ့ပါ။")
        return
    if not os.path.exists(PROMPT_PATH):
        await update.message.reply_text("❌ prompt.txt မတွေ့ပါ။")
        return
    if not GEMINI_KEYS:
        await update.message.reply_text("❌ GEMINI_KEY_N Variable မတွေ့ပါ!")
        return

    state.update({
        "running":    True,
        "stop_flag":  False,
        "start_ch":   start_ch,
        "end_ch":     end_ch,
        "current_ch": start_ch,
        "key_index":  0,
        "log":        [],
    })

    await update.message.reply_text(
        f"🚀 <b>Translation စတင်ပါပြီ!</b>\n"
        f"Chapter {start_ch} → {end_ch}\n\n"
        f"/status  /progress  /stop",
        parse_mode="HTML"
    )

    t = threading.Thread(
        target=translation_worker,
        args=(ctx.application, update.effective_chat.id, start_ch, end_ch),
        daemon=True,
    )
    t.start()
    state["thread"] = t


# ─────────────────────────────────────────────
#  FILE RECEIVE HANDLER
# ─────────────────────────────────────────────
@owner_only
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc      = update.message.document
    fname    = doc.file_name or ""
    fname_lo = fname.lower()

    if fname_lo.endswith(".epub"):
        save_path = EPUB_PATH
        label     = "📘 Epub ဖိုင်"
    elif fname_lo == "prompt.txt":
        save_path = PROMPT_PATH
        label     = "📝 prompt.txt"
    elif fname_lo in ("consistent.txt", "consistency.txt", "glossary.txt"):
        save_path = GLOSSARY_PATH
        label     = "📋 Glossary ဖိုင်"
    else:
        await update.message.reply_text(
            f"⚠️ မသိသော ဖိုင်: <code>{fname}</code>\n\n"
            "လက်ခံသော ဖိုင်များ:\n"
            "• <code>*.epub</code>\n"
            "• <code>prompt.txt</code>\n"
            "• <code>consistent.txt</code>",
            parse_mode="HTML"
        )
        return

    await update.message.reply_text(f"⬇️ {label} Download လုပ်နေပါသည်...")
    tg_file = await ctx.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(save_path)

    size_kb = os.path.getsize(save_path) // 1024
    await update.message.reply_text(
        f"✅ <b>{label} သိမ်းဆည်းပြီးပါပြီ</b>\n"
        f"📦 Size: {size_kb} KB",
        parse_mode="HTML"
    )

    if fname_lo.endswith(".epub"):
        try:
            book  = epub.read_epub(save_path)
            chs   = [it for it in book.get_items()
                     if it.get_type() == ebooklib.ITEM_DOCUMENT]
            try:
                title = book.get_metadata("DC", "title")[0][0]
            except Exception:
                title = "Unknown"
            await update.message.reply_text(
                f"📖 <b>{title}</b>\n"
                f"📝 အခန်းစုစုပေါင်း: {len(chs)} ခန်း\n\n"
                f"ဘာသာပြန်ရန်: /translate 1 {len(chs)}",
                parse_mode="HTML"
            )
        except Exception as e:
            await update.message.reply_text(f"⚠️ Epub ဖတ်ရာ error: {e}")


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN မတွေ့ပါ!")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("progress",  cmd_progress))
    app.add_handler(CommandHandler("log",       cmd_log))
    app.add_handler(CommandHandler("stop",      cmd_stop))
    app.add_handler(CommandHandler("files",     cmd_files))
    app.add_handler(CommandHandler("keys",      cmd_keys))
    app.add_handler(CommandHandler("translate", cmd_translate))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    log.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
