"""
ZONNEPANELEN YOLO MODEL TRAINER
===============================
Script om je eigen zonnepanelen detectie model te trainen.

WORKFLOW:
=========
1. Verzamel satellietbeelden:
   python train_zonnepanelen.py collect --lat 52.2195 --lon 5.1869 --radius 400

2. Label de beelden met LabelImg:
   pip install labelImg
   labelImg ./training_tiles
   
   - Open Dir: ./training_tiles
   - Change Save Dir: ./training_tiles  
   - Format: YOLO (linksboven)
   - Per foto: W = teken box, type "solar_panel", Ctrl+S, D = volgende

3. Maak classes.txt file:
   python train_zonnepanelen.py prepare --data ./training_tiles

4. Train het model:
   python train_zonnepanelen.py train --data ./training_tiles --epochs 100

5. Test het model:
   python train_zonnepanelen.py test --model zonnepanelen_yolo.pt --image test.jpg

Vereist: pip install ultralytics pillow requests python-dotenv
"""

import argparse
import math
import io
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("❌ requests niet geïnstalleerd. Run: pip install requests")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("❌ pillow niet geïnstalleerd. Run: pip install pillow")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("⚠️  python-dotenv niet geïnstalleerd. Run: pip install python-dotenv")

YOLO = None
try:
    from ultralytics import YOLO
except ImportError:
    print("⚠️  ultralytics niet geïnstalleerd - training niet beschikbaar")
    print("   Run: pip install ultralytics")


# API Keys
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

# Tile sources
PDOK_WMTS_URL = "https://service.pdok.nl/hwh/luchtfotorgb/wmts/v1_0"
GOOGLE_STATIC_URL = "https://maps.googleapis.com/maps/api/staticmap"


# ══════════════════════════════════════════════════════════════════════════════
# SATELLITE TILE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_google_satellite_image(lat, lon, size_meters=50, output_size=640, zoom=20):
    """
    Haal satellietbeeld op van Google Maps Static API.
    Veel scherper dan PDOK!
    """
    if not GOOGLE_MAPS_API_KEY:
        print("❌ GOOGLE_MAPS_API_KEY niet gevonden in .env bestand!")
        return None
    
    params = {
        "center": f"{lat},{lon}",
        "zoom": zoom,
        "size": f"{output_size}x{output_size}",
        "maptype": "satellite",
        "key": GOOGLE_MAPS_API_KEY,
    }
    
    try:
        r = requests.get(GOOGLE_STATIC_URL, params=params, timeout=15)
        if r.status_code == 200:
            img = Image.open(io.BytesIO(r.content))
            # Check of het geen error image is
            if img.size[0] == output_size:
                return img
            else:
                print(f"      ⚠️  Onverwachte image size: {img.size}")
                return None
        else:
            print(f"      ⚠️  Google API error: {r.status_code}")
            return None
    except Exception as e:
        print(f"      ⚠️  Google download error: {e}")
        return None


def lat_lon_to_tile(lat, lon, zoom):
    """Converteer lat/lon naar tile x,y coordinaten"""
    n = 2 ** zoom
    tile_x = int((lon + 180) / 360 * n)
    lat_rad = math.radians(lat)
    tile_y = int((1 - math.asinh(math.tan(lat_rad)) / math.pi) / 2 * n)
    return tile_x, tile_y


def download_tile(tile_x, tile_y, zoom=19):
    """Download een enkele PDOK luchtfoto tile"""
    url = f"{PDOK_WMTS_URL}/Actueel_orthoHR/EPSG:3857/{zoom}/{tile_x}/{tile_y}.jpeg"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return Image.open(io.BytesIO(r.content))
    except Exception as e:
        print(f"      ⚠️  Download error: {e}")
    return None


def get_satellite_image(lat, lon, size_meters=50, output_size=640):
    """
    Haal satellietbeeld op rond een locatie.
    Stitcht meerdere tiles samen en cropt naar gewenste grootte.
    """
    zoom = 19  # ~0.3m per pixel bij zoom 19
    
    # Haal center tile
    center_x, center_y = lat_lon_to_tile(lat, lon, zoom)
    
    # Download 3x3 grid van tiles
    tiles = {}
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            tile = download_tile(center_x + dx, center_y + dy, zoom)
            if tile:
                tiles[(dx, dy)] = tile
    
    if not tiles:
        return None
    
    # Stitch tiles samen (3x3 * 256px = 768x768)
    stitched = Image.new('RGB', (768, 768), (0, 0, 0))
    for (dx, dy), tile in tiles.items():
        x = (dx + 1) * 256
        y = (dy + 1) * 256
        stitched.paste(tile, (x, y))
    
    # Crop centrum en resize
    # Bij zoom 19: ~0.3m per pixel, dus 50m = ~167 pixels
    meters_per_pixel = 0.3
    crop_pixels = int(size_meters / meters_per_pixel)
    half = crop_pixels // 2
    
    center = 384  # Midden van 768
    left = max(0, center - half)
    top = max(0, center - half)
    right = min(768, center + half)
    bottom = min(768, center + half)
    
    cropped = stitched.crop((left, top, right, bottom))
    resized = cropped.resize((output_size, output_size), Image.LANCZOS)
    
    return resized


# ══════════════════════════════════════════════════════════════════════════════
# COLLECT COMMAND
# ══════════════════════════════════════════════════════════════════════════════

def cmd_collect(args):
    """Verzamel satellietbeelden in een grid"""
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Determine source
    use_google = GOOGLE_MAPS_API_KEY and not args.pdok
    
    # Grid berekening
    # ~111km per graad lat, ~70km per graad lon in NL
    lat_step = args.step / 111000
    lon_step = args.step / 70000
    steps = int(args.radius / args.step)
    total = (2 * steps + 1) ** 2
    
    print("=" * 60)
    print("📡 SATELLIETBEELDEN VERZAMELEN")
    print("=" * 60)
    print(f"   Bron: {'Google Maps (scherp!)' if use_google else 'PDOK'}")
    print(f"   Centrum: {args.lat}, {args.lon}")
    print(f"   Straal: {args.radius}m")
    print(f"   Grid stap: {args.step}m")
    print(f"   Tiles: {total}")
    print(f"   Output: {output_dir}")
    if use_google:
        estimated_cost = total * 0.002  # ~€0.002 per tile
        print(f"   Geschatte kosten: €{estimated_cost:.2f}")
    print("=" * 60)
    print()
    
    if not use_google and not GOOGLE_MAPS_API_KEY:
        print("💡 Tip: Voeg GOOGLE_MAPS_API_KEY toe aan .env voor scherpere beelden!")
        print()
    
    count = 0
    errors = 0
    start_time = time.time()
    
    for i in range(-steps, steps + 1):
        for j in range(-steps, steps + 1):
            lat = args.lat + i * lat_step
            lon = args.lon + j * lon_step
            
            current = (i + steps) * (2 * steps + 1) + (j + steps) + 1
            
            # Get satellite image
            if use_google:
                img = get_google_satellite_image(lat, lon, size_meters=args.step, output_size=640, zoom=20)
            else:
                img = get_satellite_image(lat, lon, size_meters=args.step, output_size=640)
            
            if img:
                # Convert to RGB if needed (Google returns PNG palette mode sometimes)
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                filename = f"tile_{lat:.6f}_{lon:.6f}.jpg"
                img.save(output_dir / filename, quality=95)
                count += 1
                print(f"   [{current:3d}/{total}] ✓ {filename}")
            else:
                errors += 1
                print(f"   [{current:3d}/{total}] ✗ Geen beeld")
            
            # Rate limiting
            time.sleep(0.1 if use_google else 0.05)
    
    elapsed = time.time() - start_time
    
    print()
    print("=" * 60)
    print(f"✅ KLAAR!")
    print(f"   {count} beelden opgeslagen")
    print(f"   {errors} fouten")
    print(f"   Tijd: {elapsed:.1f}s")
    print("=" * 60)
    print()
    print("📝 VOLGENDE STAPPEN:")
    print()
    print("   1. Installeer LabelImg:")
    print("      pip install labelImg")
    print()
    print("   2. Start LabelImg:")
    print("      labelImg")
    print()
    print("   3. In LabelImg:")
    print(f"      - Open Dir → {output_dir}")
    print(f"      - Change Save Dir → {output_dir}")
    print("      - Linksboven: kies 'YOLO' format")
    print()
    print("   4. Label elke foto:")
    print("      - W = teken rechthoek rond zonnepanelen")
    print("      - Type class naam: solar_panel")
    print("      - Ctrl+S = opslaan")
    print("      - D = volgende foto")
    print()
    print("   5. Als je ~100+ fotos hebt gelabeld:")
    print(f"      python train_zonnepanelen.py prepare --data {output_dir}")


# ══════════════════════════════════════════════════════════════════════════════
# PREPARE COMMAND
# ══════════════════════════════════════════════════════════════════════════════

def cmd_prepare(args):
    """Bereid dataset voor: maak train/val split en classes.txt"""
    import random
    import shutil
    
    data_dir = Path(args.data)
    
    print("=" * 60)
    print("📦 DATASET VOORBEREIDEN")
    print("=" * 60)
    
    # Check of er gelabelde data is
    txt_files = list(data_dir.glob("*.txt"))
    txt_files = [f for f in txt_files if f.name != "classes.txt"]
    jpg_files = list(data_dir.glob("*.jpg")) + list(data_dir.glob("*.jpeg")) + list(data_dir.glob("*.png"))
    
    print(f"   Gevonden: {len(jpg_files)} afbeeldingen")
    print(f"   Gevonden: {len(txt_files)} label files")
    
    if len(txt_files) == 0:
        print()
        print("❌ Geen label files gevonden!")
        print("   Heb je de afbeeldingen al gelabeld met LabelImg?")
        print("   Zorg dat je YOLO format hebt geselecteerd (linksboven in LabelImg)")
        return
    
    # Maak classes.txt als die niet bestaat
    classes_file = data_dir / "classes.txt"
    if not classes_file.exists():
        classes_file.write_text("solar_panel\n")
        print("   ✓ classes.txt aangemaakt")
    
    # Maak directory structuur
    images_train = data_dir / "images" / "train"
    images_val = data_dir / "images" / "val"
    labels_train = data_dir / "labels" / "train"
    labels_val = data_dir / "labels" / "val"
    
    for d in [images_train, images_val, labels_train, labels_val]:
        d.mkdir(parents=True, exist_ok=True)
    
    # Verzamel image+label pairs
    pairs = []
    for img_path in jpg_files:
        label_path = img_path.with_suffix('.txt')
        if label_path.exists():
            pairs.append((img_path, label_path))
    
    print(f"   Gevonden: {len(pairs)} image+label pairs")
    
    if len(pairs) < 10:
        print()
        print("⚠️  Minder dan 10 gelabelde afbeeldingen.")
        print("   Voor goede resultaten heb je minimaal 50-100 nodig.")
    
    # Shuffle en split 80/20
    random.shuffle(pairs)
    split_idx = int(len(pairs) * 0.8)
    train_pairs = pairs[:split_idx]
    val_pairs = pairs[split_idx:]
    
    print(f"   Train set: {len(train_pairs)} afbeeldingen")
    print(f"   Val set: {len(val_pairs)} afbeeldingen")
    print()
    
    # Kopieer files
    for img_path, label_path in train_pairs:
        shutil.copy2(img_path, images_train / img_path.name)
        shutil.copy2(label_path, labels_train / label_path.name)
    
    for img_path, label_path in val_pairs:
        shutil.copy2(img_path, images_val / img_path.name)
        shutil.copy2(label_path, labels_val / label_path.name)
    
    # Maak dataset.yaml
    yaml_content = f"""# Zonnepanelen Dataset
# Gegenereerd door train_zonnepanelen.py

path: {data_dir.absolute()}
train: images/train
val: images/val

# Classes
names:
  0: solar_panel

nc: 1
"""
    yaml_path = data_dir / "dataset.yaml"
    yaml_path.write_text(yaml_content)
    
    print("✅ Dataset voorbereid!")
    print()
    print(f"   📁 {images_train} ({len(train_pairs)} files)")
    print(f"   📁 {images_val} ({len(val_pairs)} files)")
    print(f"   📄 {yaml_path}")
    print()
    print("📝 VOLGENDE STAP:")
    print(f"   python train_zonnepanelen.py train --data {data_dir}")


# ══════════════════════════════════════════════════════════════════════════════
# TRAIN COMMAND
# ══════════════════════════════════════════════════════════════════════════════

def cmd_train(args):
    """Train YOLO model"""
    if YOLO is None:
        print("❌ ultralytics niet geïnstalleerd!")
        print("   Run: pip install ultralytics")
        return
    
    data_dir = Path(args.data)
    yaml_path = data_dir / "dataset.yaml"
    
    if not yaml_path.exists():
        print(f"❌ dataset.yaml niet gevonden in {data_dir}")
        print(f"   Run eerst: python train_zonnepanelen.py prepare --data {data_dir}")
        return
    
    print("=" * 60)
    print("🚀 MODEL TRAINEN")
    print("=" * 60)
    print(f"   Dataset: {data_dir}")
    print(f"   Model: YOLOv8{args.size}")
    print(f"   Epochs: {args.epochs}")
    print(f"   Output: {args.output}")
    print("=" * 60)
    print()
    
    # Laad pretrained model
    model = YOLO(f'yolov8{args.size}.pt')
    
    # Train
    results = model.train(
        data=str(yaml_path),
        epochs=args.epochs,
        imgsz=640,
        batch=16 if args.size in ['n', 's'] else 8,
        name='zonnepanelen',
        patience=20,
        save=True,
        plots=True,
        verbose=True,
    )
    
    # Kopieer beste model
    best_model = Path("runs/detect/zonnepanelen/weights/best.pt")
    if best_model.exists():
        import shutil
        shutil.copy(best_model, args.output)
        print()
        print("=" * 60)
        print(f"✅ MODEL OPGESLAGEN: {args.output}")
        print("=" * 60)
        print()
        print("📝 GEBRUIK:")
        print(f"   1. Kopieer {args.output} naar dezelfde map als buurt_live.py")
        print(f"   2. Run: python buurt_live.py")
        print(f"   3. Zonnepanelen worden nu automatisch gedetecteerd!")
    else:
        print("⚠️  Kon beste model niet vinden in runs/detect/zonnepanelen/weights/")


# ══════════════════════════════════════════════════════════════════════════════
# TEST COMMAND
# ══════════════════════════════════════════════════════════════════════════════

def cmd_test(args):
    """Test model op een afbeelding"""
    if YOLO is None:
        print("❌ ultralytics niet geïnstalleerd!")
        return
    
    if not Path(args.model).exists():
        print(f"❌ Model niet gevonden: {args.model}")
        return
    
    if not Path(args.image).exists():
        print(f"❌ Afbeelding niet gevonden: {args.image}")
        return
    
    print(f"🔍 Testen {args.model} op {args.image}...")
    print()
    
    model = YOLO(args.model)
    results = model(args.image, conf=args.conf, save=True)
    
    for r in results:
        if len(r.boxes) == 0:
            print("   Geen zonnepanelen gedetecteerd")
        else:
            print(f"   {len(r.boxes)} detectie(s):")
            for box in r.boxes:
                conf = float(box.conf[0])
                cls = int(box.cls[0])
                name = model.names.get(cls, f"class_{cls}")
                print(f"      - {name}: {conf:.1%} confidence")
    
    print()
    print("📁 Resultaat opgeslagen in: runs/detect/predict/")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Zonnepanelen YOLO Model Trainer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Voorbeelden:
  %(prog)s collect --lat 52.22 --lon 5.18 --radius 400
  %(prog)s prepare --data ./training_tiles
  %(prog)s train --data ./training_tiles --epochs 100
  %(prog)s test --model zonnepanelen_yolo.pt --image test.jpg
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Commando')
    
    # COLLECT
    p_collect = subparsers.add_parser('collect', help='Verzamel satellietbeelden')
    p_collect.add_argument('--lat', type=float, required=True, help='Centrum latitude (bijv. 52.22)')
    p_collect.add_argument('--lon', type=float, required=True, help='Centrum longitude (bijv. 5.18)')
    p_collect.add_argument('--radius', type=int, default=400, help='Straal in meters (default: 400)')
    p_collect.add_argument('--step', type=int, default=50, help='Grid stap in meters (default: 50)')
    p_collect.add_argument('--output', type=str, default='./training_tiles', help='Output directory')
    p_collect.add_argument('--pdok', action='store_true', help='Gebruik PDOK ipv Google Maps')
    
    # PREPARE
    p_prepare = subparsers.add_parser('prepare', help='Bereid dataset voor na labelen')
    p_prepare.add_argument('--data', type=str, required=True, help='Directory met gelabelde data')
    
    # TRAIN
    p_train = subparsers.add_parser('train', help='Train YOLO model')
    p_train.add_argument('--data', type=str, required=True, help='Dataset directory')
    p_train.add_argument('--epochs', type=int, default=100, help='Aantal epochs (default: 100)')
    p_train.add_argument('--size', type=str, default='n', choices=['n', 's', 'm', 'l', 'x'],
                        help='Model grootte: n=nano, s=small, m=medium (default: n)')
    p_train.add_argument('--output', type=str, default='zonnepanelen_yolo.pt', help='Output model naam')
    
    # TEST
    p_test = subparsers.add_parser('test', help='Test model op afbeelding')
    p_test.add_argument('--model', type=str, required=True, help='Model .pt file')
    p_test.add_argument('--image', type=str, required=True, help='Test afbeelding')
    p_test.add_argument('--conf', type=float, default=0.25, help='Confidence threshold (default: 0.25)')
    
    args = parser.parse_args()
    
    if args.command == 'collect':
        cmd_collect(args)
    elif args.command == 'prepare':
        cmd_prepare(args)
    elif args.command == 'train':
        cmd_train(args)
    elif args.command == 'test':
        cmd_test(args)
    else:
        parser.print_help()
        print()
        print("=" * 60)
        print("QUICK START:")
        print("=" * 60)
        print()
        print("1. Verzamel beelden van een wijk met zonnepanelen:")
        print("   python train_zonnepanelen.py collect --lat 52.22 --lon 5.18 --radius 400")
        print()
        print("2. Label de beelden (installeer eerst: pip install labelImg):")
        print("   labelImg ./training_tiles")
        print()
        print("3. Bereid dataset voor:")
        print("   python train_zonnepanelen.py prepare --data ./training_tiles")
        print()
        print("4. Train het model:")
        print("   python train_zonnepanelen.py train --data ./training_tiles")
        print()


if __name__ == "__main__":
    main()