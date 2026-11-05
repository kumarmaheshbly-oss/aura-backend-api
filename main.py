import os
from fastapi import FastAPI, Depends
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, JSON
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from pydantic import BaseModel
from typing import Dict, Any, List
from datetime import datetime

# --- DATABASE SETUP ---
SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL")

if SQLALCHEMY_DATABASE_URL and SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace("postgres://", "postgresql://", 1)

if not SQLALCHEMY_DATABASE_URL:
    SQLALCHEMY_DATABASE_URL = "sqlite:///./aura.db"

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- DATABASE MODELS ---
class ConjunctionReport(Base):
    __tablename__ = "conjunction_reports"
    id = Column(Integer, primary_key=True, index=True)
    primary_object = Column(String, index=True)
    secondary_object = Column(String, index=True)
    tca = Column(String)
    miss_distance_m = Column(Float)
    relative_speed_km_s = Column(Float)
    pc_foster_exact = Column(Float)
    risk = Column(String, index=True)
    rtn_data = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# --- PYDANTIC SCHEMAS ---
class ReportCreate(BaseModel):
    primary_object: str
    secondary_object: str
    tca: str
    miss_distance_m: float
    relative_speed_km_s: float
    pc_foster_exact: float
    risk: str
    rtn_data: Dict[str, Any]

class ReportResponse(ReportCreate):
    id: int
    created_at: datetime
    class Config:
        from_attributes = True

# --- FASTAPI APP ---
app = FastAPI(title="Project Aura Cloud API")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- ENDPOINTS ---
@app.get("/")
def read_root():
    return {"status": "online", "message": "Project Aura Cloud API is running"}

@app.delete("/api/reports/clear")
def clear_database(db: Session = Depends(get_db)):
    db.query(ConjunctionReport).delete()
    db.commit()
    return {"message": "Database cleared successfully"}

@app.get("/api/reports/", response_model=List[ReportResponse])
def get_reports(risk_level: str = None, skip: int = 0, limit: int = 10, db: Session = Depends(get_db)):
    query = db.query(ConjunctionReport)
    if risk_level:
        query = query.filter(ConjunctionReport.risk == risk_level)
    return query.order_by(ConjunctionReport.pc_foster_exact.desc()).offset(skip).limit(limit).all()

@app.post("/api/reports/", response_model=ReportResponse)
def create_report(report: ReportCreate, db: Session = Depends(get_db)):
    db_report = ConjunctionReport(**report.dict())
    db.add(db_report)
    db.commit()
    db.refresh(db_report)
    return db_report
