### Oracle ARM 抢注脚本

该脚本使用 Oracle Cloud Infrastructure Python SDK 创建 `VM.Standard.A1.Flex` 实例。当前代码已按新版 OCI SDK 的调用方式保留 `LaunchInstanceDetails` / `LaunchInstanceShapeConfigDetails` / `CreateVnicDetails` / `InstanceSourceViaImageDetails`，并增强了 `main.tf` 解析、错误处理和运行参数。

> 仅支持密钥登录，需要密码生成的请自行修改 `oracle_arm.py`。

## 运行前准备

### 一、新建 `data` 文件夹，编辑 `.env`、`config` 文件以及私钥 `p.pem` 并保存到 `data` 文件夹内

#### `.env` 文件按以下格式保存内容

```bash
USE_TG=True
TG_BOT_TOKEN=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ
TG_USER_ID=987654321
TG_API_HOST=api.telegram.org
```

- `USE_TG=True` 启用电报通知，如不需要可将 `True` 改为 `False`，自行在日志内查看具体情况。
- `TG_BOT_TOKEN` 填写电报机器人 TOKEN。
- `TG_USER_ID` 填写用户、频道或群组 ID。
- `TG_API_HOST` 填写电报自建 API 反代地址，供网络环境无法访问时使用，网络正常则保持默认。

#### `config` 文件按以下格式保存内容

```bash
[DEFAULT]
user=ocid1.user.oc1..aaaaaaxxxxxxxxxxxxxxxxxxxxxxxxxxxx
fingerprint=xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx
key_file=/opt/oci/p.pem
tenancy=ocid1.tenancy.oc1..aaaaaaaxxxxxxxxxxxxxxxxxxxxxxxxxxxx
region=us-ashburn-1
```

- `user`、`fingerprint`、`tenancy`、`region` 的取值请参考[甲骨文官方文档](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm#SDK_and_CLI_Configuration_File#ariaid-title3)。
- `key_file` 指定私钥文件，文件名可修改，位置建议不做改动（该位置为 Docker 内文件存放地址，如有修改请同步调整配置）。私钥获取方式建议参考[官方文档](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm#ariaid-title3)的 `To generate an API signing key pair`。

### 二、准备 `main.tf`

参考[大鸟博客](https://www.daniao.org/14121.html)的 `1、生成main.tf` 获取 `main.tf` 文件，保存到与 `data` 文件夹相同的目录层。

脚本会读取以下字段：

- `compartment_id`
- `memory_in_gbs`
- `ocpus`
- `availability_domain`
- `subnet_id`
- `display_name`
- `source_id`
- `boot_volume_size_in_gbs`（可选，缺省为 `50`）
- `ssh_authorized_keys`

新版解析器兼容 `ocpus = 4` 和 `ocpus = "4"` 两种 Terraform 写法。

### 三、新建 `docker-compose.yml`

粘贴以下内容并保存到与 `data` 文件夹相同的目录层：

```yaml
services:
  oci:
    image: sheepgreen/oracle-arm # 或使用 github 镜像 ghcr.io/slippersheepig/oracle-arm
    container_name: oci
    volumes:
      - ./data:/opt/oci
      - ./main.tf:/oci/main.tf
```

### 四、启动

```bash
docker-compose up -d
```

## 可选环境变量

如需改变默认挂载路径或配置名称，可设置：

- `OCI_ARM_CONFIG_DIR`：默认 `/opt/oci`。
- `OCI_ARM_DOTENV`：默认 `${OCI_ARM_CONFIG_DIR}/.env`。
- `OCI_ARM_OCI_CONFIG`：默认 `${OCI_ARM_CONFIG_DIR}/config`。
- `OCI_ARM_OCI_PROFILE`：默认 `DEFAULT`。
- `OCI_ARM_TF_PATH`：默认 `main.tf`。

也可以直接传入 `main.tf` 路径：

```bash
python oracle_arm.py /path/to/main.tf
```

## 文件位置示例

![1b224fae4533f397be7b93fd67725f2c](https://github.com/slippersheepig/oracle_arm/assets/58287293/15bb7a67-92ed-41f7-9136-826175c477ca)
