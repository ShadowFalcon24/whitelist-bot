import os
import re
import json
import logging
import asyncio
import subprocess
import aiohttp

from dotenv import load_dotenv
from twitchAPI.twitch import Twitch
from twitchAPI.eventsub.websocket import EventSubWebsocket

load_dotenv()

TWITCH_CLIENT_ID     = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_CHANNEL_NAME  = os.getenv("TWITCH_CHANNEL_NAME")
REWARD_ID            = os.getenv("REWARD_ID")
SCREEN_SESSION       = os.getenv("SCREEN_SESSION", "mcserver")
USER_DB_FILE         = "/app/data/users.json"

RETRY_DELAYS = [1, 2, 5]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

class WhitelistManager:
    def __init__(self):
        self.user_db = self.load_db()
        self.session = aiohttp.ClientSession()

    def load_db(self):
        if os.path.isfile(USER_DB_FILE):
            try:
                with open(USER_DB_FILE, "r") as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"Fehler beim Laden der User-DB: {e}")
                return {}
        return {}

    def save_db(self):
        os.makedirs(os.path.dirname(USER_DB_FILE), exist_ok=True)
        with open(USER_DB_FILE, "w") as f:
            json.dump(self.user_db, f, indent=2)
        logging.info("User DB saved")

    def valid_format(self, name: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_]{3,16}", name))

    async def exists_mojang(self, name: str) -> bool:
        url = f"https://api.mojang.com/users/profiles/minecraft/{name}"
        for delay in RETRY_DELAYS:
            try:
                async with self.session.get(url, timeout=5) as r:
                    return r.status == 200
            except aiohttp.ClientError:
                await asyncio.sleep(delay)
        logging.warning("Mojang API nicht erreichbar")
        return False

    def screen_cmd(self, cmd: str) -> bool:
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
        url = "https://api.twitch.tv/helix/channel_points/custom_rewards/redemptions"
        params = {
            "broadcaster_id": self.broadcaster_id,
            "reward_id": REWARD_ID,
            "id": redemption_id,
            "status": "CANCELED"
        }
        token = await twitch.get_app_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": TWITCH_CLIENT_ID
        }
        for delay in RETRY_DELAYS:
            try:
                async with twitch.session.patch(url, params=params, headers=headers) as resp:
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

    async def handle_redemption(self, twitch: Twitch, event: dict):
        twitch_user = event['user_login']
        mc_name = event['user_input'].strip()
        redemption_id = event['id']

        logging.info(f"Redemption {redemption_id} by {twitch_user}: '{mc_name}'")

        if not self.valid_format(mc_name):
            logging.warning("Ungültiges MC-Format")
            await self.refund_points(twitch, redemption_id)
            return

        if not await self.exists_mojang(mc_name):
            logging.warning("Ungültiger oder nicht existierender Mojang-Account")
            await self.refund_points(twitch, redemption_id)
            return

        for user, stored_mc in self.user_db.items():
            if stored_mc == mc_name and user != twitch_user:
                logging.warning(f"MC-Name {mc_name} ist bereits durch {user} registriert.")
                await self.refund_points(twitch, redemption_id)
                return

        old_name = self.user_db.get(twitch_user)
        if old_name and old_name != mc_name:
            if self.screen_cmd(f"whitelist remove {old_name}"):
                logging.info(f"Ehemaliger Name {old_name} für {twitch_user} entfernt")

        if not self.screen_cmd(f"whitelist add {mc_name}"):
            logging.error(f"Hinzufügen von {mc_name} zur Whitelist fehlgeschlagen")
            await self.refund_points(twitch, redemption_id)
            return

        self.user_db[twitch_user] = mc_name
        self.save_db()
        logging.info(f"{mc_name} erfolgreich für {twitch_user} whitelisted")

    async def close(self):
        await self.session.close()


async def main():
    logging.info("Starte Twitch-Whitelist-Bot (EventSub WebSocket)")

    twitch = Twitch(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)
    await twitch.authenticate_app([])

    user_info_data = []
    async for user in twitch.get_users(logins=[TWITCH_CHANNEL_NAME]):
        user_info_data.append(user)

    if not user_info_data:
        logging.error(f"Channel {TWITCH_CHANNEL_NAME} nicht gefunden")
        return

    # Access TwitchUser object's id attribute, not as dict
    broadcaster_id = user_info_data[0].id

    manager = WhitelistManager()
    manager.broadcaster_id = broadcaster_id

    eventsub = EventSubWebsocket(twitch)

    async def on_redemption(event: dict):
        if event['reward']['id'] != REWARD_ID:
            return
        await manager.handle_redemption(twitch, event)

    await eventsub.listen_channel_points_custom_reward_redemption_add(
        broadcaster_id, on_redemption
    )

    try:
        await eventsub.start()
    except KeyboardInterrupt:
        logging.info("Bot durch Benutzer gestoppt")
    finally:
        await manager.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot durch Benutzer gestoppt")
