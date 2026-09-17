import socket
import subprocess
import sys
import time

# Find PID on port 8888 using netstat
result = subprocess.run(
    ['netstat', '-ano'],
    capture_output=True, text=True, encoding='gbk', errors='replace'
)
for line in result.stdout.splitlines():
    if ':8888' in line and 'LISTENING' in line:
        parts = line.split()
        pid = parts[-1]
        print(f'Found PID {pid} on port 8888')
        # Kill it
        r2 = subprocess.run(['taskkill', '/PID', pid, '/F'], capture_output=True, text=True)
        print(f'Kill result: {r2.stdout.strip()} {r2.stderr.strip()}')
        break

time.sleep(1)
# Verify
try:
    s = socket.create_connection(('127.0.0.1', 8888), timeout=1)
    s.close()
    print('Port 8888 still in use')
except:
    print('Port 8888 is now free')
