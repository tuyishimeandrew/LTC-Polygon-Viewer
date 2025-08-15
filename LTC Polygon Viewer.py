import streamlit as st
import geopandas as gpd
import pandas as pd
import folium
from streamlit_folium import st_folium
from shapely.geometry import mapping
import tempfile
import requests
import io

st.set_page_config(layout="wide", page_title="Latitude Farm Polygon Viewer")

st.title("Farm Polygon Viewer")

@st.cache_data
def download_file_to_temp(url: str) -> str:
    resp = requests.get(url)
    resp.raise_for_status()
    suffix = ''
    if url.lower().endswith('.kml') or '.kml?' in url.lower():
        suffix = '.kml'
    elif url.lower().endswith('.xlsx') or '.xls' in url.lower():
        suffix = '.xlsx'
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
    if 'Name' not in gdf.columns and 'name' in gdf.columns:
        gdf = gdf.rename(columns={'name': 'Name'})
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
    farmer_col = None
    for col in df.columns:
        if col.strip().lower() in ['farmercode', 'farmer_code', 'code', 'farmer code']:
            farmer_col = col
            break
    if farmer_col is None:
        farmer_col = df.columns[0]
    df[farmer_col] = df[farmer_col].astype(str).str.upper().str.strip()
    kg = _kml_gdf.copy()
    kg['Name'] = kg['Name'].astype(str)
    kg['code8'] = kg['Name'].str[:8].str.upper().str.strip()
    valid_codes = set(df[farmer_col])
    kg = kg[kg['code8'].isin(valid_codes)].reset_index(drop=True)
    kg = kg.merge(df, left_on='code8', right_on=farmer_col, how='left', suffixes=(None, '_excel'))
    if kg.crs is None:
        kg = kg.set_crs('epsg:4326')
    else:
        kg = kg.to_crs(epsg=4326)
    return kg, df, farmer_col

def folium_map_for_gdf(gdf: gpd.GeoDataFrame, popup_fields=None, initial_zoom=12):
    if len(gdf) == 0:
        m = folium.Map(location=[0,0], zoom_start=2)
        return m
    bounds = gdf.total_bounds
    minx, miny, maxx, maxy = bounds
    center_lat = (miny + maxy) / 2
    center_lon = (minx + maxx) / 2
    m = folium.Map(location=[center_lat, center_lon], zoom_start=initial_zoom)
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
        existing_fields = [f for f in popup_fields if f in gdf.columns]
        if existing_fields:
            gj.add_child(folium.features.GeoJsonPopup(fields=existing_fields, labels=True, localize=True, parse_html=False))
        gj.add_to(m)
    except Exception:
        for idx, row in gdf.iterrows():
            try:
                geo_json = {
                    'type': 'Feature',
                    'geometry': mapping(row.geometry),
                    'properties': {col: row[col] for col in gdf.columns if col not in ['geometry']}
                }
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

_raw_base = "https://raw.githubusercontent.com/tuyishimeandrew/LTC-Polygon-Viewer/main"
kml_url = f"{_raw_base}/SurveyCTO%20Inspection%20Polygons.kml"
excel_url = f"{_raw_base}/Group%20Polygons.xlsx"

try:
    kml_gdf = read_kml_from_url(kml_url)
    groups_df = read_excel_from_url(excel_url)
except Exception as e:
    st.error(f'Error loading files: {e}')
    st.stop()

try:
    kg, df_excel, farmer_col = prepare_data(kml_gdf, groups_df)
except Exception as e:
    st.error(f'Error preparing data: {e}')
    st.stop()

popup_fields = ['Name', 'code8']
if farmer_col and farmer_col in df_excel.columns:
    popup_fields.append(farmer_col)
for c in ['Village', 'village', 'Group', 'group']:
    if c in df_excel.columns and c not in popup_fields:
        popup_fields.append(c)

st.sidebar.header('Filters')

village_col = None
group_col = None
for c in df_excel.columns:
    if c.strip().lower() in ['village', 'village_name']:
        village_col = c
    if c.strip().lower() in ['group', 'group_name']:
        group_col = c

villages = sorted(df_excel[village_col].dropna().astype(str).unique()) if village_col else []
groups = sorted(df_excel[group_col].dropna().astype(str).unique()) if group_col else []

village_sel = st.sidebar.selectbox('Village', options=['(any)'] + villages)
group_sel = st.sidebar.selectbox('Group', options=['(any)'] + groups)

filtered = kg.copy()
if village_col and village_sel != '(any)':
    filtered = filtered[filtered[village_col].astype(str) == village_sel]
if group_col and group_sel != '(any)':
    filtered = filtered[filtered[group_col].astype(str) == group_sel]

if len(kg) == 0:
    st.warning('No polygons available.')
else:
    if len(filtered) == 0:
        st.info('No matches — showing all.')
        display_gdf = kg
    else:
        display_gdf = filtered
    m = folium_map_for_gdf(display_gdf, popup_fields=popup_fields)
    st_folium(m, width="100%", height=800)
