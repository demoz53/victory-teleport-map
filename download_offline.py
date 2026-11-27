"""download_offline.py: save a copy of everything the teleport map loads from the web, so it works offline.

    python download_offline.py                    # day and night maps up to zoom 15 (about 44,000 small tiles)
    python download_offline.py --max-zoom 16      # more detail up close (about 175,000 tiles)
    python download_offline.py --layers day       # the day map only (half the tiles)
    python download_offline.py --dry-run          # only say how many tiles it would fetch

It saves, into the `offline` folder next to this script:
  leaflet/        the Leaflet map library (cdnjs)
  data/, locales/, images/icons/   the marker lists, their names and icons (jeanropke's GTA VI map)
  tiles/<layer>/<z>/<x>_<y>.jpg    the map tiles (the CDN behind that map)
  manifest.json   written last; modmap.py uses the copy once it is there

Run it again to resume or to add a layer or zoom level: files already saved are skipped.
Beyond the saved zoom the page enlarges the last saved level. The copy holds Rockstar Games' map imagery and
the community map's data: keep it for your own use, do not commit or share it.
Python 3.8 or later, standard library only.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TILES = "https://map-tiles.b-cdn.net/assets/gtavi/"
DATA = "https://jeanropke.github.io/GTAVIMap/"
LEAFLET = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/"
UA = {"User-Agent": "victory-teleport-map offline copy (Python urllib)"}
# The whole game map is one zoom-8 tile, (127, 127); zoom z has 4^(z-8) tiles under it.
BASE_ZOOM, BASE_TILE = 8, 127


def fetch(url, tries=4):
    """The body, or None for a 404. Retries other failures with a growing pause."""
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = e
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {err}")


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def get_file(out, base, rel, force=False):
    """Saves base+rel at out/rel (skipped when already there) and returns its bytes."""
    path = os.path.join(out, *rel.split("/"))
    if not force and os.path.isfile(path):
        with open(path, "rb") as f:
            return f.read()
    data = fetch(base + rel)
    if data is None:
        raise RuntimeError(f"{base + rel}: not found")
    save(path, data)
    return data


def tiles_at(layer, z):
    n = 1 << (z - BASE_ZOOM)
    for x in range(BASE_TILE * n, BASE_TILE * n + n):
        for y in range(BASE_TILE * n, BASE_TILE * n + n):
            yield f"{layer}/{z}/{x}_{y}.jpg"


def tiles_for(layers, max_zoom):
    for layer in layers:
        for z in range(BASE_ZOOM, max_zoom + 1):
            yield from tiles_at(layer, z)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(HERE, "offline"), help="where to save (default: ./offline)")
    ap.add_argument("--max-zoom", type=int, default=15, choices=range(8, 18), metavar="8-17", help="most detailed zoom to save (default 15)")
    ap.add_argument("--layers", default="day,night", help="day, night or day,night (default)")
    ap.add_argument("--workers", type=int, default=4, choices=range(1, 9), metavar="1-8", help="downloads at a time (default 4)")
    ap.add_argument("--dry-run", action="store_true", help="only count what would be fetched")
    ap.add_argument("--refresh-data", action="store_true", help="fetch the marker lists and names again (they change as the map grows)")
    a = ap.parse_args()
    layers = [l for l in a.layers.split(",") if l in ("day", "night")]
    if not layers:
        ap.error("--layers must be day, night or day,night")
    out = os.path.abspath(a.out)
    tiles = list(tiles_for(layers, a.max_zoom))
    tdir = os.path.join(out, "tiles")
    missing_path = os.path.join(tdir, "missing.txt")
    missing = set()
    if os.path.isfile(missing_path):
        with open(missing_path, encoding="utf-8") as f:
            missing = {l.strip() for l in f if l.strip()}
    todo = [t for t in tiles if t not in missing and not os.path.isfile(os.path.join(tdir, *t.split("/")))]
    print(f"{len(tiles)} tiles for {'+'.join(layers)} up to zoom {a.max_zoom}; {len(todo)} still to fetch (most are 0.5-5 KB)")
    if a.dry_run:
        return 0

    print("Leaflet ...", flush=True)
    for f in ("leaflet.min.js", "leaflet.min.css"):
        get_file(os.path.join(out, "leaflet"), LEAFLET, f)

    print("marker lists, names and icons ...", flush=True)
    ds = json.loads(get_file(out, DATA, "data/datasets.json", a.refresh_data))
    get_file(out, DATA, "locales/en.json", a.refresh_data)
    get_file(out, DATA, "data/gtavi/regions.json", a.refresh_data)
    icons = set()
    for d in ds.get("datasets", []):
        rel = d.get("path", "")
        if not re.fullmatch(r"data/gtavi/native-[\w-]+\.json", rel):  # only the files the page reads
            continue
        for cat in json.loads(get_file(out, DATA, rel, a.refresh_data)):
            icon = cat.get("icon") if isinstance(cat, dict) else None
            if isinstance(icon, str) and re.fullmatch(r"[\w-]+", icon):
                icons.add(icon)
    for icon in sorted(icons):
        try:
            get_file(out, DATA, f"images/icons/{icon}.png")
        except RuntimeError as e:
            print(f"  icon skipped: {e}")
    print(f"  {len(icons)} icons", flush=True)

    print(f"tiles ({a.workers} at a time; Ctrl+C stops, running it again resumes) ...", flush=True)
    done = gone = 0
    t0 = time.time()

    def one(t):
        data = fetch(TILES + t)
        if data is None:
            return t, False
        save(os.path.join(tdir, *t.split("/")), data)
        return t, True

    try:
        with concurrent.futures.ThreadPoolExecutor(a.workers) as ex:
            for t, ok in ex.map(one, todo):
                if ok:
                    done += 1
                else:
                    gone += 1
                    missing.add(t)
                if (done + gone) % 500 == 0:
                    rate = (done + gone) / max(time.time() - t0, 0.1)
                    print(f"  {done + gone}/{len(todo)}  ({rate:.0f} a second)", flush=True)
    finally:
        os.makedirs(tdir, exist_ok=True)
        with open(missing_path, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(missing)))
    print(f"  {done} tiles saved, {gone} do not exist on the server", flush=True)

    # The manifest says what is complete on disk: each layer up to the last zoom it has every tile of.
    have = lambda t: t in missing or os.path.isfile(os.path.join(tdir, *t.split("/")))
    zooms = {}
    for layer in ("day", "night"):
        z = BASE_ZOOM - 1
        while z < 17 and all(have(t) for t in tiles_at(layer, z + 1)):
            z += 1
        if z >= BASE_ZOOM:
            zooms[layer] = z
    if not zooms:
        print("no layer is complete yet; run it again")
        return 1
    man = {"maxZoom": min(zooms.values()), "layers": list(zooms), "zoomByLayer": zooms,
           "saved": datetime.datetime.now().isoformat(timespec="seconds"),
           "sources": {"tiles": TILES, "data": DATA, "leaflet": LEAFLET}}
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1)
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(out) for f in fs)
    print(f"done: {out} ({size / 1e6:.0f} MB). modmap.py now uses this copy; --online makes it use the web again.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped; run it again to resume")
        sys.exit(1)
