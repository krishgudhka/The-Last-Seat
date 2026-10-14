from fastapi import FastAPI

app = FastAPI(title="The Last Seat")


@app.get("/")
def home():
    return {"message": "Last Seat API is running"}