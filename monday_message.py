#!/usr/bin/env python3
import os
import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM_NUMBER = os.environ["TWILIO_FROM_NUMBER"]
GIRLFRIEND_PHONE = os.environ["GIRLFRIEND_PHONE"]
GIRLFRIEND_NAME = os.environ.get("GIRLFRIEND_NAME", "")


def generate_message():
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    name_context = f"Kjærestens navn er {GIRLFRIEND_NAME}. " if GIRLFRIEND_NAME else ""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": f"""Du er en kjærlig og litt morsom kjæreste som sender en mandagsmelding på norsk.
{name_context}
Skriv en kort, varm og personlig mandagsmelding (maks 2-3 setninger).
Varier tonen — noen ganger søt, noen ganger litt humor, noen ganger inspirerende.
Aldri generisk eller klisjéfylt. Bare selve meldingen, ingen annen tekst."""
        }],
    )

    return response.content[0].text.strip()


def send_sms(message):
    resp = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json",
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        data={
            "From": TWILIO_FROM_NUMBER,
            "To": GIRLFRIEND_PHONE,
            "Body": message,
        },
        timeout=15,
    )
    resp.raise_for_status()
    print(f"SMS sendt: {message}")


def main():
    message = generate_message()
    send_sms(message)


if __name__ == "__main__":
    main()
