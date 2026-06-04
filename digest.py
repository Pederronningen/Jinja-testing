#!/usr/bin/env python3
import os
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta, timezone
import time
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

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

RSS_FEEDS = {
    "NRK": "https://www.nrk.no/toppsaker.rss",
    "VG": "https://www.vg.no/rss/feed/",
    "Aftenposten": "https://www.aftenposten.no/rss/feed/forsiden/",
}

NORSK_DAGER = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
NORSK_MND = [
    "januar", "februar", "mars", "april", "mai", "juni",
    "juli", "august", "september", "oktober", "november", "desember",
]


def _decode_str(value, encoding=None):
    if isinstance(value, bytes):
        return value.decode(encoding or "utf-8", errors="replace")
    return value or ""


def fetch_emails():
    log.info("Kobler til Gmail IMAP…")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("INBOX")
        since = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
        _, nums = mail.search(None, f"SINCE {since}")
        num_list = nums[0].split()
        log.info("Fant %d e-poster siden %s", len(num_list), since)

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

            emails.append({"subject": subject, "sender": sender})

        return emails
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def fetch_news():
    log.info("Henter RSS-nyheter…")
    cutoff = time.gmtime((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp())
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


def analyze_with_claude(emails, news_articles):
    log.info("Analyserer med Claude…")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    email_lines = (
        "\n".join(f"- Fra: {e['sender']} | Emne: {e['subject']}" for e in emails[:50])
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
    now = datetime.now()
    today = f"{NORSK_DAGER[now.weekday()]} {now.day}. {NORSK_MND[now.month - 1]} {now.year}"

    emails = fetch_emails()
    news = fetch_news()
    analysis = analyze_with_claude(emails, news)

    env = Environment(loader=FileSystemLoader("templates"), autoescape=False)
    template = env.get_template("digest.j2")
    text = template.render(today=today, email_count=len(emails), analysis=analysis)

    send_to_slack(text)
    log.info("Ferdig.")


if __name__ == "__main__":
    main()
