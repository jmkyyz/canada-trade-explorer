/*
 * line_draw: time-series lines that draw themselves left to right.
 *
 * Tracks per series key s:
 *   head.s   x position (data units) the line is drawn up to
 *   op.s     line opacity
 * Per highlight h = "<series>|<x>":
 *   hl.h     0..1 marker visibility
 *   pulse.h  0..pulses, fractional part drives the expanding ring
 *
 * Decoder.LineBase is reused by line_split (and any future line template).
 */
(function () {
  "use strict";

  /** Points of a series up to `head` (data x units), interpolating the last one. */
  function partial(points, head) {
    const out = [];
    for (let i = 0; i < points.length; i++) {
      const p = points[i];
      if (p.xp <= head) { out.push(p); continue; }
      const prev = points[i - 1];
      if (prev && prev.y != null && p.y != null && head > prev.xp) {
        const f = (head - prev.xp) / (p.xp - prev.xp);
        out.push({ xp: head, y: prev.y + (p.y - prev.y) * f, interp: true });
      }
      break;
    }
    return out;
  }

  /** y value of a series at data position xp (linear interpolation). */
  function valueAt(points, xp) {
    for (let i = 0; i < points.length; i++) {
      if (points[i].xp === xp) return points[i].y;
      if (points[i].xp > xp && i > 0) {
        const a = points[i - 1], b = points[i];
        if (a.y == null || b.y == null) return null;
        return a.y + (b.y - a.y) * (xp - a.xp) / (b.xp - a.xp);
      }
    }
    return null;
  }

  const LineBase = {
    partial, valueAt,

    setup(ctx) {
      const { root, theme, config, data, plotRect } = ctx;
      const C = theme.chart, M = C.plot_margin, A = C.axis;
      const positions = data.x.positions;
      const xIndex = new Map(data.x.values.map((v, i) => [v, positions[i]]));
      ctx.xpos = v => (v === "end" ? positions[positions.length - 1] : xIndex.get(v));
      ctx.xmin = d3.min(positions);
      ctx.xmax = d3.max(positions);

      ctx.series = (config.series || []).map((s, i) => ({
        ...s,
        index: i,
        points: positions.map((xp, j) => ({ xp, x: data.x.values[j], y: data.columns[s.column]?.[j] ?? null })),
      }));
      ctx.seriesByKey = Object.fromEntries(ctx.series.map(s => [s.key, s]));

      // Scales. One y axis, shared by every line.
      const ys = ctx.series.flatMap(s => s.points.map(p => p.y)).filter(v => v != null);
      let lo = config.y_min ?? d3.min(ys), hi = config.y_max ?? d3.max(ys);
      if (config.y_zero !== false) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
      const yLabelSpace = config.y_label ? A.label_size + 24 : 0;
      const plot = {
        x: plotRect.x + M.left, y: plotRect.y + M.top + yLabelSpace,
        width: plotRect.width - M.left - M.right,
        height: plotRect.height - M.top - M.bottom - yLabelSpace,
      };
      ctx.plot = plot;
      ctx.x = d3.scaleLinear().domain([ctx.xmin, ctx.xmax]).range([plot.x, plot.x + plot.width]);
      // Pick the tick count near the theme's target that wastes the least vertical space.
      const span = d => d[1] - d[0];
      let best = null;
      for (let n = Math.max(3, A.y_ticks - 1); n <= A.y_ticks + 2; n++) {
        const d = d3.scaleLinear().domain([lo, hi]).nice(n).domain();
        const waste = 1 - (hi - lo) / span(d);
        if (!best || waste < best.waste - 0.02) best = { n, d, waste };
      }
      ctx.yTicks = best.n;
      ctx.y = d3.scaleLinear().domain(best.d).range([plot.y + plot.height, plot.y]);
      if (config.y_min != null) ctx.y.domain([config.y_min, ctx.y.domain()[1]]);
      if (config.y_max != null) ctx.y.domain([ctx.y.domain()[0], config.y_max]);

      const layer = root.append("g").attr("class", "plot");
      ctx.plotLayer = layer;
      this.drawAxes(ctx, layer);
      ctx.gapLayer = layer.append("g").attr("class", "gaps");
      ctx.lineLayer = layer.append("g").attr("class", "lines");
      ctx.annLayer = layer.append("g").attr("class", "annotations");
      ctx.hlLayer = layer.append("g").attr("class", "highlights");
      ctx.labelLayer = layer.append("g").attr("class", "end-labels");

      ctx.lineGen = d3.line().defined(p => p.y != null).x(p => ctx.x(p.xp)).y(p => ctx.y(p.y));
      for (const s of ctx.series) {
        s.path = ctx.lineLayer.append("path").attr("fill", "none")
          .attr("stroke", s.color).attr("stroke-width", C.line.width)
          .attr("stroke-linejoin", "round").attr("stroke-linecap", "round")
          .attr("stroke-dasharray", s.dash ? C.line.dash : null);
        s.dot = ctx.lineLayer.append("circle").attr("r", C.line.tip_dot_radius)
          .attr("fill", s.color).attr("stroke", C.background).attr("stroke-width", 4);
        s.label = ctx.labelLayer.append("text")
          .style("font-family", theme.fonts.body).style("font-size", C.end_label.size + "px")
          .style("font-weight", C.end_label.weight).attr("fill", C.title.color)
          .attr("stroke", C.background).attr("stroke-width", 8).attr("paint-order", "stroke")
          .attr("x", 0).attr("y", 0);
        s.labelLines = ctx.helpers.wrapText(s.label, s.name, M.right - C.end_label.gap - 10,
                                            C.end_label.size * 1.1);
        ctx.tl.init("head." + s.key, ctx.xmin - 1);
        ctx.tl.init("op." + s.key, 1);
      }
      this.buildAnnotations(ctx);
      ctx.highlights = new Map();
      ctx.fmtValue = ctx.helpers.formatter(config.value_format || ",.0f");
    },

    drawAxes(ctx, layer) {
      const { theme, config, data, plot } = ctx;
      const A = theme.chart.axis;
      const fy = ctx.helpers.formatter(config.y_format || ",~s");
      const g = layer.append("g").attr("class", "axes");
      const tickFont = sel => sel.style("font-family", theme.fonts.numeric)
        .style("font-size", A.tick_size + "px").attr("fill", A.color);

      for (const v of ctx.y.ticks(ctx.yTicks)) {
        const yy = ctx.y(v);
        g.append("line").attr("x1", plot.x).attr("x2", plot.x + plot.width)
          .attr("y1", yy).attr("y2", yy)
          .attr("stroke", v === 0 ? A.zero_color : A.grid_color)
          .attr("stroke-width", v === 0 ? A.zero_width : A.grid_width);
        g.append("text").attr("x", plot.x - 14).attr("y", yy).attr("dy", "0.35em")
          .attr("text-anchor", "end").call(tickFont).text(fy(v));
      }
      if (config.y_label) {
        g.append("text").attr("x", ctx.plotRect.x).attr("y", plot.y - A.tick_size)
          .style("font-family", theme.fonts.body).style("font-size", A.label_size + "px")
          .attr("fill", A.color).text(config.y_label);
      }
      // x ticks: whole years for time-like data, data values otherwise.
      const timeLike = ["quarter", "month", "date", "year"].includes(data.x.type);
      let ticks;
      if (timeLike) {
        ticks = d3.range(Math.ceil(ctx.xmin), Math.floor(ctx.xmax) + 1).map(v => [v, String(v)]);
        const step = Math.ceil(ticks.length / A.x_ticks);
        ticks = ticks.filter((_, i) => i % step === 0);
      } else if (data.x.type === "number") {
        ticks = ctx.x.ticks(A.x_ticks).map(v => [v, d3.format(",~r")(v)]);
      } else {
        const step = Math.ceil(data.x.values.length / A.x_ticks);
        ticks = data.x.values.map((v, i) => [data.x.positions[i], v]).filter((_, i) => i % step === 0);
      }
      const base = plot.y + plot.height;
      for (const [xp, label] of ticks) {
        const xx = ctx.x(xp);
        g.append("line").attr("x1", xx).attr("x2", xx).attr("y1", base).attr("y2", base + 12)
          .attr("stroke", A.zero_color).attr("stroke-width", A.grid_width);
        g.append("text").attr("x", xx).attr("y", base + 14 + A.tick_size)
          .attr("text-anchor", "middle").call(tickFont).text(label);
      }
      if (config.x_label) {
        g.append("text").attr("x", plot.x + plot.width / 2).attr("y", base + 2 * A.tick_size + 34)
          .attr("text-anchor", "middle")
          .style("font-family", theme.fonts.body).style("font-size", A.label_size + "px")
          .attr("fill", A.color).text(config.x_label);
      }
    },

    buildAnnotations(ctx) {
      const { theme, config, plot } = ctx;
      const T = theme.chart.annotation;
      ctx.annotations = [];
      for (const a of config.annotations || []) {
        const xp = ctx.xpos(a.x);
        if (xp == null) continue;
        const s = ctx.seriesByKey[a.series] || ctx.series[0];
        const yv = a.y != null && a.y !== "" ? +a.y : valueAt(s.points, xp);
        const px = ctx.x(xp), py = ctx.y(yv ?? ctx.y.domain()[1]);
        const below = a.position === "below";
        const g = ctx.annLayer.append("g").attr("opacity", 0);
        const text = g.append("text")
          .style("font-family", theme.fonts.body).style("font-size", T.size + "px")
          .style("font-weight", T.weight).attr("fill", T.color)
          .attr("stroke", T.background).attr("stroke-width", 10).attr("paint-order", "stroke")
          .attr("x", 0).attr("y", 0);
        const lines = ctx.helpers.wrapText(text, a.text, T.max_width, T.size * 1.2);
        const bb = text.node().getBBox();
        const boxH = lines * T.size * 1.2;
        // Keep the label inside the plot horizontally; it sits above/below the point.
        let tx = Math.max(plot.x, Math.min(px - bb.width / 2, plot.x + plot.width + theme.chart.plot_margin.right - bb.width - 8));
        let ty = below ? py + T.offset : py - T.offset - boxH;
        ty = Math.max(plot.y - 20, Math.min(ty, plot.y + plot.height - boxH));
        tx += +a.dx || 0;
        ty += +a.dy || 0;
        text.attr("transform", `translate(${tx},${ty + T.size * 0.85})`);
        const ly1 = below ? ty - 8 : ty + boxH + 4;
        g.insert("line", "text").attr("x1", px).attr("x2", px)
          .attr("y1", below ? py + 16 : py - 16).attr("y2", ly1)
          .attr("stroke", T.leader_color).attr("stroke-width", T.leader_width);
        g.append("circle").attr("cx", px).attr("cy", py).attr("r", 7).attr("fill", T.leader_color);
        ctx.annotations.push({ id: a.id, g });
      }
    },

    compile(cue, ctx) {
      const p = cue.params || {}, t = cue.t, tl = ctx.tl;
      const fade = ctx.theme.motion.fade;
      switch (cue.action) {
        case "draw_line": {
          const to = ctx.xpos(p.to || "end");
          tl.set("op." + p.series, t, 1);
          // Start from the first data point, not from "nothing drawn yet" left of the axis.
          const from = Math.max(tl.value("head." + p.series, t), ctx.xmin);
          tl.tween("head." + p.series, t, +p.duration || 2, to, p.ease || "cubic-in-out", 0, from);
          return true;
        }
        case "reveal_line":
          tl.set("head." + p.series, t, ctx.xmax);
          tl.tween("op." + p.series, t, +p.duration || fade, 1, "cubic-in-out", 1, 0);
          return true;
        case "hide_line":
          tl.tween("op." + p.series, t, +p.duration || fade, 0);
          return true;
        case "highlight_point": {
          const key = `${p.series}|${p.x}`;
          const period = ctx.theme.motion.pulse_period;
          const pulses = p.pulses == null ? 2 : +p.pulses;
          ctx.highlights.set(key, { series: p.series, x: p.x, label: p.label !== false });
          tl.tween("hl." + key, t, 0.3, 1, "cubic-out");
          tl.set("pulse." + key, t, 0);
          if (pulses > 0) tl.tween("pulse." + key, t, pulses * period, pulses, "linear");
          return true;
        }
        case "clear_highlights":
          for (const key of ctx.highlights.keys()) tl.tween("hl." + key, t, +p.duration || fade, 0);
          return true;
      }
      return false;
    },

    render(t, ctx, chartOp) {
      const { tl, theme } = ctx;
      const C = theme.chart;
      ctx.plotLayer.attr("opacity", chartOp);
      const labels = [];
      for (const s of ctx.series) {
        const head = tl.value("head." + s.key, t);
        const op = tl.value("op." + s.key, t);
        const pts = this.linePoints ? this.linePoints(s, head, t, ctx) : partial(s.points, head);
        const visible = pts.length > 1 && op > 0;
        s.path.attr("d", visible ? ctx.lineGen(pts) : null).attr("opacity", op);
        const tip = visible ? pts[pts.length - 1] : null;
        const showTip = tip && tip.y != null && ctx.config.options?.show_dots !== false;
        s.dot.attr("opacity", showTip ? op : 0);
        if (tip && tip.y != null) s.dot.attr("cx", ctx.x(tip.xp)).attr("cy", ctx.y(tip.y));
        if (tip && tip.y != null && ctx.config.options?.end_labels !== false) {
          labels.push({ s, x: ctx.x(tip.xp) + C.end_label.gap, y: ctx.y(tip.y), op });
        } else {
          s.label.attr("opacity", 0);
        }
      }
      this.placeLabels(labels, ctx);

      for (const a of ctx.annotations) a.g.attr("opacity", tl.value("ann." + a.id, t));

      // Highlights
      const H = C.highlight;
      const hls = ctx.hlLayer.selectAll("g.hl").data([...ctx.highlights.entries()], d => d[0]);
      const enter = hls.enter().append("g").attr("class", "hl");
      enter.append("circle").attr("class", "ring").attr("fill", "none");
      enter.append("circle").attr("class", "dot");
      enter.append("text").attr("class", "val").attr("text-anchor", "middle")
        .style("font-family", theme.fonts.numeric).style("font-size", H.label_size + "px")
        .style("font-weight", H.label_weight).attr("fill", C.title.color)
        .attr("stroke", C.background).attr("stroke-width", 10).attr("paint-order", "stroke");
      enter.merge(hls).each(function ([key, h]) {
        const g = d3.select(this);
        const vis = tl.value("hl." + key, t);
        const s = ctx.seriesByKey[h.series];
        const xp = ctx.xpos(h.x);
        const yv = s ? valueAt(s.points, xp) : null;
        if (!vis || yv == null) { g.attr("opacity", 0); return; }
        const cx = ctx.x(xp), cy = ctx.y(yv);
        const pulse = tl.value("pulse." + key, t);
        const total = Math.round(pulse) === pulse ? 0 : 1; // between pulses?
        const f = pulse - Math.floor(pulse);
        g.attr("opacity", vis);
        g.select(".dot").attr("cx", cx).attr("cy", cy).attr("r", H.radius * (0.6 + 0.4 * vis))
          .attr("fill", s.color).attr("stroke", C.background).attr("stroke-width", 4);
        g.select(".ring").attr("cx", cx).attr("cy", cy)
          .attr("r", H.radius + (H.ring_radius - H.radius) * f)
          .attr("stroke", s.color).attr("stroke-width", H.ring_width)
          .attr("opacity", total ? 1 - f : 0);
        const above = cy - H.ring_radius - 6 > ctx.plot.y;
        // If the line's tip (and its name label) sits on this point, put the value to the left.
        const atTip = Math.abs(tl.value("head." + h.series, t) - xp) < 1e-6;
        g.select(".val")
          .attr("x", atTip ? cx - H.radius - 14 : cx)
          .attr("text-anchor", atTip ? "end" : "middle")
          .attr("y", atTip ? cy - H.radius : above ? cy - H.radius - 22 : cy + H.radius + H.label_size + 14)
          .attr("opacity", h.label ? 1 : 0)
          .text(ctx.fmtValue(yv));
      });
    },

    /** Direct labels at line tips, nudged apart vertically so they never overlap. */
    placeLabels(labels, ctx) {
      const size = ctx.theme.chart.end_label.size * 1.1;
      labels.sort((a, b) => a.y - b.y);
      const heights = labels.map(l => l.s.labelLines * size);
      for (let pass = 0; pass < 8; pass++) {
        for (let i = 1; i < labels.length; i++) {
          const a = labels[i - 1], b = labels[i];
          const minGap = (heights[i - 1] + heights[i]) / 2 + 6;
          const overlap = minGap - (b.y - a.y);
          if (overlap > 0) { a.y -= overlap / 2; b.y += overlap / 2; }
        }
      }
      labels.forEach((l, i) => {
        const top = l.y - heights[i] / 2 + size * 0.8;
        l.s.label.attr("opacity", l.op).attr("transform", `translate(${l.x},${top})`);
      });
    },
  };

  window.Decoder.LineBase = LineBase;
  window.Decoder.registerTemplate("line_draw", Object.create(LineBase));
})();
