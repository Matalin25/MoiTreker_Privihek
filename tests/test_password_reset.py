"""Run: python -m unittest discover -s tests -v (no network or real user data)."""
import importlib
import os
import smtplib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash
from password_reset import digest, send_reset_email, delivery_failure_reason, EmailConfigurationError


class PasswordResetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {
            'HABITFLOW_DB': str(Path(cls.temp.name) / 'test.db'),
            'TELEGRAM_BOT_TOKEN': '', 'HABITFLOW_ENABLE_SCHEDULER': '0',
        })
        cls.env.start()
        cls.module = importlib.import_module('app')
        cls.app = cls.module.app
        cls.app.config.update(TESTING=True, SECRET_KEY='test-secret')

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.client = self.app.test_client()
        with self.app.app_context():
            db = self.module.get_db()
            for table in ['password_resets', 'password_reset_requests', 'users']:
                db.execute('DELETE FROM ' + table)
            db.execute('INSERT INTO users(email, password_hash, created_at) VALUES (?, ?, ?)',
                       ('test@example.com', generate_password_hash('old-password'), '2026-11-15'))
            db.commit()
        self.mail = patch('password_reset.send_reset_email')
        self.send = self.mail.start()
        self.addCleanup(self.mail.stop)

    def csrf(self):
        with self.client.session_transaction() as session:
            return session['password_reset_csrf']

    def request_reset(self, email='test@example.com'):
        self.client.get('/forgot-password')
        return self.client.post('/forgot-password', data={'email': email, 'csrf_token': self.csrf()})

    def issue_token(self, token='test-token', expiry=None):
        with self.app.app_context():
            db = self.module.get_db()
            uid = db.execute('SELECT id FROM users').fetchone()[0]
            db.execute('INSERT INTO password_resets VALUES (?, ?, ?, 0)',
                       (digest(token), uid, expiry if expiry is not None else int(time.time()) + 1800))
            db.commit()
        return '/reset-password/' + token

    def test_pages_and_delivery(self):
        self.assertIn(b'/forgot-password', self.client.get('/login').data)
        self.assertEqual(self.request_reset().status_code, 302)
        self.send.assert_called_once()
        recipient, token = self.send.call_args.args
        self.assertEqual(recipient, 'test@example.com')
        with self.app.app_context():
            row = self.module.get_db().execute('SELECT token_hash FROM password_resets').fetchone()
            self.assertEqual(row[0], digest(token))
            self.assertNotEqual(row[0], token)

    def test_unknown_email_same_response(self):
        known = self.request_reset()
        self.send.reset_mock()
        unknown = self.request_reset('unknown@example.com')
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.location, unknown.location)
        self.send.assert_not_called()

    def test_rate_limit(self):
        self.request_reset()
        self.request_reset()
        self.send.assert_called_once()

    def test_csrf(self):
        self.assertEqual(self.client.post('/forgot-password', data={'email': 'test@example.com'}).status_code, 400)
        self.send.assert_not_called()

    def test_reset_one_time_and_login(self):
        path = self.issue_token()
        other = self.issue_token('another-token')
        response = self.client.get(path)
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        data = {'password': 'new-password', 'password_confirm': 'new-password', 'csrf_token': self.csrf()}
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertEqual(self.client.get(path).status_code, 400)
        self.assertEqual(self.client.get(other).status_code, 400)
        self.assertEqual(self.client.post('/login', data={'email': 'test@example.com', 'password': 'old-password'}).status_code, 200)
        self.assertEqual(self.client.post('/login', data={'email': 'test@example.com', 'password': 'new-password'}).status_code, 302)

    def test_expired_and_invalid(self):
        self.assertEqual(self.client.get(self.issue_token(expiry=0)).status_code, 400)
        self.assertEqual(self.client.get('/reset-password/invalid').status_code, 400)

    def test_validation_keeps_password(self):
        path = self.issue_token()
        self.client.get(path)
        for password, confirmation, csrf in [('short', 'short', self.csrf()),
                                              ('new-password', 'different', self.csrf()),
                                              ('new-password', 'new-password', 'invalid')]:
            self.client.post(path, data={'password': password, 'password_confirm': confirmation, 'csrf_token': csrf})
        with self.app.app_context():
            row = self.module.get_db().execute('SELECT password_hash FROM users').fetchone()
            self.assertTrue(check_password_hash(row[0], 'old-password'))

    def test_delivery_failure_does_not_expose_account(self):
        self.send.side_effect = RuntimeError('smtp failure')
        self.assertEqual(self.request_reset().status_code, 302)
        with self.app.app_context():
            self.assertEqual(self.module.get_db().execute('SELECT COUNT(*) FROM password_resets').fetchone()[0], 0)

    def test_smtp_tls(self):
        with patch.dict(os.environ, {'HABITFLOW_PUBLIC_URL': 'https://example.com',
                                    'SMTP_HOST': 'smtp.example.com', 'SMTP_PORT': '587',
                                    'SMTP_FROM': 'sender@example.com', 'SMTP_USERNAME': 'sender',
                                    'SMTP_PASSWORD': 'secret', 'SMTP_SECURITY': 'starttls'}):
            with patch('password_reset.smtplib.SMTP') as smtp:
                send_reset_email('test@example.com', 'random-token')
                connection = smtp.return_value.__enter__.return_value
                connection.starttls.assert_called_once()
                connection.login.assert_called_once_with('sender', 'secret')
                message = connection.send_message.call_args.args[0]
                self.assertIn('https://example.com/reset-password/random-token', message.get_content())

    def test_safe_delivery_diagnostics(self):
        cases = [
            (smtplib.SMTPAuthenticationError(535, b'sensitive response'), ('SMTP_AUTH_FAILED', 535)),
            (TimeoutError('sensitive host'), ('SMTP_TIMEOUT', None)),
            (ConnectionRefusedError('sensitive host'), ('SMTP_CONNECTION_FAILED', None)),
            (ValueError('sensitive configuration'), ('SMTP_CONFIG_INVALID', None)),
        ]
        for error, expected in cases:
            with self.subTest(category=expected[0]):
                self.assertEqual(delivery_failure_reason(error), expected)

    def test_exact_configuration_diagnostics(self):
        config = {'HABITFLOW_PUBLIC_URL': 'https://example.com', 'SMTP_HOST': 'smtp.mail.ru',
                  'SMTP_FROM': 'test@example.com', 'SMTP_PORT': '465', 'SMTP_SECURITY': 'ssl'}
        cases = [('HABITFLOW_PUBLIC_URL', '', 'CONFIG_HABITFLOW_PUBLIC_URL_INVALID'),
                 ('SMTP_HOST', '', 'CONFIG_SMTP_HOST_MISSING'),
                 ('SMTP_FROM', '', 'CONFIG_SMTP_FROM_MISSING'),
                 ('SMTP_FROM', 'invalid', 'CONFIG_SMTP_FROM_INVALID'),
                 ('SMTP_SECURITY', 'invalid', 'CONFIG_SMTP_SECURITY_INVALID'),
                 ('SMTP_PORT', 'invalid', 'CONFIG_SMTP_PORT_INVALID'),
                 ('SMTP_PORT', '99999', 'CONFIG_SMTP_PORT_INVALID')]
        for name, value, reason in cases:
            with self.subTest(setting=name, reason=reason):
                with patch.dict(os.environ, {**config, name: value}):
                    with self.assertRaises(EmailConfigurationError) as error:
                        send_reset_email('test@example.com', 'token')
                    self.assertEqual(delivery_failure_reason(error.exception), (reason, None))
