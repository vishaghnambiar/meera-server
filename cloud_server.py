"""
MEERA Cloud Server - Clean Version
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

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
ALLOWED_USER_IDS = set(map(int, os.getenv("ALLOWED_USER_IDS", "").split(",") if os.getenv("ALLOWED_USER_IDS") else []))
PC_SECRET        = os.getenv("PC_SECRET", "meera-secret-2024")
WEBHOOK_URL      = os.getenv("WEBHOOK_URL", "")

app = FastAPI()
bot = Bot(token=TELEGRAM_TOKEN)
pc_ws = None

def get_gemini_url():
    key = os.getenv("GEMINI_API_KEY", GEMINI_API_KEY)
    return f"https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash:generateContent?key={key}"

async def parse_command(user_text):
    prompt = (
        "Reply ONLY with valid JSON. No explanation, no markdown, no extra text.\n"
        "Format: {\"action\": \"open_website\", \"params\": {\"url\": \"https://youtube.com\"}, \"reply\": \"Opening YouTube!\"}\n\n"
        "Actions: screenshot, open_website, open_app, shutdown, restart, sleep, "
        "cancel_shutdown, volume_up, volume_down, volume_mute, media_play_pause, "
        "media_next, media_prev, type_text, web_search, get_time, get_battery, "
        "lock_pc, system_info, open_folder\n\n"
        "Reply style: warm Indian female assistant.\n\n"
        "User request: " + user_text
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 300}
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(get_gemini_url(), json=payload)
            data = resp.json()
            parts = data["candidates"][0]["content"]["parts"]
            raw = ""
            for part in parts:
                text = part.get("text", "")
                if "{" in text and "action" in text:
                    raw = text
                    break
            if not raw:
                raw = parts[-1].get("text", "")
            raw = raw.strip()
            if "```" in raw:
                start = raw.find("```") + 3
                if raw[start:start+4] == "json":
                    start += 4
                end = raw.rfind("```")
                raw = raw[start:end]
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                raw = raw[start:end]
            return json.loads(raw.strip())
    except Exception as e:
        log.error(f"Gemini error: {e}")
        return {"action": "unknown", "params": {}, "reply": "Sorry, I could not understand that. Please try again!"}

async def download_voice(file_id):
    file = await bot.get_file(file_id)
    async with httpx.AsyncClient() as client:
        resp = await client.get(file.file_path)
        return resp.content

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

async def send_to_pc(command, user_text):
    global pc_ws
    if pc_ws is None:
        return False
    try:
        await pc_ws.send_text(json.dumps({"type": "command", "command": command, "user_text": user_text}))
        return True
    except Exception as e:
        log.error(f"Send to PC failed: {e}")
        pc_ws = None
        return False

async def check_auth(update):
    if not ALLOWED_USER_IDS:
        return True
    return update.effective_user.id in ALLOWED_USER_IDS

async def cmd_start(update: Update, context):
    await update.message.reply_text(
        "Hi! I am MEERA, your personal AI assistant!\n\n"
        "Tell me what to do on your PC:\n"
        "- Open YouTube\n- Take a screenshot\n- Shut down PC\n"
        "- What time is it?\n- Search Google for cricket score\n\n"
        "Make sure MEERA is running on your PC!"
    )

async def cmd_status(update: Update, context):
    status = "PC is connected!" if pc_ws else "PC not connected. Run meera_ui.py on your PC."
    await update.message.reply_text(status)

async def handle_text(update: Update, context):
    if not await check_auth(update):
        await update.message.reply_text("Unauthorised.")
        return
    if pc_ws is None:
        await update.message.reply_text("Your PC is not connected. Please start MEERA on your PC first.")
        return
    user_text = update.message.text
    await update.message.chat.send_action("typing")
    parsed = await parse_command(user_text)
    reply = parsed.get("reply", "On it!")
    command = {"action": parsed.get("action"), "params": parsed.get("params", {})}
    sent = await send_to_pc(command, user_text)
    if sent:
        await update.message.reply_text(reply)
    else:
        await update.message.reply_text("Could not reach your PC. Is MEERA running?")

async def handle_voice(update: Update, context):
    if not await check_auth(update):
        return
    if pc_ws is None:
        await update.message.reply_text("PC not connected.")
        return
    await update.message.reply_text("Got your voice message! Processing...")
    audio_bytes = await download_voice(update.message.voice.file_id)
    try:
        await pc_ws.send_text(json.dumps({
            "type": "transcribe_and_execute",
            "audio_b64": base64.b64encode(audio_bytes).decode(),
        }))
    except Exception as e:
        await update.message.reply_text("Error sending voice to PC.")

tg_app = None

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
