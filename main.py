from fastapi import FastAPI, WebSocket, Request,Form
from fastapi.responses import HTMLResponse,FileResponse,RedirectResponse,JSONResponse
from contextlib import asynccontextmanager
from test import FujiHostInterface,backend_logger
from configuration import SECRET_KEY
import asyncio
import logging
import time
import socket
import threading
from weakref import WeakSet
import colorlog
from pydantic import BaseModel
from datetime import datetime
from typing import Literal
import os
from starlette.middleware.sessions import SessionMiddleware
from fastapi import HTTPException
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

CONFIG_DIR = Path("config")

# Configure API Request Logger
api_logger = logging.getLogger("api_logger")
api_logger.setLevel(logging.INFO)
api_handler = logging.FileHandler("api_requests.log")
api_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
api_logger.addHandler(api_handler)
api_logger.propagate = False  # Disable propagation to root


logger = logging.getLogger(__name__)

# Global state
clients = WeakSet()
fuji_instance: FujiHostInterface | None = None
hostname = socket.gethostname()
connection_lock = threading.Lock()

def get_fuji_status() -> dict:
    """Returns the current connection status of the Fuji machine"""
    return {
        'connected': fuji_instance.connected if fuji_instance else False,
        'host': fuji_instance.HOST if fuji_instance else 'N/A',
        'hostname': fuji_instance.HOSTNAME if fuji_instance else 'N/A',
        'port': fuji_instance.PORT if fuji_instance else 'N/A',
        'local_hostname': hostname
    }

@asynccontextmanager
async def lifespan(app: FastAPI):
    global fuji_instance
    try:
        fuji_instance = FujiHostInterface()
        await asyncio.to_thread(fuji_instance.connect)
        asyncio.create_task(broadcast_updates())
        asyncio.create_task(connection_monitor())
        yield
    finally:
        if fuji_instance:
            await asyncio.to_thread(fuji_instance.close)
        clients.clear()
        fuji_instance = None

app = FastAPI(lifespan=lifespan)

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)


# Colored Logging Format
formatter = colorlog.ColoredFormatter(
    "%(log_color)s%(asctime)s - %(levelname)s - %(message)s",
    log_colors={
        'DEBUG': 'cyan',
        'INFO': 'green',
        'WARNING': 'yellow',
        'ERROR': 'red',
        'CRITICAL': 'bold_red',
    }
)


# Connection monitor task
async def connection_monitor():
    while True:
        try:
            if fuji_instance and not fuji_instance.connected:
                backend_logger.info("Attempting to reconnect...")
                fuji_instance.connect()
            await asyncio.sleep(5)
        except Exception as e:
            backend_logger.error(f"Connection monitor error: {str(e)}")
            await asyncio.sleep(5)

@app.middleware("http")

async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    
    api_logger.info(
        f"Method={request.method} Path={request.url.path} "
        f"Status={response.status_code} Duration={process_time:.2f}s"
    )
    return response

class MessageLog(BaseModel):
    timestamp: datetime
    direction: Literal['sent', 'received']
    raw: str
    machine: str
    module: str | None = None

# Thread-safe broadcast updates
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    cookie_header = websocket.headers.get("cookie")
    if not cookie_header or "session" not in cookie_header:
        await websocket.close(code=1008)
        return
    
    await websocket.accept()
    clients.add(websocket)
    try:
        initial_data = {
            'message_log': [],
            'fuji_status': get_fuji_status()
        }
        
        if fuji_instance:
            with connection_lock:
                all_messages = fuji_instance.production_state.get('message_log', [])
                # Send last 100 messages in reverse order (newest first)
                initial_data['message_log'] = all_messages[-10000:][::-1]
        
        await websocket.send_json(initial_data)
        
        # Keep connection alive
        while True:
            await websocket.receive_text()
    except Exception as e:
        logger.error(f"WebSocket error: {str(e)}")
    finally:
        clients.discard(websocket)

async def broadcast_updates():
    last_index = 0
    while True:
        try:
            if fuji_instance and fuji_instance.connected:
                with connection_lock:
                    messages = fuji_instance.production_state.get('message_log', [])
                    current_count = len(messages)
                    
                    if current_count > last_index:
                        # Send messages in reverse order (newest first)
                        new_messages = messages[last_index:current_count][::-1]
                        last_index = current_count
                        
                        for client in list(clients):
                            try:
                                await client.send_json({
                                    'new_messages': new_messages,
                                    'fuji_status': get_fuji_status()
                                })
                            except Exception as e:
                                logger.error(f"Client error: {str(e)}")
                                clients.discard(client)
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Broadcast error: {str(e)}")
            await asyncio.sleep(1)

# Add login routes
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return FileResponse(r"pages/signin.html")

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    print(f"Received login attempt - Username: {username}, Password: {password}")  # Debugging log

    if username == "admin" and password == "1":
        request.session["authenticated"] = True
        request.session["username"] = username  # Store username

        print(f"Login successful,{username} redirecting...")  # Debugging log
        backend_logger.info(f"Login successful,{username} redirecting...")
        return RedirectResponse(url="/", status_code=303)
    
    print("Invalid login attempt")  # Debugging log
    return HTMLResponse("Invalid credentials", status_code=401)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")

@app.get("/check-session")
async def check_session(request: Request):
    return {
        "authenticated": request.session.get("authenticated", False),
        "username": request.session.get("username", "")
    }

# Add route to serve the form
@app.get("/add-line", response_class=HTMLResponse)
async def add_line_page(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login")
    return FileResponse(r"pages/add_line.html")

# Add route to handle form submission
@app.post("/save-line-config")
async def save_line_config(request: Request,
    line_name: str = Form(...),
    machine_name: str = Form(...),
    machine_type: str = Form(...),
    nbr_modules: int = Form(...),  # Changed from Number_module
    server_ip: str = Form(...),
    hostif_port: int = Form(...),
    kitting_port: str = Form(None),  # Make optional
    db_type: str = Form(...),
    nexim_dbname: str = Form(...),
    nexim_db_superusername: str = Form(...),  # Changed from nexim_superuser
    nexim_db_superuserpwd: str = Form(...),  # Changed from nexim_superpass
    fuji_dbname: str = Form(...),  # Changed from fujidb_name
    fuji_dbadminname: str = Form(...),  # Changed from fujidb_admin
    fuji_dbadminpwd: str = Form(...),  # Changed from fujidb_adminpass
    fuji_dbusername: str = Form(...),  # Changed from fujidb_user
    fuji_dbuserpwd: str = Form(...),  # Changed from fujidb_userpass
    profilername: str = Form(None),
    profiler_adminname: str = Form(None),
    profiler_adminpwd: str = Form(None)):
    



    try :
        # Validate database type
        if db_type.lower() not in ["oracle", "sqlserver"]:
            raise HTTPException(status_code=400, detail="Invalid database type")
        # Create config directory if not exists
        CONFIG_DIR = Path("config")
        CONFIG_DIR.mkdir(exist_ok=True)
        # Create XML structure
        root = ET.Element("ProductionLineConfig")
        
        # Basic Info
        basic = ET.SubElement(root, "Basic")
        ET.SubElement(basic, "LineName").text = line_name
        ET.SubElement(basic, "MachineName").text = machine_name
        ET.SubElement(basic, "MachineType").text = machine_type
        ET.SubElement(basic,"Number_module").text=str(nbr_modules)
        # Network
        network = ET.SubElement(root, "Network")
        ET.SubElement(network, "ServerIP").text = server_ip
        ET.SubElement(network, "HostIFPort").text = str(hostif_port)
        ET.SubElement(network, "KittingPort").text = str(kitting_port)
        
        # Databases
        dbs = ET.SubElement(root, "Databases")
        nexim = ET.SubElement(dbs, "NeximDB")
        ET.SubElement(nexim, "Type").text = db_type
        ET.SubElement(nexim, "Name").text = nexim_dbname
        ET.SubElement(nexim, "SuperUser").text = nexim_db_superusername
        ET.SubElement(nexim, "Password").text = nexim_db_superuserpwd
        
        fujidb = ET.SubElement(dbs, "FujiDB")
        ET.SubElement(fujidb, "Name").text = fuji_dbname
        ET.SubElement(fujidb, "AdminUser").text = fuji_dbadminname
        ET.SubElement(fujidb, "AdminPassword").text = fuji_dbadminpwd
        ET.SubElement(fujidb, "AppUser").text = fuji_dbusername
        ET.SubElement(fujidb, "AppPassword").text = fuji_dbuserpwd
        
        profiler = ET.SubElement(root, "Profiler")
        if profilername:
            ET.SubElement(profiler, "Name").text = profilername
            ET.SubElement(profiler, "AdminUser").text = profiler_adminname
            ET.SubElement(profiler, "AdminPassword").text = profiler_adminpwd
        
        # Save to file
        filename = CONFIG_DIR / "Line_config.xml"
        tree = ET.ElementTree(root)
        tree.write(filename, encoding="utf-8", xml_declaration=True)
        
        return {"status": "success", "message": "Configuration saved"}
    except Exception as e:
        backend_logger.error(f"XML generation error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/lines", response_class=HTMLResponse)
async def list_lines(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login")
    
    lines = []
    for file in CONFIG_DIR.glob("*.xml"):
        lines.append(file.stem.replace("_config", ""))
    
    return f"""
    <html>
        <head>
            <title>Configured Lines</title>
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-gray-900 text-gray-100 min-h-screen">
            <nav class="bg-gray-800 p-4">
                <div class="container mx-auto flex justify-between items-center">
                    <h1 class="text-xl font-bold">Configured Production Lines</h1>
                    <a href="/" class="bg-gray-600 hover:bg-gray-700 px-4 py-2 rounded-lg">Back</a>
                </div>
            </nav>
            <div class="container mx-auto px-4 py-8">
                <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                    {"".join([
                        f'''<div class="bg-gray-800 p-4 rounded-lg">
                            <h3 class="text-lg font-semibold mb-2">{line}</h3>
                            <a href="/edit-line?line={quote(line)} 
                               class="text-indigo-400 hover:text-indigo-300">Edit</a>
                        </div>'''
                        for line in lines
                    ])}
                </div>
            </div>
        </body>
    </html>
    """

@app.get("/get-line-config")
async def get_line_config(line: str):
    try:
        file_path = CONFIG_DIR / "line_config.xml"
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Configuration not found")
        
        tree = ET.parse(file_path)
        root = tree.getroot()
        
        config = {
            "line_name": root.findtext("Basic/LineName"),
            "machine_name": root.findtext("Basic/MachineName"),
            "machine_type": root.findtext("Basic/MachineType"),
            "nbr_modules": root.findtext("Basic/Number_module"),
            "server_ip": root.findtext("Network/ServerIP"),
            "hostif_port": root.findtext("Network/HostIFPort"),
            "kitting_port": root.findtext("Network/KittingPort"),
            "db_type": root.findtext("Databases/NeximDB/Type"),
            "nexim_dbname": root.findtext("Databases/NeximDB/Name"),
            "nexim_db_superusername": root.findtext("Databases/NeximDB/SuperUser"),
            "nexim_db_superuserpwd": root.findtext("Databases/NeximDB/Password"),
            "fuji_dbname": root.findtext("Databases/FujiDB/Name"),
            "fuji_dbadminname": root.findtext("Databases/FujiDB/AdminUser"),
            "fuji_dbadminpwd": root.findtext("Databases/FujiDB/AdminPassword"),
            "fuji_dbusername": root.findtext("Databases/FujiDB/AppUser"),
            "fuji_dbuserpwd": root.findtext("Databases/FujiDB/AppPassword"),
            "profilername": root.findtext("Profiler/Name"),
            "profiler_adminname": root.findtext("Profiler/AdminUser"),
            "profiler_adminpwd": root.findtext("Profiler/AdminPassword")
        }
        
        return JSONResponse(content=config)
    
    except Exception as e:
        logger.error(f"Error loading config: {str(e)}")
        raise HTTPException(status_code=500, detail="Error loading configuration")

@app.get("/edit-line", response_class=HTMLResponse)
async def edit_line_page(request: Request, line: str):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login")
    return FileResponse(r"pages/edit_line.html")


    
@app.post("/update-line-config")
async def update_line_config(
    request: Request,
    original_line_name: str = Form(...),
    line_name: str = Form(...),
    machine_name: str = Form(...),
    machine_type: str = Form(...),
    nbr_modules: int = Form(...),
    server_ip: str = Form(...),
    hostif_port: int = Form(...),
    kitting_port: str = Form(None),
    db_type: str = Form(...),
    nexim_dbname: str = Form(...),
    nexim_db_superusername: str = Form(...),
    nexim_db_superuserpwd: str = Form(...),
    fuji_dbname: str = Form(...),
    fuji_dbadminname: str = Form(...),
    fuji_dbadminpwd: str = Form(...),
    fuji_dbusername: str = Form(...),
    fuji_dbuserpwd: str = Form(...),
    profilername: str = Form(None),
    profiler_adminname: str = Form(None),
    profiler_adminpwd: str = Form(None)
):
    try:
        # Validate database type
        if db_type.lower() not in ["oracle", "sqlserver"]:
            raise HTTPException(status_code=400, detail="Invalid database type")

        # Delete old config if name changed
        if original_line_name != line_name:
            old_file = CONFIG_DIR / f"{original_line_name}_config.xml"
            if old_file.exists():
                old_file.unlink()

        # Create XML structure
        root = ET.Element("ProductionLineConfig")
        
        # Basic Info
        basic = ET.SubElement(root, "Basic")
        ET.SubElement(basic, "LineName").text = line_name
        ET.SubElement(basic, "MachineName").text = machine_name
        ET.SubElement(basic, "MachineType").text = machine_type
        ET.SubElement(basic, "Number_module").text = str(nbr_modules)
        
        # Network
        network = ET.SubElement(root, "Network")
        ET.SubElement(network, "ServerIP").text = server_ip
        ET.SubElement(network, "HostIFPort").text = str(hostif_port)
        ET.SubElement(network, "KittingPort").text = str(kitting_port)
        
        # Databases
        dbs = ET.SubElement(root, "Databases")
        nexim = ET.SubElement(dbs, "NeximDB")
        ET.SubElement(nexim, "Type").text = db_type
        ET.SubElement(nexim, "Name").text = nexim_dbname
        ET.SubElement(nexim, "SuperUser").text = nexim_db_superusername
        ET.SubElement(nexim, "Password").text = nexim_db_superuserpwd
        
        fujidb = ET.SubElement(dbs, "FujiDB")
        ET.SubElement(fujidb, "Name").text = fuji_dbname
        ET.SubElement(fujidb, "AdminUser").text = fuji_dbadminname
        ET.SubElement(fujidb, "AdminPassword").text = fuji_dbadminpwd
        ET.SubElement(fujidb, "AppUser").text = fuji_dbusername
        ET.SubElement(fujidb, "AppPassword").text = fuji_dbuserpwd
        
        # Profiler
        profiler = ET.SubElement(root, "Profiler")
        if profilername:
            ET.SubElement(profiler, "Name").text = profilername
            ET.SubElement(profiler, "AdminUser").text = profiler_adminname
            ET.SubElement(profiler, "AdminPassword").text = profiler_adminpwd
        
        # Save to file with correct name
        filename = CONFIG_DIR / f"{line_name}_config.xml"
        tree = ET.ElementTree(root)
        tree.write(filename, encoding="utf-8", xml_declaration=True)
        
        return RedirectResponse(url="/lines", status_code=303)
    
    except Exception as e:
        logger.error(f"Error updating config: {str(e)}")
        raise HTTPException(status_code=500, detail="Error updating configuration")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login")
    else :
        username = request.session.get("username", "Guest")
        return f"""
        <html>
            <head>
                <title>Fuji Machine Interface</title>
                <script src="https://cdn.tailwindcss.com"></script>
                <style>
                    /* Custom Scrollbar Styling */
                    #log-container {{
                        scrollbar-width: thin;
                        scrollbar-color: #4a5568 #2d3748;
                    }}

                    #log-container::-webkit-scrollbar {{
                        width: 8px;
                    }}

                    #log-container::-webkit-scrollbar-track {{
                        background: #2d3748;
                        border-radius: 4px;
                    }}

                    #log-container::-webkit-scrollbar-thumb {{
                        background-color: #4a5568;
                        border-radius: 4px;
                        border: 2px solid #2d3748;
                    }}

                    #log-container::-webkit-scrollbar-thumb:hover {{
                        background-color: #718096;
                    }}

                    .message-received {{ border-left-color: #3b82f6; }}
                    .message-sent {{ border-left-color: #10b981; }}
                    
                    @keyframes slideIn {{
                        from {{ transform: translateY(20px); opacity: 0; }}
                        to {{ transform: translateY(0); opacity: 1; }}
                    }}
                </style>
            </head>
            <body class="bg-gray-900 text-gray-100 min-h-screen">
                <!-- Navigation Bar -->
                <nav class="bg-gray-800 p-4 flex justify-between items-center">
                    <div class="flex items-center space-x-4">
                        <h1 class="text-xl font-bold text-indigo-400">FUJI NXT MES Monitor</h1>
                        <span class="text-gray-400">|</span>
                        <span class="text-gray-300">Logged in as: {username}</span>
                    </div>
                    <div class="flex items-center space-x-4">
                        <a href="/add-line" class="bg-indigo-600 hover:bg-indigo-700 px-4 py-2 rounded-lg text-sm font-semibold">
                            Add Line
                        </a>
                        <a href="/lines" class="bg-indigo-600 hover:bg-indigo-700 px-4 py-2 rounded-lg text-sm font-semibold">
                            Edit Line
                        </a>
                        <a href="/logout" class="bg-red-600 hover:bg-red-700 px-4 py-2 rounded-lg text-sm font-semibold">
                            Logout
                        </a>
                    </div>
                </nav>
                <div class="container mx-auto px-4 py-8">
                    <div class="text-center mb-8">
                        <h1 class="text-3xl font-bold text-indigo-400 mb-2">FUJI NXT MES Monitor</h1>
                        <p class="text-gray-400">Real-time machine communication</p>
                        <div class="mt-4 flex justify-center items-center space-x-4">
                            <span id="connection-status" class="text-sm text-green-500">
                                {hostname} | {fuji_instance.HOSTNAME if fuji_instance else 'N/A'}
                            </span>
                        </div>
                    </div>
                    
                    <div class="status-indicators space-y-4 mb-8">
                        <div class="bg-gray-800 p-4 rounded-lg flex items-center justify-between">
                            <div class="flex items-center space-x-3">
                                <span class="relative flex h-3 w-3">
                                    <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-purple-400 opacity-75"></span>
                                    <span class="relative inline-flex rounded-full h-3 w-3 bg-purple-500"></span>
                                </span>
                                <span class="text-sm">
                                    Server: <span class="font-mono text-purple-400">{socket.gethostname()}</span>
                                </span>
                            </div>
                            <span class="text-xs text-gray-400">
                                {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
                            </span>
                        </div>

                        <div class="bg-gray-800 p-4 rounded-lg flex items-center justify-between" id="fuji-connection">
                            <div class="flex items-center space-x-3">
                                <span class="relative flex h-3 w-3">
                                    <span id="fuji-ping" class="absolute inline-flex h-full w-full rounded-full bg-red-500"></span>
                                </span>
                                <span class="text-sm">
                                    Machine: 
                                    <span class="font-mono" id="fuji-status">
                                        {fuji_instance.HOST if fuji_instance else 'N/A'}:{fuji_instance.PORT if fuji_instance else 'N/A'}
                                    </span>
                                </span>
                            </div>
                            <span class="text-xs text-gray-400" id="fuji-status-text">
                                {fuji_instance.connected if fuji_instance else False}
                            </span>
                        </div>
                    </div>

                    <div class="bg-gray-800 rounded-lg shadow-xl p-6">
                        <div class="flex justify-between items-center mb-4">
                            <h2 class="text-xl font-semibold">Communication Log</h2>
                            <div class="flex items-center space-x-4">
                                <span id="message-count" class="bg-gray-700 px-3 py-1 rounded-full text-sm">0 messages</span>
                                <button onclick="clearLog()" class="bg-red-500 hover:bg-red-600 text-white px-3 py-1 rounded-full text-sm">
                                    Clear
                                </button>
                            </div>
                        </div>
                        <div id="log-container" class="h-[600px] overflow-y-auto pr-4">
                            <!-- Messages inserted here -->
                        </div>
                    </div>
                </div>

                <script>
                    const ws = new WebSocket('ws://' + window.location.host + '/ws');
                    const logContainer = document.getElementById('log-container');
                    const maxMessages = 200;
                    let autoScroll = true;

                    function createMessageElement(msg) {{
                        const div = document.createElement('div');
                        div.className = `log-item p-4 rounded-lg bg-gray-700 border-l-4 mb-2 ${{ 
                            msg.direction === 'received' ? 'message-received' : 'message-sent' 
                        }} animate-slideIn`;
                        
                        div.innerHTML = `
                            <div class="flex justify-between items-start mb-1">
                                <div class="flex items-center space-x-3">
                                    <span class="font-mono text-xs text-indigo-400">${{msg.timestamp}}</span>
                                    <span class="px-2 py-1 text-xs font-bold uppercase ${{ 
                                        msg.direction === 'received' ? 'bg-blue-900 text-blue-300' : 'bg-green-900 text-green-300' 
                                    }} rounded">${{msg.direction}}</span>
                                    <span class="text-xs text-gray-400">${{msg.machine || ''}}</span>
                                </div>
                            </div>
                            <div class="font-mono text-sm text-gray-200 break-all">${{msg.raw}}</div>
                        `;
                        
                        // Preserve scroll position when adding new messages
                        div.style.overflowAnchor = "none";
                        return div;
                    }}

                    function updateConnectionStatus(data) {{
                        const statusElement = document.getElementById('fuji-status-text');
                        const pingElement = document.getElementById('fuji-ping');
                        
                        if(data.fuji_status) {{
                            statusElement.textContent = data.fuji_status.connected ? 'Connected' : 'Disconnected';
                            statusElement.className = data.fuji_status.connected ? 
                                'text-xs text-green-500' : 'text-xs text-red-500';
                            pingElement.className = data.fuji_status.connected ? 
                                'absolute inline-flex h-full w-full rounded-full bg-green-500 animate-ping' :
                                'absolute inline-flex h-full w-full rounded-full bg-red-500';
                        }}
                    }}

                    function clearLog() {{
                        logContainer.innerHTML = '';
                        document.getElementById('message-count').textContent = '0 messages';
                    }}

                    function handleNewMessages(messages) {{
                        const fragment = document.createDocumentFragment();
                        
                        messages.reverse().forEach(msg => {{
                            const element = createMessageElement(msg);
                            fragment.prepend(element);
                        }});

                        logContainer.prepend(fragment);

                        // Remove excess messages
                        while(logContainer.children.length > maxMessages) {{
                            logContainer.lastChild.remove();
                        }}

                        // Update counter
                        document.getElementById('message-count').textContent = 
                            `${{logContainer.children.length}} messages`;

                        // Auto-scroll if enabled
                        if(autoScroll && messages.length > 0) {{
                            logContainer.scrollTo({{
                                top: 0,
                                behavior: 'auto'
                            }});
                        }}
                    }}

                    ws.onmessage = (event) => {{
                        const data = JSON.parse(event.data);
                        updateConnectionStatus(data);

                        if(data.message_log) {{
                            logContainer.innerHTML = '';
                            handleNewMessages(data.message_log);
                        }}

                        if(data.new_messages) {{
                            handleNewMessages(data.new_messages);
                        }}
                    }};

                    // Track scroll position for auto-scroll management
                    logContainer.addEventListener('scroll', () => {{
                        autoScroll = logContainer.scrollTop === 0;
                    }});

                    ws.onclose = () => {{
                        document.getElementById('fuji-status-text').textContent = 'Disconnected';
                        document.getElementById('fuji-status-text').className = 'text-xs text-red-500';
                    }};
                </script>
            </body>
        </html>
        """
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    favicon_path = os.path.join(os.getcwd(), "static", "favicon.ico")
    if not os.path.exists(favicon_path):
        return {"error": "favicon.ico not found"}
    return FileResponse(favicon_path)
