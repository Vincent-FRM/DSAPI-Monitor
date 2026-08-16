# DSAPI-Monitor（DeepSeek API 用量监控 · V1.4.0）

Windows 桌面悬浮工具，用来查看 DeepSeek API 充值余额、消费、请求次数和 Token 用量。

## 主要功能与改进

- API Key 与平台 userToken 使用当前 Windows 用户的 DPAPI 加密后保存，不再明文落盘。
- 配置迁移至 `%APPDATA%\DSAPI-Monitor`，程序目录不再携带真实凭证。
- 同一个 userToken 每轮只请求一次平台用量，再拆分其下各 API Key，减少请求和 HTTP 429。
- 单 Key 匹配失败时明确显示“无法可靠拆分”，不会再用账户总量冒充该 Key 的用量，也不会显示假零。
- 同一 DS账号 下多把 Key 的共享余额与账户用量只计一次；多个不同 userToken 会明确提示并分组汇总。
- 网络失败时只继承同一统计日内的上次成功数据，并标注“上次数据”；不会跨日沿用昨天结果。
- 增加窗口关闭按钮、精简右键菜单、固定右下角开关和凭证显示开关。
- 配置/状态采用原子写入，降低异常退出导致 JSON 损坏的风险。
- 托盘菜单新增 `language`，支持 `中文` 与 `English` 即时切换并记住选择。
- 托盘菜单提供“开机自启”开关，由用户自行决定是否启用，默认关闭。
- “固定右下角”启用时窗口不可拖动；取消固定后才能自由移动。
- 托盘菜单与窗口右键菜单新增“时间维度”，支持今日、昨日、近 7 天、近 30 天、本月和上月，启动时默认今日。
- 悬浮窗新增模型下拉菜单，模型名从平台用量响应动态获取，可筛选账号总量或单 Key 的模型用量，启动时默认“所有模型”。
- 账号/Key 与模型下拉框统一使用高对比度灰蓝底白字，并同步调整展开列表配色。
- 设置窗口添加账户后自动选中新账户；保存后左侧账户列表立即同步名称并保持当前选择。
- 设置窗口账户列表可显示 8 行，并支持准确删除高亮账户及通过“上移/下移”调整顺序。
- 主程序与系统托盘统一使用黑底、蓝色艺术字“DS”图标，与悬浮窗标题配色一致。

## 数据来源和稳定性

| 显示项 | 数据来源 | 稳定性 |
|---|---|---|
| 充值余额 | `GET https://api.deepseek.com/user/balance`（API Key） | DeepSeek 公开文档接口 |
| 所选范围消费、请求和 Tokens | `platform.deepseek.com/api/v0/usage/...`（userToken） | 官网网页内部接口，可能随网页改版变化 |
| 今日消费估算 | 所选时区当日开盘余额 − 当前余额 | 仅“今日”可用，不能按 Key 精确拆分 |

平台用量接口不是 DeepSeek 面向开发者承诺兼容的公开 API。若其失效，余额仍可显示，状态栏会给出错误；重新登录并更新 userToken 不能解决接口改版本身的问题。

## 使用方法

1. 运行 `DSAPI-Monitor.exe`。
2. 首次启动会打开设置窗口，填写 DS账号名称、API Key 名称和 API Key。
3. 如需官方当日消费、请求数和 Tokens，再填写平台 userToken。
4. 右键悬浮窗可立即刷新、设置账号信息、切换时间维度或打开配置目录；其他窗口功能保留在托盘菜单。
5. 如需登录 Windows 后自动运行，可在托盘菜单中勾选“开机自启”。
6. 在托盘菜单或窗口右键菜单的“时间维度”中选择统计范围；每次启动默认显示“今日”。
7. 在 Key 下方的模型菜单选择具体模型；消费、请求次数和 Tokens 会同步切换，余额始终显示当前账户余额。

托盘菜单的 `language` 子菜单可选择 `中文` 或 `English`。切换立即生效，下次启动继续使用上次选择。

窗口标题栏的 `×`：安装托盘支持时隐藏到托盘，否则直接退出。“固定右下角”启用时不能拖动窗口，取消勾选后可自由移动。

## 获取凭证

### API Key

登录 <https://platform.deepseek.com>，在 API Keys 页面创建。不要把真实 Key 放进压缩包、源码仓库、截图或问题报告。

### 平台 userToken

1. 用 Chrome 登录 <https://platform.deepseek.com/usage>。
2. 按 F12 打开开发者工具，进入 Console。
3. 执行：

   ```js
   JSON.parse(localStorage.getItem('userToken')).value
   ```

4. 将结果填入设置窗口。

userToken 等同于网页登录态。它也会通过 DPAPI 加密，但任何能以你的 Windows 账户运行的程序仍可能读取，因此不要在不可信电脑上使用。

## 配置与迁移

- 配置：`%APPDATA%\DSAPI-Monitor\dsm_config.json`
- 估算状态：`%APPDATA%\DSAPI-Monitor\dsm_state.json`
- 示例：`dsm_config.example.json`（不含凭证）

V1.0 程序目录旁若存在 `dsm_config.json`，V1.1 首次启动会读取它，把凭证加密迁移至 AppData，并将原文件替换为迁移位置提示。

DPAPI 密文绑定当前 Windows 用户，不能直接复制到另一台电脑或另一个 Windows 账户。换电脑时请在新电脑重新输入凭证。

## 统计口径

- “今日”按设置中的 GMT+8 或 UTC+0 自然日统计。
- “近 7 天”和“近 30 天”均包含今天；“本月”统计当月 1 日至今天，“上月”统计上一个完整自然月。
- 未配置 userToken 时只有“今日消费”可根据余额变化估算，历史区间需要平台 userToken。
- `by_api_key` 内部接口可用且 Key 脱敏 ID 匹配成功时，单 Key 视图显示该 Key 明细。
- 模型列表直接取自平台响应中的 `model` 字段，不固定模型名；DeepSeek 新增模型后无需修改程序即可显示。
- 如果平台响应没有提供模型字段，或多 Key 场景无法可靠拆分当前 Key，模型菜单只显示“所有模型”。
- 只有一把 Key 时，账户级结果可以作为该 Key 结果显示。
- 多把 Key 且无法可靠拆分时，单 Key 视图显示不可用，不静默回退。
- `[官方]`、`[≈估算]`、`[混合口径]` 和 `[上次数据]` 分别表示数据来源和新鲜度。

每轮请求数通常为：每把 API Key 1 次余额请求，加上每个唯一 userToken 2 次平台用量请求。若按 Key 接口无数据而回退经典接口，会按覆盖月份额外请求；失败重试也会增加请求数。

## 从源码运行

Python 3.8+：

```powershell
python deepseek_usage_monitor.py
```

托盘功能需要 Pillow 与 pystray。没有它们时窗口仍可使用，并可通过标题栏 `×` 退出。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试完全使用本地夹具，不会访问 DeepSeek，也不需要真实凭证。

## 构建 EXE

```powershell
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean --onefile --noconsole `
  --name DSAPI-Monitor --icon app_icon.ico `
  --add-data "app_icon_source_v2.png;." --version-file version_info.txt `
  deepseek_usage_monitor.py
```

也可以直接运行 `build.ps1`，它会先执行测试，再构建并输出 EXE 的 SHA-256。

不要把真实 `dsm_config.json` 或 `dsm_state.json` 复制进发布目录。公开分发前建议使用可信代码签名证书签署 EXE；没有证书的自签名不能建立 SmartScreen 信誉。

## 发布前检查

- 搜索发布目录，确认不存在 `sk-...`、userToken、`dsm_config.json` 和 `dsm_state.json`。
- 运行全部测试。
- 在空白 Windows 用户环境验证首次启动、DPAPI 保存、托盘和退出。
- 对最终 EXE 计算 SHA-256，并在下载页公布。
