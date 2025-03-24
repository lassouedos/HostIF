import socket
import struct
import threading
import time
import logging
from datetime import datetime
import keyboard
from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker,scoped_session,declarative_base
import sqlalchemy as sa
from collections import deque


# Constants

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

Base = declarative_base()

class ProductionLog(Base):
    __tablename__ = 'production_logs'
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime)
    event_type = Column(String)
    machine = Column(String)
    data = Column(JSON)

class ErrorLog(Base):
    __tablename__ = 'error_logs'
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime)
    error_code = Column(String)
    module = Column(String)
    details = Column(JSON)

# Initialize Backend Logger
backend_logger = logging.getLogger("backend_logger")
backend_logger.setLevel(logging.INFO)
backend_logger.propagate = False  # Ensure no propagation
# Add handler only if none exist
if not backend_logger.handlers:
    backend_handler = logging.FileHandler("fuji_interface.log")
    backend_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    backend_logger.addHandler(backend_handler)



class FujiHostInterface:
    def __init__(self):
        self.sock = None
        self.seq_id = 0
        self.connected = False
        self.lock = threading.Lock()
        self.HOST = '192.168.100.231'  # Central Server Lite IP
        self.PORT = 30040
        self.HOSTNAME = None
        # Initialize production state with proper structure
        self.production_state = {
            'current_program': None,
            'active_panels': {},
            'feeder_config': {},
            'error_log': [],
            'bom_data': {},
            'slot_status': {},
            'production_history': [],
            'message_log' : []

        }

        self.current_db_day = datetime.now().day
        self.engine = None
        self.Session = None
        self._init_daily_db()

    def _init_daily_db(self):
        """Initialize database connection for the day"""
        self.engine = self._get_daily_engine()
        self.Session = scoped_session(sessionmaker(bind=self.engine))
        Base.metadata.create_all(self.engine)
        backend_logger.info(f"Initialized daily database: production_{datetime.now().strftime('%Y%m%d')}.db")

    def _get_daily_engine(self):
        """Instance method to create daily database engine"""
        today = datetime.now().strftime("%Y%m%d")
        return create_engine(f'sqlite:///production_{today}.db')

    
    def _check_daily_rotation(self):   
        if datetime.now().day != self.current_db_day:
            backend_logger.info("Rotating to new daily database")
            self._init_daily_db()
            self.current_db_day = datetime.now().day
            

    def resolve_hostname(self):
        """Attempt to resolve IP to hostname"""
        try:
            self.HOSTNAME = socket.gethostbyaddr(self.HOST)[0]
        except (socket.herror, socket.gaierror) as e:
            backend_logger.warning(f"Could not resolve hostname: {str(e)}")
            self.HOSTNAME = self.HOST  # Fallback to IP

    def log_production_event(self, event_type: str, data: dict):
        self._check_daily_rotation()
        session = self.Session()
        try:
            log = ProductionLog(
                timestamp=datetime.now(),
                event_type=event_type,
                machine=MACHINE,
                data=data
            )
            session.add(log)
            session.commit()
        except Exception as e:
            backend_logger.error(f"Failed to log event: {str(e)}")
        finally:
            session.close()

    def log_error_event(self, error_code: str, module: str, details: dict):
        session = self.Session()
        try:
            log = ErrorLog(
                timestamp=datetime.now(),
                error_code=error_code,
                module=module,
                details=details
            )
            session.add(log)
            session.commit()
        except Exception as e:
            backend_logger.error(f"Failed to log error: {str(e)}")
        finally:
            session.close()

    def build_message(self, raw_msg):
        """Add STX/ETX and 4-byte big-endian length header."""
        stx_etx_msg = f"\x02{raw_msg}\x03"
        msg_length = len(stx_etx_msg)
        header = struct.pack(">I", msg_length)
        return header + stx_etx_msg.encode()
    
    def _parse_time(self, timestr):
        # Strict ISO 8601 variant validation
        if len(timestr) != 14 or not timestr.isdigit():
            raise ValueError(f"Invalid time format: {timestr}")
        return datetime.strptime(timestr, "%Y%m%d%H%M%S")

    def _parse_components(self, parts, fields_per_item, field_names):
        components = []
        for i in range(0, len(parts), fields_per_item):
            component = {name: parts[i+j] for j, name in enumerate(field_names)}
            components.append(component)
        return components
    
    # NXTR SPECIFIC SLOT RANGES MISSING
    def validate_slot(self, module_type, slot):
        nxtr_ranges = {
            'feeder': (1, 99),
            'tray_ma': (901, 924),
            'tray_mb': (925, 948)
        }
        if module_type == 'NXTR' and not (
            nxtr_ranges['feeder'][0] <= slot <= nxtr_ranges['feeder'][1] or
            nxtr_ranges['tray_ma'][0] <= slot <= nxtr_ranges['tray_ma'][1]
        ):
            raise ValueError(f"Invalid NXTR slot: {slot}")
        
    def connect(self):
        try : 
            """Establish TCP connection to Central Server Lite."""
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.HOST, self.PORT))
            self.connected = True
            self.resolve_hostname()  # Resolve hostname on connect
            backend_logger.info(f"Connected to {self.HOSTNAME}==>{self.HOST}:{self.PORT}")
            print(f"Connected to {self.HOST}:{self.PORT}")

            # Start thread to listen for incoming messages
            threading.Thread(target=self.listen_for_messages, daemon=True).start()

            # Perform initialization sequence
            self.send_setev()
            self.send_startev()
        except Exception as e:
            backend_logger.error(f"Connection failed: {str(e)}")
            print(f"Connection failed: {str(e)}")
            self.connected = False
            time.sleep(5)



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
                backend_logger.info(f"Sent: {raw_msg}")
                # In _send_message():
                log_entry = {
                    'timestamp': datetime.now().strftime('%H:%M:%S'),
                    'direction': 'sent',
                    'raw': raw_msg
                }
                self.production_state['message_log'].append(log_entry)
                # Keep log size manageable
                if len(self.production_state['message_log']) > 100:
                    self.production_state['message_log'].pop(0)
                print(f"Sent: {raw_msg} Time: {time.strftime('%H:%M:%S')}")
            except Exception as e:
                backend_logger.error(f"Error sending message: {str(e)}")
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
                backend_logger.info(f"Received: {decoded}")
                print(f"Received: {decoded} Time: {time.strftime('%H:%M:%S')}")

                log_entry = {
                    'timestamp': datetime.now().strftime('%H:%M:%S'),
                    'direction': 'received',
                    'raw': decoded
                }
                self.production_state['message_log'].append(log_entry)
                if len(self.production_state['message_log']) > 100:
                    self.production_state['message_log'].pop(0)              

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
                elif command == "FEEDERLIST_ACK":
                    self.handle_feederlist_ack(parts)
                elif command == "BOMLIST":
                    self.handle_bomlist(parts)
                elif command == "PCBCHECKIN":
                    self.handle_pcbcheckin(parts)
                elif command == "PCBCHECKOUT":
                    self.handle_pcbcheckout(parts)
                elif command == "FEEDERSETUP":
                    self.handle_feedersetup(parts)
                elif command == "SLOTSTTCHG":
                    self.handle_slotsttchg(parts)
                elif command == "ERRORREPORT":  
                    self.handle_errorreport(parts)
                elif command == "PARTSREFILL":  
                    self.handle_partsrefill(parts)
                elif command == "UNLOADCOMP":
                    self.handle_unloadcomp(parts)
                elif command == "PRODCOMPLETEDII":
                    self.handle_prodcompletedii(parts)
                

            except Exception as e:
                print(f" error Receive : {e}")
                backend_logger.error(f" error Receive : {e}")
                self.connected = False
                break

    def handle_setev_ack(self, parts):
        """Process SETEV_ACK reply."""
        seq_id = parts[1]
        result = parts[3]
        if result == "0":
            print("SETEV successful!")
            backend_logger.info(f"SETEV successful! {result}")
        else:
            print(f"SETEV NG! Result: {result}")
            backend_logger.critical(f"SETEV NG! Result: {result}")

    def handle_startev_ack(self, parts):
        """Process STARTEV_ACK reply."""
        seq_id = parts[1]
        result = parts[3]
        if result == "0":
            print("Event notifications started!")
            backend_logger.info(f"Event notifications started! {result}")
        else:
            print(f"STARTEV failed! Result: {result}")
            backend_logger.critical(f"STARTEV failed! Result: {result}")

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
        backend_logger.info(f"Parts unloaded: {data}")
        
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
        backend_logger.info(f"Program change: {data}")
        
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
        backend_logger.info(f"Production started: {data}")
        
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
        backend_logger.info(f"Production completed: {data}")
        
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
        backend_logger.info(f"Machine state changed: {data}")
        
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
        backend_logger.info(f"Machine alarm ON: {data}")
        
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
        backend_logger.info(f"Machine alarm OFF: {data}")
        
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
        backend_logger.info(f"Head usage report: {data}")
        
        # HEADUSAGE_ACK format: SEQ_ID|RESULT|MACHINE|MODULE
        ack_msg = f"HEADUSAGE_ACK\t{seq_id}\t0\t{data['Machine']}\t{data['ModuleNo']}"
        self._send_message(ack_msg)

        # Add to FujiHostInterface class
    
    def send_feederlist_request(self, program_name="NXTIIIPAMH241005pin", group_name="PAM"):
        """Send FEEDERLIST request to get feeder positions"""
        self.seq_id += 1
        side=""
        current_time = datetime.now().strftime("%Y%m%d%H%M%S")
        feederlist_msg = (
                f"FEEDERLIST\t{self.seq_id}\t{current_time}\t{LINE_NAME}\t{MACHINE}\t"
                f"{group_name}\t{program_name}{side}"
            )        
        self._send_message(feederlist_msg)

    def handle_feederlist_ack(self, parts):
        """Process FEEDERLIST_ACK reply"""
        seq_id = parts[1]
        result = parts[2]
        machine = parts[3]
        group = parts[4]
        program = parts[5]
        
        feeder_data = []
        if result == "0":
            # Parse feeder position data
            num_feeder = int(parts[6])
            index = 7
            
            for _ in range(num_feeder):
                module = parts[index]
                stage = parts[index+1]
                slot = parts[index+2]
                feeder_name = parts[index+3]
                qty = parts[index+4]
                
                # Parse parts
                num_parts = int(parts[index+5])
                parts_list = parts[index+6 : index+6+num_parts]
                index += 6 + num_parts
                
                # Parse references
                num_refs = int(parts[index])
                refs_list = parts[index+1 : index+1+num_refs]
                index += 1 + num_refs
                
                feeder_data.append({
                    'module': module,
                    'stage': stage,
                    'slot': slot,
                    'feeder_name': feeder_name,
                    'qty': qty,
                    'parts': parts_list,
                    'references': refs_list
                })
                
            backend_logger.info(f"Received feeder positions for {program}")
            print("Feeder Position Data:")
            for fd in feeder_data:
                print(f"Module {fd['module']} | Slot {fd['slot']} | Feeder {fd['feeder_name']}")
                print(f"-> Parts: {', '.join(fd['parts'])}")
                print(f"-> Refs: {', '.join(fd['references'])}")

    def handle_bomlist(self, parts):
        """6.10.1 BOM list notification"""
        try:
            seq_id = parts[1]
            time = self._parse_time(parts[2])
            line = parts[3]
            machine = parts[4]
            lane = parts[5]
            program = parts[6]
            
            bom_items = []
            num_items = int(parts[7])
            index = 8
            
            for _ in range(num_items):
                block = parts[index]
                part = parts[index+1]
                ref = parts[index+2]
                bom_items.append({
                    'block': block,
                    'part': part,
                    'reference': ref
                })
                index +=3

            # Send ACK
            ack_msg = f"BOMLIST_ACK\t{seq_id}\t0\t{machine}\t{lane}\t{program}"
            self._send_message(ack_msg)
            
            backend_logger.info(f"Received BOM list for {program}")
            self.process_bom(bom_items)

        except Exception as e:
            backend_logger.error(f"BOMLIST error: {str(e)}")

    def process_bom(self, bom_items):
        """Process BOM data"""
        print("\n=== BOM List ===")
        backend_logger.info("=== BOM List ===")
        for item in bom_items:
            
            print(f"Block {item['block']}: Part {item['part']} ({item['reference']})")
            backend_logger.info(f"Block {item['block']}: Part {item['part']} ({item['reference']})")
    

    def handle_pcbcheckin(self, parts):
        """6.28.1 Panel checkin notification"""
        try:
            seq_id = parts[1]
            time = self._parse_time(parts[2])
            line = parts[3]
            machine = parts[4]
            lane = parts[5]
            program = parts[6]
            panel_id = parts[7]
            msl_time = parts[8]

            # Send ACK
            ack_msg = f"PCBCHECKIN_ACK\t{seq_id}\t0\t{machine}\t{lane}\t{program}\t{panel_id}"
            self._send_message(ack_msg)
            
            backend_logger.info(f"Panel {panel_id} checked in")
            self.process_panel_checkin(panel_id, msl_time)

        except Exception as e:
            backend_logger.error(f"PCBCHECKIN error: {str(e)}")
        
    def handle_pcbcheckout(self, parts):
        """6.29.1 Panel checkout notification"""
        try:
            seq_id = parts[1]
            time = self._parse_time(parts[2])
            line = parts[3]
            machine = parts[4]
            lane = parts[5]
            program = parts[6]
            panel_id = parts[7]
            msl_time = parts[8]
            status = parts[9]
            
            components = []
            num_components = int(parts[10])
            index = 11
            
            for _ in range(num_components):
                comp = {
                    'module': parts[index],
                    'stage': parts[index+1],
                    'slot': parts[index+2],
                    'part': parts[index+3],
                    'reel': parts[index+4],
                    'feeder': parts[index+5],
                    'pickups': parts[index+6],
                    'errors': parts[index+7],
                    'rejects': parts[index+8],
                    'dislodged': parts[index+9],
                    'nopickup': parts[index+10]
                }
                components.append(comp)
                index +=11

            # Send ACK
            ack_msg = f"PCBCHECKOUT_ACK\t{seq_id}\t0\t{machine}\t{lane}\t{program}\t{panel_id}"
            self._send_message(ack_msg)
            
            backend_logger.info(f"Panel {panel_id} checked out")
            self.process_panel_checkout(panel_id, components)

        except Exception as e:
            backend_logger.error(f"PCBCHECKOUT error: {str(e)}")

     # Add to FujiHostInterface class

    def handle_feedersetup(self, parts):
        """6.24.1 Feeder setup report"""
        try:
            seq_id = parts[1]
            time = self._parse_time(parts[2])
            line = parts[3]
            machine = parts[4]
            module = parts[5]
            program = parts[6]
            
            feeders = []
            num_feeders = int(parts[7])
            index = 8
            
            for _ in range(num_feeders):
                feeders.append({
                    'stage': parts[index],
                    'slot': parts[index+1],
                    'feeder_id': parts[index+2]
                })
                index +=3

            ack_msg = (f"FEEDERSETUP_ACK\t{seq_id}\t{machine}\t{module}\t{num_feeders}\t" +
                    "\t".join([f"{f['stage']}\t{f['slot']}\t0\t{f['feeder_id']}" 
                                for f in feeders]))
            self._send_message(ack_msg)
            
            self.process_feeder_setup(feeders)

        except Exception as e:
            backend_logger.error(f"FEEDERSETUP error: {str(e)}")

    def handle_partsrefill(self, parts):
        """6.25.1 Part resupply request"""
        try:
            seq_id = parts[1]
            time = self._parse_time(parts[2])
            machine = parts[3]
            module = parts[4]
            stage = parts[5]
            slot = parts[6]
            
            refill_data = {
                'feeder_id': parts[7],
                'status': parts[8],
                'comp_chg': parts[9],
                'verify_type': parts[10],
                'reels': []
            }
            
            num_reels = int(parts[11])
            index = 12
            
            for _ in range(num_reels):
                refill_data['reels'].append({
                    'reel_id': parts[index],
                    'part_no': parts[index+1],
                    'vendor': parts[index+2],
                    'lot': parts[index+3],
                    'datecode': parts[index+4],
                    'lighting': parts[index+5],
                    'qty': parts[index+6],
                    'tray_count': parts[index+7],
                    'x': parts[index+8],
                    'y': parts[index+9],
                    'status': parts[index+10]
                })
                index +=11

            ack_msg = f"PARTSREFILL_ACK\t{seq_id}\t0"
            self._send_message(ack_msg)
            
            self.process_parts_refill(refill_data)

        except Exception as e:
            backend_logger.error(f"PARTSREFILL error: {str(e)}")

    def handle_slotsttchg(self, parts):
        """6.23.1 Change device status command"""
        try:
            seq_id = parts[1]
            time = self._parse_time(parts[2])
            line = parts[3]
            machine = parts[4]
            module = parts[5]
            
            changes = []
            num_changes = int(parts[6])
            index = 7
            
            for _ in range(num_changes):
                changes.append({
                    'stage': parts[index],
                    'slot': parts[index+1],
                    'status': parts[index+2],
                    'sub_status': parts[index+3]
                })
                index +=4

            ack_msg = f"SLOTSTTCHG_ACK\t{seq_id}\t0\t{machine}\t{module}"
            self._send_message(ack_msg)
            
            self.process_slot_status_changes(changes)

        except Exception as e:
            backend_logger.error(f"SLOTSTTCHG error: {str(e)}")

    def handle_errorreport(self, parts):
        """6.26.1 Error report"""
        try:
            error_data = {
                'seq_id': parts[1],
                'time': self._parse_time(parts[2]),
                'line': parts[3],
                'machine': parts[4],
                'module': parts[5],
                'stage': parts[6],
                'slot': parts[7],
                'status': parts[8],
                'qty': parts[9],
                'tray_count': parts[10],
                'x': parts[11],
                'y': parts[12]
            }

            ack_msg = f"ERRORREPORT_ACK\t{error_data['seq_id']}\t0"
            self._send_message(ack_msg)
            
            self.process_error_report(error_data)

        except Exception as e:
            backend_logger.error(f"ERRORREPORT error: {str(e)}")

    def handle_prodcompletedii(self, parts):
        """6.27.1 Production completed II"""
        try:
            prod_data = {
                'seq_id': parts[1],
                'time': self._parse_time(parts[2]),
                'line': parts[3],
                'machine': parts[4],
                'module': parts[5],
                'lane': parts[6],
                'mode': parts[7],
                'program': parts[8],
                'panel': parts[9],
                'blocks': parts[10],
                'skips': parts[11],
                'bs_info': parts[12],
                'cycle_time': parts[13]
            }

            ack_msg = (f"PRODCOMPLETEDII_ACK\t{prod_data['seq_id']}\t0\t"
                    f"{prod_data['machine']}\t{prod_data['module']}\t"
                    f"{prod_data['lane']}\t{prod_data['mode']}\t"
                    f"{prod_data['program']}\t{prod_data['panel']}")
            self._send_message(ack_msg)
            
            self.process_production_complete_ii(prod_data)

        except Exception as e:
            backend_logger.error(f"PRODCOMPLETEDII error: {str(e)}")

    def process_bom(self, bom_items):
        """Process and store BOM data with error handling"""
        try:
            backend_logger.info("Processing BOM data...")
            self.production_state['bom_data'].clear()
            
            for item in bom_items:
                part_number = item['part']
                if part_number not in self.production_state['bom_data']:
                    self.production_state['bom_data'][part_number] = {
                        'references': [],
                        'blocks': []
                    }
                
                self.production_state['bom_data'][part_number]['references'].append(item['reference'])
                self.production_state['bom_data'][part_number]['blocks'].append(item['block'])
            
            backend_logger.info(f"Processed {len(bom_items)} BOM items")
            print(f"BOM updated with {len(self.production_state['bom_data'])} unique parts")

        except Exception as e:
            backend_logger.error(f"BOM processing failed: {str(e)}")
            self._send_system_alert("BOM_PROCESSING_ERROR")

    def process_panel_checkin(self, panel_id, msl_time):
        """Handle panel check-in with validation"""
        try:
            if panel_id in self.production_state['active_panels']:
                raise ValueError(f"Panel {panel_id} already in system")
            
            self.production_state['active_panels'][panel_id] = {
                'checkin_time': datetime.now(),
                'msl_deadline': self._parse_time(msl_time),
                'placement_data': {},
                'status': 'IN_PROGRESS'
            }
            backend_logger.info(f"Panel {panel_id} checked in successfully")
            print(f"New panel registered: {panel_id}")

        except ValueError as ve:
            backend_logger.warning(f"Invalid panel checkin: {str(ve)}")
        except Exception as e:
            backend_logger.error(f"Panel checkin processing error: {str(e)}")

    def process_panel_checkout(self, panel_id, components):
        """Process panel checkout and calculate metrics"""
        try:
            if panel_id not in self.production_state['active_panels']:
                raise KeyError(f"Unknown panel {panel_id}")
            
            panel_data = self.production_state['active_panels'].pop(panel_id)
            panel_data['checkout_time'] = datetime.now()
            
            # Calculate placement statistics
            total = 0
            errors = 0
            for comp in components:
                total += int(comp['pickups'])
                errors += int(comp['errors']) + int(comp['rejects']) + int(comp['dislodged'])
            
            panel_data['yield'] = ((total - errors) / total * 100) if total > 0 else 0
            panel_data['components'] = components
            
            backend_logger.info(f"Panel {panel_id} completed with {panel_data['yield']:.1f}% yield")
            print(f"Panel {panel_id} checkout processed. Yield: {panel_data['yield']:.1f}%")

        except KeyError as ke:
            backend_logger.error(f"Panel checkout error: {str(ke)}")
        except Exception as e:
            backend_logger.error(f"Panel processing failed: {str(e)}")

    def process_feeder_data(self, feeder_data):
        """Update feeder configuration state"""
        try:
            self.production_state['feeder_config'].clear()
            
            for fd in feeder_data:
                config_key = f"{fd['module']}-{fd['stage']}-{fd['slot']}"
                self.production_state['feeder_config'][config_key] = {
                    'feeder_type': fd['feeder_name'],
                    'part_numbers': fd['parts'],
                    'references': fd['references'],
                    'quantity': int(fd['quantity']),
                    'last_updated': datetime.now()
                }
            
            backend_logger.info(f"Updated {len(feeder_data)} feeder positions")
            print(f"Feeder configuration updated with {len(self.production_state['feeder_config'])} entries")

        except ValueError as ve:
            backend_logger.error(f"Invalid feeder data format: {str(ve)}")
        except Exception as e:
            backend_logger.error(f"Feeder data processing failed: {str(e)}")

    def process_error_report(self, error_data):
        """Handle error reports with severity classification"""
        try:
            error_entry = {
                'timestamp': datetime.now(),
                'module': error_data['module'],
                'slot': error_data['slot'],
                'code': error_data['status'],
                'coordinates': (error_data['x'], error_data['y']),
                'qty_remaining': int(error_data['qty'])
            }
            
            self.production_state['error_log'].append(error_entry)
            
            # Classify error severity
            if error_data['status'] in ['900', '901', '902']:
                self._escalate_critical_error(error_entry)
            
            backend_logger.warning(f"Error logged: {error_data['status']} at {error_data['module']}")
            print(f"Error {error_data['status']} recorded at module {error_data['module']}")

        except Exception as e:
            backend_logger.error(f"Error processing failed: {str(e)}")

    def _escalate_critical_error(self, error_entry):
        """Internal method for critical error handling"""
        alert_msg = (f"CRITICAL ERROR {error_entry['code']} | "
                    f"Module: {error_entry['module']} | "
                    f"Slot: {error_entry['slot']} | "
                    f"Qty Remaining: {error_entry['qty_remaining']}")
        print(f"! SYSTEM ALERT ! {alert_msg}")
        backend_logger.critical(alert_msg)
        # Add actual notification logic here (email, SMS, etc.)

    def _send_system_alert(self, alert_type):
        """Unified alerting method"""
        alert_msg = f"{alert_type} | {datetime.now().isoformat()}"
        print(f"! SYSTEM ALERT ! {alert_msg}")
        backend_logger.critical(alert_msg)

    def close(self):
        """Close connection gracefully."""
        self.connected = False
        if self.sock:
            self.sock.close()
        print("Connection closed.")

# Modify main execution block
if __name__ == "__main__":
    fuji = FujiHostInterface()
    try:
        fuji.connect()
        print("Press B to request feeder positions")
        while fuji.connected:
            if keyboard.is_pressed('b'):  # Requires keyboard package
                fuji.send_feederlist_request()
                time.sleep(0.5)  # Debounce
            elif keyboard.is_pressed('c'):
                backend_logger.info(f"close it by keyboard")
                fuji.close()
            time.sleep(0.1)
    except KeyboardInterrupt:
        fuji.close()