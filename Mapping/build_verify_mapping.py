"""Build verify feeder→consumer mapping (one primary outage event per feeder)."""
from topology_mapping import (
    DATA_DIR,
    build_verify_feeder_consumer_mapping,
    load_event_feeder_outage_xlsb,
    load_file,
)
import os


def main() -> None:
    outage_xlsb = DATA_DIR + "Event Feeder Outage Data of April'26.xlsb"
    con_csv = DATA_DIR + "kashi_april_2026_consumer_outage.csv"
    if not os.path.exists(outage_xlsb):
        raise SystemExit(f"Missing feeder outage file: {outage_xlsb}")
    if not os.path.exists(con_csv):
        raise SystemExit(f"Missing consumer outage file: {con_csv}")

    print("Loading feeder + consumer outage data …")
    feeders = load_event_feeder_outage_xlsb(outage_xlsb)
    consumers = load_file(con_csv)
    print(f"  Feeders:   {feeders['device_id'].nunique()} devices")
    print(f"  Consumers: {consumers['device_id'].nunique()} devices")
    print("\nBuilding verify map …")
    vmap = build_verify_feeder_consumer_mapping(feeders, consumers, out_dir=DATA_DIR)
    print(f"\nDone — {len(vmap)} feeders with matched consumers.")


if __name__ == "__main__":
    main()
