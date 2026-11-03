// Cover thumbnails, in-page video preview and keyless download links for finished tasks.
// Loaded after admin.js and reuses its globals ($, esc, toast, state, stateMap, normalizeStatus).

const MEDIA_BUTTON_LABELS = { preview: "预览", "copy-link": "复制链接" };
let mediaPreviewTask = null;
// Survives the periodic table re-render, so an expanded prompt stays open.
const expandedPrompts = new Set();

function mediaTask(id) {
  return state.tasks.find((task) => task.id === id) || (mediaPreviewTask?.id === id ? mediaPreviewTask : null);
}

function mediaShareLink(task) {
  return new URL(task.share_url, location.origin).href;
}

function mediaThumb(task) {
  const image = task.poster_url ? `<img src="${esc(task.poster_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : "";
  if (!task.share_url) {
    const [label] = stateMap[normalizeStatus(task.status)] || [task.status || "—"];
    return `<span class="media-thumb">${image}<span class="media-thumb-label">${esc(label)}</span></span>`;
  }
  return `<button class="media-thumb playable" data-media-action="preview" data-task-id="${esc(task.id)}" type="button" aria-label="预览视频 ${esc(task.id)}">${image}<span class="media-thumb-play" aria-hidden="true">▶</span></button>`;
}

function mediaButton(task, action) {
  if (!task.share_url) return "";
  return `<button class="button ghost" data-media-action="${action}" data-task-id="${esc(task.id)}" type="button">${MEDIA_BUTTON_LABELS[action]}</button>`;
}

function mediaFocusPreview(id) {
  const task = state.tasks.find((item) => item.id === id);
  if (!task?.share_url) return "";
  return `<div class="media-focus">${mediaThumb(task)}<div class="row-actions">${mediaButton(task, "preview")}${mediaButton(task, "copy-link")}</div></div>`;
}

// Expansion is applied after rendering instead of being baked into the row HTML, so
// toggling a prompt does not make the next auto refresh re-render (and deselect) the table.
function mediaPrompt(task) {
  if (!task.prompt) return '<span class="task-prompt">无提示词</span>';
  const id = esc(task.id);
  return `<span class="task-prompt" data-prompt-action="toggle" data-task-id="${id}">${esc(task.prompt)}</span>`
    + `<span class="task-prompt-actions"><button class="text-button" data-prompt-action="toggle" data-task-id="${id}" type="button" hidden>展开全文</button>`
    + `<button class="text-button" data-prompt-action="copy" data-task-id="${id}" type="button">复制提示词</button></span>`;
}

// Re-applies expansion and only offers "展开全文" when the two-line clamp actually hides text.
function syncPrompts(root) {
  root.querySelectorAll(".task-prompt[data-prompt-action]").forEach((prompt) => {
    const expanded = expandedPrompts.has(prompt.dataset.taskId);
    prompt.classList.toggle("expanded", expanded);
    const toggle = prompt.nextElementSibling?.querySelector('[data-prompt-action="toggle"]');
    if (!toggle) return;
    toggle.textContent = expanded ? "收起" : "展开全文";
    toggle.hidden = !expanded && prompt.scrollHeight <= prompt.clientHeight + 1;
  });
}

function togglePrompt(id) {
  if (expandedPrompts.has(id)) expandedPrompts.delete(id);
  else expandedPrompts.add(id);
  syncPrompts(document);
}

async function copyPrompt(task) {
  if (await copyToClipboard(task.prompt)) toast("提示词已复制", `共 ${task.prompt.length} 个字符`);
  else window.prompt("无法自动复制，请手动复制提示词", task.prompt);
}

async function copyToClipboard(text) {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch { /* fall back to execCommand below */ }
  }
  // Plain-HTTP deployments have no Clipboard API. A modal dialog makes the rest of the page
  // inert, so the scratch field has to live inside the open dialog to stay selectable.
  const field = document.createElement("textarea");
  field.value = text;
  field.readOnly = true;
  field.className = "media-copy-field";
  (document.querySelector("dialog[open]") || document.body).append(field);
  field.select();
  let copied = false;
  try { copied = document.execCommand("copy"); } catch { copied = false; }
  field.remove();
  return copied;
}

async function copyShareLink(task) {
  const link = mediaShareLink(task);
  if (await copyToClipboard(link)) toast("下载链接已复制", link);
  else window.prompt("无法自动复制，请手动复制下载链接", link);
}

function mediaDialog() {
  const existing = $("mediaPreviewDialog");
  if (existing) return existing;
  const dialog = document.createElement("dialog");
  dialog.id = "mediaPreviewDialog";
  dialog.className = "media-dialog";
  dialog.innerHTML = `
    <div class="media-dialog-body">
      <div class="dialog-heading"><div><p class="eyebrow">PREVIEW</p><h2 class="mono media-dialog-title" data-media-title></h2></div><button class="icon-button" data-media-close type="button" aria-label="关闭">×</button></div>
      <div class="media-player">
        <video controls playsinline preload="auto"></video>
        <div class="media-player-status" role="status"></div>
      </div>
      <p class="media-dialog-prompt" data-media-prompt></p>
      <div class="dialog-actions">
        <a class="button ghost" data-media-download download>下载</a>
        <button class="button ghost" data-media-action="copy-link" type="button">复制链接</button>
        <button class="button primary" data-media-close type="button">关闭</button>
      </div>
    </div>`;
  document.body.append(dialog);
  const video = dialog.querySelector("video");
  video.addEventListener("loadeddata", () => { dialog.querySelector(".media-player-status").hidden = true; });
  video.addEventListener("error", () => { if (video.getAttribute("src")) showMediaError(dialog, video.getAttribute("src")); });
  dialog.addEventListener("close", () => {
    video.pause();
    video.removeAttribute("src");
    video.load();
    mediaPreviewTask = null;
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog || event.target.closest("[data-media-close]")) dialog.close();
  });
  return dialog;
}

function setMediaStatus(dialog, text, isError = false) {
  const status = dialog.querySelector(".media-player-status");
  status.hidden = false;
  status.textContent = text;
  status.classList.toggle("error", isError);
}

async function showMediaError(dialog, src) {
  let detail = "视频加载失败";
  try {
    const response = await fetch(src, { headers: { Range: "bytes=0-0" } });
    if (!response.ok) {
      const data = await response.json();
      detail = data?.error?.message || data?.detail || `HTTP ${response.status}`;
    }
  } catch { /* keep the generic message */ }
  if (dialog.querySelector("video").getAttribute("src") === src) setMediaStatus(dialog, `${detail}，请稍后重试或直接下载。`, true);
}

function openVideoPreview(task) {
  const dialog = mediaDialog();
  const video = dialog.querySelector("video");
  mediaPreviewTask = task;
  dialog.querySelector("[data-media-title]").textContent = task.id;
  dialog.querySelector("[data-media-prompt]").textContent = task.prompt || "";
  dialog.querySelector("[data-media-download]").href = task.share_url;
  dialog.querySelector("[data-media-download]").download = `${task.id}.mp4`;
  dialog.querySelector('[data-media-action="copy-link"]').dataset.taskId = task.id;
  setMediaStatus(dialog, "正在加载视频…首次播放需要先从 TikTok 拉取，可能要等几秒");
  if (task.poster_url) video.poster = task.poster_url;
  else video.removeAttribute("poster");
  video.src = task.share_url;
  if (!dialog.open) dialog.showModal();
  video.play().catch(() => { /* autoplay may be blocked; controls stay available */ });
}

document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-media-action]");
  if (!trigger) return;
  const task = mediaTask(trigger.dataset.taskId);
  if (!task?.share_url) return;
  if (trigger.dataset.mediaAction === "preview") openVideoPreview(task);
  if (trigger.dataset.mediaAction === "copy-link") copyShareLink(task);
});

document.addEventListener("error", (event) => {
  if (event.target instanceof HTMLImageElement && event.target.parentElement?.classList.contains("media-thumb")) event.target.remove();
}, true);

document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-prompt-action]");
  if (!trigger) return;
  const task = mediaTask(trigger.dataset.taskId);
  if (!task?.prompt) return;
  if (trigger.dataset.promptAction === "copy") copyPrompt(task);
  // A drag that selects prompt text also ends in a click; leave the selection alone.
  if (trigger.dataset.promptAction === "toggle" && !(trigger.classList.contains("task-prompt") && getSelection().toString())) togglePrompt(task.id);
});

const mediaTaskTable = $("tasks");
if (mediaTaskTable) new MutationObserver(() => syncPrompts(mediaTaskTable)).observe(mediaTaskTable, { childList: true });
window.addEventListener("resize", debounce(() => mediaTaskTable && syncPrompts(mediaTaskTable)));
