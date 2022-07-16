import argparse
import re
import asyncio
import sys
import os
from numpy import true_divide
import portalocker
import math
import time

from telethon import TelegramClient, events
from py3cw.request import Py3CW
from singlebot import SingleBot
from multibot import MultiBot
from signals import Signals
from config import Config
from pathlib import Path
from logger import Logger, NotificationHandler
from threading import Thread

######################################################
#                       Config                       #
######################################################

# load configuration file
attributes = Config()

program = Path(__file__).stem

# Parse and interpret options
parser = argparse.ArgumentParser(
    description="3CQSBot bringing 3CQS signals to 3Commas."
)

parser.add_argument("-d", "--datadir", help="data directory to use", type=str)
args = parser.parse_args()
if args.datadir:
    datadir = args.datadir
else:
    datadir = os.getcwd()

# Handle timezone
if hasattr(time, "tzset"):
    os.environ["TZ"] = attributes.get("timezone", "Europe/Amsterdam")
    time.tzset()

# Init notification handler
notification = NotificationHandler(
    program,
    attributes.get("notifications", False),
    attributes.get("notify-urls", []),
)

# Initialise logging
logging = Logger(
    datadir,
    program,
    notification,
    attributes.get("logrotate", 7),
    attributes.get("debug", False),
    attributes.get("notifications", False),
)

logging.info(f"Loaded configuration from '{datadir}/config.ini'")

######################################################
#                        Init                        #
######################################################

# Initialize 3Commas API client
p3cw = Py3CW(
    key=attributes.get("key"),
    secret=attributes.get("secret"),
    request_options={
        "request_timeout": attributes.get("timeout", 3),
        "nr_of_retries": attributes.get("retries", 5),
        "retry_backoff_factor": attributes.get("delay_between_retries", 2.0),
    },
)

# Initialize Telegram API client
client = TelegramClient(
    attributes.get("sessionfile", "tgsesssion"),
    attributes.get("api_id"),
    attributes.get("api_hash"),
)

# Initialize global variables
asyncState = type("", (), {})()
asyncState.btc_downtrend = False
asyncState.bot_active = True
asyncState.first_topcoin_call = True
asyncState.fgi = -1
asyncState.fgi_downtrend = False
asyncState.fgi_allows_trading = True
asyncState.fgi_time_until_update = 1
asyncState.dca_conf = "dcabot"
asyncState.chatid = ""
asyncState.fh = 0
asyncState.account_data = {}
asyncState.pair_data = []
asyncState.symrank_success = False
asyncState.multibot = {}

######################################################
#                     Methods                        #
######################################################
def run_once():
    asyncState.fh = open(os.path.realpath(__file__), "r")
    try:
        portalocker.lock(asyncState.fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except:
        sys.exit(
            "Another 3CQSBot is already running in this directory - please use another one!"
        )


# Check for single instance run
run_once()


def parse_tg(raw_text):
    return raw_text.split("\n")


def tg_data(text_lines):

    # Make sure the message is a signal
    if len(text_lines) == 7:
        data = {}
        signal = text_lines[1]
        token = text_lines[2].replace("#", "")
        action = text_lines[3].replace("BOT_", "")
        volatility_score = text_lines[4].replace("Volatility Score ", "")

        if volatility_score == "N/A":
            volatility_score = 9999999

        priceaction_score = text_lines[5].replace("Price Action Score ", "")

        if priceaction_score == "N/A":
            priceaction_score = 9999999

        symrank = text_lines[6].replace("SymRank #", "")

        if symrank == "N/A":
            symrank = 9999999

        if signal == "SymRank Top 100 Triple Tracker":
            signal = "triple100"
        elif signal == "SymRank Top 100 Quadruple Tracker (BETA)":
            signal = "quadruple100"
        elif signal == "SymRank Top 250 Quadruple Tracker (BETA)":
            signal = "quadruple250"
        elif signal == "SymRank Top 30":
            signal = "top30"
        elif signal == "Super Volatility":
            signal = "svol"
        elif signal == "Super Volatility Double Tracker":
            signal = "svoldouble"
        elif signal == "Hyper Volatility":
            signal = "hvol"
        elif signal == "Hyper Volatility Double Tracker":
            signal = "hvoldouble"
        elif signal == "Ultra Volatility":
            signal = "uvol"
        elif signal == "X-Treme Volatility":
            signal = "xvol"

        data = {
            "signal": signal,
            "pair": attributes.get("market") + "_" + token,
            "action": action,
            "volatility": float(volatility_score),
            "price_action": float(priceaction_score),
            "symrank": int(symrank),
        }
    # Symrank list
    elif len(text_lines) == 17:
        pairs = {}
        data = []

        if "Volatile" not in text_lines[0]:
            for row in text_lines:
                if ". " in row:
                    # Sort the pair list from Telegram
                    line = re.split(" +", row)
                    pairs.update(
                        {int(line[0][:-1]): line[1], int(line[2][:-1]): line[3]}
                    )

            allpairs = dict(sorted(pairs.items()))
            data = list(allpairs.values())
    # too many requests or other commands
    else:
        data = False

    return data


def bot_data():

    # Gets information about existing bots in 3Commas
    botlimit = attributes.get("system_bot_value", 300)
    pages = math.ceil(botlimit / 100)
    bots = []

    for page in range(1, pages + 1):
        if page == 1:
            offset = 0
        else:
            offset = (page - 1) * 100

        error, data = p3cw.request(
            entity="bots",
            action="",
            additional_headers={"Forced-Mode": attributes.get("trade_mode")},
            payload={"limit": 100, "offset": offset},
        )

        if error:
            sys.exit(error["msg"])
        else:
            if data:
                bots += data
            else:
                break

    return bots


def account_data():

    # Gets information about the used 3commas account (paper or real)
    account = {}

    error, data = p3cw.request(
        entity="accounts",
        action="",
        additional_headers={"Forced-Mode": attributes.get("trade_mode")},
    )

    if error:
        logging.error(error["msg"])
        sys.tracebacklimit = 0
        sys.exit("Problem fetching account data from 3commas api - stopping!")
    else:
        for accounts in data:
            if accounts["name"] == attributes.get("account_name"):
                account.update({"id": str(accounts["id"])})
                account.update({"market_code": str(accounts["market_code"])})

        if "id" not in account:
            sys.tracebacklimit = 0
            sys.exit(
                "Account with name '" + attributes.get("account_name") + "' not found"
            )

    return account


def pair_data(account, interval_sec):

    while True:
        pairs = []
        asyncState.pair_data = []

        error, data = p3cw.request(
            entity="accounts",
            action="market_pairs",
            additional_headers={"Forced-Mode": attributes.get("trade_mode")},
            payload={"market_code": account["market_code"]},
        )

        if error:
            logging.error(error["msg"])
            sys.tracebacklimit = 0
            sys.exit("Problem fetching pair data from 3commas api - stopping!")

        error, blacklist_data = p3cw.request(entity="bots", action="pairs_black_list")

        if error:
            logging.error(error["msg"])
            sys.tracebacklimit = 0
            sys.exit(
                "Problem fetching pairs blacklist data from 3commas api - stopping!"
            )

        for pair in data:
            if attributes.get("market") in pair:
                if (
                    pair not in attributes.get("token_denylist", [])
                    and pair not in blacklist_data["pairs"]
                ):
                    pairs.append(pair)

        asyncState.pair_data = pairs
        time.sleep(interval_sec)


async def symrank():

    logging.info(
        "Sending /symrank command to 3C Quick Stats on Telegram to get new pairs"
    )
    while not asyncState.symrank_success:
        await client.send_message(asyncState.chatid, "/symrank")
        await asyncio.sleep(5)
        # prevent from calling the symrank command too much otherwise a timeout is caused
        if not asyncState.symrank_success:
            await asyncio.sleep(60)


def bot_switch(interval_sec):

    while True:

        if (
            not asyncState.bot_active
            and not asyncState.btc_downtrend
            and not asyncState.fgi_downtrend
        ):

            if not asyncState.btc_downtrend and attributes.get("btc_pulse", False):
                logging.info("BTC uptrending", True)

            if not asyncState.fgi_downtrend and attributes.get("fearandgreed", False):
                if asyncState.fgi >= attributes.get(
                    "fgi_trade_min", 0
                ) and asyncState.fgi <= attributes.get("fgi_trade_max", 100):
                    logging.info(
                        "FGI inside allowed trading range ["
                        + str(attributes.get("fgi_trade_min", 0))
                        + ".."
                        + str(attributes.get("fgi_trade_max", 100))
                        + "]",
                        True,
                    )
                    asyncState.fgi_allows_trading = True

            if not asyncState.btc_downtrend or asyncState.fgi_allows_trading:
                if attributes.get("single"):
                    asyncState.bot_active = True
                    logging.info(
                        "Single bot mode activated - waiting for pair #start signals",
                        True,
                    )
                elif attributes.get("continuous_update", False):
                    # listen continously to 3cqs msgs on TG, avoid symrank calls
                    if asyncState.multibot == {}:
                        bot = MultiBot(
                            [], bot_data(), {}, 0, attributes, p3cw, logging, asyncState
                        )
                    else:
                        bot = MultiBot(
                            [],
                            asyncState.multibot,
                            {},
                            0,
                            attributes,
                            p3cw,
                            logging,
                            asyncState,
                        )
                    bot.enable(asyncState.multibot)
                    asyncState.bot_active = bot.asyncState.bot_active
                    asyncState.multibot = bot.bot_data
                    logging.info(
                        "Multi bot activated - waiting for pair #start signals", True
                    )
                # enabling bot only after sending symrank call to avoid messing up with old pairs
                else:
                    logging.info(
                        "Multi bot activated - using pairs from actual top30 symrank list",
                        True,
                    )
                    asyncState.symrank_success = False

                notification.send_notification()

        elif asyncState.bot_active and (
            asyncState.btc_downtrend or asyncState.fgi_downtrend
        ):

            if asyncState.btc_downtrend and attributes.get("btc_pulse", False):
                logging.info("BTC downtrending", True)

            if asyncState.fgi_downtrend and attributes.get("fearandgreed", False):
                if asyncState.fgi < attributes.get(
                    "fgi_trade_min", 0
                ) or asyncState.fgi > attributes.get("fgi_trade_max", 100):
                    logging.info(
                        "FGI downtrending or outside the allowed trading range ["
                        + str(attributes.get("fgi_trade_min", 0))
                        + ".."
                        + str(attributes.get("fgi_trade_max", 100))
                        + "]",
                        True,
                    )
                    asyncState.fgi_allows_trading = False

            if asyncState.btc_downtrend or not asyncState.fgi_allows_trading:
                if attributes.get("single"):
                    bot = SingleBot(
                        [], bot_data(), {}, attributes, p3cw, logging, asyncState
                    )
                    bot.disable(bot_data(), True)  # True = disable all single bots
                    asyncState.bot_active = bot.asyncState.bot_active
                else:
                    if asyncState.multibot == {}:
                        bot = MultiBot(
                            [], bot_data(), {}, 0, attributes, p3cw, logging, asyncState
                        )
                    else:
                        bot = MultiBot(
                            [],
                            asyncState.multibot,
                            {},
                            0,
                            attributes,
                            p3cw,
                            logging,
                            asyncState,
                        )
                    bot.disable(asyncState.multibot)
                    asyncState.bot_active = bot.asyncState.bot_active
                    asyncState.multibot = bot.bot_data

                notification.send_notification()

        else:
            logging.debug("Nothing do to")
            logging.debug("bot_active: " + str(asyncState.bot_active))

        time.sleep(interval_sec)


def fgi_dca_conf_change(interval_sec):

    while True:
        if asyncState.fgi >= attributes.get(
            "fgi_min", 0, "fgi_defensive"
        ) and asyncState.fgi <= attributes.get("fgi_max", 30, "fgi_defensive"):
            asyncState.dca_conf = "fgi_defensive"

        if asyncState.fgi >= attributes.get(
            "fgi_min", 31, "fgi_moderate"
        ) and asyncState.fgi <= attributes.get("fgi_max", 60, "fgi_moderate"):
            asyncState.dca_conf = "fgi_moderate"

        if asyncState.fgi >= attributes.get(
            "fgi_min", 61, "fgi_aggressive"
        ) and asyncState.fgi <= attributes.get("fgi_max", 100, "fgi_aggressive"):
            asyncState.dca_conf = "fgi_aggressive"

        # Check if section fgi_defensive, fgi_moderate and fgi_aggressive are defined in config.ini, if not use standard settings of [dcabot]
        if (
            attributes.get("fgi_min", -1, "fgi_defensive") == -1
            or attributes.get("fgi_min", -1, "fgi_moderate") == -1
            or attributes.get("fgi_min", -1, "fgi_aggressive") == -1
        ):
            logging.info(
                "DCA settings for [fgi_defensive], [fgi_moderate] or [fgi_aggressive] are not configured. Using standard settings of [dcabot] for all FGI values 0-100"
            )
            asyncState.dca_conf = "dcabot"

        time.sleep(interval_sec)


def _handle_task_result(task: asyncio.Task) -> None:

    try:
        task.result()
    except asyncio.CancelledError:
        pass  # Task cancellation should not be logged as an error.
    except Exception:  # pylint: disable=broad-except
        logging.exception(
            "Exception raised by task = %r",
            task,
        )


@client.on(events.NewMessage(chats=attributes.get("chatroom", "3C Quick Stats")))
async def my_event_handler(event):

    tg_output = tg_data(parse_tg(event.raw_text))
    logging.debug("TG msg: " + str(tg_output))

    if tg_output and asyncState.fgi_allows_trading:
        account_output = asyncState.account_data
        pair_output = asyncState.pair_data
        # if signal with #START or #STOP
        if tg_output and not isinstance(tg_output, list):

            logging.info(
                "New 3CQS signal '" + str(tg_output["signal"]) + "' incoming..."
            )
            # Check if pair is in whitelist
            if attributes.get("token_whitelist", []):
                token_whitelisted = tg_output["pair"] in attributes.get(
                    "token_whitelist", []
                )
            else:
                token_whitelisted = True

            if not asyncState.bot_active and not attributes.get(
                "continuous_update", False
            ):
                logging.info("Signal not processed because of BTC downtrend")
            # Check if it is the right signal
            elif (
                tg_output["signal"] == attributes.get("symrank_signal")
                or attributes.get("symrank_signal") == "all"
            ) and token_whitelisted:

                if attributes.get("single") or asyncState.multibot == {}:
                    bot_output = bot_data()
                else:
                    bot_output = asyncState.multibot

                # Choose multibot or singlebot
                if attributes.get("single"):
                    bot = SingleBot(
                        tg_output,
                        bot_output,
                        account_output,
                        attributes,
                        p3cw,
                        logging,
                        asyncState,
                    )
                else:
                    bot = MultiBot(
                        tg_output,
                        bot_output,
                        account_output,
                        pair_output,
                        attributes,
                        p3cw,
                        logging,
                        asyncState,
                    )

                # Trigger bot if limits passed
                if tg_output["volatility"] != 0 and tg_output["pair"] in pair_output:
                    if (
                        tg_output["volatility"]
                        >= attributes.get("volatility_limit_min", 0.1)
                        and tg_output["volatility"]
                        <= attributes.get("volatility_limit_max", 100)
                        and tg_output["price_action"]
                        >= attributes.get("price_action_limit_min", 0.1)
                        and tg_output["price_action"]
                        <= attributes.get("price_action_limit_max", 100)
                        and tg_output["symrank"]
                        >= attributes.get("symrank_limit_min", 1)
                        and tg_output["symrank"]
                        <= attributes.get("symrank_limit_max", 100)
                    ) or tg_output["action"] == "STOP":

                        bot.trigger()
                        asyncState.multibot = bot.bot_data
                        notification.send_notification()

                    else:
                        logging.info(
                            "Start signal for "
                            + str(tg_output["pair"])
                            + " with symrank: "
                            + str(tg_output["symrank"])
                            + ", volatility: "
                            + str(tg_output["volatility"])
                            + " and price action: "
                            + str(tg_output["price_action"])
                            + " not meeting config filter limits - signal ignored"
                        )
                else:
                    logging.info(
                        str(tg_output["pair"])
                        + " is not traded on '"
                        + attributes.get("account_name")
                        + "'"
                    )
            else:
                if tg_output["signal"] == attributes.get(
                    "symrank_signal"
                ) and attributes.get("token_whitelist", []):
                    logging.info("Signal ignored because pair is not whitelisted")
                else:
                    logging.info(
                        "Signal ignored because '"
                        + attributes.get("symrank_signal")
                        + "' is configured"
                    )
        # if symrank list
        elif tg_output and isinstance(tg_output, list):
            if (
                not attributes.get("single")
                and not attributes.get("deal_mode", "", asyncState.dca_conf) == "signal"
                and not asyncState.symrank_success
            ):
                asyncState.symrank_success = True
                logging.info("New symrank list incoming - updating bot", True)
                if asyncState.multibot == {}:
                    bot_output = bot_data()
                else:
                    bot_output = asyncState.multibot

                # create/update and enable multibot with pairs from "/symrank"
                bot = MultiBot(
                    tg_output,
                    bot_output,
                    account_output,
                    pair_output,
                    attributes,
                    p3cw,
                    logging,
                    asyncState,
                )
                bot.create()
                asyncState.bot_active = bot.asyncState.bot_active
                asyncState.multibot = bot.bot_data
                # if deal_mode == signal configured, trigger a deal if random_pair == true
                if attributes.get(
                    "deal_mode", "", asyncState.dca_conf
                ) == "signal" and attributes.get("random_pair", "False"):
                    if (
                        asyncState.multibot["active_deals_count"]
                        != asyncState.multibot["max_active_deals"]
                    ):
                        bot.trigger(random_only=True)
                        asyncState.multibot = bot.bot_data
                    else:
                        logging.info(
                            "No random deal for filtered coins started because "
                            + str(asyncState.multibot["active_deals_count"])
                            + "/"
                            + str(asyncState.multibot["max_active_deals"])
                            + " deals already active"
                        )
                notification.send_notification()

            else:
                logging.debug(
                    "Ignoring /symrank call, because we're running in single mode!"
                )


async def main():

    signals = Signals(logging)
    asyncState.account_data = account_data()

    # Update available pair_data every 360 minutes for e.g. new blacklisted pairs or new tradable pairs
    pair_data_thread = Thread(
        target=pair_data,
        args=(
            asyncState.account_data,
            3600 * 6,
        ),
        daemon=True,
        name="Background update pair_data",
    )
    pair_data_thread.start()
    while not asyncState.pair_data:
        time.sleep(1)

    logging.debug("Refreshing cache...")

    user = await client.get_participants("The3CQSBot")
    asyncState.chatid = user[0].id

    logging.info("*** 3CQS Bot started ***", True)
    logging.info("** Configuration **", True)
    if attributes.get("single"):
        logging.info("Bot mode: 'Single Pair'", True)
    else:
        logging.info("Bot Mode: 'Multi Pair'", True)

    logging.info(
        "Listening to 3cqs signals: '" + str(attributes.get("symrank_signal")) + "'",
        True,
    )
    logging.info(
        "Topcoin filter: '" + str(attributes.get("topcoin_filter", False)) + "'", True
    )
    logging.info("BTC Pulse: '" + str(attributes.get("btc_pulse", False)) + "'", True)
    logging.info(
        "FGI Trading: '" + str(attributes.get("fearandgreed", False)) + "'", True
    )
    logging.info(
        "Continuous pair update: '"
        + str(attributes.get("continuous_update", False))
        + "'",
        True,
    )
    logging.info(
        "External/TV bot switching: '"
        + str(attributes.get("ext_botswitch", False))
        + "'",
        True,
    )
    logging.info("Quote currency: '" + str(attributes.get("market")) + "'")
    logging.info(
        "Token whitelist: '" + str(attributes.get("token_whitelist", "No")) + "'", True
    )

    # Check part of the config before starting the client
    if attributes.get("btc_pulse", False) and attributes.get("ext_botswitch", False):
        sys.tracebacklimit = 0
        sys.exit(
            "Check config.ini: btc_pulse AND ext_botswitch both set to true - not allowed"
        )

    # Create independent threads for FGI and tasks for BTC pulse up-/downtrend check
    if attributes.get("fearandgreed", False):

        fgi_thread = Thread(
            target=signals.get_fgi,
            args=(
                asyncState,
                attributes.get("fgi_ema_fast", 9),
                attributes.get("fgi_ema_slow", 50),
            ),
            daemon=True,
            name="Background signals.get_fgi",
        )
        fgi_thread.start()
        while asyncState.fgi == -1:
            time.sleep(1)

        fgi_dca_conf_change_thread = Thread(
            target=fgi_dca_conf_change,
            args=(asyncState.fgi_time_until_update + 30,),
            daemon=True,
            name="Background fgi_dca_conf_change",
        )
        fgi_dca_conf_change_thread.start()
        while not asyncState.dca_conf in [
            "fgi_defensive",
            "fgi_moderate",
            "fgi_aggressive",
        ]:
            time.sleep(1)

    logging.info("DCA settings: '[" + asyncState.dca_conf + "]'", True)

    if attributes.get("btc_pulse", False):
        btcpulse_thread = Thread(
            target=signals.getbtcpulse,
            args=(
                asyncState,
                300,
            ),
            daemon=True,
            name="Background signals.getbtcpulse",
        )
        btcpulse_thread.start()

    if attributes.get("fearandgreed", False) or attributes.get("btc_pulse", False):
        bot_switch_thread = Thread(
            target=bot_switch,
            args=(60,),
            daemon=True,
            name="Background bot_switch",
        )
        bot_switch_thread.start()
        while not asyncState.fgi_allows_trading:
            time.sleep(1)

    while True:
        while (
            asyncState.fgi_allows_trading
            and not asyncState.symrank_success
            and not attributes.get("single")
            and attributes.get("deal_mode", "", asyncState.dca_conf) != "signal"
        ):
            await symrank()


client.start()
client.loop.run_until_complete(main())
client.run_until_disconnected()
