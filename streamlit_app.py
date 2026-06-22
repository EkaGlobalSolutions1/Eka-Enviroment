import time
from datetime import datetime, timedelta


import requests
import pandas as pd
import streamlit as st
import plotly.graph_objects as go


# =========================
# CONFIG
# =========================
st.set_page_config(page_title="EKA – IOE en tiempo real (CO₂ + METEO)", layout="wide")


st.title("EKA – IOE en tiempo real (Sensores CO₂ + METEO)")
st.caption("IOE global + semáforo + ranking CO₂ + meteorología (auto-refresh)")


# =========================
# SIDEBAR – DEMASE (CO2)
# =========================
st.sidebar.header("Conexión DEMASE (CO₂)")


demase_base_url = st.sidebar.text_input("Base URL DEMASE", "https://www.demasesl.com").strip().rstrip("/")
demase_token = st.sidebar.text_input("Token DEMASE", type="password").strip()


# =========================
# SIDEBAR – METEO (Pentaho CDA)
# =========================
st.sidebar.header("Conexión METEO (La Palma – Pentaho CDA)")


meteo_enabled = st.sidebar.toggle("Integrar METEO", value=True)


meteo_base = st.sidebar.text_input(
    "Base URL METEO",
    "https://bi.lapalma.es/pentaho/plugin/cda/api/doQuery",
).strip()


meteo_path = st.sidebar.text_input(
    "path (CDA)",
    "/public/sc_lapalma/verticals/sql/environment.cda",
).strip()


meteo_trust_user = st.sidebar.text_input("_TRUST_USER_", "opendata_sc_lapalma").strip()


# =========================
# SIDEBAR – TIEMPO REAL
# =========================
st.sidebar.header("Tiempo real")
auto_refresh = st.sidebar.toggle("Auto-refresh", value=True)
refresh_seconds = st.sidebar.slider("Refresh (segundos)", 2, 30, 5)
window_size = st.sidebar.slider("Ventana (puntos) IOE global", 5, 300, 80)


# =========================
# SIDEBAR – UMBRALES
# =========================
st.sidebar.header("Semáforo (IOE global)")
ioe_red = st.sidebar.slider("ROJO si IOE < ", 0.0, 1.0, 0.35)
ioe_yellow = st.sidebar.slider("AMARILLO si IOE < ", 0.0, 1.0, 0.55)


if ioe_yellow <= ioe_red:
    st.sidebar.warning("Ajuste automático: AMARILLO debe ser mayor que ROJO.")
    ioe_yellow = min(1.0, ioe_red + 0.05)


st.sidebar.header("Peso en IOE global")
w_co2 = st.sidebar.slider("Peso CO₂", 0.0, 1.0, 0.75)
w_meteo = 1.0 - w_co2
st.sidebar.caption(f"Peso METEO = {w_meteo:.2f}")


# =========================
# HELPERS
# =========================
def classify_semaphore(ioe_value: float) -> str:
    if ioe_value is None or pd.isna(ioe_value):
        return "⚪ Sin datos"
    if ioe_value < ioe_red:
        return "🔴 ROJO"
    if ioe_value < ioe_yellow:
        return "🟡 AMARILLO"
    return "🟢 VERDE"


def safe_float(x):
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


# =========================
# DEMASE (CO2)
# =========================
def fetch_demase_data(base_url: str, token: str):
    """Devuelve una LISTA de lecturas DEMASE o [] y muestra diagnóstico."""
    if not base_url:
        st.sidebar.error("DEMASE: Base URL vacía.")
        return []
    if not token:
        st.sidebar.warning("DEMASE: Token vacío (pégalo en la barra lateral).")
        return []


    url = f"{base_url}/datos_actuales"
    params = {"token": token}


    with st.sidebar.expander("🧪 Diagnóstico DEMASE", expanded=False):
        st.write("URL:", url)
        st.write("Token length:", len(token))


        try:
            r = requests.get(url, params=params, timeout=20)
            st.write("HTTP status:", r.status_code)
            st.write("Final URL:", r.url)


            if r.status_code != 200:
                st.error(f"DEMASE: respuesta no OK ({r.status_code})")
                st.text(r.text[:1000])
                return []


            data = r.json()
            if isinstance(data, list):
                st.success(f"DEMASE JSON OK (lista) - items: {len(data)}")
                st.json(data[:2])
                return data


            st.warning("DEMASE: JSON recibido pero no es lista.")
            st.write(type(data))
            st.json(data)
            return []


        except Exception as e:
            st.error(f"DEMASE: error request/JSON: {repr(e)}")
            return []


def flatten_demase_rows(rows: list) -> pd.DataFrame:
    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue


        valores = row.get("valores") or {}
        if not isinstance(valores, dict):
            valores = {}


        ts = row.get("Ts", None)
        dt = pd.to_datetime(ts, unit="s", errors="coerce") if ts is not None else pd.NaT


        records.append(
            {
                "sensor_id": valores.get("Id", None),
                "timestamp": dt,
                "co2": pd.to_numeric(valores.get("Co2", None), errors="coerce"),
                "temp": pd.to_numeric(valores.get("temp", None), errors="coerce"),
                "bat": pd.to_numeric(valores.get("bat", None), errors="coerce"),
                "co2_pct": pd.to_numeric(valores.get("co2%", None), errors="coerce"),
                "altura": row.get("Altura", None),
            }
        )


    df = pd.DataFrame(records)
    if not df.empty:
        df = df.dropna(subset=["sensor_id"])
        df["sensor_id"] = df["sensor_id"].astype(int)
    return df


def compute_ioe_co2(df: pd.DataFrame) -> pd.DataFrame:
    """
    IOE DEMO (CO₂):
    - CO2 alto => baja C
    - temp alta + datos faltantes => sube A
    """
    out = df.copy()


    out["missing_co2"] = out["co2"].isna().astype(int)
    out["missing_temp"] = out["temp"].isna().astype(int)


    out["co2_f"] = out["co2"].fillna(0)
    out["temp_f"] = out["temp"].fillna(0)
    out["bat_f"] = out["bat"].fillna(0)


    out["co2_norm"] = (out["co2_f"] / 2000).clip(0, 1)   # 0..2000 ppm
    out["temp_norm"] = (out["temp_f"] / 50).clip(0, 1)   # 0..50 ºC
    out["bat_norm"] = (out["bat_f"] / 20).clip(0, 1)     # 0..20 V aprox


    out["C"] = (1 - out["co2_norm"]).clip(0, 1)
    out["A"] = (0.35 * out["temp_norm"] + 0.25 * out["missing_co2"] + 0.15 * out["missing_temp"]).clip(0, 1)
    out["IOE"] = (out["C"] - out["A"]).clip(0, 1)


    out["sensor_name"] = out["sensor_id"].apply(lambda x: f"Sensor {x}")
    return out


# =========================
# METEO (Pentaho CDA)
# =========================
def pentaho_doquery(base_url: str, path: str, trust_user: str, data_access_id: str, extra_params: dict | None = None):
    """
    Llama a Pentaho CDA doQuery y devuelve dict JSON o {}.
    Estructura típica: {"resultset": [...], "metadata": [...], ...}
    """
    params = {
        "path": path,
        "_TRUST_USER_": trust_user,
        "dataAccessId": data_access_id,
        "outputType": "json",
    }
    if extra_params:
        params.update(extra_params)


    try:
        r = requests.get(base_url, params=params, timeout=25)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.sidebar.error(f"METEO: error {data_access_id}: {repr(e)}")
        return {}


def pentaho_json_to_df(payload: dict) -> pd.DataFrame:
    """
    Convierte respuesta Pentaho CDA a DataFrame usando metadata/resultset.
    """
    if not isinstance(payload, dict):
        return pd.DataFrame()


    meta = payload.get("metadata", None)
    rs = payload.get("resultset", None)


    if not isinstance(meta, list) or not isinstance(rs, list):
        return pd.DataFrame()


    colnames = []
    for m in meta:
        # suele venir como {"colName": "...", ...}
        colnames.append(m.get("colName", f"col_{len(colnames)}"))


    df = pd.DataFrame(rs, columns=colnames)
    return df


def fetch_meteo_stations():
    # dataAccessId=weatherobserved_stations (doc)
    payload = pentaho_doquery(
        meteo_base,
        meteo_path,
        meteo_trust_user,
        "weatherobserved_stations",
        extra_params=None,
    )
    return pentaho_json_to_df(payload)


def fetch_meteo_lastdata(entityid: str | None):
    # dataAccessId=weatherobserved_lastdata (doc) + params opcionales
    # Si no pasas entityid devuelve todas las estaciones (doc).
    now = datetime.utcnow()
    start = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    finish = now.strftime("%Y-%m-%d %H:%M:%S")


    extra = {
        "paramentityid": entityid or "",
        "paramstart": start,
        "paramfinish": finish,
        "parammunicipality": "",
    }


    payload = pentaho_doquery(
        meteo_base,
        meteo_path,
        meteo_trust_user,
        "weatherobserved_lastdata",
        extra_params=extra,
    )
    return pentaho_json_to_df(payload)


def compute_ioe_meteo(meteo_row: pd.Series) -> float | None:
    """
    IOE METEO (demo robusta):
    - Penaliza viento alto, precipitación alta, temperatura extrema.
    Devuelve IOE 0..1 (aprox).
    """
    if meteo_row is None or meteo_row.empty:
        return None


    # Heurística: intenta encontrar columnas típicas si existen
    cols = {c.lower(): c for c in meteo_row.index}


    def pick(*names):
        for n in names:
            if n in cols:
                return safe_float(meteo_row[cols[n]])
        return None


    temp = pick("temperature", "airtemperature", "temp", "tempc", "t")
    wind = pick("windspeed", "wind_speed", "wind")
    rain = pick("precipitation", "rain", "dailyprecipitation")


    # Normalizaciones (ajusta si quieres)
    # temp: penaliza por extremos (0..50)
    if temp is None:
        temp_pen = 0.15  # si falta, pequeña penalización
    else:
        # ideal 18..26, fuera penaliza
        if 18 <= temp <= 26:
            temp_pen = 0.0
        else:
            temp_pen = min(0.6, abs(temp - 22) / 30)


    # viento: 0..20 m/s aprox (si tu unidad es distinta, ajusta)
    if wind is None:
        wind_pen = 0.10
    else:
        wind_pen = min(0.6, wind / 20)


    # lluvia: 0..50 mm/día (aprox)
    if rain is None:
        rain_pen = 0.05
    else:
        rain_pen = min(0.6, rain / 50)


    A = min(1.0, 0.45 * temp_pen + 0.35 * wind_pen + 0.20 * rain_pen)
    C = max(0.0, 1.0 - A)
    ioe = max(0.0, min(1.0, C - 0.15 * A))
    return ioe


# =========================
# ESTADO (histórico)
# =========================
if "history" not in st.session_state:
    st.session_state.history = []  # {"time":..., "ioe":..., "ioe_co2":..., "ioe_meteo":...}


# =========================
# FETCH + COMPUTE
# =========================
# --- CO2
rows = fetch_demase_data(demase_base_url, demase_token)
df_co2 = flatten_demase_rows(rows)
ioe_co2 = None


if not df_co2.empty:
    df_co2 = compute_ioe_co2(df_co2)
    ioe_co2 = float(df_co2["IOE"].mean())


# --- METEO
df_stations = pd.DataFrame()
df_meteo_last = pd.DataFrame()
selected_station = None
ioe_meteo = None


if meteo_enabled:
    with st.sidebar.expander("🧪 Diagnóstico METEO", expanded=False):
        st.write("Base:", meteo_base)
        st.write("path:", meteo_path)
        st.write("_TRUST_USER_:", meteo_trust_user)


    df_stations = fetch_meteo_stations()


    station_options = []
    if not df_stations.empty:
        # el doc dice que existe "entityid" y "name" :contentReference[oaicite:3]{index=3}
        if "entityid" in df_stations.columns:
            if "name" in df_stations.columns:
                station_options = [
                    f"{row['name']}  ({row['entityid']})" for _, row in df_stations.iterrows()
                ]
            else:
                station_options = [str(x) for x in df_stations["entityid"].tolist()]


    if station_options:
        pick_label = st.sidebar.selectbox("Estación METEO", options=station_options, index=0)
        # extrae entityid entre paréntesis si existe
        if "(" in pick_label and pick_label.endswith(")"):
            selected_station = pick_label.split("(")[-1].rstrip(")")
        else:
            selected_station = pick_label


        df_meteo_last = fetch_meteo_lastdata(selected_station)
    else:
        # si no hay estaciones, intenta igualmente lastdata sin entityid (todas)
        df_meteo_last = fetch_meteo_lastdata(None)


    # tomar 1 fila para IOE meteo
    if not df_meteo_last.empty:
        meteo_row = df_meteo_last.iloc[0]
        ioe_meteo = compute_ioe_meteo(meteo_row)


# =========================
# IOE GLOBAL (COMBINADO)
# =========================
global_ioe = None
if ioe_co2 is not None and (not meteo_enabled or ioe_meteo is None):
    global_ioe = ioe_co2
elif meteo_enabled and ioe_meteo is not None and ioe_co2 is None:
    global_ioe = ioe_meteo
elif ioe_co2 is not None and meteo_enabled and ioe_meteo is not None:
    global_ioe = (w_co2 * ioe_co2) + (w_meteo * ioe_meteo)


# guardar histórico
if global_ioe is not None:
    st.session_state.history.append(
        {"time": datetime.now(), "ioe": global_ioe, "ioe_co2": ioe_co2, "ioe_meteo": ioe_meteo}
    )


history_df = pd.DataFrame(st.session_state.history).tail(window_size)


# =========================
# KPIs (TOP)
# =========================
k1, k2, k3, k4, k5, k6 = st.columns([1.2, 1.2, 1, 1, 1, 1])


k1.metric("IOE GLOBAL", f"{global_ioe:.3f}" if global_ioe is not None else "—")
k2.metric("Semáforo Global", classify_semaphore(global_ioe) if global_ioe is not None else "⚪ Sin datos")
k3.metric("IOE CO₂", f"{ioe_co2:.3f}" if ioe_co2 is not None else "—")
k4.metric("IOE METEO", f"{ioe_meteo:.3f}" if ioe_meteo is not None else "—")
k5.metric("Sensores CO₂ activos", int(len(df_co2)) if not df_co2.empty else 0)


if not df_co2.empty:
    n_red = int((df_co2["IOE"] < ioe_red).sum())
else:
    n_red = 0
k6.metric("CO₂ en ROJO", n_red)


# =========================
# EVOLUCIÓN (IOE GLOBAL)
# =========================
st.subheader("Evolución en tiempo real (Sistema)")


if not history_df.empty:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=history_df["time"], y=history_df["ioe"], mode="lines+markers", name="IOE Global"))
    if "ioe_co2" in history_df.columns and history_df["ioe_co2"].notna().any():
        fig.add_trace(go.Scatter(x=history_df["time"], y=history_df["ioe_co2"], mode="lines", name="IOE CO₂"))
    if "ioe_meteo" in history_df.columns and history_df["ioe_meteo"].notna().any():
        fig.add_trace(go.Scatter(x=history_df["time"], y=history_df["ioe_meteo"], mode="lines", name="IOE METEO"))


    fig.update_layout(
        height=380,
        xaxis_title="Tiempo",
        yaxis_title="IOE",
        template="plotly_white",
        margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h"),
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Aún no hay histórico (espera 2–3 refrescos).")


# =========================
# BLOQUE METEO (TABLA)
# =========================
if meteo_enabled:
    st.subheader("METEO – última medida")
    if selected_station:
        st.caption(f"Estación seleccionada: {selected_station}")


    if not df_meteo_last.empty:
        st.dataframe(df_meteo_last.head(10), use_container_width=True, hide_index=True)
    else:
        st.info("METEO: sin datos (revisa base/path/_TRUST_USER_).")


# =========================
# RANKING CO2
# =========================
st.subheader("CO₂ – Sensores en riesgo (ranking)")


if not df_co2.empty:
    df_rank = df_co2.sort_values("IOE", ascending=True).copy()
    df_rank["Semáforo"] = df_rank["IOE"].apply(classify_semaphore)


    st.dataframe(
        df_rank[["sensor_name", "sensor_id", "co2", "temp", "bat", "IOE", "Semáforo"]].head(25),
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("CO₂: no hay datos (revisa DEMASE Base URL y Token).")


# =========================
# DETALLE SENSOR CO2 MÁS CRÍTICO
# =========================
st.subheader("CO₂ – Detalle del sensor más crítico")


if not df_co2.empty:
    worst = df_co2.sort_values("IOE").iloc[0]
    cols = st.columns(5)
    cols[0].metric("Sensor", str(worst["sensor_id"]))
    cols[1].metric("CO₂ (ppm)", "—" if pd.isna(worst["co2"]) else f"{worst['co2']:.0f}")
    cols[2].metric("Temp (ºC)", "—" if pd.isna(worst["temp"]) else f"{worst['temp']:.1f}")
    cols[3].metric("Bat", "—" if pd.isna(worst["bat"]) else f"{worst['bat']:.2f}")
    cols[4].metric("IOE", f"{worst['IOE']:.3f}  {classify_semaphore(worst['IOE'])}")
else:
    st.info("CO₂: sin datos del sensor crítico.")

# =========================
# SOFÍA – ASISTENTE EKA
# =========================
# =========================
# SOFÍA – ASISTENTE EKA
# =========================

st.subheader("🤖 SOFÍA – Asistente Inteligente EKA")

# Guardar historial de conversación
if "sofia_messages" not in st.session_state:
    st.session_state.sofia_messages = [
        {
            "role": "assistant",
            "content": (
                "Hola, soy SOFÍA, tu asistente de Inteligencia Predictiva EKA.\n\n"
                "Estoy especializada en la monitorización ambiental de CO₂ y meteorología.\n\n"
                "Puedo ayudarte a interpretar el IOE ambiental, el estado de los sensores, "
                "las condiciones METEO y recomendar acciones preventivas."
            )
        }
    ]


# =========================
# Función de respuesta SOFÍA
# =========================

def respuesta_sofia(pregunta):

    pregunta = pregunta.lower()

    # Estado actual
    if global_ioe is not None:
        estado = classify_semaphore(global_ioe)
        ioe_texto = f"{global_ioe:.3f}"
    else:
        estado = "⚪ Sin datos"
        ioe_texto = "No disponible"


    # Sensor con menor IOE
    sensor_info = None

    if not df_co2.empty:
        peor = df_co2.sort_values("IOE").iloc[0]

        sensor_info = {
            "id": int(peor["sensor_id"]),
            "ioe": peor["IOE"],
            "co2": peor["co2"],
            "temp": peor["temp"],
            "bat": peor["bat"]
        }


    # Saludo
    if "hola" in pregunta:
        return (
            "Hola. Soy SOFÍA EKA. Actualmente analizo la estabilidad "
            "de la monitorización de CO₂ y meteorología. "
            "¿Qué quieres conocer?"
        )


    # Estado general
    elif "cómo está" in pregunta or "estado" in pregunta:
        return (
            f"El sistema presenta un estado {estado}. "
            f"El IOE ambiental global es {ioe_texto}."
        )


    # IOE
    elif "ioe" in pregunta:
        return (
            f"El Índice Operativo EKA actual es {ioe_texto}. "
            "Este indicador resume la estabilidad del sistema considerando "
            "los datos de CO₂, sensores y condiciones meteorológicas."
        )


    # Anomalías
    elif "anomalía" in pregunta or "alerta" in pregunta:

        if "VERDE" in estado:
            return (
                "El sistema se encuentra en estado verde. "
                "Actualmente no existen anomalías significativas en la "
                "monitorización ambiental."
            )

        else:
            return (
                f"El sistema muestra un estado {estado}. "
                "Se han detectado variaciones que pueden estar relacionadas "
                "con CO₂, temperatura, batería del sensor o condiciones METEO."
            )


    # Riesgo
    elif "riesgo" in pregunta:

        if "VERDE" in estado:
            return (
                "El nivel de riesgo es bajo. El sistema se encuentra estable."
            )

        return (
            f"El nivel de riesgo actual es {estado}. "
            "Se recomienda vigilancia reforzada."
        )


    # Sensor crítico
    elif "sensor" in pregunta or "crítico" in pregunta:

        if sensor_info:

            return (
                f"El sensor con menor IOE relativo es el Sensor {sensor_info['id']}.\n\n"
                f"IOE: {sensor_info['ioe']:.3f}\n"
                f"CO₂: {sensor_info['co2']:.0f} ppm\n"
                f"Temperatura: {sensor_info['temp']:.1f} °C\n"
                f"Batería: {sensor_info['bat']:.2f} V\n\n"
                "Este valor representa el punto menos estable del conjunto, "
                "aunque no implica necesariamente una incidencia."
            )

        return "Actualmente no hay datos disponibles de sensores."


    # CO2
    elif "co2" in pregunta or "co₂" in pregunta:

        if sensor_info:
            return (
                f"El sensor analizado con menor estabilidad registra "
                f"{sensor_info['co2']:.0f} ppm de CO₂."
            )

        return "No dispongo de datos de CO₂ en este momento."


    # METEO
    elif "meteo" in pregunta or "meteorología" in pregunta:

        if ioe_meteo is not None:
            return (
                f"El IOE asociado a las condiciones meteorológicas "
                f"es {ioe_meteo:.3f}."
            )

        return "Actualmente no hay datos meteorológicos disponibles."


    # Recomendaciones
    elif "acción" in pregunta or "recomienda" in pregunta or "qué hago" in pregunta:

        if "VERDE" in estado:
            return (
                "El sistema está estable. Recomiendo mantener la monitorización "
                "continua, comprobar la calidad de los datos y seguir observando "
                "la evolución del IOE."
            )

        return (
            "Recomiendo:\n"
            "1. Revisar sensores con menor IOE.\n"
            "2. Comprobar valores elevados de CO₂.\n"
            "3. Verificar temperatura, batería y comunicaciones.\n"
            "4. Analizar la influencia de las condiciones meteorológicas.\n"
            "5. Mantener vigilancia reforzada."
        )


    # Si pregunta por agua o fugas
    elif "fuga" in pregunta or "agua" in pregunta:

        return (
            "Este dashboard está configurado actualmente para monitorización "
            "ambiental de CO₂ y meteorología, no para una red hidráulica."
        )


    # Ayuda general
    else:

        return (
            "Puedes preguntarme:\n\n"
            "• ¿Cómo está el sistema?\n"
            "• ¿Cuál es el IOE actual?\n"
            "• ¿Existe alguna anomalía?\n"
            "• ¿Cuál es el sensor más crítico?\n"
            "• ¿Cómo están los niveles de CO₂?\n"
            "• ¿Qué indican los datos METEO?\n"
            "• ¿Qué acciones recomiendas?"
        )


# =========================
# Mostrar historial del chat
# =========================

for mensaje in st.session_state.sofia_messages:
    with st.chat_message(mensaje["role"]):
        st.markdown(mensaje["content"])


# Entrada de preguntas
pregunta_usuario = st.chat_input(
    "Pregunta a SOFÍA sobre CO₂, meteorología o el IOE ambiental..."
)


# Procesar pregunta
if pregunta_usuario:

    st.session_state.sofia_messages.append(
        {
            "role": "user",
            "content": pregunta_usuario
        }
    )

    respuesta = respuesta_sofia(pregunta_usuario)

    st.session_state.sofia_messages.append(
        {
            "role": "assistant",
            "content": respuesta
        }
    )


# =========================
# AUTO-REFRESH
# =========================
if auto_refresh:
    time.sleep(refresh_seconds)
    st.rerun()
