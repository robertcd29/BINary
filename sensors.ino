
#include <WiFi.h>
#include <HTTPClient.h>
#include <math.h>

// Wi-Fi - Hotspot iPhone
const char* ssid = "net";
const char* password = "mihaimihai12345";

// Raspberry Pi
const char* serverURL = "http://172.20.10.2:5000/api/sensors";
const char* serverIP = "172.20.10.2";
const int serverPort = 5000;

// Pinii senzorilor
const int TRIG_PINS[] = {19, 25, 27};
const int ECHO_PINS[] = {21, 26, 14};

const int NUM_SENSORS = 3;

// Calibrare individuala
const float EMPTY_DISTANCE_CM[] = {32.0, 36.0, 36.0};
const float FULL_DISTANCE_CM = 3.0;

// Filtrare
const int NUM_READINGS = 7;                  // 7 citiri per senzor
const int READING_INTERVAL_MS = 40;          // Pauza intre citiri
const float CHANGE_THRESHOLD = 3.0;          // Prag de 3 puncte procentuale

// Interval verificare senzori
const unsigned long SEND_INTERVAL = 2000;
unsigned long lastSend = 0;

// Distante si procente calculate
float distances[NUM_SENSORS];
float percentages[NUM_SENSORS];

// Ultimele valori stabile
float stablePercentages[NUM_SENSORS];
bool initialized[NUM_SENSORS] = {false, false, false};

// Indica daca exista modificari care trebuie transmise
bool pendingChanges = false;


// Conectare WiFi
bool connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

  Serial.println("\nConectare Wi-Fi...");
  WiFi.begin(ssid, password);

  int attempts = 0;

  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    Serial.print(".");
    delay(500);
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWi-Fi conectat!");

    Serial.print("IP ESP32: ");
    Serial.println(WiFi.localIP());

    Serial.print("Gateway: ");
    Serial.println(WiFi.gatewayIP());

    Serial.print("Semnal Wi-Fi: ");
    Serial.print(WiFi.RSSI());
    Serial.println(" dBm");

    return true;
  }

  Serial.println("\nConectare Wi-Fi esuata!");
  return false;
}


// Calculeaza mediana unui set de valori
float calculateMedian(float values[], int count) {

  float sorted[NUM_READINGS];

  for (int i = 0; i < count; i++) {
    sorted[i] = values[i];
  }

  // Sortare crescatoare
  for (int i = 0; i < count - 1; i++) {
    for (int j = i + 1; j < count; j++) {

      if (sorted[i] > sorted[j]) {
        float aux = sorted[i];
        sorted[i] = sorted[j];
        sorted[j] = aux;
      }
    }
  }

  // Pentru 7 valori, mediana este valoarea din mijloc
  return sorted[count / 2];
}


// Citeste senzorul ultrasonic o singura data
float readSingleSensor(int index) {

  digitalWrite(TRIG_PINS[index], LOW);
  delayMicroseconds(2);

  digitalWrite(TRIG_PINS[index], HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PINS[index], LOW);

  long duration = pulseIn(ECHO_PINS[index], HIGH, 30000);

  if (duration == 0) {
    return -1;
  }

  return duration * 0.0343 / 2.0;
}


// Face mai multe citiri si returneaza mediana
float readFilteredSensor(int index) {

  float readings[NUM_READINGS];
  int validReadings = 0;

  for (int i = 0; i < NUM_READINGS; i++) {

    float distance = readSingleSensor(index);

    if (distance > 0) {
      readings[validReadings] = distance;
      validReadings++;
    }

    delay(READING_INTERVAL_MS);
  }

  // Avem nevoie de cel putin 4 citiri valide
  if (validReadings < 4) {
    return -1;
  }

  return calculateMedian(readings, validReadings);
}


// Calcul procent umplere
float calculatePercentage(float distance, int index) {

  float percentage =
      (EMPTY_DISTANCE_CM[index] - distance) /
      (EMPTY_DISTANCE_CM[index] - FULL_DISTANCE_CM) * 100.0;

  return constrain(percentage, 0.0, 100.0);
}


// Actualizeaza valorile stabile doar daca diferenta este suficienta
void updateStableValues() {

  for (int i = 0; i < NUM_SENSORS; i++) {

    // Prima citire valida initializeaza valoarea
    if (!initialized[i]) {

      stablePercentages[i] = percentages[i];
      initialized[i] = true;
      pendingChanges = true;

      Serial.print("Initializare pubela ");
      Serial.print(i + 1);
      Serial.print(": ");
      Serial.print(stablePercentages[i]);
      Serial.println("%");

    } else {

      float difference =
          fabs(percentages[i] - stablePercentages[i]);

      // Modificam valoarea doar daca diferenta este >= 3%
      if (difference >= CHANGE_THRESHOLD) {

        Serial.print("Modificare acceptata pubela ");
        Serial.print(i + 1);
        Serial.print(": ");
        Serial.print(stablePercentages[i]);
        Serial.print("% -> ");
        Serial.print(percentages[i]);
        Serial.println("%");

        stablePercentages[i] = percentages[i];
        pendingChanges = true;

      } else {

        Serial.print("Pubela ");
        Serial.print(i + 1);
        Serial.print(": modificare ignorata (");
        Serial.print(difference);
        Serial.println("%)");
      }
    }
  }
}


// Test conexiune TCP catre Raspberry Pi
bool testConnection() {

  WiFiClient client;

  Serial.println("\nTest conexiune TCP catre Raspberry Pi...");

  if (client.connect(serverIP, serverPort, 5000)) {
    Serial.println("Conexiune TCP reusita!");
    client.stop();
    return true;
  }

  Serial.println("Conexiune TCP esuata!");
  return false;
}


// Trimitere date catre Raspberry Pi
bool sendData() {

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi deconectat!");
    return false;
  }

  if (!testConnection()) {
    Serial.println("Raspberry Pi nu este accesibil!");
    return false;
  }

  HTTPClient http;

  http.setConnectTimeout(5000);
  http.setTimeout(10000);

  if (!http.begin(serverURL)) {
    Serial.println("Initializare HTTP esuata!");
    return false;
  }

  http.addHeader("Content-Type", "application/json");

  // Trimitem valorile stabile, nu valorile brute
  String json = "{";
  json += "\"bin1\":" + String(stablePercentages[0], 1) + ",";
  json += "\"bin2\":" + String(stablePercentages[1], 1) + ",";
  json += "\"bin3\":" + String(stablePercentages[2], 1);
  json += "}";

  Serial.print("Trimit date stabile: ");
  Serial.println(json);

  int httpCode = http.POST(json);

  Serial.print("Raspuns server: ");
  Serial.println(httpCode);

  bool success = (httpCode >= 200 && httpCode < 300);

  if (httpCode > 0) {
    Serial.println(http.getString());
  } else {
    Serial.print("Eroare HTTP: ");
    Serial.println(http.errorToString(httpCode));
  }

  http.end();

  return success;
}


// Initializare
void setup() {

  Serial.begin(115200);
  delay(1000);

  Serial.println("\nPornire ESP32 cu 3 senzori si filtrare...");

  for (int i = 0; i < NUM_SENSORS; i++) {

    pinMode(TRIG_PINS[i], OUTPUT);
    pinMode(ECHO_PINS[i], INPUT);

    digitalWrite(TRIG_PINS[i], LOW);
  }

  connectWiFi();
}


// Bucla principala
void loop() {

  if (WiFi.status() != WL_CONNECTED) {

    Serial.println("\nWi-Fi deconectat. Incerc reconectarea...");

    if (!connectWiFi()) {
      delay(5000);
      return;
    }
  }

  if (millis() - lastSend >= SEND_INTERVAL) {

    lastSend = millis();

    Serial.println("\n--- Citire senzori ---");

    bool allSensorsValid = true;

    for (int i = 0; i < NUM_SENSORS; i++) {

      // Citim fiecare senzor de 7 ori si calculam mediana
      distances[i] = readFilteredSensor(i);

      Serial.print("Pubela ");
      Serial.print(i + 1);
      Serial.print(": ");

      if (distances[i] < 0) {

        Serial.println("Citiri insuficiente!");

        allSensorsValid = false;

      } else {

        percentages[i] = calculatePercentage(distances[i], i);

        Serial.print("Distanta mediana: ");
        Serial.print(distances[i]);
        Serial.print(" cm | Umplere calculata: ");
        Serial.print(percentages[i]);
        Serial.println("%");
      }

      // Evitam interferentele intre senzori
      delay(100);
    }

    // Actualizam valorile numai daca toti senzorii au raspuns
    if (allSensorsValid) {

      updateStableValues();

      // Asteptam initializarea tuturor celor 3 senzori
      if (initialized[0] && initialized[1] && initialized[2]) {

        if (pendingChanges) {

          Serial.println("Exista modificari. Transmit datele...");

          if (sendData()) {
            pendingChanges = false;
            Serial.println("Date transmise cu succes!");
          } else {
            Serial.println("Transmitere esuata. Vom reincerca.");
          }

        } else {

          Serial.println("Nu exista modificari semnificative. Nu transmitem.");
        }
      }

    } else {

      Serial.println("Citire incompleta. Pastram valorile anterioare.");
    }

    Serial.println("-------------------------");
  }

  delay(10);
}
