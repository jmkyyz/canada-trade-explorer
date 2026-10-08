/*
 * line_split: a series that diverges from a base series (previous vs. revised).
 *
 * Extends LineBase (line_draw.js). Extra tracks per diverging series s:
 *   split.s     0..1 how far s has peeled away from the base (1 = its own values)
 *   gap.s       0..1 opacity of the shaded gap between base and s
 *   gaplab.s|x  0..1 visibility of the bracketed difference label at x
 *
 * Before the split point the two series are the same line, so the diverging
 * series is only ever drawn from the split point onward.
 */
(function () {
  "use strict";
  const B = window.Decoder.LineBase;

  const LineSplit = Object.create(B);

  LineSplit.setup = function (ctx) {
    B.setup.call(this, ctx);
    const opts = ctx.config.options || {};
    ctx.baseKey = opts.base_series || ctx.series[0]?.key;
    const xs = ctx.data.x.values;
    ctx.defaultSplit = opts.split_from && xs.includes(opts.split_from) ? opts.split_from : xs[0];
    ctx.splits = {};   // series key -> {index, base}
    ctx.gapLabels = new Map();
    ctx.gapFmt = ctx.helpers.formatter(opts.gap_format || "+,.0f");
    const C = ctx.theme.chart;
    for (const s of ctx.series) {
      if (s.key === ctx.baseKey) continue;
      s.gapPath = ctx.gapLayer.append("path").attr("fill", s.color)
        .attr("opacity", 0).attr("stroke", "none");
    }
    ctx.gapLabelLayer = ctx.labelLayer.append("g").attr("class", "gap-labels");
    ctx.gapLabelStyle = C.gap_label;
  };

  LineSplit.compile = function (cue, ctx) {
    const p = cue.params || {}, t = cue.t, tl = ctx.tl;
    const fade = ctx.theme.motion.fade;
    switch (cue.action) {
      case "split_line": {
        const from = p.from || ctx.defaultSplit;
        const index = Math.max(0, ctx.data.x.values.indexOf(from));
        ctx.splits[p.series] = { index, base: ctx.baseKey };
        const dur = +p.duration || 2.5, ease = p.ease || "cubic-in-out";
        tl.set("op." + p.series, t, 1);
        if (p.style === "draw") {
          tl.set("split." + p.series, t, 1);
          tl.tween("head." + p.series, t, dur, ctx.xmax, ease, 0, ctx.xpos(from));
        } else {
          tl.set("head." + p.series, t, ctx.xmax);
          tl.tween("split." + p.series, t, dur, 1, ease, 0, 0);
        }
        return true;
      }
      case "shade_gap":
        tl.tween("gap." + p.series, t, +p.duration || fade, 1);
        return true;
      case "label_gap": {
        const key = `${p.series}|${p.x || "end"}`;
        ctx.gapLabels.set(key, { series: p.series, x: p.x || "end" });
        tl.tween("gaplab." + key, t, +p.duration || fade, 1);
        return true;
      }
    }
    return B.compile.call(this, cue, ctx);
  };

  /** Points of a diverging series: from the split point on, morphed from the base. */
  LineSplit.linePoints = function (s, head, t, ctx) {
    const sp = ctx.splits[s.key];
    if (!sp) return B.linePoints.call(this, s, head, t, ctx);
    const base = ctx.seriesByKey[sp.base];
    const f = ctx.tl.value("split." + s.key, t) ?? 1;
    const pts = s.points.slice(sp.index).map((pt, k) => {
      const b = base.points[sp.index + k];
      const y = pt.y == null || b.y == null ? pt.y : b.y + (pt.y - b.y) * f;
      return { ...pt, y, baseY: b.y };
    });
    return B.partial(pts, head);
  };

  LineSplit.render = function (t, ctx, chartOp) {
    B.render.call(this, t, ctx, chartOp);
    const C = ctx.theme.chart, tl = ctx.tl;
    const area = d3.area().defined(p => p.y != null && p.baseY != null)
      .x(p => ctx.x(p.xp)).y0(p => ctx.y(p.baseY)).y1(p => ctx.y(p.y));
    for (const s of ctx.series) {
      if (!s.gapPath) continue;
      const g = tl.has("gap." + s.key) ? tl.value("gap." + s.key, t) : 0;
      if (!g || !ctx.splits[s.key]) { s.gapPath.attr("opacity", 0); continue; }
      const pts = this.linePoints(s, tl.value("head." + s.key, t), t, ctx)
        .map(p => ({ ...p, baseY: p.baseY ?? B.valueAt(ctx.seriesByKey[ctx.baseKey].points, p.xp) }));
      s.gapPath.attr("d", pts.length > 1 ? area(pts) : null)
        .attr("opacity", g * C.gap_fill_opacity);
    }

    // Bracketed gap labels
    const G = ctx.gapLabelStyle;
    const sel = ctx.gapLabelLayer.selectAll("g.gl").data([...ctx.gapLabels.entries()], d => d[0]);
    const enter = sel.enter().append("g").attr("class", "gl");
    enter.append("path").attr("fill", "none").attr("stroke", C.title.color).attr("stroke-width", 3);
    enter.append("text").attr("text-anchor", "end")
      .style("font-family", ctx.theme.fonts.numeric).style("font-size", G.size + "px")
      .style("font-weight", G.weight).attr("fill", C.title.color)
      .attr("stroke", C.background).attr("stroke-width", 10).attr("paint-order", "stroke");
    enter.merge(sel).each(function ([key, gl]) {
      const g = d3.select(this);
      const vis = tl.value("gaplab." + key, t);
      const s = ctx.seriesByKey[gl.series], base = ctx.seriesByKey[ctx.baseKey];
      const xp = ctx.xpos(gl.x);
      const a = B.valueAt(base.points, xp), b = s && B.valueAt(s.points, xp);
      if (!vis || a == null || b == null) { g.attr("opacity", 0); return; }
      const px = ctx.x(xp) - 26, ya = ctx.y(a), yb = ctx.y(b);
      g.attr("opacity", vis);
      g.select("path").attr("d", `M${px + 12},${ya} H${px} V${yb} H${px + 12}`);
      g.select("text").attr("x", px - 12).attr("y", (ya + yb) / 2).attr("dy", "0.35em")
        .text(ctx.gapFmt(b - a));
    });
  };

  window.Decoder.registerTemplate("line_split", LineSplit);
})();
