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
    return bool(re.fullmatch(r"[A-Za-z0-9_]{3,16}", name))

def refund_redemption(redemption_id: str):
    """Setzt den Status der Redemption auf CANCELED zurück und erstattet die Punkte."""
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
    full_cmd = f"{command}\n"
    try:
        subprocess.run(
            ["screen", "-S", SCREEN_SESSION, "-p", "0", "-X", "stuff", full_cmd],
            check=True
        )
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Screen-Befehl fehlgeschlagen: {e}")
        return False

def whitelist_add(username: str) -> bool:
    """Player zur Whitelist hinzufügen."""
    return run_screen_command(f"whitelist add {username}")

def whitelist_remove(username: str) -> bool:
    """Player von der Whitelist entfernen."""
    return run_screen_command(f"whitelist remove {username}")

def load_user_db() -> dict:
    """Lade die Zuordnung Twitch-User -> Minecraft-Username aus JSON."""
    if not os.path.exists(USER_DB_FILE):
        return {}
    with open(USER_DB_FILE, "r") as f:
        return json.load(f)

def save_user_db(data: dict):
    """Speichere die Zuordnung Twitch-User -> Minecraft-Username als JSON."""
    with open(USER_DB_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ──────── 4. Der Bot ───────────────────────────────────────────────
class Bot(commands.Bot):
    def __init__(self):
        super().__init__(token=TWITCH_TOKEN, prefix="!", initial_channels=[])

    async def event_ready(self):
        logging.info(f"🤖 Bot verbunden als {self.nick}")

    async def event_raw_usernotice(self, channel, tags):
        # Nur User-Notices für Kanalpunkt-Redemptions
        if tags.get("msg-id") != "reward-redeemed":
            return

        reward_id = tags.get("custom-reward-id")
        if reward_id != REWARD_ID:
            return

        twitch_user    = tags.get("login")
        username       = tags.get("text", "").strip()
        redemption_id  = tags.get("id")

        logging.info(f"🎁 Redemption von {twitch_user}: „{username}“")

        # Validieren
        if not is_valid_mc_username(username):
            logging.warning(f"⚠ Ungültiger Username: {username}")
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
                # Alte Zuordnung entfernen
                del user_db[twitch_user]

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
