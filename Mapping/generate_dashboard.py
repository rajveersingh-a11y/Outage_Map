"""
Generate outage_dashboard.html
Light mode | Timeline (heatmap) | Network Graph (vis.js hierarchical)
"""

import pandas as pd
import numpy as np
import json
import os
from pathlib import Path
from datetime import datetime

HERE = Path(__file__).resolve().parent
DATA_DIR = str(HERE) + os.sep
START    = datetime(2026, 4, 1, 0, 0, 0)
N_HOURS  = 720


def parse_oracle_dt(s: str):
    """
    Parse Oracle-ish timestamps like:
      '03-APR-26 06.31.00.000000000 PM'
    """
    s = str(s).strip()
    parts = s.split(" ")
    if len(parts) == 3:
        date_part, time_part, ampm = parts
        hms = time_part.split(".")[:3]
        return pd.to_datetime(f"{date_part} {':'.join(hms)} {ampm}", format="%d-%b-%y %I:%M:%S %p", errors="coerce")
    return pd.NaT


def parse_ddmmyyyy_dt(s: str):
    """
    Parse timestamps like:
      '01-04-2026 03:06:00'
    """
    return pd.to_datetime(s, format="%d-%m-%Y %H:%M:%S", errors="coerce")


def load_file(path):
    df = pd.read_csv(path)
    df["start"] = df["OCCU"].apply(parse_oracle_dt)
    df["end"]   = df["RESTO"].apply(parse_oracle_dt)
    df = df.dropna(subset=["start", "end"])
    df["duration_min"] = (df["end"] - df["start"]).dt.total_seconds() / 60
    df = df[df["duration_min"] > 0].copy()
    return df.rename(columns={"DEVICE_ID_clean": "device_id"})


def load_event_feeder_outage_xlsb(path: str) -> pd.DataFrame:
    """
    Expected columns (from 'Event Feeder Outage Data of April'26.xlsb'):
      - 'Meter No'
      - 'Occurrence Date Time'
      - 'Restoration Date Time'
      - 'Tripping Duration (In Minutes)' (optional; we recompute anyway)
    """
    df = pd.read_excel(path, engine="pyxlsb", sheet_name=0)
    df = df.rename(
        columns={
            "Meter No": "device_id",
            "Occurrence Date Time": "occu",
            "Restoration Date Time": "resto",
        }
    )
    if "device_id" not in df.columns:
        raise ValueError(f"Expected 'Meter No' column in {path}")
    df["start"] = df["occu"].apply(parse_ddmmyyyy_dt)
    df["end"] = df["resto"].apply(parse_ddmmyyyy_dt)
    df = df.dropna(subset=["device_id", "start", "end"])
    df["duration_min"] = (df["end"] - df["start"]).dt.total_seconds() / 60
    df = df[df["duration_min"] > 0].copy()
    df["device_id"] = df["device_id"].astype(str).str.strip()
    return df[["device_id", "start", "end", "duration_min"]]


def compute_intervals(df):
    result = {}
    for device_id, group in df.groupby("device_id"):
        ivs = []
        for _, row in group.iterrows():
            sh = max(0.0, min(float(N_HOURS), (row["start"] - START).total_seconds() / 3600))
            eh = max(0.0, min(float(N_HOURS), (row["end"]   - START).total_seconds() / 3600))
            if eh > sh:
                ivs.append([round(sh, 3), round(eh, 3)])
        if ivs:
            result[device_id] = ivs
    return result


def conf_code(s):
    s = str(s)
    return "H" if "HIGH" in s else "M" if "MEDIUM" in s else "L"


print("Loading CSVs …")
outage_xlsb = DATA_DIR + "Event Feeder Outage Data of April'26.xlsb"
if not os.path.exists(outage_xlsb):
    raise FileNotFoundError(
        "Missing outage input file: "
        f"{outage_xlsb}\n"
        "Place 'Event Feeder Outage Data of April'26.xlsb' next to this script."
    )

feeders = load_event_feeder_outage_xlsb(outage_xlsb)

# Optional: if you later add DTR/consumer outage files in the older CSV format,
# the dashboard will automatically include them. For now we keep them empty.
dtrs = pd.DataFrame(columns=["device_id", "start", "end", "duration_min"])
consumers = pd.DataFrame(columns=["device_id", "start", "end", "duration_min"])

print("Computing intervals …")
intervals = {
    "feeders":   compute_intervals(feeders),
    "dtrs":      compute_intervals(dtrs),
    "consumers": compute_intervals(consumers),
}

print("Loading topology mappings (optional) …")
feeder_of_dtr = {}
dtr_of_consumer = {}
score_of_dtr = {}
score_of_consumer = {}
feeder_to_dtrs: dict = {}
dtr_to_consumers: dict = {}
feeder_to_consumers: dict = {}

best_fd_path = DATA_DIR + "best_feeder_per_dtr.csv"
best_dc_path = DATA_DIR + "best_dtr_per_consumer.csv"
best_fc_path = DATA_DIR + "best_feeder_per_consumer.csv"

if os.path.exists(best_fd_path) and os.path.exists(best_dc_path) and os.path.exists(best_fc_path):
    fd = pd.read_csv(best_fd_path)
    dc = pd.read_csv(best_dc_path)

    feeder_of_dtr = dict(zip(fd["dn_id"], fd["up_id"]))
    score_of_dtr = {k: round(v, 3) for k, v in zip(fd["dn_id"], fd["score"])}
    dtr_of_consumer = dict(zip(dc["dn_id"], dc["up_id"]))
    score_of_consumer = {k: round(v, 3) for k, v in zip(dc["dn_id"], dc["score"])}

    for _, row in fd.iterrows():
        feeder_to_dtrs.setdefault(row["up_id"], []).append(
            {"id": row["dn_id"], "s": round(row["score"], 3), "c": conf_code(row.get("confidence", ""))}
        )
    for k in feeder_to_dtrs:
        feeder_to_dtrs[k].sort(key=lambda x: -x["s"])

    for _, row in dc.iterrows():
        dtr_to_consumers.setdefault(row["up_id"], []).append(
            {"id": row["dn_id"], "s": round(row["score"], 3), "c": conf_code(row.get("confidence", ""))}
        )
    for k in dtr_to_consumers:
        dtr_to_consumers[k].sort(key=lambda x: -x["s"])

    fc_best = pd.read_csv(best_fc_path)
    for _, row in fc_best.iterrows():
        feeder_to_consumers.setdefault(row["up_id"], []).append(
            {"id": row["dn_id"], "s": round(row["score"], 3), "c": conf_code(row.get("confidence", ""))}
        )
    for k in feeder_to_consumers:
        feeder_to_consumers[k].sort(key=lambda x: -x["s"])
        feeder_to_consumers[k] = feeder_to_consumers[k][:300]  # top 300 per feeder
else:
    print("  (No mapping CSVs found; network graph/verify will show feeder-only context.)")

# ── Block load: 30-min missing-data signal ────────────────────────────────────
N_HALF = 1440   # 30-min slots across April (30 days × 48 slots)
BL_COVERAGE_SLOTS = N_HALF
BL_COVERAGE_HOURS = BL_COVERAGE_SLOTS * 0.5

print("Loading block load data (all 3 sheets: Apr 1-30) …")
try:
    _dfs = [pd.read_excel(DATA_DIR + "FEEDER BLOCKLOAD DATA_April'26.xlsb",
                          engine='pyxlsb', sheet_name=s)
            for s in ['Sheet1', 'Sheet2', 'Sheet3']]
    bl = pd.concat(_dfs, ignore_index=True)
    bl["RTC DTTM"] = pd.to_datetime(bl["RTC DTTM"])
    bl["slot"] = ((bl["RTC DTTM"] - START).dt.total_seconds() / 1800).astype(int)
    bl["current_zero"] = (bl["Current R"] == 0) & (bl["Current Y"] == 0) & (bl["Current B"] == 0)
    bl = bl[(bl["slot"] >= 0) & (bl["slot"] < N_HALF)].copy()

    bl_ids_set   = set(bl["Device Id"].unique())
    out_ids_set  = set(feeders["device_id"].unique())
    common_ids   = sorted(out_ids_set & bl_ids_set)   # 27 feeders with BOTH signals

    # ── Filter intervals to only the 27 common feeders ──────────────────────────
    intervals["feeders"] = {k: v for k, v in intervals["feeders"].items()
                            if k in set(common_ids)}

    # full April is now available → no yellow "no-data" zone needed
    bl_feeder_ids: list = common_ids

    def _slots_to_ivs(slots_sorted):
        """Convert sorted list of integer slots to [[start_h, end_h], ...] intervals."""
        if not slots_sorted:
            return []
        ivs, s = [], slots_sorted[0]
        for k in range(1, len(slots_sorted)):
            if slots_sorted[k] != slots_sorted[k - 1] + 1:
                ivs.append([round(s * 0.5, 3), round((slots_sorted[k - 1] + 1) * 0.5, 3)])
                s = slots_sorted[k]
        ivs.append([round(s * 0.5, 3), round((slots_sorted[-1] + 1) * 0.5, 3)])
        return ivs

    # blockload_gaps: missing slots across full April per common feeder
    blockload_gaps: dict = {}
    # current_zero: slots where Current R=Y=B=0 (outage auto-index signal)
    current_zero_ivs: dict = {}

    for fid in common_ids:
        grp = bl[bl["Device Id"] == fid]
        present = set(grp["slot"].values)
        missing = sorted(i for i in range(N_HALF) if i not in present)
        if missing:
            blockload_gaps[fid] = _slots_to_ivs(missing)
        # current=0 intervals (record exists but all currents are zero)
        zero_slots = sorted(grp[grp["current_zero"]]["slot"].values.tolist())
        if zero_slots:
            current_zero_ivs[fid] = _slots_to_ivs(zero_slots)

    # ── Trend analysis: Current=0 during outage (auto-indexing validation) ──────
    trend_rows = []
    for fid in common_ids:
        out_slots: set = set()
        for _, row in feeders[feeders["device_id"] == fid].iterrows():
            sh = max(0, (row["start"] - START).total_seconds() / 1800)
            eh = min(N_HALF, (row["end"] - START).total_seconds() / 1800)
            for s in range(int(sh), min(N_HALF, int(np.ceil(eh)))):
                out_slots.add(s)
        grp = bl[bl["Device Id"] == fid]
        during  = grp[grp["slot"].isin(out_slots)]
        n_d, z_d = len(during), int(during["current_zero"].sum()) if len(during) else 0
        outside = grp[~grp["slot"].isin(out_slots)]
        n_o, z_o = len(outside), int(outside["current_zero"].sum()) if len(outside) else 0
        pct_zero_during  = round(z_d / n_d * 100, 1) if n_d else 0
        pct_zero_outside = round(z_o / n_o * 100, 1) if n_o else 0
        trend_match = pct_zero_during >= 80 if n_d else False
        trend_rows.append({
            "feeder":              fid,
            "outage_slots":        len(out_slots),
            "bl_records_during":   n_d,
            "pct_current_zero_during_outage":  pct_zero_during,
            "pct_current_zero_outside_outage": pct_zero_outside,
            "current_zero_trend_match":        trend_match,
        })

    trend_df = pd.DataFrame(trend_rows)
    n_with_outage = int((trend_df["outage_slots"] > 0).sum())
    n_match       = int(trend_df["current_zero_trend_match"].sum())
    n_has_gap     = len(blockload_gaps)

    trend_summary = {
        "total_feeders":           len(common_ids),
        "feeders_with_outage":     n_with_outage,
        "current_zero_match":      n_match,        # feeders where I=0 during >=80% of outage
        "feeders_with_bl_gap":     n_has_gap,
        "avg_pct_zero_during":     round(float(trend_df[trend_df.outage_slots>0]
                                               ["pct_current_zero_during_outage"].mean()), 1),
        "detail": trend_rows,
    }
    print(f"Block load (full April): {len(bl_ids_set)} feeders in file; "
          f"{len(common_ids)} common with outage; {len(blockload_gaps)} with BL gaps")
    print(f"Current=0 trend: {n_match}/{n_with_outage} feeders confirm I=0 during ≥80% of outage "
          f"(avg {trend_summary['avg_pct_zero_during']}%)")
except Exception as e:
    import traceback; traceback.print_exc()
    print(f"Block load skipped: {e}")
    bl_feeder_ids    = []
    blockload_gaps   = {}
    current_zero_ivs = {}
    trend_summary    = {"total_feeders": 0, "feeders_with_outage": 0,
                        "current_zero_match": 0, "feeders_with_bl_gap": 0,
                        "avg_pct_zero_during": 0, "detail": []}

payload = json.dumps({
    "intervals":            intervals,
    "n_hours":              N_HOURS,
    "n_half":               N_HALF,
    "bl_coverage_hours":    BL_COVERAGE_HOURS,
    "bl_feeder_ids":        bl_feeder_ids,
    "blockload_gaps":       blockload_gaps,
    "current_zero_ivs":     current_zero_ivs,   # slots where I_R=I_Y=I_B=0 (auto-index)
    "trend_summary":        trend_summary,
    "feeder_of_dtr":        feeder_of_dtr,
    "dtr_of_consumer":      dtr_of_consumer,
    "score_of_dtr":         score_of_dtr,
    "score_of_consumer":    score_of_consumer,
    "feeder_to_dtrs":       feeder_to_dtrs,
    "dtr_to_consumers":     dtr_to_consumers,
    "feeder_to_consumers":  feeder_to_consumers,
})
print(f"JSON payload: {len(payload)/1024/1024:.1f} MB")


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Smart Meter Outage Dashboard – April 2026</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#f6f8fa;color:#1f2328;
     height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* ── Header ── */
header{background:#ffffff;border-bottom:1px solid #d0d7de;padding:0 16px;
       display:flex;align-items:center;gap:12px;height:52px;flex-shrink:0;
       box-shadow:0 1px 3px rgba(0,0,0,.06)}
header h1{font-size:13px;font-weight:600;color:#1f2328;white-space:nowrap}
.badge{background:#0969da;color:#fff;font-size:10px;padding:2px 8px;
       border-radius:10px;font-weight:600;letter-spacing:.4px;flex-shrink:0}
.view-toggle{display:flex;gap:1px;background:#eaeef2;border:1px solid #d0d7de;
              border-radius:8px;padding:3px;flex-shrink:0}
.vbtn{padding:4px 14px;border-radius:6px;border:none;background:transparent;
      color:#656d76;font-size:11px;font-weight:500;cursor:pointer;transition:.15s}
.vbtn.active{background:#ffffff;color:#0969da;box-shadow:0 1px 3px rgba(0,0,0,.12)}
.vbtn:hover:not(.active){color:#1f2328}
.hdr-stats{display:flex;gap:20px;margin-left:auto}
.hdr-stat{text-align:right}
.hdr-stat-val{font-size:16px;font-weight:700;color:#0969da}
.hdr-stat-lbl{font-size:9px;color:#8c959f;text-transform:uppercase;letter-spacing:.4px}

/* ── Layout ── */
.layout{display:flex;flex:1;overflow:hidden}

/* ── Sidebar ── */
.sidebar{width:210px;background:#ffffff;border-right:1px solid #d0d7de;
         display:flex;flex-direction:column;flex-shrink:0}
.tabs{display:flex;border-bottom:1px solid #eaeef2}
.tab{flex:1;padding:9px 2px;font-size:11px;text-align:center;cursor:pointer;
     color:#8c959f;border-bottom:2px solid transparent;transition:.15s;
     font-weight:500;user-select:none}
.tab.active{color:#0969da;border-bottom-color:#0969da;background:#f6f8fa}
.tab:hover:not(.active){color:#1f2328}
.search-wrap{padding:8px;border-bottom:1px solid #eaeef2}
.search-wrap input{width:100%;background:#f6f8fa;border:1px solid #d0d7de;
                   border-radius:6px;padding:5px 9px;color:#1f2328;font-size:11px;
                   outline:none;transition:.15s}
.search-wrap input:focus{border-color:#0969da;background:#fff}
.show-all-btn{margin:6px 8px 2px;padding:5px;background:#f6f8fa;
              border:1px solid #d0d7de;border-radius:6px;color:#0969da;
              font-size:10px;font-weight:500;cursor:pointer;text-align:center;transition:.15s}
.show-all-btn:hover{background:#dbeafe;border-color:#0969da}
.sel-count{padding:2px 10px;font-size:9px;color:#8c959f}
.device-list{flex:1;overflow-y:auto;padding:2px 0}
.device-list::-webkit-scrollbar{width:4px}
.device-list::-webkit-scrollbar-thumb{background:#d0d7de;border-radius:2px}
.dev-item{padding:5px 10px;font-size:10px;cursor:pointer;display:flex;
           align-items:center;gap:6px;border-left:2px solid transparent;transition:.1s}
.dev-item:hover{background:#f6f8fa}
.dev-item.sel{background:#dbeafe;border-left-color:#0969da;color:#0969da}
.dev-name{flex:1;font-family:'Courier New',monospace;font-size:10px;overflow:hidden;
           text-overflow:ellipsis;white-space:nowrap}
.dev-pct{font-size:9px;color:#8c959f;white-space:nowrap}
.dev-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.dot-r{background:#d73a49}.dot-g{background:#d0d7de}
.sb-footer{padding:5px 10px;border-top:1px solid #eaeef2;font-size:9px;color:#8c959f}

/* ── Main ── */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}

/* ── Timeline view ── */
#view-tl{flex:1;display:flex;flex-direction:column;overflow:hidden;min-height:0}
.chart-area{flex:1;min-height:0;position:relative;background:#fff}
#heatmap-plot{width:100%;height:100%}
.empty-state{position:absolute;inset:0;display:flex;flex-direction:column;
              align-items:center;justify-content:center;color:#d0d7de;gap:10px;pointer-events:none}
.empty-state p{font-size:13px;color:#8c959f}
.empty-state small{font-size:11px;color:#c6cdd5}
.legend-bar{position:absolute;bottom:10px;right:14px;display:flex;align-items:center;
             gap:10px;background:rgba(255,255,255,.95);border:1px solid #d0d7de;
             border-radius:6px;padding:4px 10px;font-size:10px;color:#656d76;
             box-shadow:0 1px 4px rgba(0,0,0,.08)}
.leg-dot{width:10px;height:10px;border-radius:2px;display:inline-block}
.detail-panel{height:170px;border-top:1px solid #d0d7de;display:flex;
               flex-direction:column;flex-shrink:0;background:#fff}
.detail-hdr{padding:6px 14px;display:flex;align-items:center;gap:10px;
             border-bottom:1px solid #eaeef2;flex-shrink:0}
.detail-id{font-size:12px;font-weight:600;font-family:'Courier New',monospace;color:#0969da}
.detail-meta{font-size:10px;color:#8c959f;flex:1}
.detail-close{cursor:pointer;color:#8c959f;font-size:16px;padding:0 4px;line-height:1}
.detail-close:hover{color:#1f2328}
#detail-plot{flex:1;min-height:0}

/* ── Mapping view ── */
#view-map{flex:1;display:none;flex-direction:column;overflow:hidden;min-height:0;background:#f6f8fa}
.map-empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
            gap:10px;color:#8c959f}
.map-empty p{font-size:13px}
.map-empty small{font-size:11px;color:#c6cdd5}

/* Map header bar */
.map-bar{padding:8px 14px;background:#ffffff;border-bottom:1px solid #d0d7de;
          display:flex;align-items:center;gap:8px;flex-shrink:0;
          box-shadow:0 1px 3px rgba(0,0,0,.04)}
.map-bar-title{font-size:13px;font-weight:700;font-family:'Courier New',monospace;color:#1f2328}
.map-bar-sub{font-size:10px;color:#8c959f;padding:2px 6px;background:#f6f8fa;
              border:1px solid #d0d7de;border-radius:4px}
.map-chips{display:flex;gap:5px;margin-left:auto;flex-wrap:wrap;align-items:center}
.chip{padding:2px 8px;border-radius:10px;font-size:10px;font-weight:500;line-height:1.4}
.ch-blue  {background:#dbeafe;color:#0969da;border:1px solid #bfdbfe}
.ch-green {background:#dcfce7;color:#1a7f37;border:1px solid #bbf7d0}
.ch-amber {background:#fef9c3;color:#9a6700;border:1px solid #fde68a}
.ch-red   {background:#fee2e2;color:#d73a49;border:1px solid #fecaca}

/* Graph + tree split */
.map-body{display:flex;flex:1;overflow:hidden;min-height:0}
.graph-panel{flex:3;min-width:0;display:flex;flex-direction:column;
              border-right:1px solid #d0d7de;background:#fff}
.graph-toolbar{display:flex;align-items:center;gap:6px;padding:6px 10px;
                border-bottom:1px solid #eaeef2;flex-shrink:0;background:#f6f8fa}
.g-legend{display:flex;align-items:center;gap:10px;font-size:10px;color:#656d76;flex:1}
.g-leg-item{display:flex;align-items:center;gap:4px}
.g-dot{width:12px;height:12px;border-radius:50%;display:inline-block;flex-shrink:0}
.g-hint{font-size:10px;color:#8c959f;font-style:italic}
.graph-btns{display:flex;gap:4px}
.gbtn{padding:3px 9px;font-size:10px;font-weight:500;cursor:pointer;border-radius:5px;
       border:1px solid #d0d7de;background:#fff;color:#1f2328;transition:.15s}
.gbtn:hover{background:#f6f8fa;border-color:#0969da;color:#0969da}
#network-graph{flex:1;min-height:0}

/* Tree panel */
.tree-panel{flex:1.4;min-width:220px;max-width:340px;display:flex;flex-direction:column;background:#fff}
.tree-hdr{padding:7px 12px;border-bottom:1px solid #eaeef2;font-size:11px;font-weight:600;
           color:#1f2328;background:#f6f8fa;flex-shrink:0}
.tree-scroll{flex:1;overflow-y:auto;padding:0}
.tree-scroll::-webkit-scrollbar{width:4px}
.tree-scroll::-webkit-scrollbar-thumb{background:#d0d7de;border-radius:2px}

/* Tree root node */
.tree-root-node{display:flex;align-items:center;gap:6px;padding:8px 12px;
                 background:#dbeafe;border-bottom:1px solid #bfdbfe;flex-shrink:0}
.root-id{font-family:'Courier New',monospace;font-size:11px;font-weight:700;color:#0550ae;flex:1}
.root-chip{font-size:9px;font-weight:500;padding:1px 6px;border-radius:8px}

/* DTR rows */
.dtr-row{display:flex;align-items:center;gap:5px;padding:5px 10px 5px 14px;
          font-size:10px;cursor:pointer;border-left:3px solid transparent;
          border-bottom:1px solid #f0f3f6;transition:.1s;user-select:none}
.dtr-row:hover{background:#f6f8fa}
.dtr-row.open{border-left-color:#1a7f37;background:#f0fdf4}
.dtr-arrow{width:12px;color:#8c959f;font-size:9px;flex-shrink:0;text-align:center}
.dtr-id{font-family:'Courier New',monospace;color:#1a7f37;flex:1;overflow:hidden;
         text-overflow:ellipsis;white-space:nowrap;font-size:10px}
.dtr-meta{font-size:9px;color:#8c959f;white-space:nowrap}

/* Consumer rows */
.con-block{border-left:3px solid #bbf7d0;margin-left:14px;background:#f6fef6}
.con-row{display:flex;align-items:center;gap:4px;padding:3px 8px;
          font-size:9px;border-bottom:1px solid #f0f3f6;cursor:pointer;transition:.1s}
.con-row:hover{background:#dcfce7}
.con-id{font-family:'Courier New',monospace;color:#1f2328;flex:1;overflow:hidden;
         text-overflow:ellipsis;white-space:nowrap}
.con-meta{font-size:9px;color:#8c959f;white-space:nowrap}
.load-more-btn{padding:5px 10px;font-size:9px;color:#0969da;cursor:pointer;text-align:center;
                background:#f6f8fa;border-top:1px solid #eaeef2;font-weight:500}
.load-more-btn:hover{background:#dbeafe}

/* Confidence badges */
.cb{padding:1px 5px;border-radius:3px;font-size:8px;font-weight:600;flex-shrink:0}
.cbH{background:#dcfce7;color:#1a7f37;border:1px solid #bbf7d0}
.cbM{background:#fef9c3;color:#9a6700;border:1px solid #fde68a}
.cbL{background:#fee2e2;color:#d73a49;border:1px solid #fecaca}

/* Consumer chain view */
.chain-panel{flex:1;min-width:0;display:flex;flex-direction:column;
              align-items:center;justify-content:center;gap:8px;padding:20px;
              overflow-y:auto;background:#fff}
.chain-card{width:100%;max-width:280px;padding:12px 14px;border-radius:8px;
             border:1px solid #d0d7de;background:#fff;
             box-shadow:0 1px 4px rgba(0,0,0,.06)}
.chain-card.feeder-card{border-color:#bfdbfe;background:#f0f7ff}
.chain-card.dtr-card   {border-color:#bbf7d0;background:#f0fdf4}
.chain-card.con-card   {border-color:#fde68a;background:#fffef0}
.chain-label{font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:.4px;
              color:#8c959f;margin-bottom:4px}
.chain-id{font-size:13px;font-weight:700;font-family:'Courier New',monospace;color:#1f2328}
.chain-score{font-size:10px;color:#8c959f;margin-top:3px}
.chain-arrow{font-size:20px;color:#d0d7de;line-height:1}

/* ── Sankey legend bar ── */
.sankey-legend{display:flex;align-items:center;gap:10px;padding:5px 10px;
               border-bottom:1px solid #eaeef2;flex-shrink:0;background:#f6f8fa;flex-wrap:wrap}
.sankey-legend .g-leg-item{display:flex;align-items:center;gap:4px;font-size:10px;color:#656d76}
.sankey-legend .g-dot{width:11px;height:11px;border-radius:50%;display:inline-block;flex-shrink:0}
.sankey-hint{font-size:10px;color:#8c959f;font-style:italic;margin-left:4px}
#sankey-plot{flex:1;min-height:0}

/* ── Verify mode toggle ── */
.vfy-mode-toggle{display:flex;gap:1px;background:#eaeef2;border:1px solid #d0d7de;
                  border-radius:8px;padding:3px;flex-shrink:0}
.vfy-mode-btn{padding:4px 13px;border-radius:6px;border:none;background:transparent;
               color:#656d76;font-size:11px;font-weight:500;cursor:pointer;transition:.15s;white-space:nowrap}
.vfy-mode-btn.active{background:#fff;color:#0969da;box-shadow:0 1px 3px rgba(0,0,0,.12)}
.vfy-mode-btn:hover:not(.active){color:#1f2328}

/* ── Verify view ── */
#view-verify{flex:1;display:none;flex-direction:column;overflow:hidden;min-height:0;background:#fff}
.vfy-controls{padding:8px 14px;background:#f6f8fa;border-bottom:1px solid #d0d7de;
               display:flex;align-items:center;gap:10px;flex-shrink:0;flex-wrap:wrap}
.vfy-lbl{font-size:11px;color:#656d76;font-weight:500;white-space:nowrap}
.vfy-select{padding:4px 8px;border:1px solid #d0d7de;border-radius:6px;background:#fff;
             font-size:11px;color:#1f2328;font-family:'Courier New',monospace;
             outline:none;cursor:pointer;max-width:180px}
.vfy-select:focus{border-color:#0969da}
.vfy-num{width:54px;padding:4px 6px;border:1px solid #d0d7de;border-radius:6px;
          background:#fff;font-size:11px;color:#1f2328;outline:none;text-align:center}
.vfy-num:focus{border-color:#0969da}
.vfy-info{font-size:10px;color:#8c959f;margin-left:auto;white-space:nowrap}
.vfy-legend{display:flex;align-items:center;gap:10px;margin-left:12px}
.vfy-leg-item{display:flex;align-items:center;gap:4px;font-size:10px;color:#656d76}
.vfy-leg-swatch{width:10px;height:10px;border-radius:2px;flex-shrink:0}
.vfy-plot-wrap{flex:1;min-height:0;position:relative;background:#fff;overflow:hidden}
#verify-plot{width:100%;height:100%}
.vfy-empty{position:absolute;inset:0;display:flex;flex-direction:column;
            align-items:center;justify-content:center;color:#8c959f;gap:8px;pointer-events:none}
.vfy-empty p{font-size:13px}.vfy-empty small{font-size:11px;color:#c6cdd5}

/* ── Trend insight banner ── */
.trend-banner{display:flex;align-items:stretch;gap:0;background:#f6f8fa;
              border-bottom:1px solid #d0d7de;flex-shrink:0;flex-wrap:wrap;overflow:hidden}
.trend-card{padding:8px 18px;display:flex;flex-direction:column;align-items:center;
            justify-content:center;border-right:1px solid #d0d7de;gap:2px;min-width:110px}
.trend-card:last-child{border-right:none}
.trend-val{font-size:22px;font-weight:700;line-height:1;font-variant-numeric:tabular-nums}
.trend-lbl{font-size:9px;color:#656d76;text-align:center;line-height:1.3;text-transform:uppercase;letter-spacing:.3px}
.trend-note{flex:1;padding:8px 14px;font-size:10px;color:#656d76;display:flex;
            align-items:center;line-height:1.5;max-width:420px}
.tv-green{color:#1a7f37}.tv-blue{color:#0969da}.tv-amber{color:#9a6700}.tv-red{color:#cf222e}
</style>
</head>
<body>

<header>
  <span class="badge">AMI</span>
  <h1>Smart Meter Outage Dashboard – April 2026 &nbsp;|&nbsp; Kashi</h1>
  <div class="view-toggle">
    <button class="vbtn active" id="vbtn-tl"  onclick="setView('timeline')">⏱ Timeline</button>
    <button class="vbtn"        id="vbtn-map" onclick="setView('mapping')">🔗 Network Graph</button>
    <button class="vbtn"        id="vbtn-vfy" onclick="setView('verify')">🔍 Verify</button>
  </div>
  <div class="hdr-stats">
    <div class="hdr-stat"><div class="hdr-stat-val" id="s-f">—</div><div class="hdr-stat-lbl">Feeders</div></div>
    <div class="hdr-stat"><div class="hdr-stat-val" id="s-d">—</div><div class="hdr-stat-lbl">DTRs</div></div>
    <div class="hdr-stat"><div class="hdr-stat-val" id="s-c">—</div><div class="hdr-stat-lbl">Consumers</div></div>
  </div>
</header>

<!-- ── Trend insight banner (populated by JS) ── -->
<div class="trend-banner" id="trend-banner" style="display:none">
  <div class="trend-card">
    <div class="trend-val tv-blue"  id="tb-total">—</div>
    <div class="trend-lbl">Feeders<br>visualised</div>
  </div>
  <div class="trend-card">
    <div class="trend-val tv-green" id="tb-bl-ok">—</div>
    <div class="trend-lbl">BL normal<br>during outage</div>
  </div>
  <div class="trend-card">
    <div class="trend-val tv-amber" id="tb-gap">—</div>
    <div class="trend-lbl">Feeders with<br>BL gap Apr 1-12</div>
  </div>
  <div class="trend-card">
    <div class="trend-val tv-red"   id="tb-lost">—</div>
    <div class="trend-lbl">BL lost<br>during outage</div>
  </div>
  <div class="trend-note" id="tb-note"></div>
</div>

<div class="layout">
  <aside class="sidebar">
    <div class="tabs">
      <div class="tab active" data-tab="feeders"   onclick="switchTab('feeders')">Feeders</div>
      <div class="tab"        data-tab="dtrs"      onclick="switchTab('dtrs')">DTRs</div>
      <div class="tab"        data-tab="consumers" onclick="switchTab('consumers')">Consumers</div>
    </div>
    <div class="search-wrap">
      <input id="search-inp" type="text" placeholder="Search device ID…" oninput="onSearch()"/>
    </div>
    <div class="show-all-btn" onclick="showAll()">▶ Show all in view</div>
    <div class="sel-count" id="sel-count"></div>
    <div class="device-list" id="dev-list"></div>
    <div class="sb-footer" id="sb-footer"></div>
  </aside>

  <main class="main">

    <!-- ── Timeline view ── -->
    <div id="view-tl">
      <div class="chart-area">
        <div id="heatmap-plot"></div>
        <div class="empty-state" id="tl-empty">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#d0d7de" stroke-width="1.2">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
          </svg>
          <p>Select devices from the left panel</p>
          <small>Or click "Show all in view"</small>
        </div>
        <div class="legend-bar" id="hm-legend">
          <span class="leg-dot" style="background:#eaeef2;border:1px solid #d0d7de"></span>Normal&nbsp;
          <span class="leg-dot" style="background:#d73a49"></span>Outage
        </div>
      </div>
      <div class="detail-panel" id="detail-panel" style="display:none">
        <div class="detail-hdr">
          <span class="detail-id" id="detail-id">—</span>
          <span class="detail-meta" id="detail-meta"></span>
          <span class="detail-close" onclick="closeDetail()">✕</span>
        </div>
        <div id="detail-plot"></div>
      </div>
    </div>

    <!-- ── Mapping / Network Graph view ── -->
    <div id="view-map">
      <div class="map-empty" id="map-empty">
        <svg width="52" height="52" viewBox="0 0 24 24" fill="none" stroke="#d0d7de" stroke-width="1.2">
          <circle cx="12" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/>
          <line x1="12" y1="7" x2="5.3" y2="17.3"/><line x1="12" y1="7" x2="18.7" y2="17.3"/>
        </svg>
        <p>Select a device from the left panel</p>
        <small>Feeders show full hierarchy · DTRs show their consumers · Consumers show parent chain</small>
      </div>

      <div id="map-content" style="display:none;flex:1;flex-direction:column;overflow:hidden;min-height:0">
        <div class="map-bar">
          <span class="map-bar-title" id="map-title">—</span>
          <span class="map-bar-sub"   id="map-sub">—</span>
          <div class="map-chips"      id="map-chips"></div>
        </div>
        <div class="map-body" id="map-body">
          <!-- filled by JS -->
        </div>
      </div>
    </div>

    <!-- ── Verify view ── -->
    <div id="view-verify">
      <div class="vfy-controls">
        <div class="vfy-mode-toggle">
          <button class="vfy-mode-btn active" id="vbtn-fc" onclick="setVerifyMode('fc')">Feeder → Consumer</button>
          <button class="vfy-mode-btn"         id="vbtn-dc" onclick="setVerifyMode('dc')">DTR → Consumer</button>
        </div>
        <!-- FC mode controls -->
        <div id="vfy-fc-ctl" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span class="vfy-lbl">Feeder</span>
          <select id="vfy-feeder" class="vfy-select" onchange="renderVerify()">
            <option value="">— pick a feeder —</option>
          </select>
          <span class="vfy-lbl">Top consumers</span>
          <input id="vfy-max-cons-fc" class="vfy-num" type="number" value="50" min="1" max="300" onchange="renderVerify()"/>
        </div>
        <!-- DC mode controls -->
        <div id="vfy-dc-ctl" style="display:none;align-items:center;gap:8px;flex-wrap:wrap">
          <span class="vfy-lbl">DTR</span>
          <select id="vfy-dtr" class="vfy-select" onchange="renderVerify()">
            <option value="">— pick a DTR —</option>
          </select>
          <span class="vfy-lbl">Top consumers</span>
          <input id="vfy-max-cons-dc" class="vfy-num" type="number" value="30" min="1" max="200" onchange="renderVerify()"/>
        </div>
        <div class="vfy-legend">
          <span class="vfy-leg-item"><span class="vfy-leg-swatch" style="background:#dbeafe;border:1px solid #bfdbfe"></span>Feeder / DTR row</span>
          <span class="vfy-leg-item"><span class="vfy-leg-swatch" style="background:#fef9c3;border:1px solid #fde68a"></span>Consumer row</span>
          <span class="vfy-leg-item"><span class="vfy-leg-swatch" style="background:#d73a49"></span>Outage</span>
          <span class="vfy-leg-item"><span class="vfy-leg-swatch" style="background:#eaeef2;border:1px solid #d0d7de"></span>Normal</span>
        </div>
        <span class="vfy-info" id="vfy-info"></span>
      </div>
      <div class="vfy-plot-wrap">
        <div id="verify-plot"></div>
        <div class="vfy-empty" id="vfy-empty">
          <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="#d0d7de" stroke-width="1.3">
            <rect x="3" y="3" width="18" height="18" rx="2"/>
            <line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/>
            <line x1="9" y1="3" x2="9" y2="21"/>
          </svg>
          <p>Select a feeder or DTR above</p>
          <small>Feeder→Consumer: direct mapping correlation &nbsp;|&nbsp; DTR→Consumer: downstream mapping</small>
        </div>
      </div>
    </div>

  </main>
</div>

<script>
// ── Data ─────────────────────────────────────────────────────────
const D   = __PAYLOAD__;
const N   = D.n_hours;              // 720 hourly slots
const NH  = D.n_half;              // 1440 half-hour slots
const BL_COV_H = D.bl_coverage_hours; // 288 h = Apr 12 boundary
const BL_COV_S = BL_COV_H * 2;    // 576 slots
const IV  = D.intervals;
const BG  = D.blockload_gaps;      // gaps within Apr 1-12 per feeder (blue)
const BL_IDS = new Set(D.bl_feeder_ids); // feeders present in BL file
const CZ  = D.current_zero_ivs;        // {feeder_id: [[start_h,end_h],...]} Current R=Y=B=0
const TS  = D.trend_summary;           // current=0 auto-indexing trend analysis
const FD  = D.feeder_to_dtrs;
const DC  = D.dtr_to_consumers;
const FC  = D.feeder_to_consumers;
const FOD = D.feeder_of_dtr;
const DOC = D.dtr_of_consumer;
const SFD = D.score_of_dtr;
const SDC = D.score_of_consumer;

// ── Hour labels (hourly, 720) ─────────────────────────────────────
const HLABELS = Array.from({length:N}, (_,h) => {
  const d = Math.floor(h/24)+1, hr = String(h%24).padStart(2,'0');
  return `Apr ${d} ${hr}:00`;
});
const TVALS = Array.from({length:30}, (_,i) => i*24);
const TTEXT = Array.from({length:30}, (_,i) => `Apr ${i+1}`);
const XIDX  = Array.from({length:N},  (_,i) => i);

// ── 30-min labels (half-hourly, 1440) ────────────────────────────
const HLABELS30 = Array.from({length:NH}, (_,s) => {
  const d  = Math.floor(s/48)+1;
  const hr = String(Math.floor((s%48)/2)).padStart(2,'0');
  const mn = s%2 === 0 ? '00' : '30';
  return `Apr ${d} ${hr}:${mn}`;
});
const TVALS30 = Array.from({length:30}, (_,i) => i*48);
const TTEXT30 = Array.from({length:30}, (_,i) => `Apr ${i+1}`);
const XIDX30  = Array.from({length:NH}, (_,i) => i);

// ── App state ─────────────────────────────────────────────────────
let curTab  = 'feeders';
let curView = 'timeline';
let selected = new Set();
let listData = [];

// ── Helpers ───────────────────────────────────────────────────────
function toTS(ivs) {               // hourly (720)
  const a = new Int8Array(N);
  if (!ivs) return a;
  for (const [s,e] of ivs)
    for (let h=Math.max(0,Math.floor(s)); h<Math.min(N,Math.ceil(e)); h++) a[h]=1;
  return a;
}
function toTS30(ivs) {             // 30-min (1440) — outage intervals in hours
  const a = new Int8Array(NH);
  if (!ivs) return a;
  for (const [s,e] of ivs) {
    const ss = Math.max(0, Math.floor(s*2));
    const ee = Math.min(NH, Math.ceil(e*2));
    for (let i=ss; i<ee; i++) a[i]=1;
  }
  return a;
}
// 4-state combo (outage × current=0), encoded as state/3 for colorscale:
//   0   = 0/3 = normal         — grey
//   1/3 ≈ 0.33 = outage only   — red   (outage but current somehow >0, rare)
//   2/3 ≈ 0.67 = current=0 only — green (I=0 without logged outage)
//   1   = 3/3 = outage + I=0   — dark red/maroon (expected: aligned)
function toCombo30(outage_ivs, cz_ivs) {
  const out = toTS30(outage_ivs);
  const cz  = toTS30(cz_ivs);
  const a   = new Float32Array(NH);
  for (let i=0; i<NH; i++) {
    if      (out[i] && cz[i])  a[i] = 3/3;   // both — confirmed outage
    else if (cz[i])            a[i] = 2/3;   // I=0 only
    else if (out[i])           a[i] = 1/3;   // outage only (I>0 — unexpected)
    else                       a[i] = 0;     // normal
  }
  return a;
}
function outagePct(ivs) {
  return ivs ? ivs.reduce((t,[s,e])=>t+(e-s),0)/N : 0;
}
// 4-state colorscale
const COMBO_CS = [
  [0,    '#f0f3f6'],  // 0   — normal, grey
  [0.28, '#f0f3f6'],
  [0.29, '#fa8072'],  // 1/3 — outage only (I>0), light red / salmon
  [0.61, '#fa8072'],
  [0.62, '#1a7f37'],  // 2/3 — I=0 only (no logged outage), green
  [0.94, '#1a7f37'],
  [0.95, '#d73a49'],  // 3/3 — outage + I=0 confirmed, strong red
  [1.0,  '#d73a49'],
];
const COMBO_TEXT = v => {
  if (v === 0)    return '✅ Normal';
  if (v <= 0.34)  return '🟠 Outage (I>0 — check data)';
  if (v <= 0.68)  return '🟢 Current=0 (no logged outage)';
  return '🔴 Outage confirmed (I=0)';
};
function pctStr(id, tab) {
  const ivs = IV[tab||curTab][id];
  return ivs ? (outagePct(ivs)*100).toFixed(1) : '0.0';
}
function fmtH(h) { return h<1 ? `${Math.round(h*60)}m` : `${h.toFixed(1)}h`; }
function totalH(ivs) { return ivs ? ivs.reduce((t,[s,e])=>t+(e-s),0) : 0; }
function cc(c) { return c==='H'?'cbH':c==='M'?'cbM':'cbL'; }
function cl(c) { return c==='H'?'HIGH':c==='M'?'MED':'LOW'; }

// ── View toggle ───────────────────────────────────────────────────
function setView(v) {
  curView = v;
  document.getElementById('vbtn-tl').classList.toggle('active',  v==='timeline');
  document.getElementById('vbtn-map').classList.toggle('active', v==='mapping');
  document.getElementById('vbtn-vfy').classList.toggle('active', v==='verify');
  document.getElementById('view-tl').style.display     = v==='timeline' ? 'flex' : 'none';
  document.getElementById('view-map').style.display    = v==='mapping'  ? 'flex' : 'none';
  document.getElementById('view-verify').style.display = v==='verify'   ? 'flex' : 'none';
  if      (v==='mapping')  renderMapping();
  else if (v==='verify')   renderVerify();
  else                     renderHeatmap();
}

// ── Sidebar ───────────────────────────────────────────────────────
function switchTab(tab) {
  curTab = tab; selected.clear();
  document.querySelectorAll('.tab').forEach(el =>
    el.classList.toggle('active', el.dataset.tab===tab));
  document.getElementById('search-inp').value = '';
  buildList('');
  if (tab==='feeders') showAll();
  else if (curView==='timeline') renderHeatmap();
  else renderMapping();
}

function buildList(filter) {
  const data = IV[curTab]; filter = filter.toLowerCase();
  listData = Object.entries(data)
    .filter(([id]) => !filter || id.toLowerCase().includes(filter))
    .map(([id,ivs]) => ({id, pct:outagePct(ivs)}))
    .sort((a,b)=>b.pct-a.pct);
  const ul = document.getElementById('dev-list');
  ul.innerHTML = '';
  listData.slice(0,300).forEach(({id,pct}) => {
    const div = document.createElement('div');
    div.className = 'dev-item'+(selected.has(id)?' sel':'');
    div.innerHTML =
      `<span class="dev-dot ${pct>0.001?'dot-r':'dot-g'}"></span>`+
      `<span class="dev-name" title="${id}">${id}</span>`+
      `<span class="dev-pct">${(pct*100).toFixed(1)}%</span>`;
    div.onclick = () => toggleDev(id, div);
    ul.appendChild(div);
  });
  const total = Object.keys(data).length;
  document.getElementById('sb-footer').textContent =
    `${Math.min(300,listData.length)} of ${total.toLocaleString()} shown`;
}

function onSearch() { buildList(document.getElementById('search-inp').value); }

function toggleDev(id, el) {
  if (selected.has(id)) { selected.delete(id); el.classList.remove('sel'); }
  else                   { selected.add(id);    el.classList.add('sel');    }
  updateSelCount();
  if (curView==='timeline') { renderHeatmap(); if(selected.size===1) showDetail([...selected][0]); }
  else renderMapping();
}

function showAll() {
  const ids = listData.length ? listData.map(d=>d.id) : Object.keys(IV[curTab]);
  const lim = curTab==='feeders' ? ids.length : Math.min(80, ids.length);
  selected.clear(); ids.slice(0,lim).forEach(id=>selected.add(id));
  buildList(document.getElementById('search-inp').value);
  updateSelCount();
  if (curView==='timeline') renderHeatmap(); else renderMapping();
}

function updateSelCount() {
  document.getElementById('sel-count').textContent =
    selected.size ? `${selected.size} selected` : '';
}

// ══════════════════════════════════════════════════════════════════
//  TIMELINE VIEW
// ══════════════════════════════════════════════════════════════════
function renderHeatmap() {
  const ids = [...selected];
  document.getElementById('tl-empty').style.display = ids.length ? 'none' : '';
  if (!ids.length) { Plotly.purge('heatmap-plot'); return; }

  const isFeeder = curTab === 'feeders';
  const yg = ids.length>60?0:ids.length>20?.5:1;

  if (isFeeder) {
    // ── Feeder: 30-min 4-state combo (outage × current=0) ──────────
    const z   = ids.map(id => Array.from(toCombo30(IV.feeders[id], CZ[id])));
    const raw = ids.map(id => Array.from(toCombo30(IV.feeders[id], CZ[id])));
    Plotly.react('heatmap-plot',[{
      type:'heatmap', z, y:ids, x:XIDX30,
      colorscale:COMBO_CS, zmin:0, zmax:1,
      showscale:true,
      colorbar:{
        tickvals:[0, 1/3, 2/3, 1],
        ticktext:['Normal','Outage (I>0)','I=0 only','Outage+I=0'],
        tickfont:{size:9,color:'#1f2328'},
        len:0.5, thickness:10, x:1.01,
      },
      xgap:0, ygap:yg,
      customdata:ids.map(()=>HLABELS30),
      hovertemplate:'<b>%{y}</b><br>%{customdata}<br>%{text}<extra></extra>',
      text:raw.map(r=>r.map(v=>COMBO_TEXT(v))),
    }],{
      paper_bgcolor:'#ffffff', plot_bgcolor:'#f6f8fa',
      margin:{l:130,r:90,t:12,b:46},
      xaxis:{tickvals:TVALS30,ticktext:TTEXT30,tickfont:{size:10,color:'#8c959f'},
             gridcolor:'#eaeef2',linecolor:'#d0d7de',range:[0,NH]},
      yaxis:{automargin:true,tickfont:{size:9,color:'#1f2328',family:'Courier New'},
             gridcolor:'#eaeef2'},
    },{responsive:true,displayModeBar:true,
       modeBarButtonsToRemove:['select2d','lasso2d','toImage'],displaylogo:false});
    // update static legend bar for feeder view
    document.getElementById('hm-legend').innerHTML =
      '<span style="display:flex;align-items:center;gap:4px;font-size:10px;color:#656d76">' +
      '<span style="width:10px;height:10px;border-radius:2px;background:#f0f3f6;border:1px solid #d0d7de;display:inline-block"></span>Normal&nbsp;' +
      '<span style="width:10px;height:10px;border-radius:2px;background:#d73a49;display:inline-block"></span>Outage+I=0&nbsp;' +
      '<span style="width:10px;height:10px;border-radius:2px;background:#1a7f37;display:inline-block"></span>I=0 only&nbsp;' +
      '<span style="width:10px;height:10px;border-radius:2px;background:#fa8072;display:inline-block"></span>Outage (I>0)' +
      '</span>';
  } else {
    // ── DTR / Consumer: hourly outage only ────────────────────────
    const z = ids.map(id=>Array.from(toTS(IV[curTab][id])));
    Plotly.react('heatmap-plot',[{
      type:'heatmap', z, y:ids, x:XIDX,
      colorscale:[[0,'#f0f3f6'],[1,'#d73a49']],
      showscale:false, xgap:0, ygap:yg,
      customdata:ids.map(()=>HLABELS),
      hovertemplate:'<b>%{y}</b><br>%{customdata}<br>%{text}<extra></extra>',
      text:z.map(r=>r.map(v=>v?'🔴 Outage':'✅ Normal')),
    }],{
      paper_bgcolor:'#ffffff', plot_bgcolor:'#f6f8fa',
      margin:{l:130,r:14,t:12,b:46},
      xaxis:{tickvals:TVALS,ticktext:TTEXT,tickfont:{size:10,color:'#8c959f'},
             gridcolor:'#eaeef2',linecolor:'#d0d7de',range:[0,N]},
      yaxis:{automargin:true,tickfont:{size:9,color:'#1f2328',family:'Courier New'},
             gridcolor:'#eaeef2'},
    },{responsive:true,displayModeBar:true,
       modeBarButtonsToRemove:['select2d','lasso2d','toImage'],displaylogo:false});
    document.getElementById('hm-legend').innerHTML =
      '<span style="display:flex;align-items:center;gap:4px;font-size:10px;color:#656d76">' +
      '<span style="width:10px;height:10px;border-radius:2px;background:#eaeef2;border:1px solid #d0d7de;display:inline-block"></span>Normal&nbsp;' +
      '<span style="width:10px;height:10px;border-radius:2px;background:#d73a49;display:inline-block"></span>Outage' +
      '</span>';
  }

  document.getElementById('heatmap-plot').on('plotly_click',ev=>{
    if(ev.points.length) showDetail(ev.points[0].y);
  });
}

function showDetail(id) {
  const isFeeder = curTab === 'feeders';
  document.getElementById('detail-panel').style.display='';
  document.getElementById('detail-id').textContent = id;

  if (isFeeder) {
    // ── Feeder: 2-signal panel — Outage (red) | Current=0 (green) ─
    const out_ivs = IV.feeders[id];
    const cz_ivs  = CZ[id];
    const outTS   = Array.from(toTS30(out_ivs));   // 0=normal 1=outage
    const czTS    = Array.from(toTS30(cz_ivs));    // 0=I>0    1=I=0

    // Stats
    const outSlots = outTS.filter(v=>v).length;
    const czSlots  = czTS.filter(v=>v).length;
    let matchSlots = 0;
    for(let i=0;i<NH;i++) if(outTS[i]&&czTS[i]) matchSlots++;
    const matchPct = outSlots ? (matchSlots/outSlots*100).toFixed(1) : '—';
    const outH = totalH(out_ivs), czH = totalH(cz_ivs);

    document.getElementById('detail-meta').textContent =
      `🔴 Outage: ${fmtH(outH)} (${(outH/N*100).toFixed(1)}%)` +
      `   🟢 Current=0: ${fmtH(czH)} (${(czH/N*100).toFixed(1)}%)` +
      `   Match: ${matchPct}% of outage slots have I=0`;

    // Build step arrays for both signals
    const xs=[],outY=[],czY=[];
    for(let s=0;s<NH;s++){
      xs.push(s,s+1);
      outY.push(outTS[s], outTS[s]);
      czY.push(czTS[s],   czTS[s]);
    }
    // Offset current=0 band to y 1.5→2.5 so both bands are clearly separated
    const CZ_OFF = 1.6;  // baseline offset for current=0 band

    Plotly.react('detail-plot',[
      // ── Outage signal (red, y: 0→1) ──────────────────────────────
      {name:'Outage (0/1)', type:'scatter', mode:'lines',
       x:xs, y:outY,
       line:{color:'#d73a49', width:2, shape:'linear'},
       fill:'tozeroy', fillcolor:'rgba(215,58,73,0.20)',
       hoverinfo:'skip'},
      // ── Current=0 baseline (invisible anchor) ────────────────────
      {showlegend:false, type:'scatter', mode:'lines',
       x:xs, y:xs.map(()=>CZ_OFF),
       line:{color:'rgba(0,0,0,0)', width:0}, hoverinfo:'skip'},
      // ── Current=0 signal (green, y: 1.6→2.6) ────────────────────
      {name:'Current=0 (0/1)', type:'scatter', mode:'lines',
       x:xs, y:czY.map(v => CZ_OFF + v),
       line:{color:'#1a7f37', width:2, shape:'linear'},
       fill:'tonexty', fillcolor:'rgba(26,127,55,0.20)',
       hoverinfo:'skip'},
      // ── Hover markers — outage ────────────────────────────────────
      {showlegend:false, type:'scatter', mode:'markers',
       x:XIDX30, y:outTS,
       marker:{opacity:0, size:5},
       customdata:HLABELS30,
       text:outTS.map(v=>v?'🔴 Outage':'✅ Normal'),
       hovertemplate:'<b>%{customdata}</b><br>Outage: %{text}<extra></extra>'},
      // ── Hover markers — current=0 ─────────────────────────────────
      {showlegend:false, type:'scatter', mode:'markers',
       x:XIDX30, y:czTS.map(v=>CZ_OFF+v),
       marker:{opacity:0, size:5},
       customdata:HLABELS30,
       text:czTS.map(v=>v?'🟢 I=0 (de-energised)':'⚡ I>0 (energised)'),
       hovertemplate:'<b>%{customdata}</b><br>Current: %{text}<extra></extra>'},
    ],{
      paper_bgcolor:'#ffffff', plot_bgcolor:'#f6f8fa',
      margin:{l:70,r:14,t:6,b:28},
      hovermode:'x unified',
      showlegend:true,
      legend:{x:0, y:1.15, orientation:'h', font:{size:10}},
      xaxis:{tickvals:TVALS30, ticktext:TTEXT30, tickfont:{size:9,color:'#8c959f'},
             gridcolor:'#eaeef2', linecolor:'#d0d7de', range:[0,NH]},
      yaxis:{
        fixedrange:true,
        tickvals:[0, 1, CZ_OFF, CZ_OFF+1],
        ticktext:['Normal','Outage','I>0','I=0'],
        tickfont:{size:9, color:'#8c959f'},
        gridcolor:'rgba(200,200,200,0.4)',
        range:[-0.1, CZ_OFF+1.3],
      },
    },{responsive:true, displayModeBar:false});

  } else {
    // ── DTR / Consumer: hourly outage only ────────────────────────
    const ivs=IV[curTab][id], ts=toTS(ivs), th=totalH(ivs);
    let meta=`Outage: ${(th/N*100).toFixed(1)}% (${fmtH(th)} / 720h)`;
    if(curTab==='dtrs'&&FOD[id])      meta+=`  │  Feeder: ${FOD[id]} (score ${SFD[id]})`;
    if(curTab==='consumers'&&DOC[id]) meta+=`  │  DTR: ${DOC[id]} (score ${SDC[id]})`;
    document.getElementById('detail-meta').textContent=meta;
    const xs=[],ys=[];
    for(let h=0;h<N;h++){xs.push(h,h+1);ys.push(ts[h],ts[h]);}
    Plotly.react('detail-plot',[
      {type:'scatter',mode:'lines',x:xs,y:ys,
       line:{color:'#d73a49',width:1.5,shape:'linear'},
       fill:'tozeroy',fillcolor:'rgba(215,58,73,0.10)',hoverinfo:'skip'},
      {type:'scatter',mode:'markers',x:XIDX,y:Array.from(ts),
       marker:{opacity:0,size:6},customdata:HLABELS,
       text:Array.from(ts).map(v=>v?'Outage':'Normal'),
       hovertemplate:'<b>%{customdata}</b><br>%{text}<extra></extra>'},
    ],{
      paper_bgcolor:'#ffffff',plot_bgcolor:'#f6f8fa',
      margin:{l:50,r:14,t:4,b:28},showlegend:false,hovermode:'x unified',
      xaxis:{tickvals:TVALS,ticktext:TTEXT,tickfont:{size:9,color:'#8c959f'},
             gridcolor:'#eaeef2',linecolor:'#d0d7de',range:[0,N]},
      yaxis:{range:[-0.08,1.4],fixedrange:true,tickvals:[0,1],
             ticktext:['Normal','Outage'],tickfont:{size:9,color:'#8c959f'},gridcolor:'#eaeef2'},
    },{responsive:true,displayModeBar:false});
  }
}
function closeDetail(){
  document.getElementById('detail-panel').style.display='none';
  Plotly.purge('detail-plot');
}

// ══════════════════════════════════════════════════════════════════
//  SANKEY (MAPPING) VIEW
// ══════════════════════════════════════════════════════════════════

// ── Sankey data builders ──────────────────────────────────────────
function buildSankeyData_Feeder(feederId) {
  const MAX_DTRS=40, MAX_CONS=8;
  const allDtrs=FD[feederId]||[], showDtrs=allDtrs.slice(0,MAX_DTRS), remDtrs=allDtrs.length-showDtrs.length;
  const labels=[], nc=[], src=[], tgt=[], val=[], lc=[];
  labels.push(feederId); nc.push('#0969da');
  const fIdx=0;
  showDtrs.forEach(dtr=>{
    const dIdx=labels.length;
    labels.push(dtr.id); nc.push('#1a7f37');
    src.push(fIdx); tgt.push(dIdx); val.push(Math.max(1,Math.round(dtr.s*100))); lc.push('rgba(9,105,218,0.20)');
    const allCons=DC[dtr.id]||[], showCons=allCons.slice(0,MAX_CONS), remCons=allCons.length-showCons.length;
    showCons.forEach(con=>{
      const cIdx=labels.length;
      labels.push(con.id); nc.push('#e3b341');
      src.push(dIdx); tgt.push(cIdx); val.push(Math.max(1,Math.round(con.s*100))); lc.push('rgba(26,127,55,0.18)');
    });
    if(remCons>0){
      const rIdx=labels.length;
      labels.push(`+${remCons} more`); nc.push('#c6cdd5');
      src.push(dIdx); tgt.push(rIdx); val.push(Math.max(1,remCons)); lc.push('rgba(198,205,213,0.30)');
    }
  });
  if(remDtrs>0){
    const rIdx=labels.length;
    labels.push(`+${remDtrs} more DTRs`); nc.push('#c6cdd5');
    src.push(fIdx); tgt.push(rIdx); val.push(Math.max(1,remDtrs)); lc.push('rgba(198,205,213,0.30)');
  }
  return {labels,nc,src,tgt,val,lc};
}

function buildSankeyData_DTR(dtrId) {
  const MAX_CONS=20;
  const feeder=FOD[dtrId], allCons=DC[dtrId]||[], showCons=allCons.slice(0,MAX_CONS), remCons=allCons.length-showCons.length;
  const labels=[], nc=[], src=[], tgt=[], val=[], lc=[];
  let dIdx=0;
  if(feeder){
    labels.push(feeder); nc.push('#0969da');
    labels.push(dtrId);  nc.push('#1a7f37'); dIdx=1;
    src.push(0); tgt.push(1); val.push(Math.max(1,Math.round((SFD[dtrId]||0.8)*100))); lc.push('rgba(9,105,218,0.20)');
  } else {
    labels.push(dtrId); nc.push('#1a7f37');
  }
  showCons.forEach(con=>{
    const cIdx=labels.length;
    labels.push(con.id); nc.push('#e3b341');
    src.push(dIdx); tgt.push(cIdx); val.push(Math.max(1,Math.round(con.s*100))); lc.push('rgba(26,127,55,0.18)');
  });
  if(remCons>0){
    const rIdx=labels.length;
    labels.push(`+${remCons} more`); nc.push('#c6cdd5');
    src.push(dIdx); tgt.push(rIdx); val.push(Math.max(1,remCons)); lc.push('rgba(198,205,213,0.30)');
  }
  return {labels,nc,src,tgt,val,lc};
}

function buildSankeyData_Consumer(conId, dtrId, feederId) {
  const labels=[], nc=[], src=[], tgt=[], val=[], lc=[];
  if(feederId&&dtrId){
    labels.push(feederId,dtrId,conId); nc.push('#0969da','#1a7f37','#e3b341');
    src.push(0,1); tgt.push(1,2);
    val.push(Math.max(1,Math.round((SFD[dtrId]||0.8)*100)), Math.max(1,Math.round((SDC[conId]||0.8)*100)));
    lc.push('rgba(9,105,218,0.20)','rgba(26,127,55,0.18)');
  } else if(dtrId){
    labels.push(dtrId,conId); nc.push('#1a7f37','#e3b341');
    src.push(0); tgt.push(1); val.push(Math.max(1,Math.round((SDC[conId]||0.8)*100))); lc.push('rgba(26,127,55,0.18)');
  }
  return {labels,nc,src,tgt,val,lc};
}

function renderSankey(d) {
  const el=document.getElementById('sankey-plot');
  if(!el||!d.labels.length) return;
  Plotly.react(el,[{
    type:'sankey', orientation:'h', arrangement:'snap',
    node:{
      pad:10, thickness:14,
      line:{color:'rgba(255,255,255,0.6)',width:0.5},
      label:d.labels, color:d.nc,
      hovertemplate:'<b>%{label}</b><extra></extra>',
    },
    link:{
      source:d.src, target:d.tgt, value:d.val, color:d.lc,
      hovertemplate:'%{source.label} → %{target.label}<br>Strength: %{value:.0f}<extra></extra>',
    },
  }],{
    paper_bgcolor:'#ffffff', plot_bgcolor:'#ffffff',
    margin:{l:6,r:6,t:6,b:6},
    font:{size:9,family:'Courier New, monospace',color:'#1f2328'},
  },{responsive:true,displayModeBar:false});
}

const SANKEY_LEGEND = `
  <span class="g-leg-item"><span class="g-dot" style="background:#0969da"></span>Feeder</span>
  <span class="g-leg-item"><span class="g-dot" style="background:#1a7f37"></span>DTR</span>
  <span class="g-leg-item"><span class="g-dot" style="background:#e3b341"></span>Consumer</span>
  <span class="g-leg-item"><span class="g-dot" style="background:#c6cdd5;border:1px solid #d0d7de"></span>+N more (aggregated)</span>
  <span class="sankey-hint">Link thickness = score confidence</span>`;

// ── View builders ─────────────────────────────────────────────────
function buildFeederView(feederId) {
  const dtrs=FD[feederId]||[], totCon=dtrs.reduce((t,d)=>t+(DC[d.id]?DC[d.id].length:0),0), fp=pctStr(feederId,'feeders');
  setMapBar(feederId,'Feeder',
    `<span class="chip ch-blue">⚡ Feeder</span>`+
    `<span class="chip ch-green">${dtrs.length} DTRs</span>`+
    `<span class="chip ch-amber">${totCon.toLocaleString()} Consumers</span>`+
    `<span class="chip ch-red">${fp}% outage</span>`);
  document.getElementById('map-body').innerHTML=`
    <div class="graph-panel">
      <div class="sankey-legend">${SANKEY_LEGEND}</div>
      <div id="sankey-plot"></div>
    </div>
    <div class="tree-panel">
      <div class="tree-hdr">DTR → Consumer list</div>
      <div class="tree-scroll" id="tree-scroll">${buildFeederTree(feederId,dtrs)}</div>
    </div>`;
  renderSankey(buildSankeyData_Feeder(feederId));
}

function buildDTRView(dtrId) {
  const cons=DC[dtrId]||[], feeder=FOD[dtrId], dp=pctStr(dtrId,'dtrs');
  setMapBar(dtrId,'DTR',
    (feeder?`<span class="chip ch-blue">⚡ ${feeder}</span>`:'')+
    `<span class="chip ch-green">🔌 DTR</span>`+
    `<span class="chip ch-amber">${cons.length.toLocaleString()} Consumers</span>`+
    `<span class="chip ch-red">${dp}% outage</span>`);
  document.getElementById('map-body').innerHTML=`
    <div class="graph-panel">
      <div class="sankey-legend">${SANKEY_LEGEND}</div>
      <div id="sankey-plot"></div>
    </div>
    <div class="tree-panel">
      <div class="tree-hdr">Consumer list (${cons.length.toLocaleString()} total)</div>
      <div class="tree-scroll" id="tree-scroll">
        <div class="tree-root-node">
          <span>🔌</span>
          <span class="root-id">${dtrId}</span>
          <span class="root-chip" style="background:#dcfce7;color:#1a7f37;border:1px solid #bbf7d0">${cons.length.toLocaleString()} consumers</span>
        </div>
        <div id="cons-${dtrId}">${renderConBlock(dtrId,0)}</div>
      </div>
    </div>`;
  renderSankey(buildSankeyData_DTR(dtrId));
}

function buildConsumerView(conId) {
  const dtrId=DOC[conId], feederId=dtrId?FOD[dtrId]:null, cp=pctStr(conId,'consumers');
  const sibCount=dtrId?(DC[dtrId]||[]).length:0;
  setMapBar(conId,'Consumer',
    (feederId?`<span class="chip ch-blue">⚡ ${feederId}</span>`:'')+
    (dtrId?`<span class="chip ch-green">🔌 ${dtrId}</span>`:'')+
    `<span class="chip ch-amber">👤 Consumer</span>`+
    `<span class="chip ch-red">${cp}% outage</span>`);
  document.getElementById('map-body').innerHTML=`
    <div class="graph-panel">
      <div class="sankey-legend">${SANKEY_LEGEND}</div>
      <div id="sankey-plot"></div>
    </div>
    <div class="tree-panel">
      <div class="tree-hdr">Parent chain</div>
      <div class="tree-scroll" id="tree-scroll">
        ${buildChainCards(conId,dtrId,feederId)}
        ${dtrId?`<div style="padding:7px 10px 3px;font-size:10px;font-weight:600;color:#656d76;border-top:1px solid #eaeef2;margin-top:8px">Siblings in ${dtrId} (${sibCount.toLocaleString()})</div>
        <div id="cons-${dtrId}">${renderConBlock(dtrId,0)}</div>`:''}
      </div>
    </div>`;
  renderSankey(buildSankeyData_Consumer(conId,dtrId,feederId));
}

// ── Render mapping view ───────────────────────────────────────────
function renderMapping() {
  const ids=[...selected];
  const emEl=document.getElementById('map-empty'), coEl=document.getElementById('map-content');
  if(!ids.length){emEl.style.display='';coEl.style.display='none';return;}
  emEl.style.display='none'; coEl.style.display='flex';
  const id=ids[0];
  if(curTab==='feeders')      buildFeederView(id);
  else if(curTab==='dtrs')    buildDTRView(id);
  else                        buildConsumerView(id);
}

// ── Tree builder helpers ──────────────────────────────────────────
function buildFeederTree(feederId, dtrs) {
  const totCon = dtrs.reduce((t,d)=>t+(DC[d.id]?DC[d.id].length:0),0);
  let h = `<div class="tree-root-node">
    <span>⚡</span><span class="root-id">${feederId}</span>
    <span class="root-chip ch-green" style="background:#dcfce7;color:#1a7f37;border:1px solid #bbf7d0">${dtrs.length} DTRs</span>
    <span class="root-chip ch-amber" style="background:#fef9c3;color:#9a6700;border:1px solid #fde68a;margin-left:2px">${totCon.toLocaleString()} con</span>
  </div>`;
  dtrs.forEach(dtr => {
    const nCon = (DC[dtr.id]||[]).length;
    const dp   = pctStr(dtr.id,'dtrs');
    h += `<div class="dtr-row" id="drow-${dtr.id}" onclick="toggleDTRRow('${dtr.id}')">
      <span class="dtr-arrow" id="arr-${dtr.id}">▶</span>
      <span>🔌</span>
      <span class="dtr-id">${dtr.id}</span>
      <span class="cb ${cc(dtr.c)}">${cl(dtr.c)}</span>
      <span class="dtr-meta">${dp}% · ${nCon.toLocaleString()}</span>
    </div>
    <div id="cons-${dtr.id}" style="display:none">${renderConBlock(dtr.id,0)}</div>`;
  });
  return h;
}

function renderConBlock(dtrId, offset) {
  const cons=DC[dtrId]||[], show=cons.slice(offset,offset+50), rem=cons.length-(offset+50);
  let h='<div class="con-block">';
  show.forEach(con=>{
    const cp=pctStr(con.id,'consumers');
    h+=`<div class="con-row" onclick="jumpTo('${con.id}')">
      <span style="color:#8c959f;font-size:10px">👤</span>
      <span class="con-id">${con.id}</span>
      <span class="cb ${cc(con.c)}">${cl(con.c)}</span>
      <span class="con-meta">${con.s} · ${cp}%</span>
    </div>`;
  });
  if(rem>0) h+=`<div class="load-more-btn" onclick="loadMore('${dtrId}',${offset+50})">↓ Load ${Math.min(50,rem)} more of ${rem} remaining</div>`;
  return h+'</div>';
}

function toggleDTRRow(dtrId) {
  const el=document.getElementById('cons-'+dtrId);
  const ar=document.getElementById('arr-'+dtrId);
  const row=document.getElementById('drow-'+dtrId);
  if(el.style.display==='none'){el.style.display='';ar.textContent='▼';row.classList.add('open');}
  else{el.style.display='none';ar.textContent='▶';row.classList.remove('open');}
}

function loadMore(dtrId, offset) {
  const el=document.getElementById('cons-'+dtrId);
  const moreEl=el.querySelector('.load-more-btn');
  const tmp=document.createElement('div');
  tmp.innerHTML=renderConBlock(dtrId,offset);
  const newBlock=tmp.querySelector('.con-block');
  const existBlock=el.querySelector('.con-block');
  moreEl.remove();
  Array.from(newBlock.children).forEach(c=>existBlock.appendChild(c));
}

function jumpTo(conId) {
  document.getElementById('search-inp').value=conId;
  switchTab('consumers');
  selected.clear(); selected.add(conId);
  buildList(conId); updateSelCount(); renderMapping();
}
function jumpTo_dtr(dtrId) {
  document.getElementById('search-inp').value=dtrId;
  switchTab('dtrs');
  selected.clear(); selected.add(dtrId);
  buildList(dtrId); updateSelCount(); renderMapping();
}
function jumpTo_feeder(feederId) {
  document.getElementById('search-inp').value=feederId;
  switchTab('feeders');
  selected.clear(); selected.add(feederId);
  buildList(feederId); updateSelCount(); renderMapping();
}

function buildChainCards(conId, dtrId, feederId) {
  const cp=pctStr(conId,'consumers');
  const dp=dtrId?pctStr(dtrId,'dtrs'):'—';
  const fp=feederId?pctStr(feederId,'feeders'):'—';
  let h='';
  if(feederId) h+=`
    <div class="chain-card feeder-card" style="cursor:pointer" onclick="jumpTo_feeder('${feederId}')">
      <div class="chain-label">⚡ Feeder <span style="font-size:9px;color:#8c959f">(click to explore)</span></div>
      <div class="chain-id">${feederId}</div>
      <div class="chain-score">${fp}% outage</div>
    </div><div class="chain-arrow">↓</div>`;
  if(dtrId) h+=`
    <div class="chain-card dtr-card" style="cursor:pointer" onclick="jumpTo_dtr('${dtrId}')">
      <div class="chain-label">🔌 DTR &nbsp; score: ${SFD[dtrId]||'—'} <span style="font-size:9px;color:#8c959f">(click)</span></div>
      <div class="chain-id">${dtrId}</div>
      <div class="chain-score">${dp}% outage</div>
    </div><div class="chain-arrow">↓</div>`;
  h+=`
    <div class="chain-card con-card">
      <div class="chain-label">👤 Consumer &nbsp; score: ${SDC[conId]||'—'}</div>
      <div class="chain-id">${conId}</div>
      <div class="chain-score">${cp}% outage</div>
    </div>`;
  return h;
}

function setMapBar(title, sub, chipsHTML) {
  document.getElementById('map-title').textContent = title;
  document.getElementById('map-sub').textContent   = sub;
  document.getElementById('map-chips').innerHTML   = chipsHTML;
}

// ══════════════════════════════════════════════════════════════════
//  VERIFY VIEW  (two modes: Feeder→Consumer direct  |  DTR→Consumer)
// ══════════════════════════════════════════════════════════════════
let vfyMode = 'fc';

function setVerifyMode(mode) {
  vfyMode = mode;
  document.getElementById('vbtn-fc').classList.toggle('active', mode==='fc');
  document.getElementById('vbtn-dc').classList.toggle('active', mode==='dc');
  document.getElementById('vfy-fc-ctl').style.display = mode==='fc' ? 'flex' : 'none';
  document.getElementById('vfy-dc-ctl').style.display = mode==='dc' ? 'flex' : 'none';
  Plotly.purge('verify-plot');
  document.getElementById('vfy-empty').style.display = '';
  document.getElementById('vfy-info').textContent = '';
}

function initVerify() {
  // Populate feeder dropdown
  const fsel = document.getElementById('vfy-feeder');
  Object.keys(IV.feeders).sort().forEach(f => {
    const o = document.createElement('option'); o.value=f; o.textContent=f; fsel.appendChild(o);
  });
  // Populate DTR dropdown (show only DTRs with consumer data)
  const dsel = document.getElementById('vfy-dtr');
  Object.keys(DC).sort().forEach(d => {
    const o = document.createElement('option'); o.value=d; o.textContent=d; dsel.appendChild(o);
  });
}

function renderVerify() {
  if (vfyMode==='fc') renderVerifyFC(); else renderVerifyDC();
}

// ── Mode FC: Feeder → direct consumers (feeder_to_consumers mapping) ─
function renderVerifyFC() {
  const feederId = document.getElementById('vfy-feeder').value;
  const emEl = document.getElementById('vfy-empty');
  if (!feederId) { emEl.style.display=''; Plotly.purge('verify-plot'); return; }
  emEl.style.display = 'none';
  const MAX = Math.max(1, parseInt(document.getElementById('vfy-max-cons-fc').value)||50);
  const cons = (FC[feederId]||[]).slice(0, MAX);

  const rows=[], shapes=[];
  // Row 0: feeder (blue tint)
  rows.push({label:`⚡ ${feederId}`, ts:toTS(IV.feeders[feederId])});
  shapes.push({type:'rect',xref:'paper',x0:0,x1:1,yref:'y',
    y0:-0.5,y1:0.5,fillcolor:'rgba(9,105,218,0.07)',line:{width:0},layer:'below'});
  // Separator
  shapes.push({type:'line',xref:'paper',x0:0,x1:1,yref:'y',
    y0:0.5,y1:0.5,line:{color:'#c6cdd5',width:1.5},layer:'above'});
  // Consumer rows (amber tint)
  cons.forEach((con,i) => {
    const ri = i+1;
    shapes.push({type:'rect',xref:'paper',x0:0,x1:1,yref:'y',
      y0:ri-0.5,y1:ri+0.5,fillcolor:'rgba(227,179,65,0.05)',line:{width:0},layer:'below'});
    rows.push({label:`  👤 ${con.id}`, ts:toTS(IV.consumers[con.id])});
  });

  document.getElementById('vfy-info').textContent =
    `${rows.length} rows · feeder row + ${cons.length} direct consumers`+
    (FC[feederId]&&FC[feederId].length>MAX ? ` (of ${FC[feederId].length} total)` : '');
  renderVerifyHeatmap(rows, shapes);
}

// ── Mode DC: DTR → consumers ──────────────────────────────────────
function renderVerifyDC() {
  const dtrId = document.getElementById('vfy-dtr').value;
  const emEl  = document.getElementById('vfy-empty');
  if (!dtrId) { emEl.style.display=''; Plotly.purge('verify-plot'); return; }
  emEl.style.display = 'none';
  const MAX = Math.max(1, parseInt(document.getElementById('vfy-max-cons-dc').value)||30);
  const allCons = DC[dtrId]||[], cons = allCons.slice(0, MAX);

  const rows=[], shapes=[];
  // Row 0: DTR (green tint)
  rows.push({label:`🔌 ${dtrId}`, ts:toTS(IV.dtrs[dtrId])});
  shapes.push({type:'rect',xref:'paper',x0:0,x1:1,yref:'y',
    y0:-0.5,y1:0.5,fillcolor:'rgba(26,127,55,0.07)',line:{width:0},layer:'below'});
  // Separator
  shapes.push({type:'line',xref:'paper',x0:0,x1:1,yref:'y',
    y0:0.5,y1:0.5,line:{color:'#c6cdd5',width:1.5},layer:'above'});
  // Consumer rows
  cons.forEach((con,i) => {
    const ri = i+1;
    shapes.push({type:'rect',xref:'paper',x0:0,x1:1,yref:'y',
      y0:ri-0.5,y1:ri+0.5,fillcolor:'rgba(227,179,65,0.05)',line:{width:0},layer:'below'});
    rows.push({label:`  👤 ${con.id}`, ts:toTS(IV.consumers[con.id])});
  });

  document.getElementById('vfy-info').textContent =
    `${rows.length} rows · DTR row + ${cons.length} consumers`+
    (allCons.length>MAX ? ` (of ${allCons.length} total)` : '');
  renderVerifyHeatmap(rows, shapes);
}

// ── Shared heatmap renderer ───────────────────────────────────────
function renderVerifyHeatmap(rows, shapes) {
  const z     = rows.map(r => Array.from(r.ts));
  const yidx  = rows.map((_,i) => i);
  const ytext = rows.map(r => r.label);
  const n     = rows.length;
  const htxt  = z.map(row => row.map(v => v ? '🔴 Outage' : '✅ Normal'));
  const ygap  = n>100 ? 0 : n>40 ? 0.3 : 0.5;
  const tfsz  = n>100 ? 7 : n>50  ? 8 : 9;
  // Use newPlot (purge first) so Plotly always measures the container correctly
  Plotly.purge('verify-plot');
  Plotly.newPlot('verify-plot',[{
    type:'heatmap', z, y:yidx, x:XIDX,
    colorscale:[[0,'#f0f3f6'],[1,'#d73a49']],
    showscale:false, xgap:0, ygap,
    // simple hover using y (device label) + x (hour index mapped to label)
    hovertemplate:'<b>%{y}</b><br>%{text}<extra></extra>',
    text:htxt,
  }],{
    paper_bgcolor:'#ffffff', plot_bgcolor:'#ffffff',
    margin:{l:175,r:10,t:10,b:46},
    xaxis:{tickvals:TVALS,ticktext:TTEXT,tickfont:{size:10,color:'#8c959f'},
           gridcolor:'#eaeef2',linecolor:'#d0d7de',range:[0,N]},
    yaxis:{tickvals:yidx,ticktext:ytext,range:[n-0.5,-0.5],
           fixedrange:true,tickfont:{size:tfsz,family:'Courier New',color:'#1f2328'},
           gridcolor:'rgba(0,0,0,0)'},
    shapes,
  },{responsive:true,displayModeBar:false});
}

// ── Init ──────────────────────────────────────────────────────────
document.getElementById('s-f').textContent = Object.keys(IV.feeders).length;
document.getElementById('s-d').textContent = Object.keys(IV.dtrs).length.toLocaleString();
document.getElementById('s-c').textContent = Object.keys(IV.consumers).length.toLocaleString();

// ── Trend banner ──────────────────────────────────────────────────
if (TS && TS.total_feeders > 0) {
  const nMatch = TS.current_zero_match;
  const nOut   = TS.feeders_with_outage;
  document.getElementById('tb-total').textContent  = TS.total_feeders;
  document.getElementById('tb-bl-ok').textContent  = `${nMatch}/${nOut}`;
  document.getElementById('tb-gap').textContent    = TS.feeders_with_bl_gap;
  document.getElementById('tb-lost').textContent   = TS.avg_pct_zero_during + '%';
  const noteEl = document.getElementById('tb-note');
  noteEl.innerHTML =
    '<b style="color:#1a7f37">Auto-Indexing Validated:</b> ' + nMatch + '/' + nOut +
    ' feeders confirm <b>Current R=Y=B → 0 during outage</b> (avg ' + TS.avg_pct_zero_during + '% of outage slots). ' +
    'BL records ARE transmitted during outage, but current values drop to zero — ' +
    'this is the correct signal for auto-indexing. ' +
    (TS.feeders_with_bl_gap > 0
      ? TS.feeders_with_bl_gap + ' feeders have BL data gaps (Apr 4 system issue).'
      : '');
  // update label for 4th card
  document.querySelector('#tb-lost').closest('.trend-card').querySelector('.trend-lbl').innerHTML =
    'Avg I=0<br>during outage';
  document.querySelector('#tb-bl-ok').closest('.trend-card').querySelector('.trend-lbl').innerHTML =
    'I=0 trend<br>confirmed';
  document.getElementById('trend-banner').style.display = 'flex';
}

initVerify();
switchTab('feeders');
</script>
</body>
</html>
"""

html = HTML.replace("__PAYLOAD__", payload)
out  = DATA_DIR + "outage_dashboard2.html"
with open(out, "w", encoding="utf-8") as f:
    f.write(html)

size_mb = os.path.getsize(out) / 1024 / 1024
print(f"\nDashboard written: {out}")
print(f"File size: {size_mb:.1f} MB")
print(f'\nOpen with:  open "{out}"')
