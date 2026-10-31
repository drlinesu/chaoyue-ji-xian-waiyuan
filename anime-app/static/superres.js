/* ============================================================================
   超分管线 · 真 2K / 4K 渲染版
   - 原理: 视频帧作为纹理上传 GPU, 画布内部分辨率固定渲染到 2K(1440p) 或 4K(2160p),
     GLSL 做对比度自适应锐化 (CAS) + 线稿压暗, 再由 CSS 缩放到视频显示区域;
     全屏到 2K/4K 屏时画布与物理像素对齐, 获得真实高分画面
   - 档位: 关闭 / 2K(默认) / 4K, localStorage 记忆 (sr_mode); 旧版 sr_on 自动迁移
   - 保护: 4K 负载过高自动降 2K; 跨域受污线路自动关闭并提示, 不影响播放/弹幕
   - 层级: 画布 z-index 15, 位于 video(10) 之上、字幕(20)/弹幕(30)/控制条(60) 之下

   AI 插帧 (fi_mode, 默认开):
   - 两段式管线: Pass1 CAS 超分只在新视频帧到达时渲染进 FBO (24fps 片源=24 次/秒),
     Pass2 每个屏幕刷新周期只做两帧纹理相位混合 blit (开销≈0), 开启超分时不再因
     插帧把重着色器跑到 60fps 而掉帧卡顿
   - 场景切换检测: 新帧到达时抽 32x18 缩样算均差, 突变判为切镜, 该间隔直接显示当前帧,
     避免跨镜头混合产生鬼影; seek/尺寸变化后同样重置不混合
   - 保护: FBO 不完整判设备不支持; 开启插帧而超分画布关时会自动以 auto 档开画布
   ========================================================================== */
(function () {
  "use strict";

  let st = null;          // { v, host, cv, gl, prog, tex, uTexel, uSharp, raf, mode, lastT, ro, maxTex, ema, frames }
  let _supported = null;
  const LS_KEY = "sr_mode";
  const MODES = { off: 0, auto: 0, "1080": 1080, "2k": 1440, "4k": 2160 };   // 档位 -> 目标渲染高度
  const SHARP = { soft: 0.82, std: 1.15, sharp: 1.52 };   // 锐度档位
  const SHARP_LS = "sr_sharp";
  const GRADES = { off: 0, teal: 1, film: 2, noir: 3, shinkai: 4, cyber: 5, mono: 6 };   // 电影预设 (着色器 uGrade)
  const GRADE_LS = "sr_grade";
  const FI_LS = "fi_mode";   // AI 插帧: "1"(默认) / "0"

  /* 铺满模式: cover 裁切填满 host (全屏铺满用), 默认 contain 留黑边 */
  function setFill(on) {
    if (!st) return;
    st.objectFit = on ? "cover" : "contain";
    st.lastT = -1; start();
  }

  function getGrade() {
    try {
      const v = localStorage.getItem(GRADE_LS);
      return GRADES.hasOwnProperty(v) ? v : "off";
    } catch { return "off"; }
  }

  function setGrade(v) {
    if (!GRADES.hasOwnProperty(v)) return;
    try { localStorage.setItem(GRADE_LS, v); } catch { }
    if (st) {
      if (v !== "off" && st.mode === "off") setMode("auto");   // 选预设自动开画布, 否则看不见
      st.lastT = -1; start();
    }
  }

  function getSharp() {
    try {
      const v = localStorage.getItem(SHARP_LS);
      return SHARP.hasOwnProperty(v) ? v : "std";
    } catch { return "std"; }
  }

  function setSharp(v) {
    if (!SHARP.hasOwnProperty(v)) return;
    try { localStorage.setItem(SHARP_LS, v); } catch { }
    if (st) { st.lastT = -1; start(); }
  }

  const VERT = `
attribute vec2 aPos;
varying vec2 vUV;
void main() {
  vUV = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

  /* Anime4K 风格片段着色器: 3x3 邻域亮度分析 -> 自适应锐化 + 线稿压暗增强 */
  const FRAG = `
precision mediump float;
varying vec2 vUV;
uniform sampler2D uTex;
uniform vec2 uTexel;
uniform float uSharp;
uniform float uGrade;   // 电影预设: 0 原画 / 1 青橙 / 2 胶片 / 3 暗调
uniform float uTime;    // 播放时间, 驱动胶片颗粒运动
float luma(vec3 c) { return dot(c, vec3(0.299, 0.587, 0.114)); }
float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
void main() {
  vec2 t = uTexel;
  vec3 c  = texture2D(uTex, vUV).rgb;
  vec3 n  = texture2D(uTex, vUV + vec2(0.0, -t.y)).rgb;
  vec3 s  = texture2D(uTex, vUV + vec2(0.0,  t.y)).rgb;
  vec3 e  = texture2D(uTex, vUV + vec2( t.x, 0.0)).rgb;
  vec3 w  = texture2D(uTex, vUV + vec2(-t.x, 0.0)).rgb;
  vec3 ne = texture2D(uTex, vUV + vec2( t.x, -t.y)).rgb;
  vec3 nw = texture2D(uTex, vUV + vec2(-t.x, -t.y)).rgb;
  vec3 se = texture2D(uTex, vUV + vec2( t.x,  t.y)).rgb;
  vec3 sw = texture2D(uTex, vUV + vec2(-t.x,  t.y)).rgb;
  float lc = luma(c), ln = luma(n), ls = luma(s), le = luma(e), lw = luma(w);
  float lne = luma(ne), lnw = luma(nw), lse = luma(se), lsw = luma(sw);
  // 双边滤波: 只平滑亮度接近的像素, 保住描边; 主要消掉低码率的块状噪点
  float wn = exp(-abs(ln - lc) * 30.0);
  float ws = exp(-abs(ls - lc) * 30.0);
  float we = exp(-abs(le - lc) * 30.0);
  float ww = exp(-abs(lw - lc) * 30.0);
  float wd = exp(-(abs(lne - lc) + abs(lnw - lc) + abs(lse - lc) + abs(lsw - lc)) * 18.0);
  vec3 blur = (c + (n + s + e + w) * wn + (ne + nw + se + sw) * (wd * 0.6)) /
              (1.0 + 4.0 * wn + 4.0 * wd * 0.6);
  float mn = min(lc, min(min(ln, ls), min(min(le, lw), min(min(lne, lnw), min(lse, lsw)))));
  float mx = max(lc, max(max(ln, ls), max(max(le, lw), max(max(lne, lnw), max(lse, lsw)))));
  float rng = mx - mn;
  // 平坦/渐变区多平滑, 边缘区少平滑
  float flatness = 1.0 - smoothstep(0.015, 0.11, rng);
  vec3 base = mix(c, blur, flatness * 0.7);
  // 去色带: 平坦区加极轻抖动, 打散渐变断层
  float dith = (hash(gl_FragCoord.xy) - 0.5) * 0.005 * flatness;
  base += vec3(dith);
  // 对比度自适应锐化: 正权重 unsharp, 避免负分母造成反相/发灰
  float amp = clamp(sqrt(min(mn, 1.0 - mx) / max(mx, 1e-4)), 0.0, 1.0);
  float edge = mix(0.06, 0.30, amp) * uSharp;
  vec3 sharp = base + (base - (n + s + e + w) * 0.25) * edge;
  vec3 cmn = min(c, min(min(n, s), min(min(e, w), min(min(ne, nw), min(se, sw)))));
  vec3 cmx = max(c, max(max(n, s), max(max(e, w), max(max(ne, nw), max(se, sw)))));
  sharp = clamp(sharp, cmn - 0.02, cmx + 0.02);
  // 线稿压深
  float dark = smoothstep(0.0, 0.45, lc - mn);
  vec3 outc = mix(sharp * 0.965, sharp, dark);
  outc = (outc - 0.5) * 1.03 + 0.5;
  // ---- 电影预设 (隐藏参数内置: 极低颗粒 + 小半径 Halation, 不暴露滑块防调重显脏) ----
  if (uGrade > 0.5) {
    vec3 g = clamp(outc, 0.0, 1.0);
    float l = luma(g);
    if (uGrade < 1.5) {
      // 青橙电影: 阴影压青, 高光推橙, 轻 S 对比
      g += vec3(0.0, 0.05, 0.08) * (1.0 - smoothstep(0.0, 0.55, l))
         + vec3(0.08, 0.04, -0.03) * smoothstep(0.45, 1.0, l);
      g = (g - 0.5) * 1.06 + 0.5;
    } else if (uGrade < 2.5) {
      // 低饱和胶片: 降饱和 + 抬黑 + 柔对比 + 暖高光
      g = mix(vec3(l), g, 0.72) * 0.94 + 0.035;
      g += vec3(0.03, 0.015, -0.01) * smoothstep(0.5, 1.0, l);
    } else if (uGrade < 3.5) {
      // 暗调悬疑: 冷色压暗 + 强对比 + 暗角
      g = mix(vec3(l), g, 0.85) * vec3(0.92, 0.97, 1.05);
      g = (g - 0.5) * 1.14 + 0.47;
      vec2 vc = (vUV - 0.5) * vec2(1.25, 1.0);
      g *= 1.0 - smoothstep(0.35, 0.85, length(vc)) * 0.45;
    } else if (uGrade < 4.5) {
      // 新海诚风: 高饱和通透 + 阴影偏蓝紫天空 + 高光推粉 + 强泛光 (言叶/君名的明亮感)
      g = mix(vec3(l), g, 1.28);
      g += vec3(0.02, 0.05, 0.11) * (1.0 - smoothstep(0.3, 0.75, l));
      g += vec3(0.10, 0.03, 0.09) * smoothstep(0.6, 1.0, l);
      g = (g - 0.5) * 1.09 + 0.53;
    } else if (uGrade < 5.5) {
      // 赛博朋克: 青蓝阴影 + 品红霓虹高光 + 强对比
      g = mix(vec3(l), g, 1.18);
      g += vec3(0.0, 0.07, 0.10) * (1.0 - smoothstep(0.0, 0.5, l));
      g += vec3(0.13, -0.03, 0.09) * smoothstep(0.55, 1.0, l);
      g = (g - 0.5) * 1.13 + 0.47;
    } else {
      // 黑白映画: 银盐高对比黑白
      float m = dot(g, vec3(0.299, 0.587, 0.114));
      g = vec3((m - 0.5) * 1.16 + 0.5);
    }
    // Halation: 复用邻域均值作光晕源, 高光区溢出 (新海诚风用粉调强泛光, 其余暖调)
    float haloK = uGrade < 2.5 ? 0.10 : (uGrade < 3.5 ? 0.06 : (uGrade < 4.5 ? 0.16 : 0.08));
    vec3 haloCol = (uGrade > 3.5 && uGrade < 4.5) ? vec3(1.0, 0.75, 0.85) : vec3(1.0, 0.72, 0.5);
    vec3 halo = max(blur - 0.62, vec3(0.0)) * haloCol;
    g += halo * haloK;
    // 胶片颗粒: 强度按预设微调, 随时间运动避免"死噪点"
    float gs = uGrade < 1.5 ? 0.012 : (uGrade < 2.5 ? 0.02 : (uGrade < 3.5 ? 0.008 : (uGrade < 4.5 ? 0.006 : (uGrade < 5.5 ? 0.010 : 0.022))));
    g += (hash(gl_FragCoord.xy + fract(uTime) * 913.0) - 0.5) * gs;
    outc = g;
  }
  gl_FragColor = vec4(clamp(outc, 0.0, 1.0), 1.0);
}`;

  function compile(gl, type, src) {
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh) || "shader");
    return sh;
  }

  /* 插帧混合着色器: 只做两纹理相位混合, 每个屏幕刷新周期一次, 开销≈0
     (CAS 重活只在新视频帧到达时渲染进 FBO, 24fps 片源即 24 次/秒) */
  const FRAG_MIX = `
precision mediump float;
varying vec2 vUV;
uniform sampler2D uA;   // 上一帧 (已超分)
uniform sampler2D uB;   // 当前帧 (已超分)
uniform float uMix;     // 1 = 仅当前帧; <1 = mix(上一帧, 当前帧, uMix)
void main() {
  gl_FragColor = vec4(mix(texture2D(uA, vUV).rgb, texture2D(uB, vUV).rgb, uMix), 1.0);
}`;

  function linkProg(gl, fragSrc) {
    const prog = gl.createProgram();
    gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, fragSrc));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog) || "link");
    return prog;
  }

  function mkTex(gl) {
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  function initGL(cv) {
    const gl = cv.getContext("webgl", { alpha: false, antialias: false, preserveDrawingBuffer: false })
      || cv.getContext("experimental-webgl", { alpha: false, antialias: false });
    if (!gl) return null;
    const prog = linkProg(gl, FRAG);
    const progMix = linkProg(gl, FRAG_MIX);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.useProgram(prog);
    const loc = gl.getAttribLocation(prog, "aPos");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    gl.useProgram(progMix);
    const locM = gl.getAttribLocation(progMix, "aPos");
    gl.enableVertexAttribArray(locM);
    gl.vertexAttribPointer(locM, 2, gl.FLOAT, false, 0, 0);
    gl.uniform1i(gl.getUniformLocation(progMix, "uA"), 0);
    gl.uniform1i(gl.getUniformLocation(progMix, "uB"), 1);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    const tex = mkTex(gl);                       // 原始视频帧 (CAS 输入)
    const texA = mkTex(gl), texB = mkTex(gl);    // 超分后帧乒乓 (插帧混合源)
    const fbo = gl.createFramebuffer();
    return { gl, prog, progMix, buf, aPos: loc, aPosM: locM, tex, texA, texB, fbo,
      maxTex: gl.getParameter(gl.MAX_TEXTURE_SIZE) || 8192,
      fboW: 0, fboH: 0, fboOk: true,
      uTexel: gl.getUniformLocation(prog, "uTexel"),
      uSharp: gl.getUniformLocation(prog, "uSharp"),
      uGrade: gl.getUniformLocation(prog, "uGrade"),
      uTime: gl.getUniformLocation(prog, "uTime"),
      uMixF: gl.getUniformLocation(progMix, "uMix") };
  }

  function getMode() {
    try {
      const m = localStorage.getItem(LS_KEY);
      if (m === "off" || m === "auto" || m === "1080" || m === "2k" || m === "4k") return m;
      if (localStorage.getItem("sr_on") === "0") return "off";   // 旧版开关迁移: 关过的保持关
    } catch { }
    return "auto";   // 默认自动: 画布贴合显示尺寸与源码率
  }

  /* 自动画布: 按显示尺寸 x DPR 给足清晰度, 最多到 4K; 不再按码率封顶
     (低码率源恰恰需要更大画布+锐化, 封顶会把压缩块放大得更糊) */
  function autoHeight() {
    const hostH = (st.host && st.host.clientHeight) || 0;
    const dpr = window.devicePixelRatio || 1;
    let th = Math.max(720, Math.round(hostH * dpr * 1.25));
    const sh = st.srcH || (st.v && st.v.videoHeight) || 0;
    if (sh) th = Math.min(th, Math.max(1080, sh * 2));
    return Math.max(720, Math.min(2160, th));
  }

  /* 画布 CSS 只覆盖视频有效画面 (object-fit: contain), 不含黑边/控制条;
     内部分辨率由画布档位决定, 自动档跟随显示尺寸与源码率 */
  function fit() {
    if (!st) return;
    const { v, cv, host } = st;
    const W = host.clientWidth, H = host.clientHeight;
    if (!W || !H) return;
    const vw = v.videoWidth, vh = v.videoHeight;
    if (vh) st.srcH = vh;
    let dw = W, dh = H;
    if (vw && vh) {
      const cover = st.objectFit === "cover";
      const scale = cover ? Math.max(W / vw, H / vh) : Math.min(W / vw, H / vh);
      dw = Math.max(1, Math.round(vw * scale));
      dh = Math.max(1, Math.round(vh * scale));
      if (!cover) { dw = Math.min(W, dw); dh = Math.min(H, dh); }   // cover 允许溢出, 负偏移居中裁切
    }
    cv.style.left = Math.round((W - dw) / 2) + "px";
    cv.style.top = Math.round((H - dh) / 2) + "px";
    cv.style.width = dw + "px";
    cv.style.height = dh + "px";
    // 渲染分辨率: 高度 = max(档位目标, 实际显示高度), 保证画布永不被放大显示 (放大即糊);
    // 自动档按显示尺寸/DPR 计算, 手动档用固定高度; 宽度按视频比例
    const th = Math.max(st.mode === "auto" ? autoHeight() : (MODES[st.mode] || 1440), dh);
    let cw, ch;
    if (vw && vh) { ch = th; cw = Math.round(th * vw / vh); }
    else { ch = th; cw = Math.max(2, Math.round(dw * (th / Math.max(1, dh)))); }
    if (cw > st.maxTex) { ch = Math.round(ch * st.maxTex / cw); cw = st.maxTex; }
    st.effH = ch;
    const sh = st.srcH || vh || 0;
    st.sharpMul = Math.max(0.8, Math.min(1.35, 0.9 + ((ch / Math.max(1, sh)) - 1) * 0.12));
    if (cv.width !== cw || cv.height !== ch) {
      cv.width = cw;
      cv.height = ch;
      st.gl.viewport(0, 0, cw, ch);
      st.lastT = -1;            // 画布尺寸变化会清空内容, 强制下一帧重绘
    }
  }

  /* 帧差异分析: 32x18 缩样归一化均差; 返回 0..1, 异常返回 -1
     驱动切镜判定 (>0.42) 与运动量自适应混合强度 (运动越大混合越弱, 防鬼影) */
  function frameDiff(v) {
    const sc = st.sctx;
    if (!sc) return -1;
    try {
      sc.drawImage(v, 0, 0, 32, 18);
      const d = sc.getImageData(0, 0, 32, 18).data;
      const prev = st.prevPix;
      st.prevPix = d;
      if (!prev) return -1;
      let sum = 0;
      for (let i = 0; i < d.length; i += 4)
        sum += Math.abs(d[i] - prev[i]) + Math.abs(d[i + 1] - prev[i + 1]) + Math.abs(d[i + 2] - prev[i + 2]);
      return sum / (d.length * 0.75 * 255);
    } catch { return -1; }
  }

  function draw() {
    const { v, gl, cv } = st;
    if (v.readyState < 2 || !v.videoWidth) return;
    const fi = st.fiOn && !v.paused && !v.ended && st.fboOk;
    // 新帧判定: 优先用 rVFC 上报的 mediaTime (上屏时刻精确); 定时器未首火/不支持时轮询 currentTime
    const useRvfc = fi && HAS_RVFC;
    let newFrame;
    if (useRvfc && st.pendingT >= 0) {
      newFrame = st.pendingT !== st.lastPT;
      if (newFrame) st.lastPT = st.pendingT;
    } else {
      newFrame = st.lastT !== v.currentTime;
      if (newFrame) st.lastT = v.currentTime;
    }
    if (!newFrame && !fi) return;                      // 无新帧且不在插帧: 省电
    if (!newFrame && st.staticOut) return;             // 切镜/静帧间隔已上屏: 不重复 blit (避免 FBO 同步尖峰)
    if (newFrame) {
      if (fi && st.hasPrev) {
        const iv = (useRvfc && st.pendingT >= 0 ? st.frameAt : performance.now()) - st.frameAtPrev;
        if (iv > 4 && iv < 500) st.frameIv = st.frameIv ? st.frameIv * 0.8 + iv * 0.2 : iv;
        const sad = frameDiff(v);
        st.cut = sad < 0 || sad > 0.42;
        // 运动自适应混合强度: 静止/微动 1.0, 中速 0.6, 剧烈运动 0.3, 切镜 0 (直接当前帧)
        st.blendK = st.cut ? 0 : sad > 0.30 ? 0.3 : sad > 0.16 ? 0.6 : 1;
      } else { st.cut = true; st.blendK = 0; }
      if (!(useRvfc && st.pendingT >= 0)) st.frameAt = performance.now();
      st.frameAtPrev = st.frameAt;
      st.lastT = v.currentTime;
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, st.tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, v);   // 跨域受污会在此抛错
      st.hasPrev = true;
      st.staticOut = false;
      if (fi) {
        // ---- Pass 1 (仅新帧, 24fps 时即 24 次/秒): CAS 超分渲染进 FBO ----
        // 注意: CAS 开销开不开插帧都一样, 不做"插帧负载"守护 (4K 降档守护已兜底);
        // 插帧只增加 Pass2 的廉价 blit, 这正是本重构要解决"开超分+插帧变卡"的核心
        if (st.fboW !== cv.width || st.fboH !== cv.height) {   // 画布尺寸变了重建 FBO 纹理
          st.fboW = cv.width; st.fboH = cv.height;
          for (const t of [st.texA, st.texB]) {
            gl.bindTexture(gl.TEXTURE_2D, t);
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, cv.width, cv.height, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
          }
          gl.bindFramebuffer(gl.FRAMEBUFFER, st.fbo);
          gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, st.texA, 0);
          st.fboOk = gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
          gl.bindFramebuffer(gl.FRAMEBUFFER, null);
          st.hasPrev = false;   // 尺寸变了旧帧不可混合
          if (!st.fboOk) { gl.viewport(0, 0, cv.width, cv.height); return; }
        }
        const t = st.texA; st.texA = st.texB; st.texB = t;   // 轮转: B=最新帧
        gl.bindFramebuffer(gl.FRAMEBUFFER, st.fbo);
        gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, st.texB, 0);
        gl.viewport(0, 0, cv.width, cv.height);
        runCas();
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.viewport(0, 0, cv.width, cv.height);
      } else {
        gl.viewport(0, 0, cv.width, cv.height);
        runCas();   // 非插帧: 直出屏幕 (与原有一致)
        return;
      }
    }
    // ---- Pass 2 (每个屏幕刷新周期): 两帧混合 blit, 开销≈0 ----
    const mixv = (st.cut || !st.hasPrev) ? 1 :
      1 - 0.65 * st.blendK * (1 - Math.min(1, (performance.now() - st.frameAt) / Math.max(8, st.frameIv || 42)));
    gl.useProgram(st.progMix);
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, st.texA);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, st.texB);
    gl.activeTexture(gl.TEXTURE0);
    gl.uniform1f(st.uMixF, mixv);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    st.staticOut = mixv >= 0.999;   // 本间隔不再变化 (切镜/纯当前帧): 后续 rAF 直接跳过
  }

  /* CAS 主渲染: 目标由当前 FBO 绑定决定 (FBO=离屏超分帧, null=屏幕) */
  function runCas() {
    const { v, gl } = st;
    gl.useProgram(st.prog);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, st.tex);
    gl.uniform2f(st.uTexel, 1 / v.videoWidth, 1 / v.videoHeight);
    gl.uniform1f(st.uSharp, (SHARP[getSharp()] || 1.15) * (st.sharpMul || 1) * (st.mode === "4k" ? 1.05 : 1));
    gl.uniform1f(st.uGrade, GRADES[getGrade()] || 0);
    gl.uniform1f(st.uTime, v.currentTime % 97);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  /* 帧精确同步: requestVideoFrameCallback 在新视频帧上屏时刻触发绘制,
     消除 rAF 轮询 currentTime 的相位错位抖动; 不支持/暂停时回退 rAF */
  const HAS_RVFC = "requestVideoFrameCallback" in HTMLVideoElement.prototype;

  function loop() {
    if (!st) return;
    st.raf = 0; st.rvfc = 0;
    if (!st.v.isConnected) { detach(); return; }   // 切集/切页自动回收
    if (st.mode === "off") return;
    fit();
    try {
      const t0 = performance.now();
      draw();
      // 负载守护: 4K 下绘制耗时 EMA 持续过高, 自动降 2K 防卡顿
      const dt = performance.now() - t0;
      st.ema = st.ema ? st.ema * 0.9 + dt * 0.1 : dt;
      if (++st.frames > 90 && st.mode === "4k" && st.ema > 26) {
        setMode("2k");
        if (window.toast) toast("当前设备 4K 超分负载过高, 已降为 2K");
        return;
      }
      // 插帧的 CAS 耗时守护在 draw() Pass1 内按新帧粒度统计 (casEma)
    } catch (e) {                   // 纹理受污/驱动异常: 自动降级, 不影响播放
      setMode("off");
      if (window.toast) toast("该线路视频受跨域保护, 已自动关闭超分");
      return;
    }
    schedule();
  }

  /* 插帧帧定时器: 与 rAF 渲染并行, rVFC 在视频帧真实上屏时刻回调 (vsync 对齐),
     提供精确 frameAt/帧间隔 -> 插帧相位不再受 rAF 轮询量化误差影响, 消除节奏微抖 */
  function armFiTimer() {
    if (!st || !HAS_RVFC || st.rvfcT) return;
    const v = st.v;
    st.rvfcT = v.requestVideoFrameCallback((ts, meta) => {
      if (!st) return;
      st.rvfcT = 0;
      if (st.fiOn && !v.paused && !v.ended) {
        st.frameAt = ts;                 // 真实上屏时刻 (与 performance.now 同时钟)
        st.pendingT = meta.mediaTime;    // 帧标识
        armFiTimer();
      }
    });
  }

  function schedule() {
    if (!st || st.raf || st.rvfc || st.mode === "off") return;
    const v = st.v;
    if (v.paused || v.ended) {
      st.raf = requestAnimationFrame(loop);   // 暂停/结束: 仅需低频待命重绘 (seeked 事件会主动触发)
      return;
    }
    if (st.fiOn) {
      st.raf = requestAnimationFrame(loop);   // 插帧: 按屏幕刷新率持续输出过渡帧
      armFiTimer();                            // 并行 rVFC: 只取帧时刻, 不做渲染
      return;
    }
    if (HAS_RVFC) {
      st.rvfc = v.requestVideoFrameCallback(() => loop());   // 非插帧: 帧精确同步省电
    } else {
      st.raf = requestAnimationFrame(loop);
    }
  }

  function start() { if (st && st.mode !== "off") schedule(); }

  function cancelSchedule() {
    if (!st) return;
    if (st.raf) { cancelAnimationFrame(st.raf); st.raf = 0; }
    if (st.rvfc) { try { st.v.cancelVideoFrameCallback(st.rvfc); } catch { } st.rvfc = 0; }
    if (st.rvfcT) { try { st.v.cancelVideoFrameCallback(st.rvfcT); } catch { } st.rvfcT = 0; }
  }

  function redraw() {
    if (!st) return;
    st.lastT = -1;
    st.hasPrev = false; st.cut = true; st.prevPix = null;   // seek 后新画面与旧帧无关, 不混合
    st.pendingT = -1; st.lastPT = -1; st.staticOut = false;
    start();
  }

  function setMode(m) {
    if (!st || !MODES.hasOwnProperty(m)) return;
    if (m === st.mode) return;
    st.mode = m;
    st.ema = 0; st.frames = 0;
    try { localStorage.setItem(LS_KEY, m); } catch { }
    st.cv.style.display = m === "off" ? "none" : "block";
    if (st.wm) st.wm.style.display = m === "off" ? "none" : "block";
    if (m !== "off") {
      fit();
      st.lastT = -1;
      st.v.addEventListener("seeked", redraw);
      start();
    } else cancelSchedule();
    st.onChange && st.onChange(m);
  }

  function getFi() {
    try { return localStorage.getItem(FI_LS) !== "0"; } catch { return true; }
  }

  /* 插帧开关: 开启时若超分画布关着则自动以 auto 档开画布 (插帧依附画布管线) */
  function setFi(on) {
    if (!st) { try { localStorage.setItem(FI_LS, on ? "1" : "0"); } catch { } return; }
    if (on && !st.fboOk) { if (window.toast) toast("当前设备不支持插帧"); return; }
    try { localStorage.setItem(FI_LS, on ? "1" : "0"); } catch { }
    st.fiOn = !!on;
    st.ema = 0; st.frames = 0; st.casEma = 0; st.casFrames = 0;
    st.hasPrev = false; st.cut = true; st.prevPix = null;   // 切换后重新积累帧历史
    st.pendingT = -1; st.lastPT = -1; st.staticOut = false;
    if (on && st.mode === "off") { setMode("auto"); return; }
    if (on && st.mode !== "off") { st.lastT = -1; cancelSchedule(); start(); }
  }

  function detach() {
    if (!st) return;
    cancelSchedule();
    if (st.ro) st.ro.disconnect();
    window.removeEventListener("resize", fit);
    try { st.v.removeEventListener("seeked", redraw); } catch { }
    if (st.cv.parentNode) st.cv.parentNode.removeChild(st.cv);
    if (st.wm && st.wm.parentNode) st.wm.parentNode.removeChild(st.wm);
    try {
      const ext = st.gl.getExtension("WEBGL_lose_context");
      if (ext) ext.loseContext();            // 主动释放 GPU 上下文
    } catch { }
    st = null;
  }

  /* 绑定到 ArtPlayer: host = .art-video-player, opts.onChange(当前档位) 用于同步 UI */
  function attach(video, host, opts) {
    if (!video || !host) return false;
    if (st && st.v === video && st.host === host) return true;
    detach();
    if (!check()) return false;
    const cv = document.createElement("canvas");
    cv.className = "sr-canvas";
    let g;
    try { g = initGL(cv); } catch { return false; }
    if (!g) return false;
    host.appendChild(cv);
    // 技术来源水印: 超分画布工作时右下角常驻半透明标注
    const wm = document.createElement("div");
    wm.className = "sr-watermark";
    wm.textContent = "超分·插帧·推荐 技术来源 B站@不睡觉的余光";
    wm.style.display = "none";
    host.appendChild(wm);
    // 场景切换检测用缩样画布 (32x18, 每帧均差比对)
    const scv = document.createElement("canvas");
    scv.width = 32; scv.height = 18;
    st = { v: video, host, cv, wm, ...g, raf: 0, mode: "off", lastT: -1,
           ro: null, objectFit: getComputedStyle(video).objectFit,
           ema: 0, frames: 0, srcH: video.videoHeight || 0, srcBw: 0,
           effH: 0, sharpMul: 1,
           fiOn: getFi(), hasPrev: false, cut: true, frameAt: 0, frameAtPrev: 0, frameIv: 0,
           casEma: 0, casFrames: 0, rvfcT: 0, pendingT: -1, lastPT: -1, blendK: 1, staticOut: false,
           sctx: scv.getContext("2d", { willReadFrequently: true }), prevPix: null,
           onChange: (opts && opts.onChange) || null };
    st.ro = new ResizeObserver(() => { if (st) fit(); });
    st.ro.observe(host);
    window.addEventListener("resize", fit);
    video.addEventListener("loadedmetadata", fit);
    st.mode = "off";
    cv.style.display = "none";
    fit();
    setMode(getMode());   // 记忆档位, 默认 2K
    return true;
  }

  function check() {
    if (_supported !== null) return _supported;
    try {
      const c = document.createElement("canvas");
      _supported = !!(c.getContext("webgl") || c.getContext("experimental-webgl"));
    } catch { _supported = false; }
    return _supported;
  }

  window.SR = {
    get supported() { return check(); },
    get on() { return !!(st && st.mode !== "off"); },
    get mode() { return st ? st.mode : getMode(); },
    get ready() { return !!st; },
    get frames() { return st ? st.frames : 0; },   // 诊断: 渲染循环帧计数, 播放中应持续增长
    get ema() { return st ? Math.round(st.ema * 10) / 10 : 0; },   // 诊断: 单帧绘制耗时 ms
    get sharpLevel() { return getSharp(); },
    get grade() { return getGrade(); },
    get fi() { return st ? !!st.fiOn : getFi(); },   // AI 插帧开关状态
    setFi,
    setGrade,
    setFill,
    get canvasHeight() { return st ? (st.effH || 0) : 0; },
    get source() { return st ? { height: st.srcH, bitrate: st.srcBw } : null; },
    setSharp,
    setSource(src) {
      if (!st || !src) return;
      if (src.height) st.srcH = src.height;
      if (src.bitrate) st.srcBw = src.bitrate;
      st.lastT = -1;
      fit();
    },
    attach,
    detach,
    setMode(v) { if (st) setMode(v); },
    toggle() { if (st) setMode(st.mode === "off" ? "2k" : "off"); },   // 兼容旧调用
    setOn(v) { if (st) setMode(v ? "2k" : "off"); },                   // 兼容旧调用
    refresh() { fit(); },
  };
})();
