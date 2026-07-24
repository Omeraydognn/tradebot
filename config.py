import os
from dotenv import load_dotenv

load_dotenv()

# Polymarket
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
POLYMARKET_ADDRESS = os.getenv("POLYMARKET_ADDRESS")

# Binance
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

# AI (Gemini)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Paper Trading
INITIAL_BALANCE = float(os.getenv("INITIAL_BALANCE", "1000.0"))  # $1000 starting balance per strategy
POLYMARKET_FEE_RATE = 0.0156  # ~1.56% taker fee at 50/50

# Dashboard
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5050"))
