# wifi-gate 常驻镜像：Python 3 标准库，零第三方依赖
FROM python:3.11-alpine

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PUID=1000 \
    PGID=1000

WORKDIR /app

COPY server.py router_adapter.py probe_miwifi.py selftest.py store.py admin_api.py importer.py docker-entrypoint.py simulate_device.py updater.py ./
COPY question_bank.json VERSION ./
COPY static/ ./static/
# 只把「示例配置」打进镜像；真实 config.json（含管理密码）由 docker-compose 挂载，
# 且已在 .dockerignore 中排除，永远不会进入镜像层。
COPY config.example.json ./config.default.json

RUN mkdir -p /app/state \
 && python3 -m compileall -q /app

# 不设 USER：入口脚本先修正挂载目录属主，再主动降权到 PUID/PGID
ENTRYPOINT ["python3", "/app/docker-entrypoint.py"]
CMD ["python3", "/app/server.py"]

# 端口从挂载的 config.json 读取，改端口不需要重建镜像
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import json,os,urllib.request; c=os.environ.get('WIFIGATE_CONFIG','/app/config.json'); p=json.load(open(c))['server']['port']; r=urllib.request.urlopen('http://127.0.0.1:%s/api/health'%p,timeout=3); raise SystemExit(0 if r.status==200 else 1)"

EXPOSE 8080
