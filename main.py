import os
import re
import json
import logging
import subprocess
import requests
from time import sleep
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
    logging.error("Missing one or more required environment variables")
    exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

RETRY_DELAYS = [1, 2, 5]

class TwitchWhitelistBot(commands.Bot):
    def __init__(self):
        self.session = requests.Session()
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
        self.user_db = self.load_user_db()
        logging.info("Bot initialized and user DB loaded")

    def load_user_db(self) -> dict:
        if os.path.isfile(USER_DB_FILE):
            with open(USER_DB_FILE, "r") as f:
                return json.load(f)
        return {}

    def save_user_db(self):
        with open(USER_DB_FILE, "w") as f:
            json.dump(self.user_db, f, indent=2)
        logging.info("User DB saved")

    def is_valid_format(self, name: str) -> bool:
        valid = bool(re.fullmatch(r"[A-Za-z0-9_]{3,16}", name))
        logging.debug(f"Format check '{name}': {valid}")
        return valid

    def exists_mojang(self, name: str) -> bool:
        url = f"https://api.mojang.com/users/profiles/minecraft/{name}"
        for delay in RETRY_DELAYS:
            try:
                r = self.session.get(url, timeout=5)
                logging.debug(f"Mojang API {name}: {r.status_code}")
                return r.status_code == 200
            except requests.RequestException as e:
                logging.warning(f"Mojang API error: {e}, retrying in {delay}s")
                sleep(delay)
        return False

    def refund(self, redemption_id: str):
        url = "https://api.twitch.tv/helix/channel_points/custom_rewards/redemptions"
        params = {"broadcaster_id": TWITCH_CHANNEL_ID, "reward_id": REWARD_ID, "id": redemption_id, "status": "CANCELED"}
        headers = {"Authorization": f"Bearer {TWITCH_TOKEN}", "Client-Id": TWITCH_CLIENT_ID}
        for delay in RETRY_DELAYS:
            try:
                r = self.session.patch(url, params=params, headers=headers, timeout=5)
                logging.debug(f"Refund API {r.status_code}")
                if r.status_code == 200:
                    return True
                logging.error(f"Refund failed {r.status_code}")
            except requests.RequestException as e:
                logging.warning(f"Refund error: {e}, retrying in {delay}s")
                sleep(delay)
        return False

    def screen_cmd(self, cmd: str) -> bool:
        try:
            subprocess.run(["screen", "-S", SCREEN_SESSION, "-p", "0", "-X", "stuff", cmd + "\n"], check=True)
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"Screen cmd error: {e}")
            return False

    async def event_ready(self):
        logging.info(f"Connected as {self.nick} in {TWITCH_CHANNEL_NAME}")

    async def event_raw_usernotice(self, channel, tags):
        if tags.get("msg-id") != "reward-redeemed" or tags.get("custom-reward-id") != REWARD_ID:
            return
        user = tags.get("login")
        mc = tags.get("text", "").strip()
        rid = tags.get("id")
        logging.info(f"Redemption {rid} by {user}: '{mc}'")
        if not self.is_valid_format(mc) or not self.exists_mojang(mc):
            logging.warning("Invalid Minecraft username")
            self.refund(rid)
            return
        if mc in self.user_db.values():
            owner = [u for u, m in self.user_db.items() if m == mc][0]
            if owner != user:
                logging.warning(f"Username taken by {owner}")
                self.refund(rid)
                return
        if user in self.user_db and self.user_db[user] != mc:
            old = self.user_db[user]
            if self.screen_cmd(f"whitelist remove {old}"):
                logging.info(f"Removed old {old}")
            del self.user_db[user]
        if not self.screen_cmd(f"whitelist add {mc}"):
            logging.error("Whitelist add failed")
            self.refund(rid)
            return
        self.user_db[user] = mc
        self.save_user_db()
        logging.info(f"Whitelisted {mc} for {user}")

if __name__ == "__main__":
    logging.info("Starting Twitch whitelist bot")
    TwitchWhitelistBot().run()
