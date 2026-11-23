"""Level band, flood-pool flag and trend (plan §4.2; exact thresholds). Pure functions.

Band, when % full is available (storage based):     when only elevation and full pool are known:
  above_full  elevation >= full pool + 0.50 ft         same
  full        % >= 97.0 (and below + 0.50 ft)          0.50 ft above .. 1.00 ft below full pool
  near_full   85.0 <= % < 97.0                         1.01-3.00 ft below
  below_full  60.0 <= % < 85.0                         3.01-10.00 ft below
  low         30.0 <= % < 60.0                         10.01-25.00 ft below
  very_low    % < 30.0                                 more than 25.00 ft below
  level_only  no full-pool value (and no % full) on record
Flood pool: flood_control and elevation >= full pool + 2.00 ft.
Trend from the 24-hour change: >= +1.00 rising_fast; +0.05..+0.99 rising; -0.04..+0.04 steady;
-0.05..-0.99 falling; <= -1.00 falling_fast; None when there is no change figure.

Per-lake `bands` override (lakes.json), any subset of:
  {"above_ft": 0.5, "pct": [97, 85, 60, 30], "ft_below": [1.0, 3.0, 10.0, 25.0], "flood_ft": 2.0}
"""

BAND_LABEL = {"above_full": "Above full pool", "full": "Full", "near_full": "Near full",
              "below_full": "Below full", "low": "Low", "very_low": "Very low", "level_only": "Level only"}
TREND_LABEL = {"rising_fast": "Rising fast", "rising": "Rising", "steady": "Steady", "falling": "Falling",
               "falling_fast": "Falling fast"}
BANDS = tuple(BAND_LABEL)
TRENDS = tuple(TREND_LABEL)
DEFAULT_BANDS = {"above_ft": 0.50, "pct": [97.0, 85.0, 60.0, 30.0], "ft_below": [1.00, 3.00, 10.00, 25.00],
                 "flood_ft": 2.00}
ORDER = ["full", "near_full", "below_full", "low"]


def _cfg(bands):
    cfg = dict(DEFAULT_BANDS)
    if bands:
        cfg.update({k: v for k, v in bands.items() if k in DEFAULT_BANDS and v is not None})
    return cfg


def _r2(x):
    return round(float(x) + 0.0, 2)


def vs_full_pool(elev_ft, full_pool_ft):
    if elev_ft is None or full_pool_ft is None:
        return None
    return _r2(elev_ft - full_pool_ft)


def band(elev_ft, full_pool_ft, pct_full, flood_control, bands=None):
    """One of above_full|full|near_full|below_full|low|very_low|level_only."""
    cfg = _cfg(bands)
    if elev_ft is not None and full_pool_ft is not None and _r2(elev_ft - full_pool_ft) >= cfg["above_ft"]:
        return "above_full"
    if pct_full is not None:
        p = round(float(pct_full), 1)
        for name, limit in zip(ORDER, cfg["pct"]):
            if p >= limit:
                return name
        return "very_low"
    if elev_ft is None or full_pool_ft is None:
        return "level_only"
    below = _r2(full_pool_ft - elev_ft)  # positive = below full pool
    for name, limit in zip(ORDER, cfg["ft_below"]):
        if below <= limit:
            return name
    return "very_low"


def in_flood_pool(elev_ft, full_pool_ft, flood_control, bands=None):
    if not flood_control or elev_ft is None or full_pool_ft is None:
        return False
    return _r2(elev_ft - full_pool_ft) >= _cfg(bands)["flood_ft"]


def trend(change_24h_ft):
    """rising_fast|rising|steady|falling|falling_fast, or None without a 24-hour change."""
    if change_24h_ft is None:
        return None
    c = _r2(change_24h_ft)
    if c >= 1.00:
        return "rising_fast"
    if c >= 0.05:
        return "rising"
    if c > -0.05:
        return "steady"
    if c > -1.00:
        return "falling"
    return "falling_fast"
