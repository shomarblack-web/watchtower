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
    NARRATION_PROMPTS, INTRO_SCRIPT, PACKS, PACK_LABELS,
)

CARDS = json.loads((Path(__file__).parent / "cards.json").read_text())

# Pull the trailing "(Protect!)" / "(Accuse!)" style tag off each ability's
# text so we can remind a player of their move when that phase comes up.
# e.g. "...may shield another player (Protect!)" -> tagged under "Protect".
_PHASE_TAG_RE = re.compile(r"\(([A-Za-z]+)!\)")
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
        state[c["id"]] = {
            "active": False,
            "player_name": "",
            "health": c["start_health"] if c["has_health"] else None,
            "protection": [False, False, False],
            "last_action": None,     # e.g. "Watchtower", "Exposed", "Deactivated"
            "shield": SHIELD_START if c["has_shield"] else None,
            "cuffed": False if c["has_cuffs"] else None,
            "cured": False if c["has_cure"] else None,
            "fixed": False if c["has_fixit"] else None,
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
}


def is_unlocked(character_id):
    pack = CHARACTERS_BY_ID.get(character_id, {}).get("pack")
    if pack is None:
        return False
    return pack in GAME["unlocked_packs"]


def log_activity(text):
    GAME["activity"].insert(0, text)
    GAME["activity"] = GAME["activity"][:12]


def vote_tally():
    tally = {}
    for target_id in GAME["votes"].values():
        tally[target_id] = tally.get(target_id, 0) + 1
    # sort descending by count
    return sorted(tally.items(), key=lambda kv: -kv[1])


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
        CHARACTERS_BY_ID[cid]["name"]
        for cid, st in GAME["characters"].items()
        if st["active"] and CHARACTERS_BY_ID.get(cid, {}).get("team") == team
    ]


def _character_display_name(cid):
    """Real player name if assigned, else the character's own name."""
    st = GAME["characters"].get(cid)
    if not st:
        return None
    return st.get("player_name") or CHARACTERS_BY_ID.get(cid, {}).get("name")


def render_phase_script():
    """Build the exact line(s) the moderator should read aloud for whatever
    phase is currently active, filled in from live game state. Returns None
    when no phase is selected, or for phases with no script defined.
    """
    idx = GAME["phase_index"]
    if idx is None:
        return None
    phase = PHASES[idx]

    if phase == "Report":
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
            text = (
                f"...I'm sending {heroes} to the Martian Prison to rescue {civilians}. "
                f"You're surprised to find {villains} in the prison as well. "
                f"Scanners indicate at least {martian_count} among you."
            )
            return {"phase": "Report", "kind": "briefing", "lines": [text]}
        else:
            rescued = [CHARACTERS_BY_ID[c]["name"] for c in history["rescued"] if c in CHARACTERS_BY_ID]
            eliminated = [CHARACTERS_BY_ID[c]["name"] for c in history["eliminated"] if c in CHARACTERS_BY_ID]
            rescued_clause = (
                f"I safely beamed {_join_names(rescued)} up to Watchtower."
                if rescued else "No one made it to Watchtower last round."
            )
            eliminated_clause = (
                f"Unfortunately, {_join_names(eliminated)} didn't survive the night."
                if eliminated else "Everyone else made it through the night safely."
            )
            text = f"Welcome back. {rescued_clause} {eliminated_clause}"
            return {"phase": "Report", "kind": "recap", "lines": [text]}

    if phase == "Discuss":
        return {"phase": "Discuss", "kind": "static", "lines": [
            "Booting up the teleporter. You've got two minutes to discuss who you want to send to Watchtower."
        ]}

    if phase == "Vote":
        nominees = _join_names([
            st["player_name"] for cid, st in GAME["characters"].items()
            if st["active"] and st.get("player_name")
        ])
        lines = [f"1. Raise your hand if you want {nominees} to reach Watchtower?"]
        tally = vote_tally()
        if tally:
            winner_name = _character_display_name(tally[0][0]) or "the winner"
            lines.append(f"2. Calibrating teleporter. Keep still {winner_name}.")
        else:
            lines.append("2. Calibrating teleporter\u2026 (waiting for votes)")
        return {"phase": "Vote", "kind": "live", "lines": lines}

    if phase == "Accuse":
        return {"phase": "Accuse", "kind": "static", "lines": [
            "...Any accusations of identity I need to log?"
        ]}

    if phase == "Rescue":
        winner_name = "the winner"
        if GAME["last_vote_winner"]:
            winner_name = _character_display_name(GAME["last_vote_winner"]) or winner_name
        return {"phase": "Rescue", "kind": "static", "lines": [
            f"I'm beaming up {winner_name}",
            "MIND THE FLASH OF THE TELEPORTER BEAM. EVERYONE, EYES CLOSED!",
        ]}

    return None


def public_state(reveal_names):
    """Everything the frontend needs to render, in one payload.

    reveal_names=True (host only) includes each character's assigned player
    name. reveal_names=False (players) strips that field entirely so no
    phone can see who's playing whom - except each player privately learns
    their own via the separate 'whoami_result' event below.
    """
    characters = {}
    for cid, st in GAME["characters"].items():
        c = dict(st)
        if not reveal_names:
            c["player_name"] = ""
        characters[cid] = c
    return {
        "round": GAME["round"],
        "num_rounds": NUM_ROUNDS,
        "phase_index": GAME["phase_index"],
        "phases": PHASES,
        "characters": characters,
        "votes": GAME["votes"],
        "tally": vote_tally(),
        "activity": GAME["activity"],
        "map": GAME["map"],
        "players": GAME["players"],
        "roster_locked": GAME["roster_locked"],
        "unlocked_packs": sorted(GAME["unlocked_packs"]),
        "phase_script": render_phase_script(),
    }


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
            matches.append(CHARACTERS_BY_ID[cid]["name"])
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


def push_phase_reminders():
    """Privately nudge each player whose character has an ability tagged
    for the phase that's currently active."""
    phase_name = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    for sid, name in PLAYER_SIDS.items():
        cid = find_player_character_id(name)
        abilities = []
        if cid and phase_name:
            abilities = ABILITY_PHASE_MAP.get(cid, {}).get(phase_name, [])
        socketio.emit("phase_reminder", {
            "phase": phase_name,
            "character": CHARACTERS_BY_ID[cid]["name"] if cid else None,
            "abilities": abilities,
        }, room=sid)


def broadcast():
    socketio.emit("state", public_state(reveal_names=True), room="hosts")
    socketio.emit("state", public_state(reveal_names=False), room="players")
    push_whoami()


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
    broadcast()


@socketio.on("set_phase")
def on_set_phase(data):
    old_phase = PHASES[GAME["phase_index"]] if GAME["phase_index"] is not None else None
    idx = data.get("phase_index")
    if old_phase == "Vote":
        tally = vote_tally()
        GAME["last_vote_winner"] = tally[0][0] if tally else None
    GAME["phase_index"] = idx
    if idx is not None:
        log_activity(f"Phase: {PHASES[idx]}!")
        if PHASES[idx] not in ("Vote", "Accuse"):
            GAME["votes"] = {}
    broadcast()
    push_phase_reminders()


@socketio.on("toggle_character")
def on_toggle_character(data):
    cid = data["id"]
    if cid not in GAME["characters"]:
        return
    if not is_unlocked(cid):
        return  # character's pack isn't unlocked - ignore the toggle
    st = GAME["characters"][cid]
    st["active"] = not st["active"]
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
    broadcast()


@socketio.on("start_game")
def on_start_game():
    GAME["roster_locked"] = True
    log_activity(f"Roster locked with {len(GAME['players'])} players")
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
        char_name = CHARACTERS_BY_ID[cid]["name"]
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
    card = CARDS.get(cid, {})
    socketio.emit("my_card_result", {
        "assigned": True,
        "character": CHARACTERS_BY_ID[cid]["name"],
        "card": card,
    }, room=request.sid)


@socketio.on("cast_vote")
def on_cast_vote(data):
    voter = data.get("voter", "").strip()
    target_id = data.get("target_id")
    if not voter or target_id not in GAME["characters"]:
        return
    GAME["votes"][voter] = target_id
    log_activity(f"{voter} voted")
    broadcast()


@socketio.on("reset_votes")
def on_reset_votes():
    GAME["votes"] = {}
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
    # Keep everyone who's currently connected in the roster so they don't
    # have to re-type their name, but drop eliminated flags and reordering.
    seen = set()
    fresh_players = []
    for name in PLAYER_SIDS.values():
        norm = name.strip().lower()
        if name and norm not in seen:
            seen.add(norm)
            fresh_players.append({"name": name.strip(), "eliminated": False})
    GAME["players"] = fresh_players
    log_activity("New game started")
    socketio.emit("game_reset", room="hosts")
    broadcast()


if __name__ == "__main__":
    ip = get_lan_ip()
    print("\n  White Martian - Watchtower is running")
    print(f"  Host console:   http://localhost:5000/host")
    print(f"  Player devices: http://{ip}:5000/play  (same WiFi)\n")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
