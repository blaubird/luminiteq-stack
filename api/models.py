from sqlalchemy import (
    Column, String, Text, DateTime, Integer, Enum, ForeignKey
)
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class Tenant(Base):
    __tablename__ = "tenants"
    id            = Column(String, primary_key=True, index=True)
    phone_id      = Column(String, unique=True, nullable=False)
    wh_token      = Column(Text, nullable=False)
    system_prompt = Column(Text, default="You are a helpful assistant.")

class Message(Base):
    __tablename__ = "messages"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id  = Column(String, ForeignKey("tenants.id"), index=True)
    wa_msg_id  = Column(String, unique=True)
    role       = Column(Enum("user", "assistant", name="role_enum"))
    text       = Column(Text)
    ts         = Column(DateTime, default=datetime.utcnow)
