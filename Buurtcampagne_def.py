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
import json

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
    if not geometry:
        return None
    coords = geometry.get("coordinates", [])
    if geometry.get("type") == "Polygon":
        coords = coords[0]
    elif geometry.get("type") == "MultiPolygon":
        coords = coords[0][0]
    if len(coords) < 4:
        return None
    n = len(coords) - 1
    centrum_x = sum(c[0] for c in coords[:-1]) / n
    centrum_y = sum(c[1] for c in coords[:-1]) / n
    afstand_adres_centrum = math.sqrt((adres_x - centrum_x)**2 + (adres_y - centrum_y)**2)
    if afstand_adres_centrum < 1.0:
        return {'gevel_richting': None, 'betrouwbaar': False, 'reden': 'adres=centrum'}
    dx = adres_x - centrum_x
    dy = adres_y - centrum_y
    straat_hoek = math.degrees(math.atan2(dx, dy))
    if straat_hoek < 0:
        straat_hoek += 360
    zijdes = []
    for i in range(len(coords) - 1):
        p1 = coords[i]
        p2 = coords[i + 1]
        lengte = math.sqrt((p2[0]-p1[0])**2 + (p2[1]-p1[1])**2)
        midden = ((p1[0]+p2[0])/2, (p1[1]+p2[1])/2)
        dx_z = midden[0] - centrum_x
        dy_z = midden[1] - centrum_y
        zijde_richting = math.degrees(math.atan2(dx_z, dy_z))
        if zijde_richting < 0:
            zijde_richting += 360
        hoek_zijde = math.degrees(math.atan2(p2[0]-p1[0], p2[1]-p1[1]))
        if hoek_zijde < 0:
            hoek_zijde += 360
        hoek_verschil = abs(zijde_richting - straat_hoek)
        if hoek_verschil > 180:
            hoek_verschil = 360 - hoek_verschil
        zijdes.append({"lengte": lengte, "hoek_zijde": hoek_zijde, "hoek_verschil": hoek_verschil})
    zijdes_sorted = sorted(zijdes, key=lambda z: z["lengte"])
    kandidaten = zijdes_sorted[:2]
    voorgevel = min(kandidaten, key=lambda z: z["hoek_verschil"])
    gevel_hoek = (voorgevel["hoek_zijde"] + 90) % 360
    gevel_check = abs(gevel_hoek - straat_hoek)
    if gevel_check > 180:
        gevel_check = 360 - gevel_check
    if gevel_check > 90:
        gevel_hoek = (gevel_hoek + 180) % 360
    gevel_richting = hoek_naar_richting(gevel_hoek)
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
    url = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse"
    params = {"X": x, "Y": y, "type": "buurt", "rows": 1, "fl": "buurtcode,buurtnaam,wijkcode,gemeentecode"}
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

    # WFS paginering: haal ALLE features op (default max = 1000 per pagina)
    PAGE_SIZE = 1000
    features = []
    start_index = 0
    while True:
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeName": "bag:verblijfsobject", "outputFormat": "application/json",
            "bbox": f"{minx},{miny},{maxx},{maxy},EPSG:28992",
            "count": str(PAGE_SIZE),
            "startIndex": str(start_index),
        }
        r = requests.get(BAG_WFS, params=params)
        r.raise_for_status()
        page = r.json().get("features", [])
        features.extend(page)
        if len(page) < PAGE_SIZE:
            break  # laatste pagina
        start_index += PAGE_SIZE
        print(f"   ... {len(features)} features opgehaald, volgende pagina...")

    if len(features) > PAGE_SIZE:
        print(f"   ℹ️  Totaal {len(features)} features opgehaald via paginering")
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
        x, y = geom["coordinates"]
        dist = afstand(center_x, center_y, x, y)
        if dist > straal:
            continue
        gebruiksdoel = props.get("gebruiksdoel", "")
        if "woonfunctie" not in gebruiksdoel.lower():
            continue
        pand_id = props.get("pandidentificatie")
        aantal_in_pand = len(panden.get(pand_id, []))
        if aantal_in_pand <= 3:
            bouwtype = "LAAGBOUW"
        else:
            bouwtype = "HOOGBOUW"
        woningtype = None
        lat, lon = rd_to_latlon(x, y)
        postcode = props.get("postcode", "")
        if postcode:
            postcode = postcode.replace(" ", "").upper()
        woningen.append({
            "straat": props.get("openbare_ruimte"),
            "huisnr": props.get("huisnummer"),
            "huisletter": props.get("huisletter") or "",
            "toevoeging": props.get("huisnummertoevoeging") or "",
            "postcode": postcode,
            "plaats": props.get("woonplaats"),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "rd_x": round(x, 1),
            "rd_y": round(y, 1),
            "afstand_m": round(dist, 0),
            "bouwjaar": props.get("bouwjaar"),
            "woon_opp_m2": props.get("oppervlakte"),
            "bouwtype": bouwtype,
            "woningtype": woningtype,
            "aantal_in_pand": aantal_in_pand,
            "pand_id": pand_id,
            "perceel_opp_m2": None,
            "energielabel": None,
            "dak_opp_m2": None,
            "dak_orientatie": None,
            "dak_orient_naam": None,
            "dak_helling_gr": None,
            "bouwlagen": None,
            "hoogte_nok_m": None,
            "hoogte_goot_m": None,
        })
    woningen.sort(key=lambda x: x['afstand_m'])
    laagbouw = len([w for w in woningen if w['bouwtype'] == 'LAAGBOUW'])
    hoogbouw = len([w for w in woningen if w['bouwtype'] == 'HOOGBOUW'])
    print(f"   ✓ {len(woningen)} woningen gevonden (~{laagbouw} laagbouw, ~{hoogbouw} hoogbouw)")
    return woningen

# -----------------------------
# 3. Kadaster percelen
# -----------------------------
def get_perceel_data(x, y, debug=False):
    try:
        buffer = 0.1
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeName": "kadastralekaart:Perceel", "outputFormat": "application/json",
            "bbox": f"{x-buffer},{y-buffer},{x+buffer},{y+buffer},EPSG:28992",
            "count": "1"
        }
        r = requests.get(KADASTER_WFS, params=params, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        features = data.get("features", [])
        if not features:
            return None
        props = features[0].get("properties", {})
        geom = features[0].get("geometry")
        return {
            'perceelnummer': props.get("perceelnummer"),
            'oppervlakte': props.get("kadastraleGrootteWaarde"),
            'geometry': geom,
            'kadastraleAanduiding': props.get("kadastraleAanduiding")
        }
    except:
        return None

def get_perceel_opp(x, y, debug=False):
    data = get_perceel_data(x, y, debug)
    if data:
        return data.get('oppervlakte')
    return None

def enrich_percelen(woningen, max_requests=150, debug=False):
    print(f"📐 Perceeldata + geveloriëntatie ophalen...")
    laagbouw = [w for w in woningen if w['bouwtype'] == 'LAAGBOUW']
    print(f"   {len(laagbouw)} laagbouw woningen om te checken")
    count = 0
    found = 0
    gevel_found = 0
    errors = 0
    for w in laagbouw:
        if count >= max_requests:
            break
        perceel_data = get_perceel_data(w['rd_x'], w['rd_y'], debug=(count < 3 and debug))
        if perceel_data:
            try:
                w['perceel_opp_m2'] = int(float(perceel_data['oppervlakte'])) if perceel_data['oppervlakte'] else None
                w['perceelnummer'] = perceel_data['perceelnummer']
                w['kadastrale_aanduiding'] = perceel_data.get('kadastraleAanduiding')
                found += 1
                if perceel_data.get('geometry'):
                    gevel_result = bepaal_gevel_orientatie(perceel_data['geometry'], w['rd_x'], w['rd_y'])
                    if gevel_result and gevel_result.get('betrouwbaar'):
                        w['gevel_orientatie'] = gevel_result['gevel_richting']
                        gevel_found += 1
                    else:
                        w['gevel_orientatie'] = None
                else:
                    w['gevel_orientatie'] = None
            except Exception as e:
                errors += 1
        else:
            errors += 1
        count += 1
        if count % 50 == 0:
            print(f"   ... {count}/{len(laagbouw)} ({found} percelen, {gevel_found} gevels)")
            time.sleep(0.3)
    print(f"   ✓ {found} percelen gevonden, {gevel_found} geveloriëntaties bepaald")
    return woningen


def bereken_eigendom_score(woningen, cbs_data=None):
    print(f"🎯 Eigendom scoremodel berekenen (v2)...")
    start = time.time()
    cbs_huur_pct = cbs_data.get('pct_huur') if cbs_data else None
    cbs_stedelijkheid = cbs_data.get('stedelijkheid') if cbs_data else None
    if cbs_huur_pct:
        print(f"   CBS huur%: {cbs_huur_pct}%")
    if cbs_stedelijkheid:
        print(f"   CBS stedelijkheid: {cbs_stedelijkheid}")
    cbs_huur_factor = int(cbs_huur_pct / 10) if cbs_huur_pct else 0
    from collections import defaultdict
    perceel_groepen = defaultdict(list)
    for w in woningen:
        perceel_nr = w.get('perceelnummer')
        if perceel_nr:
            perceel_groepen[perceel_nr].append(w)
    for perceel_nr, groep in perceel_groepen.items():
        aantal = len(groep)
        woon_opps = [w.get('woon_opp_m2') for w in groep if w.get('woon_opp_m2')]
        perc_opp = groep[0].get('perceel_opp_m2', 0) or 0
        verschillend_volume = False
        if len(woon_opps) >= 2 and aantal == 2:
            gem_opp = sum(woon_opps) / len(woon_opps)
            opp_variatie = max(woon_opps) - min(woon_opps)
            variatie_pct = (opp_variatie / gem_opp) * 100 if gem_opp > 0 else 0
            verschillend_volume = variatie_pct > 25
        for w in groep:
            w['perceel_gedeeld_door'] = aantal
            w['groep_verschillend_volume'] = verschillend_volume
            w['perceel_opp_totaal'] = perc_opp
    for w in woningen:
        signalen = []
        aantal_op_perceel = w.get('perceel_gedeeld_door', 1) or 1
        woningtype = (w.get('woningtype') or w.get('woningtype_geschat') or '').lower()
        bouwtype = w.get('bouwtype', '')
        perceel_opp = w.get('perceel_opp_totaal', 0) or 0
        verschillend_volume = w.get('groep_verschillend_volume', False)
        aantal_in_pand = w.get('aantal_in_pand', 1) or 1
        is_flat = aantal_in_pand > 3
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
        if is_flat:
            basis_huur = 50
            huur_kans = min(95, basis_huur + (cbs_huur_factor * 5))
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
        if aantal_op_perceel > 1:
            basis_huur = 80
            extra_huur = cbs_huur_factor
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
        basis_koop = 70
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
    return bereken_eigendom_score(woningen, cbs_data)


def parse_huisnummer(huisnr_raw):
    if huisnr_raw is None:
        return None, None
    huisnr_str = str(huisnr_raw).strip()
    if not huisnr_str:
        return None, None
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
    from collections import defaultdict
    straten = defaultdict(lambda: {'even': [], 'oneven': []})
    for w in woningen:
        straat = w.get('straat')  
        if not straat:
            continue
        huisnr, suffix = parse_huisnummer(w.get('huisnr'))
        if huisnr is None:
            continue
        w['_huisnr_num'] = huisnr
        w['_huisnr_suffix'] = suffix or ''
        if huisnr % 2 == 0:
            straten[straat]['even'].append(w)
        else:
            straten[straat]['oneven'].append(w)
    geschat_count = 0
    anker_count = 0
    propagated_count = 0
    for straat, zijden in straten.items():
        for zijde, huizen in zijden.items():
            if len(huizen) < 2:
                continue
            huizen.sort(key=lambda x: (x['_huisnr_num'], x['_huisnr_suffix']))
            ankers = []
            for i, huis in enumerate(huizen):
                woningtype = huis.get('woningtype', '').lower() if huis.get('woningtype') else ''
                if woningtype and ('rij' in woningtype or 'tussen' in woningtype or 'midden' in woningtype):
                    ankers.append(i)
                    anker_count += 1
            if not ankers:
                for i, huis in enumerate(huizen):
                    if huis.get('woningtype'):
                        continue
                    schatting = _schat_woningtype_simpel(huis)
                    if schatting:
                        huis['woningtype_geschat'] = schatting['geschat_type']
                        huis['woningtype_zekerheid'] = schatting['zekerheid_pct']
                        huis['woningtype_reden'] = schatting['reden']
                        geschat_count += 1
                continue
            for anker_idx in ankers:
                anker = huizen[anker_idx]
                anker_perceel = anker.get('perceel_opp_m2') or 0
                anker_woon = anker.get('woon_opp_m2') or 0
                for i in range(anker_idx + 1, len(huizen)):
                    huis = huizen[i]
                    if huis.get('woningtype'):
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
                        huis['woningtype_reden'] = f'hoek (einde blok): {", ".join(redenen)}'
                        propagated_count += 1
                        break
                    else:
                        huis['woningtype_geschat'] = 'rijwoning'
                        huis['woningtype_zekerheid'] = 80
                        huis['woningtype_reden'] = f'zelfde grootte als anker nr {anker.get("huisnr")}'
                        propagated_count += 1
                for i in range(anker_idx - 1, -1, -1):
                    huis = huizen[i]
                    if huis.get('woningtype'):
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
                        huis['woningtype_reden'] = f'zelfde grootte als anker nr {anker.get("huisnr")}'
                        propagated_count += 1
    for w in woningen:
        w.pop('_huisnr_num', None)
        w.pop('_huisnr_suffix', None)
    if anker_count > 0:
        print(f"   ✓ Straatanalyse: {anker_count} ankers gevonden, {propagated_count} types gepropageerd")
    if geschat_count > 0:
        print(f"   ✓ {geschat_count} woningtypes geschat (fallback perceelgrootte)")
    return woningen


def _schat_woningtype_simpel(woning):
    perceel_opp = woning.get('perceel_opp_m2') or 0
    woon_opp = woning.get('woon_opp_m2') or 0
    aantal_op_perceel = woning.get('perceel_gedeeld_door') or 1
    if perceel_opp == 0:
        return None
    if aantal_op_perceel > 1:
        return None
    if perceel_opp >= 800:
        zekerheid = min(95, 70 + (perceel_opp - 800) / 20)
        return {'geschat_type': 'vrijstaand', 'zekerheid_pct': round(zekerheid), 'reden': f'groot perceel ({int(perceel_opp)}m²)'}
    elif perceel_opp >= 500:
        if woon_opp >= 150:
            zekerheid = 60 + min(20, (woon_opp - 150) / 5)
            return {'geschat_type': 'vrijstaand', 'zekerheid_pct': round(zekerheid), 'reden': f'perceel {int(perceel_opp)}m² + woning {int(woon_opp)}m²'}
        else:
            return {'geschat_type': '2^1kap', 'zekerheid_pct': 55, 'reden': f'perceel {int(perceel_opp)}m² + woning {int(woon_opp)}m²'}
    elif perceel_opp >= 300 and aantal_op_perceel == 1:
        if woon_opp >= 120:
            return {'geschat_type': '2^1kap', 'zekerheid_pct': 60, 'reden': f'middelgroot perceel ({int(perceel_opp)}m²)'}
        else:
            return {'geschat_type': 'hoekwoning', 'zekerheid_pct': 50, 'reden': f'perceel {int(perceel_opp)}m²'}
    return None


def schat_woningtype(woning):
    if woning.get('woningtype'):
        return None
    return _schat_woningtype_simpel(woning)


def verrijk_woningtype_schattingen(woningen, debug=False):
    return detecteer_woningtype_via_straat(woningen, debug=debug)

# -----------------------------
# 4. Energielabels (EP-Online)
# -----------------------------
def get_energielabel(postcode, huisnr, huisletter=None, toevoeging=None, debug=False):
    if not EP_ONLINE_API_KEY:
        return None
    postcode = str(postcode).replace(" ", "").upper()
    url = f"{EP_ONLINE_API}/PandEnergielabel/Adres"
    headers = {"Authorization": EP_ONLINE_API_KEY, "Accept": "application/json"}
    params = {"postcode": postcode, "huisnummer": huisnr}
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
                return {
                    'energieklasse': record.get("Energieklasse"),
                    'gebouwtype': record.get('Gebouwtype'),
                    'bouwjaar_label': record.get('Bouwjaar'),
                    'gebruiksoppervlakte': record.get('Gebruiksoppervlakte_thermische_zone'),
                    'registratiedatum': record.get('Registratiedatum'),
                    'geldig_tot': record.get('Geldig_tot'),
                }
        elif r.status_code in (404, 401):
            return None
    except:
        pass
    return None

def enrich_energielabels(woningen, max_requests=150, debug=False):
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
            w['postcode'], w['huisnr'],
            w['huisletter'] if w['huisletter'] else None,
            w['toevoeging'] if w['toevoeging'] else None,
            debug=(count < 3 and debug)
        )
        if result:
            w['energielabel'] = result['energieklasse']
            w['energielabel_data'] = result
            found += 1
            ep_gebouwtype = result.get('gebouwtype')
            if ep_gebouwtype:
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
                    if aantal_in_pand > 3:
                        w['bouwtype'] = 'HOOGBOUW'
                    else:
                        w['bouwtype'] = 'LAAGBOUW'
                else:
                    w['woningtype'] = ep_gebouwtype
                w['woningtype_bron'] = 'EP-Online'
                type_updated += 1
        else:
            no_label += 1
        count += 1
        if count % 50 == 0:
            print(f"   ... {count}/{len(woningen)} checked ({found} met label)")
            time.sleep(0.3)
    print(f"   ✓ {found} met label, {no_label} zonder label")
    if type_updated > 0:
        print(f"   ✓ {type_updated} woningtypes bijgewerkt met EP-Online data")
    return woningen

# -----------------------------
# 5. CBS buurtdata
# -----------------------------
def get_cbs_buurt(buurtcode, debug=False):
    if not buurtcode:
        return {}
    try:
        if debug:
            print(f"   CBS data ophalen voor {buurtcode}...")
        data = cbsodata.get_data('85039NED')
        for row in data:
            if row.get('Codering_3', '').strip() == buurtcode:
                hh_totaal = row.get('HuishoudensTotaal_28') or 1
                eenpersoons_abs = row.get('Eenpersoonshuishoudens_29') or 0
                zonder_kind_abs = row.get('HuishoudensZonderKinderen_30') or 0
                met_kind_abs = row.get('HuishoudensMetKinderen_31') or 0
                pct_eenpersoons = round(eenpersoons_abs / hh_totaal * 100) if hh_totaal else 0
                pct_zonder_kind = round(zonder_kind_abs / hh_totaal * 100) if hh_totaal else 0
                pct_met_kind = round(met_kind_abs / hh_totaal * 100) if hh_totaal else 0
                inwoners = row.get('AantalInwoners_5') or 1
                pct_0_15 = round((row.get('k_0Tot15Jaar_8') or 0) / inwoners * 100) if inwoners else 0
                pct_15_25 = round((row.get('k_15Tot25Jaar_9') or 0) / inwoners * 100) if inwoners else 0
                pct_25_45 = round((row.get('k_25Tot45Jaar_10') or 0) / inwoners * 100) if inwoners else 0
                pct_45_65 = round((row.get('k_45Tot65Jaar_11') or 0) / inwoners * 100) if inwoners else 0
                pct_65plus = round((row.get('k_65JaarOfOuder_12') or 0) / inwoners * 100) if inwoners else 0
                pct_westers = round((row.get('WestersTotaal_17') or 0) / inwoners * 100) if inwoners else 0
                pct_niet_westers = round((row.get('NietWestersTotaal_18') or 0) / inwoners * 100) if inwoners else 0
                return {
                    'inwoners': row.get('AantalInwoners_5'),
                    'huishoudens': row.get('HuishoudensTotaal_28'),
                    'gem_huishoudgrootte': row.get('GemiddeldeHuishoudensgrootte_32'),
                    'bevolkingsdichtheid': row.get('Bevolkingsdichtheid_33'),
                    'pct_eenpersoons': pct_eenpersoons,
                    'pct_hh_zonder_kind': pct_zonder_kind,
                    'pct_hh_met_kind': pct_met_kind,
                    'pct_0_15': pct_0_15,
                    'pct_15_25': pct_15_25,
                    'pct_25_45': pct_25_45,
                    'pct_45_65': pct_45_65,
                    'pct_65plus': pct_65plus,
                    'pct_westers': pct_westers,
                    'pct_niet_westers': pct_niet_westers,
                    'woningen_totaal': row.get('Woningvoorraad_34'),
                    'pct_eengezins': row.get('PercentageEengezinswoning_36'),
                    'pct_meergezins': row.get('PercentageMeergezinswoning_37'),
                    'pct_koop': row.get('Koopwoningen_40'),
                    'pct_huur': row.get('HuurwoningenTotaal_41'),
                    'pct_huur_corporatie': row.get('InBezitWoningcorporatie_42'),
                    'pct_huur_overig': row.get('InBezitOverigeVerhuurders_43'),
                    'gem_woz_k': row.get('GemiddeldeWOZWaardeVanWoningen_35'),
                    'gem_elektra_kwh': row.get('GemiddeldeElektriciteitsleveringTotaal_47'),
                    'gem_gas_m3': row.get('GemiddeldAardgasverbruikTotaal_55'),
                    'auto_per_hh': row.get('PersonenautoSPerHuishouden_103'),
                    'gem_inkomen_k': row.get('GemiddeldInkomenPerInkomensontvanger_71'),
                    'pct_laag_inkomen': row.get('HuishoudensMetEenLaagInkomen_78'),
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
# 7. Tabel + CSV export
# -----------------------------
def print_tabel(woningen, max_rows=100):
    print("\n" + "="*235)
    print(f"{'#':>3} | {'Adres':<28} | {'PC':^7} | {'Bouw':<8} | {'Type':<10} | {'Jaar':>4} | {'Woon':>5} | {'Perc':>5} | {'#Ged':>4} | {'Eig':<4} | {'Kans':>4} | {'K%':>3} | {'F%':>3} | {'H%':>3} | {'Label':>5} | {'Afst':>4}")
    print("-"*235)
    for i, w in enumerate(woningen[:max_rows], 1):
        adres = f"{w['straat']} {w['huisnr']}"
        if w['huisletter']:
            adres += w['huisletter']
        if w['toevoeging']:
            adres += f"-{w['toevoeging']}"
        adres = adres[:28]
        bouwtype = w['bouwtype'][:8] if w['bouwtype'] else "-"
        wtype = w['woningtype'][:10] if w['woningtype'] else "-"
        jaar = str(w['bouwjaar']) if w['bouwjaar'] else "-"
        woon = str(int(w['woon_opp_m2'])) if w['woon_opp_m2'] else "-"
        perc = str(int(w['perceel_opp_m2'])) if w['perceel_opp_m2'] else "-"
        gedeeld = str(w.get('perceel_gedeeld_door', '')) if w.get('perceel_gedeeld_door') else "-"
        eigendom = w.get('eigendom', '') or "-"
        eig_kans = str(w.get('eigendom_kans', '')) if w.get('eigendom_kans') else "-"
        k_koop = str(w.get('kans_koop', '')) if w.get('kans_koop') else "-"
        k_fam = str(w.get('kans_familie_erf', '')) if w.get('kans_familie_erf') else "-"
        k_huur = str(w.get('kans_huur', '')) if w.get('kans_huur') else "-"
        label = w['energielabel'] or "-"
        afst = f"{int(w['afstand_m'])}"
        print(f"{i:>3} | {adres:<28} | {w['postcode']:^7} | {bouwtype:<8} | {wtype:<10} | {jaar:>4} | {woon:>5} | {perc:>5} | {gedeeld:>4} | {eigendom:<4} | {eig_kans:>4} | {k_koop:>3} | {k_fam:>3} | {k_huur:>3} | {label:>5} | {afst:>4}m")
    print("="*235)

def export_csv(woningen, filename="buurt_data.csv"):
    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow([
            'nr', 'straat', 'huisnr', 'huisletter', 'toevoeging', 'postcode', 'plaats',
            'lat', 'lon', 'afstand_m',
            'bouwtype', 'woningtype', 'aantal_in_pand', 'bouwjaar',
            'woon_opp_m2', 'perceel_opp_m2', 'perceelnummer', 'kadastrale_aanduiding',
            'perceel_gedeeld_door', 'eigendom', 'gevel_orientatie', 'energielabel',
            'dak_opp_m2', 'dak_orientatie_gr', 'dak_orient_naam', 'dak_helling_gr',
            'bouwlagen', 'hoogte_nok_m', 'hoogte_goot_m', 'pand_id'
        ])
        for i, w in enumerate(woningen, 1):
            writer.writerow([
                i, w['straat'], w['huisnr'], w['huisletter'], w['toevoeging'],
                w['postcode'], w['plaats'], w['lat'], w['lon'], w['afstand_m'],
                w['bouwtype'], w['woningtype'], w['aantal_in_pand'], w['bouwjaar'],
                w['woon_opp_m2'], w['perceel_opp_m2'] or '',
                w.get('perceelnummer', '') or '', w.get('kadastrale_aanduiding', '') or '',
                w.get('perceel_gedeeld_door', '') or '', w.get('eigendom', '') or '',
                w.get('gevel_orientatie', '') or '', w['energielabel'] or '',
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
# 8. HTML KAART MET FILTERS + EXPORT
# -----------------------------
TEMPLATE_FILE = "buurt_kaart_template.html"


def _bouw_cbs_html(cbs_data):
    """Bouw CBS content HTML voor in de template"""
    if not cbs_data:
        return '<div style="margin-top:8px;color:#64748b;font-size:12px">Geen CBS data</div>'

    def _fmt_woz(raw):
        if raw and raw > 1000:
            return f"&euro;{round(raw/1000)}k"
        return f"&euro;{raw}k" if raw else "-"

    def _fmt_ink(raw):
        if raw and raw > 100:
            return f"&euro;{round(raw/10)}k"
        return f"&euro;{raw}k" if raw else "-"

    woz = _fmt_woz(cbs_data.get('gem_woz_k'))
    ink = _fmt_ink(cbs_data.get('gem_inkomen_k'))
    pct_jong = (cbs_data.get('pct_0_15', 0) or 0) + (cbs_data.get('pct_15_25', 0) or 0)
    pct_mid = (cbs_data.get('pct_25_45', 0) or 0) + (cbs_data.get('pct_45_65', 0) or 0)
    pct_oud = cbs_data.get('pct_65plus', 0) or 0
    g = cbs_data.get

    return (
        '<div class="cb-section">'
        '<div class="cb-label">Inwoners</div>'
        f'<div class="cb-grid"><span>{g("inwoners","-")}</span>'
        f'<span class="cb-dim">dichth. {g("bevolkingsdichtheid","-")}/km&sup2;</span></div>'
        '</div>'
        '<div class="cb-section">'
        '<div class="cb-label">Huishoudens</div>'
        f'<div class="cb-grid"><span>{g("huishoudens","-")}</span>'
        f'<span class="cb-dim">gem. {g("gem_huishoudgrootte","-")} pers</span></div>'
        f'<div class="cb-bar-wrap"><div class="cb-bar" style="width:{g("pct_eenpersoons",0)}%;background:#64748b"></div>'
        f'<span>Alleen {g("pct_eenpersoons",0)}%</span></div>'
        f'<div class="cb-bar-wrap"><div class="cb-bar" style="width:{g("pct_hh_met_kind",0)}%;background:#6ea8fe"></div>'
        f'<span>Met kind {g("pct_hh_met_kind",0)}%</span></div>'
        '</div>'
        '<div class="cb-section">'
        '<div class="cb-label">Leeftijd</div>'
        f'<div class="cb-bar-wrap"><div class="cb-bar" style="width:{pct_jong}%;background:#6ea8fe"></div>'
        f'<span>0-24: {pct_jong}%</span></div>'
        f'<div class="cb-bar-wrap"><div class="cb-bar" style="width:{pct_mid}%;background:#64748b"></div>'
        f'<span>25-64: {pct_mid}%</span></div>'
        f'<div class="cb-bar-wrap"><div class="cb-bar" style="width:{pct_oud}%;background:#475569"></div>'
        f'<span>65+: {pct_oud}%</span></div>'
        '</div>'
        '<div class="cb-section">'
        '<div class="cb-label">Woningen</div>'
        f'<div class="cb-split"><div class="cb-chip koop">Koop {g("pct_koop","-")}%</div>'
        f'<div class="cb-chip huur">Huur {g("pct_huur","-")}%</div></div>'
        f'<div class="cb-grid"><span class="cb-dim">corp. {g("pct_huur_corporatie","-")}%</span>'
        f'<span class="cb-dim">WOZ {woz}</span></div>'
        '</div>'
        '<div class="cb-section">'
        '<div class="cb-label">Energie</div>'
        f'<div class="cb-grid"><span class="cb-dim">Gas {g("gem_gas_m3","-")} m&sup3;</span>'
        f'<span class="cb-dim">Elek {g("gem_elektra_kwh","-")} kWh</span></div>'
        '</div>'
        '<div class="cb-section" style="border:none;padding-bottom:0">'
        '<div class="cb-label">Inkomen</div>'
        f'<div class="cb-grid"><span>Gem. {ink}</span>'
        f'<span class="cb-dim">laag {g("pct_laag_inkomen","-")}%</span></div>'
        '</div>'
    )


def _bouw_woningen_json(woningen):
    """Bouw JSON array met alle woningdata voor client-side filtering"""
    result = []
    for i, w in enumerate(woningen):
        if not w.get('lat') or not w.get('lon'):
            continue
        adres = f"{w.get('straat', '')} {w.get('huisnr', '')}"
        if w.get('huisletter'):
            adres += w['huisletter']
        if w.get('toevoeging'):
            adres += f"-{w['toevoeging']}"
        result.append({
            'i': i, 'lat': w['lat'], 'lon': w['lon'], 'adres': adres,
            'postcode': w.get('postcode', ''), 'plaats': w.get('plaats', ''),
            'straat': w.get('straat', ''), 'huisnr': w.get('huisnr', ''),
            'huisletter': w.get('huisletter', ''), 'toevoeging': w.get('toevoeging', ''),
            'bouwtype': w.get('bouwtype', ''), 'woningtype': w.get('woningtype', ''),
            'woningtype_geschat': w.get('woningtype_geschat', ''),
            'woningtype_zekerheid': w.get('woningtype_zekerheid', ''),
            'bouwjaar': w.get('bouwjaar'), 'woon_opp_m2': w.get('woon_opp_m2'),
            'perceel_opp_m2': w.get('perceel_opp_m2'),
            'perceelnummer': w.get('perceelnummer', ''),
            'kadastrale_aanduiding': w.get('kadastrale_aanduiding', ''),
            'perceel_gedeeld_door': w.get('perceel_gedeeld_door'),
            'aantal_in_pand': w.get('aantal_in_pand', 1),
            'eigendom': w.get('eigendom', ''), 'eigendom_kans': w.get('eigendom_kans', 0),
            'kans_koop': w.get('kans_koop', 0), 'kans_huur': w.get('kans_huur', 0),
            'kans_familie_erf': w.get('kans_familie_erf', 0),
            'signalen': w.get('signalen', []),
            'energielabel': w.get('energielabel', ''),
            'afstand_m': w.get('afstand_m', 0),
            'gevel_orientatie': w.get('gevel_orientatie', ''),
            'dak_opp_m2': w.get('dak_opp_m2'),
            'dak_orient_naam': w.get('dak_orient_naam', ''),
            'dak_helling_gr': w.get('dak_helling_gr'),
            'bouwlagen': w.get('bouwlagen'),
            'hoogte_nok_m': w.get('hoogte_nok_m'),
            'hoogte_goot_m': w.get('hoogte_goot_m'),
            'pand_id': w.get('pand_id', ''),
        })
    return result


def genereer_html_kaart(woningen, output_file="buurt_kaart.html", titel="Buurtdata Kaart",
                        cbs_data=None, buurtcode=None, buurtnaam=None,
                        centrum_lat=None, centrum_lon=None, straal=None,
                        template_path=None):
    """
    Genereer interactieve HTML kaart door template te vullen met data.
    Leest buurt_kaart_template.html en vervangt %%PLACEHOLDERS%%.
    """
    if not woningen:
        print("❌ Geen woningen om te tonen")
        return None

    # Zoek template naast dit script
    if template_path is None:
        import os
        script_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(script_dir, TEMPLATE_FILE)

    try:
        with open(template_path, 'r', encoding='utf-8') as f:
            html = f.read()
    except FileNotFoundError:
        print(f"❌ Template niet gevonden: {template_path}")
        print(f"   Zorg dat '{TEMPLATE_FILE}' naast het script staat.")
        return None

    # Bereken centrum
    lats = [w['lat'] for w in woningen if w.get('lat')]
    lons = [w['lon'] for w in woningen if w.get('lon')]
    if not lats or not lons:
        print("❌ Geen geldige coördinaten")
        return None
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    # Bouw data
    buurt_display = buurtnaam or "Onbekend"
    if buurtcode:
        buurt_display += f" ({buurtcode})"

    kadaster_link = ""
    if buurtcode:
        kadaster_link = (
            f'<a href="https://kadastralekaart.com/buurten/{buurtcode}" '
            f'target="_blank" style="color:#6ea8fe;text-decoration:none;font-size:12px;">'
            f'&#x1f517; kadastralekaart.com</a>'
        )

    woningen_data = _bouw_woningen_json(woningen)
    config_data = {
        'center_lat': center_lat,
        'center_lon': center_lon,
        'centrum_lat': centrum_lat,
        'centrum_lon': centrum_lon,
        'straal': straal,
    }

    # Vervang placeholders in template
    html = html.replace('%%TITEL%%', titel)
    html = html.replace('%%BUURT_DISPLAY%%', buurt_display)
    html = html.replace('%%KADASTER_LINK%%', kadaster_link)
    html = html.replace('%%CBS_CONTENT%%', _bouw_cbs_html(cbs_data))
    html = html.replace('%%WONINGEN_JSON%%', json.dumps(woningen_data, ensure_ascii=False))
    html = html.replace('%%CONFIG_JSON%%', json.dumps(config_data, ensure_ascii=False))

    # Schrijf output
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html)

    count_koop = len([w for w in woningen if w.get('eigendom') == 'KOOP'])
    count_huur = len([w for w in woningen if w.get('eigendom') == 'HUUR'])
    count_fam = len([w for w in woningen if w.get('eigendom') == 'FAM'])

    print(f"🗺️  Kaart gegenereerd: {output_file}")
    print(f"   {count_koop} KOOP | {count_fam} FAM | {count_huur} HUUR | {len(woningen)} totaal")
    print(f"   ✨ Interactieve filters + CSV export ingebouwd")

    return output_file


# -----------------------------
# MAIN
# -----------------------------
def verzamel_data(straat, huisnr, plaats, straal=200, debug=False):
    print("\n" + "="*60)
    print("🏘️  BUURT DATA VERZAMELAAR")
    print("="*60)
    print(f"📍 Centrum: {straat} {huisnr}, {plaats}")
    print(f"📏 Straal: {straal}m")
    print("="*60 + "\n")
    total_start = time.time()
    start = time.time()
    cx, cy = get_rd_from_address(straat, huisnr, plaats)
    buurtcode, buurtnaam = get_buurtcode(cx, cy)
    from pyproj import Transformer
    transformer = Transformer.from_crs("EPSG:28992", "EPSG:4326", always_xy=True)
    centrum_lon, centrum_lat = transformer.transform(cx, cy)
    print(f"📍 Buurt: {buurtnaam} ({buurtcode}) [{time.time()-start:.1f}s]")
    start = time.time()
    woningen = get_bag_woningen(cx, cy, straal)
    print(f"   ⏱️  BAG ophalen: {time.time()-start:.1f}s")
    if not woningen:
        print("❌ Geen woningen gevonden")
        return [], buurtcode, buurtnaam, {}, centrum_lat, centrum_lon
    start = time.time()
    woningen = enrich_percelen(woningen, max_requests=len(woningen), debug=debug)
    print(f"   ⏱️  Percelen ophalen: {time.time()-start:.1f}s")
    start = time.time()
    woningen = enrich_energielabels(woningen, max_requests=len(woningen), debug=debug)
    print(f"   ⏱️  Energielabels ophalen: {time.time()-start:.1f}s")
    start = time.time()
    cbs = get_cbs_buurt(buurtcode, debug=debug)
    print(f"   ⏱️  CBS data ophalen: {time.time()-start:.1f}s")
    if cbs:
        print(f"\n📊 CBS Buurtdata ({buurtnaam}):")
        if cbs.get('pct_koop'):
            print(f"   % Koop: {cbs.get('pct_koop')}%")
        if cbs.get('pct_huur'):
            print(f"   % Huur: {cbs.get('pct_huur')}%")
    start = time.time()
    woningen = detecteer_huur_gedeeld_perceel(woningen, cbs_data=cbs)
    print(f"   ⏱️  Eigendom detectie: {time.time()-start:.1f}s")
    start = time.time()
    woningen = verrijk_woningtype_schattingen(woningen)
    print(f"   ⏱️  Woningtype schatting: {time.time()-start:.1f}s")
    print(f"\n⏱️  TOTALE TIJD: {time.time()-total_start:.1f}s")
    return woningen, buurtcode, buurtnaam, cbs, centrum_lat, centrum_lon


if __name__ == "__main__":
    STRAAT = "Hendrik van den Heuvellaan"
    HUISNR = "19"
    PLAATS = "Hooglanderveen"
    STRAAL = 500
    DEBUG = True
    
    woningen, buurtcode, buurtnaam, cbs_data, centrum_lat, centrum_lon = verzamel_data(STRAAT, HUISNR, PLAATS, STRAAL, debug=DEBUG)
    
    if not woningen:
        exit()
    
    print_tabel(woningen, max_rows=100)
    export_csv(woningen, "buurt_data.csv")
    
    kaart_titel = f"Buurtdata {STRAAT} {HUISNR}, {PLAATS} ({STRAAL}m)"
    genereer_html_kaart(woningen, "buurt_kaart.html", kaart_titel,
                        cbs_data=cbs_data, buurtcode=buurtcode, buurtnaam=buurtnaam,
                        centrum_lat=centrum_lat, centrum_lon=centrum_lon, straal=STRAAL)
    
    print(f"\n✅ Klaar!")
    print(f"   📊 CSV data: buurt_data.csv")
    print(f"   🗺️  Kaart: buurt_kaart.html (open in browser)")