# 本地云配置审阅与签名

`cloud-config-staging` 只用于产生候选，GitHub Actions 不持有签名私钥。所有操作由事先审阅和固定的本机工具完成；不要 checkout 或执行候选分支里的工具。此工具不会推送文件，也不会修改 `ext/verification.json`。

## 签名格式

`assets/cloudRes.json` 保留原本的根对象，每条热修复新增 `source_sha256`（目标提交中源码的原始字节 SHA-256）。配置根对象额外包含 `_signature`：

```json
{"alg":"ECDSA-P256-SHA256-DER","value":"Base64 DER signature"}
```

签名消息为 UTF-8 字节 `IDV-CLOUD-CONFIG-V1`、一个零字节，再加去掉根 `_signature` 后的完整 JSON 对象，以 Python `json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)` 得到的 UTF-8 字节。拒绝重复键和非有限数字；允许有限浮点。旧客户端可忽略新增字段，但旧版不会获得签名保护。

## 首次准备（人工控制）

从可信版本审阅签名工具，编译本地 Secure Enclave 组件，不应把签名密钥或包含凭据的文件提交到仓库：

```sh
swiftc -O tools/cloud_sign_enclave.swift -o /绝对路径/cloud-sign-enclave
```

`generate` 会实际创建绑定本机的 P-256 私钥，须由设备主人明确知情后手动执行：

```sh
/绝对路径/cloud-sign-enclave generate > /安全位置/cloud-sign-public.pem
```

公开密钥须经独立核对后内置在未来客户端中。该密钥受当前生物识别配置保护；设备损坏或生物识别集合变化可能使密钥不可用，必须先设计独立的恢复/轮换机制。`public` 可导出已有公钥；`sign` 使用新认证上下文，具体系统环境是否逐次提示需实机验证。工具本身不应从不可信 staging 更新。

## 准备和审阅

```sh
python3 -I tools/cloud_config_signer.py prepare \
  --repo /可信仓库路径 --fetch \
  --baseline /本机已核验的上次云配置.json \
  --output /安全目录/本次候选
```

`--fetch` 固定远端 `cloud-config-staging` 的完整 commit。若候选已经在本机，可改为 `--commit <完整提交号>`。工具只从 Git 对象读取配置及每项 HF 源码，不执行或导入这些源码，不依赖 staging 内的 Python 程序。它计算真实原始字节的 SHA-256，冻结输出并显示签名消息哈希和 `lastModified`。

审阅 `review.diff`、`hotfix-sources.txt` 和 `hotfix-sources/` 下所有相关源码文本，以及 `login_base64_page*.txt` 里的内嵌页面。显示的差异只和所提供的本地基线比较，工具不会自动证明基线可信；无基线时必须审阅完整候选。嵌入网页只作为文本输出，切勿在签名工具中执行它们。仅检查哈希不等于审阅代码。

`lastModified` 必须为正整数。存在基线且内容变化时必须大于基线；若不满足，回到候选生成阶段调整，再重新审阅。不得在签名后修改值。

## 签名和发布

```sh
python3 -I tools/cloud_config_signer.py sign /安全目录/本次候选 \
  --signer /绝对路径/cloud-sign-enclave \
  --public-key /安全位置/cloud-sign-public.pem
python3 -I tools/cloud_config_signer.py check /安全目录/本次候选 \
  --public-key /安全位置/cloud-sign-public.pem
```

人工核对后需输入完整签名消息 SHA-256，Secure Enclave 随后要求设备认证。完成后以提供的公钥重新验签最终写出的 `cloudRes.json`。把这个**完整文件**交给后续受控发布步骤；不得重新序列化、改变字段或由 CI 二次计算其内容。签名包仅是发布候选，是否向云端推送由用户决定。

本工具不应自动执行传入候选内容；签名只能说明对应字节经私钥授权，不能代替代码审阅。回滚须使用更高 `lastModified` 的新签名，而不是回传低版本旧文件。
