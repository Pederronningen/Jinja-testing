#!/usr/bin/env python3
import os
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta, date as date_type
import time
from collections import Counter
from zoneinfo import ZoneInfo
import icalendar
import recurring_ical_events
import feedparser
import requests
import anthropic
from jinja2 import Environment, FileSystemLoader
from dotenv import load_dotenv
import json
import logging

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CALENDAR_ICS_URL = os.environ.get("GOOGLE_CALENDAR_ICS_URL")

IMAP_ACCOUNTS = [
    {
        "label": "Gmail",
        "user": os.environ["GMAIL_USER"],
        "password": os.environ["GMAIL_APP_PASSWORD"],
        "host": "imap.gmail.com",
    },
    {
        "label": "Jobb",
        "user": os.environ["DITTUTSTYR_USER"],
        "password": os.environ["DITTUTSTYR_APP_PASSWORD"],
        "host": "imap.gmail.com",
    },
]

RSS_FEEDS = {
    "NRK": "https://www.nrk.no/toppsaker.rss",
    "VG": "https://www.vg.no/rss/feed/",
    "Aftenposten": "https://www.aftenposten.no/rss/feed/forsiden/",
}

OSLO_TZ = ZoneInfo("Europe/Oslo")

NORSK_DAGER = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
NORSK_MND = [
    "januar", "februar", "mars", "april", "mai", "juni",
    "juli", "august", "september", "oktober", "november", "desember",
]

WEATHER_SYMBOLS = {
    "clearsky": "Klart",
    "fair": "Lettskyet",
    "partlycloudy": "Delvis skyet",
    "cloudy": "Overskyet",
    "fog": "Tåke",
    "lightrain": "Lett regn",
    "rain": "Regn",
    "heavyrain": "Kraftig regn",
    "lightrainshowers": "Regnbyger",
    "rainshowers": "Regnbyger",
    "heavyrainshowers": "Kraftige regnbyger",
    "lightsleet": "Lett sludd",
    "sleet": "Sludd",
    "sleetshowers": "Sluddbyger",
    "lightsnow": "Lett snø",
    "snow": "Snø",
    "heavysnow": "Kraftig snø",
    "snowshowers": "Snøbyger",
    "thunder": "Torden",
}


def _decode_str(value, encoding=None):
    if isinstance(value, bytes):
        return value.decode(encoding or "utf-8", errors="replace")
    return value or ""


def _fetch_emails_from_account(account):
    log.info("Kobler til %s (%s)…", account["label"], account["host"])
    mail = imaplib.IMAP4_SSL(account["host"])
    try:
        mail.login(account["user"], account["password"])
        mail.select("INBOX")
        since = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
        _, nums = mail.search(None, f"SINCE {since}")
        num_list = nums[0].split()
        log.info("%s: fant %d e-poster siden %s", account["label"], len(num_list), since)

        emails = []
        for num in num_list:
            _, data = mail.fetch(num, "(RFC822.HEADER)")
            msg = email.message_from_bytes(data[0][1])

            raw_subject = msg.get("Subject", "(ingen emne)")
            subject = "".join(
                _decode_str(part, enc) for part, enc in decode_header(raw_subject)
            ).strip()

            raw_from = msg.get("From", "")
            sender = "".join(
                _decode_str(part, enc) for part, enc in decode_header(raw_from)
            ).strip()

            emails.append({"account": account["label"], "subject": subject, "sender": sender})

        return emails
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def fetch_emails():
    all_emails = []
    for account in IMAP_ACCOUNTS:
        try:
            all_emails.extend(_fetch_emails_from_account(account))
        except Exception as exc:
            log.warning("Kunne ikke hente e-post fra %s: %s", account["label"], exc)
    return all_emails


def fetch_news():
    log.info("Henter RSS-nyheter…")
    cutoff = time.gmtime((datetime.now().astimezone(OSLO_TZ) - timedelta(hours=24)).timestamp())
    articles = []

    for source, url in RSS_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            count = 0
            for entry in feed.entries:
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub and pub >= cutoff:
                    articles.append({
                        "source": source,
                        "title": entry.get("title", "").strip(),
                        "summary": entry.get("summary", "").strip()[:300],
                        "published": datetime(*pub[:6]).strftime("%H:%M"),
                    })
                    count += 1
            log.info("%s: %d artikler siste 24t", source, count)
        except Exception as exc:
            log.warning("Kunne ikke hente %s: %s", source, exc)

    return articles


def fetch_calendar(ics_url):
    log.info("Henter kalender…")
    resp = requests.get(
        ics_url,
        headers={"User-Agent": "DagligDigest/1.0"},
        timeout=15,
    )
    resp.raise_for_status()
    cal = icalendar.Calendar.from_ical(resp.content)

    today = datetime.now(OSLO_TZ).date()
    tomorrow = today + timedelta(days=1)

    def parse_day(d):
        events = recurring_ical_events.of(cal).at(d)
        result = []

        def sort_key(ev):
            dt = ev.get("DTSTART").dt
            if isinstance(dt, datetime):
                return dt.astimezone(OSLO_TZ)
            return datetime(dt.year, dt.month, dt.day, tzinfo=OSLO_TZ)

        for ev in sorted(events, key=sort_key):
            start = ev.get("DTSTART").dt
            end_raw = ev.get("DTEND")
            summary = str(ev.get("SUMMARY", "")).strip()

            if isinstance(start, date_type) and not isinstance(start, datetime):
                result.append({"time": "Heldag", "title": summary})
            else:
                start = start.astimezone(OSLO_TZ)
                time_str = start.strftime("%H:%M")
                if end_raw:
                    end = end_raw.dt
                    if isinstance(end, datetime):
                        time_str += f"–{end.astimezone(OSLO_TZ).strftime('%H:%M')}"
                result.append({"time": time_str, "title": summary})

        return result

    return {"today": parse_day(today), "tomorrow": parse_day(tomorrow)}


def fetch_weather():
    log.info("Henter vær for Oslo…")
    resp = requests.get(
        "https://api.met.no/weatherapi/locationforecast/2.0/compact",
        params={"lat": 59.9139, "lon": 10.7522},
        headers={"User-Agent": "DagligDigest/1.0 github.com/Pederronningen/Jinja-testing"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    today = datetime.now(OSLO_TZ).date()
    temps, precip, symbols = [], 0.0, []

    for ts in data["properties"]["timeseries"]:
        t = datetime.fromisoformat(ts["time"].replace("Z", "+00:00")).astimezone(OSLO_TZ)
        if t.date() != today:
            continue
        temps.append(ts["data"]["instant"]["details"]["air_temperature"])
        if "next_1_hours" in ts["data"]:
            symbols.append(ts["data"]["next_1_hours"]["summary"]["symbol_code"])
            precip += ts["data"]["next_1_hours"]["details"].get("precipitation_amount", 0.0)

    if not temps:
        return None

    dominant_code = Counter(s.split("_")[0] for s in symbols).most_common(1)[0][0] if symbols else ""
    return {
        "temp_min": round(min(temps)),
        "temp_max": round(max(temps)),
        "condition": WEATHER_SYMBOLS.get(dominant_code, dominant_code.capitalize()),
        "precip": round(precip, 1),
    }


def analyze_with_claude(emails, news_articles):
    log.info("Analyserer med Claude…")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    email_lines = (
        "\n".join(
            f"- [{e['account']}] Fra: {e['sender']} | Emne: {e['subject']}"
            for e in emails[:50]
        )
        if emails else "Ingen nye e-poster"
    )
    news_lines = (
        "\n".join(
            f"- [{a['source']} {a['published']}] {a['title']}"
            for a in news_articles[:30]
        )
        if news_articles else "Ingen nye artikler"
    )

    prompt = f"""Du er en personlig assistent. Lag et kort daglig sammendrag på norsk.

E-POSTER (siste 24 timer):
{email_lines}

NYHETER (siste 24 timer):
{news_lines}

Svar med KUN dette JSON-objektet, ingen annen tekst:
{{
  "email_summary": "<1-2 setninger som oppsummerer hva slags e-poster som kom inn>",
  "news_significant": <true kun hvis det har skjedd noe virkelig viktig: krig, terror, store naturkatastrofer, alvorlige politiske kriser — vanlige nyheter er false>,
  "news_summary": "<2-3 setninger om de viktigste nyhetene, eller null hvis news_significant er false>"
}}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

    return json.loads(raw)


def send_to_slack(text):
    payload = {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}
    resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
    resp.raise_for_status()
    log.info("Slack-melding sendt.")


def main():
    now = datetime.now(OSLO_TZ)
    today = f"{NORSK_DAGER[now.weekday()]} {now.day}. {NORSK_MND[now.month - 1]} {now.year}"

    emails = fetch_emails()
    news = fetch_news()
    analysis = analyze_with_claude(emails, news)

    calendar = None
    if GOOGLE_CALENDAR_ICS_URL:
        try:
            calendar = fetch_calendar(GOOGLE_CALENDAR_ICS_URL)
        except Exception as exc:
            log.warning("Kunne ikke hente kalender: %s", exc)

    weather = None
    try:
        weather = fetch_weather()
    except Exception as exc:
        log.warning("Kunne ikke hente vær: %s", exc)

    env = Environment(loader=FileSystemLoader("templates"), autoescape=False)
    template = env.get_template("digest.j2")
    text = template.render(
        today=today,
        email_count=len(emails),
        analysis=analysis,
        calendar=calendar,
        weather=weather,
    )

    send_to_slack(text)
    log.info("Ferdig.")


if __name__ == "__main__":
    main()
