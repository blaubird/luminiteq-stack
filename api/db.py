import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base  # Абсолютный импорт

# Если не задано, используем sqlite в файл local.db рядом с этим скриптом
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./local.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # для SQLite
)
SessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=engine
)

def init_db():
    # Создаёт local.db рядом с кодом, если его ещё нет
    Base.metadata.create_all(bind=engine)
