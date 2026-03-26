#!/usr/bin/env python3
"""
Mock SilverSprint Arduino controller.

Uses socat to create a virtual serial port pair, then symlinks one end
into /dev/cu.MockSprint so the app's IOKit port scanner can find it.

Requirements:
    brew install socat

Usage:
    sudo python3 mock_controller.py
"""

import os
import subprocess
import sys
import time
import random
import select

FIRMWARE_VERSION = "MOCK-1.0"
SOCAT_PORT_A = "/tmp/mock_sprint_a"
SOCAT_PORT_B = "/tmp/mock_sprint_b"


class MockController:
    def __init__(self):
        self.socat_proc = None
        self.fd = None
        self.read_buf = ""

        # Race state
        self.race_active = False
        self.race_start_time = 0.0
        self.countdown = 0
        self.countdown_next = 0.0
        self.race_duration_secs = 60
        self.race_length_ticks = 0
        self.race_type = "time"

        # Per-racer state (4 racers always)
        self.racer_ticks = [0, 0, 0, 0]
        self.racer_speeds = [0.0, 0.0, 0.0, 0.0]
        self.racer_finished = [False, False, False, False]

        self.last_update = 0.0
        self.update_interval = 0.05

    def start_socat(self):
        for p in [SOCAT_PORT_A, SOCAT_PORT_B]:
            if os.path.exists(p) or os.path.islink(p):
                os.unlink(p)

        self.socat_proc = subprocess.Popen(
            [
                "socat",
                "-d", "-d",
                f"pty,raw,echo=0,link={SOCAT_PORT_A}",
                f"pty,raw,echo=0,link={SOCAT_PORT_B}",
            ],
            stderr=subprocess.PIPE,
        )

        for _ in range(50):
            if os.path.exists(SOCAT_PORT_A) and os.path.exists(SOCAT_PORT_B):
                break
            time.sleep(0.1)
        else:
            print("ERROR: socat failed to create pty pair", file=sys.stderr)
            sys.exit(1)

        self.fd = os.open(SOCAT_PORT_A, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

    def cleanup(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        if self.socat_proc:
            self.socat_proc.terminate()
            self.socat_proc.wait()
        for p in [SOCAT_PORT_A, SOCAT_PORT_B]:
            if os.path.exists(p) or os.path.islink(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def send(self, msg):
        data = (msg + "\r\n").encode()
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def handle_command(self, cmd):
        cmd = cmd.strip()
        if not cmd:
            return

        if cmd == "v":
            self.send(f"V:{FIRMWARE_VERSION}")
        elif cmd == "g":
            self.start_race()
        elif cmd == "s":
            self.stop_race()
        elif cmd == "m":
            self.send("M:on")
        elif cmd.startswith("t"):
            try:
                self.race_duration_secs = int(cmd[1:])
            except ValueError:
                pass
        elif cmd.startswith("l"):
            try:
                self.race_length_ticks = int(cmd[1:])
                self.send(f"L:{self.race_length_ticks}")
            except ValueError:
                pass
        elif cmd == "d":
            self.race_type = "distance"
        elif cmd == "x":
            self.race_type = "time"
        else:
            print(f"  [mock] Unknown command: {cmd!r}")

    def start_race(self):
        print("  [mock] Race starting...")
        self.race_active = False
        self.countdown = 3
        self.countdown_next = time.time() + 1.0
        self.send("CD:3")

        for i in range(4):
            self.racer_ticks[i] = 0
            self.racer_finished[i] = False
            self.racer_speeds[i] = random.uniform(80, 120)

    def stop_race(self):
        self.race_active = False
        self.countdown = 0
        print("  [mock] Race stopped")

    def update_countdown(self):
        if self.countdown <= 0:
            return

        now = time.time()
        if now >= self.countdown_next:
            self.countdown -= 1
            if self.countdown > 0:
                self.send(f"CD:{self.countdown}")
                self.countdown_next = now + 1.0
            else:
                self.send("CD:0")
                self.race_active = True
                self.race_start_time = time.time()
                self.last_update = self.race_start_time
                print("  [mock] Race running!")

    def update_race(self):
        if not self.race_active:
            return

        now = time.time()
        if now - self.last_update < self.update_interval:
            return

        dt = now - self.last_update
        self.last_update = now

        race_millis = int((now - self.race_start_time) * 1000)

        for i in range(4):
            if not self.racer_finished[i]:
                jitter = random.uniform(0.85, 1.15)
                self.racer_ticks[i] += int(self.racer_speeds[i] * dt * jitter)

        tick_str = ",".join(str(t) for t in self.racer_ticks)
        self.send(f"R:{tick_str},{race_millis}")

        all_finished = True
        for i in range(4):
            if self.racer_finished[i]:
                continue

            finished = False
            if self.race_type == "time":
                if race_millis >= self.race_duration_secs * 1000:
                    finished = True
            elif self.race_type == "distance":
                if self.race_length_ticks > 0 and self.racer_ticks[i] >= self.race_length_ticks:
                    finished = True

            if finished:
                self.racer_finished[i] = True
                self.send(f"{i}F:{race_millis}")
                print(f"  [mock] Racer {i} finished at {race_millis}ms")
            else:
                all_finished = False

        if all_finished:
            self.race_active = False
            print("  [mock] All racers finished!")

    def run(self):
        self.start_socat()

        print(f"\nMock SilverSprint controller running")
        print(f"Select 'Mock SilverSprint Controller' in the app's hardware dropdown")
        print(f"Press Ctrl+C to quit\n")

        try:
            while True:
                ready, _, _ = select.select([self.fd], [], [], 0.01)

                if ready:
                    try:
                        data = os.read(self.fd, 1024).decode("utf-8", errors="ignore")
                        self.read_buf += data
                    except OSError:
                        pass

                    while "\n" in self.read_buf:
                        line, self.read_buf = self.read_buf.split("\n", 1)
                        line = line.strip()
                        if line:
                            print(f"  [recv] {line!r}")
                            self.handle_command(line)

                self.update_countdown()
                self.update_race()

        except KeyboardInterrupt:
            print("\nShutting down mock controller")
        finally:
            self.cleanup()


if __name__ == "__main__":
    controller = MockController()
    controller.run()
