"""Background jobs (normalise video, transcribe, render) on one worker thread.

One worker is deliberate: Whisper and frame capture each use every core, so
running them side by side only makes both slower.
"""
import traceback
from concurrent.futures import ThreadPoolExecutor

from . import db

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="decoder-job")


def start(project_id: int, kind: str, fn) -> int:
    """fn(report) -> result dict. report(fraction, message) updates the job row."""
    jid = db.create_job(project_id, kind)

    def report(fraction, message=None):
        fields = {"progress": round(max(0.0, min(1.0, fraction)), 3)}
        if message:
            fields["message"] = message
        db.update_job(jid, **fields)

    def run():
        db.update_job(jid, status="running", message="Starting")
        try:
            result = fn(report)
            db.update_job(jid, status="done", progress=1.0, result=result or {},
                          message=(result or {}).get("message", "Done"))
        except Exception as e:  # report every failure to the UI
            traceback.print_exc()
            db.update_job(jid, status="error", message=str(e)[:2000])

    _pool.submit(run)
    return jid
