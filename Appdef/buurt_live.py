"""
BUURTCAMPAGNE DATA VERZAMELAAR - LIVE VERSIE + ZONNEPANELEN AI
==============================================================
Start een WebSocket server en opent de browser.
Data wordt real-time naar de kaart gepusht.
Detecteert zonnepanelen via YOLOv8 op satellietbeelden.

Vereist: pip install websockets pyproj cbsodata requests ultralytics pillow

Gebruik:
    python buurt_live.py

Of importeer en roep aan:
    from buurt_live import start_live
    start_live("Dorpsstraat", "1", "Amsterdam", straal=300)
"""

import asyncio
import json
import webbrowser
import os
import math
import time
import io
from collections import defaultdict
from pathlib import Path
from http.server import SimpleHTTPRequestHandler
import socketserver
import threading

import requests
from pyproj import Transformer

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Optionele imports
try:
    import websockets
except ImportError:
    print("❌ websockets niet geïnstalleerd. Run: pip install websockets")
    exit(1)

try:
    import cbsodata
except ImportError:
    cbsodata = None
    print("⚠️ cbsodata niet geïnstalleerd - CBS data wordt overgeslagen")

# YOLO voor zonnepanelen detectie
try:
    from ultralytics import YOLO
    from PIL import Image
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False
    print("⚠️ ultralytics/PIL niet geïnstalleerd - zonnepanelen detectie uitgeschakeld")
    print("   Run: pip install ultralytics pillow")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════
WS_PORT = 8765
HTTP_PORT = 8766
TEMPLATE_FILE = "buurt_kaart_live.html"
OUTPUT_HTML = "buurt_kaart.html"

LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
BAG_WFS = "https://service.pdok.nl/lv/bag/wfs/v2_0"
KADASTER_WFS = "https://service.pdok.nl/kadaster/kadastralekaart/wfs/v5_0"
EP_ONLINE_API = "https://public.ep-online.nl/api/v5"
EP_ONLINE_API_KEY = os.getenv("EP_ONLINE_API_KEY", "")

# PDOK Luchtfoto WMTS voor satellietbeelden
PDOK_WMTS_URL = "https://service.pdok.nl/hwh/luchtfotorgb/wmts/v1_0"

# Google Maps Static API (scherper!)
GOOGLE_STATIC_URL = "https://maps.googleapis.com/maps/api/staticmap"
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

# YOLO model config
YOLO_MODEL_PATH = str(Path(__file__).parent / "zonnepanelen_yolo.pt")  # Custom trained model
YOLO_CONFIDENCE = 0.25  # Minimum confidence threshold
YOLO_TILE_SIZE = 640  # Pixel size voor YOLO input
YOLO_METERS_PER_TILE = 50  # Meters per tile (bepaalt zoom niveau)

# Gebruik Google Maps als API key beschikbaar is
USE_GOOGLE_MAPS = bool(GOOGLE_MAPS_API_KEY)

transformer = Transformer.from_crs("EPSG:28992", "EPSG:4326", always_xy=True)
transformer_to_rd = Transformer.from_crs("EPSG:4326", "EPSG:28992", always_xy=True)
transformer_to_webmercator = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def rd_to_latlon(x, y):
    lon, lat = transformer.transform(x, y)
    return lat, lon


def afstand(x1, y1, x2, y2):
    return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)


# ══════════════════════════════════════════════════════════════════════════════
# ZONNEPANELEN DETECTIE
# ══════════════════════════════════════════════════════════════════════════════
class ZonnepanelenDetector:
    """YOLOv8 based zonnepanelen detectie op satellietbeelden"""

    def __init__(self, model_path=None):
        self.model = None
        self.model_path = model_path or YOLO_MODEL_PATH

        if not YOLO_AVAILABLE:
            print("⚠️ YOLO niet beschikbaar")
            return

        # Probeer custom model te laden, anders gebruik pretrained
        if Path(self.model_path).exists():
            print(f"🤖 Laden custom YOLO model: {self.model_path}")
            self.model = YOLO(self.model_path)
        else:
            print(f"⚠️ Custom model niet gevonden: {self.model_path}")
            print("   Gebruik 'yolov8n.pt' als fallback (detecteert geen zonnepanelen)")
            print("   Train een custom model met: python train_zonnepanelen.py")
            # Laad basis model voor testen (detecteert geen zonnepanelen maar werkt)
            self.model = YOLO('yolov8n.pt')

    def get_google_satellite_image(self, lat, lon, size=640, zoom=20):
        """Haal satellietbeeld op van Google Maps Static API"""
        if not GOOGLE_MAPS_API_KEY:
            return None
            
        params = {
            "center": f"{lat},{lon}",
            "zoom": zoom,
            "size": f"{size}x{size}",
            "maptype": "satellite",
            "key": GOOGLE_MAPS_API_KEY,
        }
        
        try:
            r = requests.get(GOOGLE_STATIC_URL, params=params, timeout=15)
            if r.status_code == 200:
                img = Image.open(io.BytesIO(r.content))
                if img.size[0] == size:
                    return img
            return None
        except Exception as e:
            print(f"  ⚠️ Google Maps fout: {e}")
            return None

    def get_satellite_tile(self, lat, lon, meters=50):
        """
        Haal satellietbeeld op van PDOK rond gegeven coördinaten.
        Returns PIL Image of None bij fout.
        """
        try:
            # Converteer naar Web Mercator (EPSG:3857) voor WMTS
            x, y = transformer_to_webmercator.transform(lon, lat)

            # Bereken tile coordinaten voor zoom level 19 (hoogste resolutie)
            # Bij zoom 19: ~0.3m per pixel
            zoom = 19
            n = 2 ** zoom
            tile_x = int((lon + 180) / 360 * n)
            tile_y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)

            # PDOK WMTS URL
            url = f"{PDOK_WMTS_URL}/Actueel_orthoHR/EPSG:3857/{zoom}/{tile_x}/{tile_y}.jpeg"

            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                img = Image.open(io.BytesIO(r.content))
                return img
            else:
                return None
        except Exception as e:
            print(f"  ⚠️ Satellietbeeld fout: {e}")
            return None

    def get_satellite_area(self, lat, lon, meters=50, target_size=640):
        """
        Haal meerdere tiles op en stitch ze samen voor een gebied van X meters.
        Returns PIL Image gecropt rond het centrum.
        """
        try:
            # Bij zoom 19: ~0.3m per pixel, dus 50m = ~167 pixels
            # Een tile is 256x256 pixels = ~77m
            # Voor 50m rondom hebben we waarschijnlijk 2x2 of 3x3 tiles nodig

            zoom = 19
            meters_per_pixel = 0.3  # Ongeveer bij zoom 19 in NL
            pixels_needed = int(meters * 2 / meters_per_pixel)

            # Haal centrale tile
            n = 2 ** zoom
            center_tile_x = int((lon + 180) / 360 * n)
            center_tile_y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)

            # Bereken pixel positie binnen tile
            tile_lon_width = 360 / n
            tile_lon_start = center_tile_x * tile_lon_width - 180
            pixel_x_in_tile = int((lon - tile_lon_start) / tile_lon_width * 256)

            # Haal 3x3 grid van tiles
            tiles = {}
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    tx, ty = center_tile_x + dx, center_tile_y + dy
                    url = f"{PDOK_WMTS_URL}/Actueel_orthoHR/EPSG:3857/{zoom}/{tx}/{ty}.jpeg"
                    try:
                        r = requests.get(url, timeout=5)
                        if r.status_code == 200:
                            tiles[(dx, dy)] = Image.open(io.BytesIO(r.content))
                    except:
                        pass

            if not tiles:
                return None

            # Stitch tiles samen (3x3 = 768x768)
            stitched = Image.new('RGB', (768, 768))
            for (dx, dy), tile in tiles.items():
                stitched.paste(tile, ((dx + 1) * 256, (dy + 1) * 256))

            # Crop rond centrum en resize naar target
            center_x = 384 + (pixel_x_in_tile - 128)
            center_y = 384  # Simplified, zou ook berekend moeten worden
            half = pixels_needed // 2

            left = max(0, center_x - half)
            top = max(0, center_y - half)
            right = min(768, center_x + half)
            bottom = min(768, center_y + half)

            cropped = stitched.crop((left, top, right, bottom))
            resized = cropped.resize((target_size, target_size), Image.LANCZOS)

            return resized

        except Exception as e:
            print(f"  ⚠️ Satellietbeeld area fout: {e}")
            return None

    def detect(self, image):
        """
        Run YOLO detectie op een PIL Image.
        Returns dict met detectie resultaten.
        """
        if not self.model or not image:
            return {'detected': False, 'confidence': 0, 'count': 0, 'boxes': []}

        try:
            # Run inference
            results = self.model(image, conf=YOLO_CONFIDENCE, verbose=False)

            # Parse resultaten
            detections = []
            for r in results:
                for box in r.boxes:
                    cls = int(box.cls[0])
                    conf = float(box.conf[0])
                    # Check of het zonnepanelen class is (afhankelijk van je model)
                    # Bij custom model: class 0 = zonnepaneel
                    # Bij pretrained yolov8: geen zonnepanelen class
                    class_name = self.model.names.get(cls, '')
                    if 'solar' in class_name.lower() or 'zonnepanel' in class_name.lower() or 'panel' in class_name.lower():
                        detections.append({
                            'class': class_name,
                            'confidence': round(conf, 2),
                            'box': box.xyxy[0].tolist()
                        })

            if detections:
                best_conf = max(d['confidence'] for d in detections)
                return {
                    'detected': True,
                    'confidence': best_conf,
                    'count': len(detections),
                    'boxes': detections
                }
            else:
                return {'detected': False, 'confidence': 0, 'count': 0, 'boxes': []}

        except Exception as e:
            print(f"  ⚠️ YOLO detectie fout: {e}")
            return {'detected': False, 'confidence': 0, 'count': 0, 'boxes': [], 'error': str(e)}

    def detect_at_location(self, lat, lon, save_debug=False):
        """
        Volledige pipeline: haal satellietbeeld en run detectie.
        Gebruikt Google Maps als API key beschikbaar is, anders PDOK.
        """
        # Haal satellietbeeld - prioriteit: Google Maps (scherper)
        image = None
        source = "unknown"
        
        if USE_GOOGLE_MAPS:
            image = self.get_google_satellite_image(lat, lon, size=YOLO_TILE_SIZE, zoom=20)
            source = "google"
        
        if image is None:
            # Fallback naar PDOK
            image = self.get_satellite_area(lat, lon, meters=YOLO_METERS_PER_TILE, target_size=YOLO_TILE_SIZE)
            source = "pdok"

        if image is None:
            return {'detected': False, 'confidence': 0, 'count': 0, 'error': 'Geen satellietbeeld'}

        # Optioneel: save voor debugging/training
        if save_debug:
            debug_dir = Path("debug_tiles")
            debug_dir.mkdir(exist_ok=True)
            image.save(debug_dir / f"{lat}_{lon}_{source}.jpg")

        # Run detectie
        result = self.detect(image)
        result['source'] = source
        return result


# ══════════════════════════════════════════════════════════════════════════════
# DATA OPHALEN FUNCTIES
# ══════════════════════════════════════════════════════════════════════════════
def get_rd_from_address(straat, huisnr, plaats):
    """Haal RD coördinaten op voor een adres"""
    params = {"q": f"{straat} {huisnr} {plaats}", "rows": 1, "fl": "centroide_rd"}
    r = requests.get(LOCATIESERVER, params=params, timeout=10)
    r.raise_for_status()
    docs = r.json()["response"]["docs"]
    if not docs:
        raise ValueError(f"Adres niet gevonden: {straat} {huisnr}, {plaats}")
    coord = docs[0]["centroide_rd"].replace("POINT(", "").replace(")", "")
    x, y = map(float, coord.split())
    return x, y


def get_buurtcode(x, y):
    """Haal buurtcode op via reverse geocoding"""
    url = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse"
    params = {"X": x, "Y": y, "type": "buurt", "rows": 1, "fl": "buurtcode,buurtnaam"}
    try:
        r = requests.get(url, params=params, timeout=10)
        docs = r.json().get("response", {}).get("docs", [])
        if docs:
            return docs[0].get('buurtcode'), docs[0].get('buurtnaam')
    except:
        pass
    return None, None


def get_bag_woningen(center_x, center_y, straal):
    """Haal alle woningen op binnen straal"""
    minx, maxx = center_x - straal, center_x + straal
    miny, maxy = center_y - straal, center_y + straal
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typeName": "bag:verblijfsobject", "outputFormat": "application/json",
        "bbox": f"{minx},{miny},{maxx},{maxy},EPSG:28992"
    }
    r = requests.get(BAG_WFS, params=params, timeout=30)
    r.raise_for_status()
    features = r.json().get("features", [])

    # Tel per pand
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

        if "woonfunctie" not in props.get("gebruiksdoel", "").lower():
            continue

        pand_id = props.get("pandidentificatie")
        aantal_in_pand = len(panden.get(pand_id, []))
        bouwtype = "LAAGBOUW" if aantal_in_pand <= 3 else "HOOGBOUW"

        lat, lon = rd_to_latlon(x, y)
        postcode = (props.get("postcode") or "").replace(" ", "").upper()

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
            "woningtype": None,
            "aantal_in_pand": aantal_in_pand,
            "pand_id": pand_id,
            "perceel_opp_m2": None,
            "perceelnummer": None,
            "kadastrale_aanduiding": None,
            "perceel_gedeeld_door": None,
            "energielabel": None,
            "eigendom": None,
            "eigendom_kans": 0,
            "kans_koop": 0,
            "kans_huur": 0,
            "kans_familie_erf": 0,
            "signalen": [],
        })

    woningen.sort(key=lambda w: w['afstand_m'])
    return woningen


def get_perceel_data(x, y):
    """Haal perceeldata op"""
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
        features = r.json().get("features", [])
        if not features:
            return None
        props = features[0].get("properties", {})
        return {
            'perceelnummer': props.get("perceelnummer"),
            'oppervlakte': props.get("kadastraleGrootteWaarde"),
            'kadastrale_aanduiding': props.get("kadastraleAanduiding"),
        }
    except:
        return None


def get_energielabel(postcode, huisnr, huisletter=None, toevoeging=None):
    """Haal energielabel op"""
    if not EP_ONLINE_API_KEY:
        return None
    headers = {"Authorization": EP_ONLINE_API_KEY, "Accept": "application/json"}
    params = {"postcode": postcode, "huisnummer": huisnr}
    if huisletter:
        params["huisletter"] = huisletter
    if toevoeging:
        params["huisnummertoevoeging"] = toevoeging
    try:
        r = requests.get(f"{EP_ONLINE_API}/PandEnergielabel/Adres",
                         headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data:
                return {
                    'energieklasse': data[0].get("Energieklasse"),
                    'gebouwtype': data[0].get('Gebouwtype'),
                }
    except:
        pass
    return None


def get_cbs_buurt(buurtcode):
    """Haal CBS data op"""
    if not cbsodata or not buurtcode:
        return {}
    try:
        data = cbsodata.get_data('85039NED')
        for row in data:
            if row.get('Codering_3', '').strip() == buurtcode:
                hh = row.get('HuishoudensTotaal_28') or 1
                inw = row.get('AantalInwoners_5') or 1
                return {
                    'inwoners': row.get('AantalInwoners_5'),
                    'huishoudens': row.get('HuishoudensTotaal_28'),
                    'gem_huishoudgrootte': row.get('GemiddeldeHuishoudensgrootte_32'),
                    'bevolkingsdichtheid': row.get('Bevolkingsdichtheid_33'),
                    'pct_eenpersoons': round((row.get('Eenpersoonshuishoudens_29') or 0) / hh * 100),
                    'pct_hh_met_kind': round((row.get('HuishoudensMetKinderen_31') or 0) / hh * 100),
                    'pct_65plus': round((row.get('k_65JaarOfOuder_12') or 0) / inw * 100),
                    'pct_koop': row.get('Koopwoningen_40'),
                    'pct_huur': row.get('HuurwoningenTotaal_41'),
                    'pct_huur_corporatie': row.get('InBezitWoningcorporatie_42'),
                    'gem_woz_k': row.get('GemiddeldeWOZWaardeVanWoningen_35'),
                    'gem_gas_m3': row.get('GemiddeldAardgasverbruikTotaal_55'),
                    'gem_elektra_kwh': row.get('GemiddeldeElektriciteitsleveringTotaal_47'),
                    'stedelijkheid': row.get('MateVanStedelijkheid_116'),
                }
    except:
        pass
    return {}


def bereken_eigendom(woning, cbs_data=None, perceel_telling=None):
    """
    Bereken eigendom kans voor één woning.
    
    Args:
        woning: Dict met woning data
        cbs_data: CBS buurtdata
        perceel_telling: Dict met {perceelnummer: [woningen]} voor gedeelde percelen
    """
    cbs_huur_pct = cbs_data.get('pct_huur') if cbs_data else None
    cbs_huur_factor = int(cbs_huur_pct / 10) if cbs_huur_pct else 0

    aantal_in_pand = woning.get('aantal_in_pand', 1) or 1
    is_flat = aantal_in_pand > 3
    woningtype = (woning.get('woningtype') or '').lower()
    perceel_opp = woning.get('perceel_opp_m2') or 0
    
    # Check of perceel gedeeld wordt met meerdere woningen
    perceel_gedeeld = woning.get('perceel_gedeeld_door', 1) or 1

    signalen = []

    # Vrijstaand/2^1kap (alleen als max 2 op perceel)
    if perceel_gedeeld <= 2 and ('vrijstaand' in woningtype or '2^1kap' in woningtype or '2-onder' in woningtype or 'twee-onder' in woningtype):
        woning['eigendom'] = 'KOOP'
        woning['eigendom_kans'] = 95
        woning['kans_koop'] = 95
        woning['kans_huur'] = 5
        signalen.append("vrijstaand/2^1kap")
    # Gedeeld perceel met meerdere woningen = waarschijnlijk huur
    elif perceel_gedeeld > 2:
        huur_kans = min(95, 80 + cbs_huur_factor)
        koop_kans = 100 - huur_kans
        woning['eigendom'] = 'HUUR'
        woning['eigendom_kans'] = huur_kans
        woning['kans_koop'] = koop_kans
        woning['kans_huur'] = huur_kans
        signalen.append(f"{perceel_gedeeld} adressen/perceel")
    # Flat/hoogbouw
    elif is_flat:
        huur_kans = min(95, 50 + (cbs_huur_factor * 5))
        koop_kans = 100 - huur_kans
        woning['eigendom'] = 'HUUR' if huur_kans >= 50 else 'KOOP'
        woning['eigendom_kans'] = max(huur_kans, koop_kans)
        woning['kans_koop'] = koop_kans
        woning['kans_huur'] = huur_kans
        signalen.append("flat/appartement")
    # Laagbouw met eigen perceel
    else:
        koop_kans = max(30, 70 - cbs_huur_factor)
        huur_kans = 100 - koop_kans
        woning['eigendom'] = 'KOOP' if koop_kans >= 50 else 'HUUR'
        woning['eigendom_kans'] = max(koop_kans, huur_kans)
        woning['kans_koop'] = koop_kans
        woning['kans_huur'] = huur_kans
        signalen.append("eigen perceel")

    if cbs_huur_pct:
        signalen.append(f"CBS {cbs_huur_pct}% huur")

    woning['signalen'] = signalen
    return woning


def bouw_cbs_html(cbs_data, buurtnaam, buurtcode):
    """Bouw CBS panel HTML"""
    if not cbs_data:
        return f'<div class="cb-title">📊 {buurtnaam or "Onbekend"}</div><div style="color:#64748b;font-size:12px;margin-top:8px">Geen CBS data</div>'

    def fmt_woz(v):
        if v and v > 1000:
            return f"€{round(v/1000)}k"
        return f"€{v}k" if v else "-"

    g = cbs_data.get
    link = f'<a href="https://kadastralekaart.com/buurten/{buurtcode}" target="_blank" style="color:#6ea8fe;text-decoration:none;font-size:11px">🔗 kadastralekaart.com</a>' if buurtcode else ''

    return f'''
    <div class="cb-title">📊 {buurtnaam} ({buurtcode})</div>
    {link}
    <div class="cb-section">
        <div class="cb-label">Bevolking</div>
        <div class="cb-grid"><span>{g("inwoners","-")}</span><span class="cb-dim">{g("bevolkingsdichtheid","-")}/km²</span></div>
    </div>
    <div class="cb-section">
        <div class="cb-label">Huishoudens</div>
        <div class="cb-grid"><span>{g("huishoudens","-")}</span><span class="cb-dim">gem. {g("gem_huishoudgrootte","-")}</span></div>
        <div class="cb-bar-wrap"><div class="cb-bar" style="width:{g("pct_eenpersoons",0)}%;background:#64748b"></div><span>Alleen {g("pct_eenpersoons",0)}%</span></div>
    </div>
    <div class="cb-section">
        <div class="cb-label">Woningen</div>
        <div class="cb-split"><div class="cb-chip koop">Koop {g("pct_koop","-")}%</div><div class="cb-chip huur">Huur {g("pct_huur","-")}%</div></div>
        <div class="cb-grid"><span class="cb-dim">corp. {g("pct_huur_corporatie","-")}%</span><span class="cb-dim">WOZ {fmt_woz(g("gem_woz_k"))}</span></div>
    </div>
    <div class="cb-section" style="border:none">
        <div class="cb-label">Energie</div>
        <div class="cb-grid"><span class="cb-dim">Gas {g("gem_gas_m3","-")} m³</span><span class="cb-dim">Elek {g("gem_elektra_kwh","-")} kWh</span></div>
    </div>
    '''


# ══════════════════════════════════════════════════════════════════════════════
# HTML GENERATOR
# ══════════════════════════════════════════════════════════════════════════════
def genereer_live_html(titel, center_lat, center_lon, centrum_lat, centrum_lon, straal, ws_host="localhost"):
    """Genereer de live HTML met config"""
    script_dir = Path(__file__).parent
    template_path = script_dir / TEMPLATE_FILE

    if not template_path.exists():
        raise FileNotFoundError(f"Template niet gevonden: {template_path}")

    html = template_path.read_text(encoding='utf-8')

    config = {
        'ws_port': WS_PORT,
        'ws_host': ws_host,
        'center_lat': center_lat,
        'center_lon': center_lon,
        'centrum_lat': centrum_lat,
        'centrum_lon': centrum_lon,
        'straal': straal,
        'google_maps_api_key': GOOGLE_MAPS_API_KEY,
    }

    html = html.replace('%%TITEL%%', titel)
    html = html.replace('%%CONFIG_JSON%%', json.dumps(config))

    output_path = script_dir / OUTPUT_HTML
    output_path.write_text(html, encoding='utf-8')
    return str(output_path)


# ══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET SERVER + DATA PIPELINE
# ══════════════════════════════════════════════════════════════════════════════
class LiveDataCollector:
    def __init__(self, straat, huisnr, plaats, straal=200, detect_solar=True, host="localhost"):
        self.straat = straat
        self.huisnr = huisnr
        self.plaats = plaats
        self.straal = straal
        self.host = host
        self.detect_solar = detect_solar and YOLO_AVAILABLE
        self.clients = set()
        self.woningen = []
        self.cbs_data = {}
        self.new_search_request = None
        self._abort_collect = False

        # Initialize solar detector
        if self.detect_solar:
            self.solar_detector = ZonnepanelenDetector()
        else:
            self.solar_detector = None

    async def broadcast(self, message):
        """Stuur bericht naar alle connected clients"""
        if self.clients:
            msg = json.dumps(message, ensure_ascii=False)
            await asyncio.gather(*[client.send(msg) for client in self.clients])

    async def handle_client(self, websocket):
        """Handle een WebSocket client connectie"""
        self.clients.add(websocket)
        try:
            # Stuur init bericht
            await websocket.send(json.dumps({
                'type': 'init',
                'total_expected': '~' + str(int(3.14 * (self.straal/50)**2 * 10))  # Rough estimate
            }))

            # Verwerk inkomende berichten
            async for message in websocket:
                try:
                    data = json.loads(message)
                    if data.get('type') == 'start_solar':
                        print("☀️ Zonnepanelen scan gestart via browser")
                        self.solar_trigger.set()
                    elif data.get('type') == 'new_search':
                        print(f"🔍 Nieuwe scan aangevraagd via browser: {data}")
                        self.new_search_request = data
                        self.new_search_event.set()
                        # Onderbreek ook eventuele solar wacht
                        self.solar_trigger.set()
                        self._abort_collect = True
                except Exception:
                    pass
        finally:
            self.clients.discard(websocket)

    async def collect_data(self):
        """Main data collection pipeline - runs async"""
        print("\n" + "="*60)
        print("🏘️  BUURT DATA VERZAMELAAR - LIVE")
        print("="*60)
        print(f"📍 {self.straat} {self.huisnr}, {self.plaats}")
        print(f"📏 Straal: {self.straal}m")
        print("="*60 + "\n")

        # Wacht even tot client connect
        await asyncio.sleep(1)
        while not self.clients:
            await asyncio.sleep(0.2)

        await self.broadcast({'type': 'status', 'text': 'Locatie opzoeken...'})

        # 1. Locatie opzoeken
        try:
            cx, cy = get_rd_from_address(self.straat, self.huisnr, self.plaats)
            buurtcode, buurtnaam = get_buurtcode(cx, cy)
            centrum_lat, centrum_lon = rd_to_latlon(cx, cy)
            print(f"📍 Buurt: {buurtnaam} ({buurtcode})")
        except Exception as e:
            await self.broadcast({'type': 'error', 'text': f'Adres niet gevonden: {e}'})
            return

        # 2. CBS data ophalen (parallel)
        if self._abort_collect: return
        await self.broadcast({'type': 'status', 'text': 'CBS buurtdata ophalen...'})
        self.cbs_data = get_cbs_buurt(buurtcode)
        if self.cbs_data:
            cbs_html = bouw_cbs_html(self.cbs_data, buurtnaam, buurtcode)
            await self.broadcast({'type': 'cbs', 'html': cbs_html})

        # 3. BAG woningen ophalen
        if self._abort_collect: return
        await self.broadcast({'type': 'status', 'text': 'Woningen ophalen uit BAG...'})
        try:
            self.woningen = get_bag_woningen(cx, cy, self.straal)
            print(f"✓ {len(self.woningen)} woningen gevonden")
        except Exception as e:
            await self.broadcast({'type': 'error', 'text': f'BAG fout: {e}'})
            return

        # 4. Verstuur basis woningen in batches
        if self._abort_collect: return
        await self.broadcast({'type': 'status', 'text': f'{len(self.woningen)} woningen laden...'})
        batch_size = 25
        for i in range(0, len(self.woningen), batch_size):
            if self._abort_collect: return
            batch = self.woningen[i:i+batch_size]
            for w in batch:
                bereken_eigendom(w, self.cbs_data, None)
            await self.broadcast({
                'type': 'batch',
                'data': batch,
                'status': f'Basis data... {min(i+batch_size, len(self.woningen))}/{len(self.woningen)}'
            })
            await asyncio.sleep(0.05)

        # 5. Verrijk met perceel + energielabel (alleen laagbouw)
        if self._abort_collect: return
        laagbouw = [w for w in self.woningen if w['bouwtype'] == 'LAAGBOUW']
        print(f"📐 Perceeldata ophalen voor {len(laagbouw)} laagbouw woningen...")

        for i, w in enumerate(laagbouw):
            if self._abort_collect: return
            perceel = get_perceel_data(w['rd_x'], w['rd_y'])
            if perceel:
                if perceel.get('oppervlakte'):
                    w['perceel_opp_m2'] = int(float(perceel['oppervlakte']))
                if perceel.get('perceelnummer'):
                    w['perceelnummer'] = perceel['perceelnummer']
                if perceel.get('kadastrale_aanduiding'):
                    w['kadastrale_aanduiding'] = perceel['kadastrale_aanduiding']

            if w.get('postcode'):
                label = get_energielabel(
                    w['postcode'], w['huisnr'],
                    w['huisletter'] or None, w['toevoeging'] or None
                )
                if label:
                    w['energielabel'] = label.get('energieklasse')
                    if label.get('gebouwtype'):
                        w['woningtype'] = label['gebouwtype']

            if i % 10 == 0:
                await asyncio.sleep(0.1)
                await self.broadcast({
                    'type': 'status',
                    'text': f'Perceeldata ophalen... {i+1}/{len(laagbouw)}'
                })

        # Tel hoeveel woningen per perceelnummer
        if self._abort_collect: return
        perceel_telling = defaultdict(list)
        for w in laagbouw:
            perceelnr = w.get('perceelnummer')
            if perceelnr:
                perceel_telling[perceelnr].append(w)

        for perceelnr, groep in perceel_telling.items():
            aantal = len(groep)
            for w in groep:
                w['perceel_gedeeld_door'] = aantal

        for w in laagbouw:
            if self._abort_collect: return
            bereken_eigendom(w, self.cbs_data, perceel_telling)
            await self.broadcast({
                'type': 'batch',
                'data': [w],
                'status': f'Verrijkt {laagbouw.index(w)+1}/{len(laagbouw)}'
            })

        # 6. Zonnepanelen detectie — wacht op knop in browser
        if self.detect_solar and self.solar_detector and self.solar_detector.model:
            kandidaten = [w for w in self.woningen if w['bouwtype'] == 'LAAGBOUW']

            # Stuur solar_ready → browser toont knop
            await self.broadcast({
                'type': 'solar_ready',
                'count': len(kandidaten),
            })
            print(f"☀️ {len(kandidaten)} woningen klaar voor solar scan — wacht op bevestiging in browser...")

            # Wacht tot gebruiker op de knop drukt (max 10 min)
            try:
                await asyncio.wait_for(self.solar_trigger.wait(), timeout=600)
            except asyncio.TimeoutError:
                print("⏱️ Solar scan timeout — overgeslagen")
                await self.broadcast({'type': 'complete'})
                return

            # Afgebroken door nieuwe scan aanvraag?
            if self._abort_collect:
                print("🔄 Solar scan overgeslagen — nieuwe scan aangevraagd")
                return

            await self.broadcast({'type': 'status', 'text': f'Zonnepanelen scannen... 0/{len(kandidaten)}'})
            print(f"☀️ Zonnepanelen detectie gestart voor {len(kandidaten)} woningen...")

            detected_count = 0
            for i, w in enumerate(kandidaten):
                result = self.solar_detector.detect_at_location(w['lat'], w['lon'])
                w['zonnepanelen'] = result.get('detected', False)
                w['zonnepanelen_confidence'] = result.get('confidence', 0)
                w['zonnepanelen_count'] = result.get('count', 0)

                if result.get('detected'):
                    detected_count += 1

                await self.broadcast({
                    'type': 'solar_update',
                    'index': self.woningen.index(w),
                    'data': {
                        'zonnepanelen': w['zonnepanelen'],
                        'zonnepanelen_confidence': w['zonnepanelen_confidence'],
                        'zonnepanelen_count': w['zonnepanelen_count'],
                    }
                })

                if (i + 1) % 5 == 0:
                    await self.broadcast({
                        'type': 'status',
                        'text': f'Zonnepanelen scannen... {i+1}/{len(kandidaten)}',
                        'progress': f'{detected_count} gevonden'
                    })
                    await asyncio.sleep(0.05)

            print(f"   ☀️ {detected_count} woningen met zonnepanelen gedetecteerd")

        # 7. Klaar!
        print(f"\n✅ Klaar! {len(self.woningen)} woningen verzameld")
        await self.broadcast({'type': 'complete'})

    async def run(self):
        """Start WebSocket server en data collection"""
        # Initialiseer asyncio events binnen de event loop
        self.solar_trigger = asyncio.Event()
        self.new_search_event = asyncio.Event()

        # Genereer HTML
        try:
            cx, cy = get_rd_from_address(self.straat, self.huisnr, self.plaats)
            centrum_lat, centrum_lon = rd_to_latlon(cx, cy)
        except:
            centrum_lat, centrum_lon = 52.0, 5.0  # Fallback

        titel = f"Buurtdata {self.straat} {self.huisnr}, {self.plaats}"
        html_path = genereer_live_html(titel, centrum_lat, centrum_lon,
                                       centrum_lat, centrum_lon, self.straal,
                                       ws_host=self.host)

        # Start WebSocket server (luister op alle interfaces als host geen localhost is)
        ws_bind = "0.0.0.0" if self.host != "localhost" else "localhost"
        print(f"🌐 WebSocket server op ws://{self.host}:{WS_PORT}")
        server = await websockets.serve(self.handle_client, ws_bind, WS_PORT)

        # Start HTTP server zodat andere apparaten de kaart kunnen openen
        http_bind = "0.0.0.0" if self.host != "localhost" else "localhost"
        html_dir = str(Path(html_path).parent)

        class QuietHandler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=html_dir, **kwargs)
            def log_message(self, format, *args):
                pass  # Geen HTTP logs in terminal

        http_server = socketserver.TCPServer((http_bind, HTTP_PORT), QuietHandler)
        http_server.allow_reuse_address = True
        http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        http_thread.start()

        if self.host != "localhost":
            print(f"🌍 Netwerk URL: http://{self.host}:{HTTP_PORT}/buurt_kaart.html")
            print(f"   (open deze URL op andere apparaten in je netwerk)")

        # Open browser lokaal
        local_url = f"http://localhost:{HTTP_PORT}/buurt_kaart.html"
        print(f"🌐 Browser openen: {local_url}")
        webbrowser.open(local_url)

        # Herhaalbare scan loop: start scan, wacht op new_search, start opnieuw
        while True:
            # Reset state voor deze ronde
            self.woningen = []
            self.cbs_data = {}
            self.solar_trigger.clear()
            self.new_search_event.clear()
            self._abort_collect = False

            await self.collect_data()

            # Wacht op nieuwe scan aanvraag vanuit browser (of Ctrl+C)
            print("\n💡 Wacht op nieuwe scan via browser, of Ctrl+C om te stoppen.")
            try:
                await self.new_search_event.wait()
            except asyncio.CancelledError:
                break

            # Verwerk nieuwe scan aanvraag
            req = self.new_search_request
            if req:
                if req.get('straat'):
                    self.straat = req['straat']
                    self.huisnr = str(req.get('huisnr', ''))
                    self.plaats = req.get('plaats', self.plaats)
                elif req.get('lat') and req.get('lon'):
                    # Kaartpunt: reverse geocode naar straat/huisnr
                    try:
                        url = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse"
                        r = requests.get(url, params={
                            "lat": req['lat'], "lon": req['lon'],
                            "type": "adres", "rows": 1,
                            "fl": "straatnaam,huisnummer,woonplaatsnaam"
                        }, timeout=10)
                        docs = r.json().get("response", {}).get("docs", [])
                        if docs:
                            self.straat = docs[0].get('straatnaam', 'Onbekend')
                            self.huisnr = str(docs[0].get('huisnummer', '1'))
                            self.plaats = docs[0].get('woonplaatsnaam', self.plaats)
                    except Exception as e:
                        print(f"⚠️ Reverse geocode fout: {e}")

                if req.get('straal'):
                    self.straal = int(req['straal'])

            print(f"\n🔄 Nieuwe scan: {self.straat} {self.huisnr}, {self.plaats} ({self.straal}m)")

            # Bereken nieuw centrum voor kaartupdate
            try:
                cx, cy = get_rd_from_address(self.straat, self.huisnr, self.plaats)
                nieuw_lat, nieuw_lon = rd_to_latlon(cx, cy)
            except Exception as e:
                print(f"⚠️ Centrum berekening fout: {e}")
                nieuw_lat = req.get('lat') if req else None
                nieuw_lon = req.get('lon') if req else None

            # Stuur reset + nieuw centrum + straal naar browser
            await self.broadcast({
                'type': 'new_search_started',
                'lat': nieuw_lat,
                'lon': nieuw_lon,
                'straal': self.straal,
                'label': f"{self.straat} {self.huisnr}, {self.plaats}",
            })
            # Directe statusfeedback
            await self.broadcast({'type': 'status', 'text': f'Nieuwe scan: {self.straat} {self.huisnr}, {self.plaats}...'})

        server.close()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def start_live(straat, huisnr, plaats, straal=200, detect_solar=True, host="localhost"):
    """Start de live data collector"""
    collector = LiveDataCollector(straat, huisnr, plaats, straal, detect_solar, host=host)
    try:
        asyncio.run(collector.run())
    except KeyboardInterrupt:
        print("\n👋 Gestopt.")


if __name__ == "__main__":
    # === CONFIGURATIE ===
    STRAAT = "Slangenburg"
    HUISNR = "62"
    PLAATS = "Barneveld"
    STRAAL = 100
    DETECT_SOLAR = True  # Zonnepanelen detectie aan/uit

    # IP adres voor lokaal netwerk (bijv. "192.168.1.10")
    # Gebruik "localhost" als je alleen op dezelfde machine werkt
    HOST = "192.168.0.85"

    start_live(STRAAT, HUISNR, PLAATS, STRAAL, DETECT_SOLAR, host=HOST)