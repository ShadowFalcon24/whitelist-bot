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
    logging.error(f"Missing config: NAME={TWITCH_CHANNEL_NAME}, ID={TWITCH_CHANNEL_ID}")
    exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

def is_valid_mc_username_format(name: str) -> bool:
    valid = bool(re.fullmatch(r"[A-Za-z0-9_]{3,16}", name))
    logging.info(f"Format check '{name}': {'valid' if valid else 'invalid'}")
    return valid

def mc_username_exists(name: str) -> bool:
    url = f"https://api.mojang.com/users/profiles/minecraft/{name}"
    try:
        r = requests.get(url, timeout=5)
        logging.info(f"Mojang API {name}: status={r.status_code}")
        return r.status_code == 200
    except requests.RequestException as e:
        logging.error(f"Mojang API error for '{name}': {e}")
        return False

def refund_redemption(redemption_id: str):
    try:
        url = "https://api.twitch.tv/helix/channel_points/custom_rewards/redemptions"
        params = {"broadcaster_id": TWITCH_CHANNEL_ID, "reward_id": REWARD_ID, "id": redemption_id, "status": "CANCELED"}
        headers = {"Authorization": f"Bearer {TWITCH_TOKEN}", "Client-Id": TWITCH_CLIENT_ID}
        r = requests.patch(url, params=params, headers=headers, timeout=5)
        logging.info(f"Refund API status={r.status_code}")
        if r.status_code == 200:
            logging.info(f"Refunded {redemption_id}")
        else:
            logging.error(f"Refund failed {r.status_code}: {r.text}")
    except requests.RequestException as e:
        logging.error(f"Refund request error: {e}")

def run_screen_command(command: str) -> bool:
    try:
        subprocess.run(["screen", "-S", SCREEN_SESSION, "-p", "0", "-X", "stuff", command + "\n"], check=True)
        logging.info(f"Screen: {command} succeeded")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Screen error: {e}")
        return False

def whitelist_add(username: str) -> bool:
    return run_screen_command(f"whitelist add {username}")

def whitelist_remove(username: str) -> bool:
    return run_screen_command(f"whitelist remove {username}")

def load_user_db() -> dict:
    if not os.path.exists(USER_DB_FILE): return {}
    with open(USER_DB_FILE, "r") as f: data = json.load(f)
    return data

def save_user_db(data: dict):
    with open(USER_DB_FILE, "w") as f: json.dump(data, f, indent=2)

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(token=TWITCH_TOKEN, prefix="!", initial_channels=[TWITCH_CHANNEL_NAME])
        logging.info("Bot init complete")

    async def event_ready(self):
        logging.info(f"Connected as {self.nick} in {TWITCH_CHANNEL_NAME}")

    async def event_raw_usernotice(self, channel, tags):
        try:
            if tags.get("msg-id") != "reward-redeemed": return
            if tags.get("custom-reward-id") != REWARD_ID: return
            twitch_user   = tags.get("login")
            username      = tags.get("text", "").strip()
            redemption_id = tags.get("id")
            logging.info(f"Redemption triggered: id={redemption_id}, user={twitch_user}, input='{username}'")
            if not is_valid_mc_username_format(username):
                logging.warning("Invalid format")
                refund_redemption(redemption_id)
                return
            if not mc_username_exists(username):
                logging.warning("User not found in Mojang")
                refund_redemption(redemption_id)
                return
            user_db = load_user_db()
            if username in user_db.values() and any(u!=twitch_user and m==username for u,m in user_db.items()):
                conflict=[u for u,m in user_db.items() if m==username and u!=twitch_user][0]
                logging.warning(f"Name {username} bound to {conflict}")
                refund_redemption(redemption_id)
                return
            if twitch_user in user_db and user_db[twitch_user]!=username:
                old=user_db[twitch_user]; whitelist_remove(old); del user_db[twitch_user]
                logging.info(f"Removed old {old} for {twitch_user}")
            if not whitelist_add(username):
                logging.error("Whitelist add failed")
                refund_redemption(redemption_id)
                return
            user_db[twitch_user]=username; save_user_db(user_db)
            logging.info(f"Whitelisted {username} for {twitch_user}")
        except Exception as e:
            logging.exception(f"Error handling redemption: {e}")

if __name__ == "__main__":
    logging.info("Starting bot")
    Bot().run()
