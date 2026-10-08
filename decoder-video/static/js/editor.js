/* Decoder editor: chart config, transcript + cue tagging, synced preview. */
(function () {
  "use strict";
  const $ = s => document.querySelector(s);
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v === true) n.setAttribute(k, "");
      else if (v !== false && v != null) n.setAttribute(k, v);
    }
    for (const k of kids.flat()) if (k != null) n.append(k.nodeType ? k : document.createTextNode(k));
    return n;
  };
  const fmtT = t => {
    if (t == null) return "–";
    const m = Math.floor(t / 60), s = t - m * 60;
    return `${m}:${s.toFixed(1).padStart(4, "0")}`;
  };

  const PID = +document.querySelector("main.editor").dataset.pid;
  let P = null;                 // project payload from the API
  let selWord = null;           // selected word index
  let editingCue = null;        // cue id being edited in the cue editor
  let reattachCue = null;       // orphaned cue waiting for a word click
  let chartReady = false;
  let virtualT = 0, virtualPlaying = false, lastFrame = null;

  const video = $("#video"), frame = $("#chart-frame");
  video.addEventListener("error", () => {
    const n = $("#talent-empty");
    n.textContent = "This browser can't play the preview video (H.264). Use Chrome or Safari.";
    n.style.fontSize = "36px"; n.style.padding = "40px"; n.style.textAlign = "center";
    n.classList.remove("hidden");
  });

  async function api(method, url, body) {
    const opts = { method, headers: {} };
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
    const r = await fetch(url, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }

  async function load() {
    P = await api("GET", `/api/projects/${PID}`);
    $("#template-label").textContent = P.manifest.label;
    layoutStage();
    renderConfigForm();
    renderVideoInfo();
    renderTranscript();
    renderCueList();
    renderCaptionStyle();
    for (const kind of ["video", "transcribe", "render"]) {
      const j = P.jobs[kind];
      if (j) showJob(kind, j);
      if (j && (j.status === "running" || j.status === "queued")) pollJob(kind, j.id);
    }
    loadRenders();
  }

  // ------------------------------------------------------------ preview stage
  function layoutStage() {
    const th = P.theme, L = th.layout, phone = $("#phone");
    const scale = phone.clientWidth / th.canvas.width;
    $("#stage").style.transform = `scale(${scale})`;
    $("#stage").style.background = th.canvas.background;
    Object.assign(frame.style, { left: L.chart.x + "px", top: L.chart.y + "px",
      width: L.chart.width + "px", height: L.chart.height + "px" });
    Object.assign($("#talent").style, { left: L.talent.x + "px", top: L.talent.y + "px",
      width: L.talent.width + "px", height: L.talent.height + "px" });
    const lay = P.project.layout || {};
    const zoom = +(lay.talent_zoom ?? 1), center = +(lay.talent_crop_center ?? L.talent.default_crop_center);
    $("#crop-zoom").value = zoom; $("#crop-center").value = center;
    $("#layout-mode").value = lay.mode || "stacked";
    // Mirror render.py _talent_filter: scale the 1080x1920 master, crop the talent window.
    const sw = th.canvas.width * zoom, sh = th.canvas.height * zoom;
    const x = Math.max(0, (sw - L.talent.width) / 2);
    const y = Math.min(Math.max(0, center * sh - L.talent.height / 2), sh - L.talent.height);
    Object.assign(video.style, { width: sw + "px", height: sh + "px", left: -x + "px", top: -y + "px" });
    if (P.project.video) {
      const src = `/p/${PID}/files/${P.project.video.master}?v=${P.project.updated_at}`;
      if (!video.src.endsWith(src)) video.src = src;
      $("#talent-empty").classList.add("hidden");
    }
  }

  function renderCaptionStyle() {
    const c = P.theme.captions, cap = $("#captions");
    const seam = c.position === "seam";
    Object.assign(cap.style, {
      bottom: seam ? "auto" : c.margin_bottom + "px",
      top: seam ? P.theme.layout.talent.y + (c.seam_offset ?? 24) + "px" : "auto",
      padding: `0 ${c.margin_side}px`,
      fontFamily: c.font_css, fontSize: c.size + "px", fontWeight: c.bold ? 700 : 400,
      color: c.color, textTransform: c.uppercase ? "uppercase" : "none", lineHeight: 1.15,
      // libass outline approximated with a stroke drawn under the fill
      webkitTextStroke: `${c.outline * 2}px ${c.outline_color}`, paintOrder: "stroke fill",
    });
    cap.style.setProperty("--cap-active", c.highlight_active_word ? c.active_color : c.color);
  }

  async function reloadChart() {
    const spec = await api("GET", `/api/projects/${PID}/spec`);
    const w = frame.contentWindow;
    if (w && w.__load) { w.__load(spec); w.__seek(currentTime()); }
  }

  frame.addEventListener("load", () => {
    const w = frame.contentWindow;
    const wait = () => (w.__ready ? (chartReady = true) : setTimeout(wait, 50));
    wait();
  });

  function duration() {
    if (P?.project.video && video.duration) return video.duration;
    const last = Math.max(0, ...(P?.cues || []).map(c => c.t || 0));
    return Math.max(20, last + 5);
  }
  function currentTime() { return P?.project.video ? video.currentTime : virtualT; }
  function seek(t) {
    t = Math.max(0, Math.min(duration(), t));
    if (P.project.video) video.currentTime = t; else virtualT = t;
  }
  function playing() { return P?.project.video ? !video.paused : virtualPlaying; }
  function togglePlay() {
    if (P.project.video) {
      if (video.paused) video.play().catch(e => console.warn("play() failed", e));
      else video.pause();
    }
    else virtualPlaying = !virtualPlaying;
  }

  let lastWordNow = -1, lastCapKey = "";
  function tick(ts) {
    if (virtualPlaying && lastFrame != null) {
      virtualT += (ts - lastFrame) / 1000;
      if (virtualT >= duration()) { virtualT = duration(); virtualPlaying = false; }
    }
    lastFrame = ts;
    if (P) {
      const t = currentTime();
      if (chartReady) frame.contentWindow.__seek(t);
      $("#clock").textContent = fmtT(t);
      const scrub = $("#scrub");
      scrub.max = duration();
      if (document.activeElement !== scrub) scrub.value = t;
      $("#play").textContent = playing() ? "Pause" : "Play";
      updateCaptions(t);
      updateNowWord(t);
    }
    requestAnimationFrame(tick);
  }

  function updateCaptions(t) {
    const g = P.captions.find(g => t >= g.start && t < g.end);
    let active = -1;
    if (g) g.words.forEach((w, i) => { if (w.start <= t) active = i; });
    const key = g ? g.start + ":" + active : "";
    if (key === lastCapKey) return;
    lastCapKey = key;
    const cap = $("#captions");
    cap.textContent = "";
    if (g) g.words.forEach((w, i) => {
      if (i) cap.append(" ");
      cap.append(el("span", { class: i === active ? "active" : "" }, w.text));
    });
  }

  function updateNowWord(t) {
    const words = P.transcript?.words || [];
    let now = -1;
    for (let i = 0; i < words.length; i++) { if (words[i].start <= t) now = i; else break; }
    if (now === lastWordNow) return;
    document.querySelector(".w.now")?.classList.remove("now");
    const n = document.querySelector(`.w[data-i="${now}"]`);
    if (n) {
      n.classList.add("now");
      if (playing()) n.scrollIntoView({ block: "nearest" });
    }
    lastWordNow = now;
  }

  // ------------------------------------------------------------ chart config
  function renderConfigForm() {
    const cfg = P.project.config, data = P.project.data, form = $("#config-form");
    form.textContent = "";
    const cols = Object.keys(data.columns), xs = data.x.values;
    const text = (name, label, ph = "") =>
      el("label", {}, label, el("input", { name, value: cfg[name] ?? "", placeholder: ph }));
    form.append(
      text("title", "Title"),
      text("subtitle", "Subtitle (optional)"),
      text("source", "Source line"),
      el("div", { class: "row" }, text("y_label", "Y axis label"), text("x_label", "X axis label (optional)")),
      el("div", { class: "row" },
        text("y_format", "Y tick format (d3-format)", ",~s"),
        text("value_format", "Value label format", ",.0f"),
        text("y_min", "Y min (blank = auto)"), text("y_max", "Y max (blank = auto)")),
      el("label", { class: "radio" }, el("input", { type: "checkbox", name: "y_zero", checked: cfg.y_zero !== false }),
        "Always include zero on the y axis"),
    );

    // Series
    const seriesBox = el("div", { class: "stack", id: "series-box" });
    const seriesRow = s => el("div", { class: "series-row" },
      el("label", {}, "Name", el("input", { "data-f": "name", value: s.name })),
      el("label", {}, "Column", el("select", { "data-f": "column" },
        cols.map(c => el("option", { value: c, selected: c === s.column }, c)))),
      el("label", {}, "Colour", el("input", { type: "color", "data-f": "color", value: s.color })),
      el("label", { class: "radio" }, el("input", { type: "checkbox", "data-f": "dash", checked: !!s.dash }), "Dashed"),
      el("button", { type: "button", class: "danger", onclick: e => e.target.closest(".series-row").remove() }, "×"),
    );
    (cfg.series || []).forEach(s => { const r = seriesRow(s); r.dataset.key = s.key; seriesBox.append(r); });
    const addSeries = el("button", { type: "button", onclick: () => {
      const used = new Set([...seriesBox.querySelectorAll("[data-f=column]")].map(x => x.value));
      const c = cols.find(c => !used.has(c)) || cols[0];
      const pal = P.theme.chart.series_palette;
      const r = seriesRow({ name: c, column: c, color: pal[seriesBox.children.length % pal.length] });
      r.dataset.key = c; seriesBox.append(r);
    } }, "+ Series");
    form.append(el("h3", {}, "Series ", el("span", { class: "muted small" },
      `(${P.manifest.min_series}–${P.manifest.max_series})`)), seriesBox, addSeries);

    // Annotations
    const annBox = el("div", { class: "stack" });
    const seriesOpts = sel => (cfg.series || []).map(s => el("option", { value: s.key, selected: s.key === sel }, s.name));
    const annRow = (a, i) => el("div", { class: "ann-row" },
      el("span", { class: "id" }, String(a.id ?? i + 1)),
      el("label", {}, "Text", el("input", { "data-f": "text", value: a.text || "" })),
      el("label", {}, "At x", el("select", { "data-f": "x" }, xs.map(x => el("option", { value: x, selected: x === a.x }, x)))),
      el("label", {}, "On line", el("select", { "data-f": "series" }, seriesOpts(a.series))),
      el("label", {}, "Position", el("select", { "data-f": "position" },
        ["above", "below"].map(p => el("option", { value: p, selected: p === a.position }, p)))),
      el("label", {}, "Nudge x", el("input", { "data-f": "dx", type: "number", step: 10, value: a.dx || 0 })),
      el("label", {}, "Nudge y", el("input", { "data-f": "dy", type: "number", step: 10, value: a.dy || 0 })),
      el("button", { type: "button", class: "danger", onclick: e => e.target.closest(".ann-row").remove() }, "×"),
    );
    (cfg.annotations || []).forEach((a, i) => annBox.append(annRow(a, i)));
    form.append(el("h3", {}, "Annotations ",
      el("span", { class: "muted small" }, "(numbered: cues refer to them by number)")), annBox,
      el("button", { type: "button", onclick: () => {
        const ids = [...annBox.querySelectorAll(".id")].map(x => +x.textContent);
        annBox.append(annRow({ id: Math.max(0, ...ids) + 1, x: xs[Math.floor(xs.length / 2)] }, 0));
      } }, "+ Annotation"));

    // Template options
    if (P.manifest.options.length) {
      form.append(el("h3", {}, "Template options"));
      for (const o of P.manifest.options) {
        const v = cfg.options?.[o.name] ?? o.default;
        let input;
        if (o.type === "bool") {
          form.append(el("label", { class: "radio" }, input = el("input", { type: "checkbox", "data-opt": o.name, checked: !!v }), o.label));
          continue;
        }
        if (o.type === "x") input = el("select", { "data-opt": o.name }, xs.map(x => el("option", { value: x, selected: x === v }, x)));
        else if (o.type === "series") input = el("select", { "data-opt": o.name }, seriesOpts(v));
        else input = el("input", { "data-opt": o.name, value: v ?? "" });
        form.append(el("label", {}, o.label, input));
      }
    }
    form.append(el("div", { class: "row" },
      el("button", { class: "primary", type: "submit" }, "Save chart"),
      el("span", { id: "config-msg", class: "small" })),
      el("hr"),
      el("label", {}, "Replace data CSV (keeps this config; for swapping in the real series)",
        el("input", { type: "file", accept: ".csv", id: "csv-replace" })));
    $("#csv-replace").addEventListener("change", replaceCsv);
  }

  function collectConfig() {
    const form = $("#config-form"), f = n => form.elements[n];
    const cfg = { ...P.project.config };
    for (const n of ["title", "subtitle", "source", "y_label", "x_label", "y_format", "value_format"]) cfg[n] = f(n).value;
    for (const n of ["y_min", "y_max"]) cfg[n] = f(n).value === "" ? null : +f(n).value;
    cfg.y_zero = f("y_zero").checked;
    const used = new Set();
    cfg.series = [...form.querySelectorAll(".series-row")].map(r => {
      const g = n => r.querySelector(`[data-f=${n}]`);
      let key = r.dataset.key || g("column").value;
      while (used.has(key)) key += "_2";
      used.add(key);
      return { key, column: g("column").value, name: g("name").value, color: g("color").value, dash: g("dash").checked };
    });
    cfg.annotations = [...form.querySelectorAll(".ann-row")].map(r => {
      const g = n => r.querySelector(`[data-f=${n}]`).value;
      return { id: +r.querySelector(".id").textContent, text: g("text"), x: g("x"), series: g("series"),
               position: g("position"), dx: +g("dx") || 0, dy: +g("dy") || 0 };
    });
    cfg.options = { ...(cfg.options || {}) };
    for (const i of form.querySelectorAll("[data-opt]")) cfg.options[i.dataset.opt] = i.type === "checkbox" ? i.checked : i.value;
    return cfg;
  }

  $("#config-form").addEventListener("submit", async e => {
    e.preventDefault();
    const msg = $("#config-msg");
    try {
      P = await api("PUT", `/api/projects/${PID}/config`, { config: collectConfig() });
      msg.textContent = "Saved"; msg.className = "small job done";
      renderConfigForm(); renderCueList(); reloadChart();
    } catch (err) { msg.textContent = err.message; msg.className = "small job error"; }
  });

  async function replaceCsv(e) {
    const fd = new FormData(); fd.append("csv", e.target.files[0]);
    try { P = await api("POST", `/api/projects/${PID}/csv`, fd); renderConfigForm(); reloadChart(); }
    catch (err) { alert(err.message); }
  }

  $("#project-name").addEventListener("change", async e => {
    await api("PUT", `/api/projects/${PID}/config`, { config: P.project.config, name: e.target.value });
  });

  // ------------------------------------------------------------ video + jobs
  function renderVideoInfo() {
    const v = P.project.video, t = P.transcript;
    $("#video-info").textContent = v
      ? `${v.uploaded_name}: ${v.duration.toFixed(1)}s, ${v.source.width}×${v.source.height} ${v.source.codec}` +
        (t ? ` · transcript v${t.version} (${t.words.length} words, ${t.source})` : " · not transcribed")
      : "No video yet.";
  }

  function showJob(kind, j) {
    const box = $("#job-" + kind);
    if (!box) return;
    box.className = "job " + j.status;
    box.textContent = "";
    if (j.status === "running" || j.status === "queued") {
      box.append(el("div", {}, j.message || j.status),
                 el("div", { class: "bar" }, el("i", { style: `width:${(j.progress * 100).toFixed(1)}%` })));
    } else {
      box.append(j.message || j.status);
    }
  }

  async function pollJob(kind, jid) {
    for (;;) {
      const j = await api("GET", `/api/jobs/${jid}`);
      showJob(kind, j);
      if (j.status === "done" || j.status === "error") {
        if (j.status === "done") await onJobDone(kind);
        return j;
      }
      await new Promise(r => setTimeout(r, 700));
    }
  }

  async function onJobDone(kind) {
    P = await api("GET", `/api/projects/${PID}`);
    if (kind === "video") {
      layoutStage(); renderVideoInfo();
      const tj = await api("GET", `/api/projects/${PID}`).then(p => p.jobs.transcribe);
      if (tj && (tj.status === "queued" || tj.status === "running")) pollJob("transcribe", tj.id);
    }
    if (kind === "transcribe") {
      renderVideoInfo(); renderTranscript(); renderCueList(); reloadChart();
      switchTab("cues");
    }
    if (kind === "render") loadRenders();
  }

  $("#upload-video").addEventListener("click", () => {
    const file = $("#video-file").files[0];
    if (!file) return alert("Choose a video file first");
    const fd = new FormData();
    fd.append("video", file);
    fd.append("transcribe", $("#auto-transcribe").checked ? "1" : "0");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/projects/${PID}/video`);
    xhr.upload.onprogress = e => showJob("video", { status: "running", progress: e.loaded / e.total, message: "Uploading" });
    xhr.onload = () => {
      const j = JSON.parse(xhr.responseText || "{}");
      if (xhr.status >= 400) return showJob("video", { status: "error", message: j.error });
      pollJob("video", j.job);
    };
    xhr.send(fd);
  });

  $("#transcribe").addEventListener("click", async () => {
    try { const r = await api("POST", `/api/projects/${PID}/transcribe`); pollJob("transcribe", r.job); }
    catch (e) { showJob("transcribe", { status: "error", message: e.message }); }
  });

  $("#import-btn").addEventListener("click", async () => {
    try {
      const r = await api("POST", `/api/projects/${PID}/transcript/import`, JSON.parse($("#import-json").value));
      showJob("transcribe", { status: "done", message: r.message });
      await onJobDone("transcribe");
    } catch (e) { showJob("transcribe", { status: "error", message: e.message }); }
  });

  // ------------------------------------------------------------ transcript + cues
  function cuesByWord() {
    const m = new Map();
    for (const c of P.cues) if (c.status !== "orphaned" && c.transcript_id === P.transcript?.id) {
      if (!m.has(c.word_index)) m.set(c.word_index, []);
      m.get(c.word_index).push(c);
    }
    return m;
  }

  function renderTranscript() {
    const box = $("#transcript");
    box.textContent = "";
    if (!P.transcript) { box.append(el("span", { class: "muted" }, "No transcript yet. Upload a video in step 2.")); return; }
    const byWord = cuesByWord();
    for (const w of P.transcript.words) {
      const cls = ["w", byWord.has(w.i) ? "has-cue" : "", w.prob < 0.5 ? "low" : "", w.i === selWord ? "sel" : ""];
      box.append(el("span", {
        class: cls.join(" "), "data-i": w.i,
        title: `${fmtT(w.start)}${byWord.has(w.i) ? " · " + byWord.get(w.i).map(c => c.description).join("; ") : ""}`,
      }, w.text), " ");
    }
    lastWordNow = -1;
  }

  $("#transcript").addEventListener("click", async e => {
    const n = e.target.closest(".w");
    if (!n) return;
    const i = +n.dataset.i;
    if (reattachCue != null) {
      await api("PUT", `/api/cues/${reattachCue}`, { word_index: i });
      reattachCue = null;
      await refresh();
      return;
    }
    selectWord(i);
  });

  $("#transcript").addEventListener("dblclick", async e => {
    const n = e.target.closest(".w");
    if (!n) return;
    const i = +n.dataset.i, w = P.transcript.words[i];
    const text = prompt("Fix this word (timing stays the same). A space splits it into two words; empty deletes it:", w.text);
    if (text != null && text !== w.text) await editWord(i, "text", text);
  });

  function selectWord(i, cue = null) {
    selWord = i;
    editingCue = cue ? cue.id : null;
    if (P.project.video) video.pause(); else virtualPlaying = false;
    seek(P.transcript.words[i].start + (cue?.offset || 0));
    document.querySelector(".w.sel")?.classList.remove("sel");
    document.querySelector(`.w[data-i="${i}"]`)?.classList.add("sel");
    renderCueEditor(cue);
  }

  function paramInput(p, value) {
    const cfg = P.project.config, data = P.project.data;
    let v = value ?? p.default;
    if (v === "@diverging") v = (cfg.series || []).find(s => s.key !== cfg.options?.base_series)?.key;
    if (typeof v === "string" && v.startsWith("@theme:"))
      v = v.slice(7).split(".").reduce((o, k) => o?.[k], P.theme);
    const attrs = { "data-p": p.name };
    switch (p.type) {
      case "textarea":
        return el("textarea", { ...attrs, rows: 3, placeholder: "Press Return for a new line" }, v ?? "");
      case "color":
        return el("input", { ...attrs, type: "color", value: v || "#000000" });
      case "series":
        return el("select", attrs, (cfg.series || []).map(s => el("option", { value: s.key, selected: s.key === v }, s.name)));
      case "x":
        return el("select", attrs, [...(p.optional ? [""] : []), ...(p.allow_end ? ["end"] : []), ...data.x.values]
          .map(x => el("option", { value: x, selected: x === (v ?? "") },
            x === "" ? `(${p.placeholder || "default"})` : x === "end" ? "the end" : x)));
      case "annotation":
        return el("select", attrs, (cfg.annotations || []).map(a =>
          el("option", { value: a.id, selected: +a.id === +v }, `${a.id}: ${a.text}`)));
      case "choice":
        return el("select", attrs, p.options.map(o => el("option", { value: o, selected: o === v }, o)));
      case "bool":
        return el("input", { ...attrs, type: "checkbox", checked: !!v });
      case "number": case "int":
        return el("input", { ...attrs, type: "number", step: p.type === "int" ? 1 : 0.1, value: v ?? "" });
      default:
        return el("input", { ...attrs, value: v ?? "" });
    }
  }

  function renderCueEditor(cue = null) {
    const box = $("#cue-editor"), w = P.transcript.words[selWord];
    box.classList.remove("hidden");
    box.textContent = "";
    const existing = cuesByWord().get(selWord) || [];
    box.append(el("h4", {}, `“${w.text}” at ${fmtT(w.start)}`));
    const next = P.transcript.words[selWord + 1];
    const fix = el("input", { value: w.text, style: "width:160px", title: "A space splits it into two words" });
    box.append(el("div", { class: "row word-tools" },
      el("span", { class: "small muted" }, "Fix transcript:"), fix,
      el("button", { type: "button", onclick: () => editWord(selWord, "text", fix.value) }, "Save word"),
      next ? el("button", { type: "button", title: "Join with the next word, e.g. “1” + “.2” → “1.2”",
        onclick: () => editWord(selWord, "merge") }, `Merge with “${next.text}”`) : null,
      el("button", { type: "button", class: "danger", onclick: () => editWord(selWord, "delete") }, "Delete word")));
    if (existing.length) {
      box.append(el("ul", { class: "existing" }, existing.map(c => el("li", {},
        el("span", {}, c.description),
        el("button", { type: "button", onclick: () => selectWord(c.word_index, c) }, "Edit"),
        el("button", { type: "button", class: "danger", onclick: () => deleteCue(c.id) }, "Delete")))));
    }
    const actions = P.manifest.actions;
    const groups = [...new Set(actions.map(a => a.group || "Chart"))];
    const sel = el("select", {}, groups.map(g => el("optgroup", { label: g },
      actions.filter(a => (a.group || "Chart") === g)
        .map(a => el("option", { value: a.name, selected: cue?.action === a.name }, a.label)))));
    const desc = el("div", { class: "desc" });
    const params = el("div", { class: "row" });
    const offset = el("input", { type: "number", step: 0.05, value: cue?.offset ?? 0, style: "width:90px" });
    // New cues start from the settings of the latest cue of the same kind (same
    // text slot for "Show text"), so sizes and colours stay consistent. Text isn't copied.
    const lastLike = (action, slot) => [...P.cues].reverse().find(c => c.action === action &&
      (slot == null || c.params.slot === slot));
    const fill = (slot) => {
      const a = actions.find(a => a.name === sel.value);
      desc.textContent = a.description;
      let base = cue && cue.action === a.name ? cue.params : null;
      if (!base) {
        const prev = lastLike(a.name, a.name === "show_text" ? (slot ?? null) : null) ||
                     (a.name === "show_text" ? lastLike(a.name) : null);
        base = prev ? { ...prev.params, text: undefined } : {};
        if (slot != null) base.slot = slot;
        // First text in a slot: slots 1/2/3 start as label / big number / note, stacked
        // down the chart (4-6 repeat the pattern). Later cues copy the slot's last cue.
        if (a.name === "show_text" && !lastLike(a.name, base.slot ?? "1")) {
          const k = ((+(base.slot ?? 1)) - 1) % 3, ts = P.theme.overlay?.text_size ?? 72;
          base.y = [30, 55, 78][k];
          base.size = [ts, Math.round(ts * 3), Math.round(ts * 0.8)][k];
        }
      }
      params.textContent = "";
      for (const p of a.params) {
        const input = paramInput(p, base[p.name]);
        params.append(p.type === "bool"
          ? el("label", { class: "radio" }, input, p.label)
          : el("label", { class: p.type === "textarea" ? "wide" : "" }, p.label, input));
        if (a.name === "show_text" && p.name === "slot" && !(cue && cue.action === a.name))
          input.addEventListener("change", () => fill(input.value));
      }
    };
    sel.addEventListener("change", () => fill());
    fill();
    const msg = el("span", { class: "small" });
    const save = async () => {
      const ps = {};
      for (const i of params.querySelectorAll("[data-p]")) ps[i.dataset.p] = i.type === "checkbox" ? i.checked : i.value;
      try {
        if (editingCue) await api("PUT", `/api/cues/${editingCue}`, { action: sel.value, params: ps, offset: +offset.value });
        else await api("POST", `/api/projects/${PID}/cues`, { word_index: selWord, action: sel.value, params: ps, offset: +offset.value });
        editingCue = null;
        await refresh();
      } catch (e) { msg.textContent = e.message; msg.className = "small job error"; }
    };
    box.append(
      el("div", { class: "row" }, el("label", {}, editingCue ? "Edit action" : "Add action", sel),
        el("label", {}, "Offset (s)", offset)),
      desc, params,
      el("div", { class: "row", style: "margin-top:10px" },
        el("button", { class: "primary", type: "button", onclick: save }, editingCue ? "Save cue" : "Add cue"),
        el("button", { type: "button", onclick: () => { seek(w.start - 1.5); togglePlay(); } }, "▶ Play from here"),
        el("button", { type: "button", onclick: () => { box.classList.add("hidden"); selWord = null; editingCue = null;
          document.querySelector(".w.sel")?.classList.remove("sel"); } }, "Close"),
        msg));
  }

  async function editWord(i, op, text = "") {
    try {
      await api("POST", `/api/projects/${PID}/transcript/words/${i}`, { op, text });
      if (op === "delete" && i >= P.transcript.words.length - 1) selWord = Math.max(0, i - 1);
      await refresh();
    } catch (e) { alert(e.message); }
  }

  async function deleteCue(id) {
    await api("DELETE", `/api/cues/${id}`);
    await refresh();
  }

  async function refresh() {
    P = await api("GET", `/api/projects/${PID}`);
    renderTranscript(); renderCueList(); reloadChart();
    if (selWord != null && P.transcript) renderCueEditor(P.cues.find(c => c.id === editingCue));
  }

  function renderCueList() {
    const body = $("#cue-list");
    body.textContent = "";
    $("#cuesheet-csv").href = `/api/projects/${PID}/cuesheet.csv`;
    $("#cuesheet-json").href = `/api/projects/${PID}/cuesheet.json`;
    const words = P.transcript?.words || [];
    for (const c of P.cues) {
      const orphan = c.status === "orphaned";
      body.append(el("tr", { class: c.status },
        el("td", { class: "mono" }, orphan ? "–" : fmtT(c.t)),
        el("td", {}, orphan ? el("span", { class: "status-orphaned" }, `“${c.anchor_word}” not found`) : (words[c.word_index]?.text ?? "?")),
        el("td", {}, c.description),
        el("td", {},
          orphan
            ? el("button", { type: "button", onclick: () => { reattachCue = c.id; switchTab("cues");
                alert("Now click the word this cue should attach to."); } }, "Re-attach")
            : el("button", { type: "button", title: "Play from just before", onclick: () => { seek(c.t - 1.5); if (!playing()) togglePlay(); } }, "▶"),
          " ",
          orphan ? null : el("button", { type: "button", onclick: () => { switchTab("cues"); selectWord(c.word_index, c); } }, "Edit"),
          " ",
          el("button", { type: "button", class: "danger", onclick: () => deleteCue(c.id) }, "×"))));
    }
    if (!P.cues.length) body.append(el("tr", {}, el("td", { colspan: 4, class: "muted" }, "No cues yet.")));
  }

  // ------------------------------------------------------------ render
  $("#render").addEventListener("click", async () => {
    try { const r = await api("POST", `/api/projects/${PID}/render`); pollJob("render", r.job); }
    catch (e) { showJob("render", { status: "error", message: e.message }); }
  });

  async function loadRenders() {
    const list = await api("GET", `/api/projects/${PID}/renders`);
    const box = $("#renders");
    box.textContent = "";
    const f = (path, label) => el("li", {}, el("a", { href: `/p/${PID}/files/${path}?download=1` }, label));
    for (const r of list) {
      box.append(el("div", { class: "render-item" }, el("div", { class: "render-info" },
        el("b", {}, r.dir.split("/").pop()),
        el("div", { class: "muted small" }, `${r.frames} frames, ${r.duration.toFixed(1)}s · capture ${r.capture.seconds}s ` +
          `(${r.capture.screenshots} drawn, ${r.capture.reused} reused)`),
        el("ul", { class: "outputs" },
          f(r.final, "Final MP4 (H.264 1080×1920)"),
          f(r.overlay_mov, "Chart overlay, ProRes 4444 with alpha (.mov)"),
          el("li", {}, `PNG sequence: ${r.overlay_png_dir}/ (in the project folder)`),
          f(r.srt, "Captions (.srt)"),
          f(r.cue_sheet_csv, "Cue sheet (.csv)"),
          f(r.cue_sheet_json, "Cue sheet (.json)"),
          f(r.transcript_json, "Transcript with word timings (.json)"))),
        // #t=1 shows a frame from the second second, not the (often blank) first frame
        el("video", { src: `/p/${PID}/files/${r.final}#t=1`, controls: true, preload: "metadata" })));
    }
  }

  // ------------------------------------------------------------ misc wiring
  function switchTab(name) {
    document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab").forEach(t => t.classList.toggle("hidden", t.id !== "tab-" + name));
  }
  document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));

  $("#play").addEventListener("click", togglePlay);
  $("#scrub").addEventListener("input", e => seek(+e.target.value));
  document.addEventListener("keydown", e => {
    if (e.code === "Space" && !["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(document.activeElement.tagName)) {
      e.preventDefault(); togglePlay();
    }
  });

  let layoutTimer = null;
  const saveLayout = () => {
    const layout = { talent_crop_center: +$("#crop-center").value, talent_zoom: +$("#crop-zoom").value,
                     mode: $("#layout-mode").value };
    P.project.layout = { ...P.project.layout, ...layout };
    layoutStage();
    clearTimeout(layoutTimer);
    layoutTimer = setTimeout(() => api("PUT", `/api/projects/${PID}/layout`, layout), 300);
  };
  ["#crop-center", "#crop-zoom", "#layout-mode"].forEach(s => $(s).addEventListener("input", saveLayout));
  window.addEventListener("resize", () => P && layoutStage());

  load().catch(e => alert(e.message));
  requestAnimationFrame(tick);
})();
