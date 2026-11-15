"""Одноразовые ссылки восстановления пароля, доставка через SMTP."""
import hashlib
import os
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from urllib.parse import urlsplit

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

RESET_SCHEMA = """
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS password_reset_requests (
    email_hash TEXT NOT NULL,
    ip_hash TEXT NOT NULL,
    requested_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reset_requests_time ON password_reset_requests(requested_at);
"""


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class EmailConfigurationError(ValueError):
    """Безопасный код ошибки настройки, не содержащий её значение."""


def delivery_failure_reason(error):
    """Только безопасная категория и числовой код, без текста ответа SMTP."""
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return 'SMTP_AUTH_FAILED', error.smtp_code
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return 'SMTP_RECIPIENT_REFUSED', None
    if isinstance(error, smtplib.SMTPSenderRefused):
        return 'SMTP_SENDER_REFUSED', error.smtp_code
    if isinstance(error, ssl.SSLError):
        return 'SMTP_TLS_FAILED', None
    if isinstance(error, TimeoutError):
        return 'SMTP_TIMEOUT', None
    if isinstance(error, smtplib.SMTPResponseException):
        return 'SMTP_SERVER_REJECTED', error.smtp_code
    if isinstance(error, smtplib.SMTPException):
        return 'SMTP_PROTOCOL_FAILED', None
    if isinstance(error, OSError):
        return 'SMTP_CONNECTION_FAILED', None
    if isinstance(error, EmailConfigurationError):
        return str(error), None
    if isinstance(error, UnicodeEncodeError):
        return 'SMTP_ENCODING_INVALID', None
    if isinstance(error, ValueError):
        return 'SMTP_CONFIG_INVALID', None
    return 'SMTP_UNEXPECTED_ERROR', None


def send_reset_email(recipient, token):
    base = os.environ.get('HABITFLOW_PUBLIC_URL', '').strip().rstrip('/')
    try:
        parsed = urlsplit(base)
    except ValueError:
        raise EmailConfigurationError('CONFIG_HABITFLOW_PUBLIC_URL_INVALID') from None
    if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment:
        raise EmailConfigurationError('CONFIG_HABITFLOW_PUBLIC_URL_INVALID')
    host = os.environ.get('SMTP_HOST', '').strip()
    sender = os.environ.get('SMTP_FROM', '').strip()
    if not host:
        raise EmailConfigurationError('CONFIG_SMTP_HOST_MISSING')
    if not sender:
        raise EmailConfigurationError('CONFIG_SMTP_FROM_MISSING')
    if any(c in sender for c in '\r\n') or '@' not in sender:
        raise EmailConfigurationError('CONFIG_SMTP_FROM_INVALID')
    mode = os.environ.get('SMTP_SECURITY', 'starttls').strip().lower()
    if mode not in ('ssl', 'starttls'):
        raise EmailConfigurationError('CONFIG_SMTP_SECURITY_INVALID')
    try:
        port = int(os.environ.get('SMTP_PORT', '465' if mode == 'ssl' else '587').strip())
    except ValueError:
        raise EmailConfigurationError('CONFIG_SMTP_PORT_INVALID') from None
    if not 1 <= port <= 65535:
        raise EmailConfigurationError('CONFIG_SMTP_PORT_INVALID')
    message = EmailMessage()
    message['Subject'] = 'HabitFlow — восстановление пароля'
    message['From'] = sender
    message['To'] = recipient
    message.set_content(
        'Чтобы установить новый пароль HabitFlow, откройте ссылку:\n\n'
        f'{base}/reset-password/{token}\n\n'
        'Ссылка действует 30 минут и может быть использована один раз.\n'
        'Если вы не запрашивали восстановление, проигнорируйте письмо.'
    )
    context = ssl.create_default_context()
    if mode == 'ssl':
        connection = smtplib.SMTP_SSL(host, port, timeout=15, context=context)
    else:
        connection = smtplib.SMTP(host, port, timeout=15)
    with connection as smtp:
        if mode == 'starttls':
            smtp.starttls(context=context)
        username = os.environ.get('SMTP_USERNAME', '').strip()
        if username:
            smtp.login(username, os.environ.get('SMTP_PASSWORD', ''))
        smtp.send_message(message)


def register_password_reset(app, get_db):
    bp = Blueprint('password_reset', __name__)

    def csrf_valid():
        expected = session.get('password_reset_csrf', '')
        supplied = request.form.get('csrf_token', '')
        return bool(expected and secrets.compare_digest(expected.encode('utf-8'), supplied.encode('utf-8')))

    def csrf_token():
        if 'password_reset_csrf' not in session:
            session['password_reset_csrf'] = secrets.token_urlsafe(32)
        return session['password_reset_csrf']

    @bp.route('/forgot-password', methods=['GET', 'POST'])
    def forgot_password():
        if request.method == 'POST':
            if not csrf_valid():
                return render_template('forgot_password.html', csrf_token=csrf_token(),
                                       error='Обновите страницу и попробуйте снова.'), 400
            email = (request.form.get('email') or '').strip().lower()
            db = get_db()
            now = int(time.time())
            email_hash = digest(email)
            # Не доверяем X-Forwarded-For без настройки доверенного прокси.
            ip_hash = digest(request.remote_addr or '')
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM password_reset_requests WHERE requested_at < ?', (now - 900,))
            db.execute('DELETE FROM password_resets WHERE expires_at < ?', (now,))
            count = db.execute('SELECT COUNT(*) FROM password_reset_requests WHERE ip_hash = ?',
                               (ip_hash,)).fetchone()[0]
            recent = db.execute('SELECT 1 FROM password_reset_requests WHERE email_hash = ? '
                                'AND requested_at > ?', (email_hash, now - 60)).fetchone()
            allowed = count < 5 and not recent and 3 <= len(email) <= 254 and '@' in email
            user = None
            if allowed:
                db.execute('INSERT INTO password_reset_requests VALUES (?, ?, ?)',
                           (email_hash, ip_hash, now))
                user = db.execute('SELECT id, email FROM users WHERE email = ?', (email,)).fetchone()
                if user:
                    token = secrets.token_urlsafe(32)
                    db.execute('INSERT INTO password_resets(token_hash, user_id, expires_at) VALUES (?, ?, ?)',
                               (digest(token), user['id'], now + 1800))
            db.commit()
            if user:
                try:
                    send_reset_email(user['email'], token)
                except Exception as error:
                    db.execute('DELETE FROM password_resets WHERE token_hash = ?', (digest(token),))
                    db.commit()
                    # Исключения SMTP могут содержать адреса и секреты.
                    reason, code = delivery_failure_reason(error)
                    app.logger.error('Password reset email delivery failed: %s (code=%s)', reason, code)
            flash('Если аккаунт с таким email существует, мы отправили ссылку для восстановления. '
                  'Проверьте почту и папку «Спам». Повторить запрос можно через минуту.', 'success')
            return redirect(url_for('password_reset.forgot_password'))
        return render_template('forgot_password.html', csrf_token=csrf_token())

    @bp.route('/reset-password/<token>', methods=['GET', 'POST'])
    def reset_password(token):
        db = get_db()
        now = int(time.time())
        hashed = digest(token)
        row = db.execute('SELECT user_id FROM password_resets WHERE token_hash = ? '
                         'AND used = 0 AND expires_at > ?', (hashed, now)).fetchone()
        if not row:
            return render_template('reset_password.html', invalid=True), 400
        error = None
        if request.method == 'POST':
            password = request.form.get('password') or ''
            if not csrf_valid():
                error = 'Обновите страницу и попробуйте снова.'
            elif not 6 <= len(password) <= 1024:
                error = 'Пароль должен содержать от 6 до 1024 символов.'
            elif password != request.form.get('password_confirm'):
                error = 'Пароли не совпадают.'
            else:
                password_hash = generate_password_hash(password)
                db.execute('BEGIN IMMEDIATE')
                changed = db.execute('UPDATE password_resets SET used = 1 WHERE token_hash = ? '
                                     'AND used = 0 AND expires_at > ?',
                                     (hashed, int(time.time()))).rowcount
                if not changed:
                    db.rollback()
                    return render_template('reset_password.html', invalid=True), 400
                db.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                           (password_hash, row['user_id']))
                db.execute('UPDATE password_resets SET used = 1 WHERE user_id = ?', (row['user_id'],))
                db.commit()
                session.clear()
                flash('Пароль изменён. Войдите с новым паролем.', 'success')
                return redirect(url_for('login'))
        return render_template('reset_password.html', csrf_token=csrf_token(), error=error)

    @bp.after_request
    def protect_response(response):
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        return response

    app.register_blueprint(bp)
