from app.soh import KM_PER_MI, Sample, compute


def charge_run(true_cap=70.0, full_mi=300.0, start=40, end=90, step=5):
    out, t = [], 0
    for soc in range(start, end + 1, step):
        out.append(Sample(ts=t, battery_level=soc, usable_battery_level=soc,
                          battery_range_mi=full_mi * soc / 100,
                          charge_energy_added=(soc - start) / 100 * true_cap,
                          charging_state="Charging" if soc < end else "Complete"))
        t += 900
    return out


def test_both_methods_recover_truth():
    r = compute(charge_run(), ref_range_km=300 * KM_PER_MI / 0.9, ref_capacity_kwh=70 / 0.9)
    assert abs(r.soh_range_pct - 90) < 0.2
    assert abs(r.capacity_kwh - 70) < 0.1
    assert abs(r.soh_charge_pct - 90) < 0.2
    assert r.confidence == "high"
    assert r.capacity_kwh_low < 70 < r.capacity_kwh_high


def test_no_online_samples():
    r = compute([Sample(ts=1, state="asleep")], ref_range_km=500)
    assert r.soh_pct is None and r.confidence == "none"


def test_low_soc_only_is_low_confidence():
    s = [Sample(ts=i, battery_level=30, usable_battery_level=30, battery_range_mi=90,
                charging_state="Disconnected", charge_energy_added=0) for i in range(4)]
    r = compute(s, ref_range_km=300 * KM_PER_MI)
    assert r.confidence == "low" and abs(r.soh_range_pct - 100) < 0.2


def test_cold_samples_excluded():
    s = [Sample(ts=0, battery_level=80, usable_battery_level=74, battery_range_mi=200),
         Sample(ts=1, battery_level=80, usable_battery_level=80, battery_range_mi=240)]
    r = compute(s, ref_range_km=300 * KM_PER_MI)
    assert r.range_samples_used == 1 and abs(r.soh_range_pct - 100) < 0.2


def test_short_session_ignored():
    r = compute(charge_run(start=60, end=70), ref_range_km=500, ref_capacity_kwh=75)
    assert r.capacity_kwh is None


def test_disagreement_flagged():
    r = compute(charge_run(), ref_range_km=300 * KM_PER_MI, ref_capacity_kwh=90)
    assert r.confidence == "low"


def _shift(run, dt):
    return [Sample(**{**s.__dict__, "ts": s.ts + dt}) for s in run]


def test_sessions_separated_by_sleep_are_not_joined():
    # 40 -> 55 %, car sleeps, then a new plug-in 50 -> 80 %. Joined, this would read as
    # 40 points for the second session's 21 kWh: 52.5 kWh instead of 70.
    first = charge_run(start=40, end=55)[:-1] + [Sample(ts=2700, battery_level=55, usable_battery_level=55,
                                                        battery_range_mi=165, charge_energy_added=10.5, charging_state="Charging")]
    samples = first + [Sample(ts=5000, state="asleep")] + _shift(charge_run(start=50, end=80), 10000)
    r = compute(samples, ref_range_km=300 * KM_PER_MI, ref_capacity_kwh=70)
    assert (r.session_soc_from, r.session_soc_to) == (50, 80)
    assert abs(r.capacity_kwh - 70) < 0.1


def test_energy_counter_restart_splits_sessions_without_a_gap():
    first = [s for s in charge_run(start=40, end=55) if s.charging_state == "Charging"]
    samples = first + _shift(charge_run(start=50, end=80), 10000)
    r = compute(samples, ref_range_km=300 * KM_PER_MI, ref_capacity_kwh=70)
    assert (r.session_soc_from, r.session_soc_to) == (50, 80)
    assert abs(r.capacity_kwh - 70) < 0.1
