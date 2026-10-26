// Cover thumbnails, in-page video preview and keyless download links for finished tasks.
// Loaded after admin.js and reuses its globals ($, esc, toast, state, stateMap, normalizeStatus).

const MEDIA_BUTTON_LABELS = { preview: "预览", "copy-link": "复制链接" };
let mediaPreviewTask = null;

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
