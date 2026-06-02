from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

HERE = Path(__file__).resolve().parent
START = datetime(2026, 4, 1, 0, 0, 0)
N_HOURS = 720
N_HALF = 1440


def _dt_outage(s: str) -> pd.Timestamp:
    return pd.to_datetime(s, format="%d-%m-%Y %H:%M:%S", errors="coerce")


def _parse_oracle_dt(s: str) -> pd.Timestamp:
    """
    Parse timestamps like:
      '01-APR-26 12.14.36.000000000 AM'
    """
    s = str(s).strip()
    parts = s.split(" ")
    if len(parts) == 3:
        date_part, time_part, ampm = parts
        hms = time_part.split(".")[:3]
        return pd.to_datetime(
            f"{date_part} {':'.join(hms)} {ampm}",
            format="%d-%b-%y %I:%M:%S %p",
            errors="coerce",
        )
    return pd.NaT

def _compute_outage_intervals(df: pd.DataFrame) -> dict[str, list[list[float]]]:
    out: dict[str, list[list[float]]] = {}
    for fid, g in df.groupby("device_id"):
        ivs: list[list[float]] = []
        for _, r in g.iterrows():
            sh = (r["start"] - START).total_seconds() / 3600
            eh = (r["end"] - START).total_seconds() / 3600
            sh = max(0.0, min(float(N_HOURS), float(sh)))
            eh = max(0.0, min(float(N_HOURS), float(eh)))
            if eh > sh:
                ivs.append([round(sh, 3), round(eh, 3)])
        if ivs:
            out[str(fid)] = ivs
    return out


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


def _load_data() -> dict[str, Any]:
    outage_xlsb = HERE / "Event Feeder Outage Data of April'26.xlsb"
    blockload_xlsb = HERE / "FEEDER BLOCKLOAD DATA_April'26.xlsb"
    consumer_outage_csv = HERE / "kashi_april_2026_consumer_outage.csv"
    consumer_part_csv = HERE / "part-00000-d7e6c5d1-d0e5-430d-8ac3-c995d369c557-c000.csv"

    # Outage events
    df = pd.read_excel(outage_xlsb, engine="pyxlsb", sheet_name=0)
    df = df.rename(columns={"Meter No": "device_id", "Occurrence Date Time": "occu", "Restoration Date Time": "resto"})
    df["device_id"] = df["device_id"].astype(str).str.strip()
    df["start"] = df["occu"].apply(_dt_outage)
    df["end"] = df["resto"].apply(_dt_outage)
    df = df.dropna(subset=["device_id", "start", "end"])
    df["duration_min"] = (df["end"] - df["start"]).dt.total_seconds() / 60
    df = df[df["duration_min"] > 0].copy()

    outage_ivs = _compute_outage_intervals(df)
    outage_pct = {fid: sum(e - s for s, e in ivs) / N_HOURS for fid, ivs in outage_ivs.items()}
    feeder_ids = set(outage_ivs.keys())

    # Blockload current=0 intervals
    usecols = ["Device Id", "Current R", "Current Y", "Current B", "RTC DTTM"]
    zero_slots_by_f: dict[str, list[int]] = {}

    for sheet in ["Sheet1", "Sheet2", "Sheet3"]:
        bl = pd.read_excel(blockload_xlsb, engine="pyxlsb", sheet_name=sheet, usecols=usecols)
        bl["Device Id"] = bl["Device Id"].astype(str).str.strip()
        bl = bl[bl["Device Id"].isin(feeder_ids)].copy()
        if bl.empty:
            continue

        bl["RTC DTTM"] = pd.to_datetime(bl["RTC DTTM"], errors="coerce")
        bl = bl.dropna(subset=["RTC DTTM", "Device Id"])
        bl["slot"] = ((bl["RTC DTTM"] - START).dt.total_seconds() / 1800).astype(int)
        bl = bl[(bl["slot"] >= 0) & (bl["slot"] < N_HALF)].copy()

        cz = (bl["Current R"] == 0) & (bl["Current Y"] == 0) & (bl["Current B"] == 0)
        bl = bl[cz].copy()
        if bl.empty:
            continue

        for fid, g in bl.groupby("Device Id"):
            fid = str(fid)
            zero_slots_by_f.setdefault(fid, []).extend(g["slot"].tolist())

    cz_ivs: dict[str, list[list[float]]] = {}
    for fid, slots in zero_slots_by_f.items():
        slots_sorted = sorted(set(int(s) for s in slots))
        if slots_sorted:
            cz_ivs[fid] = _slots_to_ivs(slots_sorted)

    # ---- Consumer outage intervals (from kashi + part CSVs) ----
    # Use the smaller sources (avoid Outage_Clean_Consumer.csv which is ~1.1GB).
    c_frames: list[pd.DataFrame] = []
    if consumer_outage_csv.exists():
        c1 = pd.read_csv(consumer_outage_csv)
        # expected columns: DEVICE_ID_clean, OCCU, RESTO
        c1 = c1.rename(columns={"DEVICE_ID_clean": "device_id", "OCCU": "occu", "RESTO": "resto"})
        c1["device_id"] = c1["device_id"].astype(str).str.strip()
        c1["start"] = c1["occu"].apply(_parse_oracle_dt)
        c1["end"] = c1["resto"].apply(_parse_oracle_dt)
        c1 = c1.dropna(subset=["device_id", "start", "end"])
        c1["duration_min"] = (c1["end"] - c1["start"]).dt.total_seconds() / 60
        c1 = c1[c1["duration_min"] > 0].copy()
        c_frames.append(c1[["device_id", "start", "end", "duration_min"]])

    if consumer_part_csv.exists():
        c2 = pd.read_csv(consumer_part_csv)
        # expected columns: device_id, occurrence_time, restoration_time
        c2["device_id"] = c2["device_id"].astype(str).str.strip()
        c2["start"] = pd.to_datetime(c2["occurrence_time"], errors="coerce")
        c2["end"] = pd.to_datetime(c2["restoration_time"], errors="coerce")
        c2 = c2.dropna(subset=["device_id", "start", "end"])
        c2["duration_min"] = (c2["end"] - c2["start"]).dt.total_seconds() / 60
        c2 = c2[c2["duration_min"] > 0].copy()
        c_frames.append(c2[["device_id", "start", "end", "duration_min"]])

    if c_frames:
        cdf = pd.concat(c_frames, ignore_index=True).drop_duplicates(subset=["device_id", "start", "end"])
        consumer_ivs = _compute_outage_intervals(cdf)
        consumer_pct = {cid: sum(e - s for s, e in ivs) / N_HOURS for cid, ivs in consumer_ivs.items()}
    else:
        consumer_ivs = {}
        consumer_pct = {}

    return {
        "outage_ivs": outage_ivs,
        "cz_ivs": cz_ivs,
        "outage_pct": outage_pct,
        "consumer_ivs": consumer_ivs,
        "consumer_pct": consumer_pct,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


DATA: dict[str, Any] | None = None
DATA_ERROR: str | None = None
DATA_LOADING = False


def _loader():
    global DATA, DATA_ERROR, DATA_LOADING
    try:
        DATA = _load_data()
    except Exception as e:
        DATA_ERROR = f"{type(e).__name__}: {e}"
    finally:
        DATA_LOADING = False


def ensure_loading_started() -> None:
    global DATA_LOADING
    if DATA is not None or DATA_ERROR is not None or DATA_LOADING:
        return
    DATA_LOADING = True
    t = threading.Thread(target=_loader, daemon=True)
    t.start()


def require_data() -> dict[str, Any]:
    ensure_loading_started()
    if DATA_ERROR is not None:
        raise HTTPException(status_code=500, detail=DATA_ERROR)
    if DATA is None:
        raise HTTPException(status_code=503, detail="Loading data, retry")
    return DATA


app = FastAPI(title="Outage Dashboard 2 API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    ensure_loading_started()


@app.get("/api/meta")
def meta():
    ensure_loading_started()
    if DATA_ERROR is not None:
        return {"loading": False, "error": DATA_ERROR}
    if DATA is None:
        return {"loading": True, "n_hours": N_HOURS, "n_half": N_HALF}
    return {
        "loading": False,
        "generated_at": DATA["generated_at"],
        "n_hours": N_HOURS,
        "n_half": N_HALF,
        "feeders": len(DATA["outage_ivs"]),
        "feeders_with_blockload": len(DATA["cz_ivs"]),
        "consumers": len(DATA.get("consumer_ivs", {})),
    }


@app.get("/api/feeders")
def feeders(q: str = "", limit: int = Query(300, ge=1, le=2000), offset: int = Query(0, ge=0)):
    d = require_data()
    items = [
        {"id": fid, "pct": float(d["outage_pct"].get(fid, 0.0))}
        for fid in d["outage_ivs"].keys()
        if (not q) or (q.lower() in fid.lower())
    ]
    items.sort(key=lambda x: x["pct"], reverse=True)
    return {"total": len(items), "items": items[offset : offset + limit]}


@app.get("/api/feeder")
def feeder(id: str = Query(...)):
    d = require_data()
    fid = id.strip()
    if fid not in d["outage_ivs"]:
        raise HTTPException(status_code=404, detail="unknown feeder")
    return {"id": fid, "outage_ivs": d["outage_ivs"].get(fid, []), "cz_ivs": d["cz_ivs"].get(fid, [])}


@app.get("/api/consumers")
def consumers(q: str = "", limit: int = Query(300, ge=1, le=2000), offset: int = Query(0, ge=0)):
    d = require_data()
    ivs = d.get("consumer_ivs", {})
    pct = d.get("consumer_pct", {})
    items = [{"id": cid, "pct": float(pct.get(cid, 0.0))} for cid in ivs.keys() if (not q) or (q.lower() in cid.lower())]
    items.sort(key=lambda x: x["pct"], reverse=True)
    return {"total": len(items), "items": items[offset : offset + limit]}


@app.get("/api/consumer")
def consumer(id: str = Query(...)):
    d = require_data()
    cid = id.strip()
    ivs = d.get("consumer_ivs", {})
    if cid not in ivs:
        raise HTTPException(status_code=404, detail="unknown consumer")
    return {"id": cid, "outage_ivs": ivs.get(cid, [])}


@app.get("/", response_class=HTMLResponse)
def root():
    html_path = HERE / "outage_dashboard2.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))
