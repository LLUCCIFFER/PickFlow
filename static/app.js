/* PickFlow — 前端逻辑 v3.3 (照片墙) */

const $ = (id) => document.getElementById(id);
const VIEWS = ["landing", "processing", "prescreen", "preview", "arena", "done"];
const RECENT_KEY = "pic-arena.recent-folders";
const TUTORIAL_KEY = "pic-arena.tutorial-seen";
const CONFIRM_MOVE_KEY = "pic-arena.confirmed-move";
const CONFIRM_REAL_KEY = "pic-arena.confirmed-real";
const VERDICT_HOLD_MS = 380;

let busy = false;
let pollHandle = null;
let lastSession = null;
let currentGroup = null;
let currentMode = "move";
let recentWinners = []; // 最近胜出路径，给 arena-stack 用
let streamSeq = 0;       // streaming log 已渲染到的 event_seq

// ---- 处理页照片墙 ----
let wallCells = [];          // [{el, ev, addedAt}]
let wallQueue = [];          // 待渲染事件
let wallDrainHandle = null;
const WALL_CELL_COUNT = 40; // 10 columns × 4 rows
const WALL_FILL_MS = 200;
const WALL_REPLACE_MS = 420;
const WALL_QUEUE_CAP = 80;

// =================================================================
// 全局引擎状态徽章
// =================================================================
function setStatus(label, state = "idle") {
  // state: idle / busy / waiting / done / error
  const badge = $("status-badge");
  if (!badge) return;
  $("status-text").textContent = label;
  badge.classList.remove("is-busy", "is-waiting", "is-done", "is-error");
  if (state !== "idle") badge.classList.add(`is-${state}`);
}

// =================================================================
// 视图切换 + title + history
// =================================================================
function showView(name, push = true) {
  for (const v of VIEWS) $(`view-${v}`).classList.toggle("active", v === name);
  updateTitle(name);
  document.body.dataset.view = name;
  // 每次回到 landing 都把"开始"按钮复位——之前 handleStart 把它 disabled 后没复位，
  // 走完一次任务再回主页选新文件夹时会卡死，只能刷新页面才能再点。
  if (name === "landing") {
    const startBtn = document.getElementById("start-btn");
    if (startBtn) startBtn.disabled = false;
    const errEl = document.getElementById("start-error");
    if (errEl) errEl.textContent = "";
  }
  if (push && history.state?.view !== name) {
    history.pushState({ view: name }, "", location.pathname);
  }
}
function updateTitle(view) {
  const map = {
    landing: "PickFlow · 本地智能选片助手",
    processing: "分析中… · PickFlow",
    prescreen: "初筛复核 · PickFlow",
    preview: "分组预览 · PickFlow",
    arena: currentGroup ? `组 #${currentGroup.id_short} · PickFlow`
                        : "选片中 · PickFlow",
    done: "完成 · PickFlow",
  };
  document.title = map[view] || "PickFlow";
}
window.addEventListener("popstate", (e) => {
  const v = e.state?.view;
  if (v && VIEWS.includes(v)) showView(v, false);
});

// =================================================================
// 基础工具
// =================================================================
function basename(p) { return p ? p.split("/").pop() : ""; }
function shortenHome(p) {
  if (!p) return "";
  const home = navigator.platform.startsWith("Mac") ? "/Users/" : "/home/";
  if (p.startsWith(home)) {
    const rest = p.slice(home.length);
    const slash = rest.indexOf("/");
    if (slash > 0) return "~/" + rest.slice(slash + 1);
  }
  return p;
}
function imgUrl(path, width) {
  let u = `/api/image?path=${encodeURIComponent(path)}`;
  if (width) u += `&w=${width}`;
  return u;
}
function originalUrl(path) {
  return `/api/image_original?path=${encodeURIComponent(path)}`;
}

async function fetchJSON(url, opts = {}) {
  opts.headers = { ...(opts.headers || {}) };
  if (opts.body && !opts.headers["Content-Type"]) {
    opts.headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(url, opts);
  const text = await resp.text();
  let data = null;
  try { data = JSON.parse(text); } catch {}
  if (!resp.ok) {
    const msg = (data && data.error) || text || `HTTP ${resp.status}`;
    throw new Error(msg);
  }
  return data;
}

function fmtElapsed(s) {
  if (s == null || isNaN(s) || s < 0) return "";
  if (s < 60) return `${s.toFixed(1)} 秒`;
  const m = Math.floor(s / 60), r = Math.round(s - m * 60);
  return `${m} 分 ${r} 秒`;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 把 EXIF datetime 字符串归到拍摄半天名称
function halfDayLabel(dtStr) {
  if (!dtStr) return "未标注时间";
  const m = dtStr.match(/T(\d{2}):/);
  if (!m) return "未标注时间";
  const hr = parseInt(m[1], 10);
  if (hr < 6) return "凌晨";
  if (hr < 11) return "上午";
  if (hr < 14) return "中午";
  if (hr < 17) return "下午";
  if (hr < 20) return "傍晚";
  return "夜间";
}
function dateLabel(dtStr) {
  if (!dtStr) return "";
  const m = dtStr.match(/(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return "";
  return `${parseInt(m[2], 10)} 月 ${parseInt(m[3], 10)} 日`;
}

// =================================================================
// Toast
// =================================================================
let toastTimer = null;
function toast(msg, ms = 2400) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), ms);
}

// =================================================================
// 确认对话框（基于 Promise）
// =================================================================
function confirmDialog(title, body) {
  return new Promise((resolve) => {
    $("confirm-title").textContent = title;
    $("confirm-body").textContent = body;
    $("confirm").classList.remove("hidden");
    const cleanup = (ans) => {
      $("confirm").classList.add("hidden");
      $("confirm-ok").removeEventListener("click", ok);
      $("confirm-cancel").removeEventListener("click", cancel);
      resolve(ans);
    };
    const ok = () => cleanup(true);
    const cancel = () => cleanup(false);
    $("confirm-ok").addEventListener("click", ok);
    $("confirm-cancel").addEventListener("click", cancel);
  });
}

// =================================================================
// 全局"回主页"按钮：任意页面退出当前流程，清空已选文件夹
// =================================================================
async function goHome() {
  const onLanding = document.body.dataset.view === "landing";
  if (!onLanding) {
    const ok = await confirmDialog(
      "回到主页？",
      "当前任务会被取消，已选文件夹和未保存进度会清空。\n（winners/ losers/ 文件夹里已搬过去的图片不会动）"
    );
    if (!ok) return;
  }
  // 1. 后端重置
  try {
    await fetchJSON("/api/reset_session", { method: "POST" });
  } catch (e) {
    console.warn("reset_session 失败（继续清前端）:", e);
  }
  // 2. 清前端状态
  try {
    const fi = $("folder-input");
    if (fi) fi.value = "";
    $("folder-snapshot")?.classList.add("hidden");
    // 重置全局变量（防御性）
    if (typeof currentGroup !== "undefined") currentGroup = null;
    if (typeof lastSession !== "undefined") lastSession = null;
    // 停掉 job polling（processing 页面用的）
    if (typeof stopJobPolling === "function") stopJobPolling();
  } catch (e) {
    console.warn("前端状态清理异常:", e);
  }
  // 3. 切到 landing 视图
  showView("landing");
  setStatus("引擎就绪", "idle");
}

document.querySelectorAll(".btn-go-home").forEach(el => el.addEventListener("click", goHome));

// =================================================================
// 着陆页
// =================================================================
function loadRecent() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); }
  catch { return []; }
}
function pushRecent(path) {
  let rs = loadRecent().filter((p) => p !== path);
  rs.unshift(path);
  rs = rs.slice(0, 5);
  localStorage.setItem(RECENT_KEY, JSON.stringify(rs));
}
function renderRecent() {
  const wrap = $("recent-folders");
  if (!wrap) return;
  const rs = loadRecent();
  wrap.innerHTML = "";
  if (!rs.length) return;
  for (const p of rs) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "recent-chip";
    chip.textContent = shortenHome(p);
    chip.title = p;
    chip.addEventListener("click", () => {
      $("folder-input").value = p;
      requestFolderPeek(p);
    });
    wrap.appendChild(chip);
  }
}

function bindSlider(input, label) {
  const sync = () => label.textContent = input.value;
  input.addEventListener("input", sync);
  sync();
}
bindSlider($("thr-near"), $("thr-near-val"));
bindSlider($("thr-far"), $("thr-far-val"));
bindSlider($("thr-near-secs"), $("thr-near-secs-val"));

function currentEngine() {
  return "fast";
}
function syncPrescreenStrength() {
  const on = $("opt-prescreen").checked;
  $("prescreen-strength-row").classList.toggle("is-disabled", !on);
  const faceOpt = $("opt-face-aware");
  if (faceOpt && !faceOpt.dataset.unsupported) {
    faceOpt.disabled = true;
    faceOpt.parentElement?.classList.toggle("is-disabled", true);
  }
}
$("opt-prescreen").addEventListener("change", syncPrescreenStrength);
syncPrescreenStrength();

// 当前版本只保留极速模式；保留同步函数以兼容旧页面状态。
function syncEngineSwitch() {
  const engine = "fast";
  document.querySelectorAll(".engine-opt").forEach(el => {
    el.classList.toggle("is-active", el.dataset.engine === engine);
  });
  syncPrescreenStrength();
}
document.querySelectorAll('input[name="engine"]').forEach(el => {
  el.addEventListener("change", syncEngineSwitch);
});
// 整个 .engine-opt 块都可点击切换（不只点 radio）
document.querySelectorAll(".engine-opt").forEach(el => {
  el.addEventListener("click", () => {
    const radio = el.querySelector('input[type="radio"]');
    if (radio && !radio.checked) {
      radio.checked = true;
      syncEngineSwitch();
    }
  });
});

syncEngineSwitch();

// ---------- 文件夹快照（路径选好的瞬间触发） ----------
let peekTimer = null;
let peekToken = 0;
function requestFolderPeek(folder) {
  clearTimeout(peekTimer);
  if (!folder || folder.length < 2) {
    $("folder-snapshot").classList.add("hidden");
    setStatus("引擎就绪", "idle");
    return;
  }
  peekTimer = setTimeout(() => doFolderPeek(folder), 200);
}
async function doFolderPeek(folder) {
  const myToken = ++peekToken;
  setStatus("正在快速读取目录…", "busy");
  let r;
  try {
    r = await fetchJSON("/api/peek_folder", {
      method: "POST",
      body: JSON.stringify({ folder }),
    });
  } catch {
    if (myToken !== peekToken) return;
    setStatus("引擎就绪", "idle");
    return;
  }
  if (myToken !== peekToken) return;
  if (!r.ok || !r.count) {
    $("folder-snapshot").classList.add("hidden");
    setStatus(r.error || "未在该目录找到照片", "idle");
    return;
  }
  $("snap-count").textContent = r.count.toLocaleString();
  const period = r.latest ? `${r.earliest} – ${r.latest}` : r.earliest;
  $("snap-period").textContent = period;
  $("snap-active").textContent = r.active_period
    ? `主要在 ${r.active_period} 拍摄`
    : "";
  $("snap-size").textContent = r.size_text;
  $("snap-resume").textContent = r.span_days > 1 ? `跨 ${r.span_days} 天` : "";
  const sw = $("snap-samples");
  sw.innerHTML = "";
  (r.samples || []).slice(0, 3).forEach((p) => {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.src = imgUrl(p, 220);
    sw.appendChild(img);
  });
  sw.style.display = r.samples?.length ? "" : "none";
  $("folder-snapshot").classList.remove("hidden");
  setStatus(`已读取 ${r.count.toLocaleString()} 张 · 待处理`, "idle");
}

$("folder-input").addEventListener("input", (e) => {
  requestFolderPeek(e.target.value.trim());
});

const dropZone = $("folder-drop");
["dragover", "dragenter"].forEach((e) =>
  dropZone.addEventListener(e, (ev) => {
    ev.preventDefault();
    dropZone.classList.add("drag-over");
  })
);
["dragleave", "drop"].forEach((e) =>
  dropZone.addEventListener(e, (ev) => {
    ev.preventDefault();
    dropZone.classList.remove("drag-over");
  })
);
dropZone.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files?.[0];
  if (f && f.path) {
    $("folder-input").value = f.path;
    requestFolderPeek(f.path);
  } else {
    toast("浏览器无法直接获取文件夹路径，请粘贴绝对路径");
  }
});

async function handleStart(e) {
  if (e && e.preventDefault) e.preventDefault();
  const folder = $("folder-input").value.trim();
  const dry_run = false;  // 试运行入口已下线；后端仍兼容此参数
  // 一次性运行：每次 start 后端都会清掉 winners/losers/state，不再需要这个选项
  const wipe_cache = true;
  const mode = document.querySelector('input[name="mode"]:checked')?.value || "copy";
  const engine = currentEngine();
  const prescreen_enabled = $("opt-prescreen").checked;
  const prescreen_strength = document.querySelector('input[name="prescreen-strength"]:checked')?.value || "standard";
  const face_aware = false;
  const threshold_near = parseInt($("thr-near").value);
  const threshold_far = parseInt($("thr-far").value);
  const near_seconds = parseInt($("thr-near-secs").value) * 60;
  $("start-error").textContent = "";
  if (!folder) { $("start-error").textContent = "请填写文件夹路径"; return; }

  if (mode === "move" && !dry_run && !localStorage.getItem(CONFIRM_MOVE_KEY)) {
    const ok = await confirmDialog(
      "确认移动文件",
      '选择"移动"模式：原文件会被搬到 winners/ 与 losers/。强烈推荐"复制"模式以便反悔。继续？'
    );
    if (!ok) return;
    localStorage.setItem(CONFIRM_MOVE_KEY, "1");
  } else if (!dry_run && !localStorage.getItem(CONFIRM_REAL_KEY)) {
    const ok = await confirmDialog(
      "开始处理",
      `将在 ${folder}/winners 与 /losers 创建副本（复制模式）。继续？`
    );
    if (!ok) return;
    localStorage.setItem(CONFIRM_REAL_KEY, "1");
  }

  $("start-btn").disabled = true;
  try {
    const r = await fetchJSON("/api/start", {
      method: "POST",
      body: JSON.stringify({
        folder, dry_run, wipe_cache, mode, engine,
        threshold_near, threshold_far, near_seconds,
        prescreen_enabled, prescreen_strength, face_aware,
        llm_model: "",
      }),
    });
    pushRecent(folder);
    if (r && r.resumed) {
      await bootstrap();
    } else {
      currentMode = mode;
      enterProcessing(folder);
    }
  } catch (err) {
    $("start-error").textContent = err.message;
    $("start-btn").disabled = false;
    setStatus("启动失败", "error");
  }
}

$("start-btn").addEventListener("click", handleStart);
$("start-form").addEventListener("submit", handleStart);

$("browse-btn").addEventListener("click", async () => {
  const btn = $("browse-btn");
  btn.disabled = true;
  try {
    const r = await fetchJSON("/api/browse_folder", { method: "POST" });
    if (r.cancelled) return;
    if (r.folder) {
      $("folder-input").value = r.folder;
      $("start-error").textContent = "";
      requestFolderPeek(r.folder);
    }
  } catch (e) {
    toast("无法打开选择对话框：" + e.message);
  } finally {
    btn.disabled = false;
  }
});
$("folder-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); handleStart(e); }
});

// =================================================================
// 处理进度页
// =================================================================
function enterProcessing(folder) {
  showView("processing");
  $("proc-folder").textContent = folder;
  $("proc-title").textContent = "正在过目每张照片";
  $("proc-error").textContent = "";
  $("progress-fill").classList.add("indeterminate");
  $("progress-fill").style.width = "0%";
  $("progress-count").textContent = "—";
  $("progress-pct").textContent = "—";
  $("progress-label").textContent = "扫描文件夹…";
  const hint = document.getElementById("first-run-hint");
  if (hint) hint.classList.add("hidden");
  $("proc-eta").textContent = "";
  $("proc-counter-scanned").textContent = "0";
  $("proc-counter-total").textContent = "—";
  $("proc-counter-rejected").textContent = "0";
  streamSeq = 0;
  const track = document.getElementById("proc-terminal-track");
  if (track) { track.innerHTML = ""; track.style.transform = "translateY(0)"; }
  buildPhotoWall();
  setStatus("分析中", "busy");
  startJobPolling();
}

// ---------- 照片墙 ----------
const CAT_LABEL = {
  blur: "BLUR",
  eyes: "EYES",
  exposure: "EXPOSURE",
  comp: "FRAMING",
  format: "FORMAT",
  other: "OTHER",
};

function buildPhotoWall() {
  const wall = $("proc-wall");
  if (!wall) return;
  wall.innerHTML = "";
  wall.classList.remove("collecting");
  collectingStarted = false;
  wallCells = [];
  wallQueue = [];
  for (let i = 0; i < WALL_CELL_COUNT; i++) {
    const cell = document.createElement("div");
    cell.className = "wall-cell";
    cell.innerHTML = `
      <img class="wall-image" alt="">
      <div class="wall-overlay">
        <div class="overlay-text">
          <span class="overlay-cat"></span>
          <span class="overlay-reason"></span>
        </div>
      </div>`;
    wall.appendChild(cell);
    wallCells.push({ el: cell, ev: null, addedAt: 0 });
  }
  startWallDrain();
}

function startWallDrain() {
  stopWallDrain();
  scheduleNextDrain();
}
function stopWallDrain() {
  if (wallDrainHandle) { clearTimeout(wallDrainHandle); wallDrainHandle = null; }
}
function scheduleNextDrain() {
  // 自适应间隔：还有空格走快节奏（填充感），全填满后走慢节奏（停留感）
  const hasEmpty = wallCells.some(c => c.ev === null);
  const delay = hasEmpty ? WALL_FILL_MS : WALL_REPLACE_MS;
  wallDrainHandle = setTimeout(() => {
    drainWall();
    scheduleNextDrain();
  }, delay);
}

function classifyReason(reason) {
  if (!reason) return "other";
  if (/失焦|焦点|模糊|未跟上/.test(reason)) return "blur";
  if (/闭眼|眨眼/.test(reason)) return "eyes";
  if (/曝光|高光|过暗|过亮|溢出/.test(reason)) return "exposure";
  if (/被切|反差|信息|缺少/.test(reason)) return "comp";
  if (/截图|尺寸|文件异常|非拍摄/.test(reason)) return "format";
  return "other";
}

function pickWallCell() {
  // 还有空格：随机挑一个空格（"逐渐填满"感）
  const empties = wallCells.filter(c => c.ev === null);
  if (empties.length > 0) {
    return empties[Math.floor(Math.random() * empties.length)];
  }
  // 全占了 → 严格 FIFO，最先放进来的最先被换掉
  let oldest = wallCells[0];
  for (const c of wallCells) {
    if (c.addedAt < oldest.addedAt) oldest = c;
  }
  return oldest;
}

function renderWallCell(cell, ev) {
  // 立刻占位（避免下个 tick 重复挑同一格），但视觉上仍保持上一态直到图加载完
  cell.ev = ev;
  cell.addedAt = performance.now();
  const wasLoaded = cell.el.classList.contains("loaded");
  if (wasLoaded) cell.el.classList.add("leaving");
  const startDelay = wasLoaded ? 220 : 0;
  setTimeout(() => preloadAndPaint(cell, ev), startDelay);
}

function preloadAndPaint(cell, ev) {
  // 还是这个事件吗？（drain 太快可能已经被替换）
  if (cell.ev !== ev) return;
  const url = imgUrl(ev.path, 260);
  const probe = new Image();
  const done = () => {
    if (cell.ev !== ev) return;
    paintWallCell(cell, ev, url);
  };
  probe.onload = done;
  probe.onerror = done;  // 加载失败也走 paint：至少蒙层 + 原因显示
  probe.src = url;
}

function paintWallCell(cell, ev, url) {
  const img = cell.el.querySelector(".wall-image");
  const catEl = cell.el.querySelector(".overlay-cat");
  const reasonEl = cell.el.querySelector(".overlay-reason");

  // 清掉旧分类 / 状态 class
  cell.el.className = "wall-cell";

  if (ev.reject) {
    const cat = classifyReason(ev.reason);
    cell.el.classList.add("rejected", "cat-" + cat);
    catEl.textContent = CAT_LABEL[cat] || "";
    reasonEl.textContent = ev.reason || "失败";
  } else if (!ev.ok) {
    cell.el.classList.add("error");
    catEl.textContent = "UNREAD";
    reasonEl.textContent = ev.reason || "未能读取";
  } else {
    cell.el.classList.add("ok");
    catEl.textContent = "";
    reasonEl.textContent = "";
  }

  // 设 src（probe 已预加载，浏览器命中缓存秒出）
  img.src = url;
  // 下一帧加 loaded，让 CSS transition 平滑接管
  requestAnimationFrame(() => {
    if (cell.ev !== ev) return;
    cell.el.classList.add("loaded");
  });
}

function drainWall() {
  // 队列过长：保留尾部，丢老的——避免显示远落后于实际进度
  if (wallQueue.length > WALL_QUEUE_CAP) {
    wallQueue = wallQueue.slice(-WALL_QUEUE_CAP);
  }
  if (!wallQueue.length) return;
  const cell = pickWallCell();
  const ev = wallQueue.shift();
  renderWallCell(cell, ev);
}

function appendStreamEvent(ev) {
  wallQueue.push(ev);
  pushTerminalLine(ev);
}

let collectingStarted = false;
function startCollectingAnimation() {
  if (collectingStarted) return;
  collectingStarted = true;
  stopWallDrain();
  const wall = $("proc-wall");
  if (!wall) return;
  wall.classList.add("collecting");
  const rect = wall.getBoundingClientRect();
  const cx = rect.width / 2;
  const cy = rect.height / 2;
  const loaded = wallCells.filter(c => c.el.classList.contains("loaded"));
  loaded.forEach((c, i) => {
    const cr = c.el.getBoundingClientRect();
    const dx = cx - (cr.left - rect.left + cr.width / 2);
    const dy = cy - (cr.top - rect.top + cr.height / 2);
    const rot = (Math.random() - 0.5) * 16;
    const delay = i * 25;
    c.el.style.transitionDelay = `${delay}ms`;
    c.el.style.transform = `translate(${dx}px, ${dy}px) scale(0.13) rotate(${rot}deg)`;
    c.el.style.opacity = '0.7';
  });
  const unloaded = wallCells.filter(c => !c.el.classList.contains("loaded"));
  unloaded.forEach(c => { c.el.style.opacity = '0'; });
  let overlay = wall.querySelector(".proc-wall-deck-overlay");
  if (!overlay) {
    overlay = document.createElement("div");
    overlay.className = "proc-wall-deck-overlay";
    overlay.innerHTML = '<div class="deck-label">正在整理分组…</div>';
    wall.appendChild(overlay);
  }
  setTimeout(() => overlay.classList.add("visible"), loaded.length * 25 + 400);
}

// ---------- 终端式滚动条 ----------
const TERMINAL_MAX = 60;
const TERMINAL_VISIBLE = 7;
const TERMINAL_EXPANDED = 14;

(function initTerminalToggle() {
  document.addEventListener("click", (e) => {
    const term = e.target.closest("#proc-terminal");
    if (!term) return;
    term.classList.toggle("expanded");
    const track = document.getElementById("proc-terminal-track");
    if (!track || !track.children.length) return;
    const vis = term.classList.contains("expanded") ? TERMINAL_EXPANDED : TERMINAL_VISIBLE;
    const overflow = Math.max(0, track.children.length - vis);
    const lineH = track.children[0].getBoundingClientRect().height || (12 * 1.55);
    track.style.transform = `translateY(-${overflow * lineH}px)`;
  });
})();
function fmtExif(ev) {
  const parts = [];
  if (ev.shutter) parts.push(ev.shutter);
  if (ev.aperture) parts.push("f/" + ev.aperture);
  if (ev.iso) parts.push("ISO" + ev.iso);
  return parts.length ? parts.join(" ") : "—";
}
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function pushTerminalLine(ev) {
  const track = document.getElementById("proc-terminal-track");
  if (!track) return;
  const row = document.createElement("div");
  row.className = "proc-terminal-line " +
    (ev.ok ? "is-pass" : (ev.reject ? "is-reject" : "is-fail"));
  // 后端按 engine 给 signals=[{kind,label,value}, ...]；前端只负责按 kind 着色渲染
  const signals = Array.isArray(ev.signals) ? ev.signals : [];
  const signalHtml = signals.map(s =>
    `<span class="tt-sig tt-${escapeHtml(s.kind || "sig")}">` +
    `${escapeHtml(s.label || "")} ${escapeHtml(s.value || "—")}` +
    `</span>`
  ).join("");
  row.innerHTML =
    `<span class="tt-name" title="${escapeHtml(ev.name || "")}">${escapeHtml(ev.name || "—")}</span>` +
    `<span class="tt-exif">${escapeHtml(fmtExif(ev))}</span>` +
    signalHtml +
    `<span class="tt-verdict">${escapeHtml(ev.verdict || "—")}</span>`;
  track.appendChild(row);
  while (track.children.length > TERMINAL_MAX) {
    track.removeChild(track.firstChild);
  }
  const term = document.getElementById("proc-terminal");
  const vis = (term && term.classList.contains("expanded")) ? TERMINAL_EXPANDED : TERMINAL_VISIBLE;
  const overflow = Math.max(0, track.children.length - vis);
  const lineH = row.getBoundingClientRect().height || (12 * 1.55);
  track.style.transform = `translateY(-${overflow * lineH}px)`;
}

function startJobPolling() {
  stopJobPolling();
  jobPollFailStreak = 0;
  // C1 修复：250ms 太密——每秒 4 次 / 200 张图 800 次轮询，每次都拿 LOCK
  // 和分组线程抢；放宽到 600ms，体感不变但后台吞吐更稳。
  pollHandle = setInterval(refreshJob, 600);
  refreshJob();
}
function stopJobPolling() {
  if (pollHandle) { clearInterval(pollHandle); pollHandle = null; }
  jobPollFailStreak = 0;
}

// B1 修复：连续 N 次 /api/job 失败就弹 toast 让用户知道（服务端崩 / 网断）
// 避免 UI 永远卡在 processing 状态。
let jobPollFailStreak = 0;
let jobPollFailToasted = false;
const JOB_POLL_FAIL_THRESHOLD = 8;  // ≈ 5 秒（8 × 600ms）

function bumpCounter(elId, value) {
  const el = $(elId);
  if (!el) return;
  const old = el.textContent;
  const newStr = (typeof value === "number") ? value.toLocaleString() : value;
  if (old !== newStr) {
    el.textContent = newStr;
    el.classList.add("pop");
    setTimeout(() => el.classList.remove("pop"), 240);
  }
}

async function refreshJob() {
  let j;
  try {
    j = await fetchJSON(`/api/job?since=${streamSeq}`);
    if (jobPollFailStreak > 0 && jobPollFailToasted) {
      toast("连接已恢复");
    }
    jobPollFailStreak = 0;
    jobPollFailToasted = false;
  } catch {
    jobPollFailStreak += 1;
    if (jobPollFailStreak === JOB_POLL_FAIL_THRESHOLD && !jobPollFailToasted) {
      jobPollFailToasted = true;
      toast("和后台失联了——检查服务是否还在运行，或刷新页面");
    }
    return;
  }
  if (j.status === "idle") return;

  // 增量送入照片墙队列
  if (Array.isArray(j.events) && j.events.length) {
    for (const ev of j.events) {
      if (ev.seq <= streamSeq) continue;
      appendStreamEvent(ev);
      streamSeq = ev.seq;
    }
  }
  // 实时检出失败计数
  if (typeof j.rejected_running === "number") {
    bumpCounter("proc-counter-rejected", j.rejected_running);
  }

  if (j.status === "error") {
    stopJobPolling();
    $("proc-error").textContent = "处理失败：" + (j.error || "未知错误");
    $("progress-fill").classList.remove("indeterminate");
    $("start-btn").disabled = false;
    setStatus("处理失败", "error");
    return;
  }
  if (j.status === "cancelled") {
    stopJobPolling();
    toast("已中止");
    setStatus("已中止", "idle");
    setTimeout(() => {
      showView("landing");
      $("start-btn").disabled = false;
    }, 400);
    return;
  }

  $("proc-elapsed").textContent = j.elapsed > 0 ? `已用 ${fmtElapsed(j.elapsed)}` : "";

  if (j.total > 0) {
    const pct = Math.min(100, Math.round((j.done / j.total) * 100));
    $("progress-fill").classList.remove("indeterminate");
    $("progress-fill").style.width = pct + "%";
    $("progress-count").textContent = `${j.done} / ${j.total}`;
    $("progress-pct").textContent = pct + "%";
    bumpCounter("proc-counter-scanned", j.done);
    bumpCounter("proc-counter-total", j.total);
    // 真有进度了 = 依赖装完 + 扫描完毕，藏起首次运行提示
    document.getElementById("first-run-hint")?.classList.add("hidden");
    setStatus(`分析中 · ${j.done.toLocaleString()} / ${j.total.toLocaleString()}`, "busy");

    if (j.done >= 5 && j.status !== "done" && j.elapsed > 1) {
      const speed = j.done / j.elapsed;
      const remain = (j.total - j.done) / speed;
      $("proc-eta").textContent = `预计还剩 ${fmtElapsed(remain)}`;
    } else if (j.status === "done") {
      $("proc-eta").textContent = "";
    }
  } else {
    $("progress-fill").classList.add("indeterminate");
    $("progress-count").textContent = "—";
    $("progress-pct").textContent = "—";
  }
  $("progress-label").textContent = j.label || "";
  if (j.status === "grouping") {
    $("progress-label").textContent = "正在组连拍…";
    startCollectingAnimation();
  }

  if (j.skipped_count > 0) {
    $("skip-notice").classList.remove("hidden");
    $("skip-num").textContent = j.skipped_count;
    const list = $("skip-list");
    list.innerHTML = "";
    (j.skipped_sample || []).forEach((s) => {
      const li = document.createElement("li");
      li.textContent = `${basename(s.path)} — ${s.reason}`;
      li.title = s.path;
      list.appendChild(li);
    });
    if (j.skipped_count > (j.skipped_sample || []).length) {
      const li = document.createElement("li");
      li.textContent = `… 还有 ${j.skipped_count - (j.skipped_sample || []).length} 张`;
      list.appendChild(li);
    }
  } else {
    $("skip-notice").classList.add("hidden");
  }

  if (j.status === "done") {
    stopJobPolling();
    if (j.mode) currentMode = j.mode;
    try {
      const s = await fetchJSON("/api/status");
      bumpCounter("proc-counter-rejected", s.prescreen_auto_rejected_count || 0);
    } catch {}
    // 等照片墙队列消化得差不多再切（最多 4s；用户也可以看到一张满墙）
    const tEnd = performance.now() + 4000;
    while (wallQueue.length > 0 && performance.now() < tEnd) {
      await sleep(120);
    }
    stopWallDrain();
    setTimeout(enterPrescreenOrArena, 800);
  }
}

$("btn-cancel-job").addEventListener("click", async () => {
  try { await fetchJSON("/api/cancel_job", { method: "POST" }); }
  catch (e) { toast("中止失败：" + e.message); }
});

// =================================================================
// 智能初筛复核页
// =================================================================
let prescreenItems = [];
let prescreenFilter = "__all__";

function shouldShowPrescreen(status) {
  return !!(
    status &&
    status.prescreen_enabled &&
    !status.prescreen_reviewed &&
    !status.selection_started
  );
}

async function enterPrescreenOrArena() {
  const s = await fetchJSON("/api/status");
  if (!s.ready) { showView("landing"); return; }
  if (shouldShowPrescreen(s)) {
    // 0 拒 → 没东西可复核，直接进分组+锦标赛
    if ((s.prescreen_pending_count || 0) === 0) {
      await confirmPrescreenAndContinue();
      return;
    }
    await enterPrescreen(s);
  } else if (s.finished_groups >= s.total_groups) {
    enterDone(s);
  } else {
    enterArena();
  }
}

function updatePrescreenStats(status, items) {
  const total = status.image_count || 0;
  const rejected = status.prescreen_auto_rejected_count || items.length || 0;
  const restored = items.filter((item) => item.restored).length;
  const pending = Math.max(0, rejected - restored);
  $("ps-total").textContent = total;
  $("ps-rejected").textContent = rejected;
  $("ps-pending").textContent = pending;
  $("ps-entering").textContent = restored;
  $("prescreen-title").textContent = rejected
    ? `${rejected} 张看起来可以放手`
    : "没有检出可疑照片";
  $("prescreen-sub").textContent = rejected
    ? `如有想留下的，点一下保留；其余将归入 losers/，完成页仍可召回。`
    : "扫描没发现明显问题，可直接进入选片。";
  setStatus(rejected
    ? `初筛复核 · 待复核 ${pending} 张`
    : `初筛完成 · 无可疑照片`, "waiting");
}

function renderPrescreenChips(items) {
  const wrap = $("prescreen-chips");
  wrap.innerHTML = "";
  if (!items.length) return;
  const buckets = new Map();
  buckets.set("__all__", items.length);
  for (const it of items) {
    const r = it.reason || "其他";
    buckets.set(r, (buckets.get(r) || 0) + 1);
  }
  const order = ["__all__", ...Array.from(buckets.keys()).filter((k) => k !== "__all__")
    .sort((a, b) => buckets.get(b) - buckets.get(a))];
  for (const key of order) {
    const chip = document.createElement("button");
    chip.type = "button";
    const cat = key === "__all__" ? "" : classifyReason(key);
    chip.className = "prescreen-chip" + (cat ? ` cat-${cat}` : "");
    if (key === prescreenFilter) chip.classList.add("active");
    const label = key === "__all__" ? "全部" : key;
    chip.innerHTML = `${label}<span class="chip-count">${buckets.get(key)}</span>`;
    chip.addEventListener("click", () => {
      prescreenFilter = key;
      renderPrescreenChips(prescreenItems);
      renderPrescreenGrid();
    });
    wrap.appendChild(chip);
  }
}

function renderPrescreenGrid() {
  const grid = $("prescreen-auto-grid");
  grid.innerHTML = "";
  const items = prescreenFilter === "__all__"
    ? prescreenItems
    : prescreenItems.filter((it) => (it.reason || "其他") === prescreenFilter);
  if (!items.length) {
    grid.innerHTML = '<div class="winners-empty">这个分类下没有照片</div>';
    return;
  }
  items.forEach((item, i) => {
    const card = document.createElement("button");
    card.type = "button";
    const cat = classifyReason(item.reason);
    card.className = `auto-reject-card cat-${cat}`;
    if (item.restored) card.classList.add("is-restored");
    card.style.animationDelay = `${Math.min(i * 18, 500)}ms`;
    card.disabled = !!item.restored;
    card.innerHTML = `
      <img loading="lazy" src="${imgUrl(item.path, 520)}" alt="${item.name}">
      <span class="ar-reason">${item.restored ? "已保留" : item.reason}</span>
      <span class="ar-name">${item.name}</span>`;
    card.addEventListener("click", async () => {
      if (card.disabled) return;
      card.disabled = true;
      card.classList.add("is-busy");
      try {
        await fetchJSON("/api/restore_rejected", {
          method: "POST",
          body: JSON.stringify({ group_id: item.group_id, path: item.path }),
        });
        item.restored = true;
        card.classList.remove("is-busy");
        card.classList.add("is-restored");
        card.querySelector(".ar-reason").textContent = "已保留";
        const s = await fetchJSON("/api/status");
        updatePrescreenStats(s, prescreenItems);
        toast("已保留，将进入选片");
      } catch (err) {
        card.disabled = false;
        card.classList.remove("is-busy");
        toast("保留失败：" + err.message);
      }
    });
    grid.appendChild(card);
  });
}

async function enterPrescreen(status) {
  showView("prescreen");
  const s = status || (await fetchJSON("/api/status"));
  lastSession = s;
  prescreenFilter = "__all__";
  try {
    const data = await fetchJSON("/api/auto_rejected");
    prescreenItems = data.items || [];
  } catch {
    prescreenItems = [];
  }
  $("prescreen-auto-count").textContent = prescreenItems.length ? `${prescreenItems.length} 张` : "";
  $("prescreen-auto-section").classList.toggle("hidden", !prescreenItems.length);
  renderPrescreenChips(prescreenItems);
  renderPrescreenGrid();
  updatePrescreenStats(s, prescreenItems);
}

async function confirmPrescreenAndContinue() {
  setStatus("整理分组中…", "busy");
  const overlay = $("grouping-overlay");
  const slotsEl = $("grp-slots");
  const stripEl = $("grp-strip");
  if (!overlay) return;

  // -- Phase 0: 全屏开场动画（intro-phase 标志位让 CSS 把 header 居中、放大、显示 spinner）--
  // 防御性 scroll-to-top：用户可能滚到 prescreen 底部点的按钮
  try { window.scrollTo({ top: 0, left: 0, behavior: "instant" }); }
  catch { window.scrollTo(0, 0); }
  document.body.scrollTop = 0;
  document.documentElement.scrollTop = 0;

  overlay.classList.remove("hidden");
  overlay.classList.add("intro-phase");
  slotsEl.innerHTML = "";
  stripEl.innerHTML = "";
  $("grp-title").textContent = "正在整理分组";
  $("grp-sub").textContent = "分析照片相似度，按主体智能合并";
  // 留一帧让 display 生效再开始 opacity transition
  requestAnimationFrame(() => overlay.classList.add("visible"));

  // 给开场动画 1.2 秒展示，再退回顶部模式
  const introPromise = sleep(1200);

  // -- Phase 1: 发起异步分组（后端立即返回） --
  const resp = await fetchJSON("/api/confirm_prescreen", { method: "POST" });

  if (!resp.async) {
    // 已有 groups（resume 等场景），走快路径
    const s = await fetchJSON("/api/status");
    await introPromise;  // 保证开场动画至少展示完
    overlay.classList.remove("intro-phase");
    overlay.classList.remove("visible");
    await sleep(400);
    overlay.classList.add("hidden");
    if (s.finished_groups >= s.total_groups) enterDone(s);
    else enterArena();
    return;
  }

  // 等开场动画完整跑完再收顶 → 让用户清楚感知"分组开始了"
  await introPromise;
  overlay.classList.remove("intro-phase");
  // 收顶后副标题换成更准确的进度文案
  $("grp-title").textContent = "正在分析相似度…";
  $("grp-sub").textContent = "";

  // -- Phase 3: 立即构建底部照片流（不等分组完成）--
  const allPaths = resp.all_paths || [];
  const stripImgEls = {};
  const stripPaths = [...allPaths, ...allPaths];
  stripPaths.forEach(p => {
    const img = document.createElement("img");
    img.className = "grp-strip-img";
    img.src = imgUrl(p, 120);
    img.dataset.path = p;
    stripEl.appendChild(img);
    if (!stripImgEls[p]) stripImgEls[p] = [];
    stripImgEls[p].push(img);
  });
  stripEl.style.animationDuration = Math.max(10, allPaths.length * 0.8) + "s";

  // -- Phase 4: 轮询分组进度，实时发牌 --
  let since = 0;
  let slotCount = 0;
  let groupingDone = false;
  let groupingError = false;
  const maxSlots = 12;

  async function animateGroup(g) {
    if (slotCount >= maxSlots) return;
    const slot = document.createElement("div");
    slot.className = "grp-slot";
    const label = document.createElement("span");
    label.className = "grp-slot-label";
    label.textContent = `${g.size} 张`;
    slot.appendChild(label);
    slotsEl.appendChild(slot);
    slotCount++;
    await sleep(50);
    slot.classList.add("active");
    await sleep(100);

    const photos = g.samples.slice(0, 4);
    const slotRect = slot.getBoundingClientRect();

    for (let pi = 0; pi < photos.length; pi++) {
      const p = photos[pi];
      (stripImgEls[p] || []).forEach(el => el.classList.add("highlight"));

      const stripImg = (stripImgEls[p] || [])[0];
      const fromRect = stripImg
        ? stripImg.getBoundingClientRect()
        : { left: window.innerWidth / 2, top: window.innerHeight - 45, width: 56, height: 74 };

      const fly = document.createElement("div");
      fly.className = "grp-flying";
      const flyImg = document.createElement("img");
      flyImg.src = imgUrl(p, 260);
      fly.appendChild(flyImg);
      fly.style.left = fromRect.left + "px";
      fly.style.top = fromRect.top + "px";
      fly.style.width = fromRect.width + "px";
      fly.style.height = fromRect.height + "px";
      fly.style.transform = "rotate(0deg)";
      document.body.appendChild(fly);

      await sleep(20);
      const stackOff = pi * 3;
      fly.style.left = (slotRect.left + stackOff) + "px";
      fly.style.top = (slotRect.top + stackOff) + "px";
      fly.style.width = slotRect.width + "px";
      fly.style.height = slotRect.height + "px";
      fly.style.transform = `rotate(${(Math.random() - 0.5) * 6}deg)`;

      await sleep(350);

      const card = document.createElement("div");
      card.className = "grp-slot-card";
      card.style.transform = `rotate(${(Math.random() - 0.5) * 4}deg)`;
      card.style.zIndex = pi;
      const cImg = document.createElement("img");
      cImg.src = imgUrl(p, 260);
      card.appendChild(cImg);
      slot.appendChild(card);
      slot.classList.add("has-cards");

      fly.style.opacity = "0";
      setTimeout(() => fly.remove(), 300);

      (stripImgEls[p] || []).forEach(el => {
        el.classList.remove("highlight");
        el.classList.add("used");
      });

      await sleep(50);
    }
  }

  // 动画队列：收到的组排队等待动画
  const animQueue = [];
  let animRunning = false;

  async function drainAnimQueue() {
    if (animRunning) return;
    animRunning = true;
    while (animQueue.length > 0) {
      const g = animQueue.shift();
      await animateGroup(g);
      await sleep(80);
    }
    animRunning = false;
  }

  // 轮询循环
  while (!groupingDone) {
    await sleep(150);
    try {
      const prog = await fetchJSON(`/api/grouping_progress?since=${since}`);
      if (prog.groups && prog.groups.length > 0) {
        $("grp-title").textContent = "正在整理分组…";
        for (const g of prog.groups) {
          animQueue.push(g);
          since++;
        }
        drainAnimQueue();
      }
      if (prog.status === "done") {
        groupingDone = true;
        $("grp-sub").textContent =
          `${prog.multi} 组连拍 · ${allPaths.length} 张照片`;
      } else if (prog.status === "error") {
        groupingDone = true;
        groupingError = true;
        $("grp-title").textContent = "分组失败";
        $("grp-sub").textContent = prog.error || "未知错误";
      }
    } catch { /* retry */ }
  }

  // 等待剩余动画完成
  while (animQueue.length > 0 || animRunning) {
    await sleep(100);
  }

  if (groupingError) {
    await sleep(2000);
    overlay.classList.remove("visible");
    await sleep(450);
    overlay.classList.add("hidden");
    document.querySelectorAll(".grp-flying").forEach(el => el.remove());
    toast("分组失败，请重试");
    return;
  }

  $("grp-title").textContent = "分组完成";
  await sleep(800);
  overlay.classList.remove("visible");
  await sleep(450);
  overlay.classList.add("hidden");
  document.querySelectorAll(".grp-flying").forEach(el => el.remove());

  const s = await fetchJSON("/api/status");
  if (s.total_groups > 0 && s.finished_groups >= s.total_groups) enterDone(s);
  else enterArena();
}

$("btn-prescreen-continue").addEventListener("click", async () => {
  try { await confirmPrescreenAndContinue(); }
  catch (e) { toast("继续失败：" + e.message); }
});
$("btn-prescreen-skip").addEventListener("click", async () => {
  try { await confirmPrescreenAndContinue(); }
  catch (e) { toast("继续失败：" + e.message); }
});
$("btn-prescreen-all").addEventListener("click", async () => {
  const pending = prescreenItems.filter((item) => !item.restored);
  if (!pending.length) { toast("没有可保留的照片"); return; }
  const btn = $("btn-prescreen-all");
  btn.disabled = true;
  btn.textContent = "处理中…";
  try {
    for (const item of pending) {
      await fetchJSON("/api/restore_rejected", {
        method: "POST",
        body: JSON.stringify({ group_id: item.group_id, path: item.path }),
      });
      item.restored = true;
    }
    const s = await fetchJSON("/api/status");
    renderPrescreenGrid();
    renderPrescreenChips(prescreenItems);
    updatePrescreenStats(s, prescreenItems);
    toast(`已保留 ${pending.length} 张`);
  } catch (e) {
    toast("全部保留失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "全部保留 · 我自己看";
  }
});

// =================================================================
// 分组预览页
// =================================================================
async function enterPreview(status) {
  const s = status || (await fetchJSON("/api/status"));
  if (!s.ready) { showView("landing"); return; }
  if (shouldShowPrescreen(s)) {
    if ((s.prescreen_pending_count || 0) === 0) {
      await confirmPrescreenAndContinue();
      return;
    }
    await enterPrescreen(s);
    return;
  }
  if (s.selection_started) { enterArena(); return; }

  showView("preview");
  $("prv-near").value = s.threshold_near;
  $("prv-far").value = s.threshold_far;
  $("prv-secs").value = Math.round(s.near_seconds / 60);
  $("prv-near-val").textContent = $("prv-near").value;
  $("prv-far-val").textContent = $("prv-far").value;
  $("prv-secs-val").textContent = $("prv-secs").value;
  await refreshPreview(s);
}

function fmtBurstSpan(seconds) {
  if (!seconds || seconds <= 0) return "";
  if (seconds < 5) return `约 ${seconds.toFixed(1)} 秒内`;
  if (seconds < 60) return `${Math.round(seconds)} 秒内`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟内`;
  return `${(seconds / 3600).toFixed(1)} 小时内`;
}

async function refreshPreview(status) {
  const s = status || (await fetchJSON("/api/status"));
  $("preview-title").textContent =
    `${s.multi_groups} 组连拍 · ${s.image_count} 张待选`;
  $("preview-sub").textContent = "金边那张是 AI 候选——你可以推翻。如果分组不太对，下面可以调阈值。";
  setStatus(`分组就绪 · ${s.multi_groups} 组待决`, "waiting");

  const data = await fetchJSON("/api/preview_groups");
  const grid = $("preview-grid");
  grid.innerHTML = "";
  if (!data.groups.length) {
    grid.innerHTML = '<div class="winners-empty">没有需要选片的相似组（每张都独立成组）</div>';
    return;
  }

  // 按拍摄半天分章节
  const sections = new Map();
  data.groups.forEach((g) => {
    const key = g.earliest_dt
      ? `${dateLabel(g.earliest_dt)} · ${halfDayLabel(g.earliest_dt)}`
      : "未标注时间";
    if (!sections.has(key)) sections.set(key, []);
    sections.get(key).push(g);
  });

  for (const [chapter, groups] of sections.entries()) {
    const head = document.createElement("div");
    head.className = "album-chapter";
    head.innerHTML = `
      <span class="album-chapter-name">${chapter}</span>
      <span class="album-chapter-meta">${groups.length} 组</span>`;
    grid.appendChild(head);

    groups.forEach((g) => {
      const card = document.createElement("div");
      card.className = "preview-card";
      const samples = g.samples.map((p) => {
        const cls = p === g.best_path ? "is-best" : "";
        return `<img loading="lazy" src="${imgUrl(p, 220)}" class="${cls}" alt="">`;
      }).join("");
      const spanText = fmtBurstSpan(g.span_seconds);
      const metaLine = spanText ? `<span class="pc-meta-line">${spanText}</span>` : "";
      card.innerHTML = `
        <div class="pc-imgs">${samples}</div>
        <div class="pc-meta">连拍 ${g.size} 张${metaLine}</div>`;
      grid.appendChild(card);
    });
  }
}

bindSlider($("prv-near"), $("prv-near-val"));
bindSlider($("prv-far"), $("prv-far-val"));
bindSlider($("prv-secs"), $("prv-secs-val"));

$("btn-regroup").addEventListener("click", async () => {
  const btn = $("btn-regroup");
  btn.disabled = true; btn.textContent = "重新分组中…";
  setStatus("正在重新分组…", "busy");
  try {
    await fetchJSON("/api/regroup", {
      method: "POST",
      body: JSON.stringify({
        threshold_near: parseInt($("prv-near").value),
        threshold_far: parseInt($("prv-far").value),
        near_seconds: parseInt($("prv-secs").value) * 60,
      }),
    });
    await refreshPreview();
  } catch (e) {
    toast("重新分组失败：" + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "用新阈值重新分组";
  }
});

$("btn-preview-continue").addEventListener("click", () => enterArena());
$("btn-preview-back").addEventListener("click", () => showView("landing"));

// =================================================================
// 选片页
// =================================================================
async function enterArena() {
  const s = await fetchJSON("/api/status");
  if (!s.ready) { enterProcessing("?"); return; }
  lastSession = s;
  currentMode = s.mode || "copy";
  $("arena-folder").textContent = shortenHome(s.folder);
  $("arena-folder").title = s.folder;
  $("dry-run-pill").classList.toggle("hidden", !s.dry_run);
  $("mode-pill").textContent = currentMode === "copy" ? "复制" : "移动";
  $("mode-pill").classList.toggle("hidden", false);
  showView("arena");
  if (!localStorage.getItem(TUTORIAL_KEY)) {
    $("tutorial").classList.remove("hidden");
  }
  await loadCurrent();
}

$("btn-tut-close").addEventListener("click", () => {
  $("tutorial").classList.add("hidden");
  localStorage.setItem(TUTORIAL_KEY, "1");
});

function setOverallProgress(s) {
  const total = s.multi_groups || 0;
  const done = Math.min(total, s.finished_multi_groups || 0);
  const pct = total > 0 ? Math.min(100, (done / total) * 100) : 0;
  $("overall-fill").style.width = pct + "%";
  $("overall-text").textContent = total > 0
    ? `${done} / ${total} 组`
    : "全部为单图，无需选择";
  if (total > 0) {
    setStatus(`选片中 · 已决 ${done} / ${total} 组`, "waiting");
  }
}

function fmtBytes(n) {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function renderLlmComment(elId, meta) {
  const el = $(elId);
  if (!el) return;
  const reason = meta && meta.llm_reason;
  const verdict = meta && meta.llm_verdict;
  if (!reason || !verdict) {
    el.hidden = true;
    el.textContent = "";
    el.classList.remove("is-pass", "is-reject");
    return;
  }
  const pass = verdict === "pass";
  el.classList.toggle("is-pass", pass);
  el.classList.toggle("is-reject", !pass);
  el.innerHTML = "";
  const mark = document.createElement("span");
  mark.className = "llm-mark";
  mark.textContent = pass ? "★" : "✗";
  const text = document.createElement("span");
  text.className = "llm-text";
  text.textContent = reason;
  el.appendChild(mark);
  el.appendChild(text);
  el.hidden = false;
}

function renderExifLine(elId, meta, otherMeta) {
  const el = $(elId);
  el.innerHTML = "";
  if (!meta) { el.textContent = ""; return; }
  const fields = [
    ["camera", meta.camera],
    ["lens", meta.lens],
    ["focal_length", meta.focal_length],
    ["aperture", meta.aperture],
    ["shutter", meta.shutter],
    ["iso", meta.iso ? `ISO ${meta.iso}` : ""],
    ["dim", (meta.width && meta.height) ? `${meta.width}×${meta.height}` : ""],
    ["size", fmtBytes(meta.file_size)],
  ];
  fields.forEach(([key, val]) => {
    if (!val) return;
    const span = document.createElement("span");
    span.textContent = val;
    if (otherMeta) {
      const otherVal = key === "iso" ? (otherMeta.iso ? `ISO ${otherMeta.iso}` : "") :
                       key === "dim" ? ((otherMeta.width && otherMeta.height) ? `${otherMeta.width}×${otherMeta.height}` : "") :
                       key === "size" ? fmtBytes(otherMeta.file_size) :
                       otherMeta[key] || "";
      if (otherVal && otherVal !== val) span.classList.add("exif-diff");
    }
    el.appendChild(span);
  });
}

// 偏好倾向（≥ 8 次决策后启用，避免冷启动噪声）
function _prefBias() {
  const p = lastSession && lastSession.preferences;
  if (!p || p.decisions < 8) return {};
  const ratio = (chosen, passed) => {
    const t = chosen + passed;
    return t < 4 ? 0 : (chosen - passed) / t;  // -1 ~ +1
  };
  return {
    aesthetic: ratio(p.aesthetic_chosen, p.aesthetic_passed),
    sharper: ratio(p.sharper_chosen, p.sharper_passed),
    brighter: ratio(p.brighter_chosen, p.brighter_passed),
  };
}

// 基于本地质量分 + EXIF + 美学分 生成"摄影师式"两图差异提示
function describeDiff(leftMeta, rightMeta) {
  if (!leftMeta || !rightMeta) return null;
  const L = leftMeta, R = rightMeta;
  const out = [];
  const hasFace = (L.face_count || 0) > 0 || (R.face_count || 0) > 0;

  const bias = _prefBias();

  // 1. 闭眼检测（最强信号）
  const lEyes = L.eyes_open_score, rEyes = R.eyes_open_score;
  if (lEyes != null && rEyes != null) {
    if (lEyes < 0.45 && rEyes >= 0.65) return { text: "看起来左图主体在闭眼", weak: false };
    if (rEyes < 0.45 && lEyes >= 0.65) return { text: "看起来右图主体在闭眼", weak: false };
  }

  // 1.5 美学分差距（NIMA / Schuhmann predictor）
  if (L.aesthetic_score != null && R.aesthetic_score != null) {
    const da = L.aesthetic_score - R.aesthetic_score;
    if (Math.abs(da) > 0.45) {
      const side = da > 0 ? "左图" : "右图";
      let text = `看起来${side}观感更舒服`;
      if (bias.aesthetic && Math.abs(bias.aesthetic) > 0.3) {
        text += "（与近期选择一致）";
      }
      return { text, weak: false };
    }
  }

  // 2. 人脸锐度差异
  if (hasFace && L.face_sharpness != null && R.face_sharpness != null) {
    const denom = Math.max(1, Math.min(L.face_sharpness, R.face_sharpness));
    const diff = (L.face_sharpness - R.face_sharpness) / denom;
    if (diff > 0.25) {
      let text = "看起来左图人脸更清晰";
      if (bias.sharper > 0.3) text += "（与近期选择一致）";
      return { text, weak: false };
    }
    if (diff < -0.25) {
      let text = "看起来右图人脸更清晰";
      if (bias.sharper > 0.3) text += "（与近期选择一致）";
      return { text, weak: false };
    }
  }

  // 3. 整图锐度
  if (L.blur_score != null && R.blur_score != null) {
    const lb = L.blur_score, rb = R.blur_score;
    const denom = Math.max(1, Math.min(lb, rb));
    const diff = (lb - rb) / denom;
    if (Math.abs(diff) > 0.4) {
      out.push(diff > 0 ? "左图主体更锐" : "右图主体更锐");
    }
  }

  // 4. 曝光差异（EV 档数估算）
  if (L.brightness_mean != null && R.brightness_mean != null) {
    const lm = L.brightness_mean, rm = R.brightness_mean;
    if (lm > 0 && rm > 0) {
      const stops = Math.log2(lm / rm);
      if (Math.abs(stops) > 0.6) {
        const sign = stops > 0 ? "左图" : "右图";
        const ev = Math.abs(stops);
        const evText = ev > 1.4 ? "更亮约 1.5 档"
                     : ev > 0.9 ? "更亮约 1 档"
                     : "略亮";
        out.push(`${sign}${evText}`);
      }
    }
  }

  // 5. ISO 差异
  if (L.iso && R.iso) {
    const li = parseInt(L.iso), ri = parseInt(R.iso);
    if (li > 0 && ri > 0 && (li / ri >= 2 || ri / li >= 2)) {
      out.push(li > ri ? "左图 ISO 更高" : "右图 ISO 更高");
    }
  }

  // 6. 兜底
  if (!out.length) {
    return { text: "两张差异微弱，按 ↑ 都留也是个选择", weak: true };
  }
  return { text: out.slice(0, 2).join(" · "), weak: false };
}

function renderArenaDiff(group) {
  const diffEl = $("arena-diff");
  if (!group || !group.right || !group.left_meta || !group.right_meta) {
    diffEl.classList.add("hidden");
    return;
  }
  const d = describeDiff(group.left_meta, group.right_meta);
  if (!d) { diffEl.classList.add("hidden"); return; }
  $("arena-diff-text").textContent = d.text;
  diffEl.classList.remove("hidden");
}

function renderArenaStack() {
  const el = $("arena-stack");
  el.innerHTML = "";
  if (!recentWinners.length) {
    el.classList.add("hidden");
    return;
  }
  const cap = document.createElement("span");
  cap.className = "stack-cap";
  cap.textContent = `已选 ${lastSession?.winner_count || recentWinners.length}`;
  el.appendChild(cap);
  recentWinners.slice(-3).forEach((p) => {
    const img = document.createElement("img");
    img.src = imgUrl(p, 96);
    img.loading = "lazy";
    el.appendChild(img);
  });
  el.classList.remove("hidden");
}

function pushRecentWinner(path) {
  if (!path) return;
  recentWinners.push(path);
  if (recentWinners.length > 12) recentWinners = recentWinners.slice(-12);
  renderArenaStack();
}

function renderStrip(members) {
  const strip = $("strip");
  strip.innerHTML = "";
  if (!members || !members.length) return;
  const cap = members.slice(0, 32);
  cap.forEach((m) => {
    const cell = document.createElement("div");
    cell.className = `strip-cell strip-${m.status}`;
    cell.innerHTML = `<img loading="lazy" src="${imgUrl(m.path, 128)}" alt="">`;
    cell.title = m.name + " · " + m.status;
    cell.addEventListener("click", () => openLightbox({ path: m.path, name: m.name }));
    strip.appendChild(cell);
  });
  if (members.length > 32) {
    const more = document.createElement("div");
    more.className = "strip-more";
    more.textContent = `+${members.length - 32}`;
    strip.appendChild(more);
  }
}

function renderGroup(group, sessionStatus) {
  currentGroup = group;
  if (sessionStatus) {
    lastSession = sessionStatus;
    setOverallProgress(sessionStatus);
  }
  if (!group) {
    $("img-left").removeAttribute("src");
    $("img-right").removeAttribute("src");
    $("arena-diff").classList.add("hidden");
    return;
  }

  $("caption-left").textContent = basename(group.left);
  $("caption-right").textContent = basename(group.right);

  const decided = group.decided || 0;
  const total = group.total_images || 0;
  const groupPct = total ? Math.min(100, (decided / total) * 100) : 0;
  $("group-fill").style.width = groupPct + "%";
  // 组标题：把裸 id 换成有时间感的描述
  let title = `组 #${group.id_short || ""}`;
  if (group.earliest_dt) {
    title = `${halfDayLabel(group.earliest_dt)} · 连拍 ${total} 张`;
  } else if (total > 1) {
    title = `连拍 ${total} 张`;
  }
  $("group-label").textContent = title;
  $("group-prog-text").textContent =
    `${decided} / ${total} 已决 · 剩 ${group.remaining_in_group}`;

  ["side-left", "side-right"].forEach((id) => {
    $(id).classList.remove("is-winner", "is-loser", "is-broken");
  });

  resetZoom();

  const single = !!group.left && !group.right && !group.finished;
  $("arena").classList.toggle("single-mode", single);
  $("single-banner").classList.toggle("hidden", !single);

  swapImage("img-left", "side-left", group.left);
  swapImage("img-right", "side-right", group.right);

  renderExifLine("exif-left", group.left_meta, group.right_meta);
  renderExifLine("exif-right", group.right_meta, group.left_meta);

  renderLlmComment("llm-left", group.left_meta);
  renderLlmComment("llm-right", group.right_meta);

  renderArenaDiff(group);
  renderStrip(group.members);
  renderArenaStack();

  $("btn-undo").disabled = !group.can_undo;

  if (group.next_preload) $("preload").src = imgUrl(group.next_preload);

  updateTitle("arena");
}

function swapImage(elId, sideId, path) {
  const img = $(elId);
  const side = $(sideId);
  if (!path) {
    img.classList.add("fading");
    img.removeAttribute("src");
    return;
  }
  const newUrl = imgUrl(path);
  if (img.dataset.path === path) {
    img.classList.remove("fading");
    return;
  }
  img.dataset.path = path;
  img.classList.add("fading");
  fetch(newUrl).then(async (resp) => {
    if (resp.headers.get("X-Image-Status") === "failed") {
      side.classList.add("is-broken");
    }
    const blob = await resp.blob();
    const objUrl = URL.createObjectURL(blob);
    if (img.dataset.path === path) {
      if (img.dataset.objUrl) URL.revokeObjectURL(img.dataset.objUrl);
      img.dataset.objUrl = objUrl;
      img.src = objUrl;
      requestAnimationFrame(() => img.classList.remove("fading"));
    } else {
      URL.revokeObjectURL(objUrl);
    }
  }).catch(() => {
    // B2 修复：网络错误也要标 is-broken，否则用户看到一边空白没图却不知道为啥
    if (img.dataset.path === path) {
      side.classList.add("is-broken");
    }
    img.classList.remove("fading");
  });
}

async function loadCurrent() {
  const r = await fetchJSON("/api/group");
  const s = await fetchJSON("/api/status");
  lastSession = s;
  if (r.done) { enterDone(s); return; }
  renderGroup(r.group, s);
}

async function decide(action) {
  if (busy || !currentGroup) return;
  if (!currentGroup.right && action === "pick-right") {
    toast("右侧无图。↑ 或 ← 留下，↓ 或 [ 丢弃。"); return;
  }
  if (action === "pick-left" && $("side-left").classList.contains("is-broken")) {
    toast("这张无法读取，无法选中。可按 [ 单独踢掉。"); return;
  }
  if (action === "pick-right" && $("side-right").classList.contains("is-broken")) {
    toast("这张无法读取，无法选中。可按 ] 单独踢掉。"); return;
  }
  if (action === "both-keep" && currentGroup.right &&
      ($("side-left").classList.contains("is-broken") ||
       $("side-right").classList.contains("is-broken"))) {
    toast("有损坏图，无法两张都留。请先用 [ 或 ] 踢掉那张。"); return;
  }
  busy = true;

  const left = $("side-left");
  const right = $("side-right");
  let loser;
  if (action === "pick-left") {
    loser = "right";
    left.classList.add("is-winner"); right.classList.add("is-loser");
    pushRecentWinner(currentGroup.left);
  } else if (action === "pick-right") {
    loser = "left";
    right.classList.add("is-winner"); left.classList.add("is-loser");
    pushRecentWinner(currentGroup.right);
  } else if (action === "both-keep") {
    loser = "neither";
    left.classList.add("is-winner"); right.classList.add("is-winner");
    pushRecentWinner(currentGroup.left);
    pushRecentWinner(currentGroup.right);
  } else {
    loser = "both";
    left.classList.add("is-loser"); right.classList.add("is-loser");
  }

  const animDone = sleep(VERDICT_HOLD_MS);
  let r, s;
  try {
    r = await fetchJSON("/api/choose", {
      method: "POST",
      body: JSON.stringify({ loser }),
    });
  } catch (err) {
    busy = false;
    left.classList.remove("is-winner", "is-loser");
    right.classList.remove("is-winner", "is-loser");
    toast("出错了：" + err.message);
    return;
  }
  await animDone;
  s = await fetchJSON("/api/status");
  if (r.done) { busy = false; enterDone(s); return; }
  renderGroup(r.group, s);
  busy = false;
}

async function kickSide(side) {
  if (busy || !currentGroup) return;
  if (side === "right" && !currentGroup.right) {
    toast("右侧无图。"); return;
  }
  if (side === "left" && !currentGroup.left) return;
  busy = true;
  try {
    const r = await fetchJSON("/api/kick", {
      method: "POST",
      body: JSON.stringify({ side }),
    });
    const s = await fetchJSON("/api/status");
    if (r.done) { enterDone(s); return; }
    renderGroup(r.group, s);
  } catch (e) {
    toast("踢出失败：" + e.message);
  } finally {
    busy = false;
  }
}

async function skipGroup() {
  if (busy) return;
  busy = true;
  try {
    const r = await fetchJSON("/api/skip_group", { method: "POST" });
    const s = await fetchJSON("/api/status");
    if (r.done) { enterDone(s); return; }
    renderGroup(r.group, s);
    toast("放到最后了，可以稍后再决");
  } finally {
    busy = false;
  }
}

async function undo() {
  if (busy) return;
  busy = true;
  try {
    const r = await fetchJSON("/api/undo", { method: "POST" });
    const s = await fetchJSON("/api/status");
    if (r.done) { enterDone(s); return; }
    renderGroup(r.group, s);
  } catch (err) {
    console.error(err);
  } finally {
    busy = false;
  }
}

$("side-left").addEventListener("click", (e) => {
  if (e.target.closest(".kick-btn")) return;
  if (dragMoved) return;          // 缩放态下拖动产生的 click 不算选片
  decide("pick-left");
});
$("side-right").addEventListener("click", (e) => {
  if (e.target.closest(".kick-btn")) return;
  if (dragMoved) return;
  decide("pick-right");
});
document.querySelectorAll(".kick-btn").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    kickSide(btn.dataset.side);
  });
});
$("btn-both-keep").addEventListener("click", () => decide("both-keep"));
$("btn-both").addEventListener("click", () => decide("both-out"));
$("btn-skip-group").addEventListener("click", () => skipGroup());
$("btn-undo").addEventListener("click", () => undo());
$("btn-quit").addEventListener("click", async () => {
  const ok = await confirmDialog(
    "回到首页",
    "回到首页处理另一个文件夹？当前进度已自动保存，下次可恢复。"
  );
  if (!ok) return;
  showView("landing");
  $("start-btn").disabled = false;
});

// =================================================================
// 同步缩放（左右联动）
// =================================================================
const ZOOM_PRESETS = [1, 2, 4];
let zoom = { scale: 1, tx: 0, ty: 0 };
let dragging = null;
// 缩放态下区分"点击 = 选片"和"按住拖动 = 平移"。
// mousedown 后超过 DRAG_THRESHOLD_PX 的位移就算拖动；mouseup 之后浏览器仍会派发
// click 到 .side-left/.side-right（这是 DOM 规范），由 click handler 看 dragMoved 拦截。
const DRAG_THRESHOLD_PX = 4;
let dragMoved = false;

function applyZoom() {
  const t = `translate(${zoom.tx}px, ${zoom.ty}px) scale(${zoom.scale})`;
  $("img-left").style.transform = t;
  $("img-right").style.transform = t;
  const pill = $("zoom-pill");
  if (zoom.scale > 1.001) {
    pill.classList.remove("hidden");
    pill.textContent = `${zoom.scale.toFixed(zoom.scale < 2 ? 1 : 0)}×`;
  } else {
    pill.classList.add("hidden");
  }
  document.querySelectorAll(".zoom-viewport").forEach((vp) =>
    vp.classList.toggle("zoomed", zoom.scale > 1.001)
  );
}

function resetZoom() {
  zoom = { scale: 1, tx: 0, ty: 0 };
  applyZoom();
}

function cycleZoom() {
  const idx = ZOOM_PRESETS.findIndex((v) => Math.abs(v - zoom.scale) < 0.05);
  const next = ZOOM_PRESETS[(idx + 1) % ZOOM_PRESETS.length];
  zoom.scale = next;
  if (next === 1) { zoom.tx = 0; zoom.ty = 0; }
  applyZoom();
}

function clampPan() {
  const vp = document.querySelector(".zoom-viewport");
  if (!vp) return;
  const rect = vp.getBoundingClientRect();
  const max = (zoom.scale - 1) * rect.width / 2;
  const maxY = (zoom.scale - 1) * rect.height / 2;
  zoom.tx = Math.max(-max, Math.min(max, zoom.tx));
  zoom.ty = Math.max(-maxY, Math.min(maxY, zoom.ty));
}

document.querySelectorAll(".zoom-viewport").forEach((vp) => {
  vp.addEventListener("wheel", (e) => {
    if (!$("view-arena").classList.contains("active")) return;
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    const newScale = Math.max(1, Math.min(8, zoom.scale * factor));
    zoom.scale = newScale;
    if (newScale <= 1.001) { zoom.tx = 0; zoom.ty = 0; }
    clampPan();
    applyZoom();
  }, { passive: false });

  vp.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    if (zoom.scale <= 1.001) return;
    dragging = {
      startX: e.clientX, startY: e.clientY,
      txStart: zoom.tx, tyStart: zoom.ty,
    };
    dragMoved = false;
    e.preventDefault();
  });
});

window.addEventListener("mousemove", (e) => {
  if (!dragging) return;
  const dx = e.clientX - dragging.startX;
  const dy = e.clientY - dragging.startY;
  if (!dragMoved && (Math.abs(dx) > DRAG_THRESHOLD_PX || Math.abs(dy) > DRAG_THRESHOLD_PX)) {
    dragMoved = true;
  }
  zoom.tx = dragging.txStart + dx;
  zoom.ty = dragging.tyStart + dy;
  clampPan();
  applyZoom();
});
window.addEventListener("mouseup", () => {
  dragging = null;
  // dragMoved 必须留到接下来的 click handler 读完——click 在 mouseup 之后由浏览器
  // 同步派发。setTimeout(0) 让清理动作落到下一个 task，比 click handler 晚一步。
  if (dragMoved) {
    setTimeout(() => { dragMoved = false; }, 0);
  }
});

// =================================================================
// 键盘
// =================================================================
document.addEventListener("keydown", (e) => {
  if (e.repeat) return;
  if (!$("lightbox").classList.contains("hidden")) {
    if (e.key === "Escape") closeLightbox();
    return;
  }
  if (!$("view-arena").classList.contains("active")) return;
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if (e.key === "ArrowLeft") { e.preventDefault(); decide("pick-left"); }
  else if (e.key === "ArrowRight") { e.preventDefault(); decide("pick-right"); }
  else if (e.key === "ArrowUp") { e.preventDefault(); decide("both-keep"); }
  else if (e.key === "ArrowDown") { e.preventDefault(); decide("both-out"); }
  else if (e.key === "s" || e.key === "S") { skipGroup(); }
  else if (e.key === "z" || e.key === "Z") {
    if (e.shiftKey) { undo(); }
    else { cycleZoom(); }
  }
  else if (e.key === "[") { kickSide("left"); }
  else if (e.key === "]") { kickSide("right"); }
});

// =================================================================
// 完成页
// =================================================================
async function renderWinnersAlbum() {
  const album = $("done-album");
  album.innerHTML = "";
  let list = [];
  try {
    const w = await fetchJSON("/api/winners");
    list = w.winners || [];
  } catch (err) {
    album.innerHTML = `<div class="winners-empty">加载失败：${err.message}</div>`;
    return;
  }
  $("winners-count").textContent = list.length ? `${list.length} 张` : "";
  if (!list.length) {
    album.innerHTML = '<div class="winners-empty">没有胜出的照片</div>';
    return;
  }

  // 按 datetime 升序
  list.sort((a, b) => (a.datetime || "z").localeCompare(b.datetime || "z"));
  const sections = new Map();
  for (const item of list) {
    const dt = item.datetime;
    const key = dt ? `${dateLabel(dt)} · ${halfDayLabel(dt)}` : "未标注时间";
    if (!sections.has(key)) sections.set(key, []);
    sections.get(key).push(item);
  }

  let cellIdx = 0;
  for (const [chapter, items] of sections.entries()) {
    const head = document.createElement("div");
    head.className = "album-chapter";
    head.innerHTML = `
      <span class="album-chapter-name">${chapter}</span>
      <span class="album-chapter-meta">${items.length} 张</span>`;
    album.appendChild(head);

    const row = document.createElement("div");
    row.className = "album-row";
    items.forEach((item) => {
      const cell = document.createElement("div");
      cell.className = "album-cell";
      cell.style.animationDelay = `${Math.min(cellIdx * 18, 500)}ms`;
      cellIdx++;
      const badgeText = item.group_size > 1
        ? `从 ${item.group_size} 张里` : "独张";
      cell.innerHTML = `
        <img loading="lazy" src="${imgUrl(item.path, 480)}" alt="${item.name}">
        <span class="album-badge">${badgeText}</span>`;
      cell.title = `${item.name} · ${badgeText}`;
      cell.addEventListener("click", () => openLightbox(item));
      row.appendChild(cell);
    });
    album.appendChild(row);
  }
}

async function renderAutoRejectedGrid(options = {}) {
  const {
    sectionId = "auto-reject-section",
    gridId = "auto-reject-grid",
    countId = "auto-reject-count",
    refreshWinners = true,
    onRestored = null,
  } = options;
  const section = $(sectionId);
  const grid = $(gridId);
  grid.innerHTML = "";
  try {
    const data = await fetchJSON("/api/auto_rejected");
    const items = data.items || [];
    section.classList.toggle("hidden", !items.length);
    $(countId).textContent = items.length ? `${items.length} 张` : "";
    if (!items.length) return items;
    items.forEach((item, i) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "auto-reject-card";
      if (item.restored) card.classList.add("is-restored");
      card.style.animationDelay = `${Math.min(i * 20, 500)}ms`;
      card.disabled = !!item.restored;
      card.innerHTML = `
        <img loading="lazy" src="${imgUrl(item.path, 520)}" alt="${item.name}">
        <span class="ar-reason">${item.restored ? "已保留" : item.reason}</span>
        <span class="ar-name">${item.name}</span>`;
      card.addEventListener("click", async () => {
        if (card.disabled) return;
        card.disabled = true;
        card.classList.add("is-busy");
        try {
          await fetchJSON("/api/restore_rejected", {
            method: "POST",
            body: JSON.stringify({ group_id: item.group_id, path: item.path }),
          });
          card.classList.remove("is-busy");
          card.classList.add("is-restored");
          card.querySelector(".ar-reason").textContent = "已保留";
          if (refreshWinners) await renderWinnersAlbum();
          const s = await fetchJSON("/api/status");
          if ($("view-done").classList.contains("active")) {
            $("stat-winners").textContent = s.winner_count || 0;
            $("stat-losers").textContent = s.loser_count || 0;
            $("done-to").textContent = (s.winner_count || 0).toLocaleString();
          }
          if (onRestored) await onRestored(item, s);
          toast("已保留，将进入选片");
        } catch (err) {
          card.disabled = false;
          card.classList.remove("is-busy");
          toast("保留失败：" + err.message);
        }
      });
      grid.appendChild(card);
    });
    return items;
  } catch (err) {
    section.classList.remove("hidden");
    grid.innerHTML = `<div class="winners-empty">列表加载失败：${err.message}</div>`;
    return [];
  }
}

async function enterDone(status) {
  showView("done");
  const s = status || lastSession || (await fetchJSON("/api/status"));
  const singles = (s.total_groups || 0) - (s.multi_groups || 0);
  const total = s.image_count || ((s.winner_count || 0) + (s.loser_count || 0));
  const kept = s.winner_count || 0;

  // 两数字 hero
  $("done-from").textContent = total.toLocaleString();
  $("done-to").textContent = kept.toLocaleString();

  // 隐藏的兼容字段（其他地方可能还在读）
  $("stat-groups").textContent = s.multi_groups || 0;
  $("stat-winners").textContent = kept;
  $("stat-losers").textContent = s.loser_count || 0;

  const modeWord = s.mode === "move" ? "移动" : "复制";
  const baseLine = s.dry_run
    ? "试运行模式 · 没有真的搬运文件，但你已确认了所有选择。"
    : `胜出以「${modeWord}」归到 winners/，淘汰归到 losers/。`;
  $("done-sub").textContent = singles > 0
    ? `${baseLine}（其中 ${singles} 张无相似副本，直接归入 winners/。）`
    : baseLine;
  $("path-win").textContent = (s.folder || "") + "/winners";
  $("path-lose").textContent = (s.folder || "") + "/losers";

  const unfinished = s.unfinished_groups || 0;
  $("unfinished-notice").classList.toggle("hidden", unfinished <= 0);
  $("unfinished-num").textContent = unfinished;

  setStatus(`完成 · 留下 ${kept.toLocaleString()} 张`, "done");

  await renderWinnersAlbum();
  await renderAutoRejectedGrid();

  try {
    const k = await fetchJSON("/api/skipped");
    const list = k.skipped || [];
    $("skipped-count").textContent = `(${list.length})`;
    const ul = $("skipped-list");
    ul.innerHTML = "";
    list.slice(-50).reverse().forEach((it) => {
      const li = document.createElement("li");
      li.textContent = `${basename(it.path)} — ${it.reason}`;
      li.title = it.path;
      ul.appendChild(li);
    });
    $("skipped-details").style.display = list.length ? "" : "none";
  } catch {}
}

$("btn-restart").addEventListener("click", () => {
  showView("landing");
  $("start-btn").disabled = false;
  $("folder-input").value = "";
  $("folder-snapshot").classList.add("hidden");
  renderRecent();
  setStatus("引擎就绪", "idle");
});

$("btn-go-unfinished").addEventListener("click", () => enterArena());

$("btn-open-folder").addEventListener("click", async () => {
  try { await fetchJSON("/api/open_folder", { method: "POST", body: JSON.stringify({}) }); }
  catch (e) { toast("打开失败：" + e.message); }
});

$("btn-redo-folder").addEventListener("click", async () => {
  const s = lastSession || (await fetchJSON("/api/status").catch(() => null));
  if (!s || !s.folder) { toast("没有可重做的会话"); return; }
  const ok = await confirmDialog(
    "重做这个文件夹",
    `将清掉 ${s.folder}/winners 与 /losers 子目录、所有缓存与本次进度，` +
    `用同样的设置（${s.mode === "move" ? "移动" : "复制"}模式` +
    `${s.dry_run ? "、试运行" : ""}）重新分组挑选。\n\n这步不可撤销，确定继续？`
  );
  if (!ok) return;
  try {
    await fetchJSON("/api/start", {
      method: "POST",
      body: JSON.stringify({
        folder: s.folder,
        dry_run: !!s.dry_run,
        wipe_cache: true,
        mode: s.mode || "copy",
        engine: "fast",
        threshold_near: s.threshold_near,
        threshold_far: s.threshold_far,
        near_seconds: s.near_seconds,
        prescreen_enabled: s.prescreen_enabled,
        prescreen_strength: s.prescreen_strength,
        llm_model: "",
      }),
    });
    currentMode = s.mode || "copy";
    enterProcessing(s.folder);
  } catch (e) {
    toast("重做失败：" + e.message);
  }
});

async function reopenGroup(groupId, groupSize, btn) {
  if (!groupId) return;
  const word = groupSize > 1 ? "重选这组" : "重新决定这张";
  const ok = await confirmDialog(
    word,
    groupSize > 1
      ? `把这组的胜者和淘汰者从 winners/ 与 losers/ 还原回原位置，回到擂台从头挑。${
          currentMode === "move" ? "（移动模式：文件会真的搬回去）" : ""
        }`
      : `把这张从 winners/ 还原回原位置，回到擂台决定保留还是丢弃。${
          currentMode === "move" ? "（移动模式：文件会真的搬回去）" : ""
        }`
  );
  if (!ok) return;
  if (btn) { btn.disabled = true; btn.textContent = "处理中…"; }
  try {
    const r = await fetchJSON("/api/reopen_group", {
      method: "POST",
      body: JSON.stringify({ group_id: groupId }),
    });
    if (r.failed && r.failed.length) {
      toast(`还原完成，${r.failed.length} 个文件无法还原（详见日志）`);
    }
    enterArena();
  } catch (e) {
    toast("反悔失败：" + e.message);
    if (btn) { btn.disabled = false; btn.textContent = `↩ ${word}`; }
  }
}

// =================================================================
// Lightbox
// =================================================================
let lbItem = null;
function openLightbox(item) {
  lbItem = item;
  $("lb-img").src = imgUrl(item.path);
  $("lb-caption").textContent = item.name || basename(item.path);
  $("lightbox").classList.remove("hidden");
}
function closeLightbox() {
  $("lightbox").classList.add("hidden");
  $("lb-img").removeAttribute("src");
}
$("lb-close").addEventListener("click", closeLightbox);
$("lightbox").addEventListener("click", (e) => {
  if (e.target.id === "lightbox") closeLightbox();
});
$("lb-original").addEventListener("click", () => {
  if (!lbItem) return;
  $("lb-img").src = originalUrl(lbItem.path);
  toast("加载原图中…");
});

// =================================================================
// 启动
// =================================================================
async function bootstrap() {
  renderRecent();
  setStatus("引擎就绪", "idle");
  try {
    const s = await fetchJSON("/api/status");
    if (s.ready) {
      lastSession = s;
      currentMode = s.mode || "copy";
      if (shouldShowPrescreen(s)) {
        if ((s.prescreen_pending_count || 0) === 0) {
          await confirmPrescreenAndContinue();
        } else {
          enterPrescreen(s);
        }
      } else if (s.finished_groups >= s.total_groups) {
        enterDone(s);
      } else {
        enterArena();
      }
      return;
    }
  } catch {}
  try {
    const j = await fetchJSON("/api/job");
    if (j.status && j.status !== "idle" && j.status !== "done" && j.status !== "error" && j.status !== "cancelled") {
      enterProcessing(j.folder || "");
      return;
    }
  } catch {}
  showView("landing", false);
  history.replaceState({ view: "landing" }, "", location.pathname);
}

// =================================================================
// 相机参数水印 / 韩式贴纸滤镜导出弹窗
// =================================================================
const WM_MODES = {
  beautify: {
    title: "贴纸与滤镜导出",
    sub: "为胜出的照片添加韩式贴纸、相纸边框或网感滤镜，导出到 winners 子文件夹",
    templateTitle: "贴纸 / 滤镜",
    loadUrl: "/api/beautify/styles",
    listKey: "styles",
    previewUrl: "/api/beautify/preview",
    startUrl: "/api/beautify/start",
    statusUrl: "/api/beautify/status",
    cancelUrl: "/api/beautify/cancel",
    openUrl: "/api/beautify/open_out_dir",
    cfgKey: "style",
    defaultTemplate: "seoul_booth",
    outputLabel: "winners/kstyle_时间戳",
    stopConfirm: "中止贴纸/滤镜导出？已完成的照片会保留。",
    doneToast: "贴纸/滤镜导出完成",
  },
  watermark: {
    title: "专业相机参数水印",
    sub: "为胜出的照片批量添加机型、镜头、焦段、光圈、快门、ISO 与拍摄时间水印",
    templateTitle: "水印样式",
    loadUrl: "/api/watermark/templates",
    listKey: "templates",
    previewUrl: "/api/watermark/preview",
    startUrl: "/api/watermark/start",
    statusUrl: "/api/watermark/status",
    cancelUrl: "/api/watermark/cancel",
    openUrl: "/api/watermark/open_out_dir",
    cfgKey: "template",
    defaultTemplate: "A",
    outputLabel: "winners/watermarked_时间戳",
    stopConfirm: "中止相机参数水印导出？已完成的照片会保留。",
    doneToast: "相机参数水印导出完成",
  },
};

const WM = {
  mode: "beautify",
  previewIdx: 0,
  totalWinners: 0,
  previewSeq: 0,
  debounceHandle: null,
  pollHandle: null,
  isExporting: false,
  selected: {
    beautify: "seoul_booth",
    watermark: "A",
  },
  templates: {
    beautify: [],
    watermark: [],
  },
};

const WM_BEAUTIFY_THUMBS = {
  seoul_booth: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'>
    <rect width='120' height='60' rx='7' fill='#fff7f8'/>
    <rect x='12' y='8' width='96' height='36' rx='4' fill='url(#p1)'/>
    <path d='M18 15c5-8 18-1 0 11C0 14 13 7 18 15Z' fill='#ff8fb2'/>
    <path d='M96 12l3 7 8 1-6 5 2 8-7-4-7 4 2-8-6-5 8-1 3-7Z' fill='#ffd56f'/>
    <rect x='42' y='48' width='36' height='4' rx='2' fill='#eab2c4'/>
    <defs><linearGradient id='p1' x1='0' y1='0' x2='1' y2='1'><stop stop-color='#ffd7e2'/><stop offset='1' stop-color='#dce8ff'/></linearGradient></defs>
  </svg>`,
  milk_peach: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'>
    <rect width='120' height='60' rx='7' fill='url(#p2)'/>
    <circle cx='92' cy='10' r='24' fill='#fff' opacity='.38'/>
    <rect x='22' y='43' width='76' height='5' rx='2.5' fill='#fff' opacity='.7'/>
    <defs><linearGradient id='p2' x1='0' y1='0' x2='1' y2='1'><stop stop-color='#ffe4da'/><stop offset='.55' stop-color='#fff4f7'/><stop offset='1' stop-color='#e6f0ff'/></linearGradient></defs>
  </svg>`,
  clean_flash: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'>
    <rect width='120' height='60' rx='7' fill='url(#p3)'/>
    <circle cx='103' cy='7' r='28' fill='#fff' opacity='.74'/>
    <path d='M22 16h50M22 26h35M22 36h46' stroke='#fff' stroke-width='4' stroke-linecap='round' opacity='.55'/>
    <defs><linearGradient id='p3' x1='0' y1='0' x2='1' y2='1'><stop stop-color='#ddeaff'/><stop offset='1' stop-color='#f8fbff'/></linearGradient></defs>
  </svg>`,
  berry_sticker: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'>
    <rect width='120' height='60' rx='7' fill='#fff2f8'/>
    <rect x='9' y='7' width='102' height='38' rx='4' fill='url(#p4)'/>
    <path d='M25 17c4-7 15-1 0 9-15-10-4-16 0-9Z' fill='#df5d90'/>
    <path d='M91 17l3 7 7 1-5 5 1 7-6-4-6 4 1-7-5-5 7-1 3-7Z' fill='#b894ff'/>
    <rect x='36' y='49' width='48' height='4' rx='2' fill='#d95c8f'/>
    <defs><linearGradient id='p4' x1='0' y1='0' x2='1' y2='1'><stop stop-color='#ffb7ce'/><stop offset='1' stop-color='#9f78db'/></linearGradient></defs>
  </svg>`,
  ribbon_diary: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'>
    <rect width='120' height='60' rx='7' fill='#f7fff7'/>
    <rect x='10' y='8' width='100' height='37' rx='5' fill='url(#p5)'/>
    <path d='M19 42l15-12v24zM51 42L36 30v24z' fill='#88d5c2'/>
    <rect x='32' y='35' width='6' height='14' rx='2' fill='#fff' opacity='.82'/>
    <rect x='42' y='49' width='36' height='4' rx='2' fill='#8bc8ba'/>
    <defs><linearGradient id='p5' x1='0' y1='0' x2='1' y2='1'><stop stop-color='#e6fff4'/><stop offset='1' stop-color='#c9e8ff'/></linearGradient></defs>
  </svg>`,
  aura_sticker: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'>
    <rect width='120' height='60' rx='7' fill='#fbf7ff'/>
    <rect x='10' y='8' width='100' height='38' rx='5' fill='url(#p6)'/>
    <circle cx='90' cy='18' r='18' fill='#fff' opacity='.48'/>
    <path d='M24 17l3 7 7 1-5 5 1 7-6-4-6 4 1-7-5-5 7-1 3-7Z' fill='#b98dff'/>
    <rect x='38' y='49' width='44' height='4' rx='2' fill='#c59bff'/>
    <defs><linearGradient id='p6' x1='0' y1='0' x2='1' y2='1'><stop stop-color='#eadcff'/><stop offset='.58' stop-color='#ffe0ef'/><stop offset='1' stop-color='#dcecff'/></linearGradient></defs>
  </svg>`,
  retro_film: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'>
    <rect width='120' height='60' rx='7' fill='#39291f'/>
    <rect x='14' y='9' width='92' height='42' rx='4' fill='url(#p7)'/>
    <path d='M20 17h80M20 43h80' stroke='#ffe0aa' stroke-width='2' opacity='.5'/>
    <defs><linearGradient id='p7' x1='0' y1='0' x2='1' y2='1'><stop stop-color='#d8a56f'/><stop offset='1' stop-color='#5a3426'/></linearGradient></defs>
  </svg>`,
  watercolor: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'>
    <rect width='120' height='60' rx='7' fill='#fffaf0'/>
    <circle cx='40' cy='28' r='22' fill='#ffc9d6' opacity='.56'/>
    <circle cx='67' cy='27' r='25' fill='#bfe6ff' opacity='.56'/>
    <circle cx='82' cy='31' r='18' fill='#d8f3c4' opacity='.58'/>
    <path d='M20 43c18-18 39-23 80-15' stroke='#937e73' stroke-width='2' fill='none' opacity='.42'/>
  </svg>`,
  pencil_sketch: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'>
    <rect width='120' height='60' rx='7' fill='#f6f1e9'/>
    <path d='M22 42c14-27 34-28 48-8 9 12 18 10 28-7' stroke='#34312e' stroke-width='3' fill='none'/>
    <path d='M24 19h28M68 18h22M40 49h41' stroke='#34312e' stroke-width='2' opacity='.38'/>
  </svg>`,
  comic_pop: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'>
    <rect width='120' height='60' rx='7' fill='#16151a'/>
    <rect x='12' y='8' width='96' height='44' rx='5' fill='#ffd84d'/>
    <path d='M22 39l18-24 19 16 15-11 23 25' stroke='#151515' stroke-width='5' fill='none' stroke-linejoin='round'/>
    <circle cx='88' cy='20' r='8' fill='#ff5a7a' stroke='#151515' stroke-width='3'/>
  </svg>`,
  mono_film: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'>
    <rect width='120' height='60' rx='7' fill='#111'/>
    <rect x='14' y='9' width='92' height='42' rx='4' fill='url(#p8)'/>
    <path d='M25 42c18-20 32-24 50-5 8 8 14 8 23 0' stroke='#f3f1ea' stroke-width='3' fill='none' opacity='.7'/>
    <defs><linearGradient id='p8' x1='0' y1='0' x2='1' y2='1'><stop stop-color='#eeeeee'/><stop offset='1' stop-color='#3a3a3a'/></linearGradient></defs>
  </svg>`,
};

const WM_WATERMARK_THUMBS = {
  A: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'><rect width='120' height='60' rx='7' fill='#f8f7f4'/><rect x='8' y='8' width='104' height='34' rx='4' fill='#d9d4ca'/><rect x='8' y='44' width='104' height='8' fill='#fff'/><circle cx='60' cy='48' r='4' fill='#1d1d1f'/><path d='M15 49h25M80 49h25' stroke='#1d1d1f' stroke-width='2' stroke-linecap='round'/></svg>`,
  B_full: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'><rect width='120' height='60' rx='7' fill='#fff'/><rect x='10' y='7' width='100' height='39' fill='#d8d5cf'/><path d='M34 52h52' stroke='#1d1d1f' stroke-width='3' stroke-linecap='round'/></svg>`,
  B_clean: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'><rect width='120' height='60' rx='7' fill='#fff'/><rect x='10' y='7' width='100' height='40' fill='#e5e2dc'/><circle cx='60' cy='53' r='3' fill='#1d1d1f'/></svg>`,
  C_full: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'><rect width='120' height='60' rx='7' fill='#d6d2ca'/><rect x='20' y='10' width='80' height='38' rx='5' fill='#fff' opacity='.92'/><rect x='30' y='17' width='60' height='20' fill='#a9a397'/><path d='M33 43h54' stroke='#1d1d1f' stroke-width='2'/></svg>`,
  C_clean: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'><rect width='120' height='60' rx='7' fill='#d6d2ca'/><rect x='22' y='10' width='76' height='40' rx='5' fill='#fff' opacity='.92'/><circle cx='60' cy='43' r='4' fill='#1d1d1f'/></svg>`,
  D_full: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'><rect width='120' height='60' rx='7' fill='#fff'/><rect x='14' y='8' width='92' height='34' fill='#d6d2c8'/><path d='M31 51h58' stroke='#1d1d1f' stroke-width='2'/></svg>`,
  D_clean: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'><rect width='120' height='60' rx='7' fill='#fff'/><rect x='14' y='8' width='92' height='34' fill='#d6d2c8'/><circle cx='60' cy='51' r='4' fill='#1d1d1f'/></svg>`,
  F_full: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'><rect width='120' height='60' rx='7' fill='#f7f4ee'/><rect x='8' y='7' width='104' height='46' fill='#ded9cf'/><rect x='16' y='40' width='10' height='10' fill='#cc785c'/><rect x='29' y='40' width='10' height='10' fill='#8da6a0'/><path d='M70 47h33' stroke='#1d1d1f' stroke-width='2'/></svg>`,
  F_clean: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'><rect width='120' height='60' rx='7' fill='#f7f4ee'/><rect x='8' y='7' width='104' height='46' fill='#ded9cf'/><rect x='16' y='40' width='10' height='10' fill='#cc785c'/><circle cx='93' cy='45' r='4' fill='#1d1d1f'/></svg>`,
  G: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'><rect width='120' height='60' rx='7' fill='#fff'/><rect x='12' y='9' width='96' height='42' fill='#d9d6cf'/></svg>`,
  H: `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'><rect width='120' height='60' rx='7' fill='#1d1d1f'/><rect x='17' y='12' width='86' height='37' rx='5' fill='#2f3032'/><rect x='29' y='20' width='44' height='21' fill='#b9b4aa'/><circle cx='88' cy='31' r='8' fill='#0d0d0e'/></svg>`,
};

function wmMeta() {
  return WM_MODES[WM.mode] || WM_MODES.beautify;
}

function wmTemplates() {
  return WM.templates[WM.mode] || [];
}

function wmSelected() {
  return WM.selected[WM.mode] || wmMeta().defaultTemplate;
}

function wmSetSelected(id) {
  WM.selected[WM.mode] = id || wmMeta().defaultTemplate;
}

function wmThumbSvg(t) {
  const map = WM.mode === "watermark" ? WM_WATERMARK_THUMBS : WM_BEAUTIFY_THUMBS;
  if (map[t.id]) return map[t.id];
  const label = Array.from(t.name || t.id || "").slice(0, 5).join("");
  return `<svg viewBox='0 0 120 60' xmlns='http://www.w3.org/2000/svg'>
    <rect width='120' height='60' rx='7' fill='#f6f3ed'/>
    <rect x='12' y='10' width='96' height='30' rx='5' fill='#ddd7cd'/>
    <text x='60' y='52' text-anchor='middle' font-size='11' fill='#1d1d1f'>${escapeHtml(label)}</text>
  </svg>`;
}

function wmCfg() {
  return {
    [wmMeta().cfgKey]: wmSelected(),
    preview_index: WM.previewIdx,
  };
}

async function wmLoadTemplates() {
  if (wmTemplates().length) return wmTemplates();
  const meta = wmMeta();
  try {
    const res = await fetchJSON(meta.loadUrl);
    WM.templates[WM.mode] = res[meta.listKey] || [];
  } catch (e) {
    WM.templates[WM.mode] = [];
    toast("加载样式列表失败：" + e.message);
  }
  return wmTemplates();
}

function wmRenderTemplatePicker() {
  const grid = $("wm-template-grid");
  if (!grid) return;
  const selected = wmSelected();
  grid.innerHTML = wmTemplates().map((t) => {
    const svg = wmThumbSvg(t);
    return `
    <button type="button" class="wm-tpl-card ${t.id === selected ? "active" : ""}"
            data-tpl="${escapeHtml(t.id)}">
      <div class="wm-tpl-thumb">${svg}</div>
      <div class="wm-tpl-info">
        <div class="name">${escapeHtml(t.name || t.id)}</div>
        <div class="desc">${escapeHtml(t.desc || "")}</div>
      </div>
    </button>`;
  }).join("");
  grid.querySelectorAll(".wm-tpl-card").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-tpl");
      if (id === wmSelected()) return;
      wmSetSelected(id);
      wmRenderTemplatePicker();
      wmRefreshPreview();
    });
  });
}

function wmShowSpinner(show) {
  $("wm-preview-spinner").classList.toggle("hidden", !show);
  if (show) $("wm-preview-img").classList.add("hidden");
}

function wmFillInfo(res) {
  const meta = wmMeta();
  const current = wmTemplates().find((t) => t.id === wmSelected()) || {};
  const info = WM.mode === "beautify" ? (res?.style || current) : current;
  let rows;
  if (WM.mode === "watermark") {
    const exif = res?.exif || {};
    const camera = [exif.make, exif.model].filter(Boolean).join(" ") || "未读取到机身信息";
    const params = [
      exif.lens,
      exif.focal_length,
      exif.f_number,
      exif.exposure,
      exif.iso,
      exif.datetime,
    ].filter(Boolean).join(" · ") || "照片无完整 EXIF 时自动留空";
    rows = [
      ["当前水印", info.name || "标准底栏"],
      ["样式", info.desc || "专业相机参数水印"],
      ["相机", camera],
      ["参数", params],
      ["输出目录", meta.outputLabel],
    ];
  } else {
    rows = [
      ["当前风格", info.name || "韩式大头贴"],
      ["效果", info.desc || "导出为独立副本，不覆盖原图。"],
      ["输出目录", meta.outputLabel],
      ["说明", "生成副本，原 winners 保持不变"],
    ];
  }
  $("wm-exif-list").innerHTML = rows.map(([k, v]) => `
    <li><span class="k">${escapeHtml(k)}</span><span class="v">${escapeHtml(v)}</span></li>
  `).join("");
}

async function wmRefreshPreview() {
  if (WM.isExporting) return;
  WM.previewSeq += 1;
  const mySeq = WM.previewSeq;
  wmShowSpinner(true);
  try {
    const res = await fetchJSON(wmMeta().previewUrl, {
      method: "POST",
      body: JSON.stringify(wmCfg()),
    });
    if (mySeq !== WM.previewSeq) return;
    $("wm-preview-img").src = "data:image/jpeg;base64," + res.image_b64;
    $("wm-preview-img").classList.remove("hidden");
    wmShowSpinner(false);
    WM.totalWinners = res.total_winners || 1;
    WM.previewIdx = res.preview_index || 0;
    $("wm-preview-idx").textContent = `${WM.previewIdx + 1} / ${WM.totalWinners}`;
    $("wm-preview-title").textContent = `预览 · ${res.source_name}`;
    wmFillInfo(res);
  } catch (e) {
    if (mySeq !== WM.previewSeq) return;
    wmShowSpinner(false);
    toast("预览失败：" + e.message);
  }
}

function wmRefreshDebounced(delay = 300) {
  clearTimeout(WM.debounceHandle);
  WM.debounceHandle = setTimeout(wmRefreshPreview, delay);
}

async function wmOpen(mode = "beautify") {
  WM.mode = mode;
  const meta = wmMeta();
  $("wm-modal").classList.remove("hidden");
  $("wm-title").textContent = meta.title;
  $("wm-sub").textContent = meta.sub;
  $("wm-template-title").textContent = meta.templateTitle;
  WM.previewIdx = 0;
  WM.isExporting = false;
  $("wm-progress").classList.add("hidden");
  $("wm-stop").classList.add("hidden");
  $("wm-open-out").classList.add("hidden");
  $("wm-start").classList.remove("hidden");
  $("wm-start").disabled = false;
  $("wm-start").textContent = "开始导出";
  $("wm-cancel").textContent = "关闭";
  $("wm-preview-img").classList.add("hidden");
  await wmLoadTemplates();
  if (!wmTemplates().find((t) => t.id === wmSelected())) {
    wmSetSelected((wmTemplates()[0] && wmTemplates()[0].id) || meta.defaultTemplate);
  }
  wmRenderTemplatePicker();
  wmRefreshPreview();
}

function wmClose() {
  if (WM.isExporting) {
    if (!confirm("导出正在进行中，确定关闭吗？后台仍会继续。")) return;
  }
  $("wm-modal").classList.add("hidden");
  clearTimeout(WM.debounceHandle);
  if (WM.pollHandle) { clearTimeout(WM.pollHandle); WM.pollHandle = null; }
}

async function wmStart() {
  $("wm-start").disabled = true;
  $("wm-start").textContent = "启动中…";
  try {
    const res = await fetchJSON(wmMeta().startUrl, {
      method: "POST",
      body: JSON.stringify(wmCfg()),
    });
    WM.isExporting = true;
    $("wm-progress").classList.remove("hidden");
    $("wm-start").classList.add("hidden");
    $("wm-stop").classList.remove("hidden");
    $("wm-progress-text").textContent = `开始导出 ${res.total} 张…`;
    $("wm-progress-fill").style.width = "0%";
    wmPoll();
  } catch (e) {
    $("wm-start").disabled = false;
    $("wm-start").textContent = "开始导出";
    toast("启动失败：" + e.message);
  }
}

async function wmPoll() {
  try {
    const s = await fetchJSON(wmMeta().statusUrl);
    const done = s.done || 0;
    const total = s.total || 1;
    const pct = Math.round(done / total * 100);
    $("wm-progress-fill").style.width = pct + "%";
    if (s.status === "running") {
      $("wm-progress-text").textContent =
        `处理中 ${done}/${total} · ${s.current || ""}`;
      WM.pollHandle = setTimeout(wmPoll, 500);
    } else if (s.status === "done") {
      WM.isExporting = false;
      $("wm-progress-text").textContent =
        `完成 · 成功 ${s.ok} / ${s.total}` +
        (s.failed_count ? `，失败 ${s.failed_count}` : "") +
        ` · 用时 ${Math.round(s.elapsed)}s`;
      $("wm-stop").classList.add("hidden");
      $("wm-open-out").classList.remove("hidden");
      $("wm-cancel").textContent = "完成";
      toast(`${wmMeta().doneToast}（${s.ok} 张）`);
    } else if (s.status === "cancelled") {
      WM.isExporting = false;
      $("wm-progress-text").textContent = `已中止 · 完成 ${s.ok}/${s.total}`;
      $("wm-stop").classList.add("hidden");
      $("wm-start").classList.remove("hidden");
      $("wm-start").disabled = false;
      $("wm-start").textContent = "重新开始";
    } else if (s.status === "error") {
      WM.isExporting = false;
      $("wm-progress-text").textContent = `出错：${s.error || "未知"}`;
      $("wm-stop").classList.add("hidden");
      $("wm-start").classList.remove("hidden");
      $("wm-start").disabled = false;
      $("wm-start").textContent = "重新开始";
    }
  } catch (e) {
    WM.pollHandle = setTimeout(wmPoll, 1500);
  }
}

async function wmStop() {
  if (!confirm(wmMeta().stopConfirm)) return;
  try {
    await fetchJSON(wmMeta().cancelUrl, { method: "POST", body: JSON.stringify({}) });
  } catch (e) { toast("中止失败：" + e.message); }
}

async function wmOpenOut() {
  try {
    await fetchJSON(wmMeta().openUrl, { method: "POST", body: JSON.stringify({}) });
  } catch (e) { toast("打开失败：" + e.message); }
}

// 绑定事件
$("btn-watermark").addEventListener("click", () => wmOpen("watermark"));
$("btn-beautify").addEventListener("click", () => wmOpen("beautify"));
$("wm-close").addEventListener("click", wmClose);
$("wm-cancel").addEventListener("click", wmClose);
$("wm-start").addEventListener("click", wmStart);
$("wm-stop").addEventListener("click", wmStop);
$("wm-open-out").addEventListener("click", wmOpenOut);
$("wm-prev").addEventListener("click", () => {
  if (WM.totalWinners <= 1) return;
  WM.previewIdx = (WM.previewIdx - 1 + WM.totalWinners) % WM.totalWinners;
  wmRefreshPreview();
});
$("wm-next").addEventListener("click", () => {
  if (WM.totalWinners <= 1) return;
  WM.previewIdx = (WM.previewIdx + 1) % WM.totalWinners;
  wmRefreshPreview();
});
$("wm-modal").addEventListener("click", (e) => {
  if (e.target.id === "wm-modal") wmClose();
});

function initVisualMotion() {
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
  if (reduceMotion) return;

  const parallaxEls = Array.from(document.querySelectorAll(
    ".hero-title, .hero-sub, .hero-meta, .done-hero, .winners-section, .wm-modal-card"
  ));
  parallaxEls.forEach((el, i) => {
    el.classList.add("parallax-layer");
    el.dataset.depth = String(0.012 + (i % 4) * 0.006);
  });

  let raf = 0;
  const updateParallax = () => {
    raf = 0;
    const active = document.querySelector(".view.active");
    const scrollTop = window.scrollY || document.documentElement.scrollTop || active?.scrollTop || 0;
    const viewportH = window.innerHeight || 800;
    parallaxEls.forEach((el) => {
      const rect = el.getBoundingClientRect();
      const depth = Number(el.dataset.depth || 0.015);
      const centerOffset = rect.top + rect.height / 2 - viewportH / 2;
      const y = Math.max(-28, Math.min(28, -centerOffset * depth + scrollTop * depth * 0.08));
      el.style.setProperty("--pf-parallax-y", `${y.toFixed(2)}px`);
    });
  };
  const queueParallax = () => {
    if (!raf) raf = requestAnimationFrame(updateParallax);
  };

  window.addEventListener("scroll", queueParallax, { passive: true });
  document.addEventListener("scroll", queueParallax, true);
  window.addEventListener("resize", queueParallax);
  queueParallax();

  const interactiveSelector = ".btn, .btn-ghost, .wm-tpl-card, .meta-item, .fast-only-card";
  document.addEventListener("pointerdown", (e) => {
    const el = e.target.closest?.(interactiveSelector);
    if (!el) return;
    el.classList.remove("is-pressed-pop");
    void el.offsetWidth;
    el.classList.add("is-pressed-pop");
    window.setTimeout(() => el.classList.remove("is-pressed-pop"), 360);
  }, { passive: true });

  document.addEventListener("pointermove", (e) => {
    const el = e.target.closest?.(interactiveSelector);
    if (!el) return;
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const x = e.clientX / rect.width - rect.left / rect.width - 0.5;
    const y = e.clientY / rect.height - rect.top / rect.height - 0.5;
    el.style.transform =
      `perspective(900px) rotateX(${(-y * 2.2).toFixed(2)}deg) ` +
      `rotateY(${(x * 3.0).toFixed(2)}deg) translate3d(0, -1px, 0)`;
  }, { passive: true });

  document.addEventListener("pointerout", (e) => {
    const el = e.target.closest?.(interactiveSelector);
    if (!el || (e.relatedTarget && el.contains(e.relatedTarget))) return;
    el.style.transform = "";
  }, true);
}

initVisualMotion();
bootstrap();
