const socket = io();

// ---- animated overlay show/hide (fade + scale, see .map-overlay CSS) ----
function showOverlay(id) {
  document.getElementById(id).classList.add("overlay-open");
}
function hideOverlay(id) {
  document.getElementById(id).classList.remove("overlay-open");
}

// ---- Type: X tags render as a small stylized letter badge instead of
// the plain word, matching the team badge colors used elsewhere ----
const TYPE_BADGE = {
  civilian: '<span class="type-tag type-tag-civilian">C</span>',
  hero: '<span class="type-tag type-tag-hero">H</span>',
  villain: '<span class="type-tag type-tag-villain">V</span>',
};
function styleTypeTags(text) {
  return text.replace(/type:?\s*(civilian|hero|villain)(\s+only)?/gi, (m, cls, only) => {
    return "Type: " + TYPE_BADGE[cls.toLowerCase()] + (only ? " only" : "");
  });
}

// ---- ability text parser (KIND ABILITY[.:] Title[.!?…] Description) ----
// Title-ending punctuation is "." (stripped - just a neutral sentence
// end) or "!"/"?"/an ellipsis (kept - usually part of the title's own
// flavor, e.g. "Zip!", "Bzz!", "Join Me…").
function parseAbilityText(a) {
  const m = a.match(/^([A-Z ]+ABILITY)[.:]\s*(.+?)(\.\.\.|\u2026|[.!?])\s*([\s\S]*)$/);
  if (!m) return null;
  const [, kind, titleBase, term, rawDesc] = m;
  const keepTerm = term === "." ? "" : (term === "..." || term === "\u2026" ? "\u2026" : term);
  const desc = styleTypeTags(rawDesc.replace(/^[.\s]+/, ""));
  return { kind, title: titleBase + keepTerm, desc };
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

// ---- vibration feedback (Android only - iOS Safari has never
// implemented the Vibration API, so this silently does nothing there) ----
function vibrateDevice(pattern) {
  if (navigator.vibrate) {
    navigator.vibrate(pattern);
  }
}

// ---- conditions (Exposed / Eliminated / Rescued / Targeted) ----
socket.on("condition_alert", (data) => {
  if (data.title === "Eliminated!") {
    vibrateDevice([200, 100, 200, 100, 400]);
  }
  renderConditionOverlay([data]);
});

socket.on("hp_lost", (data) => {
  vibrateDevice(150);
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

// ---- ask Watchtower (silent Martian-check inspection) ----
socket.on("inspect_prompt", (data) => {
  const list = document.getElementById("inspect-candidate-list");
  const candidates = data.candidates || [];
  list.innerHTML = candidates.length
    ? candidates.map(name => `<div class="hostage-target" data-name="${name}">${name}</div>`).join("")
    : `<div class="empty">No one else is active right now.</div>`;
  list.querySelectorAll(".hostage-target").forEach(el => {
    el.addEventListener("click", () => submitInspectTarget(el.dataset.name));
  });
  showOverlay("inspect-prompt-overlay");
});

function submitInspectTarget(targetName) {
  socket.emit("ask_watchtower", { asker: myName, target_name: targetName });
  hideOverlay("inspect-prompt-overlay");
  document.getElementById("inspect-waiting-target").textContent = targetName;
  showOverlay("inspect-waiting-overlay");
}

socket.on("ask_watchtower_error", (data) => {
  hideOverlay("inspect-waiting-overlay");
  alert(data.message);
});

socket.on("inspection_answer", (data) => {
  hideOverlay("inspect-waiting-overlay");
  document.getElementById("inspect-answer-text").textContent =
    data.answer ? `Yes, ${data.target_name} is a White Martian!` : `No, ${data.target_name} is not a White Martian.`;
  showOverlay("inspect-answer-overlay");
});

function closeInspectAnswer() {
  hideOverlay("inspect-answer-overlay");
}

// ---- Protect phase - silently choose who to shield ----
socket.on("protect_prompt", (data) => {
  const list = document.getElementById("protect-candidate-list");
  const candidates = data.candidates || [];
  const items = candidates.length
    ? candidates.map(name => {
        const label = name.trim().toLowerCase() === myName.trim().toLowerCase() ? `${name} (yourself)` : name;
        return `<div class="hostage-target" data-name="${name}">${label}</div>`;
      }).join("")
    : `<div class="empty">No one else is active right now.</div>`;
  list.innerHTML = items;
  list.querySelectorAll(".hostage-target").forEach(el => {
    el.addEventListener("click", () => submitProtectTarget(el.dataset.name));
  });
  showOverlay("protect-prompt-overlay");
});

function submitProtectTarget(targetName) {
  socket.emit("submit_protect_target", { protector: myName, target_name: targetName });
  hideOverlay("protect-prompt-overlay");
  document.getElementById("protect-confirm-text").textContent = `You chose to shield ${targetName}.`;
  showOverlay("protect-confirm-overlay");
}

function closeProtectConfirm() {
  hideOverlay("protect-confirm-overlay");
}

// ---- Parasite - absorb an Exposed player's abilities ----
socket.on("absorption_prompt", (data) => {
  const list = document.getElementById("absorption-candidate-list");
  const candidates = data.candidates || [];
  list.innerHTML = candidates.length
    ? candidates.map(name => `<div class="hostage-target" data-name="${name}">${name}</div>`).join("")
    : `<div class="empty">No one is currently Exposed.</div>`;
  list.querySelectorAll(".hostage-target").forEach(el => {
    el.addEventListener("click", () => submitAbsorptionTarget(el.dataset.name));
  });
  showOverlay("absorption-prompt-overlay");
});

function submitAbsorptionTarget(targetName) {
  socket.emit("submit_absorption_target", { parasite: myName, target_name: targetName });
  hideOverlay("absorption-prompt-overlay");
  document.getElementById("absorption-confirm-text").textContent = `You absorbed ${targetName}'s abilities.`;
  showOverlay("absorption-confirm-overlay");
}

function closeAbsorptionConfirm() {
  hideOverlay("absorption-confirm-overlay");
}

// ---- Dr. Alchemy - target a player, then choose Protector/Eliminator ----
socket.on("alchemy_prompt", (data) => {
  const list = document.getElementById("alchemy-candidate-list");
  const candidates = data.candidates || [];
  list.innerHTML = candidates.length
    ? candidates.map(name => `<div class="hostage-target" data-name="${name}">${name}</div>`).join("")
    : `<div class="empty">No one else is active right now.</div>`;
  list.querySelectorAll(".hostage-target").forEach(el => {
    el.addEventListener("click", () => {
      socket.emit("submit_alchemy_target", { alchemist: myName, target_name: el.dataset.name });
      hideOverlay("alchemy-prompt-overlay");
    });
  });
  showOverlay("alchemy-prompt-overlay");
});

socket.on("alchemy_choice_prompt", (data) => {
  document.getElementById("alchemy-choice-target").textContent = data.target_name;
  showOverlay("alchemy-choice-overlay");
});

function submitAlchemyChoice(choice) {
  socket.emit("submit_alchemy_choice", { alchemist: myName, choice });
  hideOverlay("alchemy-choice-overlay");
  const label = choice === "protector" ? "Protector" : "Eliminator";
  document.getElementById("alchemy-confirm-text").textContent = `They are now a ${label}.`;
  showOverlay("alchemy-confirm-overlay");
}

function closeAlchemyConfirm() {
  hideOverlay("alchemy-confirm-overlay");
}

// ---- Citizen's Arrest / Forget the Rules ----
socket.on("arrest_prompt", (data) => {
  const list = document.getElementById("arrest-candidate-list");
  const candidates = data.candidates || [];
  list.innerHTML = candidates.length
    ? candidates.map(name => `<div class="hostage-target" data-name="${name}">${name}</div>`).join("")
    : `<div class="empty">No one else is active right now.</div>`;
  list.querySelectorAll(".hostage-target").forEach(el => {
    el.addEventListener("click", () => {
      socket.emit("submit_arrest_target", { arrester: myName, target_name: el.dataset.name });
      hideOverlay("arrest-prompt-overlay");
      document.getElementById("arrest-confirm-text").textContent = `You arrested ${el.dataset.name}.`;
      showOverlay("arrest-confirm-overlay");
    });
  });
  showOverlay("arrest-prompt-overlay");
});

function closeArrestConfirm() {
  hideOverlay("arrest-confirm-overlay");
}

// ---- A Good Doctor (Dr. Caitlin Snow, Leslie Thompkins, Dr. Harleen Quinzel) ----
socket.on("good_doctor_prompt", (data) => {
  const list = document.getElementById("good-doctor-candidate-list");
  const candidates = data.candidates || [];
  list.innerHTML = candidates.length
    ? candidates.map(name => `<div class="hostage-target" data-name="${name}">${name}</div>`).join("")
    : `<div class="empty">No one is currently Eliminated.</div>`;
  list.querySelectorAll(".hostage-target").forEach(el => {
    el.addEventListener("click", () => {
      socket.emit("submit_good_doctor_target", { doctor: myName, target_name: el.dataset.name });
      hideOverlay("good-doctor-prompt-overlay");
      document.getElementById("good-doctor-confirm-text").textContent =
        `You asked Watchtower to restore ${el.dataset.name}.`;
      showOverlay("good-doctor-confirm-overlay");
    });
  });
  showOverlay("good-doctor-prompt-overlay");
});

function closeGoodDoctorConfirm() {
  hideOverlay("good-doctor-confirm-overlay");
}

// ---- Beast Boy's Giraffe! - peek at one player's identity ----
socket.on("giraffe_prompt", (data) => {
  const list = document.getElementById("giraffe-candidate-list");
  const candidates = data.candidates || [];
  list.innerHTML = candidates.length
    ? candidates.map(name => `<div class="hostage-target" data-name="${name}">${name}</div>`).join("")
    : `<div class="empty">No one else is active right now.</div>`;
  list.querySelectorAll(".hostage-target").forEach(el => {
    el.addEventListener("click", () => {
      socket.emit("submit_giraffe_target", { beast_boy: myName, target_name: el.dataset.name });
      hideOverlay("giraffe-prompt-overlay");
    });
  });
  showOverlay("giraffe-prompt-overlay");
});

socket.on("giraffe_reveal", (data) => {
  document.getElementById("giraffe-reveal-text").textContent = `${data.player} is ${data.character}`;
  showOverlay("giraffe-reveal-overlay");
});

function closeGiraffeReveal() {
  hideOverlay("giraffe-reveal-overlay");
}

// ---- Telepathic Link / Telepathic Team (Martian Manhunter, Miss Martian) ----
socket.on("telepathic_link_prompt", (data) => {
  const list = document.getElementById("telepathic-link-candidate-list");
  const candidates = data.candidates || [];
  list.innerHTML = candidates.length
    ? candidates.map(name => `<div class="hostage-target" data-name="${name}">${name}</div>`).join("")
    : `<div class="empty">No one new is available to link with right now.</div>`;
  list.querySelectorAll(".hostage-target").forEach(el => {
    el.addEventListener("click", () => {
      socket.emit("submit_telepathic_link_target", { inspector: myName, target_name: el.dataset.name });
      hideOverlay("telepathic-link-prompt-overlay");
    });
  });
  showOverlay("telepathic-link-prompt-overlay");
});

socket.on("telepathic_team_reveal", (data) => {
  const list = document.getElementById("telepathic-team-list");
  const entries = data.entries || [];
  list.innerHTML = entries.length
    ? entries.map(e => `<div class="hostage-target" style="cursor:default">${e.player} is ${e.character}</div>`).join("")
    : `<div class="empty">No one else is currently linked.</div>`;
  showOverlay("telepathic-team-reveal-overlay");
});

function closeTelepathicTeamReveal() {
  hideOverlay("telepathic-team-reveal-overlay");
}

// ---- The Flash's Fastest Man Alive - seat swap ----
socket.on("speedster_swap_prompt", (data) => {
  const list = document.getElementById("speedster-swap-candidate-list");
  const candidates = data.candidates || [];
  list.innerHTML = candidates.length
    ? candidates.map(name => `<div class="hostage-target" data-name="${name}">${name}</div>`).join("")
    : `<div class="empty">No one else is active right now.</div>`;
  list.querySelectorAll(".hostage-target").forEach(el => {
    el.addEventListener("click", () => {
      socket.emit("submit_speedster_swap_target", { flash: myName, target_name: el.dataset.name });
      hideOverlay("speedster-swap-prompt-overlay");
    });
  });
  showOverlay("speedster-swap-prompt-overlay");
});

// Public - every player sees this, by design. It's a visible tell.
socket.on("seat_swap_announcement", (data) => {
  document.getElementById("seat-swap-announcement-text").textContent =
    `${data.player_a} swapped seats with ${data.player_b}!`;
  showOverlay("seat-swap-announcement-overlay");
});

function closeSeatSwapAnnouncement() {
  hideOverlay("seat-swap-announcement-overlay");
}

// ---- Plastic Man's Group Hug - silent left/right shield ----
socket.on("plastic_man_prompt", () => {
  showOverlay("plastic-man-prompt-overlay");
});

function submitPlasticManChoice(direction) {
  socket.emit("submit_plastic_man_choice", { plastic_man: myName, direction });
  hideOverlay("plastic-man-prompt-overlay");
}

// ---- Reverse Flash's Not So Fast - seat swap with a Teleport target ----
socket.on("reverse_flash_prompt", (data) => {
  const list = document.getElementById("reverse-flash-candidate-list");
  const candidates = data.candidates || [];
  list.innerHTML = candidates.length
    ? candidates.map(name => `<div class="hostage-target" data-name="${name}">${name}</div>`).join("")
    : `<div class="empty">No one is currently Targeted for Teleportation.</div>`;
  list.querySelectorAll(".hostage-target").forEach(el => {
    el.addEventListener("click", () => {
      socket.emit("submit_reverse_flash_target", { reverse_flash: myName, target_name: el.dataset.name });
      hideOverlay("reverse-flash-prompt-overlay");
    });
  });
  showOverlay("reverse-flash-prompt-overlay");
});

// ---- Thunder's Stomp - pick a side ----
socket.on("thunder_prompt", () => {
  showOverlay("thunder-prompt-overlay");
});

function submitThunderChoice(direction) {
  socket.emit("submit_thunder_choice", { thunder: myName, direction });
  hideOverlay("thunder-prompt-overlay");
}

// ---- Hawkman's Timeless Love / Joker's Mad Love - guess who it is.
// Results (correct or not) arrive via the existing condition_alert and
// shuffle_reveal handlers already wired up elsewhere - nothing more
// needed here beyond showing the candidate list. ----
socket.on("wake_prompt", (data) => {
  const list = document.getElementById("wake-candidate-list");
  const candidates = data.candidates || [];
  list.innerHTML = candidates.length
    ? candidates.map(name => `<div class="hostage-target" data-name="${name}">${name}</div>`).join("")
    : `<div class="empty">No one else is active right now.</div>`;
  list.querySelectorAll(".hostage-target").forEach(el => {
    el.addEventListener("click", () => {
      socket.emit("submit_wake_target", { waker: myName, target_name: el.dataset.name });
      hideOverlay("wake-prompt-overlay");
    });
  });
  showOverlay("wake-prompt-overlay");
});

// ---- Secret Identity roster view (Plastic Man's Petty Thief, Zatanna's
// Thgiels fo Dnah) - view-only, auto-dismisses after 10 seconds ----
let secretRosterTimer = null;
socket.on("secret_roster_view", (data) => {
  const list = document.getElementById("secret-roster-list");
  const entries = data.entries || [];
  list.innerHTML = entries.length
    ? entries.map(e => `<div class="hostage-target" style="cursor:default">${e.player} is ${e.character}</div>`).join("")
    : `<div class="empty">No one is currently assigned.</div>`;
  showOverlay("secret-roster-overlay");

  let remaining = 10;
  const countdownEl = document.getElementById("secret-roster-countdown");
  const barEl = document.getElementById("secret-roster-bar");
  countdownEl.textContent = remaining;
  barEl.style.transition = "none";
  barEl.style.width = "100%";
  // Force reflow so the next width change animates smoothly from 100%.
  void barEl.offsetWidth;
  barEl.style.transition = "width 1s linear";
  if (secretRosterTimer) clearInterval(secretRosterTimer);
  secretRosterTimer = setInterval(() => {
    remaining -= 1;
    countdownEl.textContent = Math.max(remaining, 0);
    barEl.style.width = `${Math.max(remaining, 0) * 10}%`;
    if (remaining <= 0) {
      clearInterval(secretRosterTimer);
      secretRosterTimer = null;
      hideOverlay("secret-roster-overlay");
    }
  }, 1000);
});

// ---- Round-change requests (Mind Merge, Blackout, Altering the
// Timeline, Loyal Assistant, Construct, Turn the Earth) ----
function renderRoundChangeButton(rc) {
  if (rc.pending) {
    return `<button class="btn-ghost round-change-btn" disabled style="margin-top:8px;width:auto">Waiting for Watchtower&hellip;</button>`;
  }
  const disabledAttr = rc.enabled ? "" : "disabled";
  return `<button class="btn-primary round-change-btn" style="margin-top:8px;width:auto" ${disabledAttr}
            onclick="requestRoundChange('${rc.label}')">
            Request: ${rc.target_phase}!
          </button>`;
}

function requestRoundChange(label) {
  if (!currentCardId) return;
  socket.emit("request_round_change", { id: currentCardId, player: myName });
  // Refresh the card shortly after so the button flips to "Waiting..."
  setTimeout(() => socket.emit("get_my_card", { name: myName }), 200);
}

// ---- secret identity reveal (Know You Anywhere) ----
socket.on("secret_identity_reveal", (data) => {
  const reveals = data.reveals || [];
  document.getElementById("secret-identity-text").innerHTML = reveals
    .map(r => `<div>${r.target_player} is ${r.target_name}</div>`)
    .join("");
  showOverlay("secret-identity-overlay");
});

function closeSecretIdentity() {
  hideOverlay("secret-identity-overlay");
}

// ---- my card ----
const TEAM_ICONS = {
  civilian: { letter: "C", bg: "#f3aecb", fg: "#3a1a28" },
  villain: { letter: "V", bg: "#2fbf6e", fg: "#0a2214" },
  hero: { letter: "H", bg: "#3b7fe0", fg: "#ffffff" },
  martian: { letter: "M", bg: "#9aa1ab", fg: "#1a1c1f" },
};

function letterBadgeSvg(letter, bg, fg, title) {
  return `<svg viewBox="0 0 44 44" class="team-badge" title="${title}">
    <circle cx="22" cy="22" r="21" fill="${bg}" stroke="#05070a" stroke-width="2"/>
    <text x="22" y="30" text-anchor="middle" font-family="Rajdhani, sans-serif"
          font-weight="800" font-size="22" fill="${fg}">${letter}</text>
  </svg>`;
}

function kryptonianBadgeSvg() {
  return `<svg viewBox="0 0 44 44" class="team-badge" title="Kryptonian">
    <circle cx="22" cy="22" r="21" fill="#7dd3fc" stroke="#05070a" stroke-width="2"/>
    <text x="22" y="30" text-anchor="middle" font-family="Rajdhani, sans-serif"
          font-weight="800" font-size="22" fill="#062a3d">K</text>
  </svg>`;
}

function furyBadgeSvg() {
  return `<svg viewBox="0 0 44 44" class="team-badge" title="Fury">
    <circle cx="22" cy="22" r="21" fill="#e5484d" stroke="#05070a" stroke-width="2"/>
    <path d="M11 15 L18 18" stroke="#3a0508" stroke-width="2.5" stroke-linecap="round"/>
    <path d="M33 15 L26 18" stroke="#3a0508" stroke-width="2.5" stroke-linecap="round"/>
    <circle cx="16" cy="22" r="2.4" fill="#3a0508"/>
    <circle cx="28" cy="22" r="2.4" fill="#3a0508"/>
    <path d="M14 33 Q22 26 30 33" stroke="#3a0508" stroke-width="2.5" fill="none" stroke-linecap="round"/>
  </svg>`;
}

function starroBadgeSvg() {
  const cx = 22, cy = 22, rOuter = 20, rInner = 8;
  const points = [];
  for (let i = 0; i < 10; i++) {
    const r = i % 2 === 0 ? rOuter : rInner;
    const angle = (Math.PI / 5) * i - Math.PI / 2;
    points.push(`${cx + r * Math.cos(angle)},${cy + r * Math.sin(angle)}`);
  }
  return `<svg viewBox="0 0 44 44" class="team-badge" title="Starro">
    <polygon points="${points.join(" ")}" fill="#c084fc" stroke="#05070a" stroke-width="2" stroke-linejoin="round"/>
  </svg>`;
}

function speedsterBadgeSvg() {
  return `<svg viewBox="0 0 44 44" class="team-badge" title="Speedster">
    <circle cx="22" cy="22" r="21" fill="#f5d76e" stroke="#05070a" stroke-width="2"/>
    <polygon points="24,8 12,25 20,25 18,36 32,18 23,18" fill="#3a2f05" stroke="#3a2f05" stroke-width="1" stroke-linejoin="round"/>
  </svg>`;
}

function renderCardBadges(data) {
  const el = document.getElementById("mycard-badges");
  const badges = [];
  const teamIcon = TEAM_ICONS[data.team];
  if (teamIcon) {
    badges.push(letterBadgeSvg(teamIcon.letter, teamIcon.bg, teamIcon.fg, data.team));
  }
  if (data.is_kryptonian) badges.push(kryptonianBadgeSvg());
  if (data.is_speedster) badges.push(speedsterBadgeSvg());
  if (data.fury) badges.push(furyBadgeSvg());
  if (data.starro) badges.push(starroBadgeSvg());
  el.innerHTML = badges.join("");
}

let currentCardId = null;

socket.on("my_card_result", (data) => {
  const body = document.getElementById("mycard-body");
  document.getElementById("mycard-name").textContent = data.assigned ? data.character : "No character yet";
  renderCardBadges(data.assigned ? data : {});
  currentCardId = data.assigned ? data.id : null;
  if (!data.assigned) {
    body.innerHTML = `<div class="empty">You haven't been assigned a character yet — ask your host to shuffle.</div>`;
    return;
  }
  const card = data.card || {};
  const rc = data.round_change;
  const abilityRows = (card.abilities || []).map(a => {
    const parsed = parseAbilityText(a);
    if (parsed) {
      const isRoundChangeAbility = rc && parsed.title === rc.label;
      const buttonHtml = isRoundChangeAbility ? renderRoundChangeButton(rc) : "";
      return `<div class="ability-row">
                <div class="ability-kind">${parsed.kind}</div>
                <div class="ability-title">${parsed.title}</div>
                <div class="ability-desc">${parsed.desc}</div>
                ${buttonHtml}
              </div>`;
    }
    return `<div class="ability-row"><div class="ability-desc">${a}</div></div>`;
  }).join("");
  const trackerHtml = data.lobo_tracker ? `
    <div class="card-meta" style="margin-top:14px">The Main Man — Exposed Tracker (${data.lobo_tracker.civilian + data.lobo_tracker.hero + data.lobo_tracker.martian} / 3)</div>
    <div class="lobo-tracker-row"><span class="lobo-tracker-label">Civilians</span><span class="lobo-tracker-count">${data.lobo_tracker.civilian}</span></div>
    <div class="lobo-tracker-row"><span class="lobo-tracker-label">Heroes</span><span class="lobo-tracker-count">${data.lobo_tracker.hero}</span></div>
    <div class="lobo-tracker-row"><span class="lobo-tracker-label">Martians</span><span class="lobo-tracker-count">${data.lobo_tracker.martian}</span></div>
  ` : "";
  const speedsterHtml = data.speedster_count !== null && data.speedster_count !== undefined ? `
    <div class="card-meta" style="margin-top:14px">Speed Thief — Active Speedsters in Play</div>
    <div class="lobo-tracker-row"><span class="lobo-tracker-label">Speedsters (not counting you)</span><span class="lobo-tracker-count">${data.speedster_count}</span></div>
  ` : "";
  const kryptonianHtml = data.kryptonian_count !== null && data.kryptonian_count !== undefined ? `
    <div class="card-meta" style="margin-top:14px">For Krypton — Active Kryptonians in Play</div>
    <div class="lobo-tracker-row"><span class="lobo-tracker-label">Kryptonians (not counting you)</span><span class="lobo-tracker-count">${data.kryptonian_count}</span></div>
  ` : "";
  body.innerHTML = `
    ${card.role ? `<div class="card-meta">${card.role}</div>` : ""}
    ${card.signal ? `<div class="card-meta">${card.signal}</div>` : ""}
    <div class="ability-list">${abilityRows || '<div class="empty">No abilities on file.</div>'}</div>
    ${card.strategy ? `<div class="card-strategy">${card.strategy}</div>` : ""}
    ${trackerHtml}
    ${speedsterHtml}
    ${kryptonianHtml}
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
    const parsed = parseAbilityText(a);
    return parsed ? `<div><b>${parsed.title}</b> — ${parsed.desc}</div>` : `<div>${a}</div>`;
  }).join("");
  toast.style.display = "block";
});
function dismissReminder() {
  document.getElementById("phase-reminder-toast").style.display = "none";
}

socket.on("phase_guide", (data) => {
  const toast = document.getElementById("phase-guide-toast");
  if (!data.text) {
    toast.style.display = "none";
    return;
  }
  document.getElementById("phase-guide-title").textContent = `${data.phase}! — How to Play`;
  document.getElementById("phase-guide-body").textContent = data.text;
  toast.style.display = "block";
});
function dismissGuide() {
  document.getElementById("phase-guide-toast").style.display = "none";
}

const VOTE_PHASES = ["Vote", "Eliminate"];
let myVoteLocked = false;
let pendingVote = null;
let myCanVote = false;

socket.on("my_vote_result", (data) => {
  myVote = data.choice || null;
  myVoteLocked = !!data.voted;
  myCanVote = !!data.can_vote;
  pendingVote = null;
  if (latestState) renderVoteList(latestState);
});

socket.on("timer_update", (timer) => {
  renderPlayerTimer(timer);
});

function renderPlayerTimer(timer) {
  const el = document.getElementById("player-timer");
  if (!timer || !timer.label) {
    el.style.display = "none";
    return;
  }
  const remaining = Math.max(0, timer.remaining);
  const m = Math.floor(remaining / 60);
  const s = remaining % 60;
  document.getElementById("player-timer-label").textContent = timer.label;
  document.getElementById("player-timer-display").textContent =
    `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  el.classList.toggle("time-up", remaining <= 0);
  el.classList.toggle("paused", !timer.running);
  el.style.display = "flex";
}

socket.on("state", (state) => {
  latestState = state;

  const round = document.getElementById("round-label");
  const phaseLabel = document.getElementById("phase-label");
  round.textContent = `ROUND ${state.round} / ${state.num_rounds}`;
  phaseLabel.textContent = state.phase_index !== null ? PHASES[state.phase_index] + "!" : "Standing by…";

  renderPlayerTimer(state.timer);

  const votePanel = document.getElementById("vote-panel");
  const inVotePhase = state.phase_index !== null && VOTE_PHASES.includes(PHASES[state.phase_index]) && myCanVote;
  votePanel.style.display = inVotePhase ? "block" : "none";

  if (inVotePhase) {
    document.getElementById("vote-panel-heading").textContent =
      PHASES[state.phase_index] === "Eliminate" ? "Choose who to eliminate" : "Cast your vote";
    renderVoteList(state);
  }

  renderActiveList(state);
  renderPlayerSeatingDiagram(state);
});

function renderPlayerSeatingDiagram(state) {
  const panel = document.getElementById("seating-panel");
  const el = document.getElementById("player-seating-diagram");
  const seats = state.seats || [];
  if (!seats.length) {
    panel.style.display = "none";
    return;
  }
  panel.style.display = "block";
  const size = 240, cx = size / 2, cy = size / 2, radius = 85, seatR = 20;
  const n = seats.length;
  const myLower = (myName || "").trim().toLowerCase();
  const seatEls = seats.map((name, i) => {
    const angle = (2 * Math.PI * i) / n - Math.PI / 2;
    const x = cx + radius * Math.cos(angle);
    const y = cy + radius * Math.sin(angle);
    const isMe = name.trim().toLowerCase() === myLower;
    const fill = isMe ? "#f5b942" : "#1b2330";
    const textColor = isMe ? "#1a1305" : "#e8edf2";
    return `
      <circle cx="${x}" cy="${y}" r="${seatR}" fill="${fill}" stroke="#f5b942" stroke-width="2"/>
      <text x="${x}" y="${y + 5}" text-anchor="middle" font-family="Rajdhani, sans-serif"
            font-weight="700" font-size="13" fill="${textColor}">${(name || "").trim().slice(0, 2).toUpperCase()}</text>
    `;
  }).join("");
  el.innerHTML = `
    <svg viewBox="0 0 ${size} ${size + 36}" style="width:100%; max-width:240px; display:block; margin:0 auto;">
      <circle cx="${cx}" cy="${cy}" r="${radius + seatR + 6}" fill="none" stroke="#2a3341" stroke-width="1" stroke-dasharray="3,4"/>
      ${seatEls}
      <circle cx="${cx}" cy="${cy - radius - seatR - 20}" r="14" fill="#3b7fe0" stroke="#05070a" stroke-width="2"/>
      <text x="${cx}" y="${cy - radius - seatR - 16}" text-anchor="middle" font-family="Rajdhani, sans-serif"
            font-weight="800" font-size="9" fill="#fff">WT</text>
    </svg>
    <div style="text-align:center; color:var(--muted); font-size:11px">Your seat is highlighted</div>
  `;
}

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
