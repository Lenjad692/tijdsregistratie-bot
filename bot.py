import os
import logging
from datetime import datetime, date, timedelta
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import openai
import json
import re
import tempfile
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]

client = openai.OpenAI(api_key=OPENAI_API_KEY)

state = {
    "responded_today": False,
    "leave_until": None,
}

def is_on_leave():
    if state["leave_until"] is None:
        return False
    return date.today() <= state["leave_until"]

def reset_daily_state():
    state["responded_today"] = False

def write_to_sheet(entries, transcript):
    rows = []
    for entry in entries:
        rows.append({
            "datum": entry.get("datum", date.today().strftime("%-d-%-m-%Y")),
            "klant": entry.get("klant", ""),
            "minuten": entry.get("minuten", 0),
            "beschrijving": entry.get("beschrijving", ""),
            "transcript": transcript
        })
    payload = {"rows": rows}
    logger.info(f"Sending to webhook: {rows}")
    r = httpx.post(WEBHOOK_URL, json=payload, follow_redirects=True, timeout=30)
    logger.info(f"Webhook response: {r.status_code} {r.text[:200]}")
    if r.status_code != 200 or r.text.strip() != "OK":
        raise Exception(f"Webhook fout: {r.status_code} {r.text[:100]}")

async def transcribe_voice(file_path):
    with open(file_path, "rb") as f:
        transcript = client.audio.transcriptions.create(model="whisper-1", file=f, language="nl")
    return transcript.text

async def parse_timeentry(transcript):
    today = date.today()
    yesterday = today - timedelta(days=1)
    prompt = f"""
Je bent een assistent die tijdsregistraties verwerkt. De gebruiker zei:
"{transcript}"

Vandaag: {today.strftime("%-d-%-m-%Y")} ({today.strftime("%A")})
Gisteren: {yesterday.strftime("%-d-%-m-%Y")}

Extraheer ALLE tijdsvermeldingen:
- datum: "D-M-YYYY"
- klant: naam
- beschrijving: kort
- minuten: getal (uren x 60)

Antwoord ALLEEN met JSON array, geen uitleg, geen backticks:
[{{"datum": "6-3-2026", "klant": "...", "beschrijving": "...", "minuten": 60}}]
"""
    response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0)
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    return json.loads(raw)

async def detect_leave(transcript):
    today = date.today()
    prompt = f"""
Gebruiker stuurde: "{transcript}"
Vandaag: {today.strftime("%-d-%-m-%Y")}

Geeft de gebruiker aan dat ze NIET werken (verlof, vrij, ziek)?
Antwoord ALLEEN met JSON, geen uitleg, geen backticks:
- Verlof vandaag: {{"is_leave": true, "until": "{today.strftime("%-d-%-m-%Y")}"}}
- Verlof periode: {{"is_leave": true, "until": "D-M-YYYY"}}
- Geen verlof: {{"is_leave": false, "until": null}}
"""
    response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0)
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    return json.loads(raw)

async def process_message(update, transcript):
    leave_info = await detect_leave(transcript)
    if leave_info.get("is_leave"):
        until_str = leave_info.get("until")
        try:
            until_date = datetime.strptime(until_str, "%d-%m-%Y").date()
            state["leave_until"] = until_date
            state["responded_today"] = True
            await update.message.reply_text(f"🏖️ Begrepen! Geen herinneringen tot en met {until_date.strftime('%-d/%-m')}. Geniet! 😊")
        except Exception:
            state["responded_today"] = True
            await update.message.reply_text("🏖️ Begrepen, geen registratie vandaag!")
        return

    entries = await parse_timeentry(transcript)
    confirm_text = "✅ Dit ga ik invullen:\n\n"
    for e in entries:
        uren = e['minuten'] / 60
        confirm_text += f"• *{e['klant']}* ({e['datum']}) — {e['beschrijving']} ({e['minuten']} min / {uren:.1f}u)\n"
    await update.message.reply_text(confirm_text, parse_mode="Markdown")

    write_to_sheet(entries, transcript)
    state["responded_today"] = True
    await update.message.reply_text("🎉 Staat in je sheet!")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text("🎧 Even verwerken...")
    try:
        tg_file = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
        transcript = await transcribe_voice(tmp_path)
        os.unlink(tmp_path)
        await update.message.reply_text(f"📝 Ik hoorde:\n_{transcript}_", parse_mode="Markdown")
        await process_message(update, transcript)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Oeps: {str(e)}\n\nProbeer opnieuw!")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return
    text = update.message.text
    if text.startswith("/start"):
        await update.message.reply_text(
            "👋 Hallo Lenja! Ik ben je tijdsregistratie-bot.\n\nElke weekdag om 17:00 stuur ik je een herinnering.\nStuur een 🎤 voice bericht — meerdere klanten mag in één bericht!\n\n*Handige commando's:*\n• _'vandaag verlof'_ → geen herinnering vandaag\n• _'ik ben volgende week in verlof'_ → geen herinneringen die week\n• _'gisteren werkte ik voor...'_ → voegt toe met gisteren als datum",
            parse_mode="Markdown"
        )
        return
    await update.message.reply_text("📝 Verwerken...")
    try:
        await process_message(update, text)
    except Exception as e:
        await update.message.reply_text(f"❌ Oeps: {str(e)}")

async def send_daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    reset_daily_state()
    if is_on_leave():
        return
    await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="👋 Hé Lenja! Werkdag bijna gedaan!\n\nStuur me een 🎤 *voice bericht*:\n• Voor welke klanten werkte je?\n• Wat deed je?\n• Hoeveel uur?\n\nMeerdere klanten mag in één bericht! 📊", parse_mode="Markdown")

async def send_followup_reminder(context: ContextTypes.DEFAULT_TYPE):
    if state["responded_today"] or is_on_leave():
        return
    await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="⏰ Nog even! Geen tijdsregistratie ontvangen.\n\nWas je vrij? Stuur *'vandaag verlof'*. 😊", parse_mode="Markdown")

async def send_morning_reminder(context: ContextTypes.DEFAULT_TYPE):
    if is_on_leave():
        return
    yesterday = date.today() - timedelta(days=1)
    if yesterday.weekday() >= 5:
        return
    if not state["responded_today"]:
        await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🌅 Goedemorgen! Gisteren ({yesterday.strftime('%-d/%-m')}) nog geen tijdsregistratie.\n\nWil je dat alsnog doen? Zeg erbij 'gisteren'! 🎤")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    app.job_queue.run_daily(send_daily_reminder, time=datetime.strptime("16:00", "%H:%M").time(), days=(0,1,2,3,4))
    app.job_queue.run_daily(send_followup_reminder, time=datetime.strptime("16:30", "%H:%M").time(), days=(0,1,2,3,4))
    app.job_queue.run_daily(send_morning_reminder, time=datetime.strptime("08:00", "%H:%M").time(), days=(0,1,2,3,4))
    logger.info("🤖 Bot gestart!")
    app.run_polling()

if __name__ == "__main__":
    main()
