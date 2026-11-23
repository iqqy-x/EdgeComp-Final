#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <ArduinoJson.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// Set these values for the Wi-Fi currently used by the laptop.
constexpr char WIFI_SSID[] = "Your_WiFi_SSID";
constexpr char WIFI_PASSWORD[] = "Your_WiFi_Password";
constexpr char SERVER_URL[] = "http://your-server-url";

constexpr uint8_t MPU_SDA = 21;
constexpr uint8_t MPU_SCL = 22;
constexpr uint8_t OLED_SDA = 33;
constexpr uint8_t OLED_SCL = 32;
constexpr uint8_t BUTTON_START = 16;
constexpr uint8_t BUTTON_LABEL = 17;
constexpr uint8_t BUZZER_PIN = 25;

constexpr uint8_t MPU_ADDRESS = 0x68;
constexpr uint8_t OLED_ADDRESS = 0x3C;
constexpr int OLED_WIDTH = 128;
constexpr int OLED_HEIGHT = 64;

constexpr uint32_t SAMPLE_PERIOD_US = 20000;
constexpr size_t SAMPLE_COUNT = 3000;
constexpr size_t BATCH_SIZE = 100;
constexpr uint8_t COUNTDOWN_SECONDS = 5;
constexpr float GRAVITY = 9.80665f;
constexpr float TO_DEGREES = 180.0f / PI;

struct Sample {
    uint32_t timestampMs;
    float ax;
    float ay;
    float az;
    float gx;
    float gy;
    float gz;
};

struct Button {
    uint8_t pin;
    bool rawHigh = true;
    bool stableHigh = true;
    uint32_t changedAt = 0;

    void begin(uint8_t pinNumber) {
        pin = pinNumber;
        pinMode(pin, INPUT_PULLUP);
    }

    bool pressed() {
        bool currentHigh = digitalRead(pin) == HIGH;
        uint32_t now = millis();
        if (currentHigh != rawHigh) {
            rawHigh = currentHigh;
            changedAt = now;
        }
        if (now - changedAt >= 40 && currentHigh != stableHigh) {
            stableHigh = currentHigh;
            return !stableHigh;
        }
        return false;
    }
};

struct HttpResult {
    int code = -1;
    String body;

    bool ok() const {
        return code >= 200 && code < 300;
    }
};

TwoWire mpuWire(0);
TwoWire oledWire(1);
Adafruit_MPU6050 mpu;
Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &oledWire, -1);
Button startButton;
Button labelButton;
Sample samples[SAMPLE_COUNT];

constexpr const char *LABELS[] = {"diam", "jalan", "lari"};
uint8_t labelIndex = 0;
String subject = "S01";
int sessionNumber = 1;
String sessionId;
bool uploadPending = false;
uint32_t lastConfigMs = 0;
uint32_t lastConnectMs = 0;

void beep(int frequency, int durationMs) {
    tone(BUZZER_PIN, frequency, durationMs);
}

void screen(const String &title, const String &first,
            const String &second = "", const String &third = "") {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 0);
    display.println(title);
    display.println("---------------------");
    display.println(first);
    display.println(second);
    display.println(third);
    display.display();
}

void showReady() {
    screen("DATA LOGGER - READY",
           subject + "  Sesi: " + String(sessionNumber),
           "Label: " + String(LABELS[labelIndex]),
           "SW1 Mulai | SW2 Label");
}

HttpResult requestServer(const char *method, const String &path,
                         const String &payload = "") {
    HttpResult result;
    if (WiFi.status() != WL_CONNECTED) {
        return result;
    }
    HTTPClient http;
    http.setTimeout(8000);
    if (!http.begin(String(SERVER_URL) + path)) {
        return result;
    }
    if (strcmp(method, "GET") == 0) {
        result.code = http.GET();
    } else {
        http.addHeader("Content-Type", "application/json");
        result.code = http.POST(payload);
    }
    if (result.code > 0) {
        result.body = http.getString();
    }
    http.end();
    return result;
}

bool connectWiFi() {
    if (WiFi.status() == WL_CONNECTED) {
        return true;
    }
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("Connecting Wi-Fi");
    uint32_t started = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - started < 15000) {
        Serial.print(".");
        delay(400);
    }
    Serial.println();
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("Wi-Fi unavailable");
        return false;
    }
    WiFi.setSleep(false);
    Serial.print("ESP32 IP: ");
    Serial.println(WiFi.localIP());
    return true;
}

bool fetchConfig() {
    HttpResult result = requestServer("GET", "/api/config");
    if (!result.ok()) {
        Serial.printf("Config request failed: %d\n", result.code);
        return false;
    }
    JsonDocument doc;
    if (deserializeJson(doc, result.body)) {
        Serial.println("Invalid config JSON");
        return false;
    }
    String newSubject = doc["subjek"].as<String>();
    int newSession = doc["sesi"] | 0;
    int expectedSamples = doc["expected_samples"] | 0;
    if (newSubject.length() != 3 || newSession < 1 ||
        expectedSamples != static_cast<int>(SAMPLE_COUNT)) {
        Serial.println("Unexpected server configuration");
        return false;
    }
    subject = newSubject;
    sessionNumber = newSession;
    lastConfigMs = millis();
    return true;
}

bool startSession() {
    String payload = "{\"label\":\"" + String(LABELS[labelIndex]) + "\"}";
    HttpResult result = requestServer("POST", "/api/start", payload);
    if (!result.ok()) {
        Serial.printf("Start rejected (%d): %s\n", result.code, result.body.c_str());
        return false;
    }
    JsonDocument doc;
    if (deserializeJson(doc, result.body)) {
        return false;
    }
    sessionId = doc["session_id"].as<String>();
    Serial.println("Started session: " + sessionId);
    return sessionId.length() > 0;
}

void abortSession() {
    String payload = "{\"session_id\":\"" + sessionId + "\"}";
    HttpResult result = requestServer("POST", "/api/abort", payload);
    if (!result.ok()) {
        Serial.println("Abort request failed; cancel active session in dashboard");
        screen("ABORT WARNING", "Buka dashboard", "Batalkan sesi aktif");
        beep(600, 500);
        delay(3000);
    } else {
        Serial.println("Session aborted");
        screen("SESI DIBATALKAN", sessionId);
        beep(900, 250);
        delay(1500);
    }
    sessionId = "";
}

bool countdown() {
    for (int seconds = COUNTDOWN_SECONDS; seconds > 0; --seconds) {
        screen("SIAPKAN AKTIVITAS", subject + " Sesi " + String(sessionNumber),
               String(LABELS[labelIndex]), "Mulai dalam " + String(seconds) + "s");
        uint32_t tick = millis();
        while (millis() - tick < 1000) {
            if (startButton.pressed()) {
                return false;
            }
            delay(5);
        }
    }
    return true;
}

enum class CaptureResult { Complete, Cancelled, SensorError, TimingError };

CaptureResult recordSamples() {
    screen("RECORDING 50 Hz", subject + " Sesi " + String(sessionNumber),
           String(LABELS[labelIndex]), "60 detik | SW1 batal");
    beep(2300, 150);

    uint32_t startUs = micros();
    uint32_t previousUs = startUs;
    uint32_t greatestGap = 0;
    for (size_t i = 0; i < SAMPLE_COUNT; ++i) {
        uint32_t due = startUs + static_cast<uint32_t>(i) * SAMPLE_PERIOD_US;
        while (static_cast<int32_t>(micros() - due) < 0) {
            int32_t remaining = static_cast<int32_t>(due - micros());
            if (remaining > 2000) {
                delay(1);
            } else {
                delayMicroseconds(100);
            }
            if (startButton.pressed()) {
                return CaptureResult::Cancelled;
            }
        }

        uint32_t stampUs = micros();
        if (i > 0) {
            uint32_t gap = stampUs - previousUs;
            greatestGap = max(greatestGap, gap);
            if (gap > 40000) {
                Serial.printf("Timing gap too large: %lu us\n", static_cast<unsigned long>(gap));
                return CaptureResult::TimingError;
            }
        }
        previousUs = stampUs;

        sensors_event_t accel;
        sensors_event_t gyro;
        sensors_event_t temperature;
        if (!mpu.getEvent(&accel, &gyro, &temperature)) {
            return CaptureResult::SensorError;
        }
        samples[i] = {
            (stampUs - startUs) / 1000,
            accel.acceleration.x / GRAVITY,
            accel.acceleration.y / GRAVITY,
            accel.acceleration.z / GRAVITY,
            gyro.gyro.x * TO_DEGREES,
            gyro.gyro.y * TO_DEGREES,
            gyro.gyro.z * TO_DEGREES,
        };
    }
    Serial.printf("Captured %u samples; max sampling gap: %lu us\n",
                  static_cast<unsigned>(SAMPLE_COUNT),
                  static_cast<unsigned long>(greatestGap));
    return CaptureResult::Complete;
}

String makeBatch(size_t offset) {
    String payload;
    payload.reserve(11000);
    payload = "{\"session_id\":\"" + sessionId + "\",\"start_index\":";
    payload += String(offset);
    payload += ",\"samples\":[";

    size_t end = min(offset + BATCH_SIZE, SAMPLE_COUNT);
    for (size_t i = offset; i < end; ++i) {
        const Sample &s = samples[i];
        if (i > offset) {
            payload += ',';
        }
        payload += '[';
        payload += String(s.timestampMs) + ',';
        payload += String(s.ax, 5) + ',';
        payload += String(s.ay, 5) + ',';
        payload += String(s.az, 5) + ',';
        payload += String(s.gx, 5) + ',';
        payload += String(s.gy, 5) + ',';
        payload += String(s.gz, 5) + ']';
    }
    payload += "]}";
    return payload;
}

bool postWithRetry(const String &path, const String &payload) {
    for (int attempt = 1; attempt <= 5; ++attempt) {
        if (WiFi.status() != WL_CONNECTED) {
            connectWiFi();
        }
        HttpResult result = requestServer("POST", path, payload);
        if (result.ok()) {
            return true;
        }
        Serial.printf("POST %s attempt %d: HTTP %d %s\n",
                      path.c_str(), attempt, result.code, result.body.c_str());
        delay(1200);
    }
    return false;
}

bool uploadAndFinish() {
    screen("MENGIRIM DATA", "Jangan matikan ESP32", sessionId);
    for (size_t start = 0; start < SAMPLE_COUNT; start += BATCH_SIZE) {
        String payload = makeBatch(start);
        if (!postWithRetry("/api/batch", payload)) {
            Serial.printf("Upload stopped at batch %u\n", static_cast<unsigned>(start));
            return false;
        }
        Serial.printf("Uploaded %u/%u\n",
                      static_cast<unsigned>(min(start + BATCH_SIZE, SAMPLE_COUNT)),
                      static_cast<unsigned>(SAMPLE_COUNT));
    }
    String body = "{\"session_id\":\"" + sessionId + "\"}";
    if (!postWithRetry("/api/finish", body)) {
        return false;
    }
    uploadPending = false;
    Serial.println("CSV SAVED, WAITING FOR REVIEW IN DASHBOARD");
    beep(1400, 250);
    screen("CSV TERSIMPAN", sessionId, "Status: perlu review", "Pilih sesi di laptop");
    delay(3500);
    sessionId = "";
    showReady();
    return true;
}

void runSession() {
    if (!connectWiFi() || !fetchConfig() || !startSession()) {
        screen("GAGAL MULAI", "Periksa Wi-Fi/Flask", "dan dashboard laptop");
        beep(600, 350);
        delay(2000);
        showReady();
        return;
    }
    if (!countdown()) {
        abortSession();
        showReady();
        return;
    }

    CaptureResult result = recordSamples();
    if (result != CaptureResult::Complete) {
        Serial.println("Capture cancelled or sensor/timing error");
        abortSession();
        showReady();
        return;
    }

    // Finish signal precedes transfer so the participant can stop moving.
    beep(1200, 500);
    uploadPending = true;
    if (!uploadAndFinish()) {
        screen("UPLOAD GAGAL", "Data masih di RAM", "SW1: ulangi kirim", "JANGAN RESET ESP32");
        beep(500, 500);
    }
}

void setup() {
    Serial.begin(115200);
    delay(500);
    startButton.begin(BUTTON_START);
    labelButton.begin(BUTTON_LABEL);
    pinMode(BUZZER_PIN, OUTPUT);
    digitalWrite(BUZZER_PIN, LOW);

    mpuWire.begin(MPU_SDA, MPU_SCL, 100000);
    oledWire.begin(OLED_SDA, OLED_SCL, 400000);
    if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDRESS, true, false)) {
        Serial.println("OLED initialization failed");
        while (true) delay(1000);
    }
    screen("ESP32 DATA LOGGER", "Initializing MPU6050...");
    if (!mpu.begin(MPU_ADDRESS, &mpuWire)) {
        screen("MPU6050 ERROR", "Periksa SDA/SCL", "GPIO21 / GPIO22");
        while (true) delay(1000);
    }
    mpu.setAccelerometerRange(MPU6050_RANGE_8_G);
    mpu.setGyroRange(MPU6050_RANGE_500_DEG);
    mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);

    Serial.printf("Sample RAM size: %u bytes\n", static_cast<unsigned>(sizeof(samples)));
    Serial.printf("Free heap at boot: %lu\n", static_cast<unsigned long>(ESP.getFreeHeap()));
    if (!connectWiFi() || !fetchConfig()) {
        screen("KONEKSI BELUM SIAP", "Periksa Wi-Fi/Flask", "SW1 coba lagi");
    } else {
        showReady();
    }
}

void loop() {
    if (uploadPending) {
        if (startButton.pressed()) {
            if (!uploadAndFinish()) {
                screen("UPLOAD GAGAL", "Data masih di RAM", "SW1: ulangi kirim", "JANGAN RESET ESP32");
            }
        }
        delay(10);
        return;
    }

    if (labelButton.pressed()) {
        labelIndex = (labelIndex + 1) % 3;
        beep(1800, 70);
        showReady();
    }
    if (startButton.pressed()) {
        runSession();
    }
    if (millis() - lastConfigMs > 5000) {
        if (WiFi.status() == WL_CONNECTED && fetchConfig()) {
            showReady();
        } else if (millis() - lastConnectMs > 10000) {
            lastConnectMs = millis();
            connectWiFi();
        }
    }
    delay(10);
}
