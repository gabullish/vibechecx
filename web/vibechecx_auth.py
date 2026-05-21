"""VibeChecx Auth — bcrypt passwords, no email, no resets"""
import os
import sys
import secrets
import psycopg2
import bcrypt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vibechecx_config import DB_CONFIG as DB  # noqa: E402

def hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password, stored):
    try:
        return bcrypt.checkpw(password.encode(), stored.encode())
    except (ValueError, Exception):
        pass
    if ":" in stored:
        parts = stored.split(":")
        if len(parts) == 2 and len(parts[1]) == 64:
            import hashlib
            return hashlib.sha256((parts[0] + password).encode()).hexdigest() == parts[1]
    return False

def register(username, password):
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                    (username, hash_password(password)))
        conn.commit()
        return True, None
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return False, "Username already taken"
    finally:
        conn.close()

def login(username, password):
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("SELECT id, password_hash FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None, "Invalid credentials"
    user_id, pw_hash = row
    if not verify_password(password, pw_hash):
        conn.close()
        return None, "Invalid credentials"
    if ":" in pw_hash or not pw_hash.startswith("$2b$"):
        new_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        cur.execute("UPDATE users SET password_hash=%s WHERE id=%s", (new_hash, user_id))
        conn.commit()
    conn.close()
    return user_id, None


def rotate_sessions(user_id):
    """Invalidate any prior sessions for this user. Called on fresh login."""
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
    conn.commit()
    conn.close()

def create_session(user_id):
    session_id = secrets.token_hex(32)
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO sessions (id, user_id, expires_at) VALUES (%s, %s, NOW() + INTERVAL '30 days')",
                (session_id, user_id))
    conn.commit()
    conn.close()
    return session_id

def get_user_from_session(session_id):
    if not session_id: return None
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT u.id, u.username, u.created_at
            FROM users u
            JOIN sessions s ON s.user_id = u.id
            WHERE s.id = %s AND s.expires_at > NOW()
        """, (session_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return None
        # is_admin column may not exist on all deployments (e.g. fresh Supabase).
        # Default to False when missing.
        try:
            cur.execute("SELECT is_admin FROM users WHERE id=%s", (row[0],))
            admin_row = cur.fetchone()
            is_admin = bool(admin_row[0]) if admin_row else False
        except Exception:
            is_admin = False
        conn.close()
        return {"id": row[0], "username": row[1], "created_at": row[2], "is_admin": is_admin}
    except Exception:
        conn.close()
        return None

def logout(session_id):
    if not session_id: return
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
    conn.commit()
    conn.close()
