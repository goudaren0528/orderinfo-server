from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import os
import uuid
from functools import wraps
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
load_dotenv()

# 配置
app = Flask(__name__)
# 默认使用 SQLite，如果需要使用 Postgres，只需修改环境变量或此处配置
# 例如: 'postgresql://user:password@localhost/dbname'
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///auth.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-it-in-prod')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')

if not ADMIN_PASSWORD:
    raise ValueError("严重错误：未设置 ADMIN_PASSWORD 环境变量。请设置该环境变量以配置管理员密码。")

db = SQLAlchemy(app)

# --- 模型定义 ---

class License(db.Model):
    code = db.Column(db.String(64), primary_key=True)
    max_devices = db.Column(db.Integer, default=1)
    expire_date = db.Column(db.DateTime, nullable=False) # 过期时间
    created_at = db.Column(db.DateTime, default=datetime.now)
    # 关联设备
    devices = db.relationship('Device', backref='license', lazy=True)

    @property
    def active_devices_count(self):
        # 统计有效心跳设备 (10分钟内)
        threshold = datetime.now() - timedelta(minutes=10)
        return Device.query.filter(
            Device.license_code == self.code,
            Device.last_heartbeat >= threshold
        ).count()

class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    machine_id = db.Column(db.String(128), nullable=False) # 机器唯一码
    license_code = db.Column(db.String(64), db.ForeignKey('license.code'), nullable=False)
    last_heartbeat = db.Column(db.DateTime, default=datetime.now)
    ip_address = db.Column(db.String(64))

# --- 辅助函数 ---

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def cleanup_stale_devices(license_code):
    """清理指定授权码下超过 10 分钟未心跳的设备"""
    threshold = datetime.now() - timedelta(minutes=10)
    # 查找该授权码下所有超时的设备
    stale_devices = Device.query.filter(
        Device.license_code == license_code,
        Device.last_heartbeat < threshold
    ).all()
    
    for device in stale_devices:
        db.session.delete(device)
    
    if stale_devices:
        db.session.commit()

# --- 页面路由 ---

@app.route('/')
def index():
    if session.get('logged_in'):
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/admin')
def admin_redirect():
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        else:
            flash('密码错误', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    licenses = License.query.order_by(License.created_at.desc()).all()
    total_licenses = len(licenses)
    
    # 计算全平台在线设备总数
    threshold = datetime.now() - timedelta(minutes=10)
    total_devices = Device.query.filter(Device.last_heartbeat >= threshold).count()
    
    return render_template('dashboard.html', 
                           licenses=licenses, 
                           total_licenses=total_licenses,
                           total_devices=total_devices,
                           now=datetime.now())

@app.route('/dashboard/generate', methods=['POST'])
@login_required
def generate_license_view():
    days = int(request.form.get('days', 365))
    max_devices = int(request.form.get('max_devices', 1))
    custom_code = request.form.get('code', '').strip()
    
    code = custom_code if custom_code else str(uuid.uuid4())
    expire_date = datetime.now() + timedelta(days=days)
    
    new_license = License(
        code=code,
        max_devices=max_devices,
        expire_date=expire_date
    )
    
    try:
        db.session.add(new_license)
        db.session.commit()
        flash(f'授权码生成成功: {code}', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'生成失败: {str(e)}', 'danger')
        
    return redirect(url_for('dashboard'))

@app.route('/dashboard/delete/<code>', methods=['POST'])
@login_required
def delete_license(code):
    license_obj = License.query.get(code)
    if license_obj:
        # 级联删除设备
        Device.query.filter_by(license_code=code).delete()
        db.session.delete(license_obj)
        db.session.commit()
        flash('授权码已删除', 'success')
    else:
        flash('授权码不存在', 'danger')
    return redirect(url_for('dashboard'))

# --- API 接口 ---

@app.route('/api/activate', methods=['POST'])
def activate():
    data = request.json
    code = data.get('code')
    machine_id = data.get('machine_id')
    
    if not code or not machine_id:
        return jsonify({"status": "error", "message": "参数不完整"}), 400
        
    license_obj = License.query.get(code)
    
    if not license_obj:
        return jsonify({"status": "error", "message": "授权码无效"}), 404
        
    if datetime.now() > license_obj.expire_date:
        return jsonify({"status": "error", "message": "授权码已过期"}), 403
        
    # 清理僵尸设备
    cleanup_stale_devices(code)
    
    # 检查设备是否已存在
    device = Device.query.filter_by(license_code=code, machine_id=machine_id).first()
    
    if device:
        # 已存在的设备，更新心跳
        device.last_heartbeat = datetime.now()
        device.ip_address = request.remote_addr
        db.session.commit()
        return jsonify({
            "status": "success", 
            "message": "激活成功", 
            "expire_date": license_obj.expire_date.strftime("%Y-%m-%d")
        })
    
    # 新设备，检查名额
    current_count = Device.query.filter_by(license_code=code).count()
    if current_count >= license_obj.max_devices:
        return jsonify({
            "status": "error", 
            "message": f"设备数量已达上限 ({current_count}/{license_obj.max_devices})，请在其他设备退出后重试"
        }), 403
        
    # 添加新设备
    new_device = Device(
        machine_id=machine_id,
        license_code=code,
        last_heartbeat=datetime.now(),
        ip_address=request.remote_addr
    )
    db.session.add(new_device)
    db.session.commit()
    
    return jsonify({
        "status": "success", 
        "message": "激活成功",
        "expire_date": license_obj.expire_date.strftime("%Y-%m-%d")
    })

@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    code = data.get('code')
    machine_id = data.get('machine_id')
    
    device = Device.query.filter_by(license_code=code, machine_id=machine_id).first()
    
    if not device:
        return jsonify({"status": "error", "message": "设备未激活或已掉线"}), 401
        
    license_obj = License.query.get(code)
    if not license_obj or datetime.now() > license_obj.expire_date:
        return jsonify({"status": "error", "message": "授权已过期"}), 403
        
    device.last_heartbeat = datetime.now()
    db.session.commit()
    
    return jsonify({"status": "success"})

# 保留 API 方式生成授权码（方便脚本调用）
@app.route('/admin/generate', methods=['POST'])
def generate_license_api():
    # 简单保护：建议 API 也加上密钥验证
    if request.headers.get('X-Admin-Secret') != ADMIN_PASSWORD:
         return jsonify({"message": "Unauthorized"}), 401
        
    days = request.json.get('days', 365)
    max_devices = request.json.get('max_devices', 1)
    custom_code = request.json.get('code')
    
    code = custom_code if custom_code else str(uuid.uuid4())
    expire_date = datetime.now() + timedelta(days=days)
    
    new_license = License(
        code=code,
        max_devices=max_devices,
        expire_date=expire_date
    )
    
    try:
        db.session.add(new_license)
        db.session.commit()
        return jsonify({
            "status": "success",
            "code": code,
            "expire_date": expire_date.strftime("%Y-%m-%d"),
            "max_devices": max_devices
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# 初始化数据库
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    # 避免与客户端(默认5000)冲突，使用 5005 端口
    app.run(host='0.0.0.0', port=5005, debug=True)
