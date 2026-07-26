from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "Steno Desk Backend"}

@app.get("/api/health")
def health_check():
    return {"status": "healthy"}
