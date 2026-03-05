from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_mail import Mail, Message
import pymysql
import boto3
import os
import jwt
import random
import string
import hashlib
import datetime
from dotenv import load_dotenv
from functools import wraps
from botocore.exceptions import NoCredentialsError
import uuid

load_dotenv()

app = Flask(__name__)

# ─────────────────────────────────
# CORS CONFIG
# ─────────────────────────────────

FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://ranjit-9qx.pages.dev")

CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [
                FRONTEND_URL,
                "https://ranjit-9qx.pages.dev",
                "http://localhost:3000",
                "http://127.0.0.1:3000"
            ]
        }
    },
    supports_credentials=True
)

# ─────────────────────────────────
# SECRET
# ─────────────────────────────────

app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    "change-this-secret-key-in-production"
)

# ─────────────────────────────────
# DATABASE CONFIG
# ─────────────────────────────────

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_USER = os.environ.get("DB_USER", "admin")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "password")
DB_NAME = os.environ.get("DB_NAME", "listenme")
DB_PORT = int(os.environ.get("DB_PORT", 3306))

# ─────────────────────────────────
# S3 CONFIG
# ─────────────────────────────────

S3_BUCKET = os.environ.get("S3_BUCKET")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
AWS_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.environ.get("AWS_SECRET_KEY")

# ─────────────────────────────────
# ADMIN EMAIL
# ─────────────────────────────────

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()

# ─────────────────────────────────
# EMAIL CONFIG
# ─────────────────────────────────

app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", 587))
app.config["MAIL_USE_TLS"] = True
app.config["MAIL_USE_SSL"] = False
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD")
app.config["MAIL_DEFAULT_SENDER"] = os.environ.get("MAIL_USERNAME")

mail = Mail(app)

# ─── DATABASE ──────────────────────────────────────────────────────────────────
def get_db():
    return pymysql.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME, port=DB_PORT,
        charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )

def init_db():
    """Create all tables if they don't exist yet."""
    conn = get_db()
    try:
        with conn.cursor() as c:
            # Users
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id         INT AUTO_INCREMENT PRIMARY KEY,
                    email      VARCHAR(255) UNIQUE NOT NULL,
                    password   VARCHAR(255) NOT NULL,
                    name       VARCHAR(255) NOT NULL,
                    is_admin   TINYINT(1) DEFAULT 0,
                    verified   TINYINT(1) DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # OTP codes (email verification & 2FA)
            c.execute("""
                CREATE TABLE IF NOT EXISTS otp_codes (
                    id         INT AUTO_INCREMENT PRIMARY KEY,
                    email      VARCHAR(255) NOT NULL,
                    code       VARCHAR(10)  NOT NULL,
                    used       TINYINT(1) DEFAULT 0,
                    expires_at DATETIME NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_email (email)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # Password reset tokens
            c.execute("""
                CREATE TABLE IF NOT EXISTS password_reset_tokens (
                    id         INT AUTO_INCREMENT PRIMARY KEY,
                    email      VARCHAR(255) NOT NULL,
                    token      VARCHAR(128) UNIQUE NOT NULL,
                    used       TINYINT(1) DEFAULT 0,
                    expires_at DATETIME NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_token (token),
                    INDEX idx_email (email)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # Songs  — cover_s3_key stores the S3 key for the cover image
            c.execute("""
                CREATE TABLE IF NOT EXISTS songs (
                    id            INT AUTO_INCREMENT PRIMARY KEY,
                    user_id       INT NOT NULL,
                    title         VARCHAR(255) NOT NULL,
                    artist        VARCHAR(255) NOT NULL DEFAULT 'Unknown Artist',
                    album         VARCHAR(255),
                    genre         VARCHAR(100),
                    s3_key        VARCHAR(512) NOT NULL,
                    cover_s3_key  VARCHAR(512),
                    duration      FLOAT,
                    file_size     BIGINT DEFAULT 0,
                    play_count    INT DEFAULT 0,
                    uploaded_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_artist (artist),
                    INDEX idx_genre  (genre)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # Add cover_s3_key column if it doesn't exist (migration for old installs)
            try:
                c.execute("""
                    ALTER TABLE songs ADD COLUMN cover_s3_key VARCHAR(512) AFTER s3_key
                """)
            except Exception:
                pass  # column already exists

            # Favorites
            c.execute("""
                CREATE TABLE IF NOT EXISTS favorites (
                    id         INT AUTO_INCREMENT PRIMARY KEY,
                    user_id    INT NOT NULL,
                    song_id    INT NOT NULL,
                    added_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uq_user_song (user_id, song_id),
                    INDEX idx_user_id (user_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            # Recently played — per-user history (last 50 per user)
            c.execute("""
                CREATE TABLE IF NOT EXISTS recently_played (
                    id         INT AUTO_INCREMENT PRIMARY KEY,
                    user_id    INT NOT NULL,
                    song_id    INT NOT NULL,
                    played_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user_played (user_id, played_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

        print("Database initialised OK")
    except Exception as e:
        print(f"init_db error: {e}")
    finally:
        conn.close()

# ─── S3 ────────────────────────────────────────────────────────────────────────
def get_s3():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT"),
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        region_name=os.environ.get("S3_REGION")
    )

def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()

def generate_otp():
    return ''.join(random.choices(string.digits, k=6))

# ─── AUTH DECORATORS ───────────────────────────────────────────────────────────
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'error': 'Token missing'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            request.user_id    = data['user_id']
            request.user_email = data['email']
            request.is_admin   = data.get('is_admin', False)
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired. Please log in again.'}), 401
        except Exception:
            return jsonify({'error': 'Invalid token'}), 401
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'error': 'Token missing'}), 401
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=['HS256'])
            request.user_id    = data['user_id']
            request.user_email = data['email']
            request.is_admin   = data.get('is_admin', False)
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired'}), 401
        except Exception:
            return jsonify({'error': 'Invalid token'}), 401
        if not request.is_admin:
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated

# ─── EMAIL HELPER ──────────────────────────────────────────────────────────────
def _email_base(content):
    return f"""
    <div style="font-family:'Helvetica Neue',Arial,sans-serif;max-width:520px;margin:auto;
                background:#111;color:#e2e8f0;border-radius:16px;overflow:hidden;">
      <div style="background:linear-gradient(135deg,#1db954,#17a349);padding:32px 40px;">
        <h1 style="margin:0;font-size:26px;font-weight:900;letter-spacing:-0.5px;color:#fff;">
          🎧 ListenMe
        </h1>
        <p style="margin:6px 0 0;font-size:13px;color:rgba(255,255,255,0.75);">
          Your personal music streaming space
        </p>
      </div>
      <div style="padding:36px 40px;">{content}</div>
      <div style="padding:20px 40px;border-top:1px solid #222;font-size:12px;color:#555;">
        ListenMe · Secure · Private · Yours
      </div>
    </div>
    """

# ═══════════════════════════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/auth/signup', methods=['POST'])
def signup():
    data     = request.get_json()
    email    = data.get('email', '').strip().lower()
    password = data.get('password', '')
    name     = data.get('name', '').strip()

    if not email or not password or not name:
        return jsonify({'error': 'All fields are required'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400

    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id FROM users WHERE email=%s", (email,))
            if c.fetchone():
                return jsonify({'error': 'This email is already registered'}), 409

            hashed   = hash_password(password)
            is_admin = (email == ADMIN_EMAIL) if ADMIN_EMAIL else False
            c.execute(
                "INSERT INTO users (email, password, name, is_admin, verified) VALUES (%s,%s,%s,%s,FALSE)",
                (email, hashed, name, is_admin)
            )
            otp     = generate_otp()
            expires = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
            c.execute("INSERT INTO otp_codes (email, code, expires_at) VALUES (%s,%s,%s)",
                      (email, otp, expires))

        content = f"""
          <p style="font-size:18px;font-weight:700;margin:0 0 8px;">Welcome to ListenMe, {name}! 🎶</p>
          <p style="color:#94a3b8;margin:0 0 28px;">Please verify your email to start listening.</p>
          <div style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;
                      padding:28px;text-align:center;margin-bottom:28px;">
            <p style="color:#64748b;margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:2px;">
              Verification Code
            </p>
            <div style="font-size:42px;font-weight:900;letter-spacing:14px;color:#1db954;">{otp}</div>
          </div>
          <p style="color:#475569;font-size:13px;">Expires in 10 minutes.</p>
        """
        msg = Message('Verify your ListenMe account', recipients=[email])
        msg.html = _email_base(content)
        mail.send(msg)
        return jsonify({'message': 'Check your email for a verification code.'}), 201
    finally:
        conn.close()


@app.route('/api/auth/verify-email', methods=['POST'])
def verify_email():
    data  = request.get_json()
    email = data.get('email', '').strip().lower()
    code  = data.get('code', '').strip()

    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT id FROM otp_codes
                WHERE email=%s AND code=%s AND used=FALSE AND expires_at > NOW()
                ORDER BY created_at DESC LIMIT 1
            """, (email, code))
            otp_row = c.fetchone()
            if not otp_row:
                return jsonify({'error': 'Invalid or expired code'}), 400

            c.execute("UPDATE otp_codes SET used=TRUE WHERE id=%s", (otp_row['id'],))
            c.execute("UPDATE users SET verified=TRUE WHERE email=%s", (email,))
            c.execute("SELECT id, email, name, is_admin FROM users WHERE email=%s", (email,))
            user = c.fetchone()

        token = jwt.encode({
            'user_id':  user['id'],
            'email':    user['email'],
            'is_admin': bool(user['is_admin']),
            'exp':      datetime.datetime.utcnow() + datetime.timedelta(days=30)
        }, app.config['SECRET_KEY'], algorithm='HS256')

        return jsonify({
            'token': token,
            'user':  {'id': user['id'], 'email': user['email'],
                      'name': user['name'], 'is_admin': bool(user['is_admin'])}
        }), 200
    finally:
        conn.close()


@app.route('/api/auth/resend-otp', methods=['POST'])
def resend_otp():
    data  = request.get_json()
    email = data.get('email', '').strip().lower()
    if not email:
        return jsonify({'error': 'Email is required'}), 400

    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id, name, verified FROM users WHERE email=%s", (email,))
            user = c.fetchone()
            if not user:
                return jsonify({'error': 'Email not found'}), 404
            if user['verified']:
                return jsonify({'error': 'Email already verified'}), 400

            otp     = generate_otp()
            expires = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
            c.execute("UPDATE otp_codes SET used=TRUE WHERE email=%s", (email,))
            c.execute("INSERT INTO otp_codes (email, code, expires_at) VALUES (%s,%s,%s)",
                      (email, otp, expires))

        content = f"""
          <p style="font-size:18px;font-weight:700;margin:0 0 8px;">New verification code</p>
          <div style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;
                      padding:28px;text-align:center;margin-bottom:28px;">
            <p style="color:#64748b;margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:2px;">
              Verification Code
            </p>
            <div style="font-size:42px;font-weight:900;letter-spacing:14px;color:#1db954;">{otp}</div>
          </div>
          <p style="color:#475569;font-size:13px;">Expires in 10 minutes.</p>
        """
        msg = Message('Your new ListenMe verification code', recipients=[email])
        msg.html = _email_base(content)
        mail.send(msg)
        return jsonify({'message': 'New code sent.'}), 200
    finally:
        conn.close()


@app.route('/api/auth/login', methods=['POST'])
def login():
    data     = request.get_json()
    email    = data.get('email', '').strip().lower()
    password = data.get('password', '')

    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id, email, name, password, verified, is_admin FROM users WHERE email=%s", (email,))
            user = c.fetchone()
            if not user or user['password'] != hash_password(password):
                return jsonify({'error': 'Invalid email or password'}), 401

            if not user['verified']:
                # Re-send OTP
                otp     = generate_otp()
                expires = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
                c.execute("UPDATE otp_codes SET used=TRUE WHERE email=%s", (email,))
                c.execute("INSERT INTO otp_codes (email, code, expires_at) VALUES (%s,%s,%s)",
                          (email, otp, expires))
                content = f"""
                  <p style="font-size:18px;font-weight:700;margin:0 0 8px;">Verify your account</p>
                  <div style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;
                              padding:28px;text-align:center;margin-bottom:28px;">
                    <div style="font-size:42px;font-weight:900;letter-spacing:14px;color:#1db954;">{otp}</div>
                  </div>
                  <p style="color:#475569;font-size:13px;">Expires in 10 minutes.</p>
                """
                msg = Message('Verify your ListenMe account', recipients=[email])
                msg.html = _email_base(content)
                try:
                    mail.send(msg)
                except Exception:
                    pass
                return jsonify({'error': 'verify_required', 'email': email}), 403

            # Update admin flag from env in case it changed
            is_admin_now = (email == ADMIN_EMAIL) if ADMIN_EMAIL else bool(user['is_admin'])
            if is_admin_now != bool(user['is_admin']):
                c.execute("UPDATE users SET is_admin=%s WHERE id=%s", (is_admin_now, user['id']))

            token = jwt.encode({
                'user_id':  user['id'],
                'email':    user['email'],
                'is_admin': is_admin_now,
                'exp':      datetime.datetime.utcnow() + datetime.timedelta(days=30)
            }, app.config['SECRET_KEY'], algorithm='HS256')

            return jsonify({
                'token': token,
                'user':  {'id': user['id'], 'email': user['email'],
                          'name': user['name'], 'is_admin': is_admin_now}
            }), 200
    finally:
        conn.close()


@app.route('/api/auth/me', methods=['GET'])
@token_required
def me():
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id, email, name, is_admin, created_at FROM users WHERE id=%s",
                      (request.user_id,))
            user = c.fetchone()
            if not user:
                return jsonify({'error': 'User not found'}), 404
            user['is_admin']   = bool(user['is_admin'])
            user['created_at'] = user['created_at'].isoformat() if user['created_at'] else None
            return jsonify({'user': user}), 200
    finally:
        conn.close()

# ═══════════════════════════════════════════════════════════════════════════════
#  PASSWORD RESET
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/auth/forgot-password', methods=['POST'])
def forgot_password():
    data  = request.get_json()
    email = data.get('email', '').strip().lower()
    if not email:
        return jsonify({'error': 'Email is required'}), 400

    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id, name FROM users WHERE email=%s AND verified=TRUE", (email,))
            user = c.fetchone()
            if user:
                reset_token = str(uuid.uuid4()).replace('-','') + str(uuid.uuid4()).replace('-','')
                expires     = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
                c.execute("UPDATE password_reset_tokens SET used=TRUE WHERE email=%s", (email,))
                c.execute(
                    "INSERT INTO password_reset_tokens (email, token, expires_at) VALUES (%s,%s,%s)",
                    (email, reset_token, expires)
                )
                app_url    = os.environ.get('APP_URL', '').rstrip('/')
                reset_link = f"{app_url}/reset-password.html?token={reset_token}"

                content = f"""
                  <p style="font-size:18px;font-weight:700;margin:0 0 8px;">Reset your password</p>
                  <p style="color:#94a3b8;margin:0 0 28px;">
                    Hi {user['name']}, click below to reset your password.
                  </p>
                  <div style="text-align:center;margin:32px 0;">
                    <a href="{reset_link}"
                       style="background:#1db954;color:#000;
                              padding:16px 40px;border-radius:50px;text-decoration:none;
                              font-weight:700;font-size:16px;display:inline-block;">
                      Reset My Password →
                    </a>
                  </div>
                  <p style="color:#475569;font-size:13px;">
                    This link expires in <strong style="color:#e2e8f0;">1 hour</strong>.<br/>
                    If you didn't request this, just ignore this email.
                  </p>
                """
                msg = Message('Reset your ListenMe password', recipients=[email])
                msg.html = _email_base(content)
                mail.send(msg)
        return jsonify({'message': 'If this email is registered, a reset link has been sent.'}), 200
    finally:
        conn.close()


@app.route('/api/auth/reset-password', methods=['POST'])
def reset_password():
    data         = request.get_json()
    token        = data.get('token', '').strip()
    new_password = data.get('password', '')
    if not token or not new_password:
        return jsonify({'error': 'Token and new password are required'}), 400
    if len(new_password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400

    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT id, email FROM password_reset_tokens
                WHERE token=%s AND used=FALSE AND expires_at > NOW()
            """, (token,))
            token_row = c.fetchone()
            if not token_row:
                return jsonify({'error': 'This reset link is invalid or has expired.'}), 400

            c.execute("UPDATE users SET password=%s WHERE email=%s",
                      (hash_password(new_password), token_row['email']))
            c.execute("UPDATE password_reset_tokens SET used=TRUE WHERE id=%s", (token_row['id'],))
        return jsonify({'message': 'Password reset successful. You can now log in.'}), 200
    finally:
        conn.close()


@app.route('/api/auth/verify-reset-token', methods=['POST'])
def verify_reset_token():
    data  = request.get_json()
    token = data.get('token', '').strip()
    if not token:
        return jsonify({'valid': False}), 400
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT id FROM password_reset_tokens
                WHERE token=%s AND used=FALSE AND expires_at > NOW()
            """, (token,))
            return jsonify({'valid': bool(c.fetchone())}), 200
    finally:
        conn.close()

# ═══════════════════════════════════════════════════════════════════════════════
#  SONGS — ADMIN UPLOAD  (cover image fix is here)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/songs/upload', methods=['POST'])
@admin_required
def upload_song():
    if 'audio' not in request.files:
        return jsonify({'error': 'Audio file is required'}), 400

    audio_file = request.files['audio']
    title      = request.form.get('title', 'Untitled').strip()
    artist     = request.form.get('artist', 'Unknown Artist').strip()
    album      = request.form.get('album', '').strip()
    genre      = request.form.get('genre', '').strip()

    allowed_audio = {'mp3', 'wav', 'ogg', 'flac', 'm4a', 'aac'}
    ext = audio_file.filename.rsplit('.', 1)[-1].lower() if '.' in audio_file.filename else ''
    if ext not in allowed_audio:
        return jsonify({'error': f'Unsupported audio format. Allowed: {", ".join(allowed_audio)}'}), 400

    s3        = get_s3()
    audio_key = f"songs/{uuid.uuid4()}.{ext}"

    try:
        audio_file.seek(0, 2)
        file_size = audio_file.tell()
        audio_file.seek(0)
        s3.upload_fileobj(
            audio_file, S3_BUCKET, audio_key,
            ExtraArgs={'ContentType': f'audio/{ext}', 'ContentDisposition': 'inline'}
        )
    except NoCredentialsError:
        return jsonify({'error': 'AWS credentials error — check S3 config'}), 500
    except Exception as e:
        return jsonify({'error': f'Audio upload failed: {str(e)}'}), 500

    # ── Cover image upload ─────────────────────────────────────────────────────
    cover_key = None
    cover_file = request.files.get('cover')
    if cover_file and cover_file.filename:
        c_ext = cover_file.filename.rsplit('.', 1)[-1].lower() if '.' in cover_file.filename else ''
        allowed_img = {'jpg', 'jpeg', 'png', 'webp', 'gif'}
        if c_ext in allowed_img:
            cover_key = f"covers/{uuid.uuid4()}.{c_ext}"
            content_type_map = {
                'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                'png': 'image/png',  'webp': 'image/webp', 'gif': 'image/gif'
            }
            try:
                s3.upload_fileobj(
                    cover_file, S3_BUCKET, cover_key,
                    ExtraArgs={
                        'ContentType': content_type_map.get(c_ext, 'image/jpeg'),
                        'ContentDisposition': 'inline'
                    }
                )
            except Exception as e:
                # Cover upload failed — don't block the whole upload
                cover_key = None
                print(f"Cover upload warning: {e}")

    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO songs (user_id, title, artist, album, genre, s3_key, cover_s3_key, file_size)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (request.user_id, title, artist, album, genre, audio_key, cover_key, file_size))
            song_id = c.lastrowid
        return jsonify({
            'message': 'Song uploaded successfully!',
            'song_id': song_id,
            'has_cover': cover_key is not None
        }), 201
    finally:
        conn.close()


def _presign_songs(songs):
    """Add audio_url and cover_url (pre-signed S3 URLs) to a list of song dicts."""
    s3 = get_s3()
    for song in songs:
        try:
            song['audio_url'] = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET, 'Key': song['s3_key']},
                ExpiresIn=3600
            )
        except Exception:
            song['audio_url'] = None

        song['cover_url'] = None
        if song.get('cover_s3_key'):
            try:
                song['cover_url'] = s3.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': S3_BUCKET, 'Key': song['cover_s3_key']},
                    ExpiresIn=3600
                )
            except Exception:
                song['cover_url'] = None

        if song.get('uploaded_at') and not isinstance(song['uploaded_at'], str):
            song['uploaded_at'] = song['uploaded_at'].isoformat()
    return songs


@app.route('/api/songs', methods=['GET'])
@token_required
def get_songs():
    artist_filter = request.args.get('artist', '').strip()
    conn = get_db()
    try:
        with conn.cursor() as c:
            if artist_filter:
                c.execute("""
                    SELECT s.id, s.title, s.artist, s.album, s.genre,
                           s.s3_key, s.cover_s3_key, s.duration, s.file_size,
                           s.play_count, s.uploaded_at
                    FROM songs s
                    WHERE s.artist = %s
                    ORDER BY s.title ASC
                """, (artist_filter,))
            else:
                c.execute("""
                    SELECT s.id, s.title, s.artist, s.album, s.genre,
                           s.s3_key, s.cover_s3_key, s.duration, s.file_size,
                           s.play_count, s.uploaded_at
                    FROM songs s
                    ORDER BY s.uploaded_at DESC
                """)
            songs = c.fetchall()

            # Get user's favorites for quick lookup
            c.execute("SELECT song_id FROM favorites WHERE user_id=%s", (request.user_id,))
            fav_ids = {row['song_id'] for row in c.fetchall()}

        songs = _presign_songs(songs)
        for song in songs:
            song['is_favorite'] = song['id'] in fav_ids
        return jsonify({'songs': songs}), 200
    finally:
        conn.close()


@app.route('/api/artists', methods=['GET'])
@token_required
def get_artists():
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT artist,
                       COUNT(*) AS song_count,
                       MAX(cover_s3_key) AS cover_s3_key
                FROM songs
                GROUP BY artist
                ORDER BY artist ASC
            """)
            artists = c.fetchall()

        s3 = get_s3()
        for a in artists:
            a['cover_url'] = None
            if a.get('cover_s3_key'):
                try:
                    a['cover_url'] = s3.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': S3_BUCKET, 'Key': a['cover_s3_key']},
                        ExpiresIn=3600
                    )
                except Exception:
                    pass
        return jsonify({'artists': artists}), 200
    finally:
        conn.close()


@app.route('/api/songs/favorites', methods=['GET'])
@token_required
def get_favorites():
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT s.id, s.title, s.artist, s.album, s.genre,
                       s.s3_key, s.cover_s3_key, s.duration, s.file_size,
                       s.play_count, s.uploaded_at
                FROM songs s
                JOIN favorites f ON f.song_id = s.id
                WHERE f.user_id = %s
                ORDER BY f.added_at DESC
            """, (request.user_id,))
            songs = c.fetchall()

        songs = _presign_songs(songs)
        for song in songs:
            song['is_favorite'] = True
        return jsonify({'songs': songs}), 200
    finally:
        conn.close()


@app.route('/api/songs/<int:song_id>/favorite', methods=['POST'])
@token_required
def add_favorite(song_id):
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("SELECT id FROM songs WHERE id=%s", (song_id,))
            if not c.fetchone():
                return jsonify({'error': 'Song not found'}), 404
            try:
                c.execute("INSERT INTO favorites (user_id, song_id) VALUES (%s,%s)",
                          (request.user_id, song_id))
            except pymysql.err.IntegrityError:
                pass  # already favorited
        return jsonify({'message': 'Added to favorites'}), 200
    finally:
        conn.close()


@app.route('/api/songs/<int:song_id>/favorite', methods=['DELETE'])
@token_required
def remove_favorite(song_id):
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM favorites WHERE user_id=%s AND song_id=%s",
                      (request.user_id, song_id))
        return jsonify({'message': 'Removed from favorites'}), 200
    finally:
        conn.close()


@app.route('/api/songs/<int:song_id>/play', methods=['POST'])
@token_required
def increment_play(song_id):
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("UPDATE songs SET play_count = play_count + 1 WHERE id=%s", (song_id,))
            # Track recently played
            c.execute("""
                INSERT INTO recently_played (user_id, song_id)
                VALUES (%s, %s)
            """, (request.user_id, song_id))
            # Keep only last 50 entries per user
            c.execute("""
                DELETE FROM recently_played
                WHERE user_id = %s
                  AND id NOT IN (
                    SELECT id FROM (
                        SELECT id FROM recently_played
                        WHERE user_id = %s
                        ORDER BY played_at DESC
                        LIMIT 50
                    ) t
                  )
            """, (request.user_id, request.user_id))
        return jsonify({'ok': True}), 200
    finally:
        conn.close()


@app.route('/api/songs/recently-played', methods=['GET'])
@token_required
def get_recently_played():
    """Return distinct recently played songs for the logged-in user (last 20)."""
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT s.id, s.title, s.artist, s.album, s.genre,
                       s.s3_key, s.cover_s3_key, s.duration, s.file_size,
                       s.play_count, s.uploaded_at
                FROM recently_played rp
                JOIN songs s ON s.id = rp.song_id
                WHERE rp.user_id = %s
                GROUP BY s.id
                ORDER BY MAX(rp.played_at) DESC
                LIMIT 20
            """, (request.user_id,))
            songs = c.fetchall()

            c.execute("SELECT song_id FROM favorites WHERE user_id=%s", (request.user_id,))
            fav_ids = {row['song_id'] for row in c.fetchall()}

        songs = _presign_songs(songs)
        for song in songs:
            song['is_favorite'] = song['id'] in fav_ids
        return jsonify({'songs': songs}), 200
    finally:
        conn.close()


@app.route('/api/songs/<int:song_id>', methods=['DELETE'])
@admin_required
def delete_song(song_id):
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("SELECT s3_key, cover_s3_key FROM songs WHERE id=%s", (song_id,))
            song = c.fetchone()
            if not song:
                return jsonify({'error': 'Song not found'}), 404
            s3 = get_s3()
            try:
                s3.delete_object(Bucket=S3_BUCKET, Key=song['s3_key'])
            except Exception:
                pass
            if song.get('cover_s3_key'):
                try:
                    s3.delete_object(Bucket=S3_BUCKET, Key=song['cover_s3_key'])
                except Exception:
                    pass
            c.execute("DELETE FROM favorites WHERE song_id=%s", (song_id,))
            c.execute("DELETE FROM recently_played WHERE song_id=%s", (song_id,))
            c.execute("DELETE FROM songs WHERE id=%s", (song_id,))
        return jsonify({'message': 'Song deleted'}), 200
    finally:
        conn.close()


@app.route('/api/songs/my', methods=['GET'])
@token_required
def my_songs():
    """Admin: all songs with cover URLs for library management."""
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT id, title, artist, album, genre, s3_key, cover_s3_key,
                       duration, file_size, play_count, uploaded_at
                FROM songs ORDER BY uploaded_at DESC
            """)
            songs = c.fetchall()
        songs = _presign_songs(songs)
        return jsonify({'songs': songs}), 200
    finally:
        conn.close()


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'app': 'ListenMe'}), 200


# ── Run init_db at startup (works with both gunicorn and direct python) ─────────
if not os.environ.get('APP_URL'):
    print("WARNING: APP_URL not set — password reset links will be broken!")
if not ADMIN_EMAIL:
    print("WARNING: ADMIN_EMAIL not set — no one can upload music!")
else:
    print(f"Admin email: {ADMIN_EMAIL}")

init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)