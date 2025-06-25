import os
import re
import json
import logging
import subprocess
import requests
from dotenv import load_dotenv
from twitchio.ext import commands

load_dotenv()
TWITCH_TOKEN         = os.getenv("TWITCH_TOKEN")
TWITCH_CLIENT_ID     = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CHANNEL_NAME  = os.getenv("TWITCH_CHANNEL_NAME")
TWITCH_CHANNEL_ID    = os.getenv("TWITCH_CHANNEL_ID")
SCREEN_SESSION       = os.getenv("SCREEN_SESSION", "mcserver")
REWARD_ID            = os.getenv("REWARD_ID")
USER_DB_FILE         = "/app/data/users.json"

if not TWITCH_CHANNEL_NAME or not TWITCH_CHANNEL_ID:
    logging.error(f"Missing configuration: CHANNEL_NAME={TWITCH_CHANNEL_NAME}, CHANNEL_ID={TWITCH_CHANNEL_ID}")
    exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def is_valid_mc_username_format(name: str) -> bool:
    valid = bool(re.fullmatch(r"[A-Za-z0-9_]{3,16}", name))
    logging.info(f"Username format check '{name}': {'valid' if valid else 'invalid'}")
    return valid


def mc_username_exists(name: str) -> bool:
    url = f"https://api.mojang.com/users/profiles/minecraft/{name}"
    logging.info(f"Checking Mojang API for user '{name}' at {url}")
    r = requests.get(url)
    logging.info(f"Mojang API response for '{name}': {r.status_code}")
    return r.status_code == 200


def refund_redemption(redemption_id: str):
    logging.info(f"Attempting refund for redemption {redemption_id}")
    url = "https://api.twitch.tv/helix/channel_points/custom_rewards/redemptions"
    params = {"broadcaster_id": TWITCH_CHANNEL_ID, "reward_id": REWARD_ID, "id": redemption_id, "status": "CANCELED"}
    headers = {"Authorization": f"Bearer {TWITCH_TOKEN}", "Client-Id": TWITCH_CLIENT_ID}
    r = requests.patch(url, params=params, headers=headers)
    logging.info(f"Twitch refund API call returned status {r.status_code}")
    if r.status_code == 200:
        logging.info(f"Refunded redemption {redemption_id}")
    else:
        logging.error(f"Refund failed: {r.status_code} {r.text}")


def run_screen_command(command: str) -> bool:
    logging.info(f"Sending screen command: {command}")
    try:
        subprocess.run(["screen", "-S", SCREEN_SESSION, "-p", "0", "-X", "stuff", command + "\n"], check=True)
        logging.info("Screen command succeeded")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Screen command error: {e}")
        return False


def whitelist_add(username: str) -> bool:
    logging.info(f"Adding to whitelist: {username}")
    return run_screen_command(f"whitelist add {username}")


def whitelist_remove(username: str) -> bool:
    logging.info(f"Removing from whitelist: {username}")
    return run_screen_command(f"whitelist remove {username}")


def load_user_db() -> dict:
    logging.info(f"Loading user DB from {USER_DB_FILE}")
    if not os.path.exists(USER_DB_FILE):
        logging.info("User DB not found, initializing new DB")
        return {}
    with open(USER_DB_FILE, "r") as f:
        data = json.load(f)
    logging.info(f"User DB loaded ({len(data)} entries)")
    return data


def save_user_db(data: dict):
    logging.info(f"Saving user DB with {len(data)} entries")
    with open(USER_DB_FILE, "w") as f:
        json.dump(data, f, indent=2)
    logging.info("User DB saved successfully")

class Bot(commands.Bot):
    def __init__(self):
        logging.info("Initializing Bot")
        super().__init__(token=TWITCH_TOKEN, prefix="!", initial_channels=[TWITCH_CHANNEL_NAME])

    async def event_ready(self):
        logging.info(f"Connected to Twitch as {self.nick} in {TWITCH_CHANNEL_NAME}")

    async def event_raw_usernotice(self, channel, tags):
        logging.info("Received raw_usernotice event")
        if tags.get("msg-id") != "reward-redeemed":
            logging.debug("Ignored event: not a reward-redeemed")
            return
        if tags.get("custom-reward-id") != REWARD_ID:
            logging.debug(f"Ignored event: reward-id {tags.get('custom-reward-id')}")
            return

        twitch_user   = tags.get("login")
        username      = tags.get("text", "").strip()
        redemption_id = tags.get("id")
        logging.info(f"Processing redemption {redemption_id} by {twitch_user} with input '{username}'")

        if not is_valid_mc_username_format(username):
            logging.warning(f"Invalid username format: {username}")
            refund_redemption(redemption_id)
            return
        if not mc_username_exists(username):
            logging.warning(f"Minecraft username does not exist: {username}")
            refund_redemption(redemption_id)
            return

        user_db = load_user_db()
        if username in user_db.values() and any(u != twitch_user and m == username for u, m in user_db.items()):
            conflict = [u for u, m in user_db.items() if m == username and u != twitch_user][0]
            logging.warning(f"Username {username} already bound to Twitch user {conflict}")
            refund_redemption(redemption_id)
            return

        if twitch_user in user_db and user_db[twitch_user] != username:
            old = user_db[twitch_user]
            logging.info(f"User {twitch_user} already whitelisted as {old}, removing old entry")
            whitelist_remove(old)
            del user_db[twitch_user]

        if not whitelist_add(username):
            refund_redemption(redemption_id)
            return

        user_db[twitch_user] = username
        save_user_db(user_db)
        logging.info(f"Successfully whitelisted {username} for {twitch_user}")

if __name__ == "__main__":
    logging.info("Starting Twitch whitelist bot")
    Bot().run()
