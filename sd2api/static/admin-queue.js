// Queue status for pending tasks (see sd2api/queue_wait.py).
// TikTok reports the same "generating" state while a task waits in its queue and while it
// renders, so pending tasks show how long they have waited, how much of the wait estimated
// at submission is left, and TikTok's current estimate for the model.
// Loaded after admin.js and reuses its globals ($, $$, esc, api, state).

const QUEUE_BUSY_SECONDS = 60 * 60;
const queueState = { models: {} };

function formatWait(seconds) {
  const minutes = Math.round(Math.max(0, Number(seconds) || 0) / 60);
  if (minutes < 1) return "不到 1 分钟";
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
}

function queueOf(taskOrId) {
  if (taskOrId && typeof taskOrId === "object") return taskOrId.queue || null;
  return state.tasks.find((task) => task.id === taskOrId)?.queue || null;
}

// [text, isBusy] lines, most important first.
function queueLines(queue) {
  if (!queue) return [];
  const lines = [[`已等待 ${formatWait(queue.elapsed_seconds)}`, false]];
  if (Number(queue.submit_wait_seconds) > 0) {
    lines.push(queue.remaining_seconds > 0
      ? [`预计还需约 ${formatWait(queue.remaining_seconds)}`, false]
      : [`已超出提交时预计 ${formatWait(queue.overdue_seconds)}`, true]);
  }
  if (queue.current_wait_seconds !== undefined) {
    lines.push([`TikTok 当前排队约 ${formatWait(queue.current_wait_seconds)}`, queue.current_wait_seconds >= QUEUE_BUSY_SECONDS]);
  }
  return lines;
}

const queueLineHtml = ([text, busy], className) => `<span class="${className}${busy ? " queue-busy" : ""}">${esc(text)}</span>`;

// Status cell lines for the video table; "" when the task has no queue details.
function queueStatusCell(task) {
  return queueLines(queueOf(task)).map((line) => queueLineHtml(line, "cell-sub")).join("");
}

function queueRecentLine(task) {
  const lines = queueLines(queueOf(task)).slice(0, 2);
  return lines.length ? `<div class="queue-line">${lines.map((line) => queueLineHtml(line, "queue-part")).join("")}</div>` : "";
}

function queueFocusLine(taskId) {
  const lines = queueLines(queueOf(taskId));
  return lines.length ? `<div class="queue-focus" title="TikTok 对排队中和生成中返回同一个状态；预计来自 Creative Studio，每 90 秒更新">${lines.map((line) => queueLineHtml(line, "queue-part")).join("")}</div>` : "";
}

// Estimated progress (1–95) from the wait TikTok predicted at submission, if known.
function queueProgress(taskId) {
  const progress = queueOf(taskId)?.progress;
  return Number.isFinite(progress) ? progress : undefined;
}

function renderModeWaits() {
  const waits = queueState.models[$("generationModel")?.value] || {};
  $$(".mode-button[data-mode]").forEach((button) => {
    let label = button.querySelector(".mode-wait");
    if (!label) {
      label = document.createElement("small");
      label.className = "mode-wait";
      label.title = "TikTok Creative Studio 当前预计排队时间，每 90 秒更新";
      button.append(label);
    }
    const wait = waits[button.dataset.mode];
    label.textContent = wait ? `排队约 ${formatWait(wait.seconds)}` : "";
    label.classList.toggle("queue-busy", Boolean(wait && wait.seconds >= QUEUE_BUSY_SECONDS));
  });
}

async function refreshQueueWait() {
  queueState.loadedAt = Date.now();
  try {
    const data = await api("/admin/queue-wait");
    queueState.models = data.models || {};
  } catch (error) {
    console.warn("queue wait", error);
    queueState.models = {};
  }
  renderModeWaits();
  renderQueueBoard();
}

$("generationModel")?.addEventListener("change", renderModeWaits);

// Topbar summary of the current wait for the models worth choosing between: a pill with the
// shortest and longest wait that opens a per-model list. Same data as the mode buttons.
const QUEUE_BOARD = [
  { model: "seedance-2.0", label: "2.0", name: "Seedance 2.0", mode: "text" },
  { model: "seedance-2.0", label: "2.0", name: "Seedance 2.0", mode: "image" },
  { model: "seedance-2.0", label: "2.0", name: "Seedance 2.0", mode: "reference" },
  { model: "seedance-2.0-fast", label: "Fast", name: "Seedance 2.0 Fast", mode: "reference" },
  { model: "seedance-2.0-mini", label: "Mini", name: "Seedance 2.0 Mini", mode: "reference" },
];
const QUEUE_MODE_NAMES = { text: ["T2V", "文生视频"], image: ["I2V", "图生视频"], reference: ["R2V", "参考生视频"] };
const QUEUE_REFRESH_MS = 90 * 1000;
const queueClock = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.25"/><path d="M8 4.6V8l2.3 1.5"/></svg>';

// Short form for the pill and list: "<1分", "29分", "5.3h", "12h".
function formatWaitShort(seconds) {
  const minutes = Math.round(Math.max(0, Number(seconds) || 0) / 60);
  if (minutes < 1) return "<1分";
  if (minutes < 60) return `${minutes}分`;
  const hours = minutes / 60;
  return hours < 10 ? `${Number(hours.toFixed(1))}h` : `${Math.round(hours)}h`;
}

// Colour by the rounded minutes shown, so "10分" is never amber: ≤10 min, ≤1 h, longer.
function waitLevel(seconds) {
  const minutes = Math.round(Math.max(0, Number(seconds) || 0) / 60);
  return minutes <= 10 ? "q-ok" : minutes <= 60 ? "q-warn" : "q-bad";
}

function queueBoardRows() {
  return QUEUE_BOARD.map((entry) => ({ ...entry, wait: queueState.models[entry.model]?.[entry.mode] }));
}

function queueBoardRoot() {
  let root = $("queueBoard");
  if (root) return root;
  const actions = document.querySelector(".topbar-actions");
  if (!actions) return null;
  root = document.createElement("div");
  root.id = "queueBoard";
  root.className = "queue-board hidden";
  root.innerHTML = `<button id="queueBoardToggle" class="queue-pill" type="button" aria-haspopup="true" aria-expanded="false" aria-controls="queueBoardPanel"></button><div id="queueBoardPanel" class="queue-panel hidden"></div>`;
  actions.prepend(root);
  return root;
}

const waitValueHtml = (seconds) => `<b class="${waitLevel(seconds)}">${esc(formatWaitShort(seconds))}</b>`;

function renderQueueBoard() {
  const root = queueBoardRoot();
  if (!root) return;
  const rows = queueBoardRows();
  const known = rows.filter((row) => row.wait);
  root.classList.toggle("hidden", !known.length);
  if (!known.length) {
    setQueueBoardOpen(false);
    return;
  }
  const seconds = known.map((row) => row.wait.seconds);
  const low = Math.min(...seconds);
  const high = Math.max(...seconds);
  const range = formatWaitShort(low) === formatWaitShort(high)
    ? waitValueHtml(low)
    : `${waitValueHtml(low)}<i>–</i>${waitValueHtml(high)}`;
  const toggle = $("queueBoardToggle");
  toggle.innerHTML = `${queueClock}<span class="queue-pill-label">排队</span><span class="queue-pill-range">${range}</span><svg class="queue-pill-caret" viewBox="0 0 10 10" aria-hidden="true"><path d="M2.5 3.75 5 6.25l2.5-2.5"/></svg>`;
  toggle.setAttribute("aria-label", `TikTok 当前排队预估 ${formatWait(low)} 到 ${formatWait(high)}，点击查看各模型`);

  const checkedAt = Math.max(...known.map((row) => Number(row.wait.checked_at) || 0));
  $("queueBoardPanel").innerHTML = `
    <p class="queue-panel-title">TikTok 当前排队预估</p>
    <div class="queue-panel-rows">${rows.map((row, index) => {
      const [mode, modeName] = QUEUE_MODE_NAMES[row.mode];
      const groupStart = index === 0 || rows[index - 1].model !== row.model;
      const title = `${row.name} · ${modeName} ${mode}${row.wait ? ` · 预计排队约 ${formatWait(row.wait.seconds)}` : " · 暂无预估"}`;
      return `<div class="queue-row${groupStart && index ? " group-start" : ""}" title="${esc(title)}">
        <span class="queue-row-model">${groupStart ? esc(row.label) : ""}</span>
        <span class="queue-row-mode">${mode}</span>
        <span class="queue-row-wait">${row.wait ? `${waitValueHtml(row.wait.seconds)}<span class="queue-dot ${waitLevel(row.wait.seconds)}"></span>` : '<b class="queue-none">—</b><span class="queue-dot"></span>'}</span>
      </div>`;
    }).join("")}</div>
    <p class="queue-panel-foot">${checkedAt ? `${esc(formatTime(checkedAt, true))} 更新 · ` : ""}每 90 秒刷新</p>`;
}

function setQueueBoardOpen(open) {
  const toggle = $("queueBoardToggle");
  const panel = $("queueBoardPanel");
  if (!toggle || !panel) return;
  panel.classList.toggle("hidden", !open);
  toggle.setAttribute("aria-expanded", String(open));
}

document.addEventListener("click", (event) => {
  const root = $("queueBoard");
  if (!root) return;
  if (event.target.closest("#queueBoardToggle")) {
    setQueueBoardOpen($("queueBoardPanel").classList.contains("hidden"));
  } else if (!root.contains(event.target)) {
    setQueueBoardOpen(false);
  }
});

document.addEventListener("keydown", (event) => {
  const panel = $("queueBoardPanel");
  if (event.key !== "Escape" || !panel || panel.classList.contains("hidden")) return;
  setQueueBoardOpen(false);
  $("queueBoardToggle").focus();
});

// The generate page already refreshes the estimates with its own polling; every other page
// refreshes them here, once signed in and while the tab is visible.
const consoleSignedIn = () => Boolean(state.key) && $("auth").classList.contains("hidden");

function refreshQueueBoardIfStale() {
  if (!consoleSignedIn() || document.hidden) return;
  if (Date.now() - (queueState.loadedAt || 0) < QUEUE_REFRESH_MS - 1000) return;
  refreshQueueWait();
}

queueBoardRoot();
setInterval(refreshQueueBoardIfStale, 15 * 1000);
document.addEventListener("visibilitychange", refreshQueueBoardIfStale);
new MutationObserver(refreshQueueBoardIfStale).observe($("auth"), { attributes: true, attributeFilter: ["class"] });
