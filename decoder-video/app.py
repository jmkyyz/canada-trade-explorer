"""Decoder video prototype: Flask app.

    python app.py            -> http://127.0.0.1:5055/
"""
import json
import os
import re
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, url_for

from decoder import captions, config, cues, db, jobs, media, render, segmentation, transcribe, transcript_edit
from decoder import templates as tpl
from decoder.csvdata import parse_csv
from decoder.spec import build_spec, chart_page
from decoder.theme import list_themes, load_theme

app = Flask(__name__, static_folder=str(config.STATIC_DIR), template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024**3  # phone video at high bitrate

SAMPLE_CSV = config.ROOT / "sample_data" / "canada_population_change.csv"
SAMPLE_CONFIG = config.ROOT / "sample_data" / "canada_population_change.config.json"

VIDEO_EXT = {".mov", ".mp4", ".m4v", ".webm", ".mkv"}


def err(msg, code=400):
    return jsonify({"error": msg}), code


def project_or_404(pid):
    p = db.get_project(pid)
    if not p:
        abort(404)
    return p


def project_payload(p):
    t = db.latest_transcript(p["id"])
    theme = load_theme(p["theme"])
    all_cues = db.list_cues(p["id"])
    timed = {c["id"]: c["t"] for c in cues.timed_cues(p["id"], t)}
    tmpl = tpl.get(p["template"])
    for c in all_cues:
        c["t"] = timed.get(c["id"])
        c["description"] = tmpl.describe_cue(c["action"], c["params"], p["config"])
    groups = captions.group_words(t["words"], theme["captions"]) if t else []
    return {
        "project": p,
        "transcript": t,
        "cues": sorted(all_cues, key=lambda c: (c["t"] if c["t"] is not None else 1e9, c["id"])),
        "captions": [{"start": g["start"], "end": g["end"],
                      "words": [{"text": w["text"], "start": w["start"]} for w in g["words"]]}
                     for g in groups],
        "manifest": tmpl.manifest(),
        "theme": theme,
        "jobs": {k: db.latest_job(p["id"], k) for k in ("video", "transcribe", "render")},
        "phase2_available": segmentation.available(),
    }


# ---------------------------------------------------------------- pages

@app.get("/")
def index():
    return render_template("index.html", projects=db.list_projects(), templates=tpl.manifests(),
                           themes=list_themes())


@app.get("/p/<int:pid>")
def editor(pid):
    p = project_or_404(pid)
    return render_template("editor.html", project=p)


@app.get("/p/<int:pid>/chart")
def chart(pid):
    """The exact page the renderer captures, served for the preview iframe."""
    p = project_or_404(pid)
    return chart_page(build_spec(p), "/static", p["theme"])


@app.get("/p/<int:pid>/files/<path:rel>")
def project_file(pid, rel):
    project_or_404(pid)
    return send_from_directory(config.project_dir(pid), rel,
                               as_attachment=request.args.get("download") == "1")


# ---------------------------------------------------------------- projects

@app.get("/api/templates")
def api_templates():
    return jsonify(tpl.manifests())


@app.post("/api/projects")
def api_create_project():
    f = request.form
    template = f.get("template", "line_draw")
    theme_name = f.get("theme") or config.DEFAULT_THEME
    try:
        T = tpl.get(template)
        theme = load_theme(theme_name)
    except (KeyError, FileNotFoundError) as e:
        return err(str(e))
    upload = request.files.get("csv")
    use_sample = f.get("sample") == "1" or not (upload and upload.filename)
    if use_sample:
        text, csv_name = SAMPLE_CSV.read_text(), SAMPLE_CSV.name
    else:
        text, csv_name = upload.read().decode("utf-8-sig"), upload.filename
    try:
        data = parse_csv(text)
    except ValueError as e:
        return err(f"CSV problem: {e}")
    cfg = T.default_config(data, theme)
    if use_sample and SAMPLE_CONFIG.exists():
        sample_cfg = json.loads(SAMPLE_CONFIG.read_text())
        cfg.update(sample_cfg.get("common", {}))
        cfg.update(sample_cfg.get(template, {}))
        cfg["series"] = cfg["series"][: T.max_series]
    name = f.get("name") or cfg.get("title") or "Untitled Decoder"
    pid = db.create_project(name, template, theme_name, cfg, data, csv_name)
    (config.project_dir(pid) / "data.csv").write_text(text)
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"id": pid})
    return redirect(url_for("editor", pid=pid))


@app.get("/api/projects/<int:pid>")
def api_project(pid):
    return jsonify(project_payload(project_or_404(pid)))


@app.delete("/api/projects/<int:pid>")
def api_delete_project(pid):
    project_or_404(pid)
    db.delete_project(pid)
    return jsonify({"ok": True})


@app.put("/api/projects/<int:pid>/config")
def api_config(pid):
    p = project_or_404(pid)
    body = request.get_json(force=True)
    cfg = body.get("config", body)
    T = tpl.get(p["template"])
    for i, a in enumerate(cfg.get("annotations", []), 1):
        a["id"] = int(a.get("id") or i)
    errors = T.validate_config(cfg, p["data"])
    if errors:
        return err("; ".join(errors))
    updates = {"config": cfg}
    if body.get("name"):
        updates["name"] = body["name"]
    db.update_project(pid, **updates)
    return jsonify(project_payload(db.get_project(pid)))


@app.put("/api/projects/<int:pid>/layout")
def api_layout(pid):
    p = project_or_404(pid)
    layout = {**p["layout"], **request.get_json(force=True)}
    db.update_project(pid, layout=layout)
    return jsonify({"layout": layout})


@app.post("/api/projects/<int:pid>/csv")
def api_replace_csv(pid):
    """Swap in new data (e.g. the real StatCan series) keeping the chart config."""
    p = project_or_404(pid)
    upload = request.files.get("csv")
    if not upload:
        return err("No CSV uploaded")
    text = upload.read().decode("utf-8-sig")
    try:
        data = parse_csv(text)
    except ValueError as e:
        return err(f"CSV problem: {e}")
    errors = tpl.get(p["template"]).validate_config(p["config"], data)
    if errors:
        return err("New CSV doesn't fit the current chart config: " + "; ".join(errors))
    db.update_project(pid, data=data, csv_name=upload.filename)
    (config.project_dir(pid) / "data.csv").write_text(text)
    return jsonify(project_payload(db.get_project(pid)))


# ---------------------------------------------------------------- video + transcript

def _vocab_prompt(p):
    c = p["config"]
    bits = [c.get("title"), c.get("subtitle")] + [s["name"] for s in c.get("series", [])]
    return " ".join(b for b in bits if b)[:400]


def _transcribe_job(pid):
    def run(report):
        p = db.get_project(pid)
        pdir = config.project_dir(pid)
        wav = pdir / "talent_16k.wav"
        report(0.02, "Extracting audio")
        media.extract_wav(pdir / p["video"]["master"], wav)
        report(0.05, f"Loading Whisper model ({config.WHISPER_MODEL}); first run downloads it")
        res = transcribe.transcribe(wav, _vocab_prompt(p),
                                    lambda f: report(0.05 + 0.9 * f, "Transcribing"),
                                    p["video"]["duration"])
        return _store_transcript(pid, res["words"], res["source"], res["language"])
    return run


def _store_transcript(pid, words, source, language):
    if not words:
        raise ValueError("No speech found in the video")
    had = db.latest_transcript(pid) is not None
    t = db.add_transcript(pid, words, source, language)
    counts = cues.reattach(pid, t) if had else {}
    msg = f"Transcribed {len(words)} words"
    if any(counts.values()):
        msg += (f". Cues re-attached: {counts['ok']} matched, {counts['moved']} moved,"
                f" {counts['orphaned']} need attention")
    return {"transcript_id": t["id"], "version": t["version"], "reattach": counts, "message": msg}


@app.post("/api/projects/<int:pid>/video")
def api_upload_video(pid):
    p = project_or_404(pid)
    upload = request.files.get("video")
    if not upload or not upload.filename:
        return err("No video uploaded")
    ext = Path(upload.filename).suffix.lower()
    if ext not in VIDEO_EXT:
        return err(f"Unsupported video type {ext}")
    pdir = config.project_dir(pid)
    original = pdir / f"talent_original{ext}"
    upload.save(original)
    auto_transcribe = request.form.get("transcribe", "1") == "1"
    theme = load_theme(p["theme"])
    canvas = theme["canvas"]

    def run(report):
        report(0.01, "Checking video")
        info = media.probe(original)
        if info["duration"] > config.MAX_VIDEO_SECONDS + 1:
            raise ValueError(f"Video is {info['duration']:.0f}s; the limit is "
                             f"{config.MAX_VIDEO_SECONDS}s")
        master = pdir / "talent_master.mp4"
        tmp = pdir / "talent_master.tmp.mp4"
        report(0.02, "Normalising video (H.264, 30fps, 1080x1920)")
        out = media.normalize(original, tmp, canvas["fps"], canvas["width"], canvas["height"],
                              lambda f: report(0.02 + 0.95 * f, "Normalising video"))
        os.replace(tmp, master)
        db.update_project(pid, video={
            "original": original.name, "uploaded_name": upload.filename, "master": master.name,
            "duration": out["duration"], "width": out["width"], "height": out["height"],
            "source": out["source"]})
        if auto_transcribe:
            jobs.start(pid, "transcribe", _transcribe_job(pid))
        return {"message": f"Video ready ({out['duration']:.1f}s)"}

    jid = jobs.start(pid, "video", run)
    return jsonify({"job": jid})


@app.post("/api/projects/<int:pid>/transcribe")
def api_transcribe(pid):
    p = project_or_404(pid)
    if not p["video"]:
        return err("Upload a video first")
    return jsonify({"job": jobs.start(pid, "transcribe", _transcribe_job(pid))})


@app.post("/api/projects/<int:pid>/transcript/import")
def api_import_transcript(pid):
    """Word timings from elsewhere: JSON list of {text|word, start, end}."""
    project_or_404(pid)
    body = request.get_json(force=True)
    words = body.get("words", body) if isinstance(body, dict) else body
    try:
        words = transcribe.clean_imported(words)
        res = _store_transcript(pid, words, "import", body.get("language", "en")
                                if isinstance(body, dict) else "en")
    except (ValueError, KeyError, TypeError) as e:
        return err(f"Transcript problem: {e}")
    return jsonify(res)


@app.post("/api/projects/<int:pid>/transcript/words/<int:i>")
def api_edit_word(pid, i):
    """Fix the transcript: {"op": "text", "text": "1.2"} renames (spaces split
    the word, empty deletes it), {"op": "merge"} joins with the next word,
    {"op": "delete"}. Timing is kept and cues stay on their words."""
    project_or_404(pid)
    b = request.get_json(force=True)
    try:
        return jsonify(transcript_edit.apply(pid, b.get("op", "text"), i, b.get("text", "")))
    except ValueError as e:
        return err(str(e))


# ---------------------------------------------------------------- cues

def _validate_cue(p, action, params):
    return tpl.get(p["template"]).validate_cue(action, params or {}, p["config"], p["data"])


@app.post("/api/projects/<int:pid>/cues")
def api_add_cue(pid):
    p = project_or_404(pid)
    t = db.latest_transcript(pid)
    if not t:
        return err("Transcribe first")
    b = request.get_json(force=True)
    i = int(b["word_index"])
    if not 0 <= i < len(t["words"]):
        return err("Word index out of range")
    params, errors = _validate_cue(p, b["action"], b.get("params"))
    if errors:
        return err("; ".join(errors))
    cid = db.add_cue(pid, t["id"], i, transcribe.norm(t["words"][i]["text"]), b["action"], params,
                     float(b.get("offset") or 0))
    return jsonify({"id": cid})


@app.put("/api/cues/<int:cid>")
def api_update_cue(cid):
    c = db.get_cue(cid)
    if not c:
        abort(404)
    pid = db.connect().execute("SELECT project_id FROM cues WHERE id=?", (cid,)).fetchone()[0]
    p = db.get_project(pid)
    b = request.get_json(force=True)
    fields = {}
    if "action" in b or "params" in b:
        params, errors = _validate_cue(p, b.get("action", c["action"]), b.get("params", c["params"]))
        if errors:
            return err("; ".join(errors))
        fields.update(action=b.get("action", c["action"]), params=params)
    if "offset" in b:
        fields["offset"] = float(b["offset"] or 0)
    if "word_index" in b:  # re-anchor (e.g. fixing an orphaned cue)
        t = db.latest_transcript(pid)
        i = int(b["word_index"])
        if not 0 <= i < len(t["words"]):
            return err("Word index out of range")
        fields.update(word_index=i, transcript_id=t["id"], status="ok",
                      anchor_word=transcribe.norm(t["words"][i]["text"]))
    db.update_cue(cid, **fields)
    return jsonify({"ok": True})


@app.delete("/api/cues/<int:cid>")
def api_delete_cue(cid):
    db.delete_cue(cid)
    return jsonify({"ok": True})


@app.get("/api/projects/<int:pid>/spec")
def api_spec(pid):
    return jsonify(build_spec(project_or_404(pid)))


@app.get("/api/projects/<int:pid>/cuesheet.<fmt>")
def api_cuesheet(pid, fmt):
    p = project_or_404(pid)
    t = db.latest_transcript(pid)
    fps = load_theme(p["theme"])["canvas"]["fps"]
    rows = cues.cue_sheet(p, t, fps) if t else []
    name = re.sub(r"[^\w-]+", "_", p["name"]).strip("_") or "decoder"
    if fmt == "csv":
        return cues.cue_sheet_csv(rows), 200, {
            "Content-Type": "text/csv",
            "Content-Disposition": f'attachment; filename="{name}_cue_sheet.csv"'}
    if fmt == "json":
        return jsonify({"project": p["name"], "fps": fps, "cues": rows})
    abort(404)


# ---------------------------------------------------------------- render + jobs

@app.post("/api/projects/<int:pid>/render")
def api_render(pid):
    project_or_404(pid)
    stages = {"capture": (0.0, 0.6), "overlay": (0.6, 0.75), "compose": (0.75, 0.99),
              "done": (1.0, 1.0)}

    def run(report):
        def say(stage, f, msg):
            a, b = stages.get(stage, (0, 1))
            report(a + (b - a) * f, msg)
        res = render.render_project(pid, say)
        res["message"] = (f"Rendered {res['frames']} frames; chart capture took "
                          f"{res['capture']['seconds']}s")
        return res

    return jsonify({"job": jobs.start(pid, "render", run)})


@app.get("/api/jobs/<int:jid>")
def api_job(jid):
    j = db.get_job(jid)
    if not j:
        abort(404)
    return jsonify(j)


@app.get("/api/projects/<int:pid>/renders")
def api_renders(pid):
    project_or_404(pid)
    out = []
    for f in sorted((config.project_dir(pid) / "renders").glob("*/render.json"), reverse=True):
        out.append(json.loads(f.read_text()))
    return jsonify(out)


db.init()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5055"))
    app.run(host="127.0.0.1", port=port, debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
