"""
BUURTCAMPAGNE DATA VERZAMELAAR v2
=================================
Verzamelt zoveel mogelijk data per woning voor analyse.

DATABRONNEN:
- BAG: Adres, woningtype, bouwjaar, woonoppervlakte
- Kadaster: Perceeloppervlakte
- EP-Online: Energielabel
- CBS: Buurtstatistieken
- [Optioneel] 3DBAG: Dakoppervlak, oriëntatie, hellingshoek, bouwlagen

WONINGTYPES:
- LAAGBOUW: vrijstaand, 2-onder-1-kap, rijwoning, hoekwoning
- HOOGBOUW: appartement, flat, portiek, galerij
"""

import requests
import math
from pyproj import Transformer
from collections import defaultdict
import cbsodata
import csv
from datetime import datetime
import time

# Config
LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
BAG_WFS = "https://service.pdok.nl/lv/bag/wfs/v2_0"
BAG_3D_API = "https://api.3dbag.nl/collections/pand/items"
KADASTER_WFS = "https://service.pdok.nl/kadaster/kadastralekaart/wfs/v5_0"
EP_ONLINE_API = "https://public.ep-online.nl/api/v5"

EP_ONLINE_API_KEY = "NTYxMTZCQ0ZENUVGNkJBMzlFOTY3MjQyQ0MxMURBRjFDNUM0QkI3REI4MzI4QUIxNjVFOTNGRUE1MDk4RDlEMTNFNThCQTRCNTExMDJGNDQ2QjQ2REFEM0M1OERBODBC"

transformer = Transformer.from_crs("EPSG:28992", "EPSG:4326", always_xy=True)

def afstand(x1, y1, x2, y2):
    return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)

def rd_to_latlon(x, y):
    lon, lat = transformer.transform(x, y)
    return lat, lon

def orientatie_naam(azimut):
    if azimut is None:
        return None
    azimut = float(azimut) % 360
    if azimut < 22.5 or azimut >= 337.5: return "N"
    elif azimut < 67.5: return "NO"
    elif azimut < 112.5: return "O"
    elif azimut < 157.5: return "ZO"
    elif azimut < 202.5: return "Z"
    elif azimut < 247.5: return "ZW"
    elif azimut < 292.5: return "W"
    else: return "NW"

def hoek_naar_richting(hoek):
    """Converteer hoek (0-360) naar windrichting"""
    richtingen = ["N", "NO", "O", "ZO", "Z", "ZW", "W", "NW"]
    return richtingen[int((hoek + 22.5) / 45) % 8]

def bepaal_gevel_orientatie(geometry, adres_x, adres_y):
    """
    Bepaal geveloriëntatie op basis van perceel geometrie en adres centroïde.
    
    Methode:
    1. Bereken centrum van perceel
    2. Richting centrum → adres = straat-richting
    3. Pak de 2 kortste zijdes (voor/achter bij rijtjeshuis)
    4. Kies de korte zijde met kleinste hoek_verschil = voorgevel
    
    Returns:
        dict met 'gevel_richting' (N/NO/O/ZO/Z/ZW/W/NW) en 'betrouwbaar' (bool)
        of None als niet te bepalen
    """
    if not geometry:
        return None
    
    # Extract coords
    coords = geometry.get("coordinates", [])
    if geometry.get("type") == "Polygon":
        coords = coords[0]
    elif geometry.get("type") == "MultiPolygon":
        coords = coords[0][0]
    
    if len(coords) < 4:  # Minimaal een driehoek + sluitpunt
        return None
    
    # Bereken centrum van perceel
    n = len(coords) - 1  # laatste punt = eerste punt
    centrum_x = sum(c[0] for c in coords[:-1]) / n
    centrum_y = sum(c[1] for c in coords[:-1]) / n
    
    # Afstand adres tot centrum
    afstand_adres_centrum = math.sqrt((adres_x - centrum_x)**2 + (adres_y - centrum_y)**2)
    
    # Als adres en centrum te dicht bij elkaar liggen, is richting onbetrouwbaar
    if afstand_adres_centrum < 1.0:
        return {'gevel_richting': None, 'betrouwbaar': False, 'reden': 'adres=centrum'}
    
    # Richting van centrum naar adres = straat-richting
    dx = adres_x - centrum_x
    dy = adres_y - centrum_y
    straat_hoek = math.degrees(math.atan2(dx, dy))
    if straat_hoek < 0:
        straat_hoek += 360
    
    # Bereken alle zijdes
    zijdes = []
    for i in range(len(coords) - 1):
        p1 = coords[i]
        p2 = coords[i + 1]
        lengte = math.sqrt((p2[0]-p1[0])**2 + (p2[1]-p1[1])**2)
        midden = ((p1[0]+p2[0])/2, (p1[1]+p2[1])/2)
        
        # Richting van centrum naar midden van deze zijde
        dx_z = midden[0] - centrum_x
        dy_z = midden[1] - centrum_y
        zijde_richting = math.degrees(math.atan2(dx_z, dy_z))
        if zijde_richting < 0:
            zijde_richting += 360
        
        # Hoek van de zijde zelf (voor gevelrichting berekening)
        hoek_zijde = math.degrees(math.atan2(p2[0]-p1[0], p2[1]-p1[1]))
        if hoek_zijde < 0:
            hoek_zijde += 360
        
        # Verschil met straat-richting
        hoek_verschil = abs(zijde_richting - straat_hoek)
        if hoek_verschil > 180:
            hoek_verschil = 360 - hoek_verschil
        
        zijdes.append({
            "lengte": lengte,
            "hoek_zijde": hoek_zijde,
            "hoek_verschil": hoek_verschil
        })
    
    # Sorteer op lengte, pak 2 kortste als kandidaten (voor/achter)
    zijdes_sorted = sorted(zijdes, key=lambda z: z["lengte"])
    kandidaten = zijdes_sorted[:2]
    
    # Voorgevel = kandidaat met kleinste hoek_verschil (meest richting straat)
    voorgevel = min(kandidaten, key=lambda z: z["hoek_verschil"])
    
    # Bereken gevelrichting (loodrecht op de zijde, richting straat)
    gevel_hoek = (voorgevel["hoek_zijde"] + 90) % 360
    
    # Check of gevel naar straat wijst of weg ervan
    gevel_check = abs(gevel_hoek - straat_hoek)
    if gevel_check > 180:
        gevel_check = 360 - gevel_check
    if gevel_check > 90:
        # Gevel wijst verkeerde kant op, draai 180°
        gevel_hoek = (gevel_hoek + 180) % 360
    
    gevel_richting = hoek_naar_richting(gevel_hoek)
    
    # Betrouwbaarheid: als hoek_verschil klein is, is het zeker
    betrouwbaar = voorgevel["hoek_verschil"] < 45
    
    return {
        'gevel_richting': gevel_richting,
        'gevel_hoek': round(gevel_hoek, 0),
        'betrouwbaar': betrouwbaar,
        'afstand_adres_centrum': round(afstand_adres_centrum, 1)
    }

# -----------------------------
# 1. Locatie opzoeken
# -----------------------------
def get_rd_from_address(straat, huisnr, plaats):
    params = {"q": f"{straat} {huisnr} {plaats}", "rows": 1, "fl": "centroide_rd"}
    r = requests.get(LOCATIESERVER, params=params)
    r.raise_for_status()
    docs = r.json()["response"]["docs"]
    if not docs:
        raise ValueError("Adres niet gevonden")
    coord = docs[0]["centroide_rd"].replace("POINT(", "").replace(")", "")
    x, y = map(float, coord.split())
    return x, y

def get_buurtcode(x, y):
    """Haal buurtcode op via PDOK reverse geocoding"""
    url = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse"
    params = {
        "X": x,
        "Y": y,
        "type": "buurt",
        "rows": 1,
        "fl": "buurtcode,buurtnaam,wijkcode,gemeentecode"
    }
    
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        docs = r.json().get("response", {}).get("docs", [])
        if docs:
            return docs[0].get('buurtcode'), docs[0].get('buurtnaam')
    except Exception as e:
        print(f"   ⚠️ Buurtcode lookup fout: {e}")
    
    return None, None

# -----------------------------
# 2. BAG Woningen ophalen
# -----------------------------
def get_bag_woningen(center_x, center_y, straal):
    print(f"📍 BAG woningen ophalen binnen {straal}m...")
    
    minx, maxx = center_x - straal, center_x + straal
    miny, maxy = center_y - straal, center_y + straal

    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": "bag:verblijfsobject", "outputFormat": "application/json",
        "bbox": f"{minx},{miny},{maxx},{maxy},EPSG:28992"
    }

    r = requests.get(BAG_WFS, params=params)
    r.raise_for_status()
    features = r.json().get("features", [])
    
    # Tel verblijfsobjecten per pand
    panden = defaultdict(list)
    for f in features:
        pand_id = f.get("properties", {}).get("pandidentificatie")
        if pand_id:
            panden[pand_id].append(f)

    woningen = []
    for f in features:
        geom = f.get("geometry")
        props = f.get("properties", {})
        if not geom or not props:
            continue
        
        # Filter op afstand
        x, y = geom["coordinates"]
        dist = afstand(center_x, center_y, x, y)
        if dist > straal:
            continue
        
        # Filter op woonfunctie
        gebruiksdoel = props.get("gebruiksdoel", "")
        if "woonfunctie" not in gebruiksdoel.lower():
            continue
        
        pand_id = props.get("pandidentificatie")
        aantal_in_pand = len(panden.get(pand_id, []))
        
        # Woningtype classificatie op basis van aantal adressen in pand
        # <= 3 adressen = laagbouw (eengezins, 2^1kap, of onderverhuur/kamer)
        # > 3 adressen = hoogbouw/gestapeld (flat, portiek, galerij)
        if aantal_in_pand <= 3:
            bouwtype = "LAAGBOUW"
        else:
            bouwtype = "HOOGBOUW"
        
        # Woningtype blijft leeg - wordt alleen gevuld door EP-Online
        woningtype = None
        
        lat, lon = rd_to_latlon(x, y)
        
        # Postcode opschonen (spaties verwijderen)
        postcode = props.get("postcode", "")
        if postcode:
            postcode = postcode.replace(" ", "").upper()
        
        woningen.append({
            # Adres
            "straat": props.get("openbare_ruimte"),
            "huisnr": props.get("huisnummer"),
            "huisletter": props.get("huisletter") or "",
            "toevoeging": props.get("huisnummertoevoeging") or "",
            "postcode": postcode,
            "plaats": props.get("woonplaats"),
            
            # Locatie
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "rd_x": round(x, 1),
            "rd_y": round(y, 1),
            "afstand_m": round(dist, 0),
            
            # BAG basis
            "bouwjaar": props.get("bouwjaar"),
            "woon_opp_m2": props.get("oppervlakte"),
            "bouwtype": bouwtype,  # LAAGBOUW of HOOGBOUW (schatting)
            "woningtype": woningtype,  # None tot EP-Online bevestigt
            "aantal_in_pand": aantal_in_pand,
            "pand_id": pand_id,
            
            # Wordt later ingevuld
            "perceel_opp_m2": None,
            "energielabel": None,
            
            # 3DBAG (optioneel)
            "dak_opp_m2": None,
            "dak_orientatie": None,
            "dak_orient_naam": None,
            "dak_helling_gr": None,
            "bouwlagen": None,
            "hoogte_nok_m": None,
            "hoogte_goot_m": None,
        })
    
    # Sorteer op afstand
    woningen.sort(key=lambda x: x['afstand_m'])
    
    # Stats
    laagbouw = len([w for w in woningen if w['bouwtype'] == 'LAAGBOUW'])
    hoogbouw = len([w for w in woningen if w['bouwtype'] == 'HOOGBOUW'])
    print(f"   ✓ {len(woningen)} woningen gevonden (~{laagbouw} laagbouw, ~{hoogbouw} hoogbouw)")
    print(f"   ℹ️  Woningtype wordt bepaald door EP-Online energielabel")
    
    return woningen

# -----------------------------
# 3. Kadaster percelen (VERBETERD)
# -----------------------------
def get_perceel_data(x, y, debug=False):
    """
    Haal perceeldata op via Kadaster WFS
    
    Returns dict met:
    - perceelnummer: bijv "8554" (het nummer op de kaart)
    - oppervlakte: in m²
    - geometry: polygon voor geveloriëntatie
    - kadastraleAanduiding: volledige kadastrale aanduiding
    """
    try:
        buffer = 0.1  # Kleine buffer om juiste perceel te pakken
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
        
        if r.status_code != 200:
            if debug:
                print(f"      Kadaster error: {r.status_code}")
            return None
            
        data = r.json()
        features = data.get("features", [])
        
        if not features:
            return None
        
        props = features[0].get("properties", {})
        geom = features[0].get("geometry")
        
        # Direct het perceelnummer veld gebruiken
        perceel_nr = props.get("perceelnummer")
        opp = props.get("kadastraleGrootteWaarde")
        kad_aanduiding = props.get("kadastraleAanduiding")
        
        if debug:
            print(f"      Perceel: {perceel_nr}, opp: {opp}m²")
        
        return {
            'perceelnummer': perceel_nr,
            'oppervlakte': opp,
            'geometry': geom,
            'kadastraleAanduiding': kad_aanduiding
        }
            
    except requests.exceptions.Timeout:
        if debug:
            print("      Timeout!")
    except Exception as e:
        if debug:
            print(f"      Error: {e}")
    return None


def get_perceel_opp(x, y, debug=False):
    """Haal perceeloppervlakte op via Kadaster WFS (backwards compatible)"""
    data = get_perceel_data(x, y, debug)
    if data:
        return data.get('oppervlakte')
    return None

def enrich_percelen(woningen, max_requests=150, debug=False):
    """Voeg perceeldata en geveloriëntatie toe - alleen voor LAAGBOUW"""
    print(f"📐 Perceeldata + geveloriëntatie ophalen...")
    
    # Filter alleen laagbouw
    laagbouw = [w for w in woningen if w['bouwtype'] == 'LAAGBOUW']
    print(f"   {len(laagbouw)} laagbouw woningen om te checken")
    
    count = 0
    found = 0
    gevel_found = 0
    errors = 0
    
    for w in laagbouw:
        if count >= max_requests:
            break
        
        # Haal volledige perceeldata op (inclusief perceelnummer en geometry)
        perceel_data = get_perceel_data(w['rd_x'], w['rd_y'], debug=(count < 3 and debug))
        
        if perceel_data:
            try:
                w['perceel_opp_m2'] = int(float(perceel_data['oppervlakte'])) if perceel_data['oppervlakte'] else None
                w['perceelnummer'] = perceel_data['perceelnummer']
                w['kadastrale_aanduiding'] = perceel_data.get('kadastraleAanduiding')
                found += 1
                
                # Bepaal geveloriëntatie op basis van perceel geometry
                if perceel_data.get('geometry'):
                    gevel_result = bepaal_gevel_orientatie(
                        perceel_data['geometry'], 
                        w['rd_x'], 
                        w['rd_y']
                    )
                    if gevel_result and gevel_result.get('betrouwbaar'):
                        w['gevel_orientatie'] = gevel_result['gevel_richting']
                        gevel_found += 1
                    else:
                        w['gevel_orientatie'] = None  # Onzeker, laat leeg
                else:
                    w['gevel_orientatie'] = None
                    
            except Exception as e:
                if debug:
                    print(f"      Error: {e}")
                errors += 1
        else:
            errors += 1
        
        count += 1
        
        # Progress
        if count % 25 == 0:
            print(f"   ... {count}/{len(laagbouw)} ({found} percelen, {gevel_found} gevels)")
            time.sleep(0.2)
    
    print(f"   ✓ {found} percelen gevonden, {gevel_found} geveloriëntaties bepaald")
    
    return woningen


def bereken_eigendom_score(woningen, cbs_data=None):
    """
    Bereken eigendom score per woning - NIEUW VEREENVOUDIGD MODEL.
    
    Regels:
    1. Vrijstaand / 2^1kap → 95% KOOP (klaar)
    2. Meerdere adressen in 1 PAND (flat/appartement) → CBS huur% bepaalt kans
    3. Meerdere adressen op 1 PERCEEL (laagbouw) → 80% HUUR + CBS correctie
    4. 1 adres op 1 perceel (rijtjeswoning) → 70% KOOP - CBS correctie
    5. Familie-erf → aparte detectie (2 woningen, groot perceel, verschillend volume)
    
    CBS huur% correctie:
    - Per 10% huur → +1 richting huur (of -1 richting koop)
    
    Factoren die NIET meer meetellen:
    - Rijwoning/hoekwoning als type
    - WOZ waarde
    - Bouwjaar matching
    """
    print(f"🎯 Eigendom scoremodel berekenen (v2)...")
    start = time.time()
    
    # CBS data
    cbs_huur_pct = cbs_data.get('pct_huur') if cbs_data else None
    cbs_stedelijkheid = cbs_data.get('stedelijkheid') if cbs_data else None
    
    if cbs_huur_pct:
        print(f"   CBS huur%: {cbs_huur_pct}%")
    if cbs_stedelijkheid:
        print(f"   CBS stedelijkheid: {cbs_stedelijkheid}")
    
    # CBS huur correctie factor (per 10% = 1 punt)
    cbs_huur_factor = int(cbs_huur_pct / 10) if cbs_huur_pct else 0
    
    # Groepeer per perceelnummer voor groepsanalyse
    from collections import defaultdict
    perceel_groepen = defaultdict(list)
    
    for w in woningen:
        perceel_nr = w.get('perceelnummer')
        if perceel_nr:
            perceel_groepen[perceel_nr].append(w)
    
    # Analyseer elke groep voor familie-erf detectie
    for perceel_nr, groep in perceel_groepen.items():
        aantal = len(groep)
        
        # Bereken groepskenmerken
        woon_opps = [w.get('woon_opp_m2') for w in groep if w.get('woon_opp_m2')]
        perc_opp = groep[0].get('perceel_opp_m2', 0) or 0
        
        # Verschillend bouwvolume detectie (voor familie-erf)
        verschillend_volume = False
        if len(woon_opps) >= 2 and aantal == 2:
            gem_opp = sum(woon_opps) / len(woon_opps)
            opp_variatie = max(woon_opps) - min(woon_opps)
            variatie_pct = (opp_variatie / gem_opp) * 100 if gem_opp > 0 else 0
            # >25% verschil bij 2 woningen = significant
            verschillend_volume = variatie_pct > 25
        
        for w in groep:
            w['perceel_gedeeld_door'] = aantal
            w['groep_verschillend_volume'] = verschillend_volume
            w['perceel_opp_totaal'] = perc_opp
    
    # Bereken scores per woning
    for w in woningen:
        signalen = []
        
        aantal_op_perceel = w.get('perceel_gedeeld_door', 1) or 1
        woningtype = (w.get('woningtype') or w.get('woningtype_geschat') or '').lower()
        bouwtype = w.get('bouwtype', '')
        perceel_opp = w.get('perceel_opp_totaal', 0) or 0
        verschillend_volume = w.get('groep_verschillend_volume', False)
        aantal_in_pand = w.get('aantal_in_pand', 1) or 1
        
        # Detecteer flat/hoogbouw ALLEEN op basis van aantal adressen in pand
        # >3 adressen in 1 pand = echt gestapelde bouw (flat, portiek, galerij)
        # <=3 adressen in 1 pand = laagbouw (ook als EP-Online "appartement" zegt - dan is het onderverhuur)
        is_flat = aantal_in_pand > 3
        
        # === REGEL 1: Vrijstaand / 2^1kap → 95% KOOP ===
        # ALLEEN als het ook echt een eigen perceel is (niet meerdere woningen op 1 perceel)
        if aantal_op_perceel <= 2 and ('vrijstaand' in woningtype or '2^1kap' in woningtype or '2-onder' in woningtype or 'twee-onder' in woningtype):
            w['kans_koop'] = 95
            w['kans_huur'] = 5
            w['kans_familie_erf'] = 0
            w['eigendom'] = 'KOOP'
            w['eigendom_kans'] = 95
            signalen.append(f"vrijstaand/2^1kap")
            w['signalen'] = signalen
            w['eigendom_toelichting'] = "95% koop - vrijstaand/2^1kap"
            continue
        
        # === REGEL 5: Familie-erf detectie (voor andere regels) ===
        # 2 woningen op groot perceel met verschillend volume
        is_familie_erf = False
        if aantal_op_perceel == 2 and verschillend_volume and perceel_opp > 400:
            is_familie_erf = True
            w['kans_koop'] = 20
            w['kans_huur'] = 10
            w['kans_familie_erf'] = 70
            w['eigendom'] = 'FAM'
            w['eigendom_kans'] = 70
            signalen.append(f"2 won + groot perceel + versch. volume")
            w['signalen'] = signalen
            w['eigendom_toelichting'] = "70% familie-erf - 2 woningen verschillend volume"
            continue
        
        # === REGEL 2: Flat/appartement → CBS huur% bepaalt ===
        if is_flat:
            # Basis 50%, CBS huur% verschuift
            basis_huur = 50
            huur_kans = min(95, basis_huur + (cbs_huur_factor * 5))  # Sterker effect voor flats
            koop_kans = 100 - huur_kans
            
            w['kans_koop'] = koop_kans
            w['kans_huur'] = huur_kans
            w['kans_familie_erf'] = 0
            
            if huur_kans >= 50:
                w['eigendom'] = 'HUUR'
                w['eigendom_kans'] = huur_kans
            else:
                w['eigendom'] = 'KOOP'
                w['eigendom_kans'] = koop_kans
            
            signalen.append(f"flat/appartement")
            if cbs_huur_pct:
                signalen.append(f"CBS huur {cbs_huur_pct}%")
            w['signalen'] = signalen
            w['eigendom_toelichting'] = f"{w['eigendom_kans']}% {w['eigendom'].lower()} - flat + CBS"
            continue
        
        # === REGEL 3: Meerdere adressen op 1 perceel (laagbouw) → 80% HUUR ===
        if aantal_op_perceel > 1:
            # Basis 80% huur
            basis_huur = 80
            
            # CBS correctie
            extra_huur = cbs_huur_factor  # +1 per 10% CBS huur
            
            # Stedelijkheid > 2 + groot perceel > 2000m² → +1 huur
            if cbs_stedelijkheid and cbs_stedelijkheid > 2 and perceel_opp > 2000:
                extra_huur += 1
                signalen.append(f"stedelijk + groot perceel")
            
            huur_kans = min(95, basis_huur + extra_huur)
            koop_kans = 100 - huur_kans
            
            w['kans_koop'] = koop_kans
            w['kans_huur'] = huur_kans
            w['kans_familie_erf'] = 0
            
            w['eigendom'] = 'HUUR'
            w['eigendom_kans'] = huur_kans
            
            signalen.append(f"{aantal_op_perceel} adressen/perceel")
            if cbs_huur_pct:
                signalen.append(f"CBS huur {cbs_huur_pct}%")
            w['signalen'] = signalen
            w['eigendom_toelichting'] = f"{huur_kans}% huur - meerdere op perceel"
            continue
        
        # === REGEL 4: 1 adres op 1 perceel (rijtjeswoning) → 70% KOOP ===
        # Basis 70% koop
        basis_koop = 70
        
        # CBS correctie: per 10% CBS huur → -1 koop
        koop_kans = max(30, basis_koop - cbs_huur_factor)
        huur_kans = 100 - koop_kans
        
        w['kans_koop'] = koop_kans
        w['kans_huur'] = huur_kans
        w['kans_familie_erf'] = 0
        
        if koop_kans >= 50:
            w['eigendom'] = 'KOOP'
            w['eigendom_kans'] = koop_kans
        else:
            w['eigendom'] = 'HUUR'
            w['eigendom_kans'] = huur_kans
        
        signalen.append(f"eigen perceel")
        if cbs_huur_pct:
            signalen.append(f"CBS huur {cbs_huur_pct}%")
        w['signalen'] = signalen
        w['eigendom_toelichting'] = f"{w['eigendom_kans']}% {w['eigendom'].lower()} - rijtjeswoning"
    
    # Statistieken
    huur = len([w for w in woningen if w.get('eigendom') == 'HUUR'])
    koop = len([w for w in woningen if w.get('eigendom') == 'KOOP'])
    fam = len([w for w in woningen if w.get('eigendom') == 'FAM'])
    flats = len([w for w in woningen if w.get('bouwtype') == 'HOOGBOUW'])
    
    print(f"   ✓ KOOP: {koop}")
    print(f"   ✓ HUUR: {huur}")
    print(f"   ✓ FAM (familie-erf): {fam}")
    print(f"   ✓ Flats/hoogbouw: {flats}")
    print(f"   ⏱️  Score berekening: {time.time()-start:.1f}s")
    
    return woningen


def detecteer_huur_gedeeld_perceel(woningen, cbs_data=None):
    """
    Wrapper die het scoremodel aanroept.
    Behouden voor backwards compatibility.
    """
    return bereken_eigendom_score(woningen, cbs_data)


def parse_huisnummer(huisnr_raw):
    """Parse huisnummer naar numeriek deel en suffix"""
    if huisnr_raw is None:
        return None, None
    
    huisnr_str = str(huisnr_raw).strip()
    if not huisnr_str:
        return None, None
    
    # Extract numeriek deel
    num_part = ''
    suffix = ''
    for i, c in enumerate(huisnr_str):
        if c.isdigit():
            num_part += c
        else:
            suffix = huisnr_str[i:]
            break
    
    if not num_part:
        return None, suffix
    
    return int(num_part), suffix


def detecteer_woningtype_via_straat(woningen, debug=False):
    """
    Detecteer woningtype op basis van straatcontext en energielabel ankers.
    
    Principe:
    1. Energielabel heeft ALTIJD gelijk - nooit overschrijven
    2. Gebruik bekende labels als ankers
    3. Propageer vanaf anker: nummers op/af lopen
    4. Groter perceel/woning = hoekwoning (einde blok)
    5. Zelfde grootte = rijwoning (midden blok)
    
    Returns: woningen met toegevoegd:
    - woningtype_geschat: gedetecteerd type (als geen energielabel)
    - woningtype_zekerheid: percentage
    - woningtype_reden: toelichting
    """
    from collections import defaultdict
    
    # Stap 1: Groepeer per straat, gescheiden in even/oneven
    straten = defaultdict(lambda: {'even': [], 'oneven': []})
    
    for w in woningen:
        straat = w.get('straatnaam')
        if not straat:
            continue
        
        huisnr, suffix = parse_huisnummer(w.get('huisnummer'))
        if huisnr is None:
            continue
        
        w['_huisnr_num'] = huisnr
        w['_huisnr_suffix'] = suffix or ''
        
        # Even/oneven bepaalt welke kant van de straat
        if huisnr % 2 == 0:
            straten[straat]['even'].append(w)
        else:
            straten[straat]['oneven'].append(w)
    
    geschat_count = 0
    anker_count = 0
    propagated_count = 0
    
    # Stap 2: Per straat + zijde analyseren
    for straat, zijden in straten.items():
        for zijde, huizen in zijden.items():
            if len(huizen) < 2:
                continue
            
            # Sorteer op huisnummer
            huizen.sort(key=lambda x: (x['_huisnr_num'], x['_huisnr_suffix']))
            
            # Stap 3: Vind ankers (woningen met energielabel rijwoning/tussenwoning)
            ankers = []
            for i, huis in enumerate(huizen):
                woningtype = huis.get('woningtype', '').lower()
                if woningtype and ('rij' in woningtype or 'tussen' in woningtype or 'midden' in woningtype):
                    ankers.append(i)
                    anker_count += 1
            
            if not ankers:
                # Geen ankers - fallback naar perceelgrootte
                for i, huis in enumerate(huizen):
                    if huis.get('woningtype'):  # Energielabel = waarheid
                        continue
                    schatting = _schat_woningtype_simpel(huis)
                    if schatting:
                        huis['woningtype_geschat'] = schatting['geschat_type']
                        huis['woningtype_zekerheid'] = schatting['zekerheid_pct']
                        huis['woningtype_reden'] = schatting['reden']
                        geschat_count += 1
                continue
            
            # Stap 4: Propageer vanaf ankers
            for anker_idx in ankers:
                anker = huizen[anker_idx]
                anker_perceel = anker.get('perceel_opp_m2') or 0
                anker_woon = anker.get('woon_opp_m2') or 0
                
                # Propageer nummers OMHOOG (naar einde straat)
                for i in range(anker_idx + 1, len(huizen)):
                    huis = huizen[i]
                    if huis.get('woningtype'):  # Energielabel = altijd gelijk
                        # Check of dit een hoek is (dan stoppen met propageren)
                        if 'hoek' in huis.get('woningtype', '').lower():
                            break
                        continue
                    
                    huis_perceel = huis.get('perceel_opp_m2') or 0
                    huis_woon = huis.get('woon_opp_m2') or 0
                    
                    # Vergelijk met anker
                    perceel_groter = huis_perceel > anker_perceel * 1.15 if anker_perceel else False
                    woon_groter = huis_woon > anker_woon * 1.10 if anker_woon else False
                    
                    if perceel_groter or woon_groter:
                        # Significant groter = hoekwoning
                        huis['woningtype_geschat'] = 'hoekwoning'
                        huis['woningtype_zekerheid'] = 75
                        redenen = []
                        if perceel_groter:
                            redenen.append(f'perceel groter ({int(huis_perceel)}m² vs {int(anker_perceel)}m²)')
                        if woon_groter:
                            redenen.append(f'woning groter ({int(huis_woon)}m² vs {int(anker_woon)}m²)')
                        huis['woningtype_reden'] = f'hoek (einde blok): {", ".join(redenen)}'
                        propagated_count += 1
                        break  # Stop propageren, nieuw blok begint
                    else:
                        # Zelfde grootte = rijwoning
                        huis['woningtype_geschat'] = 'rijwoning'
                        huis['woningtype_zekerheid'] = 80
                        huis['woningtype_reden'] = f'zelfde grootte als anker nr {anker.get("huisnummer")}'
                        propagated_count += 1
                
                # Propageer nummers OMLAAG (naar begin straat)
                for i in range(anker_idx - 1, -1, -1):
                    huis = huizen[i]
                    if huis.get('woningtype'):  # Energielabel = altijd gelijk
                        if 'hoek' in huis.get('woningtype', '').lower():
                            break
                        continue
                    
                    huis_perceel = huis.get('perceel_opp_m2') or 0
                    huis_woon = huis.get('woon_opp_m2') or 0
                    
                    perceel_groter = huis_perceel > anker_perceel * 1.15 if anker_perceel else False
                    woon_groter = huis_woon > anker_woon * 1.10 if anker_woon else False
                    
                    if perceel_groter or woon_groter:
                        huis['woningtype_geschat'] = 'hoekwoning'
                        huis['woningtype_zekerheid'] = 75
                        redenen = []
                        if perceel_groter:
                            redenen.append(f'perceel groter ({int(huis_perceel)}m² vs {int(anker_perceel)}m²)')
                        if woon_groter:
                            redenen.append(f'woning groter ({int(huis_woon)}m² vs {int(anker_woon)}m²)')
                        huis['woningtype_reden'] = f'hoek (begin blok): {", ".join(redenen)}'
                        propagated_count += 1
                        break
                    else:
                        huis['woningtype_geschat'] = 'rijwoning'
                        huis['woningtype_zekerheid'] = 80
                        huis['woningtype_reden'] = f'zelfde grootte als anker nr {anker.get("huisnummer")}'
                        propagated_count += 1
    
    # Cleanup tijdelijke velden
    for w in woningen:
        w.pop('_huisnr_num', None)
        w.pop('_huisnr_suffix', None)
    
    if debug or anker_count > 0:
        print(f"   ✓ Straatanalyse: {anker_count} ankers gevonden, {propagated_count} types gepropageerd")
    if geschat_count > 0:
        print(f"   ✓ {geschat_count} woningtypes geschat (fallback perceelgrootte)")
    
    return woningen


def _schat_woningtype_simpel(woning):
    """
    Simpele fallback schatting op basis van perceelgrootte.
    Alleen gebruikt als geen straatcontext beschikbaar.
    """
    perceel_opp = woning.get('perceel_opp_m2') or 0
    woon_opp = woning.get('woon_opp_m2') or 0
    aantal_op_perceel = woning.get('perceel_gedeeld_door') or 1
    
    if perceel_opp == 0:
        return None
    
    if aantal_op_perceel > 1:
        return None
    
    if perceel_opp >= 800:
        zekerheid = min(95, 70 + (perceel_opp - 800) / 20)
        return {
            'geschat_type': 'vrijstaand',
            'zekerheid_pct': round(zekerheid),
            'reden': f'groot perceel ({int(perceel_opp)}m²)'
        }
    elif perceel_opp >= 500:
        if woon_opp >= 150:
            zekerheid = 60 + min(20, (woon_opp - 150) / 5)
            return {
                'geschat_type': 'vrijstaand',
                'zekerheid_pct': round(zekerheid),
                'reden': f'perceel {int(perceel_opp)}m² + woning {int(woon_opp)}m²'
            }
        else:
            return {
                'geschat_type': '2^1kap',
                'zekerheid_pct': 55,
                'reden': f'perceel {int(perceel_opp)}m² + woning {int(woon_opp)}m²'
            }
    elif perceel_opp >= 300 and aantal_op_perceel == 1:
        if woon_opp >= 120:
            return {
                'geschat_type': '2^1kap',
                'zekerheid_pct': 60,
                'reden': f'middelgroot perceel ({int(perceel_opp)}m²)'
            }
        else:
            return {
                'geschat_type': 'hoekwoning',
                'zekerheid_pct': 50,
                'reden': f'perceel {int(perceel_opp)}m²'
            }
    
    return None


def schat_woningtype(woning):
    """
    DEPRECATED: Gebruik detecteer_woningtype_via_straat() voor betere resultaten.
    Behouden voor backwards compatibility.
    """
    if woning.get('woningtype'):
        return None  # Energielabel = waarheid
    return _schat_woningtype_simpel(woning)


def verrijk_woningtype_schattingen(woningen, debug=False):
    """Voeg woningtype schattingen toe via straatanalyse"""
    return detecteer_woningtype_via_straat(woningen, debug=debug)

# -----------------------------
# 4. Energielabels (EP-Online)
# -----------------------------
def get_energielabel(postcode, huisnr, huisletter=None, toevoeging=None, debug=False):
    """
    Haal energielabel op via EP-Online API v5
    
    Returns dict met:
    - energieklasse: A+++, A++, A+, A, B, C, D, E, F, G
    - bouwjaar, gebouwtype, gebruiksoppervlakte, etc.
    
    Of None als geen label gevonden (404)
    """
    if not EP_ONLINE_API_KEY:
        return None
    
    postcode = str(postcode).replace(" ", "").upper()
    
    url = f"{EP_ONLINE_API}/PandEnergielabel/Adres"
    
    headers = {
        "Authorization": EP_ONLINE_API_KEY,
        "Accept": "application/json"
    }
    
    params = {
        "postcode": postcode,
        "huisnummer": huisnr
    }
    if huisletter:
        params["huisletter"] = huisletter
    if toevoeging:
        params["huisnummertoevoeging"] = toevoeging
    
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        
        if r.status_code == 200:
            data = r.json()
            if data and len(data) > 0:
                record = data[0]
                label = record.get("Energieklasse")
                if debug:
                    print(f"      ✓ {postcode}-{huisnr}: {label}")
                # Return volledige data voor eventueel later gebruik
                return {
                    'energieklasse': label,
                    'gebouwtype': record.get('Gebouwtype'),
                    'bouwjaar_label': record.get('Bouwjaar'),
                    'gebruiksoppervlakte': record.get('Gebruiksoppervlakte_thermische_zone'),
                    'registratiedatum': record.get('Registratiedatum'),
                    'geldig_tot': record.get('Geldig_tot'),
                }
        elif r.status_code == 404:
            if debug:
                print(f"      - {postcode}-{huisnr}: geen label")
            return None
        elif r.status_code == 401:
            if debug:
                print(f"      ⚠️ API key ongeldig!")
            return None
                
    except Exception as e:
        if debug:
            print(f"      Error: {e}")
    return None

def enrich_energielabels(woningen, max_requests=150, debug=False):
    """Voeg energielabels toe via EP-Online API"""
    print(f"🏷️  Energielabels ophalen (EP-Online)...")
    
    count = 0
    found = 0
    no_label = 0
    type_updated = 0
    
    for w in woningen:
        if count >= max_requests:
            break
        
        if not w.get('postcode'):
            continue
        
        result = get_energielabel(
            w['postcode'], 
            w['huisnr'], 
            w['huisletter'] if w['huisletter'] else None, 
            w['toevoeging'] if w['toevoeging'] else None,
            debug=(count < 3 and debug)
        )
        
        if result:
            w['energielabel'] = result['energieklasse']
            w['energielabel_data'] = result
            found += 1
            
            # Update woningtype met EP-Online gebouwtype (betrouwbaarder)
            ep_gebouwtype = result.get('gebouwtype')
            if ep_gebouwtype:
                # Mapping EP-Online gebouwtype naar onze types
                ep_type_lower = ep_gebouwtype.lower()
                aantal_in_pand = w.get('aantal_in_pand', 1)
                
                if 'vrijstaand' in ep_type_lower:
                    w['woningtype'] = 'vrijstaand'
                    w['bouwtype'] = 'LAAGBOUW'
                elif 'twee-onder' in ep_type_lower or '2-onder' in ep_type_lower or '2^1' in ep_type_lower:
                    w['woningtype'] = '2^1kap'
                    w['bouwtype'] = 'LAAGBOUW'
                elif 'hoek' in ep_type_lower:
                    w['woningtype'] = 'hoekwoning'
                    w['bouwtype'] = 'LAAGBOUW'
                elif 'tussen' in ep_type_lower or 'rij' in ep_type_lower:
                    w['woningtype'] = 'rijwoning'
                    w['bouwtype'] = 'LAAGBOUW'
                elif 'maisonette' in ep_type_lower:
                    w['woningtype'] = 'maisonette'
                    w['bouwtype'] = 'LAAGBOUW'
                elif 'appartement' in ep_type_lower or 'flat' in ep_type_lower or 'portiek' in ep_type_lower or 'galerij' in ep_type_lower:
                    w['woningtype'] = 'appartement'
                    # Alleen HOOGBOUW als er daadwerkelijk >3 adressen in pand zijn
                    # Anders is het waarschijnlijk onderverhuur/kamer in laagbouw
                    if aantal_in_pand > 3:
                        w['bouwtype'] = 'HOOGBOUW'
                    else:
                        w['bouwtype'] = 'LAAGBOUW'  # Onderverhuur in laagbouw
                else:
                    # Gebruik EP-Online type direct
                    w['woningtype'] = ep_gebouwtype
                
                w['woningtype_bron'] = 'EP-Online'
                type_updated += 1
        else:
            no_label += 1
        
        count += 1
        
        if count % 25 == 0:
            print(f"   ... {count} checked ({found} met label)")
            time.sleep(0.2)
    
    print(f"   ✓ {found} met label, {no_label} zonder label")
    if type_updated > 0:
        print(f"   ✓ {type_updated} woningtypes bijgewerkt met EP-Online data")
    return woningen

# -----------------------------
# 5. CBS buurtdata (WERKENDE VERSIE)
# -----------------------------
def get_cbs_buurt(buurtcode, debug=False):
    """Haal CBS kerncijfers op voor een buurt - uitgebreide versie"""
    if not buurtcode:
        return {}
    
    try:
        if debug:
            print(f"   CBS data ophalen voor {buurtcode}...")
        
        data = cbsodata.get_data('85039NED')
        
        for row in data:
            if row.get('Codering_3', '').strip() == buurtcode:
                # Huishoudens totaal voor percentage berekening
                hh_totaal = row.get('HuishoudensTotaal_28') or 1
                
                # Bereken percentages huishoudsamenstelling
                eenpersoons_abs = row.get('Eenpersoonshuishoudens_29') or 0
                zonder_kind_abs = row.get('HuishoudensZonderKinderen_30') or 0
                met_kind_abs = row.get('HuishoudensMetKinderen_31') or 0
                
                pct_eenpersoons = round(eenpersoons_abs / hh_totaal * 100) if hh_totaal else 0
                pct_zonder_kind = round(zonder_kind_abs / hh_totaal * 100) if hh_totaal else 0
                pct_met_kind = round(met_kind_abs / hh_totaal * 100) if hh_totaal else 0
                
                # Inwoners totaal voor leeftijd percentage
                inwoners = row.get('AantalInwoners_5') or 1
                
                # Leeftijdsgroepen (absolute aantallen -> percentages)
                pct_0_15 = round((row.get('k_0Tot15Jaar_8') or 0) / inwoners * 100) if inwoners else 0
                pct_15_25 = round((row.get('k_15Tot25Jaar_9') or 0) / inwoners * 100) if inwoners else 0
                pct_25_45 = round((row.get('k_25Tot45Jaar_10') or 0) / inwoners * 100) if inwoners else 0
                pct_45_65 = round((row.get('k_45Tot65Jaar_11') or 0) / inwoners * 100) if inwoners else 0
                pct_65plus = round((row.get('k_65JaarOfOuder_12') or 0) / inwoners * 100) if inwoners else 0
                
                # Migratieachtergrond (absolute -> percentage)
                pct_westers = round((row.get('WestersTotaal_17') or 0) / inwoners * 100) if inwoners else 0
                pct_niet_westers = round((row.get('NietWestersTotaal_18') or 0) / inwoners * 100) if inwoners else 0
                
                return {
                    # === BEVOLKING ===
                    'inwoners': row.get('AantalInwoners_5'),
                    'huishoudens': row.get('HuishoudensTotaal_28'),
                    'gem_huishoudgrootte': row.get('GemiddeldeHuishoudensgrootte_32'),
                    'bevolkingsdichtheid': row.get('Bevolkingsdichtheid_33'),
                    
                    # === HUISHOUDSAMENSTELLING (%) ===
                    'pct_eenpersoons': pct_eenpersoons,
                    'pct_hh_zonder_kind': pct_zonder_kind,
                    'pct_hh_met_kind': pct_met_kind,
                    
                    # === LEEFTIJDSOPBOUW (%) ===
                    'pct_0_15': pct_0_15,
                    'pct_15_25': pct_15_25,
                    'pct_25_45': pct_25_45,
                    'pct_45_65': pct_45_65,
                    'pct_65plus': pct_65plus,
                    
                    # === MIGRATIEACHTERGROND (%) ===
                    'pct_westers': pct_westers,
                    'pct_niet_westers': pct_niet_westers,
                    
                    # === WONINGEN ===
                    'woningen_totaal': row.get('Woningvoorraad_34'),
                    'pct_eengezins': row.get('PercentageEengezinswoning_36'),
                    'pct_meergezins': row.get('PercentageMeergezinswoning_37'),
                    'pct_koop': row.get('Koopwoningen_40'),
                    'pct_huur': row.get('HuurwoningenTotaal_41'),
                    'pct_huur_corporatie': row.get('InBezitWoningcorporatie_42'),
                    'pct_huur_overig': row.get('InBezitOverigeVerhuurders_43'),
                    'gem_woz_k': row.get('GemiddeldeWOZWaardeVanWoningen_35'),
                    
                    # === ENERGIE ===
                    'gem_elektra_kwh': row.get('GemiddeldeElektriciteitsleveringTotaal_47'),
                    'gem_gas_m3': row.get('GemiddeldAardgasverbruikTotaal_55'),
                    
                    # === VERVOER ===
                    'auto_per_hh': row.get('PersonenautoSPerHuishouden_103'),
                    
                    # === INKOMEN ===
                    'gem_inkomen_k': row.get('GemiddeldInkomenPerInkomensontvanger_71'),
                    'pct_laag_inkomen': row.get('HuishoudensMetEenLaagInkomen_78'),
                    
                    # === STEDELIJKHEID ===
                    'stedelijkheid': row.get('MateVanStedelijkheid_116'),
                }
                
    except Exception as e:
        if debug:
            print(f"   ⚠️ CBS fout: {e}")
    
    return {}

# -----------------------------
# 6. 3DBAG (OPTIONEEL)
# -----------------------------
def get_3dbag_pand(pand_id):
    """Haal 3DBAG data op voor één pand"""
    try:
        if not pand_id.startswith("NL.IMBAG.Pand."):
            pand_id = f"NL.IMBAG.Pand.{pand_id}"
        
        url = f"{BAG_3D_API}/{pand_id}"
        r = requests.get(url, timeout=10)
        
        if r.status_code != 200:
            return None
        
        data = r.json()
        attrs = {}
        if "feature" in data:
            attrs = data["feature"].get("attributes", {}) or data["feature"].get("properties", {})
        elif "attributes" in data:
            attrs = data["attributes"]
        elif "properties" in data:
            attrs = data["properties"]
        
        return {
            "dak_opp_m2": attrs.get("b3_opp_dak_plat") or attrs.get("b3_opp_dak_schuin") or attrs.get("b3_opp_grond"),
            "dak_orientatie": attrs.get("b3_azimut"),
            "dak_helling_gr": attrs.get("b3_hellingshoek"),
            "bouwlagen": attrs.get("b3_bouwlagen"),
            "hoogte_nok_m": attrs.get("b3_h_dak_max"),
            "hoogte_goot_m": attrs.get("b3_h_dak_min"),
        }
    except:
        return None

def enrich_3dbag(woningen, max_panden=100):
    """Voeg 3DBAG data toe"""
    print(f"🏗️  3DBAG data ophalen (max {max_panden} panden)...")
    
    pand_cache = {}
    pand_ids = list(set(w['pand_id'] for w in woningen if w['pand_id']))[:max_panden]
    
    for i, pand_id in enumerate(pand_ids):
        if i > 0 and i % 20 == 0:
            print(f"   ... {i}/{len(pand_ids)}")
            time.sleep(0.5)
        
        data = get_3dbag_pand(pand_id)
        if data:
            pand_cache[pand_id] = data
    
    found = 0
    for w in woningen:
        pand_id = w['pand_id']
        if pand_id in pand_cache:
            data = pand_cache[pand_id]
            w['dak_opp_m2'] = data.get('dak_opp_m2')
            w['dak_orientatie'] = data.get('dak_orientatie')
            w['dak_orient_naam'] = orientatie_naam(data.get('dak_orientatie'))
            w['dak_helling_gr'] = data.get('dak_helling_gr')
            w['bouwlagen'] = data.get('bouwlagen')
            w['hoogte_nok_m'] = data.get('hoogte_nok_m')
            w['hoogte_goot_m'] = data.get('hoogte_goot_m')
            found += 1
    
    print(f"   ✓ {found} panden verrijkt")
    return woningen

# -----------------------------
# 7. Tabel printen
# -----------------------------
def print_tabel(woningen, max_rows=100):
    """Print uitgebreide tabel met alle data inclusief kansen"""
    
    print("\n" + "="*235)
    print(f"{'#':>3} | {'Adres':<28} | {'PC':^7} | {'Bouw':<8} | {'Type':<10} | {'Jaar':>4} | {'Woon':>5} | {'Perc':>5} | {'#Ged':>4} | {'Eig':<4} | {'Kans':>4} | {'K%':>3} | {'F%':>3} | {'H%':>3} | {'Label':>5} | {'Afst':>4}")
    print("-"*235)
    
    for i, w in enumerate(woningen[:max_rows], 1):
        # Adres samenstellen
        adres = f"{w['straat']} {w['huisnr']}"
        if w['huisletter']:
            adres += w['huisletter']
        if w['toevoeging']:
            adres += f"-{w['toevoeging']}"
        adres = adres[:28]
        
        # Waarden formatteren
        bouwtype = w['bouwtype'][:8] if w['bouwtype'] else "-"
        wtype = w['woningtype'][:10] if w['woningtype'] else "-"
        jaar = str(w['bouwjaar']) if w['bouwjaar'] else "-"
        woon = str(int(w['woon_opp_m2'])) if w['woon_opp_m2'] else "-"
        perc = str(int(w['perceel_opp_m2'])) if w['perceel_opp_m2'] else "-"
        gedeeld = str(w.get('perceel_gedeeld_door', '')) if w.get('perceel_gedeeld_door') else "-"
        
        # Eigendom en kansen
        eigendom = w.get('eigendom', '') or "-"
        eig_kans = str(w.get('eigendom_kans', '')) if w.get('eigendom_kans') else "-"
        k_koop = str(w.get('kans_koop', '')) if w.get('kans_koop') else "-"
        k_fam = str(w.get('kans_familie_erf', '')) if w.get('kans_familie_erf') else "-"
        k_huur = str(w.get('kans_huur', '')) if w.get('kans_huur') else "-"
        
        label = w['energielabel'] or "-"
        afst = f"{int(w['afstand_m'])}"
        
        print(f"{i:>3} | {adres:<28} | {w['postcode']:^7} | {bouwtype:<8} | {wtype:<10} | {jaar:>4} | {woon:>5} | {perc:>5} | {gedeeld:>4} | {eigendom:<4} | {eig_kans:>4} | {k_koop:>3} | {k_fam:>3} | {k_huur:>3} | {label:>5} | {afst:>4}m")
    
    print("="*235)
    print("\nLegenda: Eig=KOOP/FAM/WSS/HUUR, Kans=zekerheid%, K%=koop%, F%=familie-erf%, H%=huur%")
    print("         FAM=familie-erf (2 woningen, verschillend volume), WSS=onzeker (geen duidelijke winnaar)")

def export_csv(woningen, filename="buurt_data.csv"):
    """Exporteer alle data naar CSV"""
    
    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f, delimiter=';')
        
        # Header
        writer.writerow([
            'nr', 'straat', 'huisnr', 'huisletter', 'toevoeging', 'postcode', 'plaats',
            'lat', 'lon', 'afstand_m',
            'bouwtype', 'woningtype', 'aantal_in_pand', 'bouwjaar', 
            'woon_opp_m2', 'perceel_opp_m2', 'perceelnummer', 'kadastrale_aanduiding',
            'perceel_gedeeld_door', 'eigendom', 'gevel_orientatie', 'energielabel',
            'dak_opp_m2', 'dak_orientatie_gr', 'dak_orient_naam', 'dak_helling_gr',
            'bouwlagen', 'hoogte_nok_m', 'hoogte_goot_m',
            'pand_id'
        ])
        
        for i, w in enumerate(woningen, 1):
            writer.writerow([
                i,
                w['straat'], w['huisnr'], w['huisletter'], w['toevoeging'],
                w['postcode'], w['plaats'],
                w['lat'], w['lon'], w['afstand_m'],
                w['bouwtype'], w['woningtype'], w['aantal_in_pand'], w['bouwjaar'],
                w['woon_opp_m2'], w['perceel_opp_m2'] or '', 
                w.get('perceelnummer', '') or '',
                w.get('kadastrale_aanduiding', '') or '',
                w.get('perceel_gedeeld_door', '') or '',
                w.get('eigendom', '') or '',
                w.get('gevel_orientatie', '') or '',
                w['energielabel'] or '',
                w['dak_opp_m2'] or '', 
                round(w['dak_orientatie'], 1) if w['dak_orientatie'] else '',
                w['dak_orient_naam'] or '',
                round(w['dak_helling_gr'], 1) if w['dak_helling_gr'] else '',
                w['bouwlagen'] or '', 
                round(w['hoogte_nok_m'], 2) if w['hoogte_nok_m'] else '',
                round(w['hoogte_goot_m'], 2) if w['hoogte_goot_m'] else '',
                w['pand_id']
            ])
    
    print(f"\n💾 Geëxporteerd: {filename}")
    return filename


def genereer_html_kaart(woningen, output_file="buurt_kaart.html", titel="Buurtdata Kaart", cbs_data=None, buurtcode=None, buurtnaam=None, centrum_lat=None, centrum_lon=None, straal=None):
    """
    Genereer een interactieve HTML kaart met alle woningen.
    
    - OpenStreetMap achtergrond via Leaflet
    - Gekleurde markers: KOOP=groen, FAM=blauw, WSS=geel, HUUR=oranje, onbekend=grijs
    - Centrum marker met straal cirkel
    - Klik popup met alle details
    - CBS buurtdata panel met link naar kadastrale kaart
    """
    
    if not woningen:
        print("❌ Geen woningen om te tonen")
        return None
    
    # Bereken centrum van alle punten
    lats = [w['lat'] for w in woningen if w.get('lat')]
    lons = [w['lon'] for w in woningen if w.get('lon')]
    
    if not lats or not lons:
        print("❌ Geen geldige coördinaten gevonden")
        return None
    
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)
    
    # Genereer markers JavaScript
    markers_js = []
    
    for w in woningen:
        if not w.get('lat') or not w.get('lon'):
            continue
        
        # Bepaal bouwtype icoon (SVG)
        bouwtype = w.get('bouwtype', '')
        # Bepaal hoogbouw/laagbouw icoon op basis van aantal adressen in pand
        aantal_in_pand = w.get('aantal_in_pand', 1) or 1
        is_hoogbouw = aantal_in_pand > 3
        
        # SVG iconen (wit, past bij elke achtergrondkleur)
        if is_hoogbouw:
            # Hoogbouw/flat icoon (appartementengebouw)
            svg_icon = '<svg viewBox="0 0 24 24" width="14" height="14" fill="white"><path d="M17 11V3H7v4H3v14h8v-4h2v4h8V11h-4zM7 19H5v-2h2v2zm0-4H5v-2h2v2zm0-4H5V9h2v2zm4 4H9v-2h2v2zm0-4H9V9h2v2zm0-4H9V5h2v2zm4 8h-2v-2h2v2zm0-4h-2V9h2v2zm0-4h-2V5h2v2zm4 12h-2v-2h2v2zm0-4h-2v-2h2v2z"/></svg>'
        else:
            # Laagbouw/huis icoon
            svg_icon = '<svg viewBox="0 0 24 24" width="14" height="14" fill="white"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>'
        
        # Kleur op basis van eigendom
        eigendom = w.get('eigendom', '')
        
        if eigendom == 'KOOP':
            fill_color = '#22c55e'  # groen
            border_color = '#15803d'
        elif eigendom == 'FAM':
            fill_color = '#3b82f6'  # blauw
            border_color = '#1d4ed8'
        elif eigendom == 'HUUR':
            fill_color = '#f97316'  # oranje
            border_color = '#c2410c'
        else:
            fill_color = '#eab308'  # geel (onzeker)
            border_color = '#a16207'
        
        # Adres samenstellen
        adres = f"{w.get('straat', '')} {w.get('huisnr', '')}"
        if w.get('huisletter'):
            adres += w['huisletter']
        if w.get('toevoeging'):
            adres += f"-{w['toevoeging']}"
        
        # Popup content
        popup_lines = [
            f"<b>{adres}</b>",
            f"{w.get('postcode', '')} {w.get('plaats', '')}",
            "<hr style='margin:5px 0'>",
        ]
        
        if w.get('bouwtype'):
            woningtype_display = w.get('woningtype')
            geschat_type = w.get('woningtype_geschat')
            geschat_zekerheid = w.get('woningtype_zekerheid')
            
            if woningtype_display:
                type_str = f" ({woningtype_display})"
            elif geschat_type:
                type_str = f" (<i>~{geschat_type} {geschat_zekerheid}%</i>)"
            else:
                type_str = ""
            popup_lines.append(f"<b>Type:</b> {w.get('bouwtype')}{type_str}")
        if w.get('bouwjaar'):
            popup_lines.append(f"<b>Bouwjaar:</b> {w.get('bouwjaar')}")
        if w.get('woon_opp_m2'):
            popup_lines.append(f"<b>Woonoppervlakte:</b> {int(w.get('woon_opp_m2'))} m²")
        if w.get('perceel_opp_m2'):
            popup_lines.append(f"<b>Perceel:</b> {int(w.get('perceel_opp_m2'))} m²")
        if w.get('perceelnummer'):
            popup_lines.append(f"<b>Perceelnr:</b> {w.get('perceelnummer')}")
        if w.get('perceel_gedeeld_door') and w.get('perceel_gedeeld_door') > 1:
            popup_lines.append(f"<b>Gedeeld door:</b> {w.get('perceel_gedeeld_door')} woningen")
        
        popup_lines.append("<hr style='margin:5px 0'>")
        
        if eigendom:
            eigendom_tekst = {
                'KOOP': '🟢 KOOP',
                'FAM': '🔵 FAMILIE-ERF',
                'WSS': '🟡 ONZEKER',
                'HUUR': '🟠 HUUR'
            }.get(eigendom, eigendom)
            kans = w.get('eigendom_kans', 0)
            popup_lines.append(f"<b>Eigendom:</b> {eigendom_tekst} ({kans}%)")
            
            # Kansen tonen
            k_koop = w.get('kans_koop', 0)
            k_fam = w.get('kans_familie_erf', 0)
            k_huur = w.get('kans_huur', 0)
            popup_lines.append(f"<small>Koop {k_koop}% | Fam {k_fam}% | Huur {k_huur}%</small>")
            
            if w.get('signalen'):
                popup_lines.append(f"<small><i>{', '.join(w.get('signalen', [])[:3])}</i></small>")
        
        if w.get('energielabel'):
            label = w.get('energielabel')
            label_color = {
                'A+++': '#009036', 'A++': '#009036', 'A+': '#009036', 'A': '#009036',
                'B': '#55ab26', 'C': '#c8d100', 'D': '#fff200',
                'E': '#f7a600', 'F': '#ee7203', 'G': '#e2001a'
            }.get(label, '#666')
            popup_lines.append(f"<b>Energielabel:</b> <span style='background:{label_color};color:white;padding:2px 6px;border-radius:3px;font-weight:bold'>{label}</span>")
        
        if w.get('afstand_m'):
            popup_lines.append(f"<b>Afstand:</b> {int(w.get('afstand_m'))} m")
        
        popup_content = "<br>".join(popup_lines)
        popup_content = popup_content.replace("'", "\\'").replace('"', '\\"')
        
        # DivIcon met gekleurde cirkel en SVG icoon
        marker_js = f"""
        L.marker([{w['lat']}, {w['lon']}], {{
            icon: L.divIcon({{
                className: 'custom-marker',
                html: '<div style="background:{fill_color};border:2px solid {border_color};border-radius:50%;width:22px;height:22px;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 4px rgba(0,0,0,0.3);">{svg_icon}</div>',
                iconSize: [22, 22],
                iconAnchor: [11, 11],
                popupAnchor: [0, -11]
            }})
        }}).addTo(map).bindPopup('{popup_content}');"""
        
        markers_js.append(marker_js)
    
    # Tel per categorie
    count_koop = len([w for w in woningen if w.get('eigendom') == 'KOOP'])
    count_fam = len([w for w in woningen if w.get('eigendom') == 'FAM'])
    count_huur = len([w for w in woningen if w.get('eigendom') == 'HUUR'])
    count_onbekend = len([w for w in woningen if not w.get('eigendom')])
    
    # Tel laagbouw vs hoogbouw op basis van aantal_in_pand (>3 = hoogbouw)
    count_laagbouw = len([w for w in woningen if (w.get('aantal_in_pand') or 1) <= 3])
    count_hoogbouw = len([w for w in woningen if (w.get('aantal_in_pand') or 1) > 3])
    
    # Tel laagbouw koop vs huur
    count_laagbouw_koop = len([w for w in woningen if (w.get('aantal_in_pand') or 1) <= 3 and w.get('eigendom') == 'KOOP'])
    count_laagbouw_huur = len([w for w in woningen if (w.get('aantal_in_pand') or 1) <= 3 and w.get('eigendom') == 'HUUR'])
    
    # Tel hoogbouw koop vs huur
    count_hoogbouw_koop = len([w for w in woningen if (w.get('aantal_in_pand') or 1) > 3 and w.get('eigendom') == 'KOOP'])
    count_hoogbouw_huur = len([w for w in woningen if (w.get('aantal_in_pand') or 1) > 3 and w.get('eigendom') == 'HUUR'])
    
    # Kadaster link
    kadaster_link = ""
    if buurtcode:
        kadaster_link = f'<a href="https://kadastralekaart.com/buurten/{buurtcode}" target="_blank" style="color: #3b82f6; text-decoration: none;">🔗 Bekijk op kadastralekaart.com</a>'
    
    # CBS panel HTML
    cbs_panel_html = ""
    buurt_display = buurtnaam or "Onbekend"
    if buurtcode:
        buurt_display += f" ({buurtcode})"
    
    if cbs_data:
        # Huishoudsamenstelling pie chart simulatie met bars
        pct_1p = cbs_data.get('pct_eenpersoons', 0) or 0
        pct_zk = cbs_data.get('pct_hh_zonder_kind', 0) or 0
        pct_mk = cbs_data.get('pct_hh_met_kind', 0) or 0
        
        # Leeftijdsopbouw
        pct_jong = (cbs_data.get('pct_0_15', 0) or 0) + (cbs_data.get('pct_15_25', 0) or 0)
        pct_mid = (cbs_data.get('pct_25_45', 0) or 0) + (cbs_data.get('pct_45_65', 0) or 0)
        pct_oud = cbs_data.get('pct_65plus', 0) or 0
        
        # Formatteer WOZ (CBS geeft hele euros, toon in k)
        woz_raw = cbs_data.get('gem_woz_k')
        if woz_raw and woz_raw > 1000:
            woz_display = f"€{round(woz_raw/1000)}k"
        elif woz_raw:
            woz_display = f"€{woz_raw}k"
        else:
            woz_display = "-"
        
        # Formatteer inkomen (CBS geeft in honderden: 352 = €35.200, toon als €35k)
        ink_raw = cbs_data.get('gem_inkomen_k')
        if ink_raw and ink_raw > 100:
            ink_display = f"€{round(ink_raw/10)}k"
        elif ink_raw:
            ink_display = f"€{ink_raw}k"
        else:
            ink_display = "-"
        
        cbs_panel_html = f"""
        <div class="cbs-panel">
            <h3>📊 {buurt_display}</h3>
            {kadaster_link}
            
            <div class="cbs-section" style="margin-top: 10px;">
                <b>Bevolking</b>
                <div class="cbs-row"><span>Inwoners:</span><span>{cbs_data.get('inwoners', '-')}</span></div>
                <div class="cbs-row"><span>Huishoudens:</span><span>{cbs_data.get('huishoudens', '-')}</span></div>
                <div class="cbs-row"><span>Gem. grootte:</span><span>{cbs_data.get('gem_huishoudgrootte', '-')}</span></div>
                <div class="cbs-row"><span>Dichtheid:</span><span>{cbs_data.get('bevolkingsdichtheid', '-')}/km²</span></div>
            </div>
            
            <div class="cbs-section">
                <b>Huishoudens</b>
                <div class="cbs-bar-row">
                    <span style="color:#1e3a5f">●</span> Alleenstaand: {pct_1p}%
                </div>
                <div class="cbs-bar-row">
                    <span style="color:#2d5a87">●</span> Zonder kind: {pct_zk}%
                </div>
                <div class="cbs-bar-row">
                    <span style="color:#4a90c2">●</span> Met kind: {pct_mk}%
                </div>
            </div>
            
            <div class="cbs-section">
                <b>Leeftijd</b>
                <div class="cbs-bar-row">
                    <span style="color:#4a90c2">●</span> 0-24 jaar: {pct_jong}%
                </div>
                <div class="cbs-bar-row">
                    <span style="color:#2d5a87">●</span> 25-64 jaar: {pct_mid}%
                </div>
                <div class="cbs-bar-row">
                    <span style="color:#1e3a5f">●</span> 65+ jaar: {pct_oud}%
                </div>
            </div>
            
            <div class="cbs-section">
                <b>Woningen ({cbs_data.get('woningen_totaal', '-')})</b>
                <div class="cbs-row"><span>% Eengezins:</span><span>{cbs_data.get('pct_eengezins', '-')}%</span></div>
                <div class="cbs-row"><span>% Meergezins:</span><span>{cbs_data.get('pct_meergezins', '-')}%</span></div>
                <div class="cbs-row highlight-koop"><span>% Koop:</span><span>{cbs_data.get('pct_koop', '-')}%</span></div>
                <div class="cbs-row highlight-huur"><span>% Huur:</span><span>{cbs_data.get('pct_huur', '-')}%</span></div>
                <div class="cbs-row" style="font-size:11px;color:#666"><span>&nbsp;&nbsp;↳ corporatie:</span><span>{cbs_data.get('pct_huur_corporatie', '-')}%</span></div>
                <div class="cbs-row"><span>Gem. WOZ:</span><span>{woz_display}</span></div>
            </div>
            
            <div class="cbs-section">
                <b>Energie & Vervoer</b>
                <div class="cbs-row"><span>Gem. gas:</span><span>{cbs_data.get('gem_gas_m3', '-')} m³</span></div>
                <div class="cbs-row"><span>Gem. elektra:</span><span>{cbs_data.get('gem_elektra_kwh', '-')} kWh</span></div>
                <div class="cbs-row"><span>Auto's/huish.:</span><span>{cbs_data.get('auto_per_hh', '-')}</span></div>
            </div>
            
            <div class="cbs-section">
                <b>Inkomen</b>
                <div class="cbs-row"><span>Gem. inkomen:</span><span>{ink_display}</span></div>
                <div class="cbs-row"><span>% laag inkomen:</span><span>{cbs_data.get('pct_laag_inkomen', '-')}%</span></div>
                <div class="cbs-row"><span>Stedelijkheid:</span><span>{cbs_data.get('stedelijkheid', '-')}</span></div>
            </div>
        </div>
        """
    else:
        # Alleen buurtnaam tonen als geen CBS data
        cbs_panel_html = f"""
        <div class="cbs-panel">
            <h3>📍 {buurt_display}</h3>
            {kadaster_link}
            <p style="margin-top: 10px; color: #666;">Geen CBS data beschikbaar</p>
        </div>
        """
    
    # HTML genereren
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{titel}</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
        #map {{ height: calc(100vh - 60px); width: 100%; }}
        .header {{
            background: #1e293b;
            color: white;
            padding: 12px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            height: 60px;
        }}
        .header h1 {{ font-size: 18px; font-weight: 600; }}
        .legend {{
            display: flex;
            gap: 20px;
            font-size: 14px;
        }}
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .legend-dot {{
            width: 14px;
            height: 14px;
            border-radius: 50%;
            border: 2px solid white;
        }}
        .leaflet-popup-content {{
            font-size: 13px;
            line-height: 1.5;
            min-width: 220px;
        }}
        .leaflet-popup-content b {{
            color: #1e293b;
        }}
        .leaflet-popup-content hr {{
            border: none;
            border-top: 1px solid #e2e8f0;
        }}
        .cbs-panel {{
            position: absolute;
            top: 70px;
            right: 10px;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.15);
            padding: 15px;
            z-index: 1000;
            width: 240px;
            font-size: 13px;
        }}
        .cbs-panel h3 {{
            margin-bottom: 5px;
            font-size: 14px;
            color: #1e293b;
        }}
        .cbs-section {{
            margin-bottom: 12px;
            padding-bottom: 8px;
            border-bottom: 1px solid #e2e8f0;
        }}
        .cbs-section:last-child {{
            border-bottom: none;
            margin-bottom: 0;
            padding-bottom: 0;
        }}
        .cbs-section b {{
            display: block;
            margin-bottom: 5px;
            color: #475569;
            font-size: 11px;
            text-transform: uppercase;
        }}
        .cbs-row {{
            display: flex;
            justify-content: space-between;
            padding: 2px 0;
        }}
        .cbs-row span:last-child {{
            font-weight: 600;
        }}
        .highlight-koop {{
            background: #dcfce7;
            padding: 3px 5px;
            border-radius: 4px;
            margin: 2px -5px;
        }}
        .highlight-huur {{
            background: #ffedd5;
            padding: 3px 5px;
            border-radius: 4px;
            margin: 2px -5px;
        }}
        .cbs-bar-row {{
            font-size: 12px;
            padding: 1px 0;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🏘️ {titel}</h1>
        <div class="legend">
            <div class="legend-item" title="Laagbouw KOOP">
                <div style="background:#22c55e;border:2px solid #15803d;border-radius:50%;width:20px;height:20px;display:flex;align-items:center;justify-content:center;">
                    <svg viewBox="0 0 24 24" width="12" height="12" fill="white"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>
                </div>
                <span>Laag-K ({count_laagbouw_koop})</span>
            </div>
            <div class="legend-item" title="Laagbouw HUUR">
                <div style="background:#f97316;border:2px solid #c2410c;border-radius:50%;width:20px;height:20px;display:flex;align-items:center;justify-content:center;">
                    <svg viewBox="0 0 24 24" width="12" height="12" fill="white"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>
                </div>
                <span>Laag-H ({count_laagbouw_huur})</span>
            </div>
            <div class="legend-item" title="Hoogbouw KOOP">
                <div style="background:#22c55e;border:2px solid #15803d;border-radius:50%;width:20px;height:20px;display:flex;align-items:center;justify-content:center;">
                    <svg viewBox="0 0 24 24" width="12" height="12" fill="white"><path d="M17 11V3H7v4H3v14h8v-4h2v4h8V11h-4zM7 19H5v-2h2v2zm0-4H5v-2h2v2zm0-4H5V9h2v2zm4 4H9v-2h2v2zm0-4H9V9h2v2zm0-4H9V5h2v2zm4 8h-2v-2h2v2zm0-4h-2V9h2v2zm0-4h-2V5h2v2zm4 12h-2v-2h2v2zm0-4h-2v-2h2v2z"/></svg>
                </div>
                <span>Hoog-K ({count_hoogbouw_koop})</span>
            </div>
            <div class="legend-item" title="Hoogbouw HUUR">
                <div style="background:#f97316;border:2px solid #c2410c;border-radius:50%;width:20px;height:20px;display:flex;align-items:center;justify-content:center;">
                    <svg viewBox="0 0 24 24" width="12" height="12" fill="white"><path d="M17 11V3H7v4H3v14h8v-4h2v4h8V11h-4zM7 19H5v-2h2v2zm0-4H5v-2h2v2zm0-4H5V9h2v2zm4 4H9v-2h2v2zm0-4H9V9h2v2zm0-4H9V5h2v2zm4 8h-2v-2h2v2zm0-4h-2V9h2v2zm0-4h-2V5h2v2zm4 12h-2v-2h2v2zm0-4h-2v-2h2v2z"/></svg>
                </div>
                <span>Hoog-H ({count_hoogbouw_huur})</span>
            </div>
            <div class="legend-item" title="Familie-erf">
                <div style="background:#3b82f6;border:2px solid #1d4ed8;border-radius:50%;width:20px;height:20px;display:flex;align-items:center;justify-content:center;">
                    <svg viewBox="0 0 24 24" width="12" height="12" fill="white"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg>
                </div>
                <span>FAM ({count_fam})</span>
            </div>
            <div class="legend-item" style="margin-left: 10px; font-weight: 600;">
                Koop: {count_koop} | Huur: {count_huur} | Tot: {len(woningen)}
            </div>
        </div>
    </div>
    <div id="map"></div>
    {cbs_panel_html}
    
    <script>
        var map = L.map('map').setView([{center_lat}, {center_lon}], 17);
        
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            maxZoom: 19,
            attribution: '© OpenStreetMap contributors'
        }}).addTo(map);
        
        // Centrum marker en straal cirkel
        {f'''
        L.marker([{centrum_lat}, {centrum_lon}], {{
            icon: L.divIcon({{
                className: 'centrum-marker',
                html: '<div style="background:#dc2626;width:16px;height:16px;border-radius:50%;border:3px solid white;box-shadow:0 2px 5px rgba(0,0,0,0.3);"></div>',
                iconSize: [16, 16],
                iconAnchor: [8, 8]
            }})
        }}).addTo(map).bindPopup('<b>📍 Centrum</b><br>{titel.split(",")[0] if "," in titel else titel}');
        
        L.circle([{centrum_lat}, {centrum_lon}], {{
            radius: {straal},
            color: '#dc2626',
            weight: 2,
            opacity: 0.8,
            fillColor: '#dc2626',
            fillOpacity: 0.05,
            dashArray: '5, 5'
        }}).addTo(map);
        ''' if centrum_lat and centrum_lon and straal else ''}
        
        // Markers
        {''.join(markers_js)}
    </script>
</body>
</html>"""
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)
    
    print(f"🗺️  Kaart gegenereerd: {output_file}")
    print(f"   {count_koop} KOOP | {count_fam} FAM | {count_huur} HUUR | {count_onbekend} onbekend")
    print(f"   {count_laagbouw} laagbouw (🏠) | {count_hoogbouw} hoogbouw (🏢)")
    
    return output_file

# -----------------------------
# MAIN
# -----------------------------
def verzamel_data(straat, huisnr, plaats, straal=200, debug=False):
    """Verzamel basis data + perceel + energielabel (zonder 3DBAG)"""
    
    print("\n" + "="*60)
    print("🏘️  BUURT DATA VERZAMELAAR")
    print("="*60)
    print(f"📍 Centrum: {straat} {huisnr}, {plaats}")
    print(f"📏 Straal: {straal}m")
    print("="*60 + "\n")
    
    total_start = time.time()
    
    # 1. Centrum locatie
    start = time.time()
    cx, cy = get_rd_from_address(straat, huisnr, plaats)
    buurtcode, buurtnaam = get_buurtcode(cx, cy)
    
    # Converteer RD naar lat/lon voor kaart
    from pyproj import Transformer
    transformer = Transformer.from_crs("EPSG:28992", "EPSG:4326", always_xy=True)
    centrum_lon, centrum_lat = transformer.transform(cx, cy)
    
    print(f"📍 Buurt: {buurtnaam} ({buurtcode}) [{time.time()-start:.1f}s]")
    
    # 2. BAG woningen
    start = time.time()
    woningen = get_bag_woningen(cx, cy, straal)
    print(f"   ⏱️  BAG ophalen: {time.time()-start:.1f}s")
    
    if not woningen:
        print("❌ Geen woningen gevonden")
        return [], buurtcode, buurtnaam, {}, centrum_lat, centrum_lon
    
    # 3. Perceeldata (alleen laagbouw)
    start = time.time()
    woningen = enrich_percelen(woningen, max_requests=500, debug=debug)
    print(f"   ⏱️  Percelen ophalen: {time.time()-start:.1f}s")
    
    # 4. Energielabels
    start = time.time()
    woningen = enrich_energielabels(woningen, max_requests=500, debug=debug)
    print(f"   ⏱️  Energielabels ophalen: {time.time()-start:.1f}s")
    
    # 5. CBS buurtdata
    start = time.time()
    cbs = get_cbs_buurt(buurtcode, debug=debug)
    print(f"   ⏱️  CBS data ophalen: {time.time()-start:.1f}s")
    
    if cbs:
        print(f"\n📊 CBS Buurtdata ({buurtnaam}):")
        print("   --- Bevolking ---")
        if cbs.get('inwoners'):
            print(f"   Inwoners: {cbs.get('inwoners')}")
        if cbs.get('huishoudens'):
            print(f"   Huishoudens: {cbs.get('huishoudens')}")
        if cbs.get('gem_huishoudgrootte'):
            print(f"   Gem. huishoudgrootte: {cbs.get('gem_huishoudgrootte')}")
        if cbs.get('bevolkingsdichtheid'):
            print(f"   Bevolkingsdichtheid: {cbs.get('bevolkingsdichtheid')} per km²")
        
        print("   --- Woningen ---")
        if cbs.get('pct_eengezins'):
            print(f"   % Eengezins: {cbs.get('pct_eengezins')}%")
        if cbs.get('pct_meergezins'):
            print(f"   % Meergezins: {cbs.get('pct_meergezins')}%")
        if cbs.get('pct_koop'):
            print(f"   % Koop: {cbs.get('pct_koop')}%")
        if cbs.get('pct_huur'):
            print(f"   % Huur: {cbs.get('pct_huur')}%")
        if cbs.get('gem_woz_k'):
            print(f"   Gem. WOZ: €{cbs.get('gem_woz_k')}k")
        
        print("   --- Inkomen ---")
        if cbs.get('gem_inkomen_k'):
            print(f"   Gem. inkomen: €{cbs.get('gem_inkomen_k')}k")
        
        if cbs.get('stedelijkheid'):
            print(f"   Stedelijkheid: {cbs.get('stedelijkheid')}")
    else:
        print(f"\n⚠️ Geen CBS data gevonden voor buurt {buurtcode}")
        cbs = {}
    
    # 6. Eigendom detectie (na CBS data zodat we huur% kunnen gebruiken)
    start = time.time()
    woningen = detecteer_huur_gedeeld_perceel(woningen, cbs_data=cbs)
    print(f"   ⏱️  Eigendom detectie: {time.time()-start:.1f}s")
    
    # 7. Woningtype schatting voor woningen zonder energielabel
    start = time.time()
    woningen = verrijk_woningtype_schattingen(woningen)
    print(f"   ⏱️  Woningtype schatting: {time.time()-start:.1f}s")
    
    print(f"\n⏱️  TOTALE TIJD: {time.time()-total_start:.1f}s")
    
    return woningen, buurtcode, buurtnaam, cbs, centrum_lat, centrum_lon


if __name__ == "__main__":
    
    # === CONFIGURATIE ===
    STRAAT = "Waltersingel"
    HUISNR = "83"
    PLAATS = "Apeldoorn"
    STRAAL = 200
    DEBUG = True  # Zet op True voor debug output
    
    # === STAP 1: DATA OPHALEN (basis + perceel + energielabel) ===
    woningen, buurtcode, buurtnaam, cbs_data, centrum_lat, centrum_lon = verzamel_data(STRAAT, HUISNR, PLAATS, STRAAL, debug=DEBUG)
    
    if not woningen:
        exit()
    
    # === STAP 2: TABEL TONEN ===
    print_tabel(woningen, max_rows=100)
    
    # === STAP 3: STATISTIEKEN ===
    laagbouw = [w for w in woningen if w['bouwtype'] == 'LAAGBOUW']
    hoogbouw = [w for w in woningen if w['bouwtype'] == 'HOOGBOUW']
    met_perceel = len([w for w in woningen if w['perceel_opp_m2']])
    met_label = len([w for w in woningen if w['energielabel']])
    
    print(f"\n📈 Samenvatting:")
    print(f"   Totaal: {len(woningen)} woningen")
    print(f"   - Laagbouw: {len(laagbouw)}")
    print(f"   - Hoogbouw: {len(hoogbouw)}")
    print(f"   Met perceeldata: {met_perceel}")
    print(f"   Met energielabel: {met_label}")
    
    # === STAP 4: 3DBAG VERRIJKING? ===
    print("\n" + "="*60)
    print("🏗️  OPTIONEEL: 3DBAG VERRIJKING")
    print("="*60)
    print("""
Wil je ook 3DBAG data ophalen?
Dit voegt toe: dakoppervlak, oriëntatie, helling, bouwlagen, hoogte

Nuttig voor: zonnepanelen, dakdekkers, schilders, glazenwassers

⚠️  Dit duurt 1-2 minuten extra (per pand 1 API call)

[J] Ja, ophalen
[N] Nee, exporteer zonder 3DBAG
""")
    
    keuze = input("Keuze (J/N): ").strip().upper()
    
    if keuze == 'J':
        woningen = enrich_3dbag(woningen, max_panden=100)
        print_tabel(woningen, max_rows=100)
        
        met_dak = len([w for w in woningen if w['dak_opp_m2']])
        print(f"\n   Met 3DBAG dakdata: {met_dak}")
    
    # === STAP 5: EXPORT ===
    filename = export_csv(woningen, "buurt_data.csv")
    
    # === STAP 6: KAART GENEREREN ===
    kaart_titel = f"Buurtdata {STRAAT} {HUISNR}, {PLAATS} ({STRAAL}m)"
    genereer_html_kaart(woningen, "buurt_kaart.html", kaart_titel, 
                        cbs_data=cbs_data, buurtcode=buurtcode, buurtnaam=buurtnaam,
                        centrum_lat=centrum_lat, centrum_lon=centrum_lon, straal=STRAAL)
    
    print(f"\n✅ Klaar!")
    print(f"   📊 CSV data: buurt_data.csv")
    print(f"   🗺️  Kaart: buurt_kaart.html (open in browser)")