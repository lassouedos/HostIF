import socket
import struct
import threading
import time
import logging
from datetime import datetime
# Constants
HOST = '192.168.100.231'  # Central Server Lite IP
PORT = 30040
EVENT_NAMES = [
    "UNLOADCOMP", "CHANGECOMP", "PGCHANGEII", "BOMLIST", "PRODSTARTED",
    "PRODCOMPLETED", "MCSTATECHANGE", "MCALARMON", "MCALARMOFF", "PARTSUSAGE",
    "HEADUSAGE", "NOZZLEUSAGE", "HOLDERERROR", "SLOTSTTCHG", "FEEDERSETUP",
    "PARTSREFILL", "ERRORREPORT", "PRODCOMPLETEDII", "PCBCHECKIN", "PCBCHECKOUT",
    "FEEDERLIST"
]
LINE_NAME = "LINE1"
MACHINE = "NXT1"
MODULE_NO = "1"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('fuji_interface.log'),
        logging.StreamHandler()
    ]
)
class FujiHostInterface:
    def __init__(self):
        self.sock = None
        self.seq_id = 0
        self.connected = False
        self.lock = threading.Lock()

    def build_message(self, raw_msg):
        """Add STX/ETX and 4-byte big-endian length header."""
        stx_etx_msg = f"\x02{raw_msg}\x03"
        msg_length = len(stx_etx_msg)
        header = struct.pack(">I", msg_length)
        return header + stx_etx_msg.encode()
    
    def _parse_time(self, timestr):
            return datetime.strptime(timestr, "%Y%m%d%H%M%S")

    def _parse_components(self, parts, fields_per_item, field_names):
        components = []
        for i in range(0, len(parts), fields_per_item):
            component = {name: parts[i+j] for j, name in enumerate(field_names)}
            components.append(component)
        return components
    
    def connect(self):
        try : 
            """Establish TCP connection to Central Server Lite."""
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((HOST, PORT))
            self.connected = True
            logging.info(f"Connected to {HOST}:{PORT}")
            print(f"Connected to {HOST}:{PORT}")

            # Start thread to listen for incoming messages
            threading.Thread(target=self.listen_for_messages, daemon=True).start()

            # Perform initialization sequence
            self.send_setev()
            self.send_startev()
        except Exception as e:
            logging.error(f"Connection failed: {str(e)}")
            print(f"Connection failed: {str(e)}")
            self.connected = False


    def send_setev(self):
        """Send SETEV to enable events with ACK."""
        self.seq_id += 1
        events_with_ack = [f"{event}\t1" for event in EVENT_NAMES]
        setev_msg = f"SETEV\t{self.seq_id}\t{MACHINE}\t{len(EVENT_NAMES)}\t" + "\t".join(events_with_ack)
        self._send_message(setev_msg)

    def send_startev(self):
        """Send STARTEV to begin event notifications."""
        self.seq_id += 1
        startev_msg = f"STARTEV\t{self.seq_id}\t{MACHINE}"
        self._send_message(startev_msg)

    def handle_keepalive(self, seq_id):
        """Respond to KEEPALIVE requests."""
        keepalive_ack = f"KEEPALIVE_ACK\t{seq_id}"
        self._send_message(keepalive_ack)

    def _send_message(self, raw_msg):
        """Thread-safe message sending."""
        with self.lock:
            try:
                full_msg = self.build_message(raw_msg)
                self.sock.sendall(full_msg)
                logging.info(f"Sent: {raw_msg}")
                print(f"Sent: {raw_msg} Time: {time.strftime('%H:%M:%S')}")
            except Exception as e:
                logging.error(f"Error sending message: {str(e)}")
                print(f"Send error: {e}")
                self.connected = False

    def listen_for_messages(self):
        """Listen for incoming messages and handle them."""
        while self.connected:
            try:
                # Read header (4 bytes)
                header = self.sock.recv(4)
                if not header:
                    break

                # Extract message length
                msg_length = struct.unpack(">I", header)[0]

                # Read remaining message
                data = self.sock.recv(msg_length)
                if not data:
                    break

                # Parse message (strip STX/ETX)
                decoded = data.decode().strip("\x02\x03")
                logging.info(f"Received: {decoded}")
                print(f"Received: {decoded} Time: {time.strftime('%H:%M:%S')}")

                # Handle message types
                parts = decoded.split("\t")
                command= parts[0]
                if command == "SETEV_ACK":
                    self.handle_setev_ack(parts)
                elif command == "STARTEV_ACK":
                    self.handle_startev_ack(parts)
                elif command == "KEEPALIVE":
                    self.handle_keepalive(parts[1])
                elif command == "PGCHANGEII":
                    self.handle_pgchangeii(parts)
                elif command == "PRODSTARTED":
                    self.handle_prodstarted(parts)
                elif command == "PRODCOMPLETED":
                    self.handle_prodcompleted(parts)
                elif command == "MCSTATECHANGE":
                    self.handle_mcstatechange(parts)
                elif command == "MCALARMON":
                    self.handle_mcalarmon(parts)
                elif command == "MCALARMOFF":
                    self.handle_mcalarmoff(parts)
                elif command == "HEADUSAGE":
                    self.handle_headusage(parts)

            except Exception as e:
                print(f" error Receive : {e}")
                logging.error(f" error Receive : {e}")
                self.connected = False
                break

    def handle_setev_ack(self, parts):
        """Process SETEV_ACK reply."""
        seq_id = parts[1]
        result = parts[3]
        if result == "0":
            print("SETEV successful!")
            logging.info(f"SETEV successful! {result}")
        else:
            print(f"SETEV NG! Result: {result}")
            logging.critical(f"SETEV NG! Result: {result}")

    def handle_startev_ack(self, parts):
        """Process STARTEV_ACK reply."""
        seq_id = parts[1]
        result = parts[3]
        if result == "0":
            print("Event notifications started!")
            logging.info(f"Event notifications started! {result}")
        else:
            print(f"STARTEV failed! Result: {result}")
            logging.critical(f"STARTEV failed! Result: {result}")

    def handle_unloadcomp(self, parts):
        """UNLOADCOMP: Parts Removal Notification (UNLOADCOMP)"""
        seq_id = parts[1]
        data = {
            "time": self._parse_time(parts[2]),
            "LineName": parts[3],
            "Machine": parts[4],
            "module": parts[5],
            "components": self._parse_components(parts[7:], 7, [  # 7 fields per component
                'stage', 'slot', 'part_no', 'feeder_id', 'reel_id',
                'quantity', 'remaining_time'
            ])
        }
        logging.info(f"Parts unloaded: {data}")
        
        # Build UNLOADCOMP_ACK
        num_list = len(data['components'])
        ack_parts = [
            f"UNLOADCOMP_ACK",
            seq_id,
            data['Machine'],
            data['module'],
            str(num_list)
        ]
        # Add each component's Stage, Slot, Result (0=OK), PartNo, FeederID, ReelID, RemainingTime
        for comp in data['components']:
            ack_parts.extend([
                comp['stage'],
                comp['slot'],
                '0',  # Assume Result=0 (OK)
                comp['part_no'],
                comp['feeder_id'],
                comp['reel_id'],
                comp['remaining_time']
            ])
        ack_msg = "\t".join(ack_parts)
        self._send_message(ack_msg)

    def handle_pgchangeii(self, parts):
        """6.9.1 Program Change Completion 2 (PGCHANGEII)"""
        seq_id = parts[1]
        data = {
            "time": self._parse_time(parts[2]),
            "LineName": parts[3],
            "Machine": parts[4],
            "ModuleNo": parts[5],
            "LaneNo": parts[6],
            "ProgramName": parts[7],
            "components": self._parse_components(parts[9:], 3, ['stage', 'slot', 'parts'])
        }
        logging.info(f"Program change: {data}")
        
        # PGCHANGEIL_ACK format: SEQ_ID|RESULT|MACHINE|MODULE|LANE|PROGRAM
        ack_msg = f"PGCHANGEIL_ACK\t{seq_id}\t0\t{data['Machine']}\t{data['ModuleNo']}\t{data['LaneNo']}\t{data['ProgramName']}"
        self._send_message(ack_msg)

    def handle_prodstarted(self, parts):
        """6.13.1 Production Start Notification (PRODSTARTED)"""
        seq_id = parts[1]
        data = {
            "time": self._parse_time(parts[2]),
            "LineName": parts[3],
            "Machine": parts[4],
            "ModuleNo": parts[5],
            "LaneNo": parts[6],
            "ProdMode": parts[7],
            "ProgramName": parts[8],
            "PanelNo": parts[9]
        }
        logging.info(f"Production started: {data}")
        
        # PRODSTARTED_ACK format: SEQ_ID|RESULT|MACHINE|MODULE|LANE|PROD_MODE|PROGRAM|PANEL
        ack_msg = f"PRODSTARTED_ACK\t{seq_id}\t0\t{data['Machine']}\t{data['ModuleNo']}\t{data['LaneNo']}\t{data['ProdMode']}\t{data['ProgramName']}\t{data['PanelNo']}"
        self._send_message(ack_msg)

    def handle_prodcompleted(self, parts):
        """6.14.1 Production Completed (PRODCOMPLETED)"""
        seq_id = parts[1]
        data = {
            "time": self._parse_time(parts[2]),
            "LineName": parts[3],
            "Machine": parts[4],
            "ModuleNo": parts[5],
            "LaneNo": parts[6],
            "ProdMode": parts[7],
            "ProgramName": parts[8],
            "PanelNo": parts[9],
            "BlockCount": parts[10],
            "BlockSkipCount": parts[11]
        }
        logging.info(f"Production completed: {data}")
        
        # PRODCOMPLETED_ACK format: SEQ_ID|RESULT|MACHINE|MODULE|LANE|PROD_MODE|PROGRAM|PANEL
        ack_msg = f"PRODCOMPLETED_ACK\t{seq_id}\t0\t{data['Machine']}\t{data['ModuleNo']}\t{data['LaneNo']}\t{data['ProdMode']}\t{data['ProgramName']}\t{data['PanelNo']}"
        self._send_message(ack_msg)

    def handle_mcstatechange(self, parts):
        """6.15.1 Machine State Change (MCSTATECHANGE)"""
        seq_id = parts[1]
        data = {
            "time": self._parse_time(parts[2]),
            "LineName": parts[3],
            "Machine": parts[4],
            "ModuleNo": parts[5],
            "PrevStatus": parts[6],
            "CurrStatus": parts[7]
        }
        logging.info(f"Machine state changed: {data}")
        
        # MCSTATECHANGE_ACK format: SEQ_ID|RESULT|MACHINE|MODULE
        ack_msg = f"MCSTATECHANGE_ACK\t{seq_id}\t0\t{data['Machine']}\t{data['ModuleNo']}"
        self._send_message(ack_msg)

    def handle_mcalarmon(self, parts):
        """6.16.1 Machine Alarm ON (MCALARMON)"""
        seq_id = parts[1]
        data = {
            "time": self._parse_time(parts[2]),
            "LineName": parts[3],
            "Machine": parts[4],
            "ModuleNo": parts[5],
            "ErrorCode": parts[6],
            "SubErrorCode": parts[7]
        }
        logging.info(f"Machine alarm ON: {data}")
        
        # MCALARMON_ACK format: SEQ_ID|RESULT|MACHINE|MODULE
        ack_msg = f"MCALARMON_ACK\t{seq_id}\t0\t{data['Machine']}\t{data['ModuleNo']}"
        self._send_message(ack_msg)

    def handle_mcalarmoff(self, parts):
        """6.17.1 Machine Alarm OFF (MCALARMOFF)"""
        seq_id = parts[1]
        data = {
            "time": self._parse_time(parts[2]),
            "LineName": parts[3],
            "Machine": parts[4],
            "ModuleNo": parts[5],
            "ErrorCode": parts[6],
            "SubErrorCode": parts[7]
        }
        logging.info(f"Machine alarm OFF: {data}")
        
        # MCALARMOFF_ACK format: SEQ_ID|RESULT|MACHINE|MODULE
        ack_msg = f"MCALARMOFF_ACK\t{seq_id}\t0\t{data['Machine']}\t{data['ModuleNo']}"
        self._send_message(ack_msg)

    def handle_headusage(self, parts):
        """6.20.1 Head Pickup Count Report (HEADUSAGE)"""
        seq_id = parts[1]
        data = {
            "time": self._parse_time(parts[2]),
            "LineName": parts[3],
            "Machine": parts[4],
            "ModuleNo": parts[5],
            "components": self._parse_components(parts[7:], 12, [
                'head_no', 'head_id', 'head_name', 'pickup_count',
                'error_head', 'error_reject', 'reject_head',
                'dislodged_head', 'rescan_count', 'no_pickup'
            ])
        }
        logging.info(f"Head usage report: {data}")
        
        # HEADUSAGE_ACK format: SEQ_ID|RESULT|MACHINE|MODULE
        ack_msg = f"HEADUSAGE_ACK\t{seq_id}\t0\t{data['Machine']}\t{data['ModuleNo']}"
        self._send_message(ack_msg)

    def close(self):
        """Close connection gracefully."""
        self.connected = False
        if self.sock:
            self.sock.close()
        print("Connection closed.")

if __name__ == "__main__":
    fuji = FujiHostInterface()
    try:
        fuji.connect()
        # Keep main thread alive
        while fuji.connected:
            time.sleep(1)
    except KeyboardInterrupt:
        fuji.close()