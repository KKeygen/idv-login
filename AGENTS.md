# AGENTS.md

## 项目概览

- 本仓库是 `IdentityV-login-helper`，一个 Python 编写的第五人格登录辅助工具。
- 主要入口是 `src/main.py`；渠道服登录实现集中在 `src/channelHandler/`。
- 项目面向 Windows 预编译包和 macOS Apple Silicon；发布流程参考 `.github/workflows/pack.yaml`。
- 许可证是 GPLv3。引入或复制第三方代码时必须确认许可证兼容，并保留必要声明。

## 本地环境

- 推荐使用 Python 3.12；CI/发布流程使用 `3.12.10` 或 `3.12`。
- 安装依赖：

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

- Windows 打包额外依赖 `pywin32`，macOS/通用 PyInstaller 打包额外依赖 `pyinstaller`。

## 常用命令

- 语法/导入级别快速检查：

```bash
python -m compileall src
```

- 运行主程序：

```bash
python src/main.py
```

- 常见参数：

```bash
python src/main.py --open-ui
python src/main.py --proxy-port 10717
python src/main.py --uri "idvlogin://..."
```

- macOS 脚本入口：

```bash
bash run-mac.sh
```

- Windows README 中的单文件构建示例：

```bash
pyinstaller -F src/main.py -n idv-login-v10beta.exe -i assets/icon.ico --version-file assets/version.txt --uac-admin
```

## 验证要求

- 改动 Python 代码后，至少运行：

```bash
python -m compileall src
```

- 改动 `tools/generate_verification.py`、`tools/点我启动工具.bat` 或发布相关文件时，同时检查 `.github/workflows/verification.yaml` 和 `.github/workflows/pack.yaml` 是否仍匹配。
- 当前仓库没有独立测试套件；不要声称已通过单元测试，除非你实际新增并运行了测试。
- 对登录流程、代理、证书、hosts、URI Scheme、DNS 策略等系统副作用代码，优先做静态检查和小范围验证；不要在不需要时直接运行会修改用户系统设置的完整主程序。

## 代码结构约定

- `src/main.py` 负责命令行参数、生命周期、代理/证书/清理等主流程。
- `src/channelmgr.py` 定义渠道账号抽象；新增渠道时优先参考 `src/channelHandler/miChannelHandler.py` 的结构。
- 渠道登录具体实现通常位于 `src/channelHandler/<channel>Login/`，对外适配器位于 `src/channelHandler/*ChannelHandler.py`。
- 全局状态集中在 `src/app_state.py` 和 `src/envmgr.py`；改动时注意退出清理逻辑和持久化兼容性。
- 云端资源、版本、热修复、验证数据相关逻辑分布在 `src/cloudRes.py`、`src/cloudSync.py`、`src/hotfixmgr.py`、`ext/verification.json` 和 `tools/generate_verification.py`。

## 修改准则

- 保持改动小而聚焦，优先沿用现有同步/异步回调风格、日志风格和中文用户提示。
- 不要随意重命名公开字段，例如渠道账号序列化中的 `login_info`、`user_info`、`ext_info`、`device_info`、`oAuthData`、`game_id`。
- 处理账号、session、token、设备信息、证书和代理配置时，避免把敏感值写入日志、异常消息或提交文件。
- 不要提交本地运行产生的构建产物、缓存、日志、证书、临时下载内容或用户账号数据。
- 改动发布流程时，注意 Windows embed Python 布局：`dist/python-embed`、`dist/src`、`tools/点我启动工具.bat`。
- 如果新增依赖，更新 `requirements.txt`，并确认 Windows/macOS 发布流程都能安装。

## Git 注意事项

- 默认分支是 `main`。
- 这个 clone 可能是浅克隆；需要完整历史时先确认网络条件，再执行 `git fetch --unshallow`。
- 进行提交前查看：

```bash
git status --short
git diff
```
