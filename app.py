"""
White Martian - Watchtower
A live, multi-device game tracker for the host's Mafia/Werewolf variant.

Run:  python app.py
Host device:   http://localhost:5000/host
Player phones: http://<host-machine-LAN-IP>:5000/play   (same WiFi)

Architecture:
- One process holds the single source of truth (GAME state, below).
- The host page can change anything. Player pages are read-mostly + can cast a vote.
- Every change is broadcast over Socket.IO to every connected browser, so the
  host's laptop and every player's phone always show the same live state.
  No page-refresh, no polling.
- Privacy: the host console needs to see which real player is playing which
  character. Player phones must NOT see that mapping for anyone but
  themselves - Joe should learn he's Superman without learning who's playing
  Batman. So two different payloads go out on every change: hosts (room
  "hosts") get the full state including every player_name; players (room
  "players") get a stripped copy with player_name removed. Each registered
  player also privately gets a "whoami_result" telling them only their own
  character(s), computed server-side and sent to their socket id alone.
"""
import socket
import json
import re
import random
from pathlib import Path
from flask import Flask, render_template, request
from flask_socketio import SocketIO, join_room

from characters import (
    CHARACTERS, CHARACTERS_BY_ID, TEAM_LABELS, TEAM_COLORS, PHASES, NUM_ROUNDS,
    MAX_HEALTH, SHIELD_START, COLUMN_COLORS, DCEU_GRID, DCEU_LOCATIONS, PHASE_INFO,
    NARRATION_PROMPTS, INTRO_SCRIPT, PACKS, PACK_LABELS, SWITCH_CHARACTERS,
    HOSTAGE_ABILITIES, KRYPTONIAN_IDS, KNOWS_IDENTITY_OF,
)

CARDS = json.loads((Path(__file__).parent / "cards.json").read_text())

# Pull the trailing "(Protect!)" / "(Accuse!)" style tag off each ability's
# text so we can remind a player of their move when that phase comes up.
# e.g. "...may shield another player (Protect!)" -> tagged under "Protect".
_PHASE_TAG_RE = re.compile(r"[/(]([A-Za-z]+)!\)")

# ------------------------------------------------------------------------
# Player-facing conditions. Each maps to one of the existing action
# buttons; clicking it toggles a persistent flag (not just a one-off log
# entry) and, when turned ON, privately alerts whoever's playing that
# character with the exact rules text. At the start of every round, anyone
# with an active condition gets a recap of everything still in effect.
# ------------------------------------------------------------------------
CONDITIONS = {
    "expose": {
        "flag": "exposed",
        "title": "Exposed!",
        "body": "Everyone now knows which Hero, Villain, or Martian you are. "
                "You may no longer use your Active or Super Ability.",
    },
    "end": {
        "flag": "eliminated",
        "title": "Eliminated!",
        "body": "You were successfully targeted by the White Martians without "
                "interference. You may no longer Discuss! or Vote!.",
    },
    "watchtower": {
        "flag": "rescued",
        "title": "Rescued!",
        "body": "You are in the safety zone of Watchtower. You may still "
                "Discuss and Vote, but can no longer be targeted for "
                "elimination or teleportation, and may no longer use your "
                "Passive, Active, or Super Ability.",
    },
    "teleport": {
        "flag": "targeted",
        "title": "Targeted!",
        "body": "You've been chosen during Discuss! to be Rescued, or during "
                "Eliminate! to be eliminated.",
    },
}

PHASE_SET_LOWER = {p.lower() for p in PHASES}

ABILITY_PHASE_MAP = {}  # cid -> {phase_name: [ability_text, ...]}
for _cid, _card in CARDS.items():
    for _ability in _card.get("abilities", []):
        for _tag in _PHASE_TAG_RE.findall(_ability):
            if _tag.lower() in PHASE_SET_LOWER:
                phase_name = next(p for p in PHASES if p.lower() == _tag.lower())
                ABILITY_PHASE_MAP.setdefault(_cid, {}).setdefault(phase_name, []).append(_ability)

app = Flask(__name__)
app.config["SECRET_KEY"] = "white-martian-watchtower"
socketio = SocketIO(app, cors_allowed_origins="*")


def fresh_character_state():
    state = {}
    for c in CHARACTERS:
        # Switchable characters with a shield don't get it until revealed -
        # their card ties the shield to a Hero-only ability.
        shield_locked_pending_reveal = c["is_switchable"] and c["has_shield"]
        state[c["id"]] = {
            "active": False,
            "player_name": "",
            "health": c["start_health"] if c["has_health"] else None,
            "protection": [False, False, False],
            "last_action": None,     # e.g. "Watchtower", "Exposed", "Deactivated"
            "shield": None if shield_locked_pending_reveal else (SHIELD_START if c["has_shield"] else None),
            "cuffed": False if c["has_cuffs"] else None,
            "cured": False if c["has_cure"] else None,
            "fixed": False if c["has_fixit"] else None,
            "revealed": False if c["is_switchable"] else None,
            "hostage": False,
            "exposed": False,
            "eliminated": False,
            "rescued": False,
            "targeted": False,
        }
    return state


def fresh_map_state():
    return {name: False for name in DCEU_LOCATIONS}  # False = not blacked out


FREE_PACK_IDS = {p["id"] for p in PACKS if p.get("free")}

GAME = {
    "round": 1,
    "phase_index": None,   # index into PHASES, or None if no phase active
    "characters": fresh_character_state(),
    "votes": {},            # voter_name -> target_character_id
    "activity": [],         # small rolling feed of recent actions
    "map": fresh_map_state(),
    "players": [],          # [{"name": str, "eliminated": bool}, ...] join order
    "roster_locked": False,
    "unlocked_packs": set(FREE_PACK_IDS),
    "last_vote_winner": None,        # character id, captured when Vote phase ends
    "round_events": {"rescued": [], "eliminated": []},  # this round, so far
    "round_history": {},             # round_number -> {"rescued":[ids],"eliminated":[ids]}
    "super_abilities_announced": False,
    "hostage_event": None,
    "game_over": None,
    "timer": None,
    "pending_inspection": None,
    "active_inspector_cid": None,
}


def is_unlocked(character_id):
    pack = CHARACTERS_BY_ID.get(character_id, {}).get("pack")
    if pack is None:
        return False
    return pack in GAME["unlocked_packs"]


def display_name_for(cid):
    """The character's currently-showing name - their secret identity once
    revealed, their ordinary civilian name until then."""
    c = CHARACTERS_BY_ID.get(cid)
    if not c:
        return cid
    if c["is_switchable"]:
        st = GAME["characters"].get(cid, {})
        if st.get("revealed"):
            return c["reveal_name"]
    return c["name"]


_ABILITY_TYPE_RE = re.compile(r"type:?\s*(civilian|hero|villain)", re.IGNORECASE)


def _ability_visible_to_player(ability_text, revealed):
    """Card text for switch characters tags each ability with which state
    it belongs to (e.g. "*Type: Civilian only", "**Type: Hero only"). An
    ability with no such tag applies either way. Only used for the
    player-facing My Card view - the host always sees everything."""
    m = _ABILITY_TYPE_RE.search(ability_text)
    if not m:
        return True
    tag = m.group(1).lower()
    if tag == "civilian":
        return not revealed
    return revealed  # hero or villain tag = only visible after reveal


def log_activity(text):
    GAME["activity"].insert(0, text)
    GAME["activity"] = GAME["activity"][:5]


def vote_tally():
    """Tally by real player name now (not character id) - see cast_vote."""
    tally = {}
    for target_name in GAME["votes"].values():
        tally[target_name] = tally.get(target_name, 0) + 1
    return sorted(tally.items(), key=lambda kv: -kv[1])


def vote_candidates():
    """Real names of every player currently behind an active character -
    the pool of people who can be voted for. Deliberately just names, with
    no character id attached, so the payload can't be used to reconstruct
    who's playing whom even by someone inspecting raw network traffic.
    """
    return [
        st["player_name"] for st in GAME["characters"].values()
        if st["active"] and st.get("player_name")
    ]


def eliminate_candidates():
    """Real names of active, non-Martian players - the pool White Martians
    vote on to eliminate. Martians don't vote each other off."""
    return [
        st["player_name"] for cid, st in GAME["characters"].items()
        if st["active"] and st.get("player_name")
        and CHARACTERS_BY_ID.get(cid, {}).get("team") != "martian"
    ]


def has_martian_inspect_ability(cid):
    """True if this character's Inspect-tagged ability is specifically the
    'ask Watchtower if this player is a Martian' kind (detected by the
    ability text itself mentioning Martian), as opposed to some other
    Inspect-tagged effect like Dr. Alchemy's type-swap."""
    for a in ABILITY_PHASE_MAP.get(cid, {}).get("Inspect", []):
        if "martian" in a.lower():
            return True
    return False


def active_player_names(exclude_name=None):
    """Real names of every active, assigned player, optionally excluding
    one (e.g. the player asking shouldn't be able to inspect themselves)."""
    names = [
        st["player_name"] for st in GAME["characters"].values()
        if st["active"] and st.get("player_name")
    ]
    if exclude_name:
        names = [n for n in names if n.strip().lower() != exclude_name.strip().lower()]
    return names


def _resolve_identity_targets(specs):
    """Expand a list of target specs (literal character ids, or
    'team:<name>' for a whole team) into concrete character ids."""
    ids = []
    for spec in specs:
        if spec.startswith("team:"):
            team = spec.split(":", 1)[1]
            ids.extend(
                cid for cid, c in CHARACTERS_BY_ID.items() if c.get("team") == team
            )
        else:
            ids.append(spec)
    return ids


def compute_secret_identity_reveals():
    """For every active, assigned character with a 'knows identity of X'
    passive (X being a single character or a whole team), one entry per
    resolved target that's also active and assigned. Used both to push
    the private player alerts (grouped per-asker) and to show the host a
    summary of who was told what."""
    reveals = []
    for asker_cid, specs in KNOWS_IDENTITY_OF.items():
        asker_st = GAME["characters"].get(asker_cid)
        if not asker_st or not asker_st["active"] or not asker_st.get("player_name"):
            continue
        for target_cid in _resolve_identity_targets(specs):
            if target_cid == asker_cid:
                continue  # never "reveal" yourself to yourself
            target_st = GAME["characters"].get(target_cid)
            if not target_st or not target_st["active"] or not target_st.get("player_name"):
                continue
            reveals.append({
                "asker_id": asker_cid,
                "asker_name": display_name_for(asker_cid),
                "asker_player": asker_st["player_name"],
                "target_id": target_cid,
                "target_name": display_name_for(target_cid),
                "target_player": target_st["player_name"],
            })
    return reveals


def push_secret_identity_reveals():
    by_asker = {}
    for reveal in compute_secret_identity_reveals():
        by_asker.setdefault(reveal["asker_player"], []).append(reveal)
    for asker_player, reveals in by_asker.items():
        sid = _sid_for_player(asker_player)
        if sid:
            socketio.emit("secret_identity_reveal", {
                "reveals": [
                    {"target_player": r["target_player"], "target_name": r["target_name"]}
                    for r in reveals
                ]
            }, room=sid)


def eligible_inspectors():
    """Active characters whose Inspect ability is the 'ask Watchtower if
    a player is a Martian' kind, and whose ability is currently visible
    (i.e. not a locked Super Ability before Round 3). Powers the host's
    Inspect-phase wizard."""
    return [
        {"id": cid, "name": display_name_for(cid)}
        for cid, st in GAME["characters"].items()
        if st["active"] and has_martian_inspect_ability(cid)
        and _visible_phase_abilities(cid, "Inspect")
    ]


def spotlight_characters():
    """Active character ids whose card has an ability tagged for whatever
    phase is currently selected - used to highlight them on the host
    console as a "hey, this character has a move right now" reminder."""
    idx = GAME["phase_index"]
    if idx is None:
        return []
    phase = PHASES[idx]
    return [
        cid for cid, st in GAME["characters"].items()
        if st["active"] and phase in ABILITY_PHASE_MAP.get(cid, {})
    ]


_PLACEHOLDER_ABILITY_RE = re.compile(r"Name\.\s*Description\.\s*\(Phase!\)", re.IGNORECASE)
_CORRUPTED_SUPER_RE = re.compile(r"\bER ABILITY\.", re.IGNORECASE)  # e.g. "SMPTHINGER ABILITY" - a "SUPER" typo


def has_draft_content(cid):
    """True if this character's card still has unfinished placeholder text
    left over from the original file (a generic 'Name. Description.
    (Phase!)' stand-in, or the 'SUPER' typo that left prefixes like
    'SMPTHINGER ABILITY' instead of 'SUPER ABILITY')."""
    for a in CARDS.get(cid, {}).get("abilities", []):
        if _PLACEHOLDER_ABILITY_RE.search(a) or _CORRUPTED_SUPER_RE.search(a):
            return True
    return False


def draft_characters():
    """All character ids whose card still has unfinished content, regardless
    of whether they're currently active - a host-facing reminder of what
    still needs real writing."""
    return [cid for cid in CHARACTERS_BY_ID if has_draft_content(cid)]


def real_super_ability(cid):
    """This character's Super Ability text, or None if they don't have one
    or it's still the unfinished 'Name. Description. (Phase!)' placeholder
    left over in the original file."""
    for a in CARDS.get(cid, {}).get("abilities", []):
        if "SUPER ABILITY" in a.upper() and not _PLACEHOLDER_ABILITY_RE.search(a):
            return a
    return None


def super_active_characters():
    """Active character ids with a real Super Ability, once Round 3 or
    later - Super Abilities don't switch on until then."""
    if GAME["round"] < 3:
        return []
    return [
        cid for cid, st in GAME["characters"].items()
        if st["active"] and real_super_ability(cid)
    ]


def _join_names(names):
    """'A' / 'A and B' / 'A, B, and C' - falls back to 'no one' when empty."""
    names = [n for n in names if n]
    if not names:
        return "no one"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def _active_names_by_team(team):
    return [
        display_name_for(cid)
        for cid, st in GAME["characters"].items()
        if st["active"] and CHARACTERS_BY_ID.get(cid, {}).get("team") == team
    ]


def render_phase_script():
    """Build the exact line(s) the moderator should read aloud for whatever
    phase is currently active, filled in from live game state. Returns
    None when no phase is selected.
    """
    idx = GAME["phase_index"]
    if idx is None:
        return None
    phase = PHASES[idx]

    if phase == "Secret Identity":
        reveals = compute_secret_identity_reveals()
        if reveals:
            lines = [
                f"Told {r['asker_player']} ({r['asker_name']}): {r['target_player']} is {r['target_name']}."
                for r in reveals
            ]
        else:
            lines = ["No active, assigned characters currently have a matching "
                     "Know You Anywhere-style ability to reveal."]
        result = {"phase": "Secret Identity", "kind": "static", "lines": lines}

    elif phase == "Report":
        prev_round = GAME["round"] - 1
        history = GAME["round_history"].get(prev_round)
        if GAME["round"] <= 1 or not history:
            heroes = _join_names(_active_names_by_team("hero"))
            civilians = _join_names(_active_names_by_team("civilian"))
            villains = _join_names(_active_names_by_team("villain"))
            martian_count = sum(
                1 for cid, st in GAME["characters"].items()
                if st["active"] and CHARACTERS_BY_ID.get(cid, {}).get("team") == "martian"
            )
            martian_label = "White Martian" if martian_count == 1 else "White Martians"
            text = (
                f"...I'm sending {heroes} to the Martian Prison to rescue {civilians}. "
                f"You're surprised to find {villains} in the prison as well. "
                f"Scanners indicate at least {martian_count} {martian_label} among you."
            )
            result = {"phase": "Report", "kind": "briefing", "lines": [text]}
        else:
            rescued = [display_name_for(c) for c in history["rescued"] if c in CHARACTERS_BY_ID]
            eliminated = [display_name_for(c) for c in history["eliminated"] if c in CHARACTERS_BY_ID]
            rescued_clause = (
                f"I safely beamed {_join_names(rescued)} up to Watchtower."
                if rescued else "No one made it to Watchtower last round."
            )
            eliminated_clause = (
                f"Unfortunately, {_join_names(eliminated)} didn't survive the night."
                if eliminated else "Everyone else made it through the night safely."
            )
            text = f"Welcome back. {rescued_clause} {eliminated_clause}"
            result = {"phase": "Report", "kind": "recap", "lines": [text]}

    elif phase == "Discuss":
        result = {"phase": "Discuss", "kind": "static", "lines": [
            "Booting up the teleporter. You've got two minutes to discuss who you want to send to Watchtower."
        ]}

    elif phase == "Vote":
        nominees = _join_names(vote_candidates())
        lines = [f"1. Raise your hand if you want {nominees} to reach Watchtower?"]
        tally = vote_tally()
        if tally:
            lines.append(f"2. Calibrating teleporter. Keep still {tally[0][0]}.")
        else:
            lines.append("2. Calibrating teleporter\u2026 (waiting for votes)")
        result = {"phase": "Vote", "kind": "live", "lines": lines}

    elif phase == "Accuse":
        result = {"phase": "Accuse", "kind": "static", "lines": [
            "...Any accusations of identity I need to log?"
        ]}

    elif phase == "Rescue":
        winner_name = GAME["last_vote_winner"] or "the winner"
        result = {"phase": "Rescue", "kind": "static", "lines": [
            f"I'm beaming up {winner_name}",
            "MIND THE FLASH OF THE TELEPORTER BEAM. EVERYONE, EYES CLOSED!",
        ]}

    elif phase == "Eliminate":
        martian_count = sum(
            1 for cid, st in GAME["characters"].items()
            if st["active"] and CHARACTERS_BY_ID.get(cid, {}).get("team") == "martian"
        )
        martian_label = "White Martian" if martian_count == 1 else "White Martians"
        result = {"phase": "Eliminate", "kind": "static", "lines": [
            f"1. {martian_label} open your eyes.",
            "2. Vote on who to eliminate.",
            "3. Close eyes.",
        ]}

    elif phase == "Protect":
        shield_actors = [
            cid for cid, st in GAME["characters"].items()
            if st["active"] and CHARACTERS_BY_ID.get(cid, {}).get("has_shield")
            and st.get("shield") is not None  # skip switch characters not yet revealed
        ]
        if shield_actors:
            lines = [
                f"{display_name_for(cid)}, open your eyes. Choose one other player "
                f"to shield... All right, now close your eyes."
                for cid in shield_actors
            ]
        else:
            lines = ["No active character currently has a Protect/Shield ability."]
        result = {"phase": "Protect", "kind": "steps", "lines": lines}

    elif phase == "Inspect":
        result = {"phase": "Inspect", "kind": "interactive", "lines": []}

    else:
        return None

    return result



def public_state(reveal_names):
    """Everything the frontend needs to render, in one payload.

    reveal_names=True (host only) includes each character's assigned player
    name, plus the raw voter->target map. reveal_names=False (players)
    strips both: player_name is blanked so no phone can see who's playing
    whom (each player privately learns their own via 'whoami_result'), and
    the vote data they get is a bare, decorrelated list of candidate names
    (vote_candidates) plus a total count - never anything that ties a name
    back to a character id, and never who voted for whom. Each player's own
    locked-in choice comes separately via 'my_vote_result'.
    """
    characters = {}
    for cid, st in GAME["characters"].items():
        c = dict(st)
        if not reveal_names:
            c["player_name"] = ""
        c["display_name"] = display_name_for(cid)
        characters[cid] = c
    current_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    state = {
        "round": GAME["round"],
        "num_rounds": NUM_ROUNDS,
        "phase_index": GAME["phase_index"],
        "phases": PHASES,
        "characters": characters,
        "tally": vote_tally(),
        "vote_count": len(GAME["votes"]),
        "vote_candidates": (
            vote_candidates() if current_phase == "Vote"
            else eliminate_candidates() if current_phase == "Eliminate"
            else []
        ),
        "spotlight_characters": spotlight_characters(),
        "super_active_characters": super_active_characters(),
        "draft_characters": draft_characters(),
        "hostage_event": GAME["hostage_event"],
        "game_over": GAME["game_over"],
        "timer": GAME["timer"],
        "activity": GAME["activity"],
        "map": GAME["map"],
        "players": GAME["players"],
        "roster_locked": GAME["roster_locked"],
        "unlocked_packs": sorted(GAME["unlocked_packs"]),
        "phase_script": render_phase_script(),
    }
    if reveal_names:
        state["votes"] = GAME["votes"]
        state["pending_inspection"] = GAME["pending_inspection"]
        state["active_inspector_cid"] = GAME["active_inspector_cid"]
        state["eligible_inspectors"] = eligible_inspectors()
    return state


# sid -> the name that player typed in on /play, used to compute their
# private "you are playing X" reveal. Never sent to anyone but themselves.
PLAYER_SIDS = {}


def whoami_for(name):
    if not name:
        return []
    target = name.strip().lower()
    if not target:
        return []
    matches = []
    for cid, st in GAME["characters"].items():
        pname = (st.get("player_name") or "").strip().lower()
        if pname and pname == target:
            matches.append(display_name_for(cid))
    return matches


def find_player_character_id(name):
    """Return the character id assigned to this player name, or None."""
    if not name:
        return None
    target = name.strip().lower()
    for cid, st in GAME["characters"].items():
        pname = (st.get("player_name") or "").strip().lower()
        if pname and pname == target:
            return cid
    return None


def add_to_roster(name):
    """Add a newly-registered player to the visible roster, in join order.
    No-ops if the roster is locked or the name is already present."""
    if not name or GAME["roster_locked"]:
        return
    norm = name.strip().lower()
    if any(p["name"].strip().lower() == norm for p in GAME["players"]):
        return
    GAME["players"].append({"name": name.strip(), "eliminated": False})


def push_whoami():
    for sid, name in PLAYER_SIDS.items():
        socketio.emit("whoami_result", {"characters": whoami_for(name)}, room=sid)


def push_my_votes():
    """Privately tell each player their own locked-in vote (or none yet),
    plus whether they're eligible to vote at all in the current phase -
    never broadcast who voted for whom to anyone but the host."""
    phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    for sid, name in PLAYER_SIDS.items():
        choice = GAME["votes"].get(name)
        can_vote = False
        if phase == "Vote":
            can_vote = True
        elif phase == "Eliminate":
            cid = find_player_character_id(name)
            can_vote = bool(cid) and CHARACTERS_BY_ID.get(cid, {}).get("team") == "martian"
        socketio.emit("my_vote_result", {
            "voted": choice is not None, "choice": choice, "can_vote": can_vote,
        }, room=sid)


def push_condition_alert(cid, title, body):
    """Privately alert whoever's playing this character about a condition
    that just started applying to them."""
    pname = (GAME["characters"].get(cid, {}).get("player_name") or "").strip().lower()
    if not pname:
        return
    for sid, name in PLAYER_SIDS.items():
        if name.strip().lower() == pname:
            socketio.emit("condition_alert", {"title": title, "body": body}, room=sid)


def push_condition_recap():
    """At the start of a new round, remind every player of whichever
    conditions are still in effect for their character."""
    for sid, name in PLAYER_SIDS.items():
        cid = find_player_character_id(name)
        if not cid:
            continue
        st = GAME["characters"].get(cid, {})
        active = [c for c in CONDITIONS.values() if st.get(c["flag"])]
        if active:
            socketio.emit("condition_recap", {
                "conditions": [{"title": c["title"], "body": c["body"]} for c in active]
            }, room=sid)


def _end_game(winner, title, message):
    GAME["game_over"] = {"winner": winner, "title": title, "message": message}
    log_activity(f"GAME OVER \u2014 {title}: {message}")
    socketio.emit("game_over", GAME["game_over"], room="hosts")
    socketio.emit("game_over", GAME["game_over"], room="players")


def check_win_condition():
    """Check every FAIL/SUCCEED condition after each relevant action.
    Only fires once per game (first condition met wins)."""
    if GAME["game_over"]:
        return
    chars = GAME["characters"]

    def active_team(team):
        return [
            cid for cid, st in chars.items()
            if st["active"] and CHARACTERS_BY_ID.get(cid, {}).get("team") == team
        ]

    martians = active_team("martian")
    heroes = active_team("hero")
    civilians = active_team("civilian")

    all_civilians_rescued = bool(civilians) and all(chars[c]["rescued"] for c in civilians)
    all_heroes_rescued = bool(heroes) and all(chars[c]["rescued"] for c in heroes)

    # --- White Martians win ---
    if martians and all(chars[c]["rescued"] for c in martians):
        _end_game("Martians", "WHITE MARTIANS WIN!",
                  "All White Martians were teleported to Watchtower.")
        return
    if chars.get("martian_manhunter", {}).get("exposed"):
        _end_game("Martians", "WHITE MARTIANS WIN!", "Martian Manhunter was Exposed!")
        return
    if heroes and all(chars[c]["eliminated"] for c in heroes) and not all_civilians_rescued:
        _end_game("Martians", "WHITE MARTIANS WIN!",
                  "All Heroes were eliminated before all Civilians were rescued.")
        return
    if civilians and all(chars[c]["eliminated"] for c in civilians):
        _end_game("Martians", "WHITE MARTIANS WIN!", "All Civilians were eliminated.")
        return
    if all_heroes_rescued and not all_civilians_rescued:
        _end_game("Martians", "WHITE MARTIANS WIN!",
                  "All Heroes reached Watchtower before all Civilians were rescued.")
        return

    # --- Heroes win ---
    if martians and all(chars[c]["exposed"] for c in martians):
        _end_game("Heroes", "HEROES WIN!", "All White Martians were Exposed!")
        return
    if all_civilians_rescued:
        _end_game("Heroes", "HEROES WIN!", "All Civilians have been safely rescued!")
        return


# ------------------------------------------------------------------------
# "How to Play" tutorial guidance, sent to players at the start of each
# phase - only through Round 3, so new players get walked through the
# flow early on without it cluttering later rounds. Whole-table phases
# (Report/Discuss/Vote/Rescue) get the same message for everyone. Role
# phases (Accuse/Eliminate/Protect/Inspect) only get a guide here for
# players whose character has NO ability tagged for that phase ("keep
# your eyes closed and wait") - anyone who DOES have one already gets the
# specific ability text via push_phase_reminders, so they're not told the
# same thing twice.
# ------------------------------------------------------------------------
PHASE_GUIDE_EVERYONE = {
    "Report": "Watchtower is about to recap what happened last round. Just listen!",
    "Discuss": "Talk it out! Openly discuss who should be sent to Watchtower "
               "(about 2 minutes). This phase ends once a player is "
               "nominated and a different, non-targeted player seconds that "
               "nomination. You may not nominate yourself.",
    "Vote": "Everyone votes thumbs up or thumbs down on the targeted player "
            "- the targeted player votes too. Majority decides. If they win "
            "the vote, they're Targeted-for-Teleportation and immune to "
            "accusations. If they lose, we go back to Discuss.",
    "Rescue": "Watchtower is about to reveal who's been safely rescued.",
}
PHASE_GUIDE_BYSTANDER = {
    "Accuse": "This phase is for Accuser-type characters. Sit tight.",
    "Eliminate": "Keep your eyes closed and wait for Watchtower to act.",
    "Protect": "Keep your eyes closed unless Watchtower calls on you.",
    "Inspect": "Keep your eyes closed unless Watchtower calls on you.",
}


def push_phase_guide():
    phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    for sid, name in PLAYER_SIDS.items():
        text = None
        if phase and GAME["round"] <= 3:
            if phase in PHASE_GUIDE_EVERYONE:
                text = PHASE_GUIDE_EVERYONE[phase]
            elif phase in PHASE_GUIDE_BYSTANDER:
                cid = find_player_character_id(name)
                is_actor = bool(_visible_phase_abilities(cid, phase))
                if not is_actor:
                    text = PHASE_GUIDE_BYSTANDER[phase]
        socketio.emit("phase_guide", {"phase": phase, "text": text}, room=sid)


def _visible_phase_abilities(cid, phase_name):
    """Ability text tagged for this phase, excluding Super Abilities before
    Round 3 (they're not usable yet, so no point reminding players about
    them early)."""
    if not cid or not phase_name:
        return []
    abilities = ABILITY_PHASE_MAP.get(cid, {}).get(phase_name, [])
    if GAME["round"] < 3:
        abilities = [a for a in abilities if "SUPER ABILITY" not in a.upper()]
    return abilities


def push_phase_reminders():
    """Privately nudge each player whose character has an ability tagged
    for the phase that's currently active."""
    phase_name = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    for sid, name in PLAYER_SIDS.items():
        cid = find_player_character_id(name)
        abilities = _visible_phase_abilities(cid, phase_name)
        socketio.emit("phase_reminder", {
            "phase": phase_name,
            "character": CHARACTERS_BY_ID[cid]["name"] if cid else None,
            "abilities": abilities,
        }, room=sid)


def broadcast():
    socketio.emit("state", public_state(reveal_names=True), room="hosts")
    socketio.emit("state", public_state(reveal_names=False), room="players")
    push_whoami()
    push_my_votes()


# ---------------------------------------------------------------- routes ---

@app.route("/host")
def host_page():
    return render_template(
        "host.html",
        characters=CHARACTERS,
        team_labels=TEAM_LABELS,
        team_colors=TEAM_COLORS,
        phases=PHASES,
        num_rounds=NUM_ROUNDS,
        dceu_grid=DCEU_GRID,
        column_colors=COLUMN_COLORS,
        cards=CARDS,
        prompts=NARRATION_PROMPTS,
        intro_script=INTRO_SCRIPT,
        packs=PACKS,
    )


@app.route("/play")
def player_page():
    return render_template(
        "player.html",
        characters=CHARACTERS,
        team_labels=TEAM_LABELS,
        team_colors=TEAM_COLORS,
        phases=PHASES,
        num_rounds=NUM_ROUNDS,
        phase_info=PHASE_INFO,
    )


@app.route("/")
def index():
    base = request.host_url.rstrip("/")
    return f"""
    <html><body style="font-family:sans-serif;background:#0b0f14;color:#e8edf2;padding:40px">
    <h2>White Martian &mdash; Watchtower</h2>
    <p>Host console: <a style="color:#4fc3f7" href="{base}/host">{base}/host</a> (the moderator's device)</p>
    <p>Player view: share this link with players &rarr;
       <b>{base}/play</b></p>
    </body></html>
    """


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


# ---------------------------------------------------------- socket events --

@socketio.on("connect")
def on_connect():
    pass  # wait for register_host / register_player before sending anything


@socketio.on("register_host")
def on_register_host():
    join_room("hosts")
    socketio.emit("state", public_state(reveal_names=True), room=request.sid)


@socketio.on("register_player")
def on_register_player(data):
    name = (data or {}).get("name", "").strip()
    join_room("players")
    if name:
        PLAYER_SIDS[request.sid] = name
        add_to_roster(name)
    else:
        PLAYER_SIDS.pop(request.sid, None)
    socketio.emit("state", public_state(reveal_names=False), room=request.sid)
    socketio.emit("whoami_result", {"characters": whoami_for(name)}, room=request.sid)
    choice = GAME["votes"].get(name)
    socketio.emit("my_vote_result", {"voted": choice is not None, "choice": choice}, room=request.sid)
    if name:
        broadcast()  # let the host's Players panel pick up the new arrival


@socketio.on("disconnect")
def on_disconnect():
    PLAYER_SIDS.pop(request.sid, None)


@socketio.on("set_round")
def on_set_round(data):
    old_round = GAME["round"]
    new_round = max(1, min(NUM_ROUNDS, int(data["round"])))
    if new_round != old_round:
        # Archive whatever happened in the round we're leaving, then start
        # a fresh events bucket for the round we're entering.
        GAME["round_history"][old_round] = GAME["round_events"]
        GAME["round_events"] = {"rescued": [], "eliminated": []}
    GAME["round"] = new_round
    log_activity(f"Round set to {GAME['round']}")

    if new_round != old_round:
        push_condition_recap()

    if new_round >= 3 and not GAME["super_abilities_announced"]:
        GAME["super_abilities_announced"] = True
        log_activity("Super Abilities are now active!")
        for cid in super_active_characters():
            ability = real_super_ability(cid)
            pname = (GAME["characters"][cid].get("player_name") or "").strip().lower()
            if not pname:
                continue
            for sid, name in PLAYER_SIDS.items():
                if name.strip().lower() == pname:
                    socketio.emit("super_ability_unlocked", {
                        "character": display_name_for(cid),
                        "ability": ability,
                    }, room=sid)

    broadcast()


@socketio.on("set_phase")
def on_set_phase(data):
    old_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    idx = data.get("phase_index")

    if idx is not None and PHASES[idx] == "Accuse":
        has_targeted = any(st["active"] and st["targeted"] for st in GAME["characters"].values())
        if not has_targeted:
            socketio.emit("character_limit_error", {
                "message": "Select Teleport (\u201cTargeted for Teleportation\u201d) for at "
                           "least one player before moving to Accuse!."
            }, room=request.sid)
            return

    if old_phase == "Vote":
        tally = vote_tally()
        GAME["last_vote_winner"] = tally[0][0] if tally else None
    if old_phase == "Inspect" and (idx is None or PHASES[idx] != "Inspect"):
        GAME["pending_inspection"] = None
        GAME["active_inspector_cid"] = None
    GAME["phase_index"] = idx
    if idx is not None:
        log_activity(f"Phase: {PHASES[idx]}!")
        if PHASES[idx] not in ("Vote", "Accuse"):
            GAME["votes"] = {}
        if PHASES[idx] == "Protect":
            for st in GAME["characters"].values():
                st["protection"] = [False, False, False]
    broadcast()
    push_phase_reminders()
    push_phase_guide()
    if idx is not None and PHASES[idx] == "Secret Identity":
        push_secret_identity_reveals()


@socketio.on("clear_all_characters")
def on_clear_all_characters():
    count = 0
    for cid, st in GAME["characters"].items():
        if st["active"]:
            st["active"] = False
            st["last_action"] = None
            count += 1
    log_activity(f"Cleared all {count} active characters")
    broadcast()


@socketio.on("toggle_character")
def on_toggle_character(data):
    cid = data["id"]
    if cid not in GAME["characters"]:
        return
    if not is_unlocked(cid):
        return  # character's pack isn't unlocked - ignore the toggle
    st = GAME["characters"][cid]
    activating = not st["active"]
    if activating and GAME["roster_locked"]:
        active_count = sum(1 for s in GAME["characters"].values() if s["active"])
        player_count = len(GAME["players"])
        if active_count >= player_count:
            socketio.emit("character_limit_error", {
                "message": f"Roster is locked with {player_count} player"
                           f"{'s' if player_count != 1 else ''} - you already have "
                           f"{active_count} characters active. Deactivate one first, "
                           f"or unlock the roster to add more players."
            }, room=request.sid)
            return
    st["active"] = activating
    if not st["active"]:
        st["last_action"] = None
    log_activity(f"{CHARACTERS_BY_ID[cid]['name']} {'activated' if st['active'] else 'deactivated'}")
    broadcast()


@socketio.on("toggle_pack")
def on_toggle_pack(data):
    pack_id = data.get("pack_id")
    if pack_id not in PACK_LABELS or pack_id in FREE_PACK_IDS:
        return  # unknown pack, or trying to toggle the always-on Basic pack
    if pack_id in GAME["unlocked_packs"]:
        GAME["unlocked_packs"].discard(pack_id)
        # Locking a pack deactivates and clears any of its characters that
        # were active, so the roster never shows a character from a pack
        # that's no longer unlocked.
        for cid, st in GAME["characters"].items():
            if CHARACTERS_BY_ID[cid].get("pack") == pack_id and st["active"]:
                st["active"] = False
                st["player_name"] = ""
                st["last_action"] = None
        log_activity(f"Pack locked: {PACK_LABELS[pack_id]}")
    else:
        GAME["unlocked_packs"].add(pack_id)
        log_activity(f"Pack unlocked: {PACK_LABELS[pack_id]}")
    broadcast()


@socketio.on("set_player_name")
def on_set_player_name(data):
    cid = data["id"]
    if cid in GAME["characters"]:
        GAME["characters"][cid]["player_name"] = data.get("name", "")
        broadcast()


@socketio.on("adjust_health")
def on_adjust_health(data):
    cid = data["id"]
    delta = int(data.get("delta", 0))
    st = GAME["characters"].get(cid)
    if not st or st["health"] is None:
        return
    st["health"] = max(0, min(MAX_HEALTH, st["health"] + delta))
    log_activity(f"{CHARACTERS_BY_ID[cid]['name']} health: {st['health']}")
    broadcast()


@socketio.on("adjust_shield")
def on_adjust_shield(data):
    cid = data["id"]
    delta = int(data.get("delta", 0))
    st = GAME["characters"].get(cid)
    if not st or st["shield"] is None:
        return
    st["shield"] = max(0, min(MAX_HEALTH, st["shield"] + delta))
    log_activity(f"{CHARACTERS_BY_ID[cid]['name']} shield: {st['shield']}")
    broadcast()


@socketio.on("recharge_shields")
def on_recharge_shields():
    for cid, st in GAME["characters"].items():
        if st["shield"] is not None:
            st["shield"] = min(MAX_HEALTH, st["shield"] + 1)
    log_activity("All shields recharged +1")
    broadcast()


@socketio.on("toggle_special")
def on_toggle_special(data):
    """Generic toggle for cuffs / cure / fixit style on-off abilities."""
    cid, field = data["id"], data["field"]
    st = GAME["characters"].get(cid)
    if not st or field not in ("cuffed", "cured", "fixed") or st.get(field) is None:
        return
    st[field] = not st[field]
    label = {"cuffed": "Cuffed", "cured": "Cured", "fixed": "Fixed"}[field]
    log_activity(f"{CHARACTERS_BY_ID[cid]['name']}: {label} {'ON' if st[field] else 'off'}")
    broadcast()


@socketio.on("reveal_character")
def on_reveal_character(data):
    """Flip a switch character (Mary Batson -> Mary Marvel, etc.) between
    their civilian disguise and their secret identity. Unlocks their
    shield the first time they're revealed, if their card has one."""
    cid = data.get("id")
    char = CHARACTERS_BY_ID.get(cid)
    st = GAME["characters"].get(cid)
    if not char or not st or not char.get("is_switchable"):
        return
    st["revealed"] = not st["revealed"]
    if st["revealed"] and char["has_shield"] and st["shield"] is None:
        st["shield"] = SHIELD_START
    civilian_name, secret_name = char["name"], char["reveal_name"]
    if st["revealed"]:
        log_activity(f"{civilian_name} revealed as {secret_name}!")
    else:
        log_activity(f"{secret_name} concealed again as {civilian_name}")
    broadcast()
    # Privately tell whoever is playing this character - same pop-up
    # treatment as the initial shuffle reveal.
    pname = (st.get("player_name") or "").strip().lower()
    if pname:
        for sid, name in PLAYER_SIDS.items():
            if name.strip().lower() == pname:
                socketio.emit("shuffle_reveal", {
                    "character": display_name_for(cid), "id": cid,
                }, room=sid)


@socketio.on("take_hostage")
def on_take_hostage(data):
    """A villain takes a player hostage. If their card names a counterpart
    hero (or a category like 'kryptonian'), that hero has 10 real-world
    seconds to reveal their identity or the hostage loses 1 health -
    tracked as GAME['hostage_event'] so the host console can show a
    persistent resolve banner. Two-Face has no counterpart - his ability
    is a free choice of two targets with no reveal-to-save consequence."""
    holder_id = data.get("holder_id")
    target_ids = data.get("target_ids") or []
    holder = CHARACTERS_BY_ID.get(holder_id)
    holder_st = GAME["characters"].get(holder_id)
    if not holder or not holder_st or not holder.get("has_hostage"):
        return
    if holder.get("is_switchable") and not holder_st.get("revealed"):
        socketio.emit("character_limit_error", {
            "message": f"{holder['name']} must be revealed before taking hostages."
        }, room=request.sid)
        return

    counterpart = holder.get("hostage_counterpart")
    target_ids = [t for t in dict.fromkeys(target_ids) if t != holder_id]

    if counterpart is None:
        # Two-Face: free choice of exactly two targets, resolved by coin
        # flip / host judgment - no automatic reveal-to-save consequence.
        if len(target_ids) != 2 or any(
            t not in GAME["characters"] or not GAME["characters"][t]["active"] for t in target_ids
        ):
            socketio.emit("character_limit_error", {
                "message": "Pick exactly two active characters to take hostage."
            }, room=request.sid)
            return
        names = []
        for t in target_ids:
            GAME["characters"][t]["hostage"] = True
            names.append(display_name_for(t))
        log_activity(f"{display_name_for(holder_id)} takes {names[0]} and {names[1]} hostage!")
        broadcast()
        return

    # Named or category counterpart: exactly one hostage target, then a
    # 10-second real-world window for the counterpart hero to reveal.
    if (len(target_ids) != 1 or target_ids[0] not in GAME["characters"]
            or not GAME["characters"][target_ids[0]]["active"]):
        socketio.emit("character_limit_error", {
            "message": "Pick exactly one active character to take hostage."
        }, room=request.sid)
        return
    hostage_id = target_ids[0]

    if counterpart == "kryptonian":
        counterpart_ids = [cid for cid in KRYPTONIAN_IDS if GAME["characters"].get(cid, {}).get("active")]
        counterpart_label = _join_names([display_name_for(c) for c in counterpart_ids]) or "a Kryptonian"
    else:
        counterpart_ids = [counterpart]
        counterpart_label = display_name_for(counterpart)

    GAME["characters"][hostage_id]["hostage"] = True
    GAME["hostage_event"] = {
        "holder_id": holder_id,
        "hostage_id": hostage_id,
        "counterpart_ids": counterpart_ids,
        "counterpart_label": counterpart_label,
    }
    log_activity(
        f"{display_name_for(holder_id)} takes {display_name_for(hostage_id)} hostage! "
        f"{counterpart_label} has 10 seconds to reveal."
    )
    broadcast()


@socketio.on("release_hostage")
def on_release_hostage(data):
    """The counterpart hero revealed in time - hostage is safe."""
    cid = data.get("id")
    st = GAME["characters"].get(cid)
    if not st or not st.get("hostage"):
        return
    st["hostage"] = False
    if GAME["hostage_event"] and GAME["hostage_event"]["hostage_id"] == cid:
        GAME["hostage_event"] = None
    log_activity(f"{display_name_for(cid)} released from hostage - saved in time!")
    broadcast()


@socketio.on("hostage_consequence")
def on_hostage_consequence(data):
    """Nobody revealed in time - the hostage loses 1 health."""
    cid = data.get("id")
    st = GAME["characters"].get(cid)
    if not st or not st.get("hostage"):
        return
    st["hostage"] = False
    if st["health"] is not None:
        st["health"] = max(0, st["health"] - 1)
    if GAME["hostage_event"] and GAME["hostage_event"]["hostage_id"] == cid:
        GAME["hostage_event"] = None
    log_activity(f"No one revealed in time - {display_name_for(cid)} loses 1 health!")
    broadcast()


@socketio.on("sync_timer")
def on_sync_timer(data):
    """Host's timer display pushes its current state here on every tick/
    change; relay to players as a view-only display - they get no
    controls, just the live countdown."""
    label = data.get("label")
    GAME["timer"] = None if not label else {
        "label": label,
        "remaining": data.get("remaining", 0),
        "running": bool(data.get("running")),
    }
    socketio.emit("timer_update", GAME["timer"], room="players")


@socketio.on("toggle_location")
def on_toggle_location(data):
    name = data.get("name")
    if name not in GAME["map"]:
        return
    GAME["map"][name] = not GAME["map"][name]
    log_activity(f"Map: {name} {'blacked out' if GAME['map'][name] else 'restored'}")
    broadcast()


@socketio.on("toggle_protection")
def on_toggle_protection(data):
    cid, slot = data["id"], int(data["slot"])
    if cid in GAME["characters"] and 0 <= slot < 3:
        prot = GAME["characters"][cid]["protection"]
        prot[slot] = not prot[slot]
        broadcast()


@socketio.on("character_action")
def on_character_action(data):
    cid, action = data["id"], data["action"]
    if cid not in GAME["characters"]:
        return
    st = GAME["characters"][cid]
    name = CHARACTERS_BY_ID[cid]["name"]
    if action == "deactivate":
        st["active"] = False
        st["last_action"] = None
        for cond in CONDITIONS.values():
            st[cond["flag"]] = False
        log_activity(f"{name} deactivated")
    else:
        st["last_action"] = action
        log_activity(f"{name}: {action.capitalize()}")
        # Track outcomes for next round's Report recap. "watchtower" =
        # reached Watchtower safely; "end" = eliminated/game over.
        if action == "watchtower" and cid not in GAME["round_events"]["rescued"]:
            GAME["round_events"]["rescued"].append(cid)
        if action == "end" and cid not in GAME["round_events"]["eliminated"]:
            GAME["round_events"]["eliminated"].append(cid)
        # Condition toggle + private player alert, only on turning ON.
        if action in CONDITIONS:
            cond = CONDITIONS[action]
            st[cond["flag"]] = not st[cond["flag"]]
            if st[cond["flag"]]:
                push_condition_alert(cid, cond["title"], cond["body"])
    broadcast()
    check_win_condition()


@socketio.on("start_game")
def on_start_game():
    GAME["roster_locked"] = True
    log_activity(f"Roster locked with {len(GAME['players'])} players")
    active_count = sum(1 for s in GAME["characters"].values() if s["active"])
    player_count = len(GAME["players"])
    if active_count > player_count:
        socketio.emit("character_limit_error", {
            "message": f"Roster locked with {player_count} player"
                       f"{'s' if player_count != 1 else ''}, but {active_count} "
                       f"characters are active - deactivate {active_count - player_count} "
                       f"to match, or Shuffle will fail."
        }, room=request.sid)
    broadcast()


@socketio.on("toggle_player_eliminated")
def on_toggle_player_eliminated(data):
    name = (data or {}).get("name", "")
    for p in GAME["players"]:
        if p["name"] == name:
            p["eliminated"] = not p["eliminated"]
            log_activity(f"{name} marked {'eliminated' if p['eliminated'] else 'alive'}")
            break
    broadcast()


@socketio.on("shuffle_characters")
def on_shuffle_characters():
    players = [p["name"] for p in GAME["players"]]
    active_ids = [cid for cid, st in GAME["characters"].items() if st["active"]]

    if not players:
        socketio.emit("shuffle_error", {"message": "No players in the roster yet."}, room=request.sid)
        return
    if len(players) > len(active_ids):
        socketio.emit("shuffle_error", {
            "message": f"Not enough active characters ({len(active_ids)}) for {len(players)} players. "
                       f"Toggle more characters on in the roster first."
        }, room=request.sid)
        return

    # Clear any previous assignments on active characters, then deal fresh.
    for cid in active_ids:
        GAME["characters"][cid]["player_name"] = ""

    chosen = random.sample(active_ids, len(players))
    random.shuffle(players)
    assignment = dict(zip(players, chosen))
    for name, cid in assignment.items():
        GAME["characters"][cid]["player_name"] = name

    log_activity(f"Shuffled characters to {len(players)} players")
    broadcast()

    for name, cid in assignment.items():
        char_name = display_name_for(cid)
        for sid, pname in PLAYER_SIDS.items():
            if pname.strip().lower() == name.strip().lower():
                socketio.emit("shuffle_reveal", {"character": char_name, "id": cid}, room=sid)
    push_phase_reminders()


@socketio.on("get_my_card")
def on_get_my_card(data):
    name = (data or {}).get("name") or PLAYER_SIDS.get(request.sid, "")
    cid = find_player_character_id(name)
    if not cid:
        socketio.emit("my_card_result", {"assigned": False}, room=request.sid)
        return
    card = dict(CARDS.get(cid, {}))
    char = CHARACTERS_BY_ID.get(cid, {})
    if char.get("is_switchable"):
        revealed = GAME["characters"].get(cid, {}).get("revealed", False)
        card["abilities"] = [
            a for a in card.get("abilities", [])
            if _ability_visible_to_player(a, revealed)
        ]
    socketio.emit("my_card_result", {
        "assigned": True,
        "character": display_name_for(cid),
        "card": card,
    }, room=request.sid)


@socketio.on("cast_vote")
def on_cast_vote(data):
    voter = (data.get("voter") or "").strip()
    target_name = (data.get("target_name") or "").strip()
    if not voter or not target_name:
        return
    if voter in GAME["votes"]:
        return  # one vote only - first submission is final, no changing it
    phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if phase == "Eliminate":
        voter_cid = find_player_character_id(voter)
        if not voter_cid or CHARACTERS_BY_ID.get(voter_cid, {}).get("team") != "martian":
            return  # only White Martians vote during Eliminate!
        candidates = eliminate_candidates()
    elif phase == "Vote":
        candidates = vote_candidates()
    else:
        return  # voting isn't open outside Vote!/Eliminate!
    if target_name not in candidates:
        return  # not a valid candidate right now - ignore
    GAME["votes"][voter] = target_name
    log_activity(f"{voter} voted")
    broadcast()


@socketio.on("reset_votes")
def on_reset_votes():
    GAME["votes"] = {}
    broadcast()


def _sid_for_player(name):
    target = name.strip().lower()
    for sid, pname in PLAYER_SIDS.items():
        if pname.strip().lower() == target:
            return sid
    return None


@socketio.on("send_inspect_prompt")
def on_send_inspect_prompt(data):
    """Host invites a specific eligible character to ask Watchtower a
    question this phase - only that character's player can submit a
    target until it's answered or the host moves to someone else."""
    cid = data.get("id")
    st = GAME["characters"].get(cid)
    if not st or not st["active"] or not has_martian_inspect_ability(cid):
        return
    phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if phase != "Inspect":
        return
    if not _visible_phase_abilities(cid, "Inspect"):
        return  # e.g. Superman's X-Ray Vision is a locked Super Ability before Round 3
    GAME["active_inspector_cid"] = cid
    GAME["pending_inspection"] = None
    pname = (st.get("player_name") or "").strip()
    sid = _sid_for_player(pname) if pname else None
    if sid:
        socketio.emit("inspect_prompt", {
            "candidates": active_player_names(exclude_name=pname)
        }, room=sid)
    log_activity(f"{display_name_for(cid)} was invited to ask Watchtower a question")
    broadcast()


@socketio.on("ask_watchtower")
def on_ask_watchtower(data):
    """A player with a 'is this player a Martian?' Inspect ability
    silently asks Watchtower about another active player. The host sees
    the request privately and answers Yes/No, which goes back to only
    the asking player - no one else ever sees this exchange."""
    asker_name = (data.get("asker") or "").strip()
    target_name = (data.get("target_name") or "").strip()
    if not asker_name or not target_name:
        return
    phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if phase != "Inspect":
        return
    asker_cid = find_player_character_id(asker_name)
    if not asker_cid or not has_martian_inspect_ability(asker_cid):
        return
    if asker_cid != GAME["active_inspector_cid"]:
        return  # host hasn't invited this character to ask yet
    if GAME["pending_inspection"]:
        sid = _sid_for_player(asker_name)
        if sid:
            socketio.emit("ask_watchtower_error", {
                "message": "Watchtower is answering someone else right now - try again in a moment."
            }, room=sid)
        return
    if target_name not in active_player_names(exclude_name=asker_name):
        return
    GAME["pending_inspection"] = {"asker_name": asker_name, "target_name": target_name}
    log_activity("An Inspect ability asked Watchtower a question")
    broadcast()


@socketio.on("answer_watchtower")
def on_answer_watchtower(data):
    pending = GAME["pending_inspection"]
    if not pending:
        return
    answer = bool(data.get("answer"))
    sid = _sid_for_player(pending["asker_name"])
    if sid:
        socketio.emit("inspection_answer", {
            "target_name": pending["target_name"], "answer": answer,
        }, room=sid)
    log_activity(f"Watchtower answered {'Yes' if answer else 'No'}")
    GAME["pending_inspection"] = None
    GAME["active_inspector_cid"] = None
    broadcast()


def _clear_player_from_characters(name):
    norm = name.strip().lower()
    for st in GAME["characters"].values():
        if (st.get("player_name") or "").strip().lower() == norm:
            st["player_name"] = ""


@socketio.on("remove_player")
def on_remove_player(data):
    name = (data or {}).get("name", "")
    before = len(GAME["players"])
    GAME["players"] = [p for p in GAME["players"] if p["name"].strip().lower() != name.strip().lower()]
    if len(GAME["players"]) != before:
        _clear_player_from_characters(name)
        log_activity(f"{name} removed from roster")
        broadcast()


@socketio.on("remove_all_players")
def on_remove_all_players():
    for p in GAME["players"]:
        _clear_player_from_characters(p["name"])
    GAME["players"] = []
    log_activity("All players removed from roster")
    broadcast()


@socketio.on("add_player")
def on_add_player(data):
    name = (data or {}).get("name", "").strip()
    if not name:
        return
    norm = name.lower()
    if any(p["name"].strip().lower() == norm for p in GAME["players"]):
        return  # already in the roster
    GAME["players"].append({"name": name, "eliminated": False})
    log_activity(f"{name} added to roster")
    broadcast()


@socketio.on("new_game")
def on_new_game():
    GAME["round"] = 1
    GAME["phase_index"] = None
    GAME["characters"] = fresh_character_state()
    GAME["votes"] = {}
    GAME["activity"] = []
    GAME["map"] = fresh_map_state()
    GAME["roster_locked"] = False
    GAME["unlocked_packs"] = set(FREE_PACK_IDS)
    GAME["last_vote_winner"] = None
    GAME["round_events"] = {"rescued": [], "eliminated": []}
    GAME["round_history"] = {}
    GAME["super_abilities_announced"] = False
    GAME["hostage_event"] = None
    GAME["game_over"] = None
    GAME["pending_inspection"] = None
    GAME["active_inspector_cid"] = None
    # Player roster is left exactly as the host set it up via the New Game
    # dialog (remove/add/remove-all) - no automatic repopulation here.
    log_activity("New game started")
    socketio.emit("game_reset", room="hosts")
    broadcast()


if __name__ == "__main__":
    ip = get_lan_ip()
    print("\n  White Martian - Watchtower is running")
    print(f"  Host console:   http://localhost:5000/host")
    print(f"  Player devices: http://{ip}:5000/play  (same WiFi)\n")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
