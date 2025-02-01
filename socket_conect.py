import socket
import threading
import time
import subprocess
import platform

class HostInterface:
    def __init__(self, host='127.0.0.1', port=30040):
        self.host = host
        self.port = port
        self.sock = None
        self.connected = False
        self.keepalive_interval = 120  # Send KEEPALIVE every 120 seconds
        self.seq_id = 1  # Initialize sequence ID
    
    def generate_seq_id(self):
        """Generate a unique sequence ID and reset after 999999."""
        self.seq_id += 1
        if self.seq_id > 999999:
            self.seq_id = 1
        return str(self.seq_id)
    
    def check_connection(self):
        """Check if the host is reachable via ping (Windows & Linux compatible)."""
        try:
            param = "-n" if platform.system().lower() == "windows" else "-c"
            response = subprocess.run(["ping", param, "1", self.host], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if response.returncode == 0:
                print(f"Ping to {self.host} successful.")
                return True
            else:
                print(f"Ping to {self.host} failed.")
                return False
        except Exception as e:
            print(f"Ping check error: {e}")
            return False
    
    def check_port(self):
        """Check if the port is open on the host."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            result = sock.connect_ex((self.host, self.port))
            if result == 0:
                print(f"Port {self.port} on {self.host} is open.")
                return True
            else:
                print(f"Port {self.port} on {self.host} is closed.")
                return False
    
    def connect(self):
        """Establish a TCP connection to the CSL server."""
        while not self.connected:
            if self.check_connection() and self.check_port():
                try:
                    self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    self.sock.connect((self.host, self.port))
                    self.connected = True
                    print(f"Connected to CSL at {self.host}:{self.port}")
                    threading.Thread(target=self.listen, daemon=True).start()
                    threading.Thread(target=self.send_keepalive, daemon=True).start()
                    self.send_setev()
                except Exception as e:
                    print(f"Connection failed: {e}. Retrying in 5 seconds...")
            else:
                print("Host or port not reachable. Retrying in 5 seconds...")
            time.sleep(5)
    
    def listen(self):
        """Listen for incoming messages from CSL."""
        while self.connected:
            try:
                data = self.sock.recv(1024)
                if not data:
                    print("Connection lost. Reconnecting...")
                    self.connected = False
                    self.connect()
                else:
                    message = data.decode('utf-8')
                    print(f"Received from CSL: {message}")
                    self.process_message(message)
            except Exception as e:
                print(f"Error receiving data: {e}")
                self.connected = False
                self.connect()
    
    def process_message(self, message):
        """Process incoming messages from CSL."""
        print(f"Processing message: {message}")
        if "SETEV_ACK" in message:
            self.send_startev()
        elif "STARTEV_ACK" in message:
            print("Event notification successfully started.")
        elif "KEEPALIVE" in message:
            self.send_message("STXKEEPALIVE_ACK\t" + self.generate_seq_id() + "ETX")
    
    def send_message(self, message):
        """Send a message to CSL."""
        if self.connected:
            try:
                self.sock.sendall(message.encode('utf-8'))
                print(f"Sent to CSL: {message}")
            except Exception as e:
                print(f"Error sending message: {e}")
                self.connected = False
                self.connect()
    
    def send_keepalive(self):
        """Send KEEPALIVE messages at regular intervals."""
        while self.connected:
            self.send_message("STXKEEPALIVE\t" + self.generate_seq_id() + "ETX")
            time.sleep(self.keepalive_interval)

    def send_setev(self):
        """Send the Valid Event Setting Notification (SETEV)."""
        message = "STXSETEV\tEventType\t" + self.generate_seq_id() + "\tETX"
        self.send_message(message)
        print("Sent SETEV to CSL")
    
    def send_startev(self):
        """Send the Event Notice Commencement Notification (STARTEV)."""
        message = "STXSTARTEV\t" + self.generate_seq_id() + "\tETX"
        self.send_message(message)
        print("Sent STARTEV to CSL")

if __name__ == "__main__":
    client = HostInterface(host='192.168.100.231', port=30040)
    client.connect()
