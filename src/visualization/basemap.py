"""
Base map for every folium page in this project.

OpenStreetMap's volunteer tile servers block requests from local HTML files (no
Referer, see https://operations.osmfoundation.org/policies/tiles/), and CARTO now
needs an API key. The Dutch government's PDOK BRT-Achtergrondkaart is open data,
free, keyless and meant for this kind of use; it covers the Netherlands (and a
margin around it), which is where EHHV flights are.

Alternatives (grey, aerial photo, Esri street map) are added hidden; they appear
in maps that have a LayerControl.
"""

import folium

PDOK = 'https://service.pdok.nl/brt/achtergrondkaart/wmts/v2_0/{layer}/EPSG:3857/{{z}}/{{x}}/{{y}}.png'
PDOK_ATTR = 'Kaartgegevens &copy; <a href="https://www.kadaster.nl">Kadaster</a> / PDOK (CC BY 4.0)'
PDOK_PHOTO = 'https://service.pdok.nl/hwh/luchtfotorgb/wmts/v1_0/Actueel_orthoHR/EPSG:3857/{z}/{x}/{y}.jpeg'
ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}'
ESRI_ATTR = 'Tiles &copy; Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors, and the GIS user community'


def add_basemaps(m, default='standaard'):
    """Add the base layers to an existing folium map (created with tiles=None)."""
    layers = [
        ('Kaart (PDOK)', PDOK.format(layer='standaard'), PDOK_ATTR, 19),
        ('Kaart grijs (PDOK)', PDOK.format(layer='grijs'), PDOK_ATTR, 19),
        ('Luchtfoto (PDOK)', PDOK_PHOTO, PDOK_ATTR, 19),
        ('Street map (Esri)', ESRI, ESRI_ATTR, 19),
    ]
    names = {'standaard': 'Kaart (PDOK)', 'grijs': 'Kaart grijs (PDOK)', 'luchtfoto': 'Luchtfoto (PDOK)',
             'esri': 'Street map (Esri)'}
    first = names.get(default, 'Kaart (PDOK)')
    for name, url, attr, max_zoom in sorted(layers, key=lambda l: l[0] != first):
        folium.TileLayer(tiles=url, attr=attr, name=name, max_zoom=max_zoom, max_native_zoom=max_zoom,
                         overlay=False, control=True, show=(name == first)).add_to(m)
    return m


def make_map(location, zoom_start=13, default='standaard', **kwargs):
    """folium.Map with the PDOK base map (keyless, allowed from local files)."""
    kwargs.pop('tiles', None)
    m = folium.Map(location=location, zoom_start=zoom_start, tiles=None, **kwargs)
    return add_basemaps(m, default)
