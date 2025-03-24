from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse,FileResponse
from contextlib import asynccontextmanager
from test import FujiHostInterface,backend_logger
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
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

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
                initial_data['message_log'] = all_messages[-100:][::-1]
        
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

@app.get("/", response_class=HTMLResponse)
def read_root():
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
