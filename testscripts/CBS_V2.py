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
    
    if not docs:
        raise ValueError("Adres niet gevonden")

    coord = docs[0]["centroide_rd"].replace("POINT(", "").replace(")", "")
    x, y = map(float, coord.split())
    return x, y

# -----------------------------
# Stap 2: Buurtcode ophalen
# -----------------------------
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
    return docs[0] if docs else None

# -----------------------------
# Stap 3: BAG verblijfsobjecten binnen straal
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

    # Verzamel verblijfsobjecten per pand
    panden = defaultdict(list)
    for f in features:
        props = f.get("properties", {})
        pand_id = props.get("pandidentificatie")
        if pand_id:
            panden[pand_id].append(props)

    resultaten = []

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

        # Classificatie
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
            "huisletter": props.get("huisletter"),
            "toevoeging": props.get("huisnummertoevoeging"),
            "postcode": props.get("postcode"),
            "plaats": props.get("woonplaats"),
            "afstand_m": round(dist, 1),
            "woningtype": woningtype,
            "is_appartement": is_appartement,
            "is_flat": is_flat,
            "aantal_in_pand": aantal_verblijfsobjecten
        })

    # Sorteer op afstand
    resultaten.sort(key=lambda x: x['afstand_m'])
    return resultaten

# -----------------------------
# Stap 4: CBS buurtdata
# -----------------------------
def get_cbs_buurt_data(buurtcode: str):
    data = cbsodata.get_data('85039NED')
    for row in data:
        if row.get('Codering_3', '').strip() == buurtcode:
            return row
    return None

# -----------------------------
# Stap 5: Gecombineerd profiel
# -----------------------------
def get_profiel_voor_straal(straat, huisnr, plaats, straal=100):
    """Haal compleet profiel op voor adressen binnen straal"""
    
    print(f"🔍 Zoeken rond {straat} {huisnr}, {plaats} (straal: {straal}m)...")
    
    # 1. Haal centrum + buurtcode
    cx, cy = get_rd_from_address(straat, huisnr, plaats)
    lat, lon = rd_to_latlon(cx, cy)
    buurt_info = get_buurtcode_from_rd(cx, cy)
    buurtcode = buurt_info.get('buurtcode') if buurt_info else None
    
    print(f"📍 Coördinaten: {lat:.6f}, {lon:.6f}")
    print(f"🏘️  Buurt: {buurt_info.get('buurtnaam')} ({buurtcode})")
    
    # 2. BAG adressen in straal
    adressen = get_bag_adressen_in_straal(cx, cy, straal)
    print(f"🏠 {len(adressen)} adressen gevonden in straal")
    
    # 3. Tel woningtypes
    totaal = len(adressen)
    grondgebonden = [a for a in adressen if a['woningtype'] == 'grondgebonden']
    kleine_appartementen = [a for a in adressen if a['woningtype'] == 'appartement']
    flats = [a for a in adressen if a['woningtype'] == 'flat']
    
    # 4. CBS data voor context
    print(f"📊 CBS data ophalen...")
    cbs = get_cbs_buurt_data(buurtcode) if buurtcode else None
    
    return {
        # Locatie
        "adres": f"{straat} {huisnr}, {plaats}",
        "lat": lat,
        "lon": lon,
        "straal_m": straal,
        "buurt": buurt_info.get('buurtnaam') if buurt_info else None,
        "buurtcode": buurtcode,
        
        # Woningen in straal (exacte BAG data)
        "totaal_adressen": totaal,
        "grondgebonden": len(grondgebonden),
        "kleine_appartementen": len(kleine_appartementen),
        "flats": len(flats),
        
        # Alle adressen (voor export)
        "adressen": adressen,
        
        # CBS buurt context
        "cbs": {
            "inwoners": cbs.get('AantalInwoners_5') if cbs else None,
            "huishoudens": cbs.get('HuishoudensTotaal_28') if cbs else None,
            "gem_huishoudgrootte": cbs.get('GemiddeldeHuishoudensgrootte_32') if cbs else None,
            "pct_eengezins": cbs.get('PercentageEengezinswoning_36') if cbs else None,
            "pct_meergezins": cbs.get('PercentageMeergezinswoning_37') if cbs else None,
            "pct_koopwoning": cbs.get('Koopwoningen_40') if cbs else None,
            "pct_huurwoning": cbs.get('HuurwoningenTotaal_41') if cbs else None,
            "gem_woz": cbs.get('GemiddeldeWOZWaardeVanWoningen_35') if cbs else None,
            "gem_inkomen": cbs.get('GemiddeldInkomenPerInkomensontvanger_71') if cbs else None,
            "bevolkingsdichtheid": cbs.get('Bevolkingsdichtheid_33') if cbs else None,
            "stedelijkheid": cbs.get('MateVanStedelijkheid_116') if cbs else None,
        }
    }

# -----------------------------
# Stap 6: Buurtcampagne analyse
# -----------------------------
def analyseer_voor_buurtcampagne(profiel, min_grondgebonden_pct=50):
    """Analyseer of locatie geschikt is voor buurtcampagne"""
    
    totaal = profiel['totaal_adressen']
    grondgebonden = profiel['grondgebonden']
    
    if totaal == 0:
        return {
            "geschikt": False,
            "reden": "Geen adressen gevonden",
            "score": 0
        }
    
    pct_grondgebonden = (grondgebonden / totaal) * 100
    
    # Scoring
    score = 0
    redenen = []
    
    # Woningtype score (max 40 punten)
    if pct_grondgebonden >= 80:
        score += 40
        redenen.append(f"✅ Uitstekend: {pct_grondgebonden:.0f}% grondgebonden")
    elif pct_grondgebonden >= 60:
        score += 30
        redenen.append(f"✅ Goed: {pct_grondgebonden:.0f}% grondgebonden")
    elif pct_grondgebonden >= 40:
        score += 20
        redenen.append(f"⚠️ Matig: {pct_grondgebonden:.0f}% grondgebonden")
    else:
        score += 10
        redenen.append(f"❌ Laag: {pct_grondgebonden:.0f}% grondgebonden")
    
    # Volume score (max 30 punten)
    if grondgebonden >= 50:
        score += 30
        redenen.append(f"✅ Veel potentie: {grondgebonden} grondgebonden woningen")
    elif grondgebonden >= 25:
        score += 20
        redenen.append(f"✅ Voldoende: {grondgebonden} grondgebonden woningen")
    elif grondgebonden >= 10:
        score += 10
        redenen.append(f"⚠️ Beperkt: {grondgebonden} grondgebonden woningen")
    else:
        redenen.append(f"❌ Te weinig: {grondgebonden} grondgebonden woningen")
    
    # CBS context score (max 30 punten)
    cbs = profiel.get('cbs', {})
    
    # Inkomen (koopkracht)
    gem_inkomen = cbs.get('gem_inkomen')
    if gem_inkomen:
        if gem_inkomen >= 30:
            score += 15
            redenen.append(f"✅ Hoog inkomen: €{gem_inkomen}k")
        elif gem_inkomen >= 25:
            score += 10
            redenen.append(f"✅ Gemiddeld inkomen: €{gem_inkomen}k")
        else:
            score += 5
            redenen.append(f"⚠️ Lager inkomen: €{gem_inkomen}k")
    
    # Koopwoningen (eigenaren = beslissers)
    pct_koop = cbs.get('pct_koopwoning')
    if pct_koop:
        if pct_koop >= 60:
            score += 15
            redenen.append(f"✅ Veel eigenaren: {pct_koop}% koopwoning")
        elif pct_koop >= 40:
            score += 10
            redenen.append(f"✅ Mix koop/huur: {pct_koop}% koopwoning")
        else:
            score += 5
            redenen.append(f"⚠️ Veel huur: {pct_koop}% koopwoning")
    
    geschikt = score >= 50 and pct_grondgebonden >= min_grondgebonden_pct
    
    return {
        "geschikt": geschikt,
        "score": score,
        "max_score": 100,
        "analyse": redenen,
        "aanbeveling": "🟢 Geschikt voor buurtcampagne" if geschikt else "🔴 Minder geschikt voor buurtcampagne",
        "stats": {
            "totaal_adressen": totaal,
            "grondgebonden": grondgebonden,
            "pct_grondgebonden": round(pct_grondgebonden, 1),
            "flats": profiel['flats'],
            "kleine_appartementen": profiel['kleine_appartementen']
        }
    }

# -----------------------------
# Stap 7: Export naar CSV
# -----------------------------
def export_adressen_csv(profiel, filename=None):
    """Exporteer adressen naar CSV voor mailinglijst"""
    import csv
    
    if not filename:
        filename = f"adressen_{profiel['buurtcode']}_{profiel['straal_m']}m.csv"
    
    adressen = profiel['adressen']
    
    # Filter alleen grondgebonden (optioneel)
    # adressen = [a for a in adressen if a['woningtype'] == 'grondgebonden']
    
    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'straat', 'huisnummer', 'huisletter', 'toevoeging', 
            'postcode', 'plaats', 'woningtype', 'afstand_m'
        ])
        writer.writeheader()
        
        for a in adressen:
            writer.writerow({
                'straat': a['straat'],
                'huisnummer': a['huisnummer'],
                'huisletter': a.get('huisletter') or '',
                'toevoeging': a.get('toevoeging') or '',
                'postcode': a['postcode'],
                'plaats': a['plaats'],
                'woningtype': a['woningtype'],
                'afstand_m': a['afstand_m']
            })
    
    print(f"💾 {len(adressen)} adressen geëxporteerd naar {filename}")
    return filename

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    # Configuratie
    straat = "Archimedesstraat"
    huisnr = "22"
    plaats = "Apeldoorn"
    straal = 150  # meters
    
    # Haal profiel op
    profiel = get_profiel_voor_straal(straat, huisnr, plaats, straal)
    
    # Analyseer voor buurtcampagne
    print("\n" + "="*50)
    print("📊 BUURTCAMPAGNE ANALYSE")
    print("="*50)
    
    analyse = analyseer_voor_buurtcampagne(profiel)
    
    print(f"\n{analyse['aanbeveling']}")
    print(f"Score: {analyse['score']}/{analyse['max_score']}\n")
    
    for regel in analyse['analyse']:
        print(f"  {regel}")
    
    print(f"\n📈 Statistieken:")
    for key, val in analyse['stats'].items():
        print(f"  {key}: {val}")
    
    # CBS context
    print(f"\n🏘️ Buurt context ({profiel['buurt']}):")
    for key, val in profiel['cbs'].items():
        if val is not None:
            print(f"  {key}: {val}")
    
    # Export (uncomment om te gebruiken)
    # export_adressen_csv(profiel)
    
    # Toon eerste 10 adressen
    print(f"\n📋 Eerste 10 adressen:")
    for a in profiel['adressen'][:100]:
        adres = f"{a['straat']} {a['huisnummer']}"
        if a.get('huisletter'):
            adres += a['huisletter']
        if a.get('toevoeging'):
            adres += f"-{a['toevoeging']}"
        print(f"  {adres} ({a['woningtype']}, {a['afstand_m']}m)")