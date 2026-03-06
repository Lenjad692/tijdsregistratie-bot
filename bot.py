import os
import logging
import asyncio
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import openai
import gspread
from google.oauth2.service_account import Credentials
import json
import re
import tempfile
import httpx

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Config via environment variables ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

openai.api_key = OPENAI_API_KEY
client = openai.OpenAI(api_key=OPENAI_API_KEY)

# --- Google Sheets setup ---
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    return sh.sheet1

# --- Transcribe voice message ---
async def transcribe_voice(file_path: str) -> str:
    with open(file_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="nl"
        )
    return transcript.text

# --- Parse transcript with GPT ---
async def parse_timeentry(transcript: str, date_str: str) -> list[dict]:
    prompt = f"""
Je bent een assistent die tijdsregistraties verwerkt. De gebruiker heeft gesproken en gezegd:

"{transcript}"

Datum van vandaag: {date_str}

Extraheer alle tijdsvermeldingen. Voor elke vermelding geef je:
- klant: naam van de klant
- beschrijving: korte omschrijving van het werk
- minuten: aantal minuten (als uren gezegd worden, vermenigvuldig met 60; als niet duidelijk, schat redelijk)

Antwoord ALLEEN met een JSON array, geen uitleg:
[{{"klant": "...", "beschrijving": "...", "minuten": 60}}]

Als er meerdere klanten zijn, geef meerdere objecten in de array.
Als iets niet duidelijk is, maak een redelijke schatting.
"""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    raw = response.choices[0].message.content.strip()
    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    return json.loads(raw)

# --- Write to Google Sheet ---
def write_to_sheet(entries: list[dict], date_str: str, transcript: str):
    sheet = get_sheet()
    for entry in entries:
        row = [
            date_str,
            entry.get("klant", ""),
            entry.get("minuten", 0),
            entry.get("beschrijving", ""),
            transcript
        ]
        sheet.append_row(row)

# --- Daily reminder ---
async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="👋 Hé Lenja! Werkdag bijna gedaan!\n\nStuur me een 🎤 *voice bericht* en vertel me:\n- Voor welke klanten heb je gewerkt?\n- Wat heb je gedaan?\n- Hoeveel uur/minuten?\n\nIk zet het netjes in je sheet! 📊",
        parse_mode="Markdown"
    )

# --- Handle voice messages ---
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Only respond to authorized user
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    await update.message.reply_text("🎧 Berichtje ontvangen, even verwerken...")

    try:
        # Download voice file
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        await tg_file.download_to_drive(tmp_path)

        # Transcribe
        transcript = await transcribe_voice(tmp_path)
        os.unlink(tmp_path)

        await update.message.reply_text(f"📝 Ik hoorde:\n_{transcript}_", parse_mode="Markdown")

        # Parse
        date_str = datetime.now().strftime("%-d-%-m-%Y")
        entries = await parse_timeentry(transcript, date_str)

        # Confirm with user
        confirm_text = "✅ Dit ga ik invullen:\n\n"
        for e in entries:
            uren = e['minuten'] / 60
            confirm_text += f"• *{e['klant']}* — {e['beschrijving']} ({e['minuten']} min / {uren:.1f}u)\n"

        await update.message.reply_text(confirm_text, parse_mode="Markdown")

        # Write to sheet
        write_to_sheet(entries, date_str, transcript)

        await update.message.reply_text("🎉 Perfect, staat in je sheet!")

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ Oeps, er ging iets mis: {str(e)}\n\nProbeer opnieuw!")

# --- Handle text messages too (optional convenience) ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    text = update.message.text
    if text.startswith("/start"):
        await update.message.reply_text(
            "👋 Hallo Lenja! Ik ben je tijdsregistratie-bot.\n\nStuur me elke dag om 17:00 een 🎤 voice bericht met wat je gedaan hebt, en ik zet het in je Google Sheet!\n\nJe kan ook nu al een voice bericht sturen om het te testen. 😊"
        )
        return

    # Also allow text input as fallback
    try:
        await update.message.reply_text("📝 Tekstinvoer ontvangen, even verwerken...")
        date_str = datetime.now().strftime("%-d-%-m-%Y")
        entries = await parse_timeentry(text, date_str)

        confirm_text = "✅ Dit ga ik invullen:\n\n"
        for e in entries:
            uren = e['minuten'] / 60
            confirm_text += f"• *{e['klant']}* — {e['beschrijving']} ({e['minuten']} min / {uren:.1f}u)\n"

        await update.message.reply_text(confirm_text, parse_mode="Markdown")
        write_to_sheet(entries, date_str, text)
        await update.message.reply_text("🎉 Perfect, staat in je sheet!")

    except Exception as e:
        await update.message.reply_text(f"❌ Oeps: {str(e)}")

# --- Main ---
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Handlers
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))

    # Daily reminder at 17:00 (UTC+1 = 16:00 UTC)
    app.job_queue.run_daily(
        send_daily_reminder,
        time=datetime.strptime("16:00", "%H:%M").time(),
        days=(0, 1, 2, 3, 4)  # Mon-Fri only
    )

    logger.info("🤖 Bot gestart!")
    app.run_polling()

if __name__ == "__main__":
    main()
