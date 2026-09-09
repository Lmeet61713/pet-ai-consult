# 端口表

更新日期：2026-08-14

当前规范端口来自 `compose.yaml` 和 `scripts/start_*.sh`。

| 服务 | 端口 | 暴露范围 |
|---|---:|---|
| pet-consult FastAPI | 8100 | 宿主机回环或私有网络 |
| Qwen3.5-9B vLLM | 8101 | 私有网络 |
| VisionGateway | 8102 | 私有网络 |
| 可选 Guard | 8103 | 私有网络 |
| Redis | 6379 | 私有网络 |
| Nginx gateway | 6006、6008 | 由算力平台映射到公网 HTTPS |

仅 Nginx 可以对公网暴露。vLLM、VisionGateway、Guard、Redis 和 FastAPI 不应直接开放公网。

注意：旧 `docker-compose.yml` 仍使用 API `8081` 和 vLLM `8000`。当前文档不采用该口径。

