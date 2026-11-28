"""Turn raw data into the report's metric table.

The first four metrics need only the vehicle_data polling. The rest need Fleet
Telemetry signals; without them the metric is reported as "not collected"
rather than guessed.

Status thresholds below are rules of thumb, NOT Tesla specifications. Tune them
against cars whose condition you know before relying on the colour coding.
"""
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict, dataclass
from statistics import median

from .soh import KM_PER_MI, Result

SOH_WATCH, SOH_ALERT = 80.0, 70.0            # % of original capacity
SPREAD_WATCH, SPREAD_ALERT = 0.030, 0.060    # V between highest and lowest brick
TEMP_DELTA_WATCH = 8.0                       # degrees between hottest and coldest module
TEMP_MAX_WATCH = 50.0
FAST_CHARGE_WATCH = 50.0                     # % of lifetime energy from DC
CHARGE_LIMIT_WATCH = 90.0                    # average daily limit above this (NCA/NMC packs)
ISOLATION_DROP_WATCH = 0.5                   # min below 50 % of median = unstable isolation
WEAK_BRICK_SHARE = 0.8                       # same brick lowest in >80 % of readings


@dataclass
class Metric:
    key: str
    label: str
    group: str
    value: str = "not collected"
    status: str = "na"        # ok | watch | alert | info | na
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _nums(tele: dict, field: str) -> list[float]:
    return [v for _, v in tele.get(field, []) if isinstance(v, (int, float))]


def _last(tele: dict, field: str):
    s = tele.get(field)
    return s[-1][1] if s else None


def _series(tele: dict, field: str) -> list[tuple[int, float]]:
    return [(ts, v) for ts, v in tele.get(field, []) if isinstance(v, (int, float))]


def _asof(series: list[tuple[int, float]], ts: int) -> float | None:
    """Last value at or before ts.

    Fleet Telemetry sends a field only when it changes and each field has its
    own interval, so two fields rarely share a timestamp. The last value sent
    is the current one until the next arrives.
    """
    i = bisect_right(series, (ts, float("inf")))
    return series[i - 1][1] if i else None


def _paired_ts(tele: dict, a: str, b: str) -> list[tuple[int, float, float]]:
    """(ts, a, b) at every moment either field reported, once both are known."""
    sa, sb = _series(tele, a), _series(tele, b)
    out = []
    for ts in sorted({t for t, _ in sa} | {t for t, _ in sb}):
        va, vb = _asof(sa, ts), _asof(sb, ts)
        if va is not None and vb is not None:
            out.append((ts, va, vb))
    return out


def _paired(tele: dict, a: str, b: str) -> list[tuple[float, float]]:
    return [(va, vb) for _, va, vb in _paired_ts(tele, a, b)]


def build(res: Result, tele: dict, alerts: list[dict], ref_range_km: float,
          ref_capacity_kwh: float | None) -> tuple[list[Metric], dict]:
    G1, G2 = "Battery performance & condition", "Advanced battery intelligence"
    m: list[Metric] = []
    have_tele = bool(tele)

    # ---- From polling alone --------------------------------------------
    nominal = _nums(tele, "NominalFullPackEnergyKwh")
    nominal_kwh = round(median(nominal), 1) if nominal else None
    soh, method = res.soh_pct, "rated range at 100 % vs. range when new"
    if nominal_kwh and ref_capacity_kwh:
        soh, method = round(nominal_kwh / ref_capacity_kwh * 100, 1), "BMS nominal full pack energy vs. capacity when new"
    x = Metric("soh", "State of Health (SoH)", G1)
    if soh is not None:
        x.value = f"{soh:.1f} %"
        x.status = "ok" if soh >= SOH_WATCH else "watch" if soh >= SOH_ALERT else "alert"
        parts = [f"Method: {method}."]
        if res.soh_range_pct is not None:
            parts.append(f"Range method {res.soh_range_pct} % ({res.soh_range_low}–{res.soh_range_high}).")
        if res.soh_charge_pct is not None:
            parts.append(f"Charge-session method {res.soh_charge_pct} %.")
        x.detail = " ".join(parts)
    m.append(x)

    x = Metric("capacity", "Battery Capacity", G1)
    if nominal_kwh:
        x.value, x.status, x.detail = f"{nominal_kwh} kWh", "info", "Nominal full pack energy reported by the BMS."
    elif res.capacity_kwh:
        x.value, x.status = f"{res.capacity_kwh} kWh", "info"
        x.detail = (f"Measured over a charge from {res.session_soc_from:.0f} % to {res.session_soc_to:.0f} % "
                    f"({res.session_energy_kwh} kWh added); plausible band {res.capacity_kwh_low}–{res.capacity_kwh_high} kWh.")
    else:
        x.detail = "Needs a charging session of 20+ points in the window, or Fleet Telemetry."
    if ref_capacity_kwh and x.status == "info":
        x.detail += f" When new: {ref_capacity_kwh} kWh."
    m.append(x)

    x = Metric("range", "Estimated Range", G1)
    if res.full_range_km:
        x.value, x.status = f"{res.full_range_km:.0f} km", "info"
        x.detail = f"Rated range extrapolated to 100 % charge. When new: {ref_range_km:.0f} km."
    m.append(x)

    bms_alerts = [a for a in alerts if str(a.get("name", "")).upper().startswith("BMS")]
    x = Metric("status", "Battery Status", G1)
    if soh is not None:
        if soh < SOH_ALERT:
            x.value, x.status = "Poor", "alert"
        elif soh < SOH_WATCH or bms_alerts:
            x.value, x.status = "Fair", "watch"
        else:
            x.value, x.status = "Good", "ok"
        x.detail = f"Confidence in the estimate: {res.confidence}."
        if bms_alerts:
            x.detail += f" {len(bms_alerts)} recent BMS alert(s)."
    m.append(x)

    # ---- Needs Fleet Telemetry -----------------------------------------
    x = Metric("fast_charge", "Fast Charge Percentage", G2)
    dc, tot = _last(tele, "LifetimeDcChargeEnergyKwh"), _last(tele, "LifetimeEnergyChargedKwh")
    if isinstance(dc, (int, float)) and isinstance(tot, (int, float)) and tot > 0:
        pct = dc / tot * 100
        x.value, x.status = f"{pct:.0f} %", "watch" if pct > FAST_CHARGE_WATCH else "ok"
        x.detail = f"{dc:.0f} kWh of {tot:.0f} kWh lifetime charging energy came from DC fast charging."
    m.append(x)

    x = Metric("charge_limit", "Average Charge Limit", G2)
    lim = _nums(tele, "ChargeLimitSoc")
    if lim:
        avg = sum(lim) / len(lim)
        x.value, x.status = f"{avg:.0f} %", "watch" if avg > CHARGE_LIMIT_WATCH else "ok"
        x.detail = "Average of the limit set during the observation window only; the car does not expose its history."
    m.append(x)

    x = Metric("temperature", "Battery Temperature", G2)
    tp = _paired(tele, "ModuleTempMax", "ModuleTempMin")
    if tp:
        tmax, tmin = max(a for a, _ in tp), min(b for _, b in tp)
        delta = max(a - b for a, b in tp)
        x.value = f"{tmin:.0f}–{tmax:.0f} °C"
        x.status = "watch" if delta > TEMP_DELTA_WATCH or tmax > TEMP_MAX_WATCH else "ok"
        x.detail = f"Largest difference between hottest and coldest module at one moment: {delta:.1f} °C."
    m.append(x)

    iso = _nums(tele, "IsolationResistance")
    x = Metric("moisture", "Moisture Detection", G2)
    if iso:
        lo, med = min(iso), median(iso)
        unstable = med > 0 and lo < med * ISOLATION_DROP_WATCH
        x.value, x.status = ("Isolation unstable" if unstable else "No indication"), ("watch" if unstable else "ok")
        x.detail = (f"Indirect: based on high-voltage isolation resistance (min {lo:.0f}, median {med:.0f}, unit as reported). "
                    "A falling or unstable value can mean moisture ingress; it is not a moisture sensor.")
    m.append(x)

    vp = _paired(tele, "BrickVoltageMax", "BrickVoltageMin")
    spreads = [a - b for a, b in vp]
    x = Metric("cell_spread", "Cell Voltage Spread", G2)
    if spreads:
        med_mv, max_mv = median(spreads) * 1000, max(spreads) * 1000
        x.value = f"{med_mv:.0f} mV"
        x.status = "alert" if median(spreads) > SPREAD_ALERT else "watch" if median(spreads) > SPREAD_WATCH else "ok"
        x.detail = f"Median gap between highest and lowest brick; worst reading {max_mv:.0f} mV over {len(spreads)} readings."
    m.append(x)

    x = Metric("cell_balance", "Cell Balance", G2)
    sp = _paired(tele, "Soc", "BrickSocMinPercent")
    if spreads:
        x.value = {"ok": "Balanced", "watch": "Slight imbalance", "alert": "Imbalanced"}[m[-1].status]
        x.status = m[-1].status
        if sp:
            x.detail = f"Weakest brick sits {median(a - b for a, b in sp):.1f} points below pack state of charge."
    m.append(x)
    m[-2], m[-1] = m[-1], m[-2]  # Cell Balance before Cell Voltage Spread

    x = Metric("consumption", "Energy Consumption", G2)
    used, odo = _last(tele, "LifetimeEnergyUsed"), _last(tele, "Odometer")
    if isinstance(used, (int, float)) and isinstance(odo, (int, float)) and odo > 0:
        x.value, x.status = f"{used * 1000 / (odo * KM_PER_MI):.0f} Wh/km", "info"
        x.detail = "Lifetime average: total energy discharged from the pack divided by odometer (includes climate and standby use)."
    m.append(x)

    x = Metric("charging", "Charging", G2)
    ac, dcp = _nums(tele, "ACChargingPower"), _nums(tele, "DCChargingPower")
    peak_ac, peak_dc = max(ac, default=0), max(dcp, default=0)
    if peak_ac > 0 or peak_dc > 0:
        kind = "DC" if peak_dc > peak_ac else "AC"
        x.value, x.status = f"{kind}, peak {max(peak_ac, peak_dc):.0f} kW", "info"
        ph = _last(tele, "ChargerPhases")
        x.detail = "Observed in the window." + (f" {ph:.0f}-phase supply." if isinstance(ph, (int, float)) and kind == "AC" else "")
    elif res.session_energy_kwh:
        x.value, x.status = f"{res.session_energy_kwh} kWh added", "info"
        x.detail = f"One session from {res.session_soc_from:.0f} % to {res.session_soc_to:.0f} %."
    elif res.charging_seen:
        x.value, x.status = "Charging observed", "info"
        x.detail = "The car was charging during the window, but the session was too short to measure."
    elif have_tele or res.samples_online:
        x.value, x.detail = "No charging observed", "The car was not charged during the window."
    m.append(x)

    x = Metric("motor", "Motor Integrity", G2)
    stator = {s: _nums(tele, f"DiStatorTemp{s}") for s in ("F", "R", "REL", "RER")}
    stator = {k: v for k, v in stator.items() if v}
    hv = _last(tele, "Hvil")
    hv = hv if isinstance(hv, str) else None
    di_alerts = [a for a in alerts if str(a.get("name", "")).upper().startswith(("DI_", "DIF_", "DIR_"))]
    if stator or hv or di_alerts:          # each indicator stands on its own; any one is enough to report
        x.value, x.status, parts = "No fault indicated", "ok", []
        if stator:
            parts.append("Peak stator temperature " + ", ".join(f"{k} {max(v):.0f} °C" for k, v in stator.items()) + ".")
        if di_alerts:
            x.value, x.status = "Drive unit alerts present", "watch"
            parts.append(f"{len(di_alerts)} drive unit alert(s) in the car's recent alerts.")
        if hv:
            parts.append(f"High-voltage interlock: {hv}.")
            if "ok" not in hv.lower():
                x.value, x.status = "Interlock fault", "alert"
        x.detail = " ".join(parts + ["Indicators only; the API has no direct motor health value."])
    m.append(x)

    x = Metric("motor_leak", "Internal Motor Leakage", G2)
    spd, iso_ts = _series(tele, "VehicleSpeed"), _series(tele, "IsolationResistance")
    at_speed = [(v, _asof(spd, ts)) for ts, v in iso_ts]
    driving = [v for v, s in at_speed if s is not None and s > 0]
    parked = [v for v, s in at_speed if s == 0]
    if driving and parked:
        ratio = median(driving) / median(parked) if median(parked) else 1
        bad = ratio < ISOLATION_DROP_WATCH
        x.value, x.status = ("Possible leakage" if bad else "No indication"), ("watch" if bad else "ok")
        x.detail = (f"Indirect: isolation resistance while driving is {ratio * 100:.0f} % of the parked value. "
                    "A large drop with the drive units energised points at the motor/inverter side.")
    elif iso_ts:
        x.value, x.detail = "Needs a drive", "Isolation resistance must be seen both parked and driving within the window."
    m.append(x)

    x = Metric("cell_degradation", "Battery Cell Degradation", G2)
    lows = [int(v) for v in _nums(tele, "NumBrickVoltageMin")]
    if lows and spreads:
        brick, n = Counter(lows).most_common(1)[0]
        share = n / len(lows)
        weak = share >= WEAK_BRICK_SHARE and len(lows) >= 5
        severe = weak and median(spreads) > SPREAD_WATCH
        x.value = "Weak brick suspected" if severe else "One brick consistently lowest" if weak else "Even"
        x.status = "alert" if severe else "watch" if weak else "ok"
        x.detail = f"Brick {brick} had the lowest voltage in {share * 100:.0f} % of {len(lows)} readings."
    m.append(x)

    x = Metric("bms_faults", "BMS Fault Codes", G2)
    if alerts is not None and (alerts or res.samples_online):
        x.value = f"{len(bms_alerts)} BMS / {len(alerts)} total" if alerts else "None"
        x.status = "watch" if bms_alerts else "ok"
        x.detail = "; ".join(f"{a.get('name')} ({a.get('ts') or a.get('time') or '?'})" for a in bms_alerts[:8]) or \
            "From the car's recent alerts list."
    m.append(x)

    return m, {"soh_pct": soh, "soh_method": method if soh is not None else None, "telemetry": have_tele}


def energy(tele: dict) -> dict:
    """Energy used/recovered inside the window and lifetime charging split. Empty dict without telemetry.

    The car reports lifetime counters only, so window figures are differences
    between the first and last reading. Use is attributed to driving when the
    car was moving at the start of an interval or at any point during it,
    otherwise to standing.
    """
    out: dict = {}
    used = [(ts, v) for ts, v in tele.get("LifetimeEnergyUsed", []) if isinstance(v, (int, float))]
    if len(used) >= 2:
        spd = _series(tele, "VehicleSpeed")
        driving = standing = 0.0
        for (t0, v0), (t1, v1) in zip(used, used[1:]):
            d = max(v1 - v0, 0.0)
            during = spd[bisect_right(spd, (t0, float("inf"))):bisect_right(spd, (t1, float("inf")))]
            if (_asof(spd, t0) or 0) > 0 or any(v > 0 for _, v in during):
                driving += d
            else:
                standing += d
        out.update(used_kwh=round(driving + standing, 1), driving_kwh=round(driving, 1),
                   standing_kwh=round(standing, 1), days=round((used[-1][0] - used[0][0]) / 86400, 1))
    regen = _nums(tele, "LifetimeEnergyGainedRegen")
    if len(regen) >= 2:
        out["regen_kwh"] = round(max(regen[-1] - regen[0], 0.0), 1)
    dc, tot = _last(tele, "LifetimeDcChargeEnergyKwh"), _last(tele, "LifetimeEnergyChargedKwh")
    if isinstance(dc, (int, float)) and isinstance(tot, (int, float)) and tot > 0:
        out.update(lifetime_charged_kwh=round(tot, 1), lifetime_dc_kwh=round(dc, 1),
                   lifetime_ac_kwh=round(max(tot - dc, 0.0), 1))
    return out


def brick_series(tele: dict) -> list[list[float]]:
    """[ts, highest brick V, lowest brick V] for the voltage spread chart."""
    return [[ts, hi, lo] for ts, hi, lo in _paired_ts(tele, "BrickVoltageMax", "BrickVoltageMin")]
