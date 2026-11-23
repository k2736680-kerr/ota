# 穿戴 OTA 自动化测试工具

本地 Web 控制台，一次只跑一台 USB 手机。眼镜、戒指、手表三条链路分开配置。工具不修改 PHP 后台。

## 当前状态

- **手表 DaFit 是已验证的正式链路**：`config/app_watch.yaml` + `server/scripts/run_dafit_flow.py`，调度器在真机模式下走这条流程。2026-11-09 已在 OPPO PCPM00 上跑完 T01、T02、T04、T05、T06、T07、T08、T09 各一轮，全部 PASS。
- **眼镜 daecho 链路已接通真机**：`config/app_glass.yaml` + `server/scripts/run_glass_flow.py`。2026-11-13 在新 V06（`DD:4D:D6:18:0C:4A`）上跑完 7 个用例，**全部 PASS**。
- **戒指 Da Ring 已接通真机**：`config/app_ring.yaml` + `server/scripts/run_ring_flow.py`。包名 `com.moyoung.ring`，路径是「我的 → 固件升级 → 固件升级」，按钮文案是「更新到最新版本」。进度不在页面上，看 logcat `ring_tag` 的 `onProgressChanged`。
- 同版本循环：**页面完成 + 能重连即 PASS**；版本号可变可不变，只写进备注，不因版本不变改判失败。
- 验收按链路**全量用例**跑；T08 息屏失败记问题，不能放过。PHP 配包由测试人员负责。
- 三链路读版本都优先跟页面稳定文案（眼镜/戒指「当前版本」，手表「最新版本/有新版本」旁的固件号），不绑 resourceId；换机/App 改版文案不变就不用改配置。
- **App 被切到后台不再判成升级失败**：2026-11-13 升级到 69% 时有人切走去连戒指，旧逻辑 45 秒读不到页面就报「升级页丢失」，其实升级一直在后台跑（切回来已 73%）。现在分清「被切后台」和「页面真丢」。
- `server/scripts/` 只放正式执行链路。截图、dump 等临时文件放 `runtime/`，可以整体清空，启动时会自动重建。历史报告和日志在 `output/`，不随临时目录清理。根目录不要再放 `.tmp_*` / `.watch*` 监测脚本。

升级判定只看「还在不在动」，不设总时长上限；眼镜主控一轮实测 550~660 秒。详见 [docs/使用说明.md](docs/使用说明.md) 的「升级要跑多久，怎么算超时」。

```bat
python start.py --dry-run
```

浏览器打开 http://127.0.0.1:8765/ 。真机用法、死机规则和换机步骤见 [docs/使用说明.md](docs/使用说明.md)。页面元素校准见 [docs/元素校准.md](docs/元素校准.md)。

开跑前先在页面点「蓝牙/Wi-Fi自检」，确认这台手机能开关蓝牙和 Wi-Fi。

```bat
python -m unittest server.tests.test_core
```

依赖见 `requirements.txt`：fastapi、uvicorn、uiautomator2、pyyaml、requests。
