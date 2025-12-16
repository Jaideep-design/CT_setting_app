import streamlit as st
import time
import queue
import paho.mqtt.client as mqtt
import warnings
from streamlit_autorefresh import st_autorefresh

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

# =====================================================
# SESSION STATE INIT
# =====================================================
def init_state():
    defaults = {
        "mqtt_client": None,
        "connected": False,
        "command_topic": None,
        "response_topic": None,
        "rx_queue": queue.Queue(),
        "response_log": [],
        "ct_power": None,
        "export_limit": None,
        "last_cmd_ts": None,
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

    command_topic = f"/AC/5/{device_id}/Command"
    response_topic = f"/AC/5/{device_id}/Response"
    rx_queue = st.session_state.rx_queue

    client = mqtt.Client()

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(response_topic)
            rx_queue.put(("CONNECTED", None))

    def on_message(client, userdata, msg):
        rx_queue.put(("MSG", msg.payload.decode(errors="ignore")))

    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    st.session_state.mqtt_client = client
    st.session_state.command_topic = command_topic
    st.session_state.response_topic = response_topic

def publish(cmd):
    st.session_state.last_cmd_ts = time.time()
    st.session_state.mqtt_client.publish(
        st.session_state.command_topic,
        cmd,
        qos=1
    )

# =====================================================
# RESPONSE HANDLING
# =====================================================
def extract_register_value(payload: str, register: str):
    if not payload:
        return None

    try:
        data = json.loads(payload)
        rsp = data.get("rsp", "")
    except json.JSONDecodeError:
        rsp = payload  # fallback for non-JSON messages

    for line in rsp.splitlines():
        line = line.strip()
        if line.startswith(f"{register}:"):
            try:
                return int(line.split(":")[1])
            except ValueError:
                return None

    return None

def wait_for_register(register, timeout=6):
    start = time.time()
    seen = set()

    while time.time() - start < timeout:

        # 🔥 CRITICAL: process MQTT messages WHILE waiting
        while not st.session_state.rx_queue.empty():
            event, payload = st.session_state.rx_queue.get()

            if event == "MSG":
                st.session_state.response_log.append(payload)
                st.session_state.response_log = st.session_state.response_log[-100:]

        # Now parse updated log
        for payload in st.session_state.response_log:
            if payload in seen:
                continue
            seen.add(payload)

            value = extract_register_value(payload, register)
            if value is not None:
                return value

        time.sleep(0.1)

    return None

# =====================================================
# UI
# =====================================================
st.set_page_config("Solax Zero Export Control", layout="centered")
st.title("🔌 Solax Inverter – Zero Export Control")

device = st.selectbox("Select Device", DEVICE_TOPICS)

if st.button("Connect", disabled=st.session_state.connected):
    mqtt_connect(device)

# =====================================================
# AUTO REFRESH (CRITICAL)
# =====================================================
if st.session_state.mqtt_client:
    st_autorefresh(interval=AUTO_REFRESH_MS, key="mqtt_refresh")

# =====================================================
# PROCESS MQTT EVENTS
# =====================================================
while not st.session_state.rx_queue.empty():
    event, payload = st.session_state.rx_queue.get()

    if event == "CONNECTED":
        st.session_state.connected = True

    elif event == "MSG":
        st.session_state.response_log.append(payload)
        st.session_state.response_log = st.session_state.response_log[-MAX_LOG_LINES:]

# =====================================================
# STATUS
# =====================================================
if st.session_state.connected:
    st.success("Connected to MQTT")
else:
    st.warning("Not connected")
    st.stop()

# =====================================================
# DEBUG VIEW
# =====================================================
with st.expander("📡 Raw MQTT Responses"):
    st.text_area(
        "Responses",
        value="\n\n---\n\n".join(st.session_state.response_log),
        height=300
    )

# =====================================================
# INVERTER SETTINGS
# =====================================================
st.divider()
st.subheader("Inverter Settings")

if st.button("Update"):
    with st.spinner("Reading CT & Export limit..."):
        publish("READ04**12345##1234567890,1032")
        st.session_state.ct_power = wait_for_register("1032")

        publish("READ03**12345##1234567890,0802")
        st.session_state.export_limit = wait_for_register("0802")

ct_enabled = "Yes" if st.session_state.ct_power not in (None, 0) else "No"
st.text_input("CT Enabled", ct_enabled, disabled=True)

st.text_input(
    "Export Limit (W)",
    str(st.session_state.export_limit or ""),
    disabled=True
)

# =====================================================
# ZERO EXPORT CONTROL
# =====================================================
st.divider()

if ct_enabled == "Yes":
    new_val = st.number_input(
        "Set Export Limit (W)",
        min_value=1,
        max_value=10000,
        value=st.session_state.export_limit or 1
    )

    if st.button("Apply Export Setting"):
        with st.spinner("Applying..."):
            publish("UP#,1536:02014")
            publish(f"UP#,1540:{new_val:05d}")
            publish("UP#,1536:00001")

            publish("READ03**12345##1234567890,0802")
            verify = wait_for_register("0802")

        if verify == new_val:
            st.success("Export value updated successfully")
            st.session_state.export_limit = verify
        else:
            st.error("Export update failed")
else:
    st.info("CT not enabled. Zero export unavailable.")


