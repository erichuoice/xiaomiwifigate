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
| 已占端口 | `simple-docker` 占 9001；**8080 被路由器宿主自身某服务占用** | 用 `WIFIGATE_PORT=8088`，见第四步 |
| socket | `/var/run/docker.sock` 可访问 | 容器内 CLI 直连宿主 daemon，不用开 SSH |

> `WARNING: No swap limit support` 之类是路由器内核没开 cgroup 资源限制，不影响本程序运行。

**还需确认的两项：**

```sh
# 宿主上所有监听端口（--pid=host 才能看到宿主进程，普通 docker ps 看不到）
docker run --rm --pid=host --network host python:3.11-alpine netstat -ltnp 2>/dev/null | head -25

# 外网可达性：能不能拉镜像
docker pull hello-world
```

**探测结论怎么用：**

| 结果 | 应对 |
|---|---|
| `uname -m` 是 `aarch64` | 正常，用 `python:3.11-alpine`（官方多架构镜像） |
| `uname -m` 是 `armv7l` | 换 `python:3.11-alpine` 的 arm/v7 版本，其余不变 |
| 8080 已被占用（**BE7000 实测如此**） | 启动加 `-e WIFIGATE_PORT=8088`，或改 `config.json` 的 `server.port` |
| `docker pull` 失败 | 路由器直连 Docker Hub 常超时，改用国内源，见第四步 |
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

**先拉镜像。** 直连 Docker Hub 在国内大概率超时
（`Get "https://registry-1.docker.io/v2/": context deadline exceeded`），
换成国内镜像站再打本地标签：

```sh
docker pull docker.m.daocloud.io/library/python:3.11-alpine
docker tag  docker.m.daocloud.io/library/python:3.11-alpine python:3.11-alpine
docker images | head -5      # 必须看到 python  3.11-alpine 这一行
```

备选镜像站（同样结构，换域名即可）：`docker.1panel.live`、`hub.rat.dev`。
`dockerpull.org`、`docker.nju.edu.cn` 实测不可用。

然后启动：

```sh
docker run -d \
  --name wifi-gate \
  --network host \
  --restart unless-stopped \
  --stop-timeout 10 \
  -e TZ=Asia/Shanghai \
  -e PYTHONUNBUFFERED=1 \
  -e WIFIGATE_PORT=8088 \
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
| `-e WIFIGATE_PORT=8088` | 覆盖监听端口，见下方「端口冲突」 |
| `-v .../wifi-gate:/app` | 源码 + 配置 + state 全在 U 盘，重启不丢 |
| 不设 `PUID/PGID` | 路由器上容器是 root，不需要降权 |

### 端口冲突：为什么默认改成了 8088

`--network host` 下容器与路由器共用一个网络命名空间，`-p` 映射会被忽略，
实际监听端口由 `server.port` 决定。**实测 BE7000 官方的 8080 已被路由器自身
某个服务占用**，用默认值启动会直接崩：

```
OSError: [Errno 98] Address in use
```

两种解法，任选其一：

1. **环境变量（推荐）**：启动时加 `-e WIFIGATE_PORT=8088`，优先级高于配置文件，
   引导模式下也生效，不必预先写 `config.json`
2. **改配置文件**：在 `config.json` 里把 `server.port` 改成 8088

`--restart unless-stopped` 会记住完整启动参数，断电重启不会丢这个环境变量；
但若你手动 `docker rm` 后用旧命令重建，漏了 `-e WIFIGATE_PORT` 就会再次撞车。

想看清 8080 到底被谁占，可以借用 host 的 PID 命名空间：

```sh
docker run --rm --pid=host --network host python:3.11-alpine netstat -ltnp 2>/dev/null | head -25
```

---

## 第五步：浏览器完成首次配置

容器起来后，浏览器打开：

```
http://192.168.31.1:8088/
```

端口要和启动命令里的 `WIFIGATE_PORT` 一致。

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

# 启动日志（看端口对不对）
docker logs wifi-gate

# 服务自检：必须在 wifi-gate 容器内部访问 127.0.0.1
docker exec wifi-gate wget -qO- http://127.0.0.1:8088/api/health
```

> 别在 `simple-docker` 的终端里直接 `curl http://127.0.0.1:8088` —— 它是 bridge 网络，
> 那个 `127.0.0.1` 指的是它自己，不是宿主，只会得到连接被拒。

返回 `{"ok": true, ...}` 就通了，其中应该能看到 `"mode": "miwifi"`。

然后从**手机**验证（这条最关键，容器里测不出真实 IP 问题）：

1. 手机连主 Wi-Fi，浏览器打开 `http://192.168.31.1:8088/` —— 能打开答题页
2. 米家 App 里对这台手机点 **暂停联网**
3. 手机**再刷新**答题页 —— 仍然能打开（说明局域网互通，方案成立）
4. 答对题目 → 观察是否恢复上网

第 4 步是最后一道验证。第一次请保持 `dry_run: true`（`/setup` 里的「先演练」默认就勾着），
它会真登录路由器、真解析 MAC，但**不发修改规则请求**，只写日志。确认日志里
`[DRY-RUN] ALLOW mac=...` 的那串 MAC 就是这台手机，再把 `config.json` 的
`router.dry_run` 改成 `false`，然后 `docker restart wifi-gate` 生效。

---

## 第七步：以后怎么升级

`3.1.0` 起程序自带更新功能，**不用再进终端敲 wget**：

1. 浏览器打开后台 `http://192.168.31.1:8088/admin` → **设置** → 拉到最下面「程序更新」
2. 点 **检查更新**：会读远端 `VERSION` 和本地比对，几秒内出结果
3. 有新版本就点 **立即更新并重启**：下载主干代码 → 覆盖程序文件 → 2 秒后进程自己重启
4. 想全自动就勾上「每 6 小时自动检查」，再决定要不要勾「发现新版本自动安装并重启」

它只覆盖程序文件，`config.json`、`state/`（题库、授权记录、日志）都不动；替换前会把旧文件
打包到 `state/backup/`，要回退就解包回去再重启。自动安装会打断正在答题的人约 2–3 秒。

路由器直连 GitHub 通常不通，更新器默认先走 `https://gh-proxy.com/` 加速站、失败再直连。
要换源，重建容器时加：

```sh
-e WIFIGATE_MIRROR=https://gh-proxy.com/,direct
```

**兜底：后台进不去 / 更新器连不上外网**，还是用老办法手动覆盖两个文件：

```sh
docker run --rm -v /mnt/usb-fa757357/wifi-gate:/app alpine sh -c '
  wget -q -O /app/server.py https://gh-proxy.com/https://raw.githubusercontent.com/erichuoice/xiaomiwifigate/main/server.py &&
  wget -q -O /app/static/index.html https://gh-proxy.com/https://raw.githubusercontent.com/erichuoice/xiaomiwifigate/main/static/index.html &&
  echo OK
'
docker restart wifi-gate
docker exec wifi-gate wget -qO- http://127.0.0.1:8088/api/health
```

`health` 里现在带 `"version"` 字段，能直接看出跑的是哪一版。

---

## 已知限制与风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 固件升级可能重置 Docker 环境 | 容器消失，断网时打不开答题页 | 升级后检查 `docker ps`；保留 U 盘里的源码，重建容器两分钟 |
| 路由器重启后容器未自启 | 同上 | 重启后确认一次；`--restart unless-stopped` 依赖 Docker 服务本身自启 |
| 单点故障 | 容器挂了 = 断网且无法自救 | 保留 NAS 方案作为备用，随时可切回 |
| U 盘寿命 | 长期写入 `state/` 和日志 | 用品牌 U 盘；日志已做 5MB 轮转 |
| 8080 端口冲突 | 容器起不来，报 `Address in use` | BE7000 实测 8080 已被占用，启动统一加 `-e WIFIGATE_PORT=8088` |
| 路由器拉不到 Docker Hub 镜像 | `docker pull` 超时 | 走国内镜像站，见第四步 |
| 路由器直连 GitHub 超时 | 无法从仓库拉源码 | 改用加速站前缀 `https://gh-proxy.com/` + 原地址，或用电脑拷进 U 盘 |
| 自动更新把带 bug 的版本装上去 | 服务起不来，孩子断网期间没人能答题 | 只开「自动检查」不开「自动安装」；`state/backup/` 里留 5 份旧文件可回退 |

---

## 回退到 NAS

路由器方案和 NAS 方案用的是同一份代码，切换只是换个地方跑：

1. 路由器上 `docker stop wifi-gate && docker rm wifi-gate`
2. NAS 上 `cd wifi-gate && ./gate.sh up`

`config.json` 和 `state/grants.json` 可以直接搬过去，授权记录不会丢。
