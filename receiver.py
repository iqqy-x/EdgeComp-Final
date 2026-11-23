"""Local session-based data logger for ESP32/MPU6050.

Run on the laptop connected to the same trusted Wi-Fi as the ESP32.
"""

import csv
import json
import math
import os
from datetime import datetime
from pathlib import Path
from threading import RLock

from flask import Flask, jsonify, redirect, render_template_string, request, url_for

app = Flask(__name__)
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "dataset"
RAW_DIR = DATA_DIR / "raw"
STATE_PATH = DATA_DIR / "manifest.json"
FINAL_CSV = DATA_DIR / "dataset_final.csv"
COLUMNS = [
    "timestamp_ms", "subjek", "sesi", "posisi", "ax", "ay", "az",
    "gx", "gy", "gz", "label",
]
LABELS = ("diam", "jalan", "lari")
SAMPLE_RATE = 50
DURATION_SECONDS = 60
EXPECTED_SAMPLES = SAMPLE_RATE * DURATION_SECONDS
MAX_BATCH = 100
LOCK = RLock()


def now_string():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def persist_state():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, STATE_PATH)


def load_state():
    if STATE_PATH.exists():
        with STATE_PATH.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
        return loaded
    return {
        "config": {"subjek": "S01", "sesi": 1, "posisi": "pinggang"},
        "active_id": None,
        "sessions": [],
    }


def lookup(session_id):
    return next((s for s in state["sessions"] if s["id"] == session_id), None)


def partial_path(session):
    return RAW_DIR / session["subjek"] / (session["id"] + ".csv.part")


def final_path(session):
    return RAW_DIR / session["subjek"] / (session["id"] + ".csv")


def reconcile_active_session():
    active_id = state["active_id"]
    if not active_id:
        return
    session = lookup(active_id)
    if not session or session["status"] != "recording":
        state["active_id"] = None
        persist_state()
        return
    path = partial_path(session)
    if not path.exists():
        session["status"] = "interrupted"
        state["active_id"] = None
        persist_state()
        return
    with path.open("r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
    session["received"] = len(rows)
    session["last_timestamp_ms"] = int(rows[-1]["timestamp_ms"]) if rows else -1
    persist_state()


state = load_state()
reconcile_active_session()


def fail(message, status=400):
    return jsonify(error=message), status


@app.get("/health")
def health():
    return jsonify(status="online", service="ESP32 Data Logger")


@app.get("/api/config")
def device_config():
    with LOCK:
        return jsonify(
            **state["config"],
            duration_seconds=DURATION_SECONDS,
            sample_rate_hz=SAMPLE_RATE,
            expected_samples=EXPECTED_SAMPLES,
            active=state["active_id"] is not None,
        )


@app.post("/api/start")
def start_session():
    payload = request.get_json(silent=True) or {}
    label = payload.get("label")
    if label not in LABELS:
        return fail("Label harus diam, jalan, atau lari")

    with LOCK:
        if state["active_id"] is not None:
            return fail("Masih ada sesi aktif; batalkan di dashboard dahulu", 409)

        config = state["config"]
        subject = config["subjek"]
        number = config["sesi"]
        attempts = [
            s["attempt"] for s in state["sessions"]
            if s["subjek"] == subject and s["sesi"] == number
        ]
        attempt = max(attempts, default=0) + 1
        session_id = f"{subject}_sesi{number:02d}_attempt{attempt:02d}"

        session = {
            "id": session_id,
            "subjek": subject,
            "sesi": number,
            "attempt": attempt,
            "posisi": config["posisi"],
            "label": label,
            "status": "recording",
            "received": 0,
            "last_timestamp_ms": -1,
            "created_at": now_string(),
        }
        path = partial_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or final_path(session).exists():
            return fail("Nama file sudah ada; periksa manifest", 409)

        with path.open("x", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(COLUMNS)

        state["sessions"].append(session)
        state["active_id"] = session_id
        persist_state()
        print(f"START {session_id} / {label}", flush=True)
        return jsonify(session_id=session_id, expected_samples=EXPECTED_SAMPLES), 201


@app.post("/api/batch")
def receive_batch():
    payload = request.get_json(silent=True) or {}
    session_id = payload.get("session_id")
    index = payload.get("start_index")
    samples = payload.get("samples")

    if not isinstance(session_id, str) or type(index) is not int:
        return fail("Session ID atau start_index tidak valid")
    if not isinstance(samples, list) or not 1 <= len(samples) <= MAX_BATCH:
        return fail("Batch harus berisi 1 sampai 100 sampel")

    with LOCK:
        session = lookup(session_id)
        if session and session["status"] == "pending_review":
            if 0 <= index and index + len(samples) <= session["received"]:
                return jsonify(received=session["received"], duplicate=True)
        if state["active_id"] != session_id:
            return fail("Sesi tidak aktif", 409)
        session = lookup(session_id)
        current = session["received"]
        if index < current and index + len(samples) <= current:
            return jsonify(received=current, duplicate=True)
        if index != current or current + len(samples) > EXPECTED_SAMPLES:
            return fail(f"Urutan salah; indeks yang diharapkan {current}", 409)

        rows = []
        previous_ts = session["last_timestamp_ms"]
        for sample in samples:
            if not isinstance(sample, list) or len(sample) != 7:
                return fail("Setiap sampel harus berisi 7 angka")
            stamp = sample[0]
            if type(stamp) is not int or not previous_ts < stamp <= 120000:
                return fail("timestamp_ms harus meningkat")
            try:
                values = [float(number) for number in sample[1:]]
            except (ValueError, TypeError, OverflowError):
                return fail("Nilai sensor bukan angka")
            if not all(math.isfinite(value) for value in values):
                return fail("Nilai sensor harus finite")
            rows.append([
                stamp, session["subjek"], session["sesi"],
                session["posisi"], *values, session["label"],
            ])
            previous_ts = stamp

        try:
            with partial_path(session).open("a", newline="", encoding="utf-8") as file:
                csv.writer(file).writerows(rows)
                file.flush()
                os.fsync(file.fileno())
        except OSError:
            app.logger.exception("Failed writing sensor batch")
            return fail("Gagal menyimpan batch ke disk", 500)

        session["received"] += len(rows)
        session["last_timestamp_ms"] = previous_ts
        persist_state()
        return jsonify(received=session["received"])


def compute_quality(path):
    with path.open("r", newline="", encoding="utf-8") as file:
        timestamps = [int(row["timestamp_ms"]) for row in csv.DictReader(file)]
    intervals = [b - a for a, b in zip(timestamps, timestamps[1:])]
    if not intervals:
        return {"mean_interval_ms": None, "max_interval_ms": None, "out_of_range_intervals": 0}
    return {
        "mean_interval_ms": round(sum(intervals) / len(intervals), 2),
        "max_interval_ms": max(intervals),
        "out_of_range_intervals": sum(not 15 <= dt <= 25 for dt in intervals),
    }


@app.post("/api/finish")
def finish_session():
    payload = request.get_json(silent=True) or {}
    session_id = payload.get("session_id")
    with LOCK:
        session = lookup(session_id)
        if session and session["status"] == "pending_review":
            return jsonify(status="saved", session_id=session_id, samples=session["received"])
        if not session or state["active_id"] != session_id:
            return fail("Sesi tidak aktif", 409)
        if session["received"] != EXPECTED_SAMPLES:
            return fail(f"Baru masuk {session['received']} dari {EXPECTED_SAMPLES} sampel", 409)
        path = partial_path(session)
        quality = compute_quality(path)
        try:
            path.rename(final_path(session))
        except OSError:
            app.logger.exception("Failed finalizing recording")
            return fail("Gagal menyelesaikan CSV", 500)
        session.update(quality)
        session["status"] = "pending_review"
        session["finished_at"] = now_string()
        state["active_id"] = None
        persist_state()
        print(f"SAVED {session_id}: {session['received']} samples", flush=True)
        return jsonify(status="saved", session_id=session_id, samples=session["received"], quality=quality)


def cancel_active(reason="aborted"):
    active_id = state["active_id"]
    if not active_id:
        return False
    session = lookup(active_id)
    session["status"] = reason
    session["finished_at"] = now_string()
    state["active_id"] = None
    persist_state()
    return True


@app.post("/api/abort")
def abort_session():
    payload = request.get_json(silent=True) or {}
    with LOCK:
        if payload.get("session_id") != state["active_id"]:
            return fail("Sesi tidak aktif", 409)
        cancel_active()
        return jsonify(status="aborted")


PAGE = """<!doctype html>
<html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ESP32 Data Logger</title><style>
body{font:15px/1.55 system-ui,Segoe UI,sans-serif;max-width:1050px;margin:30px auto;padding:0 18px;color:#20232a;background:#f5f6f8}
section{background:white;padding:18px 22px;border:1px solid #e1e4e8;border-radius:9px;margin:16px 0}
h1{margin-bottom:4px} p{margin:6px 0 14px} label{margin-right:18px}select,input,button{padding:9px;margin:5px 4px 5px 0}
button{cursor:pointer;background:#1b5978;color:#fff;border:0;border-radius:5px}button.warn{background:#994b26}button.secondary{background:#555e67}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:9px 7px;border-bottom:1px solid #ddd;text-align:left}table button{font-size:12px;padding:5px 8px}
.info{background:#edf6ff;padding:10px;border-radius:5px}.muted{color:#606a76}.scroll{overflow-x:auto}
</style></head><body>
<h1>ESP32 MPU6050 Data Logger</h1>
<p class="muted">Pilih subjek dan nomor sesi di sini. Pilih label aktivitas dengan tombol SW2 pada ESP32.</p>
<section><h2>Pengaturan rekaman berikutnya</h2>
<form method="post" action="{{ url_for('settings') }}">
<label>Subjek <select name="subjek">{% for s in subjects %}<option value="{{s}}" {% if s==config.subjek %}selected{% endif %}>{{s}}</option>{% endfor %}</select></label>
<label>Sesi <input type="number" min="1" max="99" name="sesi" value="{{config.sesi}}" style="width:65px"></label>
<label>Posisi <input value="{{config.posisi}}" disabled style="width:90px"></label>
<button type="submit" {% if active %}disabled{% endif %}>Simpan pengaturan</button>
</form>
<p class="info">Durasi: {{duration}} detik, sampling: 50 Hz, target: 3000 sampel per sesi. Status: <b>{{'SEDANG MEREKAM/MENGIRIM' if active else 'SIAP'}}</b>.</p>
{% if active %}<form method="post" action="{{url_for('reset_active')}}" onsubmit="return confirm('Batalkan sesi aktif? Hanya lakukan jika ESP32 sudah berhenti atau gagal.')">
<button class="warn" type="submit">Batalkan sesi aktif yang macet</button></form>{% endif %}
<p class="muted">Setelah merekam, klik Muat ulang untuk melihat file, lalu Terima atau Tolak. Sesi yang ditolak bisa diulang dengan nomor yang sama.</p>
<form method="get"><button class="secondary">Muat ulang</button></form>
</section>
<section><h2>Rekaman yang tersimpan</h2><div class="scroll"><table><thead><tr><th>File / ID</th><th>Label</th><th>Status</th><th>Sampel</th><th>Rata-rata Δt</th><th>Interval di luar 15–25 ms</th><th>Tindakan</th></tr></thead><tbody>
{% for s in sessions %}<tr><td>{{s.id}}</td><td>{{s.label}}</td><td>{{s.status}}</td><td>{{s.received}}</td><td>{{s.get('mean_interval_ms', '-')}}</td><td>{{s.get('out_of_range_intervals', '-')}}</td><td>
{% if s.status in ('pending_review','rejected') %}<form style="display:inline" method="post" action="{{url_for('review',session_id=s.id,action='accept')}}"><button>Terima</button></form>{% endif %}
{% if s.status in ('pending_review','accepted') %}<form style="display:inline" method="post" action="{{url_for('review',session_id=s.id,action='reject')}}"><button class="warn">Tolak</button></form>{% endif %}
</td></tr>{% else %}<tr><td colspan="7">Belum ada sesi.</td></tr>{% endfor %}
</tbody></table></div></section>
<section><h2>Dataset final</h2><p>Total sesi diterima: <b>{{accepted}}</b>. Hanya sesi diterima yang akan digabung.</p>
<form method="post" action="{{url_for('merge_dataset')}}"><button>Buat / Perbarui dataset_final.csv</button></form>
{% if final_exists %}<a href="{{url_for('download_dataset')}}">Unduh dataset_final.csv</a>{% endif %}
</section>
</body></html>"""


@app.get("/")
def dashboard():
    with LOCK:
        return render_template_string(
            PAGE,
            config=state["config"],
            subjects=[f"S{i:02d}" for i in range(1, 6)],
            duration=DURATION_SECONDS,
            sessions=list(reversed(state["sessions"])),
            active=state["active_id"] is not None,
            accepted=sum(s["status"] == "accepted" for s in state["sessions"]),
            final_exists=FINAL_CSV.exists(),
        )


@app.post("/settings")
def settings():
    subject = request.form.get("subjek", "")
    number_text = request.form.get("sesi", "")
    if subject not in [f"S{i:02d}" for i in range(1, 6)]:
        return fail("Subjek tidak tersedia")
    try:
        number = int(number_text)
    except ValueError:
        return fail("Sesi harus berupa angka")
    if not 1 <= number <= 99:
        return fail("Sesi harus 1 sampai 99")
    with LOCK:
        if state["active_id"] is not None:
            return fail("Tidak dapat mengubah pengaturan saat sesi aktif", 409)
        state["config"] = {"subjek": subject, "sesi": number, "posisi": "pinggang"}
        persist_state()
    return redirect(url_for("dashboard"), code=303)


@app.post("/sessions/<session_id>/<action>")
def review(session_id, action):
    if action not in ("accept", "reject"):
        return fail("Aksi tidak valid", 404)
    with LOCK:
        session = lookup(session_id)
        if not session or session["status"] not in ("pending_review", "accepted", "rejected"):
            return fail("Sesi belum dapat dinilai", 409)
        if action == "accept":
            if not final_path(session).exists():
                return fail("CSV sesi tidak ditemukan", 409)
            for other in state["sessions"]:
                if (other["id"] != session_id and other["status"] == "accepted"
                    and other["subjek"] == session["subjek"] and other["sesi"] == session["sesi"]):
                    other["status"] = "superseded"
            session["status"] = "accepted"
        else:
            session["status"] = "rejected"
        persist_state()
    return redirect(url_for("dashboard"), code=303)


@app.post("/reset-active")
def reset_active():
    with LOCK:
        cancel_active("interrupted")
    return redirect(url_for("dashboard"), code=303)


@app.post("/merge")
def merge_dataset():
    with LOCK:
        sessions = sorted(
            (s for s in state["sessions"] if s["status"] == "accepted"),
            key=lambda s: (s["subjek"], s["sesi"], s["attempt"]),
        )
        if not sessions:
            return fail("Belum ada sesi diterima", 409)
        for session in sessions:
            if not final_path(session).exists():
                return fail("Ada file CSV sesi yang hilang", 409)
        temp = FINAL_CSV.with_suffix(".csv.tmp")
        with temp.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=COLUMNS)
            writer.writeheader()
            for session in sessions:
                with final_path(session).open("r", newline="", encoding="utf-8") as source:
                    reader = csv.DictReader(source)
                    if reader.fieldnames != COLUMNS:
                        return fail(f"Format kolom salah: {session['id']}", 409)
                    for row in reader:
                        writer.writerow(row)
        os.replace(temp, FINAL_CSV)
    return redirect(url_for("dashboard"), code=303)


@app.get("/download")
def download_dataset():
    from flask import send_file
    if not FINAL_CSV.exists():
        return fail("Dataset final belum dibuat", 404)
    return send_file(FINAL_CSV, as_attachment=True, download_name="dataset_final.csv")


if __name__ == "__main__":
    print(f"Folder dataset: {DATA_DIR}")
    print("Dashboard: http://localhost:5000/")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=False)
