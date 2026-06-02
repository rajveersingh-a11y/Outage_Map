"""
Smart Meter Outage-Based Topology Mapping
Feeder → DT → Consumer hierarchy via co-occurrence scoring
"""

import pandas as pd
import numpy as np
from datetime import timedelta
from pathlib import Path
import os
import warnings
warnings.filterwarnings("ignore")

# ── Config ─────────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
DATA_DIR = str(HERE) + os.sep
LAG_WINDOW_MIN   = 15     # max minutes a consumer/DT can lag behind upstream event
TIME_TOL_MIN     = 30     # tolerance window for start-time alignment
MIN_OVERLAP_FRAC = 0.05   # minimum overlap fraction to count as co-occurrence

# Scoring weights  (must sum to 1 for α+β+γ interpretation)
ALPHA = 0.40   # temporal overlap ratio
BETA  = 0.25   # duration similarity
GAMMA = 0.35   # co-occurrence frequency (normalised)
DELTA = 0.10   # lag penalty subtraction

CONFIRM_THRESHOLD = 0.60   # score ≥ this → confirmed
REVIEW_THRESHOLD  = 0.35   # score ≥ this → review; below → low-confidence

# ── Datetime parsing ───────────────────────────────────────────────────────────
def parse_oracle_dt(s: str) -> pd.Timestamp:
    """Parse '03-APR-26 06.31.00.000000000 PM' → Timestamp (assumes 20xx)."""
    s = s.strip()
    # normalise dots in time part to colons
    parts = s.split(" ")
    if len(parts) == 3:
        date_part, time_part, ampm = parts
        time_part = time_part.split(".")[0:3]
        time_str  = ":".join(time_part[:3]) + " " + ampm
        return pd.to_datetime(f"{date_part} {time_str}", format="%d-%b-%y %I:%M:%S %p")
    return pd.NaT


def load_file(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["start"] = df["OCCU"].apply(parse_oracle_dt)
    df["end"]   = df["RESTO"].apply(parse_oracle_dt)
    df = df.dropna(subset=["start", "end"])
    df["duration_min"] = (df["end"] - df["start"]).dt.total_seconds() / 60
    df = df[df["duration_min"] > 0].copy()
    df = df.rename(columns={"DEVICE_ID_clean": "device_id"})
    return df[["device_id", "start", "end", "duration_min", "MeterCategory"]]


def load_event_feeder_outage_xlsb(path: str) -> pd.DataFrame:
    """
    Loader for: 'Event Feeder Outage Data of April'26.xlsb'
    Columns:
      - Meter No
      - Occurrence Date Time  (dd-mm-YYYY HH:MM:SS)
      - Restoration Date Time (dd-mm-YYYY HH:MM:SS)
      - Tripping Duration (In Minutes)
    """
    df = pd.read_excel(path, engine="pyxlsb", sheet_name=0)
    df = df.rename(
        columns={
            "Meter No": "device_id",
            "Occurrence Date Time": "occu",
            "Restoration Date Time": "resto",
        }
    )
    df["start"] = pd.to_datetime(df["occu"], format="%d-%m-%Y %H:%M:%S", errors="coerce")
    df["end"] = pd.to_datetime(df["resto"], format="%d-%m-%Y %H:%M:%S", errors="coerce")
    df = df.dropna(subset=["device_id", "start", "end"])
    df["duration_min"] = (df["end"] - df["start"]).dt.total_seconds() / 60
    df = df[df["duration_min"] > 0].copy()
    df["device_id"] = df["device_id"].astype(str).str.strip()
    df["MeterCategory"] = "FEEDER"
    return df[["device_id", "start", "end", "duration_min", "MeterCategory"]]


def build_blockload_validation(blockload_xlsb: str, feeder_outages: pd.DataFrame) -> pd.DataFrame:
    """
    Joins feeder outage windows with blockload signal (Current R/Y/B all zero) and
    returns per-feeder validation metrics.
    """
    _dfs = [
        pd.read_excel(blockload_xlsb, engine="pyxlsb", sheet_name=s)
        for s in ["Sheet1", "Sheet2", "Sheet3"]
    ]
    bl = pd.concat(_dfs, ignore_index=True)
    bl["RTC DTTM"] = pd.to_datetime(bl["RTC DTTM"], errors="coerce")
    bl = bl.dropna(subset=["RTC DTTM", "Device Id"])
    bl["Device Id"] = bl["Device Id"].astype(str).str.strip()
    bl["current_zero"] = (bl["Current R"] == 0) & (bl["Current Y"] == 0) & (bl["Current B"] == 0)

    start_month = pd.Timestamp(2026, 4, 1)
    bl["slot"] = ((bl["RTC DTTM"] - start_month).dt.total_seconds() / 1800).astype("Int64")
    bl = bl[(bl["slot"] >= 0) & (bl["slot"] < 1440)].copy()

    # Precompute outage slots per feeder (30-min slots)
    out_by_feeder = {}
    for fid, g in feeder_outages.groupby("device_id"):
        slots = set()
        for _, row in g.iterrows():
            sh = max(0, int(np.floor((row["start"] - start_month).total_seconds() / 1800)))
            eh = min(1440, int(np.ceil((row["end"] - start_month).total_seconds() / 1800)))
            for s in range(sh, eh):
                slots.add(s)
        out_by_feeder[fid] = slots

    rows = []
    for fid, g in bl.groupby("Device Id"):
        out_slots = out_by_feeder.get(fid, set())
        during = g[g["slot"].isin(list(out_slots))] if out_slots else g.iloc[0:0]
        outside = g[~g["slot"].isin(list(out_slots))] if out_slots else g

        n_d = int(len(during))
        z_d = int(during["current_zero"].sum()) if n_d else 0
        n_o = int(len(outside))
        z_o = int(outside["current_zero"].sum()) if n_o else 0

        pct_zero_during = round(z_d / n_d * 100, 1) if n_d else 0.0
        pct_zero_outside = round(z_o / n_o * 100, 1) if n_o else 0.0
        rows.append(
            {
                "feeder": fid,
                "outage_slots": len(out_slots),
                "bl_records_during_outage": n_d,
                "pct_current_zero_during_outage": pct_zero_during,
                "pct_current_zero_outside_outage": pct_zero_outside,
                "trend_match_ge_80pct": bool(pct_zero_during >= 80.0) if n_d else False,
            }
        )
    return pd.DataFrame(rows).sort_values(["trend_match_ge_80pct", "pct_current_zero_during_outage"], ascending=[False, False])


# ── Core overlap scoring ───────────────────────────────────────────────────────
def compute_overlap(up_start, up_end, dn_start, dn_end):
    """Returns (overlap_minutes, time_overlap_ratio, lag_minutes)."""
    # allow downstream to start slightly after upstream (lag)
    effective_up_start = up_start - timedelta(minutes=LAG_WINDOW_MIN)
    overlap_start = max(effective_up_start, dn_start)
    overlap_end   = min(up_end, dn_end)
    overlap_min   = max(0, (overlap_end - overlap_start).total_seconds() / 60)
    # ratio = overlap / downstream duration
    dn_dur = (dn_end - dn_start).total_seconds() / 60
    ratio  = overlap_min / dn_dur if dn_dur > 0 else 0
    # lag = how many minutes after upstream start did downstream start
    lag = max(0, (dn_start - up_start).total_seconds() / 60)
    return overlap_min, ratio, lag


def duration_similarity(dur_up, dur_dn):
    """1 - normalised absolute difference, clamped to [0,1]."""
    if dur_up <= 0:
        return 0
    ratio = min(dur_up, dur_dn) / max(dur_up, dur_dn)
    return ratio  # already in [0,1]


# ── Pair scoring across all events ─────────────────────────────────────────────
def score_pairs(upstream_df: pd.DataFrame, downstream_df: pd.DataFrame,
                label: str = "") -> pd.DataFrame:
    """
    For every (upstream_device, downstream_device) pair compute aggregate score.
    Returns DataFrame with columns: up_id, dn_id, score, n_events, confidence_flag
    """
    up_events = upstream_df.groupby("device_id").apply(
        lambda g: list(zip(g["start"], g["end"], g["duration_min"]))
    ).to_dict()
    dn_events = downstream_df.groupby("device_id").apply(
        lambda g: list(zip(g["start"], g["end"], g["duration_min"]))
    ).to_dict()

    up_ids = list(up_events.keys())
    dn_ids = list(dn_events.keys())

    print(f"  Scoring {label}: {len(up_ids)} upstream × {len(dn_ids)} downstream")

    records = []
    for up_id in up_ids:
        u_evs = up_events[up_id]
        n_up  = len(u_evs)

        for dn_id in dn_ids:
            d_evs = dn_events[dn_id]

            overlaps, dur_sims, lags = [], [], []

            for (us, ue, ud) in u_evs:
                for (ds, de, dd) in d_evs:
                    # Quick gate: starts must be within LAG_WINDOW + TIME_TOL
                    gap = abs((ds - us).total_seconds() / 60)
                    if gap > (LAG_WINDOW_MIN + TIME_TOL_MIN):
                        continue
                    ov_min, ov_ratio, lag = compute_overlap(us, ue, ds, de)
                    if ov_ratio < MIN_OVERLAP_FRAC:
                        continue
                    overlaps.append(ov_ratio)
                    dur_sims.append(duration_similarity(ud, dd))
                    lags.append(lag)

            if not overlaps:
                continue

            n_matched = len(overlaps)
            freq_score = min(1.0, n_matched / max(n_up, 1))  # normalised

            avg_overlap  = np.mean(overlaps)
            avg_dur_sim  = np.mean(dur_sims)
            avg_lag_min  = np.mean(lags)
            lag_penalty  = min(1.0, avg_lag_min / LAG_WINDOW_MIN)

            score = (ALPHA * avg_overlap
                     + BETA  * avg_dur_sim
                     + GAMMA * freq_score
                     - DELTA * lag_penalty)
            score = float(np.clip(score, 0, 1))

            records.append({
                "up_id":       up_id,
                "dn_id":       dn_id,
                "score":       round(score, 4),
                "n_events":    n_matched,
                "avg_overlap": round(avg_overlap, 3),
                "avg_dur_sim": round(avg_dur_sim, 3),
                "avg_lag_min": round(avg_lag_min, 1),
            })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records).sort_values(["up_id", "score"], ascending=[True, False])
    df["confidence"] = pd.cut(
        df["score"],
        bins=[-np.inf, REVIEW_THRESHOLD, CONFIRM_THRESHOLD, np.inf],
        labels=["LOW – field verify", "MEDIUM – review", "HIGH – confirmed"]
    )
    return df


# ── Best-match selection ───────────────────────────────────────────────────────
def best_mapping(score_df: pd.DataFrame, col_up="up_id", col_dn="dn_id") -> pd.DataFrame:
    """Return top-scoring upstream for each downstream device."""
    if score_df.empty:
        return pd.DataFrame()
    idx = score_df.groupby(col_dn)["score"].idxmax()
    return score_df.loc[idx].sort_values("score", ascending=False).reset_index(drop=True)


# ── Hierarchical consistency boost/penalty ─────────────────────────────────────
def apply_hierarchy_consistency(feeder_dt: pd.DataFrame,
                                dt_consumer: pd.DataFrame,
                                feeder_consumer: pd.DataFrame) -> pd.DataFrame:
    """
    If consumer → DT-X and DT-X → Feeder-Y, check consumer also aligns with Feeder-Y.
    Boost score if consistent, penalise if not.
    """
    if feeder_dt.empty or dt_consumer.empty or feeder_consumer.empty:
        return feeder_consumer

    # best feeder per DT
    best_ft = best_mapping(feeder_dt, "up_id", "dn_id").set_index("dn_id")["up_id"].to_dict()
    # best DT per consumer
    best_dt = best_mapping(dt_consumer, "up_id", "dn_id").set_index("dn_id")["up_id"].to_dict()

    def adjust(row):
        consumer  = row["dn_id"]
        feeder_sc = row["up_id"]
        dt_of_consumer = best_dt.get(consumer)
        if dt_of_consumer is None:
            return row["score"]
        feeder_of_dt = best_ft.get(dt_of_consumer)
        if feeder_of_dt is None:
            return row["score"]
        if feeder_of_dt == feeder_sc:
            return min(1.0, row["score"] * 1.10)   # +10% consistency bonus
        else:
            return row["score"] * 0.85              # -15% inconsistency penalty

    fc = feeder_consumer.copy()
    fc["score"] = fc.apply(adjust, axis=1).round(4)
    fc["confidence"] = pd.cut(
        fc["score"],
        bins=[-np.inf, REVIEW_THRESHOLD, CONFIRM_THRESHOLD, np.inf],
        labels=["LOW – field verify", "MEDIUM – review", "HIGH – confirmed"]
    )
    return fc.sort_values(["up_id", "score"], ascending=[True, False])


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("Loading data …")

    outage_xlsb = DATA_DIR + "Event Feeder Outage Data of April'26.xlsb"
    feeders = load_event_feeder_outage_xlsb(outage_xlsb)

    # If you later provide DTR/consumer outage CSVs (old format), this script can
    # still run the original topology mapping pipeline.
    dtr_csv = DATA_DIR + "kashi_april_2026_dtr_outage.csv"
    con_csv = DATA_DIR + "kashi_april_2026_consumer_outage.csv"
    if os.path.exists(dtr_csv) and os.path.exists(con_csv):
        dtrs = load_file(dtr_csv)
        consumers = load_file(con_csv)
    else:
        dtrs = pd.DataFrame(columns=["device_id", "start", "end", "duration_min", "MeterCategory"])
        consumers = pd.DataFrame(columns=["device_id", "start", "end", "duration_min", "MeterCategory"])

    print(f"  Feeders:   {len(feeders):,} events, {feeders['device_id'].nunique()} devices")
    print(f"  DTRs:      {len(dtrs):,} events, {dtrs['device_id'].nunique()} devices")
    print(f"  Consumers: {len(consumers):,} events, {consumers['device_id'].nunique()} devices")

    # Always emit a normalized feeder outage export for reuse.
    feeders_out = feeders[["device_id", "start", "end", "duration_min"]].copy()
    feeders_out.to_csv(DATA_DIR + "feeder_outage_events_normalized.csv", index=False)
    print(f"  Wrote: {DATA_DIR}feeder_outage_events_normalized.csv")

    # Blockload validation summary (Current=0 trend during outage)
    bl_xlsb = DATA_DIR + "FEEDER BLOCKLOAD DATA_April'26.xlsb"
    if os.path.exists(bl_xlsb):
        vdf = build_blockload_validation(bl_xlsb, feeders)
        vdf.to_csv(DATA_DIR + "feeder_blockload_validation.csv", index=False)
        print(f"  Wrote: {DATA_DIR}feeder_blockload_validation.csv")
    else:
        print("  Blockload file not found; skipping validation CSV.")

    # If we don't have downstream outages, stop after the feeder + blockload outputs.
    if dtrs.empty or consumers.empty:
        print("\nNo DTR/Consumer outage inputs found. Topology mapping steps skipped.")
        print("Done.")
        return

    # ── Step 1: Feeder → DTR ─────────────────────────────────────────────────
    print("\n[1/3] Scoring Feeder → DTR …")
    feeder_dt_scores = score_pairs(feeders, dtrs, "Feeder→DTR")

    # ── Step 2: DTR → Consumer ───────────────────────────────────────────────
    print("\n[2/3] Scoring DTR → Consumer …")
    dt_consumer_scores = score_pairs(dtrs, consumers, "DTR→Consumer")

    # ── Step 3: Feeder → Consumer (direct, for consistency check) ────────────
    print("\n[3/3] Scoring Feeder → Consumer …")
    feeder_consumer_scores = score_pairs(feeders, consumers, "Feeder→Consumer")

    # ── Hierarchy consistency adjustment ─────────────────────────────────────
    print("\nApplying hierarchical consistency …")
    feeder_consumer_scores = apply_hierarchy_consistency(
        feeder_dt_scores, dt_consumer_scores, feeder_consumer_scores
    )

    # ── Output ────────────────────────────────────────────────────────────────
    out_dir = DATA_DIR

    # Full score matrices
    if not feeder_dt_scores.empty:
        feeder_dt_scores.to_csv(out_dir + "feeder_dt_scores.csv", index=False)
    if not dt_consumer_scores.empty:
        dt_consumer_scores.to_csv(out_dir + "dt_consumer_scores.csv", index=False)
    if not feeder_consumer_scores.empty:
        feeder_consumer_scores.to_csv(out_dir + "feeder_consumer_scores.csv", index=False)

    # Best mapping per downstream device
    best_fd   = best_mapping(feeder_dt_scores)
    best_dc   = best_mapping(dt_consumer_scores)
    best_fc   = best_mapping(feeder_consumer_scores)

    if not best_fd.empty:
        best_fd.to_csv(out_dir + "best_feeder_per_dtr.csv", index=False)
    if not best_dc.empty:
        best_dc.to_csv(out_dir + "best_dtr_per_consumer.csv", index=False)
    if not best_fc.empty:
        best_fc.to_csv(out_dir + "best_feeder_per_consumer.csv", index=False)

    # Summary report
    print("\n" + "="*65)
    print("TOPOLOGY MAPPING SUMMARY")
    print("="*65)

    for label, df, up_col, dn_col in [
        ("Feeder → DTR",      best_fd,  "up_id", "dn_id"),
        ("DTR → Consumer",    best_dc,  "up_id", "dn_id"),
        ("Feeder → Consumer", best_fc,  "up_id", "dn_id"),
    ]:
        if df.empty:
            print(f"\n{label}: no matches found")
            continue
        counts = df["confidence"].value_counts()
        print(f"\n{label}  ({len(df)} mappings)")
        print(f"  HIGH – confirmed  : {counts.get('HIGH – confirmed', 0)}")
        print(f"  MEDIUM – review   : {counts.get('MEDIUM – review', 0)}")
        print(f"  LOW – field verify: {counts.get('LOW – field verify', 0)}")
        top5 = df.head(5)[["up_id", "dn_id", "score", "n_events", "confidence"]]
        print(f"  Top-5 by score:")
        print(top5.to_string(index=False))

    # Low-confidence flag file
    low_conf_rows = []
    for label, df in [("Feeder→DTR", best_fd),
                       ("DTR→Consumer", best_dc),
                       ("Feeder→Consumer", best_fc)]:
        if df.empty:
            continue
        lc = df[df["confidence"] == "LOW – field verify"].copy()
        lc.insert(0, "mapping_type", label)
        low_conf_rows.append(lc)

    if low_conf_rows:
        low_conf = pd.concat(low_conf_rows)
        low_conf.to_csv(out_dir + "low_confidence_flags.csv", index=False)
        print(f"\nLow-confidence devices flagged for field verification: {len(low_conf)}")

    print(f"\nOutput files written to: {out_dir}")
    print("  feeder_dt_scores.csv")
    print("  dt_consumer_scores.csv")
    print("  feeder_consumer_scores.csv")
    print("  best_feeder_per_dtr.csv")
    print("  best_dtr_per_consumer.csv")
    print("  best_feeder_per_consumer.csv")
    print("  low_confidence_flags.csv")
    print("Done.")


if __name__ == "__main__":
    main()
