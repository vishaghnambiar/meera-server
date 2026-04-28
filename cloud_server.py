"""
MEERA Cloud Server
Telegram bot + Gemini AI command parser + WebSocket relay to PC.
Deploy this on Railway / Render (free tier).
Uses Google Gemini API — 100% FREE, no credit card needed.
Get your free key at: https://aistudio.google.com/app/apikey
"""

import asyncio
import json
import os
import tempfile
import logging
from pathlib import Path

import httpx
import google.generativeai as genai
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Header, HTTPException
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
WEBHOOK_URL      = os.getenv("WEBHOOK_URL", "")  # e.g. https://your-app.railway.app

app  = FastAPI()
bot  = Bot(token=TELEGRAM_TOKEN)

# Connected PC WebSocket
pc_ws: WebSocket | None = None

# Gemini client (FREE — 1500 requests/day)
genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    generation_config={"temperature": 0.1, "max_output_tokens": 300}
)


# ── Claude AI: parse intent → command ────────────────────────────────────────
SYSTEM_PROMPT = """
You are MEERA's command parser. Convert user requests into structured JSON commands for a Windows PC.

ALWAYS respond with ONLY a valid JSON object — no explanation, no markdown.

Format:
{
  "action": "<action_name>",
  "params": { ... },
  "reply": "<friendly reply from MEERA in first person, Indian female style, 1-2 sentences>"
}

Available actions and their params:
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
- open_folder: {"path": "path or shortcut like Desktop, Documents, Downloads"}
- run_command: {"cmd": "shell command"}

MEERA's personality in replies: warm, helpful, slightly formal, Indian female voice.
Examples: "Sure, I am taking the screenshot right away!", "Of course, opening YouTube for you!"

If unclear, pick the closest action.
"""

def parse_command(user_text: str) -> dict:
    """Ask Gemini (free) to parse the user's text into a command JSON."""
    try:
        prompt = SYSTEM_PROMPT + f"\n\nUser request: {user_text}"
        response = gemini.generate_content(prompt)
        raw = response.text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip().rstrip("```")
        return json.loads(raw)
    except Exception as e:
        log.error(f"Gemini parse error: {e}")
        return {
            "action": "unknown",
            "params": {},
            "reply": "Sorry, I could not understand that command. Could you please try again?"
        }


# ── Voice transcription (faster-whisper, runs on PC side) ─────────────────────
# The cloud server downloads the Telegram voice file and sends raw bytes to PC.
# PC transcribes using faster-whisper (local, free).

async def download_voice(file_id: str) -> bytes:
    """Download a voice file from Telegram."""
    file = await bot.get_file(file_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(file.file_path)
        return resp.content


# ── WebSocket: PC connection ──────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    x_pc_secret: str = Header(None)
):
    global pc_ws
    if x_pc_secret != PC_SECRET:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    pc_ws = websocket
    log.info("PC connected via WebSocket.")

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "result":
                # PC finished executing — nothing to do here unless we want to log
                log.info(f"PC result: {msg.get('text','')}")
            elif msg.get("type") == "pong":
                pass
    except WebSocketDisconnect:
        log.warning("PC disconnected.")
        pc_ws = None


async def send_to_pc(command: dict, user_text: str) -> bool:
    """Forward a command to the connected PC."""
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
        log.error(f"Failed to send to PC: {e}")
        pc_ws = None
        return False


# ── Telegram handlers ─────────────────────────────────────────────────────────
async def check_auth(update: Update) -> bool:
    """Only allow configured user IDs."""
    if not ALLOWED_USER_IDS:
        return True  # No restriction if not configured
    return update.effective_user.id in ALLOWED_USER_IDS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *MEERA is online!*\n\n"
        "I am your personal AI assistant. Just tell me what to do on your PC — "
        "you can type or send a voice message.\n\n"
        "Examples:\n"
        "• _Take a screenshot_\n"
        "• _Open YouTube_\n"
        "• _Shut down the PC_\n"
        "• _What time is it?_\n"
        "• _Search for Python tutorials_\n\n"
        "Make sure your PC agent is running!",
        parse_mode="Markdown"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "🟢 PC is connected." if pc_ws else "🔴 PC is not connected. Start meera_ui.py on your PC."
    await update.message.reply_text(status)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        await update.message.reply_text("⛔ Unauthorised user.")
        return

    user_text = update.message.text
    await update.message.chat.send_action("typing")

    if pc_ws is None:
        await update.message.reply_text(
            "⚠️ Your PC is not connected. Please make sure MEERA is running on your PC."
        )
        return

    parsed = parse_command(user_text)
    reply  = parsed.get("reply", "On it!")
    command = {"action": parsed.get("action"), "params": parsed.get("params", {})}

    sent = await send_to_pc(command, user_text)
    if sent:
        await update.message.reply_text(f"✅ {reply}")
    else:
        await update.message.reply_text("⚠️ Could not reach your PC. Is the agent running?")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_auth(update):
        await update.message.reply_text("⛔ Unauthorised user.")
        return

    if pc_ws is None:
        await update.message.reply_text("⚠️ PC not connected.")
        return

    await update.message.reply_text("🎤 Heard you! Transcribing...")

    # Download voice file
    voice = update.message.voice
    audio_bytes = await download_voice(voice.file_id)

    # Send audio to PC for transcription (faster-whisper runs locally)
    try:
        await pc_ws.send_text(json.dumps({
            "type": "transcribe_and_execute",
            "audio_b64": __import__('base64').b64encode(audio_bytes).decode(),
        }))
        await update.message.reply_text("🔄 Processing your voice command on your PC...")
    except Exception as e:
        log.error(f"Voice relay error: {e}")
        await update.message.reply_text("⚠️ Error relaying voice message.")


# ── Telegram Webhook ──────────────────────────────────────────────────────────
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


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global tg_app

    tg_app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .updater(None)   # webhook mode
        .build()
    )

    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("status", cmd_status))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    tg_app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    await tg_app.initialize()
    await tg_app.start()

    # Set webhook
    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL}/telegram"
        await bot.set_webhook(webhook_url)
        log.info(f"Webhook set to {webhook_url}")
    else:
        log.warning("WEBHOOK_URL not set — webhook not configured.")


@app.on_event("shutdown")
async def shutdown():
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()
