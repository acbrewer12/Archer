/*
 * aux_battery_monitor.ino — Archer Dual-Battery Monitor
 * Upload to the Arduino Uno already installed in the Sierra cab.
 *
 * Reads the aux (second) battery voltage via a resistor-divider on A1
 * and sends "AUX_BATT:<voltage>\n" over USB serial every 2 seconds.
 * archer.py/_arduino_reader() picks this up and updates truck_state.
 *
 * ── Wiring ──────────────────────────────────────────────────────────
 *
 *   Aux battery (+) ──┬── 10kΩ ──┬── A1 (Arduino)
 *                     │          │
 *                              3.3kΩ
 *                                │
 *   Aux battery (-) / chassis GND ─── GND (Arduino)
 *
 *   Voltage divider output at A1:
 *     V_A1 = V_batt * 3.3 / (10 + 3.3) = V_batt * 0.2481
 *   Arduino ADC maps 0–5V to 0–1023.
 *
 *   Max safe input: 5V / 0.2481 ≈ 20.2V  — well above any 12V system.
 *   Typical range: 11.0V (dead) to 14.8V (charging).
 *
 *   Use 1% tolerance resistors for best accuracy.
 *   Optionally add a 100nF ceramic cap across the 3.3kΩ for noise filtering.
 *
 * ── Calibration ─────────────────────────────────────────────────────
 *   Measure actual battery voltage with a multimeter, compare to the
 *   reported value, and adjust CAL_FACTOR below.
 *   Example: multimeter reads 12.85V, sketch reports 12.62V
 *     → CAL_FACTOR = 12.85 / 12.62 = 1.018
 */

const float R1          = 10000.0;   // 10kΩ top resistor
const float R2          =  3300.0;   // 3.3kΩ bottom resistor
const float VREF        =     5.0;   // Arduino Vcc (measure yours; often 4.95–5.05V)
const float CAL_FACTOR  =     1.0;   // tune after first bench test
const int   AUX_PIN     =    A1;     // analog input pin
const float SEND_EVERY  =  2000.0;   // ms between transmissions

unsigned long lastSend = 0;

void setup() {
  Serial.begin(9600);
  analogReference(DEFAULT);   // 5V reference
}

void loop() {
  unsigned long now = millis();
  if (now - lastSend >= (unsigned long)SEND_EVERY) {
    lastSend = now;

    int   raw     = analogRead(AUX_PIN);
    float v_a1    = raw * (VREF / 1023.0);
    float v_batt  = v_a1 * (R1 + R2) / R2 * CAL_FACTOR;

    // Sanity-check: only transmit if reading is in a plausible range
    if (v_batt >= 8.0 && v_batt <= 16.5) {
      Serial.print("AUX_BATT:");
      Serial.println(v_batt, 2);   // e.g. "AUX_BATT:12.84"
    }
  }

  // Arduino still receives commands from archer.py on the same serial port.
  // (Commands are one-way writes from the Pi; no parsing needed here unless
  //  you want to add LIGHTS/EXHAUST/etc. response handling.)
}
