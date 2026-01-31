"""
Test: Geveloriëntatie bepalen via PERCEEL POLYGON
=================================================
De KORTE zijde van het perceel is meestal de voorgevel (aan de straat).
We bepalen welke korte zijde het dichtstbij de straat ligt.
"""
import requests
import math

LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
KADASTER_WFS = "https://service.pdok.nl/kadaster/kadastralekaart/wfs/v5_0"

def get_rd_from_address(straat, huisnr, plaats):
    """Haal RD coordinaten op voor een adres"""
    params = {"q": f"{straat} {huisnr} {plaats}", "rows": 1, "fl": "centroide_rd,weergavenaam"}
    r = requests.get(LOCATIESERVER, params=params, timeout=10)
    docs = r.json()["response"]["docs"]
    if docs:
        centroid = docs[0].get("centroide_rd", "")
        if centroid:
            coord = centroid.replace("POINT(", "").replace(")", "")
            x, y = map(float, coord.split())
            return x, y, docs[0].get("weergavenaam")
    return None, None, None

def get_straat_punten(straat, plaats, count=20):
    """Haal meerdere punten op de SPECIFIEKE straat op (voor straat locatie bepaling)"""
    params = {
        "q": f"{straat} {plaats}",
        "rows": count,
        "fl": "centroide_rd,huisnummer,straatnaam",
        "fq": "type:adres"
    }
    r = requests.get(LOCATIESERVER, params=params, timeout=10)
    docs = r.json()["response"]["docs"]
    
    punten = []
    straat_lower = straat.lower()
    
    for doc in docs:
        # Filter: alleen adressen op de juiste straat
        doc_straat = doc.get("straatnaam", "").lower()
        if straat_lower not in doc_straat and doc_straat not in straat_lower:
            continue
            
        centroid = doc.get("centroide_rd", "")
        if centroid:
            coord = centroid.replace("POINT(", "").replace(")", "")
            x, y = map(float, coord.split())
            punten.append((x, y))
    
    return punten

def get_perceel_polygon(x, y):
    """Haal perceel polygon op via Kadaster WFS"""
    buffer = 5
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeName": "kadastralekaart:Perceel",
        "outputFormat": "application/json",
        "bbox": f"{x-buffer},{y-buffer},{x+buffer},{y+buffer},EPSG:28992",
        "count": "1"
    }
    
    r = requests.get(KADASTER_WFS, params=params, timeout=15)
    
    if r.status_code == 200:
        data = r.json()
        features = data.get("features", [])
        if features:
            return features[0]
    return None

def bereken_zijde_lengte(p1, p2):
    """Bereken lengte tussen 2 punten"""
    return math.sqrt((p2[0] - p1[0])**2 + (p2[1] - p1[1])**2)

def bereken_zijde_hoek(p1, p2):
    """
    Bereken hoek van een zijde in graden
    0° = Noord, 90° = Oost, 180° = Zuid, 270° = West
    
    Dit is de richting waarin de gevel KIJKT (loodrecht op de zijde)
    """
    dx = p2[0] - p1[0]  # Oost-West
    dy = p2[1] - p1[1]  # Noord-Zuid
    
    # Hoek van de zijde zelf
    zijde_hoek_rad = math.atan2(dx, dy)
    zijde_hoek = math.degrees(zijde_hoek_rad)
    
    # Normaliseer naar 0-360
    if zijde_hoek < 0:
        zijde_hoek += 360
    
    return zijde_hoek

def hoek_naar_richting(hoek):
    """Converteer hoek naar windrichting"""
    richtingen = ["N", "NO", "O", "ZO", "Z", "ZW", "W", "NW"]
    idx = int((hoek + 22.5) / 45) % 8
    return richtingen[idx]

def middelpunt_zijde(p1, p2):
    """Bereken middelpunt van een zijde"""
    return ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)

def afstand_tot_straat(punt, straat_punten):
    """Bereken minimale afstand van punt tot straat (set van punten)"""
    if not straat_punten:
        return float('inf')
    
    min_afstand = float('inf')
    for sp in straat_punten:
        afstand = math.sqrt((punt[0] - sp[0])**2 + (punt[1] - sp[1])**2)
        min_afstand = min(min_afstand, afstand)
    
    return min_afstand

def analyseer_perceel(geometry, straat_punten):
    """
    Analyseer perceel polygon
    - Vind korte zijdes
    - Bepaal welke korte zijde het dichtstbij de straat ligt
    """
    coords = geometry.get("coordinates", [])
    
    if geometry.get("type") == "Polygon":
        coords = coords[0]
    elif geometry.get("type") == "MultiPolygon":
        coords = coords[0][0]
    
    if len(coords) < 3:
        return None
    
    # Bereken alle zijdes
    zijdes = []
    for i in range(len(coords) - 1):
        p1 = coords[i]
        p2 = coords[i + 1]
        lengte = bereken_zijde_lengte(p1, p2)
        hoek = bereken_zijde_hoek(p1, p2)
        midden = middelpunt_zijde(p1, p2)
        afstand_straat = afstand_tot_straat(midden, straat_punten)
        
        zijdes.append({
            "p1": p1,
            "p2": p2,
            "lengte": lengte,
            "hoek_zijde": hoek,
            "richting_zijde": hoek_naar_richting(hoek),
            "midden": midden,
            "afstand_straat": afstand_straat
        })
    
    return zijdes

def bepaal_voorgevel(zijdes):
    """
    Bepaal welke zijde de voorgevel is:
    1. Pak de 2 kortste zijdes
    2. Degene die het dichtstbij de straat ligt = voorgevel
    """
    # Sorteer op lengte
    zijdes_op_lengte = sorted(zijdes, key=lambda z: z["lengte"])
    
    # Pak kortste 2 (of 50% kortste als er meer zijdes zijn)
    n_kort = max(2, len(zijdes) // 2)
    korte_zijdes = zijdes_op_lengte[:n_kort]
    
    # Van de korte zijdes, pak degene dichtstbij straat
    voorgevel = min(korte_zijdes, key=lambda z: z["afstand_straat"])
    
    return voorgevel, korte_zijdes

def test_adres(straat, huisnr, plaats):
    """Test geveloriëntatie voor een adres"""
    
    print(f"\n{'='*60}")
    print(f"🏠 {straat} {huisnr}, {plaats}")
    print("="*60)
    
    # Stap 1: Haal coordinaten
    x, y, naam = get_rd_from_address(straat, huisnr, plaats)
    if not x:
        print("❌ Adres niet gevonden")
        return None
    
    print(f"✓ Gevonden: {naam}")
    print(f"   RD: {x:.1f}, {y:.1f}")
    
    # Stap 2: Haal straat punten (ALLEEN van vestigingsstraat)
    straat_punten = get_straat_punten(straat, plaats)
    print(f"✓ {len(straat_punten)} punten op {straat} gevonden")
    
    # Stap 3: Haal perceel
    perceel = get_perceel_polygon(x, y)
    if not perceel:
        print("❌ Perceel niet gevonden")
        return None
    
    props = perceel.get("properties", {})
    opp = props.get("kadastraleGrootteWaarde")
    print(f"✓ Perceel: {opp} m²")
    
    # Stap 4: Analyseer polygon
    geometry = perceel.get("geometry")
    if not geometry:
        print("❌ Geen geometrie")
        return None
    
    zijdes = analyseer_perceel(geometry, straat_punten)
    if not zijdes:
        print("❌ Kan zijdes niet berekenen")
        return None
    
    # Print alle zijdes
    print(f"\n📐 Alle perceel zijdes:")
    zijdes_sorted = sorted(zijdes, key=lambda z: z["lengte"])
    for i, z in enumerate(zijdes_sorted):
        print(f"   {i+1}. {z['lengte']:5.1f}m | loopt {z['richting_zijde']:>2} | afstand straat: {z['afstand_straat']:.1f}m")
    
    # Stap 5: Bepaal voorgevel
    voorgevel, korte_zijdes = bepaal_voorgevel(zijdes)
    
    print(f"\n🔍 Korte zijdes (kandidaat voorgevel):")
    for z in korte_zijdes:
        marker = " ← VOORGEVEL" if z == voorgevel else ""
        print(f"   {z['lengte']:5.1f}m | afstand straat: {z['afstand_straat']:.1f}m{marker}")
    
    # De gevel KIJKT loodrecht op de zijde
    # +90° geeft de richting naar "buiten" (naar de straat)
    gevel_hoek = (voorgevel["hoek_zijde"] + 90) % 360
    gevel_richting = hoek_naar_richting(gevel_hoek)
    
    # Achtertuin is tegenovergesteld
    achter_hoek = (gevel_hoek + 180) % 360
    achter_richting = hoek_naar_richting(achter_hoek)
    
    print(f"\n🧭 GEVELORIËNTATIE:")
    print(f"   Voorgevel: {voorgevel['lengte']:.1f}m breed")
    print(f"   Voorgevel kijkt naar: {gevel_richting} ({gevel_hoek:.0f}°)")
    print(f"   Achtertuin kijkt naar: {achter_richting} ({achter_hoek:.0f}°)")
    
    # Zon indicatie
    if gevel_richting in ["Z", "ZO", "ZW"]:
        print(f"\n   ☀️  Voorgevel op ZUIDEN → veel zon aan voorkant")
    elif gevel_richting in ["N", "NO", "NW"]:
        print(f"\n   🌲 Voorgevel op NOORDEN → achtertuin heeft zon")
    elif gevel_richting in ["O"]:
        print(f"\n   🌅 Voorgevel op OOSTEN → ochtendzon voorkant")
    elif gevel_richting in ["W"]:
        print(f"\n   🌇 Voorgevel op WESTEN → avondzon voorkant")
    
    return {
        "voorgevel_breedte": voorgevel["lengte"],
        "gevel_hoek": gevel_hoek,
        "gevel_richting": gevel_richting,
        "achter_richting": achter_richting
    }


if __name__ == "__main__":
    
    # Test adressen
    TEST_ADRESSEN = [
        ("Slangenburg", "62", "Barneveld"),
        ("Oldenbarnevelderweg", "111", "Barneveld"),
    ]
    
    print("\n" + "#"*60)
    print("# GEVELORIËNTATIE VIA PERCEEL POLYGON")
    print("# (korte zijde + dichtstbij straat = voorgevel)")
    print("#"*60)
    
    for straat, huisnr, plaats in TEST_ADRESSEN:
        test_adres(straat, huisnr, plaats)
    
    # Interactieve modus
    print("\n" + "="*60)
    print("INTERACTIEVE TEST")
    print("="*60)
    print("Voer een adres in (straat huisnr plaats) of 'stop'")
    
    while True:
        print("\n")
        adres = input("Adres: ").strip()
        if adres.lower() in ['stop', 'quit', 'exit', '']:
            break
        
        parts = adres.rsplit(' ', 2)
        if len(parts) >= 3:
            plaats = parts[-1]
            huisnr = parts[-2]
            straat = ' '.join(parts[:-2])
        elif len(parts) == 2:
            straat, huisnr = parts[0], parts[1]
            plaats = "Barneveld"
        else:
            print("Gebruik: straatnaam huisnummer plaats")
            continue
        
        test_adres(straat, huisnr, plaats)