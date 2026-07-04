# Single source of truth for every character in the game.
# Adding a character = adding one line here. No UI code to touch.
#
# team: martian | hero | villain | civilian | sidekick | bystander
# actions: which action buttons this character's row shows on the host board

TEAM_LABELS = {
    "martian": "Martians",
    "hero": "Heroes",
    "villain": "Villains",
    "civilian": "Civilians",
    "sidekick": "Sidekicks",
    "bystander": "Bystanders",
}

TEAM_COLORS = {
    "martian": "#9aa1ab",    # bland, desaturated slate
    "hero": "#3b7fe0",       # red/white/blue trio - blue as primary
    "villain": "#2fbf6e",    # green/purple duo - green as primary
    "civilian": "#f3aecb",   # pastel pink/powder-blue duo - pink as primary
    "sidekick": "#e0b13f",   # gold/blue duo - gold as primary
    "bystander": "#c9cfd8",
}

STANDARD_ACTIONS = ["end", "hive", "teleport", "watchtower", "expose", "deactivate"]

# Starting health per team, from the original file's hero_health/civ_health/
# vill_health/skick_health constants. Martians and bystanders never had a
# health track in the original - kept that way here.
BASE_HEALTH = {"hero": 2, "villain": 1, "civilian": 1, "sidekick": 1}
HEALTH_OVERRIDES = {"superman": 3}  # "must be targeted thrice" passive
MAX_HEALTH = 4

# Characters with an independent shield/protection charge (originally all
# wired to one shared global counter - fixed here so each is separate).
# Starting charge count matches the original's protxn_show = 1.
SHIELD_CHARACTERS = {
    "superman", "the_flash", "green_lantern", "captain_marvel", "zatanna",
    "plastic_man", "booster_gold", "krypto", "streaky", "supergirl",
    "superboy", "wonder_girl", "miss_martian", "freddie_freeman",
    "kendra_saunders", "mary_batson",
}
SHIELD_START = 1

# "Citizen's arrest" handcuffs toggle - the three law-enforcement civilians
CUFFS_CHARACTERS = {"james_gordon", "joe_west", "maggie_sawyer"}

# "Cure" ability - the three medical-professional civilians
CURE_CHARACTERS = {"leslie_thompkins", "dr_harleen_quinzel", "dr_caitlin_snow"}

# "Fix-it" tech ability
FIXIT_CHARACTERS = {"harrison_wells", "felicity_smoak"}

CHARACTERS = []


def _add(team, names):
    for n in names:
        cid = n.lower().replace(".", "").replace("'", "").replace(" ", "_")
        char = {
            "id": cid,
            "name": n,
            "team": team,
            "actions": STANDARD_ACTIONS,
            "has_health": team in BASE_HEALTH,
            "start_health": HEALTH_OVERRIDES.get(cid, BASE_HEALTH.get(team)),
            "has_shield": cid in SHIELD_CHARACTERS,
            "has_cuffs": cid in CUFFS_CHARACTERS,
            "has_cure": cid in CURE_CHARACTERS,
            "has_fixit": cid in FIXIT_CHARACTERS,
        }
        CHARACTERS.append(char)


_add("martian", ["White Martian I", "White Martian II"])

_add("hero", [
    "Superman", "Wonder Woman", "Batman", "The Flash", "Green Lantern",
    "Black Lightning", "Captain Marvel", "Martian Manhunter", "Vibe",
    "Swamp Thing", "Zatanna", "Hawkman", "The Spectre", "Plastic Man",
    "Booster Gold",
])

_add("villain", [
    "Lex Luthor", "Lena Luthor", "Reverse Flash", "Riddler",
    "Tobias Whale", "Lobo", "Parasite", "Miss Tessmacher", "Otis", "Mercy",
    "Zod", "Cheetah", "Joker", "Sinestro", "Black Adam", "Zoom", "Faora",
    "Grodd", "Roulette", "Mr. Mxyzptlk", "Bat-Mite", "Dr. Alchemy", "Ares",
    "Maxima", "Vandal Savage", "Ra's Al-Ghul", "Starro", "Granny Goodness",
    "Doomsday", "Darkseid", "Brainiac", "Poison Ivy",
    "Ma'alefa'k", "Reign",
])

_add("civilian", [
    "Lois Lane", "Jimmy Olsen", "Cat Grant", "Perry White", "Steve Trevor",
    "Iris West", "Joe West", "A. Pennyworth", "Leslie Thompkins",
    "James Gordon", "Maggie Sawyer", "Mary Batson", "Freddie Freeman",
    "Martin Stein", "Jefferson Jackson", "Martha Kent", "Jonathan Kent",
    "Pete Ross", "Lana Lang", "Kendra Saunders", "Dr. Harleen Quinzel",
    "Dr. Caitlin Snow", "Harvey Dent", "Harrison Wells", "Felicity Smoak",
])

_add("sidekick", [
    "Krypto", "Streaky", "Robin", "Batgirl", "Kid Flash", "Jesse Quick",
    "Supergirl", "Superboy", "Wonder Girl", "Miss Martian", "Bumblebee",
    "Beast Boy", "Thunder",
])

_add("bystander", [
    "Bystander 1", "Bystander 2", "Bystander 3",
    "Bystander 4", "Bystander 5", "Bystander 6",
])

# A few characters are referred to by different names in the card-pack set
# list than in the original file. IDs stay the same (so cards.json, shields,
# etc. keep working) - only the displayed name changes.
DISPLAY_NAME_OVERRIDES = {
    "captain_marvel": "Shazam!",
    "martha_kent": "Ma Kent",
    "jonathan_kent": "Pa Kent",
    "a_pennyworth": "Alfred Pennyworth",
    "grodd": "Gorilla Grodd",
    "reign": "Samantha Arias",  # starts disguised - see SWITCH_CHARACTERS below
}
for _c in CHARACTERS:
    if _c["id"] in DISPLAY_NAME_OVERRIDES:
        _c["name"] = DISPLAY_NAME_OVERRIDES[_c["id"]]

# Reign starts the game disguised as an ordinary civilian (Samantha Arias),
# same as the other six switch characters below - reclassify her team to
# match, since the roster groups by team and she should sit with the other
# disguised civilians until revealed.
for _c in CHARACTERS:
    if _c["id"] == "reign":
        _c["team"] = "civilian"

# ------------------------------------------------------------------------
# Switch mechanic: these seven start the game displayed under their
# ordinary civilian name. The host triggers a reveal mid-game (via the
# "Reveal" button on their roster row), which swaps their displayed name
# to their secret identity everywhere - roster, "My Card," the shuffle
# reveal, and narration - and unlocks whatever their card's Hero/Villain-
# only abilities describe. Their card's own text already distinguishes
# "Type: Civilian only" vs "Type: Hero/Villain only" abilities; players
# only ever see the abilities that match their character's current state
# (see get_my_card in app.py). Three of them (Mary Batson, Freddie Freeman,
# Kendra Saunders) also don't get their shield charge until revealed, since
# their card ties the shield to their Hero-only "Human Shield" ability.
# ------------------------------------------------------------------------
SWITCH_CHARACTERS = {
    "mary_batson": "Mary Marvel",
    "freddie_freeman": "Cpt. Marvel, Jr.",
    "kendra_saunders": "Hawkwoman",
    "harvey_dent": "Two-Face",
    "dr_harleen_quinzel": "Harley Quinn",
    "dr_caitlin_snow": "Killer Frost",
    "reign": "Reign",
}
for _c in CHARACTERS:
    _c["is_switchable"] = _c["id"] in SWITCH_CHARACTERS
    _c["reveal_name"] = SWITCH_CHARACTERS.get(_c["id"])

# ------------------------------------------------------------------------
# Hostage-taking. A villain targets one player and takes them hostage; the
# named "counterpart" hero has 10 seconds (a real-world, verbal callout -
# "I am Batman!") to reveal their identity or the hostage loses 1 health.
# counterpart is:
#   - a character id: one specific hero must step up
#   - "kryptonian": any currently-active character in KRYPTONIAN_IDS may
#     step up to save the hostage
#   - None: Two-Face's "Let Fate Decide" - free choice of two targets, no
#     hero-reveal-to-save mechanic, resolved by coin flip instead
# ------------------------------------------------------------------------
KRYPTONIAN_IDS = {"superman", "supergirl", "superboy", "krypto"}

# Broader than KRYPTONIAN_IDS above (which is scoped narrowly to "heroes who
# can save a Kryptonian hostage"). This one is for the Kryptonian team icon
# on player cards, so it includes villain-side Kryptonians too.
KRYPTONIAN_TEAM_IDS = {"superman", "supergirl", "superboy", "krypto", "zod", "faora", "reign"}
for _c in CHARACTERS:
    _c["is_kryptonian"] = _c["id"] in KRYPTONIAN_TEAM_IDS

# ------------------------------------------------------------------------
# "Know You Anywhere" and its many variants. Each entry is a LIST of
# target specs, each either a literal character id (single target) or
# "team:<team_name>" (know everyone currently active on that team).
# Multiple specs can combine (The Spectre knows both Martians AND
# Villains). Powers the Secret Identity phase: when triggered, each
# knower (if active and assigned) privately learns which players are
# behind every one of their resolved, active, assigned targets.
#
# Plastic Man's "Petty Thief" is deliberately excluded - it's a Super
# Ability letting him look at a physical Secret Identity roster himself,
# not a "knows a specific target" passive, same as Zatanna's and Granny
# Goodness's "check the Secret Identity page" abilities.
# ------------------------------------------------------------------------
KNOWS_IDENTITY_OF = {
    "steve_trevor": ["wonder_woman"],
    "lois_lane": ["superman"],
    "iris_west": ["the_flash"],
    "joe_west": ["the_flash"],
    "a_pennyworth": ["batman"],
    "leslie_thompkins": ["batman"],
    "martha_kent": ["superman"],
    "jonathan_kent": ["superman"],
    "pete_ross": ["superman"],
    "lana_lang": ["superman"],
    "robin": ["batman"],
    "batgirl": ["batman"],
    "kid_flash": ["the_flash"],
    "jesse_quick": ["the_flash"],
    "ares": ["wonder_woman"],
    "maxima": ["superman"],
    "vandal_savage": ["kendra_saunders"],
    "ras_al-ghul": ["batman"],
    "mr_mxyzptlk": ["superman"],
    "bat-mite": ["batman"],
    "mary_batson": ["captain_marvel"],
    "freddie_freeman": ["captain_marvel"],
    "martin_stein": ["jefferson_jackson"],
    "jefferson_jackson": ["martin_stein"],
    "dr_caitlin_snow": ["the_flash"],
    "jimmy_olsen": ["superman"],

    # Category reveals - whole team, not one person
    "cat_grant": ["team:civilian"],
    "perry_white": ["team:civilian"],
    "james_gordon": ["team:villain"],
    "martian_manhunter": ["team:martian"],
    "krypto": ["team:hero"],
    "streaky": ["team:hero"],
    "dr_alchemy": ["team:bystander"],
    "dr_harleen_quinzel": ["team:villain"],
    "the_spectre": ["team:martian", "team:villain"],

    # Named-subset, symmetric pair (Luthor <-> his inner circle)
    "lex_luthor": ["miss_tessmacher", "otis", "mercy"],
    "lena_luthor": ["miss_tessmacher", "otis", "mercy"],
    "miss_tessmacher": ["lex_luthor", "lena_luthor"],
    "otis": ["lex_luthor", "lena_luthor"],
    "mercy": ["lex_luthor", "lena_luthor"],
}
for _c in CHARACTERS:
    _c["knows_identity_of"] = KNOWS_IDENTITY_OF.get(_c["id"])

HOSTAGE_ABILITIES = {
    "tobias_whale": "black_lightning",
    "reverse_flash": "the_flash",
    "zoom": "the_flash",
    "sinestro": "green_lantern",
    "black_adam": "captain_marvel",
    "faora": "kryptonian",
    "reign": "kryptonian",
    "harvey_dent": None,
}
for _c in CHARACTERS:
    _c["has_hostage"] = _c["id"] in HOSTAGE_ABILITIES
    _c["hostage_counterpart"] = HOSTAGE_ABILITIES.get(_c["id"])

# Well-known comics epithets, used as hover text on the host console.
# Deliberately not exhaustive - only characters with a genuinely
# recognizable nickname get one; everyone else falls back to the default
# "View character card" tooltip instead of a made-up epithet.
EPITHETS = {
    "superman": "The Man of Steel",
    "wonder_woman": "The Amazing Amazon",
    "batman": "The Dark Knight",
    "the_flash": "The Fastest Man Alive",
    "green_lantern": "The Emerald Knight",
    "captain_marvel": "The World's Mightiest Mortal",
    "martian_manhunter": "The Manhunter from Mars",
    "swamp_thing": "The Guardian of the Green",
    "zatanna": "Mistress of Magic",
    "hawkman": "The Winged Warrior",
    "the_spectre": "The Spirit of Vengeance",

    "lex_luthor": "The World's Greatest Criminal Mind",
    "reverse_flash": "Professor Zoom",
    "riddler": "Prince of Puzzles",
    "lobo": "The Main Man",
    "faora": "The Deadliest Woman on Any World",
    "grodd": "King of Gorilla City",
    "mr_mxyzptlk": "The Imp from the 5th Dimension",
    "dr_alchemy": "Master of the Philosopher's Stone",
    "ares": "The God of War",
    "maxima": "Empress of Almerac",
    "vandal_savage": "The Immortal",
    "ras_al-ghul": "The Demon's Head",
    "starro": "Starro the Conqueror",
    "granny_goodness": "Mistress of the Female Furies",
    "doomsday": "The Ultimate Killing Machine",
    "darkseid": "Lord of Apokolips",
    "brainiac": "The Collector of Worlds",
    "poison_ivy": "Queen of the Green",
    "reign": "The Worldkiller",
    "joker": "The Clown Prince of Crime",
    "sinestro": "The Yellow Lantern",
    "black_adam": "The Champion of Kandaq",

    "jimmy_olsen": "Superman's Pal",
    "james_gordon": "Commissioner Gordon",
    "a_pennyworth": "The Butler",
    "lana_lang": "The Girl Next Door",
    "dr_harleen_quinzel": "Harley Quinn",
    "dr_caitlin_snow": "Killer Frost",
    "harvey_dent": "Gotham's White Knight",
    "felicity_smoak": "Overwatch",
    "kendra_saunders": "Hawkgirl",
    "mary_batson": "Mary Marvel",
    "freddie_freeman": "Shazam Jr.",
    "martin_stein": "The Nuclear Man",
    "jefferson_jackson": "The Nuclear Man",
    "lois_lane": "Metropolis' Best Reporter",
    "perry_white": "Editor-in-Chief of the Daily Planet",

    "krypto": "The Superdog",
    "streaky": "The Supercat",
    "robin": "The Boy Wonder",
    "batgirl": "The Dominoed Daredoll",
    "kid_flash": "The Fastest Teen Alive",
    "supergirl": "The Girl of Steel",
    "superboy": "The Boy of Steel",
    "beast_boy": "The Changeling",
}
for _c in CHARACTERS:
    _c["epithet"] = EPITHETS.get(_c["id"])

CHARACTERS_BY_ID = {c["id"]: c for c in CHARACTERS}

# ------------------------------------------------------------------------
# Card packs. "basic" is always unlocked and free; every other pack starts
# locked, and the host toggles it on/off from the top of the host console.
# A character not listed in any pack below has no way to become available
# yet (see the note at the bottom of this file).
# ------------------------------------------------------------------------
PACKS = [
    {
        "id": "basic", "label": "Basic", "free": True,
        "characters": [
            "white_martian_i", "white_martian_ii", "superman", "wonder_woman",
            "lois_lane", "steve_trevor", "jimmy_olsen", "lex_luthor",
        ],
    },
    {
        "id": "hall_of_justice", "label": "Hall of Justice", "free": False,
        "characters": [
            "batman", "the_flash", "green_lantern", "captain_marvel",
            "black_lightning", "martian_manhunter", "plastic_man", "hawkman",
        ],
    },
    {
        "id": "super_friends", "label": "Super Friends", "free": False,
        "characters": [
            "cat_grant", "perry_white", "iris_west", "joe_west",
            "james_gordon", "maggie_sawyer", "a_pennyworth", "leslie_thompkins",
        ],
    },
    {
        "id": "smallville", "label": "Smallville", "free": False,
        "characters": [
            "lena_luthor", "miss_tessmacher", "otis", "mercy", "lana_lang",
            "pete_ross", "martha_kent", "jonathan_kent",
        ],
    },
    {
        "id": "hostage_situation", "label": "Hostage Situation", "free": False,
        "characters": [
            "bystander_1", "reverse_flash", "zoom", "faora", "cheetah",
            "tobias_whale", "joker", "black_adam",
        ],
    },
    {
        "id": "young_justice", "label": "Young Justice", "free": False,
        "characters": [
            "robin", "batgirl", "supergirl", "superboy", "krypto", "streaky",
            "thunder", "kid_flash", "jesse_quick", "miss_martian",
            "wonder_girl", "bumblebee", "beast_boy", "zatanna",
            "bystander_2", "bystander_3",
        ],
    },
    {
        "id": "interstellar_threats", "label": "Interstellar Threats", "free": False,
        "characters": [
            "lobo", "sinestro", "zod", "starro", "doomsday", "darkseid",
            "granny_goodness", "maalefak",
        ],
    },
    {
        "id": "power_struggle", "label": "Power Struggle", "free": False,
        "characters": [
            "ares", "vandal_savage", "maxima", "ras_al-ghul", "roulette",
            "riddler", "booster_gold",
        ],
    },
    {
        "id": "agents_of_chaos", "label": "Agents of Chaos", "free": False,
        "characters": [
            "bat-mite", "mr_mxyzptlk", "grodd", "parasite", "dr_alchemy",
            "bystander_4", "bystander_5", "bystander_6",
        ],
    },
    {
        "id": "civil_disobedience", "label": "Civil Disobedience", "free": False,
        "characters": [
            "harvey_dent", "dr_harleen_quinzel", "dr_caitlin_snow", "reign",
            "kendra_saunders", "mary_batson", "freddie_freeman",
            "martin_stein", "jefferson_jackson", "the_spectre",
        ],
    },
]

PACK_LABELS = {p["id"]: p["label"] for p in PACKS}

# Assign each character its pack id (None = not in any pack yet, so it
# can never become available until you add it to one).
for _p in PACKS:
    for _cid in _p["characters"]:
        if _cid in CHARACTERS_BY_ID:
            CHARACTERS_BY_ID[_cid]["pack"] = _p["id"]
for _c in CHARACTERS:
    _c.setdefault("pack", None)

# Characters that exist in the game but aren't in any pack from the set
# list you gave me: Vibe, Swamp Thing, Brainiac, Poison Ivy, Harrison Wells,
# and Felicity Smoak. They're kept in the roster with pack=None, which means
# they simply can't be unlocked yet - the host console will never show a way
# to turn them on. Add them to a pack's "characters" list above (or a new
# pack) whenever you decide where they belong.
UNPACKED_CHARACTER_IDS = [c["id"] for c in CHARACTERS if c["pack"] is None]

PHASES = ["Secret Identity", "Report", "Discuss", "Vote", "Accuse", "Rescue", "Eliminate", "Protect", "Inspect"]
NUM_ROUNDS = 7

# Short reference text for the player-facing "Rules & Phases" screen. These
# are inferred from context in the original file (nothing this explicit
# existed there) - treat as a starting draft and edit to match your actual
# rules exactly before relying on them at the table.
PHASE_INFO = {
    "Report": "The Watchtower reports what happened overnight to all players.",
    "Discuss": "Open discussion among all players. A 5-minute timer runs on the host's screen.",
    "Vote": "Players vote on who they suspect should be eliminated.",
    "Accuse": "A player may publicly accuse another player of being a Martian.",
    "Rescue": "A character with a rescue ability may act to save a targeted player.",
    "Eliminate": "The Martians secretly choose a player to eliminate.",
    "Protect": "Characters with a protective ability may shield another player.",
    "Inspect": "Characters with an investigative ability may learn information about another player.",
}

# The moderator's opening script, read aloud before the game starts.
INTRO_SCRIPT = (
    "White Martians have abducted civilians from Earth including Steve Trevor, "
    "Lois Lane, Jimmy Olsen, and even Lex Luthor! Now the Justice League must go "
    "undercover as civilian prisoners to rescue their friends and teleport them "
    "back to Justice League HQ aka the Watchtower.\n\n"
    "But it's a trap!\n\n"
    "The White Martians mind-wipe the Justice League as soon as they get inside "
    "the prison. Now the Justice League don't know their own teammates, or which "
    "civilians are their friends and which are White Martians in disguise. Oracle "
    "is waiting in the Watchtower for the all-clear to teleport civilians out of "
    "the White Martian prison, one by one. Will the Justice League rescue mission "
    "be a success, or will the White Martians infiltrate Watchtower and destroy "
    "the Earth?\n\n"
    "Good Luck, Heroes!"
)

# Narration scripts for the moderator - how to "talk like a Hero" and
# declare outcomes consistently at the table. [Bracketed] words are the
# moderator's fill-in-the-blank slots.
NARRATION_PROMPTS = [
    {
        "title": "Hero is eliminated",
        "text": "In the BLINK OF AN EYE [player] gets a bad headache\u2026and s/he/they drops dead.",
    },
    {
        "title": "Hero survives elimination",
        "text": "In the BLINK OF AN EYE [player] gets a bad headache\u2026but s/he's/they're fine.",
    },
    {
        "title": "Hero / Super Friend / Civilian is transported to Watchtower",
        "text": "[Player] dematerializes. Oracle tells you [role] is safe and sound.",
    },
    {
        "title": "White Martian / Villain is transported to Watchtower",
        "text": "[Player] dematerializes. Oracle tells you [role] is hacking Watchtower's security system.",
    },
    {
        "title": "Wonder Woman discovers White Martians",
        "text": "In the BLINK OF AN EYE a White Martian reverts to their true form then back to human.",
        "deprecated": True,
    },
    {
        "title": "Hero / Villain uses Super / Active Ability",
        "text": "I am [Hero/Villain]. I use [Super/Active Ability] to [action].",
    },
    {
        "title": "Black Lightning shocks a White Martian",
        "text": "In the BLINK OF AN EYE [player] feels a shock, reverts to their Martian form, then quickly back to human.",
    },
    {
        "title": "Start the Rescue Mission (Eyes Open)",
        "text": "This is Watchtower... Come in Justice League!",
    },
    {
        "title": "Transition into the night phases (Eyes Closed)",
        "text": "This is Watchtower... Teleporter engaged! Mind the flash.",
    },
    {
        "title": "After Inspect! phase resolves",
        "text": "There's a burst of light and then\u2026",
    },
]

# ------------------------------------------------------------------------
# DCEU Map — an 11 (rows, 0-10) x 7 (columns, ROYGBIV) coordinate board of
# named locations, recovered from the original file's 77 "zone_N" buttons.
# The same location name can appear in more than one cell; in the original,
# "blacking out" a location disables *every* cell with that name at once,
# not just the one clicked - preserved here.
# ------------------------------------------------------------------------
COLUMN_COLORS = [
    ("R", "#e5484d"), ("O", "#f5a623"), ("Y", "#f5d76e"), ("G", "#4fc97e"),
    ("B", "#4fc3f7"), ("I", "#8b7ff0"), ("V", "#c084e0"),
]

DCEU_GRID = [
    ["Gorilla City", "Kandor", "Earth-2", "MOGO", "Thanagar", "New Genesis", "The Green"],
    ["Kandor", "Gorilla City", "Qward", "Earth-2", "New Genesis", "The Green", "Thanagar"],
    ["Phantom Zone", "Arkham Asylum", "Speed Force", "Iron Heights", "Blackgate Penitentiary", "Lian Yu", "Apokolips"],
    ["Arkham Asylum", "Phantom Zone", "Iron Heights", "Speed Force", "Lian Yu", "Watchtower", "Phantom Zone"],
    ["Speed Force", "Arkham Asylum", "Phantom Zone", "Thanagar", "Apokolips", "Lian Yu", "Blackgate Penitentiary"],
    ["Apokolips", "Speed Force", "Arkham Asylum", "Martian Prison", "Iron Heights", "Blackgate Penitentiary", "Lian Yu"],
    ["Qward", "Happy Harbor", "Fortress of Solitude", "STAR Labs", "Batcave", "Fortress of Solitude", "Rock of Eternity"],
    ["Themyscira", "MOGO", "Happy Harbor", "Thanagar", "Rock of Eternity", "Batcave", "STAR Labs"],
    ["Fortress of Solitude", "Watchtower", "Qward", "Gorilla City", "STAR Labs", "Themyscira", "Batcave"],
    ["Rock of Eternity", "Fortress of Solitude", "Batcave", "Qward", "Happy Harbor", "STAR Labs", "Themyscira"],
    ["Batcave", "Rock of Eternity", "Fortress of Solitude", "Themyscira", "Qward", "Happy Harbor", "Thanagar"],
]

DCEU_LOCATIONS = sorted({name for row in DCEU_GRID for name in row})
