const socket = io();

// ---- animated overlay show/hide (fade + scale, see .map-overlay CSS) ----
function showOverlay(id) {
  document.getElementById(id).classList.add("overlay-open");
}
function hideOverlay(id) {
  document.getElementById(id).classList.remove("overlay-open");
}

let myName = localStorage.getItem("watchtower_name") || "";
let myVote = null;
let latestState = null;

if (myName) showGame();

socket.on("connect", () => {
  socket.emit("register_player", { name: myName });
});

socket.on("whoami_result", (data) => {
  renderWhoAmI((data && data.characters) || []);
});

function setName() {
  const v = document.getElementById("player-name").value.trim();
  if (!v) return;
  myName = v;
  localStorage.setItem("watchtower_name", v);
  socket.emit("register_player", { name: myName });
  showGame();
}

function showGame() {
  document.getElementById("name-gate").style.display = "none";
  document.getElementById("game-view").style.display = "block";
  document.getElementById("player-toolbar").style.display = "flex";
}

function renderWhoAmI(characters) {
  const el = document.getElementById("whoami-banner");
  if (!el) return;
  if (!characters.length) {
    el.style.display = "none";
    el.textContent = "";
    return;
  }
  el.style.display = "block";
  el.textContent = characters.length === 1
    ? `You are ${characters[0]}`
    : `You are: ${characters.join(", ")}`;
}

// ---- shuffle reveal ----
socket.on("shuffle_reveal", (data) => {
  document.getElementById("reveal-name").textContent = data.character;
  showOverlay("shuffle-overlay");
});

function closeReveal() {
  hideOverlay("shuffle-overlay");
}

// ---- super ability unlocked (Round 3+) ----
socket.on("super_ability_unlocked", (data) => {
  document.getElementById("super-character-name").textContent = data.character;
  document.getElementById("super-ability-text").textContent = data.ability;
  showOverlay("super-overlay");
});

function closeSuperOverlay() {
  hideOverlay("super-overlay");
}

// ---- conditions (Exposed / Eliminated / Rescued / Targeted) ----
socket.on("condition_alert", (data) => {
  renderConditionOverlay([data]);
});

socket.on("condition_recap", (data) => {
  renderConditionOverlay(data.conditions || []);
});

function renderConditionOverlay(conditions) {
  if (!conditions.length) return;
  const body = document.getElementById("condition-list-body");
  body.innerHTML = conditions.map(c => `
    <div class="condition-entry">
      <div class="reveal-label" style="color:var(--amber)">${c.title}</div>
      <div style="font-size:14px; color:var(--text); margin:8px 0 14px; line-height:1.5">${c.body}</div>
    </div>
  `).join("");
  showOverlay("condition-overlay");
}

function closeConditionOverlay() {
  hideOverlay("condition-overlay");
}

// ---- game over ----
socket.on("game_over", (data) => {
  document.getElementById("gameover-title").textContent = data.title;
  document.getElementById("gameover-message").textContent = data.message;
  showOverlay("gameover-overlay");
});

function closeGameOver() {
  hideOverlay("gameover-overlay");
}

// ---- my card ----
socket.on("my_card_result", (data) => {
  const body = document.getElementById("mycard-body");
  document.getElementById("mycard-name").textContent = data.assigned ? data.character : "No character yet";
  if (!data.assigned) {
    body.innerHTML = `<div class="empty">You haven't been assigned a character yet — ask your host to shuffle.</div>`;
    return;
  }
  const card = data.card || {};
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
});

function openMyCard() {
  socket.emit("get_my_card", { name: myName });
  showOverlay("mycard-overlay");
}
function closeMyCard() {
  hideOverlay("mycard-overlay");
}

// ---- rules & phases ----
function openRules() {
  const body = document.getElementById("rules-body");
  body.innerHTML = PHASES.map(p => `
    <div class="ability-row">
      <div class="ability-title">${p}!</div>
      <div class="ability-desc">${PHASE_INFO[p] || ""}</div>
    </div>
  `).join("");
  showOverlay("rules-overlay");
}
function closeRules() {
  hideOverlay("rules-overlay");
}

// ---- phase reminder toast ----
socket.on("phase_reminder", (data) => {
  const toast = document.getElementById("phase-reminder-toast");
  if (!data.abilities || !data.abilities.length) {
    toast.style.display = "none";
    return;
  }
  document.getElementById("phase-toast-title").textContent = `${data.phase}! — ${data.character}`;
  document.getElementById("phase-toast-body").innerHTML = data.abilities.map(a => {
    const m = a.match(/^([A-Z ]+ABILITY)\.\s*([^.]*)\.\s*(.*)$/);
    return m ? `<div><b>${m[2]}</b> — ${m[3]}</div>` : `<div>${a}</div>`;
  }).join("");
  toast.style.display = "block";
});
function dismissReminder() {
  document.getElementById("phase-reminder-toast").style.display = "none";
}

const VOTE_PHASES = ["Vote"];
let myVoteLocked = false;
let pendingVote = null;

socket.on("my_vote_result", (data) => {
  myVote = data.choice || null;
  myVoteLocked = !!data.voted;
  pendingVote = null;
  if (latestState) renderVoteList(latestState);
});

socket.on("state", (state) => {
  latestState = state;

  const round = document.getElementById("round-label");
  const phaseLabel = document.getElementById("phase-label");
  round.textContent = `ROUND ${state.round} / ${state.num_rounds}`;
  phaseLabel.textContent = state.phase_index !== null ? PHASES[state.phase_index] + "!" : "Standing by…";

  const votePanel = document.getElementById("vote-panel");
  const inVotePhase = state.phase_index !== null && VOTE_PHASES.includes(PHASES[state.phase_index]);
  votePanel.style.display = inVotePhase ? "block" : "none";

  if (inVotePhase) {
    renderVoteList(state);
  }

  renderActiveList(state);
});

function renderVoteList(state) {
  const list = document.getElementById("vote-list");
  const candidates = state.vote_candidates || [];
  const confirmEl = document.getElementById("vote-confirm");

  if (!candidates.length) {
    list.innerHTML = `<div class="empty">No one is on the board yet.</div>`;
    confirmEl.innerHTML = "";
    return;
  }

  if (myVoteLocked) {
    list.innerHTML = candidates.map(name => `
      <div class="vote-option locked-option ${myVote === name ? 'picked' : ''}">
        <span>${name}</span>
        ${myVote === name ? '<b>✓</b>' : ''}
      </div>
    `).join("");
    confirmEl.innerHTML = `<div class="confirm-banner">Vote locked in for ${myVote}</div>`;
    return;
  }

  list.innerHTML = candidates.map((name, i) => `
    <div class="vote-option ${pendingVote === name ? 'picked' : ''}" data-idx="${i}">
      <span>${name}</span>
    </div>
  `).join("");
  list.querySelectorAll(".vote-option").forEach((el, i) => {
    el.addEventListener("click", () => selectVoteCandidate(candidates[i]));
  });

  confirmEl.innerHTML = pendingVote
    ? `<div class="vote-confirm-prompt">
         <div>Vote for <b>${pendingVote}</b>?</div>
         <div class="vote-confirm-buttons">
           <button class="btn-primary" onclick="submitVote()">Confirm Vote</button>
           <button class="btn-ghost" onclick="cancelPendingVote()">Cancel</button>
         </div>
       </div>`
    : "";
}

function selectVoteCandidate(name) {
  if (myVoteLocked) return;
  pendingVote = name;
  renderVoteList(latestState);
}

function cancelPendingVote() {
  pendingVote = null;
  renderVoteList(latestState);
}

function submitVote() {
  if (!myName || !pendingVote || myVoteLocked) return;
  socket.emit("cast_vote", { voter: myName, target_name: pendingVote });
}

function renderActiveList(state) {
  const el = document.getElementById("active-list");
  const active = CHARACTERS.filter(c => state.characters[c.id] && state.characters[c.id].active);
  if (!active.length) {
    el.innerHTML = `<div class="empty">Nobody's active yet — hang tight.</div>`;
    return;
  }
  el.innerHTML = active.map(c => {
    const displayName = state.characters[c.id].display_name || c.name;
    return `<div class="feed-item"><span class="team-dot" style="background:${TEAM_COLORS[c.team]};display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px"></span>${displayName}</div>`;
  }).join("");
}
