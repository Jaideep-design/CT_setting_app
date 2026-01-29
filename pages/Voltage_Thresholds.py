import streamlit as st
import time
import queue
import json
import re
import paho.mqtt.client as mqtt
from streamlit_autorefresh import st_autorefresh
import warnings

warnings.filterwarnings("ignore")

# =====================================================
# PAGE CONFIG
# =====================================================
st.set_page_config("Grid Voltage Threshold Control", layout="centered")

st.title("⚡ Grid Voltage Threshold Control")

# =====================================================
# MQTT CONFIG
# =====================================================
MQTT_BROKER = "ecozen.ai"
MQTT_PORT = 1883

AUTO_REFRESH_MS = 500
MAX_LOG_LINES = 100
TIMEOUT = 6

TOPIC_PREFIX = "EZMCOGX"
DEVICE_TOPICS = [f"{TOPIC_PREFIX}{i:06d}" for i in range(1, 301)]

# =====================================================
# 🔁 REGISTER PLACEHOLDERS 
# =====================================================
REG_VOLTAGE_HIGH = "0808"  
REG_VOLTAGE_LOW  = "0811"  

# =====================================================
# SESSION STATE INIT
# =====================================================
def init_state():
    defaults = {
        "mqtt_client": None,
        "rx_queue": queue.Queue(),

        "state": "IDLE",
        "command_topic": None,

        "voltage_high": None,
        "voltage_low": None,

        "pending_register": None,
        "pending_since": None,
        "parsed_payloads": set(),

        "response_log": [],
        "parse_debug": [],

        "write_mode": None,          # "HIGH" | "LOW"
        "write_password": "",
        "write_unlocked": False,
        "write_value": None,
        "lock_sent_at": None,

        "response_cursor": 0
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

    cmd = f"/AC/5/{device_id}/Command"
    rsp = f"/AC/5/{device_id}/Response"

    rx_queue = st.session_state.rx_queue
    client = mqtt.Client()

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(rsp)
            rx_queue.put(("CONNECTED", None))

    def on_message(client, userdata, msg):
        rx_queue.put(("MSG", msg.payload.decode(errors="ignore")))

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()

    st.session_state.mqtt_client = client
    st.session_state.command_topic = cmd
    st.session_state.state = "CONNECTING"

def publish(cmd):
    ts = time.time()
    st.session_state.parse_debug.append(f"📤 [{ts:.3f}] SENT → {cmd}")
    st.session_state.mqtt_client.publish(
        st.session_state.command_topic,
        cmd,
        qos=1
    )

# =====================================================
# RX QUEUE
# =====================================================
def drain_rx_queue():
    while not st.session_state.rx_queue.empty():
        event, payload = st.session_state.rx_queue.get()

        if event == "CONNECTED":
            st.session_state.state = "CONNECTED"

        elif event == "MSG":
            st.session_state.response_log.append((time.time(), payload))
            st.session_state.response_log = st.session_state.response_log[-MAX_LOG_LINES:]

# =====================================================
# PARSING
# =====================================================
def extract_register(payload, register):
    dbg = st.session_state.parse_debug
    dbg.append(payload)

    try:
        rsp = json.loads(payload).get("rsp", "")
    except Exception:
        m = re.search(r'"rsp"\s*:\s*"([\s\S]*)"\s*}', payload)
        if not m:
            return None
        rsp = m.group(1)

    if "READ PROCESSING" in rsp:
        return None

    for line in rsp.splitlines():
        if line.startswith(f"{register}:"):
            return int(line.split(":")[1])

    return None

def is_up_processed(payload):
    try:
        rsp = json.loads(payload).get("rsp", "")
        return "UP PROCESSED" in rsp
    except Exception:
        return False

# =====================================================
# EVENT-DRIVEN PARSER  (VOLTAGE VERSION)
# =====================================================
def run_state_machine():
    if (
        not st.session_state.pending_register
        and st.session_state.state not in (
            "WAIT_UP_PROCESSED",
            "VERIFY_VOLTAGE_DELAY",
        )
    ):
        return

    # ⏱ timeout guard (UNCHANGED)
    if time.time() - st.session_state.pending_since > TIMEOUT:
        if st.session_state.state == "WAIT_UP_PROCESSED":
            st.session_state.parse_debug.append("⏱ TIMEOUT waiting for UP PROCESSED")
        else:
            st.session_state.parse_debug.append("⏱ TIMEOUT waiting for register")

        st.session_state.pending_register = None
        st.session_state.state = "CONNECTED"
        st.session_state.parsed_payloads.clear()
        return

    # ---------------- VERIFY DELAY ----------------
    if st.session_state.state == "VERIFY_VOLTAGE_DELAY":
        if time.time() < st.session_state.verify_at:
            return

        publish(
            f"READ03**12345##1234567890,{st.session_state.pending_register}"
        )
        st.session_state.state = "VERIFY_VOLTAGE_ONCE"
        st.session_state.pending_since = time.time()
        st.session_state.parsed_payloads.clear()
        return

    for ts, payload in st.session_state.response_log[st.session_state.response_cursor:]:
        st.session_state.response_cursor += 1

        if payload in st.session_state.parsed_payloads:
            continue

        st.session_state.parsed_payloads.add(payload)

        # =====================================================
        # WAIT FOR *FINAL* UP PROCESSED (after LOCK)
        # =====================================================
        if st.session_state.state == "WAIT_UP_PROCESSED":

            if ts < st.session_state.lock_sent_at:
                continue

            if is_up_processed(payload):
                st.session_state.parse_debug.append(
                    "🔐 Final UP PROCESSED received → settling before verify"
                )

                st.session_state.verify_at = time.time() + 0.8
                st.session_state.state = "VERIFY_VOLTAGE_DELAY"
                st.session_state.parsed_payloads.clear()
                break

            continue

        # =====================================================
        # REGISTER-BASED STATES
        # =====================================================
        value = extract_register(payload, st.session_state.pending_register)
        if value is None:
            continue

        # ---------------- READ FLOW ----------------
        if st.session_state.state == "READ_HIGH":
            st.session_state.voltage_high = value

            publish("READ03**12345##1234567890,0811")
            st.session_state.pending_register = "0811"
            st.session_state.pending_since = time.time()
            st.session_state.state = "READ_LOW"
            st.session_state.parsed_payloads.clear()
            break

        elif st.session_state.state == "READ_LOW":
            st.session_state.voltage_low = value

            st.session_state.pending_register = None
            st.session_state.state = "CONNECTED"
            st.session_state.parsed_payloads.clear()
            break

        # ---------------- VERIFY ----------------
        elif st.session_state.state == "VERIFY_VOLTAGE_ONCE":

            if value == st.session_state.write_value:
                if st.session_state.write_mode == "Upper":
                    st.session_state.voltage_high = value
                else:
                    st.session_state.voltage_low = value

                st.success(f"✅ Voltage threshold set to {value} V")
                st.session_state.parse_debug.append(
                    f"✔ Verification success: {st.session_state.pending_register}={value}"
                )
            else:
                st.error(
                    f"❌ Verification failed. Expected {st.session_state.write_value}, got {value}"
                )

            st.session_state.state = "CONNECTED"
            st.session_state.pending_register = None
            st.session_state.write_unlocked = False
            st.session_state.write_value = None
            st.session_state.parsed_payloads.clear()
            break

# =====================================================
# LOOP
# =====================================================
if st.session_state.mqtt_client:
    st_autorefresh(interval=AUTO_REFRESH_MS, key="mqtt_refresh")
    drain_rx_queue()
    run_state_machine()

# =====================================================
# UI
# =====================================================
device = st.selectbox("Select Device", DEVICE_TOPICS)

if st.button("Connect", disabled=st.session_state.state != "IDLE"):
    mqtt_connect(device)

# st.success("Connected") if st.session_state.state == "CONNECTED" else st.warning("Connecting...")
if st.session_state.state == "CONNECTED":
    st.success("Connected")
else:
    st.warning("Connecting...")

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
# READ
# =====================================================
if st.button("Read Voltage Thresholds", disabled=st.session_state.state != "CONNECTED"):
    publish(f"READ03**12345##1234567890,{REG_VOLTAGE_HIGH}")
    st.session_state.pending_register = REG_VOLTAGE_HIGH
    st.session_state.pending_since = time.time()
    st.session_state.state = "READ_HIGH"
    st.session_state.response_cursor = len(st.session_state.response_log)

st.text_input("Upper Voltage Threshold", st.session_state.voltage_high, disabled=True)
st.text_input("Lower Voltage Threshold", st.session_state.voltage_low, disabled=True)

# =====================================================
# WRITE
# =====================================================
st.divider()
st.subheader("⚙️ Set Voltage Threshold")

mode = st.radio("Select Register", ["Upper", "Lower"])
value = st.number_input("Voltage Value", min_value=150, max_value=300)

if st.button("Set"):
    st.session_state.write_mode = mode
    st.session_state.write_value = value
    st.session_state.state = "WRITE_PASSWORD"

# -------------------------------
# PASSWORD
# -------------------------------
if st.session_state.state == "WRITE_PASSWORD":
    st.subheader("🔐 Enter Inverter Password")

    pwd = st.text_input("Password", type="password")

    if st.button("Unlock"):
        padded = pwd.zfill(5)
        st.session_state.write_password = padded

        publish(f"UP#,1536:{padded}")

        if padded == "02014":
            st.session_state.write_unlocked = True
            st.session_state.state = "WRITE_VALUE"
            st.success("Unlocked successfully")
        else:
            st.error("Invalid password")
            
# -------------------------------
# WRITE VALUE
# -------------------------------
if st.session_state.state == "WRITE_VALUE" and st.session_state.write_unlocked:
    st.subheader("⚙️ Set Voltage Threshold")

    value = st.number_input(
        "Voltage (V)",
        min_value=150,
        max_value=300,
        value=st.session_state.write_value or 230
    )

    if st.button("Set Value"):
        padded_val = f"{value:05d}"
        st.session_state.write_value = value

        if st.session_state.write_mode == "Upper":
            publish(f"UP#,1566:{padded_val}")
            st.session_state.pending_register = "0808"
        else:
            publish(f"UP#,1567:{padded_val}")
            st.session_state.pending_register = "0811"

        st.session_state.state = "WRITE_LOCK"

# -------------------------------
# LOCK & APPLY
# -------------------------------
if st.session_state.state == "WRITE_LOCK":
    st.subheader("🔒 Lock Settings")

    if st.button("Lock & Apply"):
        lock_ts = time.time()
        publish("UP#,1536:00001")

        st.session_state.lock_sent_at = lock_ts
        st.session_state.state = "WAIT_UP_PROCESSED"
        st.session_state.pending_since = lock_ts

        st.session_state.parsed_payloads.clear()
        st.session_state.response_cursor = len(st.session_state.response_log)
