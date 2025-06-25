import os  # Für Betriebssystemfunktionen wie Pfade und Umgebungsvariablen
import re  # Für reguläre Ausdrücke zur Namensvalidierung
import json  # Für das Speichern und Laden der User-Datenbank
import logging  # Für Logging-Ausgaben
import asyncio  # Für asynchrone Programmierung
import subprocess  # Für das Ausführen von Shell-Kommandos (screen)
import aiohttp  # Für asynchrone HTTP-Requests

from dotenv import load_dotenv  # Für das Laden von .env-Dateien
from twitchAPI.twitch import Twitch  # Twitch API Wrapper
from twitchAPI.eventsub.websocket import EventSubWebsocket  # Für EventSub Websocket

# Lädt Umgebungsvariablen aus einer .env-Datei
load_dotenv()

# Konfigurationswerte aus Umgebungsvariablen
TWITCH_CLIENT_ID     = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_CHANNEL_NAME  = os.getenv("TWITCH_CHANNEL_NAME")
REWARD_ID            = os.getenv("REWARD_ID")
SCREEN_SESSION       = os.getenv("SCREEN_SESSION", "mcserver")
USER_DB_FILE         = "/app/data/users.json"  # Pfad zur User-Datenbank

# Verzögerungen für Retry-Mechanismen (z.B. bei API-Fehlern)
RETRY_DELAYS = [1, 2, 5]

# Logging-Konfiguration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class WhitelistManager:
    """
    Diese Klasse verwaltet die Whitelist-Logik, die User-Datenbank und die Kommunikation mit Minecraft und Twitch.
    """
    def __init__(self):
        # Lädt die User-Datenbank beim Start
        self.user_db = self.load_db()
        self.session = None  # HTTP-Session wird asynchron erstellt
        self.broadcaster_id = None  # Twitch Broadcaster-ID

    async def init_session(self):
        # Erstellt eine aiohttp-Session, falls noch nicht vorhanden
        if self.session is None:
            self.session = aiohttp.ClientSession()

    def load_db(self):
        # Lädt die User-Datenbank aus einer JSON-Datei
        if os.path.isfile(USER_DB_FILE):
            try:
                with open(USER_DB_FILE, "r") as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"Fehler beim Laden der User-DB: {e}")
                return {}
        return {}

    def save_db(self):
        # Speichert die User-Datenbank in eine JSON-Datei
        os.makedirs(os.path.dirname(USER_DB_FILE), exist_ok=True)
        with open(USER_DB_FILE, "w") as f:
            json.dump(self.user_db, f, indent=2)
        logging.info("User DB saved")

    def valid_format(self, name: str) -> bool:
        # Prüft, ob der Minecraft-Name das richtige Format hat
        return bool(re.fullmatch(r"[A-Za-z0-9_]{3,16}", name))

    async def exists_mojang(self, name: str) -> bool:
        # Prüft, ob ein Minecraft-Account existiert (Mojang API)
        await self.init_session()
        url = f"https://api.mojang.com/users/profiles/minecraft/{name}"
        for delay in RETRY_DELAYS:
            try:
                async with self.session.get(url, timeout=5) as r:
                    return r.status == 200  # 200 = Account existiert
            except aiohttp.ClientError:
                await asyncio.sleep(delay)  # Bei Fehlern: kurz warten und erneut versuchen
        logging.warning("Mojang API nicht erreichbar")
        return False

    def screen_cmd(self, cmd: str) -> bool:
        # Sendet einen Befehl an die Minecraft-Konsole via screen
        try:
            subprocess.run(
                ["screen", "-S", SCREEN_SESSION, "-p", "0", "-X", "stuff", cmd + "\n"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logging.info(f"Sent to MC console: {cmd}")
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"Screen cmd error: {e}")
            return False

    async def refund_points(self, twitch: Twitch, redemption_id: str):
        # Versucht, die Twitch Channel Points für eine ungültige Einlösung zurückzuerstatten
        await self.init_session()
        url = "https://api.twitch.tv/helix/channel_points/custom_rewards/redemptions"
        params = {
            "broadcaster_id": self.broadcaster_id,
            "reward_id": REWARD_ID,
            "id": redemption_id,
        }
        data = {"status": "CANCELED"}
        # Achtung: Du brauchst einen User-Token mit dem Scope 'channel:manage:redemptions'
        token = await twitch.get_app_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": TWITCH_CLIENT_ID,
            "Content-Type": "application/json"
        }
        for delay in RETRY_DELAYS:
            try:
                async with self.session.patch(url, params=params, headers=headers, json=data) as resp:
                    if resp.status == 200:
                        logging.info(f"Refunded redemption {redemption_id}")
                        return True
                    else:
                        logging.warning(f"Refund failed with status {resp.status}")
            except Exception as e:
                logging.warning(f"Refund attempt error: {e}")
                await asyncio.sleep(delay)
        logging.error(f"Failed to refund redemption {redemption_id}")
        return False

    async def handle_redemption(self, twitch: Twitch, event):
        # Hauptlogik für die Bearbeitung einer Channel Points-Einlösung
        # event kann ein Objekt oder dict sein, je nach twitchAPI-Version
        data = event.data if hasattr(event, "data") else event
        twitch_user = data['user_login']  # Twitch-Benutzername
        mc_name = data['user_input'].strip()  # Eingegebener Minecraft-Name
        redemption_id = data['id']  # Redemption-ID für Rückerstattung

        logging.info(f"Redemption {redemption_id} by {twitch_user}: '{mc_name}'")

        # Prüfe Format
        if not self.valid_format(mc_name):
            logging.warning("Ungültiges MC-Format")
            await self.refund_points(twitch, redemption_id)
            return

        # Prüfe, ob Account existiert
        if not await self.exists_mojang(mc_name):
            logging.warning("Ungültiger oder nicht existierender Mojang-Account")
            await self.refund_points(twitch, redemption_id)
            return

        # Prüfe, ob Name schon von anderem User verwendet wird
        for user, stored_mc in self.user_db.items():
            if stored_mc == mc_name and user != twitch_user:
                logging.warning(f"MC-Name {mc_name} ist bereits durch {user} registriert.")
                await self.refund_points(twitch, redemption_id)
                return

        # Entferne alten Namen, falls vorhanden
        old_name = self.user_db.get(twitch_user)
        if old_name and old_name != mc_name:
            if self.screen_cmd(f"whitelist remove {old_name}"):
                logging.info(f"Ehemaliger Name {old_name} für {twitch_user} entfernt")

        # Füge neuen Namen zur Whitelist hinzu
        if not self.screen_cmd(f"whitelist add {mc_name}"):
            logging.error(f"Hinzufügen von {mc_name} zur Whitelist fehlgeschlagen")
            await self.refund_points(twitch, redemption_id)
            return

        # Speichere neuen Namen in der User-DB
        self.user_db[twitch_user] = mc_name
        self.save_db()
        logging.info(f"{mc_name} erfolgreich für {twitch_user} whitelisted")

    async def close(self):
        # Schliesst die HTTP-Session, wenn sie existiert
        if self.session:
            await self.session.close()


async def main():
    # Hauptfunktion: Initialisiert Twitch, EventSub und startet den Bot
    logging.info("Starte Twitch-Whitelist-Bot (EventSub WebSocket)")

    twitch = Twitch(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)  # Twitch-API-Objekt
    await twitch.authenticate_app([])  # Authentifiziere App

    # Hole Broadcaster-Info (Twitch-User-ID)
    users = []
    async for user in twitch.get_users(logins=[TWITCH_CHANNEL_NAME]):
        users.append(user)
    if not users:
        logging.error(f"Channel {TWITCH_CHANNEL_NAME} nicht gefunden")
        return
    broadcaster_id = users[0].id

    manager = WhitelistManager()  # Erstelle Whitelist-Manager
    manager.broadcaster_id = broadcaster_id
    await manager.init_session()

    eventsub = EventSubWebsocket(twitch)  # EventSub Websocket für Twitch-Events

    async def on_redemption(event):
        # Callback für neue Channel Points-Einlösungen
        data = event.data if hasattr(event, "data") else event
        if data['reward']['id'] != REWARD_ID:
            return  # Nur auf das gewünschte Reward reagieren
        await manager.handle_redemption(twitch, event)

    await eventsub.start()  # Starte EventSub Websocket
    await eventsub.listen_channel_points_custom_reward_redemption_add(
        broadcaster_id, on_redemption
    )  # Abonniere Channel Points-Events
    logging.info("Subscriptions registered, waiting for events...")

    try:
        await asyncio.Future()  # Halte den Bot am Laufen
    except KeyboardInterrupt:
        logging.info("Bot durch Benutzer gestoppt")
    finally:
        await eventsub.stop()  # Stoppe EventSub sauber
        await manager.close()  # Schliesse HTTP-Session


if __name__ == "__main__":
    # Entry-Point: Starte das Hauptprogramm
    asyncio.run(main())
