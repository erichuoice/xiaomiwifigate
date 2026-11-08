# 部署到小米路由器 BE7000（Docker）

> 目标：把 wifi-gate 从 NAS 迁到路由器本机，不依赖任何外部设备。
>
> **已确认可行**：BE7000 官方固件内置 Docker（底层是标准 dockerd，管理界面为 SimpleDocker），
> 支持 `--network host`，容器数据落在 USB 外接盘上。

---

## 为什么必须在路由器上开 host 网络

这不是可选优化，是方案成立的前提。

程序的核心链路是：

```
手机发请求 → 拿到「来源 IP」 → 反查这台设备的 MAC → 调路由器接口放行这个 MAC
```

默认的 bridge 网络下，容器里看到的来源 IP 全是 Docker 网关（如 172.17.0.1），
所有手机在程序眼里是同一台设备 —— 放行会打到错设备，或者干脆全失效。

注意：**MAC 本身是从路由器设备列表接口查的**（`resolve_mac` 优先调 API，查不到才回退
本机 `/proc/net/arp`），所以 host 网络的唯一作用是拿到手机的真实来源 IP。
这一点反而让路由器部署比 NAS 更可靠 —— 设备列表由路由器自己维护，
不存在 ARP 表过期、容器看不到邻居表这类问题。

---

## 前提清单

| 项目 | 要求 | 说明 |
|---|---|---|
| U 盘 / 移动硬盘 | **EXT4 格式**，可用空间 **≥ 32GB** | 路由器只认 EXT4；NTFS/exFAT 不行。128MB 闪存装不下镜像 |
| 路由器固件 | 任意版本（不需要降级） | 本方案不改固件、不开 SSH、不刷机 |
| 路由器密码 | 已知后台管理密码 | 后面要登录 `192.168.31.1` |

> 格式化 U 盘会清空数据，先备份。Windows 上没有原生 EXT4 工具，可用 DiskGenius；
> macOS 下 `diskutil` 也不支持 EXT4，建议在 NAS 或 Linux 机器上 `mkfs.ext4`。

---

## 第一步：启用 Docker（纯图形界面）

1. U 盘插入路由器**背面 USB 3.0** 接口
2. 浏览器打开 `http://192.168.31.1`，登录后台
3. 进入 **存储功能** → 打开 **USB 3.0** 开关 → 创建虚拟内存 **256MB**（相当于 swap，1GB 内存跑容器有用）
4. 切到 **DOCKER** 页 → 点 **安装 Docker** → 等完成 → 点 **运行 Docker**
5. 点 **安装第三方管理** → 再点 **管理 Docker**，会打开 SimpleDocker：`http://192.168.31.1:9001/#/`
6. 默认账号密码都是 `admin`，**进去第一件事改密码**

---

## 第二步：探测关键值

> **重要前提**：SimpleDocker 的「终端命令」进的是 `simple-docker` **容器内部**，不是路由器宿主。
> 因此 `df -h`、`netstat` 看到的是容器自己的视图 —— 查不到 USB 挂载点，也看不到宿主端口。
> 这两项必须换路子拿。

先装命令行工具（只需一次）：

```sh
apk update && apk add docker-cli
```

确认能连上守护进程（能看到 socket 就说明走的是宿主 daemon，不是容器内独立的）：

```sh
ls -l /var/run/docker.sock
docker info | grep -iE "Docker Root Dir|Server Version|Architecture"
docker ps -a
```

`Docker Root Dir` 一般返回 `/mnt/usb-xxxxxxxx/mi_docker/lib/docker`，
**`usb-` 后面那串就是你的挂载点标识**，后面所有路径都用它。

拿不到就往 UI 走：SimpleDocker → **存储管理** → 卷详情 → 挂载点一栏写着
`/mnt/usb-xxxxxxxx/mi_docker/lib/docker/volumes/`，同样看 `usb-` 之后那串。

**实测记录（BE7000，2026-11-08，本机真机）：**

| 探测项 | 实测结果 | 说明 |
|---|---|---|
| `uname -m` | `aarch64` | 已确认。IPQ9554 是 64 位 ARM，用官方 `python:3.11-alpine` 的 arm64 版本，无需自定义构建 |
| `df -h \| grep -i mnt` | 空 | 容器内不可见，改走 `docker info` 或 UI 存储管理 |
| `docker info` | `docker: not found` | 镜像里没带 CLI，必须先 `apk add docker-cli` |
| `netstat -ltnp` | 空 | 容器内看不到宿主端口，改用 `docker ps` |
| Docker 版本 | `Server Version: 20.10.17` | 版本偏老，但 `--network host`、`--restart`、healthcheck 都支持 |
| Docker Root Dir | `/mnt/usb-fa757357/mi_docker/lib/docker/` | **本机 USB 挂载点 = `/mnt/usb-fa757357`** |
| 已占端口 | `simple-docker` 占 9001 | **8080 空闲**，无需改端口 |
| socket | `/var/run/docker.sock` 可访问 | 容器内 CLI 直连宿主 daemon，不用开 SSH |

> `WARNING: No swap limit support` 之类是路由器内核没开 cgroup 资源限制，不影响本程序运行。

**还需确认的两项：**

```sh
# 8080 是否被占用：看现有容器的端口映射
docker ps --format '{{.Names}}\t{{.Ports}}'

# 外网可达性：能不能拉镜像
docker pull hello-world
```

**探测结论怎么用：**

| 结果 | 应对 |
|---|---|
| `uname -m` 是 `aarch64` | 正常，用 `python:3.11-alpine`（官方多架构镜像） |
| `uname -m` 是 `armv7l` | 换 `python:3.11-alpine` 的 arm/v7 版本，其余不变 |
| 8080 已被占用 | 改 `config.json` 的 `server.port`（比如 8800），改完重启容器 |
| `docker pull` 失败 | 路由器 DNS 或出网有问题，先解决再继续 |
| USB 挂载点形如 `/mnt/usb-aabbccdd` | 后面所有路径都用这个，**别照抄示例** |

---

## 第三步：把源码放进 U 盘

### 方式一：路由器直接从 GitHub 拉（推荐）

仓库：<https://github.com/erichuoice/xiaomiwifigate>（公开仓库，匿名可下载）

在 SimpleDocker 终端里跑这一段，文件会直接落到 U 盘：

```sh
docker run --rm -v /mnt/usb-fa757357:/target alpine sh -c '
  apk add --no-cache ca-certificates >/dev/null &&
  wget -q -O /tmp/repo.tgz https://github.com/erichuoice/xiaomiwifigate/archive/refs/heads/main.tar.gz &&
  tar xzf /tmp/repo.tgz -C /tmp &&
  mkdir -p /target/wifi-gate &&
  cp -a /tmp/xiaomiwifigate-main/. /target/wifi-gate/ &&
  ls -l /target/wifi-gate && echo "=== 下载完成 ==="
'
```

若 `wget` 卡住不动，说明路由器出网到 GitHub 有问题（国内常见），先在同一个终端测：

```sh
docker run --rm alpine sh -c 'wget -q --timeout=10 -O /dev/null https://github.com && echo OK || echo FAIL'
```

`FAIL` 就改用方式二。

### 方式二：从电脑手动拷贝

在 U 盘根目录建 `wifi-gate` 文件夹，放入以下文件：

```
/mnt/usb-xxxx/wifi-gate/
├── server.py
├── store.py
├── admin_api.py
├── importer.py
├── router_adapter.py
├── docker-entrypoint.py
├── question_bank.json
├── static/
│   ├── index.html
│   ├── admin.html
│   └── setup.html       ← 首次配置引导页
└── state/               ← 需要可写，程序会在这里写授权记录
```

目录里的 `usb-fa757357` 是本机实测挂载点，换机器时用 `docker info | grep "Docker Root Dir"` 重新确认。

> **不需要准备 `config.json`。** 首次启动时程序发现没有配置，会自动进入引导模式：
> 所有页面跳转到 `/setup`，在浏览器里填完路由器密码后，它自己生成 `config.json`
> （权限 600）。密码全程不经过命令行参数，也不会进镜像层。

> 不需要 `Dockerfile`、不需要构建镜像。程序是纯 Python 标准库，零第三方依赖，
> 直接挂载源码目录运行官方 python 镜像即可 —— 升级时改文件、重启容器就生效。

确认 `state/` 目录可写：`ls -ld /mnt/usb-xxxx/wifi-gate/state`

---

## 第四步：启动容器

```sh
docker pull python:3.11-alpine

docker run -d \
  --name wifi-gate \
  --network host \
  --restart unless-stopped \
  --stop-timeout 10 \
  -e TZ=Asia/Shanghai \
  -e PYTHONUNBUFFERED=1 \
  -v /mnt/usb-xxxx/wifi-gate:/app \
  -w /app \
  python:3.11-alpine \
  python3 server.py
```

把 `/mnt/usb-xxxx` 换成第二步探测到的真实路径。

**逐项说明：**

| 参数 | 为什么必须这样 |
|---|---|
| `--network host` | 拿真实客户端 IP，见文首说明。不能省 |
| `--restart unless-stopped` | 容器崩溃自拉起 |
| `--stop-timeout 10` | 给程序 10 秒优雅退出，把手上的授权记录落盘 |
| `-v .../wifi-gate:/app` | 源码 + 配置 + state 全在 U 盘，重启不丢 |
| 不设 `PUID/PGID` | 路由器上容器是 root，不需要降权 |

`--network host` 下 `-p` 端口映射会被忽略，实际监听端口由 `config.json` 的
`server.port` 决定；首次启动时没有 config，用默认的 8080。

---

## 第五步：浏览器完成首次配置

容器起来后，浏览器打开：

```
http://192.168.31.1:8080/
```

会自动跳到 `/setup`，表单两项：

| 字段 | 填什么 |
|---|---|
| 路由器地址 | 默认 `http://192.168.31.1`，一般不用改 |
| 路由器管理密码 | 登录路由器后台那个密码（**不是 Wi-Fi 密码**） |

**答题后台的登录密码默认与路由器管理密码相同**，不需要再填一次。如果想分开，
可以手动改 `config.json` 里的 `server.admin_password`。

下面还有个 **「先演练，不真改规则」** 复选框，默认勾着。它会真登录路由器、
真读取设备列表，但不会发任何修改规则的请求 —— 第一次部署建议保持勾选。

点「保存并验证」后：

- 成功会显示「已连接到路由器，能正常读取设备列表」
- 如果密码错，会提示「配置已保存，但登录路由器失败」并给出原因 —— 这时改密码重新部署即可

配置写入 U 盘上的 `config.json`（权限 600），**立即生效，不用重启容器**。
之后 `/setup` 会自动关闭（再访问会跳回首页），防止别人用它改写密码。

---

## 第六步：验证

```sh
# 容器活着
docker ps | grep wifi-gate

# 服务自检（容器内部）
curl -s http://127.0.0.1:8080/api/health
```

返回 `{"ok": true, ...}` 就通了，其中应该能看到 `"mode": "miwifi"`。

然后从**手机**验证（这条最关键，容器里测不出真实 IP 问题）：

1. 手机连主 Wi-Fi，浏览器打开 `http://192.168.31.1:8080/` —— 能打开答题页
2. 米家 App 里对这台手机点 **暂停联网**
3. 手机**再刷新**答题页 —— 仍然能打开（说明局域网互通，方案成立）
4. 答对题目 → 观察是否恢复上网

第 4 步是最后一道验证。第一次请保持 `dry_run: true`（`/setup` 里的「先演练」默认就勾着），
它会真登录路由器、真解析 MAC，但**不发修改规则请求**，只写日志。确认日志里
`[DRY-RUN] ALLOW mac=...` 的那串 MAC 就是这台手机，再把 `config.json` 的
`router.dry_run` 改成 `false`，然后 `docker restart wifi-gate` 生效。

---

## 已知限制与风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 固件升级可能重置 Docker 环境 | 容器消失，断网时打不开答题页 | 升级后检查 `docker ps`；保留 U 盘里的源码，重建容器两分钟 |
| 路由器重启后容器未自启 | 同上 | 重启后确认一次；`--restart unless-stopped` 依赖 Docker 服务本身自启 |
| 单点故障 | 容器挂了 = 断网且无法自救 | 保留 NAS 方案作为备用，随时可切回 |
| U 盘寿命 | 长期写入 `state/` 和日志 | 用品牌 U 盘；日志已做 5MB 轮转 |
| 8080 端口冲突 | 容器起不来 | 探测时已确认；冲突就改 `config.json` |

---

## 回退到 NAS

路由器方案和 NAS 方案用的是同一份代码，切换只是换个地方跑：

1. 路由器上 `docker stop wifi-gate && docker rm wifi-gate`
2. NAS 上 `cd wifi-gate && ./gate.sh up`

`config.json` 和 `state/grants.json` 可以直接搬过去，授权记录不会丢。
