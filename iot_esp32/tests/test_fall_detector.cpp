#include "../smart_jacket/FallDetector.h"
// Build & run on a PC:  g++ -std=c++17 -o test_fd test_fall_detector.cpp && ./test_fd
#include <cstdio>
#include <random>
std::mt19937 rng(7); std::normal_distribution<float> n(0, 1);
struct Sim { FallDetector fd; uint32_t t = 0; int falls = 0;
  void run(int ms, float ax, float ay, float az, float noise = 0.05f, float gyro = 0.02f) {
    for (int i = 0; i < ms / 10; i++, t += 10)
      falls += fd.update(ax + noise*n(rng), ay + noise*n(rng), az + noise*n(rng), gyro*n(rng), gyro*n(rng), gyro*n(rng), t);
  } };
int main() {
  const float G = 9.81f; int fails = 0;
  auto check = [&](const char *name, bool ok) { printf("%-45s %s\n", name, ok ? "PASS" : "FAIL"); fails += !ok; };
  { Sim s; s.run(2000, 0, 0, G); s.run(400, 0, 0, 0.2f*G, 0.3f, 2); s.run(80, 25, 5, 30, 1, 5); s.run(4000, G, 0, 0.3f);
    check("free fall + impact + lying still -> fall", s.falls == 1 && s.fd.isFallen()); 
    s.run(4000, 0, 0, G); check("standing up again clears latch", !s.fd.isFallen()); }
  { Sim s; s.run(2000, 0, 0, G); s.run(60, 20, 5, 25, 1, 5); s.run(4000, 0, G, 0.3f);
    check("trip: impact only + lying still -> fall", s.falls == 1); }
  { Sim s; s.run(2000, 0, 0, G); s.run(300, 0, 0, 0.1f*G, 0.3f, 2); s.run(60, 0, 0, 3.5f*G, 1, 1); s.run(4000, 0, 0, G, 1.5f, 1.0f);
    check("jump: free fall + landing, stays upright -> no fall", s.falls == 0); }
  { Sim s; s.run(2000, 0, 0, G); for (int k = 0; k < 20; k++) { s.run(250, 2, 1, 1.4f*G, 1.0f, 1.0f); s.run(250, -2, -1, 0.7f*G, 1.0f, 1.0f); }
    check("walking -> no fall", s.falls == 0); }
  { Sim s; s.run(2000, 0, 0, G); s.run(60, 25, 0, 20, 1, 5); s.run(600, G, 0, 0.5f, 3.0f, 3.0f); s.run(4000, 0, 0, G, 1.0f, 1.0f);
    check("hard bump, bends, back upright & moving -> no fall", s.falls == 0); }
  { Sim s; s.run(2000, 0, 0, G); s.run(5000, G*0.7f, 0, G*0.7f, 0.1f);
    check("bending over 45 deg without impact -> no fall", s.falls == 0); }
  { Sim s; s.fd.setUpright(0, -1, 0); s.run(2000, 0, -G, 0); s.run(400, 0, 0, 0, 0.3f, 2); s.run(80, 25, -20, 5, 1, 5); s.run(4000, 0, 0, G, 0.3f);
    check("other mounting (y axis vertical) -> fall", s.falls == 1); }
  return fails;
}
