// Smart Jacket - ESP32 firmware (final, merged)
//
// Sensors: MAX30102 heart rate, MPU6050 accel/gyro (+/-16 g), gas sensor on GPIO 34.
// Sends to the Raspberry Pi over MQTT:
//   esp32/sensor_data  every 1 s. First keys in the same order as the Pi CSV:
//                      accel_x, accel_y, accel_z, gas, avg_bpm, finger, mpu_temp
//                      then extras used by the ML / fall code: worker_id, gyro_x/y/z (deg/s),
//                      accel_peak_g, accel_min_g, gyro_peak_dps, fall
//   esp32/fall_event   immediately when FallDetector.h detects a fall
// Receives from the Pi:
//   pi/risk/<WORKER_ID> "LOW" / "MEDIUM" / "HIGH" / "FALL" from the ML model (live_inference.py)
//                      -> one WS2812B RGB LED on GPIO 13 and the buzzer on GPIO 23
//
// Fall detection runs here at 100 Hz (FallDetector.h). No calibration: upright = MPU6050 z axis UP
// (accel_z about +9.8 when the worker stands). While a fall is active, the WS2812B blinks red and the
// buzzer beeps fast.
// loop() never blocks, so the 100 Hz sampling and heart beat detection keep running even
// when Wi-Fi or MQTT is down.

#include <WiFi.h>
#include <PubSubClient.h>
#include <Wire.h>
#include "MAX30105.h"
#include "heartRate.h"
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_NeoPixel.h>
#include "FallDetector.h"

// Wi-Fi credentials (configure before flashing)
const char* ssid = "YOUR_WIFI_SSID";
const char* password = "YOUR_WIFI_PASSWORD";

// Raspberry Pi IP address
const char* mqtt_server = "YOUR_PI_IP_ADDRESS";
const char* TOPIC_DATA = "esp32/sensor_data";
const char* TOPIC_FALL = "esp32/fall_event";
const char* WORKER_ID  = "worker_1";   // unique per jacket
String topicRisk;                      // "pi/risk/" + WORKER_ID, built in setup()

WiFiClient espClient;
PubSubClient client(espClient);

MAX30105 max30102;
Adafruit_MPU6050 mpu;

// Upright = MPU6050 z axis pointing UP. If the sensor is mounted differently, change this,
// e.g. (0, -1, 0) if accel_y reads -9.8 when standing (and pass --upright 0,-1,0 on the Pi).
FallDetector fall(0, 0, 1);

const int GAS_SENSOR_PIN = 34;   // MQ / Q sensor analog out
const int SDA_PIN = 21;
const int SCL_PIN = 22;


// Risk indicator (ML risk from the Pi on pi/risk/<WORKER_ID>): one WS2812B + buzzer
const int RISK_LED_PIN    = 13;  // WS2812B DIN (through 330 ohm, see README)
const uint8_t RISK_LED_BRIGHTNESS = 60;  // 0-255; 60 is clearly visible and keeps current low
const int BUZZER_PIN      = 23;  // through an NPN transistor, see README
Adafruit_NeoPixel riskLed(1, RISK_LED_PIN, NEO_GRB + NEO_KHZ800);
uint32_t riskLedColor = 0xFFFFFFFF;  // last colour sent, so show() only runs on a change
const bool BUZZER_PASSIVE = false;  // true if your buzzer only clicks with digitalWrite (needs a tone)
const unsigned long RISK_TIMEOUT_MS = 15000;  // no risk message this long = link lost
const unsigned long RISK_DOWN_MS = 5000;      // a lower level must hold this long before stepping down

enum Risk { RISK_NONE, RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_FALL };
Risk shownRisk = RISK_NONE;       // what the LEDs show
Risk pendingRisk = RISK_NONE;     // lower level waiting for RISK_DOWN_MS
unsigned long pendingSince = 0;
unsigned long lastRiskMsg = 0;
unsigned long riskMsgCount = 0;
int lastGasRaw = 0;               // latest gas reading, for the local fallback risk
// Local fallback when the Pi sends no risk (same thresholds as processor.py)
const int GAS_WARN = 2000, GAS_DANGER = 3000;
const float BPM_HIGH = 80, TEMP_HIGH = 38.0;
bool riskLinkLost = false;

// Heart rate: rolling average of the last few beats
const byte RATE_SIZE = 6;
float rates[RATE_SIZE];
byte rateSpot = 0;
byte rateCount = 0;
unsigned long lastBeat = 0;
float bpmAverage = 0;
int finger = 0;
bool maxOk = false;
bool mpuOk = false;

const long FINGER_THRESHOLD = 50000;

// Latest MPU6050 reading (updated at 100 Hz)
float lastAx = 0, lastAy = 0, lastAz = 0;
float lastGx = 0, lastGy = 0, lastGz = 0;  // rad/s
float lastTemp = 0;
bool fallSinceLastPacket = false;

unsigned long lastImu = 0;
unsigned long lastPublish = 0;
unsigned long lastMqttAttempt = 0;
unsigned long lastWifiAttempt = 0;
unsigned long lastStatus = 0;
long lastIr = 0;

void resetHeartRate() {
  rateSpot = 0;
  rateCount = 0;
  lastBeat = 0;
  bpmAverage = 0;
}

const char* riskName(Risk r) {
  switch (r) {
    case RISK_LOW: return "LOW";
    case RISK_MEDIUM: return "MEDIUM";
    case RISK_HIGH: return "HIGH";
    case RISK_FALL: return "FALL";
    default: return "NO LINK";
  }
}

// New risk from the Pi: go UP at once, go DOWN only after the lower level holds RISK_DOWN_MS,
// so the LEDs/buzzer don't flicker when the model jumps between classes.
void onRisk(Risk r) {
  lastRiskMsg = millis();
  if (++riskMsgCount <= 3) Serial.printf("RISK message %lu from Pi: %s\n", riskMsgCount, riskName(r));
  riskLinkLost = false;
  if (r >= shownRisk) {
    if (r != shownRisk) Serial.printf("RISK %s -> %s\n", riskName(shownRisk), riskName(r));
    shownRisk = r;
    pendingRisk = RISK_NONE;
  } else if (r != pendingRisk) {
    pendingRisk = r;
    pendingSince = millis();
  } else if (millis() - pendingSince >= RISK_DOWN_MS) {
    Serial.printf("RISK %s -> %s\n", riskName(shownRisk), riskName(r));
    shownRisk = r;
    pendingRisk = RISK_NONE;
  }
}

// Handles the risk level from the Pi on pi/risk/<WORKER_ID>
void onMqttMessage(char* topic, byte* message, unsigned int length) {
  String cmd;
  for (unsigned int i = 0; i < length; i++) cmd += (char)message[i];

  if (topicRisk == topic) {
    if (cmd == "LOW") onRisk(RISK_LOW);
    else if (cmd == "MEDIUM") onRisk(RISK_MEDIUM);
    else if (cmd == "HIGH") onRisk(RISK_HIGH);
    else if (cmd == "FALL") onRisk(RISK_FALL);
  }
}

void buzzer(bool on) {
  if (BUZZER_PASSIVE) {
    if (on) tone(BUZZER_PIN, 2700); else noTone(BUZZER_PIN);
  } else {
    digitalWrite(BUZZER_PIN, on ? HIGH : LOW);
  }
}

// WS2812B colour + buzzer. A fall detected on the jacket itself always wins, even without Wi-Fi.
//   LOW: green.  MEDIUM: yellow.  (buzzer silent)
//   HIGH: red, beep 0.2 s every second.  FALL (here or from the Pi): red blinking fast, fast beeping.
//   No ML risk from the Pi (none yet, or none for 15 s): the jacket works out LOW / MEDIUM / HIGH
//   itself from gas, heart rate and temperature, so the LED still shows a colour.
//   NO LINK (MQTT not connected at all): blue blinking slowly, silent.
Risk localRisk() {
  bool bpmHigh = finger && bpmAverage > BPM_HIGH;
  if (lastGasRaw >= GAS_DANGER) return RISK_HIGH;
  if (lastGasRaw >= GAS_WARN || bpmHigh || lastTemp >= TEMP_HIGH) return RISK_MEDIUM;
  return RISK_LOW;
}

bool usingLocalRisk = false;

void updateRiskIndicator() {
  unsigned long now = millis();
  if (!riskLinkLost && lastRiskMsg != 0 && now - lastRiskMsg > RISK_TIMEOUT_MS) {
    riskLinkLost = true;
    shownRisk = RISK_NONE;
    Serial.println("RISK: no message from the Pi for 15 s, using the jacket's own thresholds");
  }
  bool piRisk = lastRiskMsg != 0 && !riskLinkLost;
  if (!piRisk && client.connected() && now > 20000) {
    if (!usingLocalRisk) Serial.printf("RISK: nothing on %s from the Pi, using the jacket's own thresholds\n", topicRisk.c_str());
    usingLocalRisk = true;
  } else if (piRisk) {
    usingLocalRisk = false;
  }
  Risk r = fall.isFallen() ? RISK_FALL : (usingLocalRisk && !piRisk && client.connected() ? localRisk() : shownRisk);
  bool fast = (now / 100) % 2;
  uint32_t color = 0;
  bool beep = false;
  switch (r) {
    case RISK_LOW:    color = riskLed.Color(0, 255, 0); break;
    case RISK_MEDIUM: color = riskLed.Color(255, 160, 0); break;
    case RISK_HIGH:   color = riskLed.Color(255, 0, 0); beep = (now % 1000) < 200; break;
    case RISK_FALL:   color = fast ? riskLed.Color(255, 0, 0) : 0; beep = fast; break;
    default:          color = (now / 1000) % 2 ? riskLed.Color(0, 0, 255) : 0; break;
  }
  if (color != riskLedColor) {
    riskLedColor = color;
    riskLed.setPixelColor(0, color);
    riskLed.show();
  }
  buzzer(beep);
}

// Lists every device on the I2C bus. MPU6050 = 0x68 (0x69 if AD0 is high), MAX30102 = 0x57.
void scanI2C() {
  Serial.println("I2C scan (SDA 21, SCL 22):");
  int found = 0;
  for (byte addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      found++;
      Serial.printf("  found 0x%02X", addr);
      if (addr == 0x68 || addr == 0x69) Serial.print("  <- MPU6050");
      else if (addr == 0x57) Serial.print("  <- MAX30102");
      Serial.println();
    }
  }
  if (found == 0) Serial.println("  NOTHING found: check VCC (3.3 V), GND, SDA->21, SCL->22");
}

const char* mqttStateText(int s) {
  switch (s) {
    case -4: return "timeout (broker not answering)";
    case -3: return "connection lost";
    case -2: return "connect failed (wrong Pi IP, broker not running, or broker only listens on localhost)";
    case -1: return "disconnected";
    case 0:  return "connected";
    case 5:  return "not authorized (broker needs a username/password)";
    default: return "refused by broker";
  }
}

void setup() {
  Serial.begin(115200);
  delay(1500);  // give the Serial Monitor time to attach so the startup report is not lost
  Serial.println();
  Serial.println("=== Smart Jacket iot_esp32 sketch (smart_jacket.ino) ===");
  analogReadResolution(12);

  riskLed.begin();
  riskLed.setBrightness(RISK_LED_BRIGHTNESS);
  riskLed.clear();
  riskLed.show();
  pinMode(BUZZER_PIN, OUTPUT);
  buzzer(false);
  topicRisk = String("pi/risk/") + WORKER_ID;

  Wire.begin(SDA_PIN, SCL_PIN);
  scanI2C();
  Wire.setClock(400000);  // fast I2C for 100 Hz MPU reads

  mpuOk = mpu.begin();
  if (mpuOk) {
    // The library default (+/-2 g) clips at 19.6 m/s^2, so fall impacts would be cut off.
    mpu.setAccelerometerRange(MPU6050_RANGE_16_G);
    mpu.setGyroRange(MPU6050_RANGE_1000_DEG);
    mpu.setFilterBandwidth(MPU6050_BAND_44_HZ);
    Serial.println("MPU6050: OK (+/-16 g, fall detection ON)");
  } else {
    Serial.println("MPU6050: NOT FOUND -> fall detection OFF, accel values will be 0. Check wiring.");
  }

  // Heart rate sensor is optional: without it the jacket still does fall + gas.
  maxOk = max30102.begin(Wire, I2C_SPEED_FAST);
  if (maxOk) {
    max30102.setup();  // default: 100 samples/s after averaging; this turns the LEDs on
    max30102.setPulseAmplitudeRed(0x1F);
    max30102.setPulseAmplitudeIR(0x1F);
    max30102.setPulseAmplitudeGreen(0);
    Serial.printf("MAX30102: OK (part ID 0x%02X, expected 0x15). Its red LED should now glow faintly.\n",
                  max30102.readPartID());
  } else {
    Serial.println("MAX30102: NOT FOUND -> no red light, avg_bpm=0, finger=0. Check wiring / 0x57 in the scan.");
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  Serial.printf("Wi-Fi: connecting to \"%s\"", ssid);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(500);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("\nWi-Fi: connected, ESP32 IP ");
    Serial.print(WiFi.localIP());
    Serial.printf(", signal %d dBm\n", WiFi.RSSI());
  } else {
    Serial.printf("\nWi-Fi: NOT connected (status %d). Check ssid/password, 2.4 GHz only. Retrying in the background.\n",
                  (int)WiFi.status());
  }

  client.setServer(mqtt_server, 1883);
  client.setCallback(onMqttMessage);
  client.setBufferSize(512);  // default 256 is too small for the full packet

  Serial.printf("MQTT: broker %s:1883, publishing %s every 1 s\n", mqtt_server, TOPIC_DATA);
  Serial.println("Setup done. A STATUS line prints every 5 s.");
}

// Tries once every 5 s (a failed connect can pause the loop for a moment, so not more often)
void maintainMQTT() {
  if (client.connected()) return;
  if (millis() - lastMqttAttempt < 5000) return;
  lastMqttAttempt = millis();

  Serial.printf("MQTT: connecting to %s...", mqtt_server);
  String clientId = "ESP32-" + WiFi.macAddress();
  if (client.connect(clientId.c_str())) {
    Serial.println("connected");
    bool ok = client.subscribe(topicRisk.c_str());
    Serial.printf("MQTT: subscribed to %s (%s)\n", topicRisk.c_str(), ok ? "ok" : "FAILED");
  } else {
    Serial.printf("FAILED, state %d = %s\n", client.state(), mqttStateText(client.state()));
  }
}

// Non-blocking: asks the Wi-Fi to reconnect at most every 5 s
bool maintainWiFi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  if (millis() - lastWifiAttempt >= 5000) {
    lastWifiAttempt = millis();
    Serial.println("Wi-Fi lost, reconnecting...");
    WiFi.reconnect();
  }
  return false;
}

// Reads every new MAX30102 sample from its FIFO (no waiting) and runs beat detection on it.
void readHeartRate() {
  if (!maxOk) return;
  max30102.check();
  while (max30102.available()) {
    long irValue = max30102.getFIFOIR();
    max30102.nextSample();
    lastIr = irValue;

    finger = irValue > FINGER_THRESHOLD ? 1 : 0;
    if (!finger) {
      resetHeartRate();
    } else if (checkForBeat(irValue)) {
      unsigned long now = millis();
      if (lastBeat != 0) {
        float bpm = 60000.0 / (now - lastBeat);
        if (bpm > 40 && bpm < 200) {
          rates[rateSpot] = bpm;
          rateSpot = (rateSpot + 1) % RATE_SIZE;
          if (rateCount < RATE_SIZE) rateCount++;

          float sum = 0;
          for (byte i = 0; i < rateCount; i++) sum += rates[i];
          bpmAverage = sum / rateCount;
        }
      }
      lastBeat = now;
    }
  }
}

void publishFallEvent() {
  char buf[200];
  snprintf(buf, sizeof(buf),
    "{\"worker_id\":\"%s\",\"event\":\"fall\",\"impact_g\":%.2f,\"tilt_deg\":%.1f,\"fall_count\":%lu}",
    WORKER_ID, fall.lastImpactG(), fall.tilt(), (unsigned long)fall.fallCount());
  if (client.connected()) client.publish(TOPIC_FALL, buf);
  Serial.println(buf);
}

void publishSensorData() {
  int gasRaw = analogRead(GAS_SENSOR_PIN);
  lastGasRaw = gasRaw;
  float peakG, minG, peakGyro;
  fall.takeSummary(peakG, minG, peakGyro);
  int fallFlag = (fallSinceLastPacket || fall.isFallen()) ? 1 : 0;
  fallSinceLastPacket = false;

  char payload[512];
  snprintf(payload, sizeof(payload),
    "{\"accel_x\":%.2f,\"accel_y\":%.2f,\"accel_z\":%.2f,"
    "\"gas\":%d,\"avg_bpm\":%.1f,\"finger\":%d,\"mpu_temp\":%.2f,"
    "\"worker_id\":\"%s\",\"gyro_x\":%.1f,\"gyro_y\":%.1f,\"gyro_z\":%.1f,"
    "\"accel_peak_g\":%.2f,\"accel_min_g\":%.2f,\"gyro_peak_dps\":%.1f,\"fall\":%d}",
    lastAx, lastAy, lastAz,
    gasRaw, bpmAverage, finger, lastTemp,
    WORKER_ID, lastGx * 57.29578f, lastGy * 57.29578f, lastGz * 57.29578f,
    peakG, minG, peakGyro, fallFlag
  );

  if (client.connected() && client.publish(TOPIC_DATA, payload)) {
    Serial.println(payload);
  } else {
    Serial.println("MQTT publish failed");
  }
}

void loop() {
  unsigned long now = millis();

  // 1. MPU6050 + fall detection at 100 Hz (runs even without Wi-Fi)
  if (mpuOk && now - lastImu >= 10) {
    lastImu = now;
    sensors_event_t accel, gyro, temp;
    mpu.getEvent(&accel, &gyro, &temp);
    lastAx = accel.acceleration.x; lastAy = accel.acceleration.y; lastAz = accel.acceleration.z;
    lastGx = gyro.gyro.x; lastGy = gyro.gyro.y; lastGz = gyro.gyro.z;
    lastTemp = temp.temperature;  // MPU6050 chip temperature, not body temperature
    if (fall.update(lastAx, lastAy, lastAz, lastGx, lastGy, lastGz, now)) {
      fallSinceLastPacket = true;
      Serial.println("FALL DETECTED");
      publishFallEvent();  // immediate alert, does not wait for the 1 s packet
    }
  }

  // 2. Heart rate (every pass, reads all new samples)
  readHeartRate();

  // 3. Risk LED (WS2812B) + buzzer
  updateRiskIndicator();

  // 4. Network
  if (maintainWiFi()) {
    maintainMQTT();
    client.loop();
  }

  // 5. Publish once per second
  if (now - lastPublish >= 1000) {
    lastPublish = now;
    publishSensorData();
  }

  // 6. Status line every 5 s
  if (now - lastStatus >= 5000) {
    lastStatus = now;
    Serial.printf("STATUS wifi=%s mqtt=%s mpu=%s max30102=%s ir=%ld finger=%d bpm=%.1f fallen=%d risk=%s (%s)\n",
                  WiFi.status() == WL_CONNECTED ? "OK" : "DOWN",
                  client.connected() ? "OK" : "DOWN",
                  mpuOk ? "OK" : "MISSING", maxOk ? "OK" : "MISSING",
                  lastIr, finger, bpmAverage, fall.isFallen() ? 1 : 0,
                  fall.isFallen() ? "FALL" : riskName(usingLocalRisk ? localRisk() : shownRisk),
                  usingLocalRisk ? "jacket thresholds" : (lastRiskMsg ? "from Pi" : "waiting"));
  }
}
