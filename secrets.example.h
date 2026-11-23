// Copiaza acest fisier in secrets.h (langa cam.ino / sensors.ino) si pune valorile reale.
// secrets.h este in .gitignore si nu se urca pe GitHub.
#pragma once

// Numele retelei Wi-Fi (hotspot-ul la care se conecteaza ESP32)
#define WIFI_SSID     "your-wifi-name"

// Parola retelei Wi-Fi
#define WIFI_PASSWORD "your-wifi-password"

// IP-ul Raspberry Pi in reteaua de mai sus (serverul Flask, portul 5000)
#define PI_IP         "192.168.1.100"
