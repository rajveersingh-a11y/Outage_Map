"""One-off: build dashboard2_consumer_cache.pkl (run once, then API loads instantly)."""
from dashboard2_api import CONSUMER_CACHE, _load_consumers_from_csv, _save_pickle

if __name__ == "__main__":
    print("Building consumer cache …")
    blob = _load_consumers_from_csv()
    _save_pickle(CONSUMER_CACHE, blob)
    print("Done:", CONSUMER_CACHE)
