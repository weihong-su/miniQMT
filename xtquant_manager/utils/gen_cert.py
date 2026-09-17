"""
自签证书生成工具

为局域网部署生成绑定到指定 IP 的自签 TLS 证书。

Usage:
    python xtquant_manager/utils/gen_cert.py --ip 192.168.1.100 --out certs/
    python xtquant_manager/utils/gen_cert.py --ip 192.168.1.100 --days 3650 --out certs/
"""
import argparse
import datetime
import ipaddress
import os
import sys


def generate_self_signed_cert(
    ip: str,
    out_dir: str = "certs",
    days: int = 3650,
    cert_name: str = "server.crt",
    key_name: str = "server.key",
) -> tuple:
    """
    生成绑定到指定 IP 的自签 TLS 证书（SAN 扩展）。

    Args:
        ip: 绑定的 IP 地址，如 "192.168.1.100"
        out_dir: 证书输出目录
        days: 有效期（天）
        cert_name: 证书文件名
        key_name: 私钥文件名

    Returns:
        (cert_path: str, key_path: str)

    Raises:
        ImportError: 未安装 cryptography 库
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("需要安装 cryptography 库: pip install cryptography>=42.0.0")
        sys.exit(1)

    # 生成 RSA 私钥
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # 证书主题
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "XtQuantManager"),
        x509.NameAttribute(NameOID.COMMON_NAME, ip),
    ])

    # 构建证书
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(
            datetime.datetime.utcnow() + datetime.timedelta(days=days)
        )
        .add_extension(
            x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.IPv4Address(ip)),
                x509.DNSName("localhost"),
            ]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .sign(private_key, hashes.SHA256())
    )

    # 写入文件
    os.makedirs(out_dir, exist_ok=True)
    cert_path = os.path.join(out_dir, cert_name)
    key_path = os.path.join(out_dir, key_name)

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    with open(key_path, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))

    print(f"证书已生成:")
    print(f"  证书: {cert_path}")
    print(f"  私钥: {key_path}")
    print(f"  绑定 IP: {ip}")
    print(f"  有效期: {days} 天")
    print()
    print("启动服务示例:")
    print(f"  XtQuantServer(host='{ip}', port=8888,")
    print(f"                ssl_certfile='{cert_path}',")
    print(f"                ssl_keyfile='{key_path}')")

    return cert_path, key_path


def main():
    parser = argparse.ArgumentParser(
        description="为 XtQuantManager 生成自签 TLS 证书"
    )
    parser.add_argument(
        "--ip", required=True,
        help="绑定的局域网 IP 地址，如 192.168.1.100"
    )
    parser.add_argument(
        "--out", default="certs",
        help="证书输出目录（默认: certs/）"
    )
    parser.add_argument(
        "--days", type=int, default=3650,
        help="有效期（天，默认: 3650 = 10年）"
    )
    parser.add_argument(
        "--cert-name", default="server.crt",
        help="证书文件名（默认: server.crt）"
    )
    parser.add_argument(
        "--key-name", default="server.key",
        help="私钥文件名（默认: server.key）"
    )
    args = parser.parse_args()

    # 验证 IP 地址格式
    try:
        ipaddress.IPv4Address(args.ip)
    except ValueError:
        print(f"错误: 无效的 IPv4 地址: {args.ip}")
        sys.exit(1)

    generate_self_signed_cert(
        ip=args.ip,
        out_dir=args.out,
        days=args.days,
        cert_name=args.cert_name,
        key_name=args.key_name,
    )


if __name__ == "__main__":
    main()
