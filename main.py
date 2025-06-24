import os
import re
import json
import logging
import subprocess
import requests
from dotenv import load_dotenv
from twitchio.ext import commands

# ──────── 1. Konfiguration ─────────────────────────────────────────
load_dotenv()
TWITCH_TOKEN      = os.getenv("TWITCH_TOKEN")
TWITCH_CLIENT_ID  = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CHANNEL_ID = os.getenv("TWITCH_CHANNEL_ID")  # numerische Kanal-ID
SCREEN_SESSION    = os.getenv("SCREEN_SESSION", "mcserver")
REWARD_ID         = os.getenv("REWARD_ID")
USER_DB_FILE      = "users.json"

if not TWITCH_CHANNEL_ID:
    logging.error("TWITCH_CHANNEL_ID ist nicht gesetzt!")
    exit(1)

# ──────── 2. Logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ──────── 3. Helferfunktionen ─────────────────────────────────────

def is_valid_mc_username(name: str) -> bool:
    valid = bool(re.fullmatch(r"[A-Za-z0-9_]{3,16}", name))
    logging.info(f"Überprüfe Username '{name}' -> {'gültig' if valid else 'ungültig'}")
    return valid


def refund_redemption(redemption_id: str):
    logging.info(f"Starte Rückerstattung für Redemption {redemption_id}")
    url = "https://api.twitch.tv/helix/channel_points/custom_rewards/redemptions"
    params = {
        "broadcaster_id": TWITCH_CHANNEL_ID,
        "reward_id": REWARD_ID,
        "id": redemption_id,
        "status": "CANCELED"
    }
    headers = {
        "Authorization": f"Bearer {TWITCH_TOKEN}",
        "Client-Id": TWITCH_CLIENT_ID,
    }
    r = requests.patch(url, params=params, headers=headers)
    if r.status_code == 200:
        logging.info(f"✔ Redemption {redemption_id} zurückerstattet.")
    else:
        logging.error(f"✖ Refund fehlgeschlagen: {r.status_code} – {r.text}")


def run_screen_command(command: str) -> bool:
    logging.info(f"Sende Screen-Befehl: {command}")
    try:
        subprocess.run(
            ["screen", "-S", SCREEN_SESSION, "-p", "0", "-X", "stuff", command + "\n"],
            check=True
        )
        logging.info("✔ Screen-Befehl ausgeführt")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Screen-Befehl fehlgeschlagen: {e}")
        return False


def whitelist_add(username: str) -> bool:
    logging.info(f"Whitelist hinzufügen: {username}")
    return run_screen_command(f"whitelist add {username}")


def whitelist_remove(username: str) -> bool:
    logging.info(f"Whitelist entfernen: {username}")
    return run_screen_command(f"whitelist remove {username}")


def load_user_db() -> dict:
    if not os.path.exists(USER_DB_FILE):
        logging.info(f"DB-Datei '{USER_DB_FILE}' nicht gefunden. Neue wird angelegt.")
        return {}
    logging.info(f"Lade DB aus '{USER_DB_FILE}'")
    with open(USER_DB_FILE, "r") as f:
        data = json.load(f)
    logging.info(f"DB geladen ({len(data)} Einträge)")
    return data


def save_user_db(data: dict):
    logging.info(f"Speichere DB mit {len(data)} Einträgen")
    with open(USER_DB_FILE, "w") as f:
        json.dump(data, f, indent=2)
    logging.info("DB gespeichert")

# ──────── 4. Bot ─────────────────────────────────────────────────
class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            token=TWITCH_TOKEN,
            prefix="!",
            initial_channels=[TWITCH_CHANNEL_ID]  # channel ID statt Name
        )

    async def event_ready(self):
        logging.info(f"🤖 Verbunden als {self.nick}, Channel-ID: {TWITCH_CHANNEL_ID}")

    async def event_raw_usernotice(self, channel, tags):
        logging.info("Empfange raw_usernotice Event")
        if tags.get("msg-id") != "reward-redeemed":
            logging.debug("Kein reward-redeemed Event")
            return

        if tags.get("custom-reward-id") != REWARD_ID:
            logging.debug(f"Ignoriere Reward {tags.get('custom-reward-id')}")
            return

        twitch_user   = tags.get("login")
        username      = tags.get("text", "").strip()
        redemption_id = tags.get("id")
        logging.info(f"🎁 {twitch_user} hat '{username}' eingelöst (ID: {redemption_id})")

        if not is_valid_mc_username(username):
            refund_redemption(redemption_id)
            return

        user_db = load_user_db()
        if twitch_user in user_db and user_db[twitch_user] != username:
            old = user_db[twitch_user]
            whitelist_remove(old)
            del user_db[twitch_user]
            logging.info(f"Alte Zuordnung entfernt: {old}")

        if not whitelist_add(username):
            refund_redemption(redemption_id)
            return

        user_db[twitch_user] = username
        save_user_db(user_db)
        logging.info(f"Neuer Eintrag: {twitch_user} → {username}")

# ──────── 5. Start ─────────────────────────────────────────────────
if __name__ == "__main__":
    logging.info("Starte Bot...")
    Bot().run()
