"""Send a tuning command to the ESP32 TV transmitter without resetting it: python3 tv.py d0.2"""
import sys, time, serial
s = serial.Serial()
s.port, s.baudrate, s.timeout = "/dev/cu.usbserial-0001", 115200, 0.5
s.dtr = s.rts = False
s.open()
# macOS still pulses the reset line on open, so wait for the board to come back up.
out, end = b"", time.time() + 4
while b"transmitting" not in out and time.time() < end:
    out += s.read(256)
s.write((sys.argv[1] + "\n").encode())
end, out = time.time() + 3, b""
while time.time() < end:
    out += s.read(256)
print(out.decode(errors="replace").strip())
