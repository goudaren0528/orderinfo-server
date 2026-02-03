from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from collections import defaultdict, deque
from typing import Any, DefaultDict, Deque, Dict, Tuple
import os
import uuid
import time
import hmac
import secrets
import json
import base64
from functools import wraps
from dotenv import load_dotenv
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

# 加载 .env 文件中的环境变量
load_dotenv(override=True)

# 配置
app = Flask(__name__)
# 默认使用 SQLite，如果需要使用 Postgres，只需修改环境变量或此处配置
# 例如: 'postgresql://user:password@localhost/dbname'
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///auth.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.environ.get('SECRET_KEY')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
ADMIN_API_KEY = os.environ.get('ADMIN_API_KEY')
LICENSE_PRIVATE_KEY = os.environ.get('LICENSE_PRIVATE_KEY')
LICENSE_PUBLIC_KEY = os.environ.get('LICENSE_PUBLIC_KEY')
ALLOW_DEVICE_KEY_RESET = os.environ.get('ALLOW_DEVICE_KEY_RESET', 'true').lower() in ('1', 'true', 'yes')

if not app.secret_key:
    raise ValueError("严重错误：未设置 SECRET_KEY 环境变量。请设置该环境变量以配置会话密钥。")

if not ADMIN_PASSWORD:
    raise ValueError("严重错误：未设置 ADMIN_PASSWORD 环境变量。请设置该环境变量以配置管理员密码。")

if not ADMIN_API_KEY:
    raise ValueError("严重错误：未设置 ADMIN_API_KEY 环境变量。请设置该环境变量以配置管理员 API 密钥。")

app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() in ('1', 'true', 'yes')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

db: Any = SQLAlchemy(app)

PERMANENT_EXPIRE_DATE = datetime(9999, 12, 31)

# --- 模型定义 ---


class License(db.Model):
    code = db.Column(db.String(64), primary_key=True)
    max_devices = db.Column(db.Integer, default=1)
    expire_date = db.Column(db.DateTime, nullable=False)  # 过期时间
    revoked = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    remark = db.Column(db.String(255))
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


class KeyStore(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=False)


class LicenseConfig(db.Model):
    license_code = db.Column(db.String(64), db.ForeignKey('license.code'), primary_key=True)
    value = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    machine_id = db.Column(db.String(128), nullable=False)  # 机器唯一码
    license_code = db.Column(db.String(64), db.ForeignKey('license.code'), nullable=False)
    last_heartbeat = db.Column(db.DateTime, default=datetime.now)
    ip_address = db.Column(db.String(64))
    public_key = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)
    __table_args__ = (db.UniqueConstraint('license_code', 'machine_id', name='uniq_license_machine'),)


class ApiAudit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    ip_address = db.Column(db.String(64))
    endpoint = db.Column(db.String(64))
    license_code = db.Column(db.String(64))
    machine_id = db.Column(db.String(128))
    ok = db.Column(db.Boolean, default=False)
    reason = db.Column(db.String(255))


# --- 辅助函数 ---

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


rate_limit_store: DefaultDict[Tuple[str, str], Deque[float]] = defaultdict(deque)
nonce_store: Dict[str, int] = {}
config_token_store: Dict[Tuple[str, str], Tuple[str, int]] = {}
_license_private_key = None
_license_public_key = None


def get_client_ip():
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or 'unknown'


def is_rate_limited(ip, bucket, limit, window_seconds):
    dq = rate_limit_store[(ip, bucket)]
    now = time.time()
    while dq and now - dq[0] > window_seconds:
        dq.popleft()
    if len(dq) >= limit:
        return True
    dq.append(now)
    return False


def ensure_csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token


def validate_csrf_token():
    token = session.get('csrf_token')
    form_token = request.form.get('csrf_token')
    if not token or not form_token:
        return False
    return hmac.compare_digest(token, form_token)


def parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d')
    except Exception:
        return None


def parse_int(value, default=None):
    try:
        return int(value)
    except Exception:
        return default


def compute_expire_date(start_date_str, days_value, permanent_flag):
    if permanent_flag:
        return PERMANENT_EXPIRE_DATE
    start_date = parse_date(start_date_str) or datetime.now()
    days = parse_int(days_value, None)
    if days is None:
        return None
    return start_date + timedelta(days=days)


def canonical_json(data):
    return json.dumps(data, separators=(',', ':'), sort_keys=True)


def format_pem(key_text):
    """修复环境变量中可能存在的 PEM 格式问题 (如将换行符转义为 \\n)"""
    if not key_text:
        return None
    return key_text.replace('\\n', '\n').strip()


def load_license_keys():
    global _license_private_key, _license_public_key
    if _license_private_key and _license_public_key:
        return _license_private_key, _license_public_key
    
    if LICENSE_PRIVATE_KEY and LICENSE_PUBLIC_KEY:
        try:
            private_pem = format_pem(LICENSE_PRIVATE_KEY)
            public_pem = format_pem(LICENSE_PUBLIC_KEY)
            _license_private_key = serialization.load_pem_private_key(private_pem.encode(), password=None)
            _license_public_key = serialization.load_pem_public_key(public_pem.encode())
            return _license_private_key, _license_public_key
        except Exception as e:
            app.logger.error(f"Failed to load keys from environment variables: {e}")
            # 如果环境变量中的 key 无效，尝试从数据库加载或生成新的（视情况而定，这里继续往下走）
            
    store_private = KeyStore.query.get('license_private_key')
    store_public = KeyStore.query.get('license_public_key')
    if store_private and store_public:
        _license_private_key = serialization.load_pem_private_key(store_private.value.encode(), password=None)
        _license_public_key = serialization.load_pem_public_key(store_public.value.encode())
        return _license_private_key, _license_public_key
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    db.session.add(KeyStore(key='license_private_key', value=private_pem))
    db.session.add(KeyStore(key='license_public_key', value=public_pem))
    db.session.commit()
    _license_private_key = private_key
    _license_public_key = public_key
    return _license_private_key, _license_public_key


def get_license_public_key_pem():
    _, public_key = load_license_keys()
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def sign_license_payload(payload):
    private_key, _ = load_license_keys()
    body = canonical_json(payload).encode()
    signature = private_key.sign(body)
    return base64.b64encode(signature).decode()


def sign_config_payload(payload):
    private_key, _ = load_license_keys()
    body = canonical_json(payload).encode()
    signature = private_key.sign(body)
    return base64.b64encode(signature).decode()


def verify_device_signature(public_key_pem):
    signature_b64 = request.headers.get('X-Device-Signature')
    if not signature_b64:
        return False, "missing"
    try:
        signature = base64.b64decode(signature_b64)
    except Exception:
        return False, "invalid"
    body = request.get_data() or b""
    try:
        public_key = serialization.load_pem_public_key(public_key_pem.encode())
    except Exception:
        return False, "invalid"
    try:
        public_key.verify(signature, body)
    except InvalidSignature:
        return False, "invalid"
    return True, ""


def verify_request_nonce(data):
    ts = data.get('ts')
    nonce = data.get('nonce')
    if not ts or not nonce:
        return False, "missing"
    try:
        ts = int(ts)
    except Exception:
        return False, "invalid"
    now = int(time.time())
    if abs(now - ts) > 300:
        return False, "expired"
    for key, value in list(nonce_store.items()):
        if now - value > 600:
            del nonce_store[key]
    if nonce in nonce_store:
        return False, "replay"
    nonce_store[nonce] = now
    return True, ""


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


def ensure_device_unique_index():
    table_name = Device.__table__.name
    index_name = 'uniq_license_machine'
    sql = f'CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table_name} (license_code, machine_id)'
    with db.engine.begin() as conn:
        conn.execute(text(sql))


def ensure_device_public_key_column():
    dialect = db.engine.dialect.name
    table_name = Device.__table__.name
    if dialect == 'sqlite':
        with db.engine.begin() as conn:
            rows = conn.execute(text(f'PRAGMA table_info({table_name})')).fetchall()
        columns = {row[1] for row in rows}
        if 'public_key' not in columns:
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN public_key TEXT'))
    else:
        with db.engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = :table_name"
            ), {"table_name": table_name}).fetchall()
        columns = {row[0] for row in rows}
        if 'public_key' not in columns:
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN public_key TEXT'))


def ensure_license_remark_column():
    dialect = db.engine.dialect.name
    table_name = License.__table__.name
    if dialect == 'sqlite':
        with db.engine.begin() as conn:
            rows = conn.execute(text(f'PRAGMA table_info({table_name})')).fetchall()
        columns = {row[1] for row in rows}
        if 'remark' not in columns:
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN remark VARCHAR(255)'))
    else:
        with db.engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = :table_name"
            ), {"table_name": table_name}).fetchall()
        columns = {row[0] for row in rows}
        if 'remark' not in columns:
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN remark VARCHAR(255)'))


def ensure_license_revoked_column():
    dialect = db.engine.dialect.name
    table_name = License.__table__.name
    if dialect == 'sqlite':
        with db.engine.begin() as conn:
            rows = conn.execute(text(f'PRAGMA table_info({table_name})')).fetchall()
        columns = {row[1] for row in rows}
        if 'revoked' not in columns:
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN revoked BOOLEAN DEFAULT 0'))
    else:
        with db.engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = :table_name"
            ), {"table_name": table_name}).fetchall()
        columns = {row[0] for row in rows}
        if 'revoked' not in columns:
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN revoked BOOLEAN DEFAULT FALSE'))


def ensure_device_created_at_column():
    dialect = db.engine.dialect.name
    table_name = Device.__table__.name
    if dialect == 'sqlite':
        with db.engine.begin() as conn:
            rows = conn.execute(text(f'PRAGMA table_info({table_name})')).fetchall()
        columns = {row[1] for row in rows}
        if 'created_at' not in columns:
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN created_at TIMESTAMP'))
                # Set default for existing rows
                conn.execute(text(f"UPDATE {table_name} SET created_at = datetime('now') WHERE created_at IS NULL"))
    else:
        with db.engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = :table_name"
            ), {"table_name": table_name}).fetchall()
        columns = {row[0] for row in rows}
        if 'created_at' not in columns:
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN created_at TIMESTAMP'))
                conn.execute(text(f"UPDATE {table_name} SET created_at = NOW() WHERE created_at IS NULL"))


def _parse_import_expire(item):
    if bool(item.get('permanent')):
        return PERMANENT_EXPIRE_DATE
    expire_value = item.get('expire_date')
    if isinstance(expire_value, str):
        try:
            return datetime.fromisoformat(expire_value)
        except Exception:
            return None
    start_date_str = item.get('start_date')
    days_value = item.get('days')
    return compute_expire_date(start_date_str, days_value, False)


def _parse_import_item(item, idx):
    if not isinstance(item, dict):
        return None, f'第{idx + 1}条: 不是对象'
    code = (item.get('code') or '').strip()
    if not code:
        return None, f'第{idx + 1}条: code 为空'
    max_devices = parse_int(item.get('max_devices', 1), 1)
    remark = item.get('remark')
    remark_value = remark.strip() if isinstance(remark, str) else ''
    revoked_flag = bool(item.get('revoked'))
    expire_date = _parse_import_expire(item)
    if not expire_date:
        return None, f'第{idx + 1}条: 有效期无效'
    return {
        "code": code,
        "max_devices": max_devices,
        "expire_date": expire_date,
        "remark": remark_value,
        "revoked": revoked_flag
    }, None


def _build_license_payload(license_obj, code, machine_id):
    return {
        "code": code,
        "machine_id": machine_id,
        "expire_date": license_obj.expire_date.strftime("%Y-%m-%d"),
        "max_devices": license_obj.max_devices,
        "issued_at": int(time.time())
    }


def _sync_device_public_key(device, device_public_key):
    if device.public_key and device.public_key != device_public_key:
        if not ALLOW_DEVICE_KEY_RESET:
            return False
        device.public_key = device_public_key
    if not device.public_key:
        device.public_key = device_public_key
    return True


def _activation_success_response(license_payload):
    return jsonify({
        "status": "success",
        "message": "激活成功",
        "license": license_payload,
        "license_signature": sign_license_payload(license_payload),
        "public_key": get_license_public_key_pem()
    })


def _activation_error(message, status_code):
    return jsonify({"status": "error", "message": message}), status_code


def _config_error(message, status_code):
    return jsonify({"status": "error", "message": message}), status_code


def _audit_request(endpoint, code, machine_id, ok, reason):
    try:
        record = ApiAudit(
            ip_address=get_client_ip(),
            endpoint=endpoint,
            license_code=code,
            machine_id=machine_id,
            ok=ok,
            reason=reason
        )
        db.session.add(record)
        db.session.commit()
    except Exception:
        db.session.rollback()


def _issue_config_token(code, machine_id):
    token = secrets.token_urlsafe(32)
    expire_ts = int(time.time()) + 600
    config_token_store[(code, machine_id)] = (token, expire_ts)
    return token, expire_ts


def _verify_config_token(code, machine_id, token):
    record = config_token_store.get((code, machine_id))
    if not record:
        return False
    stored_token, expire_ts = record
    if int(time.time()) > expire_ts:
        config_token_store.pop((code, machine_id), None)
        return False
    return secrets.compare_digest(stored_token, token or "")


def _validate_activate_request(data):
    code = data.get('code')
    machine_id = data.get('machine_id')
    device_public_key = data.get('device_public_key')
    if not code or not machine_id or not device_public_key:
        return None, _activation_error("参数不完整", 400)
    ok, reason = verify_request_nonce(data)
    if not ok:
        return None, _activation_error("请求已失效", 401)
    ok, reason = verify_device_signature(device_public_key)
    if not ok:
        return None, _activation_error("签名校验失败", 401)
    return (code, machine_id, device_public_key), None


def _validate_device_request(data):
    code = data.get('code')
    machine_id = data.get('machine_id')
    if not code or not machine_id:
        return None, ("参数不完整", 400)
    device = Device.query.filter_by(license_code=code, machine_id=machine_id).first()
    if not device:
        return None, ("设备未激活或已掉线", 401)
    if not device.public_key:
        return None, ("设备未注册密钥", 401)
    ok, reason = verify_request_nonce(data)
    if not ok:
        return None, ("请求已失效", 401)
    ok, reason = verify_device_signature(device.public_key)
    if not ok:
        return None, ("签名校验失败", 401)
    license_obj = License.query.get(code)
    if not license_obj:
        return None, ("授权码无效", 403)
    if license_obj.revoked:
        return None, ("授权码已作废", 403)
    if datetime.now() > license_obj.expire_date:
        return None, ("授权码已过期", 403)
    return (code, device), None


def _load_common_config():
    store = KeyStore.query.get('common_config')
    if not store:
        return {}
    try:
        payload = json.loads(store.value)
        if isinstance(payload, list):
            return {"sites": payload}
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _load_license_config(code):
    record = LicenseConfig.query.get(code)
    if not record:
        return {}
    try:
        payload = json.loads(record.value)
        if isinstance(payload, list):
            return {"sites": payload}
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_license_config(code, config):
    payload = json.dumps(config, ensure_ascii=False)
    record = LicenseConfig.query.get(code)
    if record:
        record.value = payload
    else:
        record = LicenseConfig(license_code=code, value=payload)
        db.session.add(record)
    db.session.commit()


def _get_valid_license(code):
    license_obj = License.query.get(code)
    if not license_obj:
        return None, _activation_error("授权码无效", 404)
    if license_obj.revoked:
        return None, _activation_error("授权码已作废", 403)
    if datetime.now() > license_obj.expire_date:
        return None, _activation_error("授权码已过期", 403)
    return license_obj, None


def _activate_device(device, device_public_key):
    if not _sync_device_public_key(device, device_public_key):
        return _activation_error("设备密钥不匹配", 403)
    device.last_heartbeat = datetime.now()
    device.ip_address = request.remote_addr
    db.session.commit()
    return None


def _resolve_device_integrity(code, machine_id, device_public_key):
    db.session.rollback()
    existing = Device.query.filter_by(license_code=code, machine_id=machine_id).first()
    if not existing:
        return None, _activation_error("激活失败", 500)
    error = _activate_device(existing, device_public_key)
    if error:
        return None, error
    return existing, None


def _get_or_create_device(code, machine_id, device_public_key, max_devices):
    device = Device.query.filter_by(license_code=code, machine_id=machine_id).first()
    if device:
        error = _activate_device(device, device_public_key)
        return (device, None) if not error else (None, error)
    current_count = Device.query.filter_by(license_code=code).count()
    if current_count >= max_devices:
        message = f"设备数量已达上限 ({current_count}/{max_devices})，请在其他设备退出后重试"
        return None, _activation_error(message, 403)
    new_device = Device(
        machine_id=machine_id,
        license_code=code,
        last_heartbeat=datetime.now(),
        ip_address=request.remote_addr,
        public_key=device_public_key
    )
    try:
        db.session.add(new_device)
        db.session.commit()
        return new_device, None
    except IntegrityError:
        return _resolve_device_integrity(code, machine_id, device_public_key)


def _apply_import_update(existing, parsed):
    existing.max_devices = parsed["max_devices"]
    existing.expire_date = parsed["expire_date"]
    existing.remark = parsed["remark"]
    existing.revoked = parsed["revoked"]


def _build_import_license(parsed):
    return License(
        code=parsed["code"],
        max_devices=parsed["max_devices"],
        expire_date=parsed["expire_date"],
        remark=parsed["remark"],
        revoked=parsed["revoked"]
    )


def _import_licenses_from_payload(payload):
    imported = 0
    updated = 0
    duplicated = 0
    failed = []
    seen_codes = set()
    for idx, item in enumerate(payload):
        parsed, error = _parse_import_item(item, idx)
        if error:
            failed.append(error)
            continue
        code = parsed["code"]
        if code in seen_codes:
            duplicated += 1
            continue
        seen_codes.add(code)
        existing = License.query.get(code)
        if existing:
            _apply_import_update(existing, parsed)
            updated += 1
        else:
            db.session.add(_build_import_license(parsed))
            imported += 1
    db.session.commit()
    return imported, updated, duplicated, failed

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
    client_ip = get_client_ip()
    if request.method == 'POST':
        if is_rate_limited(client_ip, 'login', 10, 300):
            flash('尝试次数过多，请稍后再试', 'danger')
            return render_template('login.html')
        password = request.form.get('password')
        if password == ADMIN_PASSWORD:
            session['logged_in'] = True
            session['permanent'] = True
            session['csrf_token'] = secrets.token_urlsafe(32)
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
    remark_filter = (request.args.get('remark') or '').strip()
    query = License.query
    if remark_filter:
        query = query.filter(License.remark.ilike(f'%{remark_filter}%'))
    licenses = query.order_by(License.created_at.desc()).all()
    total_licenses = len(licenses)

    # 计算全平台在线设备总数
    threshold = datetime.now() - timedelta(minutes=10)
    total_devices = Device.query.filter(Device.last_heartbeat >= threshold).count()

    return render_template(
        'dashboard.html',
        licenses=licenses,
        total_licenses=total_licenses,
        total_devices=total_devices,
        now=datetime.now(),
        csrf_token=ensure_csrf_token(),
        permanent_cutoff=PERMANENT_EXPIRE_DATE,
        remark_filter=remark_filter
    )


@app.route('/dashboard/generate', methods=['POST'])
@login_required
def generate_license_view():
    if not validate_csrf_token():
        flash('请求无效，请刷新页面重试', 'danger')
        return redirect(url_for('dashboard'))
    start_date_str = request.form.get('start_date')
    days_value = request.form.get('days', 365)
    permanent_flag = request.form.get('permanent') == 'on'
    max_devices = parse_int(request.form.get('max_devices', 1), 1)
    custom_code = request.form.get('code', '').strip()
    remark = (request.form.get('remark') or '').strip()

    code = custom_code if custom_code else str(uuid.uuid4())
    expire_date = compute_expire_date(start_date_str, days_value, permanent_flag)
    if not expire_date:
        flash('有效期参数无效', 'danger')
        return redirect(url_for('dashboard'))

    new_license = License(
        code=code,
        max_devices=max_devices,
        expire_date=expire_date,
        remark=remark
    )

    try:
        db.session.add(new_license)
        db.session.commit()
        flash(f'授权码生成成功: {code}', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'生成失败: {str(e)}', 'danger')

    return redirect(url_for('dashboard'))


@app.route('/dashboard/update/<code>', methods=['POST'])
@login_required
def update_license(code):
    if not validate_csrf_token():
        flash('请求无效，请刷新页面重试', 'danger')
        return redirect(url_for('dashboard'))
    license_obj = License.query.get(code)
    if not license_obj:
        flash('授权码不存在', 'danger')
        return redirect(url_for('dashboard'))
    start_date_str = request.form.get('start_date')
    days_value = request.form.get('days')
    expire_date_str = request.form.get('expire_date')
    permanent_flag = request.form.get('permanent') == 'on'
    max_devices = parse_int(request.form.get('max_devices'), None)
    revoked_flag = request.form.get('revoked') == 'on'
    if 'remark' in request.form:
        license_obj.remark = (request.form.get('remark') or '').strip()
    if max_devices is not None:
        license_obj.max_devices = max_devices
    license_obj.revoked = revoked_flag
    if expire_date_str:
        parsed_expire = parse_date(expire_date_str)
        if not parsed_expire:
            flash('截止日期无效', 'danger')
            return redirect(url_for('dashboard'))
        license_obj.expire_date = parsed_expire
    else:
        expire_date = compute_expire_date(start_date_str, days_value, permanent_flag)
        if expire_date:
            license_obj.expire_date = expire_date
    db.session.commit()
    flash('授权码已更新', 'success')
    return redirect(url_for('dashboard'))


@app.route('/dashboard/delete/<code>', methods=['POST'])
@login_required
def delete_license(code):
    if not validate_csrf_token():
        flash('请求无效，请刷新页面重试', 'danger')
        return redirect(url_for('dashboard'))
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


@app.route('/dashboard/export', methods=['GET'])
@login_required
def export_licenses():
    licenses = License.query.order_by(License.created_at.desc()).all()
    payload = []
    for item in licenses:
        payload.append({
            "code": item.code,
            "max_devices": item.max_devices,
            "expire_date": item.expire_date.isoformat(),
            "created_at": item.created_at.isoformat(),
            "remark": item.remark or "",
            "revoked": bool(item.revoked)
        })
    response = jsonify(payload)
    response.headers['Content-Disposition'] = 'attachment; filename=licenses.json'
    return response


@app.route('/dashboard/import', methods=['POST'])
@login_required
def import_licenses():
    if not validate_csrf_token():
        flash('请求无效，请刷新页面重试', 'danger')
        return redirect(url_for('dashboard'))
    file = request.files.get('license_file')
    if not file:
        flash('未选择文件', 'danger')
        return redirect(url_for('dashboard'))
    try:
        data = file.read()
        payload = json.loads(data)
    except Exception:
        flash('JSON 解析失败', 'danger')
        return redirect(url_for('dashboard'))
    if not isinstance(payload, list):
        flash('JSON 格式错误', 'danger')
        return redirect(url_for('dashboard'))
    imported, updated, duplicated, failed = _import_licenses_from_payload(payload)
    message = f'导入完成：新增 {imported}，更新 {updated}，重复 {duplicated}，失败 {len(failed)}'
    flash(message, 'success')
    if failed:
        head = failed[:10]
        tail = f'... 另有 {len(failed) - len(head)} 条未展示' if len(failed) > 10 else ''
        detail = '；'.join(head) + (tail if tail else '')
        flash(f'失败明细：{detail}', 'danger')
    return redirect(url_for('dashboard'))


@app.route('/dashboard/devices/<code>')
@login_required
def get_license_devices(code):
    license_obj = License.query.get(code)
    if not license_obj:
        return jsonify({'error': '授权码不存在'}), 404
    
    devices = Device.query.filter_by(license_code=code).all()
    device_list = []
    threshold = datetime.now() - timedelta(minutes=10)
    
    for d in devices:
        is_online = d.last_heartbeat >= threshold
        device_list.append({
            'machine_id': d.machine_id,
            'ip_address': d.ip_address or '未知',
            'last_heartbeat': d.last_heartbeat.strftime('%Y-%m-%d %H:%M:%S'),
            'created_at': d.created_at.strftime('%Y-%m-%d %H:%M:%S') if d.created_at else '未知',
            'is_online': is_online
        })
    
    return jsonify(device_list)


# --- API 接口 ---

def api_exception_handler(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            app.logger.error(f"API Error: {str(e)}", exc_info=True)
            return jsonify({
                "status": "error",
                "message": f"Server Error: {str(e)}",
                "type": type(e).__name__
            }), 500
    return decorated_function


@app.route('/api/activate', methods=['POST'])
@api_exception_handler
def activate():
    client_ip = get_client_ip()
    if is_rate_limited(client_ip, 'activate', 60, 300):
        return _activation_error("请求过于频繁", 429)
    data = request.get_json(silent=True) or {}
    validated, error = _validate_activate_request(data)
    if error:
        return error
    code, machine_id, device_public_key = validated
    license_obj, error = _get_valid_license(code)
    if error:
        return error
    # 清理僵尸设备
    cleanup_stale_devices(code)

    device, error = _get_or_create_device(
        code,
        machine_id,
        device_public_key,
        license_obj.max_devices
    )
    if error:
        return error
    license_payload = _build_license_payload(license_obj, code, machine_id)
    return _activation_success_response(license_payload)


@app.route('/api/heartbeat', methods=['POST'])
@api_exception_handler
def heartbeat():
    client_ip = get_client_ip()
    if is_rate_limited(client_ip, 'heartbeat', 120, 300):
        return jsonify({"status": "error", "message": "请求过于频繁"}), 429
    data = request.get_json(silent=True) or {}
    code = data.get('code')
    machine_id = data.get('machine_id')
    if not code or not machine_id:
        return jsonify({"status": "error", "message": "参数不完整"}), 400

    device = Device.query.filter_by(license_code=code, machine_id=machine_id).first()

    if not device:
        return jsonify({"status": "error", "message": "设备未激活或已掉线"}), 401
    if not device.public_key:
        return jsonify({"status": "error", "message": "设备未注册密钥"}), 401
    ok, reason = verify_request_nonce(data)
    if not ok:
        return jsonify({"status": "error", "message": "请求已失效"}), 401
    ok, reason = verify_device_signature(device.public_key)
    if not ok:
        return jsonify({"status": "error", "message": "签名校验失败"}), 401

    license_obj = License.query.get(code)
    if not license_obj:
        return jsonify({"status": "error", "message": "授权码无效"}), 403
    if license_obj.revoked:
        return jsonify({"status": "error", "message": "授权码已作废"}), 403
    if datetime.now() > license_obj.expire_date:
        return jsonify({"status": "error", "message": "授权已过期"}), 403

    device.last_heartbeat = datetime.now()
    db.session.commit()

    return jsonify({"status": "success"})


@app.route('/api/config/fetch', methods=['POST'])
@api_exception_handler
def fetch_config():
    client_ip = get_client_ip()
    if is_rate_limited(client_ip, 'config_fetch', 120, 300):
        _audit_request('config_fetch', None, None, False, "请求过于频繁")
        return _config_error("请求过于频繁", 429)
    data = request.get_json(silent=True) or {}
    validated, error = _validate_device_request(data)
    if error:
        message, status_code = error
        _audit_request('config_fetch', data.get('code'), data.get('machine_id'), False, message)
        return _config_error(message, status_code)
    code, device = validated
    device.last_heartbeat = datetime.now()
    db.session.commit()
    common_config = _load_common_config()
    user_config = _load_license_config(code)
    config_ts = int(time.time())
    payload = {
        "code": code,
        "machine_id": device.machine_id,
        "ts": config_ts,
        "common_config": common_config,
        "user_config": user_config
    }
    signature = sign_config_payload(payload)
    token, token_expire = _issue_config_token(code, device.machine_id)
    _audit_request('config_fetch', code, device.machine_id, True, "")
    return jsonify({
        "status": "success",
        "common_config": common_config,
        "user_config": user_config,
        "config_signature": signature,
        "config_ts": config_ts,
        "config_token": token,
        "config_token_expire": token_expire
    })


@app.route('/api/config/save', methods=['POST'])
def save_config():
    client_ip = get_client_ip()
    if is_rate_limited(client_ip, 'config_save', 60, 300):
        _audit_request('config_save', None, None, False, "请求过于频繁")
        return _config_error("请求过于频繁", 429)
    data = request.get_json(silent=True) or {}
    config = data.get('config')
    if config is None or not isinstance(config, dict):
        _audit_request('config_save', data.get('code'), data.get('machine_id'), False, "配置无效")
        return _config_error("配置无效", 400)
    validated, error = _validate_device_request(data)
    if error:
        message, status_code = error
        _audit_request('config_save', data.get('code'), data.get('machine_id'), False, message)
        return _config_error(message, status_code)
    code, device = validated
    config_token = data.get('config_token')
    if not config_token or not _verify_config_token(code, device.machine_id, config_token):
        _audit_request('config_save', code, device.machine_id, False, "令牌无效")
        return _config_error("令牌无效", 401)
    device.last_heartbeat = datetime.now()
    db.session.commit()
    _save_license_config(code, config)
    token, token_expire = _issue_config_token(code, device.machine_id)
    _audit_request('config_save', code, device.machine_id, True, "")
    return jsonify({"status": "success", "config_token": token, "config_token_expire": token_expire})


@app.route('/api/public-key', methods=['GET'])
def public_key():
    return jsonify({"public_key": get_license_public_key_pem()})

# 保留 API 方式生成授权码（方便脚本调用）


@app.route('/admin/generate', methods=['POST'])
def generate_license_api():
    client_ip = get_client_ip()
    if is_rate_limited(client_ip, 'admin_generate', 30, 300):
        return jsonify({"message": "Too Many Requests"}), 429
    if request.headers.get('X-Admin-Api-Key') != ADMIN_API_KEY:
        return jsonify({"message": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    days = payload.get('days', 365)
    max_devices = payload.get('max_devices', 1)
    custom_code = payload.get('code')
    remark = (payload.get('remark') or '').strip()

    code = custom_code if custom_code else str(uuid.uuid4())
    expire_date = datetime.now() + timedelta(days=days)

    new_license = License(
        code=code,
        max_devices=max_devices,
        expire_date=expire_date,
        remark=remark
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
    ensure_device_unique_index()
    ensure_device_public_key_column()
    ensure_device_created_at_column()
    ensure_license_remark_column()
    ensure_license_revoked_column()


if __name__ == '__main__':
    # 避免与客户端(默认5000)冲突，使用 5005 端口
    host = os.environ.get('FLASK_HOST', '0.0.0.0')
    port = int(os.environ.get('FLASK_PORT', 5005))
    app.run(host=host, port=port, debug=False)
