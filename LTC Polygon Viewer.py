import streamlit as st
import geopandas as gpd
import pandas as pd
import folium
from streamlit_folium import st_folium
from shapely.geometry import mapping
import tempfile
import requests
import io

st.set_page_config(layout="wide", page_title="Farm Polygon Viewer (GitHub only)")

st.title("Farm Polygon Viewer — GitHub raw URLs only")
st.markdown(
    """
    This app **only** loads files from GitHub (raw links) or any direct raw HTTP(S) URL — no local uploads.

    Requirements:
    - Provide a **raw** GitHub URL to your KML file (the GitHub *raw* URL, not the GitHub HTML page).
    - Provide a **raw** GitHub URL to your Excel file (xlsx).

    Functionality:
    - Shows only polygons whose KML `Name` first 8 characters match `FarmerCode` in the Excel.
    - Filter by Farmer Code (prefix), Village, or Group.
    - Map auto-zooms to the results and supports pan/zoom.

    Note: this app is read-only and only intended for viewing polygons — there is no export or download functionality.
    """
)

# ----------------- Helpers -----------------
@st.cache_data
def download_file_to_temp(url: str) -> str:
    """Download URL content to a temporary file and return its path."""
    resp = requests.get(url)
    resp.raise_for_status()
    suffix = None
    if url.lower().endswith('.kml') or '.kml?' in url.lower():
        suffix = '.kml'
    elif url.lower().endswith('.zip'):
        suffix = '.zip'
    elif url.lower().endswith('.xlsx') or '.xls' in url.lower():
        suffix = '.xlsx'
    else:
        # fallback
        suffix = ''
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(resp.content)
    tmp.flush()
    tmp.close()
    return tmp.name

@st.cache_data
def read_kml_from_url(url: str) -> gpd.GeoDataFrame:
    tmp_path = download_file_to_temp(url)
    try:
        gdf = gpd.read_file(tmp_path, driver='KML')
    except Exception:
        gdf = gpd.read_file(tmp_path)
    # Some KMLs use lowercase 'name'
    if 'Name' not in gdf.columns and 'name' in gdf.columns:
        gdf = gdf.rename(columns={'name': 'Name'})
    # Ensure Name exists
    if 'Name' not in gdf.columns:
        gdf['Name'] = gdf.index.astype(str)
    gdf['Name'] = gdf['Name'].astype(str)
    return gdf

@st.cache_data
def read_excel_from_url(url: str) -> pd.DataFrame:
    resp = requests.get(url)
    resp.raise_for_status()
    bio = io.BytesIO(resp.content)
    df = pd.read_excel(bio, engine='openpyxl')
    df.columns = [c.strip() for c in df.columns]
    return df

@st.cache_data
def prepare_data(_kml_gdf: gpd.GeoDataFrame, groups_df: pd.DataFrame):
    df = groups_df.copy()
    # find FarmerCode column (case-insensitive)
    farmer_col = None
    for col in df.columns:
        if col.strip().lower() in ['farmercode', 'farmer_code', 'code', 'farmer code']:
            farmer_col = col
            break
    if farmer_col is None:
        # fallback: try first column
        farmer_col = df.columns[0]

    df[farmer_col] = df[farmer_col].astype(str).str.upper().str.strip()

    kg = _kml_gdf.copy()
    kg['Name'] = kg['Name'].astype(str)
    kg['code8'] = kg['Name'].str[:8].str.upper().str.strip()

    valid_codes = set(df[farmer_col])
    kg = kg[kg['code8'].isin(valid_codes)].reset_index(drop=True)

    # Merge on code8 -> farmer_col to attach group/village info to polygons
    kg = kg.merge(df, left_on='code8', right_on=farmer_col, how='left', suffixes=(None, '_excel'))

    # Ensure CRS is WGS84
    if kg.crs is None:
        kg = kg.set_crs('epsg:4326')
    else:
        kg = kg.to_crs(epsg=4326)

    # do not simplify here — simplification happens later (user can toggle performance mode)
    return kg, df, farmer_col


def _count_coords(geom):
    """Count number of coordinate tuples in a geometry (handles Multi* and Polygons)."""
    if geom is None:
        return 0
    geom_type = getattr(geom, 'geom_type', None)
    if geom_type == 'Polygon':
        c = len(geom.exterior.coords)
        for interior in geom.interiors:
            c += len(interior.coords)
        return c
    if geom_type and geom_type.startswith('Multi'):
        total = 0
        for part in geom.geoms:
            total += _count_coords(part)
        return total
    # fallback: try coords
    try:
        return len(list(geom.coords))
    except Exception:
        return 0


@st.cache_data
def simplify_geometries(_kg: gpd.GeoDataFrame, high_perf: bool):
    """Return a copy of _kg with a 'geometry_simp' column. Uses auto tolerance based on vertex count when high_perf is True."""
    kg = _kg.copy()
    if not high_perf:
        kg['geometry_simp'] = kg.geometry
        return kg

    # estimate complexity
    total_coords = 0
    for geom in kg.geometry:
        total_coords += _count_coords(geom)

    # choose tolerance heuristically (degrees). These are conservative defaults.
    if total_coords > 20000:
        tol = 0.0005
    elif total_coords > 5000:
        tol = 0.0001
    elif total_coords > 1000:
        tol = 0.00002
    else:
        tol = 0.0

    if tol > 0.0:
        kg['geometry_simp'] = kg.geometry.simplify(tolerance=tol, preserve_topology=True)
    else:
        kg['geometry_simp'] = kg.geometry
    return kg


import numpy as np

def folium_map_for_gdf(gdf: gpd.GeoDataFrame, popup_fields=None, initial_zoom=12):
    """Render a folium Map from a GeoDataFrame. Expects a precomputed 'geometry_simp' column for fast rendering.

    Builds a proper GeoJSON FeatureCollection using shapely.geometry.mapping so there are
    no raw geometry objects left for the JSON encoder to trip on.
    """
    if len(gdf) == 0:
        m = folium.Map(location=[0,0], zoom_start=2)
        return m
    # use simplified geometry column if present
    if 'geometry_simp' in gdf.columns:
        gdf = gdf.set_geometry('geometry_simp')

    bounds = gdf.total_bounds  # minx, miny, maxx, maxy
    minx, miny, maxx, maxy = bounds
    center_lat = (miny + maxy) / 2
    center_lon = (minx + maxx) / 2
    m = folium.Map(location=[center_lat, center_lon], zoom_start=initial_zoom)

    # Create popup configuration (fast: use GeoJsonPopup with fields)
    if popup_fields is None:
        popup_fields = ['Name', 'code8']

    # Build a GeoJSON FeatureCollection using mapping(...) for geometries and plain Python types for properties
    features = []
    geom_col = gdf.geometry.name
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None:
            continue
        geom_json = mapping(geom)
        # properties: all columns except geometry
        props = {}
        for c in gdf.columns:
            if c == geom_col:
                continue
            v = row.get(c)
            if pd.isna(v):
                props[c] = None
            else:
                # convert numpy scalars to Python scalars
                try:
                    if hasattr(v, 'item'):
                        props[c] = v.item()
                    else:
                        props[c] = v
                except Exception:
                    props[c] = str(v)
        features.append({
            'type': 'Feature',
            'geometry': geom_json,
            'properties': props
        })

    geojson = { 'type': 'FeatureCollection', 'features': features }

    try:
        gj = folium.GeoJson(
            geojson,
            name='polygons',
            tooltip=folium.GeoJsonTooltip(fields=['Name'], aliases=['Name:']),
            style_function=lambda feature: {
                'fillColor': '#ffff66',
                'color': '#0000ff',
                'weight': 2,
                'fillOpacity': 0.3,
            },
        )
        # Attach popup if fields exist in properties
        existing_fields = [f for f in (popup_fields or []) if f in gdf.columns]
        if existing_fields:
            gj.add_child(folium.features.GeoJsonPopup(fields=existing_fields, labels=True, localize=True, parse_html=False))
        gj.add_to(m)
    except Exception:
        # fallback to adding features one-by-one (shouldn't normally happen now)
        for feat in features:
            folium.GeoJson(
                feat,
                style_function=lambda feature: {
                    'fillColor': '#ffff66',
                    'color': '#0000ff',
                    'weight': 2,
                    'fillOpacity': 0.3,
                },
                popup=folium.Popup(str(feat.get('properties', {})), max_width=300)
            ).add_to(m)

    padding = 0.01
    m.fit_bounds([[miny - padding, minx - padding], [maxy + padding, maxx + padding]])

    return m

# ----------------- Sidebar (GitHub-only) -----------------
st.sidebar.header("GitHub raw URLs (required)")

# Auto-populated raw URLs for your repo (you can overwrite these if you move files)
_raw_base = "https://raw.githubusercontent.com/tuyishimeandrew/LTC-Polygon-Viewer/main"
_kml_default = f"{_raw_base}/SurveyCTO%20Inspection%20Polygons.kml"
_excel_default = f"{_raw_base}/Group%20Polygons.xlsx"

kml_url = st.sidebar.text_input('KML raw URL (e.g. https://raw.githubusercontent.com/your/repo/main/SurveyCTO Inspection Polygons.kml)', value=_kml_default)
excel_url = st.sidebar.text_input('Excel raw URL (e.g. https://raw.githubusercontent.com/your/repo/main/Group Polygons.xlsx)', value=_excel_default)

st.sidebar.markdown('---')
st.sidebar.markdown('**Tips:** Use the GitHub *raw* URL (click Raw on the file page). For private repos you must provide a link that your server can access.')

if not kml_url or not excel_url:
    st.info('Please paste both the KML raw URL and the Excel raw URL in the left sidebar.')
    st.stop()

# ----------------- Load and prepare -----------------
try:
    kml_gdf = read_kml_from_url(kml_url)
    groups_df = read_excel_from_url(excel_url)
except Exception as e:
    st.error(f'Error downloading or reading files: {e}')
    st.stop()

try:
    kg, df_excel, farmer_col = prepare_data(kml_gdf, groups_df)
except Exception as e:
    st.error(f'Error preparing data: {e}')
    st.stop()

# Performance mode: enable automatic simplification for faster map rendering
st.sidebar.markdown('---')
high_perf = st.sidebar.checkbox('Enable high-performance mode (auto-simplify geometries for faster display)', value=True)
# Simplify geometries according to performance setting (cached)
kg = simplify_geometries(kg, high_perf)

# prepare popup fields (include farmer_col and common columns if present)
popup_fields = ['Name', 'code8']
if farmer_col and farmer_col in df_excel.columns:
    popup_fields.append(farmer_col)
for c in ['Village', 'village', 'Group', 'group']:
    if c in df_excel.columns and c not in popup_fields:
        popup_fields.append(c)

show_sample = st.sidebar.checkbox('Show sample of data (first rows)')
if show_sample:
    st.subheader('KML polygons (sample)')
    st.write(kg.head())
    st.subheader('Excel (sample)')
    st.write(df_excel.head())

# Filters
st.sidebar.header('Filter results')
search_code = st.sidebar.text_input('Search Farmer Code (type prefix or full code)', '').strip().upper()

village_col = None
group_col = None
for c in df_excel.columns:
    if c.strip().lower() in ['village', 'village_name']:
        village_col = c
    if c.strip().lower() in ['group', 'group_name']:
        group_col = c

villages = sorted(df_excel[village_col].dropna().astype(str).unique()) if village_col else []
groups = sorted(df_excel[group_col].dropna().astype(str).unique()) if group_col else []

village_sel = st.sidebar.selectbox('Select Village (optional)', options=['(any)'] + villages)
group_sel = st.sidebar.selectbox('Select Group (optional)', options=['(any)'] + groups)

# apply filters
filtered = kg.copy()
if search_code:
    filtered = filtered[filtered['code8'].str.startswith(search_code)]
if village_col and village_sel != '(any)':
    filtered = filtered[filtered[village_col].astype(str) == village_sel]
if group_col and group_sel != '(any)':
    filtered = filtered[filtered[group_col].astype(str) == group_sel]

st.sidebar.markdown(f"Matching polygons: **{len(filtered)}**")

# Map display
st.subheader('Map view')
# If no polygons in the processed set, show a clear message
if kg is None or len(kg) == 0:
    st.warning('No polygons available to display from the provided files.')
else:
    # If filters produce zero results, show all available polygons by default (fast UX)
    if len(filtered) == 0:
        st.info('No filters matched — showing all available polygons.')
        display_gdf = kg
    else:
        display_gdf = filtered

    # Render the map (uses simplified geometries for speed)
    m = folium_map_for_gdf(display_gdf, popup_fields=popup_fields)
    st_folium(m, width=1000, height=700)

# Footer
st.markdown('---')
st.markdown("**Notes:** The app matches the first 8 characters of the KML `Name` field to the Excel `FarmerCode`. If your KML has a different name field, update the code to use the correct property.")
