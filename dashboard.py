import streamlit as st
import requests
import json
from datetime import datetime, timedelta
import pytz
import pandas as pd
import io
import altair as alt
import concurrent.futures
import pydeck as pdk
import base64
from pathlib import Path
from streamlit_autorefresh import st_autorefresh
import time

# --- HELPER: IMAGE TO BASE64 ---
def img_to_bytes(img_path):
    try:
        img_bytes = Path(img_path).read_bytes()
        encoded = base64.b64encode(img_bytes).decode()
        return encoded
    except FileNotFoundError:
        return ""

# Configure Altair theme - Modern Clean Design (Larger Fonts)
def configure_altair_theme():
    return {
        'config': {
            'background': 'transparent',
            'view': {
                'strokeWidth': 0,
                'fill': 'transparent'
            },
            'axis': {
                'labelColor': '#64748b',
                'titleColor': '#475569',
                'gridColor': '#f1f5f9',
                'domainWidth': 0,
                'domain': False,
                'tickSize': 0,
                'labelFont': 'Inter, sans-serif',
                'titleFont': 'Inter, sans-serif',
                'labelFontSize': 11,        # REVISI: 11px (intermediate)
                'titleFontSize': 12,        # REVISI: 12px
                'titleFontWeight': 600,
                'labelFontWeight': 500
            },
            'axisX': {
                'grid': False,
                'labelPadding': 8
            },
            'axisY': {
                'gridDash': [2, 4],
                'labelPadding': 8
            },
            'legend': {
                'labelColor': '#64748b',
                'titleColor': '#475569',
                'labelFont': 'Inter, sans-serif',
                'titleFont': 'Inter, sans-serif',
                'labelFontSize': 11,        # REVISI: 11px
                'symbolSize': 80,
                'padding': 10
            },
            'title': {
                'color': '#1e293b',
                'font': 'Inter, sans-serif',
                'fontSize': 15,             # REVISI: 15px
                'fontWeight': 700,
                'anchor': 'start',
                'offset': 12
            }
        }
    }

alt.themes.register('light_dashboard', configure_altair_theme)
alt.themes.enable('light_dashboard')

# --- KONFIGURASI ---
WIALON_HOST = "https://hst-api.wialon.com/wialon/ajax.html"
WIALON_TOKEN = "8b0f180218cc380cd02922c6cc3f0737E9A4ADC8513B979F63195B9E51BEC2195A302602"
TEMPLATE_ID = 17
TIMEZONE = pytz.timezone("Asia/Makassar")
TARGET_GROUPS = ["MGE - LIGHT VEHICLE", "MGE - SUPPORT"]

# --- SCHEDULER CONFIGURATION ---
AUTO_LOAD_HOUR = 6       # Jam target auto-load (06:xx)
AUTO_LOAD_MINUTE = 5     # Menit target auto-load (06:05)
GOLDEN_WINDOW_MINUTES = 10  # Toleransi window untuk trigger (06:05 - 06:15)

def calculate_next_trigger_seconds():
    """
    Hitung berapa DETIK lagi sampai jam 06:05 berikutnya.
    - Jika sekarang SEBELUM 06:05 hari ini -> target = hari ini 06:05
    - Jika sekarang SETELAH 06:05 hari ini -> target = besok 06:05
    """
    now = datetime.now(TIMEZONE)
    today_target = now.replace(hour=AUTO_LOAD_HOUR, minute=AUTO_LOAD_MINUTE, second=0, microsecond=0)
    
    if now < today_target:
        # Belum lewat 06:05 hari ini
        next_trigger = today_target
    else:
        # Sudah lewat, target besok
        next_trigger = today_target + timedelta(days=1)
    
    seconds_remaining = (next_trigger - now).total_seconds()
    return max(int(seconds_remaining), 60)  # Minimum 60 detik untuk safety

def is_in_golden_window():
    """
    Cek apakah waktu sekarang berada di 'Golden Window' (06:05 - 06:15).
    Window ini adalah saat auto-load akan dieksekusi.
    """
    now = datetime.now(TIMEZONE)
    window_start = now.replace(hour=AUTO_LOAD_HOUR, minute=AUTO_LOAD_MINUTE, second=0, microsecond=0)
    window_end = window_start + timedelta(minutes=GOLDEN_WINDOW_MINUTES)
    return window_start <= now < window_end

def get_yesterday_production_dates():
    """
    Dapatkan tanggal 'kemarin' untuk Production Day (H-1).
    """
    today = datetime.now(TIMEZONE).date()
    yesterday = today - timedelta(days=1)
    return yesterday, yesterday

def should_auto_load():
    """
    Tentukan apakah auto-load harus dijalankan:
    1. Waktu sekarang ada di Golden Window (06:05 - 06:15)
    2. Data untuk hari ini belum di-load (cek session_state)
    """
    if not is_in_golden_window():
        return False
    
    today_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    last_auto_load = st.session_state.get('last_auto_load_date', None)
    
    # Jika sudah auto-load hari ini, skip
    if last_auto_load == today_str:
        return False
    
    return True

# --- HELPER FUNCTIONS ---
def parse_duration_to_minutes(duration_str):
    if not duration_str or duration_str == "-" or duration_str == "":
        return 0.0
    try:
        days = 0
        time_str = duration_str
        if "day" in duration_str:
            parts = duration_str.split(" day")
            days = int(parts[0])
            time_part = parts[1].strip()
            if time_part.startswith("s "): time_part = time_part[2:]
            time_str = time_part
        h, m, s = map(int, time_str.split(":"))
        total_minutes = (days * 24 * 60) + (h * 60) + m + (s / 60)
        return round(total_minutes, 2)
    except Exception:
        return 0.0

def parse_mileage(mileage_str):
    if not mileage_str or mileage_str == "": 
        return 0.0
    try:
        clean_str = str(mileage_str).lower().replace(" km", "").replace(" ", "").replace(",", ".")
        return float(clean_str)
    except:
        return 0.0

def wialon_request(action, params, sid=None):
    payload = {"svc": action, "params": json.dumps(params)}
    if sid:
        payload["sid"] = sid
    try:
        response = requests.post(WIALON_HOST, data=payload, timeout=30)
        result = response.json()
        if isinstance(result, dict) and "error" in result and result["error"] != 0:
            st.warning(f"Wialon API Error: {result}")
        return result
    except requests.exceptions.Timeout:
        st.error("Request timeout - Wialon server tidak merespons")
        return {"error": "timeout"}
    except requests.exceptions.ConnectionError:
        st.error("Connection error - Periksa koneksi internet")
        return {"error": "connection"}
    except Exception as e:
        st.error(f"Request error: {str(e)}")
        return {"error": str(e)}

@st.cache_data(ttl=3600)
def login_wialon():
    res = wialon_request("token/login", {"token": WIALON_TOKEN})
    if "eid" in res:
        return res["eid"]
    if "error" in res:
        st.error(f"Login error details: {res}")
    return None

@st.cache_data(ttl=3600)
def find_id_by_name(sid, items_type, name):
    params = {
        "spec": {
            "itemsType": items_type,
            "propName": "sys_name",
            "propValueMask": name,
            "sortType": "sys_name"
        },
        "force": 1,
        "flags": 1,
        "from": 0,
        "to": 0
    }
    res = wialon_request("core/search_items", params, sid)
    if res and "items" in res and len(res["items"]) > 0:
        return res["items"][0]["id"]
    return None

@st.cache_data(ttl=3600)
def get_resource_id(sid):
    params = {
        "spec": {
            "itemsType": "avl_resource",
            "propName": "reporttemplates",
            "propValueMask": "*",
            "sortType": "sys_name"
        },
        "force": 1,
        "flags": 8193,
        "from": 0,
        "to": 0
    }
    res = wialon_request("core/search_items", params, sid)
    if "items" in res and len(res["items"]) > 0:
        for item in res["items"]:
            item_id = item.get("id")
            if "rep" in item and item_id:
                for t_id, t_data in item["rep"].items():
                    if isinstance(t_data, dict) and t_data.get("id") == TEMPLATE_ID:
                        return item_id
    return None

def get_value(val):
    if val is None:
        return ""
    if isinstance(val, dict):
        return val.get('t', str(val))
    return str(val)

def fetch_row_details(sid, row, index, time_from, group_name):
    """Fungsi helper untuk mengambil detail sub-row secara paralel"""
    results = []
    if 'c' in row:
        raw_shift_name = get_value(row['c'][1]) if len(row['c']) > 1 else "Unknown"
        shift_name = raw_shift_name.strip()
        
        sub_rows = []
        if 'r' in row and isinstance(row['r'], list):
            sub_rows = row['r']
        elif ('n' in row and row['n'] > 0) or (shift_name in ["Day", "Night"] and not sub_rows):
            count_to_fetch = row.get('n', 100)
            if count_to_fetch == 0: count_to_fetch = 100
            
            sub_params = {
                "tableIndex": 0,
                "rowIndex": index,
                "count": count_to_fetch,
                "offset": 0
            }
            sub_res = wialon_request("report/get_result_subrows", sub_params, sid)
            if isinstance(sub_res, list):
                sub_rows = sub_res
        
        for sub_row in sub_rows:
            if 'c' in sub_row:
                no = get_value(sub_row['c'][0])
                unit_name = get_value(sub_row['c'][1])
                beginning = get_value(sub_row['c'][2])
                initial_loc = get_value(sub_row['c'][3])
                final_loc = get_value(sub_row['c'][4])
                in_motion = get_value(sub_row['c'][5])
                mileage = get_value(sub_row['c'][6])
                idling = get_value(sub_row['c'][7]) if len(sub_row['c']) > 7 else ""
                
                results.append([
                    time_from.strftime("%Y-%m-%d"),
                    shift_name,
                    group_name,
                    no,
                    unit_name,
                    beginning,
                    initial_loc,
                    final_loc,
                    in_motion,
                    mileage,
                    idling,
                ])
    return results

def process_report(sid, group_name, time_from, time_to, template_id, resource_id):
    group_id = find_id_by_name(sid, "avl_unit_group", group_name)
    if not group_id:
        return []
    
    ts_from = int(time_from.timestamp())
    ts_to = int(time_to.timestamp())
    
    exec_params = {
        "reportResourceId": resource_id,
        "reportTemplateId": template_id,
        "reportObjectId": group_id,
        "reportObjectSecId": 0,
        "interval": {
            "from": ts_from,
            "to": ts_to,
            "flags": 0
        }
    }
    
    wialon_request("report/cleanup_result", {}, sid)
    exec_res = wialon_request("report/exec_report", exec_params, sid)
    
    results = []
    
    if "reportResult" in exec_res:
        total_rows = 0
        if len(exec_res['reportResult']['tables']) > 0:
            total_rows = exec_res['reportResult']['tables'][0]['rows']
            
        if total_rows > 0:
            row_params = {
                "tableIndex": 0,
                "indexFrom": 0,
                "indexTo": total_rows
            }
            rows_res = wialon_request("report/get_result_rows", row_params, sid)
            
            if isinstance(rows_res, list):
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    future_to_row = {
                        executor.submit(fetch_row_details, sid, row, i, time_from, group_name): i 
                        for i, row in enumerate(rows_res)
                    }
                    for future in concurrent.futures.as_completed(future_to_row):
                        try:
                            data = future.result()
                            results.extend(data)
                        except Exception as exc:
                            pass
    return results

# --- DATA FETCHING FUNCTION (Refactored for Auto-Load) ---
def fetch_and_process_data(start_time, api_end_time, filter_end_time, is_auto_load=False):
    """
    Fungsi utama untuk fetch dan process data dari Wialon API.
    Digunakan oleh manual Load button dan Auto-Load scheduler.
    
    Args:
        start_time: datetime - Waktu mulai (jam 06:00 tanggal DARI)
        api_end_time: datetime - Waktu akhir untuk API request (lebih lebar)
        filter_end_time: datetime - Waktu akhir untuk filter data (jam 06:00 tanggal SAMPAI+1)
        is_auto_load: bool - True jika dipanggil dari auto-load scheduler
    
    Returns:
        pd.DataFrame or None
    """
    # Login ke Wialon
    sid = login_wialon()
    if not sid:
        st.error("Login Failed")
        return None
        
    resource_id = get_resource_id(sid)
    if not resource_id:
        st.error("Resource Not Found")
        return None
        
    all_data = []
    groups_found = []
    
    for group in TARGET_GROUPS:
        # Gunakan api_end_time (yang sudah dilebihkan) disini
        data = process_report(sid, group, start_time, api_end_time, TEMPLATE_ID, resource_id)
        if data:
            all_data.extend(data)
            groups_found.append(f"{group} ({len(data)} rows)")

    if not all_data:
        if is_auto_load:
            st.toast("⚠️ Auto-load: No data found for yesterday.")
        else:
            st.warning("No data found.")
        return None
    
    # Create DataFrame
    df = pd.DataFrame(all_data, columns=[
        "Date", "Shift", "Group", "No", "Unit", 
        "Beginning", "Initial Location", "Final Location",
        "In Motion", "Mileage", "Idling"
    ])
    
    # ===== FIX TIMEZONE BUG (UTC to WITA) =====
    # 1. Parse string ke datetime (Naive)
    df["Beginning_DT"] = pd.to_datetime(df["Beginning"], format="%d.%m.%Y %H:%M:%S", errors='coerce')
    
    # 2. Set sebagai UTC (Karena raw data Wialon token biasanya UTC)
    df["Beginning_DT"] = df["Beginning_DT"].dt.tz_localize("UTC")
    
    # 3. Convert ke Timezone Target (Asia/Makassar = GMT+8)
    df["Beginning_DT"] = df["Beginning_DT"].dt.tz_convert(TIMEZONE)
    
    # 4. UPDATE KOLOM TAMPILAN 'Beginning' agar sesuai jam lokal baru
    df["Beginning"] = df["Beginning_DT"].dt.strftime("%d.%m.%Y %H:%M:%S")
    
    # 5. FIX SHIFT COLUMN (Hitung ulang berdasarkan jam WITA)
    def get_shift(dt):
        if pd.isna(dt):
            return "Unknown"
        return "Day" if 6 <= dt.hour < 18 else "Night"
    
    df["Shift"] = df["Beginning_DT"].apply(get_shift)
    
    # 6. FIX DATE COLUMN (Production Date berdasarkan WITA)
    def get_production_date(dt):
        if pd.isna(dt):
            return ""
        if dt.hour < 6:
            return (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            return dt.strftime("%Y-%m-%d")
    
    df["Date"] = df["Beginning_DT"].apply(get_production_date)
    
    # ===== STRICT FILTERING (Production Day 06:00 - 06:00) =====
    if not df.empty:
        original_count = len(df)
        df = df[
            (df["Beginning_DT"] >= start_time) & 
            (df["Beginning_DT"] < filter_end_time)
        ]
        filtered_count = len(df)
        
        if original_count != filtered_count:
            st.toast(f"🔍 Filtered out {original_count - filtered_count} overlap rows")

    # SORTING (Day Shift sebelum Night Shift)
    if not df.empty:
        df = df.sort_values(by="Beginning_DT", ascending=True).reset_index(drop=True)
        df["No"] = range(1, len(df) + 1)
    
    # Hapus kolom helper
    if "Beginning_DT" in df.columns:
        df = df.drop(columns=["Beginning_DT"])

    # MODIFIKASI: Dibagi 60 untuk konversi ke Jam
    df["Idling (Jam)"] = df["Idling"].apply(parse_duration_to_minutes) / 60
    df["Motion (Jam)"] = df["In Motion"].apply(parse_duration_to_minutes) / 60
    df["Mileage (km)"] = df["Mileage"].apply(parse_mileage)
    
    return df

# --- STREAMLIT UI ---
st.set_page_config(page_title="Mining Idle Time Dashboard", page_icon="⚙️", layout="wide")

# --- SMART SCHEDULER: Auto-Refresh at 06:05 AM ---
# Hitung detik sampai jam 06:05 berikutnya, lalu set timer browser SEKALI
next_trigger_seconds = calculate_next_trigger_seconds()
next_trigger_ms = next_trigger_seconds * 1000

# st_autorefresh akan me-refresh browser TEPAT saat timer habis
# Key "daily_sync" memastikan hanya satu timer aktif
refresh_count = st_autorefresh(interval=next_trigger_ms, limit=None, key="daily_sync")

# Tampilkan info scheduler di sidebar (debugging - bisa dihapus nanti)
next_trigger_time = datetime.now(TIMEZONE) + timedelta(seconds=next_trigger_seconds)
st.sidebar.markdown(f"""
<div style='background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); 
            padding: 0.8rem; border-radius: 8px; margin-bottom: 1rem; color: white; font-size: 0.75rem;'>
    <b>⏰ Auto-Load Scheduler</b><br>
    Next refresh: <b>{next_trigger_time.strftime('%d/%m/%Y %H:%M')}</b><br>
    <span style='opacity:0.8'>({next_trigger_seconds//3600}h {(next_trigger_seconds%3600)//60}m remaining)</span>
</div>
""", unsafe_allow_html=True)

# --- AUTO-LOAD EXECUTION (Golden Window: 06:05 - 06:15) WITH RETRY LOOP ---
if should_auto_load():
    st.toast("🌅 Good Morning! Auto-loading production data for yesterday...")
    
    # Hitung tanggal kemarin (H-1)
    yesterday_start, yesterday_end = get_yesterday_production_dates()
    
    # Hitung interval waktu untuk H-1
    auto_start_dt = datetime.combine(yesterday_start, datetime.min.time())
    auto_start_dt = TIMEZONE.localize(auto_start_dt)
    auto_start_time = auto_start_dt.replace(hour=6, minute=0, second=0, microsecond=0)
    
    auto_end_dt = datetime.combine(yesterday_end, datetime.min.time())
    auto_end_dt = TIMEZONE.localize(auto_end_dt)
    auto_filter_end = (auto_end_dt + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
    auto_api_end = auto_filter_end + timedelta(hours=6)
    
    # ===== RETRY LOOP MECHANISM =====
    MAX_RETRIES = 3
    RETRY_DELAY_SECONDS = 10
    auto_load_success = False
    auto_df = None
    
    status_placeholder = st.empty()
    
    for attempt in range(1, MAX_RETRIES + 1):
        status_placeholder.info(f"⏳ Auto-load attempt {attempt}/{MAX_RETRIES}...")
        
        try:
            auto_df = fetch_and_process_data(auto_start_time, auto_api_end, auto_filter_end, is_auto_load=True)
            
            if auto_df is not None and not auto_df.empty:
                # SUCCESS!
                st.session_state['data_df'] = auto_df
                st.session_state['last_auto_load_date'] = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
                auto_load_success = True
                
                status_placeholder.success(f"✅ Auto-loaded {len(auto_df)} rows for {yesterday_start} (Attempt {attempt}/{MAX_RETRIES})")
                st.toast(f"✅ Auto-loaded {len(auto_df)} rows successfully!")
                
                time.sleep(1)  # Jeda sebentar agar user sempat lihat pesan sukses
                break          # Keluar loop dulu
            else:
                # GAGAL (Data Kosong)
                if attempt < MAX_RETRIES:
                    status_placeholder.warning(f"⚠️ Attempt {attempt}/{MAX_RETRIES} returned no data. Retrying in {RETRY_DELAY_SECONDS}s...")
                    st.toast(f"⏳ Attempt {attempt}/{MAX_RETRIES} failed... Retrying...")
                    time.sleep(RETRY_DELAY_SECONDS)
                else:
                    status_placeholder.error(f"❌ All {MAX_RETRIES} attempts failed. No data available for {yesterday_start}.")
                    st.toast(f"❌ Auto-load failed after {MAX_RETRIES} attempts")
                    
        except Exception as e:
            # ERROR CRASH
            if attempt < MAX_RETRIES:
                status_placeholder.warning(f"⚠️ Attempt {attempt}/{MAX_RETRIES} error: {str(e)[:50]}... Retrying in {RETRY_DELAY_SECONDS}s")
                time.sleep(RETRY_DELAY_SECONDS)
            else:
                status_placeholder.error(f"❌ All {MAX_RETRIES} attempts failed. Error: {str(e)[:100]}")
    
    # KUNCI PERBAIKAN: Rerun dilakukan DI LUAR loop jika sukses
    if auto_load_success:
        st.rerun()

    # Mark as attempted untuk mencegah infinite loop jika gagal total
    if not auto_load_success:
        st.session_state['last_auto_load_date'] = datetime.now(TIMEZONE).strftime("%Y-%m-%d")

# Custom CSS - Responsive v2.0 (Mobile Friendly)
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    @import url('https://fonts.googleapis.com/icon?family=Material+Icons');
    
    /* ===== ROOT VARIABLES ===== */
    :root {
        --primary: #38CE3C;
        --secondary: #FF4D6B;
        --warning: #FFDE73;
        --purple: #8E32E9;
        --dark: #181824;
        
        --bg-light: #F4F6F9;
        --card-light: #FFFFFF;
        --text-primary: #181824;
        --text-secondary: #64748b;
        --border-light: #e2e8f0;
        --radius: 12px;
    }
    
    /* ===== GLOBAL STYLES & RESET ===== */
    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif !important;
        font-size: 14px;
    }
    
    .stApp {
        background-color: var(--bg-light) !important;
    }
    
    /* ===== FIX LAYOUT SPACING (FIXED HEADER STRATEGY) ===== */
    
    /* 1. CONTAINER UTAMA: Atur padding atas agar konten mulai tepat dibawah Header */
    div[data-testid="block-container"],
    div[data-testid="stMainBlockContainer"] {
        padding-top: 35px !important; /* Header 60px + Jarak 5px */
        padding-bottom: 5rem !important;
        padding-left: 5rem !important;
        padding-right: 5rem !important;
        max-width: 100% !important;
    }

    /* 2. Sembunyikan Header Bawaan & Toolbar Streamlit */
    header[data-testid="stHeader"],
    div[data-testid="stToolbar"],
    div[data-testid="stDecoration"],
    [data-testid="stToolbar"],
    [data-testid="stDecoration"] {
        display: none !important;
        visibility: hidden !important;
        height: 0 !important;
    }

    /* 3. Reset App View Container */
    div[data-testid="stAppViewContainer"] {
        padding-top: 0 !important;
        margin-top: 0 !important;
    }
    
    /* DIKOMENTAR: Agar sidebar scheduler muncul
    section[data-testid="stSidebar"] {
        display: none !important;
    }
    */

    /* ===== HEADER: FIXED (Melayang/Sticky di atas) ===== */
    
    /* 2. HEADER GHOST FIX: Hilangkan ruang hantu dari wadah header */
    div.element-container:has(.dashboard-header),
    div[data-testid="stMarkdownContainer"]:has(.dashboard-header) {
        height: 0 !important;
        margin: 0 !important;
        padding: 0 !important;
        overflow: visible !important;
        display: block !important;
    }
    
    .dashboard-header {
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        right: 0 !important; /* Memastikan header melebar penuh ke kanan */
        
        height: 70px !important;
        margin: 0 !important; /* Reset margin negatif yang bikin error */
        
        z-index: 999; /* Layer di atas konten, tapi di bawah sidebar Streamlit */
        background: #ffffff !important;
        
        /* Kosmetik */
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 0 5rem; /* Sejajar dengan body padding */
        box-shadow: 0 2px 15px rgba(0,0,0,0.05); 
        border-bottom: 1px solid #f1f5f9;
    }
    
    /* 3. FILTER ROW: RESET posisi (Hapus margin negatif) */
    [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"]:nth-of-type(1) {
        margin-top: 1.5rem !important; /* Jarak normal dari header */
        
        background-color: #ffffff !important;
        padding: 1.5rem !important;
        border-radius: 12px !important;
        border: 1px solid #e2e8f0 !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1) !important;
        margin-bottom: 1rem !important; /* Dikurangi dari 2rem → 1rem */
        gap: 1rem !important;
        position: relative;
        z-index: 998;
    }
    
    /* ===== KILL GAP ANTARA FILTER DAN KPI ===== */
    /* Kill semua spacer Streamlit otomatis */
    .element-container {
        margin-bottom: 0 !important;
    }
    
    /* Pastikan vertical block tidak punya gap berlebih */
    div[data-testid="stVerticalBlock"] {
        gap: 0.5rem !important;
    }
    
    /* ===== UNIFORM FILTER BOX SIZING ===== */
    /* Samakan tinggi semua elemen di filter row */
    [data-testid="stHorizontalBlock"]:nth-of-type(1) [data-testid="stDateInput"] > div,
    [data-testid="stHorizontalBlock"]:nth-of-type(1) [data-testid="stSelectbox"] > div,
    [data-testid="stHorizontalBlock"]:nth-of-type(1) [data-testid="stMultiSelect"] > div,
    [data-testid="stHorizontalBlock"]:nth-of-type(1) [data-testid="stTextInput"] > div {
        min-height: 42px !important;
    }
    
    /* Input fields uniform height */
    [data-testid="stHorizontalBlock"]:nth-of-type(1) input,
    [data-testid="stHorizontalBlock"]:nth-of-type(1) [data-baseweb="select"] > div,
    [data-testid="stHorizontalBlock"]:nth-of-type(1) [data-baseweb="input"] > div {
        height: 42px !important;
        min-height: 42px !important;
    }
    
    /* Button uniform height */
    [data-testid="stHorizontalBlock"]:nth-of-type(1) button {
        height: 42px !important;
        min-height: 42px !important;
    }
    
    /* Column uniform width distribution */
    [data-testid="stHorizontalBlock"]:nth-of-type(1) > div {
        flex: 1 1 auto !important;
    }
    
    /* LOGO WRAPPERS (Z-Index tinggi agar di atas judul) */
    .header-left, .header-right {
        position: relative;
        z-index: 10; 
        display: flex;
        align-items: center;
        background: #ffffff; /* Background putih untuk menutupi teks jika layar sempit */
    }
    
    /* UKURAN LOGO - FINAL */
    .header-left .header-logo { max-height: 42px !important; width: auto; }
    .header-right .header-logo { max-height: 18px !important; width: auto; } /* Planning Lebih Kecil */

    /* JUDUL TENGAH (Absolute Overlay) */
    .header-center {
        position: absolute;
        top: 0;
        left: 0;
        width: 100%; /* Penuhi seluruh lebar header */
        height: 100%;
        
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        z-index: 1; /* Di bawah logo */
        pointer-events: none; /* Agar klik tembus ke logo jika perlu */
    }
    
    .header-center h1 {
        font-family: 'Inter', sans-serif;
        font-size: 1.4rem !important;
        font-weight: 800;
        color: #111827;
        margin: 0;
        line-height: 1.2;
        text-align: center;
        width: 100%;
    }
    
    /* PERBAIKAN SUBTITLE FLEET */
    .header-center p {
        font-family: 'Inter', sans-serif;
        font-size: 0.7rem !important;
        color: #6b7280;
        
        margin: 2px 0 0 0 !important; /* Reset margin liar */
        padding: 0 !important;        /* Reset padding */
        
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 1px;
        text-align: center !important; /* Paksa Rata Tengah */
        width: 100%;                   /* Penuhi lebar */
        display: block;
    }

    /* MOBILE: Ganti ke mode tumpuk */
    @media only screen and (max-width: 768px) {
        .dashboard-header {
            flex-direction: column !important;
            height: auto !important;
            padding: 1rem;
            margin-top: -80px !important;
            gap: 0.8rem;
            overflow: visible;
        }
        
        .header-center {
            position: relative; /* Kembali ke relative */
            width: 100%;
            height: auto;
            order: 2;
            margin-bottom: 5px;
        }
        
        .header-left { order: 1; justify-content: center; width: 100%; }
        .header-right { order: 3; justify-content: center; width: 100%; }
    }

    /* ===== FIX KPI CARDS (SEPARATE LAYERS) ===== */
    .kpi-card-base {
        border-radius: 20px;
        padding: 1.5rem;
        position: relative;
        overflow: hidden; 
        color: #ffffff !important;
        transition: transform 0.3s ease, box-shadow 0.3s ease;
        border: none !important;
        min-height: 110px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        z-index: 1; /* Stacking context */
        /* Background warna (gradient) diatur oleh class masing-masing (.card-pink, dll) */
    }
    
    /* LAYER 1: PATTERN GELOMBANG (Gunakan ::before) */
    /* Ini menempatkan garis-garis DI ATAS warna background, tapi transparan */
    .kpi-card-base::before {
        content: "";
        position: absolute;
        top: 0; left: 0; width: 100%; height: 100%;
        background-image: repeating-radial-gradient(
            circle at 0% 100%, 
            transparent 0, 
            transparent 10px, 
            rgba(255, 255, 255, 0.08) 10px, /* Putih Pudar (8%) */
            rgba(255, 255, 255, 0.08) 11px
        );
        opacity: 0.6; /* Default agak samar */
        transition: opacity 0.3s ease;
        z-index: -1; /* Di belakang teks */
        pointer-events: none;
    }

    /* LAYER 2: DEKORASI BUBBLES (Gunakan ::after) */
    .kpi-card-base::after {
        content: "";
        position: absolute;
        top: -30px;
        right: -30px;
        width: 100px;
        height: 100px;
        border-radius: 50%;
        background: rgba(255, 255, 255, 0.1);
        box-shadow: 0 0 0 15px rgba(255, 255, 255, 0.05);
        z-index: -1;
        pointer-events: none;
        transition: transform 0.3s ease;
    }

    /* EFEK HOVER */
    .kpi-card-base:hover {
        transform: translateY(-5px);
        /* JANGAN GANTI BACKGROUND IMAGE DISINI */
    }
    
    /* Saat hover, pertegas pattern gelombang (tapi jangan ganti warna dasar) */
    .kpi-card-base:hover::before {
        opacity: 1; /* Pattern jadi lebih jelas/terang */
    }
    
    .kpi-card-base:hover::after {
        transform: scale(1.1); /* Bubble membesar sedikit */
    }
    
    /* --- VARIAN WARNA GRADIENT --- */
    
    /* 1. GREEN CARD (Total Trips) */
    .card-green {
        background: linear-gradient(135deg, #38CE3C 0%, #26a62a 100%);
        box-shadow: 0 10px 20px rgba(56, 206, 60, 0.3); /* Shadow Hijau */
    }
    
    /* 2. PURPLE CARD (Total Units) */
    .card-purple {
        background: linear-gradient(135deg, #8E32E9 0%, #6d28d9 100%);
        box-shadow: 0 10px 20px rgba(142, 50, 233, 0.3); /* Shadow Ungu */
    }
    
    /* 3. PINK CARD (Total Idle - Alert) */
    .card-pink {
        background: linear-gradient(135deg, #FF4D6B 0%, #d6334f 100%);
        box-shadow: 0 10px 20px rgba(255, 77, 107, 0.3); /* Shadow Pink */
    }
    
    /* 4. ORANGE CARD (Total Mileage) */
    .card-orange {
        background: linear-gradient(135deg, #fbbf24 0%, #d97706 100%);
        box-shadow: 0 10px 20px rgba(251, 191, 36, 0.3);
    }
    
    /* 5. DARK CARD (Avg Idle) */
    .card-dark {
        background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
        box-shadow: 0 10px 20px rgba(30, 41, 59, 0.3);
    }

    /* --- TYPOGRAPHY INSIDE CARD --- */
    .kpi-label-white {
        font-size: 0.85rem;
        font-weight: 500;
        opacity: 0.9; /* Sedikit transparan agar elegan */
        margin-bottom: 0.5rem;
        display: flex;
        align-items: center;
        gap: 8px;
    }
    
    .kpi-value-white {
        font-size: clamp(1.5rem, 2.5vw, 2rem);
        font-weight: 800;
        letter-spacing: -1px;
    }
    
    .kpi-unit-white {
        font-size: 0.9rem;
        font-weight: 400;
        opacity: 0.8;
    }

    /* ===== CHART & TABLE CARDS ===== */
    [data-testid="stVegaLiteChart"], .table-card {
        background-color: #ffffff !important;
        border-radius: 12px !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1) !important;
        border: 1px solid #e2e8f0 !important;
        padding: 0.5rem !important;
        width: 100% !important;
    }
    
    .table-title {
        font-size: clamp(0.9rem, 1.5vw, 1.125rem);
        font-weight: 700;
        color: var(--text-primary);
    }
    
    [data-testid="stDataFrame"] {
        background-color: #ffffff !important;
        border: 2px solid #cbd5e1 !important;
        border-radius: 12px !important;
        box-shadow: 0 4px 12px rgba(0,0,0,0.12) !important;
    }

    /* ===== FILTER ROW - Target baris pertama (Filter Inputs) ===== */
    [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"]:nth-of-type(1) {
        background-color: #ffffff !important;
        padding: 1.5rem !important;
        border-radius: 12px !important;
        border: 1px solid #e2e8f0 !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1) !important;
        margin-bottom: 2rem !important;
        gap: 1rem !important;
        
        /* Tarik ke atas secara agresif untuk menutup gap bekas header */
        margin-top: -220px !important; 
        
        position: relative;
        z-index: 998; /* Layer di bawah header (999) */
    }

    /* ===== FORM INPUTS ===== */
    .stSelectbox label, .stMultiSelect label, .stDateInput label, .stTextInput label {
        font-size: 0.8rem !important;
        font-weight: 600 !important;
        color: var(--text-secondary) !important;
        text-transform: uppercase !important;
    }
    
    .stSelectbox [data-baseweb="select"], 
    .stMultiSelect [data-baseweb="select"],
    .stDateInput input,
    .stTextInput input {
        font-size: 0.875rem !important;
        border-radius: 0.5rem !important;
        height: 42px !important;
        min-height: 42px !important;
        background-color: #ffffff !important;
        color: #1f2937 !important;
    }
    
    .stDateInput div[data-baseweb="input"],
    .stTextInput div[data-baseweb="input"], 
    .stSelectbox div[data-baseweb="select"] {
        background-color: #ffffff !important;
        border: 1px solid #e2e8f0 !important;
        border-radius: 8px !important;
    }
    
    .stDateInput div[data-baseweb="input"]:focus-within,
    .stTextInput div[data-baseweb="input"]:focus-within,
    .stSelectbox div[data-baseweb="select"]:focus-within {
        border: 1px solid #38CE3C !important;
        box-shadow: 0 0 0 1px #38CE3C !important;
    }
    
    .stMultiSelect [data-baseweb="tag"] {
        background-color: var(--primary) !important;
        color: white !important;
    }
    
    .stButton > button {
        height: 42px !important;
        min-height: 42px !important;
        font-size: 0.875rem !important;
        font-weight: 500 !important;
        border-radius: 0.5rem !important;
        transition: all 0.2s ease !important;
    }
    
    .stButton > button[kind="primary"] {
        background-color: var(--primary) !important;
        border-color: var(--primary) !important;
        color: #ffffff !important;
    }
    
    .stButton > button[kind="primary"]:hover {
        background-color: #299e2c !important;
        border-color: #299e2c !important;
        box-shadow: 0 4px 12px rgba(56, 206, 60, 0.4) !important;
    }
    
    [data-baseweb="popover"],
    [data-baseweb="menu"] {
        background-color: #ffffff !important;
    }
    
    [data-baseweb="menu"] li {
        background-color: #ffffff !important;
        color: #1f2937 !important;
    }
    
    [data-baseweb="menu"] li:hover {
        background-color: #f3f4f6 !important;
    }
    
    /* DATE PICKER GREEN */
    div[data-baseweb="calendar"] { background-color: #ffffff !important; }
    div[data-baseweb="calendar"] [aria-selected="true"] {
        background-color: #38CE3C !important; border-color: #38CE3C !important; color: #fff !important;
    }
    div[data-baseweb="calendar"] [aria-label*="Today"],
    div[data-baseweb="calendar"] [aria-label*="Hari Ini"] {
        color: #38CE3C !important; border-color: #38CE3C !important; font-weight: 800 !important;
    }
    div[data-baseweb="calendar"] [role="gridcell"]:hover {
        background-color: #ecfdf5 !important;
        border-radius: 50% !important;
    }

    /* FILTER ROW */
    [data-testid="stVerticalBlock"] > [data-testid="stHorizontalBlock"]:nth-of-type(1) {
        background-color: #ffffff !important;
        padding: 1.5rem !important;
        border-radius: 12px !important;
        border: 1px solid #e2e8f0 !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1) !important;
        margin-bottom: 2rem !important;
        gap: 1rem !important;
    }

    [data-testid="stHorizontalBlock"] {
        align-items: flex-end !important;
    }
    
    h2, h3 {
        font-family: 'Inter', sans-serif !important;
        color: var(--text-primary) !important;
    }
    
    .element-container {
        margin-bottom: 0.5rem !important;
    }
    
    div[data-testid="stVerticalBlock"] > div {
        gap: 0.75rem !important;
    }
    
    .dashboard-footer {
        background: #f8fafc;
        color: #94a3b8;
        text-align: center;
        padding: 1rem 0;
        border-top: 1px solid #e2e8f0;
        width: 100%;
        font-size: 0.7rem;
        position: fixed;
        bottom: 0;
        left: 0;
        z-index: 9999;
    }
    
    /* ===== FORCE REMOVE TOP SPACE ===== */
    [data-testid="stAppViewContainer"] > section {
        padding-top: 0 !important;
        margin-top: 0 !important;
    }
    
    [data-testid="stAppViewContainer"] > section > div {
        padding-top: 0 !important;
        margin-top: 0 !important;
    }
    
    [data-testid="stAppViewContainer"] > section > div > div:first-child {
        padding-top: 0 !important;
        margin-top: 0 !important;
    }

    /* ===== CUSTOM EXPORT BUTTON STYLE (COMPACT & RIGHT ALIGNED) ===== */
    
    /* 1. Container Tombol: Paksa Rata Kanan */
    [data-testid="stDownloadButton"] {
        display: flex !important;
        justify-content: flex-end !important; /* Geser ke kanan */
        width: 100% !important;
    }

    /* 2. Styling Tombol: Ukuran Pas & Cantik */
    [data-testid="stDownloadButton"] > button {
        width: auto !important; /* Lebar mengikuti teks + padding */
        min-width: 140px !important; /* Lebar minimal agar proporsional */
        background: linear-gradient(135deg, #38CE3C 0%, #26a62a 100%) !important;
        color: #ffffff !important;
        border: none !important;
        border-radius: 50px !important; /* Pill Shape (Lebih bulat) */
        padding: 0.5rem 1.5rem !important;
        font-weight: 600 !important;
        font-size: 0.85rem !important;
        box-shadow: 0 4px 10px rgba(56, 206, 60, 0.3) !important;
        transition: all 0.3s ease !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
    }

    /* 3. Hover Effect */
    [data-testid="stDownloadButton"] > button:hover {
        transform: translateY(-2px) !important;
        box-shadow: 0 6px 15px rgba(56, 206, 60, 0.5) !important;
        background: linear-gradient(135deg, #2ea031 0%, #1e8022 100%) !important;
    }
    
    [data-testid="stDownloadButton"] > button:active {
        transform: translateY(1px) !important;
        box-shadow: 0 2px 5px rgba(56, 206, 60, 0.3) !important;
    }
</style>
""", unsafe_allow_html=True)

# --- LOAD ASSETS ---
logo_mge_src = img_to_bytes("assets/logo_mge.png")
logo_plan_src = img_to_bytes("assets/logo_planning.png")

# Fallback jika gambar tidak ada
img_mge_html = f'<img src="data:image/png;base64,{logo_mge_src}" class="header-logo">' if logo_mge_src else "<b style='color:#38CE3C;font-size:1.5rem;'>MGE</b>"
img_plan_html = f'<img src="data:image/png;base64,{logo_plan_src}" class="header-logo">' if logo_plan_src else "<b style='color:#8E32E9;font-size:1.5rem;'>PLANNING</b>"

# --- HEADER SECTION ---
st.markdown(f"""
<div class="dashboard-header">
    <div class="header-left">
        {img_mge_html}
    </div>
    <div class="header-center">
        <h1>IDLE TIME DASHBOARD MONITORING</h1>
    </div>
    <div class="header-right">
        {img_plan_html}
    </div>
</div>
""", unsafe_allow_html=True)

# --- FILTER SECTION (Single Row) ---
cols = st.columns([1, 1, 0.7, 1, 1, 1, 1.2])

with cols[0]:
    start_date = st.date_input(
        "📅 DARI",
        value=datetime.now(TIMEZONE).date() - timedelta(days=1)
    )

with cols[1]:
    end_date = st.date_input(
        "📅 SAMPAI",
        value=datetime.now(TIMEZONE).date() - timedelta(days=1)
    )

# --- FIX INTERVAL CALCULATION (Production Day: 06:00 - 06:00 Next Day) ---
# Start Time: Tanggal 'DARI' jam 06:00 WITA
start_datetime = datetime.combine(start_date, datetime.min.time())
start_datetime = TIMEZONE.localize(start_datetime)
start_time = start_datetime.replace(hour=6, minute=0, second=0, microsecond=0)

# End Time Calculation
end_datetime = datetime.combine(end_date, datetime.min.time())
end_datetime = TIMEZONE.localize(end_datetime)

# filter_end_time: Batas SUCI untuk laporan (Jam 06:00 besoknya)
filter_end_time = (end_datetime + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)

# api_end_time: Batas REQUEST ke Wialon (Dilebihkan 6 jam buat jaga-jaga)
# Agar data trip jam 05:59 pagi besoknya PASTI ke-download
api_end_time = filter_end_time + timedelta(hours=6)

# Debugging (Tampilkan di terminal)
print(f"DEBUG TIME: API Request from {start_time} to {api_end_time}")
print(f"DEBUG TIME: Filter Range from {start_time} to {filter_end_time}")

with cols[2]:
    run_btn = st.button("🚀 Load", type="primary", use_container_width=True)

# Placeholder filters
shift_filter = []
unit_filter = []
loc_filter = []
search_term = ""

if 'data_df' in st.session_state:
    df = st.session_state['data_df']
    
    with cols[3]:
        all_shifts = df["Shift"].unique().tolist()
        default_shifts = [s for s in all_shifts if s in ["Day", "Night"]]
        shift_filter = st.multiselect("SHIFT", options=all_shifts, default=default_shifts)
    
    with cols[4]:
        unit_options = sorted(df["Unit"].unique())
        unit_filter = st.multiselect("UNIT", options=unit_options)
        
    with cols[5]:
        all_locations = [loc for loc in df["Initial Location"].unique() if loc and len(loc) > 0]
        named_locs = sorted([loc for loc in all_locations if loc and not loc.lstrip('-').replace('.', '').replace(',', '').replace(' ', '').isdigit() and not loc[0].lstrip('-').isdigit()])
        coord_locs = sorted([loc for loc in all_locations if loc not in named_locs])
        loc_options = named_locs + coord_locs
        loc_filter = st.multiselect("LOKASI", options=loc_options)
    
    with cols[6]:
        search_term = st.text_input("🔍 CARI", placeholder="Ketik unit/lokasi...")

# --- MAIN LOGIC (Manual Load Button) ---
if run_btn:
    st.cache_data.clear()
    
    with st.spinner('Loading Data (Optimized)...'):
        df = fetch_and_process_data(start_time, api_end_time, filter_end_time, is_auto_load=False)
        
        if df is not None and not df.empty:
            st.session_state['data_df'] = df
            st.toast(f"✅ Loaded {len(df)} rows successfully!")
            st.rerun()

# --- DISPLAY DATA ---
if 'data_df' in st.session_state:
    df = st.session_state['data_df']
    
    # Apply Filters
    filtered_df = df.copy()
    
    if shift_filter:
        filtered_df = filtered_df[filtered_df["Shift"].isin(shift_filter)]
        
    if unit_filter:
        filtered_df = filtered_df[filtered_df["Unit"].isin(unit_filter)]
        
    if loc_filter:
        filtered_df = filtered_df[filtered_df["Initial Location"].isin(loc_filter)]
        
    if search_term:
        mask = (
            filtered_df["Unit"].str.contains(search_term, case=False, na=False) |
            filtered_df["Initial Location"].str.contains(search_term, case=False, na=False) |
            filtered_df["Final Location"].str.contains(search_term, case=False, na=False)
        )
        filtered_df = filtered_df[mask]

    # --- KPI CARDS ---
    total_trips = len(filtered_df)
    total_units = filtered_df["Unit"].nunique()
    # MODIFIKASI: Menggunakan kolom Jam
    total_idle_hours = filtered_df["Idling (Jam)"].sum()
    total_mileage = filtered_df["Mileage (km)"].sum()
    # MODIFIKASI: Rata-rata dalam jam
    avg_idle_per_trip = total_idle_hours / total_trips if total_trips > 0 else 0
    
    # --- SPACER: FILTER TO KPI (Separation of Concerns) ---
    st.markdown("<div style='height: 1rem;'></div>", unsafe_allow_html=True)

    # --- KPI CARDS (NEW DESIGN) ---
    kpi_cols = st.columns(5)
    
    # Card 1: Total Trips (Hijau - Primary)
    with kpi_cols[0]:
        st.markdown(f"""
        <div class="kpi-card-base card-green">
            <div class="kpi-label-white">
                <span class="material-icons" style="font-size: 18px;">description</span>
                Total Trips
            </div>
            <div class="kpi-value-white">{total_trips:,}</div>
        </div>
        """, unsafe_allow_html=True)
        
    # Card 2: Total Units (Ungu - Secondary)
    with kpi_cols[1]:
        st.markdown(f"""
        <div class="kpi-card-base card-purple">
            <div class="kpi-label-white">
                <span class="material-icons" style="font-size: 18px;">local_shipping</span>
                Total Units
            </div>
            <div class="kpi-value-white">{total_units}</div>
        </div>
        """, unsafe_allow_html=True)
        
    # Card 3: Total Idle (Pink - Warning/Alert)
    with kpi_cols[2]:
        st.markdown(f"""
        <div class="kpi-card-base card-pink">
            <div class="kpi-label-white">
                <span class="material-icons" style="font-size: 18px;">timer_off</span>
                Total Idle
            </div>
            <div class="kpi-value-white">{total_idle_hours:,.1f} <span class="kpi-unit-white">jam</span></div>
        </div>
        """, unsafe_allow_html=True)
        
    # Card 4: Total Mileage (Orange - Motion)
    with kpi_cols[3]:
        st.markdown(f"""
        <div class="kpi-card-base card-orange">
            <div class="kpi-label-white">
                <span class="material-icons" style="font-size: 18px;">map</span>
                Total Mileage
            </div>
            <div class="kpi-value-white">{total_mileage:,.1f} <span class="kpi-unit-white">km</span></div>
        </div>
        """, unsafe_allow_html=True)
        
    # Card 5: Avg Idle (Dark Blue - Info)
    with kpi_cols[4]:
        st.markdown(f"""
        <div class="kpi-card-base card-dark">
            <div class="kpi-label-white">
                <span class="material-icons" style="font-size: 18px;">analytics</span>
                Avg Idle/Trip
            </div>
            <div class="kpi-value-white">{avg_idle_per_trip:.1f} <span class="kpi-unit-white">jam</span></div>
        </div>
        """, unsafe_allow_html=True)

    # --- SPACER: KPI TO CHARTS ---
    st.markdown("<div style='height: 1.5rem;'></div>", unsafe_allow_html=True)

    # --- ROW 1: 3 CHARTS ---
    col_r1_1, col_r1_2, col_r1_3 = st.columns(3, gap="medium")

    with col_r1_1:
        # Chart 1: Top 10 Unit Idle (Horizontal Bar)
        unit_idle_stats = filtered_df.groupby("Unit")["Idling (Jam)"].sum().sort_values(ascending=False).head(10).reset_index()
        unit_idle_stats.columns = ['Unit', 'Hours']
        
        if len(unit_idle_stats) > 0:
            max_val = unit_idle_stats['Hours'].max()
            
            # Bar chart - PINK untuk Idle (Alert)
            bars1 = alt.Chart(unit_idle_stats).mark_bar(
                color='#FF4D6B',
                cornerRadiusEnd=6
            ).encode(
                x=alt.X('Hours:Q', 
                    title=None,
                    scale=alt.Scale(domain=[0, max_val * 1.15]),
                    axis=alt.Axis(grid=False, labels=False, ticks=False, domain=False)
                ),
                y=alt.Y('Unit:N', 
                    sort='-x', 
                    title=None,
                    axis=alt.Axis(
                        labelLimit=180,
                        labelFontSize=10, 
                        labelColor='#555555', 
                        labelFontWeight=500,
                        tickSize=0,
                        domain=False
                    )
                ),
                tooltip=[
                    alt.Tooltip('Unit', title='Unit'), 
                    alt.Tooltip('Hours', title='Idle (Jam)', format='.1f')
                ]
            )
            
            # Text labels - PINK
            text1 = bars1.mark_text(
                align='left', 
                dx=5, 
                color='#FF4D6B', 
                fontSize=11,  # REVISI: 11px (intermediate)
                fontWeight='bold'
            ).encode(
                text=alt.Text('Hours:Q', format='.1f')
            )
            
            # Gabung + properties + configure
            final_chart1 = (bars1 + text1).properties(
                height=280,
                padding={'left': 10, 'right': 25, 'top': 10, 'bottom': 10},
                title=alt.TitleParams(
                    text='1. Top 10 Unit Idle',
                    anchor='start',
                    fontSize=15,
                    fontWeight=700,
                    color='#1e293b',
                    offset=10
                )
            ).configure(
                background='transparent'
            ).configure_view(stroke=None)
            
            st.altair_chart(final_chart1, use_container_width=True, theme=None)

    with col_r1_2:
        # Chart 2: Top 10 Lokasi Idle (Horizontal Bar - Orange)
        loc_idle = filtered_df.groupby("Initial Location")["Idling (Jam)"].sum().sort_values(ascending=False).head(10).reset_index()
        loc_idle.columns = ['Location', 'Hours']
        
        if len(loc_idle) > 0:
            max_val_loc = loc_idle['Hours'].max()
            
            # Bar chart - PURPLE untuk Lokasi
            bars2 = alt.Chart(loc_idle).mark_bar(
                color='#8E32E9',
                cornerRadiusEnd=6
            ).encode(
                x=alt.X('Hours:Q', 
                    title=None,
                    scale=alt.Scale(domain=[0, max_val_loc * 1.15]),
                    axis=alt.Axis(grid=False, labels=False, ticks=False, domain=False)
                ),
                y=alt.Y('Location:N', 
                    sort='-x', 
                    title=None, 
                    axis=alt.Axis(
                        labelLimit=180,
                        labelFontSize=9, 
                        labelColor='#555555', 
                        labelFontWeight=500,
                        tickSize=0,
                        domain=False
                    )
                ),
                tooltip=[
                    alt.Tooltip('Location', title='Lokasi'), 
                    alt.Tooltip('Hours', title='Idle (Jam)', format='.1f')
                ]
            )
            
            # Text labels - PURPLE
            text2 = bars2.mark_text(
                align='left', 
                dx=5, 
                color='#8E32E9', 
                fontSize=11,  # REVISI: 11px
                fontWeight='bold'
            ).encode(
                text=alt.Text('Hours:Q', format='.1f')
            )
            
            # Gabung + properties + configure
            final_chart2 = (bars2 + text2).properties(
                height=280,
                padding={'left': 10, 'right': 25, 'top': 10, 'bottom': 10},
                title=alt.TitleParams(
                    text='2. Top 10 Lokasi Idle',
                    anchor='start',
                    fontSize=15,
                    fontWeight=700,
                    color='#1e293b',
                    offset=10
                )
            ).configure(
                background='transparent'
            ).configure_view(stroke=None)
            
            st.altair_chart(final_chart2, use_container_width=True, theme=None)

    with col_r1_3:
        # Chart 3: Top 10 Jarak Tempuh (Horizontal Bar - Blue)
        mileage_stats = filtered_df.groupby("Unit")["Mileage (km)"].sum().sort_values(ascending=False).head(10).reset_index()
        
        if len(mileage_stats) > 0:
            max_val_mil = mileage_stats['Mileage (km)'].max()
            
            # Bar chart - GREEN untuk Mileage (Motion/Productivity)
            bars3 = alt.Chart(mileage_stats).mark_bar(
                color='#38CE3C',
                cornerRadiusEnd=6
            ).encode(
                x=alt.X('Mileage (km):Q', 
                    title=None,
                    scale=alt.Scale(domain=[0, max_val_mil * 1.15]),
                    axis=alt.Axis(grid=False, labels=False, ticks=False, domain=False)
                ),
                y=alt.Y('Unit:N', 
                    sort='-x', 
                    title=None,
                    axis=alt.Axis(
                        labelLimit=180,
                        labelFontSize=10, 
                        labelColor='#555555', 
                        labelFontWeight=500,
                        tickSize=0,
                        domain=False
                    )
                ),
                tooltip=[
                    alt.Tooltip('Unit', title='Unit'), 
                    alt.Tooltip('Mileage (km)', title='Jarak (KM)', format='.1f')
                ]
            )
            
            # Text labels - GREEN
            text3 = bars3.mark_text(
                align='left', 
                dx=5, 
                color='#38CE3C', 
                fontSize=11,  # REVISI: 11px
                fontWeight='bold'
            ).encode(
                text=alt.Text('Mileage (km):Q', format='.1f')
            )
            
            # Gabung + properties + configure
            final_chart3 = (bars3 + text3).properties(
                height=280,
                padding={'left': 10, 'right': 25, 'top': 10, 'bottom': 10},
                title=alt.TitleParams(
                    text='3. Top 10 Jarak Tempuh',
                    anchor='start',
                    fontSize=15,
                    fontWeight=700,
                    color='#1e293b',
                    offset=10
                )
            ).configure(
                background='transparent'
            ).configure_view(stroke=None)
            
            st.altair_chart(final_chart3, use_container_width=True, theme=None)

    # --- SPACER: ROW 1 TO ROW 2 (Disamakan dengan gap kolom 'medium' ~1rem) ---
    st.markdown("<div style='height: 1rem;'></div>", unsafe_allow_html=True)

    # --- ROW 2: 2 CHARTS (removed map) ---
    col_r2_1, col_r2_2 = st.columns(2, gap="medium")

    with col_r2_1:
        # Chart 4: Produktivitas - Stacked Bar (Motion vs Idle)
        prod_data = filtered_df.groupby("Unit").agg({
            "Motion (Jam)": "sum",
            "Idling (Jam)": "sum"
        }).reset_index()
        
        prod_data["Total"] = prod_data["Motion (Jam)"] + prod_data["Idling (Jam)"]
        prod_data = prod_data.sort_values("Idling (Jam)", ascending=False).head(10)
        
        if len(prod_data) > 0:
            chart_data = prod_data.melt(
                id_vars=['Unit', 'Total'],
                value_vars=['Motion (Jam)', 'Idling (Jam)'],
                var_name='Activity',
                value_name='Hours'
            )
            
            chart_data['Activity'] = chart_data['Activity'].replace({
                'Motion (Jam)': 'Motion',
                'Idling (Jam)': 'Idle'
            })
            
            unit_order = prod_data['Unit'].tolist()
            chart_data['Activity'] = pd.Categorical(chart_data['Activity'], categories=['Idle', 'Motion'], ordered=True)
            chart_data = chart_data.sort_values(['Unit', 'Activity'])
            
            # Hitung posisi mid-point untuk label
            chart_data_sorted = chart_data.sort_values(['Unit', 'Activity']).copy()
            chart_data_sorted['cumsum'] = chart_data_sorted.groupby('Unit')['Hours'].cumsum()
            chart_data_sorted['y_mid'] = chart_data_sorted['cumsum'] - (chart_data_sorted['Hours'] / 2)
            
            # Stacked Bar Chart (tanpa properties dulu)
            bars_prod = alt.Chart(chart_data_sorted).mark_bar(
                cornerRadiusTopLeft=4,
                cornerRadiusTopRight=4
            ).encode(
                x=alt.X('Unit:N', 
                    sort=unit_order, 
                    title=None, 
                    axis=alt.Axis(
                        labelAngle=-45, 
                        labelFontSize=9, 
                        labelColor='#64748b',
                        labelFontWeight=500
                    )
                ),
                y=alt.Y('Hours:Q', 
                    title='Jam', 
                    stack='zero',
                    axis=alt.Axis(
                        grid=True, 
                        gridColor='#f1f5f9', 
                        gridDash=[2, 4],
                        titleFontSize=10,
                        titleColor='#64748b'
                    )
                ),
                color=alt.Color('Activity:N', 
                    scale=alt.Scale(domain=['Idle', 'Motion'], range=['#FF4D6B', '#38CE3C']),
                    legend=alt.Legend(
                        orient='top-right',
                        direction='horizontal',
                        title=None,
                        labelFontSize=10,
                        labelColor='#64748b',
                        symbolSize=60,
                        offset=0
                    )
                ),
                order=alt.Order('Activity:N', sort='ascending'),
                tooltip=[
                    alt.Tooltip('Unit', title='Unit'),
                    alt.Tooltip('Activity', title='Aktivitas'),
                    alt.Tooltip('Hours:Q', title='Jam', format='.1f')
                ]
            )
            
            # Text label di tengah setiap segment (hanya jika cukup besar)
            text_prod = alt.Chart(chart_data_sorted[chart_data_sorted['Hours'] > 0.5]).mark_text(
                align='center',
                baseline='middle',
                fontSize=10,  # REVISI: 10px
                fontWeight=600,
                color='white'
            ).encode(
                x=alt.X('Unit:N', sort=unit_order),
                y=alt.Y('y_mid:Q'),
                text=alt.Text('Hours:Q', format='.1f')
            )
            
            # Gabung + properties dengan padding + configure
            final_chart4 = (bars_prod + text_prod).properties(
                height=280,
                padding={'left': 20, 'right': 35, 'top': 10, 'bottom': 10},
                title=alt.TitleParams(
                    text='4. Produktivitas: Rasio Jalan vs Diam',
                    anchor='start',
                    fontSize=15,
                    fontWeight=700,
                    color='#1e293b',
                    offset=10
                )
            ).configure(
                background='transparent'
            ).configure_view(stroke=None)
            
            st.altair_chart(final_chart4, use_container_width=True, theme=None)

    with col_r2_2:
        # Chart 5: Peak Hours - Vertical Bar Chart
        filtered_df['Start_Hour'] = pd.to_datetime(filtered_df['Beginning'], errors='coerce').dt.hour
        hourly_activity = filtered_df.groupby("Start_Hour").size().reset_index(name='Trip_Count')

        if len(hourly_activity) > 0:
            # Bar chart - YELLOW untuk Peak Hours
            peak_bars = alt.Chart(hourly_activity).mark_bar(
                color='#FACC15',
                cornerRadiusTopLeft=4,
                cornerRadiusTopRight=4
            ).encode(
                x=alt.X('Start_Hour:O', 
                    title='Jam', 
                    axis=alt.Axis(
                        labelAngle=0, 
                        labelFontSize=9, 
                        labelColor='#64748b',
                        labelFontWeight=500,
                        titleFontSize=10,
                        titleColor='#64748b'
                    )
                ),
                y=alt.Y('Trip_Count:Q', 
                    title='Trip',
                    axis=alt.Axis(
                        grid=True,
                        gridColor='#f1f5f9',
                        gridDash=[2, 4],
                        titleFontSize=10,
                        titleColor='#64748b'
                    )
                ),
                tooltip=[
                    alt.Tooltip('Start_Hour', title='Jam'), 
                    alt.Tooltip('Trip_Count', title='Total Trip')
                ]
            )
            
            # Text labels - Orange gelap agar terbaca
            text_peak = peak_bars.mark_text(
                align='center', 
                baseline='bottom', 
                dy=-4, 
                color='#d97706', 
                fontSize=10,  # REVISI: 10px
                fontWeight=600
            ).encode(
                text=alt.Text('Trip_Count:Q')
            )
            
            # Gabung + properties dengan padding + configure
            final_chart5 = (peak_bars + text_peak).properties(
                height=280,
                padding={'left': 20, 'right': 35, 'top': 10, 'bottom': 10},
                title=alt.TitleParams(
                    text='5. Peak Hours',
                    anchor='start',
                    fontSize=15,
                    fontWeight=700,
                    color='#1e293b',
                    offset=10
                )
            ).configure(
                background='transparent'
            ).configure_view(stroke=None)

            st.altair_chart(final_chart5, use_container_width=True, theme=None)

    st.markdown("<div style='height: 1rem;'></div>", unsafe_allow_html=True)

    # --- DATA TABLE ---
    
    table_header_col1, table_header_col2 = st.columns([4, 1])
    with table_header_col1:
        st.markdown(f"""
        <div class="table-header" style="border-bottom: none; padding-bottom: 0;">
            <div class="table-title">
                <span class="material-icons" style="color: #555;">table_chart</span>
                Detail Data ({len(filtered_df):,} rows)
            </div>
        </div>
        """, unsafe_allow_html=True)
    
    with table_header_col2:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            export_df = filtered_df[[
                "Date", "Shift", "Group", "Unit", 
                "Beginning", "Initial Location", "Final Location",
                "In Motion", "Mileage", "Idling"
            ]].copy()
            export_df.insert(0, "No", range(1, len(export_df) + 1))
            export_df.to_excel(writer, index=False, sheet_name='Idle Data')
        excel_data = output.getvalue()
        
        st.download_button(
            label="📥 Export to Excel",
            data=excel_data,
            file_name=f"Idle_Report_{start_date}_{end_date}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True
        )
    
    display_df = filtered_df[[
        "Date", "Shift", "Group", "Unit", 
        "Beginning", "Initial Location", "Final Location", 
        "In Motion", "Mileage", "Idling"
    ]].copy()
    display_df.insert(0, "No", range(1, len(display_df) + 1))
    
    st.dataframe(
        display_df, 
        use_container_width=True,
        hide_index=True,
        height=400
    )
    


    # --- FOOTER ---
    st.markdown("""
    <div class="dashboard-footer">
        © 2026 Mining Operations Dashboard. All rights reserved.
    </div>
    """, unsafe_allow_html=True)
