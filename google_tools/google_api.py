#This is the real backend connection with real Google Account connection if the toggle is enabled
#Do not enable USE_REAL_CALENDER, USE_REAL_GOOGLE if all tests do not pass 

#It logs in through google_auth.py, ensure your connection with the assigned gmail is correct 
#Each placeholder will raise a NotImplementedError to ensure no confusion with the working code 


#Importing packages
from __future__ import annotations 

#Checker to say that real connection is not built yet, it will flag 

def not_built(name:str):
    raise NotImplementedError(
          f"google_api.{name} isn't built yet (step 8). "
        "Unset USE_REAL_GOOGLE to use the fake calendar.")
    

#Six functions for the calendar which will include: getting event, listing event, inserting event, updating event, deleting event 

#Getting event
def get_event(event_id:str) -> dict | None:
    not_built("get_event")

#Listing event 
def list_events(start:str, end:str) -> list[dict]:
    not_built("list_events")

#Inserting events
def insert_event(event:dict) -> dict:
    not_built("insert_event")

#Updating events
def update_event(event_id:str, changes:dict) -> dict:
    not_built("update_event")

#Deleting event - to delete only need id and when reverted returns same id 
def delete_event(event_id: str) -> None:
    not_built("delete_event")

#Restoring event - bringing cancelled event back with same id 
def restore_event(event_id:str)-> dict | None:
    not_built("restore_event")

#One function for email connection - only sending email , reversal of email still not done 
def send_email(to: list[str], subject: str, body: str, reply_to: dict | None = None) -> dict:
    not_built("send_email")



