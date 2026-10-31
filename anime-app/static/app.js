/* ============ 超越次元 SPA · 纯追番版 ============ */
"use strict";

const $app = document.getElementById("app");
const $loading = document.getElementById("loading");
const $toast = document.getElementById("toast");

/* ---------- 工具 ---------- */
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const img = u => u ? "/api/proxy/img?u=" + encodeURIComponent(u) : "";
let loadCount = 0;
function loading(on) { loadCount += on ? 1 : -1; if (loadCount < 0) loadCount = 0; $loading.classList.toggle("show", loadCount > 0); }
function toast(msg) {
  $toast.textContent = msg; $toast.classList.add("show");
  clearTimeout($toast._t); $toast._t = setTimeout(() => $toast.classList.remove("show"), 2200);
}
async function api(path) {
  const r = await fetch("/api" + path);
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || "请求失败");
  // 首屏数据就绪: 通知启动动画可以收尾 (只关心首页)
  if (path.indexOf("/anime/home") === 0 && window.__bootDataReady) window.__bootDataReady();
  return j.data;
}
function store(key) {
  return {
    get() { try { return JSON.parse(localStorage.getItem(key)) || []; } catch { return []; } },
    set(v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch { toast("保存失败: 本地存储不可用"); } },
    add(item, max = 50) {
      const list = this.get().filter(x => x.key !== item.key);
      list.unshift(item);
      if (list.length > max) list.length = max;
      this.set(list);
    },
    has(key2) { return this.get().some(x => x.key === key2); },
    del(key2) { this.set(this.get().filter(x => x.key !== key2)); },
  };
}
const favs = store("cy_favs");
const hist = store("cy_hist_anime");

/* ---------- 观看日志 (集数级别, 按动漫分组) ---------- */
const WLOG_KEY = "cy_watch_log";
function wlogGetAll() {
  try { return JSON.parse(localStorage.getItem(WLOG_KEY)) || {}; } catch { return {}; }
}
function wlogSaveAll(obj) { localStorage.setItem(WLOG_KEY, JSON.stringify(obj)); }
function wlogRecord(animeId, title, cover, ep, epName) {
  const all = wlogGetAll();
  const key = String(animeId);
  if (!all[key]) all[key] = { title: title || "", cover: cover || "", epMap: {}, lastTs: 0 };
  all[key].title = title || all[key].title;
  all[key].cover = cover || all[key].cover;
  all[key].lastTs = Date.now();
  all[key].epMap[String(ep)] = all[key].epMap[String(ep)]
    ? { ...all[key].epMap[String(ep)], epName: epName || `第${ep}集`, ts: Date.now() }
    : { epName: epName || `第${ep}集`, ts: Date.now(), duration: 0 };
  wlogSaveAll(all);
}
function wlogGetAnime(animeId) { return wlogGetAll()[String(animeId)] || null; }
/* 累计某集观看时长 (秒) */
function wlogAddDuration(animeId, ep, sec) {
  const all = wlogGetAll();
  const a = all[String(animeId)];
  if (!a || !a.epMap) return;
  const rec = a.epMap[String(ep)];
  if (!rec) return;
  rec.duration = (rec.duration || 0) + sec;
  wlogSaveAll(all);
}
/* 播放页时长会话: 播放中每 5 秒累计一次, 切页/切集自动停止 */
let _wlogTimer = null, _wlogCur = null;
function wlogStopSession() {
  if (_wlogTimer) {
    clearInterval(_wlogTimer);
    // 离开播放页/切集前保存最后播放位置
    const v = document.getElementById("videoPlayer");
    if (v && _wlogCur && v.duration && !v.ended) wposSave(_wlogCur.animeId, _wlogCur.ep, v.currentTime, v.duration);
  }
  _wlogTimer = null;
  _wlogCur = null;
}
function wlogStartSession(animeId, ep) {
  wlogStopSession();
  _wlogCur = { animeId: String(animeId), ep: String(ep) };
  _wlogTimer = setInterval(() => {
    const v = document.getElementById("videoPlayer");
    if (!v || v.paused || v.ended || !_wlogCur) return;
    wlogAddDuration(_wlogCur.animeId, _wlogCur.ep, 5);
    wposSave(_wlogCur.animeId, _wlogCur.ep, v.currentTime, v.duration);   // 每 5 秒同步进度
  }, 5000);
}

/* ---------- 观看进度 (精确到秒, 复用观看日志的集数记录) ---------- */
function wposSave(animeId, ep, pos, dur) {
  const all = wlogGetAll();
  const a = all[String(animeId)];
  if (!a || !a.epMap || !a.epMap[String(ep)]) return;   // 只为已记录的集数保存
  const rec = a.epMap[String(ep)];
  rec.pos = Math.max(0, Math.floor(pos || 0));
  if (dur) rec.dur = Math.floor(dur);
  wlogSaveAll(all);
}
function wposGet(animeId, ep) {
  const a = wlogGetAll()[String(animeId)];
  const rec = a && a.epMap && a.epMap[String(ep)];
  return rec && rec.pos ? rec : null;
}
function wposClear(animeId, ep) {
  const all = wlogGetAll();
  const a = all[String(animeId)];
  if (!a || !a.epMap || !a.epMap[String(ep)]) return;
  delete a.epMap[String(ep)].pos;
  delete a.epMap[String(ep)].dur;
  wlogSaveAll(all);
}
function _fmtPos(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const p = n => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${m}:${p(s)}`;
}

/* ---------- 线路偏好 + 源+ 外部聚合片源 + Bangumi 评分 ---------- */
const LINE_PREF_KEY = "cy_line_pref";
function linePrefGet(animeId) {
  try { return (JSON.parse(localStorage.getItem(LINE_PREF_KEY)) || {})[String(animeId)] || ""; } catch { return ""; }
}
function linePrefSet(animeId, line) {
  try {
    const all = JSON.parse(localStorage.getItem(LINE_PREF_KEY)) || {};
    all[String(animeId)] = String(line);
    localStorage.setItem(LINE_PREF_KEY, JSON.stringify(all));
  } catch { }
}
/* 集数的观看日志键: AGE 线用 ep 原值; 源+ 直链线路用集数名中的数字 (跨线路对齐) */
function epLogKey(line, ep, name) {
  if (String(line || "").startsWith("x")) {
    const m = String(name || "").match(/\d+/);
    if (m) return String(parseInt(m[0], 10));
    return String(ep);
  }
  return String(ep);
}
/* 生成播放页跳转 hash: 源+ 直链含 "/", 需双重编码规避路由按 "/" 分割 */
function epHash(id, line, ep, name) {
  const epSeg = String(line).startsWith("x")
    ? encodeURIComponent(encodeURIComponent(ep))
    : encodeURIComponent(ep);
  return `#/watch/${esc(id)}/${esc(line)}/${epSeg}/${encodeURIComponent(name || "")}`;
}
/* 拉取源+ 外部线路 (服务端有缓存, 失败返回空) */
async function fetchExtLines(title) {
  if (!title) return [];
  try {
    const r = await api("/anime/extlines?title=" + encodeURIComponent(title));
    return (r || []).filter(l => l.episodes && l.episodes.length);
  } catch { return []; }
}
/* Bangumi 评分异步渲染 (加载慢/失败时静默留空, 不阻塞页面) */
async function bgmRender(elId, title, compact) {
  const first = document.getElementById(elId);
  if (!first || !title) return;
  try {
    const b = await api("/bangumi?kw=" + encodeURIComponent(title));
    const el = document.getElementById(elId);
    if (!el || !b || !b.id || !(b.score > 0)) return;
    const src = b.source === "AniList" ? "AniList" : "Bangumi";
    el.innerHTML = compact
      ? `<span class="bgm-chip">⭐ ${b.score}</span><span class="bgm-src">${src} 评分${b.name_cn ? ` · ${esc(b.name_cn)}` : ""}</span>`
      : `<span class="bgm-score">⭐ ${b.score}</span><span class="bgm-meta">${src} 评分${b.rank ? ` · 排名 #${b.rank}` : ""}${b.total ? ` · ${b.total} 人打分` : ""}</span>`;
  } catch { }
}
function wlogFlattenRecent(limit = 30) {
  // 展平所有动漫所有集数, 按 ts 倒序, 返回 [{ animeId, title, cover, ep, epName, ts, duration }]
  const all = wlogGetAll();
  const flat = [];
  for (const [aid, info] of Object.entries(all)) {
    for (const [ep, rec] of Object.entries(info.epMap || {})) {
      flat.push({ animeId: aid, title: info.title, cover: info.cover, ep, epName: rec.epName, ts: rec.ts, duration: rec.duration || 0, pos: rec.pos || 0, dur: rec.dur || 0 });
    }
  }
  flat.sort((a, b) => b.ts - a.ts);
  return flat.slice(0, limit);
}
function wlogClear() { localStorage.removeItem(WLOG_KEY); }

/* ---------- 时间格式化辅助 ---------- */
function _fmtFullTime(ts) {
  const dt = new Date(ts);
  const p = n => String(n).padStart(2, "0");
  return `${dt.getFullYear()}/${p(dt.getMonth() + 1)}/${p(dt.getDate())} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
}
function _fmtDuration(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  if (sec < 60) return sec + "秒";
  const m = Math.floor(sec / 60);
  if (m < 60) return m + "分钟";
  const h = Math.floor(m / 60);
  return h + "小时" + (m % 60 ? (m % 60) + "分钟" : "");
}
function _fmtDateKey(ts) {
  const dt = new Date(ts);
  const t = new Date();
  const today = new Date(t.getFullYear(), t.getMonth(), t.getDate()).getTime();
  const d0 = new Date(dt.getFullYear(), dt.getMonth(), dt.getDate()).getTime();
  const diff = (today - d0) / 86400000;
  if (diff === 0) return "今天";
  if (diff === 1) return "昨天";
  if (diff === 2) return "前天";
  if (diff < 7) return `${Math.floor(diff)}天前`;
  return `${dt.getMonth() + 1}月${dt.getDate()}日`;
}
function wlogGroupByDate(list) {
  const groups = {};
  for (const item of list) {
    const key = _fmtDateKey(item.ts);
    if (!groups[key]) groups[key] = [];
    groups[key].push(item);
  }
  // 按日期倒序: 今天 -> 昨天 -> ...
  const order = ["今天", "昨天", "前天"];
  return Object.entries(groups).sort(([a], [b]) => {
    const ai = order.indexOf(a), bi = order.indexOf(b);
    if (ai >= 0 && bi >= 0) return ai - bi;
    if (ai >= 0) return -1;
    if (bi >= 0) return 1;
    return a > b ? -1 : 1;
  }).map(([k, v]) => ({ date: k, items: v }));
}

/* ---------- 收藏按钮 ---------- */
function favBtn(id, item) {
  const key = "anime:" + id;
  const on = favs.has(key);
  return `<button class="fav-btn ${on ? "on" : ""}" id="favBtn" data-id="${esc(id)}" data-item='${esc(JSON.stringify(item))}'>${on ? "★ 已收藏" : "☆ 收藏"}</button>`;
}
document.addEventListener("click", e => {
  const btn = e.target.closest("#favBtn");
  if (!btn) return;
  const key = "anime:" + btn.dataset.id;
  if (favs.has(key)) { favs.del(key); toast("已取消收藏"); }
  else { favs.add({ key, ...JSON.parse(btn.dataset.item) }); toast("已加入收藏"); }
  btn.classList.toggle("on");
  btn.textContent = btn.classList.contains("on") ? "★ 已收藏" : "☆ 收藏";
});

/* ---------- 卡片 ---------- */
function animeCard(a) {
  const seasonBadge = a.season ? `<span class="season-badge">🆕 本季新番</span>` : "";
  // 一周更新表来自外部日程时没有 AGE 番剧 ID, 点击改为站内搜索该标题
  const go = a.id
    ? ` onclick="location.hash='#/anime/${esc(a.id)}'"`
    : (a.search ? ` onclick="location.hash='#/search/${encodeURIComponent(a.search)}'"` : "");
  return `<div class="card"${go}>
    <div class="cover"><img src="${img(a.cover)}" loading="lazy" decoding="async" fetchpriority="low" alt="">
      <div class="cover-mask"><span class="play-ico">▶</span></div>
      ${seasonBadge}
      ${a.ep ? `<span class="ep-tag">${esc(a.ep)}</span>` : ""}</div>
    <div class="title">${esc(a.title)}</div>
  </div>`;
}

/* ---------- 横滑卡片 (MI动漫风格, 悬浮显示更多信息) ---------- */
function hCard(a) {
  const seasonBadge = a.season ? `<span class="season-badge small">🆕 新番</span>` : "";
  // 一周更新表来自外部日程时没有 AGE 番剧 ID, 点击改为站内搜索该标题
  const go = a.id
    ? ` onclick="location.hash='#/anime/${esc(a.id)}'"`
    : (a.search ? ` onclick="location.hash='#/search/${encodeURIComponent(a.search)}'"` : "");
  return `<div class="h-card"${go}>
    <div class="h-cover"><img src="${img(a.cover)}" loading="lazy" decoding="async" fetchpriority="low" alt="">
      <div class="h-mask"><span class="play-ico">▶</span></div>
      ${seasonBadge}
      ${a.ep ? `<span class="h-ep">${esc(a.ep)}</span>` : ""}</div>
    <div class="h-title">${esc(a.title)}</div>
    ${a.info ? `<div class="h-info">${esc(a.info.slice(0, 28))}</div>` : ""}
  </div>`;
}
function hScrollHtml(items, limit = 20) {
  if (!items || !items.length) return `<div class="empty" style="padding:24px"><div class="icon">📭</div>暂无数据</div>`;
  return items.slice(0, limit).map(hCard).join("");
}

/* ---------- 轮播 Banner ---------- */
let _cTimer = null, _cIdx = 0, _cSlides = [];
function renderCarousel(slides) {
  _cSlides = slides || [];
  _cIdx = 0;
  const track = document.getElementById("carouselTrack");
  if (!track) return;
  if (!slides.length) {
    track.innerHTML = `<div class="carousel-slide active"><div class="carousel-info"><h2>✦ 超越次元</h2><p>次元列车已到站 · 实时更新 · 4K 高清画质</p></div></div>`;
    const _pg0 = document.getElementById("carouselProg");
    if (_pg0) _pg0.style.display = "none";
    return;
  }
  track.innerHTML = slides.map((a, i) => `
    <div class="carousel-slide ${i === 0 ? "active" : ""}" onclick="location.hash='#/anime/${esc(a.id)}'">
      <div class="carousel-bg"><img src="${img(a.cover)}" decoding="async" fetchpriority="low" alt=""></div>
      <div class="carousel-info">
        <span class="carousel-badge">🔥 推荐番剧</span>
        <h2>${esc(a.title)}</h2>
        <p>${esc((a.info || a.ep || "点击立即观看").slice(0, 60))}</p>
        <span class="carousel-btn">立即观看 ›</span>
      </div>
    </div>`).join("");
  const dots = document.getElementById("carouselDots");
  if (dots) dots.innerHTML = slides.map((_, i) =>
    `<span class="carousel-dot ${i === 0 ? "active" : ""}" onclick="carouselGoTo(${i})"></span>`).join("");
  startCarouselTimer();
}
function startCarouselTimer() {
  if (_cTimer) clearInterval(_cTimer);
  _cTimer = setInterval(() => { if (_cSlides.length > 1) window.carouselSlide(1); }, 5000);
  // 换图进度条: 与 5s 自动轮播同步, 每次换图重新充满 (单张时隐藏)
  const prog = document.getElementById("carouselProg");
  if (prog) prog.style.display = _cSlides.length > 1 ? "" : "none";
  const bar = document.getElementById("carouselBar");
  if (bar && _cSlides.length > 1) { bar.classList.remove("run"); void bar.offsetWidth; bar.classList.add("run"); }
}
window.carouselSlide = function (dir) {
  if (!_cSlides.length) return;
  _cIdx = (_cIdx + dir + _cSlides.length) % _cSlides.length;
  window.carouselGoTo(_cIdx);
};
window.carouselGoTo = function (i) {
  _cIdx = i;
  const track = document.getElementById("carouselTrack");
  if (track) track.style.transform = `translateX(-${i * 100}%)`;
  document.querySelectorAll(".carousel-slide").forEach((s, idx) => s.classList.toggle("active", idx === i));
  document.querySelectorAll(".carousel-dot").forEach((d, idx) => d.classList.toggle("active", idx === i));
  startCarouselTimer();
};

/* ---------- 周几切换 ---------- */
let _updateData = null;
window.weekdaySwitch = function (day) {
  document.querySelectorAll(".weekday-tab").forEach(t => t.classList.toggle("active", Number(t.dataset.day) === day));
  if (!_updateData) return;
  const list = (_updateData.groups || {})[String(day)] || [];
  const el = document.getElementById("weekdayScroll");
  if (el) el.innerHTML = list.length
    ? list.slice(0, 30).map(hCard).join("")
    : `<div class="empty" style="padding:24px"><div class="icon">📅</div>该日暂无更新</div>`;
};

/* 分类标签 → Emoji 映射 (44 个真实站点类型) */
const GENRE_EMOJI_MAP = {
  "搞笑": "😂", "运动": "⚽", "励志": "💪", "热血": "🔥", "战斗": "⚔️",
  "竞技": "🏆", "校园": "🎓", "青春": "🌸", "爱情": "💕", "恋爱": "💗",
  "冒险": "🗺️", "后宫": "👑", "百合": "🌷", "治愈": "🍵", "萝莉": "👶",
  "魔法": "✨", "悬疑": "🔍", "推理": "🕵️", "奇幻": "🧚", "科幻": "🚀",
  "游戏": "🎮", "神魔": "👹", "恐怖": "👻", "血腥": "🩸", "机战": "🤖",
  "战争": "💣", "犯罪": "🚨", "历史": "🏛️", "社会": "🌍", "职场": "💼",
  "剧情": "🎭", "伪娘": "🎀", "耽美": "💙", "童年": "🧸", "教育": "📚",
  "亲子": "👨‍👩‍👧", "真人": "🎬", "歌舞": "🎤", "肉番": "🍖", "美少女": "💖",
  "轻小说": "📖", "吸血鬼": "🧛", "女性向": "🌸", "泡面番": "🍜", "欢乐向": "🎉",
};
const genreEmoji = name => GENRE_EMOJI_MAP[name] || "🎬";
const genreUrl = name => "#/catalog?genre=" + encodeURIComponent(name);

/* ---------- 无限滚动加载 ---------- */
let _scroll = { page: 1, genre: 0, order: "time", loading: false, hasMore: true, seen: new Set(), observer: null, gridId: "" };

function setupInfiniteScroll(gridId, startPage, genre, order) {
  // 清理旧 observer
  if (_scroll.observer) { try { _scroll.observer.disconnect(); } catch {} _scroll.observer = null; }
  // 收集已显示的 anime ID (从卡片 onclick 属性提取)
  const seen = new Set();
  document.querySelectorAll(`#${gridId} [onclick]`).forEach(c => {
    const m = (c.getAttribute("onclick") || "").match(/#\/anime\/(\d+)/);
    if (m) seen.add(m[1]);
  });
  _scroll = { page: startPage, genre: genre || 0, order: order || "time", loading: false, hasMore: true, seen, observer: null, gridId };
  // 添加哨兵 + 加载指示器
  let sentinel = document.getElementById("infSentinel");
  if (sentinel) sentinel.remove();
  let loader = document.getElementById("infLoader");
  if (loader) loader.remove();
  const grid = document.getElementById(gridId);
  if (!grid) return;
  sentinel = document.createElement("div");
  sentinel.id = "infSentinel";
  sentinel.style.height = "2px";
  loader = document.createElement("div");
  loader.id = "infLoader";
  loader.className = "infinite-loader";
  loader.innerHTML = tvLoad() + '<p>加载更多番剧...</p>';
  loader.style.display = "none";
  grid.parentNode.insertBefore(sentinel, grid.nextSibling);
  grid.parentNode.insertBefore(loader, sentinel.nextSibling);
  _scroll.observer = new IntersectionObserver(entries => {
    if (entries[0].isIntersecting && !_scroll.loading && _scroll.hasMore) {
      loadMoreToGrid();
    }
  }, { rootMargin: "500px" });
  _scroll.observer.observe(sentinel);
}

async function loadMoreToGrid() {
  if (_scroll.loading || !_scroll.hasMore || !_scroll.gridId) return;
  _scroll.loading = true;
  _scroll.page++;
  const loader = document.getElementById("infLoader");
  if (loader) { loader.style.display = "flex"; loader.innerHTML = tvLoad() + '<p>加载更多番剧...</p>'; }
  try {
    const data = await api(`/anime/catalog?page=${_scroll.page}&genre=${encodeURIComponent(_scroll.genre)}&order=${_scroll.order}`);
    const grid = document.getElementById(_scroll.gridId);
    if (grid) {
      const newCards = (data.items || []).filter(a => !_scroll.seen.has(String(a.id)));
      newCards.forEach(a => _scroll.seen.add(String(a.id)));
      if (newCards.length) {
        grid.insertAdjacentHTML("beforeend", newCards.map(animeCard).join(""));
      }
    }
    _scroll.hasMore = data.has_more;
    if (!_scroll.hasMore) {
      if (loader) { loader.innerHTML = "<p>🎉 已加载全部番剧</p>"; }
      const sentinel = document.getElementById("infSentinel");
      if (sentinel) sentinel.style.display = "none";
      setTimeout(() => { if (loader) loader.style.display = "none"; }, 2500);
    }
  } catch (e) {
    _scroll.hasMore = false;
    if (loader) loader.innerHTML = `<p>加载失败: ${esc(e.message)}</p>`;
  } finally {
    _scroll.loading = false;
    if (_scroll.hasMore && loader) loader.style.display = "none";
  }
}

/* ---------- 页面：首页 (MI动漫风格) ---------- */
async function pageHome() {
  loading(true);
  document.title = "超越次元 - 追番神器";
  $app.innerHTML = `
    <div class="carousel" id="carousel">
      <div class="carousel-track" id="carouselTrack"></div>
      <button class="carousel-arrow prev" onclick="carouselSlide(-1)">‹</button>
      <button class="carousel-arrow next" onclick="carouselSlide(1)">›</button>
      <div class="carousel-dots" id="carouselDots"></div>
      <div class="carousel-progress" id="carouselProg"><i id="carouselBar"></i></div>
    </div>

    <div class="home-hint" id="cineHomeHint" style="display:none">
      <span>🎬 新增电影预设 · 遮幅 · 对白增强</span>
      <i onclick="cineHintClose()">×</i>
    </div>

    <div class="section">
      <div class="section-head"><h2>⚡ 快捷分类</h2></div>
      <div class="quick-genres" id="quickGenres"><div class="empty" style="padding:16px">加载中...</div></div>
    </div>

    <div class="section">
      <div class="section-head"><h2>🔥 今日更新</h2></div>
      <div class="h-scroll" id="todayScroll"><div class="empty" style="padding:24px">加载中...</div></div>
    </div>

    <div class="section" id="wlogSection">
      <div class="section-head"><h2>🕐 我的追番时间表</h2><button class="tool-btn sm" onclick="wlogClear();location.reload()" title="清除所有观看记录">🗑</button></div>
      <div class="wlog-timeline" id="wlogTimeline"></div>
    </div>

    <div class="section">
      <div class="section-head"><h2>📅 一周更新表</h2></div>
      <div class="weekday-tabs" id="weekdayTabs"></div>
      <div class="h-scroll" id="weekdayScroll"><div class="empty" style="padding:24px">加载中...</div></div>
    </div>

    <div class="section">
      <div class="section-head"><h2>🌟 热门推荐</h2></div>
      <div class="h-scroll" id="hotScroll"><div class="empty" style="padding:24px">加载中...</div></div>
    </div>

    <div class="section">
      <div class="section-head"><h2>🆕 新番放送</h2></div>
      <div class="h-scroll" id="newScroll"><div class="empty" style="padding:24px">加载中...</div></div>
    </div>

    <div class="section">
      <div class="section-head"><h2>🎨 插图画廊</h2><a class="more" href="#/illus">更多 ›</a></div>
      <div class="illus-cols" id="homeIllus"><div class="empty" style="padding:24px">加载中...</div></div>
    </div>

    <div class="section">
      <div class="section-head"><h2>🎨 分类导航</h2><a class="more" href="#/catalog">全部分类 ›</a></div>
      <div class="cat-nav" id="catNav"><div class="empty">加载中...</div></div>
    </div>

    <div class="section">
      <div class="section-head"><h2>📺 全部番剧</h2><a class="more" href="#/catalog">更多 ›</a></div>
      <div class="grid" id="homeGrid"><div class="empty">加载中...</div></div>
    </div>`;

  // 新功能轻提示: 关过就不再出现
  try {
    if (!localStorage.getItem("cine_home_hint")) {
      const h = document.getElementById("cineHomeHint");
      if (h) h.style.display = "";
    }
  } catch { }
  window.cineHintClose = function () {
    try { localStorage.setItem("cine_home_hint", "1"); } catch { }
    const el = document.getElementById("cineHomeHint");
    if (el) el.remove();
  };

  // 并行请求
  const [homeP, updateP, catalogP, illusP] = [
    api("/anime/home").catch(() => null),
    api("/anime/update").catch(() => null),
    api("/anime/catalog?page=1&genre=0").catch(() => null),
    api("/anime/illus?n=10").catch(() => null),
  ];

  // 4. 插图画廊
  illusP.then(list => {
    const el = document.getElementById("homeIllus");
    if (!el) return;
    if (!list || !list.length) { el.closest(".section").style.display = "none"; return; }
    el.innerHTML = list.map(x => illusCard(x.url)).join("");
  });

  // 1. 主列表 + 轮播 + 热门 + 新番
  homeP.then(list => {
    list = list || [];
    if (!list.length) {
      const el = document.getElementById("homeGrid");
      if (el) el.innerHTML = `<div class="empty"><div class="icon">📡</div>番剧源暂不可用</div>`;
      return;
    }
    // 轮播 Banner (前8条)
    renderCarousel(list.slice(0, 8));
    // 热门推荐 (9-20)
    const hotEl = document.getElementById("hotScroll");
    if (hotEl) hotEl.innerHTML = hScrollHtml(list.slice(8, 20), 12);
    // 新番放送 (有 ep 的优先, 否则取 20-32)
    const newEl = document.getElementById("newScroll");
    if (newEl) {
      const withEp = list.filter(x => x.ep);
      const newItems = withEp.length >= 8 ? withEp : list.slice(20, 32);
      newEl.innerHTML = hScrollHtml(newItems, 12);
    }
  });

  // 全部番剧网格 — 用 catalog 数据 (按更新时间排序), 无限滚动从第2页续抓
  catalogP.then(data => {
    const gridEl = document.getElementById("homeGrid");
    if (!gridEl) return;
    if (!data || !data.items || !data.items.length) {
      gridEl.innerHTML = `<div class="empty"><div class="icon">📡</div>番剧源暂不可用</div>`;
      return;
    }
    gridEl.innerHTML = data.items.map(animeCard).join("");
    setupInfiniteScroll("homeGrid", 2, 0);
  }).catch(e => {
    const el = document.getElementById("homeGrid");
    if (el) el.innerHTML = `<div class="empty"><div class="icon">📡</div>${esc(e.message)}</div>`;
  });

  // 1.5 观看时间表 (localStorage, 同步渲染)
  {
    const timelineEl = document.getElementById("wlogTimeline");
    const secEl = document.getElementById("wlogSection");
    const flat = wlogFlattenRecent(30);
    if (!flat.length) {
      if (secEl) secEl.style.display = "none";
    } else {
      const groups = wlogGroupByDate(flat);
      timelineEl.innerHTML = groups.map(g => `
        <div class="wlog-group">
          <div class="wlog-date">${esc(g.date)}<span class="wlog-count">${g.items.length}集</span></div>
          <div class="wlog-list">
            ${g.items.map(it => `
              <div class="wlog-item" onclick="location.hash='#/anime/${esc(it.animeId)}'">
                ${it.cover ? `<div class="wlog-cover"><img src="${img(it.cover)}" loading="lazy" alt=""></div>` : `<div class="wlog-cover none">📺</div>`}
                <div class="wlog-info">
                  <div class="wlog-title">${esc(it.title) || "未知动漫"}</div>
                  <div class="wlog-meta">
                    <span class="wlog-ep">▶ ${esc(it.epName || `第${it.ep}集`)}</span>
                    ${it.pos && it.dur ? `<span class="wlog-pos">📡 ${_fmtPos(it.pos)} / ${_fmtPos(it.dur)}</span>` : ""}
                    ${it.duration ? `<span class="wlog-dur">⏱ ${_fmtDuration(it.duration)}</span>` : ""}
                  </div>
                  ${it.pos && it.dur ? `<div class="wlog-bar"><i style="width:${Math.min(100, Math.round(it.pos / it.dur * 100))}%"></i></div>` : ""}
                </div>
                <span class="wlog-time">${_fmtFullTime(it.ts)}</span>
                <span class="wlog-go">›</span>
              </div>`).join("")}
          </div>
        </div>`).join("");
    }
  }

  // 2. 更新列表 (周几分组)
  updateP.then(data => {
    _updateData = data;
    if (!data || !data.all || !data.all.length) {
      const tEl = document.getElementById("todayScroll");
      if (tEl) tEl.innerHTML = `<div class="empty" style="padding:24px"><div class="icon">📅</div>更新暂不可用</div>`;
      const wEl = document.getElementById("weekdayScroll");
      if (wEl) wEl.innerHTML = `<div class="empty" style="padding:24px"><div class="icon">📅</div>更新暂不可用</div>`;
      return;
    }
    const groups = data.groups || {};
    const today = data.today || 1;
    const weekdays = data.weekdays || ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
    // 今日更新
    const tEl = document.getElementById("todayScroll");
    if (tEl) {
      const todayList = groups[String(today)] || data.all.slice(0, 20);
      tEl.innerHTML = hScrollHtml(todayList, 20);
    }
    // 周几 tabs
    const tabsEl = document.getElementById("weekdayTabs");
    if (tabsEl) {
      tabsEl.innerHTML = weekdays.map((name, i) => {
        const day = i + 1;
        const count = (groups[String(day)] || []).length;
        return `<button class="weekday-tab ${day === today ? "active" : ""}" data-day="${day}" onclick="weekdaySwitch(${day})">
          ${name}${count ? `<span class="weekday-count">${count}</span>` : ""}
        </button>`;
      }).join("");
    }
    // 默认显示今天
    const wsEl = document.getElementById("weekdayScroll");
    if (wsEl) {
      const todayList = groups[String(today)] || data.all.slice(0, 20);
      wsEl.innerHTML = hScrollHtml(todayList, 30);
    }
  });

  // 3. 快捷分类 + 分类导航
  catalogP.then(data => {
    if (!data) return;
    const genres = data.genres || [];
    const years = data.years || [];
    const regions = data.regions || [];
    const status = data.status || [];
    // 快捷分类条 (横向滚动)
    const qgEl = document.getElementById("quickGenres");
    if (qgEl) {
      qgEl.innerHTML =
        `<button class="qg-chip all" onclick="location.hash='#/catalog'">🌐 全部</button>` +
        genres.slice(0, 22).map(([id, name]) =>
          `<button class="qg-chip" onclick="location.hash='${genreUrl(name)}'">${genreEmoji(name)} ${name}</button>`).join("") +
        years.slice(0, 4).map(([id, name]) =>
          `<button class="qg-chip" onclick="location.hash='#/catalog'">📅 ${name}</button>`).join("") +
        regions.slice(0, 3).map(([id, name]) =>
          `<button class="qg-chip" onclick="location.hash='#/catalog'">🌍 ${name}</button>`).join("") +
        status.map(([id, name]) =>
          `<button class="qg-chip" onclick="location.hash='#/catalog'">📺 ${name}</button>`).join("");
    }
    // 分类导航网格 (4 组: 类型/年份/地区/状态)
    const cnEl = document.getElementById("catNav");
    if (cnEl) {
      cnEl.innerHTML = `
        <div class="cat-nav-group">
          <div class="cat-nav-label">🎬 按类型</div>
          <div class="cat-nav-chips">
            <button class="genre-item all" onclick="location.hash='#/catalog'">
              <span class="genre-emoji">🌐</span><span class="genre-name">全部</span></button>
            ${genres.map(([id, name]) =>
              `<button class="genre-item" onclick="location.hash='${genreUrl(name)}'">
                <span class="genre-emoji">${genreEmoji(name)}</span>
                <span class="genre-name">${esc(name)}</span></button>`).join("")}
          </div>
        </div>
        <div class="cat-nav-group">
          <div class="cat-nav-label">📅 按年份</div>
          <div class="cat-nav-chips">
            ${years.map(([id, name]) => `<button class="genre-item yr" onclick="location.hash='#/catalog'">
              <span class="genre-emoji">📅</span><span class="genre-name">${name}</span></button>`).join("")}
          </div>
        </div>
        <div class="cat-nav-group">
          <div class="cat-nav-label">🌍 按地区</div>
          <div class="cat-nav-chips">
            ${regions.map(([id, name]) => `<button class="genre-item yr" onclick="location.hash='#/catalog'">
              <span class="genre-emoji">🌍</span><span class="genre-name">${name}</span></button>`).join("")}
          </div>
        </div>
        <div class="cat-nav-group">
          <div class="cat-nav-label">📺 按状态</div>
          <div class="cat-nav-chips">
            ${status.map(([id, name]) => `<button class="genre-item yr" onclick="location.hash='#/catalog'">
              <span class="genre-emoji">📺</span><span class="genre-name">${name}</span></button>`).join("")}
          </div>
        </div>`;
    }
  });

  loading(false);
}

/* ---------- 页面：一周更新表 ---------- */
async function pageUpdate(day) {
  loading(true);
  document.title = "一周更新表 - 超越次元";
  $app.innerHTML = `
    <div class="section-head"><h2>📅 一周更新表</h2></div>
    <div class="weekday-tabs" id="weekdayTabs"><div class="empty" style="padding:10px">加载中...</div></div>
    <div class="grid" id="updateGrid" style="margin-top:18px"><div class="empty">加载中...</div></div>`;
  try {
    const data = await api("/anime/update");
    _updateData = data;
    const groups = data.groups || {};
    const today = data.today || 1;
    const weekdays = data.weekdays || ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
    const sel = Number(day) || today;
    const tabsEl = document.getElementById("weekdayTabs");
    if (tabsEl) {
      tabsEl.innerHTML = weekdays.map((name, i) => {
        const d = i + 1;
        const count = (groups[String(d)] || []).length;
        return `<button class="weekday-tab ${d === sel ? "active" : ""}" data-day="${d}" onclick="location.hash='#/update?day=${d}'">
          ${name}${count ? `<span class="weekday-count">${count}</span>` : ""}
        </button>`;
      }).join("");
    }
    const list = groups[String(sel)] || data.all || [];
    const el = document.getElementById("updateGrid");
    el.innerHTML = list.length
      ? list.map(animeCard).join("")
      : `<div class="empty"><div class="icon">📅</div>该日暂无更新</div>`;
  } catch (e) {
    const el = document.getElementById("updateGrid");
    if (el) el.innerHTML = `<div class="empty"><div class="icon">⚠️</div>${esc(e.message)}</div>`;
  }
  loading(false);
}

/* ---------- 页面: 游戏 Wiki (GameKee 热门 wiki 聚合, 纯静态无请求秒开) ---------- */
const WIKI_UP_KEY = "cy_wiki_up";          // 自检修复发现的可用上游域名记忆
const WIKI_UP_TTL = 30 * 60 * 1000;        // 上游记忆 30 分钟过期, 过期后回退默认主域
const _WIKIS = [
  { slug: "ys", mark: "原", name: "原神", desc: "提瓦特图鉴 · 角色攻略", icon: "https://ys.mihoyo.com/favicon.ico" },
  { slug: "sr", mark: "星", name: "崩坏:星穹铁道", desc: "星穹列车 · 开拓攻略", icon: "https://sr.mihoyo.com/favicon.ico" },
  { slug: "zzz", mark: "绝", name: "绝区零", desc: "新艾利都 · 绳匠手册", icon: "https://zzz.mihoyo.com/favicon.ico" },
  { slug: "ba", mark: "碧", name: "碧蓝档案", desc: "基沃托斯 · 老师图鉴", icon: "https://webcnstatic.yostar.net/ba_cn_web/prod/web/favicon.png" },
  { slug: "bh3", mark: "崩", name: "崩坏3", desc: "休伯利安 · 舰团手册", icon: "https://bh3.mihoyo.com/favicon.ico" },
  { slug: "ak", mark: "舟", name: "明日方舟", desc: "罗德岛 · 干员档案", icon: "https://ak.hypergryph.com/favicon.ico" },
  { slug: "wuwa", mark: "鸣", name: "鸣潮", desc: "今州城 · 共鸣者攻略", icon: "https://mc.kurogames.com/favicon.ico" },
  { slug: "", mark: "全", name: "全部 Wiki", desc: "GameKee 全站百科", icon: "https://www.gamekee.com/favicon.ico" },
];
function wikiBase() {
  try {
    const v = JSON.parse(localStorage.getItem(WIKI_UP_KEY) || "null");
    if (v && v.base && Date.now() - v.ts < WIKI_UP_TTL) return String(v.base).replace(/\/+$/, "");
  } catch { }
  return "https://www.gamekee.com";
}
function wikiUrl(slug) { return wikiBase() + (slug ? "/" + slug + "/" : "/"); }
function pageWiki() {
  document.title = "游戏 Wiki - 超越次元";
  $app.innerHTML = `
    <div class="section-head"><h2>📚 游戏 Wiki</h2></div>
    <p class="wiki-tip">GameKee 游戏百科 · 点击卡片进入攻略站, 加载失败可在浏览页「自检修复」</p>
    <div class="grid" id="wikiGrid">${_WIKIS.map((w, i) => wikiCard(w, i)).join("")}</div>`;
}
function wikiCard(w, i) {
  return `<div class="card wiki-card" style="animation-delay:${(i * 45)}ms" onclick="wikiOpen(${i})">
    <div class="wiki-ico"><img src="${w.icon}" alt="" loading="lazy" decoding="async" onerror="this.parentNode.classList.add('ico-fb')"><span class="wiki-mk">${w.mark}</span></div>
    <div class="wiki-t">${esc(w.name)}</div>
    <div class="wiki-d">${esc(w.desc)}</div>
    <span class="wiki-tag">GK</span></div>`;
}
function wikiOpen(i) {
  const w = _WIKIS[i]; if (!w) return;
  location.hash = "#/wiki/browse/" + w.slug;   // 标题从 _WIKIS 查表取, 规避标题含 "/" 被 hash 切碎
}
/* -- 上游加载遮罩 / 失败面板 / 手动自检修复 -- */
let _wikiSlug = "";
let _wikiFailTimer = null;   // 15s 未收到任何响应 → 判定上游加载失败, 显示失败面板
function wikiClearFailTimer() { if (_wikiFailTimer) { clearTimeout(_wikiFailTimer); _wikiFailTimer = null; } }
function wikiMaskHtml(html) {
  const el = document.getElementById("wikiFrameLoad");
  if (!el) return;
  if (html) { el.style.display = "flex"; el.innerHTML = html; }
  else el.style.display = "none";
}
function wikiFrameLoaded() { wikiClearFailTimer(); wikiMaskHtml(""); }   // iframe onload(收到任何响应)即撤遮罩
function wikiFailPanel(title, tip) {
  return `<div class="wiki-fail">
    <div class="wiki-fail-t">⚠️ ${title}</div>
    <p class="wiki-fail-d">${tip}</p>
    <div class="wiki-fail-btns">
      <button class="tool-btn" onclick="wikiSelfCheck(_wikiSlug)">🔧 自检修复</button>
      <button class="tool-btn" onclick="wikiRetry()">↻ 直接重试</button>
      <a class="tool-btn" href="${wikiUrl(_wikiSlug)}" target="_blank">↗ 新窗口打开</a>
    </div></div>`;
}
function wikiShowFail() {
  wikiClearFailTimer();
  wikiMaskHtml(wikiFailPanel("上游加载失败", "GameKee 站点连接超时或已失效, 可手动自检修复: 自动探测全部备用域名并切换到可用上游"));
}
async function wikiProbeUpstream(slug) {
  const s = slug || "";
  const cands = [...new Set([
    wikiUrl(s),                                        // 当前记忆/默认上游
    "https://www.gamekee.com/" + (s ? s + "/" : ""),   // 官方主域路径形态
    "https://" + (s || "www") + ".gamekee.com/",       // 子域形态 (302 到主域, 主域路径失效时可能仍活)
    "https://www.gamekee.com/",
  ])];
  for (const u of cands) {
    try {
      await Promise.race([
        fetch(u, { mode: "no-cors", cache: "no-store" }),   // no-cors: 收到任何 HTTP 响应即 resolve, 网络不通才 reject
        new Promise((_, rj) => setTimeout(() => rj(new Error("timeout")), 6000)),
      ]);
      return u;
    } catch { }
  }
  return null;
}
async function wikiSelfCheck(slug) {
  wikiMaskHtml(mangaLoad("正在自检上游站点…"));
  const ok = await wikiProbeUpstream(slug);
  if (ok) {
    try { localStorage.setItem(WIKI_UP_KEY, JSON.stringify({ base: new URL(ok).origin, ts: Date.now() })); } catch { }
    wikiMaskHtml(mangaLoad("已找到可用上游, 正在重新加载…"));
    wikiClearFailTimer();
    _wikiFailTimer = setTimeout(wikiShowFail, 15000);
    const f = document.getElementById("wikiFrame");
    if (f) f.src = ok;
    toast("自检完成: 已切换到可用上游");
  } else {
    wikiMaskHtml(wikiFailPanel("自检完成: 上游全部不可达", "已尝试主站与全部备用域名均连接失败, 请检查本机网络或代理后重新自检"));
    toast("自检失败: 上游全部不可达");
  }
}
function wikiRetry() {
  wikiMaskHtml(mangaLoad("正在重试…"));
  wikiClearFailTimer();
  _wikiFailTimer = setTimeout(wikiShowFail, 15000);
  const f = document.getElementById("wikiFrame");
  if (f) f.src = wikiUrl(_wikiSlug);
}
function pageWikiBrowse(slug) {
  const w = _WIKIS.find(x => x.slug === slug) || { name: "GameKee", url: wikiUrl(slug) };
  const url = w.url || wikiUrl(slug);
  document.title = w.name + " Wiki - 超越次元";
  _wikiSlug = slug;
  $app.innerHTML = `
    <div class="wiki-top">
      <button class="tool-btn" onclick="location.hash='#/wiki'">‹ 返回</button>
      <span class="wiki-top-t">${esc(w.name)} · GameKee Wiki</span>
      <button class="tool-btn" onclick="wikiSelfCheck(_wikiSlug)">🔧 自检修复</button>
      <a class="tool-btn" href="${url}" target="_blank">↗ 新窗口</a>
    </div>
    <div class="wiki-iframe-box">
      <div class="wiki-frame-load" id="wikiFrameLoad">${mangaLoad("正在进入 " + w.name + " Wiki…")}</div>
      <iframe class="wiki-frame" id="wikiFrame" src="${url}" referrerpolicy="no-referrer" onload="wikiFrameLoaded()"></iframe>
    </div>`;
  wikiClearFailTimer();
  _wikiFailTimer = setTimeout(wikiShowFail, 15000);   // 15s 未 onload → 上游加载失败面板(含自检修复入口)
}

/* ---------- 页面：分类目录 (无限滚动) ---------- */
async function pageCatalog(genre, page, order) {
  page = page || 1;
  genre = genre || 0;
  order = order || "time";
  loading(true);
  $app.innerHTML = `
    <div class="section-head">
      <h2>🎬 ${genre ? "分类番剧" : "全部番剧"}</h2>
      <div class="cat-genres" id="catGenres"></div>
      <div class="cat-orders" id="catOrders"></div>
    </div>
    <div class="grid" id="catGrid"><div class="empty">加载中...</div></div>`;
  try {
    const data = await api(`/anime/catalog?page=${page}&genre=${encodeURIComponent(genre)}&order=${order}`);
    const genres = data.genres || [];
    document.getElementById("catGenres").innerHTML =
      `<button class="chip ${!genre?'active':''}" onclick="location.hash='#/catalog?order=${order}'">全部</button>` +
      genres.map(([id, name]) =>
        `<button class="chip ${genre===name?'active':''}" onclick="location.hash='${genreUrl(name)}&order=${order}'">
          ${genreEmoji(name)} ${esc(name)}</button>`).join("");
    // 排序选择器
    const orders = data.orders || [["time","最新"],["hot","热门"]];
    document.getElementById("catOrders").innerHTML =
      orders.map(([id, name]) =>
        `<button class="chip ${order===id?'active':''}" onclick="location.hash='#/catalog?genre=${encodeURIComponent(genre)}&order=${id}'">${name}</button>`).join("");
    const el = document.getElementById("catGrid");
    el.innerHTML = data.items.length
      ? data.items.map(animeCard).join("")
      : `<div class="empty"><div class="icon">📭</div>该分类暂无番剧</div>`;
    if (data.has_more && data.items.length) {
      // 启用无限滚动 (下一页从 page+1 开始)
      setupInfiniteScroll("catGrid", page, genre, order);
    }
  } catch (e) {
    document.getElementById("catGrid").innerHTML = `<div class="empty"><div class="icon">⚠️</div>${esc(e.message)}</div>`;
  }
  loading(false);
}

/* ---------- 页面：搜索 ---------- */
async function pageSearch(kw) {
  loading(true);
  $app.innerHTML = `
    <div class="section-head"><h2>搜索「${esc(kw)}」</h2></div>
    <div class="grid" id="results"><div class="empty">搜索中...</div></div>`;
  try {
    const list = await api("/anime/search?kw=" + encodeURIComponent(kw));
    document.getElementById("results").innerHTML = list.length
      ? list.map(animeCard).join("")
      : `<div class="empty"><div class="icon">🔍</div>没有找到相关番剧</div>`;
  } catch (e) {
    document.getElementById("results").innerHTML = `<div class="empty"><div class="icon">⚠️</div>${esc(e.message)}</div>`;
  }
  loading(false);
}

/* ---------- 页面：推荐 (关键词推荐 + 年度评分榜, 独立增量模块) ---------- */
const RECO_HIST_KEY = "cy_reco_hist";
const RECO_PRESETS = ["热血", "治愈", "催泪", "恋爱", "搞笑", "悬疑", "科幻", "奇幻", "机战", "校园", "异世界", "运动", "时间循环"];
let _recoList = [];       // 当前推荐结果 (卡片按下标取用, 规避标题内联转义问题)
let _recoYearList = [];   // 当前年度榜结果
const _recoCache = { kw: "", data: null, year: 0, yearData: null };   // 内存缓存: 切页往返秒开

function recoHistGet() { try { return JSON.parse(localStorage.getItem(RECO_HIST_KEY)) || []; } catch { return []; } }
function recoHistPush(kw) {
  kw = String(kw || "").trim(); if (!kw) return;
  const arr = recoHistGet().filter(x => x !== kw);
  arr.unshift(kw);
  try { localStorage.setItem(RECO_HIST_KEY, JSON.stringify(arr.slice(0, 12))); } catch { }
}
function recoRenderHist() {
  const box = document.getElementById("recoHistChips");
  if (!box) return;
  const arr = recoHistGet();
  box.innerHTML = arr.length
    ? `<span class="reco-label" style="align-self:center">最近搜索</span>` + arr.map(k =>
      `<button class="chip" data-kw="${esc(k)}">${esc(k)}<span class="x" data-del="${esc(k)}">×</span></button>`).join("")
    : "";
}
/* B站小电视加载图标 (黑白漫画风: 天线摇摆 + 屏内红点弹跳 + 整机轻晃) */
function tvLoad() {
  return `<div class="tv-load"><span class="tv-ant a1"></span><span class="tv-ant a2"></span>
    <div class="tv-body"><div class="tv-screen"><i></i><i></i><i></i></div>
      <span class="tv-knob k1"></span><span class="tv-knob k2"></span></div>
    <span class="tv-foot f1"></span><span class="tv-foot f2"></span></div>`;
}
/* 漫画风格加载动画: 小电视图标 + 对话气泡弹点 */
function mangaLoad(text) {
  return `<div class="manga-load">${tvLoad()}
    <div class="manga-bubble"><i></i><i></i><i></i>${text ? esc(text) : ""}</div></div>`;
}
function recoSkeleton(n) {
  let s = "";
  for (let i = 0; i < n; i++) s += `<div class="card"><div class="cover reco-skel"></div><div class="reco-skel reco-skel-t"></div></div>`;
  return s;
}
function recoCardHtml(it, i) {
  return `<div class="card" onclick="recoOpen('k', ${i})">
    <div class="cover"><img src="${img(it.cover)}" loading="lazy" decoding="async" fetchpriority="low" alt="">
      <div class="cover-mask"><span class="play-ico">▶</span></div>
      <span class="reco-rank${it.rank <= 3 ? " top" : ""}">${it.rank}</span>
      ${it.score > 0 ? `<span class="ep-tag">⭐ ${it.score}</span>` : ""}</div>
    <div class="title">${esc(it.title)}</div>
  </div>`;
}
function recoYearRowHtml(it, i) {
  return `<div class="reco-row" onclick="recoOpen('y', ${i})">
    <span class="reco-no${it.rank <= 3 ? " top" : ""}">${it.rank}</span>
    <img src="${img(it.cover)}" loading="lazy" decoding="async" fetchpriority="low" alt="">
    <div class="reco-rt"><div class="t">${esc(it.title)}</div>
      <div class="m">${it.year || ""}${it.eps ? ` · ${it.eps} 集` : ""}</div></div>
    <span class="reco-sc">⭐ ${it.score}</span>
  </div>`;
}
/* 点击推荐条目: 有片源 id 直达详情; 无 id (AniList 条目) 先按标题解析片源 */
async function recoOpen(kind, i) {
  const it = kind === "k" ? _recoList[i] : _recoYearList[i];
  if (!it) return;
  if (it.id) { location.hash = "#/anime/" + it.id; return; }
  if (it._busy) return;
  it._busy = true;
  toast(`正在匹配片源「${it.title.slice(0, 12)}」...`);
  // 行内漫画加载遮罩: 集中线 + 气泡覆盖被点击的卡片/条目 (点击仍冒泡到卡片, 由 _busy 防重入)
  const host = kind === "k"
    ? document.querySelectorAll("#recoResults .card")[i]
    : document.querySelectorAll("#recoYearList .reco-row")[i];
  if (host) host.insertAdjacentHTML("beforeend",
    `<div class="manga-busy">${mangaLoad("匹配片源…")}</div>`);
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 25000);   // 与后端 22s 硬超时对齐
  try {
    const r = await fetch("/api/anime/reco/go?kw=" + encodeURIComponent(it.title), { signal: ac.signal }).then(x => x.json());
    if (!r.ok) throw new Error(r.error || "未找到可播放片源");
    if (r.data && r.data.id) location.hash = "#/anime/" + r.data.id;
    else throw new Error("未找到可播放片源");
  } catch (e) {
    toast(e.name === "AbortError" ? "匹配片源超时, 请稍后重试" : (e.message || "未找到可播放片源"));
  } finally {
    clearTimeout(timer);
    const ov = host && host.querySelector(".manga-busy");
    if (ov) ov.remove();
    setTimeout(() => { it._busy = false; }, 800);
  }
}
function recoRenderResults(data) {
  _recoList = data.items || [];
  const box = document.getElementById("recoResults");
  if (!box) return;
  const hitTip = data.mode === "feature" && (data.hits || []).length
    ? `<div class="reco-hit-tip">🎯 按特征匹配: ${data.hits.map(esc).join(" · ")}</div>` : "";
  box.innerHTML = hitTip + (_recoList.length
    ? _recoList.map(recoCardHtml).join("")
    : `<div class="empty"><div class="icon">🔍</div>没有找到相关推荐, 换个描述试试</div>`);
}
function recoRenderYear(data) {
  _recoYearList = data.items || [];
  const box = document.getElementById("recoYearList");
  if (!box) return;
  box.innerHTML = _recoYearList.length
    ? _recoYearList.map(recoYearRowHtml).join("")
    : `<div class="empty">该年份暂无评分数据</div>`;
}
async function recoSearch(kw) {
  kw = String(kw || "").trim();
  if (!kw) return toast("请输入关键词");
  const kwInput = document.getElementById("recoKw");
  if (kwInput) kwInput.value = kw;
  recoHistPush(kw); recoRenderHist();
  if (_recoCache.kw === kw && _recoCache.data) return recoRenderResults(_recoCache.data);
  const box = document.getElementById("recoResults");
  if (box) box.innerHTML = mangaLoad("正在推荐...") + recoSkeleton(10);
  try {
    const r = await api("/anime/reco/feature?kw=" + encodeURIComponent(kw));
    _recoCache.kw = kw; _recoCache.data = r;
    recoRenderResults(r);
  } catch (e) {
    if (box) box.innerHTML = `<div class="empty"><div class="icon">⚠️</div>${esc(e.message)}</div>`;
  }
}
async function recoYear(y) {
  document.querySelectorAll("#recoYearChips .chip").forEach(x => x.classList.toggle("active", +x.dataset.y === +y));
  if (_recoCache.year === +y && _recoCache.yearData) return recoRenderYear(_recoCache.yearData);
  const box = document.getElementById("recoYearList");
  if (box) box.innerHTML = mangaLoad("加载中...");
  try {
    const r = await api("/anime/reco/year?year=" + encodeURIComponent(y));
    _recoCache.year = +y; _recoCache.yearData = r;
    recoRenderYear(r);
  } catch (e) {
    if (box) box.innerHTML = `<div class="empty"><div class="icon">⚠️</div>${esc(e.message)}</div>`;
  }
}
async function pageReco() {
  document.title = "推荐 - 超越次元";
  const nowYear = new Date().getFullYear();
  const years = []; for (let y = nowYear; y >= nowYear - 7; y--) years.push(y);
  $app.innerHTML = `
    <div class="section-head"><h2>关键词推荐</h2></div>
    <div class="reco-search">
      <input id="recoKw" placeholder="支持整句描述, 如: 想看傲娇又高冷的校园恋爱 / 末世机甲热血战斗..." maxlength="60">
      <button id="recoBtn">推荐</button>
    </div>
    <div class="reco-chips" id="recoHistChips"></div>
    <div class="reco-label">热门题材</div>
    <div class="reco-chips" id="recoPresetChips">${RECO_PRESETS.map(p => `<button class="chip" data-kw="${esc(p)}">${esc(p)}</button>`).join("")}</div>
    <div class="grid" id="recoResults"><div class="empty">输入或点选关键词, 按评分推荐番剧</div></div>
    <div class="section-head" style="margin-top:36px"><h2>年度评分榜</h2><span class="more">按评分排名 · 数据源 Bangumi/AniList</span></div>
    <div class="reco-chips" id="recoYearChips">${years.map(y => `<button class="chip" data-y="${y}">${y}</button>`).join("")}</div>
    <div class="reco-yearlist" id="recoYearList"><div class="empty">选择年份查看按评分排名的年度动画</div></div>`;
  recoRenderHist();
  const kwInput = document.getElementById("recoKw");
  document.getElementById("recoBtn").onclick = () => recoSearch(kwInput.value);
  kwInput.addEventListener("keydown", e => { if (e.key === "Enter") recoSearch(kwInput.value); });
  // 事件委托只挂一次 (#app 元素常驻, 避免重复进入页面叠加监听器)
  if (!$app._recoDelegated) {
    $app._recoDelegated = true;
    $app.addEventListener("click", e => {
      const del = e.target.closest("#recoHistChips .x");
      if (del) {
        e.stopPropagation();
        try { localStorage.setItem(RECO_HIST_KEY, JSON.stringify(recoHistGet().filter(x => x !== del.dataset.del))); } catch { }
        recoRenderHist();
        return;
      }
      const chip = e.target.closest("#recoPresetChips .chip, #recoHistChips .chip");
      if (chip && chip.dataset.kw) { recoSearch(chip.dataset.kw); return; }
      const yc = e.target.closest("#recoYearChips .chip");
      if (yc && yc.dataset.y) recoYear(+yc.dataset.y);
    });
  }
  // 恢复上次浏览 (内存缓存, 切页往返秒开)
  if (_recoCache.data) {
    if (kwInput) kwInput.value = _recoCache.kw;
    recoRenderResults(_recoCache.data);
  }
  if (_recoCache.yearData) {
    document.querySelectorAll("#recoYearChips .chip").forEach(x => x.classList.toggle("active", +x.dataset.y === _recoCache.year));
    recoRenderYear(_recoCache.yearData);
  } else {
    recoYear(nowYear);   // 默认展示当前年份
  }
}

/* ---------- 页面：番剧详情 ---------- */
let _animeDetail = null;
let _detailExtLines = [];      // 详情页当前源+ 线路 (模块级, 供 renderEpisodes 使用)
let _detailPicked = false;     // 用户是否已手动选线
async function pageAnimeDetail(id) {
  loading(true);
  $app.innerHTML = `<div class="empty">加载中...</div>`;
  try {
    const d = await api("/anime/detail?id=" + encodeURIComponent(id));
    _animeDetail = d;
    _detailExtLines = [];
    _detailPicked = false;
    let line = d.lines && d.lines[0] ? d.lines[0].line : 1;
    document.title = d.title + " - 超越次元";
    // 观看时间表数据
    const wlog = wlogGetAnime(d.id);
    const allEps = (d.lines && d.lines[0]) ? d.lines[0].episodes || [] : [];
    const watchedCount = wlog ? Object.keys(wlog.epMap || {}).length : 0;
    const totalCount = allEps.length;
    const progress = totalCount ? Math.round(watchedCount * 100 / totalCount) : 0;
    const lastWatched = wlog && wlog.lastTs ? wlog.lastTs : 0;
    // 累计观看时长 (所有集数相加)
    const totalDur = wlog ? Object.values(wlog.epMap || {}).reduce((s, r) => s + (r.duration || 0), 0) : 0;
    // 生成集数小圆点 (最多显示 40 个, 超出则省略)
    const epDots = (() => {
      if (!totalCount) return "";
      const MAX_DOTS = 60;
      const eps = allEps.slice(0, MAX_DOTS);
      const dots = eps.map(ep => {
        const rec = wlog ? wlog.epMap[String(ep.ep)] : null;
        return rec
          ? `<button class="ep-dot seen" title="${esc(ep.name)} · 观看于 ${_fmtFullTime(rec.ts)}${rec.duration ? ` · 时长 ${_fmtDuration(rec.duration)}` : ""}" onclick="location.hash='#/watch/${esc(d.id)}/${esc(line)}/${esc(ep.ep)}/${encodeURIComponent(ep.name)}'" style="--ep:${esc(ep.ep)}"></button>`
          : `<button class="ep-dot" title="${esc(ep.name)}" onclick="location.hash='#/watch/${esc(d.id)}/${esc(line)}/${esc(ep.ep)}/${encodeURIComponent(ep.name)}'"></button>`;
      }).join("");
      const more = totalCount > MAX_DOTS ? `<span class="ep-dot-more">+${totalCount - MAX_DOTS}</span>` : "";
      return dots + more;
    })();
    const wlogSection = watchedCount > 0 || totalCount > 0 ? `
      <div class="section wlog-anime">
        <div class="section-head"><h2>📺 观看时间表</h2></div>
        <div class="wlog-anime-head">
          <div class="wlog-progress">
            <div class="wlog-bar"><div class="wlog-bar-fill" style="width:${progress}%"></div></div>
            <div class="wlog-progress-text">已看 <b>${watchedCount}</b> / ${totalCount} 集 ${progress ? `(${progress}%)` : ""}</div>
          </div>
          ${lastWatched ? `<div class="wlog-last">最近观看 ${_fmtFullTime(lastWatched)}${totalDur ? ` · 累计 ${_fmtDuration(totalDur)}` : ""}</div>` : `<div class="wlog-last hint">尚未观看, 点击下方集数开始</div>`}
        </div>
        ${epDots ? `<div class="ep-dots-wrap">${epDots}</div>` : ""}
      </div>` : "";

    $app.innerHTML = `
      <div class="detail-hero">
        <div class="poster"><img src="${img(d.cover)}" decoding="async" fetchpriority="low" alt=""></div>
        <div class="meta">
          <h1>${esc(d.title)}</h1>
          <div class="tag-row">
            ${d.status ? `<span class="tag hl">${esc(d.status)}</span>` : ""}
            ${d.year ? `<span class="tag">${esc(d.year)}</span>` : ""}
            ${d.region ? `<span class="tag">${esc(d.region)}</span>` : ""}
            ${(d.types || "").split(/\s+/).filter(Boolean).map(t => `<span class="tag">${esc(t)}</span>`).join("")}
            <span class="tag dm-tag" id="dmBadge" style="display:none"></span>
          </div>
          <div class="bgm-line" id="bgmDetail"></div>
          <div class="detail-desc">${esc(d.intro || "暂无简介")}</div>
          ${favBtn(d.id, { id: d.id, title: d.title, cover: d.cover })}
        </div>
      </div>
      ${wlogSection}
      <div class="section">
        <div class="line-chips" id="lineChips">${(d.lines || []).map(l =>
          `<button class="chip ${l.line == line ? "active" : ""}" data-line="${l.line}">${esc(l.name)}</button>`).join("")}</div>
        <div class="ep-list" id="epList"></div>
      </div>`;
    renderEpisodes(d, line);
    // 弹幕提示: 以第 1 集探测 (异步, 不阻塞页面; 服务端有缓存)
    if (d.title) {
      dmProbe(d.title, (allEps[0] || {}).ep || 1).then(n => {
        const el = document.getElementById("dmBadge");
        if (!el || n < 0) return;
        el.style.display = "";
        el.textContent = n ? `B站弹幕 ${n} 条` : "暂无B站弹幕";
        el.classList.add(n ? "dm-ok" : "dm-none");
      });
    }
    document.getElementById("lineChips").addEventListener("click", e => {
      const c = e.target.closest(".chip"); if (!c) return;
      document.querySelectorAll("#lineChips .chip").forEach(x => x.classList.remove("active"));
      c.classList.add("active");
      line = c.dataset.line;
      _detailPicked = true;
      manualPick(d.id, line);   // 用户手选: 播放页不再强制换到无广告源
      linePrefSet(d.id, line);
      renderEpisodes(d, line);
    });
    // 源+ 外部聚合片源: 异步合并进线路列表 (无广告源优先, 默认选中干净且集数够的源)
    const _prefLine = linePrefGet(d.id);
    fetchExtLines(d.title).then(ext => {
      _detailExtLines = [...ext].sort((a, b) =>
        qScore(b) - qScore(a) ||
        (lineIsAd(a) ? 1 : 0) - (lineIsAd(b) ? 1 : 0) ||
        ((b.episodes || []).length - (a.episodes || []).length));
      const box = document.getElementById("lineChips");
      if (!box || !_detailExtLines.length) return;
      const merged = [..._detailExtLines.map(l => ({ ...l, ext: true })), ...(d.lines || [])];
      box.innerHTML = merged.map(l =>
        `<button class="chip ${String(l.line) === String(line) ? "active" : ""}${l.ext ? " ext" : ""}" data-line="${esc(l.line)}">${l.ext ? "源+ " : ""}${esc(l.name)}${qTag(l)}${adTag(l)}</button>`).join("");
      const maxEps = Math.max(0, ..._detailExtLines.map(l => (l.episodes || []).length));
      const usable = _detailExtLines.filter(l => (l.episodes || []).length >= maxEps * 0.8);
      const bestQ = usable[0] || _detailExtLines[0];
      let want = "";
      if (_prefLine) {
        const p = merged.find(l => String(l.line) === String(_prefLine));
        if (p) want = String(_prefLine);   // 记住用户手选偏好
      }
      if (!want && !_detailPicked && bestQ) want = String(bestQ.line);   // 默认选实测画质最高
      if (want && String(want) !== String(line)) {
        line = want;
        renderEpisodes(d, line);
        box.querySelectorAll(".chip").forEach(x => x.classList.toggle("active", x.dataset.line === String(line)));
      }
    }).catch(() => { });
    bgmRender("bgmDetail", d.title, false);
    hist.add({ key: "anime:" + d.id, id: d.id, title: d.title, cover: d.cover, ts: Date.now() });
  } catch (e) {
    $app.innerHTML = `<div class="empty"><div class="icon">⚠️</div>${esc(e.message)}</div>`;
  }
  loading(false);
}
function renderEpisodes(d, line) {
  const l = [..._detailExtLines, ...(d.lines || [])].find(x => String(x.line) === String(line))
    || (d.lines || [])[0] || _detailExtLines[0];
  const el = document.getElementById("epList");
  if (!el || !l) return;
  el.innerHTML = (l.episodes || []).map(ep =>
    `<button class="ep-item" onclick="location.hash='${epHash(d.id, l.line, ep.ep, ep.name)}'">${esc(ep.name)}</button>`
  ).join("") || `<div class="empty">该线路暂无剧集</div>`;
}

/* ---------- 页面：播放器 ---------- */
/* 播放器依赖按需加载: hls/artplayer/danmaku 只在进播放页时拉取 (首屏少 ~560KB) */
let _playerLibsP = null;
function loadScriptOnce(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.async = false;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("资源加载失败: " + src));
    document.head.appendChild(s);
  });
}
function ensurePlayerLibs() {
  if (_playerLibsP) return _playerLibsP;
  const need = [];
  if (!window.Hls) need.push("/lib/hls.min.js");
  if (!window.Artplayer) need.push("/lib/artplayer.min.js?v=20261028j");
  if (!window.Danmaku) need.push("/lib/danmaku.min.js");
  _playerLibsP = need.length
    ? Promise.all(need.map(loadScriptOnce)).catch(e => { _playerLibsP = null; throw e; })
    : Promise.resolve();
  return _playerLibsP;
}

let _hls = null;
let _watchdog = null;   // 卡顿看门狗定时器
let _srcTitle = "";   // 当前播放剧目标题 (源自检修复用)

/* 播放源上游手动自检修复: 清服务端失效记忆/缓存并探测全部上游, 结果展示在失败页面上 */
async function srcSelfCheck() {
  const box = document.getElementById("srcCheck");
  if (!box) return toast("请先进入播放页");
  box.innerHTML = `<div class="src-check-run">${mangaLoad("正在自检播放源上游…")}</div>`;
  try {
    const r = await api("/anime/source_check");
    const row = s => `<span class="src-st ${s.ok ? "ok" : "bad"}">${s.ok ? "✓" : "✗"} ${esc(s.name)}<i>${s.ms || 0}ms</i></span>`;
    const list = [...(r.age ? [r.age] : []), ...((r.ext || [])), ...((r.channels || []))];
    box.innerHTML = `<div class="src-check-res">
      <div class="src-check-t">${r.ok ? "✅ 自检完成: 已清理失效缓存, 部分源可用" : "⚠️ 自检完成: 上游全部不可达"}</div>
      <div class="src-check-list">${list.map(row).join("")}</div>
      <div class="wiki-fail-btns">
        <button class="tool-btn" onclick="location.reload()">↻ 重新加载本集</button>
        <button class="tool-btn" onclick="srcSelfCheck()">🔧 再检一次</button>
      </div></div>`;
    toast(r.ok ? "自检完成: 已清理失效记忆, 可重新加载" : "自检完成: 上游全部不可达, 请检查网络");
  } catch (e) {
    box.innerHTML = `<div class="src-check-res"><div class="src-check-t">⚠️ 自检失败: ${esc(e.message || "网络异常")}</div>
      <div class="wiki-fail-btns"><button class="tool-btn" onclick="srcSelfCheck()">🔧 再检一次</button></div></div>`;
  }
}

let _watchCtx = null;       // 当前播放上下文 (线路/集数/候选片源), 运行时失效自动换源用
let _watchSwitched = false;   // 本次播放是否已执行过运行时换源 (防止反复跳转)

/* 无广告源优先: 服务端按 CDN/片源标记 ad, 默认排序、默认播放、失效换源都用它 */
function lineIsAd(l) { return !!(l && l.ad === true); }
function adTag(l) { return lineIsAd(l) ? '<i class="ad-tag">广告</i>' : ""; }
function qTag(l) { const q = l && l.quality; return q && q.label ? ` · ${esc(q.label)}` : ""; }
function qScore(l) { return (l && l.quality && l.quality.score) || 0; }
function manualPick(id, line) { try { sessionStorage.setItem("ml:" + id, String(line)); } catch { } }

/* 集数编号: 从「第01集/1/12话」等名称里取数字, 跨线路对齐用 (前导零归一) */
function epNoKey(s) {
  const m = String(s || "").match(/\d+(?:\.\d+)?/);
  return m ? String(parseFloat(m[0])) : "";
}

/* 源+ 线路按集数编号对齐, 编号缺失时退回同位置 */
function pickExtEpisode(eps, key, fallbackIdx) {
  if (!eps || !eps.length) return null;
  if (key) {
    const hit = eps.find(e => epNoKey(e.name) === key || epNoKey(e.ep) === key);
    if (hit) return hit;
  }
  return eps[fallbackIdx] || eps[0] || null;
}

/* 切换到某线路时定位到同一集: AGE 各线 ep 是「本线序号」而非集号, 必须先按集数名里的数字对齐 */
function pickLineEp(l, curEp, curName) {
  const eps = l.episodes || [];
  if (!eps.length) return { ep: curEp, name: curName };
  const key = epNoKey(curName) || epNoKey(curEp);
  return (key ? eps.find(e => epNoKey(e.name) === key) : null)
    || eps.find(e => String(e.ep) === String(curEp))
    || eps[0];
}

/* 多片源并线探测: 当前线路失效时, 其余 AGE 线路与全部源+ 线路同时解析,
 * 谁先返回可用直链就用谁 (不等慢线路), 全部失败返回 null */
function probeAltSources(id, curLine, ep, name, d, extLines, idx = 0, cleanOnly = false) {
  const key = epNoKey(name) || epNoKey(ep);
  const epIsUrl = /^https?:/i.test(String(ep));   // 源+ 线路的 ep 是直链, 不能直接当集数用
  const cands = [];
  for (const l of (d && d.lines) || []) {
    if (String(l.line) === String(curLine) || !(l.episodes || []).length) continue;
    // 优先按集数名里的数字对齐 (各线 ep 序号会因收录差异错位), 再退回 ep 序号/同位置
    let hit = key ? l.episodes.find(e => epNoKey(e.name) === key) : null;
    if (!hit && !epIsUrl) hit = l.episodes.find(e => String(e.ep) === String(ep));
    if (!hit) hit = l.episodes[idx] || l.episodes[0];
    if (hit) cands.push({ line: String(l.line), ep: String(hit.ep), name: hit.name || name, ad: null });
  }
  for (const l of extLines || []) {
    if (String(l.line) === String(curLine)) continue;
    const hit = pickExtEpisode(l.episodes, key, idx);
    if (hit) cands.push({ line: String(l.line), ep: hit.ep, name: hit.name, ad: lineIsAd(l) });
  }
  let pool = cands;
  if (cleanOnly) {
    pool = cands.filter(c => c.ad === false);   // 只要无广告线路
    if (!pool.length) return Promise.resolve(null);
  }
  if (!pool.length) return Promise.resolve(null);
  // 并行发起, 首个成功即用; 其余请求自然失败不影响结果
  return new Promise(resolve => {
    let left = pool.length, done = false;
    const finish = v => { if (done) return; done = true; resolve(v); };
    pool.forEach(c => {
      fetch(`/api/anime/play?id=${encodeURIComponent(id)}&line=${encodeURIComponent(c.line)}&ep=${encodeURIComponent(c.ep)}`)
        .then(r => r.json())
        .then(j => {
          if (done) return;
          if (j && j.ok && j.data && j.data.url) finish({ ...c, url: j.data.url, ad: j.data.ad === true });
          else if (--left === 0) finish(null);
        })
        .catch(() => { if (!done && --left === 0) finish(null); });
    });
  });
}

/* 运行时线路失效 (持续断流/解码失败): 自动并行探测其它片源并跳转 (每次播放仅换一次) */
async function watchAutoSwitch() {
  const c = _watchCtx;
  if (!c || _watchSwitched) return;
  _watchSwitched = true;
  const alt = await probeAltSources(c.id, c.line, c.ep, c.name, c.d, c.extLines, c.idx || 0, true)
    || await probeAltSources(c.id, c.line, c.ep, c.name, c.d, c.extLines, c.idx || 0);
  if (!alt) { toast("全部片源均不可用, 请稍后重试或自检修复"); return; }
  linePrefSet(c.id, alt.line);
  toast("已自动切换片源");
  location.hash = epHash(c.id, alt.line, alt.ep, alt.name || c.name);
}

async function pageWatch(id, line, ep, name) {
  loading(true);
  $app.innerHTML = `<div class="empty">解析视频地址中...</div>`;
  let dispName = String(name || "");
  try { dispName = decodeURIComponent(name || ""); } catch { }
  document.title = (dispName || "播放") + " - 超越次元";
  if (String(line).startsWith("x")) { try { ep = decodeURIComponent(ep); } catch { } }   // 源+ 直链双重编码还原
  _watchCtx = null;
  _watchSwitched = false;
  // 详情与线路数据: 即使视频解析失败也要拉取, 保证线路切换 / 评分 / 选集始终可用
  let d = _animeDetail && String(_animeDetail.id) === String(id) ? _animeDetail : null;
  try { if (!d) d = await api("/anime/detail?id=" + encodeURIComponent(id)); } catch { }
  const title = (d && d.title) || (hist.get().find(x => String(x.id) === String(id)) || {}).title || "";
  const extLines = await fetchExtLines(title);   // 源+ 线路 (服务端有缓存)
  // 无广告源排前面, 同档按集数多优先; AGE 线路放最后 (不优先采用)
  const extSorted = [...extLines].sort((a, b) =>
    qScore(b) - qScore(a) ||
    (lineIsAd(a) ? 1 : 0) - (lineIsAd(b) ? 1 : 0) ||
    ((b.episodes || []).length - (a.episodes || []).length));
  const allLines = [...extSorted.map(l => ({ ...l, ext: true })), ...((d && d.lines) || [])];
  let manualLine = "";
  try { manualLine = sessionStorage.getItem("ml:" + id) || ""; } catch { }
  let usedLine = String(line), usedEp = String(ep), usedName = dispName;
  let playUrl = null, errMsg = "", switched = false, switchedAd = false, altName = "";
  try {
    const pl = await api(`/anime/play?id=${encodeURIComponent(id)}&line=${encodeURIComponent(line)}&ep=${encodeURIComponent(ep)}`);
    playUrl = pl.url;
    if (pl.line !== undefined && pl.line !== null) usedLine = String(pl.line);
    // 默认播放无广告: 解析到广告源时, 同一集若有干净源就静默切过去 (用户手动选线除外)
    if (pl.ad && String(manualLine) !== String(line)) {
      const alt = await probeAltSources(id, usedLine, usedEp, usedName, d, extSorted, 0, true);
      if (alt) {
        playUrl = alt.url;
        usedLine = alt.line;
        usedEp = String(alt.ep);
        usedName = alt.name || usedName;
        switched = true;
        switchedAd = true;
        altName = (allLines.find(x => String(x.line) === String(usedLine)) || {}).name || "";
        linePrefSet(id, usedLine);
        try { history.replaceState(null, "", epHash(id, usedLine, usedEp, usedName)); } catch { }
      }
    }
  } catch (e) { errMsg = e.message; }
  // 原线路失效: 自动并线换源 (不展示失败页, 免得用户手动点线路)
  if (!playUrl) {
    const box0 = document.getElementById("app");
    if (box0) box0.innerHTML = `<div class="empty">原线路不可用, 正在自动切换其他片源…</div>`;
    const cur0 = allLines.find(x => String(x.line) === String(usedLine));
    const idx0 = cur0 ? Math.max(0, cur0.episodes.findIndex(e => String(e.ep) === usedEp)) : 0;
    const alt = await probeAltSources(id, usedLine, usedEp, usedName, d, extSorted, idx0, true)
      || await probeAltSources(id, usedLine, usedEp, usedName, d, extSorted, idx0);
    if (alt) {
      playUrl = alt.url;
      usedLine = alt.line;
      usedEp = String(alt.ep);
      usedName = alt.name || usedName;
      switched = true;
      altName = (allLines.find(x => String(x.line) === String(usedLine)) || {}).name || "";
      linePrefSet(id, usedLine);   // 记住可用线路, 下次直接用它
      // 静默改写地址栏, 刷新/重进时直接用可用片源 (不触发路由重渲染)
      try { history.replaceState(null, "", epHash(id, usedLine, usedEp, usedName)); } catch { }
    }
  }
  if (playUrl) { try { await ensurePlayerLibs(); } catch { } }   // 播放器依赖按需加载
  const curLine = allLines.find(x => String(x.line) === String(usedLine));
  const eps = curLine ? curLine.episodes : [];
  const idx = eps.findIndex(x => String(x.ep) === String(usedEp));
  const prev = idx > 0 ? eps[idx - 1] : null;
  const next = idx >= 0 && idx < eps.length - 1 ? eps[idx + 1] : null;
  hist.add({ key: "anime:" + id, id, title, cover: d ? d.cover : "", ts: Date.now() });
  loading(false);
  const finalUrl = playUrl ? (/\.m3u8(\?|$)/i.test(playUrl)
    ? "/api/proxy/hls?u=" + encodeURIComponent(playUrl)
    : playUrl) : null;
  // 视频区: 解析成功放播放器, 失败放错误提示 (不阻断页面其余部分, 可切换线路重试)
  const videoArea = playUrl
    ? `<div id="artPlayer"></div>
      <div class="dm-stage" id="dmStage"></div>`
    : `<div style="min-height:240px;display:flex;align-items:center;justify-content:center">
        <div class="empty"><div class="icon">⚠️</div>${esc(errMsg || "视频解析失败")}<br><br>
        <div style="color:#999;font-size:12px;margin-bottom:8px">已自动尝试 ${allLines.length} 条片源, 均不可用</div>
        <button class="tool-btn" onclick="srcSelfCheck()">🔧 自检修复</button>
        <button class="tool-btn" onclick="location.reload()">↻ 重试</button>
        <button class="tool-btn" onclick="history.back()">返回</button></div></div>
      <div class="src-check" id="srcCheck"></div>`;
  $app.innerHTML = `
    <div class="player-wrap">${videoArea}</div>
    <div class="player-title">${esc(title)} · ${esc(usedName)}</div>
    <div class="bgm-line" id="bgmWatch"></div>
    <div class="player-tools">
      ${prev ? `<button class="tool-btn" onclick="location.hash='${epHash(id, usedLine, prev.ep, prev.name)}'">⏮ 上一集</button>` : ""}
      ${next ? `<button class="tool-btn" onclick="location.hash='${epHash(id, usedLine, next.ep, next.name)}'">⏭ 下一集</button>` : ""}
      <button class="tool-btn" onclick="location.hash='#/anime/${esc(id)}'">剧目详情</button>
      ${favBtn(id, { id, title, cover: d ? d.cover : "" })}
      <button class="tool-btn glow" onclick="window.dlStart('${esc(id)}','${esc(usedLine)}','${esc(usedEp)}','${encodeURIComponent(usedName)}','${encodeURIComponent(title)}')">⬇ 下载本集</button>
      <span style="flex:1"></span>
      <span class="dm-hint" id="dmHint"></span>
      <button class="tool-btn" id="dmToggle" title="弹幕开关">弹 开</button>
    </div>
    <div class="section">
      <div class="section-head"><h2>选集</h2></div>
      <div class="line-chips" id="lineChips2">${allLines.map(l => {
        const t = pickLineEp(l, usedEp, usedName);
        return `<button class="chip ${String(l.line) === String(usedLine) ? "active" : ""}${l.ext ? " ext" : ""}" onclick="manualPick('${esc(id)}','${esc(l.line)}');linePrefSet('${esc(id)}','${esc(l.line)}');location.hash='${epHash(id, l.line, t.ep, t.name)}'">${l.ext ? "源+ " : ""}${esc(l.name)}${qTag(l)}${adTag(l)}</button>`;
      }).join("")}</div>
      <div class="ep-list" id="epList2">${eps.map(x =>
        `<button class="ep-item ${String(x.ep) === String(usedEp) ? "current" : ""}" onclick="location.hash='${epHash(id, usedLine, x.ep, x.name)}'">${esc(x.name)}</button>`).join("")}</div>
    </div>`;
  bgmRender("bgmWatch", title, true);
  _srcTitle = title;   // 播放源自检修复用: 当前剧目标题
  if (!playUrl) { wlogStopSession(); return; }   // 所有片源均不可用: 页面已含线路/评分/选集
  try {
    const _ql = (extSorted.find(l => String(l.line) === String(usedLine)) || {}).quality || null;
    window.__curQuality = _ql;   // 传给超分自动画布: 按实测码率封顶
  } catch { window.__curQuality = null; }
  setupVideo(finalUrl, /\.m3u8(\?|$)/i.test(playUrl));
  _watchCtx = { id: String(id), line: usedLine, ep: usedEp, name: usedName, d, extLines: extSorted, idx };
  if (switched) toast(switchedAd ? "已切换到无广告源: " + (altName || "") : "原线路失效, 已自动切换到" + (altName || "可用片源"));
  // 弹幕集数以集数编号为准 (源+ 线路的 ep 是直链 URL, 不能直接当集数用)
  const dmEp = /^https?:/i.test(String(usedEp)) ? (epNoKey(usedName) || "1") : String(usedEp);
  dmBoot(title, dmEp,
    String(id) + ":" + epLogKey(usedLine, usedEp, usedName));   // 弹幕 (B站数据 + 本地自建)
  // 视频真正开始播放时记录观看日志并启动时长计时
  wlogStopSession();
  const vRec = document.getElementById("videoPlayer");
  if (vRec) {
    const _epKey = epLogKey(usedLine, usedEp, usedName);   // 观看日志键 (源+ 线路按集数名归一)
    const _onPlay = () => {
      wlogRecord(id, title, d ? d.cover : "", _epKey, usedName || `第${usedEp}集`);
      wlogStartSession(id, _epKey);
      vRec.removeEventListener("playing", _onPlay);
    };
    vRec.addEventListener("playing", _onPlay, { once: true });
    // 观看进度: 元数据就绪后恢复上次位置 + 暂停/播完兜底 (元素级监听, 切集随 DOM 销毁)
    vRec.addEventListener("loadedmetadata", () => {
      const rec = wposGet(id, _epKey);
      if (rec && rec.pos > 30 && vRec.duration && rec.pos < vRec.duration - 30) {
        vRec.currentTime = rec.pos;
        toast(`已恢复到上次位置 ${_fmtPos(rec.pos)}`);
      }
    });
    vRec.addEventListener("pause", () => {
      if (vRec.duration && !vRec.ended) wposSave(id, _epKey, vRec.currentTime, vRec.duration);
    });
    vRec.addEventListener("ended", () => wposClear(id, _epKey));   // 播完清除进度
  }
  // 预载下一集地址 (服务端缓存预热, 切集秒开)
  if (next) {
    fetch(`/api/anime/play?id=${encodeURIComponent(id)}&line=${encodeURIComponent(usedLine)}&ep=${encodeURIComponent(next.ep)}`).catch(() => { });
  }
}
let _art = null;   // ArtPlayer 实例 (原生播放器 UI/控制条/画质档位, 替代原 WebGL 画布方案)

/* HLS 播放核: hls.js + ArtPlayer 画质菜单
 * 高码率支持: capLevelToPlayerSize=false 不按播放器尺寸裁档, 4K/高码率 master 全档可选 */
function playM3u8(video, url, art) {
  if (!window.Hls || !Hls.isSupported()) { video.src = url; return; }
  const hls = new Hls({
    enableWorker: true,
    lowLatencyMode: false,
    capLevelToPlayerSize: false,       // 关键: 不按容器尺寸裁剪档位, 4K 高码率片源保留完整档位
    maxBufferLength: 60,               // 前向缓冲 1 分钟 (波动网够撑且不过度囤积)
    maxMaxBufferLength: 180,
    maxBufferSize: 96 * 1000 * 1000,   // 高码率分片更大, 字节上限放大
    backBufferLength: 60,
    maxFragLookUpTolerance: 0.25,
    fragLoadingMaxRetry: 10,
    fragLoadingRetryDelay: 1000,
    fragLoadingMaxRetryTimeout: 16000,
    manifestLoadingMaxRetry: 6,
    levelLoadingMaxRetry: 6,
    appendErrorMaxRetry: 6,
    abrEwmaDefaultEstimate: 8000000,   // 起播按 8Mbps 估档, 先上高码率再按实测带宽回落
    abrBandWidthFactor: 0.85,
    abrBandWidthUpFactor: 0.8,
    startLevel: -1,
  });
  _hls = hls;
  art.hls = hls;   // 暴露给画质菜单/调试使用
  hls.loadSource(url);
  hls.attachMedia(video);
  let mediaFix = 0, netFix = 0, netFixT = 0;   // netFix: 网络错误恢复节奏 (8s 窗口计数)
  // 暂停即停缓冲: 用户暂停时不再后台拉流囤积带宽 (恢复播放/拖进度条自动续上)
  video.addEventListener("pause", () => { try { hls.pauseBuffering(); } catch { } });
  const _resumeBuf = () => { try { hls.resumeBuffering(); } catch { } };
  video.addEventListener("play", _resumeBuf);
  video.addEventListener("seeking", _resumeBuf);
  hls.on(Hls.Events.MANIFEST_PARSED, () => {
    const levels = hls.levels || [];
    if (levels.length > 1 && art.setting) {
      const label = l => `${l.height ? l.height + "P" : "未知"} · ${Math.round((l.bitrate || 0) / 1000)}kbps`;
      art.setting.add({
        html: "画质",
        tooltip: "自动",
        selector: [
          { html: "自动", value: "auto", default: true, onSelect() { hls.currentLevel = -1; try { localStorage.removeItem("hls_lock_max"); } catch { } } },
          { html: "最高码率（锁定）", value: "max", onSelect() { hls.currentLevel = levels.length - 1; try { localStorage.setItem("hls_lock_max", "1"); } catch { } } },
          ...levels.map((l, i) => ({
            html: label(l),
            value: "l" + i,
            onSelect() { hls.currentLevel = i; },
          })),
        ],
      });
    }
    let lockMax = false;
    try { lockMax = localStorage.getItem("hls_lock_max") === "1"; } catch { }
    if (lockMax && levels.length) hls.currentLevel = levels.length - 1;   // 记忆"锁定最高码率"
    video.play().catch(() => { });
  });
  hls.on(Hls.Events.LEVEL_SWITCHED, (_, data) => {
    try {
      const lv = hls.levels && hls.levels[data.level];
      if (window.SR && lv) SR.setSource({ height: lv.height || 0, bitrate: lv.bitrate || 0 });
    } catch { }
  });
  hls.on(Hls.Events.ERROR, (_, data) => {
    if (!data.fatal) return;
    // 网络抖动自动恢复: 8s 窗口内连续 fatal 视为持续波动, 拉长重连间隔避免重试风暴挤占弱网带宽
    if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
      const now = Date.now();
      if (now - netFixT > 8000) netFix = 0;
      netFixT = now;
      netFix++;
      if (netFix > 6) { toast("线路持续断流, 正在自动切换片源…"); watchAutoSwitch(); return; }   // 自愈无望: 换源
      if (netFix === 1) toast("网络波动, 自动恢复中...");
      if (netFix > 4) { setTimeout(() => { try { hls.startLoad(); } catch { } }, 4000); return; }
      hls.startLoad();
      return;
    }
    if (data.type === Hls.ErrorTypes.MEDIA_ERROR && mediaFix < 2) {
      mediaFix++; hls.recoverMediaError(); return;
    }
    // 分片/解码彻底失败: 直接自动换到可用片源
    toast("播放出错, 正在自动切换片源…");
    watchAutoSwitch();
  });
  // 卡顿看门狗: 每 3.5s 检查播放推进; 连续 2 次无推进按缓冲情况自愈
  if (_watchdog) clearInterval(_watchdog);
  let wdLast = -1, wdStall = 0;
  _watchdog = setInterval(() => {
    if (!video || video.paused || video.seeking || video.ended) return;
    const t = video.currentTime;
    if (Math.abs(t - wdLast) > 0.01) { wdLast = t; wdStall = 0; return; }
    if (++wdStall < 2) return;
    wdStall = 0;
    const b = video.buffered;
    const bufEnd = b.length ? b.end(b.length - 1) : 0;
    if (bufEnd > t + 0.3) {
      try { video.currentTime = t + 0.05; video.play().catch(() => { }); } catch { }
    } else {
      try { hls.startLoad(); } catch { }
    }
  }, 3500);
}

function setupVideo(url, isHls) {
  const box = document.getElementById("artPlayer");
  if (window.SR) { try { SR.detach(); } catch { } }   // 切集/换源先释放上一路超分画布
  if (_hls) { try { _hls.destroy(); } catch { } _hls = null; }
  if (_watchdog) { clearInterval(_watchdog); _watchdog = null; }
  if (_art) { try { _art.destroy(false); } catch { } _art = null; }
  if (!box) return;
  // 兜底: ArtPlayer 未加载时退回原生 video (功能降级, 不白屏)
  if (!window.Artplayer) {
    box.innerHTML = `<video id="videoPlayer" controls autoplay playsinline style="width:100%;height:100%"></video>`;
    const v0 = document.getElementById("videoPlayer");
    if (v0) { v0.src = url; v0.play().catch(() => { }); }
    return;
  }
  const art = new Artplayer({
    container: box,
    url: url,
    type: isHls ? "m3u8" : "",
    autoplay: true,
    theme: "#e60012",
    volume: 0.8,
    hotkey: true,
    setting: true,
    playbackRate: true,
    aspectRatio: true,
    pip: true,
    airplay: true,
    fullscreen: true,
    fullscreenWeb: true,
    miniProgressBar: true,
    autoPlayback: true,
    autoOrientation: true,
    moreVideoAttr: { playsinline: true, "webkit-playsinline": true },
    customType: { m3u8: playM3u8 },
  });
  _art = art;
  window.__art = art;
  art.video.id = "videoPlayer";   // 兼容既有观看进度/弹幕/看门狗逻辑
  // 弹幕层搬进播放器容器: 全屏时随播放器一起放大, 且不挡控制条
  // ArtPlayer 的 DOM 是异步渲染的, 必须等 ready 再挂载, 否则容器还不存在
  const mountStage = () => {
    const stage = document.getElementById("dmStage");
    if (!stage) return;
    const host = (art.video && art.video.closest(".art-video-player")) || box;
    if (host && stage.parentNode !== host) {
      host.appendChild(stage);
      window.dispatchEvent(new Event("resize"));   // 尺寸变化后弹幕层重排
    }
  };
  // 超分: WebGL 真 2K/4K 渲染画布叠在 video 上、弹幕/控制条下, 设置面板三档切换 (默认 2K, 本地记忆)
  const mountSuperRes = () => {
    if (!window.SR || !SR.supported) return;
    if (!SR.attach(art.video, art.video.closest(".art-video-player") || box)) return;
    const m = SR.mode;
    const q0 = window.__curQuality;
    if (q0) SR.setSource({ height: q0.height || 0, bitrate: q0.bitrate || 0 });
    try {
      art.setting.add({
        html: "超分画布",
        tooltip: (m === "off" ? "关闭" : m === "auto" ? "自动" : m === "1080" ? "1080P" : m === "2k" ? "2K" : "4K"),
        selector: [
          { html: "关闭", value: "off", default: m === "off" },
          { html: "自动（贴合片源）", value: "auto", default: m === "auto" },
          { html: "1080P 画布", value: "1080", default: m === "1080" },
          { html: "2K 画布", value: "2k", default: m === "2k" },
          { html: "4K 画布", value: "4k", default: m === "4k" },
        ],
        // 注意: ArtPlayer 只回调父级 onSelect (项级 onSelect 不执行), 逻辑必须集中在此
        onSelect(item) {
          SR.setMode(item.value);
          const label = { off: "关闭", auto: "自动", "1080": "1080P", "2k": "2K", "4k": "4K" }[item.value] || item.html;
          toast(item.value === "off" ? "超分画布已关闭" : "超分画布：" + label);
          return label;
        },
      });
    } catch { }
    const sh = SR.sharpLevel || "std";
    const shLabel = { soft: "柔和", std: "标准", sharp: "锐利" }[sh] || "标准";
    try {
      art.setting.add({
        html: "超分锐度",
        tooltip: shLabel,
        selector: [
          { html: "柔和", value: "soft", default: sh === "soft" },
          { html: "标准", value: "std", default: sh === "std" },
          { html: "锐利", value: "sharp", default: sh === "sharp" },
        ],
        onSelect(item) {
          SR.setSharp(item.value);
          const label = { soft: "柔和", std: "标准", sharp: "锐利" }[item.value] || item.html;
          toast("超分锐度：" + label);
          return label;
        },
      });
    } catch { }
    // AI 插帧: rAF 过渡帧输出消除低帧率抖动 (依附超分画布管线, 默认开, 本地记忆)
    try {
      const fi = !!SR.fi;
      art.setting.add({
        html: "AI 插帧",
        tooltip: fi ? "开" : "关",
        selector: [
          { html: "开启（丝滑 60fps）", value: "on", default: fi },
          { html: "关闭", value: "off", default: !fi },
        ],
        onSelect(item) {
          const on = item.value === "on";
          SR.setFi(on);
          toast(on ? "AI 插帧已开启" : "AI 插帧已关闭");
          return on ? "开" : "关";
        },
      });
    } catch { }
  };
    // ---- 电影沉浸: 画面预设 / 遮幅 / 对白增强 ----
    try {
      const gr = SR.grade || "off";
      const grLabel = { off: "原画", teal: "青橙电影", film: "低饱和胶片", noir: "暗调悬疑", shinkai: "新海诚风", cyber: "赛博朋克", mono: "黑白映画" };
      art.setting.add({
        html: "画面预设",
        tooltip: grLabel[gr] || "原画",
        selector: [
          { html: "原画（默认）", value: "off", default: gr === "off" },
          { html: "🎬 青橙电影", value: "teal", default: gr === "teal" },
          { html: "🎞 低饱和胶片", value: "film", default: gr === "film" },
          { html: "🌃 暗调悬疑", value: "noir", default: gr === "noir" },
          { html: "🌸 新海诚风", value: "shinkai", default: gr === "shinkai" },
          { html: "🌆 赛博朋克", value: "cyber", default: gr === "cyber" },
          { html: "⬛ 黑白映画", value: "mono", default: gr === "mono" },
        ],
        onSelect(item) {
          SR.setGrade(item.value);
          toast("画面预设：" + (grLabel[item.value] || item.html));
          return grLabel[item.value] || item.html;
        },
      });
    } catch { }
    try {
      const lb = CINE.lbOn();
      art.setting.add({
        html: "遮幅 2.35:1",
        tooltip: lb ? "开" : "关",
        selector: [
          { html: "关闭遮幅", value: "off", default: !lb },
          { html: "开启遮幅（2.35:1）", value: "on", default: lb },
        ],
        onSelect(item) {
          const on = item.value === "on";
          CINE.toggleLB(on);
          toast(on ? "遮幅已开启（2.35:1）" : "遮幅已关闭");
          return on ? "开" : "关";
        },
      });
    } catch { }
    try {
      const vc = CINE.vocOn();
      art.setting.add({
        html: "对白增强",
        tooltip: vc ? "开" : "原音轨",
        selector: [
          { html: "原音轨（默认）", value: "raw", default: !vc },
          { html: "🔊 对白增强", value: "voc", default: vc },
        ],
        onSelect(item) {
          if (item.value === "voc") {
            if (CINE.toggleVoc(true)) { toast("对白增强已开启"); return "对白增强"; }
            return "原音轨";
          }
          CINE.toggleVoc(false);
          toast("已切回原音轨");
          return "原音轨";
        },
      });
    } catch { }
    // 立体音效: M/S 侧链加宽声场, 耳机/外放皆有环绕感
    try {
      const wd = CINE.wideOn();
      art.setting.add({
        html: "立体音效",
        tooltip: wd ? "开" : "关",
        selector: [
          { html: "关闭（默认）", value: "off", default: !wd },
          { html: "🎧 立体环绕", value: "on", default: wd },
        ],
        onSelect(item) {
          const on = item.value === "on";
          if (on && !CINE.toggleWide(true)) { toast("当前线路不支持音频处理"); return "关"; }
          if (!on) CINE.toggleWide(false);
          toast(on ? "立体音效已开启" : "立体音效已关闭");
          return on ? "开" : "关";
        },
      });
    } catch { }
    try {
      const fl = FILL.on();
      art.setting.add({
        html: "全屏铺满",
        tooltip: fl ? "开" : "关",
        selector: [
          { html: "铺满屏幕（自适应裁切）", value: "on", default: fl },
          { html: "完整画面（留黑边）", value: "off", default: !fl },
        ],
        onSelect(item) {
          const on = item.value === "on";
          FILL.toggle(on);
          toast(on ? "全屏铺满已开启" : "全屏铺满已关闭");
          return on ? "开" : "关";
        },
      });
    } catch { }
  // 遮幅挂载 + 音频惰性启用 (等首次手势再 hook, 避免自动播放策略下声音被劫持成静音)
  const mountCine = () => {
    const host = art.video && art.video.closest(".art-video-player");
    if (host) CINE.mountLB(host);
    // 全屏事件联动铺满 (含 iOS 网页全屏)
    try {
      art.on("fullscreen", s => FILL.apply(s));
      art.on("fullscreenWeb", s => FILL.apply(s));
    } catch { }
    window.addEventListener("pointerdown", () => {
      if (CINE.vocOn()) CINE.toggleVoc(true);
      if (CINE.wideOn()) CINE.toggleWide(true);
    }, { once: true });
    // 引导试用 (仅一次): 默认全部关闭, 提示用户主动体验
    try {
      if (!localStorage.getItem("cine_hint")) {
        localStorage.setItem("cine_hint", "1");
        setTimeout(() => { if (window.toast) toast("🎬 设置里新增「画面预设」，一键体验电影感"); }, 6000);
      }
    } catch { }
  };
  const mountExtras = () => { mountStage(); mountSuperRes(); mountCine(); };
  if (art.isReady) mountExtras(); else art.once("ready", mountExtras);
  // 直链/解码失效 (非 HLS 分支): 自动换到可用片源, 不停在报错画面
  art.video.addEventListener("error", () => {
    if (_hls) return;   // HLS 分支由 hls.js 的 fatal 事件处理
    toast("直链失效, 正在自动切换片源…");
    watchAutoSwitch();
  }, { once: true });
}

/* ---------- 电影沉浸: 遮幅 Letterbox + 对白增强 (Web Audio) ----------
   设计原则: 默认关闭保持原画质, 用户一键开启; 参数全部内置, 不暴露滑块 */
const CINE = {
  _host: null, _bars: null, _ac: null, _eq: null, _comp: null, _hooked: null, _side: null,

  /* ---- 遮幅 2.35:1 ---- */
  lbOn() { try { return localStorage.getItem("cine_lb") === "1"; } catch { return false; } },
  mountLB(host) {
    if (!host || this._bars && this._host === host) return;
    this._host = host;
    const t = document.createElement("div"), b = document.createElement("div");
    t.className = "cine-bar top"; b.className = "cine-bar bottom";
    host.appendChild(t); host.appendChild(b);
    this._bars = [t, b];
    if (this.lbOn()) requestAnimationFrame(() => this._applyLB(true));
  },
  _applyLB(on) {
    if (!this._bars || !this._host) return;
    const W = this._host.clientWidth, H = this._host.clientHeight;
    const h = on ? Math.max(0, Math.round((H - W / 2.35) / 2)) : 0;
    this._bars[0].style.height = h + "px";
    this._bars[1].style.height = h + "px";
  },
  toggleLB(on) {
    try { localStorage.setItem("cine_lb", on ? "1" : "0"); } catch { }
    this._applyLB(on);
  },
  refreshLB() { if (this.lbOn()) this._applyLB(true); },

  /* ---- 对白增强 (参考 DTS Clear Dialogue 思路): 人声频段选择性提升 + 中置对白加强 + 低频轰鸣压制 ----
     ---- 立体音效 (参考 Agora 空间音频思路): M/S 宽化 + Haas 时差 + 对侧早期反射 + 侧链低切 ---- */
  vocOn() { try { return localStorage.getItem("cine_voc") === "1"; } catch { return false; } },
  _ensureAudio(video) {
    if (!video) return false;
    try {
      if (!this._ac) this._ac = new (window.AudioContext || window.webkitAudioContext)();
      if (this._hooked !== video) {
        if (this._hooked) return false;   // 一个 AudioContext 只绑过别的元素: 重建代价大, 保守不切换
        const src = this._ac.createMediaElementSource(video);   // 同域代理流, 无跨域静音问题
        const B = () => this._ac.createBiquadFilter();
        // -- 对白增强 4 段: 轰鸣高切(20->95Hz) / 人声基音 300Hz / 清晰度 2.5kHz / 空气感 5.2kHz --
        this._eqRumble = B(); this._eqRumble.type = "highpass"; this._eqRumble.frequency.value = 20; this._eqRumble.Q.value = 0.7;
        this._eqLow = B(); this._eqLow.type = "peaking"; this._eqLow.frequency.value = 300; this._eqLow.Q.value = 1.1; this._eqLow.gain.value = 0;
        this._eq = B(); this._eq.type = "peaking"; this._eq.frequency.value = 2500; this._eq.Q.value = 0.8; this._eq.gain.value = 0;
        this._eqAir = B(); this._eqAir.type = "peaking"; this._eqAir.frequency.value = 5200; this._eqAir.Q.value = 1.0; this._eqAir.gain.value = 0;
        this._comp = this._ac.createDynamicsCompressor();       // 压缩拉近对白/音效响度差
        this._comp.threshold.value = 0; this._comp.knee.value = 14;
        this._comp.ratio.value = 1; this._comp.attack.value = 0.004; this._comp.release.value = 0.24;
        // -- M/S 矩阵: mid=(L+R)/2 (对白多在中置), side=(L-R)/2 (环境/音效) --
        const sp = this._ac.createChannelSplitter(2);
        this._midG = this._ac.createGain(); this._midG.gain.value = 0.5;    // 对白增强时 0.5->0.62 抬中置
        const sg = this._ac.createGain(); sg.gain.value = 0.5;
        const invR = this._ac.createGain(); invR.gain.value = -1;
        sp.connect(this._midG, 0); sp.connect(this._midG, 1);
        sp.connect(sg, 0); sp.connect(invR, 1); invR.connect(sg);
        // -- 侧链: 对白衰减(voc) -> 低切(宽化时低音归中) -> 宽化增益 -> Haas 时差 -> 对侧早期反射 --
        this._sideDuck = this._ac.createGain(); this._sideDuck.gain.value = 1.0;   // voc 开时 0.82 压环境
        this._sideCut = B(); this._sideCut.type = "highpass"; this._sideCut.frequency.value = 10;
        this._side = this._ac.createGain(); this._side.gain.value = 1.0;           // 宽化增益 1->3.4
        this._sideDelay = this._ac.createDelay(0.05); this._sideDelay.delayTime.value = 0;
        this._sideRefDelay = this._ac.createDelay(0.05); this._sideRefDelay.delayTime.value = 0.027;
        this._sideRef = this._ac.createGain(); this._sideRef.gain.value = 0;       // 左耳反射 0->0.3
        this._sideRefR = this._ac.createGain(); this._sideRefR.gain.value = 0;     // 右耳反射(反相) 0->0.3
        const inv2 = this._ac.createGain(); inv2.gain.value = -1;
        const inv3 = this._ac.createGain(); inv3.gain.value = -1;
        const mrg = this._ac.createChannelMerger(2);
        sg.connect(this._sideDuck); this._sideDuck.connect(this._sideCut);
        this._sideCut.connect(this._side); this._side.connect(this._sideDelay);
        // L' = mid + side(即时) + 0.3*side(27ms 早期反射, 模拟房间反射增强空间感)
        this._midG.connect(mrg, 0, 0); this._side.connect(mrg, 0, 0);
        this._side.connect(this._sideRefDelay); this._sideRefDelay.connect(this._sideRef);
        this._sideRef.connect(mrg, 0, 0);
        // R' = mid - side(经 Haas 延迟) - 0.3*side(反射反相, 保持 M/S 相位一致不破像)
        this._midG.connect(mrg, 0, 1); this._sideDelay.connect(inv2); inv2.connect(mrg, 0, 1);
        this._sideRefDelay.connect(inv3); inv3.connect(this._sideRefR); this._sideRefR.connect(mrg, 0, 1);
        src.connect(this._eqRumble); this._eqRumble.connect(this._eqLow); this._eqLow.connect(this._eq);
        this._eq.connect(this._eqAir); this._eqAir.connect(this._comp); this._comp.connect(sp);
        mrg.connect(this._ac.destination);
        this._hooked = video;
      }
      return true;
    } catch { return false; }
  },
  toggleVoc(on) {
    const video = window.__art && window.__art.video;
    if (on && !this._ensureAudio(video)) { toast("当前线路不支持音频处理"); return false; }
    try { localStorage.setItem("cine_voc", on ? "1" : "0"); } catch { }
    if (this._ac) {
      if (this._ac.state === "suspended") this._ac.resume().catch(() => { });
      const t = this._ac.currentTime;
      // 关 = 链路透明; 开 = DTS Clear Dialogue 式: 人声频段 +7dB 选择性提升, 中置对白 +1.9dB, 环境侧链压到 0.82
      this._eqRumble.frequency.setTargetAtTime(on ? 95 : 20, t, 0.08);
      this._eqLow.gain.setTargetAtTime(on ? 2.5 : 0, t, 0.08);
      this._eq.gain.setTargetAtTime(on ? 7 : 0, t, 0.08);
      this._eqAir.gain.setTargetAtTime(on ? 3.5 : 0, t, 0.08);
      this._comp.threshold.setTargetAtTime(on ? -24 : 0, t, 0.08);
      this._comp.ratio.setTargetAtTime(on ? 2.8 : 1, t, 0.08);
      this._midG.gain.setTargetAtTime(on ? 0.62 : 0.5, t, 0.08);
      this._sideDuck.gain.setTargetAtTime(on ? 0.82 : 1.0, t, 0.08);
    }
    return true;
  },

  /* ---- 立体音效: M/S 侧链增益加宽声场, 关闭=增益 1.0 透明直通 ---- */
  wideOn() { try { return localStorage.getItem("cine_wide") === "1"; } catch { return false; } },
  toggleWide(on) {
    const video = window.__art && window.__art.video;
    if (on && !this._ensureAudio(video)) return false;
    try { localStorage.setItem("cine_wide", on ? "1" : "0"); } catch { }
    if (this._ac) {
      if (this._ac.state === "suspended") this._ac.resume().catch(() => { });
      const t = this._ac.currentTime;
      // Agora 空间音频式: 侧链低切 220Hz(低音归中不浑) + 宽化 3.4x + Haas 15ms + 27ms 对侧早期反射 0.3
      this._sideCut.frequency.setTargetAtTime(on ? 220 : 10, t, 0.08);
      this._side.gain.setTargetAtTime(on ? 3.4 : 1.0, t, 0.08);
      this._sideDelay.delayTime.setTargetAtTime(on ? 0.015 : 0, t, 0.08);
      this._sideRef.gain.setTargetAtTime(on ? 0.3 : 0, t, 0.08);
      this._sideRefR.gain.setTargetAtTime(on ? 0.3 : 0, t, 0.08);
    }
    return true;
  },
};
window.addEventListener("resize", () => CINE.refreshLB());

/* ---- 全屏铺满: 进全屏自动 cover 裁切填满屏幕, 出全屏恢复 contain (默认开) ---- */
const FILL = {
  on() { try { return localStorage.getItem("cine_fill") !== "0"; } catch { return true; } },
  apply(fs) {
    const host = window.__art && __art.video && __art.video.closest(".art-video-player");
    const active = this.on() && !!fs;
    if (host) host.classList.toggle("art-fill", active);
    if (window.SR && SR.setFill) SR.setFill(active);
    if (window.CINE) CINE.refreshLB();
  },
  toggle(on) {
    try { localStorage.setItem("cine_fill", on ? "1" : "0"); } catch { }
    this.apply(window.__art && __art.fullscreen);
  },
};

/* ---------- 页面：我的 ---------- */
function pageMine() {
  const fa = favs.get();
  const ha = hist.get();
  $app.innerHTML = `
    <div class="mine-tabs">
      <button class="chip active" data-t="fav" onclick="mineTab('fav')">⭐ 收藏</button>
      <button class="chip" data-t="hist" onclick="mineTab('hist')">🕐 历史</button>
      <button class="chip" data-t="lab" onclick="mineTab('lab')">✨ 体验</button>
      <button class="chip" data-t="src" onclick="mineTab('src')">🔧 源修复</button>
      <button class="chip" data-t="about" onclick="mineTab('about')">⚙ 关于</button>
    </div>
    <div id="mineLab" style="display:none"></div>
    <div id="mineSrc" style="display:none">
      <div class="about-box">
        <div class="about-logo">🔧 动漫源上游自检修复</div>
        <div class="about-tip">播放解析失败 / 线路加载不出来时使用: 自动清理服务端失效域名记忆与线路缓存, 并重新探测 AGE 主源与全部外部片源, 修复后即可正常观看。</div>
        <div class="about-actions">
          <button class="tool-btn" onclick="mineSrcCheck()">🔧 开始自检修复</button>
          <button class="tool-btn" onclick="location.hash='#/'">🏠 返回首页</button>
        </div>
        <div class="src-check" id="mineSrcRes"></div>
      </div>
      <div class="about-box" style="margin-top:14px">
        <div class="about-logo">➕ 外部片源渠道</div>
        <div class="about-tip">在「动漫文件夹 / 外部片源」放入渠道 JSON 即可接入; 也可在下方直接添加 maccms 资源站。渠道只追加, <b>App 内置片源保持不变</b>。</div>
        <div id="srcChList" style="margin:10px 0"></div>
        <div style="display:flex;flex-direction:column;gap:8px">
          <input id="chName" placeholder="渠道名称, 如 星空片源" style="padding:9px 12px;border:2px solid #1c1c1c;border-radius:10px;font:14px inherit;background:#fff">
          <input id="chApi" placeholder="接口地址, 如 https://host/api.php/provide/vod/" style="padding:9px 12px;border:2px solid #1c1c1c;border-radius:10px;font:14px inherit;background:#fff">
          <input id="chMir" placeholder="备用镜像(可选, 逗号分隔)" style="padding:9px 12px;border:2px solid #1c1c1c;border-radius:10px;font:14px inherit;background:#fff">
        </div>
        <div class="about-actions">
          <button class="tool-btn" onclick="srcChSave()">➕ 添加/保存渠道</button>
          <button class="tool-btn" onclick="srcChOpen()">📂 打开片源文件夹</button>
          <button class="tool-btn" onclick="srcChLoad()">🔄 刷新列表</button>
        </div>
      </div>
    </div>
    <div id="mineFav">
      ${fa.length ? `<div class="mine-list">${fa.map(rowItem).join("")}</div>` : `<div class="empty"><div class="icon">⭐</div>暂无收藏，去首页逛逛吧</div>`}
    </div>
    <div id="mineHist" style="display:none">
      ${ha.length ? `<div class="mine-list">${ha.map(rowItem).join("")}</div>` : `<div class="empty"><div class="icon">🕐</div>暂无观看记录</div>`}
    </div>
    <div id="mineAbout" style="display:none">
      <div class="about-box">
        <div class="about-logo">✦ 超越次元 · 次元追番事务所</div>
        <div class="about-ver" id="updInfo">当前版本 ${esc(APP_VERSION)}</div>
        <div class="about-actions">
          <button class="tool-btn" id="updBtn" onclick="updateFlow()">🔄 检查更新</button>
        </div>
        <div class="about-tip">应用内不提供下载, 只检查是否有新版本。</div>
        <div class="about-tip" style="margin-top:10px;color:#c0392b;text-align:left">
          <b>唯一公开地址</b>（本版只此一个公开仓库, 其余仓库均已设为私人）:<br>
          <a href="${OFFICIAL_URL}" target="_blank" rel="noopener">github.com/drlinesu/chaoyue-ji-xian-waiyuan</a><br>
          请勿从他人分享的链接、网盘或第三方站点下载, 谨防下载到被篡改的安装包。
        </div>
      </div>
    </div>`;
  api("/update/check").then(d => _updRender(d)).catch(e => {
    const info = document.getElementById("updInfo");
    if (info) info.textContent = "检查失败: " + e.message;
  });
  document.querySelectorAll(".mine-list .del").forEach(el => {
    el.onclick = e => {
      e.stopPropagation();
      favs.del(el.dataset.key); hist.del(el.dataset.key);
      toast("已删除"); pageMine();
    };
  });
}
/* ---------- 我的 · 功能体验实验室: 每项设置就地演示/试听, 说明它改变了什么 ---------- */
let _labInited = false;
window.labInit = () => {
  if (_labInited) return;
  _labInited = true;
  const box = document.getElementById("mineLab");
  box.innerHTML = `
    <div class="about-box lab-sec">
      <div class="about-logo">🖼 超分画质 + 锐度</div>
      <div class="about-tip">播放页设置「超分画布」把低清片源实时放大到 2K/4K 并锐化边缘线条。「锐度」控制线条强调力度。下面左图是原始放大（模糊），右图模拟超分锐化后（线条清晰）——把鼠标移上去看得更清楚：</div>
      <div class="lab-row">
        <div class="lab-cell"><canvas id="labRawCv" width="320" height="180"></canvas><span>原始放大</span></div>
        <div class="lab-cell"><canvas id="labSrCv" width="320" height="180"></canvas><span>超分锐化后</span></div>
      </div>
    </div>
    <div class="about-box lab-sec">
      <div class="about-logo">🎨 画面预设滤镜</div>
      <div class="about-tip">播放页设置「画面预设」给画面整体调色。点下面按钮在同一幅画上预览各风格（浏览器近似模拟，实际着色器效果更细腻）：</div>
      <div class="lab-row"><div class="lab-cell" style="max-width:520px;flex:1">
        <img id="labGradeImg" class="lab-img" src="https://trae-api-cn.mchost.guru/api/ide/v1/tool_text_to_image?prompt=beautiful%20anime%20scenery%2C%20blue%20sky%20white%20clouds%2C%20green%20hill%2C%20small%20town%2C%20Makoto%20Shinkai%20style%2C%20vivid%20colors&image_size=landscape_16_9" alt="滤镜测试图">
        <div id="labGradeName" style="margin-top:6px;font-weight:700">原画</div>
      </div></div>
      <div class="lab-btns" id="labGradeBtns"></div>
    </div>
    <div class="about-box lab-sec">
      <div class="about-logo">⚡ AI 插帧</div>
      <div class="about-tip">片源大多是 24 帧/秒，在 60Hz 屏幕上会一顿一顿。AI 插帧按屏幕刷新率生成过渡帧。下面是 24fps（左）与插帧 60fps（右）的直观差别——盯住小球移动的平顺度：</div>
      <div class="lab-row">
        <div class="lab-cell"><canvas id="labF24" width="300" height="90"></canvas><span>24fps 原始</span></div>
        <div class="lab-cell"><canvas id="labF60" width="300" height="90"></canvas><span>AI 插帧</span></div>
      </div>
    </div>
    <div class="about-box lab-sec">
      <div class="about-logo">🎧 音频增强 (立体音效 + 对白增强)</div>
      <div class="about-tip">播放页设置里的两个音频开关，处理链与这里试听完全一致。点播放后反复切换对比（<b>务必戴耳机</b>，音箱物理上很难呈现耳间时差效果）：</div>
      <div class="lab-btns">
        <button class="tool-btn" id="labAudioBtn" onclick="labAudioToggle()">▶ 试听一段旋律</button>
        <button class="tool-btn" id="labWideBtn" onclick="labWideToggle()">立体音效: 关</button>
        <button class="tool-btn" id="labVocBtn" onclick="labVocToggle()">对白增强: 关</button>
      </div>
      <div class="about-tip" style="margin-top:10px;line-height:1.9">
        <b>🌀 立体音效</b>（参考 Agora 空间音频）<br>
        · 原理：把声音拆成「中间」(人声对白) 和「两侧」(环境音/BGM)，两侧放大 3.4 倍，再给一耳加 15ms 微延迟 + 27ms 房间反射<br>
        · 听感：声场从"挤在头中"变成环绕头外，BGM 和音效有明显方位感<br>
        · 适合：剧场版、Live 演出、战斗场面多的番<br><br>
        <b>🗣 对白增强</b>（参考 DTS Clear Dialogue）<br>
        · 原理：压低 95Hz 以下轰鸣 → 人声清晰度频段 (2.5kHz) 提升 7dB → 中置对白单独抬升 → 环境侧音微压 → 压缩器拉平轻语与喊戏的响度差<br>
        · 听感：BGM 震耳但听不清台词的问题消失，轻声细语也听得清<br>
        · 适合：深夜压低音量看番、台词密集的剧情番<br><br>
        <b>两者可同时开启</b>：立体音效负责"空间"，对白增强负责"清晰"，链路串联互不干扰。关闭时完全透明直通，不改变原音轨。
      </div>
    </div>`;
  labDrawSrDemo();
  labGradeDemo();
  labFiDemo();
};

/* 超分演示: 低清图案放大(左, 双线性模糊) vs 3x3 锐化卷积(右, 模拟 CAS 效果) */
function labDrawSrDemo() {
  const W = 320, H = 180, src = document.createElement("canvas");
  src.width = 160; src.height = 90;
  const s = src.getContext("2d");
  const sky = s.createLinearGradient(0, 0, 0, 90);
  sky.addColorStop(0, "#6db3f2"); sky.addColorStop(1, "#e8f4ff");
  s.fillStyle = sky; s.fillRect(0, 0, 160, 90);
  s.fillStyle = "#fff"; s.beginPath(); s.ellipse(40, 24, 16, 7, 0, 0, 7); s.ellipse(110, 16, 20, 8, 0, 0, 7); s.fill();
  s.fillStyle = "#4a8f4a"; s.beginPath(); s.moveTo(0, 90); s.quadraticCurveTo(60, 40, 120, 90); s.fill();
  s.fillStyle = "#3a7a3a"; s.beginPath(); s.moveTo(70, 90); s.quadraticCurveTo(130, 48, 160, 90); s.fill();
  s.fillStyle = "#d94f4f"; s.fillRect(66, 52, 26, 22);
  s.fillStyle = "#fff"; s.fillRect(70, 56, 8, 8); s.fillRect(80, 56, 8, 8);
  s.fillStyle = "#333"; s.font = "bold 13px sans-serif"; s.fillText("超分", 8, 82);
  // 左: 直接双线性放大 (模糊)
  const raw = document.getElementById("labRawCv").getContext("2d");
  raw.imageSmoothingEnabled = true; raw.imageSmoothingQuality = "high";
  raw.drawImage(src, 0, 0, W, H);
  // 右: 放大后做 3x3 锐化卷积 (模拟 CAS 线条强调)
  const tmp = document.createElement("canvas"); tmp.width = W; tmp.height = H;
  const t = tmp.getContext("2d");
  t.imageSmoothingEnabled = true; t.imageSmoothingQuality = "high";
  t.drawImage(src, 0, 0, W, H);
  const im = t.getImageData(0, 0, W, H), d = im.data, out = new Uint8ClampedArray(d);
  const K = [0, -0.6, 0, -0.6, 3.4, -0.6, 0, -0.6, 0];   // unsharp 核
  for (let y = 1; y < H - 1; y++) for (let x = 1; x < W - 1; x++) {
    for (let c = 0; c < 3; c++) {
      let a = 0;
      for (let ky = -1; ky <= 1; ky++) for (let kx = -1; kx <= 1; kx++)
        a += d[((y + ky) * W + (x + kx)) * 4 + c] * K[(ky + 1) * 3 + kx + 1];
      out[(y * W + x) * 4 + c] = a;
    }
  }
  im.data.set(out);
  document.getElementById("labSrCv").getContext("2d").putImageData(im, 0, 0);
}

/* 滤镜演示: CSS filter 近似模拟各画面预设 */
function labGradeDemo() {
  const MAP = [
    ["off", "原画", "none"],
    ["teal", "🎬 青橙电影", "saturate(1.15) hue-rotate(-12deg) contrast(1.06)"],
    ["film", "🎞 低饱和胶片", "saturate(.7) brightness(1.05) contrast(.94) sepia(.12)"],
    ["noir", "🌃 暗调悬疑", "brightness(.78) contrast(1.2) saturate(.85)"],
    ["shinkai", "🌸 新海诚风", "saturate(1.32) brightness(1.09) contrast(1.07) hue-rotate(4deg)"],
    ["cyber", "🌆 赛博朋克", "saturate(1.35) contrast(1.16) hue-rotate(-16deg)"],
    ["mono", "⬛ 黑白映画", "grayscale(1) contrast(1.16)"],
  ];
  const btns = document.getElementById("labGradeBtns");
  btns.innerHTML = MAP.map(([v, n]) => `<button class="tool-btn sm" data-g="${v}">${n}</button>`).join("");
  btns.onclick = e => {
    const b = e.target.closest("[data-g]"); if (!b) return;
    const m = MAP.find(x => x[0] === b.dataset.g);
    document.getElementById("labGradeImg").style.filter = m[2];
    document.getElementById("labGradeName").textContent = m[1].replace(/^\S+\s/, "");
    if (SR && SR.setGrade) SR.setGrade(b.dataset.g);   // 顺手帮用户切真实预设
  };
}

/* 插帧演示: 左 24fps 跳动小球, 右插帧平滑小球 */
function labFiDemo() {
  const c24 = document.getElementById("labF24").getContext("2d");
  const c60 = document.getElementById("labF60").getContext("2d");
  const IV = 1000 / 24;
  let prev = 0, prevT = 0, lastHold = 0;
  const draw = (c, x) => {
    c.clearRect(0, 0, 300, 90);
    c.fillStyle = "#f5f5f5"; c.fillRect(0, 0, 300, 90);
    c.strokeStyle = "#ddd"; c.strokeRect(6, 12, 288, 66);
    c.fillStyle = "#e02434"; c.beginPath(); c.arc(x, 45, 12, 0, 7); c.fill();
  };
  const pos = t => { const p = (t % 2600) / 2600; return 18 + p * 264; };
  (function loop(t) {
    if (!document.getElementById("labF24")) return;   // 离开页面停止
    // 左: 位置每 41.7ms 才跳一次 (3:2 抖动感)
    if (t - lastHold >= IV) { prev = pos(t); prevT = t - (t % IV); lastHold = t; }
    draw(c24, prev);
    // 右: 模拟插帧 = 上一保持位置与当前位置按相位混合 (与播放器同思路)
    const ph = Math.min(1, (t - prevT) / IV);
    draw(c60, prev + (pos(t) - prev) * ph);
    requestAnimationFrame(loop);
  })(0);
}

/* 音频试听: 振荡器合成旋律 -> 与播放器 CINE 完全相同的处理链
   (轰鸣高切/人声三段EQ/压缩 -> M/S 矩阵 -> 侧链低切/宽化/Haas/早期反射) */
const LABAU = { ac: null, playing: false, wide: false, voc: false, timer: 0 };
window.labAudioToggle = () => {
  const btn = document.getElementById("labAudioBtn");
  if (LABAU.playing) { labAudioStop(); return; }
  if (!LABAU.ac) {
    const ac = LABAU.ac = new (window.AudioContext || window.webkitAudioContext)();
    const B = () => ac.createBiquadFilter();
    LABAU.master = ac.createGain(); LABAU.master.gain.value = 0.16;
    // -- 对白增强 4 段 (参数与 CINE 一致) --
    LABAU.rumble = B(); LABAU.rumble.type = "highpass"; LABAU.rumble.frequency.value = 20;
    LABAU.eqLow = B(); LABAU.eqLow.type = "peaking"; LABAU.eqLow.frequency.value = 300; LABAU.eqLow.Q.value = 1.1; LABAU.eqLow.gain.value = 0;
    LABAU.eq = B(); LABAU.eq.type = "peaking"; LABAU.eq.frequency.value = 2500; LABAU.eq.Q.value = 0.8; LABAU.eq.gain.value = 0;
    LABAU.air = B(); LABAU.air.type = "peaking"; LABAU.air.frequency.value = 5200; LABAU.air.gain.value = 0;
    LABAU.comp = ac.createDynamicsCompressor();
    LABAU.comp.threshold.value = 0; LABAU.comp.knee.value = 14; LABAU.comp.ratio.value = 1;
    LABAU.comp.attack.value = 0.004; LABAU.comp.release.value = 0.24;
    // -- M/S 矩阵 --
    const sp = ac.createChannelSplitter(2), mrg = ac.createChannelMerger(2);
    LABAU.mid = ac.createGain(); LABAU.mid.gain.value = 0.5;
    const sg = ac.createGain(); sg.gain.value = 0.5;
    const inv = ac.createGain(); inv.gain.value = -1;
    sp.connect(LABAU.mid, 0); sp.connect(LABAU.mid, 1);
    sp.connect(sg, 0); sp.connect(inv, 1); inv.connect(sg);
    // -- 侧链: 对白衰减 -> 低切 -> 宽化 -> Haas -> 27ms 早期反射 --
    LABAU.duck = ac.createGain(); LABAU.duck.gain.value = 1;
    LABAU.cut = B(); LABAU.cut.type = "highpass"; LABAU.cut.frequency.value = 10;
    LABAU.side = ac.createGain(); LABAU.side.gain.value = 1;
    LABAU.delay = ac.createDelay(0.05); LABAU.delay.delayTime.value = 0;
    const rd = ac.createDelay(0.05); rd.delayTime.value = 0.027;
    LABAU.refL = ac.createGain(); LABAU.refL.gain.value = 0;
    LABAU.refR = ac.createGain(); LABAU.refR.gain.value = 0;
    const inv2 = ac.createGain(); inv2.gain.value = -1;
    const inv3 = ac.createGain(); inv3.gain.value = -1;
    sg.connect(LABAU.duck); LABAU.duck.connect(LABAU.cut);
    LABAU.cut.connect(LABAU.side); LABAU.side.connect(LABAU.delay);
    LABAU.mid.connect(mrg, 0, 0); LABAU.side.connect(mrg, 0, 0);
    LABAU.side.connect(rd); rd.connect(LABAU.refL); LABAU.refL.connect(mrg, 0, 0);
    LABAU.mid.connect(mrg, 0, 1); LABAU.delay.connect(inv2); inv2.connect(mrg, 0, 1);
    rd.connect(inv3); inv3.connect(LABAU.refR); LABAU.refR.connect(mrg, 0, 1);
    LABAU.master.connect(LABAU.rumble); LABAU.rumble.connect(LABAU.eqLow);
    LABAU.eqLow.connect(LABAU.eq); LABAU.eq.connect(LABAU.air);
    LABAU.air.connect(LABAU.comp); LABAU.comp.connect(sp);
    mrg.connect(ac.destination);
  }
  const ac = LABAU.ac;
  if (ac.state === "suspended") ac.resume();
  const notes = [523, 659, 784, 1047, 784, 659];   // C5 E5 G5 C6 G5 E5 琶音, 中置声像便于听出加宽
  let i = 0;
  const osc = ac.createOscillator(); osc.type = "triangle";
  const g = ac.createGain(); g.gain.value = 0.9;
  osc.connect(g); g.connect(LABAU.master);
  osc.frequency.value = notes[0]; osc.start();
  LABAU._osc = osc;
  LABAU.timer = setInterval(() => { i = (i + 1) % notes.length; osc.frequency.setTargetAtTime(notes[i], ac.currentTime, 0.02); }, 380);
  LABAU.playing = true;
  btn.textContent = "⏸ 停止试听";
};
function labAudioStop() {
  if (!LABAU.playing) return;
  clearInterval(LABAU.timer);
  try { LABAU._osc.stop(); } catch { }
  LABAU.playing = false;
  const btn = document.getElementById("labAudioBtn");
  if (btn) btn.textContent = "▶ 试听一段旋律";
}
window.labWideToggle = () => {
  LABAU.wide = !LABAU.wide;
  const btn = document.getElementById("labWideBtn");
  if (LABAU.ac) {
    const t = LABAU.ac.currentTime;   // 与 CINE.toggleWide 参数完全一致
    LABAU.cut.frequency.setTargetAtTime(LABAU.wide ? 220 : 10, t, 0.08);
    LABAU.side.gain.setTargetAtTime(LABAU.wide ? 3.4 : 1.0, t, 0.08);
    LABAU.delay.delayTime.setTargetAtTime(LABAU.wide ? 0.015 : 0, t, 0.08);
    LABAU.refL.gain.setTargetAtTime(LABAU.wide ? 0.3 : 0, t, 0.08);
    LABAU.refR.gain.setTargetAtTime(LABAU.wide ? 0.3 : 0, t, 0.08);
  }
  if (btn) btn.textContent = LABAU.wide ? "立体音效: 开" : "立体音效: 关";
};
window.labVocToggle = () => {
  LABAU.voc = !LABAU.voc;
  const btn = document.getElementById("labVocBtn");
  if (LABAU.ac) {
    const t = LABAU.ac.currentTime;   // 与 CINE.toggleVoc 参数完全一致
    LABAU.rumble.frequency.setTargetAtTime(LABAU.voc ? 95 : 20, t, 0.08);
    LABAU.eqLow.gain.setTargetAtTime(LABAU.voc ? 2.5 : 0, t, 0.08);
    LABAU.eq.gain.setTargetAtTime(LABAU.voc ? 7 : 0, t, 0.08);
    LABAU.air.gain.setTargetAtTime(LABAU.voc ? 3.5 : 0, t, 0.08);
    LABAU.comp.threshold.setTargetAtTime(LABAU.voc ? -24 : 0, t, 0.08);
    LABAU.comp.ratio.setTargetAtTime(LABAU.voc ? 2.8 : 1, t, 0.08);
    LABAU.mid.gain.setTargetAtTime(LABAU.voc ? 0.62 : 0.5, t, 0.08);
    LABAU.duck.gain.setTargetAtTime(LABAU.voc ? 0.82 : 1.0, t, 0.08);
  }
  if (btn) btn.textContent = LABAU.voc ? "对白增强: 开" : "对白增强: 关";
};

window.mineTab = t => {
  document.querySelectorAll(".mine-tabs .chip").forEach(c => c.classList.toggle("active", c.dataset.t === t));
  document.getElementById("mineFav").style.display = t === "fav" ? "" : "none";
  document.getElementById("mineHist").style.display = t === "hist" ? "" : "none";
  const about = document.getElementById("mineAbout");
  if (about) about.style.display = t === "about" ? "" : "none";
  const src = document.getElementById("mineSrc");
  if (src) { src.style.display = t === "src" ? "" : "none"; if (t === "src") srcChLoad(); }
  const lab = document.getElementById("mineLab");
  if (lab) { lab.style.display = t === "lab" ? "" : "none"; if (t === "lab") labInit(); else if (window.labAudioStop && LABAU.playing) labAudioStop(); }
};
/* 动漫源上游手动自检修复 (我的板块): 与播放失败页 srcSelfCheck 同一后端接口,
   清服务端失效域名记忆/线路缓存并并行探测全部上游, 结果就地展示 */
window.mineSrcCheck = async function () {
  const box = document.getElementById("mineSrcRes");
  if (!box) return;
  box.innerHTML = `<div class="src-check-run">${mangaLoad("正在自检播放源上游…")}</div>`;
  try {
    const r = await api("/anime/source_check");
    const row = s => `<span class="src-st ${s.ok ? "ok" : "bad"}">${s.ok ? "✓" : "✗"} ${esc(s.name)}<i>${s.ms || 0}ms</i></span>`;
    const list = [...(r.age ? [r.age] : []), ...((r.ext || [])), ...((r.channels || []))];
    const okCnt = list.filter(s => s.ok).length;
    box.innerHTML = `<div class="src-check-res">
      <div class="src-check-t">${okCnt ? `✅ 自检完成: 已清理失效缓存, ${okCnt}/${list.length} 个源可用` : "⚠️ 自检完成: 上游全部不可达"}</div>
      <div class="src-check-list">${list.map(row).join("")}</div>
      <div class="wiki-fail-btns">
        ${okCnt ? `<button class="tool-btn" onclick="location.hash='#/'">🏠 去首页观看</button>` : ""}
        <button class="tool-btn" onclick="mineSrcCheck()">🔧 再检一次</button>
      </div></div>`;
    toast(okCnt ? `自检完成: ${okCnt}/${list.length} 个源可用, 已清理失效缓存` : "自检完成: 上游全部不可达, 请检查本机网络");
  } catch (e) {
    box.innerHTML = `<div class="src-check-res"><div class="src-check-t">⚠️ 自检失败: ${esc(e.message || "网络异常")}</div>
      <div class="wiki-fail-btns"><button class="tool-btn" onclick="mineSrcCheck()">🔧 再检一次</button></div></div>`;
  }
};
/* ---------- 外部片源渠道 (声明式 maccms 源包, 放在「动漫文件夹/外部片源」) ---------- */
let _chCache = [];
let _chEditId = "";
window.srcChLoad = async function () {
  const box = document.getElementById("srcChList");
  if (!box) return;
  box.innerHTML = `<div style="font-size:13px;color:#666">正在读取渠道…</div>`;
  try {
    _chCache = await api("/src/channels") || [];
    if (!_chCache.length) {
      box.innerHTML = `<div style="font-size:13px;color:#666">暂无外部渠道。可在下方添加, 或把渠道 JSON 放进「动漫文件夹/外部片源」。</div>`;
      return;
    }
    box.innerHTML = _chCache.map((c, i) => `<div class="src-ch-item">
      <div class="src-ch-main"><b>${esc(c.name)}</b>
        <span class="src-ch-st ${c.ok && c.enabled ? "on" : ""}">${c.error ? "⚠ " + esc(c.error) : (c.enabled ? "已启用" : "已停用")}</span>
      </div>
      <div class="src-ch-api">${esc(c.api || "—")}${(c.mirrors || []).length ? ` · 镜像 ${c.mirrors.length}` : ""}</div>
      <div class="src-ch-ops">
        <a href="javascript:void(0)" onclick="srcChToggle(${i})">${c.enabled ? "停用" : "启用"}</a>
        <a href="javascript:void(0)" onclick="srcChEdit(${i})">编辑</a>
        <a href="javascript:void(0)" onclick="srcChDel(${i})">删除</a>
      </div></div>`).join("");
  } catch (e) {
    box.innerHTML = `<div style="font-size:13px;color:#c0392b">读取失败: ${esc(e.message)}</div>`;
  }
};
window.srcChEdit = function (i) {
  const c = _chCache[i];
  if (!c) return;
  _chEditId = c.id;
  const el = id => document.getElementById(id);
  if (el("chName")) el("chName").value = c.name || "";
  if (el("chApi")) el("chApi").value = c.api || "";
  if (el("chMir")) el("chMir").value = (c.mirrors || []).join(",");
  toast("已填入, 修改后点「添加/保存渠道」");
};
window.srcChSave = async function () {
  const v = id => (document.getElementById(id) || {}).value || "";
  const name = v("chName").trim(), apiUrl = v("chApi").trim();
  if (!name || !apiUrl) { toast("请填写渠道名称与接口地址"); return; }
  try {
    let q = "/src/channel/save?name=" + encodeURIComponent(name) +
            "&api=" + encodeURIComponent(apiUrl) +
            "&mirrors=" + encodeURIComponent(v("chMir"));
    if (_chEditId) q += "&id=" + encodeURIComponent(_chEditId);
    await api(q);
    _chEditId = "";
    ["chName", "chApi", "chMir"].forEach(id => { const e = document.getElementById(id); if (e) e.value = ""; });
    toast("渠道已保存, 已自动追加到片源列表");
    srcChLoad();
  } catch (e) { toast("保存失败: " + e.message); }
};
window.srcChToggle = async function (i) {
  const c = _chCache[i];
  if (!c) return;
  try {
    await api("/src/channel/toggle?id=" + encodeURIComponent(c.id) + "&enabled=" + (c.enabled ? "0" : "1"));
    toast(c.enabled ? "已停用渠道" : "已启用渠道");
    srcChLoad();
  } catch (e) { toast("操作失败: " + e.message); }
};
window.srcChDel = async function (i) {
  const c = _chCache[i];
  if (!c || !confirm(`删除渠道「${c.name}」?`)) return;
  try {
    await api("/src/channel/delete?id=" + encodeURIComponent(c.id));
    toast("已删除渠道");
    srcChLoad();
  } catch (e) { toast("删除失败: " + e.message); }
};
window.srcChOpen = async function () {
  try { const d = await api("/src/channel/open"); toast("已打开: " + (d.dir || "片源文件夹")); }
  catch (e) { toast("打开失败: " + e.message); }
};
/* ---------- 弹幕: canvas-danmaku (danmaku@2 canvas 引擎) 实时绘制 ---------- */
let _dm = null;
const _dmProbe = {};   // "剧名|集" -> B站弹幕条数 (页面内复用, 详情页/播放页共享)
async function dmProbe(title, ep) {
  const k = title + "|" + ep;
  if (k in _dmProbe) return _dmProbe[k];
  try {
    const list = await api("/danmaku?title=" + encodeURIComponent(title) + "&ep=" + encodeURIComponent(ep));
    _dmProbe[k] = Array.isArray(list) ? list.length : 0;
  } catch { _dmProbe[k] = -1; }
  return _dmProbe[k];
}
const DM_DEFAULT = { on: true, size: 1, speed: 1 };
function dmCfg() {
  let c = {};
  try { c = JSON.parse(localStorage.getItem("dm_cfg") || "{}") || {}; } catch { }
  const out = Object.assign({}, DM_DEFAULT, c);
  if (localStorage.getItem("dm_on") === "0") out.on = false;
  return out;
}
function dmCfgSave(c) {
  localStorage.setItem("dm_cfg", JSON.stringify(c));
  localStorage.setItem("dm_on", c.on ? "1" : "0");
}
const _hexColor = c => c ? "#" + ((c >>> 0) & 0xffffff).toString(16).padStart(6, "0") : "#fff";

function dmStop() {
  if (!_dm) return;
  try { if (_dm.inst) _dm.inst.destroy(); } catch { }
  window.removeEventListener("resize", _dm.onResize);
  document.removeEventListener("fullscreenchange", _dm.onFs);
  _dm = null;
}

function dmBoot(title, ep, key) {
  dmStop();
  const v = document.getElementById("videoPlayer");
  const stage = document.getElementById("dmStage");
  if (!v || !stage) return;
  const btn = document.getElementById("dmToggle");
  const st = {
    v, stage, title, ep,
    key: key || (title + ":" + ep),
    cfg: dmCfg(), bili: [], local: [], inst: null,
    style: null, fs: 22, onResize: null,
  };
  try { st.local = JSON.parse(localStorage.getItem("dm_local:" + st.key)) || []; } catch { st.local = []; }
  _dm = st;

  const paintToggle = () => {
    if (!btn) return;
    btn.textContent = st.cfg.on ? "弹 开" : "弹 关";
    btn.title = st.cfg.on ? "弹幕已开启" : "弹幕已关闭";
  };

  /* 弹幕提示: 先显示缓存结果, 数据到位后更新条数 */
  const hint = document.getElementById("dmHint");
  const _ck = title + "|" + ep;
  const paintHint = n => {
    if (!hint) return;
    hint.textContent = n === null ? "弹幕加载中…" : (n < 0 ? "弹幕获取失败" : (n ? `B站弹幕 ${n} 条` : "暂无B站弹幕"));
    hint.classList.toggle("on", n > 0);
  };
  paintHint(typeof _dmProbe[_ck] === "number" ? _dmProbe[_ck] : null);

  /* 用库重建渲染实例 (cfg 变化 / 弹幕数据到位 / 尺寸明显变化时) */
  const build = () => {
    if (st.inst) { try { st.inst.destroy(); } catch { } st.inst = null; }
    if (!window.Danmaku) return;
    const w = stage.clientWidth || v.clientWidth || 640;
    const fs = Math.round(Math.max(18, Math.min(30, w * 0.022)) * st.cfg.size);
    st.fs = fs;
    st.style = {
      font: `700 ${fs}px "Microsoft YaHei","PingFang SC",sans-serif`,
      fillStyle: "#fff",
      strokeStyle: "rgba(0,0,0,.72)",
      lineWidth: Math.max(1.5, fs / 11),
      textBaseline: "top",
    };
    const toC = d => ({
      text: d.x,
      time: d.t,
      mode: d.m === 5 ? "top" : (d.m === 4 ? "bottom" : "rtl"),
      style: Object.assign({}, st.style, { fillStyle: _hexColor(d.c || 16777215) }),
    });
    const list = st.bili.concat(st.local).map(toC).sort((a, b) => a.time - b.time);
    try {
      st.inst = new Danmaku({
        container: stage,
        media: v,
        engine: "canvas",
        comments: st.cfg.on ? list : [],
        speed: 144 * st.cfg.speed,
      });
    } catch (e) {
      console.warn("[dm] 初始化失败", e);
    }
  };

  /* B站弹幕 (异步, 失败静默: 仅保留本地弹幕) */
  fetch("/api/danmaku?title=" + encodeURIComponent(title) + "&ep=" + encodeURIComponent(ep))
    .then(r => r.json())
    .then(j => {
      if (_dm !== st || !j || !j.ok) return;
      st.bili = (j.data || []).filter(d => d && d.x && (d.m === 1 || d.m === 4 || d.m === 5));
      _dmProbe[_ck] = st.bili.length;
      paintHint(st.bili.length);
      if (st.bili.length) build();
    })
    .catch(() => { if (_dm === st) paintHint(-1); });

  /* 弹幕开关 */
  if (btn) btn.onclick = () => {
    st.cfg.on = !st.cfg.on;
    dmCfgSave(st.cfg);
    paintToggle();
    build();
  };

  /* 尺寸变化: 字号变化明显时重建, 否则只让库重排 */
  let rzT = 0;
  st.onResize = () => {
    clearTimeout(rzT);
    rzT = setTimeout(() => {
      if (_dm !== st) return;
      const w = stage.clientWidth || 640;
      const fs = Math.round(Math.max(18, Math.min(30, w * 0.022)) * st.cfg.size);
      if (Math.abs(fs - st.fs) > 2) { build(); return; }
      try { st.inst && st.inst.resize(); } catch { }
    }, 160);
  };
  window.addEventListener("resize", st.onResize);
  /* 原生全屏只放大 video 元素, 同级弹幕层会被裁掉 -> 转发到播放器外壳全屏, 保留弹幕 */
  st.onFs = () => {
    const wrap = v.closest(".player-wrap");
    if (document.fullscreenElement === v && wrap && wrap.requestFullscreen) {
      document.exitFullscreen().then(() => wrap.requestFullscreen()).catch(() => { });
      return;
    }
    st.onResize();
  };
  document.addEventListener("fullscreenchange", st.onFs);

  paintToggle(); build();
}

/* ---------- 版本检查 (只提示, 不提供下载; 唯一官方渠道 = GitHub 发布页) ---------- */
let APP_VERSION = "2.5.8";
/* 唯一公开仓库: 其余仓库均已设为私人 */
const OFFICIAL_URL = "https://github.com/drlinesu/chaoyue-ji-xian-waiyuan";
/* 版本检查结果渲染: 有新版本时给出官方下载入口 (应用内不做下载/安装) */
function _updRender(d) {
  const info = document.getElementById("updInfo");
  const btn = document.getElementById("updBtn");
  if (!btn) return;
  APP_VERSION = d.current || APP_VERSION;
  const official = d.official || OFFICIAL_URL;
  btn.disabled = false;
  if (d.has_update) {
    if (info) info.textContent = `发现新版本 ${d.latest}（当前 ${d.current}）· 请前往官方渠道下载`;
    btn.textContent = "📥 前往 GitHub 下载";
    btn.onclick = () => window.open(official, "_blank", "noopener");
  } else {
    if (info) info.textContent = d.error ? `版本 ${d.current} · 暂时无法检查更新` : `已是最新版本 ${d.current}`;
    btn.textContent = "🔄 重新检查";
    btn.onclick = () => updateFlow();
  }
}
window.updateFlow = async function () {
  const btn = document.getElementById("updBtn");
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = "⏳ 检查中...";
  try {
    _updRender(await api("/update/check"));
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "🔄 检查更新";
    toast("检查失败: " + e.message);
  }
};
function rowItem(x) {
  return `<div class="card-row" onclick="location.hash='#/anime/${esc(x.id)}'">
    <img src="${img(x.cover)}" alt="">
    <div class="info"><div class="t">${esc(x.title)}</div>
      <div class="s">${new Date(x.ts || Date.now()).toLocaleDateString()}</div></div>
    <span class="del" data-key="${esc(x.key)}">✕</span>
  </div>`;
}

/* ---------- 插图背景 (localStorage 持久化) ---------- */
const BG_KEY = "cy_bg_url";
function bgGet() { try { return localStorage.getItem(BG_KEY) || ""; } catch { return ""; } }
function bgSet(url) {
  try { localStorage.setItem(BG_KEY, url); } catch {}
  bgApply();
}
function bgClear() {
  try { localStorage.removeItem(BG_KEY); } catch {}
  bgApply();
}
function bgApply() {
  const cur = bgGet();
  const layer = document.getElementById("bgLayer");
  if (!layer) return;
  if (cur) {
    // 用 img proxy 包装, 经后端缓存
    layer.innerHTML = `<div class="bg-img-wrap" style="background-image:url('${img(cur)}')"></div>`;
    layer.style.display = "block";
    document.body.classList.add("has-bg");
  } else {
    layer.innerHTML = "";
    layer.style.display = "none";
    document.body.classList.remove("has-bg");
  }
}
// 页面加载即恢复背景
bgApply();

/* ---------- 插画收藏 (localStorage 持久化, 按收藏时间倒序) ---------- */
const IFAV_KEY = "cy_illus_favs";
let _illusTab = "random";
function ifavGet() {
  try {
    const v = JSON.parse(localStorage.getItem(IFAV_KEY));
    if (!Array.isArray(v)) return [];                     // 数据损坏时回退空列表
    return v.filter(x => x && typeof x.url === "string"); // 丢弃脏条目
  } catch { return []; }
}
function ifavSave(list) {
  try {
    localStorage.setItem(IFAV_KEY, JSON.stringify(list));
    return true;
  } catch {
    toast("收藏保存失败: 本地存储不可用");
    return false;
  }
}
function ifavHas(u) { return ifavGet().some(x => x.url === u); }
function ifavToggle(u) {
  const list = ifavGet();
  const i = list.findIndex(x => x.url === u);
  if (i >= 0) {
    list.splice(i, 1);
    if (!ifavSave(list)) return null;   // 写失败 → 返回 null, UI 状态不变
    return false;
  }
  list.unshift({ url: u, ts: Date.now() });
  if (!ifavSave(list)) return null;
  return true;
}
window.illusFav = function (u) {
  const on = ifavToggle(u);
  if (on === null) return;   // 保存失败时不更新界面
  toast(on ? "已收藏插画 ★" : "已取消收藏");
  document.querySelectorAll(".illus-card .illus-fav-btn").forEach(btn => {
    if (btn.dataset.url === u) {
      btn.classList.toggle("on", on);
      btn.textContent = on ? "★" : "☆";
    }
  });
  if (_illusTab === "fav") renderIllusFavTab();   // 收藏页里取消后即时移除
};
// 跨标签页持久化同步: 其他窗口增删收藏时, 本页星标与收藏夹即时刷新
window.addEventListener("storage", e => {
  if (e.key !== IFAV_KEY) return;
  document.querySelectorAll(".illus-card .illus-fav-btn").forEach(btn => {
    const on = ifavHas(btn.dataset.url);
    btn.classList.toggle("on", on);
    btn.textContent = on ? "★" : "☆";
  });
  if (_illusTab === "fav") renderIllusFavTab();
});

/* ---------- 插图 (p站风画廊 + 灯箱 + 设为背景 + 收藏) ---------- */
function illusCard(u) {
  const isCur = bgGet() === u;
  const isFav = ifavHas(u);
  return `<div class="illus-card" onclick="window.openLightbox('${esc(u)}')">
    <img src="${img(u)}" loading="lazy" decoding="async" fetchpriority="low" alt="">
    <span class="illus-badge">✦ 插画</span>
    <div class="illus-acts">
      <button class="illus-fav-btn${isFav ? " on" : ""}" data-url="${esc(u)}" title="${isFav ? '取消收藏' : '收藏插画'}" onclick="event.stopPropagation();window.illusFav('${esc(u)}')">${isFav ? '★' : '☆'}</button>
      <button class="illus-bg-btn${isCur ? " active" : ""}" title="${isCur ? '当前背景' : '设为背景'}" onclick="event.stopPropagation();window.illusSetBg('${esc(u)}')">${isCur ? '✓ 背景' : '设为背景'}</button>
    </div>
  </div>`;
}
window.illusSetBg = function(u) {
  if (bgGet() === u) {
    bgClear();
    toast("已恢复默认背景");
  } else {
    bgSet(u);
    toast("已设为全站背景 ✓");
  }
};
window.openLightbox = function (u) {
  const box = document.createElement("div");
  box.className = "lightbox";
  box.innerHTML = `<img src="${img(u)}" alt=""><span class="lb-hint">点击任意处关闭</span>`;
  box.onclick = () => box.remove();
  document.addEventListener("keydown", function h(e) {
    if (e.key === "Escape") { box.remove(); document.removeEventListener("keydown", h); }
  });
  document.body.appendChild(box);
};

/* ---------- 页面：插图画廊 (随机推荐 / 我的收藏) ---------- */
function renderIllusFavTab() {
  const el = document.getElementById("illusCols");
  if (!el) return;
  const list = ifavGet();
  el.innerHTML = list.length
    ? list.map(x => illusCard(x.url)).join("")
    : `<div class="empty"><div class="icon">★</div>还没有收藏插画<br><small>悬浮卡片点 ☆ 即可收藏</small></div>`;
}
window.illusSwitchTab = function (t) {
  if (t === _illusTab) return;
  _illusTab = t;
  document.querySelectorAll(".illus-tab").forEach(b => b.classList.toggle("active", b.dataset.t === t));
  if (t === "fav") renderIllusFavTab();
  else pageIllus(0);
};
async function pageIllus(refresh, tab) {
  document.title = "插画画廊 - 超越次元";
  if (tab) _illusTab = tab;
  const hasBg = !!bgGet();
  const skel = _illusTab === "random" ? Array(24).fill(`<div class="illus-card skeleton"></div>`).join("") : "";
  $app.innerHTML = `
    <div class="section-head"><h2>🎨 插画画廊</h2><span style="flex:1"></span>
      ${hasBg ? '<button class="tool-btn" onclick="bgClear();toast(\'已恢复默认背景\')">🗑 恢复默认背景</button>' : ''}
      ${_illusTab === "random" ? '<button class="tool-btn" onclick="pageIllus(1)">🔄 换一批</button>' : ''}</div>
    <div class="illus-tabs">
      <button class="illus-tab${_illusTab === "random" ? " active" : ""}" data-t="random" onclick="illusSwitchTab('random')">🎲 随机推荐</button>
      <button class="illus-tab${_illusTab === "fav" ? " active" : ""}" data-t="fav" onclick="illusSwitchTab('fav')">★ 我的收藏</button>
    </div>
    <div class="illus-cols" id="illusCols">${skel}</div>`;
  if (_illusTab === "fav") { renderIllusFavTab(); return; }
  try {
    const list = await api("/anime/illus?n=24" + (refresh ? "&refresh=1" : ""));
    const el = document.getElementById("illusCols");
    if (el) el.innerHTML = list.length
      ? list.map(x => illusCard(x.url)).join("")
      : `<div class="empty"><div class="icon">🎨</div>插图源暂不可用</div>`;
  } catch (e) {
    const el = document.getElementById("illusCols");
    if (el) el.innerHTML = `<div class="empty"><div class="icon">⚠️</div>${esc(e.message)}</div>`;
  }
}
window.pageIllus = pageIllus;

/* ---------- 在线下载 ---------- */
const DL_STATUS = {
  waiting: ["排队中", "st-wait"], downloading: ["下载中", "st-dl"],
  done: ["已完成", "st-ok"], error: ["失败", "st-err"], cancel: ["已取消", "st-cancel"],
};
function fmtSize(b) {
  b = Number(b) || 0;
  if (b >= 1073741824) return (b / 1073741824).toFixed(2) + " GB";
  if (b >= 1048576) return (b / 1048576).toFixed(1) + " MB";
  return Math.max(1, Math.round(b / 1024)) + " KB";
}
window.dlStart = async (id, line, ep, nameEnc, titleEnc) => {
  try {
    await api(`/download/start?id=${encodeURIComponent(id)}&line=${encodeURIComponent(line)}&ep=${encodeURIComponent(ep)}&name=${nameEnc}&title=${titleEnc}`);
    toast("已加入下载队列, 可在「下载」页查看进度");
  } catch (e) { toast("下载失败: " + e.message); }
};
window.dlCancel = async jid => {
  try { await api("/download/cancel?job=" + encodeURIComponent(jid)); toast("已取消任务"); }
  catch (e) { toast(e.message); }
};
window.dlOpenDir = async () => {
  try { await api("/download/open"); } catch { }
};
window.dlRenderJob = function (j) {
  const [label, cls] = DL_STATUS[j.status] || [j.status, ""];
  const live = j.status === "waiting" || j.status === "downloading";
  return `<div class="dl-card">
    <div class="dl-head">
      <div class="dl-name">📺 ${esc(j.title)} <em>${esc(j.ep)}</em></div>
      <span class="dl-st ${cls}">${label}${live ? " " + (j.progress || 0).toFixed(0) + "%" : ""}</span>
    </div>
    ${live ? `<div class="dl-bar"><i style="width:${Math.max(2, j.progress || 0)}%"></i></div>
      <div class="dl-meta"><span>${j.total ? `分段 ${j.done || 0}/${j.total}` : "准备中..."}</span><span>${fmtSize(j.size)}</span></div>` : ""}
    ${j.status === "error" ? `<div class="dl-err">⚠️ ${esc(j.error || "未知错误")}</div>` : ""}
    ${j.status === "done" && j.file ? `<div class="dl-file">💾 ${esc(j.file)}</div>` : ""}
    ${live ? `<button class="tool-btn sm" onclick="dlCancel('${esc(j.id)}')">✕ 取消</button>` : ""}
  </div>`;
};

/* ---------- 页面：下载管理 ---------- */
let _dlTimer = null;
async function pageDownloads() {
  document.title = "下载管理 - 超越次元";
  // 骨架即时呈现, 数据到达后填充, 不阻塞页面 (本地接口偶发解析抖动时不再白屏等待)
  $app.innerHTML = `
    <div class="section-head"><h2>⬇ 下载管理</h2><span style="flex:1"></span>
      <button class="tool-btn" onclick="dlOpenDir()">📂 打开文件夹</button></div>
    <p class="illus-tip" id="dlDir"></p>
    <div id="dlJobs"><div class="empty" style="padding:18px"><div class="icon">⬇</div>加载中...</div></div>
    <div class="section-head" style="margin-top:26px"><h2>📁 已下载文件</h2></div>
    <div id="dlFiles"><div class="empty">加载中...</div></div>`;
  const load = async () => {
    let d;
    try { d = await api("/download/list"); } catch { return; }
    const dirEl = document.getElementById("dlDir");
    if (dirEl) dirEl.textContent = "保存位置: " + d.dir + (d.ffmpeg ? " · 已检测到 ffmpeg (输出 mp4)" : " · 未检测到 ffmpeg (输出 ts, 加密流无法下载)");
    const jobsEl = document.getElementById("dlJobs");
    if (jobsEl) jobsEl.innerHTML = d.jobs.length
      ? d.jobs.map(window.dlRenderJob).join("")
      : `<div class="empty" style="padding:18px"><div class="icon">⬇</div>暂无下载任务, 在播放页点击「⬇ 下载本集」即可</div>`;
    const filesEl = document.getElementById("dlFiles");
    if (filesEl) filesEl.innerHTML = d.files.length
      ? `<div class="dl-files">${d.files.map(f =>
        `<div class="dl-file-row"><span class="n">📄 ${esc(f.name)}</span><span class="s">${fmtSize(f.size)}</span></div>`).join("")}</div>`
      : `<div class="empty" style="padding:18px"><div class="icon">📭</div>还没有已完成的文件</div>`;
  };
  load();   // 异步填充, 页面即时可见
  if (_dlTimer) clearInterval(_dlTimer);
  _dlTimer = setInterval(load, 2000);
}

/* ---------- 随机动漫插图横幅 (搜索栏上方, 90s 换一张 + 贴底进度条提示换图时机) ---------- */
(function randomBg() {
  const el = document.createElement("div");
  el.className = "bg-illus";
  el.innerHTML = '<img alt=""><div class="bg-illus-progress"><i></i></div>';
  el.style.display = "none";   // 首批插图就绪前不占位, 插源不可用时保持隐藏
  document.body.prepend(el);
  const im = el.querySelector("img");
  const bar = el.querySelector(".bg-illus-progress i");
  let pool = [];
  function restartProg() {   // reflow 重启 90s 线性动画; 无图/自设背景/减动效场景由样式自动隐藏
    const wasHidden = el.style.display === "none";
    el.style.display = pool.length ? "" : "none";
    bar.classList.remove("run");
    void bar.offsetWidth;
    bar.classList.add("run");
    if (wasHidden && pool.length) {
      // 横幅弹出使内容整体下移, 补偿等量滚动保持搜索栏顶点与阅读位置不跳动
      window.scrollBy({ top: el.offsetHeight, behavior: "instant" });
    }
  }
  function rotate() {
    if (!pool.length) return;
    const next = pool[Math.floor(Math.random() * pool.length)];
    if (im.src === img(next)) return;
    im.style.opacity = "0";
    setTimeout(() => { im.src = img(next); im.style.opacity = ""; }, 500);
  }
  async function pull() {
    try {
      const list = await api("/anime/illus?n=8");
      pool = (list || []).map(x => x.url);
      if (!im.src) { rotate(); restartProg(); } else if (Math.random() < .6) rotate();
    } catch { }
  }
  im.onerror = () => { im.removeAttribute("src"); pull(); };
  pull();
  setInterval(pull, 6 * 60 * 1000);
  setInterval(() => { rotate(); restartProg(); }, 90 * 1000);
})();

/* ---------- 动态 favicon: 2D 少女逐帧动画 (色相流转+星光呼吸) ---------- */
(function dynamicFavicon() {
  const link = document.querySelector('link[rel="icon"]');
  if (!link) return;
  const cv = document.createElement("canvas");
  cv.width = cv.height = 64;
  const ctx = cv.getContext("2d");
  const base = new Image();
  base.src = "/icon_girl.jpg";
  let ready = false, idx = 0;
  base.onload = () => { ready = true; };
  const HUES = [0, 40, 80, 130, 180, 220, 270, 320]; // 色相流转序列
  function draw() {
    if (!ready) return;
    const hue = HUES[idx % HUES.length];
    const glow = 1 + 0.12 * Math.sin(idx * 1.3); // 呼吸缩放
    ctx.clearRect(0, 0, 64, 64);
    ctx.save();
    ctx.beginPath(); ctx.arc(32, 32, 31, 0, Math.PI * 2); ctx.clip();
    ctx.filter = `hue-rotate(${hue}deg) saturate(1.35) brightness(1.05)`;
    const s = 64 * glow, off = (64 - s) / 2;
    ctx.drawImage(base, off, off, s, s);
    ctx.restore();
    ctx.filter = "none";
    try { link.href = cv.toDataURL("image/png"); } catch { }
  }
  setInterval(() => { if (!document.hidden) { idx++; draw(); } }, 900);
})();

/* ---------- 樱花飘落 (粉紫电光版) ---------- */
(function sakura() {
  const box = document.createElement("div");
  box.className = "sakura-layer";
  document.body.appendChild(box);
  for (let i = 0; i < 16; i++) {
    const p = document.createElement("i");
    const size = 6 + Math.random() * 10;
    p.style.cssText = `left:${Math.random() * 100}%;width:${size}px;height:${size}px;`
      + `animation-duration:${7 + Math.random() * 8}s,${2 + Math.random() * 3}s;`
      + `animation-delay:-${Math.random() * 12}s,-${Math.random() * 3}s;`
      + `opacity:${.3 + Math.random() * .4}`;
    box.appendChild(p);
  }
})();

/* ---------- 浮动返回按钮 (日漫风, 首页自动隐藏) ---------- */
const $backFab = document.createElement("button");
$backFab.className = "back-fab";
$backFab.innerHTML = "‹ 返回";
$backFab.onclick = () => {
  if (history.length > 1) history.back();
  else location.hash = "#/";
};
document.body.appendChild($backFab);

/* ---------- 滚动位置恢复 (仅浏览器返回/前进时恢复, 全新切页始终从顶部开始) ---------- */
const _scrollMem = {};   // { "#/update": 820, "#/catalog/...": 1240 }
let _lastHash = decodeURIComponent(location.hash) || "#/";
if ("scrollRestoration" in history) history.scrollRestoration = "manual";   // 接管浏览器自动恢复, 避免与手动恢复打架

/* ---------- 路由 ---------- */
const _navStack = [];   // 访问过的 hash 栈, 用于判定前进/返回方向 (history.direction 在部分 WebView 不可用)
let _isPopNav = false;  // 本次 route 是否由浏览器前进/后退 (popstate) 触发
window.addEventListener("popstate", () => { _isPopNav = true; });

function route() {
  const h = decodeURIComponent(location.hash) || "#/";
  // 判定切页方向: popstate 触发时查栈定位, 在栈中靠后位置 = 返回, 否则 = 前进
  let _navDir = "forward";
  if (_isPopNav) {
    _isPopNav = false;
    const idx = _navStack.lastIndexOf(h);
    if (idx >= 0 && idx < _navStack.length - 1) { _navDir = "back"; _navStack.length = idx + 1; }
    else if (idx < 0) _navStack.push(h);
  } else if (_navStack[_navStack.length - 1] !== h) {
    _navStack.push(h);
  }
  // hashchange 触发时 hash 已是新值, 此时 window.scrollY 就是旧页面的 → 存它
  if (_lastHash && _lastHash !== h) {
    _scrollMem[_lastHash] = window.scrollY || document.documentElement.scrollTop;
  }
  _lastHash = h;
  if (_hls) { try { _hls.destroy(); } catch { } _hls = null; }
  if (_watchdog) { clearInterval(_watchdog); _watchdog = null; }
  if (_cTimer) { clearInterval(_cTimer); _cTimer = null; }
  if (_scroll.observer) { try { _scroll.observer.disconnect(); } catch {} _scroll.observer = null; }
  if (_dlTimer) { clearInterval(_dlTimer); _dlTimer = null; }
  wikiClearFailTimer();   // 离开 wiki 浏览页时清上游失败兜底定时器
  wlogStopSession();   // 离开播放页停止观看时长累计
  dmStop();            // 离开播放页销毁弹幕渲染实例 (释放 rAF 与画布)
  const p = h.replace(/^#\//, "").split("/");
  // 解析 ?genre=X 之类 query
  let q = {};
  if (p.length && p[p.length - 1].includes("?")) {
    const [last, qs] = p[p.length - 1].split("?");
    p[p.length - 1] = last;
    new URLSearchParams(qs).forEach((v, k) => q[k] = v);
  }
  // 仅当回到历史访问过的条目 (浏览器返回/前进) 时才恢复滚动位置;
  // 通过导航/链接全新进入的页面一律从顶部开始
  const _isHistNav = !!(history.state && typeof history.state.d === "number");
  if (!_isHistNav) { try { history.replaceState({ d: 1 }, ""); } catch { } }
  $backFab.classList.toggle("show", p[0] !== "");   // 非首页显示返回按钮
  document.body.classList.toggle("immersive", p[0] === "anime" || p[0] === "watch");   // 详情/播放页隐藏底部标签栏
  if (p[0] === "update") { location.replace("#/wiki"); return; }   // 更新表板块已改为 Wiki 板块, 旧链接自动跳转
  const navMap = { "": "home", catalog: "catalog", wiki: "wiki", illus: "illus", reco: "reco", downloads: "downloads", mine: "mine" };
  const nav = navMap[p[0]] !== undefined ? navMap[p[0]] : ((p[0] === "anime" || p[0] === "watch") ? "anime" : "");
  document.querySelectorAll(".mainnav a").forEach(a => a.classList.toggle("active", a.dataset.nav === nav));
  const doScrollRestore = () => {
    if (!_isHistNav) return;   // 全新进入的页面不恢复旧位置
    const saved = _scrollMem[h];
    if (!saved) return;
    delete _scrollMem[h];   // 只恢复一次
    const t0 = Date.now();
    const tryRestore = () => {
      // 列表页异步渲染, 内容高度足够时才恢复, 最多等 1.2s (过早恢复会被 clamp 到顶部)
      if (document.documentElement.scrollHeight > saved + 50) {
        window.scrollTo({ top: saved, behavior: "instant" });
      } else if (Date.now() - t0 < 1200) {
        setTimeout(tryRestore, 60);
      }
    };
    tryRestore();
  };
  // 渲染新页面 (滚顶必须在渲染回调内: View Transition 需先截旧页快照再滚动,
  // 否则旧页淡出快照会先跳到顶部, crossfade 视觉异常)
  const render = () => {
    // 以搜索栏所在顶栏为顶点: 顶栏上方还有插图横幅, 滚到顶栏位置即跳过大图直接看到内容
    // (横幅隐藏时 offsetTop 为 0, 自动退化为从最顶部开始; instant 跳过全局 smooth 滚动, 避免切页时长时间滚顶拖慢过渡)
    const _tb = document.querySelector(".topbar");
    window.scrollTo({ top: _tb ? _tb.offsetTop : 0, behavior: "instant" });
    if (p[0] === "") pageHome();
    else if (p[0] === "catalog") pageCatalog(q.genre || 0, q.page || 1, q.order || "time");
    else if (p[0] === "wiki") {
      if (p[1] === "browse") pageWikiBrowse(p[2] || "");
      else pageWiki();
    }
    else if (p[0] === "illus") pageIllus(q.r || 0);
    else if (p[0] === "reco") pageReco();
    else if (p[0] === "downloads") pageDownloads();
    else if (p[0] === "anime") pageAnimeDetail(p[1]);
    else if (p[0] === "watch") pageWatch(p[1], p[2], p[3], p[4] || "");
    else if (p[0] === "search") pageSearch(p[1] || "");
    else if (p[0] === "mine") pageMine();
    else pageHome();
    doScrollRestore();   // 渲染完尝试恢复位置
  };
  // 页面切换过渡: 统一使用轻量 CSS 进入动画 (单层 transform/opacity, GPU 合成, 开销恒定);
  // 不再使用 View Transition —— 整区快照+截图在大卡片列表上开销大, 与新卡片入场动画叠加导致卡顿
  render();
  // 苹果式方向感知过渡: 前进(点链接)新页从右滑入, 返回(浏览器后退)从左滑入
  const _rmReduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!_rmReduce) {
    const _cls = _navDir === "back" ? "page-enter-back" : "page-enter-forward";
    $app.classList.remove("page-enter-forward", "page-enter-back");
    void $app.offsetWidth;   // 强制 reflow 以重启动画
    $app.classList.add(_cls);
  }
}
window.addEventListener("hashchange", route);

/* ---------- 初音未来吉祥物互动 (Live2D 式): 眼神跟随 / 眨眼 / hover 眯眯眼 / 点击说话 ----------
   注: 内部标识 (alona* id/class/函数名) 沿用原名, 避免牵动 CSS 选择器与 HTML 结构 */
const ALONA_LINES = [
  "主人！今天想听什么歌？",
  "欢迎回到超越次元～",
  "新番列车已到站！",
  "本期必追清单已备好 ♪",
  "初音未来随时待命！",
  "要一起唱歌吗？谢谢…使命 ♪",
];
function alonaSay(msg) {
  const b = document.getElementById("alonaBubble");
  if (!b) return;
  b.textContent = msg;
  b.classList.add("show");
  clearTimeout(b._t);
  b._t = setTimeout(() => b.classList.remove("show"), 2600);
}
function alonaInit() {
  const el = document.getElementById("alona");
  if (!el || el._on) return;
  el._on = true;
  const reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const head = document.getElementById("alonaHead");
  const pupils = el.querySelectorAll(".alona-pupil");
  const eyes = el.querySelectorAll(".alona-eye");
  const haloWrap = document.getElementById("alonaHaloWrap");
  let tx = 0, ty = 0, cx = 0, cy = 0;
  if (!reduce) {
    document.addEventListener("mousemove", e => {
      const r = el.getBoundingClientRect();
      tx = Math.max(-1, Math.min(1, (e.clientX - (r.left + r.width / 2)) / 240));
      ty = Math.max(-1, Math.min(1, (e.clientY - (r.top + r.height / 2)) / 200));
    }, { passive: true });
    (function loop() {
      cx += (tx - cx) * 0.14; cy += (ty - cy) * 0.14;   // 阻尼追赶, Live2D 式平滑
      if (head) head.setAttribute("transform",
        `translate(${(cx * 2.2).toFixed(2)} ${(cy * 1.7).toFixed(2)}) rotate(${(cx * 5).toFixed(2)} 32 48)`);
      pupils.forEach(p => p.setAttribute("transform",
        `translate(${(cx * 1.6).toFixed(2)} ${(cy * 2.1).toFixed(2)})`));
      requestAnimationFrame(loop);
    })();
    (function blink() {
      setTimeout(() => {
        eyes.forEach(eye => eye.style.transform = "scaleY(.08)");
        setTimeout(() => { eyes.forEach(eye => eye.style.transform = ""); blink(); }, 130);
      }, 2600 + Math.random() * 3400);
    })();
  }
  el.addEventListener("click", e => {
    e.preventDefault(); e.stopPropagation();   // 吉祥物位于 logo 链接内, 点击吉祥物不触发首页跳转
    alonaSay(ALONA_LINES[Math.floor(Math.random() * ALONA_LINES.length)]);
    if (haloWrap && !reduce) {
      haloWrap.classList.remove("hop");
      void haloWrap.getBoundingClientRect();   // 强制 reflow 重启动画
      haloWrap.classList.add("hop");
    }
  });
}
alonaInit();

/* ---------- 搜索框 ---------- */
const $si = document.getElementById("searchInput");
document.getElementById("searchBtn").onclick = doSearch;
$si.addEventListener("keydown", e => { if (e.key === "Enter") doSearch(); });
function doSearch() {
  const kw = $si.value.trim();
  if (!kw) return toast("请输入关键词");
  location.hash = "#/search/" + encodeURIComponent(kw);
}

/* ---------- 启动进入动画 (splash 全屏覆盖, 至少展示 1.2s 后淡出) ---------- */
(function bootSplash() {
  const el = document.getElementById("bootSplash");
  if (!el) return;
  const t0 = Date.now();
  let done = false, resolved = false;
  // 首页数据就绪信号 (由 api() 在拿到 /anime/home 后触发)
  const dataP = new Promise(res => { window.__bootResolve = res; });
  window.__bootDataReady = () => { if (resolved) return; resolved = true; window.__bootResolve && window.__bootResolve(); };
  const hide = () => {
    if (done) return;
    done = true;
    const hold = Math.max(0, 1200 - (Date.now() - t0));   // 保证动画完整播完
    setTimeout(() => {
      el.classList.add("boot-out");
      setTimeout(() => el.remove(), 650);
    }, hold);
  };
  const finish = async () => {
    if (done) return;
    const onHome = !location.hash || location.hash === "#/";
    if (onHome) {
      // 首屏抓源期间继续播放进入动画, 数据到了才揭开 (最多多等 4.5s)
      await Promise.race([dataP, new Promise(r => setTimeout(r, 4500))]);
    }
    hide();
  };
  if (document.readyState === "complete") finish();
  else window.addEventListener("load", finish);
  setTimeout(finish, 3000);   // 首屏资源慢时兜底
})();

/* ---------- 启动 ---------- */
route();

/* ---------- 启动检查更新 (仅发现新版本时提示一次, 静默失败不打扰) ---------- */
setTimeout(() => {
  try { if (sessionStorage.getItem("cy_upd_hint")) return; } catch {}
  api("/update/check").then(d => {
    if (!d.has_update) return;
    try { sessionStorage.setItem("cy_upd_hint", "1"); } catch {}
    toast(`发现新版本 v${d.latest} · 请到「我的-关于」前往官方 GitHub 渠道下载`);
  }).catch(() => {});
}, 3000);

/* 启动静默检查更新: 发现新版本提示一次 (详情在「我的-关于」)
   15s 首查 + 60s 补查: 后台循环启动后立即检查, 但多源探测可能耗时十几秒,
   单次早查会错过窗口; upd_seen 保证每个版本只提示一次 */
function _updNotice() {
  api("/update/notice").then(d => {
    if (!d || !d.has_update || !d.latest) return;
    if (localStorage.getItem("upd_seen") === String(d.latest)) return;
    try { localStorage.setItem("upd_seen", String(d.latest)); } catch { }
    toast(`发现新版本 v${d.latest}，请到「我的-关于」前往官方 GitHub 渠道下载`);
  }).catch(() => { });
}
setTimeout(_updNotice, 15000);
setTimeout(_updNotice, 60000);
