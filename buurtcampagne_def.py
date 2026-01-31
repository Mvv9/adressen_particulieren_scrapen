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
        
        # Woningtype classificatie - alleen SCHATTING op basis van aantal
        # Definitief type wordt pas ingevuld bij EP-Online energielabel
        if aantal_in_pand <= 6:
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


def detecteer_huur_gedeeld_perceel(woningen, cbs_data=None):
    """
    Detecteer eigendom op basis van perceel en CBS data.
    
    Logica:
    1. LAAGBOUW + gedeeld perceel (≥2 woningen) → HUUR (corporatie)
    2. LAAGBOUW + eigen perceel (1 woning) → KOOP
    3. HOOGBOUW + CBS huur% > 50% → WSS (waarschijnlijk huur)
    4. Anders → leeg
    
    Markeert woningen met:
    - eigendom: "HUUR", "KOOP", "WSS" of None
    - eigendom_toelichting: uitleg waarom
    - perceel_gedeeld_door: aantal adressen op zelfde perceel
    """
    print(f"🏢 Eigendom detectie...")
    
    # CBS huur percentage ophalen
    cbs_huur_pct = None
    if cbs_data:
        cbs_huur_pct = cbs_data.get('pct_huur')
        if cbs_huur_pct:
            print(f"   CBS huur%: {cbs_huur_pct}%")
    
    # Groepeer LAAGBOUW per perceelnummer
    from collections import defaultdict
    perceel_groepen = defaultdict(list)
    
    for w in woningen:
        # Alleen laagbouw analyseren
        if w.get('bouwtype') != 'LAAGBOUW':
            continue
            
        perceel_nr = w.get('perceelnummer')
        if not perceel_nr:
            continue
        
        perceel_groepen[perceel_nr].append(w)
    
    # Markeer LAAGBOUW woningen
    huur_count = 0
    koop_count = 0
    wss_count = 0
    
    for perceel_nr, groep in perceel_groepen.items():
        aantal = len(groep)
        
        for w in groep:
            w['perceel_gedeeld_door'] = aantal
            
            # Meerdere woningen op 1 perceelnummer = HUUR (corporatie)
            # Dit is de simpele regel: gedeeld perceel = sociale huur
            if aantal >= 2:
                w['eigendom'] = "HUUR"
                w['eigendom_toelichting'] = f"gedeeld perceel ({aantal} woningen)"
                huur_count += 1
            else:
                # Eigen perceel = KOOP
                w['eigendom'] = "KOOP"
                w['eigendom_toelichting'] = "eigen perceel"
                koop_count += 1
    
    # Laagbouw zonder perceelnummer
    for w in woningen:
        if w.get('bouwtype') == 'LAAGBOUW' and 'eigendom' not in w:
            w['eigendom'] = None
            w['eigendom_toelichting'] = None
            w['perceel_gedeeld_door'] = None
    
    # HOOGBOUW: check CBS huur percentage
    for w in woningen:
        if w.get('bouwtype') == 'HOOGBOUW':
            w['perceel_gedeeld_door'] = None
            
            if cbs_huur_pct and cbs_huur_pct > 50:
                w['eigendom'] = "WSS"
                w['eigendom_toelichting'] = f"hoogbouw + {cbs_huur_pct}% huur in buurt"
                wss_count += 1
            else:
                w['eigendom'] = None
                w['eigendom_toelichting'] = None
    
    onbekend = len([w for w in woningen if w.get('eigendom') is None])
    
    print(f"   ✓ HUUR (gedeeld perceel ≥2 woningen): {huur_count}")
    print(f"   ✓ KOOP (eigen perceel): {koop_count}")
    if wss_count > 0:
        print(f"   ✓ WSS (hoogbouw + >{50}% huur in buurt): {wss_count}")
    print(f"   ✓ Onbekend: {onbekend}")
    
    return woningen

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
                    w['bouwtype'] = 'HOOGBOUW'
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
    """Haal CBS kerncijfers op voor een buurt"""
    if not buurtcode:
        return {}
    
    try:
        if debug:
            print(f"   CBS data ophalen voor {buurtcode}...")
        
        data = cbsodata.get_data('85039NED')
        
        for row in data:
            if row.get('Codering_3', '').strip() == buurtcode:
                return {
                    # Bevolking
                    'inwoners': row.get('AantalInwoners_5'),
                    'huishoudens': row.get('HuishoudensTotaal_28'),
                    'gem_huishoudgrootte': row.get('GemiddeldeHuishoudensgrootte_32'),
                    'bevolkingsdichtheid': row.get('Bevolkingsdichtheid_33'),
                    
                    # Woningen
                    'pct_eengezins': row.get('PercentageEengezinswoning_36'),
                    'pct_meergezins': row.get('PercentageMeergezinswoning_37'),
                    'pct_koop': row.get('Koopwoningen_40'),
                    'pct_huur': row.get('HuurwoningenTotaal_41'),
                    'gem_woz_k': row.get('GemiddeldeWOZWaardeVanWoningen_35'),
                    
                    # Inkomen
                    'gem_inkomen_k': row.get('GemiddeldInkomenPerInkomensontvanger_71'),
                    
                    # Stedelijkheid
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
    """Print uitgebreide tabel met alle data"""
    
    print("\n" + "="*220)
    print(f"{'#':>3} | {'Adres':<28} | {'PC':^7} | {'Bouw':<8} | {'Type':<10} | {'Jaar':>4} | {'Woon':>5} | {'Perc':>5} | {'PercNr':>6} | {'#Ged':>4} | {'Eig':<4} | {'Gevel':>5} | {'Label':>5} | {'Dak':>5} | {'Or':>3} | {'Afst':>4}")
    print("-"*220)
    
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
        perc_nr = str(w.get('perceelnummer', '')) if w.get('perceelnummer') else "-"
        gedeeld = str(w.get('perceel_gedeeld_door', '')) if w.get('perceel_gedeeld_door') else "-"
        # Eigendom: HUUR, KOOP, of WSS (waarschijnlijk huur)
        eigendom = w.get('eigendom', '') or "-"
        # Geveloriëntatie
        gevel = w.get('gevel_orientatie') or "-"
        label = w['energielabel'] or "-"
        dak = str(int(w['dak_opp_m2'])) if w['dak_opp_m2'] else "-"
        orient = w['dak_orient_naam'] or "-"
        afst = f"{int(w['afstand_m'])}"
        
        print(f"{i:>3} | {adres:<28} | {w['postcode']:^7} | {bouwtype:<8} | {wtype:<10} | {jaar:>4} | {woon:>5} | {perc:>5} | {perc_nr:>6} | {gedeeld:>4} | {eigendom:<4} | {gevel:>5} | {label:>5} | {dak:>5} | {orient:>3} | {afst:>4}m")
    
    print("="*220)
    print("\nLegenda: Bouw=LAAGBOUW/HOOGBOUW, Woon=woonoppervlakte(m²), Perc=perceeloppervlakte(m²),")
    print("         PercNr=kadaster perceelnummer, #Ged=aantal woningen op zelfde perceel,")
    print("         Eig=HUUR/KOOP/WSS(hoogbouw+huurbuurt), Gevel=voorgevel oriëntatie, Label=energielabel, Dak=dakoppervlakte(m²), Or=dak oriëntatie")

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


def genereer_html_kaart(woningen, output_file="buurt_kaart.html", titel="Buurtdata Kaart"):
    """
    Genereer een interactieve HTML kaart met alle woningen.
    
    - OpenStreetMap achtergrond via Leaflet
    - Gekleurde markers: KOOP=groen, WSS=geel, HUUR=oranje, onbekend=grijs
    - Klik popup met alle details
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
        
        # Kleur op basis van eigendom
        eigendom = w.get('eigendom', '')
        if eigendom == 'KOOP':
            fill_color = '#22c55e'  # groen
        elif eigendom == 'WSS':
            fill_color = '#eab308'  # geel
        elif eigendom == 'HUUR':
            fill_color = '#f97316'  # oranje
        else:
            fill_color = '#9ca3af'  # grijs
        
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
            woningtype = w.get('woningtype')
            type_str = f" ({woningtype})" if woningtype else ""
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
                'WSS': '🟡 WSS (wss huur)',
                'HUUR': '🟠 HUUR'
            }.get(eigendom, eigendom)
            popup_lines.append(f"<b>Eigendom:</b> {eigendom_tekst}")
            if w.get('eigendom_toelichting'):
                popup_lines.append(f"<small><i>{w.get('eigendom_toelichting')}</i></small>")
        
        if w.get('gevel_orientatie'):
            popup_lines.append(f"<b>Voorgevel:</b> {w.get('gevel_orientatie')}")
        
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
        
        marker_js = f"""
        L.circleMarker([{w['lat']}, {w['lon']}], {{
            radius: 8,
            fillColor: '{fill_color}',
            color: '#fff',
            weight: 2,
            opacity: 1,
            fillOpacity: 0.8
        }}).addTo(map).bindPopup('{popup_content}');"""
        
        markers_js.append(marker_js)
    
    # Tel per categorie
    count_koop = len([w for w in woningen if w.get('eigendom') == 'KOOP'])
    count_wss = len([w for w in woningen if w.get('eigendom') == 'WSS'])
    count_huur = len([w for w in woningen if w.get('eigendom') == 'HUUR'])
    count_onbekend = len([w for w in woningen if not w.get('eigendom')])
    
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
            min-width: 200px;
        }}
        .leaflet-popup-content b {{
            color: #1e293b;
        }}
        .leaflet-popup-content hr {{
            border: none;
            border-top: 1px solid #e2e8f0;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🏘️ {titel}</h1>
        <div class="legend">
            <div class="legend-item">
                <div class="legend-dot" style="background: #22c55e;"></div>
                <span>KOOP ({count_koop})</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background: #eab308;"></div>
                <span>WSS ({count_wss})</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background: #f97316;"></div>
                <span>HUUR ({count_huur})</span>
            </div>
            <div class="legend-item">
                <div class="legend-dot" style="background: #9ca3af;"></div>
                <span>Onbekend ({count_onbekend})</span>
            </div>
            <div class="legend-item" style="margin-left: 20px; font-weight: 600;">
                Totaal: {len(woningen)}
            </div>
        </div>
    </div>
    <div id="map"></div>
    
    <script>
        var map = L.map('map').setView([{center_lat}, {center_lon}], 17);
        
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            maxZoom: 19,
            attribution: '© OpenStreetMap contributors'
        }}).addTo(map);
        
        // Markers
        {''.join(markers_js)}
    </script>
</body>
</html>"""
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)
    
    print(f"🗺️  Kaart gegenereerd: {output_file}")
    print(f"   {count_koop} KOOP (groen) | {count_wss} WSS (geel) | {count_huur} HUUR (oranje) | {count_onbekend} onbekend (grijs)")
    
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
    
    # 1. Centrum locatie
    cx, cy = get_rd_from_address(straat, huisnr, plaats)
    buurtcode, buurtnaam = get_buurtcode(cx, cy)
    print(f"📍 Buurt: {buurtnaam} ({buurtcode})")
    
    # 2. BAG woningen
    woningen = get_bag_woningen(cx, cy, straal)
    
    if not woningen:
        print("❌ Geen woningen gevonden")
        return [], buurtcode
    
    # 3. Perceeldata (alleen laagbouw)
    woningen = enrich_percelen(woningen, max_requests=500, debug=debug)
    
    # 4. Energielabels
    woningen = enrich_energielabels(woningen, max_requests=500, debug=debug)
    
    # 5. CBS buurtdata
    cbs = get_cbs_buurt(buurtcode, debug=debug)
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
    woningen = detecteer_huur_gedeeld_perceel(woningen, cbs_data=cbs)
    
    return woningen, buurtcode, cbs


if __name__ == "__main__":
    
    # === CONFIGURATIE ===
    STRAAT = "Oldenbarnevelderweg"
    HUISNR = "111"
    PLAATS = "Barneveld"
    STRAAL = 200
    DEBUG = True  # Zet op True voor debug output
    
    # === STAP 1: DATA OPHALEN (basis + perceel + energielabel) ===
    woningen, buurtcode, cbs_data = verzamel_data(STRAAT, HUISNR, PLAATS, STRAAL, debug=DEBUG)
    
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
    genereer_html_kaart(woningen, "buurt_kaart.html", kaart_titel)
    
    print(f"\n✅ Klaar!")
    print(f"   📊 CSV data: buurt_data.csv")
    print(f"   🗺️  Kaart: buurt_kaart.html (open in browser)")