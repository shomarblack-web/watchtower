const socket = io();
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
  document.getElementById("shuffle-overlay").style.display = "flex";
});

function closeReveal() {
  document.getElementById("shuffle-overlay").style.display = "none";
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
  document.getElementById("mycard-overlay").style.display = "flex";
}
function closeMyCard() {
  document.getElementById("mycard-overlay").style.display = "none";
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
  document.getElementById("rules-overlay").style.display = "flex";
}
function closeRules() {
  document.getElementById("rules-overlay").style.display = "none";
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

const VOTE_PHASES = ["Vote", "Accuse"];

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
    myVote = state.votes[myName] || null;
    renderVoteList(state);
  }

  renderActiveList(state);
});

function renderVoteList(state) {
  const list = document.getElementById("vote-list");
  const active = CHARACTERS.filter(c => state.characters[c.id] && state.characters[c.id].active);
  if (!active.length) {
    list.innerHTML = `<div class="empty">No one is on the board yet.</div>`;
    return;
  }
  list.innerHTML = active.map(c => `
    <div class="vote-option ${myVote === c.id ? 'picked' : ''}" onclick="castVote('${c.id}')">
      <span><span class="team-dot" style="background:${TEAM_COLORS[c.team]}"></span>${c.name}</span>
      ${myVote === c.id ? '<b>✓</b>' : ''}
    </div>
  `).join("");

  const confirm = document.getElementById("vote-confirm");
  confirm.innerHTML = myVote
    ? `<div class="confirm-banner">Vote locked in</div>`
    : "";
}

function castVote(targetId) {
  if (!myName) return;
  myVote = targetId;
  socket.emit("cast_vote", { voter: myName, target_id: targetId });
}

function renderActiveList(state) {
  const el = document.getElementById("active-list");
  const active = CHARACTERS.filter(c => state.characters[c.id] && state.characters[c.id].active);
  if (!active.length) {
    el.innerHTML = `<div class="empty">Nobody's active yet — hang tight.</div>`;
    return;
  }
  el.innerHTML = active.map(c => {
    const st = state.characters[c.id];
    let statusBits = [];
    if (st.health !== null && st.health !== undefined) {
      statusBits.push(`<span style="color:${st.health === 0 ? 'var(--down)' : 'var(--muted)'}">HP ${st.health}</span>`);
    }
    if (st.shield !== null && st.shield !== undefined) {
      statusBits.push(`<span style="color:var(--hero)">🛡${st.shield}</span>`);
    }
    const status = statusBits.length ? ` — ${statusBits.join(" · ")}` : "";
    return `<div class="feed-item"><span class="team-dot" style="background:${TEAM_COLORS[c.team]};display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px"></span>${c.name}${st.player_name ? ' — ' + st.player_name : ''}${status}</div>`;
  }).join("");
}
