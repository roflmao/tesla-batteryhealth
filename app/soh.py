"""State-of-health estimation from Fleet API samples. Pure functions, no I/O.

Two independent estimates:

1. Range method (BMS view). battery_range is the car's rated range at the
   current state of charge. Extrapolated to 100 % it is proportional to the
   energy the BMS believes the pack holds:
       full_range = battery_range / (battery_level / 100)
       SoH_range  = full_range / rated range when new
   battery_level is an integer, so each sample carries +-0.5 point rounding
   error; that matters less at high SoC, hence the min_soc filter.

2. Charge method (measured). Over one charging session:
       capacity = d(charge_energy_added) / (d(battery_level) / 100)
       SoH_charge = capacity / usable capacity when new
   Needs a session spanning >= min_session_soc points to be meaningful.
"""
from dataclasses import asdict, dataclass, field
from statistics import median

KM_PER_MI = 1.609344


@dataclass
class Sample:
    ts: int
    state: str = "online"
    battery_level: float | None = None
    usable_battery_level: float | None = None
    battery_range_mi: float | None = None
    charge_energy_added: float | None = None
    charging_state: str | None = None
    odometer_mi: float | None = None
    outside_temp: float | None = None

    @classmethod
    def from_vehicle_data(cls, ts: int, d: dict) -> "Sample":
        cs, vs = d.get("charge_state") or {}, d.get("vehicle_state") or {}
        return cls(ts=ts, battery_level=cs.get("battery_level"), usable_battery_level=cs.get("usable_battery_level"),
                   battery_range_mi=cs.get("battery_range"), charge_energy_added=cs.get("charge_energy_added"),
                   charging_state=cs.get("charging_state"), odometer_mi=vs.get("odometer"),
                   outside_temp=(d.get("climate_state") or {}).get("outside_temp"))


@dataclass
class Result:
    samples_total: int = 0
    samples_online: int = 0
    # range method
    range_samples_used: int = 0
    range_min_soc_used: int | None = None
    full_range_km: float | None = None
    full_range_km_low: float | None = None
    full_range_km_high: float | None = None
    soh_range_pct: float | None = None
    soh_range_low: float | None = None
    soh_range_high: float | None = None
    # charge method
    session_soc_from: float | None = None
    session_soc_to: float | None = None
    session_energy_kwh: float | None = None
    capacity_kwh: float | None = None
    capacity_kwh_low: float | None = None
    capacity_kwh_high: float | None = None
    soh_charge_pct: float | None = None
    # summary
    soh_pct: float | None = None
    confidence: str = "none"          # none | low | medium | high
    odometer_km: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _is_cold(s: Sample) -> bool:
    """A cold pack reports usable_battery_level below battery_level; range is then understated."""
    return (s.usable_battery_level is not None and s.battery_level is not None
            and s.usable_battery_level < s.battery_level - 1)


def _range_estimates(samples: list[Sample], min_soc: int) -> list[tuple[float, float, float]]:
    out = []
    for s in samples:
        if s.battery_level is None or not s.battery_range_mi or s.battery_level < min_soc or _is_cold(s):
            continue
        lvl = s.battery_level
        out.append((s.battery_range_mi / (lvl / 100),
                    s.battery_range_mi / ((lvl + 0.5) / 100),
                    s.battery_range_mi / ((max(lvl - 0.5, 1)) / 100)))
    return out


def _sessions(samples: list[Sample]) -> list[tuple[Sample, Sample]]:
    """Group consecutive charging samples; a trailing 'Complete' sample closes the session.

    Takes every sample, including the ones without data. charge_energy_added
    restarts from zero at each plug-in, so two plug-ins must never be joined:
    a sample without data (car asleep or unreachable) ends the session, and so
    does the counter going down.
    """
    sessions: list[tuple[Sample, Sample]] = []
    cur: list[Sample] = []

    def close() -> None:
        if len(cur) >= 2:
            sessions.append((cur[0], cur[-1]))
        cur.clear()

    for s in samples:
        if s.battery_level is None or s.charge_energy_added is None:
            close()
            continue
        restarted = bool(cur) and s.charge_energy_added < cur[-1].charge_energy_added
        if s.charging_state == "Charging":
            if restarted:
                close()
            cur.append(s)
        else:
            if cur and s.charging_state == "Complete" and not restarted:
                cur.append(s)
            close()
    close()
    return sessions


def compute(samples: list[Sample], ref_range_km: float, ref_capacity_kwh: float | None = None,
            min_soc: int = 50, min_session_soc: int = 20) -> Result:
    samples = sorted(samples, key=lambda s: s.ts)
    r = Result(samples_total=len(samples))
    online = [s for s in samples if s.battery_level is not None]
    r.samples_online = len(online)
    if not online:
        r.notes.append("No data yet: the car has not been online during the window.")
        return r

    odo = [s.odometer_mi for s in online if s.odometer_mi]
    if odo:
        r.odometer_km = round(odo[-1] * KM_PER_MI)
    if any(_is_cold(s) for s in online):
        r.notes.append("Some samples were taken with a cold battery and were excluded from the range method.")

    # --- range method ---
    used_soc = min_soc
    est = _range_estimates(online, min_soc)
    if not est:
        used_soc = 20
        est = _range_estimates(online, 20)
        if est:
            r.notes.append(f"No samples at or above {min_soc} % charge; range method used lower-charge samples and is less precise.")
    if est:
        mid, lo, hi = (median(x[i] for x in est) for i in range(3))
        r.range_samples_used, r.range_min_soc_used = len(est), used_soc
        r.full_range_km = round(mid * KM_PER_MI, 1)
        r.full_range_km_low = round(lo * KM_PER_MI, 1)
        r.full_range_km_high = round(hi * KM_PER_MI, 1)
        r.soh_range_pct = round(r.full_range_km / ref_range_km * 100, 1)
        r.soh_range_low = round(r.full_range_km_low / ref_range_km * 100, 1)
        r.soh_range_high = round(r.full_range_km_high / ref_range_km * 100, 1)
    else:
        r.notes.append("Range method unavailable: no usable samples above 20 % charge.")

    # --- charge method ---
    best = None
    for a, b in _sessions(samples):
        d_soc = b.battery_level - a.battery_level
        d_e = b.charge_energy_added - a.charge_energy_added
        if d_soc >= min_session_soc and d_e > 0 and (best is None or d_soc > best[0]):
            best = (d_soc, d_e, a, b)
    if best:
        d_soc, d_e, a, b = best
        r.session_soc_from, r.session_soc_to, r.session_energy_kwh = a.battery_level, b.battery_level, round(d_e, 2)
        r.capacity_kwh = round(d_e / (d_soc / 100), 1)
        r.capacity_kwh_low = round(d_e / ((d_soc + 1) / 100), 1)    # both ends rounded: +-1 point
        r.capacity_kwh_high = round(d_e / ((d_soc - 1) / 100), 1)
        if ref_capacity_kwh:
            r.soh_charge_pct = round(r.capacity_kwh / ref_capacity_kwh * 100, 1)
    else:
        r.notes.append(f"Charge method unavailable: no charging session of at least {min_session_soc} points was observed.")

    # --- summary ---
    r.soh_pct = r.soh_range_pct if r.soh_range_pct is not None else r.soh_charge_pct
    if r.soh_range_pct is not None and r.soh_charge_pct is not None:
        gap = abs(r.soh_range_pct - r.soh_charge_pct)
        if gap <= 4 and used_soc >= min_soc:
            r.confidence = "high"
        elif gap <= 8:
            r.confidence = "medium"
        else:
            r.confidence = "low"
            r.notes.append(f"The two methods disagree by {gap:.1f} points; check the reference values entered for this car.")
    elif r.soh_range_pct is not None:
        r.confidence = "medium" if (used_soc >= min_soc and r.range_samples_used >= 3) else "low"
    elif r.soh_charge_pct is not None:
        r.confidence = "low"
    if r.soh_pct is not None and r.soh_pct > 103:
        r.notes.append("Result above 100 %: the reference range entered is probably lower than this variant's rating when new.")
    return r
