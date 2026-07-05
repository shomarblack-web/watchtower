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
            # Manual toggles for now, supporting the new Fury/Starro card
            # icons - the full "Granny converts targets into Furies" and
            # "Starro creates minions" mechanics are still deferred (per
            # the Hive redesign conversation), so the host sets these by
            # hand until that's built.
            "fury": False,
            "starro": False,
            # Parasite's Absorption: character id he most recently
            # absorbed abilities from (only meaningful for Parasite).
            "absorbed_from": None,
            # Dr. Alchemy's Alchemy Stone: None / "protector" / "eliminator" -
            # grants a Protect-phase shield or Eliminate-phase vote to
            # whoever it's set on, regardless of their normal team/kit.
            "alchemy_type": None,
            # Citizen's Arrest / Forget the Rules: "phases" (can't Discuss/
            # Vote/Accuse) or "all_abilities" (loses every ability), for
            # whichever round number is in arrested_for_round - cleared
            # automatically once the game moves past that round.
            "arrested_scope": None,
            "arrested_for_round": None,
            "arrested_by": None,
            # Ma/Pa Kent's Pep Talk: temporarily raises Superman's shield
            # cap above the normal MAX_HEALTH limit for one round.
            "shield_cap_override": None,
            "pep_talked_for_round": None,
            # True only for The Spectre, once he's inherited a player
            # via Back from Beyond - drives the bright green name display.
            "spectre_transformed": False,
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
    "lobo_tracker": {"civilian": 0, "hero": 0, "martian": 0},
    "active_inspector_cid": None,
    "active_protector_cid": None,
    "active_absorber_cid": None,
    "active_alchemist_cid": None,
    "pending_alchemy": None,
    "active_arrester_cid": None,
    "round_change_requests": {},
    "seats": [],
    "pending_gl_reveals": [],
    "plastic_man_shielded": [],
    "spectre_triggered": False,
    "active_good_doctor_cid": None,
    "good_doctor_requests": {},
    "active_telepathy_cid": None,
    "telepathic_links": {"martian_manhunter": [], "miss_martian": []},
    "liar_decoys": {},
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

# Abilities that let a player request Watchtower change/redo the current
# round's phase. Each is only clickable during its listed phase (None =
# no phase restriction) or, for "eliminated"-triggered ones, once that
# player's own character has the Eliminated condition. The host must
# approve every request before anything actually happens - clicking
# never changes the phase directly.
ROUND_CHANGE_ABILITIES = {
    "white_martian_i": {"label": "Mind Merge", "trigger": "phase", "phase": "Discuss", "target_phase": "Eliminate"},
    "white_martian_ii": {"label": "Mind Merge", "trigger": "phase", "phase": "Discuss", "target_phase": "Eliminate"},
    "the_flash": {"label": "Altering the Timeline", "trigger": "phase", "phase": "Rescue", "target_phase": "Vote"},
    "miss_tessmacher": {"label": "Loyal Assistant", "trigger": "phase", "phase": "Discuss", "target_phase": "Accuse"},
    "otis": {"label": "Loyal Assistant", "trigger": "phase", "phase": "Discuss", "target_phase": "Accuse"},
    "sinestro": {"label": "Construct", "trigger": "phase", "phase": "Rescue", "target_phase": "Accuse"},
    "dr_alchemy": {"label": "Blackout", "trigger": "phase", "phase": None, "target_phase": "Eliminate"},
    "pete_ross": {"label": "Turn the Earth", "trigger": "eliminated", "phase": None, "target_phase": "Eliminate"},
    "lana_lang": {"label": "Turn the Earth", "trigger": "eliminated", "phase": None, "target_phase": "Eliminate"},
}


def round_change_button_state(cid):
    """Whether this character's round-change request button should be
    enabled right now, for whoever is playing them."""
    info = ROUND_CHANGE_ABILITIES.get(cid)
    if not info:
        return None
    st = GAME["characters"].get(cid, {})
    if not st.get("active"):
        return None
    if info["trigger"] == "eliminated":
        enabled = bool(st.get("eliminated"))
    else:
        current_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
        enabled = info["phase"] is None or current_phase == info["phase"]
    pending = GAME["round_change_requests"].get(cid)
    return {
        "label": info["label"],
        "target_phase": info["target_phase"],
        "enabled": enabled and not pending,
        "pending": bool(pending),
    }


# "A Good Doctor" - Report! phase, targets an Eliminated player to bring
# back. For Dr. Caitlin Snow and Dr. Harleen Quinzel this only works
# before they've switched to their villain form (Civilian-only per their
# card); Leslie Thompkins has no such restriction.
GOOD_DOCTOR_CHARACTERS = {"dr_caitlin_snow", "leslie_thompkins", "dr_harleen_quinzel"}


def good_doctor_available(cid):
    """Whether this doctor can currently use A Good Doctor - active,
    Report! phase, and (for switch characters) not yet revealed."""
    if cid not in GOOD_DOCTOR_CHARACTERS:
        return False
    st = GAME["characters"].get(cid, {})
    if not st.get("active"):
        return False
    if CHARACTERS_BY_ID.get(cid, {}).get("is_switchable") and st.get("revealed"):
        return False
    current_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    return current_phase == "Report"


# "Petty Thief" (Plastic Man) and "Thgiels fo Dnah" (Zatanna) - both Super
# Abilities that let the player view the full Character-to-Player roster
# for 10 seconds. Zatanna's is Inspect!-tagged; Plastic Man's has no
# phase restriction. Both are still gated to Round 3+ like any other
# Super Ability.
SECRET_ROSTER_CHARACTERS = {"plastic_man", "zatanna"}


def secret_roster_available(cid):
    """Whether this character can currently trigger the 10s Secret
    Identity roster view - active, Round 3+, and (if phase-tagged) in
    the right phase."""
    if cid not in SECRET_ROSTER_CHARACTERS:
        return False
    st = GAME["characters"].get(cid, {})
    if not st.get("active") or GAME["round"] < 3:
        return False
    ability_text = real_super_ability(cid)
    if not ability_text:
        return False
    tagged_phases = ABILITY_PHASE_MAP.get(cid, {})
    is_phase_tagged = any(ability_text in phase_list for phase_list in tagged_phases.values())
    if not is_phase_tagged:
        return True  # e.g. Plastic Man - no phase tag at all on this ability
    current_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    return ability_text in tagged_phases.get(current_phase, [])

# James Gordon and Maggie Sawyer's "Citizen's Arrest" blocks Discuss/Vote/
# Accuse specifically. Robin, Batgirl, and Zatanna's abilities are broader -
# they block every ability the target has. Same underlying "Arrested!"
# condition and mechanical enforcement either way, but each ability gets
# its own flavor text so it stays specific and fun rather than generic.
ARREST_INFO = {
    "james_gordon": {
        "scope": "phases",
        "phase": "Inspect",
        "title": "Citizen's Arrest!",
        "alert": "You've been placed under Citizen's Arrest! You may NOT "
                 "Discuss, Vote, or Accuse next round.",
        "reminder": "You're still under Citizen's Arrest - you may NOT {phase} this round.",
    },
    "maggie_sawyer": {
        "scope": "phases",
        "phase": "Inspect",
        "title": "Citizen's Arrest!",
        "alert": "You've been placed under Citizen's Arrest! You may NOT "
                 "Discuss, Vote, or Accuse next round.",
        "reminder": "You're still under Citizen's Arrest - you may NOT {phase} this round.",
    },
    "robin": {
        "scope": "all_abilities",
        "phase": "Inspect",
        "title": "Bat-Cuffed!",
        "alert": "You've been bat-cuffed! You lose access to all your abilities next round!",
        "reminder": "You're still bat-cuffed - no abilities this round.",
    },
    "batgirl": {
        "scope": "all_abilities",
        "phase": "Inspect",
        "title": "Bat-Cuffed!",
        "alert": "You've been bat-cuffed! You lose access to all your abilities next round!",
        "reminder": "You're still bat-cuffed - no abilities this round.",
    },
    "zatanna": {
        "scope": "all_abilities",
        "phase": "Inspect",
        "title": "You are a rabbit!",
        "alert": "Zatanna's spell has turned you into a rabbit! You cannot "
                 "use any of your abilities next round.",
        "reminder": "You're still a rabbit - no abilities this round.",
    },
    "beast_boy": {
        "scope": "all_abilities",
        "phase": "Accuse",
        "title": "T-Rex Chomp!",
        "alert": "Beast Boy turned into a T-Rex and chomped you! You lose "
                 "access to all of your abilities next round.",
        "reminder": "You're still recovering from that T-Rex chomp - no abilities this round.",
    },
    "thunder": {
        "scope": "phases",
        "phase": "Inspect",
        "title": "Stomped!",
        "alert": "Thunder stomped on your side of the table! You may NOT "
                 "Discuss, Accuse, or Vote next round.",
        "reminder": "You're still shaken from that stomp - you may NOT {phase} this round.",
    },
}


# ------------------------------------------------------------------------
# ------------------------------------------------------------------------
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


def active_speedster_count(exclude_cid=None):
    """How many active characters are tagged Speedster - Zoom's card
    needs this number to resolve his 'gains one hostage per speedster'
    passive. Excludes exclude_cid (Zoom shouldn't count himself)."""
    return sum(
        1 for cid, st in GAME["characters"].items()
        if st["active"] and CHARACTERS_BY_ID.get(cid, {}).get("is_speedster")
        and cid != exclude_cid
    )


# Zod, Faora, and Reign all have a "For every Kryptonian in play... gains
# one target" passive. Their card text lists examples (Superman, Krypto,
# Doomsday) but never themselves, so - same pattern as Zoom's Speed
# Thief - each excludes themselves from their own count.
KRYPTONIAN_COUNT_CHARACTERS = {"zod", "faora", "reign"}


def active_kryptonian_count(exclude_cid=None):
    """How many active characters are tagged Kryptonian, excluding
    exclude_cid (a Kryptonian villain shouldn't count themselves)."""
    return sum(
        1 for cid, st in GAME["characters"].items()
        if st["active"] and CHARACTERS_BY_ID.get(cid, {}).get("is_kryptonian")
        and cid != exclude_cid
    )


def eliminate_candidates():
    """Real names of active, non-Martian players - the pool White Martians
    (and anyone Dr. Alchemy made an Eliminator) vote on to eliminate.
    Eliminators don't vote each other off, same as Martians."""
    return [
        st["player_name"] for cid, st in GAME["characters"].items()
        if st["active"] and st.get("player_name")
        and CHARACTERS_BY_ID.get(cid, {}).get("team") != "martian"
        and st.get("alchemy_type") != "eliminator"
    ]


def can_self_protect(cid):
    """True if this character's Protect ability explicitly allows shielding
    themselves (Wonder Girl, Zatanna), not just other players."""
    for a in ABILITY_PHASE_MAP.get(cid, {}).get("Protect", []):
        if "shield self" in a.lower() or "protect self" in a.lower():
            return True
    return False


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


def eligible_protectors():
    """Active, shield-unlocked characters whose ability is currently
    visible - powers the host's Protect-phase wizard. Also includes
    anyone Dr. Alchemy has granted Protector status to, regardless of
    their normal kit."""
    return [
        {"id": cid, "name": display_name_for(cid), "can_self_protect": can_self_protect(cid)}
        for cid, st in GAME["characters"].items()
        if st["active"] and st.get("shield") is not None and (
            (CHARACTERS_BY_ID.get(cid, {}).get("has_shield") and _visible_phase_abilities(cid, "Protect"))
            or st.get("alchemy_type") == "protector"
        )
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
        result = {"phase": "Protect", "kind": "interactive", "lines": []}

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
        "seats": GAME["seats"],
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
        "active_speedster_count": active_speedster_count(exclude_cid="zoom"),
        "kryptonian_counts": {
            cid: active_kryptonian_count(exclude_cid=cid) for cid in KRYPTONIAN_COUNT_CHARACTERS
        },
        "green_lantern_neighbors": (
            [n for n in seat_neighbors(
                (GAME["characters"].get("green_lantern", {}).get("player_name") or "").strip()
            ) if n] if GAME["characters"].get("green_lantern", {}).get("active") else []
        ),
        "plastic_man_shielded": GAME["plastic_man_shielded"],
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
        state["active_protector_cid"] = GAME["active_protector_cid"]
        state["eligible_protectors"] = eligible_protectors()
        state["lobo_tracker"] = GAME["lobo_tracker"]
        state["active_absorber_cid"] = GAME["active_absorber_cid"]
        state["active_alchemist_cid"] = GAME["active_alchemist_cid"]
        state["active_arrester_cid"] = GAME["active_arrester_cid"]
        state["eligible_arresters"] = eligible_arresters()
        state["round_change_requests"] = GAME["round_change_requests"]
        state["active_good_doctor_cid"] = GAME["active_good_doctor_cid"]
        state["good_doctor_requests"] = GAME["good_doctor_requests"]
        state["active_telepathy_cid"] = GAME["active_telepathy_cid"]
        state["telepathic_links"] = GAME["telepathic_links"]
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
            can_vote = bool(cid) and (
                CHARACTERS_BY_ID.get(cid, {}).get("team") == "martian"
                or GAME["characters"].get(cid, {}).get("alchemy_type") == "eliminator"
            )
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
    if chars.get("martian_manhunter", {}).get("eliminated"):
        _end_game("Martians", "WHITE MARTIANS WIN!", "Martian Manhunter was Eliminated!")
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


@socketio.on("adjust_lobo_tracker")
def on_adjust_lobo_tracker(data):
    """Lobo's personal tracker for 'The Main Man' - counts how many
    Civilians/Heroes/Martians he's exposed. Any combination totaling 3
    wins him the game outright."""
    category = data.get("category")
    delta = data.get("delta", 0)
    if category not in ("civilian", "hero", "martian"):
        return
    GAME["lobo_tracker"][category] = max(0, GAME["lobo_tracker"][category] + delta)
    log_activity(f"Lobo's tracker: {category} = {GAME['lobo_tracker'][category]}")
    total = sum(GAME["lobo_tracker"].values())
    if total >= 3 and not GAME["game_over"]:
        _end_game("Lobo", "LOBO WINS!",
                  "Lobo exposed 3 combined Civilians, Heroes, and Martians!")
    broadcast()


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


def current_arrest_scope(cid):
    """The Arrested! scope in effect for this character right now, if
    any - None once the round it applied to has passed."""
    st = GAME["characters"].get(cid, {})
    if st.get("arrested_scope") and st.get("arrested_for_round") == GAME["round"]:
        return st["arrested_scope"]
    return None


def push_phase_reminders():
    """Privately nudge each player whose character has an ability tagged
    for the phase that's currently active. Suppressed entirely for anyone
    currently under the "all_abilities" Arrested! restriction."""
    phase_name = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    for sid, name in PLAYER_SIDS.items():
        cid = find_player_character_id(name)
        abilities = _visible_phase_abilities(cid, phase_name)
        if cid and current_arrest_scope(cid) == "all_abilities":
            abilities = []
        socketio.emit("phase_reminder", {
            "phase": phase_name,
            "character": CHARACTERS_BY_ID[cid]["name"] if cid else None,
            "abilities": abilities,
        }, room=sid)


PHASES_BLOCKED_BY_ARREST = {"Discuss", "Vote", "Accuse"}


def push_arrest_reminders():
    """At the start of every phase, remind anyone currently Arrested! of
    the specific restriction that applies right now, using the same
    flavor text as whichever ability caught them."""
    phase_name = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if not phase_name:
        return
    for sid, name in PLAYER_SIDS.items():
        cid = find_player_character_id(name)
        if not cid:
            continue
        scope = current_arrest_scope(cid)
        if not scope:
            continue
        arrester_cid = GAME["characters"].get(cid, {}).get("arrested_by")
        info = ARREST_INFO.get(arrester_cid, {})
        title = info.get("title", "Arrested!")
        if scope == "phases" and phase_name in PHASES_BLOCKED_BY_ARREST:
            body = info.get("reminder", "You may NOT {phase} this round.").format(phase=phase_name)
            socketio.emit("condition_alert", {"title": title, "body": body}, room=sid)
        elif scope == "all_abilities" and _visible_phase_abilities(cid, phase_name):
            body = info.get("reminder", "You have no abilities this round.")
            socketio.emit("condition_alert", {"title": title, "body": body}, room=sid)


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
        for st in GAME["characters"].values():
            if st.get("arrested_for_round") is not None and new_round > st["arrested_for_round"]:
                st["arrested_scope"] = None
                st["arrested_for_round"] = None
                st["arrested_by"] = None
        superman_st = GAME["characters"].get("superman")
        if (superman_st and superman_st.get("pep_talked_for_round") is not None
                and new_round > superman_st["pep_talked_for_round"]):
            superman_st["shield_cap_override"] = None
            superman_st["pep_talked_for_round"] = None
            if superman_st.get("shield") is not None:
                superman_st["shield"] = min(MAX_HEALTH, superman_st["shield"])

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


def _set_phase_by_index(idx, error_sid=None):
    """Core phase-transition logic, shared between the host's direct
    phase-strip clicks and approved round-change requests."""
    old_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None

    if idx is not None and PHASES[idx] == "Accuse":
        has_targeted = any(st["active"] and st["targeted"] for st in GAME["characters"].values())
        if not has_targeted:
            if error_sid:
                socketio.emit("character_limit_error", {
                    "message": "Select Teleport (\u201cTargeted for Teleportation\u201d) for at "
                               "least one player before moving to Accuse!."
                }, room=error_sid)
            return False

    if old_phase == "Vote":
        tally = vote_tally()
        GAME["last_vote_winner"] = tally[0][0] if tally else None
    if old_phase == "Inspect" and (idx is None or PHASES[idx] != "Inspect"):
        GAME["pending_inspection"] = None
        GAME["active_inspector_cid"] = None
        GAME["active_alchemist_cid"] = None
        GAME["pending_alchemy"] = None
        if GAME["active_arrester_cid"] and ARREST_INFO.get(GAME["active_arrester_cid"], {}).get("phase") == "Inspect":
            GAME["active_arrester_cid"] = None
        GAME["active_telepathy_cid"] = None
    if old_phase == "Protect" and (idx is None or PHASES[idx] != "Protect"):
        GAME["active_protector_cid"] = None
    if old_phase == "Accuse" and (idx is None or PHASES[idx] != "Accuse"):
        GAME["active_absorber_cid"] = None
        if GAME["active_arrester_cid"] and ARREST_INFO.get(GAME["active_arrester_cid"], {}).get("phase") == "Accuse":
            GAME["active_arrester_cid"] = None
    if old_phase == "Eliminate" and (idx is None or PHASES[idx] != "Eliminate"):
        gl_st = GAME["characters"].get("green_lantern")
        if gl_st and gl_st["active"]:
            gl_pname = (gl_st.get("player_name") or "").strip()
            left, right = seat_neighbors(gl_pname) if gl_pname else (None, None)
            for neighbor in (left, right):
                if neighbor and neighbor not in GAME["pending_gl_reveals"]:
                    GAME["pending_gl_reveals"].append(neighbor)
    GAME["phase_index"] = idx
    if idx is not None:
        log_activity(f"Phase: {PHASES[idx]}!")
        if PHASES[idx] not in ("Vote", "Accuse"):
            GAME["votes"] = {}
        if PHASES[idx] == "Protect":
            for st in GAME["characters"].values():
                st["protection"] = [False, False, False]
        if PHASES[idx] == "Report" and GAME["pending_gl_reveals"]:
            for pname in GAME["pending_gl_reveals"]:
                sid = _sid_for_player(pname)
                if sid:
                    socketio.emit("condition_alert", {
                        "title": "Shielded by the Light!",
                        "body": "Green Lantern's Light automatically shielded you from "
                                "elimination last round - you were seated right beside them.",
                    }, room=sid)
            log_activity(f"Green Lantern's Light reveal: {', '.join(GAME['pending_gl_reveals'])}")
            GAME["pending_gl_reveals"] = []
    broadcast()
    push_phase_reminders()
    push_phase_guide()
    push_arrest_reminders()
    if idx is not None and PHASES[idx] == "Secret Identity":
        push_secret_identity_reveals()
    return True


@socketio.on("set_phase")
def on_set_phase(data):
    _set_phase_by_index(data.get("phase_index"), error_sid=request.sid)


@socketio.on("request_round_change")
def on_request_round_change(data):
    """A player clicks their round-change ability button on My Card.
    Only queues a request for the host to approve - never changes the
    phase directly."""
    cid = data.get("id")
    requester_name = (data.get("player") or "").strip()
    info = ROUND_CHANGE_ABILITIES.get(cid)
    if not info or not requester_name:
        return
    actual_cid = find_player_character_id(requester_name)
    if actual_cid != cid:
        return  # someone other than the assigned player tried to trigger this
    state = round_change_button_state(cid)
    if not state or not state["enabled"]:
        return  # not currently eligible - ignore
    GAME["round_change_requests"][cid] = {
        "label": info["label"],
        "target_phase": info["target_phase"],
        "player_name": requester_name,
    }
    log_activity(f"{display_name_for(cid)} requested Watchtower approval: "
                 f"{info['label']} \u2192 {info['target_phase']}!")
    broadcast()


@socketio.on("resolve_round_change")
def on_resolve_round_change(data):
    """Host approves or dismisses a pending round-change request."""
    cid = data.get("id")
    approve = bool(data.get("approve"))
    pending = GAME["round_change_requests"].pop(cid, None)
    if not pending:
        return
    player_name = pending["player_name"]
    sid = _sid_for_player(player_name)
    if approve:
        idx = PHASES.index(pending["target_phase"])
        log_activity(f"Watchtower approved {display_name_for(cid)}'s {pending['label']} request")
        _set_phase_by_index(idx)
        if sid:
            socketio.emit("condition_alert", {
                "title": "Approved!",
                "body": f"Watchtower approved your {pending['label']} request.",
            }, room=sid)
    else:
        log_activity(f"Watchtower denied {display_name_for(cid)}'s {pending['label']} request")
        if sid:
            socketio.emit("condition_alert", {
                "title": "Request Denied",
                "body": f"Watchtower denied your {pending['label']} request.",
            }, room=sid)
    broadcast()


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
    old_health = st["health"]
    st["health"] = max(0, min(MAX_HEALTH, st["health"] + delta))
    log_activity(f"{CHARACTERS_BY_ID[cid]['name']} health: {st['health']}")
    if st["health"] < old_health:
        pname = (st.get("player_name") or "").strip()
        sid = _sid_for_player(pname) if pname else None
        if sid:
            socketio.emit("hp_lost", {"new_health": st["health"]}, room=sid)
    broadcast()


@socketio.on("adjust_shield")
def on_adjust_shield(data):
    cid = data["id"]
    delta = int(data.get("delta", 0))
    st = GAME["characters"].get(cid)
    if not st or st["shield"] is None:
        return
    cap = st.get("shield_cap_override") or MAX_HEALTH
    st["shield"] = max(0, min(cap, st["shield"] + delta))
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
    """Generic toggle for cuffs / cure / fixit / fury / starro style on-off flags."""
    cid, field = data["id"], data["field"]
    st = GAME["characters"].get(cid)
    if not st or field not in ("cuffed", "cured", "fixed", "fury", "starro") or st.get(field) is None:
        return
    st[field] = not st[field]
    label = {"cuffed": "Cuffed", "cured": "Cured", "fixed": "Fixed",
              "fury": "Fury", "starro": "Starro"}[field]
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
        f"{counterpart_label} (or a bluffing Sidekick) has 10 seconds to reveal."
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


LOYAL_COMPANION_IDS = {"krypto", "streaky"}


def check_house_of_el_condition():
    """Krypto's and Streaky's 'Loyal Companion' passive: they're only
    eliminated once every active House of El member (Superman, Supergirl,
    Superboy) has themselves been Eliminated. Only meaningful if at least
    one House of El member is actually in play."""
    house = [
        cid for cid, st in GAME["characters"].items()
        if st["active"] and CHARACTERS_BY_ID.get(cid, {}).get("is_house_of_el")
    ]
    if not house or not all(GAME["characters"][cid]["eliminated"] for cid in house):
        return
    for cid in LOYAL_COMPANION_IDS:
        st = GAME["characters"].get(cid)
        if st and st["active"]:
            st["active"] = False
            st["last_action"] = None
            for cond in CONDITIONS.values():
                st[cond["flag"]] = False
            log_activity(f"{display_name_for(cid)} deactivated - all of House of El has been eliminated")


def check_spectre_transformation(eliminated_cid):
    """The Spectre's 'Back from Beyond' passive - the first time a
    Civilian or Bystander is Eliminated, that card deactivates and The
    Spectre takes over with that same player, once per game."""
    if GAME["spectre_triggered"]:
        return
    team = CHARACTERS_BY_ID.get(eliminated_cid, {}).get("team")
    if team not in ("civilian", "bystander"):
        return
    spectre_st = GAME["characters"].get("the_spectre")
    if not spectre_st or not spectre_st["active"] or spectre_st.get("player_name"):
        return
    eliminated_st = GAME["characters"][eliminated_cid]
    player_name = eliminated_st.get("player_name")
    if not player_name:
        return
    GAME["spectre_triggered"] = True
    eliminated_st["active"] = False
    eliminated_st["player_name"] = ""
    eliminated_st["last_action"] = None
    for cond in CONDITIONS.values():
        eliminated_st[cond["flag"]] = False
    spectre_st["active"] = True
    spectre_st["player_name"] = player_name
    spectre_st["spectre_transformed"] = True
    log_activity(f"{player_name} was Eliminated as {display_name_for(eliminated_cid)}... "
                 f"and returns from beyond as The Spectre!")
    push_condition_alert(
        "the_spectre", "Back from Beyond!",
        "You have returned from beyond as The Spectre!"
    )


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
                if action == "end":
                    check_spectre_transformation(cid)
        if action == "end":
            check_house_of_el_condition()
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


def ensure_seating():
    """Assigns a fresh random circular seating arrangement if the seat
    count doesn't match the current player count (first time, or players
    joined/left) - otherwise leaves the existing arrangement untouched,
    since people don't get up and change chairs just because characters
    got redealt."""
    players = [p["name"] for p in GAME["players"]]
    if len(GAME["seats"]) != len(players):
        shuffled = players[:]
        random.shuffle(shuffled)
        GAME["seats"] = shuffled


def seat_index(player_name):
    name = player_name.strip().lower()
    for i, seated in enumerate(GAME["seats"]):
        if seated.strip().lower() == name:
            return i
    return None


def seat_neighbors(player_name):
    """(left_name, right_name) seated beside this player, or (None, None)
    if they're not currently seated."""
    idx = seat_index(player_name)
    if idx is None or len(GAME["seats"]) < 2:
        return None, None
    n = len(GAME["seats"])
    return GAME["seats"][(idx - 1) % n], GAME["seats"][(idx + 1) % n]


def swap_seats(name_a, name_b):
    ia, ib = seat_index(name_a), seat_index(name_b)
    if ia is None or ib is None:
        return False
    GAME["seats"][ia], GAME["seats"][ib] = GAME["seats"][ib], GAME["seats"][ia]
    return True


def seats_in_direction(player_name, direction, count=2):
    """The nearest `count` players in the given direction ('left' or
    'right') from player_name's seat, not including themselves. Used by
    Plastic Man's Group Hug (count=2) and Thunder's Stomp (count=5) -
    caps naturally at however many other seats actually exist."""
    idx = seat_index(player_name)
    n = len(GAME["seats"])
    if idx is None or n < 2:
        return []
    step = -1 if direction == "left" else 1
    max_count = min(count, n - 1)
    return [GAME["seats"][(idx + step * (i + 1)) % n] for i in range(max_count)]


def two_seats_in_direction(player_name, direction):
    """The two nearest players in the given direction ('left' or
    'right') from player_name's seat - Plastic Man's Group Hug."""
    return seats_in_direction(player_name, direction, count=2)


@socketio.on("send_plastic_man_prompt")
def on_send_plastic_man_prompt(data):
    """Host invites Plastic Man to choose Left or Right for Group Hug -
    no phase restriction, matches his card text."""
    cid = data.get("id")
    if cid != "plastic_man":
        return
    st = GAME["characters"].get(cid)
    if not st or not st["active"]:
        return
    pname = (st.get("player_name") or "").strip()
    sid = _sid_for_player(pname) if pname else None
    if sid:
        socketio.emit("plastic_man_prompt", {}, room=sid)
    log_activity(f"{display_name_for(cid)} was invited to choose a Group Hug direction")
    broadcast()


@socketio.on("submit_plastic_man_choice")
def on_submit_plastic_man_choice(data):
    """Plastic Man's player privately picks Left or Right. The two
    players in that direction are silently shielded - per the card,
    they never receive any notification about it."""
    pm_name = (data.get("plastic_man") or "").strip()
    direction = data.get("direction")
    if not pm_name or direction not in ("left", "right"):
        return
    pm_cid = find_player_character_id(pm_name)
    if pm_cid != "plastic_man":
        return
    targets = two_seats_in_direction(pm_name, direction)
    GAME["plastic_man_shielded"] = targets
    log_activity(f"{pm_name} (Plastic Man) used Group Hug to the {direction} - "
                 f"{', '.join(targets) if targets else 'no one (not enough seats)'} silently shielded")
    broadcast()


@socketio.on("send_thunder_prompt")
def on_send_thunder_prompt(data):
    """Host invites Thunder to pick Left or Right for Stomp - Super
    Ability, Round 3+, Inspect! only, matching her card."""
    cid = data.get("id")
    if cid != "thunder":
        return
    st = GAME["characters"].get(cid)
    if not st or not st["active"] or GAME["round"] < 3 or not real_super_ability(cid):
        return
    current_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if current_phase != "Inspect":
        return
    pname = (st.get("player_name") or "").strip()
    sid = _sid_for_player(pname) if pname else None
    if sid:
        socketio.emit("thunder_prompt", {}, room=sid)
    log_activity(f"{display_name_for(cid)} was invited to choose a Stomp direction")
    broadcast()


@socketio.on("submit_thunder_choice")
def on_submit_thunder_choice(data):
    """Thunder's player picks Left or Right. Up to 5 players on that
    side can't Discuss, Accuse, or Vote next round - reuses the same
    Arrested! condition and enforcement as Citizen's Arrest etc."""
    thunder_name = (data.get("thunder") or "").strip()
    direction = data.get("direction")
    if not thunder_name or direction not in ("left", "right"):
        return
    thunder_cid = find_player_character_id(thunder_name)
    if thunder_cid != "thunder":
        return
    targets = seats_in_direction(thunder_name, direction, count=5)
    info = ARREST_INFO["thunder"]
    for target_name in targets:
        target_cid = find_player_character_id(target_name)
        target_st = GAME["characters"].get(target_cid)
        if not target_cid or not target_st or not target_st["active"]:
            continue
        target_st["arrested_scope"] = info["scope"]
        target_st["arrested_by"] = thunder_cid
        target_st["arrested_for_round"] = GAME["round"] + 1
        push_condition_alert(target_cid, info["title"], info["alert"])
    log_activity(f"{thunder_name} (Thunder) used Stomp to the {direction} - "
                 f"{', '.join(targets) if targets else 'no one (not enough seats)'} affected next round")
    broadcast()



@socketio.on("send_speedster_swap_prompt")
def on_send_speedster_swap_prompt(data):
    """Host invites The Flash to silently pick who to swap seats with -
    only usable during Protect!."""
    cid = data.get("id")
    if cid != "the_flash":
        return
    st = GAME["characters"].get(cid)
    if not st or not st["active"]:
        return
    current_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if current_phase != "Protect" or not _visible_phase_abilities(cid, "Protect"):
        return
    pname = (st.get("player_name") or "").strip()
    sid = _sid_for_player(pname) if pname else None
    if sid:
        socketio.emit("speedster_swap_prompt", {
            "candidates": active_player_names(exclude_name=pname)
        }, room=sid)
    log_activity(f"{display_name_for(cid)} was invited to swap seats with a player")
    broadcast()


@socketio.on("submit_speedster_swap_target")
def on_submit_speedster_swap_target(data):
    """The Flash's player privately submits who to swap seats with. The
    swap itself is announced to EVERYONE - it's an obvious tell that one
    of the two is The Flash, which is the whole point of the risk."""
    flash_name = (data.get("flash") or "").strip()
    target_name = (data.get("target_name") or "").strip()
    if not flash_name or not target_name:
        return
    flash_cid = find_player_character_id(flash_name)
    if flash_cid != "the_flash":
        return
    target_cid = find_player_character_id(target_name)
    if not target_cid or not GAME["characters"][target_cid]["active"]:
        return
    if not swap_seats(flash_name, target_name):
        return
    log_activity(f"{flash_name} swapped seats with {target_name}! (Fastest Man Alive)")
    socketio.emit("seat_swap_announcement", {
        "player_a": flash_name, "player_b": target_name,
    }, room="players")
    socketio.emit("seat_swap_announcement", {
        "player_a": flash_name, "player_b": target_name,
    }, room="hosts")
    broadcast()


@socketio.on("send_reverse_flash_prompt")
def on_send_reverse_flash_prompt(data):
    """Host invites Reverse Flash to silently pick a Teleport-targeted
    player to swap seats with - only usable during Rescue!."""
    cid = data.get("id")
    if cid != "reverse_flash":
        return
    st = GAME["characters"].get(cid)
    if not st or not st["active"]:
        return
    current_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if current_phase != "Rescue" or not _visible_phase_abilities(cid, "Rescue"):
        return
    pname = (st.get("player_name") or "").strip()
    sid = _sid_for_player(pname) if pname else None
    candidates = [
        s["player_name"] for c, s in GAME["characters"].items()
        if s["active"] and s.get("player_name") and s.get("targeted")
        and s["player_name"].strip().lower() != pname.lower()
    ]
    if sid:
        socketio.emit("reverse_flash_prompt", {"candidates": candidates}, room=sid)
    log_activity(f"{display_name_for(cid)} was invited to swap seats with a Teleport-targeted player")
    broadcast()


@socketio.on("submit_reverse_flash_target")
def on_submit_reverse_flash_target(data):
    """Reverse Flash's player privately submits who to swap seats with -
    must be someone currently Targeted for Teleportation. The swap is
    announced publicly like Flash's, and Reverse Flash himself escapes
    to Watchtower (Rescued) instead of whatever the target was facing."""
    rf_name = (data.get("reverse_flash") or "").strip()
    target_name = (data.get("target_name") or "").strip()
    if not rf_name or not target_name:
        return
    rf_cid = find_player_character_id(rf_name)
    if rf_cid != "reverse_flash":
        return
    target_cid = find_player_character_id(target_name)
    target_st = GAME["characters"].get(target_cid)
    if not target_cid or not target_st or not target_st["active"] or not target_st.get("targeted"):
        return
    if not swap_seats(rf_name, target_name):
        return
    rf_st = GAME["characters"][rf_cid]
    rf_st["rescued"] = True
    rf_st["last_action"] = "watchtower"
    if rf_cid not in GAME["round_events"]["rescued"]:
        GAME["round_events"]["rescued"].append(rf_cid)
    log_activity(f"{rf_name} swapped seats with {target_name} and teleported to Watchtower! (Not So Fast)")
    socketio.emit("seat_swap_announcement", {
        "player_a": rf_name, "player_b": target_name,
    }, room="players")
    socketio.emit("seat_swap_announcement", {
        "player_a": rf_name, "player_b": target_name,
    }, room="hosts")
    push_condition_alert(rf_cid, "Rescued!", "You swapped seats and teleported straight to Watchtower!")
    broadcast()
    check_win_condition()


def _do_shuffle(exclude_char_ids=None):
    """Randomly reassign active characters to players, excluding any
    character ids in exclude_char_ids from both the pool being reassigned
    and the shuffle (their current player keeps their existing character).
    Returns an error message string on failure, or None on success."""
    exclude_char_ids = set(exclude_char_ids or set())
    spectre_st = GAME["characters"].get("the_spectre")
    if spectre_st and spectre_st["active"] and not spectre_st.get("spectre_transformed"):
        # Dormant Spectre never gets a player via shuffle and doesn't
        # count toward the active total - he's waiting for Back from Beyond.
        exclude_char_ids.add("the_spectre")
    fixed_names = {
        GAME["characters"][cid]["player_name"].strip().lower()
        for cid in exclude_char_ids
        if GAME["characters"].get(cid, {}).get("player_name")
    }
    players = [p["name"] for p in GAME["players"] if p["name"].strip().lower() not in fixed_names]
    active_ids = [
        cid for cid, st in GAME["characters"].items()
        if st["active"] and cid not in exclude_char_ids
    ]

    if not players:
        return "No players available to shuffle."
    if len(players) > len(active_ids):
        return (f"Not enough active characters ({len(active_ids)}) for {len(players)} players. "
                f"Toggle more characters on in the roster first.")

    for cid in active_ids:
        GAME["characters"][cid]["player_name"] = ""

    chosen = random.sample(active_ids, len(players))
    random.shuffle(players)
    assignment = dict(zip(players, chosen))
    for name, cid in assignment.items():
        GAME["characters"][cid]["player_name"] = name

    for name, cid in assignment.items():
        char_name = display_name_for(cid)
        for sid, pname in PLAYER_SIDS.items():
            if pname.strip().lower() == name.strip().lower():
                socketio.emit("shuffle_reveal", {"character": char_name, "id": cid}, room=sid)
    return None


@socketio.on("shuffle_characters")
def on_shuffle_characters():
    ensure_seating()
    error = _do_shuffle()
    if error:
        socketio.emit("shuffle_error", {"message": error}, room=request.sid)
        return
    log_activity(f"Shuffled characters to {len(GAME['players'])} players")
    broadcast()
    push_phase_reminders()


@socketio.on("grodd_mind_scramble")
def on_grodd_mind_scramble():
    """Grodd's Super Ability: shuffle everyone's character assignment
    twice in a row (Grodd himself is excluded, per his card)."""
    st = GAME["characters"].get("grodd")
    if not st or not st["active"]:
        return
    if GAME["round"] < 3 or not real_super_ability("grodd"):
        socketio.emit("character_limit_error", {
            "message": "Grodd's Super Ability isn't active until Round 3."
        }, room=request.sid)
        return
    for _ in range(2):
        error = _do_shuffle(exclude_char_ids={"grodd"})
        if error:
            socketio.emit("character_limit_error", {"message": error}, room=request.sid)
            return
    log_activity("Grodd used Mind Scramble - everyone (except Grodd) shuffled twice!")
    broadcast()
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
    st = GAME["characters"].get(cid, {})
    if cid == "parasite" and st.get("absorbed_from"):
        absorbed_cid = st["absorbed_from"]
        absorbed_card = CARDS.get(absorbed_cid, {})
        absorbed_name = display_name_for(absorbed_cid)
        card = dict(card)
        card["abilities"] = (
            list(card.get("abilities", []))
            + [f"\u2014 Absorbed from {absorbed_name} \u2014"]
            + list(absorbed_card.get("abilities", []))
        )
    socketio.emit("my_card_result", {
        "assigned": True,
        "id": cid,
        "character": display_name_for(cid),
        "card": card,
        "team": char.get("team"),
        "is_kryptonian": char.get("is_kryptonian", False),
        "is_speedster": char.get("is_speedster", False),
        "fury": bool(st.get("fury")),
        "starro": bool(st.get("starro")),
        "lobo_tracker": GAME["lobo_tracker"] if cid == "lobo" else None,
        "speedster_count": active_speedster_count(exclude_cid="zoom") if cid == "zoom" else None,
        "kryptonian_count": active_kryptonian_count(exclude_cid=cid) if cid in KRYPTONIAN_COUNT_CHARACTERS else None,
        "round_change": round_change_button_state(cid),
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
        is_martian = voter_cid and CHARACTERS_BY_ID.get(voter_cid, {}).get("team") == "martian"
        is_alchemy_eliminator = voter_cid and GAME["characters"].get(voter_cid, {}).get("alchemy_type") == "eliminator"
        if not voter_cid or not (is_martian or is_alchemy_eliminator):
            return  # only White Martians (or Alchemy-made Eliminators) vote during Eliminate!
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


def exposed_player_names(exclude_name=None):
    """Real names of every active, assigned, currently-Exposed player -
    the pool Parasite can absorb from."""
    names = [
        st["player_name"] for cid, st in GAME["characters"].items()
        if st["active"] and st.get("player_name") and st.get("exposed")
    ]
    if exclude_name:
        names = [n for n in names if n.strip().lower() != exclude_name.strip().lower()]
    return names


    if exclude_name:
        names = [n for n in names if n.strip().lower() != exclude_name.strip().lower()]
    return names


# Martian Manhunter and Miss Martian both build their own private network
# of Telepathically Linked heroes one at a time (Telepathic Link, Inspect!
# phase), then can cross-reveal everyone in that network to each other at
# once (Telepathic Team, Super Ability, Round 3+). Each Martian's network
# is tracked separately - GAME["telepathic_links"][cid].
TELEPATHIC_CHARACTERS = {"martian_manhunter", "miss_martian"}

# Miss Tessmacher and Otis lie about their signal during Telepathic Link/
# Team - whoever reads their signal gets a false civilian identity
# instead of the truth. Works best when the decoy is a civilian with a
# real hero connection (Lois Lane, Steve Trevor, etc.), and even better
# if that hero is already part of the same Telepathic network.
LIAR_CHARACTERS = {"miss_tessmacher", "otis"}

CIVILIAN_HERO_DECOYS = {
    cid: targets[0] for cid, targets in KNOWS_IDENTITY_OF.items()
    if len(targets) == 1 and not targets[0].startswith("team:")
    and CHARACTERS_BY_ID.get(cid, {}).get("team") == "civilian"
}


def pick_decoy_identity(network_cids):
    """A believable false civilian identity for a lying character to be
    mistaken for - prefers one connected to a hero already in the same
    Telepathic network, falling back to any civilian-hero decoy."""
    network_heroes = {
        cid for cid in network_cids
        if CHARACTERS_BY_ID.get(cid, {}).get("team") == "hero"
    }
    matches = [decoy for decoy, hero in CIVILIAN_HERO_DECOYS.items() if hero in network_heroes]
    pool = matches or list(CIVILIAN_HERO_DECOYS.keys())
    return random.choice(pool)


def revealed_identity_for(cid, network_cids):
    """What a Telepathic Link/Team participant's identity should display
    as - the truth, unless they're a Liar, in which case a false but
    consistent decoy identity (cached per-game so the same lie holds up
    across both Link and Team reveals)."""
    if cid not in LIAR_CHARACTERS:
        return display_name_for(cid)
    if cid not in GAME["liar_decoys"]:
        GAME["liar_decoys"][cid] = pick_decoy_identity(network_cids)
    return display_name_for(GAME["liar_decoys"][cid])


def eligible_telepathic_link_targets(inspector_cid):
    """Active players not already linked to this inspector, and not the
    inspector themselves."""
    linked = set(GAME["telepathic_links"].get(inspector_cid, []))
    pname = (GAME["characters"].get(inspector_cid, {}).get("player_name") or "").strip()
    return [
        st["player_name"] for cid, st in GAME["characters"].items()
        if st["active"] and st.get("player_name") and cid not in linked
        and st["player_name"].strip().lower() != pname.lower()
    ]


@socketio.on("send_telepathic_link_prompt")
def on_send_telepathic_link_prompt(data):
    """Host invites Martian Manhunter/Miss Martian to silently pick a
    player to 'wake' and privately exchange signals with at the table -
    only usable during Inspect!."""
    cid = data.get("id")
    if cid not in TELEPATHIC_CHARACTERS:
        return
    st = GAME["characters"].get(cid)
    if not st or not st["active"]:
        return
    current_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if current_phase != "Inspect" or not _visible_phase_abilities(cid, "Inspect"):
        return
    GAME["active_telepathy_cid"] = cid
    pname = (st.get("player_name") or "").strip()
    sid = _sid_for_player(pname) if pname else None
    if sid:
        socketio.emit("telepathic_link_prompt", {
            "candidates": eligible_telepathic_link_targets(cid)
        }, room=sid)
    log_activity(f"{display_name_for(cid)} was invited to Telepathically Link with a player")
    broadcast()


@socketio.on("submit_telepathic_link_target")
def on_submit_telepathic_link_target(data):
    """The Martian's player privately submits who they linked with at
    the table - adds them to that Martian's growing network, then both
    sides digitally exchange identities. It's a two-way risk: the Martian
    always reveals their true self, but a lying target (Miss Tessmacher,
    Otis) shows the Martian a false identity instead of their own."""
    inspector_name = (data.get("inspector") or "").strip()
    target_name = (data.get("target_name") or "").strip()
    if not inspector_name or not target_name:
        return
    inspector_cid = find_player_character_id(inspector_name)
    if not inspector_cid or inspector_cid != GAME["active_telepathy_cid"]:
        return
    target_cid = find_player_character_id(target_name)
    if not target_cid or not GAME["characters"][target_cid]["active"]:
        return
    GAME["active_telepathy_cid"] = None
    links = GAME["telepathic_links"].setdefault(inspector_cid, [])
    if target_cid not in links:
        links.append(target_cid)
    log_activity(f"{display_name_for(inspector_cid)} Telepathically Linked with {display_name_for(target_cid)}")

    network_cids = links + [inspector_cid]
    target_sees = revealed_identity_for(target_cid, network_cids)
    inspector_sid = _sid_for_player(inspector_name)
    if inspector_sid:
        socketio.emit("condition_alert", {
            "title": "Telepathic Link!",
            "body": f"{target_name} is {target_sees}.",
        }, room=inspector_sid)
    target_sid = _sid_for_player(target_name)
    if target_sid:
        socketio.emit("condition_alert", {
            "title": "Telepathic Link!",
            "body": f"{inspector_name} is {display_name_for(inspector_cid)}.",
        }, room=target_sid)
    broadcast()


@socketio.on("activate_telepathic_team")
def on_activate_telepathic_team(data):
    """Martian Manhunter's/Miss Martian's Super Ability - cross-reveals
    everyone in their Telepathic network to each other, all at once,
    AND reveals the Martian's own true identity to the whole network -
    it's a risky exchange of information. Liars (Miss Tessmacher, Otis)
    show a false civilian identity instead of their own."""
    cid = data.get("id")
    if cid not in TELEPATHIC_CHARACTERS:
        return
    st = GAME["characters"].get(cid)
    if not st or not st["active"] or GAME["round"] < 3 or not real_super_ability(cid):
        return
    current_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if current_phase != "Inspect":
        return
    links = [c for c in GAME["telepathic_links"].get(cid, []) if GAME["characters"].get(c, {}).get("active")]
    if len(links) < 2:
        socketio.emit("character_limit_error", {
            "message": f"{display_name_for(cid)} needs at least 2 active Telepathically "
                       f"Linked players before Telepathic Team can cross-reveal anyone."
        }, room=request.sid)
        return
    network_cids = links + [cid]
    for viewer_cid in links:
        sid = _sid_for_player(GAME["characters"][viewer_cid].get("player_name") or "")
        if not sid:
            continue
        others = [
            {"player": GAME["characters"][other_cid]["player_name"],
             "character": revealed_identity_for(other_cid, network_cids)}
            for other_cid in links if other_cid != viewer_cid
        ]
        others.append({
            "player": GAME["characters"][cid]["player_name"],
            "character": display_name_for(cid),
        })
        socketio.emit("telepathic_team_reveal", {"entries": others}, room=sid)
    log_activity(f"{display_name_for(cid)} activated Telepathic Team - "
                 f"{len(links)} linked players revealed to each other")
    broadcast()


def full_character_roster():
    """{player_name: character_name} for every active, assigned character -
    what Plastic Man's Petty Thief and Zatanna's Thgiels fo Dnah reveal."""
    return [
        {"player": st["player_name"], "character": display_name_for(cid)}
        for cid, st in GAME["characters"].items()
        if st["active"] and st.get("player_name")
    ]


@socketio.on("send_secret_roster")
def on_send_secret_roster(data):
    """Host triggers Plastic Man's/Zatanna's Super Ability - the player
    immediately sees the full Character-to-Player roster for 10 seconds.
    No approval step needed, it's a pure view."""
    cid = data.get("id")
    if not secret_roster_available(cid):
        return
    pname = (GAME["characters"][cid].get("player_name") or "").strip()
    sid = _sid_for_player(pname) if pname else None
    if sid:
        socketio.emit("secret_roster_view", {"entries": full_character_roster()}, room=sid)
    log_activity(f"{display_name_for(cid)} viewed the Secret Identity roster (10s)")
    broadcast()


@socketio.on("send_giraffe_prompt")
def on_send_giraffe_prompt(data):
    """Beast Boy's Giraffe! - host invites him to silently pick one
    active player to peek at (Accuse! phase only)."""
    cid = data.get("id")
    if cid != "beast_boy":
        return
    st = GAME["characters"].get(cid)
    if not st or not st["active"]:
        return
    current_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if current_phase != "Accuse" or not _visible_phase_abilities(cid, "Accuse"):
        return
    pname = (st.get("player_name") or "").strip()
    sid = _sid_for_player(pname) if pname else None
    if sid:
        socketio.emit("giraffe_prompt", {
            "candidates": active_player_names(exclude_name=pname)
        }, room=sid)
    log_activity(f"{display_name_for(cid)} was invited to peek at a player's card (Giraffe!)")
    broadcast()


@socketio.on("submit_giraffe_target")
def on_submit_giraffe_target(data):
    """Beast Boy's player privately submits who to peek at - sees the
    single reveal immediately, no host approval needed."""
    beast_boy_name = (data.get("beast_boy") or "").strip()
    target_name = (data.get("target_name") or "").strip()
    if not beast_boy_name or not target_name:
        return
    beast_boy_cid = find_player_character_id(beast_boy_name)
    if beast_boy_cid != "beast_boy":
        return
    target_cid = find_player_character_id(target_name)
    if not target_cid or not GAME["characters"][target_cid]["active"]:
        return
    sid = _sid_for_player(beast_boy_name)
    if sid:
        socketio.emit("giraffe_reveal", {
            "player": target_name, "character": display_name_for(target_cid),
        }, room=sid)
    log_activity(f"Beast Boy peeked at {display_name_for(target_cid)}'s card (Giraffe!)")
    broadcast()


PEP_TALK_GIVERS = {"martha_kent": "Ma Kent", "jonathan_kent": "Pa Kent"}
PEP_TALK_SHIELD_VALUE = 6


@socketio.on("activate_pep_talk")
def on_activate_pep_talk(data):
    """Ma/Pa Kent's Pep Talk - during Discuss!, raises Superman's shield
    to 6 for the round, temporarily lifting his normal cap."""
    cid = data.get("id")
    if cid not in PEP_TALK_GIVERS:
        return
    st = GAME["characters"].get(cid)
    if not st or not st["active"] or GAME["round"] < 3 or not real_super_ability(cid):
        return
    current_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if current_phase != "Discuss":
        return
    superman_st = GAME["characters"].get("superman")
    if not superman_st or not superman_st["active"] or superman_st.get("shield") is None:
        socketio.emit("character_limit_error", {
            "message": "Superman isn't active (or has no shield) right now - Pep Talk has nothing to boost."
        }, room=request.sid)
        return
    superman_st["shield"] = PEP_TALK_SHIELD_VALUE
    superman_st["shield_cap_override"] = PEP_TALK_SHIELD_VALUE
    superman_st["pep_talked_for_round"] = GAME["round"]
    giver_name = PEP_TALK_GIVERS[cid]
    log_activity(f"{giver_name} gave Superman a Pep Talk - shield boosted to {PEP_TALK_SHIELD_VALUE}")
    push_condition_alert(
        "superman", "Pep Talk!",
        f"{giver_name} gave you a Pep Talk. You can protect up to {PEP_TALK_SHIELD_VALUE} players this round."
    )
    broadcast()


def eliminated_player_names():
    """Real names of every active, assigned, currently-Eliminated player -
    the pool a Good Doctor can propose restoring."""
    return [
        st["player_name"] for cid, st in GAME["characters"].items()
        if st["active"] and st.get("player_name") and st.get("eliminated")
    ]


@socketio.on("send_good_doctor_prompt")
def on_send_good_doctor_prompt(data):
    """Host invites Dr. Caitlin Snow/Leslie Thompkins/Dr. Harleen Quinzel
    to silently pick an Eliminated player to try to restore."""
    cid = data.get("id")
    if not good_doctor_available(cid):
        return
    st = GAME["characters"][cid]
    GAME["active_good_doctor_cid"] = cid
    pname = (st.get("player_name") or "").strip()
    sid = _sid_for_player(pname) if pname else None
    if sid:
        socketio.emit("good_doctor_prompt", {
            "candidates": eliminated_player_names()
        }, room=sid)
    log_activity(f"{display_name_for(cid)} was invited to try to restore an Eliminated player")
    broadcast()


@socketio.on("submit_good_doctor_target")
def on_submit_good_doctor_target(data):
    """The doctor's player privately submits who they want to restore.
    Queues a private request for the host to approve - nothing changes
    until Watchtower clicks OK."""
    doctor_name = (data.get("doctor") or "").strip()
    target_name = (data.get("target_name") or "").strip()
    if not doctor_name or not target_name:
        return
    doctor_cid = find_player_character_id(doctor_name)
    if not doctor_cid or doctor_cid != GAME["active_good_doctor_cid"]:
        return
    target_cid = find_player_character_id(target_name)
    target_st = GAME["characters"].get(target_cid)
    if not target_cid or not target_st or not target_st["active"] or not target_st.get("eliminated"):
        return
    GAME["active_good_doctor_cid"] = None
    GAME["good_doctor_requests"][target_cid] = {"doctor_cid": doctor_cid, "doctor_name": doctor_name}
    log_activity(f"{display_name_for(doctor_cid)} asked Watchtower to restore {display_name_for(target_cid)}")
    broadcast()


@socketio.on("resolve_good_doctor")
def on_resolve_good_doctor(data):
    """Host approves or denies a pending Good Doctor restoration."""
    target_cid = data.get("target_id")
    approve = bool(data.get("approve"))
    pending = GAME["good_doctor_requests"].pop(target_cid, None)
    if not pending:
        return
    doctor_cid = pending["doctor_cid"]
    doctor_sid = _sid_for_player(pending["doctor_name"])
    target_st = GAME["characters"].get(target_cid)
    if approve and target_st:
        target_st["eliminated"] = False
        target_st["active"] = True
        if target_st.get("health") is not None:
            target_st["health"] = min(MAX_HEALTH, target_st["health"] + 1)
        log_activity(f"Watchtower approved - {display_name_for(target_cid)} was restored by "
                     f"{display_name_for(doctor_cid)}'s A Good Doctor")
        push_condition_alert(target_cid, "Restored!",
                              f"{display_name_for(doctor_cid)} brought you back. You regain "
                              f"consciousness with at least one heart renewed.")
        if doctor_sid:
            socketio.emit("condition_alert", {
                "title": "Approved!",
                "body": f"Watchtower approved your A Good Doctor request - "
                        f"{display_name_for(target_cid)} is restored.",
            }, room=doctor_sid)
    else:
        log_activity(f"Watchtower denied restoring {display_name_for(target_cid)}")
        if doctor_sid:
            socketio.emit("condition_alert", {
                "title": "Request Denied",
                "body": f"Watchtower denied your A Good Doctor request for {display_name_for(target_cid)}.",
            }, room=doctor_sid)
    broadcast()


@socketio.on("send_absorption_prompt")
def on_send_absorption_prompt(data):
    """Host invites Parasite to silently pick one Exposed player to
    absorb abilities from - replaces whatever he'd previously absorbed."""
    cid = data.get("id")
    st = GAME["characters"].get(cid)
    if not st or not st["active"] or cid != "parasite":
        return
    phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if phase != "Accuse":
        return
    GAME["active_absorber_cid"] = cid
    pname = (st.get("player_name") or "").strip()
    sid = _sid_for_player(pname) if pname else None
    if sid:
        socketio.emit("absorption_prompt", {
            "candidates": exposed_player_names(exclude_name=pname)
        }, room=sid)
    log_activity(f"{display_name_for(cid)} was invited to absorb an Exposed player's abilities")
    broadcast()


@socketio.on("submit_absorption_target")
def on_submit_absorption_target(data):
    """Parasite's player privately submits who to absorb. Replaces any
    previously absorbed character - only one at a time."""
    parasite_name = (data.get("parasite") or "").strip()
    target_name = (data.get("target_name") or "").strip()
    if not parasite_name or not target_name:
        return
    parasite_cid = find_player_character_id(parasite_name)
    if not parasite_cid or parasite_cid != GAME["active_absorber_cid"]:
        return
    target_cid = find_player_character_id(target_name)
    target_st = GAME["characters"].get(target_cid)
    if not target_cid or not target_st or not target_st["active"] or not target_st.get("exposed"):
        return
    GAME["active_absorber_cid"] = None
    GAME["characters"]["parasite"]["absorbed_from"] = target_cid
    log_activity(f"Parasite absorbed {display_name_for(target_cid)}'s abilities")
    broadcast()


@socketio.on("send_alchemy_prompt")
def on_send_alchemy_prompt(data):
    """Host invites Dr. Alchemy to silently pick any active player, then
    choose to make them a Protector or an Eliminator."""
    cid = data.get("id")
    st = GAME["characters"].get(cid)
    if not st or not st["active"] or cid != "dr_alchemy":
        return
    phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if phase != "Inspect":
        return
    GAME["active_alchemist_cid"] = cid
    GAME["pending_alchemy"] = None
    pname = (st.get("player_name") or "").strip()
    sid = _sid_for_player(pname) if pname else None
    if sid:
        socketio.emit("alchemy_prompt", {
            "candidates": active_player_names(exclude_name=pname)
        }, room=sid)
    log_activity(f"{display_name_for(cid)} was invited to use the Alchemy Stone")
    broadcast()


@socketio.on("submit_alchemy_target")
def on_submit_alchemy_target(data):
    """Step 1: Dr. Alchemy's player picks who the Stone targets."""
    alchemist_name = (data.get("alchemist") or "").strip()
    target_name = (data.get("target_name") or "").strip()
    if not alchemist_name or not target_name:
        return
    alchemist_cid = find_player_character_id(alchemist_name)
    if not alchemist_cid or alchemist_cid != GAME["active_alchemist_cid"]:
        return
    target_cid = find_player_character_id(target_name)
    if not target_cid or not GAME["characters"][target_cid]["active"]:
        return
    GAME["pending_alchemy"] = {"alchemist_cid": alchemist_cid, "target_cid": target_cid}
    sid = _sid_for_player(alchemist_name)
    if sid:
        socketio.emit("alchemy_choice_prompt", {
            "target_name": display_name_for(target_cid)
        }, room=sid)
    broadcast()


@socketio.on("submit_alchemy_choice")
def on_submit_alchemy_choice(data):
    """Step 2: Dr. Alchemy's player chooses Protector or Eliminator for
    whoever they targeted in step 1."""
    alchemist_name = (data.get("alchemist") or "").strip()
    choice = data.get("choice")
    pending = GAME["pending_alchemy"]
    if not alchemist_name or choice not in ("protector", "eliminator") or not pending:
        return
    alchemist_cid = find_player_character_id(alchemist_name)
    if not alchemist_cid or alchemist_cid != pending["alchemist_cid"]:
        return
    target_cid = pending["target_cid"]
    target_st = GAME["characters"].get(target_cid)
    if not target_st or not target_st["active"]:
        return
    target_st["alchemy_type"] = choice
    if choice == "protector" and target_st.get("shield") is None:
        target_st["shield"] = SHIELD_START
    GAME["active_alchemist_cid"] = None
    GAME["pending_alchemy"] = None
    log_activity(f"{display_name_for(alchemist_cid)} made {display_name_for(target_cid)} a {choice.capitalize()}")
    broadcast()


def eligible_arresters():
    """Active characters who can apply the Arrested! condition, whose
    ability is currently visible."""
    return [
        {"id": cid, "name": display_name_for(cid), "scope": ARREST_INFO[cid]["scope"]}
        for cid, st in GAME["characters"].items()
        if cid in ARREST_INFO and st["active"]
        and _visible_phase_abilities(cid, ARREST_INFO[cid]["phase"])
    ]


@socketio.on("send_arrest_prompt")
def on_send_arrest_prompt(data):
    """Host invites James Gordon/Maggie Sawyer/Robin/Batgirl/Zatanna to
    silently pick a player to arrest - only usable during that ability's
    tagged phase (varies per character - see ARREST_INFO)."""
    cid = data.get("id")
    st = GAME["characters"].get(cid)
    if not st or not st["active"] or cid not in ARREST_INFO:
        return
    required_phase = ARREST_INFO[cid]["phase"]
    phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if phase != required_phase:
        return
    if not _visible_phase_abilities(cid, required_phase):
        return
    GAME["active_arrester_cid"] = cid
    pname = (st.get("player_name") or "").strip()
    sid = _sid_for_player(pname) if pname else None
    if sid:
        socketio.emit("arrest_prompt", {
            "candidates": active_player_names(exclude_name=pname)
        }, room=sid)
    log_activity(f"{display_name_for(cid)} was invited to arrest a player")
    broadcast()


@socketio.on("submit_arrest_target")
def on_submit_arrest_target(data):
    """The arresting player privately submits who to target. Applies the
    Arrested! condition, effective starting next round, and immediately
    alerts the target with the specific, flavorful restriction text tied
    to whichever ability caught them."""
    arrester_name = (data.get("arrester") or "").strip()
    target_name = (data.get("target_name") or "").strip()
    if not arrester_name or not target_name:
        return
    arrester_cid = find_player_character_id(arrester_name)
    if not arrester_cid or arrester_cid != GAME["active_arrester_cid"]:
        return
    target_cid = find_player_character_id(target_name)
    target_st = GAME["characters"].get(target_cid)
    if not target_cid or not target_st or not target_st["active"]:
        return
    info = ARREST_INFO[arrester_cid]
    target_st["arrested_scope"] = info["scope"]
    target_st["arrested_by"] = arrester_cid
    target_st["arrested_for_round"] = GAME["round"] + 1
    GAME["active_arrester_cid"] = None
    log_activity(f"{display_name_for(arrester_cid)} arrested {display_name_for(target_cid)} "
                 f"({'Discuss/Vote/Accuse blocked' if info['scope'] == 'phases' else 'all abilities blocked'} next round)")
    push_condition_alert(target_cid, info["title"], info["alert"])
    broadcast()


@socketio.on("send_protect_prompt")
def on_send_protect_prompt(data):
    """Host invites a specific eligible Hero to choose who to protect this
    phase - only that character's player can submit a target until it's
    resolved or the host moves to someone else. Also works for anyone
    Dr. Alchemy granted Protector status to."""
    cid = data.get("id")
    st = GAME["characters"].get(cid)
    char = CHARACTERS_BY_ID.get(cid)
    is_natural_protector = char and char.get("has_shield") and st and st.get("shield") is not None
    is_alchemy_protector = st and st.get("alchemy_type") == "protector" and st.get("shield") is not None
    if not st or not st["active"] or not (is_natural_protector or is_alchemy_protector):
        return
    phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    if phase != "Protect":
        return
    if is_natural_protector and not _visible_phase_abilities(cid, "Protect"):
        return  # locked Super Ability before Round 3
    GAME["active_protector_cid"] = cid
    pname = (st.get("player_name") or "").strip()
    sid = _sid_for_player(pname) if pname else None
    if sid:
        candidates = active_player_names(exclude_name=None if can_self_protect(cid) else pname)
        socketio.emit("protect_prompt", {
            "candidates": candidates,
            "can_self_protect": can_self_protect(cid),
        }, room=sid)
    log_activity(f"{display_name_for(cid)} was invited to choose someone to protect")
    broadcast()


@socketio.on("submit_protect_target")
def on_submit_protect_target(data):
    """A Hero's player privately submits who they want to protect. Fills
    the first open protection slot for that target. If the target already
    has all 3 slots full this round, the Hero is never told - only the
    host sees a notification, per the game's silent-protector design."""
    protector_name = (data.get("protector") or "").strip()
    target_name = (data.get("target_name") or "").strip()
    if not protector_name or not target_name:
        return
    protector_cid = find_player_character_id(protector_name)
    if not protector_cid or protector_cid != GAME["active_protector_cid"]:
        return  # host hasn't invited this character to protect right now
    target_cid = find_player_character_id(target_name)
    if not target_cid or not GAME["characters"][target_cid]["active"]:
        return
    GAME["active_protector_cid"] = None
    target_st = GAME["characters"][target_cid]
    protector_display = display_name_for(protector_cid)
    target_display = display_name_for(target_cid)
    if False in target_st["protection"]:
        target_st["protection"][target_st["protection"].index(False)] = True
        log_activity(f"{protector_display} protected {target_display}")
    else:
        socketio.emit("character_limit_error", {
            "message": f"{protector_display} tried to protect {target_display}, but "
                       f"their protection is already full this round. {protector_display}'s "
                       f"player was not told this failed."
        }, room="hosts")
        log_activity(f"{protector_display} tried to protect {target_display} - already full")
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
    GAME["active_protector_cid"] = None
    GAME["lobo_tracker"] = {"civilian": 0, "hero": 0, "martian": 0}
    GAME["active_absorber_cid"] = None
    GAME["active_alchemist_cid"] = None
    GAME["pending_alchemy"] = None
    GAME["active_arrester_cid"] = None
    GAME["round_change_requests"] = {}
    GAME["active_good_doctor_cid"] = None
    GAME["good_doctor_requests"] = {}
    GAME["active_telepathy_cid"] = None
    GAME["telepathic_links"] = {"martian_manhunter": [], "miss_martian": []}
    GAME["liar_decoys"] = {}
    GAME["pending_gl_reveals"] = []
    GAME["plastic_man_shielded"] = []
    GAME["spectre_triggered"] = False
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
