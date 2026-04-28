"""
MEERA Cloud Server
Telegram bot + Gemini AI (via REST API, no heavy libraries) + WebSocket relay to PC.
Deploy on Render free tier.
"""

import asyncio
import json
import os
import logging
import base64

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Header
from fastapi.responses import JSONResponse
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("MEERA")

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
ALLOWED_USER_IDS = set(map(int, os.getenv("ALLOWED_USER_IDS", "").split(",") if os.getenv("ALLOWED_USER_IDS") else []))
PC_SECRET        = os.getenv("PC_SECRET", "meera-secret-2024")
WEBHOOK_URL      = os.getenv("WEBHOOK_URL", "")

def get_gemini_url():
    key = os.getenv("GEMINI_API_KEY", "")
    return f"https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash:generateContent?key={key}"

app = FastAPI()
bot = Bot(token=TELEGRAM_TOKEN)
pc_ws: WebSocket | None = None

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are MEERA's command parser. Convert user requests into structured JSON commands for a Windows PC.

ALWAYS respond with ONLY a valid JSON object — no explanation, no markdown, no code fences.

Format:
{
  "action": "<action_name>",
  "params": {},
  "reply": "<friendly reply from MEERA, warm Indian female style, 1-2 sentences>"
}

Available actions:
- screenshot: {}
- open_website: {"url": "full url"}
- open_app: {"app": "app name"}
- shutdown: {"delay": 10}
- restart: {"delay": 10}
- sleep: {}
- cancel_shutdown: {}
- volume_up: {}
- volume_down: {}
- volume_mute: {}
- set_volume: {"level": 0-100}
- media_play_pause: {}
- media_next: {}
- media_prev: {}
- type_text: {"text": "..."}
- web_search: {"query": "..."}
- get_time: {}
- get_battery: {}
- lock_pc: {}
- system_info: {}
- open_folder: {"path": "Desktop/Documents/Downloads"}

MEERA reply examples: "Sure, taking the screenshot right away!", "Of course, opening YouTube for you!"
"""

# ── Gemini via REST (no heavy library, works on any Python version) ───────────
async def parse_command(user_text: str) -> dict:
    prompt = SYSTEM_PROMPT + f"\n\nUser request: {user_text}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 300}
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(get_gemini_url(), json=payload)
            data = resp.json()
            raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            # Strip markdown fences if present
            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip().rstrip("`").strip()
            return json.loads(raw)
    except Exception as e:
        log.error(f"Gemini error: {e}")
        return {
            "action": "unknown",
            "params": {},
            "reply": "Sorry, I could not understand that. Please try again!"
        }

# ── Voice download ────────────────────────────────────────────────────────────
async def download_voice(file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(file.file_path)
        return resp.content

# ── WebSocket: PC connection ──────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, x_pc_secret: str = Header(None)):
    global pc_ws
    if x_pc_secret != PC_SECRET:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    pc_ws = websocket
    log.info("PC connected.")
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "result":
                log.info(f"PC result: {msg.get('text','')}")
    except WebSocketDisconnect:
        log.warning("PC disconnected.")
        pc_ws = None

async def send_to_pc(command: dict, user_text: str) -> bool:
    global pc_ws
    if pc_ws is None:
        return False
    try:
        await pc_ws.send_text(json.dumps({
            "type": "command",
            "command": command,
            "user_text": user_text
        }))
        return True
    except Exception as e:
        log.error(f"Send to PC failed: {e}")
        pc_ws = None
        return False

# ── Telegram handlers ─────────────────────────────────────────────────────────
async def check_auth(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return update.effective_user.id in ALLOWED_USER_IDS

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *MEERA is online!*\n\n"
        "I am your personal AI assistant. Tell me what to do on your PC!\n\n"
        "Examples:\n"
        "• Take a screenshot\n"
        "• Open YouTube\n"
        "• Shut down the PC\n"
        "• What time is it?\n"
        "• Search for cricket score\n\n"
        "Make sure MEERA is running on your PC!",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "🟢 PC is connected!" if pc_ws else "🔴 PC not connected. Run meera_ui.py on your PC."
    await update.message.reply_text(status)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        await update.message.reply_text("⛔ Unauthorised.")
        return
    if pc_ws is None:
        await update.message.reply_text("⚠️ Your PC is not connected. Please start MEERA on your PC first.")
        return

    user_text = update.message.text
    await update.message.chat.send_action("typing")

    parsed  = await parse_command(user_text)
    reply   = parsed.get("reply", "On it!")
    command = {"action": parsed.get("action"), "params": parsed.get("params", {})}

    sent = await send_to_pc(command, user_text)
    if sent:
        await update.message.reply_text(f"✅ {reply}")
    else:
        await update.message.reply_text("⚠️ Could not reach your PC. Is MEERA running?")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        return
    if pc_ws is None:
        await update.message.reply_text("⚠️ PC not connected.")
        return
    await update.message.reply_text("🎤 Got your voice message! Processing...")
    audio_bytes = await download_voice(update.message.voice.file_id)
    try:
        await pc_ws.send_text(json.dumps({
            "type": "transcribe_and_execute",
            "audio_b64": base64.b64encode(audio_bytes).decode(),
        }))
    except Exception as e:
        await update.message.reply_text("⚠️ Error sending voice to PC.")

# ── Webhook ───────────────────────────────────────────────────────────────────
tg_app: Application | None = None

@app.post("/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot)
    await tg_app.process_update(update)
    return JSONResponse({"ok": True})

@app.get("/health")
async def health():
    return {"status": "ok", "pc_connected": pc_ws is not None}

@app.on_event("startup")
async def startup():
    global tg_app
    tg_app = Application.builder().token(TELEGRAM_TOKEN).updater(None).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("status", cmd_status))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    tg_app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    await tg_app.initialize()
    await tg_app.start()
    if WEBHOOK_URL:
        await bot.set_webhook(f"{WEBHOOK_URL}/telegram")
        log.info(f"Webhook set to {WEBHOOK_URL}/telegram")

@app.on_event("shutdown")
async def shutdown():
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
