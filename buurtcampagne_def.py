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
    """
    try:
        buffer = 0.1
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
        
        if debug:
            print(f"      Perceel: {perceel_nr}, opp: {opp}m²")
        
        return {
            'perceelnummer': perceel_nr,
            'oppervlakte': opp,
            'geometry': geom
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
    """Voeg perceeldata toe - alleen voor LAAGBOUW"""
    print(f"📐 Perceeldata ophalen...")
    
    # Filter alleen laagbouw
    laagbouw = [w for w in woningen if w['bouwtype'] == 'LAAGBOUW']
    print(f"   {len(laagbouw)} laagbouw woningen om te checken")
    
    count = 0
    found = 0
    errors = 0
    
    for w in laagbouw:
        if count >= max_requests:
            break
        
        # Haal volledige perceeldata op (inclusief perceelnummer)
        perceel_data = get_perceel_data(w['rd_x'], w['rd_y'], debug=(count < 3 and debug))
        
        if perceel_data:
            try:
                w['perceel_opp_m2'] = int(float(perceel_data['oppervlakte'])) if perceel_data['oppervlakte'] else None
                w['perceelnummer'] = perceel_data['perceelnummer']
                w['kadastrale_aanduiding'] = perceel_data['kadastraleAanduiding']
                found += 1
            except:
                errors += 1
        else:
            errors += 1
        
        count += 1
        
        # Progress
        if count % 25 == 0:
            print(f"   ... {count}/{len(laagbouw)} ({found} gevonden)")
            time.sleep(0.2)
    
    print(f"   ✓ {found} percelen gevonden, {errors} niet gevonden")
    
    # HUUR DETECTIE: check voor gedeelde percelen (via perceelnummer)
    woningen = detecteer_huur_gedeeld_perceel(woningen)
    
    return woningen


def detecteer_huur_gedeeld_perceel(woningen):
    """
    Detecteer woningcorporatie woningen op basis van gedeeld perceel.
    
    Logica (alleen voor LAAGBOUW):
    1. Groepeer op PERCEELNUMMER (kadaster ID)
    2. Als meerdere adressen hetzelfde perceelnummer delen → CORPORATIE (100% zeker)
    3. Eigen perceelnummer → PARTICULIER (koop of particuliere huur)
    
    Markeert woningen met:
    - eigendom: "CORPORATIE" of "PARTICULIER"
    - perceel_gedeeld_door: aantal adressen op zelfde perceel
    """
    print(f"🏢 Corporatie/particulier detectie via perceelnummer...")
    
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
    
    # Markeer woningen op gedeelde percelen
    corporatie_count = 0
    particulier_count = 0
    
    for perceel_nr, groep in perceel_groepen.items():
        aantal = len(groep)
        
        for w in groep:
            w['perceel_gedeeld_door'] = aantal
            
            # Meerdere laagbouw woningen op 1 perceelnummer = CORPORATIE (100% zeker)
            if aantal >= 2:
                w['eigendom'] = "CORPORATIE"
                corporatie_count += 1
            else:
                w['eigendom'] = "PARTICULIER"
                particulier_count += 1
    
    # Laagbouw zonder perceelnummer
    for w in woningen:
        if w.get('bouwtype') == 'LAAGBOUW' and 'eigendom' not in w:
            w['eigendom'] = None
            w['perceel_gedeeld_door'] = None
    
    # Hoogbouw = altijd onbekend (geen perceel info)
    for w in woningen:
        if w.get('bouwtype') == 'HOOGBOUW':
            w['eigendom'] = None
            w['perceel_gedeeld_door'] = None
    
    onbekend = len([w for w in woningen if w.get('eigendom') is None])
    
    print(f"   ✓ Corporatie (gedeeld perceelnummer): {corporatie_count}")
    print(f"   ✓ Particulier (eigen perceelnummer): {particulier_count}")
    print(f"   ✓ Onbekend (hoogbouw/geen data): {onbekend}")
    
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
    
    print("\n" + "="*210)
    print(f"{'#':>3} | {'Adres':<28} | {'PC':^7} | {'Bouw':<8} | {'Type':<10} | {'Jaar':>4} | {'Woon':>5} | {'Perc':>5} | {'PercNr':>6} | {'#Ged':>4} | {'Eig':<5} | {'Label':>5} | {'Dak':>5} | {'Or':>3} | {'Afst':>4}")
    print("-"*210)
    
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
        # Kort label: CORP of PART
        eig = w.get('eigendom', '')
        if eig == "CORPORATIE":
            eigendom = "CORP"
        elif eig == "PARTICULIER":
            eigendom = "PART"
        else:
            eigendom = "-"
        label = w['energielabel'] or "-"
        dak = str(int(w['dak_opp_m2'])) if w['dak_opp_m2'] else "-"
        orient = w['dak_orient_naam'] or "-"
        afst = f"{int(w['afstand_m'])}"
        
        print(f"{i:>3} | {adres:<28} | {w['postcode']:^7} | {bouwtype:<8} | {wtype:<10} | {jaar:>4} | {woon:>5} | {perc:>5} | {perc_nr:>6} | {gedeeld:>4} | {eigendom:<5} | {label:>5} | {dak:>5} | {orient:>3} | {afst:>4}m")
    
    print("="*210)
    print("\nLegenda: Bouw=LAAGBOUW/HOOGBOUW, Woon=woonoppervlakte(m²), Perc=perceeloppervlakte(m²),")
    print("         PercNr=kadaster perceelnummer, #Ged=aantal woningen op zelfde perceel,")
    print("         Eig=CORP(oratie)/PART(iculier), Label=energielabel, Dak=dakoppervlakte(m²), Or=oriëntatie")

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
            'perceel_gedeeld_door', 'eigendom', 'energielabel',
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
    woningen = enrich_percelen(woningen, max_requests=150, debug=debug)
    
    # 4. Energielabels
    woningen = enrich_energielabels(woningen, max_requests=150, debug=debug)
    
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
    
    return woningen, buurtcode, cbs


if __name__ == "__main__":
    
    # === CONFIGURATIE ===
    STRAAT = "Rijnlanderlaan"
    HUISNR = "28"
    PLAATS = "Barneveld"
    STRAAL = 100
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
    print(f"\n✅ Klaar! Data staat in: {filename}")