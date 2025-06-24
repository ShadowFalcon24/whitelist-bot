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
TWITCH_CHANNEL_ID = os.getenv("TWITCH_CHANNEL_ID")
SCREEN_SESSION    = os.getenv("SCREEN_SESSION", "mcserver")
REWARD_ID         = os.getenv("REWARD_ID")
USER_DB_FILE      = "users.json"

# ──────── 2. Logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ──────── 3. Hilfsfunktionen ───────────────────────────────────────

def is_valid_mc_username(name: str) -> bool:
    """Erlaubt nur A–Z, a–z, 0–9 und _; Länge 3–16."""
    valid = bool(re.fullmatch(r"[A-Za-z0-9_]{3,16}", name))
    logging.info(f"Überprüfe Username '{name}': {'gültig' if valid else 'ungültig'}")
    return valid


def refund_redemption(redemption_id: str):
    """Setzt den Status der Redemption auf CANCELED zurück und erstattet die Punkte."""
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
    """Sende einen Befehl an die Screen-Session."""
    logging.info(f"Sende Screen-Befehl: {command}")
    full_cmd = f"{command}\n"
    try:
        subprocess.run(
            ["screen", "-S", SCREEN_SESSION, "-p", "0", "-X", "stuff", full_cmd],
            check=True
        )
        logging.info("✔ Screen-Befehl erfolgreich ausgeführt")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Screen-Befehl fehlgeschlagen: {e}")
        return False


def whitelist_add(username: str) -> bool:
    """Player zur Whitelist hinzufügen."""
    logging.info(f"Whitelist hinzufügen: {username}")
    return run_screen_command(f"whitelist add {username}")


def whitelist_remove(username: str) -> bool:
    """Player von der Whitelist entfernen."""
    logging.info(f"Whitelist entfernen: {username}")
    return run_screen_command(f"whitelist remove {username}")


def load_user_db() -> dict:
    """Lade die Zuordnung Twitch-User -> Minecraft-Username aus JSON."""
    if not os.path.exists(USER_DB_FILE):
        logging.info(f"Datenbankdatei '{USER_DB_FILE}' nicht gefunden. Erstelle neue.")
        return {}
    logging.info(f"Lade Datenbank aus '{USER_DB_FILE}'")
    with open(USER_DB_FILE, "r") as f:
        data = json.load(f)
    logging.info(f"Datenbank geladen ({len(data)} Einträge)")
    return data


def save_user_db(data: dict):
    """Speichere die Zuordnung Twitch-User -> Minecraft-Username als JSON."""
    logging.info(f"Speichere Datenbank mit {len(data)} Einträgen")
    with open(USER_DB_FILE, "w") as f:
        json.dump(data, f, indent=2)
    logging.info("Datenbank gespeichert")

# ──────── 4. Der Bot ───────────────────────────────────────────────
class Bot(commands.Bot):
    def __init__(self):
        super().__init__(token=TWITCH_TOKEN, prefix="!", initial_channels=[])

    async def event_ready(self):
        logging.info(f"🤖 Bot verbunden als {self.nick}")

    async def event_raw_usernotice(self, channel, tags):
        logging.info("Empfange raw_usernotice Event")
        # Nur User-Notices für Kanalpunkt-Redemptions
        if tags.get("msg-id") != "reward-redeemed":
            logging.debug("Nicht relevant: kein reward-redeemed Event")
            return

        reward_id = tags.get("custom-reward-id")
        if reward_id != REWARD_ID:
            logging.debug(f"Ignoriere Reward {reward_id}")
            return

        twitch_user    = tags.get("login")
        username       = tags.get("text", "").strip()
        redemption_id  = tags.get("id")

        logging.info(f"🎁 Redemption von {twitch_user}: '{username}' (ID: {redemption_id})")

        # Validieren
        if not is_valid_mc_username(username):
            logging.warning(f"Ungültiger Username: {username}")
            refund_redemption(redemption_id)
            return

        # DB laden
        user_db = load_user_db()

        # Alten Account entfernen, falls vorhanden und unterschiedlich
        if twitch_user in user_db:
            old_mc = user_db[twitch_user]
            if old_mc != username:
                logging.info(f"🔁 Entferne alten Account {old_mc} für {twitch_user}")
                whitelist_remove(old_mc)
                del user_db[twitch_user]
                logging.info(f"Alte Zuordnung entfernt")

        # Neuen Account whitelisten
        if not whitelist_add(username):
            logging.error(f"❌ Konnte {username} nicht whitelisten")
            refund_redemption(redemption_id)
            return

        # DB aktualisieren
        user_db[twitch_user] = username
        save_user_db(user_db)
        logging.info(f"📦 Gespeichert: {twitch_user} → {username}")

# ──────── 5. Start ─────────────────────────────────────────────────
if __name__ == "__main__":
    Bot().run()
