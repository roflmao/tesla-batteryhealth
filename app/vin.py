"""Decode what a Tesla VIN says about the car (model, year, plant). No network lookups."""
MODELS = {"S": "Model S", "3": "Model 3", "X": "Model X", "Y": "Model Y", "C": "Cybertruck", "T": "Semi", "R": "Roadster"}
PLANTS = {"F": "Fremont, CA, USA", "A": "Austin, TX, USA", "C": "Shanghai, China", "B": "Berlin, Germany", "N": "Reno, NV, USA"}
YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"   # A=2010 ... Y=2030, 1=2031 ...


def decode(vin: str) -> dict:
    vin = (vin or "").upper()
    if len(vin) != 17:
        return {}
    i = YEAR_CODES.find(vin[9])
    return {
        "model": MODELS.get(vin[3]),
        # Fremont/Austin: model year. Shanghai/Berlin: calendar year of build.
        "year": 2010 + i if i >= 0 else None,
        "plant": PLANTS.get(vin[10]),
    }
