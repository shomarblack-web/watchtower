const socket = io();

// ---- animated overlay show/hide (fade + scale, see .map-overlay CSS) ----
function showOverlay(id) {
  document.getElementById(id).classList.add("overlay-open");
}
function hideOverlay(id) {
  document.getElementById(id).classList.remove("overlay-open");
}

let latestState = null;
const collapsedTeams = new Set(["martian", "hero", "villain", "civilian", "sidekick", "bystander"]);
const PACK_LABELS = Object.fromEntries(PACKS.map(p => [p.id, p.label]));

// One to three word hover definitions shown on every host-console button.
const TOOLTIPS = {
  charToggle: "Add to game",
  nameLink: "View character card",
  nameInput: "Assign player name",
  healthMinus: "Decrease health",
  healthPlus: "Increase health",
  shieldMinus: "Decrease shield",
  shieldPlus: "Increase shield",
  protDot: "Protection charge",
  cuffed: "Toggle handcuffs",
  cured: "Toggle cure ability",
  fixed: "Toggle repair ability",
  action_end: "Eliminate character",
  action_hive: "Starro minion",
  action_teleport: "To Be Teleported",
  action_watchtower: "Mark rescued",
  action_expose: "No More Secret ID",
  action_deactivate: "Remove from board",
  roundLed: "Set current round",
  packChip: "Toggle card pack",
  packChipFree: "Always available",
  startBtn: "Lock the roster",
  shuffleBtn: "Randomly assign characters",
  clearVotes: "Reset vote tally",
  openMap: "Open location map",
  rechargeShields: "Refill all shields",
  discussTimerBtn: "Start discussion timer",
  newGame: "Reset entire game",
  promptsBtn: "Narration scripts",
  playerRow: "Toggle eliminated",
  mapCell: "Toggle location blackout",
};

// ---- build the card pack toggle row ----
const packRow = document.getElementById("pack-row");
PACKS.forEach(p => {
  const el = document.createElement("div");
  el.className = "pack-chip" + (p.free ? " pack-free" : "");
  el.dataset.pack = p.id;
  el.innerHTML = `<span class="pack-chip-name">${p.label}</span><span class="pack-chip-count">${p.characters.length}</span>`;
  if (p.free) {
    el.title = TOOLTIPS.packChipFree;
  } else {
    el.title = TOOLTIPS.packChip;
    el.onclick = () => socket.emit("toggle_pack", { pack_id: p.id });
  }
  packRow.appendChild(el);
});

socket.on("connect", () => {
  document.getElementById("conn-status").textContent = "● live";
  socket.emit("register_host");
});

socket.on("game_reset", () => {
  collapsedTeams.clear();
  ["martian", "hero", "villain", "civilian", "sidekick", "bystander"].forEach(t => collapsedTeams.add(t));
  buildRoster();
});

socket.on("shuffle_error", (data) => {
  const el = document.getElementById("shuffle-error");
  el.textContent = data.message;
  el.style.display = "block";
  setTimeout(() => { el.style.display = "none"; }, 5000);
});

socket.on("character_limit_error", (data) => {
  const el = document.getElementById("host-toast");
  el.textContent = data.message;
  el.style.display = "block";
  setTimeout(() => { el.style.display = "none"; }, 6000);
});

socket.on("game_over", (data) => {
  document.getElementById("gameover-banner-title").textContent = data.title;
  document.getElementById("gameover-banner-message").textContent = data.message;
  document.getElementById("gameover-banner").style.display = "flex";
});

function renderPlayersPanel(state) {
  const listEl = document.getElementById("players-list");
  const startBtn = document.getElementById("start-btn");
  const shuffleBtn = document.getElementById("shuffle-btn");
  const players = state.players || [];

  if (!players.length) {
    listEl.innerHTML = `<div class="empty">Waiting for players to join…</div>`;
  } else {
    listEl.innerHTML = players.map((p, i) => `
      <div class="player-row ${p.eliminated ? "eliminated" : ""}" title="${TOOLTIPS.playerRow}" data-idx="${i}">
        <span class="player-num">${i + 1}.</span>
        <span class="player-name">${p.name}</span>
      </div>
    `).join("");
    listEl.querySelectorAll(".player-row").forEach((el, i) => {
      el.addEventListener("click", () => socket.emit("toggle_player_eliminated", { name: players[i].name }));
    });
  }

  if (state.roster_locked) {
    startBtn.textContent = "Roster Locked";
    startBtn.disabled = true;
    shuffleBtn.disabled = false;
  } else {
    startBtn.textContent = "Start";
    startBtn.disabled = false;
    shuffleBtn.disabled = true;
  }
}

function doShuffle() {
  socket.emit("shuffle_characters");
}

// ---- New Game player management ----
function openNewGameModal() {
  renderNewGamePlayerList(latestState);
  showOverlay("newgame-overlay");
}
function closeNewGameModal() {
  hideOverlay("newgame-overlay");
}
function renderNewGamePlayerList(state) {
  const listEl = document.getElementById("newgame-player-list");
  if (!listEl) return;
  const players = (state && state.players) || [];
  listEl.innerHTML = players.length
    ? players.map(p => `
        <div class="newgame-player-row">
          <span>${p.name}</span>
          <button class="newgame-remove-btn" title="Remove player">✕</button>
        </div>
      `).join("")
    : `<div class="empty">No players in the roster.</div>`;
  listEl.querySelectorAll(".newgame-remove-btn").forEach((el, i) => {
    el.addEventListener("click", () => socket.emit("remove_player", { name: players[i].name }));
  });
}
function addPlayerFromModal() {
  const input = document.getElementById("newgame-add-input");
  const name = input.value.trim();
  if (!name) return;
  socket.emit("add_player", { name });
  input.value = "";
}
function confirmStartNewGame() {
  if (!confirm("Start a brand new game? This resets the round, board, and packs.")) return;
  socket.emit("new_game");
  closeNewGameModal();
}

socket.on("disconnect", () => { document.getElementById("conn-status").textContent = "○ disconnected"; });

// ---- build the round / phase status strips once ----
const roundStrip = document.getElementById("round-strip");
for (let i = 1; i <= NUM_ROUNDS; i++) {
  const el = document.createElement("div");
  el.className = "led";
  el.textContent = i;
  el.title = TOOLTIPS.roundLed;
  el.onclick = () => socket.emit("set_round", { round: i });
  el.dataset.round = i;
  roundStrip.appendChild(el);
}

const phaseStrip = document.getElementById("phase-strip");
PHASES.forEach((p, idx) => {
  const el = document.createElement("div");
  el.className = "led led-phase";
  el.textContent = p;
  el.title = `Select ${p} phase`;
  el.onclick = () => {
    const turningOff = latestState && latestState.phase_index === idx;
    socket.emit("set_phase", { phase_index: turningOff ? null : idx });
  };
  el.dataset.phase = idx;
  phaseStrip.appendChild(el);
});

// ---- build roster once (grouped by team), then patch state on updates ----
const TEAMS = ["martian", "hero", "villain", "civilian", "sidekick", "bystander"];
const rosterEl = document.getElementById("roster");

function buildRoster() {
  rosterEl.innerHTML = "";
  TEAMS.forEach(team => {
    const members = CHARACTERS.filter(c => c.team === team);
    if (!members.length) return;

    const wrap = document.createElement("div");
    wrap.className = "team";
    wrap.dataset.team = team;

    const head = document.createElement("div");
    head.className = "team-head";
    head.innerHTML = `<span class="team-dot team-dot-${team}"></span>
                       <span class="team-name">${TEAM_LABELS[team]}</span>
                       <span class="team-count">${members.length}</span>`;
    head.onclick = () => {
      collapsedTeams.has(team) ? collapsedTeams.delete(team) : collapsedTeams.add(team);
      body.classList.toggle("collapsed");
    };
    wrap.appendChild(head);

    const body = document.createElement("div");
    body.className = "team-body" + (collapsedTeams.has(team) ? " collapsed" : "");
    members.forEach(c => body.appendChild(buildCharRow(c)));
    wrap.appendChild(body);

    rosterEl.appendChild(wrap);
  });
}

function buildCharRow(c) {
  const row = document.createElement("div");
  row.className = "char-row";
  row.id = `row-${c.id}`;
  row.dataset.pack = c.pack || "";

  const toggle = document.createElement("div");
  toggle.className = "char-toggle";
  toggle.title = TOOLTIPS.charToggle;
  toggle.onclick = () => { if (row.classList.contains("locked")) return; socket.emit("toggle_character", { id: c.id }); };

  const packLabel = c.pack ? PACK_LABELS[c.pack] : "Unassigned";
  const nameWrap = document.createElement("div");
  nameWrap.className = "char-name";
  const nameTitle = c.epithet || TOOLTIPS.nameLink;
  const secretBadge = c.is_switchable ? `<span class="secret-badge" title="Host-only: true identity">🎭 ${c.reveal_name}</span>` : "";
  nameWrap.innerHTML = `<span class="char-name-link" data-id="${c.id}" title="${nameTitle}">${c.name}</span>${secretBadge}
                         <span class="char-pack-label">${packLabel}</span>
                         <br><input type="text" placeholder="player name" data-id="${c.id}" title="${TOOLTIPS.nameInput}">`;
  nameWrap.querySelector(".char-name-link").addEventListener("click", () => openCard(c.id));
  nameWrap.querySelector("input").addEventListener("change", e => {
    socket.emit("set_player_name", { id: c.id, name: e.target.value });
  });

  const mid = document.createElement("div");
  mid.style.display = "flex";
  mid.style.flexDirection = "column";
  mid.style.gap = "4px";
  mid.style.alignItems = "center";

  if (c.has_health) {
    const health = document.createElement("div");
    health.className = "stepper";
    health.innerHTML = `<button class="step-btn" data-d="-1" title="${TOOLTIPS.healthMinus}">–</button>
                         <span class="step-val" data-role="health">–❤️</span>
                         <button class="step-btn" data-d="1" title="${TOOLTIPS.healthPlus}">+</button>`;
    health.querySelectorAll(".step-btn").forEach(b => {
      b.onclick = () => socket.emit("adjust_health", { id: c.id, delta: Number(b.dataset.d) });
    });
    mid.appendChild(health);
  }

  if (c.has_shield) {
    const shield = document.createElement("div");
    shield.className = "stepper stepper-shield";
    shield.innerHTML = `<button class="step-btn" data-d="-1" title="${TOOLTIPS.shieldMinus}">–</button>
                         <span class="step-val" data-role="shield">–🛡</span>
                         <button class="step-btn" data-d="1" title="${TOOLTIPS.shieldPlus}">+</button>`;
    shield.querySelectorAll(".step-btn").forEach(b => {
      b.onclick = () => socket.emit("adjust_shield", { id: c.id, delta: Number(b.dataset.d) });
    });
    mid.appendChild(shield);
  }

  [["has_cuffs", "cuffed", "Cuffs"], ["has_cure", "cured", "Cure"], ["has_fixit", "fixed", "Fix-it"]].forEach(([flag, field, label]) => {
    if (c[flag]) {
      const btn = document.createElement("button");
      btn.className = "action-btn special-btn";
      btn.dataset.field = field;
      btn.textContent = label;
      btn.title = TOOLTIPS[field];
      btn.onclick = () => socket.emit("toggle_special", { id: c.id, field });
      mid.appendChild(btn);
    }
  });

  if (c.is_switchable) {
    const revealBtn = document.createElement("button");
    revealBtn.className = "action-btn reveal-btn";
    revealBtn.title = `Reveal as ${c.reveal_name}`;
    revealBtn.textContent = "Reveal";
    revealBtn.onclick = () => socket.emit("reveal_character", { id: c.id });
    mid.appendChild(revealBtn);
  }

  if (c.has_hostage) {
    const hostageBtn = document.createElement("button");
    hostageBtn.className = "action-btn hostage-btn";
    hostageBtn.title = "Let Fate Decide - take two players hostage";
    hostageBtn.textContent = "Take Hostage";
    hostageBtn.onclick = () => openHostageModal(c.id);
    mid.appendChild(hostageBtn);
  }

  const right = document.createElement("div");
  right.style.display = "flex";
  right.style.flexDirection = "column";
  right.style.gap = "4px";
  right.style.alignItems = "flex-end";

  const protWrap = document.createElement("div");
  protWrap.className = "prot-wrap";
  const protLabel = document.createElement("div");
  protLabel.className = "prot-label";
  protLabel.textContent = "Protection";
  const prot = document.createElement("div");
  prot.className = "prot-row";
  for (let i = 0; i < 3; i++) {
    const dot = document.createElement("div");
    dot.className = "prot-dot";
    dot.dataset.slot = i;
    dot.title = TOOLTIPS.protDot;
    dot.onclick = () => socket.emit("toggle_protection", { id: c.id, slot: i });
    prot.appendChild(dot);
  }
  protWrap.appendChild(protLabel);
  protWrap.appendChild(prot);

  const actions = document.createElement("div");
  actions.className = "action-row";
  const ACTION_LABELS = { end: "ELM" };
  c.actions.forEach(a => {
    const btn = document.createElement("button");
    btn.className = "action-btn" + (a === "deactivate" ? " deactivate" : "");
    btn.dataset.action = a;
    btn.textContent = ACTION_LABELS[a] || a;
    btn.title = TOOLTIPS["action_" + a] || a;
    btn.onclick = () => socket.emit("character_action", { id: c.id, action: a });
    actions.appendChild(btn);
  });

  right.appendChild(protWrap);
  right.appendChild(actions);

  row.appendChild(toggle);
  row.appendChild(nameWrap);
  row.appendChild(mid);
  row.appendChild(right);
  return row;
}

buildRoster();

// ---- apply live state from server ----
let lastSeenPhaseIndex = undefined;

socket.on("state", (state) => {
  latestState = state;

  document.querySelectorAll("#round-strip .led").forEach(el => {
    el.classList.toggle("on", Number(el.dataset.round) === state.round);
  });
  document.querySelectorAll("#phase-strip .led").forEach(el => {
    el.classList.toggle("on", Number(el.dataset.phase) === state.phase_index);
  });

  if (state.phase_index !== lastSeenPhaseIndex) {
    if (state.phase_index !== null && state.phase_script) {
      openPhaseScript(state.phase_script);
    }
    lastSeenPhaseIndex = state.phase_index;
  } else if (state.phase_script) {
    renderPhaseScriptBody(state.phase_script);
  }

  const unlockedSet = new Set(state.unlocked_packs || []);
  document.querySelectorAll(".pack-chip").forEach(el => {
    el.classList.toggle("unlocked", unlockedSet.has(el.dataset.pack) || el.classList.contains("pack-free"));
  });

  renderPlayersPanel(state);
  renderHostageBanner(state);
  if (state.game_over) {
    document.getElementById("gameover-banner-title").textContent = state.game_over.title;
    document.getElementById("gameover-banner-message").textContent = state.game_over.message;
    document.getElementById("gameover-banner").style.display = "flex";
  }
  renderNewGamePlayerList(state);

  const spotlightSet = new Set(state.spotlight_characters || []);
  const superActiveSet = new Set(state.super_active_characters || []);
  const draftSet = new Set(state.draft_characters || []);

  CHARACTERS.forEach(c => {
    const st = state.characters[c.id];
    const row = document.getElementById(`row-${c.id}`);
    if (!row || !st) return;
    const locked = !c.pack || !unlockedSet.has(c.pack);
    row.classList.toggle("row-hidden", locked);
    row.classList.toggle("active", st.active && !locked);
    row.classList.toggle("spotlight", spotlightSet.has(c.id) && !locked);
    const nameInput = row.querySelector("input");
    if (document.activeElement !== nameInput) nameInput.value = st.player_name || "";

    const nameLink = row.querySelector(".char-name-link");
    if (nameLink && st.display_name) nameLink.textContent = st.display_name;

    let draftBadge = row.querySelector(".draft-badge");
    if (draftSet.has(c.id) && !locked) {
      if (!draftBadge) {
        draftBadge = document.createElement("span");
        draftBadge.className = "draft-badge";
        draftBadge.title = "This card still has unfinished placeholder text from the original file";
        draftBadge.textContent = "📝 Draft";
        nameLink.insertAdjacentElement("afterend", draftBadge);
      }
    } else if (draftBadge) {
      draftBadge.remove();
    }

    let superBadge = row.querySelector(".super-badge");
    if (superActiveSet.has(c.id) && !locked) {
      if (!superBadge) {
        superBadge = document.createElement("span");
        superBadge.className = "super-badge";
        superBadge.title = "Super Ability is active (Round 3+)";
        superBadge.textContent = "⭐ Super Active";
        nameLink.insertAdjacentElement("afterend", superBadge);
      }
    } else if (superBadge) {
      superBadge.remove();
    }

    const revealBtn = row.querySelector(".reveal-btn");
    if (revealBtn) {
      revealBtn.classList.toggle("sel", !!st.revealed);
      revealBtn.textContent = st.revealed ? "Revealed" : "Reveal";
    }

    const hostageBtn = row.querySelector(".hostage-btn");
    if (hostageBtn) {
      const needsReveal = c.is_switchable && !st.revealed;
      hostageBtn.disabled = needsReveal;
      hostageBtn.title = needsReveal
        ? "Reveal this character first"
        : "Take a player hostage";
    }

    let hostageBadge = row.querySelector(".hostage-badge");
    if (st.hostage) {
      if (!hostageBadge) {
        hostageBadge = document.createElement("span");
        hostageBadge.className = "hostage-badge";
        hostageBadge.title = "Click to release";
        hostageBadge.textContent = "🔗 Hostage";
        hostageBadge.onclick = () => socket.emit("release_hostage", { id: c.id });
        row.querySelector(".char-name").appendChild(hostageBadge);
      }
    } else if (hostageBadge) {
      hostageBadge.remove();
    }

    const CONDITION_BADGES = {
      exposed: ["👁️ Exposed", "condition-exposed"],
      eliminated: ["☠️ Eliminated", "condition-eliminated"],
      rescued: ["🏠 Rescued", "condition-rescued"],
      targeted: ["🎯 Targeted", "condition-targeted"],
    };
    Object.entries(CONDITION_BADGES).forEach(([flag, [label, cls]]) => {
      let badge = row.querySelector(`.condition-badge.${cls}`);
      if (st[flag]) {
        if (!badge) {
          badge = document.createElement("span");
          badge.className = `condition-badge ${cls}`;
          badge.title = "Click the action button again to clear";
          badge.textContent = label;
          row.querySelector(".char-name").appendChild(badge);
        }
      } else if (badge) {
        badge.remove();
      }
    });

    const healthVal = row.querySelector('[data-role="health"]');
    if (healthVal) {
      healthVal.textContent = `${st.health}❤️`;
      healthVal.classList.toggle("zero", st.health === 0);
      healthVal.classList.toggle("max", st.health >= 4);
    }
    const shieldVal = row.querySelector('[data-role="shield"]');
    if (shieldVal) {
      const shieldStepper = shieldVal.closest(".stepper");
      if (st.shield === null || st.shield === undefined) {
        shieldVal.textContent = "🔒🛡";
        shieldVal.classList.remove("zero");
        if (shieldStepper) shieldStepper.classList.add("shield-locked");
      } else {
        shieldVal.textContent = `${st.shield}🛡`;
        shieldVal.classList.toggle("zero", st.shield === 0);
        if (shieldStepper) shieldStepper.classList.remove("shield-locked");
      }
    }
    row.querySelectorAll(".special-btn").forEach(b => {
      b.classList.toggle("sel", !!st[b.dataset.field]);
    });

    row.querySelectorAll(".prot-dot").forEach(d => d.classList.toggle("on", st.protection[d.dataset.slot]));
    row.querySelectorAll(".action-btn:not(.special-btn)").forEach(b => b.classList.toggle("sel", st.last_action === b.dataset.action));
  });

  TEAMS.forEach(team => {
    const wrap = document.querySelector(`.team[data-team="${team}"]`);
    if (!wrap) return;
    const body = wrap.querySelector(".team-body");
    const visibleRows = Array.from(body.querySelectorAll(".char-row")).filter(r => !r.classList.contains("row-hidden"));
    wrap.querySelector(".team-count").textContent = visibleRows.length;
    let placeholder = body.querySelector(".team-empty-note");
    if (visibleRows.length === 0) {
      if (!placeholder) {
        placeholder = document.createElement("div");
        placeholder.className = "empty team-empty-note";
        placeholder.textContent = "No characters unlocked in this team yet.";
        body.appendChild(placeholder);
      }
      placeholder.style.display = "";
    } else if (placeholder) {
      placeholder.style.display = "none";
    }
  });

  const tallyEl = document.getElementById("tally");
  const voteCountEl = document.getElementById("vote-count");
  voteCountEl.textContent = state.vote_count ? `(${state.vote_count} cast)` : "";
  if (!state.tally.length) {
    tallyEl.innerHTML = `<div class="empty">No votes cast this phase.</div>`;
  } else {
    tallyEl.innerHTML = state.tally.map(([name, count]) => {
      return `<div class="tally-row"><span>${name}</span><b>${count}</b></div>`;
    }).join("");
  }

  const activityEl = document.getElementById("activity");
  activityEl.innerHTML = state.activity.length
    ? state.activity.map(a => `<div class="feed-item">${a}</div>`).join("")
    : `<div class="empty">Nothing yet.</div>`;

  applyMapState(state.map);
});

// ---- DCEU map ----
function toggleMap() {
  const overlay = document.getElementById("map-overlay");
  const showing = !overlay.classList.contains("overlay-open");
  if (showing) showOverlay("map-overlay"); else hideOverlay("map-overlay");
  document.getElementById("map-toggle-btn").textContent = showing ? "Close map" : "Open map";
}

function buildMap() {
  const grid = document.getElementById("map-grid");
  const table = document.createElement("div");
  table.className = "map-table";

  // header row: blank corner + 7 color headers
  const headRow = document.createElement("div");
  headRow.className = "map-row";
  headRow.appendChild(mapCell("", "map-corner"));
  COLUMN_COLORS.forEach(([letter, hex]) => {
    const el = mapCell(letter, "map-colhead");
    el.style.background = hex;
    headRow.appendChild(el);
  });
  table.appendChild(headRow);

  DCEU_GRID.forEach((row, rIdx) => {
    const rowEl = document.createElement("div");
    rowEl.className = "map-row";
    rowEl.appendChild(mapCell(String(rIdx), "map-rowhead"));
    row.forEach(locName => {
      const cell = mapCell(locName, "map-cell");
      cell.dataset.loc = locName;
      cell.title = TOOLTIPS.mapCell;
      cell.onclick = () => socket.emit("toggle_location", { name: locName });
      rowEl.appendChild(cell);
    });
    table.appendChild(rowEl);
  });

  grid.innerHTML = "";
  grid.appendChild(table);
}

function mapCell(text, cls) {
  const el = document.createElement("div");
  el.className = cls;
  el.textContent = text;
  return el;
}

function applyMapState(mapState) {
  if (!mapState) return;
  document.querySelectorAll(".map-cell").forEach(cell => {
    cell.classList.toggle("blackout", !!mapState[cell.dataset.loc]);
  });
}

buildMap();

// ---- character ability card ----
function openCard(id) {
  const c = CHARACTERS.find(x => x.id === id);
  const card = CARDS[id];
  document.getElementById("card-name").textContent = c ? c.name : id;

  const body = document.getElementById("card-body");
  if (!card) {
    body.innerHTML = `<div class="empty">No card on file for this character.</div>`;
  } else {
    const abilityRows = (card.abilities || []).map(a => {
      const m = a.match(/^([A-Z ]+ABILITY)\.\s*([^.]*)\.\s*(.*)$/);
      if (m) {
        const [, kind, title, desc] = m;
        return `<div class="ability-row">
                  <div class="ability-kind">${kind}</div>
                  <div class="ability-title">${title}</div>
                  <div class="ability-desc">${desc}</div>
                </div>`;
      }
      return `<div class="ability-row"><div class="ability-desc">${a}</div></div>`;
    }).join("");

    body.innerHTML = `
      ${card.role ? `<div class="card-meta">${card.role}</div>` : ""}
      ${card.signal ? `<div class="card-meta">${card.signal}</div>` : ""}
      <div class="ability-list">${abilityRows || '<div class="empty">No abilities on file.</div>'}</div>
      ${card.strategy ? `<div class="card-strategy">${card.strategy}</div>` : ""}
    `;
  }

  showOverlay("card-overlay");
}

function closeCard() {
  hideOverlay("card-overlay");
}

// ---- hostage modal ----
let hostageHolderId = null;
let hostageSelected = [];
let hostageMaxTargets = 1;

function openHostageModal(holderId) {
  hostageHolderId = holderId;
  hostageSelected = [];
  const c = CHARACTERS.find(x => x.id === holderId);
  const counterpart = c ? c.hostage_counterpart : null;
  hostageMaxTargets = counterpart === null || counterpart === undefined ? 2 : 1;

  const title = document.getElementById("hostage-modal-title");
  const desc = document.getElementById("hostage-modal-desc");
  const coinRow = document.getElementById("hostage-coin-row");
  const confirmBtn = document.getElementById("hostage-confirm-btn");
  document.getElementById("coin-result").textContent = "";

  if (hostageMaxTargets === 2) {
    title.textContent = `${c.name} — Let Fate Decide`;
    desc.textContent = "Pick exactly two active characters to take hostage. Flip the coin to let fate decide their outcome, then use the normal action buttons on their rows however that plays out.";
    coinRow.style.display = "block";
    confirmBtn.textContent = "Take Hostage";
  } else {
    const counterpartLabel = counterpart === "kryptonian" ? "Any active Kryptonian hero" : (CHARACTERS.find(x => x.id === counterpart) || {}).name || counterpart;
    title.textContent = `${c.name} — Take Hostage`;
    desc.textContent = `Pick one active character to take hostage. ${counterpartLabel} will have 10 real-world seconds to reveal their identity, or the hostage loses 1 health.`;
    coinRow.style.display = "none";
    confirmBtn.textContent = "Take Hostage (10s)";
  }

  renderHostageTargets();
  showOverlay("hostage-overlay");
}

function closeHostageModal() {
  hideOverlay("hostage-overlay");
}

function flipCoin() {
  const result = Math.random() < 0.5 ? "HEADS" : "TAILS";
  const el = document.getElementById("coin-result");
  el.textContent = result;
  el.style.color = result === "HEADS" ? "var(--amber)" : "var(--hero)";
}

function renderHostageTargets() {
  const list = document.getElementById("hostage-target-list");
  if (!latestState) { list.innerHTML = ""; return; }
  const candidates = CHARACTERS.filter(c => {
    const st = latestState.characters[c.id];
    return st && st.active && c.id !== hostageHolderId;
  });
  if (!candidates.length) {
    list.innerHTML = `<div class="empty">No other active characters to target.</div>`;
    return;
  }
  list.innerHTML = candidates.map(c => {
    const st = latestState.characters[c.id];
    const picked = hostageSelected.includes(c.id);
    return `<div class="hostage-target ${picked ? 'picked' : ''}" data-id="${c.id}">
              <span class="team-dot" style="background:${TEAM_COLORS[c.team]}"></span>${st.display_name || c.name}
            </div>`;
  }).join("");
  list.querySelectorAll(".hostage-target").forEach(el => {
    el.addEventListener("click", () => toggleHostageTarget(el.dataset.id));
  });
}

function toggleHostageTarget(cid) {
  if (hostageSelected.includes(cid)) {
    hostageSelected = hostageSelected.filter(x => x !== cid);
  } else if (hostageSelected.length < hostageMaxTargets) {
    hostageSelected.push(cid);
  }
  renderHostageTargets();
}

function confirmHostage() {
  if (hostageSelected.length !== hostageMaxTargets) {
    alert(`Pick exactly ${hostageMaxTargets} character${hostageMaxTargets > 1 ? "s" : ""} first.`);
    return;
  }
  socket.emit("take_hostage", { holder_id: hostageHolderId, target_ids: hostageSelected });
  closeHostageModal();
  if (hostageMaxTargets === 2) {
    openTimer(10, "Let Fate Decide!");
  } else {
    openTimer(10, "Reveal or lose 1 HP!");
  }
}

// ---- hostage resolution banner (named/category counterpart) ----
function renderHostageBanner(state) {
  const banner = document.getElementById("hostage-banner");
  const event = state.hostage_event;
  if (!event) {
    banner.style.display = "none";
    return;
  }
  const hostageName = (state.characters[event.hostage_id] || {}).display_name || event.hostage_id;
  document.getElementById("hostage-banner-text").innerHTML =
    `<b>${event.counterpart_label}</b> has 10 seconds to reveal, or <b>${hostageName}</b> loses 1 HP.`;
  banner.style.display = "flex";
}

function resolveHostageRelease() {
  if (!latestState || !latestState.hostage_event) return;
  socket.emit("release_hostage", { id: latestState.hostage_event.hostage_id });
}

function resolveHostageConsequence() {
  if (!latestState || !latestState.hostage_event) return;
  socket.emit("hostage_consequence", { id: latestState.hostage_event.hostage_id });
}

// ---- Discuss! countdown timer ----
let timerSeconds = 2 * 60;
let timerRunning = false;
let timerHandle = null;
let timerBeeped = false;
let timerLabel = "Discuss!";

function openTimer(startSeconds, label) {
  timerSeconds = startSeconds;
  timerBeeped = false;
  timerLabel = label || "Discuss!";
  document.getElementById("timer-title").textContent = timerLabel;
  renderTimer();
  showOverlay("timer-overlay");
  startTimerInterval();
}

function renderTimer() {
  const m = Math.floor(Math.max(timerSeconds, 0) / 60);
  const s = Math.max(timerSeconds, 0) % 60;
  const display = document.getElementById("timer-display");
  display.textContent = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  display.classList.toggle("time-up", timerSeconds <= 0);
  document.getElementById("timer-toggle-btn").textContent = timerRunning ? "Pause" : "Resume";
  socket.emit("sync_timer", { label: timerLabel, remaining: timerSeconds, running: timerRunning });
}

function startTimerInterval() {
  timerRunning = true;
  clearInterval(timerHandle);
  timerHandle = setInterval(() => {
    timerSeconds -= 1;
    if (timerSeconds <= 0 && !timerBeeped) {
      timerBeeped = true;
      beep();
    }
    renderTimer();
  }, 1000);
  renderTimer();
}

function toggleTimer() {
  if (timerRunning) {
    timerRunning = false;
    clearInterval(timerHandle);
    renderTimer();
  } else {
    startTimerInterval();
  }
}

function resetTimer() {
  timerSeconds = 2 * 60;
  timerBeeped = false;
  renderTimer();
}

function adjustTimer(deltaSeconds) {
  timerSeconds += deltaSeconds;
  timerBeeped = timerSeconds > 0 ? false : timerBeeped;
  renderTimer();
}

function closeTimer() {
  clearInterval(timerHandle);
  timerRunning = false;
  hideOverlay("timer-overlay");
  socket.emit("sync_timer", { label: null, remaining: 0, running: false });
}

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.value = 660;
    gain.gain.setValueAtTime(0.2, ctx.currentTime);
    osc.start();
    osc.stop(ctx.currentTime + 0.6);
  } catch (e) { /* audio not available - ignore */ }
}

// ---- moderator narration prompts ----
function openPrompts() {
  const body = document.getElementById("prompts-body");
  const introHtml = `
    <div class="ability-row">
      <div class="ability-title">Intro Script</div>
      <div class="ability-desc intro-text">${INTRO_SCRIPT}</div>
    </div>
  `;
  body.innerHTML = introHtml + PROMPTS.map(p => `
    <div class="ability-row ${p.deprecated ? "prompt-deprecated" : ""}">
      <div class="ability-title">${p.title}${p.deprecated ? ' <span class="prompt-tag">no longer used</span>' : ""}</div>
      <div class="ability-desc prompt-quote">&ldquo;${p.text}&rdquo;</div>
    </div>
  `).join("");
  showOverlay("prompts-overlay");
}
function closePrompts() {
  hideOverlay("prompts-overlay");
}

// ---- phase script popup (what Watchtower says aloud) ----
function openPhaseScript(script) {
  renderPhaseScriptBody(script);
  showOverlay("phase-script-overlay");
}

let lastChecklistPhase = null;

function renderPhaseScriptBody(script) {
  document.getElementById("phase-script-title").textContent = script.phase + "!";

  const linesEl = document.getElementById("phase-script-lines");
  linesEl.innerHTML = script.lines.length
    ? script.lines.map(line => `<div class="phase-script-line">${line}</div>`).join("")
    : `<div class="phase-script-line" style="opacity:.6">No line to read for this phase - see the checklist below.</div>`;

  const timerEl = document.getElementById("phase-script-timer");
  timerEl.innerHTML = "";
  if (script.phase === "Discuss") {
    timerEl.innerHTML = `<button class="btn-primary" style="margin-top:14px" onclick="closePhaseScript(); openTimer(2*60, 'Discuss!');">Start 2-minute timer</button>`;
  }
  if (script.phase === "Vote") {
    timerEl.innerHTML = `<button class="btn-primary" style="margin-top:14px" onclick="closePhaseScript(); openTimer(2*60, 'Vote!');">Start 2-minute timer</button>`;
  }

  // Only rebuild the checklist when the phase actually changes, so
  // checking items off doesn't get wiped out by live script refreshes
  // (e.g. Vote's tally updating every few seconds).
  if (script.phase !== lastChecklistPhase) {
    lastChecklistPhase = script.phase;
    const checklistEl = document.getElementById("phase-script-checklist");
    const items = script.checklist || [];
    checklistEl.innerHTML = items.length
      ? `<div class="checklist-heading">Host Checklist</div>` +
        items.map((item, i) => `
          <label class="checklist-item">
            <input type="checkbox" id="checklist-${i}">
            <span>${item}</span>
          </label>
        `).join("")
      : "";
  }
}

function closePhaseScript() {
  hideOverlay("phase-script-overlay");
}
