import requests
from pprint import pprint

# === CONFIG ===
STRAAT = "Slangenburg"
HUISNR = "60"
PLAATS = "Barneveld"

LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
BAG_WFS = "https://service.pdok.nl/lv/bag/wfs/v2_0"
KADASTER_WFS = "https://service.pdok.nl/kadaster/kadastralekaart/wfs/v5_0"

# -----------------------------
# 1. RD-coördinaat ophalen
# -----------------------------
def get_rd(straat, huisnr, plaats):
    params = {
        "q": f"{straat} {huisnr} {plaats}",
        "rows": 1,
        "fl": "centroide_rd,weergavenaam"
    }
    r = requests.get(LOCATIESERVER, params=params)
    r.raise_for_status()
    doc = r.json()["response"]["docs"][0]
    x, y = map(float, doc["centroide_rd"].replace("POINT(", "").replace(")", "").split())
    print("\n📍 Adres:", doc["weergavenaam"])
    print("RD:", x, y)
    return x, y

# -----------------------------
# 2. BAG verblijfsobject ophalen
# -----------------------------
def get_bag_object(x, y):
    bbox = f"{x-0.1},{y-0.1},{x+0.1},{y+0.1},EPSG:28992"
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeName": "bag:verblijfsobject",
        "outputFormat": "application/json",
        "bbox": bbox,
        "count": 1
    }
    r = requests.get(BAG_WFS, params=params)
    r.raise_for_status()
    feature = r.json()["features"][0]
    print("\n🏠 BAG verblijfsobject (properties):")
    pprint(feature["properties"])
    return feature["properties"]

# -----------------------------
# 3. Kadaster perceel ophalen (RAW)
# -----------------------------
def get_perceel(x, y):
    print("dit zijn x en y coordinaten,",x, y)
    bbox = f"{x-1},{y-1},{x+1},{y+1},EPSG:28992"
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeName": "kadastralekaart:Perceel",
        "outputFormat": "application/json",
        "bbox": bbox,
        "count": 1
    }
    r = requests.get(KADASTER_WFS, params=params)
    r.raise_for_status()
    print(r.json())
    feature = r.json()["features"][0]

    print("\n📐 KADASTER PERCEEL – PROPERTIES:")
    pprint(feature["properties"])

    print("\n📐 KADASTER PERCEEL – GEOMETRY TYPE:")
    print(feature["geometry"]["type"])

    print("\n📐 KADASTER PERCEEL – AANTAL COÖRDINATEN:")
    print(len(feature["geometry"]["coordinates"][0]))

    return feature

# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":
    x, y = get_rd(STRAAT, HUISNR, PLAATS)
    get_bag_object(x, y)
    get_perceel(x, y)
