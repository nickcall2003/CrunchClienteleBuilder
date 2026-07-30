import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Railway provides DATABASE_URL for its Postgres plugin. Fall back to local SQLite.
url = os.environ.get("DATABASE_URL", "").strip()
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql+psycopg://", 1)
elif url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)
if not url:
    os.makedirs("/data", exist_ok=True) if os.path.isdir("/data") or os.access("/", os.W_OK) else None
    dbfile = "/data/trainercrm.db" if os.path.isdir("/data") else "trainercrm.db"
    url = f"sqlite:///{dbfile}"

connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()
