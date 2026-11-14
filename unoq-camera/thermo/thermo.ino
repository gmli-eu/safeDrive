// Runs on the UNO Q's microcontroller (STM32U585). Two jobs, both through the Arduino router:
//   thermo_read -> "celsius,humidity %,age of the reading in ms", or "" when the sensor is missing.
//     Reads the Modulino Thermo on the Qwiic connector every 2 s.
//     (A string: returning a vector pulls in libstdc++ symbols that clash with the Zephyr core.)
//   matrix_show(people, cloud) -> true. Linux (stream.py) sends what the board's LED matrix shows:
//     the on-device people count as a digit (-1 = can't tell: "?"), and a cloud icon while camera
//     frames are going to Nemotron in the cloud (off: everything stays on the UNO Q).
#include <Arduino_RouterBridge.h>
#include <Modulino.h>
#include <Arduino_LED_Matrix.h>

ModulinoThermo thermo;
volatile float celsius = NAN, humidity = NAN;
volatile unsigned long readAt = 0;
unsigned long lastTry = 0;

// ---------- LED matrix: 8 rows x 13 columns, one colour ----------
Arduino_LED_Matrix matrix;
volatile int shownPeople = -2, wantPeople = -1;   // -2: nothing drawn yet
volatile bool shownCloud = false, wantCloud = false;

// 5x7 digits 0-9 and "?" (index 10), one byte per row, bit 4 = leftmost column.
const uint8_t GLYPHS[11][7] = {
  {0x0E, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0E}, {0x04, 0x0C, 0x04, 0x04, 0x04, 0x04, 0x0E},
  {0x0E, 0x11, 0x01, 0x02, 0x04, 0x08, 0x1F}, {0x1F, 0x02, 0x04, 0x02, 0x01, 0x11, 0x0E},
  {0x02, 0x06, 0x0A, 0x12, 0x1F, 0x02, 0x02}, {0x1F, 0x10, 0x1E, 0x01, 0x01, 0x11, 0x0E},
  {0x06, 0x08, 0x10, 0x1E, 0x11, 0x11, 0x0E}, {0x1F, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08},
  {0x0E, 0x11, 0x11, 0x0E, 0x11, 0x11, 0x0E}, {0x0E, 0x11, 0x11, 0x0F, 0x01, 0x02, 0x0C},
  {0x0E, 0x11, 0x01, 0x02, 0x04, 0x00, 0x04},
};
// Cloud, 6 columns wide (bit 5 = leftmost), drawn on rows 2-6 at columns 7-12.
const uint8_t CLOUD[5] = {0x0C, 0x1E, 0x3F, 0x3F, 0x1E};

bool matrix_show(int people, bool cloud) {
  wantPeople = people; wantCloud = cloud;   // drawn in loop(), not in the RPC handler
  return true;
}

void drawMatrix(int people, bool cloud) {
  uint8_t buf[8 * 13] = {0};
  const uint8_t* g = GLYPHS[(people < 0) ? 10 : (people > 9 ? 9 : people)];
  for (int r = 0; r < 7; r++)
    for (int c = 0; c < 5; c++)
      if (g[r] & (0x10 >> c)) buf[(r + 1) * 13 + c] = 7;
  if (cloud)
    for (int r = 0; r < 5; r++)
      for (int c = 0; c < 6; c++)
        if (CLOUD[r] & (0x20 >> c)) buf[(r + 2) * 13 + 7 + c] = 7;
  matrix.draw(buf);
}

void setup() {
  matrix.begin();
  matrix.setGrayscaleBits(3);   // values 0-7
  Bridge.begin();
  Bridge.provide("thermo_read", thermo_read);
  Bridge.provide("matrix_show", matrix_show);
  Modulino.begin(Wire1);   // Wire1 is the Qwiic connector
  thermo.begin();
}

String thermo_read() {
  float t = celsius, h = humidity;
  if (isnan(t)) return String();
  return String(t, 2) + "," + String(h, 1) + "," + String(millis() - readAt);
}

void loop() {
  int p = wantPeople; bool c = wantCloud;
  if (p != shownPeople || c != shownCloud) { drawMatrix(p, c); shownPeople = p; shownCloud = c; }
  if (millis() - lastTry < 2000) return;
  lastTry = millis();
  if (!thermo && !thermo.begin()) { celsius = humidity = NAN; return; }   // plugged in later
  float t = thermo.getTemperature(), h = thermo.getHumidity();
  if (isnan(t) || isnan(h) || t < -40 || t > 125) { celsius = humidity = NAN; thermo.begin(); return; }
  celsius = t; humidity = h; readAt = millis();
}
