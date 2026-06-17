# ==================== config.py ====================
from flask import Flask, request, jsonify
import requests
from datetime import datetime, timedelta
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
import time
import secrets
import hashlib
import hmac
import base64
import random
from typing import Dict, Optional, Tuple, Any, List
from dataclasses import dataclass, asdict
from functools import lru_cache, wraps
from threading import Lock
from collections import defaultdict
import re

# ==================== تنظیمات اولیه ====================
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8624726972:AAHa89X4pWrLaD7c-GI3OUjmx7FuSL-5pQQ')
JWT_SECRET = os.environ.get('JWT_SECRET', secrets.token_hex(32))
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')
TALISMAN_PASSWORD = "13640624"
TALISMAN_SALT = b'occult_v5_talisman_salt_2024'
