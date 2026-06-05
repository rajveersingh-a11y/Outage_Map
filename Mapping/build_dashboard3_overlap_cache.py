"""Build dashboard3 feeder + consumer overlap caches (run once for instant Dashboard 3 lists)."""
from dashboard3_api import (
    CONSUMER_OVERLAP_CACHE,
    FEEDER_OVERLAP_CACHE,
    _build_consumer_overlap,
    _build_feeder_overlap,
    _save_overlap_cache,
)
import dashboard2_api as d2


def main() -> None:
    print("Loading dashboard2 caches …")
    d2.ensure_load_started()
    while d2.FEEDERS is None or d2.CONSUMERS is None:
        import time

        time.sleep(1)

    print("Building feeder overlap …")
    fpct = _build_feeder_overlap()
    _save_overlap_cache(FEEDER_OVERLAP_CACHE, fpct, "feeder")

    print("Building consumer overlap …")
    cpct = _build_consumer_overlap()
    _save_overlap_cache(CONSUMER_OVERLAP_CACHE, cpct, "consumer")

    print("Done.")
    print(" ", FEEDER_OVERLAP_CACHE)
    print(" ", CONSUMER_OVERLAP_CACHE)


if __name__ == "__main__":
    main()
