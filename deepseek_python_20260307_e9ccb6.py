import os
from datetime import datetime, timedelta
from functools import wraps
import json

from flask import Flask, render_template, redirect, url_for, flash, request, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_migrate import Migrate
from sqlalchemy import func, extract
from dotenv import load_dotenv
import pandas as pd
import plotly
import plotly.graph_objs as go
from authlib.integrations.flask_client import OAuth

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key-here')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///fx_journal.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# OAuth setup
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.getenv('GOOGLE_CLIENT_ID'),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# Database Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200))
    google_id = db.Column(db.String(100), unique=True)
    trades = db.relationship('Trade', backref='user', lazy=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Trade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    
    # Trade Details
    setup_name = db.Column(db.String(100), nullable=False)
    instrument = db.Column(db.String(20), nullable=False)  # EUR/USD, GBP/USD, XAU/USD, etc.
    trade_type = db.Column(db.String(10), nullable=False)  # BUY or SELL
    entry_price = db.Column(db.Float, nullable=False)
    exit_price = db.Column(db.Float)
    lot_size = db.Column(db.Float, nullable=False)
    stop_loss = db.Column(db.Float)
    take_profit = db.Column(db.Float)
    
    # Time Frames
    entry_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    exit_time = db.Column(db.DateTime)
    time_frame = db.Column(db.String(20))  # M1, M5, M15, M30, H1, H4, D1, W1
    
    # Trade Management
    result = db.Column(db.String(20))  # WIN, LOSS, BREAK_EVEN
    profit_loss = db.Column(db.Float)  # Calculated P&L in account currency (USD)
    profit_loss_pips = db.Column(db.Float)
    
    # Notes and Tags
    notes = db.Column(db.Text)
    tags = db.Column(db.String(200))  # Comma-separated tags
    
    # Images/Charts (storing file paths)
    chart_image = db.Column(db.String(200))
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def calculate_pnl(self):
        """Calculate P&L based on instrument, lot size, and price movement"""
        if not self.exit_price:
            return 0, 0
        
        price_diff = self.exit_price - self.entry_price if self.trade_type == 'BUY' else self.entry_price - self.exit_price
        
        # Pip value calculation based on instrument
        pip_value = self.get_pip_value()
        pips_moved = price_diff / pip_value
        self.profit_loss_pips = pips_moved
        
        # Calculate actual P&L in USD
        self.profit_loss = self.calculate_usd_pnl(price_diff)
        
        # Determine result
        if self.profit_loss > 0:
            self.result = 'WIN'
        elif self.profit_loss < 0:
            self.result = 'LOSS'
        else:
            self.result = 'BREAK_EVEN'
        
        return self.profit_loss, self.profit_loss_pips

    def get_pip_value(self):
        """Get pip value for different instruments"""
        pip_values = {
            'EUR/USD': 0.0001,
            'GBP/USD': 0.0001,
            'USD/JPY': 0.01,
            'USD/CHF': 0.0001,
            'USD/CAD': 0.0001,
            'AUD/USD': 0.0001,
            'NZD/USD': 0.0001,
            'XAU/USD': 0.01,  # Gold
            'XAG/USD': 0.001,  # Silver
            'BTC/USD': 1.0,    # Bitcoin
            'ETH/USD': 0.1      # Ethereum
        }
        return pip_values.get(self.instrument, 0.0001)

    def calculate_usd_pnl(self, price_diff):
        """Calculate actual USD P&L based on instrument and lot size"""
        instrument_config = {
            'EUR/USD': {'pip_value_usd': 10, 'pip_size': 0.0001},
            'GBP/USD': {'pip_value_usd': 10, 'pip_size': 0.0001},
            'USD/JPY': {'pip_value_usd': 9.5, 'pip_size': 0.01},
            'USD/CHF': {'pip_value_usd': 10, 'pip_size': 0.0001},
            'USD/CAD': {'pip_value_usd': 8, 'pip_size': 0.0001},
            'AUD/USD': {'pip_value_usd': 10, 'pip_size': 0.0001},
            'NZD/USD': {'pip_value_usd': 10, 'pip_size': 0.0001},
            'XAU/USD': {'pip_value_usd': 100, 'pip_size': 0.01},  # Gold
            'XAG/USD': {'pip_value_usd': 50, 'pip_size': 0.001},   # Silver
            'BTC/USD': {'pip_value_usd': 1, 'pip_size': 1.0},      # Bitcoin
            'ETH/USD': {'pip_value_usd': 0.5, 'pip_size': 0.1}     # Ethereum
        }
        
        config = instrument_config.get(self.instrument, {'pip_value_usd': 10, 'pip_size': 0.0001})
        
        # Calculate pips moved
        pips_moved = price_diff / config['pip_size']
        
        # Calculate P&L based on lot size
        # Standard lot = 100,000 units, Mini lot = 10,000, Micro lot = 1,000
        pnl = pips_moved * config['pip_value_usd'] * self.lot_size
        
        return round(pnl, 2)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        
        # Check if user exists
        user = User.query.filter_by(email=email).first()
        if user:
            flash('Email already registered', 'danger')
            return redirect(url_for('register'))
        
        # Create new user
        new_user = User(username=username, email=email)
        new_user.set_password(password)
        
        db.session.add(new_user)
        db.session.commit()
        
        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('login'))
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        user = User.query.filter_by(email=email).first()
        
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('dashboard'))
        
        flash('Invalid email or password', 'danger')
    
    return render_template('login.html')

@app.route('/login/google')
def google_login():
    redirect_uri = url_for('google_authorize', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/login/google/authorize')
def google_authorize():
    token = google.authorize_access_token()
    userinfo = google.parse_id_token(token)
    
    # Check if user exists
    user = User.query.filter_by(email=userinfo['email']).first()
    
    if not user:
        # Create new user
        user = User(
            username=userinfo['email'].split('@')[0],
            email=userinfo['email'],
            google_id=userinfo['sub']
        )
        db.session.add(user)
        db.session.commit()
    
    login_user(user)
    return redirect(url_for('dashboard'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    # Get user's trades
    trades = Trade.query.filter_by(user_id=current_user.id).order_by(Trade.entry_time.desc()).all()
    
    # Calculate statistics
    total_trades = len(trades)
    winning_trades = sum(1 for t in trades if t.result == 'WIN')
    losing_trades = sum(1 for t in trades if t.result == 'LOSS')
    
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    
    total_pnl = sum(t.profit_loss for t in trades if t.profit_loss)
    avg_win = sum(t.profit_loss for t in trades if t.result == 'WIN') / winning_trades if winning_trades > 0 else 0
    avg_loss = sum(t.profit_loss for t in trades if t.result == 'LOSS') / losing_trades if losing_trades > 0 else 0
    
    # Daily P&L for chart
    daily_pnl = db.session.query(
        func.date(Trade.entry_time).label('date'),
        func.sum(Trade.profit_loss).label('total_pnl')
    ).filter(
        Trade.user_id == current_user.id,
        Trade.profit_loss.isnot(None)
    ).group_by(func.date(Trade.entry_time)).all()
    
    # Create Plotly chart
    dates = [str(d[0]) for d in daily_pnl]
    pnl_values = [float(d[1]) for d in daily_pnl]
    
    fig = go.Figure(data=[
        go.Bar(x=dates, y=pnl_values, marker_color=['green' if x > 0 else 'red' for x in pnl_values])
    ])
    
    fig.update_layout(
        title='Daily P&L',
        xaxis_title='Date',
        yaxis_title='P&L (USD)',
        showlegend=False
    )
    
    graphJSON = json.dumps(fig, cls=plotly.utils.PlotlyJSONEncoder)
    
    return render_template('dashboard.html',
                         trades=trades[:10],  # Show last 10 trades
                         total_trades=total_trades,
                         win_rate=round(win_rate, 2),
                         total_pnl=round(total_pnl, 2),
                         avg_win=round(avg_win, 2),
                         avg_loss=round(avg_loss, 2),
                         graphJSON=graphJSON)

@app.route('/add_trade', methods=['GET', 'POST'])
@login_required
def add_trade():
    if request.method == 'POST':
        trade = Trade(
            user_id=current_user.id,
            setup_name=request.form.get('setup_name'),
            instrument=request.form.get('instrument'),
            trade_type=request.form.get('trade_type'),
            entry_price=float(request.form.get('entry_price')),
            lot_size=float(request.form.get('lot_size')),
            stop_loss=float(request.form.get('stop_loss')) if request.form.get('stop_loss') else None,
            take_profit=float(request.form.get('take_profit')) if request.form.get('take_profit') else None,
            time_frame=request.form.get('time_frame'),
            notes=request.form.get('notes'),
            tags=request.form.get('tags')
        )
        
        # Set entry time
        entry_date = request.form.get('entry_date')
        entry_time = request.form.get('entry_time')
        if entry_date and entry_time:
            trade.entry_time = datetime.strptime(f"{entry_date} {entry_time}", "%Y-%m-%d %H:%M")
        
        # Set exit if provided
        exit_price = request.form.get('exit_price')
        if exit_price:
            trade.exit_price = float(exit_price)
            
            exit_date = request.form.get('exit_date')
            exit_time = request.form.get('exit_time')
            if exit_date and exit_time:
                trade.exit_time = datetime.strptime(f"{exit_date} {exit_time}", "%Y-%m-%d %H:%M")
            
            # Calculate P&L
            trade.calculate_pnl()
        
        db.session.add(trade)
        db.session.commit()
        
        flash('Trade added successfully!', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('add_trade.html')

@app.route('/edit_trade/<int:trade_id>', methods=['GET', 'POST'])
@login_required
def edit_trade(trade_id):
    trade = Trade.query.get_or_404(trade_id)
    
    if trade.user_id != current_user.id:
        flash('Unauthorized access', 'danger')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        trade.setup_name = request.form.get('setup_name')
        trade.instrument = request.form.get('instrument')
        trade.trade_type = request.form.get('trade_type')
        trade.entry_price = float(request.form.get('entry_price'))
        trade.exit_price = float(request.form.get('exit_price')) if request.form.get('exit_price') else None
        trade.lot_size = float(request.form.get('lot_size'))
        trade.stop_loss = float(request.form.get('stop_loss')) if request.form.get('stop_loss') else None
        trade.take_profit = float(request.form.get('take_profit')) if request.form.get('take_profit') else None
        trade.time_frame = request.form.get('time_frame')
        trade.notes = request.form.get('notes')
        trade.tags = request.form.get('tags')
        
        # Update times
        entry_date = request.form.get('entry_date')
        entry_time = request.form.get('entry_time')
        if entry_date and entry_time:
            trade.entry_time = datetime.strptime(f"{entry_date} {entry_time}", "%Y-%m-%d %H:%M")
        
        if trade.exit_price:
            exit_date = request.form.get('exit_date')
            exit_time = request.form.get('exit_time')
            if exit_date and exit_time:
                trade.exit_time = datetime.strptime(f"{exit_date} {exit_time}", "%Y-%m-%d %H:%M")
            
            # Recalculate P&L
            trade.calculate_pnl()
        
        db.session.commit()
        flash('Trade updated successfully!', 'success')
        return redirect(url_for('dashboard'))
    
    return render_template('edit_trade.html', trade=trade)

@app.route('/delete_trade/<int:trade_id>')
@login_required
def delete_trade(trade_id):
    trade = Trade.query.get_or_404(trade_id)
    
    if trade.user_id != current_user.id:
        flash('Unauthorized access', 'danger')
        return redirect(url_for('dashboard'))
    
    db.session.delete(trade)
    db.session.commit()
    
    flash('Trade deleted successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/calendar')
@login_required
def calendar_view():
    return render_template('calendar.html')

@app.route('/api/trades')
@login_required
def get_trades():
    trades = Trade.query.filter_by(user_id=current_user.id).order_by(Trade.entry_time).all()
    
    trade_data = []
    for trade in trades:
        trade_data.append({
            'id': trade.id,
            'title': f"{trade.instrument} - {trade.trade_type}",
            'start': trade.entry_time.isoformat(),
            'end': trade.exit_time.isoformat() if trade.exit_time else None,
            'color': 'green' if trade.result == 'WIN' else 'red' if trade.result == 'LOSS' else 'gray',
            'extendedProps': {
                'setup': trade.setup_name,
                'pnl': trade.profit_loss,
                'result': trade.result,
                'lot_size': trade.lot_size
            }
        })
    
    return jsonify(trade_data)

@app.route('/api/stats')
@login_required
def get_stats():
    period = request.args.get('period', 'daily')
    
    if period == 'daily':
        group_by = func.date(Trade.entry_time)
    elif period == 'weekly':
        group_by = func.date_trunc('week', Trade.entry_time)
    elif period == 'monthly':
        group_by = func.date_trunc('month', Trade.entry_time)
    else:
        group_by = func.date(Trade.entry_time)
    
    stats = db.session.query(
        group_by.label('period'),
        func.count(Trade.id).label('total_trades'),
        func.sum(Trade.profit_loss).label('total_pnl'),
        func.avg(Trade.profit_loss).label('avg_pnl'),
        func.sum(case([(Trade.result == 'WIN', 1)], else_=0)).label('wins'),
        func.sum(case([(Trade.result == 'LOSS', 1)], else_=0)).label('losses')
    ).filter(
        Trade.user_id == current_user.id
    ).group_by('period').order_by('period').all()
    
    result = []
    for stat in stats:
        result.append({
            'period': str(stat.period),
            'total_trades': stat.total_trades,
            'total_pnl': float(stat.total_pnl) if stat.total_pnl else 0,
            'avg_pnl': float(stat.avg_pnl) if stat.avg_pnl else 0,
            'win_rate': round(stat.wins / stat.total_trades * 100, 2) if stat.total_trades > 0 else 0
        })
    
    return jsonify(result)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)