import urllib.request
import urllib.parse
import json
import ssl
import csv
import math
import folium
from pyproj import Transformer

# -----------------------------
# Helpers
# -----------------------------
def get_pdok_data(url):
    context = ssl._create_unverified_context()
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Python-Script'})
        with urllib.request.urlopen(req, context=context) as response:
            return response.read().decode('utf-8')
    except Exception as e:
        print(f"Fout bij verbinding: {e}")
        return None

def afstand(x1, y1, x2, y2):
    return math.sqrt((x1 - x2)**2 + (y1 - y2)**2)


transformer = Transformer.from_crs("EPSG:28992", "EPSG:4326", always_xy=True)

def rd_to_latlon(x, y):
    lon, lat = transformer.transform(x, y)
    return lat, lon

def maak_kaart_preview(center_x, center_y, straal, features, bestandsnaam):
    center_lat, center_lon = rd_to_latlon(center_x, center_y)

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=16,
        tiles="OpenStreetMap"
    )

    # Middelpunt
    folium.Marker(
        [center_lat, center_lon],
        popup="Centraal adres",
        icon=folium.Icon(color="red")
    ).add_to(m)

    # Cirkel (straal)
    folium.Circle(
        radius=straal,
        location=[center_lat, center_lon],
        color="blue",
        fill=True,
        fill_opacity=0.15
    ).add_to(m)

    # Adressen
    for f in features:
        geom = f.get("geometry")
        if not geom:
            continue

        x, y = geom["coordinates"]
        lat, lon = rd_to_latlon(x, y)

        folium.CircleMarker(
            location=[lat, lon],
            radius=3,
            color="orange",
            fill=True,
            fill_opacity=0.7
        ).add_to(m)

    html_naam = f"{bestandsnaam}.html"
    m.save(html_naam)
    print(f"Kaart-preview opgeslagen als {html_naam}")

# -----------------------------
# Main
# -----------------------------
def buurt_scan():
    base_ls = "https://api.pdok.nl/bzk/locatieserver/search/v3_1"

    print("\n--- PDOK Buurt Scan (BBOX + cirkel) ---")
    straat = input("Centraal adres - Straat: ").strip()
    huisnr = input("Centraal adres - Huisnr: ").strip()
    plaats = input("Centraal adres - Plaats: ").strip()
    straal = float(input("Straal in meters (bijv. 250): ").strip())

    bestandsnaam = input("Bestandsnaam (zonder .csv): ").strip()
    if not bestandsnaam:
        bestandsnaam = "buurt_adressen"
    csv_naam = f"{bestandsnaam}.csv"

    # -----------------------------
    # 1. Coördinaten middelpunt
    # -----------------------------
    params_ls = urllib.parse.urlencode({
        'q': f'"{straat}" {huisnr} {plaats}',
        'fl': 'centroide_rd,weergavenaam',
        'rows': 1
    })

    raw = get_pdok_data(f"{base_ls}/free?{params_ls}")
    if not raw:
        print("Adres niet gevonden."); return

    data = json.loads(raw)
    docs = data.get('response', {}).get('docs', [])
    if not docs:
        print("Adres niet gevonden."); return

    coord = docs[0]['centroide_rd'].replace('POINT(', '').replace(')', '')
    center_x, center_y = map(float, coord.split())

    print(f"Startpunt: {docs[0]['weergavenaam']}")
    print("Scannen gestart...")

    # -----------------------------
    # 2. BBOX berekenen
    # -----------------------------
    minx = center_x - straal
    maxx = center_x + straal
    miny = center_y - straal
    maxy = center_y + straal

    # -----------------------------
    # 3. BAG WFS ophalen (alles in bbox)
    # -----------------------------
    wfs_params = urllib.parse.urlencode({
        'service': 'WFS',
        'version': '2.0.0',
        'request': 'GetFeature',
        'typeName': 'bag:verblijfsobject',
        'outputFormat': 'application/json',
        'bbox': f"{minx},{miny},{maxx},{maxy},EPSG:28992"
    })

    raw = get_pdok_data(
        f"https://service.pdok.nl/lv/bag/wfs/v2_0?{wfs_params}"
    )
    if not raw:
        print("Geen BAG-data ontvangen."); return

    wfs = json.loads(raw)
    features = wfs.get('features', [])
    maak_kaart_preview(center_x, center_y, straal, features, bestandsnaam)


    if not features:
        print("Geen adressen gevonden."); return

    # -----------------------------
    # 4. Filter op cirkel + opslaan
    # -----------------------------
    results = []
    headers = [
        'Straat', 'Huisnr', 'Postcode', 'Plaats',
        'Bouwjaar', 'm2', 'Gebruik',
        'Status', 'Pandstatus', 'Afstand_m'
    ]

    for f in features:
        geom = f.get('geometry')
        props = f.get('properties')

        if not geom or not props:
            continue

        x, y = geom['coordinates']
        dist = afstand(center_x, center_y, x, y)

        if dist <= straal:
            row = [
                props.get('openbare_ruimte'),
                props.get('huisnummer'),
                props.get('postcode'),
                props.get('woonplaats'),
                props.get('bouwjaar'),
                props.get('oppervlakte'),
                props.get('gebruiksdoel'),
                props.get('status'),
                props.get('pandstatus'),
                round(dist, 1)
            ]
            results.append(row)

    # -----------------------------
    # 5. CSV opslaan
    # -----------------------------
    with open(csv_naam, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f, delimiter=';')
        writer.writerow(headers)
        writer.writerows(results)

    print(f"\nKlaar! {len(results)} adressen opgeslagen in {csv_naam}.")

# -----------------------------
if __name__ == "__main__":
    buurt_scan()
