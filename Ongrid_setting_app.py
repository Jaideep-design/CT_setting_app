# -*- coding: utf-8 -*-
"""
Created on Mon Dec 15 15:06:09 2025

@author: Admin
"""

import streamlit as st
import time
import threading
import paho.mqtt.client as mqtt

# =====================
# MQTT CONFIG
# =====================
MQTT_BROKER = "ecozen.ai"
MQTT_PORT = 1883

TOPIC_PREFIX = "EZMCOGX"
TOPIC_RANGE = [f"{TOPIC_PREFIX}{i:06d}" for i in range(1, 101)]

# =====================
# SESSION STATE INIT
# =====================
if "mqtt_client" not in st.session_state:
    st.session_state.mqtt_client = None
if "connected" not in st.session_state:
    st.session_state.connected = False
if "last_response" not in st.session_state:
    st.session_state.last_response = ""
if "ct_power" not in st.session_state:
    st.session_state.ct_power = None
if "export_limit" not in st.session_state:
    st.session_state.export_limit = None

# =====================
# MQTT CALLBACKS
# =====================
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        st.session_state.connected = True
        client.subscribe(userdata["response_topic"])
    else:
        st.session_state.connected = False

def on_message(client, userdata, msg):
    payload = msg.payload.decode(errors="ignore")
    st.session_state.last_response = payload

def mqtt_connect(selected_topic):
    publish_topic = f"/AC/5/{selected_topic}/Command"
    response_topic = f"/AC/5/{selected_topic}/Response"

    client = mqtt.Client(userdata={
        "publish_topic": publish_topic,
        "response_topic": response_topic
    })
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_BROKER, MQTT_PORT, 60)

    threading.Thread(target=client.loop_forever, daemon=True).start()
    st.session_state.mqtt_client = client

def publish(cmd):
    st.session_state.mqtt_client.publish(
        st.session_state.mqtt_client._userdata["publish_topic"],
        cmd,
        qos=1
    )
    time.sleep(1)

# =====================
# STREAMLIT UI
# =====================
st.title("Solax Inverter – Zero Export Control")

# -------- Topic selection --------
selected_topic = st.selectbox(
    "Select / Enter Device Topic",
    TOPIC_RANGE,
    index=0
)

if st.button("Connect"):
    mqtt_connect(selected_topic)
    st.info("Connecting to MQTT...")

st.divider()

if not st.session_state.connected:
    st.warning("Not connected")
    st.stop()

st.success("Connected")

# =====================
# INVERTER SETTINGS
# =====================
st.subheader("Inverter Settings")

update_clicked = st.button("Update")

# -------- CT ENABLE CHECK --------
if update_clicked:
    publish("READ04**12345##1234567890,1032")
    time.sleep(1)

    try:
        ct_val = int("".join(filter(str.isdigit, st.session_state.last_response)))
        st.session_state.ct_power = ct_val
    except:
        st.session_state.ct_power = None

ct_enabled = (
    "Yes" if st.session_state.ct_power and st.session_state.ct_power != 0 else "No"
)

st.text_input(
    "CT Enabled",
    value=ct_enabled,
    disabled=True
)

# -------- EXPORT LIMIT --------
if update_clicked:
    publish("READ03**12345##1234567890,0802")
    time.sleep(1)

    try:
        export_val = int("".join(filter(str.isdigit, st.session_state.last_response)))
        st.session_state.export_limit = export_val
    except:
        st.session_state.export_limit = None

st.text_input(
    "Export Limit Set (W)",
    value=str(st.session_state.export_limit)
    if st.session_state.export_limit is not None else "",
    disabled=True
)

st.divider()

# =====================
# ZERO EXPORT CONTROL
# =====================
if ct_enabled == "Yes":
    st.subheader("Export Setting")

    new_value = st.number_input(
        "Set Export Limit (W)",
        min_value=1,
        max_value=10000,
        step=1
    )

    if st.button("Apply Export Setting"):
        # Unlock
        publish("UP#,1536:02014")

        # Write value
        publish(f"UP#,1540:{new_value:05d}")

        # Lock again
        publish("UP#,1536:00001")

        # Validate
        publish("READ03**12345##1234567890,0802")
        time.sleep(1)

        try:
            verify_val = int("".join(filter(str.isdigit, st.session_state.last_response)))
            if verify_val == new_value:
                st.success("Export value updated successfully")
            else:
                st.error("Export value update failed")
        except:
            st.error("Invalid response while verifying")

else:
    st.info("CT is not enabled. Zero export cannot be configured.")
