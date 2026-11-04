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
  try {
    const data = await api("/admin/queue-wait");
    queueState.models = data.models || {};
  } catch (error) {
    console.warn("queue wait", error);
    queueState.models = {};
  }
  renderModeWaits();
}

$("generationModel")?.addEventListener("change", renderModeWaits);
