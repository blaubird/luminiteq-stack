import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base

# Путь к БД: sqlite в файл api/local.db, или задаётся через env
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./api/local.db")

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=engine
)

def init_db():
    # создаём все таблицы, если их ещё нет
    Base.metadata.create_all(bind=engine)
