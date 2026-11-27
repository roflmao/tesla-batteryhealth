"""Simulated Tesla backend for BH_MOCK=1. Each vehicle_data call advances the car 15 minutes."""
import base64
import json

from .tesla import VehicleUnavailable

TRUE_CAPACITY_KWH = 68.4   # what the simulated pack really holds
TRUE_FULL_RANGE_MI = 289.0
VIN = "5YJ3MOCK0NF000001"


class MockClient:
    def __init__(self) -> None:
        self.step = 0

    def authorize_url(self, state: str) -> str:
        return f"/auth/callback?code=mock&state={state}"

    async def exchange_code(self, code: str) -> dict:
        claims = base64.urlsafe_b64encode(json.dumps({"sub": f"mock-{code}"}).encode()).decode().rstrip("=")
        return {"access_token": "mock", "refresh_token": "mock", "expires_in": 10**8, "id_token": f"x.{claims}.x"}

    async def region_base(self, token: str) -> str:
        return "mock://"

    async def vehicles(self, account_id: int) -> list[dict]:
        return [{"vin": VIN, "display_name": "Mock Model 3", "state": "online"}]

    async def vehicle(self, account_id: int, vin: str) -> dict:
        asleep = self.step in (2, 3)  # a nap before plugging in
        if asleep:
            self.step += 1
        return {"vin": vin, "state": "asleep" if asleep else "online"}

    async def vehicle_data(self, account_id: int, vin: str) -> dict:
        if self.step in (2, 3):
            raise VehicleUnavailable("408 vehicle unavailable")
        t = self.step
        self.step += 1
        soc0, plug_at, kw, limit = 41.6, 4, 11.0, 90.0
        added = max(0.0, (t - plug_at) * 0.25 * kw) if t >= plug_at else 0.0
        soc = soc0 + added / TRUE_CAPACITY_KWH * 100 + max(0, 2 - t) * 3.0   # arrives from a drive
        state = "Disconnected" if t < plug_at else "Charging"
        if soc >= limit:
            soc, state = limit, "Complete"
            added = (limit - soc0) / 100 * TRUE_CAPACITY_KWH
        return {
            "vin": vin, "state": "online",
            "charge_state": {
                "battery_level": round(soc), "usable_battery_level": round(soc),
                "battery_range": round(TRUE_FULL_RANGE_MI * soc / 100, 2),
                "charge_energy_added": round(added, 2),
                "charging_state": state, "charger_power": 11 if state == "Charging" else 0,
            },
            "vehicle_state": {"odometer": 61234.5, "car_version": "2026.38.4 mock"},
            "vehicle_config": {"car_type": "model3", "trim_badging": "74d", "wheel_type": "Stiletto19"},
            "climate_state": {"outside_temp": 6.5},
        }

    async def recent_alerts(self, account_id: int, vin: str) -> list[dict]:
        return [{"name": "BMS_a066", "time": "2026-11-27T07:12:00Z", "audience": ["service"],
                 "user_text": "Maximum charge level and range may be reduced"}]

    def telemetry(self, vin: str) -> dict:
        """What Fleet Telemetry would stream; only exists in mock mode."""
        t = max(self.step - 1, 0)
        charging = 4 <= t < 4 + 14
        return {
            "NominalFullPackEnergyKwh": 68.4 + (t % 3) * 0.05, "EnergyRemaining": 28.5 + t,
            "BrickVoltageMax": 3.912 + t * 0.01, "BrickVoltageMin": 3.900 + t * 0.01,
            "NumBrickVoltageMin": 37 if t % 4 else 12, "NumBrickVoltageMax": 5,
            "BrickSocMinPercent": 40.9 + t * 4, "Soc": 41.6 + t * 4,
            "ModuleTempMax": 21.5 + (6 if charging else 0), "ModuleTempMin": 19.0 + (4 if charging else 0),
            "IsolationResistance": 4100 - t * 5, "ChargeLimitSoc": 90,
            "LifetimeDcChargeEnergyKwh": 2310.0, "LifetimeEnergyChargedKwh": 16840.0 + t,
            "LifetimeEnergyUsed": 15990.0 + min(t, 2) * 2.05 + t * 0.04,
            "LifetimeEnergyGainedRegen": 3120.0 + min(t, 2) * 0.4, "Odometer": 61234.5,
            "ACChargingPower": 11.0 if charging else 0.0, "DCChargingPower": 0.0,
            "FastChargerPresent": "false", "ChargerPhases": 3,
            "DiStatorTempR": 34.0, "DiStatorTempF": 31.0, "DiInverterTR": 29.0, "Hvil": "HvilStatusOk",
            "VehicleSpeed": 62 if t < 2 else 0,
        }

    async def wake(self, account_id: int, vin: str) -> None:
        return None
