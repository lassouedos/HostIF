import socket
import logging
import struct
from threading import Thread, Lock
from time import sleep
import time
import json

# Constants
STX = b'\x02'
ETX = b'\x03'
DEFAULT_PORT = 30040
BUFFER_SIZE = 4096

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('nexim_interface.log')
    ]
)

class StateManager:
    """Track connection state and sequence IDs"""
    def __init__(self):
        self.sequence_id = 1
        self.connected = False
        self.sock = None
        self.lock = Lock()
        self.pending_acks = {}

    def get_next_seq_id(self):
        with self.lock:
            seq_id = self.sequence_id
            self.sequence_id = 1 if seq_id >= 999999 else seq_id + 1
            return seq_id

state = StateManager()

def parse_message(raw_data: bytes) -> tuple:
    """Parse raw bytes into Fuji protocol message"""
    try:
        if raw_data.startswith(STX) and raw_data.endswith(ETX):
            body = raw_data[1:-1]  # Remove STX/ETX
            return body.decode('ascii').split('\t')
        return None, []
    except Exception as e:
        logging.error(f"Parsing error: {e}")
        return None, []

def generate_message(event_type: str, **kwargs) -> bytes:
    """Generate Fuji-compliant message with size prefix"""
    try:
        parts = [event_type] + [str(v) for v in kwargs.values()]
        body = '\t'.join(parts).encode('ascii')
        message = STX + body + ETX
        size = struct.pack('>I', len(message))  # 4-byte big-endian
        return size + message
    except Exception as e:
        logging.error(f"Message generation error: {e}")
        return b''

def connect_to_server(host: str, port: int = DEFAULT_PORT) -> bool:
    """Establish TCP connection to Central Server Lite"""
    try:
        state.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        state.sock.connect((host, port))
        state.connected = True
        logging.info(f"Connected to {host}:{port}")
        return True
    except Exception as e:
        logging.error(f"Connection failed: {e}")
        state.connected = False
        return False

def send_message(event_type: str, **kwargs):
    """Send message with logging"""
    if not state.connected:
        logging.warning("Not connected to server")
        return

    try:
        msg = generate_message(event_type, **kwargs)
        state.sock.sendall(msg)
        logging.info(f"SENT: {msg[4:].decode('ascii', errors='replace')}")
    except Exception as e:
        logging.error(f"Send failed: {e}")
        state.connected = False

def receive_messages():
    buffer = b''
    while state.connected:
        try:
            data = state.sock.recv(BUFFER_SIZE)
            if not data:
                break
            buffer += data
            
            while len(buffer) >= 4:
                size = struct.unpack('>I', buffer[:4])[0]
                if len(buffer) < 4 + size:
                    break
                
                full_msg = buffer[4:4+size]
                buffer = buffer[4+size:]
                
                logging.info(f"RECV: {full_msg.decode('ascii', errors='replace')}")
                event_type, fields = parse_message(full_msg)
                
                if event_type:
                    handle_event(event_type, fields)
        except Exception as e:
            logging.error(f"Receive error: {e}")
            state.connected = False

def handle_event(event_type: str, fields: list):
    """Handle incoming events"""
    logging.info(f"Handling event: {event_type} with fields {fields}")
    
    if event_type.endswith("_ACK"):
        handle_ack(event_type, fields)
    else:
        logging.warning(f"No handler for event type: {event_type}")

def handle_ack(event_type: str, fields: list):
    """Handle acknowledgement messages"""
    base_event = event_type.replace("_ACK", "")
    seq_id = fields[0] if len(fields) > 0 else None
    result = fields[1] if len(fields) > 1 else None
    
    logging.info(f"Received ACK for {base_event}: SeqID={seq_id}, Result={result}")

def initialize_connection(host: str):
    """Complete connection initialization sequence"""
    if not connect_to_server(host):
        return False

    # Start receive thread
    Thread(target=receive_messages, daemon=True).start()
    return True

if __name__ == "__main__":
    server_host = "192.168.100.231"  # Replace with actual server IP
    if initialize_connection(server_host):
        logging.info("MES Interface running.")
    else:
        logging.error("Failed to connect to MES.")
