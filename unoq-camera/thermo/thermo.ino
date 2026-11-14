// Runs on the UNO Q's microcontroller (STM32U585): reads the Modulino Thermo on the Qwiic
// connector every 2 s and serves the latest reading to Linux through the Arduino router.
//   thermo_read -> "celsius,humidity %,age of the reading in ms", or "" when the sensor is missing
// (a string: returning a vector pulls in libstdc++ symbols that clash with the Zephyr core)
#include <Arduino_RouterBridge.h>
#include <Modulino.h>

ModulinoThermo thermo;
volatile float celsius = NAN, humidity = NAN;
volatile unsigned long readAt = 0;
unsigned long lastTry = 0;

String thermo_read() {
  float t = celsius, h = humidity;
  if (isnan(t)) return String();
  return String(t, 2) + "," + String(h, 1) + "," + String(millis() - readAt);
}

void setup() {
  Bridge.begin();
  Bridge.provide("thermo_read", thermo_read);
  Modulino.begin(Wire1);   // Wire1 is the Qwiic connector
  thermo.begin();
}

void loop() {
  if (millis() - lastTry < 2000) return;
  lastTry = millis();
  if (!thermo && !thermo.begin()) { celsius = humidity = NAN; return; }   // plugged in later
  float t = thermo.getTemperature(), h = thermo.getHumidity();
  if (isnan(t) || isnan(h) || t < -40 || t > 125) { celsius = humidity = NAN; thermo.begin(); return; }
  celsius = t; humidity = h; readAt = millis();
}
