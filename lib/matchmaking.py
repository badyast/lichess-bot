"""Challenge other bots."""
import random
import logging
import datetime
import contextlib
from pathlib import Path
from lib import model
from lib.timer import Timer, days, seconds, minutes, years
from collections import defaultdict
from collections.abc import Sequence
from lib.lichess import Lichess, RateLimitedError
from lib.config import Configuration
from typing import cast, TypeAlias
from lib.blocklist import OnlineBlocklist
from lib.lichess_types import UserProfileType, PerfType, EventType, FilterType, ChallengeType
MULTIPROCESSING_LIST_TYPE: TypeAlias = Sequence[model.Challenge]

logger = logging.getLogger(__name__)


forever = years(1000)

class Matchmaking:
    """Challenge other bots."""

    def __init__(self, li: Lichess, config: Configuration, user_profile: UserProfileType) -> None:
        """Initialize values needed for matchmaking."""
        self.li = li
        self.variants = list(filter(lambda variant: variant != "fromPosition", config.challenge.variants))
        self.matchmaking_cfg = config.matchmaking
        self.user_profile = user_profile
        self.last_challenge_created_delay = Timer(seconds(25))  # Challenges expire after 20 seconds.
        self.last_game_ended_delay = Timer(minutes(self.matchmaking_cfg.challenge_timeout))
        self.last_user_profile_update_time = Timer(minutes(5))
        self.min_wait_time = seconds(60)  # Wait before new challenge to avoid api rate limits.
        # LOCAL PATCH: a declined challenge produced no game, so the full
        # rate-limit pause is mostly wasted idle time -- the next challenge
        # also goes to a *different* bot, because challenge_filter blocks
        # re-challenging the decliner for the same reason. Measured over 394
        # declines: a median of 63 s each, about seven hours of not
        # initiating anything. The pause exists for API rate limits, and
        # there is room: we create 9 challenges per hour (peak 22) where the
        # 60 s minimum would allow 60, and challenge creation has never been
        # rate-limited in this bot's whole log history (the 118 rate limits
        # on record all hit /api/stream/event, a different endpoint).
        self.min_wait_time_after_decline = seconds(10)
        # LOCAL PATCH: dasselbe Problem, andere Ursache. Scheitert die
        # Forderung, weil der *Gegner* sein Tageslimit von 100 Bot-Partien
        # erreicht hat, entsteht ebenfalls keine Partie -- und es ist nicht
        # einmal unser Limit. `handle_challenge_error_response` filtert
        # diesen Gegner ohnehin heraus, die naechste Forderung ginge also an
        # einen anderen der ueber 300 online stehenden Bots. Trotzdem wurde
        # die volle Frist abgewartet, weil `challenge()` den Zeitgeber
        # *vor* dem Versuch zurueckstellt und damit auch dann, wenn gar
        # nichts zustande kam.
        # Gemessen ueber elf Tage: 107 Faelle, rund 10 bis 13 taeglich, je
        # 60 s -- etwa elf Minuten taeglich, in denen nichts gefordert wird.
        self.min_wait_time_after_failure = seconds(10)
        # LOCAL PATCH: Bei `tooFast`/`tooSlow` nennt der Gegner die
        # *Richtung* -- er will mehr oder weniger Bedenkzeit. Diese
        # Information wegzuwerfen und ihn stattdessen einen Tag lang fuer
        # diese Geschwindigkeit zu sperren, verschenkt einen Gegner, der
        # ausdruecklich gesagt hat, wie es klappen wuerde.
        #
        # Dasselbe gilt fuer `timeControl` ("nicht bei dieser Bedenkzeit"):
        # dort fehlt zwar die Richtung, aber wir bieten ohnehin nur zwei
        # Geschwindigkeiten an -- die andere ist damit wohldefiniert.
        #
        # Groessenordnung ueber elf Tage: `timeControl` 50 Faelle,
        # `tooFast`/`tooSlow` zusammen 8. Zum Vergleich `later` 60.
        #
        # Gemerkt wird `(Name, Art, abgelehnte Geschwindigkeit)`; die
        # naechste Herausforderung geht dann an genau diesen Gegner.
        self.nachfassen: tuple[str, str, str] | None = None
        # Je Gegner nur *ein* Nachfassen, sonst schaukelt es sich auf:
        # zu schnell -> langsameres Angebot -> "zu langsam" -> schnelleres ...
        self.nachgefasst: set[str] = set()
        self.last_challenge_failed = False
        self.last_challenge_was_declined = False
        self.rate_limit_timer = Timer()

        # Maximum time between challenges, even if there are active games
        self.max_wait_time = minutes(10) if self.matchmaking_cfg.allow_during_games else forever
        self.challenge_id = ""

        # (opponent name, game aspect) --> other bot is likely to accept challenge
        # game aspect is the one the challenged bot objects to and is one of:
        #   - game speed (bullet, blitz, etc.)
        #   - variant (standard, horde, etc.)
        #   - casual/rated
        #   - empty string (if no other reason is given or self.filter_type is COARSE)
        self.challenge_type_acceptable: defaultdict[tuple[str, str], Timer] = defaultdict(Timer)
        self.challenge_filter = self.matchmaking_cfg.challenge_filter

        for name in self.matchmaking_cfg.block_list:
            self.add_to_block_list(name)

        self.online_block_list = OnlineBlocklist(self.matchmaking_cfg.online_block_list)

        self.local_block_list: Path | None = None
        self.permablock = bool(self.matchmaking_cfg.challenge_decliner_file_name)
        if self.permablock:
            self.local_block_list = Path(self.matchmaking_cfg.challenge_decliner_file_name)
            self.read_local_block_list()

        # Bots that decline with "nobot" have a fixed account policy that won't change,
        # so these get permanently blocked regardless of challenge_decliner_file_name/permablock,
        # which would otherwise permablock every decline reason (too fast, wrong variant, etc.).
        self.nobot_block_list = Path("nobot_blocklist.txt")
        self.read_nobot_block_list()

    def read_nobot_block_list(self) -> None:
        """Read the list of bots that declined with 'nobot' in a previous session and block them forever."""
        if not self.nobot_block_list.exists():
            return

        logger.debug(f"Reading permanent nobot block list: {self.nobot_block_list}")
        with self.nobot_block_list.open(encoding="utf8") as local_list:
            for line_raw in local_list:
                name = line_raw.strip()
                if name:
                    self.add_challenge_filter(name, "", forever, add_to_file=False)

    def read_local_block_list(self) -> None:
        """Read the local block list file and reload blocks from previous session."""
        if not self.local_block_list or not self.local_block_list.exists():
            return

        if not self.local_block_list.is_file():
            raise ValueError(
                f"Configuration matchmaking: challenge_decliner_file_name: {self.local_block_list} is not a file.")

        logger.debug(f"Reading challenge decliner block list: {self.local_block_list}")
        with self.local_block_list.open(encoding="utf8") as local_list:
            for line_raw in local_list:
                line = line_raw.strip()
                if not line:
                    continue

                name, reason = line.split(",")
                self.add_challenge_filter(name, reason, forever, add_to_file=False)

    def effective_min_wait_time(self) -> datetime.timedelta:
        """The minimum pause before the next challenge, shortened after a decline.

        Kept in one place so that the decision in `should_create_challenge` and
        the time printed by `show_earliest_challenge_time` can never disagree.
        """
        if self.last_challenge_was_declined:
            return self.min_wait_time_after_decline
        if self.last_challenge_failed:
            return self.min_wait_time_after_failure
        return self.min_wait_time

    def should_create_challenge(self) -> bool:
        """Whether we should create a challenge."""
        matchmaking_enabled = self.matchmaking_cfg.allow_matchmaking
        rate_limit_ok = self.rate_limit_timer.is_expired()
        time_has_passed = self.last_game_ended_delay.is_expired()
        challenge_expired = self.last_challenge_created_delay.is_expired() and self.challenge_id
        min_wait_time_passed = (self.last_challenge_created_delay.time_since_reset()
                                > self.effective_min_wait_time())
        if challenge_expired:
            self.li.cancel(self.challenge_id)
            logger.info(f"Challenge id {self.challenge_id} cancelled.")
            self.discard_challenge(self.challenge_id)
            self.show_earliest_challenge_time()
        return bool(matchmaking_enabled and rate_limit_ok and (time_has_passed or challenge_expired) and min_wait_time_passed)

    def create_challenge(self, username: str, base_time: int, increment: int, days: int, variant: str,
                         mode: str) -> str:
        """Create a challenge."""
        params: dict[str, str | int | bool] = {"rated": mode == "rated", "variant": variant}

        if days:
            params["days"] = days
        elif base_time or increment:
            params["clock.limit"] = base_time
            params["clock.increment"] = increment
        else:
            logger.error("At least one of challenge_days, challenge_initial_time, or challenge_increment "
                         "must be greater than zero in the matchmaking section of your config file.")
            return ""

        try:
            self.last_challenge_created_delay.reset()
            self.last_challenge_was_declined = False  # LOCAL PATCH, see __init__
            self.last_challenge_failed = False  # LOCAL PATCH, see __init__
            response = self.li.challenge(username, params)
            challenge_id = response.get("id", "")
            if not challenge_id:
                self.handle_challenge_error_response(response, username)
            return challenge_id
        except RateLimitedError as e:
            logger.warning(e)
            self.rate_limit_timer = Timer(e.timeout)
        except Exception as e:
            logger.debug(e, exc_info=e)

        logger.warning("Could not create challenge")
        self.show_earliest_challenge_time()
        return ""

    def handle_challenge_error_response(self, response: ChallengeType, username: str) -> None:
        """If a challenge fails, print the error and adjust the challenge requirements in response."""
        logger.error(response)
        if response.get("bot_is_rate_limited"):
            timeout = cast(datetime.timedelta, response.get("rate_limit_timeout"))
            self.rate_limit_timer = Timer(timeout)
        elif response.get("opponent_is_rate_limited"):
            timeout = cast(datetime.timedelta, response.get("rate_limit_timeout"))
            self.add_challenge_filter(username, "", timeout, add_to_file=False)
            # LOCAL PATCH, siehe __init__: nicht unser Limit, kein Spiel
            # entstanden, und dieser Gegner ist ab jetzt gefiltert -- die
            # naechste Forderung geht an einen anderen Bot. Bewusst *nur*
            # hier gesetzt: bei `bot_is_rate_limited` ist es unser eigenes
            # Limit (dafuer gibt es `rate_limit_timer`), und beim
            # unbekannten Fehler im `else`-Zweig kennen wir die Ursache
            # nicht -- dort weiter zu draengeln waere unklug.
            self.last_challenge_failed = True
        else:
            self.add_challenge_filter(username, "", days(1), add_to_file=False)
        self.show_earliest_challenge_time()

    def perf(self) -> dict[str, PerfType]:
        """Get the bot's rating in every variant. Bullet, blitz, rapid etc. are considered different variants."""
        user_perf: dict[str, PerfType] = self.user_profile["perfs"]
        return user_perf

    def username(self) -> str:
        """Our username."""
        username: str = self.user_profile["username"]
        return username

    def update_user_profile(self) -> None:
        """Update our user profile data, to get our latest rating."""
        if self.last_user_profile_update_time.is_expired():
            self.last_user_profile_update_time.reset()
            with contextlib.suppress(Exception):
                self.user_profile = self.li.get_profile()

    def get_weights(self, online_bots: list[UserProfileType], rating_preference: str, min_rating: int, max_rating: int,
                    game_type: str) -> list[int]:
        """Get the weight for each bot. A higher weights means the bot is more likely to get challenged."""
        def rating(bot: UserProfileType) -> int:
            perfs: dict[str, PerfType] = bot.get("perfs", {})
            perf: PerfType = perfs.get(game_type, {})
            return perf.get("rating", 0)

        if rating_preference == "high":
            # A bot with max_rating rating will be twice as likely to get picked than a bot with min_rating rating.
            reduce_ratings_by = min(min_rating - (max_rating - min_rating), min_rating - 1)
            weights = [rating(bot) - reduce_ratings_by for bot in online_bots]
        elif rating_preference == "low":
            # A bot with min_rating rating will be twice as likely to get picked than a bot with max_rating rating.
            reduce_ratings_by = max(max_rating - (min_rating - max_rating), max_rating + 1)
            weights = [reduce_ratings_by - rating(bot) for bot in online_bots]
        else:
            weights = [1] * len(online_bots)
        return weights

    def choose_opponent(self) -> tuple[str | None, int, int, int, str, str]:
        """Choose an opponent."""
        override_choice = random.choice(self.matchmaking_cfg.overrides.keys() + [None])
        logger.info(f"Using the {override_choice or 'default'} matchmaking configuration.")
        override = {} if override_choice is None else self.matchmaking_cfg.overrides.lookup(override_choice)
        match_config = self.matchmaking_cfg | override

        variant = self.get_random_config_value(match_config, "challenge_variant", self.variants)
        mode = self.get_random_config_value(match_config, "challenge_mode", ["casual", "rated"])
        rating_preference = match_config.rating_preference

        base_time = random.choice(match_config.challenge_initial_time)
        increment = random.choice(match_config.challenge_increment)
        num_days = random.choice(match_config.challenge_days)

        play_correspondence = [bool(num_days), not bool(base_time or increment)]
        if random.choice(play_correspondence):
            base_time = 0
            increment = 0
        else:
            num_days = 0

        game_type = game_category(variant, base_time, increment, num_days)

        min_rating = match_config.opponent_min_rating
        max_rating = match_config.opponent_max_rating
        rating_diff = match_config.opponent_rating_difference
        bot_rating = self.perf().get(game_type, {}).get("rating", 0)
        if rating_diff is not None and bot_rating > 0:
            min_rating = bot_rating - rating_diff
            max_rating = bot_rating + rating_diff
        logger.info(f"Seeking {game_type} game with opponent rating in [{min_rating}, {max_rating}] ...")

        def is_suitable_opponent(bot: UserProfileType) -> bool:
            perf = bot.get("perfs", {}).get(game_type, {})
            return (bot["username"] != self.username()
                    and not self.in_block_list(bot["username"])
                    and perf.get("games", 0) > 0
                    and min_rating <= perf.get("rating", 0) <= max_rating)

        self.online_block_list.refresh()
        online_bots = self.li.get_online_bots()
        logger.info(f"Found {len(online_bots)} online bots")
        online_bots = list(filter(is_suitable_opponent, online_bots))
        logger.info(f"Choosing from {len(online_bots)} suitable opponents")

        def ready_for_challenge(bot: UserProfileType) -> bool:
            aspects = [variant, game_type, mode] if self.challenge_filter == FilterType.FINE else []
            return all(self.should_accept_challenge(bot["username"], aspect) for aspect in aspects)

        ready_bots = list(filter(ready_for_challenge, online_bots))
        online_bots = ready_bots or online_bots
        bot_username = None
        weights = self.get_weights(online_bots, rating_preference, min_rating, max_rating, game_type)

        try:
            bot = random.choices(online_bots, weights=weights)[0]
            bot_profile = self.li.get_public_data(bot["username"])
            if bot_profile.get("blocking"):
                self.add_to_block_list(bot["username"])
            else:
                bot_username = bot["username"]
        except Exception:
            if online_bots:
                logger.exception("Error:")
            else:
                logger.error("No suitable bots found to challenge.")

        return bot_username, base_time, increment, num_days, variant, mode

    def get_random_config_value(self, config: Configuration, parameter: str, choices: list[str]) -> str:
        """Choose a random value from `choices` if the parameter value in the config is `random`."""
        value: str = config.lookup(parameter)
        return value if value != "random" else random.choice(choices)

    def challenge(self, active_games: dict[str, str], challenge_queue: MULTIPROCESSING_LIST_TYPE,
                  max_bot_games: int) -> None:
        """
        Challenge an opponent.

        :param active_games: The games that the bot is playing (game ID -> opponent name).
        :param challenge_queue: The queue containing the challenges.
        :param max_bot_games: The maximum allowed number of simultaneous games against bots.
        """
        if not self.should_create_challenge():
            return

        # If matchmaking is not allowed while playing other games, don't create
        # a challenge when any game (against a bot or a human) is in progress.
        if not self.matchmaking_cfg.allow_during_games and active_games:
            return

        # Count only games against bots: matchmaking only challenges bots, and
        # human games must not count toward the bot-game limit (otherwise a bot
        # whose slots are filled by humans would stop matchmaking even though
        # bot slots remain free).
        bot_game_count = model.Player.count_bot_games(active_games) + len(challenge_queue)
        if (bot_game_count >= max_bot_games
                or (bot_game_count > 0 and self.last_challenge_created_delay.time_since_reset() < self.max_wait_time)):
            return

        # LOCAL PATCH: ein offenes Nachfassen hat Vorrang vor einem
        # Zufallsgegner -- der Gegner hat gerade gesagt, was er will.
        if self.nachfassen:
            name, art, alte_geschwindigkeit = self.nachfassen
            self.nachfassen = None
            self.nachgefasst.add(name)
            zeiten = self.matchmaking_cfg.challenge_initial_time
            inkremente = self.matchmaking_cfg.challenge_increment
            variante = self.get_random_config_value(self.matchmaking_cfg, "challenge_variant", self.variants)
            # `toofast` heisst "zu wenig Bedenkzeit fuer mich" -> mehr
            # anbieten; `tooslow` umgekehrt. Bei `timecontrol` nennt der
            # Gegner keine Richtung -- dort wird die erste konfigurierte
            # Kombination genommen, die in einer *anderen* Kategorie
            # landet als die abgelehnte.
            if art == "toofast":
                basis, ink = max(zeiten), max(inkremente)
            elif art == "tooslow":
                basis, ink = min(zeiten), min(inkremente)
            else:
                andere = [(b, i) for b in zeiten for i in inkremente
                          if game_category(variante, b, i, 0) != alte_geschwindigkeit]
                if not andere:
                    # Nur eine Kategorie konfiguriert: nichts anzubieten.
                    logger.info(f"No other time control configured - not retrying {name}.")
                    return
                basis, ink = andere[0]
            modus = self.get_random_config_value(self.matchmaking_cfg, "challenge_mode", ["casual", "rated"])

            logger.info(f"Retrying {name} with {basis}+{ink} after a {art} decline.")
            self.update_user_profile()
            challenge_id = self.create_challenge(name, basis, ink, 0, variante, modus)
            logger.info(f"Challenge id is {challenge_id or 'None'}.")
            self.challenge_id = challenge_id
            return

        logger.info("Challenging a random bot")
        self.update_user_profile()
        bot_username, base_time, increment, days, variant, mode = self.choose_opponent()
        if not bot_username:
            logger.info("No challenge will be created.")
            self.challenge_id = ""
            self.rate_limit_timer = Timer(seconds(60))
            return

        logger.info(f"Will challenge {bot_username} for a {variant} game.")
        challenge_id = self.create_challenge(bot_username, base_time, increment, days, variant, mode)
        logger.info(f"Challenge id is {challenge_id or 'None'}.")
        self.challenge_id = challenge_id

    def discard_challenge(self, challenge_id: str) -> None:
        """
        Clear the ID of the most recent challenge if it is no longer needed.

        :param challenge_id: The ID of the challenge that is expired, accepted, or declined.
        """
        if self.challenge_id == challenge_id:
            self.challenge_id = ""

    def game_done(self) -> None:
        """Reset the timer for when the last game ended, and prints the earliest that the next challenge will be created."""
        self.last_game_ended_delay.reset()
        self.show_earliest_challenge_time()

    def show_earliest_challenge_time(self) -> None:
        """Show the earliest that the next challenge will be created."""
        if self.matchmaking_cfg.allow_matchmaking:
            postgame_timeout = self.last_game_ended_delay.time_until_expiration()
            time_to_next_challenge = (self.effective_min_wait_time()
                                      - self.last_challenge_created_delay.time_since_reset())
            rate_limit_delay = self.rate_limit_timer.time_until_expiration()
            time_left = max(postgame_timeout, time_to_next_challenge, rate_limit_delay)
            earliest_challenge_time = datetime.datetime.now() + time_left
            logger.info(f"Next challenge will be created after {earliest_challenge_time.strftime('%c')}")

    def add_to_block_list(self, username: str) -> None:
        """Add a bot to the blocklist."""
        self.add_challenge_filter(username, "", forever, add_to_file=False)

    def in_block_list(self, username: str) -> bool:
        """Check if an opponent is in the block list to prevent future challenges."""
        return (not self.should_accept_challenge(username, "")) or username in self.online_block_list

    def add_challenge_filter(self,
                             username: str,
                             game_aspect: str,
                             timeout: datetime.timedelta,
                             *,
                             add_to_file: bool) -> None:
        """
        Prevent creating another challenge for a timeout when an opponent has declined a challenge.

        :param username: The name of the opponent.
        :param game_aspect: The aspect of a game (time control, chess variant, etc.) that caused the opponent to decline a
        challenge. If the parameter is empty, that is equivalent to adding the opponent to the block list.
        :param timeout: The amount of time to not challenge an opponent. If None, the default is a day.
        """
        timeout_timer = Timer(timeout)
        self.challenge_type_acceptable[(username, game_aspect)] = timeout_timer
        if add_to_file and self.local_block_list:
            with self.local_block_list.open("a", encoding="utf8") as block_list:
                block_list.write(f"{username},{game_aspect}\n")

    def should_accept_challenge(self, username: str, game_aspect: str) -> bool:
        """
        Whether a bot is likely to accept a challenge to a game.

        :param username: The name of the opponent.
        :param game_aspect: A category of the challenge type (time control, chess variant, etc.) to test for acceptance.
        If game_aspect is empty, this is equivalent to checking if the opponent is in the block list.
        """
        return self.challenge_type_acceptable[(username, game_aspect)].is_expired()

    def accepted_challenge(self, event: EventType) -> None:
        """
        Set the challenge id to an empty string, if the challenge was accepted.

        Otherwise, we would attempt to cancel the challenge later.
        """
        self.discard_challenge(event["game"]["id"])

    def declined_challenge(self, event: EventType) -> None:
        """
        Handle a challenge that was declined by the opponent.

        Depends on whether `FilterType` is `NONE`, `COARSE`, or `FINE`.
        """
        challenge = model.Challenge(event["challenge"], self.user_profile)
        opponent = challenge.challenge_target
        reason = event["challenge"]["declineReason"]
        logger.info(f"{opponent} declined {challenge}: {reason}")
        self.discard_challenge(challenge.id)
        self.last_challenge_was_declined = True  # LOCAL PATCH, see __init__
        if not challenge.from_self or self.challenge_filter == FilterType.NONE:
            return

        reason_key = event["challenge"]["declineReasonKey"].lower()
        if reason_key == "nobot":
            self.add_challenge_filter(opponent.name, "", forever, add_to_file=False)
            with self.nobot_block_list.open("a", encoding="utf8") as block_list:
                block_list.write(f"{opponent.name}\n")
            logger.info(f"{opponent} does not accept challenges from bots - permanently blocked.")
            self.show_earliest_challenge_time()
            return

        mode = "rated" if challenge.rated else "casual"
        decline_details: dict[str, str] = {"generic": "",
                                           "later": "",
                                           "nobot": "",
                                           "toofast": challenge.speed,
                                           "tooslow": challenge.speed,
                                           "timecontrol": challenge.speed,
                                           "rated": mode,
                                           "casual": mode,
                                           "standard": challenge.variant,
                                           "variant": challenge.variant}

        if reason_key not in decline_details:
            logger.warning(f"Unknown decline reason received: {reason_key}")
        game_problem = decline_details.get(reason_key, "") if self.challenge_filter == FilterType.FINE else ""

        # LOCAL PATCH: "later" heisst woertlich "frag spaeter nochmal" -- der
        # Gegner sagt zu *diesem Zeitpunkt* ab, nicht zu uns oder zu dieser
        # Partieform. Ihn dafuer einen ganzen Tag zu sperren verschenkt
        # genau die Gegner, die uns spielen wollen.
        #
        # Haeufigster Ablehnungsgrund ueberhaupt: **60 von 200** unserer
        # abgelehnten Herausforderungen (30 %), gemessen ueber elf Tage.
        # Die naechsthaeufigen sind "derzeit keine" (53) und "nicht bei
        # dieser Bedenkzeit" (50); nur bei "later" nennt der Gegner
        # ausdruecklich die Zeit als Grund.
        #
        # 30 Minuten statt eines Tages, und **nicht** in die Datei der
        # dauerhaft gemiedenen Gegner geschrieben -- eine voruebergehende
        # Absage gehoert nicht in eine dauerhafte Liste.
        # LOCAL PATCH, siehe __init__: Richtung merken und beim naechsten
        # Zyklus mit angepasster Bedenkzeit nachfassen.
        if (reason_key in ("toofast", "tooslow", "timecontrol")
                and opponent.name not in self.nachgefasst):
            self.nachfassen = (opponent.name, reason_key, challenge.speed)
            wunsch = {"toofast": "slower", "tooslow": "faster"}.get(reason_key, "different")
            logger.info(f"{opponent} wants a {wunsch} time control - will retry with one.")

        spaeter = reason_key == "later" and not self.permablock
        if spaeter:
            timeout = minutes(30)
            in_datei = False
        else:
            timeout = forever if self.permablock else days(1)
            in_datei = True
        self.add_challenge_filter(opponent.name, game_problem, timeout, add_to_file=in_datei)
        if spaeter:
            logger.info(f"{opponent} asked to be challenged later - retrying in 30 minutes.")
        else:
            time_span = "" if self.permablock else " today"
            logger.info(f"Will not challenge {opponent} to another {game_problem}".strip()
                        + f" game{time_span}.")

        self.show_earliest_challenge_time()


def game_category(variant: str, base_time: int, increment: int, num_days: int) -> str:
    """
    Get the game type (e.g. bullet, atomic, classical). Lichess has one rating for every variant regardless of time control.

    :param variant: The game's variant.
    :param base_time: The base time in seconds.
    :param increment: The increment in seconds.
    :param num_days: If the game is correspondence, we have some days to play the move.
    :return: The game category.
    """
    game_duration = base_time + increment * 40
    if variant != "standard":
        return variant
    if num_days:
        return "correspondence"
    if game_duration < 179:
        return "bullet"
    if game_duration < 479:
        return "blitz"
    if game_duration < 1499:
        return "rapid"
    return "classical"
