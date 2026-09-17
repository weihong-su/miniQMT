"""
Phase 4 测试：security.py
"""
import sys
import time
import unittest

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from xtquant_manager.security import (
    SecurityConfig,
    TokenBucket,
    verify_api_key,
    generate_hmac_signature,
    generate_hmac_headers,
    verify_hmac_signature,
)


# ---------------------------------------------------------------------------
# TokenBucket 测试
# ---------------------------------------------------------------------------

class TestTokenBucket(unittest.TestCase):
    def test_allow_within_rate(self):
        """在速率限制内允许请求"""
        bucket = TokenBucket(rate_per_minute=60)
        # 初始桶满，应允许多次请求
        for _ in range(10):
            self.assertTrue(bucket.allow("192.168.1.1"))

    def test_deny_when_exhausted(self):
        """超过速率时拒绝请求"""
        # rate=1 每分钟 1 次
        bucket = TokenBucket(rate_per_minute=1)
        self.assertTrue(bucket.allow("192.168.1.2"))   # 第 1 次
        self.assertFalse(bucket.allow("192.168.1.2"))  # 第 2 次被拒绝

    def test_different_ips_independent(self):
        """不同 IP 的桶相互独立"""
        bucket = TokenBucket(rate_per_minute=1)
        self.assertTrue(bucket.allow("1.1.1.1"))  # IP1 第 1 次
        self.assertFalse(bucket.allow("1.1.1.1")) # IP1 第 2 次
        self.assertTrue(bucket.allow("2.2.2.2"))  # IP2 仍可以

    def test_rate_zero_always_allow(self):
        """rate=0 时不限速"""
        bucket = TokenBucket(rate_per_minute=0)
        for _ in range(1000):
            self.assertTrue(bucket.allow("any"))

    def test_token_refill_over_time(self):
        """等待一段时间后令牌被补充"""
        bucket = TokenBucket(rate_per_minute=120)  # 每秒 2 个令牌
        # 消耗所有令牌
        while bucket.allow("refill_test"):
            pass
        # 等待 1 秒后应该补充了 ~2 个令牌
        time.sleep(1.0)
        self.assertTrue(bucket.allow("refill_test"))

    def test_concurrent_safety(self):
        """并发请求不崩溃"""
        import threading
        bucket = TokenBucket(rate_per_minute=1000)
        results = []
        lock = threading.Lock()

        def worker():
            result = bucket.allow("concurrent")
            with lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 所有结果都是合法的 bool
        self.assertEqual(len(results), 50)
        self.assertTrue(all(isinstance(r, bool) for r in results))


# ---------------------------------------------------------------------------
# API Key 验证测试
# ---------------------------------------------------------------------------

class TestVerifyApiKey(unittest.TestCase):
    LOCAL_IPS = ["127.0.0.1", "::1", "localhost"]

    def test_local_ip_always_allowed(self):
        """本机 IP 无需 token"""
        for ip in self.LOCAL_IPS:
            with self.subTest(ip=ip):
                ok, reason = verify_api_key(
                    token="", expected="secret123",
                    client_ip=ip, local_ips=self.LOCAL_IPS
                )
                self.assertTrue(ok)

    def test_remote_ip_with_correct_token(self):
        ok, reason = verify_api_key(
            token="secret123", expected="secret123",
            client_ip="192.168.1.100", local_ips=self.LOCAL_IPS
        )
        self.assertTrue(ok)

    def test_remote_ip_with_wrong_token(self):
        ok, reason = verify_api_key(
            token="wrong_token", expected="secret123",
            client_ip="192.168.1.100", local_ips=self.LOCAL_IPS
        )
        self.assertFalse(ok)

    def test_remote_ip_no_token_configured(self):
        """服务端未配置 token，非本机访问被拒绝"""
        ok, reason = verify_api_key(
            token="", expected="",  # 空 token 配置
            client_ip="192.168.1.100", local_ips=self.LOCAL_IPS
        )
        self.assertFalse(ok)

    def test_remote_ip_empty_token_provided(self):
        """客户端提供空 token，非本机访问被拒绝"""
        ok, reason = verify_api_key(
            token="", expected="secret123",
            client_ip="10.0.0.1", local_ips=self.LOCAL_IPS
        )
        self.assertFalse(ok)

    def test_none_token_treated_as_empty(self):
        """None token 不崩溃"""
        ok, reason = verify_api_key(
            token=None, expected="secret123",
            client_ip="192.168.1.1", local_ips=self.LOCAL_IPS
        )
        self.assertFalse(ok)

    def test_reason_provided_on_failure(self):
        ok, reason = verify_api_key(
            token="wrong", expected="secret",
            client_ip="192.168.1.1", local_ips=self.LOCAL_IPS
        )
        self.assertFalse(ok)
        self.assertIsInstance(reason, str)
        self.assertGreater(len(reason), 0)

    def test_timing_safe_comparison(self):
        """同长度的不同 token 比较不崩溃（时间恒定）"""
        for wrong_token in ["xxxxxx", "secret", "ABCDEF"]:
            ok, _ = verify_api_key(
                token=wrong_token, expected="123456",
                client_ip="192.168.1.1", local_ips=self.LOCAL_IPS
            )
            self.assertFalse(ok)


# ---------------------------------------------------------------------------
# HMAC 签名测试
# ---------------------------------------------------------------------------

class TestHmacSignature(unittest.TestCase):
    SECRET = "test_hmac_secret_12345"

    def test_generate_and_verify(self):
        """生成签名并验证通过"""
        timestamp = int(time.time())
        sig = generate_hmac_signature(
            "GET", "/api/v1/health", timestamp, "", self.SECRET
        )
        ok, reason = verify_hmac_signature(
            "GET", "/api/v1/health",
            str(timestamp), "", sig, self.SECRET
        )
        self.assertTrue(ok)

    def test_wrong_secret_fails(self):
        timestamp = int(time.time())
        sig = generate_hmac_signature("GET", "/api/v1/health", timestamp, "", self.SECRET)
        ok, _ = verify_hmac_signature(
            "GET", "/api/v1/health",
            str(timestamp), "", sig, "wrong_secret"
        )
        self.assertFalse(ok)

    def test_wrong_path_fails(self):
        timestamp = int(time.time())
        sig = generate_hmac_signature("GET", "/api/v1/health", timestamp, "", self.SECRET)
        ok, _ = verify_hmac_signature(
            "GET", "/api/v1/metrics",  # 不同路径
            str(timestamp), "", sig, self.SECRET
        )
        self.assertFalse(ok)

    def test_timestamp_expired(self):
        """过期时间戳被拒绝"""
        old_timestamp = int(time.time()) - 600  # 10分钟前
        sig = generate_hmac_signature("GET", "/api/v1/health", old_timestamp, "", self.SECRET)
        ok, reason = verify_hmac_signature(
            "GET", "/api/v1/health",
            str(old_timestamp), "", sig, self.SECRET,
            tolerance=300
        )
        self.assertFalse(ok)
        self.assertIn("过期", reason)

    def test_future_timestamp_within_tolerance(self):
        """时间差在容忍范围内通过"""
        timestamp = int(time.time()) + 60  # 1分钟后（在 5 分钟容忍内）
        sig = generate_hmac_signature("GET", "/api/v1/health", timestamp, "", self.SECRET)
        ok, _ = verify_hmac_signature(
            "GET", "/api/v1/health",
            str(timestamp), "", sig, self.SECRET,
            tolerance=300
        )
        self.assertTrue(ok)

    def test_invalid_timestamp_format(self):
        """无效时间戳格式返回失败"""
        ok, reason = verify_hmac_signature(
            "GET", "/api/v1/health",
            "not_a_number", "", "sig", self.SECRET
        )
        self.assertFalse(ok)
        self.assertIn("格式", reason)

    def test_missing_headers_fail(self):
        ok, reason = verify_hmac_signature(
            "GET", "/api/v1/health",
            "", "", "", self.SECRET  # 空时间戳和签名
        )
        self.assertFalse(ok)

    def test_generate_hmac_headers(self):
        """generate_hmac_headers 返回正确格式"""
        headers = generate_hmac_headers("POST", "/api/v1/accounts", self.SECRET, body="{}")
        self.assertIn("X-Timestamp", headers)
        self.assertIn("X-Signature", headers)
        self.assertIsInstance(int(headers["X-Timestamp"]), int)

    def test_body_content_in_signature(self):
        """请求体内容影响签名"""
        timestamp = int(time.time())
        sig1 = generate_hmac_signature("POST", "/test", timestamp, '{"a":1}', self.SECRET)
        sig2 = generate_hmac_signature("POST", "/test", timestamp, '{"a":2}', self.SECRET)
        self.assertNotEqual(sig1, sig2)

    def test_method_in_signature(self):
        """HTTP 方法影响签名"""
        timestamp = int(time.time())
        sig_get = generate_hmac_signature("GET", "/test", timestamp, "", self.SECRET)
        sig_post = generate_hmac_signature("POST", "/test", timestamp, "", self.SECRET)
        self.assertNotEqual(sig_get, sig_post)


# ---------------------------------------------------------------------------
# SecurityConfig 测试
# ---------------------------------------------------------------------------

class TestSecurityConfig(unittest.TestCase):
    def test_default_config(self):
        config = SecurityConfig()
        self.assertEqual(config.api_token, "")
        self.assertEqual(config.allowed_ips, [])
        self.assertEqual(config.rate_limit, 60)
        self.assertFalse(config.enable_hmac)

    def test_custom_config(self):
        config = SecurityConfig(
            api_token="secret",
            allowed_ips=["192.168.1.0/24"],
            rate_limit=120,
        )
        self.assertEqual(config.api_token, "secret")
        self.assertEqual(config.rate_limit, 120)

    def test_local_ips_default(self):
        config = SecurityConfig()
        self.assertIn("127.0.0.1", config.local_ips)
        self.assertIn("::1", config.local_ips)


if __name__ == "__main__":
    unittest.main(verbosity=2)
