import streamlit as st
import time
import queue
import paho.mqtt.client as mqtt
import warnings
from streamlit_autorefresh import st_autorefresh
import json
import re
st.set_page_config("Solax Zero Export Control", layout="centered")
GLOBAL_RX_QUEUE = queue.Queue()

warnings.filterwarnings("ignore")

# =====================================================
# CONFIG
# =====================================================
MQTT_BROKER = "ecozen.ai"
MQTT_PORT = 1883

TOPIC_PREFIX = "EZMCOGX"
DEVICE_TOPICS = [f"{TOPIC_PREFIX}{i:06d}" for i in range(1, 101)]

AUTO_REFRESH_MS = 500
MAX_LOG_LINES = 100
TIMEOUT_SECONDS = 6

# =====================================================
# SESSION STATE INIT
# =====================================================
def init_state():
    defaults = {
        "mqtt_client": None,
        "connected": False,
        "command_topic": None,
        "response_topic": None,
        # "rx_queue": queue.Queue(),
        "response_log": [],              # (ts, payload)
        "parse_debug": [],
        "parsed_payloads": set(),

        "ct_power": None,
        "export_limit": None,

        "pending_register": None,        # "1032" | "0802"
        "pending_action": None,          # read_ct | read_export | enable | disable
        "expected_export_value": None,
        "pending_since": None,
    }

    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()

# =====================================================
# MQTT SETUP
# =====================================================
def mqtt_connect(device_id):
    if st.session_state.mqtt_client:
        return

    cmd_topic = f"/AC/5/{device_id}/Command"
    rsp_topic = f"/AC/5/{device_id}/Response"

    client = mqtt.Client()

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(rsp_topic)
            GLOBAL_RX_QUEUE.put(("CONNECTED", None))

    def on_message(client, userdata, msg):
        GLOBAL_RX_QUEUE.put(("MSG", msg.payload.decode(errors="ignore")))

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    st.session_state.mqtt_client = client
    st.session_state.command_topic = cmd_topic

def publish(cmd):
    st.session_state.mqtt_client.publish(
        st.session_state.command_topic,
        cmd,
        qos=1
    )

# =====================================================
# RX QUEUE DRAIN
# =====================================================
def drain_rx_queue():
    while not GLOBAL_RX_QUEUE.empty():
        event, payload = GLOBAL_RX_QUEUE.get()

        if event == "CONNECTED":
            st.session_state.connected = True

        elif event == "MSG":
            st.session_state.response_log.append((time.time(), payload))
            st.session_state.response_log = st.session_state.response_log[-MAX_LOG_LINES:]

# =====================================================
# RESPONSE PARSING
# =====================================================
def extract_register_value(payload: str, register: str):
    debug = st.session_state.parse_debug

    debug.append("---- NEW PAYLOAD ----")
    debug.append(payload)

    rsp = None

    try:
        data = json.loads(payload)
        rsp = data.get("rsp", "")
        debug.append("Parsed via json.loads()")
    except Exception as e:
        debug.append(f"JSON decode failed: {e}")
        match = re.search(r'"rsp"\s*:\s*"([\s\S]*)"\s*}', payload)
        if not match:
            debug.append("Failed to extract rsp")
            return None
        rsp = match.group(1)
        debug.append("Parsed rsp via regex fallback")

    debug.append("Parsed rsp:")
    debug.append(rsp)

    if "READ PROCESSING" in rsp:
        debug.append("Found READ PROCESSING → ignored")
        return None

    for line in rsp.splitlines():
        line = line.strip()
        debug.append(f"Checking line: {line}")
        if line.startswith(f"{register}:"):
            value = int(line.split(":")[1])
            debug.append(f"✅ MATCH FOUND → {register} = {value}")
            return value

    return None

if st.session_state.mqtt_client:
    st_autorefresh(interval=AUTO_REFRESH_MS, key="mqtt_refresh")
    # drain_rx_queue()
    
# =====================================================
# EVENT-DRIVEN PARSER
# =====================================================
if st.session_state.pending_register:
    now = time.time()

    if now - st.session_state.pending_since > TIMEOUT_SECONDS:
        st.session_state.parse_debug.append("⏱️ Timeout waiting for response")
        st.session_state.pending_register = None
        st.session_state.pending_action = None
        st.session_state.expected_export_value = None
        st.session_state.parsed_payloads.clear()

    else:
        for ts, payload in st.session_state.response_log:
            if payload in st.session_state.parsed_payloads:
                continue

            st.session_state.parsed_payloads.add(payload)
            value = extract_register_value(payload, st.session_state.pending_register)

            if value is None:
                continue

            # ----------------------------
            # READ CT → then READ EXPORT
            # ----------------------------
            if st.session_state.pending_action == "read_ct":
                st.session_state.ct_power = value

                publish("READ03**12345##1234567890,0802")
                st.session_state.pending_register = "0802"
                st.session_state.pending_action = "read_export"
                st.session_state.pending_since = time.time()
                st.session_state.parsed_payloads.clear()
                break

            elif st.session_state.pending_action == "read_export":
                st.session_state.export_limit = value
                st.session_state.pending_register = None
                st.session_state.pending_action = None
                st.session_state.parsed_payloads.clear()
                break

            # ----------------------------
            # ENABLE / DISABLE VALIDATION
            # ----------------------------
            elif st.session_state.pending_action in ("enable", "disable"):
                st.session_state.export_limit = value

                if value == st.session_state.expected_export_value:
                    st.success("Export setting updated successfully")
                else:
                    st.error("Export setting update failed")

                st.session_state.pending_register = None
                st.session_state.pending_action = None
                st.session_state.expected_export_value = None
                st.session_state.parsed_payloads.clear()
                break

# =====================================================
# UI
# =====================================================
st.title("🔌 Solax Inverter – Zero Export Control")

device = st.selectbox("Select Device", DEVICE_TOPICS)

if st.button("Connect", disabled=st.session_state.connected):
    mqtt_connect(device)

# Auto refresh if client exists
if st.session_state.mqtt_client:
    st_autorefresh(interval=AUTO_REFRESH_MS, key="mqtt_refresh")

# 🔴 CRITICAL: always drain BEFORE checking connected
drain_rx_queue()

if not st.session_state.connected:
    st.warning("Not connected")
    st.stop()

st.success("Connected to MQTT")

# =====================================================
# DEBUG
# =====================================================
with st.expander("📡 Raw MQTT Responses"):
    st.text_area(
        "Responses",
        value="\n\n---\n\n".join(p for _, p in st.session_state.response_log),
        height=300
    )

with st.expander("🧪 Parsing Debug Trace"):
    st.text_area(
        "Parser activity",
        value="\n".join(st.session_state.parse_debug),
        height=400
    )

# =====================================================
# INVERTER SETTINGS
# =====================================================
st.divider()
st.subheader("Inverter Settings")

if st.button("Update"):
    st.session_state.parse_debug.clear()
    st.session_state.parsed_payloads.clear()

    publish("READ04**12345##1234567890,1032")
    st.session_state.pending_register = "1032"
    st.session_state.pending_action = "read_ct"
    st.session_state.pending_since = time.time()

ct_enabled = "Yes" if st.session_state.ct_power not in (None, 0) else "No"

st.text_input("CT Enabled", ct_enabled, disabled=True)
st.text_input(
    "Export Limit (W)",
    str(st.session_state.export_limit) if st.session_state.export_limit is not None else "",
    disabled=True
)

# =====================================================
# ZERO EXPORT CONTROL
# =====================================================
st.divider()
st.subheader("Zero Export Control")

can_control = (
    st.session_state.ct_power not in (None, 0)
    and st.session_state.export_limit is not None
)

col1, col2 = st.columns(2)

with col1:
    enable_clicked = st.button(
        "Enable Zero Export (1W)",
        disabled=not can_control
    )

with col2:
    disable_clicked = st.button(
        "Disable Zero Export (10000W)",
        disabled=not can_control
    )

if enable_clicked:
    st.session_state.parse_debug.clear()
    st.session_state.parsed_payloads.clear()

    publish("UP#,1536:02014")
    publish("UP#,1540:00001")
    publish("UP#,1536:00001")
    publish("READ03**12345##1234567890,0802")

    st.session_state.pending_register = "0802"
    st.session_state.pending_action = "enable"
    st.session_state.expected_export_value = 1
    st.session_state.pending_since = time.time()

if disable_clicked:
    st.session_state.parse_debug.clear()
    st.session_state.parsed_payloads.clear()

    publish("UP#,1536:02014")
    publish("UP#,1540:10000")
    publish("UP#,1536:00001")
    publish("READ03**12345##1234567890,0802")

    st.session_state.pending_register = "0802"
    st.session_state.pending_action = "disable"
    st.session_state.expected_export_value = 10000
    st.session_state.pending_since = time.time()



