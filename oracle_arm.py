import argparse
import builtins
import functools
import os
import random
import re
import sys
import time
from pathlib import Path

import oci
import requests
from dotenv import dotenv_values
from oci.config import validate_config
from oci.core import ComputeClient, VirtualNetworkClient

# Docker 收集 stdout/stderr，但未连接 TTY 时 Python 可能缓冲输出。
# 立即刷新输出以确保 `docker logs` 实时可见，同时避免将高频重试日志频繁发送至 Telegram。
print = functools.partial(builtins.print, flush=True)

DEFAULT_CONFIG_DIR = os.getenv("OCI_ARM_CONFIG_DIR", "/opt/oci")
DEFAULT_DOTENV_PATH = os.getenv("OCI_ARM_DOTENV", str(Path(DEFAULT_CONFIG_DIR) / ".env"))
DEFAULT_OCI_CONFIG_PATH = os.getenv("OCI_ARM_OCI_CONFIG", str(Path(DEFAULT_CONFIG_DIR) / "config"))
DEFAULT_OCI_PROFILE = os.getenv("OCI_ARM_OCI_PROFILE", "DEFAULT")
DEFAULT_TF_PATH = os.getenv("OCI_ARM_TF_PATH", "main.tf")

_env_config = dotenv_values(DEFAULT_DOTENV_PATH)


def _to_bool(value, default=False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _get_conf(key: str, default: str = "") -> str:
    """优先从 .env 读取配置，若不存在或为空则回退至系统环境变量。"""
    val = _env_config.get(key)
    if val is not None and str(val).strip() != "":
        return str(val).strip()
    return os.getenv(key, default)


# Telegram 推送配置
USE_TG = _to_bool(_get_conf("USE_TG", "False"), default=False)
TG_BOT_TOKEN = _get_conf("TG_BOT_TOKEN", "")
TG_USER_ID = _get_conf("TG_USER_ID", "")
TG_API_HOST = _get_conf("TG_API_HOST", "api.telegram.org")


def telegram(desp: str) -> None:
    if not USE_TG:
        return
    if not (TG_BOT_TOKEN and TG_USER_ID and TG_API_HOST):
        print("Telegram Bot 配置缺失，跳过推送")
        return

    url = f"https://{TG_API_HOST}/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_USER_ID,
        "text": f"🐢 甲骨文 ARM 抢注助手 🐢\n\n{desp}",
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

    user: str
    fingerprint: str
    key_file: str
    tenancy: str
    region: str

    def __init__(self, configfile=DEFAULT_OCI_CONFIG_PATH, profile=DEFAULT_OCI_PROFILE):
        cfg = oci.config.from_file(file_location=configfile, profile_name=profile)
        validate_config(cfg)
        self.parse(cfg)

    def parse(self, cfg) -> None:
        print("正在解析 OCI 配置文件...")
        self._config = dict(cfg)
        self.user = cfg["user"]
        self.fingerprint = cfg["fingerprint"]
        self.key_file = cfg.get("key_file")
        self.key_content = cfg.get("key_content")
        self.pass_phrase = cfg.get("pass_phrase")
        self.tenancy = cfg["tenancy"]
        self.region = cfg["region"]
        print(f"OCI 配置解析成功 (区域: {self.region})")

    @property
    def config(self) -> dict:
        return dict(self._config)

    def keys(self):
        # 保留 pass_phrase/key_content 等可选字段，确保加密 PEM 密钥等认证方式正常
        return tuple(key for key in self._config if self._config.get(key) is not None)

    def __getitem__(self, item):
        if item in self._config:
            return self._config[item]
        return getattr(self, item)

    def compartment_id(self) -> str:
        return self.tenancy


class FileParser:
    """Terraform 配置文件 (main.tf) 解析类"""

    # 正确匹配引号字符串与裸值，同时安全兼容 '#' 与 '//' 两种单行注释风格
    ASSIGNMENT_RE = re.compile(
        r'^\s*([A-Za-z0-9_".-]+)\s*=\s*(?:"([^"]*)"|([^#/\r\n]+?))\s*(?:(?:#|//).*?)?$',
        re.MULTILINE,
    )

    def __init__(self, file_path: str) -> None:
        self.parse(file_path)

    def parse(self, file_path: str) -> None:
        try:
            print(f"正在读取 Terraform 配置文件: {file_path}")
            with open(file_path, "r", encoding="utf-8") as file_obj:
                content = file_obj.read()
        except OSError as exc:
            raise SystemExit(f"main.tf 文件打开失败，请确认文件路径及读取权限: {exc}") from exc

        values = self._parse_assignments(content)
        self.compartment_id = self._required(values, "compartment_id")
        self.memory_in_gbs = self._required_float(values, "memory_in_gbs")
        self.ocpus = self._required_float(values, "ocpus")
        self.availability_domain = self._required(values, "availability_domain")
        self.subnet_id = self._required(values, "subnet_id")
        self.display_name = self._required(values, "display_name").strip().replace(" ", "-")
        self.hostname_label = self._hostname_label(self.display_name)
        self.image_id = self._required(values, "source_id")
        self.boot_volume_size_in_gbs = self._optional_float(values, "boot_volume_size_in_gbs", 50.0)
        self.assign_public_ip = self._optional_bool(values, "assign_public_ip", True)
        self.ssh_authorized_keys = self._required(values, "ssh_authorized_keys")

    def parser(self, file_path: str) -> None:
        """兼容旧方法名 parser"""
        self.parse(file_path)

    @classmethod
    def _parse_assignments(cls, content: str) -> dict:
        values = {}
        for key, q_val, raw_val in cls.ASSIGNMENT_RE.findall(content):
            clean_key = key.strip('"')
            clean_value = q_val if q_val != "" else raw_val
            clean_value = clean_value.strip().rstrip(",").strip()
            values.setdefault(clean_key, []).append(clean_value)
        return values

    @staticmethod
    def _required(values: dict, key: str) -> str:
        try:
            return values[key][-1]
        except (KeyError, IndexError) as exc:
            raise ValueError(f"main.tf 缺少必要参数: {key}") from exc

    @classmethod
    def _required_float(cls, values: dict, key: str) -> float:
        raw_value = cls._required(values, key)
        try:
            return float(raw_value)
        except ValueError as exc:
            raise ValueError(f"main.tf 参数 {key} 必须是数字，当前值: {raw_value}") from exc

    @classmethod
    def _optional_float(cls, values: dict, key: str, default: float) -> float:
        if key not in values:
            return default
        return cls._required_float(values, key)

    @classmethod
    def _optional_bool(cls, values: dict, key: str, default: bool) -> bool:
        if key not in values:
            return default
        return _to_bool(cls._required(values, key), default=default)

    @staticmethod
    def _hostname_label(display_name: str) -> str:
        # OCI VNIC hostname labels 必须符合 DNS 规范：字母、数字、连字符，长度不超过 63
        label = re.sub(r"[^A-Za-z0-9-]+", "-", display_name).strip("-").lower()
        label = re.sub(r"-+", "-", label)[:63].strip("-")
        return label or "oracle-arm"

    # 兼容历史拼写错误属性 compoartment_id，确保旧代码及外部调用 100% 兼容
    @property
    def compoartment_id(self) -> str:
        return self.compartment_id

    @compoartment_id.setter
    def compoartment_id(self, cid: str) -> None:
        self.compartment_id = cid


class InsCreate:
    """OCI ARM 实例创建与循环抢注类"""

    shape = "VM.Standard.A1.Flex"

    def __init__(self, user: OciUser, filepath: str) -> None:
        self._user = user
        self._client = ComputeClient(config=user.config)
        self.tf = FileParser(filepath)
        self.sleep_time = random.uniform(3, 6)
        self.try_count = 0
        self.desp = ""
        self.ins_id = None
        self.public_ip = None

    def create(self) -> None:
        start_text = (
            "🚀 甲骨文 ARM 抢注任务启动 🚀\n"
            "--------------------------------\n"
            f"📍 可用区域: {self.tf.availability_domain}\n"
            f"🖥️ 实例名称: {self.tf.display_name}\n"
            f"⚙️ 规格配置: {self.tf.ocpus} OCPU / {self.tf.memory_in_gbs} GB 内存 / {self.tf.boot_volume_size_in_gbs} GB 引导卷\n"
            f"🌐 分配公网: {'是' if self.tf.assign_public_ip else '否'}\n"
            "--------------------------------\n"
            "🤖 脚本已就绪，正在快马加鞭抢注中..."
        )
        print(start_text)
        telegram(start_text)

        while True:
            try:
                ins = self.launch_instance()
            except oci.exceptions.ServiceError as exc:
                self.handle_service_error(exc)
                time.sleep(self.sleep_time)
            except (oci.exceptions.RequestException, requests.RequestException, ConnectionError, TimeoutError, OSError) as exc:
                # 捕获网络连接抖动或瞬时断开，避免长周期容器挂机崩溃，同时避免网络抖动时狂轰 Telegram
                print(f"⚠️ 网络连接波动或请求超时 (将自动重试): {exc}")
                time.sleep(self.sleep_time)
            else:
                success_text = (
                    "🎉 甲骨文 ARM 实例抢注成功！ 🎉\n"
                    "--------------------------------\n"
                    f"🔢 尝试次数: 第 {self.try_count + 1} 次尝试\n"
                    f"📍 可用区域: {self.tf.availability_domain}\n"
                    f"🖥️ 实例名称: {self.tf.display_name}\n"
                    f"⚙️ 规格配置: {self.tf.ocpus} OCPU / {self.tf.memory_in_gbs} GB 内存 / {self.tf.boot_volume_size_in_gbs} GB 引导卷\n"
                )
                self.logp(success_text)
                self.ins_id = ins.id
                self.check_public_ip()
                telegram(self.desp)
                break
            finally:
                self.try_count += 1
                count_text = f"⏳ 抢注中，当前已尝试: {self.try_count} 次 (当前重试间隔: {self.sleep_time:.1f}s)"
                print(count_text)
                if self.try_count % 100 == 0:
                    tg_progress = (
                        "⏳ 抢注进度播报\n"
                        "--------------------------------\n"
                        f"🔢 已尝试次数: {self.try_count} 次\n"
                        f"⏱️ 当前请求间隔: {self.sleep_time:.1f} 秒\n"
                        f"🖥️ 目标实例: {self.tf.display_name} ({self.tf.ocpus}C / {self.tf.memory_in_gbs}G)"
                    )
                    telegram(tg_progress)

    def handle_service_error(self, exc: oci.exceptions.ServiceError) -> None:
        # 1. 触发速率限制：自动增加休眠时间退避
        if exc.status == 429 or exc.code == "TooManyRequests":
            print("⚠️ 触发速率限制 (429 TooManyRequests)，正在自动延长重试间隔...")
            if self.sleep_time < 60:
                self.sleep_time += random.uniform(3, 6)
        # 2. 容量不足（最常见的无机状态）：平滑恢复至较快的常态刷机频率
        elif self.is_capacity_error(exc):
            print("⏳ 暂无主机容量 (Out of host capacity)，继续快马加鞭抢注中...")
            if self.sleep_time > 6:
                self.sleep_time = max(3.0, self.sleep_time - random.uniform(2, 4))
        # 3. 认证失败：密钥或配置错误属于致命错误，必须立即提醒并停止
        elif exc.status == 401 or exc.code in {"NotAuthenticated", "InvalidSignature"}:
            error_msg = (
                "❌ OCI 认证失败 (HTTP 401)！\n"
                "请检查 config 中的 user/fingerprint/key_file/tenancy 是否配置正确。\n"
                f"错误详情: {exc.message}"
            )
            self.logp(error_msg)
            telegram(error_msg)
            raise exc
        # 4. 服务配额超限：说明实例已达到账号配额上限，无法继续开机，应停止
        elif exc.status == 400 and ("service limit" in str(exc.message).lower() or "limitexceeded" in str(exc.code).lower()):
            error_msg = (
                "❌ 达到服务配额上限 (Service Limit Exceeded)！\n"
                "说明已刷到机器或资源配额不足，请登录 OCI 后台检查 CPU、内存、引导卷占用并释放资源。\n"
                f"错误详情: {exc}"
            )
            self.logp(error_msg)
            telegram(error_msg)
            raise exc
        # 5. 服务端临时波动 (502/503/504 等网关错误)：仅终端输出并重试，不轰炸 Telegram
        elif exc.status in {502, 503, 504}:
            print(f"⚠️ OCI 服务端临时网络/网关波动 (HTTP {exc.status})，稍后将自动重试...")
        # 6. 其他未知服务异常：终端记录详情并继续重试
        else:
            print(f"⚠️ 收到 OCI 服务异常返回: {exc.status} - {exc.code} - {exc.message}")

    @staticmethod
    def is_capacity_error(exc) -> bool:
        message = str(getattr(exc, "message", "")).lower()
        code = str(getattr(exc, "code", "")).lower()
        return (
            exc.status in {400, 500}
            and ("out of host capacity" in message or "outofhostcapacity" in code or "out of capacity" in message)
        )

    def check_public_ip(self) -> None:
        network_client = VirtualNetworkClient(config=self._user.config)
        print("正在查询新实例的 VNIC 网络信息及 IP 地址...")
        max_attempts = 30  # 最多等待约 150 秒以确保公网 IP 分配就绪
        for attempt in range(max_attempts):
            try:
                attachments = self._client.list_vnic_attachments(
                    compartment_id=self._user.compartment_id(), instance_id=self.ins_id
                )
                data = attachments.data
                if data:
                    vnic_id = data[0].vnic_id
                    vnic = network_client.get_vnic(vnic_id).data
                    public_ip = vnic.public_ip
                    private_ip = vnic.private_ip

                    # 若配置分配公网 IP 但公网 IP 尚未就绪，继续轮询等待分配完成
                    if self.tf.assign_public_ip and not public_ip:
                        print(f"[{attempt + 1}/{max_attempts}] VNIC 已就绪，公网 IP 正在分配中，等待 5 秒后重试...")
                        time.sleep(5)
                        continue

                    ip_info = (
                        "--------------------------------\n"
                        f"🌐 公网 IP: {public_ip or '未分配 (assign_public_ip=False 或分配超时)'}\n"
                        f"🔒 内网 IP: {private_ip or '未知'}\n"
                        "--------------------------------\n"
                        "🐢 抢注任务圆满完成，脚本已安全停止，感谢使用！😄\n"
                    )
                    self.logp(ip_info)
                    self.public_ip = public_ip
                    return
            except Exception as exc:
                print(f"[{attempt + 1}/{max_attempts}] 查询 VNIC 信息出现短暂异常: {exc}，将在 5 秒后重试...")

            time.sleep(5)

        warn_text = (
            "⚠️ 实例已创建，但未能及时获取到 VNIC/公网 IP 地址。\n"
            "请登录甲骨文云控制台查看实例运行状态与网络分配。\n"
        )
        self.logp(warn_text)

    def launch_instance(self):
        return self._client.launch_instance(
            oci.core.models.LaunchInstanceDetails(
                display_name=self.tf.display_name,
                compartment_id=self.tf.compartment_id,
                shape=self.shape,
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=self.tf.ocpus, memory_in_gbs=self.tf.memory_in_gbs
                ),
                availability_domain=self.tf.availability_domain,
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=self.tf.subnet_id,
                    hostname_label=self.tf.hostname_label,
                    assign_public_ip=self.tf.assign_public_ip,
                ),
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    image_id=self.tf.image_id,
                    boot_volume_size_in_gbs=self.tf.boot_volume_size_in_gbs,
                ),
                metadata={"ssh_authorized_keys": self.tf.ssh_authorized_keys},
                is_pv_encryption_in_transit_enabled=True,
            )
        ).data

    # 兼容历史拼写错误方法名 lunch_instance
    def lunch_instance(self):
        return self.launch_instance()

    def logp(self, text: str) -> None:
        print(text)
        if USE_TG:
            self.desp += text + "\n"


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Oracle Cloud ARM VM 抢注脚本")
    parser.add_argument("main_tf", nargs="?", default=DEFAULT_TF_PATH, help="main.tf 文件路径，默认: %(default)s")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    user = OciUser()
    ins = InsCreate(user, args.main_tf)
    ins.create()


if __name__ == "__main__":
    main()
