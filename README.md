# White Martian — Watchtower (Web Edition)

A live, multi-device rebuild of your Tkinter game tracker. One host device runs
the console; players follow along and vote from their phones on the same WiFi.

## Two ways to run this

1. **On your laptop** (what you've been doing) — free, no setup, but your
   laptop has to stay on and everyone needs the same WiFi.
2. **On the internet** (new, below) — free, works for players anywhere, but
   takes about 15 minutes of one-time setup and has a couple of trade-offs
   (see "Things to know" below).

## Running it on your laptop

```bash
pip install -r requirements.txt
python app.py
```

The terminal will print two links:
- **Host console** — open on the moderator's laptop: `http://localhost:5000/host`
- **Player view** — share the printed LAN address with players' phones, e.g.
  `http://192.168.1.23:5000/play` (must be on the same WiFi network)

## Hosting it on the internet (Render, free)

This lets players join from anywhere — different houses, different cities —
using a real web link instead of your laptop's WiFi address. No credit card
needed. Takes about 15 minutes the first time.

**1. Put the code on GitHub**
- Create a free account at [github.com](https://github.com) if you don't
  have one.
- Click **New repository**, name it something like `watchtower-web`, leave
  it Public, and click **Create repository**.
- On the next page, click **uploading an existing file**, then drag your
  entire unzipped `watchtower_web` folder into the browser window (modern
  browsers preserve the subfolders like `static/` and `templates/`
  automatically). Click **Commit changes**.

**2. Deploy it on Render**
- Create a free account at [render.com](https://render.com) (no card
  required) and connect your GitHub account when prompted.
- Click **New +** → **Blueprint**, and pick the repo you just created.
  Render will read the included `render.yaml` and configure everything
  automatically — you shouldn't need to type in any settings.
- Click **Apply** / **Create**. The first deploy takes a few minutes.

**3. Get your links**
- Once it says "Live," Render gives you a URL like
  `https://white-martian-watchtower.onrender.com`.
- Host console: that URL + `/host`
- Player link: that URL + `/play` — send this to players wherever they are.

### Things to know about the free hosted version

- **It falls asleep when nobody's used it for ~15 minutes**, and takes
  30-60 seconds to wake back up on the next visit. Fine between game
  sessions; just give it a minute if the first load feels stuck.
- **All game state lives in memory, not a database.** If the service
  restarts or spins down (long idle gap, a redeploy, etc.), the round,
  roster, and all assignments reset — same as restarting `python app.py`
  locally. Avoid multi-hour breaks mid-game on the hosted version.
- If you ever want zero sleep/reset risk, Render's paid tier (~$7/month)
  keeps it running continuously — same code, no changes needed, just
  switch the plan in Render's dashboard.

## How it's built (and why it's much smaller than 23,000 lines)

The original app hand-wrote a near-identical set of functions for every one of
its ~90 characters (activate, deactivate, rescue, etc. — 942 functions total).
That duplication was also the source of the bugs found during review (a shared
toggle counter across unrelated panels, menu items with no command attached).

This version stores the roster as **data**, not code:

- `characters.py` — one entry per character (name, team, action set). Adding a
  character means adding one line here — nothing else to touch.
- `app.py` — a small Flask + Socket.IO server. It holds the single shared game
  state in memory and pushes updates to every connected browser (host + all
  phones) instantly whenever anything changes — no refreshing, no polling.
- `templates/host.html` + `static/host.js` — the full moderator console: round
  and phase strip, roster grouped by team, per-character health/protection/
  action controls, live vote tally, activity feed.
- `templates/player.html` + `static/player.js` — the phone view: current
  round/phase, active roster, and a vote button during Vote/Accuse phases.

## Bugs from the original fixed by this rebuild

1. **Desyncing toggle panels** — `villny_open()`, `heroix_open()`, `civl_open()`,
   `skix_open()` all shared one global counter (`q`), so opening/closing one
   panel could throw off the others' show/hide state. The web version gives
   each team its own independent expand/collapse state.
2. **Dead menu items** — "Dr. Silvana" and "Mad Hatter" had no `command=`
   attached in the villain menu, so clicking them did nothing.
3. **The phase toggle system** used the same fragile shared-counter pattern
   (`a, b, c, d, y, z` globals). Replaced with a single `phase_index` value.
4. **Every shielded character shared one global counter.** Superman, Flash,
   Green Lantern, Captain Marvel, Zatanna, Plastic Man, Booster Gold, Krypto,
   Streaky, Supergirl, Superboy, Wonder Girl, Miss Martian, Freddie Freeman,
   and Kendra Saunders all read and wrote the same `protxn_show` variable —
   using one character's shield affected what every other shielded character
   displayed. Each now has an independent charge count.
5. **Two of those shield buttons had no `command=` at all** in the original
   — Green Lantern's and Captain Marvel's shields were built but never wired
   up. Both are live here now.
6. **The health up/down math didn't count evenly** — clicking "up" while
   below max snapped straight to a fixed value instead of incrementing by
   one, in every character's code. Per your direction, this is now a clean
   +1/-1 counter clamped 0-4.

## What's modeled now (recovered from deeper in the original file)

- **Numeric health, not alive/down**, clamped 0-4, with per-team starting
  values pulled from the original's constants: Heroes start at 2 (Superman
  starts at 3 — his "must be targeted thrice" passive), Civilians and
  Sidekicks start at 1, Villains start at 1. Martians and Bystanders have no
  health track, matching the original.
- **Independent shield charges** for 15 characters (Superman, The Flash,
  Green Lantern, Captain Marvel, Zatanna, Plastic Man, Booster Gold, Krypto,
  Streaky, Supergirl, Superboy, Wonder Girl, Miss Martian, Freddie Freeman,
  Kendra Saunders), plus a "Recharge all shields +1" button (replicates a
  function in the original that bumped every shield at once).
- **Cure ability** — Leslie Thompkins, Dr. Harleen Quinzel, Dr. Caitlin Snow.
- **Citizen's-arrest handcuffs** — James Gordon, Joe West, Maggie Sawyer.
- **Fix-it tech ability** — Harrison Wells, Felicity Smoak.
- **The DCEU location map** — an 11×7 coordinate grid (rows 0–10, columns
  R·O·Y·G·B·I·V) of 23 named DC locations (Batcave, Arkham Asylum,
  Watchtower, Gorilla City, etc.), opened from the host console's "Open map"
  button. Clicking any cell blacks out *every* cell sharing that location
  name at once, matching the original's behavior (several coordinates
  intentionally point to the same location).

## New: epithet hover text

Hovering a character's name now shows their well-known comics epithet
("Superman" → "The Man of Steel") instead of the generic "View character
card" tooltip — but only for the 56 characters that actually have a
widely-recognized nickname. Everyone else (Vibe, Booster Gold, most
civilians and sidekicks, several lesser-known villains) keeps the default
"View character card" tooltip rather than a made-up one. The full list of
who has what is in `EPITHETS` at the top of `characters.py` if you want to
add, remove, or correct any.

## New: small usability fixes

1. **Heart icon on health** — matches the existing shield icon pattern (❤️3).
2. **Hover tooltips** — every button on the host console shows a short
   definition on hover: End = "Eliminate character," Watchtower = "Mark
   rescued," Hive = "Starro minion," Teleport = "To Be Teleported," and
   Expose = "No More Secret ID."
3. **Roster is now vertically scrollable** with its own scrollbar, so a
   long unlocked roster doesn't push the rest of the page down.
4. **Vote phase has its own 2-minute timer**, same pattern as Discuss — a
   "Start 2-minute timer" button inside the Vote phase script popup.
5. **Locked characters are now fully hidden**, not just dimmed — if a
   pack isn't unlocked, its characters don't appear in the roster at all.
   Team headers still show a live count of only the currently-visible
   (unlocked) characters, and a team with nothing unlocked yet shows a
   small "no characters unlocked in this team yet" note instead of an
   empty box.

## New: Roster starts collapsed

The roster's team sections are now collapsed by default the moment the host
console loads, and collapse again automatically every time you click "New
game" — no more scrolling past an expanded roster you haven't set up yet.

## New: Phase scripts (what Watchtower says aloud)

Selecting any phase now pops up a card showing exactly what the moderator
should say, filled in live from the current game state:

- **Report** — Round 1 (or any round with no tracked history) shows the
  mission-briefing line, listing active Heroes, Civilians, and Villains by
  character name, plus the number of active Martians. From Round 2 on, it
  automatically switches to a **recap of the previous round** instead —
  who was safely beamed to Watchtower and who didn't survive — built from
  what you clicked during that round (see below).
- **Discuss** — the fixed line, plus a "Start 2-minute timer" button that
  launches the countdown for you. I changed the Discuss timer's default
  from 5 minutes to 2 to match this new script's exact wording — let me
  know if 5 was actually intentional and I'll change it back.
- **Vote** — updates live as votes come in: the nomination line always
  lists every currently-active player's real name, and the second line
  fills in the current leading vote-getter's real name once votes start
  arriving (before that, it says "waiting for votes").
- **Accuse** — the fixed line, no placeholders.
- **Rescue** — uses whoever won the Vote phase (captured the moment you
  leave Vote, so it's still correct even though votes clear right after).

A couple of interpretation calls I made, worth confirming:
- **Report's lists use character names** (Superman, Lois Lane, Joker), since
  that's Oracle's in-fiction, all-knowing narration. **Vote and Rescue use
  real player names**, since "raise your hand" and "keep still" are
  addressed to actual people at the table. If that split isn't what you
  meant, it's a quick change.
- **Vote's "nominated players"** currently means *everyone still active* —
  there's no separate nomination step in the app yet, so it lists the full
  pool of people still in play rather than a host-picked subset.

## New: round-outcome tracking for the Report recap

Yes, this was possible — here's how it works. Clicking a character's
**Watchtower** action marks them "rescued" for the round; clicking **End**
marks them "eliminated." When you advance the round counter, whatever got
tracked during the round you're leaving is archived, and the next Report
phase opened for a later round automatically pulls from it. No manual
data entry — just use the action buttons as you normally would during
Rescue/Eliminate phases, and the recap writes itself.

## New: Card Packs

A **Card Packs** bar now sits at the top of the host console, one chip per
pack from your set list. **Basic** is always on (green, non-clickable) since
those characters are free. Every other pack starts locked (dim) — click a
chip to unlock it live, and every character in that pack immediately becomes
selectable in the roster below, tagged with its pack name next to its name
(e.g. "Batman — Hall of Justice"). Click the chip again to lock the pack
back up — any of its characters that were active get automatically
deactivated and cleared so the roster never shows something from a
relocked pack.

Locked characters still show up in the roster (dimmed, with a 🔒), so you
can preview what a pack contains, but every control on that row — the
toggle, health, shields, actions — is inert until its pack is unlocked. You
can still click a locked character's name to preview their ability card.

A few notes on how your set list mapped onto the existing roster:

- **Renamed to match your set list** (the underlying data/cards are
  unaffected): Captain Marvel → **Shazam!**, Martha Kent → **Ma Kent**,
  Jonathan Kent → **Pa Kent**, A. Pennyworth → **Alfred Pennyworth**, Grodd →
  **Gorilla Grodd**.
- **Two brand-new characters added** since they weren't in the original
  game at all: **Ma'alefa'k** (Interstellar Threats) and **Reign** (Civil
  Disobedience). Both work fully in the tracker, but since they never
  existed in the original file, they have no ability card yet — "My Card"
  will show "no card on file" for them until you write one into `cards.json`.
- **Removed**: Dr. Silvana and Mad Hatter. Both were already dead menu items
  in the original file (no working command, no ability card) and weren't in
  any pack on your list, so I retired them rather than leave permanently
  unreachable characters in the roster.
- **Bystanders expanded from 3 to 6** to cover the counts your list needs
  across Hostage Situation (1), Young Justice (2), and Agents of Chaos (3).
- **Power Struggle** is listed as "7 cards (5-V, 1-H)" but the bullet list
  itself has 7 named characters (6 villains + 1 hero) plus a "???" — I built
  the 7 named ones (Ares, Vandal Savage, Maxima, Ra's Al-Ghul, Roulette,
  Riddler, Booster Gold) and left the "???" out since there's no way to build
  a real card from a placeholder. Worth double-checking this pack's exact
  intended contents against your records.
- **Six existing characters aren't in any pack on your list**: Vibe, Swamp
  Thing, Brainiac, Poison Ivy, Harrison Wells, and Felicity Smoak. They're
  still in the roster but permanently locked with no pack to unlock — there's
  no chip that turns them on. Let me know which pack they belong in (or if
  you want a new pack for them) and I'll wire it up.

## New: Kirby Krackle player cards

The player-facing cards — the shuffle reveal ("You are SUPERMAN!"), My Card,
and Rules & Phases — now have a hand-built Kirby Krackle border: clustered
black dot bursts at each corner plus a scattered dot texture along the
frame, over a bold cosmic purple-to-orange gradient reminiscent of Kirby's
Fourth World energy effects. Card titles, the reveal name, and the phase
banner use "Bangers," a bold comic-book display font, in place of the
sci-fi HUD font used on the host console, so the player experience reads
more like a Silver Age comic panel.

I couldn't open the private Google Drive reference image you linked (it
returned an access error), so this is built from the well-known Kirby
Krackle style rather than that specific reference — let me know if it needs
adjusting once you see it. This pass covers the player side only, per how
you'd scoped the work; happy to extend the same treatment further (e.g.
halftone textures, bolder host-side accents) if you want more of it.

## New: Prompts button (moderator narration)

A **Prompts** button in the host console's top bar opens a reference of
narration scripts — how to declare eliminations, transports, ability use,
etc. — pulled directly from what you gave me. One entry, "Wonder Woman
discovers White Martians," is shown struck through and tagged "no longer
used" since you'd crossed it out — I kept it visible rather than deleting
it in case you want it back, but it won't be mistaken for current.

There's also a placeholder **Intro Script** at the top of that same panel —
no intro text was included in what you sent, so it currently just says to
fill it in. Edit `INTRO_SCRIPT` in `characters.py` with your actual opening
narration whenever you're ready.

## New: Players panel, Start, and Shuffle

The host console now has a **Players** panel on the right, listing everyone
who's joined from `/play`, numbered in join order. Click a name to cross it
out (green = alive, red strikethrough = eliminated) — this is a manual
toggle for you to track eliminations, independent of any character's health.

- **Start** locks the roster: once clicked, players who join later won't be
  added to the list or be eligible for Shuffle (they can still connect, they
  just won't be dealt a character). Use this once everyone's phone is in.
- **Shuffle** (enabled after Start) randomly assigns one character to each
  locked-in player, using only the characters you've toggled *active* on
  the roster — so if you've turned on Superman, White Martian I, and Lois
  Lane, those are the only three that can be dealt out. If you have more
  players than active characters, it tells you instead of guessing.
- Reshuffling is allowed (e.g. before the game really starts) — each
  shuffle clears old assignments on active characters first, then deals
  fresh ones.

## New: what players see after Shuffle

- **A reveal card** pops up automatically on each player's phone: "You are
  SUPERMAN!" with a button to view their full card immediately.
- **My Card** — a toolbar button always available once a player's joined,
  showing *only their own* character's signal, role, abilities, and
  strategy (fetched privately from the server; nobody else's card is ever
  sent to them).
- **Rules & Phases** — a toolbar button with a plain-language reference for
  what each of the 8 phases means (these are inferred placeholder
  descriptions — worth editing `PHASE_INFO` in `characters.py` to match your
  actual rules exactly).
- **Phase reminders** — when the host advances to a phase, any player whose
  character has an ability tagged for that phase (parsed from the original
  ability text's own "(Protect!)", "(Accuse!)", etc. tags) gets a private
  toast reminding them of it. Superman gets nudged during Accuse and
  Protect; a civilian with no phase-tagged ability gets nothing.

## New: player identity privacy

Player phones no longer receive the name-to-character mapping at all — the
server strips it out of everything sent to `/play` clients. Each player only
learns their own assignment: after typing their name on the name-gate
screen, they get a private "You are Superman"-style banner that nobody
else's phone (or network traffic) can see. The host console still sees the
full mapping for every character, since that's needed to run the game.

## New: Discuss! timer

Clicking the **Discuss** phase LED on the host console now pops up a 5-minute
countdown timer automatically. It has Pause/Resume, Reset (back to 5:00),
and +1 minute controls, plays a short beep when it hits zero, and can be
reopened any time from the "Discuss! timer" button in the side panel if you
close it early. This lives only on the host's screen — it's not synced to
player phones.

## Scope note

The ability rulebook text embedded in the original is now surfaced in the
app after all — **click any character's name** on the host console to open
their card: signal, role, passive/active/super abilities, and strategy tip,
pulled straight from the original file's 85 character-stats popups.

Three things worth knowing about that data:
- **Martians, Bystanders, Dr. Silvana, and Mad Hatter have no card** — the
  first two never had one in the original, and the latter two were the dead
  menu items from bug #2 above, so no ability text exists for them anywhere
  in the source file. Their card just says "no card on file."
- **Joker's third ability is a literal placeholder** in the original file —
  its text is `"SUPER ABILITY. Name. Description. (Phase!)"`, word for word.
  Looks like it was never finished. I didn't invent content to fill it in;
  worth writing a real one in for your next game.
- A few character names in the card popups differ slightly from the roster
  list (e.g. Krypto's card title is "Krypto the Superdog," Zod's is "General
  Zod"), matched up correctly behind the scenes either way.

## New capability (not in the original — flagging since it's a scope change)

The original was moderator-only with no player input at all. Since you asked
for players to "view/vote from their phones," I added a lightweight vote
button during the **Vote** and **Accuse** phases, tallied live on the host
screen. If that's not what you had in mind — e.g. you just want players
watching a read-only board while voting happens out loud — say the word and
I'll strip it down to view-only.

## Extending it

- To add/rename a character: edit `characters.py`.
- To change rounds/phases: edit `PHASES` / `NUM_ROUNDS` at the bottom of
  `characters.py`.
- To add a new per-character action button: add its name to
  `STANDARD_ACTIONS` in `characters.py` — it appears on every character
  automatically.
