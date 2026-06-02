from __future__ import annotations

import pickle
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

HERE = Path(__file__).resolve().parent
FEEDER_CACHE = HERE / "dashboard2_feeder_cache.pkl"
CONSUMER_CACHE = HERE / "dashboard2_consumer_cache.pkl"
CACHE_VERSION = 2
START = datetime(2026, 4, 1, 0, 0, 0)
N_HOURS = 720
N_HALF = 1440

OUTAGE_XLSB = HERE / "Event Feeder Outage Data of April'26.xlsb"
BLOCKLOAD_XLSB = HERE / "FEEDER BLOCKLOAD DATA_April'26.xlsb"
KASHI_CSV = HERE / "kashi_april_2026_consumer_outage.csv"
PART_CSV = HERE / "part-00000-d7e6c5d1-d0e5-430d-8ac3-c995d369c557-c000.csv"


def _dt_outage(s: str) -> pd.Timestamp:
    return pd.to_datetime(s, format="%d-%m-%Y %H:%M:%S", errors="coerce")


def _parse_oracle_series(series: pd.Series) -> pd.Series:
    s = series.astype(str).str.strip()
    s = s.str.replace(r"\.\d{9} ", " ", regex=True)
    s = s.str.replace(r"(\d{2})\.(\d{2})\.(\d{2})", r"\1:\2:\3", regex=True)
    return pd.to_datetime(s, format="%d-%b-%y %I:%M:%S %p", errors="coerce")


def _slots_to_ivs(slots_sorted: list[int]) -> list[list[float]]:
    if not slots_sorted:
        return []
    slots_sorted = sorted(slots_sorted)
    ivs: list[list[float]] = []
    s = slots_sorted[0]
    for i in range(1, len(slots_sorted)):
        if slots_sorted[i] != slots_sorted[i - 1] + 1:
            ivs.append([round(s * 0.5, 3), round((slots_sorted[i - 1] + 1) * 0.5, 3)])
            s = slots_sorted[i]
    ivs.append([round(s * 0.5, 3), round((slots_sorted[-1] + 1) * 0.5, 3)])
    return ivs


def _bitmap_to_ivs(buf: bytes) -> list[list[float]]:
    if not buf:
        return []
    arr = np.frombuffer(buf, dtype=np.uint8)
    slots = np.flatnonzero(arr).tolist()
    return _slots_to_ivs(slots)


def _add_df_to_bitmaps(df: pd.DataFrame, bitmaps: dict[str, np.ndarray]) -> None:
    if df.empty:
        return
    sh = ((df["start"] - START).dt.total_seconds() / 3600).clip(0, N_HOURS)
    eh = ((df["end"] - START).dt.total_seconds() / 3600).clip(0, N_HOURS)
    valid = eh > sh
    if not valid.any():
        return
    work = df.loc[valid, ["device_id", "start", "end"]].copy()
    work["ss"] = (sh[valid] * 2).astype(np.int32).clip(0, N_HALF - 1).to_numpy()
    work["ee"] = (eh[valid] * 2).astype(np.int32).clip(0, N_HALF).to_numpy()
    for did, ss, ee in zip(work["device_id"].to_numpy(), work["ss"], work["ee"]):
        key = str(did)
        if key not in bitmaps:
            bitmaps[key] = np.zeros(N_HALF, dtype=np.uint8)
        if ee > ss:
            bitmaps[key][ss:ee] = 1


def _bitmaps_to_store(bitmaps: dict[str, np.ndarray]) -> dict[str, bytes]:
    return {k: v.tobytes() for k, v in bitmaps.items()}


def _add_slots_to_bitmaps(slots: np.ndarray, device_ids: np.ndarray, bitmaps: dict[str, np.ndarray]) -> None:
    for fid, slot in zip(device_ids, slots):
        key = str(fid)
        if key not in bitmaps:
            bitmaps[key] = np.zeros(N_HALF, dtype=np.uint8)
        s = int(slot)
        if 0 <= s < N_HALF:
            bitmaps[key][s] = 1


def _pct_from_outage_bm(o_store: dict[str, bytes], all_ids: set[str]) -> dict[str, float]:
    pct: dict[str, float] = {}
    for fid in all_ids:
        if fid in o_store:
            pct[fid] = float(np.frombuffer(o_store[fid], dtype=np.uint8).sum()) / (2 * N_HOURS)
        else:
            pct[fid] = 0.0
    return pct


def _load_feeders_from_xlsb() -> dict[str, Any]:
    """Build compact bitmap cache: outage xlsb (red) + blockload xlsb (green)."""
    outage_bm: dict[str, np.ndarray] = {}

    print("[dashboard2]   feeder outage xlsb …", flush=True)
    df = pd.read_excel(OUTAGE_XLSB, engine="pyxlsb", sheet_name=0)
    df = df.rename(columns={"Meter No": "device_id", "Occurrence Date Time": "occu", "Restoration Date Time": "resto"})
    df["device_id"] = df["device_id"].astype(str).str.strip()
    df["start"] = df["occu"].apply(_dt_outage)
    df["end"] = df["resto"].apply(_dt_outage)
    df = df.dropna(subset=["device_id", "start", "end"])
    df["duration_min"] = (df["end"] - df["start"]).dt.total_seconds() / 60
    df = df[df["duration_min"] > 0].copy()
    _add_df_to_bitmaps(df[["device_id", "start", "end"]], outage_bm)
    feeder_ids = set(outage_bm.keys())

    cz_bm: dict[str, np.ndarray] = {}
    usecols = ["Device Id", "Current R", "Current Y", "Current B", "RTC DTTM"]
    for sheet in ["Sheet1", "Sheet2", "Sheet3"]:
        print(f"[dashboard2]   blockload {sheet} …", flush=True)
        bl = pd.read_excel(BLOCKLOAD_XLSB, engine="pyxlsb", sheet_name=sheet, usecols=usecols)
        bl["Device Id"] = bl["Device Id"].astype(str).str.strip()
        bl = bl[bl["Device Id"].isin(feeder_ids)].copy()
        if bl.empty:
            continue
        bl["RTC DTTM"] = pd.to_datetime(bl["RTC DTTM"], errors="coerce")
        bl = bl.dropna(subset=["RTC DTTM", "Device Id"])
        bl["slot"] = ((bl["RTC DTTM"] - START).dt.total_seconds() / 1800).astype(int)
        bl = bl[(bl["slot"] >= 0) & (bl["slot"] < N_HALF)].copy()
        cz = (bl["Current R"] == 0) & (bl["Current Y"] == 0) & (bl["Current B"] == 0)
        bl = bl[cz]
        if bl.empty:
            continue
        _add_slots_to_bitmaps(
            bl["slot"].to_numpy(dtype=np.int32),
            bl["Device Id"].to_numpy(),
            cz_bm,
        )

    o_store = _bitmaps_to_store(outage_bm)
    c_store = _bitmaps_to_store(cz_bm)
    all_ids = set(o_store) | set(c_store)

    return {
        "kind": "feeder",
        "version": CACHE_VERSION,
        "outage_bm": o_store,
        "cz_bm": c_store,
        "outage_pct": _pct_from_outage_bm(o_store, all_ids),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _feeders_runtime_view(blob: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "feeder",
        "version": blob.get("version", CACHE_VERSION),
        "outage_bm": blob["outage_bm"],
        "cz_bm": blob["cz_bm"],
        "outage_pct": blob["outage_pct"],
        "generated_at": blob.get("generated_at"),
    }


def _feeder_ivs(feeders: dict[str, Any], fid: str) -> tuple[list, list]:
    o_bm = feeders["outage_bm"].get(fid, b"")
    c_bm = feeders["cz_bm"].get(fid, b"")
    return _bitmap_to_ivs(o_bm), _bitmap_to_ivs(c_bm)


def _load_consumers_from_csv() -> dict[str, Any]:
    """Build compact bitmap cache from kashi (red) + part (green)."""
    outage_bm: dict[str, np.ndarray] = {}
    current_bm: dict[str, np.ndarray] = {}

    if KASHI_CSV.exists():
        print("[dashboard2]   kashi outage CSV …", flush=True)
        k = pd.read_csv(KASHI_CSV, usecols=["DEVICE_ID_clean", "OCCU", "RESTO"])
        k = k.rename(columns={"DEVICE_ID_clean": "device_id", "OCCU": "occu", "RESTO": "resto"})
        k["device_id"] = k["device_id"].astype(str).str.strip()
        k["start"] = _parse_oracle_series(k["occu"])
        k["end"] = _parse_oracle_series(k["resto"])
        k = k.dropna(subset=["device_id", "start", "end"])
        _add_df_to_bitmaps(k[["device_id", "start", "end"]], outage_bm)

    if PART_CSV.exists():
        print("[dashboard2]   part current CSV (chunked) …", flush=True)
        for i, chunk in enumerate(
            pd.read_csv(PART_CSV, usecols=["device_id", "occurrence_time", "restoration_time"], chunksize=250_000)
        ):
            chunk["device_id"] = chunk["device_id"].astype(str).str.strip()
            chunk["start"] = pd.to_datetime(chunk["occurrence_time"], errors="coerce", utc=True).dt.tz_localize(None)
            chunk["end"] = pd.to_datetime(chunk["restoration_time"], errors="coerce", utc=True).dt.tz_localize(None)
            chunk = chunk.dropna(subset=["device_id", "start", "end"])
            _add_df_to_bitmaps(chunk[["device_id", "start", "end"]], current_bm)
            if i and i % 2 == 0:
                print(f"[dashboard2]   … chunk {i+1}", flush=True)

    o_store = _bitmaps_to_store(outage_bm)
    c_store = _bitmaps_to_store(current_bm)
    all_ids = set(o_store) | set(c_store)
    outage_pct = {}
    for cid in all_ids:
        if cid in o_store:
            outage_pct[cid] = float(np.frombuffer(o_store[cid], dtype=np.uint8).sum()) / (2 * N_HOURS)
        else:
            outage_pct[cid] = 0.0

    return {
        "kind": "consumer",
        "version": CACHE_VERSION,
        "outage_bm": o_store,
        "current_bm": c_store,
        "outage_pct": outage_pct,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _consumers_runtime_view(blob: dict[str, Any]) -> dict[str, Any]:
    """In-memory view with lazy interval expansion per device."""
    return {
        "kind": "consumer",
        "version": blob.get("version", CACHE_VERSION),
        "outage_bm": blob["outage_bm"],
        "current_bm": blob["current_bm"],
        "outage_pct": blob["outage_pct"],
        "generated_at": blob.get("generated_at"),
    }


def _consumer_ivs(consumers: dict[str, Any], cid: str) -> tuple[list, list]:
    o_bm = consumers["outage_bm"].get(cid, b"")
    c_bm = consumers["current_bm"].get(cid, b"")
    return _bitmap_to_ivs(o_bm), _bitmap_to_ivs(c_bm)


FEEDERS: dict[str, Any] | None = None
CONSUMERS: dict[str, Any] | None = None
FEEDER_ERROR: str | None = None
CONSUMER_ERROR: str | None = None
FEEDER_LOADING = False
CONSUMER_LOADING = False
_LOAD_LOCK = threading.Lock()
_BOOTSTRAP_DONE = False


def _feeder_mtime() -> float:
    return max((p.stat().st_mtime for p in (OUTAGE_XLSB, BLOCKLOAD_XLSB) if p.exists()), default=0.0)


def _consumer_mtime() -> float:
    return max((p.stat().st_mtime for p in (KASHI_CSV, PART_CSV) if p.exists()), default=0.0)


def _read_pickle(path: Path, min_mtime: float) -> dict | None:
    if not path.exists():
        return None
    try:
        if path.stat().st_mtime < min_mtime:
            return None
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception as e:
        print(f"[dashboard2] Cache read failed ({path.name}): {e}", flush=True)
        return None


def _load_feeder_cache() -> dict | None:
    blob = _read_pickle(FEEDER_CACHE, _feeder_mtime())
    if blob is None:
        return None
    # New bitmap cache (feeder)
    if blob.get("kind") == "feeder" and "cz_bm" in blob:
        return blob
    # Legacy interval cache — ignore and rebuild
    if "outage_ivs" in blob:
        print("[dashboard2] Legacy feeder cache ignored; rebuilding …", flush=True)
    return None


def _load_consumer_cache() -> dict | None:
    blob = _read_pickle(CONSUMER_CACHE, _consumer_mtime())
    if blob is None:
        return None
    if blob.get("kind") == "consumer" or "current_bm" in blob:
        return blob
    return None


def _save_pickle(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "wb") as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)
        mb = path.stat().st_size / (1024 * 1024)
        print(f"[dashboard2] Saved {path.name} ({mb:.1f} MB)", flush=True)
    except OSError as e:
        print(f"[dashboard2] Cache write failed ({path.name}): {e}", flush=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _feeder_thread() -> None:
    global FEEDERS, FEEDER_ERROR, FEEDER_LOADING
    try:
        print("[dashboard2] Building feeder cache (xlsb) …", flush=True)
        blob = _load_feeders_from_xlsb()
        FEEDERS = _feeders_runtime_view(blob)
        _save_pickle(FEEDER_CACHE, blob)
        print(
            f"[dashboard2] Feeders ready ({len(blob['outage_bm'])} outage, {len(blob['cz_bm'])} blockload).",
            flush=True,
        )
    except Exception as e:
        FEEDER_ERROR = f"{type(e).__name__}: {e}"
        print(f"[dashboard2] Feeder load failed: {e}", flush=True)
    finally:
        FEEDER_LOADING = False


def _consumer_thread() -> None:
    global CONSUMERS, CONSUMER_ERROR, CONSUMER_LOADING
    try:
        print("[dashboard2] Building consumer cache (kashi + part) …", flush=True)
        blob = _load_consumers_from_csv()
        CONSUMERS = _consumers_runtime_view(blob)
        _save_pickle(CONSUMER_CACHE, blob)
        n_o, n_c = len(blob["outage_bm"]), len(blob["current_bm"])
        print(f"[dashboard2] Consumers ready ({n_o} outage, {n_c} current).", flush=True)
    except Exception as e:
        CONSUMER_ERROR = f"{type(e).__name__}: {e}"
        print(f"[dashboard2] Consumer load failed: {e}", flush=True)
    finally:
        CONSUMER_LOADING = False


def ensure_load_started() -> None:
    global _BOOTSTRAP_DONE, FEEDER_LOADING, CONSUMER_LOADING, FEEDERS, CONSUMERS
    if _BOOTSTRAP_DONE:
        return
    _BOOTSTRAP_DONE = True

    fb = _load_feeder_cache()
    if fb:
        FEEDERS = _feeders_runtime_view(fb)
        print(f"[dashboard2] Feeders from cache ({len(fb['outage_bm'])} devices).", flush=True)

    cb = _load_consumer_cache()
    if cb:
        CONSUMERS = _consumers_runtime_view(cb)
        print(f"[dashboard2] Consumers from cache ({len(cb['outage_bm'])} devices).", flush=True)

    threads: list[threading.Thread] = []
    if FEEDERS is None and not FEEDER_LOADING:
        FEEDER_LOADING = True
        threads.append(threading.Thread(target=_feeder_thread, daemon=True))
    if CONSUMERS is None and not CONSUMER_LOADING:
        CONSUMER_LOADING = True
        threads.append(threading.Thread(target=_consumer_thread, daemon=True))
    for t in threads:
        t.start()


def _meta() -> dict[str, Any]:
    with _LOAD_LOCK:
        feeders_ready = FEEDERS is not None
        consumers_ready = CONSUMERS is not None
        return {
            "n_hours": N_HOURS,
            "n_half": N_HALF,
            "error": FEEDER_ERROR or CONSUMER_ERROR,
            "feeders_ready": feeders_ready,
            "consumers_ready": consumers_ready,
            "loading": not feeders_ready,
            "consumers_loading": not consumers_ready,
            "feeders": len(FEEDERS["outage_pct"]) if FEEDERS else 0,
            "feeders_with_blockload": len(FEEDERS["cz_bm"]) if FEEDERS else 0,
            "consumers": len(CONSUMERS["outage_pct"]) if CONSUMERS else 0,
            "consumers_with_current": len(CONSUMERS["current_bm"]) if CONSUMERS else 0,
            "generated_at": (FEEDERS or {}).get("generated_at") or (CONSUMERS or {}).get("generated_at"),
        }


app = FastAPI(title="Outage Dashboard 2 API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def _startup():
    ensure_load_started()


@app.get("/api/meta")
def meta():
    ensure_load_started()
    return _meta()


@app.get("/api/feeders")
def feeders(q: str = "", limit: int = Query(300, ge=1, le=2000), offset: int = Query(0, ge=0)):
    ensure_load_started()
    if FEEDERS is None:
        return {"total": 0, "items": [], "loading": True}
    items = [
        {"id": fid, "pct": float(FEEDERS["outage_pct"].get(fid, 0.0))}
        for fid in FEEDERS["outage_pct"]
        if (not q) or (q.lower() in fid.lower())
    ]
    items.sort(key=lambda x: x["pct"], reverse=True)
    return {"total": len(items), "items": items[offset : offset + limit], "loading": False}


@app.get("/api/feeder")
def feeder(id: str = Query(...)):
    ensure_load_started()
    if FEEDERS is None:
        raise HTTPException(status_code=503, detail="Feeder data still loading")
    fid = id.strip()
    if fid not in FEEDERS["outage_pct"]:
        raise HTTPException(status_code=404, detail="unknown feeder")
    out_ivs, cz_ivs = _feeder_ivs(FEEDERS, fid)
    return {"id": fid, "outage_ivs": out_ivs, "cz_ivs": cz_ivs}


@app.get("/api/consumers")
def consumers(q: str = "", limit: int = Query(300, ge=1, le=2000), offset: int = Query(0, ge=0)):
    ensure_load_started()
    if CONSUMERS is None:
        return {"total": 0, "items": [], "loading": True}
    items = [
        {"id": cid, "pct": float(CONSUMERS["outage_pct"].get(cid, 0.0))}
        for cid in CONSUMERS["outage_pct"]
        if (not q) or (q.lower() in cid.lower())
    ]
    items.sort(key=lambda x: x["pct"], reverse=True)
    return {"total": len(items), "items": items[offset : offset + limit], "loading": False}


@app.get("/api/consumer")
def consumer(id: str = Query(...)):
    ensure_load_started()
    if CONSUMERS is None:
        raise HTTPException(status_code=503, detail="Consumer data still loading")
    cid = id.strip()
    if cid not in CONSUMERS["outage_pct"]:
        raise HTTPException(status_code=404, detail="unknown consumer")
    out_ivs, cur_ivs = _consumer_ivs(CONSUMERS, cid)
    return {"id": cid, "outage_ivs": out_ivs, "cz_ivs": cur_ivs}


@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse((HERE / "outage_dashboard2.html").read_text(encoding="utf-8"))
