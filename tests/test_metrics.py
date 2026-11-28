from app import db, metrics


def test_invalid_telemetry_values_are_dropped():
    out = db.normalize_telemetry({"NominalFullPackEnergyKwh": {"invalid": True}, "Soc": {"doubleValue": 55.5},
                                  "Hvil": {"stringValue": "HvilStatusOk"}, "IsolationResistance": "4100"})
    assert out == {"Soc": 55.5, "Hvil": "HvilStatusOk", "IsolationResistance": 4100.0}


def test_energy_split_with_speed_on_other_timestamps():
    # Energy counter every 600 s, speed reported only when it changes, never on the same second.
    tele = {"LifetimeEnergyUsed": [(0, 100.0), (600, 103.0), (1200, 103.2), (1800, 105.0)],
            "VehicleSpeed": [(-50, 0.0), (30, 60.0), (590, 0.0), (1300, 50.0), (1700, 0.0)]}
    e = metrics.energy(tele)
    assert (e["driving_kwh"], e["standing_kwh"]) == (4.8, 0.2)


def test_brick_readings_pair_up_without_shared_timestamps():
    tele = {"BrickVoltageMax": [(0, 4.00), (300, 4.05)], "BrickVoltageMin": [(10, 3.98), (600, 4.01)]}
    assert metrics.brick_series(tele) == [[10, 4.00, 3.98], [300, 4.05, 3.98], [600, 4.05, 4.01]]


def test_motor_leak_uses_last_known_speed():
    tele = {"IsolationResistance": [(100, 4000.0), (400, 1500.0), (900, 4000.0)],
            "VehicleSpeed": [(0, 0.0), (350, 70.0), (800, 0.0)],
            "BrickVoltageMax": [(0, 4.0)], "BrickVoltageMin": [(0, 3.99)]}
    from app.soh import Result
    mets, _ = metrics.build(Result(), tele, [], 500, None)
    leak = {m.key: m for m in mets}["motor_leak"]
    assert leak.value == "Possible leakage" and leak.status == "watch"


def _motor(tele, alerts):
    from app.soh import Result
    return {m.key: m for m in metrics.build(Result(), tele, alerts, 500, None)[0]}["motor"]


def test_motor_faults_reported_without_stator_temperatures():
    assert _motor({"Hvil": [(0, "HvilStatusFault")]}, []).status == "alert"
    assert _motor({}, [{"name": "DI_a123", "ts": None}]).value == "Drive unit alerts present"
    assert _motor({}, []).status == "na"


def test_interlock_fault_is_not_downgraded_by_drive_unit_alerts():
    m = _motor({"Hvil": [(0, "HvilStatusFault")], "DiStatorTempR": [(0, 40.0)]}, [{"name": "DIR_a001", "ts": None}])
    assert (m.value, m.status) == ("Interlock fault", "alert")


def test_short_charge_is_not_reported_as_no_charging():
    from app.soh import Sample, compute
    s = [Sample(ts=i * 900, battery_level=60 + i, usable_battery_level=60 + i, battery_range_mi=180 + 3 * i,
                charge_energy_added=i * 0.7, charging_state="Charging") for i in range(3)]
    res = compute(s, ref_range_km=480)
    charging = {m.key: m for m in metrics.build(res, {}, [], 480, None)[0]}["charging"]
    assert res.capacity_kwh is None and charging.value == "Charging observed"
