#This will be the backend for the entire application 
#Here this will determine if we are switching from the fake calendar or real Google calendar as we progress

#GUIDE 
# This placeholder USE_REAL_GOOGLE decides whether the connection will be to the real Google calender or fake calender 
# USE_REAL_CALENDER -> Not set, it will access fake data (practice calender present in JSON file)
# USE_REAL_CALENDER == 1, it will call the google_api 

#NOTE: Do not call the google_api before your code is secure and running accurately, only after the connection is seamless then connect to real Google 

#CALENDER AND ITS FIELDS    
#The features calender will have:
# id, summary,start, end, attendees, responses, raw
# cancelled events: returned with "status": "cancelled"
# list_events(start,end) -> Just sorts all events in time window, not cancelled 
# insert_event(event) -> It created event with new id 
# update_event(event_id, changes) -> updated event, "status":"confirmed", uncancels it
# delete_event(event_id) -> "status":"cancelled", can be restored with same id 

#EMAIL AND ITS FIELDS 
#send_email(to, subject,body) -> It will send message with "id"


#Importing packages 
from __future__ import annotations 
import importlib 
import os 
from types import ModuleType 

#Two keys, fake and real (Always use fake till your code is secure)
FAKE = "fake_data.fake_google"
REAL = "google_tools.google_api"

REQUIRED = ("get_event", "list_events", "insert_event", "update_event", "delete_event", "restore_event", "send_email")

#Three functions 
#1) Using real google connection, toggles and returns boolean 

def using_real_google()->bool:
    return os.environ.get("USE_REAL_GOOGLE", "").strip().lower() in {"1", "true", "yes"}

#2) Prints the dashboard (if fake or real) to avoid confusion
def backend_name() -> str:
    return "real Google" if using_real_google() else "fake calendar"

#3) The current backend funtion, reads the google connection status on every call and its tests switch 
def get_backend()->ModuleType:
    module = importlib.import_module(REAL if using_real_google() else FAKE)
    missing = [name for name in REQUIRED if not callable(getattr(module, name, None))]

    if missing:
        raise RuntimeError(f"{module.__name__} is missing: {','.join(missing)}")
    return module
    


