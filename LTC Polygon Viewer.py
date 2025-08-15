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
def add_geometries(_kg: gpd.GeoDataFrame, simplify_enabled: bool):
    """Return a copy of _kg with 'original_geometry' and 'simplified_geometry' columns."""
    kg = _kg.copy()
    kg['original_geometry'] = kg.geometry
    if simplify_enabled:
        # estimate complexity
        total_coords = sum(_count_coords(geom) for geom in kg.geometry)

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
            kg['simplified_geometry'] = kg.geometry.simplify(tolerance=tol, preserve_topology=True)
        else:
            kg['simplified_geometry'] = kg.geometry
    else:
        kg['simplified_geometry'] = kg.geometry

    # Drop original geometry to avoid confusion, use the two new columns
    kg = kg.drop(columns=['geometry'])
    return kg


def folium_map_for_gdf(gdf: gpd.GeoDataFrame, popup_fields=None, initial_zoom=12):
    """Render a folium Map from a GeoDataFrame using active geometry."""
    if len(gdf) == 0:
        m = folium.Map(location=[0,0], zoom_start=2)
        return m

    bounds = gdf.total_bounds  # minx, miny, maxx, maxy
    minx, miny, maxx, maxy = bounds
    center_lat = (miny + maxy) / 2
    center_lon = (minx + maxx) / 2
    m = folium.Map(location=[center_lat, center_lon], zoom_start=initial_zoom)

    # Create popup configuration (fast: use GeoJsonPopup with fields)
    if popup_fields is None:
        popup_fields = ['Name', 'code8']

    try:
        gj = folium.GeoJson(
            gdf.__geo_interface__,
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
        existing_fields = [f for f in popup_fields if f in gdf.columns]
        if existing_fields:
            gj.add_child(folium.features.GeoJsonPopup(fields=existing_fields, labels=True, localize=True, parse_html=False))
        gj.add_to(m)
    except Exception:
        # fallback to adding features one-by-one (slower)
        for idx, row in gdf.iterrows():
            try:
                geo_json = mapping(row.geometry)
            except Exception:
                continue
            popup_html = f"<b>Name:</b> {row.get('Name','')}<br/>"
            if 'code8' in row:
                popup_html += f"<b>FarmerCode:</b> {row.get('code8','')}<br/>"
            for c in ['Group', 'group', 'Village', 'village']:
                if c in row and pd.notna(row.get(c)):
                    popup_html += f"<b>{c}:</b> {row.get(c)}<br/>"
            folium.GeoJson(
                geo_json,
                name=str(idx),
                tooltip=row.get('Name',''),
                style_function=lambda feature: {
                    'fillColor': '#ffff66',
                    'color': '#0000ff',
                    'weight': 2,
                    'fillOpacity': 0.3,
                },
                highlight_function=lambda x: {'weight':3, 'color':'green'},
                popup=folium.Popup(popup_html, max_width=300)
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

kml_url = st.sidebar.text_input('KML raw URL', value=_kml_default)
excel_url = st.sidebar.text_input('Excel raw URL', value=_excel_default)

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
simplify_enabled = st.sidebar.checkbox('Enable geometry simplification (faster display, approximate coordinates)', value=True)
use_simplified = st.sidebar.checkbox('Use simplified coordinates (toggle to switch to actual)', value=simplify_enabled)
# Add geometries (cached)
kg = add_geometries(kg, simplify_enabled)

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

# Get unique farmer codes for selectbox
unique_codes = sorted(kg['code8'].unique())
search_code = st.sidebar.selectbox('Select Farmer Code (type to search and filter options)', options=['(any)'] + unique_codes)
search_code = '' if search_code == '(any)' else search_code

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

# Show matching farmer codes if searching
if search_code:
    matching_codes = sorted(filtered['code8'].unique())
    with st.sidebar.expander(f"Matching Farmer Codes ({len(matching_codes)})"):
        st.write(", ".join(matching_codes))

# Map display
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

    # Set geometry based on toggle
    if use_simplified and simplify_enabled:
        display_gdf = display_gdf.set_geometry('simplified_geometry')
    else:
        display_gdf = display_gdf.set_geometry('original_geometry')

    # Render the map
    m = folium_map_for_gdf(display_gdf, popup_fields=popup_fields)
    st_folium(m, width="100%", height=800)

# Footer
st.markdown('---')
st.markdown("**Notes:** The app matches the first 8 characters of the KML `Name` field to the Excel `FarmerCode`. If your KML has a different name field, update the code to use the correct property.")
