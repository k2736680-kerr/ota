# OTA 穿戴自动化 · Agent 持久记忆

> GitHub：`k2736680-kerr/ota`（`~/AutoTestTools/Projects/ota`）

## 是什么

本地 Web 控制台，**一次只连一台 USB 手机**，分三条链路：手表 DaFit、眼镜 daecho、戒指 Da Ring。不改 PHP 后台。

| 链路 | 配置 | 执行脚本 |
|------|------|----------|
| 手表（已验证） | `config/app_watch.yaml` | `server/scripts/run_dafit_flow.py` |
| 眼镜 | `config/app_glass.yaml` | `server/scripts/run_glass_flow.py` |
| 戒指 | `config/app_ring.yaml` | `server/scripts/run_ring_flow.py` |

## 怎么跑

```bash
cd ~/AutoTestTools/Projects/ota
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python start.py --dry-run   # 检查配置
python start.py             # http://127.0.0.1:8765/
python -m unittest server.tests.test_core
```

开跑前在页面做「蓝牙/Wi-Fi 自检」。真机与换机见 `docs/使用说明.md`；元素校准见 `docs/元素校准.md`。

## 关键目录

- `server/scripts/` — **仅正式执行链路**，勿堆临时脚本
- `runtime/` — 截图/dump，可整目录清空
- `output/` — 历史报告与日志，不随 runtime 清理
- `config/` — 三 app 的 YAML

## 业务约定

- 同版本循环：**页面完成 + 可重连 = PASS**；版本号变化只写备注，不作失败条件。
- App 被切后台 ≠ 升级失败（与「升级页真丢」区分）。
- 读版本优先稳定文案，不绑 resourceId。
- 升级超时看「是否还在动」，眼镜一轮约 550–660s，见使用说明。

## 勿动 / 危险

- 不要在仓库根目录新增 `.tmp_*` / `.watch*` 监测脚本。
- 调度器真机模式走 `run_dafit_flow` 等正式脚本，勿绕过调度直接改 PHP。

## Mac 注意

- `uiautomator2` + USB 调试与 Windows 相同；`start.py` 无 PowerShell 依赖。
- 设备序列号、MAC 写在 YAML 或本机 override，勿提交个人机号。

## 先读

- `README.md` → `docs/使用说明.md`