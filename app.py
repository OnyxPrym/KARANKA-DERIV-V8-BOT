#!/usr/bin/env python3
"""
================================================================================
🚀 KARANKA V8 - REAL DERIV TRADING BOT
================================================================================
• REAL TRADES ON DERIV ACCOUNT (DEMO OR REAL)
• LIVE MARKET DATA & EXECUTION
• 24/7 AUTO TRADING
• USER-CONTROLLED SETTINGS
================================================================================
"""

import os
import json
import time
import threading
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Any
from collections import defaultdict, deque
from functools import wraps
import hashlib
import secrets
import statistics
import requests
import websocket
import uuid

# Flask imports
from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for
from flask_cors import CORS

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('DerivTradingBot')

# ============ FLASK APP INITIALIZATION ============
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
CORS(app)

# ============ CONFIGURATION ============
class Config:
    MAX_CONCURRENT_TRADES = 5
    MAX_DAILY_TRADES = 200
    MIN_TRADE_AMOUNT = 1.0
    MAX_TRADE_AMOUNT = 50.0
    DEFAULT_TRADE_AMOUNT = 2.0
    SCAN_INTERVAL = 30
    SESSION_TIMEOUT = 86400
    DATABASE_FILE = 'data/users.json'
    LOG_FILE = 'data/trades.json'
    AVAILABLE_MARKETS = [
        'R_10', 'R_25', 'R_50', 'R_75', 'R_100',
        '1HZ100V', '1HZ150V', '1HZ200V',
        'frxEURUSD', 'frxGBPUSD', 'frxUSDJPY',
        'cryBTCUSD', 'cryETHUSD'
    ]
    DERIV_APP_ID = 1089
    DERIV_ENDPOINTS = [
        "wss://ws.derivws.com/websockets/v3",
        "wss://ws.binaryws.com/websockets/v3",
        "wss://ws.deriv.com/websockets/v3"
    ]
    
config = Config()

# Create data directory
os.makedirs('data', exist_ok=True)

# ============ DERIV API CLIENT ============
class DerivAPIClient:
    """Handles connection and trading with Deriv API"""
    
    def __init__(self):
        self.ws = None
        self.connected = False
        self.token = None
        self.account_id = None
        self.balance = 0.0
        self.account_type = None  # 'real' or 'demo'
        self.reconnect_attempts = 0
        self.max_reconnect = 5
        self.ping_interval = 30
        self.last_ping = time.time()
        self.connection_lock = threading.Lock()
        self.active_contracts = []
        
    def connect(self, api_token: str) -> Tuple[bool, str, float]:
        """Connect to Deriv API with token"""
        try:
            self.token = api_token
            
            for endpoint in config.DERIV_ENDPOINTS:
                try:
                    url = f"{endpoint}?app_id={config.DERIV_APP_ID}"
                    logger.info(f"Connecting to {url}")
                    
                    self.ws = websocket.create_connection(
                        url,
                        timeout=10,
                        header={
                            'User-Agent': 'Mozilla/5.0',
                            'Origin': 'https://app.deriv.com'
                        }
                    )
                    
                    # Authenticate
                    auth_msg = {
                        "authorize": api_token
                    }
                    self.ws.send(json.dumps(auth_msg))
                    
                    response = self.ws.recv()
                    data = json.loads(response)
                    
                    if "error" in data:
                        error_msg = data["error"].get("message", "Authentication failed")
                        logger.error(f"Auth failed: {error_msg}")
                        continue
                    
                    if "authorize" in data:
                        auth_data = data["authorize"]
                        self.connected = True
                        self.account_id = auth_data.get("loginid", "")
                        self.account_type = "real" if "VRTC" in self.account_id else "demo"
                        self.reconnect_attempts = 0
                        
                        # Get account balance
                        self.balance = self.get_balance()
                        
                        logger.info(f"✅ Connected to Deriv {self.account_type} account: {self.account_id}")
                        logger.info(f"💰 Balance: ${self.balance:.2f}")
                        
                        # Start ping thread
                        threading.Thread(target=self._keep_alive, daemon=True).start()
                        
                        return True, f"Connected to {self.account_type} account", self.balance
                        
                except Exception as e:
                    logger.warning(f"Endpoint {endpoint} failed: {str(e)}")
                    continue
            
            return False, "Failed to connect to all endpoints", 0.0
            
        except Exception as e:
            logger.error(f"Connection error: {str(e)}")
            return False, str(e), 0.0
    
    def get_balance(self) -> float:
        """Get current account balance"""
        try:
            if not self.connected:
                return 0.0
            
            with self.connection_lock:
                self.ws.send(json.dumps({"balance": 1, "subscribe": 1}))
                response = self.ws.recv()
                data = json.loads(response)
                
                if "balance" in data:
                    self.balance = float(data["balance"]["balance"])
                    return self.balance
            
            return 0.0
            
        except Exception as e:
            logger.error(f"Balance error: {str(e)}")
            return 0.0
    
    def get_market_data(self, symbol: str) -> Optional[Dict]:
        """Get current market data for symbol"""
        try:
            if not self.connected:
                return None
            
            with self.connection_lock:
                self.ws.send(json.dumps({
                    "ticks": symbol,
                    "subscribe": 1
                }))
                response = self.ws.recv()
                data = json.loads(response)
                
                if "error" in data:
                    return None
                
                if "tick" in data:
                    tick = data["tick"]
                    return {
                        "symbol": symbol,
                        "bid": float(tick["bid"]),
                        "ask": float(tick["ask"]),
                        "quote": float(tick["quote"]),
                        "epoch": tick["epoch"]
                    }
            
            return None
            
        except Exception as e:
            logger.error(f"Market data error: {str(e)}")
            return None
    
    def place_trade(self, symbol: str, contract_type: str, amount: float, 
                   duration: int = 5, duration_unit: str = 't') -> Tuple[bool, Dict]:
        """Place a trade on Deriv"""
        try:
            if not self.connected:
                return False, {"error": "Not connected to Deriv"}
            
            if amount < config.MIN_TRADE_AMOUNT:
                amount = config.MIN_TRADE_AMOUNT
            
            if amount > self.balance * 0.9:
                return False, {"error": f"Insufficient balance. Available: ${self.balance:.2f}"}
            
            # Prepare trade request
            trade_request = {
                "buy": 1,
                "price": amount,
                "parameters": {
                    "amount": amount,
                    "basis": "stake",
                    "contract_type": contract_type.upper(),
                    "currency": "USD",
                    "duration": duration,
                    "duration_unit": duration_unit,
                    "symbol": symbol,
                    "product_type": "basic"
                }
            }
            
            logger.info(f"Placing trade: {symbol} {contract_type} ${amount}")
            
            with self.connection_lock:
                self.ws.send(json.dumps(trade_request))
                response = self.ws.recv()
                data = json.loads(response)
                
                if "error" in data:
                    error_msg = data["error"].get("message", "Trade failed")
                    return False, {"error": error_msg}
                
                if "buy" in data:
                    buy_data = data["buy"]
                    contract_id = buy_data["contract_id"]
                    payout = float(buy_data.get("payout", amount * 1.82))
                    
                    # Update balance
                    self.balance = self.get_balance()
                    
                    trade_result = {
                        "success": True,
                        "contract_id": contract_id,
                        "payout": payout,
                        "profit": payout - amount,
                        "balance": self.balance,
                        "transaction_id": buy_data.get("transaction_id", ""),
                        "timestamp": datetime.now().isoformat()
                    }
                    
                    # Store active contract
                    self.active_contracts.append({
                        "contract_id": contract_id,
                        "symbol": symbol,
                        "contract_type": contract_type,
                        "amount": amount,
                        "purchase_time": datetime.now().isoformat(),
                        "duration": duration,
                        "duration_unit": duration_unit
                    })
                    
                    logger.info(f"✅ Trade successful: {contract_id}")
                    return True, trade_result
            
            return False, {"error": "Unknown response from Deriv"}
            
        except Exception as e:
            logger.error(f"Trade error: {str(e)}")
            return False, {"error": str(e)}
    
    def check_contract(self, contract_id: str) -> Optional[Dict]:
        """Check contract status"""
        try:
            if not self.connected:
                return None
            
            with self.connection_lock:
                self.ws.send(json.dumps({
                    "proposal_open_contract": 1,
                    "contract_id": contract_id,
                    "subscribe": 1
                }))
                response = self.ws.recv()
                data = json.loads(response)
                
                if "proposal_open_contract" in data:
                    return data["proposal_open_contract"]
            
            return None
            
        except Exception as e:
            logger.error(f"Contract check error: {str(e)}")
            return None
    
    def _keep_alive(self):
        """Keep WebSocket connection alive"""
        while self.connected:
            try:
                time.sleep(self.ping_interval)
                if self.ws:
                    self.ws.ping()
                    self.last_ping = time.time()
            except:
                self.connected = False
                logger.warning("Connection lost")
                break
    
    def disconnect(self):
        """Disconnect from Deriv"""
        try:
            if self.ws:
                self.ws.close()
            self.connected = False
            self.token = None
            logger.info("Disconnected from Deriv")
        except:
            pass

# ============ USER MANAGEMENT ============
class UserManager:
    def __init__(self):
        self.users_file = config.DATABASE_FILE
        self.users = self._load_users()
        self.sessions = {}
    
    def _load_users(self) -> Dict:
        try:
            if os.path.exists(self.users_file):
                with open(self.users_file, 'r') as f:
                    return json.load(f)
        except:
            pass
        return {}
    
    def _save_users(self):
        try:
            with open(self.users_file, 'w') as f:
                json.dump(self.users, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving users: {e}")
    
    def register_user(self, username: str, password: str, email: str = "") -> Tuple[bool, str]:
        if username in self.users:
            return False, "Username already exists"
        
        if len(password) < 6:
            return False, "Password must be at least 6 characters"
        
        salt = secrets.token_hex(16)
        hashed_password = hashlib.sha256((password + salt).encode()).hexdigest()
        
        self.users[username] = {
            'password_hash': hashed_password,
            'salt': salt,
            'email': email,
            'created_at': datetime.now().isoformat(),
            'last_login': None,
            'deriv_token': None,
            'settings': {
                'trade_amount': 2.0,
                'max_concurrent_trades': 3,
                'max_daily_trades': 50,
                'risk_level': 'medium',
                'auto_trading': True,
                'enabled_markets': ['R_10', 'R_25', 'R_50'],
                'min_confidence': 65,
                'stop_loss': 10.0,
                'take_profit': 15.0,
                'preferred_duration': 5,
                'duration_unit': 't',
                'scan_interval': 30,
                'cooldown_seconds': 30
            },
            'trading_stats': {
                'total_trades': 0,
                'successful_trades': 0,
                'failed_trades': 0,
                'total_profit': 0.0,
                'current_balance': 0.0,
                'daily_trades': 0,
                'daily_profit': 0.0
            }
        }
        
        self._save_users()
        return True, "Registration successful"
    
    def authenticate_user(self, username: str, password: str) -> Tuple[bool, str, Dict]:
        if username not in self.users:
            return False, "Invalid credentials", {}
        
        user = self.users[username]
        hashed_password = hashlib.sha256((password + user['salt']).encode()).hexdigest()
        
        if hashed_password != user['password_hash']:
            return False, "Invalid credentials", {}
        
        token = secrets.token_hex(32)
        user['last_login'] = datetime.now().isoformat()
        
        self.sessions[token] = {
            'username': username,
            'created_at': datetime.now().isoformat()
        }
        
        self._save_users()
        return True, "Login successful", {
            'token': token,
            'username': username,
            'settings': user['settings'],
            'stats': user['trading_stats'],
            'deriv_token': user.get('deriv_token')
        }
    
    def validate_token(self, token: str) -> Tuple[bool, str]:
        if token not in self.sessions:
            return False, ""
        
        session_data = self.sessions[token]
        last_activity = datetime.fromisoformat(session_data['created_at'])
        
        if (datetime.now() - last_activity).seconds > config.SESSION_TIMEOUT:
            del self.sessions[token]
            return False, ""
        
        return True, session_data['username']
    
    def get_user(self, username: str) -> Optional[Dict]:
        return self.users.get(username)
    
    def update_user_settings(self, username: str, settings: Dict) -> bool:
        if username not in self.users:
            return False
        
        self.users[username]['settings'].update(settings)
        self._save_users()
        return True
    
    def update_user_stats(self, username: str, stats_update: Dict) -> bool:
        if username not in self.users:
            return False
        
        for key, value in stats_update.items():
            if key in self.users[username]['trading_stats']:
                self.users[username]['trading_stats'][key] += value
        
        self._save_users()
        return True
    
    def save_deriv_token(self, username: str, token: str) -> bool:
        if username not in self.users:
            return False
        
        self.users[username]['deriv_token'] = token
        self._save_users()
        return True

user_manager = UserManager()

# ============ TRADING ENGINE ============
class TradingEngine:
    def __init__(self, username: str):
        self.username = username
        self.client = DerivAPIClient()
        self.running = False
        self.trading_thread = None
        self.active_trades = []
        self.trade_history = []
        self.last_trade_time = defaultdict(float)
        self.market_data = {}
        
        # Load user settings
        user_data = user_manager.get_user(username)
        self.settings = user_data['settings'] if user_data else {
            'trade_amount': 2.0,
            'max_concurrent_trades': 3,
            'max_daily_trades': 50,
            'auto_trading': True,
            'enabled_markets': ['R_10', 'R_25', 'R_50'],
            'min_confidence': 65,
            'stop_loss': 10.0,
            'scan_interval': 30,
            'cooldown_seconds': 30
        }
        
        # Performance tracking
        self.performance = {
            'total_trades': 0,
            'profitable_trades': 0,
            'total_profit': 0.0,
            'win_rate': 0.0,
            'daily_trades': 0,
            'daily_profit': 0.0,
            'consecutive_wins': 0,
            'consecutive_losses': 0
        }
        
        logger.info(f"Trading Engine created for {username}")
    
    def connect_to_deriv(self, api_token: str) -> Tuple[bool, str, float]:
        """Connect to Deriv account"""
        return self.client.connect(api_token)
    
    def start_trading(self):
        """Start auto trading"""
        if self.running:
            return False, "Already running"
        
        # Check if connected to Deriv
        if not self.client.connected:
            return False, "Not connected to Deriv. Please connect first."
        
        self.running = True
        self.trading_thread = threading.Thread(target=self._trading_loop, daemon=True)
        self.trading_thread.start()
        
        logger.info(f"Auto trading started for {self.username}")
        return True, "Auto trading started"
    
    def stop_trading(self):
        """Stop auto trading"""
        self.running = False
        logger.info(f"Auto trading stopped for {self.username}")
        return True, "Auto trading stopped"
    
    def _trading_loop(self):
        """Main trading loop"""
        while self.running:
            try:
                # Check if auto trading is enabled
                if not self.settings['auto_trading']:
                    time.sleep(5)
                    continue
                
                # Check if still connected
                if not self.client.connected:
                    logger.warning(f"Lost connection for {self.username}")
                    time.sleep(10)
                    continue
                
                # Check daily trade limit
                if self.performance['daily_trades'] >= self.settings['max_daily_trades']:
                    logger.info(f"Daily trade limit reached for {self.username}")
                    time.sleep(60)
                    continue
                
                # Check stop loss
                if self.performance['total_profit'] <= -self.settings['stop_loss']:
                    logger.warning(f"Stop loss hit for {self.username}")
                    time.sleep(60)
                    continue
                
                # Trade on each enabled market
                for symbol in self.settings['enabled_markets']:
                    if not self.running:
                        break
                    
                    # Check concurrent trades limit
                    active_count = len(self.active_trades)
                    if active_count >= self.settings['max_concurrent_trades']:
                        break
                    
                    # Check cooldown
                    current_time = time.time()
                    if symbol in self.last_trade_time:
                        time_since = current_time - self.last_trade_time[symbol]
                        if time_since < self.settings['cooldown_seconds']:
                            continue
                    
                    # Get market data
                    market_data = self.client.get_market_data(symbol)
                    if not market_data:
                        continue
                    
                    # Analyze market and make decision
                    trade_decision = self._analyze_market(symbol, market_data)
                    
                    if trade_decision['should_trade']:
                        self._execute_trade(
                            symbol=symbol,
                            direction=trade_decision['direction'],
                            amount=self.settings['trade_amount']
                        )
                    
                    time.sleep(1)  # Small delay between symbols
                
                time.sleep(self.settings['scan_interval'])
                
            except Exception as e:
                logger.error(f"Trading loop error: {e}")
                time.sleep(30)
    
    def _analyze_market(self, symbol: str, market_data: Dict) -> Dict:
        """Analyze market and decide on trade"""
        try:
            bid = market_data['bid']
            
            # Simple strategy: Use RSI-like calculation
            # Store recent prices for analysis
            if symbol not in self.market_data:
                self.market_data[symbol] = deque(maxlen=20)
            
            self.market_data[symbol].append(bid)
            
            if len(self.market_data[symbol]) < 10:
                return {'should_trade': False, 'direction': None}
            
            # Calculate simple indicators
            prices = list(self.market_data[symbol])
            recent_avg = statistics.mean(prices[-5:])
            older_avg = statistics.mean(prices[-10:-5])
            
            # Determine trend
            if recent_avg > older_avg * 1.001:  # Uptrend
                direction = "CALL"
                confidence = 70
            elif recent_avg < older_avg * 0.999:  # Downtrend
                direction = "PUT"
                confidence = 70
            else:
                # Sideways market, use volatility
                volatility = max(prices[-5:]) - min(prices[-5:])
                if volatility > bid * 0.001:  # High volatility
                    direction = "CALL" if prices[-1] > prices[-2] else "PUT"
                    confidence = 60
                else:
                    return {'should_trade': False, 'direction': None}
            
            # Check confidence threshold
            if confidence >= self.settings['min_confidence']:
                return {
                    'should_trade': True,
                    'direction': direction,
                    'confidence': confidence,
                    'price': bid
                }
            
            return {'should_trade': False, 'direction': None}
            
        except Exception as e:
            logger.error(f"Analysis error for {symbol}: {e}")
            return {'should_trade': False, 'direction': None}
    
    def _execute_trade(self, symbol: str, direction: str, amount: float):
        """Execute a trade"""
        try:
            duration = self.settings.get('preferred_duration', 5)
            duration_unit = self.settings.get('duration_unit', 't')
            
            success, result = self.client.place_trade(
                symbol=symbol,
                contract_type=direction,
                amount=amount,
                duration=duration,
                duration_unit=duration_unit
            )
            
            trade_record = {
                'id': str(uuid.uuid4()),
                'username': self.username,
                'symbol': symbol,
                'direction': direction,
                'amount': amount,
                'duration': f"{duration}{duration_unit}",
                'timestamp': datetime.now().isoformat(),
                'success': success,
                'result': result
            }
            
            self.trade_history.append(trade_record)
            self.last_trade_time[symbol] = time.time()
            
            if success:
                profit = result.get('profit', 0)
                self.active_trades.append({
                    'contract_id': result.get('contract_id'),
                    'symbol': symbol,
                    'profit': profit,
                    'timestamp': datetime.now().isoformat()
                })
                
                # Update performance
                self.performance['total_trades'] += 1
                self.performance['daily_trades'] += 1
                self.performance['total_profit'] += profit
                self.performance['daily_profit'] += profit
                
                if profit > 0:
                    self.performance['profitable_trades'] += 1
                    self.performance['consecutive_wins'] += 1
                    self.performance['consecutive_losses'] = 0
                else:
                    self.performance['consecutive_losses'] += 1
                    self.performance['consecutive_wins'] = 0
                
                if self.performance['total_trades'] > 0:
                    self.performance['win_rate'] = (
                        self.performance['profitable_trades'] / self.performance['total_trades'] * 100
                    )
                
                # Update user stats
                user_manager.update_user_stats(self.username, {
                    'total_trades': 1,
                    'successful_trades': 1 if profit > 0 else 0,
                    'failed_trades': 1 if profit <= 0 else 0,
                    'total_profit': profit
                })
                
                logger.info(f"Trade executed: {symbol} {direction} ${amount} - Profit: ${profit:.2f}")
            else:
                logger.error(f"Trade failed: {result.get('error', 'Unknown error')}")
            
            # Clean old active trades
            self.active_trades = [
                t for t in self.active_trades
                if time.time() - datetime.fromisoformat(t['timestamp']).timestamp() < 300
            ]
            
        except Exception as e:
            logger.error(f"Trade execution error: {e}")
    
    def get_status(self) -> Dict:
        """Get engine status"""
        return {
            'running': self.running,
            'connected': self.client.connected,
            'balance': self.client.balance,
            'account_type': self.client.account_type,
            'account_id': self.client.account_id,
            'performance': self.performance,
            'settings': self.settings,
            'active_trades': len(self.active_trades)
        }

# ============ SESSION MANAGEMENT ============
session_manager = {}

def token_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = None
        
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Bearer '):
            token = auth_header.split(' ')[1]
        elif request.json:
            token = request.json.get('token')
        elif 'token' in session:
            token = session.get('token')
        
        if not token:
            return jsonify({'success': False, 'message': 'Token missing'}), 401
        
        valid, username = user_manager.validate_token(token)
        if not valid:
            return jsonify({'success': False, 'message': 'Invalid token'}), 401
        
        request.username = username
        request.token = token
        return f(*args, **kwargs)
    
    return decorated_function

def get_user_engine(username: str) -> TradingEngine:
    """Get or create user's trading engine"""
    if username not in session_manager:
        session_manager[username] = TradingEngine(username)
    return session_manager[username]

# ============ TRADING UI TEMPLATE ============
TRADING_UI = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🚀 Karanka V8 - Deriv Trading Bot</title>
    <style>
        :root {
            --gold: #FFD700;
            --dark-gold: #B8860B;
            --black: #000000;
            --dark-gray: #1a1a1a;
            --light-gray: #444;
            --success: #00FF00;
            --danger: #FF0000;
            --info: #00BFFF;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: Arial, sans-serif;
        }
        
        body {
            background: var(--black);
            color: white;
            min-height: 100vh;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        
        /* Header */
        .header {
            background: var(--dark-gray);
            border: 2px solid var(--gold);
            border-radius: 10px;
            padding: 20px;
            margin-bottom: 20px;
            text-align: center;
        }
        
        .header h1 {
            color: var(--gold);
            margin-bottom: 10px;
            font-size: 2em;
        }
        
        .user-info {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 15px;
            padding: 10px;
            background: rgba(0,0,0,0.5);
            border-radius: 5px;
        }
        
        /* Tabs */
        .tabs {
            display: flex;
            background: var(--dark-gray);
            border-radius: 8px;
            margin-bottom: 20px;
            overflow: hidden;
        }
        
        .tab {
            flex: 1;
            padding: 15px;
            text-align: center;
            background: transparent;
            color: #aaa;
            border: none;
            cursor: pointer;
            border-bottom: 3px solid transparent;
            font-weight: bold;
        }
        
        .tab:hover {
            background: rgba(255, 215, 0, 0.1);
        }
        
        .tab.active {
            color: var(--gold);
            border-bottom: 3px solid var(--gold);
            background: rgba(255, 215, 0, 0.15);
        }
        
        /* Panels */
        .panel {
            display: none;
            background: var(--dark-gray);
            border-radius: 10px;
            padding: 25px;
            margin-bottom: 20px;
        }
        
        .panel.active {
            display: block;
            animation: fadeIn 0.3s;
        }
        
        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }
        
        .panel h2 {
            color: var(--gold);
            margin-bottom: 20px;
            border-bottom: 1px solid var(--light-gray);
            padding-bottom: 10px;
        }
        
        /* Stats Grid */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 25px;
        }
        
        .stat-card {
            background: rgba(0, 0, 0, 0.5);
            border: 1px solid var(--light-gray);
            border-left: 4px solid var(--gold);
            border-radius: 8px;
            padding: 20px;
        }
        
        .stat-card.success {
            border-left-color: var(--success);
        }
        
        .stat-card.danger {
            border-left-color: var(--danger);
        }
        
        .stat-card.info {
            border-left-color: var(--info);
        }
        
        .stat-value {
            font-size: 2em;
            font-weight: bold;
            color: var(--gold);
            margin-bottom: 5px;
        }
        
        .stat-card.success .stat-value {
            color: var(--success);
        }
        
        .stat-card.danger .stat-value {
            color: var(--danger);
        }
        
        .stat-card.info .stat-value {
            color: var(--info);
        }
        
        .stat-label {
            color: #aaa;
            font-size: 0.9em;
        }
        
        /* Forms */
        .form-group {
            margin-bottom: 20px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 8px;
            color: #ccc;
            font-weight: bold;
        }
        
        .form-group input,
        .form-group select,
        .form-group textarea {
            width: 100%;
            padding: 12px;
            background: rgba(0, 0, 0, 0.5);
            border: 1px solid var(--light-gray);
            border-radius: 5px;
            color: white;
            font-size: 1em;
        }
        
        .form-group input:focus,
        .form-group select:focus {
            outline: none;
            border-color: var(--gold);
            box-shadow: 0 0 5px rgba(255, 215, 0, 0.3);
        }
        
        /* Buttons */
        .btn {
            padding: 12px 25px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-weight: bold;
            transition: all 0.3s;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-size: 1em;
        }
        
        .btn-primary {
            background: var(--gold);
            color: black;
        }
        
        .btn-success {
            background: var(--success);
            color: black;
        }
        
        .btn-danger {
            background: var(--danger);
            color: white;
        }
        
        .btn-info {
            background: var(--info);
            color: white;
        }
        
        .btn:hover {
            opacity: 0.9;
            transform: translateY(-2px);
        }
        
        .btn:active {
            transform: translateY(0);
        }
        
        /* Alert */
        .alert {
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
            display: none;
        }
        
        .alert.success {
            background: rgba(0, 255, 0, 0.1);
            color: #90ff90;
            border-left: 4px solid var(--success);
        }
        
        .alert.error {
            background: rgba(255, 0, 0, 0.1);
            color: #ff9090;
            border-left: 4px solid var(--danger);
        }
        
        .alert.info {
            background: rgba(0, 191, 255, 0.1);
            color: #90e0ff;
            border-left: 4px solid var(--info);
        }
        
        /* Table */
        .table-container {
            overflow-x: auto;
            margin-top: 20px;
        }
        
        .table {
            width: 100%;
            border-collapse: collapse;
            background: rgba(0, 0, 0, 0.5);
            border-radius: 5px;
            overflow: hidden;
        }
        
        .table th {
            background: rgba(255, 215, 0, 0.2);
            padding: 12px;
            text-align: left;
            color: var(--gold);
            font-weight: bold;
        }
        
        .table td {
            padding: 12px;
            border-bottom: 1px solid var(--light-gray);
            color: #ccc;
        }
        
        .table tr:hover {
            background: rgba(255, 215, 0, 0.05);
        }
        
        .table tr:last-child td {
            border-bottom: none;
        }
        
        /* Market Selection */
        .market-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
            gap: 10px;
            margin: 20px 0;
        }
        
        .market-item {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px;
            background: rgba(0,0,0,0.3);
            border: 1px solid var(--light-gray);
            border-radius: 5px;
        }
        
        .market-item input[type="checkbox"] {
            width: 18px;
            height: 18px;
            accent-color: var(--gold);
        }
        
        /* Control Group */
        .control-group {
            display: flex;
            gap: 15px;
            margin: 20px 0;
            flex-wrap: wrap;
        }
        
        /* Connection Status */
        .connection-status {
            padding: 10px 15px;
            border-radius: 5px;
            font-weight: bold;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        
        .connected {
            background: rgba(0, 255, 0, 0.1);
            color: var(--success);
        }
        
        .disconnected {
            background: rgba(255, 0, 0, 0.1);
            color: var(--danger);
        }
        
        /* Loading */
        .loader {
            border: 3px solid rgba(255, 215, 0, 0.3);
            border-top: 3px solid var(--gold);
            border-radius: 50%;
            width: 30px;
            height: 30px;
            animation: spin 1s linear infinite;
            margin: 20px auto;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        
        /* Responsive */
        @media (max-width: 768px) {
            .stats-grid {
                grid-template-columns: 1fr;
            }
            
            .tabs {
                flex-direction: column;
            }
            
            .control-group {
                flex-direction: column;
            }
            
            .btn {
                width: 100%;
                justify-content: center;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div class="header">
            <h1>🚀 Karanka V8 - Deriv Trading Bot</h1>
            <p>Real Trading • 24/7 Operation • Auto Execution</p>
            <div class="user-info">
                <div>
                    <strong>Account:</strong> {{ username }}
                    {% if engine_status.connected %}
                    <span class="connection-status connected">
                        <span class="dot" style="color: #00ff00;">●</span>
                        Connected to Deriv
                    </span>
                    {% else %}
                    <span class="connection-status disconnected">
                        <span class="dot" style="color: #ff0000;">●</span>
                        Not Connected
                    </span>
                    {% endif %}
                </div>
                <button class="btn btn-danger" onclick="logout()">
                    <span>🚪</span> Logout
                </button>
            </div>
        </div>
        
        <!-- Tabs -->
        <div class="tabs">
            <button class="tab active" onclick="showPanel('dashboard')">📊 Dashboard</button>
            <button class="tab" onclick="showPanel('connection')">🔗 Connection</button>
            <button class="tab" onclick="showPanel('trading')">🤖 Auto Trading</button>
            <button class="tab" onclick="showPanel('markets')">📈 Markets</button>
            <button class="tab" onclick="showPanel('manual')">🕹️ Manual Trade</button>
            <button class="tab" onclick="showPanel('history')">📋 History</button>
        </div>
        
        <!-- Alert -->
        <div id="alert" class="alert"></div>
        
        <!-- Dashboard -->
        <div id="dashboard" class="panel active">
            <h2>📊 Trading Dashboard</h2>
            
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-value" id="balance">
                        ${{ "%.2f"|format(engine_status.balance) if engine_status.balance else "0.00" }}
                    </div>
                    <div class="stat-label">
                        {% if engine_status.account_type %}
                        {{ engine_status.account_type|upper }} Balance
                        {% else %}Balance{% endif %}
                    </div>
                </div>
                
                <div class="stat-card info">
                    <div class="stat-value" id="totalTrades">{{ user_data.trading_stats.total_trades }}</div>
                    <div class="stat-label">Total Trades</div>
                </div>
                
                <div class="stat-card success">
                    <div class="stat-value" id="winRate">
                        {% if user_data.trading_stats.total_trades > 0 %}
                            {{ "%.1f"|format((user_data.trading_stats.successful_trades / user_data.trading_stats.total_trades * 100)) }}%
                        {% else %}0%{% endif %}
                    </div>
                    <div class="stat-label">Win Rate</div>
                </div>
                
                <div class="stat-card">
                    <div class="stat-value" id="totalProfit">
                        ${{ "%.2f"|format(user_data.trading_stats.total_profit) }}
                    </div>
                    <div class="stat-label">Total Profit</div>
                </div>
            </div>
            
            <div class="control-group">
                <button class="btn btn-success" id="startBtn" onclick="startTrading()">
                    <span>▶️</span> Start Auto Trading
                </button>
                <button class="btn btn-danger" id="stopBtn" onclick="stopTrading()" style="display: none;">
                    <span>⏹️</span> Stop Auto Trading
                </button>
                <button class="btn btn-info" onclick="updateDashboard()">
                    <span>🔄</span> Refresh
                </button>
                
                <div style="flex: 1; display: flex; align-items: center; justify-content: flex-end;">
                    <div id="tradingStatus" style="padding: 10px 15px; border-radius: 5px; background: rgba(0,0,0,0.3);">
                        {% if engine_status.running %}
                        <span style="color: #00ff00;">●</span> Auto Trading Active
                        {% else %}
                        <span style="color: #ff0000;">●</span> Auto Trading Stopped
                        {% endif %}
                    </div>
                </div>
            </div>
            
            <div style="margin-top: 30px;">
                <h3>Account Information</h3>
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-top: 15px;">
                    <div>
                        <div style="color: #aaa;">Account ID</div>
                        <div style="color: var(--gold); font-weight: bold;">{{ engine_status.account_id or "Not connected" }}</div>
                    </div>
                    <div>
                        <div style="color: #aaa;">Account Type</div>
                        <div style="color: var(--info); font-weight: bold;">{{ engine_status.account_type|default("Unknown")|upper }}</div>
                    </div>
                    <div>
                        <div style="color: #aaa;">Active Trades</div>
                        <div style="color: var(--gold); font-weight: bold;">{{ engine_status.active_trades }}</div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Connection Panel -->
        <div id="connection" class="panel">
            <h2>🔗 Connect to Deriv</h2>
            
            <div class="form-group">
                <label>Deriv API Token</label>
                <textarea id="apiToken" rows="3" placeholder="Enter your Deriv API token..."></textarea>
                <div style="color: #aaa; font-size: 0.9em; margin-top: 5px;">
                    Get your API token from: <strong>Deriv.com → Settings → API Token</strong>
                </div>
            </div>
            
            <div class="control-group">
                <button class="btn btn-primary" onclick="connectDeriv()">
                    <span>🔗</span> Connect to Deriv
                </button>
                <button class="btn btn-danger" onclick="disconnectDeriv()">
                    <span>🔌</span> Disconnect
                </button>
            </div>
            
            <div id="connectionResult" style="margin-top: 20px;"></div>
            
            <div style="margin-top: 30px; padding: 15px; background: rgba(0,0,0,0.3); border-radius: 5px;">
                <h3>💡 How to get API Token:</h3>
                <ol style="margin-left: 20px; margin-top: 10px; color: #ccc;">
                    <li>Login to your Deriv account</li>
                    <li>Go to Settings → API Token</li>
                    <li>Generate a new API token</li>
                    <li>Copy and paste it above</li>
                    <li>Click "Connect to Deriv"</li>
                </ol>
                <p style="color: #aaa; margin-top: 10px;">
                    <strong>Note:</strong> The bot will use your real Deriv account (demo or real money) to execute trades.
                </p>
            </div>
        </div>
        
        <!-- Auto Trading Panel -->
        <div id="trading" class="panel">
            <h2>🤖 Auto Trading Settings</h2>
            
            <div class="form-group">
                <label>Trade Amount ($)</label>
                <input type="number" id="tradeAmount" value="{{ user_data.settings.trade_amount }}" 
                       min="1.00" max="50.00" step="0.10">
                <div style="color: #aaa; font-size: 0.9em;">Minimum: $1.00 | Maximum: $50.00</div>
            </div>
            
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                <div class="form-group">
                    <label>Max Concurrent Trades</label>
                    <input type="number" id="maxConcurrent" value="{{ user_data.settings.max_concurrent_trades }}" 
                           min="1" max="10">
                </div>
                
                <div class="form-group">
                    <label>Max Daily Trades</label>
                    <input type="number" id="maxDaily" value="{{ user_data.settings.max_daily_trades }}" 
                           min="10" max="200">
                </div>
            </div>
            
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                <div class="form-group">
                    <label>Stop Loss ($)</label>
                    <input type="number" id="stopLoss" value="{{ user_data.settings.stop_loss }}" 
                           min="0" max="100">
                    <div style="color: #aaa; font-size: 0.9em;">Stop trading if loss reaches this amount</div>
                </div>
                
                <div class="form-group">
                    <label>Take Profit ($)</label>
                    <input type="number" id="takeProfit" value="{{ user_data.settings.take_profit }}" 
                           min="0" max="200">
                </div>
            </div>
            
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                <div class="form-group">
                    <label>Scan Interval (seconds)</label>
                    <input type="number" id="scanInterval" value="{{ user_data.settings.scan_interval }}" 
                           min="10" max="300">
                </div>
                
                <div class="form-group">
                    <label>Cooldown (seconds)</label>
                    <input type="number" id="cooldown" value="{{ user_data.settings.cooldown_seconds }}" 
                           min="5" max="120">
                </div>
            </div>
            
            <div class="form-group">
                <label>Minimum Confidence (%)</label>
                <input type="range" id="minConfidence" min="50" max="90" 
                       value="{{ user_data.settings.min_confidence }}" 
                       oninput="document.getElementById('confidenceValue').textContent = this.value + '%'">
                <div style="color: var(--gold); font-weight: bold; margin-top: 5px;">
                    Minimum Confidence: <span id="confidenceValue">{{ user_data.settings.min_confidence }}%</span>
                </div>
            </div>
            
            <button class="btn btn-success" onclick="saveTradingSettings()" style="padding: 15px 30px;">
                <span>💾</span> Save Trading Settings
            </button>
        </div>
        
        <!-- Markets Panel -->
        <div id="markets" class="panel">
            <h2>📈 Market Selection</h2>
            <p>Select which markets to trade automatically:</p>
            
            <div class="market-grid">
                {% for market in config.AVAILABLE_MARKETS %}
                <div class="market-item">
                    <input type="checkbox" id="market_{{ market }}" name="market" value="{{ market }}"
                           {% if market in user_data.settings.enabled_markets %}checked{% endif %}>
                    <label for="market_{{ market }}" style="color: white; cursor: pointer;">{{ market }}</label>
                </div>
                {% endfor %}
            </div>
            
            <div class="control-group">
                <button class="btn btn-info" onclick="selectAllMarkets()">
                    <span>✅</span> Select All
                </button>
                <button class="btn btn-info" onclick="deselectAllMarkets()">
                    <span>❌</span> Deselect All
                </button>
                <button class="btn btn-success" onclick="saveMarketSelection()">
                    <span>💾</span> Save Market Selection
                </button>
            </div>
            
            <div style="margin-top: 30px; padding: 15px; background: rgba(0,0,0,0.3); border-radius: 5px;">
                <h3>📊 Market Performance</h3>
                <div id="marketPerformance" style="margin-top: 10px;">
                    <div class="loader"></div>
                </div>
            </div>
        </div>
        
        <!-- Manual Trade Panel -->
        <div id="manual" class="panel">
            <h2>🕹️ Manual Trade Execution</h2>
            
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 30px;">
                <div>
                    <h3>Trade Parameters</h3>
                    
                    <div class="form-group">
                        <label>Select Market</label>
                        <select id="manualSymbol">
                            {% for market in config.AVAILABLE_MARKETS %}
                            <option value="{{ market }}">{{ market }}</option>
                            {% endfor %}
                        </select>
                    </div>
                    
                    <div class="form-group">
                        <label>Trade Amount ($)</label>
                        <input type="number" id="manualAmount" value="2.00" min="1.00" max="50.00" step="0.10">
                    </div>
                    
                    <div class="form-group">
                        <label>Duration</label>
                        <select id="manualDuration">
                            <option value="1">1 Tick</option>
                            <option value="5" selected>5 Ticks</option>
                            <option value="10">10 Ticks</option>
                            <option value="60">1 Minute</option>
                            <option value="300">5 Minutes</option>
                        </select>
                    </div>
                    
                    <div class="control-group" style="margin-top: 30px;">
                        <button class="btn btn-success" onclick="executeManualTrade('CALL')" style="flex: 1;">
                            <span>📈</span> BUY / CALL
                        </button>
                        <button class="btn btn-danger" onclick="executeManualTrade('PUT')" style="flex: 1;">
                            <span>📉</span> SELL / PUT
                        </button>
                    </div>
                </div>
                
                <div>
                    <h3>Market Analysis</h3>
                    <div id="marketAnalysis" style="padding: 20px; background: rgba(0,0,0,0.3); border-radius: 5px; min-height: 200px;">
                        <p style="color: #aaa; text-align: center;">Select a market to see analysis</p>
                    </div>
                    
                    <button class="btn btn-info" onclick="analyzeMarket()" style="margin-top: 15px; width: 100%;">
                        <span>🔍</span> Analyze Selected Market
                    </button>
                </div>
            </div>
            
            <div id="manualTradeResult" style="margin-top: 30px;"></div>
        </div>
        
        <!-- History Panel -->
        <div id="history" class="panel">
            <h2>📋 Trade History</h2>
            
            <div class="control-group">
                <button class="btn btn-info" onclick="loadTradeHistory()">
                    <span>🔄</span> Refresh History
                </button>
                <select id="historyFilter" style="padding: 10px; background: rgba(0,0,0,0.5); color: white; border: 1px solid var(--light-gray); border-radius: 5px;">
                    <option value="all">All Trades</option>
                    <option value="today">Today</option>
                    <option value="profitable">Profitable</option>
                    <option value="loss">Loss</option>
                </select>
            </div>
            
            <div class="table-container">
                <table class="table" id="historyTable">
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Market</th>
                            <th>Type</th>
                            <th>Amount</th>
                            <th>Profit</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody id="historyBody">
                        <!-- Will be populated by JavaScript -->
                    </tbody>
                </table>
            </div>
            
            <div style="text-align: center; margin-top: 20px; color: #aaa;">
                Showing last 50 trades
            </div>
        </div>
    </div>
    
    <script>
        let currentToken = '{{ session.token }}';
        let currentUsername = '{{ username }}';
        let updateInterval;
        
        // Initialize
        document.addEventListener('DOMContentLoaded', function() {
            updateDashboard();
            
            // Start auto-update every 10 seconds
            updateInterval = setInterval(updateDashboard, 10000);
            
            // Load trade history
            loadTradeHistory();
        });
        
        // Tab navigation
        function showPanel(panelId) {
            // Hide all panels
            document.querySelectorAll('.panel').forEach(panel => {
                panel.classList.remove('active');
            });
            
            // Remove active from tabs
            document.querySelectorAll('.tab').forEach(tab => {
                tab.classList.remove('active');
            });
            
            // Show selected panel
            document.getElementById(panelId).classList.add('active');
            
            // Activate tab
            document.querySelectorAll('.tab').forEach(tab => {
                if (tab.onclick.toString().includes(panelId)) {
                    tab.classList.add('active');
                }
            });
            
            // Load data for panel
            if (panelId === 'history') {
                loadTradeHistory();
            }
        }
        
        // Dashboard functions
        function updateDashboard() {
            fetch('/api/status', {
                headers: {
                    'Authorization': 'Bearer ' + currentToken
                }
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    updateDashboardUI(data);
                }
            })
            .catch(error => {
                console.error('Dashboard error:', error);
            });
        }
        
        function updateDashboardUI(data) {
            // Update balance
            const balance = data.status.balance || 0;
            document.getElementById('balance').textContent = '$' + balance.toFixed(2);
            
            // Update trade counts
            document.getElementById('totalTrades').textContent = data.user.stats.total_trades;
            
            // Update win rate
            const totalTrades = data.user.stats.total_trades;
            const successfulTrades = data.user.stats.successful_trades;
            const winRate = totalTrades > 0 ? ((successfulTrades / totalTrades) * 100).toFixed(1) : 0;
            document.getElementById('winRate').textContent = winRate + '%';
            
            // Update profit
            document.getElementById('totalProfit').textContent = '$' + data.user.stats.total_profit.toFixed(2);
            
            // Update trading status
            const isRunning = data.status.running;
            const isConnected = data.status.connected;
            
            document.getElementById('startBtn').style.display = isRunning ? 'none' : 'block';
            document.getElementById('stopBtn').style.display = isRunning ? 'block' : 'none';
            
            const statusText = isRunning ? 
                '<span style="color: #00ff00;">●</span> Auto Trading Active' :
                '<span style="color: #ff0000;">●</span> Auto Trading Stopped';
            document.getElementById('tradingStatus').innerHTML = statusText;
        }
        
        // Trading control
        function startTrading() {
            fetch('/api/trading/start', {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + currentToken,
                    'Content-Type': 'application/json'
                }
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showAlert('Auto trading started successfully!', 'success');
                    updateDashboard();
                } else {
                    showAlert('Failed to start: ' + data.message, 'error');
                }
            });
        }
        
        function stopTrading() {
            fetch('/api/trading/stop', {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + currentToken,
                    'Content-Type': 'application/json'
                }
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showAlert('Auto trading stopped.', 'success');
                    updateDashboard();
                }
            });
        }
        
        // Deriv connection
        function connectDeriv() {
            const apiToken = document.getElementById('apiToken').value.trim();
            
            if (!apiToken) {
                showAlert('Please enter your Deriv API token', 'error');
                return;
            }
            
            showAlert('Connecting to Deriv...', 'info');
            
            fetch('/api/connect', {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + currentToken,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ api_token: apiToken })
            })
            .then(response => response.json())
            .then(data => {
                const resultDiv = document.getElementById('connectionResult');
                if (data.success) {
                    resultDiv.innerHTML = `
                        <div style="background: rgba(0,255,0,0.1); padding: 15px; border-radius: 5px; border-left: 4px solid #00ff00;">
                            <div style="color: #00ff00; font-weight: bold; margin-bottom: 10px;">
                                ✅ Connected to Deriv!
                            </div>
                            <div>Account ID: ${data.account_id}</div>
                            <div>Account Type: ${data.account_type.toUpperCase()}</div>
                            <div>Balance: $${data.balance.toFixed(2)}</div>
                        </div>
                    `;
                    showAlert('Successfully connected to Deriv!', 'success');
                    updateDashboard();
                } else {
                    resultDiv.innerHTML = `
                        <div style="background: rgba(255,0,0,0.1); padding: 15px; border-radius: 5px; border-left: 4px solid #ff0000;">
                            <div style="color: #ff0000; font-weight: bold;">
                                ❌ Connection Failed
                            </div>
                            <div>${data.message}</div>
                        </div>
                    `;
                    showAlert('Connection failed: ' + data.message, 'error');
                }
            });
        }
        
        function disconnectDeriv() {
            fetch('/api/disconnect', {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + currentToken,
                    'Content-Type': 'application/json'
                }
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showAlert('Disconnected from Deriv', 'success');
                    document.getElementById('connectionResult').innerHTML = '';
                    updateDashboard();
                }
            });
        }
        
        // Trading settings
        function saveTradingSettings() {
            const settings = {
                trade_amount: parseFloat(document.getElementById('tradeAmount').value),
                max_concurrent_trades: parseInt(document.getElementById('maxConcurrent').value),
                max_daily_trades: parseInt(document.getElementById('maxDaily').value),
                stop_loss: parseFloat(document.getElementById('stopLoss').value),
                take_profit: parseFloat(document.getElementById('takeProfit').value),
                scan_interval: parseInt(document.getElementById('scanInterval').value),
                cooldown_seconds: parseInt(document.getElementById('cooldown').value),
                min_confidence: parseInt(document.getElementById('minConfidence').value)
            };
            
            saveSettings(settings, 'Trading settings saved successfully!');
        }
        
        // Market selection
        function selectAllMarkets() {
            document.querySelectorAll('input[name="market"]').forEach(checkbox => {
                checkbox.checked = true;
            });
        }
        
        function deselectAllMarkets() {
            document.querySelectorAll('input[name="market"]').forEach(checkbox => {
                checkbox.checked = false;
            });
        }
        
        function saveMarketSelection() {
            const selectedMarkets = Array.from(document.querySelectorAll('input[name="market"]:checked'))
                .map(cb => cb.value);
            
            if (selectedMarkets.length === 0) {
                showAlert('Please select at least one market', 'error');
                return;
            }
            
            fetch('/api/settings/update', {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + currentToken,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    settings: { enabled_markets: selectedMarkets }
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showAlert(`Saved ${selectedMarkets.length} markets for trading`, 'success');
                }
            });
        }
        
        // Manual trade
        function analyzeMarket() {
            const symbol = document.getElementById('manualSymbol').value;
            
            fetch('/api/market/' + symbol, {
                headers: {
                    'Authorization': 'Bearer ' + currentToken
                }
            })
            .then(response => response.json())
            .then(data => {
                const analysisDiv = document.getElementById('marketAnalysis');
                if (data.success) {
                    analysisDiv.innerHTML = `
                        <div>
                            <div style="color: #ccc; margin-bottom: 10px;">Market Analysis for ${symbol}</div>
                            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
                                <div>
                                    <div style="color: #aaa;">Current Price</div>
                                    <div style="color: var(--gold); font-weight: bold;">$${data.price.toFixed(5)}</div>
                                </div>
                                <div>
                                    <div style="color: #aaa;">Bid/Ask</div>
                                    <div style="color: var(--gold); font-weight: bold;">${data.bid.toFixed(5)} / ${data.ask.toFixed(5)}</div>
                                </div>
                                <div>
                                    <div style="color: #aaa;">Signal</div>
                                    <div style="color: ${data.signal === 'CALL' ? '#00ff00' : '#ff0000'}; font-weight: bold;">
                                        ${data.signal}
                                    </div>
                                </div>
                                <div>
                                    <div style="color: #aaa;">Confidence</div>
                                    <div style="color: var(--info); font-weight: bold;">${data.confidence}%</div>
                                </div>
                            </div>
                        </div>
                    `;
                } else {
                    analysisDiv.innerHTML = `
                        <div style="color: #ff0000;">
                            Failed to analyze market: ${data.message}
                        </div>
                    `;
                }
            });
        }
        
        function executeManualTrade(direction) {
            const symbol = document.getElementById('manualSymbol').value;
            const amount = parseFloat(document.getElementById('manualAmount').value);
            const duration = parseInt(document.getElementById('manualDuration').value);
            
            if (amount < 1.00) {
                showAlert('Minimum trade amount is $1.00', 'error');
                return;
            }
            
            showAlert(`Executing ${direction} trade on ${symbol}...`, 'info');
            
            fetch('/api/trade/manual', {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + currentToken,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    symbol: symbol,
                    direction: direction,
                    amount: amount,
                    duration: duration
                })
            })
            .then(response => response.json())
            .then(data => {
                const resultDiv = document.getElementById('manualTradeResult');
                if (data.success) {
                    resultDiv.innerHTML = `
                        <div style="background: rgba(0,255,0,0.1); padding: 20px; border-radius: 5px; border-left: 4px solid #00ff00;">
                            <div style="color: #00ff00; font-weight: bold; margin-bottom: 15px; font-size: 1.2em;">
                                ✅ TRADE EXECUTED SUCCESSFULLY
                            </div>
                            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px;">
                                <div>
                                    <div style="color: #aaa;">Contract ID</div>
                                    <div style="color: white; font-weight: bold;">${data.contract_id}</div>
                                </div>
                                <div>
                                    <div style="color: #aaa;">Profit</div>
                                    <div style="color: #00ff00; font-weight: bold;">$${data.profit.toFixed(2)}</div>
                                </div>
                                <div>
                                    <div style="color: #aaa;">New Balance</div>
                                    <div style="color: var(--gold); font-weight: bold;">$${data.balance.toFixed(2)}</div>
                                </div>
                            </div>
                            <div style="margin-top: 15px; color: #ccc;">
                                Trade executed at: ${new Date().toLocaleTimeString()}
                            </div>
                        </div>
                    `;
                    showAlert('Trade executed successfully!', 'success');
                    updateDashboard();
                    loadTradeHistory();
                } else {
                    resultDiv.innerHTML = `
                        <div style="background: rgba(255,0,0,0.1); padding: 20px; border-radius: 5px; border-left: 4px solid #ff0000;">
                            <div style="color: #ff0000; font-weight: bold; margin-bottom: 10px;">
                                ❌ TRADE FAILED
                            </div>
                            <div>${data.message}</div>
                        </div>
                    `;
                    showAlert('Trade failed: ' + data.message, 'error');
                }
            });
        }
        
        // History
        function loadTradeHistory() {
            const tbody = document.getElementById('historyBody');
            tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; padding: 30px;"><div class="loader"></div></td></tr>';
            
            fetch('/api/trades/history', {
                headers: { 'Authorization': 'Bearer ' + currentToken }
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    renderTradeHistory(data.trades);
                }
            });
        }
        
        function renderTradeHistory(trades) {
            const tbody = document.getElementById('historyBody');
            tbody.innerHTML = '';
            
            if (trades.length === 0) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="6" style="text-align: center; padding: 40px; color: #666;">
                            <div style="font-size: 2em; margin-bottom: 10px;">📊</div>
                            <div>No trades yet. Start trading to see history here.</div>
                        </td>
                    </tr>
                `;
                return;
            }
            
            // Sort by timestamp (newest first)
            trades.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
            
            trades.forEach(trade => {
                const time = new Date(trade.timestamp).toLocaleTimeString();
                const profit = trade.result?.profit || 0;
                const profitColor = profit >= 0 ? '#00ff00' : '#ff0000';
                const status = trade.success ? '✅ Success' : '❌ Failed';
                
                const row = document.createElement('tr');
                row.innerHTML = `
                    <td>${time}</td>
                    <td>${trade.symbol}</td>
                    <td style="color: ${trade.direction === 'CALL' ? '#00ff00' : '#ff0000'}">
                        ${trade.direction}
                    </td>
                    <td>$${trade.amount.toFixed(2)}</td>
                    <td style="color: ${profitColor}; font-weight: bold;">
                        $${profit.toFixed(2)}
                    </td>
                    <td>${status}</td>
                `;
                tbody.appendChild(row);
            });
        }
        
        // Settings helper
        function saveSettings(settings, message) {
            fetch('/api/settings/update', {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + currentToken,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ settings: settings })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showAlert(message, 'success');
                    updateDashboard();
                } else {
                    showAlert('Failed to save: ' + data.message, 'error');
                }
            });
        }
        
        // Alert function
        function showAlert(message, type = 'info') {
            const alertDiv = document.getElementById('alert');
            alertDiv.textContent = message;
            alertDiv.className = 'alert ' + type;
            alertDiv.style.display = 'block';
            
            setTimeout(() => {
                alertDiv.style.display = 'none';
            }, 5000);
        }
        
        // Logout
        function logout() {
            window.location.href = '/logout';
        }
    </script>
</body>
</html>
'''

# ============ FLASK ROUTES ============
@app.route('/')
def index():
    """Main page"""
    return redirect('/login')

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        success, message, user_data = user_manager.authenticate_user(username, password)
        
        if success:
            session['token'] = user_data['token']
            session['username'] = username
            return redirect('/dashboard')
        
        return render_template_string('''
            <!DOCTYPE html>
            <html>
            <head>
                <title>Login - Deriv Trading Bot</title>
                <style>
                    body { background: black; color: gold; font-family: Arial; padding: 20px; }
                    .login-box { max-width: 400px; margin: 100px auto; padding: 30px; border: 2px solid gold; border-radius: 10px; }
                    input { width: 100%; padding: 12px; margin: 10px 0; background: #222; color: gold; border: 1px solid gold; border-radius: 5px; }
                    button { background: gold; color: black; padding: 12px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; width: 100%; }
                </style>
            </head>
            <body>
                <div class="login-box">
                    <h2 style="text-align: center;">🚀 Deriv Trading Bot</h2>
                    {% if error %}
                    <p style="color: red; text-align: center;">{{ error }}</p>
                    {% endif %}
                    <form method="POST">
                        <input type="text" name="username" placeholder="Username" required>
                        <input type="password" name="password" placeholder="Password" required>
                        <button type="submit">Login</button>
                    </form>
                    <p style="text-align: center; margin-top: 20px;">
                        <a href="/register" style="color: gold;">Create Account</a>
                    </p>
                </div>
            </body>
            </html>
        ''', error=message)
    
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Login - Deriv Trading Bot</title>
            <style>
                body { background: black; color: gold; font-family: Arial; padding: 20px; }
                .login-box { max-width: 400px; margin: 100px auto; padding: 30px; border: 2px solid gold; border-radius: 10px; }
                input { width: 100%; padding: 12px; margin: 10px 0; background: #222; color: gold; border: 1px solid gold; border-radius: 5px; }
                button { background: gold; color: black; padding: 12px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; width: 100%; }
            </style>
        </head>
        <body>
            <div class="login-box">
                <h2 style="text-align: center;">🚀 Deriv Trading Bot</h2>
                <form method="POST">
                    <input type="text" name="username" placeholder="Username" required>
                    <input type="password" name="password" placeholder="Password" required>
                    <button type="submit">Login</button>
                </form>
                <p style="text-align: center; margin-top: 20px;">
                    <a href="/register" style="color: gold;">Create Account</a>
                </p>
            </div>
        </body>
        </html>
    ''')

@app.route('/register', methods=['GET', 'POST'])
def register():
    """Registration page"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        confirm = request.form.get('confirm_password', '').strip()
        email = request.form.get('email', '').strip()
        
        if password != confirm:
            return render_template_string('''
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Register - Deriv Trading Bot</title>
                    <style>
                        body { background: black; color: gold; font-family: Arial; padding: 20px; }
                        .register-box { max-width: 400px; margin: 50px auto; padding: 30px; border: 2px solid gold; border-radius: 10px; }
                        input { width: 100%; padding: 12px; margin: 10px 0; background: #222; color: gold; border: 1px solid gold; border-radius: 5px; }
                        button { background: gold; color: black; padding: 12px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; width: 100%; }
                    </style>
                </head>
                <body>
                    <div class="register-box">
                        <h2 style="text-align: center;">Create Account</h2>
                        <p style="color: red; text-align: center;">Passwords do not match!</p>
                        <form method="POST">
                            <input type="text" name="username" placeholder="Username" required>
                            <input type="email" name="email" placeholder="Email (optional)">
                            <input type="password" name="password" placeholder="Password (min 6 chars)" required>
                            <input type="password" name="confirm_password" placeholder="Confirm Password" required>
                            <button type="submit">Register</button>
                        </form>
                        <p style="text-align: center; margin-top: 20px;">
                            <a href="/login" style="color: gold;">Already have account? Login</a>
                        </p>
                    </div>
                </body>
                </html>
            ''')
        
        success, message = user_manager.register_user(username, password, email)
        
        if success:
            return redirect('/login')
        
        return render_template_string('''
            <!DOCTYPE html>
            <html>
            <head>
                <title>Register - Deriv Trading Bot</title>
                <style>
                    body { background: black; color: gold; font-family: Arial; padding: 20px; }
                    .register-box { max-width: 400px; margin: 50px auto; padding: 30px; border: 2px solid gold; border-radius: 10px; }
                    input { width: 100%; padding: 12px; margin: 10px 0; background: #222; color: gold; border: 1px solid gold; border-radius: 5px; }
                    button { background: gold; color: black; padding: 12px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; width: 100%; }
                </style>
            </head>
            <body>
                <div class="register-box">
                    <h2 style="text-align: center;">Create Account</h2>
                    <p style="color: red; text-align: center;">{{ error }}</p>
                    <form method="POST">
                        <input type="text" name="username" placeholder="Username" required>
                        <input type="email" name="email" placeholder="Email (optional)">
                        <input type="password" name="password" placeholder="Password (min 6 chars)" required>
                        <input type="password" name="confirm_password" placeholder="Confirm Password" required>
                        <button type="submit">Register</button>
                    </form>
                    <p style="text-align: center; margin-top: 20px;">
                        <a href="/login" style="color: gold;">Already have account? Login</a>
                    </p>
                </div>
            </body>
            </html>
        ''', error=message)
    
    return render_template_string('''
        <!DOCTYPE html>
        <html>
        <head>
            <title>Register - Deriv Trading Bot</title>
            <style>
                body { background: black; color: gold; font-family: Arial; padding: 20px; }
                .register-box { max-width: 400px; margin: 50px auto; padding: 30px; border: 2px solid gold; border-radius: 10px; }
                input { width: 100%; padding: 12px; margin: 10px 0; background: #222; color: gold; border: 1px solid gold; border-radius: 5px; }
                button { background: gold; color: black; padding: 12px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; width: 100%; }
            </style>
        </head>
        <body>
            <div class="register-box">
                <h2 style="text-align: center;">Create Account</h2>
                <form method="POST">
                    <input type="text" name="username" placeholder="Username" required>
                    <input type="email" name="email" placeholder="Email (optional)">
                    <input type="password" name="password" placeholder="Password (min 6 chars)" required>
                    <input type="password" name="confirm_password" placeholder="Confirm Password" required>
                    <button type="submit">Register</button>
                </form>
                <p style="text-align: center; margin-top: 20px;">
                    <a href="/login" style="color: gold;">Already have account? Login</a>
                </p>
            </div>
        </body>
        </html>
    ''')

@app.route('/dashboard')
def dashboard():
    """Main dashboard"""
    if 'token' not in session or 'username' not in session:
        return redirect('/login')
    
    valid, username = user_manager.validate_token(session['token'])
    if not valid:
        return redirect('/login')
    
    user_data = user_manager.get_user(username)
    if not user_data:
        return redirect('/login')
    
    engine = get_user_engine(username)
    engine_status = engine.get_status()
    
    return render_template_string(
        TRADING_UI,
        username=username,
        config=config,
        user_data=user_data,
        engine_status=engine_status
    )

@app.route('/logout')
def logout():
    """Logout user"""
    username = session.get('username')
    if username and username in session_manager:
        session_manager[username].client.disconnect()
        del session_manager[username]
    
    session.clear()
    return redirect('/login')

# ============ API ENDPOINTS ============
@app.route('/api/status')
@token_required
def api_status():
    """Get bot status"""
    engine = get_user_engine(request.username)
    user_data = user_manager.get_user(request.username)
    
    return jsonify({
        'success': True,
        'status': engine.get_status(),
        'user': {
            'settings': user_data['settings'],
            'stats': user_data['trading_stats']
        }
    })

@app.route('/api/connect', methods=['POST'])
@token_required
def api_connect():
    """Connect to Deriv"""
    data = request.json or {}
    api_token = data.get('api_token', '').strip()
    
    if not api_token:
        return jsonify({'success': False, 'message': 'API token required'})
    
    engine = get_user_engine(request.username)
    
    # Save token
    user_manager.save_deriv_token(request.username, api_token)
    
    # Connect to Deriv
    success, message, balance = engine.connect_to_deriv(api_token)
    
    if success:
        # Update user balance
        user_manager.update_user_stats(request.username, {
            'current_balance': balance
        })
        
        return jsonify({
            'success': True,
            'message': message,
            'account_id': engine.client.account_id,
            'account_type': engine.client.account_type,
            'balance': balance
        })
    else:
        return jsonify({
            'success': False,
            'message': message
        })

@app.route('/api/disconnect', methods=['POST'])
@token_required
def api_disconnect():
    """Disconnect from Deriv"""
    engine = get_user_engine(request.username)
    engine.client.disconnect()
    
    return jsonify({'success': True, 'message': 'Disconnected from Deriv'})

@app.route('/api/settings/update', methods=['POST'])
@token_required
def api_update_settings():
    """Update user settings"""
    data = request.json or {}
    settings = data.get('settings', {})
    
    user_manager.update_user_settings(request.username, settings)
    
    engine = get_user_engine(request.username)
    engine.settings.update(settings)
    
    return jsonify({'success': True, 'message': 'Settings updated'})

@app.route('/api/trade/manual', methods=['POST'])
@token_required
def api_manual_trade():
    """Execute manual trade"""
    data = request.json or {}
    symbol = data.get('symbol', 'R_10')
    direction = data.get('direction', 'CALL')
    amount = float(data.get('amount', 2.0))
    duration = int(data.get('duration', 5))
    
    engine = get_user_engine(request.username)
    
    if not engine.client.connected:
        return jsonify({'success': False, 'message': 'Not connected to Deriv'})
    
    success, result = engine.client.place_trade(
        symbol=symbol,
        contract_type=direction,
        amount=amount,
        duration=duration,
        duration_unit='t'
    )
    
    if success:
        profit = result.get('profit', 0)
        
        # Update user stats
        user_manager.update_user_stats(request.username, {
            'total_trades': 1,
            'successful_trades': 1 if profit > 0 else 0,
            'failed_trades': 1 if profit <= 0 else 0,
            'total_profit': profit,
            'current_balance': result.get('balance', 0)
        })
        
        return jsonify({
            'success': True,
            'message': 'Trade executed successfully',
            'contract_id': result.get('contract_id'),
            'profit': profit,
            'balance': result.get('balance')
        })
    else:
        user_manager.update_user_stats(request.username, {
            'total_trades': 1,
            'failed_trades': 1
        })
        
        return jsonify({
            'success': False,
            'message': result.get('error', 'Trade failed')
        })

@app.route('/api/market/<symbol>')
@token_required
def api_market_data(symbol):
    """Get market data"""
    engine = get_user_engine(request.username)
    
    if not engine.client.connected:
        return jsonify({'success': False, 'message': 'Not connected to Deriv'})
    
    market_data = engine.client.get_market_data(symbol)
    
    if market_data:
        # Analyze market
        analysis = engine._analyze_market(symbol, market_data)
        
        return jsonify({
            'success': True,
            'symbol': symbol,
            'bid': market_data['bid'],
            'ask': market_data['ask'],
            'price': market_data['bid'],
            'signal': analysis.get('direction', 'NEUTRAL'),
            'confidence': analysis.get('confidence', 50)
        })
    else:
        return jsonify({'success': False, 'message': 'Failed to get market data'})

@app.route('/api/trading/start', methods=['POST'])
@token_required
def api_start_trading():
    """Start auto trading"""
    engine = get_user_engine(request.username)
    success, message = engine.start_trading()
    
    return jsonify({'success': success, 'message': message, 'running': engine.running})

@app.route('/api/trading/stop', methods=['POST'])
@token_required
def api_stop_trading():
    """Stop auto trading"""
    engine = get_user_engine(request.username)
    success, message = engine.stop_trading()
    
    return jsonify({'success': success, 'message': message, 'running': engine.running})

@app.route('/api/trades/history')
@token_required
def api_trade_history():
    """Get trade history"""
    engine = get_user_engine(request.username)
    
    # Get user's trades (last 50)
    user_trades = [t for t in engine.trade_history if t['username'] == request.username]
    recent_trades = user_trades[-50:] if user_trades else []
    
    return jsonify({
        'success': True,
        'trades': recent_trades,
        'total': len(user_trades)
    })

@app.route('/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'users': len(user_manager.users)
    })

# ============ START APPLICATION ============
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    
    logger.info("""
    ========================================================================
    🚀 DERIV TRADING BOT - READY FOR REAL TRADING
    ========================================================================
    • Real Deriv API Integration
    • Demo & Real Money Accounts
    • 24/7 Auto Trading
    • Live Market Execution
    • User-Controlled Settings
    ========================================================================
    """)
    
    logger.info(f"Starting server on port {port}")
    
    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,
        threaded=True
    )
