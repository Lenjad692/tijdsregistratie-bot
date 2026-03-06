import os
import logging
import threading
import asyncio
import json
import re
import tempfile
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import openai
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
REMINDER_SECRET = os.environ.get("REMINDER_SECRET", "geheim123")

client = openai.OpenAI(api_key=OPENAI_API_KEY)

state = {
    "responded_today": False,
    "leave_until": None,
    "app": None,
    "loop": None,
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
    r = httpx.post(WEBHOOK_URL, json={"rows": rows}, follow_redirects=True, timeout=30)
    if r.status_code != 200 or r.text.strip() != "OK":
        raise Exception(f"Webhook fout: {r.status_code} {r.text[:100]}")

async def transcribe_voice(file_path):
    with open(file_path, "rb") as f:
        t = client.audio.transcriptions.create(model="whisper-1", file=f, language="nl")
    return t.text

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
    r = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0)
    raw = re.sub(r"^```(?:json)?", "", r.choices[0].message.content.strip()).strip()
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
    r = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0)
    raw = re.sub(r"^```(?:json)?", "", r.choices[0].message.content.strip()).strip()
    raw = re.sub(r"```$", "", raw).strip()
    return json.loads(raw)

async def send_weekly_analysis(bot, chat_id):
    try:
        r = httpx.get(WEBHOOK_URL, params={"type": "stats", "secret": REMINDER_SECRET}, follow_redirects=True, timeout=30)
        stats = r.json()
        total_hours = stats["totalHours"]
        total_euro = stats["totalEuro"]
        week_nr = stats["weekNr"]
        per_klant = stats["perKlant"]

        doel_uren = 80
        doel_euro = 8000
        resterend = max(0, doel_uren - total_hours)
        weken_over = max(1, 4 - week_nr)
        nodig_pw = round(resterend / weken_over, 1)

        klant_tekst = ""
        for klant, minuten in sorted(per_klant.items(), key=lambda x: x[1], reverse=True):
            klant_tekst += f"  • {klant}: {round(minuten/60, 1)}u\n"

        if total_hours > doel_uren:
            motivatie = f"🌟 *Wauw, {total_hours}u — meer dan je doel van {doel_uren}u!*\nBen je zeker dat je alles geregistreerd hebt? Wil je nog iets toevoegen, of neem je de extra uren mee?"
        elif total_hours >= doel_uren * 0.75:
            motivatie = f"💪 *Goed bezig! {total_hours}u van {doel_uren}u.*\nNog {resterend}u in {weken_over} week(en) = ~{nodig_pw}u/week. Dat lukt! 🚀"
        elif total_hours >= doel_uren * 0.5:
            motivatie = f"📈 *Halverwege! {total_hours}u van {doel_uren}u.*\nNog {resterend}u in {weken_over} week(en) = ~{nodig_pw}u/week. Zet er een tandje bij! 💼"
        else:
            motivatie = f"⚡ *{total_hours}u — tijd om bij te steken!*\nNog {resterend}u in {weken_over} week(en) = ~{nodig_pw}u/week. Je kan het! 💪"

        msg = f"📊 *Weekupdate — week {week_nr} van de maand*\n\n⏱ *Uren:* {total_hours}u / {doel_uren}u\n"
        if week_nr >= 2:
            msg += f"💶 *Te factureren:* €{total_euro} / €{doel_euro}\n"
        msg += f"\n*Per klant:*\n{klant_tekst}\n{motivatie}"

        await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Weekrapport fout: {e}", exc_info=True)
        await bot.send_message(chat_id=chat_id, text=f"❌ Weekrapport fout: {str(e)}")

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
        confirm_text += f"• *{e['klant']}* ({e['datum']}) — {e['beschrijving']} ({e['minuten']} min / {round(e['minuten']/60,1)}u)\n"
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

class ReminderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        secret = params.get("secret", [""])[0]
        trigger = parsed.path.strip("/")

        if secret != REMINDER_SECRET:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return

        app = state.get("app")
        loop = state.get("loop")

        if not app or not loop:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"Bot not ready")
            return

        async def send_reminder():
            if trigger == "reminder17":
                reset_daily_state()
                if not is_on_leave():
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="👋 Hé Lenja! Werkdag bijna gedaan!\n\nStuur me een 🎤 *voice bericht*:\n• Voor welke klanten werkte je?\n• Wat deed je?\n• Hoeveel uur?\n\nMeerdere klanten mag in één bericht! 📊", parse_mode="Markdown")
            elif trigger == "reminder1730":
                if not state["responded_today"] and not is_on_leave():
                    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="⏰ Nog even! Geen tijdsregistratie ontvangen.\n\nWas je vrij? Stuur *'vandaag verlof'*. 😊", parse_mode="Markdown")
            elif trigger == "reminder09":
                if not is_on_leave():
                    yesterday = date.today() - timedelta(days=1)
                    if yesterday.weekday() < 5 and not state["responded_today"]:
                        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🌅 Goedemorgen! Gisteren ({yesterday.strftime('%-d/%-m')}) nog geen tijdsregistratie.\n\nWil je dat alsnog doen? Zeg erbij 'gisteren'! 🎤")
            elif trigger == "weekrapport":
                await send_weekly_analysis(app.bot, TELEGRAM_CHAT_ID)

        future = asyncio.run_coroutine_threadsafe(send_reminder(), loop)
        try:
            future.result(timeout=30)
        except Exception as ex:
            logger.error(f"Reminder fout: {ex}", exc_info=True)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        logger.info(f"HTTP: {format % args}")

def start_http_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), ReminderHandler)
    logger.info(f"HTTP server op poort {port}")
    server.serve_forever()

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    state["app"] = app

    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))

    t = threading.Thread(target=start_http_server, daemon=True)
    t.start()

    async def post_init(application):
        state["loop"] = asyncio.get_event_loop()

    app.post_init = post_init

    logger.info("🤖 Bot gestart!")
    app.run_polling()

if __name__ == "__main__":
    main()
