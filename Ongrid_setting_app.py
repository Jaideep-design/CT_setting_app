import streamlit as st
import time
import queue
import paho.mqtt.client as mqtt
import warnings
warnings.filterwarnings('ignore')

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
        "response_log": [],   # 👈 ADD THIS
        "ct_power": None,
        "export_limit": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()

# =====================================================
# MQTT SETUP (NO STREAMLIT INSIDE CALLBACKS)
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
            client.subscribe(response_topic)   # ✅ closure variable
            rx_queue.put(("CONNECTED", None))

    def on_message(client, userdata, msg):
        rx_queue.put(("MSG", msg.payload.decode(errors="ignore")))

    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    # Store ONLY in main thread
    st.session_state.mqtt_client = client
    st.session_state.command_topic = command_topic
    st.session_state.response_topic = response_topic

def publish(cmd, wait=1):
    st.session_state.mqtt_client.publish(
        st.session_state.command_topic,
        cmd,
        qos=1
    )
    time.sleep(wait)

def extract_register_value(payload: str, register: str):
    """
    Extracts value for a given register from Solax response.
    Example line: '1032:64392'
    """
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

def wait_for_register(register, timeout=5):
    start = time.time()
    seen = set()

    while time.time() - start < timeout:
        for payload in st.session_state.response_log:
            if payload in seen:
                continue
            seen.add(payload)

            value = extract_register_value(payload, register)
            if value is not None:
                return value

        time.sleep(0.2)

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
# PROCESS MQTT EVENTS (MAIN THREAD ONLY)
# =====================================================
while not st.session_state.rx_queue.empty():
    event, payload = st.session_state.rx_queue.get()

    if event == "CONNECTED":
        st.session_state.connected = True

    elif event == "MSG":
        st.session_state.last_response = payload
        st.session_state.response_log.append(payload)


# =====================================================
# STATUS
# =====================================================
if st.session_state.connected:
    st.success("Connected to MQTT")
    
    st.subheader("🔍 Raw MQTT Responses (Debug)")
    
    if st.session_state.response_log:
        st.text_area(
            "Incoming responses",
            value="\n\n---\n\n".join(st.session_state.response_log),
            height=250
        )
    else:
        st.info("No responses received yet.")

elif st.session_state.mqtt_client:
    st.info("Connecting to MQTT...")
    time.sleep(0.3)
    st.rerun()
else:
    st.warning("Not connected")
    st.stop()

# =====================================================
# INVERTER SETTINGS
# =====================================================
st.divider()
st.subheader("Inverter Settings")

update = st.button("Update")

if update:
    st.session_state.last_response = None
    publish("READ04**12345##1234567890,1032")
    st.session_state.ct_power = wait_for_register("1032")


ct_enabled = "Yes" if st.session_state.ct_power not in (None, 0) else "No"
st.text_input("CT Enabled", ct_enabled, disabled=True)

if update:
    st.session_state.last_response = None
    publish("READ03**12345##1234567890,0802")
    st.session_state.export_limit = wait_for_register("0802")


st.text_input(
    "Export Limit Set (W)",
    str(st.session_state.export_limit) if st.session_state.export_limit else "",
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

            st.session_state.last_response = None
            publish("READ03**12345##1234567890,0802")
            verify = wait_for_register("0802")



        if verify == new_val:
            st.success("Export value updated successfully")
            st.session_state.export_limit = verify
        else:
            st.error("Export value update failed")
else:
    st.info("CT is not enabled. Zero export cannot be configured.")
