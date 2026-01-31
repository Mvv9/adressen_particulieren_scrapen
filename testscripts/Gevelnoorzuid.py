"""
Test: Geveloriëntatie met VISUELE PLOT
======================================
Plot het perceel, adres-punt, centrum, en de bepaalde voorgevel
"""
import requests
import math
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch
import numpy as np

LOCATIESERVER = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
KADASTER_WFS = "https://service.pdok.nl/kadaster/kadastralekaart/wfs/v5_0"

def get_rd_from_address(straat, huisnr, plaats):
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

def get_perceel_polygon(x, y):
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
    if r.status_code == 200:
        data = r.json()
        features = data.get("features", [])
        if features:
            return features[0]
    return None

def hoek_naar_richting(hoek):
    richtingen = ["N", "NO", "O", "ZO", "Z", "ZW", "W", "NW"]
    return richtingen[int((hoek + 22.5) / 45) % 8]

def plot_gevel_analyse(straat, huisnr, plaats, save_path=None):
    """Maak een visuele plot van de gevelanalyse"""
    
    print(f"\n{'='*60}")
    print(f"🏠 {straat} {huisnr}, {plaats}")
    print("="*60)
    
    # Haal adres coordinaten
    adres_x, adres_y, naam = get_rd_from_address(straat, huisnr, plaats)
    if not adres_x:
        print("❌ Adres niet gevonden")
        return
    
    print(f"✓ Adres: {naam}")
    print(f"   Adres centroïde (RD): {adres_x:.1f}, {adres_y:.1f}")
    
    # Haal perceel
    perceel = get_perceel_polygon(adres_x, adres_y)
    if not perceel:
        print("❌ Perceel niet gevonden")
        return
    
    props = perceel.get("properties", {})
    geometry = perceel.get("geometry")
    
    print(f"✓ Perceel: {props.get('perceelnummer')}, {props.get('kadastraleGrootteWaarde')} m²")
    
    # Extract coords
    coords = geometry.get("coordinates", [])
    if geometry.get("type") == "Polygon":
        coords = coords[0]
    elif geometry.get("type") == "MultiPolygon":
        coords = coords[0][0]
    
    # Bereken centrum
    n = len(coords) - 1
    centrum_x = sum(c[0] for c in coords[:-1]) / n
    centrum_y = sum(c[1] for c in coords[:-1]) / n
    
    print(f"   Perceel centrum: {centrum_x:.1f}, {centrum_y:.1f}")
    
    # Richting centrum -> adres
    dx = adres_x - centrum_x
    dy = adres_y - centrum_y
    straat_hoek = math.degrees(math.atan2(dx, dy))
    if straat_hoek < 0:
        straat_hoek += 360
    
    print(f"   Richting naar adres: {hoek_naar_richting(straat_hoek)} ({straat_hoek:.1f}°)")
    
    # Bereken alle zijdes
    zijdes = []
    for i in range(len(coords) - 1):
        p1 = coords[i]
        p2 = coords[i + 1]
        lengte = math.sqrt((p2[0]-p1[0])**2 + (p2[1]-p1[1])**2)
        midden = ((p1[0]+p2[0])/2, (p1[1]+p2[1])/2)
        
        # Richting van centrum naar midden zijde
        dx_z = midden[0] - centrum_x
        dy_z = midden[1] - centrum_y
        zijde_richting = math.degrees(math.atan2(dx_z, dy_z))
        if zijde_richting < 0:
            zijde_richting += 360
        
        # Hoek zijde zelf
        hoek_zijde = math.degrees(math.atan2(p2[0]-p1[0], p2[1]-p1[1]))
        if hoek_zijde < 0:
            hoek_zijde += 360
        
        # Verschil met straat-richting
        hoek_verschil = abs(zijde_richting - straat_hoek)
        if hoek_verschil > 180:
            hoek_verschil = 360 - hoek_verschil
        
        zijdes.append({
            "p1": p1, "p2": p2,
            "lengte": lengte,
            "midden": midden,
            "hoek_zijde": hoek_zijde,
            "zijde_richting": zijde_richting,
            "hoek_verschil": hoek_verschil
        })
    
    # Sorteer op lengte
    zijdes_sorted = sorted(zijdes, key=lambda z: z["lengte"])
    kortste = zijdes_sorted[0]["lengte"]
    langste = zijdes_sorted[-1]["lengte"]
    ratio = langste / kortste if kortste > 0 else 1
    
    # RIJTJESHUIS: lang en smal perceel
    # - KORTE zijdes = voorkant/achterkant (straat en tuin)
    # - LANGE zijdes = zijkanten (grenzen aan buren links/rechts)
    #
    # VRIJSTAAND: meer vierkant perceel
    # - KORTE zijdes = voorkant/achterkant
    # - LANGE zijdes = zijkanten
    #
    # Dus in BEIDE gevallen: korte zijdes zijn voor/achter!
    
    kandidaten = zijdes_sorted[:2]  # 2 kortste zijdes = voor en achter
    
    if ratio > 3:
        perceel_type = "RIJTJESHUIS"
    else:
        perceel_type = "VRIJSTAAND"
    
    # Voorgevel = kandidaat met kleinste hoek_verschil (meest richting adres/straat)
    # MAAR: als adres en centrum te dicht bij elkaar liggen, gebruik andere methode
    afstand_adres_centrum = math.sqrt((adres_x - centrum_x)**2 + (adres_y - centrum_y)**2)
    
    if afstand_adres_centrum < 2:
        # Adres en centrum liggen te dicht op elkaar - gebruik Y-coordinaat
        # Bij de meeste straten in NL: straat ligt aan de kant met LAGERE of HOGERE Y
        # We kiezen de korte zijde met het LAAGSTE midden-Y (meest zuidelijk = vaak straat)
        print(f"   ⚠️ Adres en centrum liggen dicht bij elkaar ({afstand_adres_centrum:.1f}m)")
        print(f"   → Fallback: gebruik meest zuidelijke korte zijde als voorgevel")
        voorgevel = min(kandidaten, key=lambda z: z["midden"][1])  # laagste Y
    else:
        voorgevel = min(kandidaten, key=lambda z: z["hoek_verschil"])
    
    print(f"\n📏 Perceeltype: {perceel_type} (ratio {ratio:.1f}x)")
    print(f"\n📐 Zijdes:")
    for i, z in enumerate(zijdes_sorted):
        marker = " ← VOORGEVEL" if z == voorgevel else ""
        is_kandidaat = " [kandidaat]" if z in kandidaten else ""
        print(f"   {i+1}. {z['lengte']:5.1f}m | richting: {z['zijde_richting']:5.1f}° | "
              f"verschil: {z['hoek_verschil']:5.1f}°{is_kandidaat}{marker}")
    
    # ============ PLOT ============
    fig, ax = plt.subplots(1, 1, figsize=(12, 10))
    
    # Plot perceel polygon
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    ax.fill(xs, ys, alpha=0.3, color='lightblue', edgecolor='blue', linewidth=2)
    ax.plot(xs, ys, 'b-', linewidth=2, label='Perceel')
    
    # Plot alle zijdes met kleur op basis van hoek_verschil
    for z in zijdes:
        # Kleur: groen = straatkant, rood = achterkant
        color_val = z["hoek_verschil"] / 180  # 0 = groen, 1 = rood
        color = plt.cm.RdYlGn_r(color_val)
        
        ax.plot([z["p1"][0], z["p2"][0]], [z["p1"][1], z["p2"][1]], 
                color=color, linewidth=4, alpha=0.7)
        
        # Label met lengte
        ax.annotate(f'{z["lengte"]:.1f}m', z["midden"], 
                   fontsize=9, ha='center', va='bottom',
                   bbox=dict(boxstyle='round', facecolor='white', alpha=0.7))
    
    # Markeer voorgevel extra dik
    ax.plot([voorgevel["p1"][0], voorgevel["p2"][0]], 
            [voorgevel["p1"][1], voorgevel["p2"][1]], 
            color='green', linewidth=8, alpha=0.8, label='VOORGEVEL')
    
    # Plot adres centroïde (GROOT, ROOD)
    ax.scatter([adres_x], [adres_y], c='red', s=200, zorder=10, 
               marker='*', label=f'Adres centroïde')
    ax.annotate(f'ADRES\n({adres_x:.0f}, {adres_y:.0f})', 
               (adres_x, adres_y), fontsize=10, ha='left', va='bottom',
               xytext=(5, 5), textcoords='offset points',
               bbox=dict(boxstyle='round', facecolor='yellow', alpha=0.8))
    
    # Plot perceel centrum (BLAUW)
    ax.scatter([centrum_x], [centrum_y], c='blue', s=150, zorder=10, 
               marker='o', label='Perceel centrum')
    ax.annotate(f'CENTRUM\n({centrum_x:.0f}, {centrum_y:.0f})', 
               (centrum_x, centrum_y), fontsize=10, ha='right', va='top',
               xytext=(-5, -5), textcoords='offset points',
               bbox=dict(boxstyle='round', facecolor='lightblue', alpha=0.8))
    
    # Pijl van centrum naar adres (richting straat)
    ax.annotate('', xy=(adres_x, adres_y), xytext=(centrum_x, centrum_y),
               arrowprops=dict(arrowstyle='->', color='purple', lw=3))
    
    # Midpoint voor label
    mid_x = (centrum_x + adres_x) / 2
    mid_y = (centrum_y + adres_y) / 2
    ax.annotate(f'→ STRAAT ({straat_hoek:.0f}°)', (mid_x, mid_y),
               fontsize=10, color='purple', fontweight='bold',
               bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))
    
    # Kompas toevoegen
    kompas_x = ax.get_xlim()[1] - 5
    kompas_y = ax.get_ylim()[1] - 5
    ax.annotate('N', (kompas_x, kompas_y), fontsize=14, fontweight='bold', ha='center')
    ax.annotate('↑', (kompas_x, kompas_y - 2), fontsize=20, ha='center')
    
    # Labels
    ax.set_xlabel('RD X (m)', fontsize=12)
    ax.set_ylabel('RD Y (m)', fontsize=12)
    ax.set_title(f'{straat} {huisnr}, {plaats}\n'
                f'Perceel: {props.get("perceelnummer")} | '
                f'Type: {perceel_type} | '
                f'Voorgevel kijkt naar: {hoek_naar_richting((voorgevel["hoek_zijde"]+90)%360)}',
                fontsize=14, fontweight='bold')
    
    ax.legend(loc='upper left')
    ax.set_aspect('equal')
    ax.grid(True, alpha=0.3)
    
    # Zoom iets uit
    margin = 5
    ax.set_xlim(min(xs) - margin, max(xs) + margin)
    ax.set_ylim(min(ys) - margin, max(ys) + margin)
    
    plt.tight_layout()
    
    # Save
    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"\n💾 Plot opgeslagen: {save_path}")
    
    plt.show()
    
    return {
        "voorgevel": voorgevel,
        "straat_hoek": straat_hoek,
        "perceel_type": perceel_type
    }


if __name__ == "__main__":
    # Test Slangenburg 59
    plot_gevel_analyse("Slangenburg", "62", "Barneveld", 
                      save_path="gevel_slangenburg59.png")  # Lokaal opslaan