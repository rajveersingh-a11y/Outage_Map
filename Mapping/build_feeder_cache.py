"""One-off: build dashboard2_feeder_cache.pkl (run once, then API loads feeders in ~1s)."""
from dashboard2_api import FEEDER_CACHE, _load_feeders_from_xlsb, _save_pickle

if __name__ == "__main__":
    print("Building feeder cache …")
    blob = _load_feeders_from_xlsb()
    _save_pickle(FEEDER_CACHE, blob)
    print("Done:", FEEDER_CACHE)
