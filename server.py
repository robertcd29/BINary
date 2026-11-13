
from flask import Flask, request, jsonify, Response, render_template
from datetime import datetime
import threading
import requests
import time

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

# Adresa serverului Dariei
DARIA_URL = "http://172.20.10.7:8000/detect"

# Adresa serverului principal - endpoint-ul pentru masuratori
MAIN_SERVER_URL = "http://13.61.25.234:8000/masuratori"

# Corespondenta dintre senzorii Raspberry si pubelele din baza de date
BIN_IDS = {
    "bin1": "TM-011",
    "bin2": "TM-015",
    "bin3": "TM-016"
}

# Intervalul de transmitere catre serverul principal
SEND_INTERVAL = 5
last_server_send = 0

# Ultimele date primite de la senzori
sensor_data = {
    "bin1": None,
    "bin2": None,
    "bin3": None,
    "last_update": None
}

# Ultima fotografie primita de la ESP32-CAM
latest_image = None
image_update = None

# Protejam datele accesate de mai multe cereri
data_lock = threading.Lock()


@app.route("/")
def home():
    return render_template("index.html")


# Transmite valorile senzorilor catre serverul principal
def send_to_main_server(values_to_send):

    for bin_name, value in values_to_send.items():

        bin_id = BIN_IDS[bin_name]

        # Structura asteptata de endpoint-ul /masuratori
        payload = {
            "id": bin_id,
            "umplere": round(value)
        }

        try:
            response = requests.post(
                MAIN_SERVER_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10
            )

            response.raise_for_status()

            print(
                f"Masuratoare transmisa cu succes: "
                f"{bin_id} = {payload['umplere']}% "
                f"(HTTP {response.status_code})"
            )

        except requests.RequestException as e:

            print(
                f"Eroare la transmiterea masuratorii "
                f"pentru {bin_id}: {e}"
            )


# Primeste datele celor 3 senzori
@app.route("/api/sensors", methods=["POST"])
def receive_sensors():

    global sensor_data, last_server_send

    data = request.get_json(silent=True)

    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    required = ["bin1", "bin2", "bin3"]

    if not all(key in data for key in required):
        return jsonify({"error": "Missing sensor values"}), 400

    try:
        values = {
            key: float(data[key])
            for key in required
        }

    except (TypeError, ValueError):
        return jsonify({"error": "Values must be numbers"}), 400

    # Verificam daca valorile reprezinta procente valide
    for key, value in values.items():

        if not 0 <= value <= 100:
            return jsonify({
                "error": f"Invalid filling percentage for {key}. Expected 0-100."
            }), 400

    # Actualizam valorile locale pentru dashboard
    with data_lock:

        sensor_data = {
            **values,
            "last_update": datetime.now().isoformat()
        }

        current_time = time.monotonic()

        # Verificam daca au trecut 5 secunde de la ultima transmitere
        if current_time - last_server_send >= SEND_INTERVAL:

            last_server_send = current_time
            send_to_server = True
            values_to_send = values.copy()

        else:
            send_to_server = False
            values_to_send = None

        current_sensor_data = sensor_data.copy()

    # Transmitere asincrona catre serverul principal
    if send_to_server:

        threading.Thread(
            target=send_to_main_server,
            args=(values_to_send,),
            daemon=True
        ).start()

    # Raspundem imediat ESP32-ului
    return jsonify({
        "message": "Sensor data received",
        "data": current_sensor_data,
        "sent_to_main_server": send_to_server
    }), 200


# Returneaza ultimele valori ale senzorilor
@app.route("/api/sensors", methods=["GET"])
def get_sensors():

    with data_lock:
        return jsonify(sensor_data)


# Primeste o fotografie JPEG de la ESP32-CAM
# O salveaza pentru dashboard si o trimite Dariei
@app.route("/api/image", methods=["POST"])
def receive_image():

    global latest_image, image_update

    image = request.get_data()

    if not image:
        return jsonify({"error": "Empty image"}), 400

    if request.mimetype != "image/jpeg":
        return jsonify({"error": "Expected image/jpeg"}), 400

    # Salvam fotografia pentru dashboard
    with data_lock:

        latest_image = image
        image_update = datetime.now().isoformat()
        current_image_update = image_update

    print(f"Imagine primita de la ESP32-CAM: {len(image)} bytes")

    # Trimitem fotografia catre serverul Dariei
    try:

        response = requests.post(
            DARIA_URL,
            files={
                "image": ("camera.jpg", image, "image/jpeg")
            },
            data={
                "camera_id": "cam1"
            },
            timeout=30
        )

        print("Raspuns server Daria:", response.status_code)
        print("Rezultat Daria:", response.text)

        response.raise_for_status()

        try:
            daria_result = response.json()

        except ValueError:
            daria_result = {"response": response.text}

        return jsonify({
            "message": "Image received and forwarded",
            "size": len(image),
            "last_update": current_image_update,
            "daria_status": response.status_code,
            "daria_result": daria_result
        }), 200

    except requests.RequestException as e:

        print("Eroare la transmiterea imaginii catre Daria:", e)

        return jsonify({
            "message": "Image received, but forwarding to Daria failed",
            "size": len(image),
            "last_update": current_image_update,
            "daria_error": str(e)
        }), 200


# Afiseaza ultima fotografie primita
@app.route("/api/image", methods=["GET"])
def get_image():

    with data_lock:
        image = latest_image

    if image is None:
        return jsonify({"error": "No image received yet"}), 404

    return Response(image, mimetype="image/jpeg")


# Returneaza starea generala a sistemului
@app.route("/api/status", methods=["GET"])
def get_status():

    with data_lock:

        return jsonify({
            "server": "running",
            "sensors": sensor_data,
            "image_update": image_update,
            "image_received": latest_image is not None
        })


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )
