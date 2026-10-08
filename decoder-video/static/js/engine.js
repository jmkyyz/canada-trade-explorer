/*
 * Decoder chart engine.
 *
 * The one rule: a chart is a pure function of time. Cues are compiled once
 * into a timeline of tweened "tracks" (numbers that change over time), and
 * chart.seek(t) draws the exact state at second t. Nothing uses wall-clock
 * timers or d3 transitions, so:
 *   - the browser preview calls seek(video.currentTime) every animation frame
 *   - the renderer calls seek(frame / fps) for each captured frame
 * and both get identical pictures. seek() returns a signature string of the
 * state; the renderer reuses the previous PNG when it hasn't changed.
 *
 * Templates register with Decoder.registerTemplate(name, {setup, compile, render}).
 */
(function () {
  "use strict";

  const EASES = {
    "linear": d3.easeLinear,
    "cubic-in-out": d3.easeCubicInOut,
    "quad-out": d3.easeQuadOut,
    "cubic-out": d3.easeCubicOut,
  };

  // ---------------------------------------------------------------- timeline
  class Timeline {
    constructor() { this.tracks = new Map(); }

    _track(key, initial) {
      if (!this.tracks.has(key)) this.tracks.set(key, { initial, segs: [] });
      return this.tracks.get(key);
    }

    /** Declare a track and its value before any cue touches it. */
    init(key, initial) { this._track(key, initial); }

    has(key) { return this.tracks.has(key); }

    keys(prefix) { return [...this.tracks.keys()].filter(k => k.startsWith(prefix)); }

    /** Value of a track at time t. */
    value(key, t) {
      const tr = this.tracks.get(key);
      if (!tr) return undefined;
      let v = tr.initial;
      for (const s of tr.segs) {
        if (t < s.t0) break;
        if (t >= s.t1) { v = s.to; continue; }
        const p = s.ease((t - s.t0) / (s.t1 - s.t0));
        v = s.from + (s.to - s.from) * p;
      }
      return v;
    }

    /** Animate a track from its value at t0 to `to`. Cues compile in time order. */
    tween(key, t0, dur, to, ease = "cubic-in-out", initial = 0, from = undefined) {
      const tr = this._track(key, initial);
      if (from === undefined) from = this.value(key, t0);
      tr.segs.push({ t0, t1: t0 + Math.max(dur, 1e-6), from, to,
                     ease: EASES[ease] || EASES["cubic-in-out"] });
      tr.segs.sort((a, b) => a.t0 - b.t0);
    }

    /** Jump to a value instantly at t0. */
    set(key, t0, to, initial = 0) { this.tween(key, t0, 0, to, "linear", initial, to); }

    /** Every track value at t, rounded: used to skip re-capturing identical frames. */
    signature(t) {
      const out = [];
      for (const k of this.tracks.keys()) {
        const v = this.value(k, t);
        out.push(typeof v === "number" ? Math.round(v * 1e4) : v);
      }
      return out.join(",");
    }
  }

  // ----------------------------------------------------------------- helpers
  const fmt = (spec) => {
    try { return d3.format(spec); } catch (e) { return d3.format(",~s"); }
  };

  /** Minus sign and thin-space formatting suitable for video graphics. */
  function formatter(spec) {
    const f = fmt(spec || ",.0f");
    return v => f(v).replace("-", "−");
  }

  /** Wrap text into an SVG <text> element with <tspan> lines. Returns line count. */
  function wrapText(textSel, str, maxWidth, lineHeightPx) {
    textSel.text(null);
    const words = String(str || "").split(/\s+/).filter(Boolean);
    let line = [], lines = 0;
    let tspan = textSel.append("tspan").attr("x", textSel.attr("x") || 0).attr("dy", 0);
    for (const w of words) {
      line.push(w);
      tspan.text(line.join(" "));
      if (tspan.node().getComputedTextLength() > maxWidth && line.length > 1) {
        line.pop();
        tspan.text(line.join(" "));
        line = [w];
        lines++;
        tspan = textSel.append("tspan").attr("x", textSel.attr("x") || 0)
          .attr("dy", lineHeightPx).text(w);
      }
    }
    return words.length ? lines + 1 : 0;
  }

  /** Common chrome: panel, title, subtitle, source. Returns the free rect for the plot. */
  function buildChrome(ctx) {
    const { root, theme, config, width, height } = ctx;
    const C = theme.chart, pad = C.padding;
    root.append("rect").attr("class", "panel")
      .attr("width", width).attr("height", height).attr("fill", C.background);

    const chrome = root.append("g").attr("class", "chrome");
    ctx.chromeLayer = chrome;
    let y = pad.top;
    const innerW = width - pad.left - pad.right;

    if (config.title) {
      const t = chrome.append("text").attr("class", "title")
        .attr("x", pad.left).attr("y", y + C.title.size * 0.85)
        .style("font-family", theme.fonts.headline).style("font-size", C.title.size + "px")
        .style("font-weight", C.title.weight).attr("fill", C.title.color);
      const n = wrapText(t, config.title, innerW, C.title.size * C.title.line_height);
      y += n * C.title.size * C.title.line_height;
    }
    if (config.subtitle) {
      y += C.subtitle.gap;
      const t = chrome.append("text").attr("class", "subtitle")
        .attr("x", pad.left).attr("y", y + C.subtitle.size * 0.85)
        .style("font-family", theme.fonts.body).style("font-size", C.subtitle.size + "px")
        .style("font-weight", C.subtitle.weight).attr("fill", C.subtitle.color);
      const n = wrapText(t, config.subtitle, innerW, C.subtitle.size * C.subtitle.line_height);
      y += n * C.subtitle.size * C.subtitle.line_height;
    }
    let bottom = height - pad.bottom;
    if (config.source) {
      chrome.append("text").attr("class", "source")
        .attr("x", pad.left).attr("y", bottom)
        .style("font-family", theme.fonts.body).style("font-size", C.source.size + "px")
        .style("font-weight", C.source.weight).attr("fill", C.source.color)
        .text(config.source);
      bottom -= C.source.size + 12;
    }
    return { x: pad.left, y, width: innerW, height: bottom - y };
  }

  // ------------------------------------------------------------------ engine
  const templates = {};

  function registerTemplate(name, impl) { templates[name] = impl; }

  /**
   * spec = {template, config, data, theme, cues: [{t, action, params}], width, height}
   */
  function createChart(container, spec) {
    const impl = templates[spec.template];
    if (!impl) throw new Error("No renderer for template " + spec.template);
    const width = spec.width || spec.theme.layout.chart.width;
    const height = spec.height || spec.theme.layout.chart.height;

    d3.select(container).selectAll("*").remove();
    const svg = d3.select(container).append("svg")
      .attr("xmlns", "http://www.w3.org/2000/svg")
      .attr("width", width).attr("height", height)
      .attr("viewBox", `0 0 ${width} ${height}`)
      .style("display", "block");
    const root = svg.append("g").attr("class", "decoder-root");

    const ctx = {
      svg, root, width, height,
      theme: spec.theme, config: spec.config, data: spec.data,
      tl: new Timeline(), helpers: { formatter, wrapText },
    };
    ctx.plotRect = buildChrome(ctx);
    impl.setup(ctx);

    // Compile cues (time order). Common actions first, then the template's.
    const cues = [...(spec.cues || [])].sort((a, b) => a.t - b.t);
    const tl = ctx.tl;
    const fade = spec.theme.motion.fade;
    tl.init("chart", cues.some(c => c.action === "show_chart") ? 0 : 1);
    for (const a of spec.config.annotations || []) tl.init("ann." + a.id, 0);

    for (const cue of cues) {
      const p = cue.params || {};
      const dur = p.duration != null ? +p.duration : fade;
      switch (cue.action) {
        case "show_chart": tl.tween("chart", cue.t, dur, 1); break;
        case "hide_chart": tl.tween("chart", cue.t, dur, 0); break;
        case "show_annotation": tl.tween("ann." + p.annotation, cue.t, dur, 1); break;
        case "hide_annotation": tl.tween("ann." + p.annotation, cue.t, dur, 0); break;
        case "hold": break;
        default:
          if (!impl.compile(cue, ctx)) console.warn("Unhandled action", cue.action);
      }
    }

    let lastT = null;
    return {
      ctx,
      seek(t) {
        lastT = t;
        root.attr("opacity", 1);
        const chartOp = tl.value("chart", t);
        ctx.chromeLayer.attr("opacity", chartOp);
        impl.render(t, ctx, chartOp);
        return tl.signature(t);
      },
      get time() { return lastT; },
    };
  }

  window.Decoder = { registerTemplate, createChart, Timeline, formatter, wrapText, templates };
})();
