import hashlib
import itertools
from datetime import datetime

from .gossip_utils import *

from hivemind_exp.dht_utils import *
from hivemind_exp.name_utils import get_name_from_peer_id
from .gossip_utils import stage1_message, stage2_message, stage3_message


class Cache:
    def __init__(self, dht, coordinator, manager, logger):
        self.dht = dht
        self.coordinator = coordinator

        self.manager = manager
        self.logger = logger

        self.lock = manager.Lock()
        self.reset()

    def reset(self):
        self.leaderboard = self.manager.dict()
        self.leaderboard_v2 = self.manager.dict() # Cumulative rewards leaderboard.

        self.rewards_history = self.manager.dict()
        self.gossips = self.manager.dict()

        self.current_round = self.manager.Value("i", -1)
        self.current_stage = self.manager.Value("i", -1)

        self.last_polled = None

    def get_round_and_stage(self):
        return self.current_round.value, self.current_stage.value

    def get_leaderboard(self):
        return dict(self.leaderboard)

    def get_leaderboard_cumulative(self):
        return dict(self.leaderboard_v2)

    def get_gossips(self, since_round=0):
        return dict(self.gossips)

    def get_last_polled(self):
        return self.last_polled

    def poll_dht(self):
        try:
            self._get_round_and_stage()
            self._get_leaderboard()
            self._get_leaderboard_v2()
            self._get_gossip()

            with self.lock:
                self.last_polled = datetime.now()
        except Exception as e:
            self.logger.error("cache failed to poll dht: %s", e)

    def _get_dht_value(self, beam_size=100, **kwargs):
        return get_dht_value(self.dht, beam_size=beam_size, **kwargs)

    def _get_round_and_stage(self):
        try:
            r, s = self.coordinator.get_round_and_stage()
            self.logger.info(f"cache polled round and stage: r={r}, s={s}")
            with self.lock:
                self.current_round.value = r
                self.current_stage.value = s
        except ValueError as e:
            self.logger.warning(
                "could not get current round or stage; default to -1: %s", e
            )

    def _last_round_and_stage(self, round_num, stage):
        r = round_num
        s = stage - 1
        if s == -1:
            s = 2
            r -= 1

        return max(0, r), max(0, s)

    def _current_rewards(self) -> dict[str, Any] | None:
        # Basically a proxy for the reachable peer group.
        curr_round = self.current_round.value
        curr_stage = self.current_stage.value
        return self._get_dht_value(key=rewards_key(curr_round, curr_stage), latest=True)

    def _get_leaderboard_v2(self):
        try:
            rewards = self._current_rewards()
            if not rewards:
                return None

            curr_round = self.current_round.value
            curr_stage = self.current_stage.value
            
            with self.lock:
                # Initialize or get existing leaderboard_v2
                if "leaders" not in self.leaderboard_v2:
                    self.leaderboard_v2 = {"leaders": []}
                
                # Create a map of existing entries for easy lookup
                existing_entries = {entry["id"]: entry for entry in self.leaderboard_v2["leaders"]}
                
                # Process each peer's rewards
                for peer_id, score in rewards.items():
                    if peer_id not in existing_entries:
                        # First time seeing this peer
                        existing_entries[peer_id] = {
                            "id": peer_id,
                            "nickname": get_name_from_peer_id(peer_id),
                            "recordedRound": curr_round,
                            "recordedStage": curr_stage,
                            "cumulativeScore": float(score),  # Initial score
                            "lastScore": float(score)  # Track last score
                        }
                    else:
                        entry = existing_entries[peer_id]
                        # Same round/stage - just update current score
                        if (entry["recordedRound"] == curr_round and 
                            entry["recordedStage"] == curr_stage):
                            entry["cumulativeScore"] = float(score)
                            entry["lastScore"] = float(score)  # Update last score
                        # Different round/stage - add to cumulative
                        else:
                            entry["cumulativeScore"] += float(score)
                            entry["lastScore"] = float(score)  # Update last score
                            entry["recordedRound"] = curr_round
                            entry["recordedStage"] = curr_stage

                # Remove entries that are not in the current or previous round/stage.
                prev_round, prev_stage = self._last_round_and_stage(curr_round, curr_stage)
                current_entries = {}
                for peer_id, entry in existing_entries.items():
                    in_current = (entry["recordedRound"] == curr_round and entry["recordedStage"] == curr_stage)
                    in_prev = (entry["recordedRound"] == prev_round and entry["recordedStage"] == prev_stage)
                    if in_current or in_prev:
                        current_entries[peer_id] = entry

                # Convert back to sorted list
                sorted_leaders = sorted(
                    current_entries.values(),
                    key=lambda x: (x["cumulativeScore"], x["id"]),
                    reverse=True
                )

                # Update leaderboard_v2
                self.leaderboard_v2 = {
                    "leaders": sorted_leaders,
                    "total": len(sorted_leaders)
                }

                return self.leaderboard_v2

        except Exception as e:
            self.logger.warning("could not get leaderboard data: %s", e)
            return None

    def _get_leaderboard(self):
        try:
            rewards = self._current_rewards()
            if rewards:
                # Sorted list of (node_key, reward) pairs.
                raw = list(
                    sorted(rewards.items(), key=lambda t: (t[1], t[0]), reverse=True)
                )
            else:
                raw = []

            # Create entries for all participants
            all_entries = [
                {
                    "id": str(t[0]),
                    "nickname": get_name_from_peer_id(t[0]),
                    "score": t[1],
                    "values": [],
                }
                for t in raw
            ]
            self.logger.info(">>> lb_entries length: %d", len(all_entries))

            current_history = []
            with self.lock:
                for entry in all_entries:
                    latestScore = entry["score"]
                    id = entry["id"]
                    nn = entry["nickname"]

                    past_scores = self.rewards_history.get(id, [])
                    next_scores = (
                        past_scores
                        + [{"x": int(datetime.now().timestamp()), "y": latestScore}][
                            -100:
                        ]
                    )
                    self.logger.info(
                        ">>> id: %s, past_scores length: %d, next_scores length: %d",
                        id,
                        len(past_scores),
                        len(next_scores),
                    )
                    self.rewards_history[id] = next_scores
                    current_history.append(
                        {
                            "id": id,
                            "nickname": nn,
                            "values": next_scores,
                        }
                    )

            with self.lock:
                self.leaderboard = {
                    "leaders": all_entries,
                    "total": len(raw),
                    "rewardsHistory": current_history,
                }
        except Exception as e:
            self.logger.warning("could not get leaderboard data: %s", e)

    def _get_gossip(self):
        STAGE_GOSSIP_LIMIT = 10  # Most recent.
        STAGE_MESSAGE_FNS = [stage1_message, stage2_message, stage3_message]

        round_gossip = []
        start_time = datetime.now()
        try:
            curr_round = self.current_round.value
            curr_stage = self.current_stage.value
            curr_rewards = self._current_rewards()
            if not curr_rewards:
                raise ValueError("missing curr_rewards")

            nodes = curr_rewards.keys()
            start_round = max(0, curr_round - 3)
            for round_num, stage, node_key in itertools.product(
                range(start_round, curr_round + 1),
                range(0, 3),
                nodes,
            ):
                # Check if we've exceeded 10 seconds
                # Adding this as a stop gap to make sure the gossip collection doesn't stop other data from being polled.
                if (datetime.now() - start_time).total_seconds() > 10:
                    self.logger.warning(">>> gossip collection timed out after 10s")
                    break

                if round_num > curr_round or (
                    round_num == curr_round and stage > curr_stage
                ):
                    break

                key = outputs_key(node_key, round_num, stage)
                if outputs := self._get_dht_value(key=key):
                    sorted_outputs = sorted(
                        list(outputs.items()), key=lambda t: t[1][0]
                    )
                    for question, (ts, outputs) in sorted_outputs[-STAGE_GOSSIP_LIMIT:]:
                        gossip_id = hashlib.md5(
                            f"{node_key}_{round_num}_{stage}_{question}".encode()
                        ).hexdigest()
                        if stage < len(STAGE_MESSAGE_FNS):
                            message = STAGE_MESSAGE_FNS[stage](
                                node_key, question, ts, outputs
                            )
                        else:
                            message = f"Cannot render output for unknown stage {stage}"
                        round_gossip.append(
                            (
                                ts,
                                {
                                    "id": gossip_id,
                                    "message": message,
                                    "node": get_name_from_peer_id(node_key),
                                },
                            )
                        )
        except Exception as e:
            self.logger.warning("could not get gossip: %s", e)
        finally:
            elapsed = (datetime.now() - start_time).total_seconds()
            self.logger.info(
                ">>> completed gossip with %d messages in %.2fs",
                len(round_gossip),
                elapsed,
            )

        with self.lock:
            self.gossips = {
                "messages": [msg for _, msg in sorted(round_gossip, reverse=True)]
                or [],
            }
