import argparse
import builtins
import functools
import os
import random
import re
import signal
import sys
import time
from pathlib import Path

import oci
import requests
from dotenv import dotenv_values
from oci.config import validate_config
from oci.core import ComputeClient, VirtualNetworkClient

# 确保在各操作系统控制台（包括 Windows 默认 GBK 环境）输出 emoji 与中文字符时不触发编码错误
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Docker 收集 stdout/stderr，未连接 TTY 时立即刷新输出以确保日志实时可见
print = functools.partial(builtins.print, flush=True)

DIVIDER = "────────────────────────"

DEFAULT_CONFIG_DIR = os.getenv("OCI_ARM_CONFIG_DIR", "/opt/oci")
DEFAULT_DOTENV_PATH = os.getenv("OCI_ARM_DOTENV", str(Path(DEFAULT_CONFIG_DIR) / ".env"))
DEFAULT_OCI_CONFIG_PATH = os.getenv("OCI_ARM_OCI_CONFIG", str(Path(DEFAULT_CONFIG_DIR) / "config"))
DEFAULT_OCI_PROFILE = os.getenv("OCI_ARM_OCI_PROFILE", "DEFAULT")
DEFAULT_TF_PATH = os.getenv("OCI_ARM_TF_PATH", "main.tf")

_env_config = dotenv_values(DEFAULT_DOTENV_PATH)


def _to_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _fmt_num(value: float | int) -> str:
    """将整数浮点值格式化为无小数形式（如 4.0 -> '4'），保留非整数小数位。"""
    num = float(value)
    return str(int(num)) if num.is_integer() else str(value)


def _get_conf(key: str, default: str = "") -> str:
    """优先从 .env 读取配置，若不存在或为空则回退至系统环境变量。"""
    val = _env_config.get(key)
    if val is not None and str(val).strip() != "":
        return str(val).strip()
    env_val = os.getenv(key)
    if env_val is not None and env_val.strip() != "":
        return env_val.strip()
    return default


def _build_tg_url(api_host: str, bot_token: str) -> str:
    host = api_host.strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    return f"{host}/bot{bot_token}/sendMessage"


# Telegram 推送配置
USE_TG = _to_bool(_get_conf("USE_TG", "False"), default=False)
TG_BOT_TOKEN = _get_conf("TG_BOT_TOKEN", "")
TG_USER_ID = _get_conf("TG_USER_ID", "")
TG_API_HOST = _get_conf("TG_API_HOST", "api.telegram.org")


def telegram(text: str) -> None:
    if not USE_TG:
        return
    if not (TG_BOT_TOKEN and TG_USER_ID and TG_API_HOST):
        print("Telegram Bot 配置缺失，跳过推送")
        return

    url = _build_tg_url(TG_API_HOST, TG_BOT_TOKEN)
    payload = {
        "chat_id": TG_USER_ID,
        "text": f"🐢 甲骨文 ARM 抢注助手 🐢\n\n{text.strip()}",
    }
    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
    except Exception as exc:
        print(f"Telegram Bot 推送失败: {exc}")
    else:
        print("Telegram Bot 推送成功")


class OciUser:
    """OCI 用户配置与凭据解析类"""

    user: str | None
    fingerprint: str | None
    key_file: str | None
    key_content: str | None
    pass_phrase: str | None
    tenancy: str
    region: str

    def __init__(self, config_file: str = DEFAULT_OCI_CONFIG_PATH, profile: str = DEFAULT_OCI_PROFILE) -> None:
        cfg = oci.config.from_file(file_location=config_file, profile_name=profile)
        validate_config(cfg)
        self.parse(cfg)

    def parse(self, cfg: dict) -> None:
        print("正在解析 OCI 配置文件...")
        self._config = dict(cfg)
        self.user = cfg.get("user")
        self.fingerprint = cfg.get("fingerprint")
        self.key_file = cfg.get("key_file")
        self.key_content = cfg.get("key_content")
        self.pass_phrase = cfg.get("pass_phrase")
        self.tenancy = cfg["tenancy"]
        self.region = cfg["region"]
        print(f"OCI 配置解析成功 (区域: {self.region})")

    @property
    def config(self) -> dict:
        return dict(self._config)

    def compartment_id(self) -> str:
        return self.tenancy


class FileParser:
    """Terraform 配置文件 (main.tf) 解析类"""

    # 匹配引号字符串（支持转义字符）与裸值，兼容尾随逗号及 '#' / '//' 单行注释
    ASSIGNMENT_RE = re.compile(
        r'^\s*([A-Za-z0-9_".-]+)\s*=\s*(?:"((?:\\.|[^"\\])*)"|([^#/\r\n,]+?))\s*,?\s*(?:(?:#|//).*?)?$',
        re.MULTILINE,
    )
    _HCL_ESCAPES = {
        r"\n": "\n",
        r"\r": "\r",
        r"\t": "\t",
        r"\"": '"',
        r"\\": "\\",
    }

    def __init__(self, file_path: str) -> None:
        self.parse(file_path)

    def parse(self, file_path: str) -> None:
        resolved_path = self._resolve_path(file_path)
        try:
            print(f"正在读取 Terraform 配置文件: {resolved_path}")
            with open(resolved_path, "r", encoding="utf-8") as file_obj:
                content = file_obj.read()
        except OSError as exc:
            raise SystemExit(f"❌ main.tf 文件打开失败，请确认文件路径及读取权限: {exc}") from exc

        values = self._parse_assignments(content)
        self.compartment_id = self._required(values, "compartment_id")
        self.shape = self._optional_str(values, "shape", "VM.Standard.A1.Flex") or "VM.Standard.A1.Flex"
        self.memory_in_gbs = self._required_positive_float(values, "memory_in_gbs")
        self.ocpus = self._required_positive_float(values, "ocpus")
        self.availability_domain = self._required(values, "availability_domain")
        self.fault_domain = self._optional_str(values, "fault_domain", None)
        self.subnet_id = self._required(values, "subnet_id")
        self.display_name = self._required(values, "display_name").strip().replace(" ", "-")
        self.hostname_label = self._hostname_label(self.display_name)
        self.image_id = self._required(values, "source_id")
        self.boot_volume_size_in_gbs = self._optional_positive_int(values, "boot_volume_size_in_gbs", 50)
        self.assign_public_ip = self._optional_bool(values, "assign_public_ip", True)
        self.ssh_authorized_keys = self._required(values, "ssh_authorized_keys")

    @staticmethod
    def _resolve_path(file_path: str) -> str:
        if file_path and Path(file_path).is_file():
            return file_path
        alt_path = Path(DEFAULT_CONFIG_DIR) / "main.tf"
        if alt_path.is_file():
            return str(alt_path)
        return file_path

    @classmethod
    def _unescape_hcl(cls, text: str) -> str:
        return re.sub(r"\\[nrt\"\\]", lambda m: cls._HCL_ESCAPES.get(m.group(0), m.group(0)), text)

    @classmethod
    def _parse_assignments(cls, content: str) -> dict[str, list[str]]:
        values: dict[str, list[str]] = {}
        for match in cls.ASSIGNMENT_RE.finditer(content):
            clean_key = match.group(1).strip('"')
            quoted_val = match.group(2)
            if quoted_val is not None:
                clean_value = cls._unescape_hcl(quoted_val).strip()
            else:
                clean_value = match.group(3).strip()
            values.setdefault(clean_key, []).append(clean_value)
        return values

    @staticmethod
    def _required(values: dict[str, list[str]], key: str) -> str:
        try:
            val = values[key][-1].strip()
        except (KeyError, IndexError) as exc:
            raise ValueError(f"main.tf 缺少必要参数: {key}") from exc
        if not val:
            raise ValueError(f"main.tf 必要参数不能为空: {key}")
        return val

    @classmethod
    def _required_positive_float(cls, values: dict[str, list[str]], key: str) -> float:
        raw_value = cls._required(values, key)
        try:
            num = float(raw_value)
        except ValueError as exc:
            raise ValueError(f"main.tf 参数 {key} 必须是数字，当前值: {raw_value}") from exc
        if num <= 0:
            raise ValueError(f"main.tf 参数 {key} 必须大于 0，当前值: {raw_value}")
        return num

    @classmethod
    def _optional_positive_int(cls, values: dict[str, list[str]], key: str, default: int) -> int:
        if key not in values or not values[key][-1].strip():
            return default
        raw_value = cls._required(values, key)
        try:
            num = int(float(raw_value))
        except ValueError as exc:
            raise ValueError(f"main.tf 参数 {key} 必须是整数，当前值: {raw_value}") from exc
        if num <= 0:
            raise ValueError(f"main.tf 参数 {key} 必须大于 0，当前值: {raw_value}")
        return num

    @classmethod
    def _optional_str(cls, values: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
        if key not in values:
            return default
        val = values[key][-1].strip()
        return val if val else default

    @classmethod
    def _optional_bool(cls, values: dict[str, list[str]], key: str, default: bool) -> bool:
        if key not in values or not values[key][-1].strip():
            return default
        return _to_bool(cls._required(values, key), default=default)

    @staticmethod
    def _hostname_label(display_name: str) -> str:
        # OCI VNIC hostname_label 必须符合 DNS 规范：字母、数字、连字符，且长度不超过 63
        label = re.sub(r"[^A-Za-z0-9-]+", "-", display_name).strip("-").lower()
        label = re.sub(r"-+", "-", label)[:63].strip("-")
        return label or "oracle-arm"


class InsCreate:
    """OCI ARM 实例创建与循环抢注类"""

    def __init__(self, user: OciUser, file_path: str) -> None:
        self._user = user
        # 设置连接超时 10s、读取超时 30s，并显式关闭 SDK 内置重试与断路器，由脚本自主控制退避节奏
        self._client = ComputeClient(
            config=user.config,
            timeout=(10, 30),
            retry_strategy=oci.retry.NoneRetryStrategy(),
            circuit_breaker_strategy=oci.circuit_breaker.NoCircuitBreakerStrategy(),
        )
        self.tf = FileParser(file_path)
        self.shape = self.tf.shape
        self.sleep_time = random.uniform(3, 6)
        self.try_count = 0
        self._notification_chunks: list[str] = []
        self.instance_id: str | None = None
        self.public_ip: str | None = None

    @property
    def notification_text(self) -> str:
        return "\n".join(self._notification_chunks)

    def _spec_summary(self) -> str:
        ocpus = _fmt_num(self.tf.ocpus)
        mem = _fmt_num(self.tf.memory_in_gbs)
        return f"{ocpus} OCPU / {mem} GB 内存 / {self.tf.boot_volume_size_in_gbs} GB 引导卷"

    def create(self) -> None:
        start_lines = [
            "🚀 甲骨文 ARM 抢注任务启动",
            DIVIDER,
            f"📍 可用区域: {self.tf.availability_domain}",
        ]
        if self.tf.fault_domain:
            start_lines.append(f"🛡️ 故障域: {self.tf.fault_domain}")
        start_lines.extend([
            f"🖥️ 实例名称: {self.tf.display_name}",
            f"🧩 实例机型: {self.shape}",
            f"⚙️ 规格配置: {self._spec_summary()}",
            f"🌐 分配公网: {'是' if self.tf.assign_public_ip else '否'}",
            DIVIDER,
            "🤖 脚本已就绪，正在快马加鞭抢注中...",
        ])
        start_text = "\n".join(start_lines)
        print(start_text)
        telegram(start_text)

        while True:
            self.try_count += 1
            try:
                ins = self.launch_instance()
            except oci.exceptions.ServiceError as exc:
                self.handle_service_error(exc)
            except (
                oci.exceptions.RequestException,
                oci.exceptions.ConnectTimeout,
                requests.RequestException,
                ConnectionError,
                TimeoutError,
                OSError,
            ) as exc:
                print(f"⚠️ 网络连接波动或请求超时 (将自动重试): {exc}")
            else:
                success_text = "\n".join([
                    "🎉 甲骨文 ARM 实例抢注成功！",
                    DIVIDER,
                    f"🔢 尝试次数: 第 {self.try_count} 次尝试",
                    f"📍 可用区域: {self.tf.availability_domain}",
                    f"🖥️ 实例名称: {self.tf.display_name}",
                    f"⚙️ 规格配置: {self._spec_summary()}",
                ])
                self.logp(success_text)
                self.instance_id = ins.id
                self.check_public_ip()
                telegram(self.notification_text)
                break

            print(f"⏳ 抢注中，当前已尝试: {self.try_count} 次 (重试间隔: {self.sleep_time:.1f}s)")
            if self.try_count % 100 == 0:
                ocpus = _fmt_num(self.tf.ocpus)
                mem = _fmt_num(self.tf.memory_in_gbs)
                tg_progress = "\n".join([
                    "⏳ 抢注进度播报",
                    DIVIDER,
                    f"🔢 已尝试次数: {self.try_count} 次",
                    f"⏱️ 当前间隔: {self.sleep_time:.1f} 秒",
                    f"🖥️ 目标实例: {self.tf.display_name} ({ocpus}C / {mem}G)",
                ])
                telegram(tg_progress)

            time.sleep(self.sleep_time)

    def _abort_with_error(self, error_msg: str, exc: Exception) -> None:
        print(error_msg)
        telegram(error_msg)
        raise exc

    def handle_service_error(self, exc: oci.exceptions.ServiceError) -> None:
        code_lower = str(exc.code or "").lower()
        msg_lower = str(exc.message or "").lower()

        # 1. 触发速率限制：自动增加休眠时间退避（上限 60s）
        if exc.status == 429 or exc.code == "TooManyRequests":
            self.sleep_time = min(60.0, self.sleep_time + random.uniform(3, 6))
            print(f"⚠️ 触发速率限制 (429 TooManyRequests)，重试间隔已调整为 {self.sleep_time:.1f}s...")
        # 2. 容量不足（最常见的无机状态）：平滑恢复至较快的常态刷机频率
        elif self.is_capacity_error(exc):
            print("⏳ 暂无主机容量 (Out of host capacity)，继续快马加鞭抢注中...")
            if self.sleep_time > 6:
                self.sleep_time = max(3.0, self.sleep_time - random.uniform(2, 4))
        # 3. 认证失败：密钥或配置错误属于致命错误，立即提醒并停止
        elif exc.status == 401 or exc.code in {"NotAuthenticated", "InvalidSignature"}:
            self._abort_with_error(
                "\n".join([
                    "❌ OCI 认证失败 (HTTP 401)",
                    DIVIDER,
                    "请检查 config 中的 user / fingerprint / key_file / tenancy 是否配置正确。",
                    f"错误详情: {exc.message}",
                ]),
                exc,
            )
        # 4. 权限不足或资源不存在：通常是 IAM 权限缺失，或 subnet_id / source_id / compartment_id 错误
        elif exc.status in {403, 404} or exc.code in {"NotAuthorized", "NotAuthorizedOrNotFound", "NotFound"}:
            self._abort_with_error(
                "\n".join([
                    f"❌ 目标资源不存在或无权访问 (HTTP {exc.status} {exc.code})",
                    DIVIDER,
                    "请检查 main.tf 中的 source_id (镜像可能已下架)、subnet_id、compartment_id 或账号 IAM 权限。",
                    f"错误详情: {exc.message}",
                ]),
                exc,
            )
        # 5. 服务配额超限：说明实例已达到账号配额上限，无法继续开机，应立即停止
        elif exc.status == 400 and (
            "service limit" in msg_lower
            or "limitexceeded" in code_lower
            or "quotaexceeded" in code_lower
        ):
            self._abort_with_error(
                "\n".join([
                    "❌ 达到服务配额上限 (Limit / Quota Exceeded)",
                    DIVIDER,
                    "说明已成功创建机器或免费配额已满，请登录 OCI 控制台检查实例、引导卷占用情况。",
                    f"错误详情: {exc.message or exc}",
                ]),
                exc,
            )
        # 6. 参数无效：致命配置错误 (如内存/CPU比例非法、引导卷小于镜像要求等)，立即停止
        elif exc.status == 400 and exc.code in {"InvalidParameter", "InvalidParameterValue", "CannotParseRequest"}:
            self._abort_with_error(
                "\n".join([
                    f"❌ 请求参数无效 (HTTP 400 {exc.code})",
                    DIVIDER,
                    "请检查 main.tf 中的实例参数配置 (如 OCPU 与内存配比、引导卷大小等)。",
                    f"错误详情: {exc.message}",
                ]),
                exc,
            )
        # 7. 服务端临时波动 (502/503/504 网关错误)：仅终端记录并重试，避免频繁推送打扰
        elif exc.status in {502, 503, 504}:
            print(f"⚠️ OCI 服务端临时网关波动 (HTTP {exc.status})，稍后将自动重试...")
        # 8. 其他未知服务异常：终端记录详情并继续重试
        else:
            print(f"⚠️ 收到 OCI 服务异常返回: {exc.status} - {exc.code} - {exc.message}")

    @staticmethod
    def is_capacity_error(exc) -> bool:
        message = str(getattr(exc, "message", "") or "").lower()
        code = str(getattr(exc, "code", "") or "").lower()
        return exc.status in {400, 500, 503} and (
            "out of host capacity" in message
            or "out of capacity" in message
            or "outofhostcapacity" in code
            or "outofcapacity" in code
        )

    def check_public_ip(self) -> None:
        network_client = VirtualNetworkClient(
            config=self._user.config,
            timeout=(10, 30),
            retry_strategy=oci.retry.NoneRetryStrategy(),
            circuit_breaker_strategy=oci.circuit_breaker.NoCircuitBreakerStrategy(),
        )
        print("正在查询新实例的 VNIC 网络信息及 IP 地址...")
        target_compartment = self.tf.compartment_id or self._user.compartment_id()
        max_attempts = 30  # 最多轮询 30 次（约 150 秒）等待 VNIC 与公网 IP 就绪
        last_private_ip: str | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                attachments = self._client.list_vnic_attachments(
                    compartment_id=target_compartment,
                    instance_id=self.instance_id,
                ).data
                vnic_id = next(
                    (att.vnic_id for att in attachments if getattr(att, "vnic_id", None)),
                    None,
                )
                if not vnic_id:
                    print(f"[{attempt}/{max_attempts}] 实例网络正在初始化 (VNIC 挂载中)，等待 5 秒...")
                else:
                    vnic = network_client.get_vnic(vnic_id).data
                    public_ip = vnic.public_ip
                    private_ip = vnic.private_ip
                    if private_ip:
                        last_private_ip = private_ip

                    if self.tf.assign_public_ip and not public_ip:
                        print(f"[{attempt}/{max_attempts}] VNIC 已就绪，公网 IP 正在分配中，等待 5 秒...")
                    else:
                        public_ip_display = public_ip if public_ip else "未启用 (assign_public_ip=false)"
                        ip_info = "\n".join([
                            DIVIDER,
                            f"🌐 公网 IP: {public_ip_display}",
                            f"🔒 内网 IP: {private_ip or '未知'}",
                            DIVIDER,
                            "🐢 抢注任务圆满完成，脚本已安全停止，感谢使用！😄",
                        ])
                        self.logp(ip_info)
                        self.public_ip = public_ip
                        return
            except Exception as exc:
                print(f"[{attempt}/{max_attempts}] 查询 VNIC 信息出现短暂异常: {exc}")

            if attempt < max_attempts:
                time.sleep(5)

        warn_lines = [DIVIDER]
        if last_private_ip:
            warn_lines.append(f"🔒 内网 IP: {last_private_ip}")
        warn_lines.extend([
            "⚠️ 实例已创建成功，但轮询超时未能获取到完整的公网 IP 信息。",
            "请登录甲骨文云控制台查看实例运行状态与网络分配。",
            DIVIDER,
        ])
        self.logp("\n".join(warn_lines))

    def launch_instance(self):
        launch_kwargs = {
            "display_name": self.tf.display_name,
            "compartment_id": self.tf.compartment_id,
            "shape": self.shape,
            "shape_config": oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=self.tf.ocpus,
                memory_in_gbs=self.tf.memory_in_gbs,
            ),
            "availability_domain": self.tf.availability_domain,
            "create_vnic_details": oci.core.models.CreateVnicDetails(
                subnet_id=self.tf.subnet_id,
                hostname_label=self.tf.hostname_label,
                assign_public_ip=self.tf.assign_public_ip,
            ),
            "source_details": oci.core.models.InstanceSourceViaImageDetails(
                image_id=self.tf.image_id,
                boot_volume_size_in_gbs=self.tf.boot_volume_size_in_gbs,
            ),
            "metadata": {"ssh_authorized_keys": self.tf.ssh_authorized_keys},
            "is_pv_encryption_in_transit_enabled": True,
        }
        if self.tf.fault_domain:
            launch_kwargs["fault_domain"] = self.tf.fault_domain

        launch_details = oci.core.models.LaunchInstanceDetails(**launch_kwargs)
        return self._client.launch_instance(launch_details).data

    def logp(self, text: str) -> None:
        clean_text = text.strip("\r\n")
        print(clean_text)
        if USE_TG:
            self._notification_chunks.append(clean_text)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Oracle Cloud ARM VM 抢注脚本")
    parser.add_argument("main_tf", nargs="?", default=DEFAULT_TF_PATH, help="main.tf 文件路径，默认: %(default)s")
    return parser.parse_args(argv)


def _handle_stop_signal(signum, _frame) -> None:
    sig_name = signal.Signals(signum).name
    raise SystemExit(f"🛑 收到系统停止信号 ({sig_name})，抢注脚本已安全退出。")


def main(argv=None) -> None:
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_stop_signal)

    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        user = OciUser()
        ins = InsCreate(user, args.main_tf)
        ins.create()
    except KeyboardInterrupt as exc:
        raise SystemExit("\n🛑 用户中断运行，抢注脚本已安全退出。") from exc
    except (
        oci.exceptions.ConfigFileNotFound,
        oci.exceptions.ProfileNotFound,
        oci.exceptions.InvalidConfig,
        oci.exceptions.InvalidKeyFilePath,
        oci.exceptions.InvalidPrivateKey,
        oci.exceptions.MissingPrivateKeyPassphrase,
        ValueError,
    ) as exc:
        raise SystemExit(f"❌ 配置或凭据校验失败: {exc}") from exc


if __name__ == "__main__":
    main()
