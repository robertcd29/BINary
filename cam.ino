
#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include <WebServer.h>

// WiFi
const char* ssid = "net";
const char* password = "mihaimihai12345";

// Raspberry Pi - hotspot
const char* serverUrl = "http://172.20.10.2:5000/api/image";

// Camera AI Thinker
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27

#define Y9_GPIO_NUM      35
#define Y8_GPIO_NUM      34
#define Y7_GPIO_NUM      39
#define Y6_GPIO_NUM      36
#define Y5_GPIO_NUM      21
#define Y4_GPIO_NUM      19
#define Y3_GPIO_NUM      18
#define Y2_GPIO_NUM       5

#define VSYNC_GPIO_NUM   25
#define HREF_GPIO_NUM    23
#define PCLK_GPIO_NUM    22

WebServer server(80);

unsigned long lastSend = 0;
const unsigned long SEND_INTERVAL = 15000;

// Initializare camera
bool initCamera() {
  camera_config_t config = {};

  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer = LEDC_TIMER_0;

  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;

  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;

  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;

  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;

  config.xclk_freq_hz = 10000000;
  config.pixel_format = PIXFORMAT_JPEG;

  config.frame_size = FRAMESIZE_QVGA;
  config.jpeg_quality = 20;
  config.fb_count = 1;
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  config.fb_location = CAMERA_FB_IN_DRAM;

  esp_err_t err = esp_camera_init(&config);

  if (err != ESP_OK) {
    Serial.printf("Eroare initializare camera: 0x%x\n", err);
    return false;
  }

  Serial.println("Camera initializata cu succes!");
  return true;
}

// Scanare retele WiFi
void scanWiFi() {
  Serial.println("\nScanare retele WiFi...");

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(1000);

  int n = WiFi.scanNetworks();

  if (n == 0) {
    Serial.println("Nu au fost gasite retele!");
  } else {
    Serial.printf("Au fost gasite %d retele:\n", n);

    for (int i = 0; i < n; i++) {
      Serial.print(i + 1);
      Serial.print(". ");
      Serial.print(WiFi.SSID(i));
      Serial.print(" | Semnal: ");
      Serial.print(WiFi.RSSI(i));
      Serial.println(" dBm");
    }
  }

  WiFi.scanDelete();
}

// Conectare WiFi
bool connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);

  Serial.println("\nConectare la hotspot...");
  Serial.print("SSID: ");
  Serial.println(ssid);

  WiFi.begin(ssid, password);

  int attempts = 0;

  while (WiFi.status() != WL_CONNECTED && attempts < 40) {
    Serial.print("Status WiFi: ");
    Serial.println(WiFi.status());

    delay(500);
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi conectat!");
    Serial.print("IP ESP32-CAM: ");
    Serial.println(WiFi.localIP());

    return true;
  }

  Serial.println("\nConectare WiFi esuata!");
  Serial.print("Status final: ");
  Serial.println(WiFi.status());

  return false;
}

// Trimitere fotografie catre Raspberry Pi
void sendPhoto() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi deconectat!");
    return;
  }

  camera_fb_t* fb = esp_camera_fb_get();

  if (!fb) {
    Serial.println("Nu s-a putut captura imaginea!");
    return;
  }

  Serial.printf("Imagine capturata: %u bytes\n", fb->len);

  HTTPClient http;
  http.setTimeout(10000);

  if (http.begin(serverUrl)) {
    http.addHeader("Content-Type", "image/jpeg");

    int httpCode = http.POST(fb->buf, fb->len);

    if (httpCode > 0) {
      Serial.printf("Raspuns Raspberry Pi: %d\n", httpCode);
      Serial.println(http.getString());
    } else {
      Serial.printf("Eroare HTTP: %s\n",
                    http.errorToString(httpCode).c_str());
    }

    http.end();
  } else {
    Serial.println("Nu s-a putut conecta la server!");
  }

  esp_camera_fb_return(fb);
}

void handleRoot() {
  server.send(200, "text/plain", "ESP32-CAM functioneaza!");
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println("\nPornire ESP32-CAM...");

  if (!initCamera()) {
    Serial.println("Camera nu a pornit!");
    return;
  }

  scanWiFi();

  connectWiFi();

  server.on("/", HTTP_GET, handleRoot);
  server.begin();

  Serial.println("Server web pornit!");
}

void loop() {
  server.handleClient();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi deconectat. Incerc reconectarea...");
    WiFi.reconnect();
    delay(5000);
  }

  if (WiFi.status() == WL_CONNECTED &&
      millis() - lastSend >= SEND_INTERVAL) {
    lastSend = millis();
    sendPhoto();
  }

  delay(10);
}
