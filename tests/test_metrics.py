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
