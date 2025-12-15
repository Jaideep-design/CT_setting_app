import streamlit as st
import time
import paho.mqtt.client as mqtt

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
        "last_response": None,
        "ct_power": None,
        "export_limit": None,
        "connecting": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()

# =====================================================
# MQTT HELPERS
# =====================================================
def mqtt_connect(device_id):
    if st.session_state.mqtt_client:
        return

    command_topic = f"/AC/5/{device_id}/Command"
    response_topic = f"/AC/5/{device_id}/Response"

    client = mqtt.Client()
    client.connected_flag = False

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            client.connected_flag = True
            client.subscribe(response_topic)

    def on_message(client, userdata, msg):
        st.session_state.last_response = msg.payload.decode(errors="ignore")

    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    st.session_state.mqtt_client = client
    st.session_state.connecting = True

def publish(cmd, wait=1):
    client = st.session_state.mqtt_client
    topic = client._userdata["command_topic"] if hasattr(client, "_userdata") else None

    if not topic:
        topic = client._sock  # dummy, will be ignored

    client.publish(
        f"/AC/5/{selected_device}/Command",
        cmd,
        qos=1
    )
    time.sleep(wait)

def extract_int(payload):
    if not payload:
        return None
    digits = "".join(filter(str.isdigit, payload))
    return int(digits) if digits else None

# =====================================================
# UI
# =====================================================
st.set_page_config(page_title="Solax Zero Export Control", layout="centered")
st.title("Solax Inverter – Zero Export Control")

# ------------------ DEVICE SELECTION ------------------
selected_device = st.selectbox(
    "Select / Enter Device Topic",
    DEVICE_TOPICS
)

if st.button("Connect", disabled=st.session_state.connected):
    mqtt_connect(selected_device)

# ------------------ CONNECTION STATUS ------------------
client = st.session_state.mqtt_client

if client:
    if client.connected_flag:
        st.session_state.connected = True
        st.session_state.connecting = False
        st.success("Connected to MQTT")
    else:
        st.info("Connecting to MQTT...")
        time.sleep(0.4)
        st.rerun()
else:
    st.warning("Not connected")
    st.stop()

# =====================================================
# INVERTER SETTINGS
# =====================================================
st.divider()
st.subheader("Inverter Settings")

update_clicked = st.button("Update")

# ------------------ CT ENABLE CHECK ------------------
if update_clicked:
    st.session_state.last_response = None
    publish("READ04**12345##1234567890,1032")

    ct_val = extract_int(st.session_state.last_response)
    st.session_state.ct_power = ct_val

ct_enabled = (
    "Yes"
    if st.session_state.ct_power is not None and st.session_state.ct_power != 0
    else "No"
)

st.text_input("CT Enabled", ct_enabled, disabled=True)

# ------------------ EXPORT LIMIT READ ------------------
if update_clicked:
    st.session_state.last_response = None
    publish("READ03**12345##1234567890,0802")

    export_val = extract_int(st.session_state.last_response)
    st.session_state.export_limit = export_val

st.text_input(
    "Export Limit Set (W)",
    str(st.session_state.export_limit) if st.session_state.export_limit is not None else "",
    disabled=True
)

# =====================================================
# ZERO EXPORT CONTROL
# =====================================================
st.divider()

if ct_enabled == "Yes":
    st.subheader("Export Setting")

    new_export_value = st.number_input(
        "Set Export Limit (W)",
        min_value=1,
        max_value=10000,
        step=1,
        value=st.session_state.export_limit or 1
    )

    if st.button("Apply Export Setting"):
        with st.spinner("Applying export setting..."):
            # Unlock
            publish("UP#,1536:02014")

            # Write value
            publish(f"UP#,1540:{new_export_value:05d}")

            # Lock
            publish("UP#,1536:00001")

            # Verify
            st.session_state.last_response = None
            publish("READ03**12345##1234567890,0802")

            verify_val = extract_int(st.session_state.last_response)

        if verify_val == new_export_value:
            st.success("Export value updated successfully")
            st.session_state.export_limit = verify_val
        else:
            st.error("Export value update failed")

else:
    st.info("CT is not enabled. Zero export cannot be configured.")
