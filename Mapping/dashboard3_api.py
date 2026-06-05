"""
Dashboard 3 — Red dots only when outage AND current overlap (feeders + consumers).
Reuses dashboard2 cache pickles; overlap lists cached on disk for instant startup.
"""
from __future__ import annotations

import pickle
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

import dashboard2_api as d2

HERE = d2.HERE
FEEDER_OVERLAP_CACHE = HERE / "dashboard3_feeder_overlap.pkl"
CONSUMER_OVERLAP_CACHE = HERE / "dashboard3_consumer_overlap.pkl"
VERIFY_MAP_PATH = HERE / "verify_feeder_consumer_map.pkl"
OVERLAP_CACHE_VERSION = 1
N_HOURS = d2.N_HOURS
N_HALF = d2.N_HALF

_FEEDER_OVERLAP_PCT: dict[str, float] | None = None
_CONSUMER_OVERLAP_PCT: dict[str, float] | None = None
_CONSUMER_OVERLAP_LOADING = False
_FEEDER_OVERLAP_LOADING = False
_VERIFY_MAP: dict[str, Any] | None = None


def _load_verify_map() -> dict[str, Any]:
    global _VERIFY_MAP
    if _VERIFY_MAP is not None:
        return _VERIFY_MAP
    if VERIFY_MAP_PATH.exists():
        try:
            with open(VERIFY_MAP_PATH, "rb") as f:
                _VERIFY_MAP = pickle.load(f)
            print(f"[dashboard3] Verify map loaded ({len(_VERIFY_MAP)} feeders).", flush=True)
        except Exception as e:
            print(f"[dashboard3] Verify map read failed: {e}", flush=True)
            _VERIFY_MAP = {}
    else:
        _VERIFY_MAP = {}
    return _VERIFY_MAP


def _event_to_ivs(start_iso: str, end_iso: str) -> list[list[float]]:
    us = datetime.fromisoformat(start_iso)
    ue = datetime.fromisoformat(end_iso)
    sh = max(0.0, (us - d2.START).total_seconds() / 3600)
    eh = min(float(N_HOURS), (ue - d2.START).total_seconds() / 3600)
    if eh <= sh:
        return []
    return [[sh, eh]]


def _overlap_bitmap(o_buf: bytes, c_buf: bytes) -> np.ndarray:
    o = np.zeros(N_HALF, dtype=np.uint8)
    c = np.zeros(N_HALF, dtype=np.uint8)
    if o_buf:
        arr = np.frombuffer(o_buf, dtype=np.uint8)
        o[: min(len(arr), N_HALF)] = arr[: min(len(arr), N_HALF)]
    if c_buf:
        arr = np.frombuffer(c_buf, dtype=np.uint8)
        c[: min(len(arr), N_HALF)] = arr[: min(len(arr), N_HALF)]
    return o & c


def _overlap_ivs_from_store(
    outage_bm: dict[str, bytes],
    current_bm: dict[str, bytes],
    device_id: str,
) -> list[list[float]]:
    overlap = _overlap_bitmap(outage_bm.get(device_id, b""), current_bm.get(device_id, b""))
    return d2._bitmap_to_ivs(overlap.tobytes())


def _compute_overlap_pct_matched_only(
    outage_bm: dict[str, bytes],
    current_bm: dict[str, bytes],
) -> dict[str, float]:
    """Only devices present in both maps; store only rows with overlap > 0."""
    ids = set(outage_bm.keys()) & set(current_bm.keys())
    pct: dict[str, float] = {}
    n = len(ids)
    for i, did in enumerate(ids):
        overlap = _overlap_bitmap(outage_bm[did], current_bm[did])
        v = float(overlap.sum()) / (2 * N_HOURS)
        if v > 0.001:
            pct[did] = v
        if n > 5000 and i and i % 10000 == 0:
            print(f"[dashboard3]   … overlap {i}/{n}", flush=True)
    return pct


def _feeder_cache_mtime() -> float:
    p = d2.FEEDER_CACHE
    return p.stat().st_mtime if p.exists() else d2._feeder_mtime()


def _consumer_cache_mtime() -> float:
    p = d2.CONSUMER_CACHE
    return p.stat().st_mtime if p.exists() else d2._consumer_mtime()


def _read_overlap_cache(path: Path, min_mtime: float) -> dict[str, float] | None:
    if not path.exists() or path.stat().st_mtime < min_mtime:
        return None
    try:
        with open(path, "rb") as f:
            blob = pickle.load(f)
        if blob.get("version") != OVERLAP_CACHE_VERSION:
            return None
        return blob.get("overlap_pct", {})
    except Exception as e:
        print(f"[dashboard3] Overlap cache read failed ({path.name}): {e}", flush=True)
        return None


def _save_overlap_cache(path: Path, overlap_pct: dict[str, float], label: str) -> None:
    tmp = path.with_suffix(".tmp")
    try:
        blob = {
            "version": OVERLAP_CACHE_VERSION,
            "overlap_pct": overlap_pct,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(tmp, "wb") as f:
            pickle.dump(blob, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)
        mb = path.stat().st_size / (1024 * 1024)
        print(f"[dashboard3] Saved {path.name} ({len(overlap_pct)} matches, {mb:.1f} MB)", flush=True)
    except OSError as e:
        print(f"[dashboard3] Overlap cache write failed ({path.name}): {e}", flush=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _build_feeder_overlap() -> dict[str, float]:
    print("[dashboard3] Computing feeder overlap …", flush=True)
    assert d2.FEEDERS is not None
    return _compute_overlap_pct_matched_only(d2.FEEDERS["outage_bm"], d2.FEEDERS["cz_bm"])


def _build_consumer_overlap() -> dict[str, float]:
    print("[dashboard3] Computing consumer overlap …", flush=True)
    assert d2.CONSUMERS is not None
    return _compute_overlap_pct_matched_only(d2.CONSUMERS["outage_bm"], d2.CONSUMERS["current_bm"])


def _feeder_overlap_thread() -> None:
    global _FEEDER_OVERLAP_PCT, _FEEDER_OVERLAP_LOADING
    try:
        pct = _build_feeder_overlap()
        _FEEDER_OVERLAP_PCT = pct
        _save_overlap_cache(FEEDER_OVERLAP_CACHE, pct, "feeder")
        print(f"[dashboard3] Feeder overlap ready ({len(pct)} with match).", flush=True)
    except Exception as e:
        print(f"[dashboard3] Feeder overlap failed: {e}", flush=True)
    finally:
        _FEEDER_OVERLAP_LOADING = False


def _consumer_overlap_thread() -> None:
    global _CONSUMER_OVERLAP_PCT, _CONSUMER_OVERLAP_LOADING
    try:
        pct = _build_consumer_overlap()
        _CONSUMER_OVERLAP_PCT = pct
        _save_overlap_cache(CONSUMER_OVERLAP_CACHE, pct, "consumer")
        print(f"[dashboard3] Consumer overlap ready ({len(pct)} with match).", flush=True)
    except Exception as e:
        print(f"[dashboard3] Consumer overlap failed: {e}", flush=True)
    finally:
        _CONSUMER_OVERLAP_LOADING = False


def _ensure_feeder_overlap_pct() -> None:
    global _FEEDER_OVERLAP_PCT, _FEEDER_OVERLAP_LOADING
    if _FEEDER_OVERLAP_PCT is not None:
        return
    d2.ensure_load_started()
    if d2.FEEDERS is None:
        return

    cached = _read_overlap_cache(FEEDER_OVERLAP_CACHE, _feeder_cache_mtime())
    if cached is not None:
        _FEEDER_OVERLAP_PCT = cached
        print(f"[dashboard3] Feeder overlap from cache ({len(cached)} matches).", flush=True)
        return

    if not _FEEDER_OVERLAP_LOADING:
        _FEEDER_OVERLAP_LOADING = True
        threading.Thread(target=_feeder_overlap_thread, daemon=True).start()


def _ensure_consumer_overlap_pct() -> None:
    global _CONSUMER_OVERLAP_PCT, _CONSUMER_OVERLAP_LOADING
    if _CONSUMER_OVERLAP_PCT is not None:
        return
    d2.ensure_load_started()
    if d2.CONSUMERS is None:
        return

    cached = _read_overlap_cache(CONSUMER_OVERLAP_CACHE, _consumer_cache_mtime())
    if cached is not None:
        _CONSUMER_OVERLAP_PCT = cached
        print(f"[dashboard3] Consumer overlap from cache ({len(cached)} matches).", flush=True)
        return

    if not _CONSUMER_OVERLAP_LOADING:
        _CONSUMER_OVERLAP_LOADING = True
        threading.Thread(target=_consumer_overlap_thread, daemon=True).start()


def _meta() -> dict[str, Any]:
    d2.ensure_load_started()
    _ensure_feeder_overlap_pct()
    _ensure_consumer_overlap_pct()
    verify_map = _load_verify_map()
    f_match = len(_FEEDER_OVERLAP_PCT) if _FEEDER_OVERLAP_PCT else 0
    c_match = len(_CONSUMER_OVERLAP_PCT) if _CONSUMER_OVERLAP_PCT else 0
    return {
        "n_hours": N_HOURS,
        "n_half": N_HALF,
        "error": d2.FEEDER_ERROR or d2.CONSUMER_ERROR,
        "feeders_ready": d2.FEEDERS is not None,
        "consumers_ready": d2.CONSUMERS is not None,
        "feeders_overlap_ready": _FEEDER_OVERLAP_PCT is not None,
        "consumers_overlap_ready": _CONSUMER_OVERLAP_PCT is not None,
        "verify_ready": VERIFY_MAP_PATH.exists() and len(verify_map) > 0,
        "verify_feeders": len(verify_map),
        "loading": d2.FEEDERS is None,
        "consumers_loading": d2.CONSUMERS is None or _CONSUMER_OVERLAP_PCT is None,
        "feeders": len(d2.FEEDERS["outage_pct"]) if d2.FEEDERS else 0,
        "feeders_with_match": f_match,
        "consumers": len(d2.CONSUMERS["outage_pct"]) if d2.CONSUMERS else 0,
        "consumers_with_match": c_match,
        "match_only": True,
        "generated_at": (d2.FEEDERS or {}).get("generated_at") or (d2.CONSUMERS or {}).get("generated_at"),
    }


app = FastAPI(title="Outage Dashboard 3 API (match only)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def _startup():
    d2.ensure_load_started()
    _ensure_feeder_overlap_pct()
    _ensure_consumer_overlap_pct()
    _load_verify_map()


@app.get("/api/meta")
def meta():
    return _meta()


@app.get("/api/feeders")
def feeders(q: str = "", limit: int = Query(300, ge=1, le=2000), offset: int = Query(0, ge=0)):
    d2.ensure_load_started()
    _ensure_feeder_overlap_pct()
    if d2.FEEDERS is None or _FEEDER_OVERLAP_PCT is None:
        return {"total": 0, "items": [], "loading": True}
    items = [
        {"id": fid, "pct": float(_FEEDER_OVERLAP_PCT[fid])}
        for fid in _FEEDER_OVERLAP_PCT
        if (not q) or (q.lower() in fid.lower())
    ]
    items.sort(key=lambda x: x["pct"], reverse=True)
    return {"total": len(items), "items": items[offset : offset + limit], "loading": False}


@app.get("/api/feeder")
def feeder(id: str = Query(...)):
    d2.ensure_load_started()
    if d2.FEEDERS is None:
        raise HTTPException(status_code=503, detail="Feeder data still loading")
    fid = id.strip()
    if fid not in d2.FEEDERS["outage_pct"]:
        raise HTTPException(status_code=404, detail="unknown feeder")
    match_ivs = _overlap_ivs_from_store(d2.FEEDERS["outage_bm"], d2.FEEDERS["cz_bm"], fid)
    return {"id": fid, "outage_ivs": match_ivs, "cz_ivs": [], "match_only": True}


@app.get("/api/consumers")
def consumers(q: str = "", limit: int = Query(300, ge=1, le=2000), offset: int = Query(0, ge=0)):
    d2.ensure_load_started()
    _ensure_consumer_overlap_pct()
    if d2.CONSUMERS is None or _CONSUMER_OVERLAP_PCT is None:
        return {"total": 0, "items": [], "loading": True}
    items = [
        {"id": cid, "pct": float(_CONSUMER_OVERLAP_PCT[cid])}
        for cid in _CONSUMER_OVERLAP_PCT
        if (not q) or (q.lower() in cid.lower())
    ]
    items.sort(key=lambda x: x["pct"], reverse=True)
    return {"total": len(items), "items": items[offset : offset + limit], "loading": False}


@app.get("/api/consumer")
def consumer(id: str = Query(...)):
    d2.ensure_load_started()
    if d2.CONSUMERS is None:
        raise HTTPException(status_code=503, detail="Consumer data still loading")
    cid = id.strip()
    if cid not in d2.CONSUMERS["outage_pct"]:
        raise HTTPException(status_code=404, detail="unknown consumer")
    match_ivs = _overlap_ivs_from_store(
        d2.CONSUMERS["outage_bm"], d2.CONSUMERS["current_bm"], cid
    )
    return {"id": cid, "outage_ivs": match_ivs, "cz_ivs": [], "match_only": True}


@app.get("/api/verify/feeders")
def verify_feeders(q: str = "", limit: int = Query(500, ge=1, le=5000)):
    verify_map = _load_verify_map()
    items = [
        {
            "id": fid,
            "n_consumers": int(info.get("n_matched_consumers", 0)),
            "event_start": info.get("event_start"),
            "event_end": info.get("event_end"),
        }
        for fid, info in verify_map.items()
        if info.get("n_matched_consumers", 0) > 0 and ((not q) or (q.lower() in fid.lower()))
    ]
    items.sort(key=lambda x: (-x["n_consumers"], x["id"]))
    return {"total": len(items), "items": items[:limit], "ready": len(verify_map) > 0}


@app.get("/api/verify/feeder")
def verify_feeder(id: str = Query(...), limit: int = Query(50, ge=1, le=300)):
    d2.ensure_load_started()
    verify_map = _load_verify_map()
    fid = id.strip()
    if fid not in verify_map:
        raise HTTPException(
            status_code=404,
            detail="feeder not in verify map — run: python topology_mapping.py",
        )
    info = verify_map[fid]
    cons = info.get("consumers", [])[:limit]
    consumer_rows: list[dict[str, Any]] = []
    if d2.CONSUMERS is not None:
        for c in cons:
            cid = c["consumer_id"]
            if cid in d2.CONSUMERS["outage_pct"]:
                outage_ivs, _ = d2._consumer_ivs(d2.CONSUMERS, cid)
            else:
                outage_ivs = []
            consumer_rows.append(
                {
                    "id": cid,
                    "outage_ivs": outage_ivs,
                    "score": c.get("score"),
                    "overlap_ratio": c.get("overlap_ratio"),
                }
            )
    return {
        "id": fid,
        "event_start": info["event_start"],
        "event_end": info["event_end"],
        "duration_min": info.get("duration_min"),
        "n_matched_consumers": info.get("n_matched_consumers", len(info.get("consumers", []))),
        "feeder_event_ivs": _event_to_ivs(info["event_start"], info["event_end"]),
        "consumers": consumer_rows,
        "total_consumers": len(info.get("consumers", [])),
    }


@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse((HERE / "outage_dashboard3.html").read_text(encoding="utf-8"))
