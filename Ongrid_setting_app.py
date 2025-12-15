import streamlit as st
import time
import queue
import paho.mqtt.client as mqtt
from streamlit_autorefresh import st_autorefresh
import warnings
warnings.filterwarnings("ignore")

# =====================================================
# CONFIG
# =====================================================
MQTT_BROKER = "ecozen.ai"
MQTT_PORT = 1883

TOPIC_PREFIX = "EZMCOGX"
DEVICE_TOPICS = [f"{TOPIC_PREFIX}{i:06d}" for i in range(1, 101)]

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
        "last_response": None,
        "response_log": [],
        "ct_power": None,
        "export_limit": None,
        "waiting_for_register": None,   # 👈 key flag
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()

# =====================================================
# MQTT SETUP (NO STREAMLIT IN CALLBACKS)
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
    st.session_state.mqtt_client.publish(
        st.session_state.command_topic,
        cmd,
        qos=1
    )

# =====================================================
# SOLAX PARSING
# =====================================================
def extract_register_value(payload: str, register: str):
    if not payload:
        return None

    for line in payload.splitlines():
        line = line.strip()
        if line.startswith(f"{register}:"):
            try:
                return int(line.split(":")[1])
            except ValueError:
                return None
    return None

# =====================================================
# UI
# =====================================================
st.set_page_config(page_title="Solax Zero Export Control", layout="centered")
st.title("Solax Inverter – Zero Export Control")

device = st.selectbox("Select Device Topic", DEVICE_TOPICS)

if st.button("Connect", disabled=st.session_state.connected):
    mqtt_connect(device)

# =====================================================
# TARGETED AUTO REFRESH (ONLY WHEN WAITING)
# =====================================================
if st.session_state.waiting_for_register:
    st_autorefresh(interval=500, key="wait_refresh")

# =====================================================
# PROCESS MQTT EVENTS (MAIN THREAD)
# =====================================================
while not st.session_state.rx_queue.empty():
    event, payload = st.session_state.rx_queue.get()

    if event == "CONNECTED":
        st.session_state.connected = True

    elif event == "MSG":
        st.session_state.last_response = payload
        st.session_state.response_log.append(payload)

        # Check if we are waiting for a specific register
        reg = st.session_state.waiting_for_register
        if reg:
            val = extract_register_value(payload, reg)
            if val is not None:
                if reg == "1032":
                    st.session_state.ct_power = val
                elif reg == "0802":
                    st.session_state.export_limit = val

                st.session_state.waiting_for_register = None

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
st.subheader("🔍 Raw MQTT Responses (Debug)")
if st.session_state.response_log:
    st.text_area(
        "Incoming responses",
        value="\n\n---\n\n".join(st.session_state.response_log),
        height=250
    )
else:
    st.info("No responses yet")

# =====================================================
# INVERTER SETTINGS
# =====================================================
st.divider()
st.subheader("Inverter Settings")

if st.button("Update"):
    st.session_state.response_log.clear()
    st.session_state.last_response = None
    st.session_state.waiting_for_register = "1032"
    publish("READ04**12345##1234567890,1032")

if st.session_state.ct_power is None:
    ct_status = "Waiting..."
elif st.session_state.ct_power == 0:
    ct_status = "No"
else:
    ct_status = "Yes"

st.text_input("CT Enabled", ct_status, disabled=True)

# =====================================================
# EXPORT LIMIT READ
# =====================================================
if st.button("Read Export Limit"):
    st.session_state.response_log.clear()
    st.session_state.last_response = None
    st.session_state.waiting_for_register = "0802"
    publish("READ03**12345##1234567890,0802")

st.text_input(
    "Export Limit Set (W)",
    str(st.session_state.export_limit)
    if st.session_state.export_limit is not None else "",
    disabled=True
)

# =====================================================
# ZERO EXPORT CONTROL
# =====================================================
st.divider()

if ct_status == "Yes":
    new_val = st.number_input(
        "Set Export Limit (W)",
        min_value=1,
        max_value=10000,
        value=st.session_state.export_limit or 1
    )

    if st.button("Apply Export Setting"):
        with st.spinner("Applying export setting..."):
            publish("UP#,1536:02014")
            time.sleep(0.5)

            publish(f"UP#,1540:{new_val:05d}")
            time.sleep(0.5)

            publish("UP#,1536:00001")
            time.sleep(0.5)

            st.session_state.response_log.clear()
            st.session_state.last_response = None
            st.session_state.waiting_for_register = "0802"
            publish("READ03**12345##1234567890,0802")
else:
    st.info("CT is not enabled. Zero export cannot be configured.")
