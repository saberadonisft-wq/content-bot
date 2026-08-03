from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _connect_args() -> dict:
    return {"check_same_thread": False} if settings.content_bot_database_url.startswith("sqlite") else {}


engine = create_engine(settings.content_bot_database_url, connect_args=_connect_args(), future=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


def get_session():
    session: Session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

