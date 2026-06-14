import argparse
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

DEFAULT_CONFIG_DIR = os.getenv("OCI_ARM_CONFIG_DIR", "/opt/oci")
DEFAULT_DOTENV_PATH = os.getenv("OCI_ARM_DOTENV", str(Path(DEFAULT_CONFIG_DIR) / ".env"))
DEFAULT_OCI_CONFIG_PATH = os.getenv("OCI_ARM_OCI_CONFIG", str(Path(DEFAULT_CONFIG_DIR) / "config"))
DEFAULT_OCI_PROFILE = os.getenv("OCI_ARM_OCI_PROFILE", "DEFAULT")
DEFAULT_TF_PATH = os.getenv("OCI_ARM_TF_PATH", "main.tf")

config = dotenv_values(DEFAULT_DOTENV_PATH)


def _to_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# tg pusher config
USE_TG = _to_bool(config.get("USE_TG"), default=False)  # 如果启用tg推送 要设置为True
TG_BOT_TOKEN = config.get("TG_BOT_TOKEN", "")  # 通过 @BotFather 申请获得，示例：1077xxx4424:AAFjv0FcqxxxxxxgEMGfi22B4yh15R5uw
TG_USER_ID = config.get("TG_USER_ID", "")  # 用户、群组或频道 ID，示例：129xxx206
TG_API_HOST = config.get("TG_API_HOST", "api.telegram.org")  # 自建 API 反代地址，供网络环境无法访问时使用


def telegram(desp):
    if not USE_TG:
        return
    if not (TG_BOT_TOKEN and TG_USER_ID and TG_API_HOST):
        print("Telegram Bot 配置缺失，跳过推送")
        return
    data = (("chat_id", TG_USER_ID), ("text", "🐢甲骨文ARM抢注脚本为您播报🐢 \n\n" + desp))
    try:
        response = requests.post(
            "https://" + TG_API_HOST + "/bot" + TG_BOT_TOKEN + "/sendMessage",
            data=data,
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Telegram Bot 推送失败: {exc}")
    else:
        print("Telegram Bot 推送成功")


class OciUser:
    """
    oci 用户配置文件的类
    """

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
        print("parser cfg")
        self.user = cfg["user"]
        self.fingerprint = cfg["fingerprint"]
        self.key_file = cfg["key_file"]
        self.tenancy = cfg["tenancy"]
        self.region = cfg["region"]

    def keys(self):
        return ("user", "fingerprint", "key_file", "tenancy", "region")

    def __getitem__(self, item):
        return getattr(self, item)

    def compartment_id(self):
        return self.tenancy


class FileParser:
    ASSIGNMENT_RE = re.compile(r'^\s*([A-Za-z0-9_".-]+)\s*=\s*(.+?)\s*(?:#.*)?$', re.MULTILINE)

    def __init__(self, file_path: str) -> None:
        self.parser(file_path)

    def parser(self, file_path):
        try:
            print("filepath", file_path)
            with open(file_path, "r", encoding="utf-8") as file_obj:
                self._filebuf = file_obj.read()
        except OSError as exc:
            raise SystemExit(f"main.tf文件打开失败,请再一次确认执行了正确操作,脚本退出: {exc}") from exc

        values = self._parse_assignments(self._filebuf)
        self.compoartment_id = self._required(values, "compartment_id")
        self.memory_in_gbs = self._required_float(values, "memory_in_gbs")
        self.ocpus = self._required_float(values, "ocpus")
        self.availability_domain = self._required(values, "availability_domain")
        self.subnet_id = self._required(values, "subnet_id")
        self.display_name = self._required(values, "display_name").strip().replace(" ", "-")
        self.image_id = self._required(values, "source_id")
        self.boot_volume_size_in_gbs = self._optional_float(values, "boot_volume_size_in_gbs", 50.0)
        self.ssh_authorized_keys = self._required(values, "ssh_authorized_keys")

    @classmethod
    def _parse_assignments(cls, content):
        values = {}
        for key, raw_value in cls.ASSIGNMENT_RE.findall(content):
            clean_key = key.strip('"')
            clean_value = raw_value.strip().rstrip(",").strip()
            if clean_value.startswith('"') and clean_value.endswith('"'):
                clean_value = clean_value[1:-1]
            values.setdefault(clean_key, []).append(clean_value)
        return values

    @staticmethod
    def _required(values, key):
        try:
            return values[key][-1]
        except (KeyError, IndexError) as exc:
            raise ValueError(f"main.tf缺少必要参数: {key}") from exc

    @classmethod
    def _required_float(cls, values, key):
        raw_value = cls._required(values, key)
        try:
            return float(raw_value)
        except ValueError as exc:
            raise ValueError(f"main.tf参数 {key} 必须是数字，当前值: {raw_value}") from exc

    @classmethod
    def _optional_float(cls, values, key, default):
        if key not in values:
            return default
        return cls._required_float(values, key)

    @property
    def ssh_authorized_keys(self):
        return self._sshkey

    @ssh_authorized_keys.setter
    def ssh_authorized_keys(self, key):
        self._sshkey = key

    @property
    def boot_volume_size_in_gbs(self):
        return self._volsize

    @boot_volume_size_in_gbs.setter
    def boot_volume_size_in_gbs(self, size):
        self._volsize = size

    @property
    def image_id(self):
        return self._imgid

    @image_id.setter
    def image_id(self, imageid):
        self._imgid = imageid

    @property
    def display_name(self):
        return self._dname

    @display_name.setter
    def display_name(self, name):
        self._dname = name

    @property
    def subnet_id(self):
        return self._subid

    @subnet_id.setter
    def subnet_id(self, sid):
        self._subid = sid

    @property
    def compoartment_id(self):
        return self._comid

    @compoartment_id.setter
    def compoartment_id(self, cid):
        self._comid = cid

    @property
    def memory_in_gbs(self):
        return self._mm

    @memory_in_gbs.setter
    def memory_in_gbs(self, mm):
        self._mm = mm

    @property
    def ocpus(self):
        return self._cpu

    @ocpus.setter
    def ocpus(self, cpu_count):
        self._cpu = cpu_count

    @property
    def availability_domain(self):
        return self._adomain

    @availability_domain.setter
    def availability_domain(self, domain):
        self._adomain = domain


class InsCreate:
    shape = "VM.Standard.A1.Flex"

    def __init__(self, user: OciUser, filepath) -> None:
        self._user = user
        self._client = ComputeClient(config=dict(user))
        self.tf = FileParser(filepath)
        self.sleep_time = random.uniform(3, 6)
        self.try_count = 0
        self.desp = ""
        self.ins_id = None
        self.public_ip = None

    def create(self):
        text = "脚本开始启动:\n,区域:{}-实例:{},CPU:{}C-内存:{}G-硬盘:{}G的小🐔已经快马加鞭抢购了\n".format(
            self.tf.availability_domain,
            self.tf.display_name,
            self.tf.ocpus,
            self.tf.memory_in_gbs,
            self.tf.boot_volume_size_in_gbs,
        )
        telegram(text)
        while True:
            try:
                ins = self.launch_instance()
            except oci.exceptions.ServiceError as exc:
                self.handle_service_error(exc)
                time.sleep(self.sleep_time)
            except oci.exceptions.RequestException as exc:
                re_text = "❌发生错误:{}".format(exc)
                print(re_text)
                telegram(re_text)
                time.sleep(self.sleep_time)
            else:
                self.logp(
                    "🎉经过 {} 尝试后\n 区域:{}实例:{}-CPU:{}C-内存:{}G🐔创建成功了🎉\n".format(
                        self.try_count + 1,
                        self.tf.availability_domain,
                        self.tf.display_name,
                        self.tf.ocpus,
                        self.tf.memory_in_gbs,
                    )
                )
                self.ins_id = ins.id
                self.check_public_ip()
                telegram(self.desp)
                break
            finally:
                self.try_count += 1
                count_text = "抢注中，已经经过:{}尝试".format(self.try_count)
                print(count_text)
                if self.try_count % 100 == 0:
                    telegram(count_text)

    def handle_service_error(self, exc):
        if exc.status == 429 and exc.code == "TooManyRequests":
            print("请求太快了，自动调整请求时间ing")
            if self.sleep_time < 60:
                self.sleep_time += random.uniform(3, 6)
        elif self.is_capacity_error(exc):
            print("目前没有请求限速,快马加刷中")
            if self.sleep_time > 15:
                self.sleep_time -= random.uniform(3, 6)
        elif exc.status == 400 and "Service limit" in str(exc.message):
            self.logp(
                "❌如果看到这条推送,说明刷到机器，但是开通失败了，请后台检查你的cpu，内存，硬盘占用情况，并释放对应的资源 返回值:{},\n 脚本停止".format(exc)
            )
            telegram(self.desp)
            raise exc
        else:
            print("❌发生错误:{}".format(exc))
            telegram("❌发生错误:{}".format(exc))
        print("本次返回信息:", exc)

    @staticmethod
    def is_capacity_error(exc):
        message = str(getattr(exc, "message", "")).lower()
        return exc.status in {400, 500} and "out of host capacity" in message

    def check_public_ip(self):
        network_client = VirtualNetworkClient(config=dict(self._user))
        count = 100
        while count:
            attachments = self._client.list_vnic_attachments(
                compartment_id=self._user.compartment_id(), instance_id=self.ins_id
            )
            data = attachments.data
            if data:
                print("开始查找vnic id ")
                vnic_id = data[0].vnic_id
                public_ip = network_client.get_vnic(vnic_id).data.public_ip
                self.logp("公网ip为:{}\n 🐢脚本停止，感谢使用😄\n".format(public_ip))
                self.public_ip = public_ip
                return
            time.sleep(5)
            count -= 1
        self.logp("开机失败，被他娘甲骨文给关掉了😠，脚本停止，请重新运行\n")

    def launch_instance(self):
        return self._client.launch_instance(
            oci.core.models.LaunchInstanceDetails(
                display_name=self.tf.display_name,
                compartment_id=self.tf.compoartment_id,
                shape=self.shape,
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=self.tf.ocpus, memory_in_gbs=self.tf.memory_in_gbs
                ),
                availability_domain=self.tf.availability_domain,
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=self.tf.subnet_id, hostname_label=self.tf.display_name
                ),
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    image_id=self.tf.image_id,
                    boot_volume_size_in_gbs=self.tf.boot_volume_size_in_gbs,
                ),
                metadata=dict(ssh_authorized_keys=self.tf.ssh_authorized_keys),
                is_pv_encryption_in_transit_enabled=True,
            )
        ).data

    # 兼容旧代码/外部调用里已有的拼写错误方法名。
    def lunch_instance(self):
        return self.launch_instance()

    def logp(self, text):
        print(text)
        if USE_TG:
            self.desp += text


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
