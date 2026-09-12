> 适用于 Apple Silicon (M1/M2/M3/M4) Mac，支持官服、渠道服免扫码登录。
>

## 前提条件
如果你还没有在 Mac 上安装第五人格，请参看：[Mac 免扫码游玩《第五人格》| 教程分享 - 小红书](http://xhslink.com/a/mKDXBAFyzh89)

## 安装与运行
### 第一步：下载工具
**推荐**：[点击下载（夸克网盘）](https://pan.quark.cn/s/50eb30c7d587)

或 [备用地址（GitHub，较慢）](https://github.com/Alexander-Porter/idv-login/releases/latest)

### 第二步：打开终端
在「启动台」中搜索 **终端**（Terminal），打开它。

### 第三步：运行安装脚本
复制以下命令，在终端窗口中 **右键粘贴**，按回车键：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/KKeygen/idv-login/main/run-mac.sh)
```

脚本会自动引导你完成以下操作：

1. ✅ 检测你的 Mac 是否支持（仅支持 Apple Silicon）
2. 📂 提示你将下载的文件 **拖入终端窗口**
3. 🔒 自动校验文件完整性（SHA256）
4. 🚀 设置权限并启动工具

### 第四步：拖入文件
当脚本提示 **「请将下载的 idv-login 文件拖入此窗口」** 时：

+ 打开**访达**（Finder），找到下载的文件
+ 将文件 **直接拖到终端窗口中**（路径会自动填入）
+ 按 **回车键** 确认

> 💡 支持直接拖入 `.zip` 压缩包，脚本会自动解压。
>

### 第五步：输入密码
系统提示 **Password** 时，输入你的 **电脑解锁密码**（不是 Apple ID 密码）。

> ⚠️ 输入密码时屏幕不会显示任何字符，这是正常的，输完后按回车即可。
>

### 第六步：配置游戏启动参数
在启动游戏**之前**，需要在 CrossOver 中为第五人格添加启动参数：

1. 打开 **CrossOver**
2. 找到第五人格的容器（Bottle），**右键点击**第五人格图标
3. 选择「**修改快捷方式设置**」（或「**Configure Shortcut**」）
4. 找到「**命令行参数**」（Command Arguments）输入框
5. 在输入框中填入以下内容（注意两个参数之间有空格）：

```plain
--start_from_launcher=1 --is_multi_start
```

6. 点击「**保存**」或「**确定**」

> ⚠️ 这一步只需设置一次，之后每次启动游戏会自动使用这些参数。
>

### 第七步：启动游戏
打开 CrossOver 安装的第五人格，享受游戏！

---

## 下次使用
每次运行游戏前，重新打开终端并粘贴同一条命令即可：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/KKeygen/idv-login/main/run-mac.sh)
```

> 如果之前选择了「安装到系统目录」，也可以直接输入：`sudo idv-login`
>

---

## 常见问题
### 提示「Apple 无法验证是否包含可能危害 Mac 安全的软件」
使用上述安装脚本时**不会出现此提示**，脚本已自动处理。如果手动运行遇到此提示，请在「系统设置 → 隐私与安全性 → 安全性」中点击「仍要打开」。

### 网络异常（无法上网）
如果工具异常退出导致网络问题，请在终端运行：

```bash
networksetup -setwebproxystate Wi-Fi off
networksetup -setsecurewebproxystate Wi-Fi off
```

### Intel Mac 支持
当前仅支持 Apple Silicon (M1 及以上) Mac。Intel Mac 版本敬请期待。

---

## 卸载
在终端中运行：

```bash
sudo rm -f /usr/local/bin/idv-login
rm -f ~/Desktop/IDV登录工具.command
```

