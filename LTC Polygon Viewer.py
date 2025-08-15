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
    - Shows only polygons whose KML `Name` first 4 characters match `FarmerCode` in the Excel.
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
def prepare_data(kml_gdf: gpd.GeoDataFrame, groups_df: pd.DataFrame):
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

    kg = kml_gdf.copy()
    kg['Name'] = kg['Name'].astype(str)
    kg['code4'] = kg['Name'].str[:4].str.upper().str.strip()

    valid_codes = set(df[farmer_col])
    kg = kg[kg['code4'].isin(valid_codes)].reset_index(drop=True)

    # Merge on code4 -> farmer_col to attach group/village info to polygons
    kg = kg.merge(df, left_on='code4', right_on=farmer_col, how='left', suffixes=(None, '_excel'))

    # Ensure CRS is WGS84
    if kg.crs is None:
        kg = kg.set_crs('epsg:4326')
    else:
        kg = kg.to_crs(epsg=4326)

    return kg, df, farmer_col


def folium_map_for_gdf(gdf: gpd.GeoDataFrame, initial_zoom=12):
    if len(gdf) == 0:
        m = folium.Map(location=[0,0], zoom_start=2)
        return m
    bounds = gdf.total_bounds  # minx, miny, maxx, maxy
    minx, miny, maxx, maxy = bounds
    center_lat = (miny + maxy) / 2
    center_lon = (minx + maxx) / 2
    m = folium.Map(location=[center_lat, center_lon], zoom_start=initial_zoom)

    for idx, row in gdf.iterrows():
        try:
            geo_json = mapping(row.geometry)
        except Exception:
            continue
        popup_html = f"<b>Name:</b> {row.get('Name','')}<br/>"
        if 'code4' in row:
            popup_html += f"<b>FarmerCode:</b> {row.get('code4','')}<br/>"
        # include common excel columns if present
        for c in ['Group', 'group', 'Village', 'village']:
            if c in row and pd.notna(row.get(c)):
                popup_html += f"<b>{c}:</b> {row.get(c)}<br/>"
        folium.GeoJson(
            geo_json,
            name=str(idx),
            tooltip=row.get('Name',''),
            style_function=lambda feature: {
                'fillColor': '#ffff66',
                'color': '#0000ff',  # blue boundary
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
kml_url = st.sidebar.text_input('KML raw URL (e.g. https://raw.githubusercontent.com/your/repo/main/SurveyCTO Inspection Polygons.kml)')
excel_url = st.sidebar.text_input('Excel raw URL (e.g. https://raw.githubusercontent.com/your/repo/main/Group Polygons.xlsx)')

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
    filtered = filtered[filtered['code4'].str.startswith(search_code)]
if village_col and village_sel != '(any)':
    filtered = filtered[filtered[village_col].astype(str) == village_sel]
if group_col and group_sel != '(any)':
    filtered = filtered[filtered[group_col].astype(str) == group_sel]

st.sidebar.markdown(f"Matching polygons: **{len(filtered)}**")

# Map display
st.subheader('Map view')
if len(filtered) == 0:
    st.warning('No polygons match the current filters.')
    show_all = st.button('Show all available polygons')
    if show_all:
        m_all = folium_map_for_gdf(kg)
        st_folium(m_all, width=1000, height=700)
else:
    m = folium_map_for_gdf(filtered)
    folium.LayerControl().add_to(m)
    st_folium(m, width=1000, height=700)

# Footer
st.markdown('---')
st.markdown("**Notes:** The app matches the first 4 characters of the KML `Name` field to the Excel `FarmerCode`. If your KML has a different name field, update the code to use the correct property.")
