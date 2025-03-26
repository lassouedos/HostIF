import datetime
from pathlib import Path
def a ():
    CONFIG_DIR = Path("config")
    return CONFIG_DIR.mkdir(exist_ok=True)
a()
if a() : 
    print("Directory created")
else :  
    print("Directory already exists")