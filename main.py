import os
import re
import json
import logging
import subprocess
import asyncio
import aiohttp
from dotenv import load_dotenv
from twitchio.ext import commands

load_dotenv()
TWITCH_TOKEN        = os.getenv("TWITCH_TOKEN")
TWITCH_CLIENT_ID    = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CHANNEL_NAME = os.getenv("TWITCH_CHANNEL_NAME")
TWITCH_CHANNEL_ID   = os.getenv("TWITCH_CHANNEL_ID")
SCREEN_SESSION      = os.getenv("SCREEN_SESSION", "mcserver")
REWARD_ID           = os.getenv("REWARD_ID")
USER_DB_FILE        = "/app/data/users.json"

if not all([TWITCH_TOKEN, TWITCH_CLIENT_ID, TWITCH_CHANNEL_NAME, TWITCH_CHANNEL_ID, REWARD_ID]):
    logging.error("Missing environment variables")
    exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

RETRY_DELAYS = [1, 2, 5]

class TwitchWhitelistBot(commands.Bot):
    def __init__(self):
        self.session = aiohttp.ClientSession()
        capabilities = [
            "twitch.tv/commands",
            "twitch.tv/tags",
            "twitch.tv/membership",
            "twitch.tv/channel_points"
        ]
        super().__init__(
            token=TWITCH_TOKEN,
            prefix="!",
            initial_channels=[TWITCH_CHANNEL_NAME],
            initial_capabilities=capabilities
        )
        self.user_db = self.load_db()

    def load_db(self) -> dict:
        if os.path.isfile(USER_DB_FILE):
            with open(USER_DB_FILE, "r") as f:
                return json.load(f)
        return {}

    def save_db(self):
        with open(USER_DB_FILE, "w") as f:
            json.dump(self.user_db, f, indent=2)
        logging.info("User DB saved")

    def screen_cmd(self, cmd: str) -> bool:
        try:
            subprocess.run([
                "screen", "-S", SCREEN_SESSION, "-p", "0", "-X", "stuff", cmd + "\n"
            ], check=True)
            logging.info(f"Sent to MC console: {cmd}")
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"Screen cmd error: {e}")
            return False

    async def refund(self, rid: str) -> bool:
        url = "https://api.twitch.tv/helix/channel_points/custom_rewards/redemptions"
        params = {
            "broadcaster_id": TWITCH_CHANNEL_ID,
            "reward_id": REWARD_ID,
            "id": rid
        }
        json_data = {
            "status": "CANCELED"
        }
        headers = {
            "Authorization": f"Bearer {TWITCH_TOKEN}",
            "Client-Id": TWITCH_CLIENT_ID,
            "Content-Type": "application/json"
        }
        for delay in RETRY_DELAYS:
            try:
                async with self.session.patch(url, params=params, json=json_data, headers=headers, timeout=5) as r:
                    if r.status == 200:
                        logging.info(f"Refunded redemption {rid}")
                        return True
                    else:
                        text = await r.text()
                        logging.warning(f"Refund failed status {r.status}: {text}")
            except Exception as e:
                logging.error(f"Exception refunding redemption {rid}: {e}")
            await asyncio.sleep(delay)
        logging.error(f"Failed to refund redemption {rid}")
        return False

    async def exists_mojang(self, name: str) -> bool:
        url = f"https://api.mojang.com/users/profiles/minecraft/{name}"
        for delay in RETRY_DELAYS:
            try:
                async with self.session.get(url, timeout=5) as r:
                    if r.status == 200:
                        return True
                    elif r.status in (204, 404):
                        return False
                    else:
                        logging.warning(f"Mojang API returned unexpected status {r.status} for {name}")
            except Exception as e:
                logging.error(f"Exception checking Mojang username {name}: {e}")
            await asyncio.sleep(delay)
        # Falls nach Retries keine Antwort, gilt als nicht erreichbar -> false -> refund
        return False

    def valid_format(self, name: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_]{3,16}", name))

    async def event_ready(self):
        logging.info(f"Connected as {self.nick} in {TWITCH_CHANNEL_NAME}")

    async def event_raw_usernotice(self, channel, tags):
        logging.debug(f"raw_usernotice event tags: {dict(tags)}")
        if tags.get("msg-id") != "reward-redeemed" or tags.get("custom-reward-id") != REWARD_ID:
            logging.debug("Not a reward-redeemed or wrong reward-id")
            return

        twitch_user = tags.get("login")
        mc = tags.get("text", "").strip()
        rid = tags.get("id")
        logging.info(f"Redemption {rid} by {twitch_user}: '{mc}'")

        if not self.valid_format(mc):
            logging.warning(f"Invalid MC username format: {mc}")
            await self.refund(rid)
            return

        try:
            exists = await self.exists_mojang(mc)
        except Exception as e:
            logging.error(f"Exception while checking Mojang username: {e}")
            exists = False

        if not exists:
            logging.warning(f"Minecraft username does not exist or API unreachable: {mc}")
            await self.refund(rid)
            return

        if mc in self.user_db.values():
            owner = next(u for u, m in self.user_db.items() if m == mc)
            if owner != twitch_user:
                logging.warning(f"MC name {mc} already assigned to {owner}")
                await self.refund(rid)
                return

        if twitch_user in self.user_db and self.user_db[twitch_user] != mc:
            old = self.user_db[twitch_user]
            if self.screen_cmd(f"whitelist remove {old}"):
                logging.info(f"Removed old {old} for {twitch_user}")
            del self.user_db[twitch_user]

        if not self.screen_cmd(f"whitelist add {mc}"):
            logging.error(f"Whitelist add failed for {mc}")
            await self.refund(rid)
            return

        self.user_db[twitch_user] = mc
        self.save_db()
        logging.info(f"Whitelisted {mc} for {twitch_user}")
        logging.info("Redemption handled successfully")

    async def close(self):
        await super().close()
        await self.session.close()

if __name__ == "__main__":
    logging.info("Starting Twitch whitelist bot")
    TwitchWhitelistBot().run()
