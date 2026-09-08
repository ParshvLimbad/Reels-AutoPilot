from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
import json
import config
from datetime import datetime

# Create the database engine
engine = create_engine('sqlite:///'+config.DB_PATH)

# Create a session factory
Session = sessionmaker(bind=engine)

# Create a base class for declarative models
Base = declarative_base()

# Add `checkfirst=True` to only create the table if it doesn't exist
Base.metadata.create_all(engine, checkfirst=True)

# Define a Reels model
class Reel(Base):
    __tablename__ = 'reels'

    id = Column(Integer, primary_key=True)
    post_id = Column(String)
    code = Column(String)
    account = Column(String)
    file_name = Column(String)
    file_path = Column(String)
    caption = Column(String)
    data = Column(String)
    is_posted = Column(Boolean)
    posted_at = Column(DateTime)

class Config(Base):
    __tablename__ = 'config'

    id = Column(Integer, primary_key=True)
    key = Column(String)
    value = Column(String)
    created_at = Column(DateTime)
    updated_at = Column(DateTime)

# Create the database schema
Base.metadata.create_all(engine)


class ReelEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, 'dict'):
            return obj.dict()
        elif hasattr(obj, 'model_dump'):
            return obj.model_dump()
        elif hasattr(obj, 'isoformat'):
            return obj.isoformat()
        try:
            return {
                'id': str(getattr(obj, 'id', '')),
                'pk': str(getattr(obj, 'pk', '')),
                'code': str(getattr(obj, 'code', '')),
                'caption_text': str(getattr(obj, 'caption_text', '')),
                'video_url': str(getattr(obj, 'video_url', ''))
            }
        except Exception:
            return str(obj)

