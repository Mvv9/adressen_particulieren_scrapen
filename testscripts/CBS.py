import requests
import math
from pyproj import Transformer

from collections import defaultdict
import cbsodata


# -----------------------------
# Config
# -----------------------------
LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
BAG_WFS = "https://service.pdok.nl/lv/bag/wfs/v2_0"
transformer = Transformer.from_crs("EPSG:28992", "EPSG:4326", always_xy=True)

# -----------------------------
# Helpers
# -----------------------------
def afstand(x1, y1, x2, y2):
    return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)

def rd_to_latlon(x, y):
    lon, lat = transformer.transform(x, y)
    return lat, lon

# -----------------------------
# Stap 1: RD-coördinaat van adres
# -----------------------------
def get_rd_from_address(straat, huisnr, plaats):
    params = {
        "q": f"{straat} {huisnr} {plaats}",
        "rows": 1,
        "fl": "centroide_rd,weergavenaam"
    }

    r = requests.get(LOCATIESERVER, params=params)
    r.raise_for_status()
    docs = r.json()["response"]["docs"]
    print(docs)
    if not docs:
        raise ValueError("Adres niet gevonden")

    coord = docs[0]["centroide_rd"].replace("POINT(", "").replace(")", "")
    x, y = map(float, coord.split())
    return x, y

# -----------------------------
# Stap 2: BAG verblijfsobjecten binnen straal
# -----------------------------
def get_bag_adressen_in_straal(center_x, center_y, straal):
    minx, maxx = center_x - straal, center_x + straal
    miny, maxy = center_y - straal, center_y + straal

    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeName": "bag:verblijfsobject",
        "outputFormat": "application/json",
        "bbox": f"{minx},{miny},{maxx},{maxy},EPSG:28992"
    }

    r = requests.get(BAG_WFS, params=params)
    r.raise_for_status()
    features = r.json().get("features", [])

    # 1️⃣ Verzamel verblijfsobjecten per pand
    panden = defaultdict(list)
    for f in features:
        props = f.get("properties", {})
        pand_id = props.get("pandidentificatie")
        if pand_id:
            panden[pand_id].append(props)

    resultaten = []

    # 2️⃣ Loop opnieuw voor output per adres
    for f in features:
        geom = f.get("geometry")
        props = f.get("properties")
        if not geom or not props:
            continue

        x, y = geom["coordinates"]
        dist = afstand(center_x, center_y, x, y)

        if dist > straal:
            continue

        pand_id = props.get("pandidentificatie")
        aantal_verblijfsobjecten = len(panden.get(pand_id, []))

        # 3️⃣ Classificatie
        if aantal_verblijfsobjecten == 1:
            woningtype = "grondgebonden"
            is_appartement = False
            is_flat = False
        elif 2 <= aantal_verblijfsobjecten <= 5:
            woningtype = "appartement"
            is_appartement = True
            is_flat = False
        else:
            woningtype = "flat"
            is_appartement = True
            is_flat = True

        resultaten.append({
            "straat": props.get("openbare_ruimte"),
            "huisnummer": props.get("huisnummer"),
            "postcode": props.get("postcode"),
            "plaats": props.get("woonplaats"),
            #"bouwjaar": props.get("bouwjaar"),
            #"oppervlakte_m2": props.get("oppervlakte"),
            #"gebruiksdoel": props.get("gebruiksdoel"),
            #"status": props.get("status"),
            #"pandstatus": props.get("pandstatus"),
            #"afstand_m": round(dist, 1),

            # 👇 dit wilde je
            "woningtype": woningtype,
            "is_appartement": is_appartement,
            "is_flat": is_flat,
            "aantal_verblijfsobjecten_in_pand": aantal_verblijfsobjecten
        })

    return resultaten



def woningtype(aantal):
    if aantal == 1:
        return "grondgebonden"
    elif 2 <= aantal <= 5:
        return "klein appartementenpand"
    else:
        return "flat"
    

def get_buurtcode_from_rd(x, y):
    url = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse"

    params = {
        "X": x,
        "Y": y,
        "type": "buurt",
        "rows": 1,
        "fl": "buurtcode,buurtnaam,wijkcode,gemeentecode"
    }

    r = requests.get(url, params=params)
    r.raise_for_status()

    docs = r.json().get("response", {}).get("docs", [])
    if not docs:
        return None

    return docs[0]


def get_cbs_buurt_data(buurtcode: str):
    # Gebruik cbsodata package - veel makkelijker
    import cbsodata
    
    # 85039NED = Kerncijfers wijken en buurten 2023
    data = cbsodata.get_data('85039NED')
    
    # Filter op jouw buurtcode
    for row in data:
        if row.get('Codering_3', '').strip() == buurtcode:
            return row
    
    return None




# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    straat = "Oldenbarnevelderweg"
    huisnr = "111"
    plaats = "barneveld"
    straal = 50  # meters

    #cx, cy = get_rd_from_address(straat, huisnr, plaats)
    #adressen = get_bag_adressen_in_straal(cx, cy, straal)

    #print(f"{len(adressen)} adressen gevonden:\n")
    #for a in adressen:
        #print(a)
    
    #buurt = get_buurtcode_from_rd(cx, cy)
    #print(buurt)

    
    buurtcode = "BU02035410"
    cbs_data = get_cbs_buurt_data(buurtcode)

    print(cbs_data)
   



