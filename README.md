### 仅支持密钥登录，需要密码生成的请自行修改`oracle_arm.py`
## 步骤
### 一、新建`data`文件夹，编辑`.env`、`config`文件以及私钥`p.pem`并保存到`data`文件夹内
#### `.env`文件按以下格式保存内容
```bash
USE_TG=True
TG_BOT_TOKEN=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ
TG_USER_ID=987654321
TG_API_HOST=api.telegram.org
```
- `USE_TG=True`启用电报通知，如不需要可将`True`改为`False`，自行在日志内查看具体情况
- `TG_BOT_TOKEN`填写电报机器人TOKEN
- `TG_USER_ID`填写用户、频道或群组ID
- `TG_API_HOST`填写电报自建API反代地址，供网络环境无法访问时使用，网络正常则保持默认
#### `config`文件按以下格式保存内容
```bash
[DEFAULT]
user=ocid1.user.oc1..aaaaaaxxxxxxxxxxxxxxxxxxxxxxxxxxxx
fingerprint=xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx
key_file=/opt/oci/p.pem
tenancy=ocid1.tenancy.oc1..aaaaaaaxxxxxxxxxxxxxxxxxxxxxxxxxxxx
region=us-ashburn-1
```
- `user`、`fingerprint`、`tenancy`、`region`的取值请参考[甲骨文官方文档](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/sdkconfig.htm#SDK_and_CLI_Configuration_File#ariaid-title3)
#### `key_file`指定私钥文件，文件名可修改，位置建议不做改动（该位置为docker内文件存放地址，如有修改请自行调整所有相关文件配置），私钥获取方式建议参考[官方文档](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm#ariaid-title3)的`To generate an API signing key pair`
### 二、参考[大鸟博客](https://www.daniao.org/14121.html)的`1、生成main.tf`获取`main.tf`文件，保存到与`data`文件夹相同的目录层
### 三、新建`docker-compose.yml`文件，粘贴以下内容并保存到与`data`文件夹相同的目录层
```bash
services:
  oci:
    image: sheepgreen/oracle-arm
    container_name: oci
    volumes:
      - ./data:/opt/oci
      - ./main.tf:/oci/main.tf
```
### 四、附上文件位置图
![1b224fae4533f397be7b93fd67725f2c](https://github.com/slippersheepig/oracle_arm/assets/58287293/15bb7a67-92ed-41f7-9136-826175c477ca)
### 五、输入`docker-compose up -d`即启动成功
